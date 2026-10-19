"""skua.adapter.proc: the only place allowed to start external processes (AGENT.md 5.3).

Commands are an allowlist (``AllowedCommand``), arguments are always a list (never a shell
string, never ``shell=True``), stdin is closed so ffmpeg cannot block on an interactive prompt,
and nothing derived from the arguments or stderr is logged unredacted, because ffmpeg echoes
input URLs and those must never reach a log (AGENT.md 5.6).
"""

from __future__ import annotations

import enum
import hashlib
import logging
import re
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("skua.adapter")


class AllowedCommand(enum.Enum):
    """External programs Skua may run. Adding a member requires an ADR (AGENT.md 9)."""

    FFMPEG = "ffmpeg"
    FFPROBE = "ffprobe"


@dataclass(frozen=True)
class ProcConfig:
    """Implementation constants, not SPEC or skip-policy parameters."""

    timeout_s: float = 600.0  # upper bound for one invocation; callers pass less when they can
    stderr_tail_lines: int = 20  # how much of stderr an error message keeps


CONFIG = ProcConfig()

_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
_HOST_RE = re.compile(r"://(?:[^@/\s]*@)?(\[[^\]]*\]|[^:/\s?#]*)")


@dataclass(frozen=True)
class ProcResult:
    """Outcome of one external process run."""

    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_s: float


class ProcError(RuntimeError):
    """An allowed command could not be started, timed out, or exited non-zero."""

    def __init__(self, cmd: AllowedCommand, message: str, result: ProcResult | None = None):
        self.cmd = cmd
        self.result = result
        tail = ""
        if result is not None and result.stderr:
            lines = result.stderr.decode("utf-8", "replace").splitlines()
            tail = "\n" + "\n".join(lines[-CONFIG.stderr_tail_lines :])
        super().__init__(redact_urls(f"{cmd.value}: {message}{tail}"))


def host_hash(url: str) -> str:
    """Stable 8-hex digest of a URL's host, the only form of a URL that may be logged."""
    m = _HOST_RE.search(url)
    host = (m.group(1) if m else "").lower()
    return hashlib.sha256(host.encode()).hexdigest()[:8]


def redact_urls(text: str) -> str:
    """Replace every ``scheme://...`` token with ``<url:hosthash>``."""
    return _URL_RE.sub(lambda m: f"<url:{host_hash(m.group(0))}>", text)


def _check_args(args: Sequence[str]) -> list[str]:
    if isinstance(args, str | bytes):
        raise TypeError("args must be a list of arguments, not a single command string")
    out = list(args)
    for i, a in enumerate(out):
        if not isinstance(a, str):
            raise TypeError(f"args[{i}] must be str, got {type(a).__name__}")
        if "\x00" in a:
            raise ValueError(f"args[{i}] contains a NUL byte")
    return out


def executable(cmd: AllowedCommand) -> Path:
    """Resolve ``cmd`` on PATH; raise ProcError if it is not installed."""
    found = shutil.which(cmd.value)
    if found is None:
        raise ProcError(cmd, "not found on PATH")
    return Path(found)


def run(
    cmd: AllowedCommand,
    args: Sequence[str],
    *,
    timeout_s: float | None = None,
    cwd: Path | None = None,
    check: bool = True,
) -> ProcResult:
    """Run an allowed command with ``args``; raise ProcError on failure when ``check``."""
    if not isinstance(cmd, AllowedCommand):
        raise TypeError(f"cmd must be an AllowedCommand, got {cmd!r}")
    argv = [str(executable(cmd)), *_check_args(args)]
    limit = CONFIG.timeout_s if timeout_s is None else timeout_s
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=limit,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = ProcResult(-1, exc.stdout or b"", exc.stderr or b"", time.monotonic() - started)
        raise ProcError(cmd, f"timed out after {limit:g}s", partial) from None
    except OSError as exc:
        raise ProcError(cmd, f"could not start: {exc.strerror}") from None
    result = ProcResult(
        completed.returncode, completed.stdout, completed.stderr, time.monotonic() - started
    )
    # Arguments are deliberately not logged: they may carry stream URLs.
    log.debug(
        "external process finished",
        extra={
            "cmd": cmd.value,
            "argc": len(argv) - 1,
            "returncode": result.returncode,
            "elapsed_s": round(result.elapsed_s, 3),
        },
    )
    if check and result.returncode != 0:
        raise ProcError(cmd, f"exited with status {result.returncode}", result)
    return result
