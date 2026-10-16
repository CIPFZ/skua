"""scripts: AST-based guard that keeps decoding and external processes inside skua/adapter.

Rules (AGENT.md 5.3, CLAUDE.md):
- only ``skua/adapter/decode.py`` may import ``av``;
- only ``skua/adapter/proc.py`` may import ``subprocess``;
- nobody may use other process-spawning APIs, ``shell=True`` or dynamic imports of banned modules.

AST is used instead of grep so multi-line imports, aliases and ``from x import y`` are all seen.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

SCAN_DIRS: tuple[str, ...] = ("skua", "scripts", "tests")

# Module root -> the only repo-relative file allowed to import it (None: nobody may).
BANNED_MODULES: dict[str, str | None] = {
    "av": "skua/adapter/decode.py",
    "subprocess": "skua/adapter/proc.py",
    "pty": None,
    "pexpect": None,
    "sh": None,
    "plumbum": None,
    "ffmpeg": None,
    "moviepy": None,
    "decord": None,
    "imageio_ffmpeg": None,
}

BANNED_OS_FUNCS: frozenset[str] = frozenset(
    {"system", "popen", "posix_spawn", "posix_spawnp", "fork", "forkpty"}
)
BANNED_OS_PREFIXES: tuple[str, ...] = ("spawn", "exec")
BANNED_ASYNCIO_FUNCS: frozenset[str] = frozenset(
    {"create_subprocess_exec", "create_subprocess_shell"}
)
DYNAMIC_IMPORTERS: frozenset[str] = frozenset({"import_module", "__import__"})


@dataclass(frozen=True)
class Violation:
    """One rule hit, printed as ``path:line: rule``."""

    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


def _root(module: str) -> str:
    return module.split(".", 1)[0]


def _banned_for(module: str, rel_path: str) -> bool:
    root = _root(module)
    if root not in BANNED_MODULES:
        return False
    return BANNED_MODULES[root] != rel_path


def _is_banned_os_name(name: str) -> bool:
    return name in BANNED_OS_FUNCS or name.startswith(BANNED_OS_PREFIXES)


def _attr_chain(node: ast.expr) -> list[str] | None:
    """Return ``["os", "system"]`` for ``os.system``; None if not a plain dotted name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.hits: list[Violation] = []
        # Local alias -> canonical dotted name, e.g. {"o": "os", "spawn": "os.spawnv"}.
        self.aliases: dict[str, str] = {}

    def _hit(self, node: ast.AST, rule: str) -> None:
        self.hits.append(Violation(self.rel_path, getattr(node, "lineno", 0), rule))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _banned_for(alias.name, self.rel_path):
                self._hit(node, f"import of banned module '{alias.name}'")
            self.aliases[alias.asname or _root(alias.name)] = (
                alias.name if alias.asname else _root(alias.name)
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level == 0 and _banned_for(module, self.rel_path):
            self._hit(node, f"import from banned module '{module}'")
        for alias in node.names:
            if module in ("os", "asyncio") and alias.name == "*":
                self._hit(node, f"star import from '{module}' hides process-spawning names")
            if module == "os" and _is_banned_os_name(alias.name):
                self._hit(node, f"import of process-spawning 'os.{alias.name}'")
            if module == "asyncio" and alias.name in BANNED_ASYNCIO_FUNCS:
                self._hit(node, f"import of process-spawning 'asyncio.{alias.name}'")
            self.aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
        self.generic_visit(node)

    def _canonical(self, func: ast.expr) -> str | None:
        chain = _attr_chain(func)
        if chain is None:
            return None
        head = self.aliases.get(chain[0], chain[0])
        return ".".join([head, *chain[1:]])

    def visit_Call(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if (
                kw.arg == "shell"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is not False
            ):
                self._hit(node, "shell=True is forbidden")
            elif kw.arg == "shell" and not isinstance(kw.value, ast.Constant):
                self._hit(node, "non-literal shell= argument is forbidden")
        name = self._canonical(node.func)
        if name is not None:
            parts = name.split(".")
            if parts[0] == "os" and len(parts) == 2 and _is_banned_os_name(parts[1]):
                self._hit(node, f"call to process-spawning '{name}'")
            if parts[0] == "asyncio" and len(parts) == 2 and parts[1] in BANNED_ASYNCIO_FUNCS:
                self._hit(node, f"call to process-spawning '{name}'")
            if parts[-1] in DYNAMIC_IMPORTERS:
                self._check_dynamic_import(node, name)
        self.generic_visit(node)

    def _check_dynamic_import(self, node: ast.Call, name: str) -> None:
        if not node.args:
            return
        target = node.args[0]
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            if _banned_for(target.value, self.rel_path):
                self._hit(node, f"dynamic import of banned module '{target.value}' via {name}")
        else:
            self._hit(node, f"dynamic import with non-literal module name via {name}")


def check_source(rel_path: str, source: str) -> list[Violation]:
    """Check one Python source text; ``rel_path`` is repo-relative with forward slashes."""
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError as exc:
        return [Violation(rel_path, exc.lineno or 0, f"syntax error: {exc.msg}")]
    visitor = _Visitor(rel_path)
    visitor.visit(tree)
    return visitor.hits


def iter_python_files(repo: Path, dirs: Iterable[str] = SCAN_DIRS) -> Iterable[Path]:
    """Yield every ``.py`` file under the scanned top-level directories."""
    for d in dirs:
        base = repo / d
        if base.is_dir():
            yield from sorted(p for p in base.rglob("*.py") if ".venv" not in p.parts)


def check_repo(repo: Path) -> list[Violation]:
    """Check all scanned files in ``repo``."""
    hits: list[Violation] = []
    for path in iter_python_files(repo):
        rel = path.relative_to(repo).as_posix()
        hits.extend(check_source(rel, path.read_text(encoding="utf-8")))
    return hits


def main(argv: Sequence[str]) -> int:
    repo = Path(argv[0]).resolve() if argv else Path.cwd()
    hits = check_repo(repo)
    for hit in hits:
        print(hit, file=sys.stderr)
    if hits:
        print(f"check_imports: {len(hits)} violation(s)", file=sys.stderr)
        return 1
    print("check_imports: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
