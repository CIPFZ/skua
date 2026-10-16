"""scripts: guard against real site addresses, show names and media files entering the repo.

Rules (AGENT.md 3, CLAUDE.md):
- every ``http(s)://`` literal must point at an allowlisted host (loopback, RFC 2606 / RFC 6761
  reserved names, standards bodies, github.com for issue links); ``uv.lock`` may only reference
  PyPI hosts;
- a ``.m3u8`` reference may only be a bare file name or relative path, never ``host/…/x.m3u8``;
- no media files (by extension or magic bytes); images only under ``assets/icon/``;
- hosts and ``SKUA_DENYLIST`` terms found in the local ``.env.local`` must not appear anywhere
  (files or commit messages); matches are reported by index, never echoed.

Git is driven by the ``check_secrets.sh`` wrapper so that this module stays subprocess-free.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "127.0.0.1",
        "localhost",
        "::1",
        "datatracker.ietf.org",
        "www.rfc-editor.org",
        "github.com",
    }
)
# Host equals the suffix or ends with "." + suffix (RFC 2606 and RFC 6761 reserved names).
ALLOWED_SUFFIXES: tuple[str, ...] = (
    "example.com",
    "example.org",
    "example.net",
    "test",
    "example",
    "invalid",
    "localhost",
)
LOCKFILE_HOSTS: frozenset[str] = frozenset({"pypi.org", "files.pythonhosted.org"})
LOCKFILES: frozenset[str] = frozenset({"uv.lock"})

# Subset of the Public Suffix List (publicsuffix.org) with more than one label, so
# that denylist expansion stops at suffix + 1 and never blacklists a shared hosting suffix.
# Embedded instead of a dependency (ADR-001: no new deps); extend when a real source needs it.
MULTI_LABEL_SUFFIXES: frozenset[str] = frozenset(
    {
        # ICANN second-level registries
        "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
        "com.hk", "net.hk", "org.hk", "com.tw", "net.tw", "org.tw", "com.mo",
        "co.jp", "ne.jp", "or.jp", "ac.jp", "co.kr", "or.kr", "ne.kr",
        "com.sg", "com.my", "co.th", "in.th", "com.vn", "com.ph", "co.id", "co.in",
        "co.uk", "org.uk", "ac.uk", "com.au", "net.au", "org.au", "co.nz",
        "com.br", "com.mx", "com.ar", "com.tr", "com.ru", "co.za",
        # private-section shared hosting suffixes
        "github.io", "gitlab.io", "pages.dev", "workers.dev", "r2.dev",
        "netlify.app", "vercel.app", "web.app", "firebaseapp.com", "appspot.com",
        "herokuapp.com", "blogspot.com", "cloudfront.net", "azurewebsites.net",
        "azureedge.net", "s3.amazonaws.com", "fly.dev", "onrender.com",
    }
)  # fmt: skip

IMAGE_DIR = "assets/icon/"
MEDIA_EXTS: frozenset[str] = frozenset(
    {
        ".ts", ".m4s", ".mp4", ".m4v", ".mkv", ".mov", ".webm", ".flv", ".avi",
        ".aac", ".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus",
    }
)  # fmt: skip
IMAGE_EXTS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".icns", ".ico", ".tif", ".tiff"}
)

MIN_DENY_TERM_LEN = 3
MAGIC_PROBE_BYTES = 512
TS_PACKET = 188  # ISO/IEC 13818-1 transport packet size

URL_RE = re.compile(r"https?://(\[[^\]\s]*\]|[^\s/\"'<>()\[\]`\\?#]+)", re.IGNORECASE)
M3U8_TOKEN_RE = re.compile(r"[^\s\"'<>()\[\]`]*\.m3u8\b", re.IGNORECASE)
HOSTLIKE_RE = re.compile(r"^(?://)?([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)(?::\d+)?/")
TEMPLATE_CHARS = frozenset("{}<>$%")


@dataclass(frozen=True)
class Violation:
    """One rule hit, printed as ``where:line: rule``."""

    where: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.where}:{self.line}: {self.rule}"


def normalize_host(authority: str) -> str:
    """Strip userinfo, port and IPv6 brackets from a URL authority; lowercase."""
    host = authority.rsplit("@", 1)[-1]
    if host.startswith("["):
        return host[1:].split("]", 1)[0].lower()
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".").lower()


def is_template(host: str) -> bool:
    """A host built from a placeholder (``{host}``, ``${HOST}``, ``<host>``) is not a literal."""
    return not host or any(c in TEMPLATE_CHARS for c in host)


def is_allowed_host(host: str) -> bool:
    """True for hosts that may appear in source, tests and docs."""
    if host in ALLOWED_HOSTS or is_template(host):
        return True
    return any(host == s or host.endswith("." + s) for s in ALLOWED_SUFFIXES)


def check_text(where: str, text: str, *, lockfile: bool = False) -> list[Violation]:
    """Check URL literals and ``.m3u8`` references in one text."""
    hits: list[Violation] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in URL_RE.finditer(line):
            host = normalize_host(m.group(1))
            ok = host in LOCKFILE_HOSTS if lockfile else is_allowed_host(host)
            if not ok:
                hits.append(Violation(where, lineno, f"URL with non-allowlisted host '{host}'"))
        if lockfile:
            continue
        for m in M3U8_TOKEN_RE.finditer(line):
            token = m.group(0)
            if "://" in token:
                continue  # already judged by the URL rule
            hm = HOSTLIKE_RE.match(token)
            if hm and not is_allowed_host(normalize_host(hm.group(1))):
                hits.append(Violation(where, lineno, "m3u8 reference with a host part"))
    return hits


def _is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def sniff_media(head: bytes, *, size: int) -> str | None:
    """Return a media kind if ``head`` (first bytes of a file) looks like audio/video/image."""
    if head[4:8] in (b"ftyp", b"styp", b"moof", b"moov"):
        return "ISO-BMFF (mp4/fmp4)"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "Matroska/WebM"
    if head[:4] == b"RIFF" and head[8:12] in (b"WAVE", b"AVI "):
        return "RIFF wav/avi"
    if head[:3] in (b"ID3", b"FLV") or head[:4] in (b"fLaC", b"OggS"):
        return "audio/flv container"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0:
        return "ADTS AAC"
    if head[:8] == b"\x89PNG\r\n\x1a\n" or head[:3] == b"\xff\xd8\xff" or head[:4] == b"GIF8":
        return "image"
    if (
        size >= 2 * TS_PACKET + 1
        and len(head) > 2 * TS_PACKET
        and head[0] == head[TS_PACKET] == head[2 * TS_PACKET] == 0x47
        and not _is_text(head)
    ):
        return "MPEG-TS"
    return None


def check_media(rel_path: str, head: bytes, *, size: int) -> list[Violation]:
    """Reject media by extension or magic bytes; images are allowed only under assets/icon/."""
    ext = Path(rel_path).suffix.lower()
    in_icons = rel_path.startswith(IMAGE_DIR)
    if ext in MEDIA_EXTS:
        return [Violation(rel_path, 0, f"media file extension '{ext}'")]
    if ext in IMAGE_EXTS and not in_icons:
        return [Violation(rel_path, 0, f"image outside {IMAGE_DIR}")]
    kind = sniff_media(head, size=size)
    if kind == "image" and in_icons:
        return []
    if kind is not None:
        return [Violation(rel_path, 0, f"content looks like media: {kind}")]
    return []


def registrable_domain(host: str) -> str:
    """Public suffix + 1 label (``a.b.example.com`` -> ``example.com``, ``x.github.io`` stays).

    Uses the embedded ``MULTI_LABEL_SUFFIXES`` subset of the Public Suffix List; any other host
    falls back to "last label is the suffix". IP literals are returned unchanged.
    """
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    suffix_len = 1
    for n in range(len(labels) - 1, 1, -1):
        if ".".join(labels[-n:]) in MULTI_LABEL_SUFFIXES:
            suffix_len = n
            break
    return ".".join(labels[-(suffix_len + 1) :])


def _hosts_in(value: str) -> Iterable[str]:
    for m in URL_RE.finditer(value):
        host = normalize_host(m.group(1))
        if is_template(host) or is_allowed_host(host):
            continue
        yield host
        parent = registrable_domain(host)
        if parent != host and not is_allowed_host(parent):
            yield parent


def load_denylist(env_text: str) -> list[str]:
    """Collect hosts of every URL in ``.env.local`` plus explicit ``SKUA_DENYLIST`` terms."""
    terms: list[str] = []
    for raw in env_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip().strip("'\"")
        if key.strip() == "SKUA_DENYLIST":
            terms.extend(t.strip() for t in re.split(r"[,|]", value))
        terms.extend(_hosts_in(value))
    seen: set[str] = set()
    out: list[str] = []
    for t in terms:
        low = t.lower()
        if len(low) >= MIN_DENY_TERM_LEN and low not in seen:
            seen.add(low)
            out.append(low)
    return out


def check_denylist(where: str, text: str, terms: Sequence[str]) -> list[Violation]:
    """Case-insensitive substring search; the term itself is never printed."""
    hits: list[Violation] = []
    if not terms:
        return hits
    for lineno, line in enumerate(text.lower().splitlines(), start=1):
        for idx, term in enumerate(terms, start=1):
            if term in line:
                hits.append(Violation(where, lineno, f"denylisted term #{idx} from .env.local"))
    return hits


def check_file(repo: Path, rel_path: str, terms: Sequence[str]) -> list[Violation]:
    """Run every file-level rule on one repo-relative path."""
    path = repo / rel_path
    if not path.is_file():
        return []  # deleted in the working tree
    data = path.read_bytes()
    hits = check_media(rel_path, data[:MAGIC_PROBE_BYTES], size=len(data))
    if hits or not _is_text(data):
        return hits
    text = data.decode("utf-8")
    hits.extend(check_text(rel_path, text, lockfile=rel_path in LOCKFILES))
    hits.extend(check_denylist(rel_path, text, terms))
    return hits


def check_messages(blob: str, terms: Sequence[str]) -> list[Violation]:
    """``blob`` is NUL-separated ``<short-hash>\\n<message>`` records from ``git log -z``."""
    hits: list[Violation] = []
    for record in blob.split("\0"):
        if not record.strip():
            continue
        rev, _, message = record.lstrip("\n").partition("\n")
        where = f"commit {rev}"
        hits.extend(check_text(where, message))
        hits.extend(check_denylist(where, message, terms))
    return hits


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--paths-file", type=Path, required=True, help="NUL-separated paths")
    parser.add_argument("--messages-file", type=Path, help="git log -z --format=%%h%%n%%B output")
    args = parser.parse_args(argv)

    repo: Path = args.repo.resolve()
    env = repo / ".env.local"
    terms = load_denylist(env.read_text(encoding="utf-8")) if env.is_file() else []

    hits: list[Violation] = []
    for rel in args.paths_file.read_text(encoding="utf-8").split("\0"):
        if rel:
            hits.extend(check_file(repo, rel, terms))
    if args.messages_file is not None:
        hits.extend(check_messages(args.messages_file.read_text(encoding="utf-8"), terms))

    for hit in hits:
        print(hit, file=sys.stderr)
    if hits:
        print(f"check_secrets: {len(hits)} violation(s)", file=sys.stderr)
        return 1
    print(f"check_secrets: ok ({len(terms)} local denylist term(s) applied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
