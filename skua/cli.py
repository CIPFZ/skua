"""skua.cli: command-line entry points — orchestration only, no business logic (AGENT.md 4.2).

``skua scan <url|path> --out <skiplist.json>`` runs adapter -> evidence A -> M0 rule decoder.
stdout carries JSON only (the SkipList, or the Timeline with ``--json-events``); timings and
messages go to stderr. Exit codes are a contract for the mpv script and the proxy (SPEC 3.8).
"""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from skua.adapter.fetch import FetchError, default_cache_root, normalize_source
from skua.adapter.hls import AesKeyError, HlsSource, open_hls
from skua.adapter.playlist import UnsupportedPlaylistError
from skua.adapter.proc import redact_urls
from skua.evidence.rule_decoder import rule_decode
from skua.evidence.structural import structural_evidence
from skua.schema import SkipList, to_json

log = logging.getLogger("skua.cli")


class ExitCode(enum.IntEnum):
    """SPEC 3.8: lets callers tell "no ads" from "could not scan"."""

    OK = 0
    USAGE = 1  # bad arguments
    UNSUPPORTED = 2  # fMP4, SAMPLE-AES, BYTERANGE, live, unknown KEY METHOD, unsupported scheme
    INTEGRITY = 3  # AES key / IV integrity (CRC escalation), malformed key
    DOWNLOAD = 4  # playlist, key or segment could not be fetched
    INTERNAL = 5


class CliError(Exception):
    """A failure with a known exit code; the message is printed (URL-redacted) to stderr."""

    def __init__(self, code: ExitCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _usage_error_type() -> type[Exception]:
    # typer 0.27 vendors click; its UsageError is reached through the public BadParameter.
    for cls in typer.BadParameter.__mro__:
        if cls.__name__ == "UsageError" and issubclass(cls, Exception):
            return cls
    return typer.BadParameter


_USAGE_ERROR = _usage_error_type()

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__.split("\n")[0])


@app.callback()
def _root() -> None:
    """Skua: ad segment detection and skipping (SPEC.md)."""


def parse_headers(values: Sequence[str]) -> dict[str, str]:
    """``--header 'Name: value'`` (repeatable) -> dict. Never logged or stored (SPEC 3.1)."""
    headers: dict[str, str] = {}
    for raw in values:
        name, sep, value = raw.partition(":")
        if not sep or not name.strip() or any(c in name for c in " \t\r\n"):
            raise CliError(ExitCode.USAGE, "--header must look like 'Name: value'")
        headers[name.strip()] = value.strip()
    return headers


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


async def _open(src: str, cache: Path, headers: dict[str, str]) -> HlsSource:
    try:
        return await open_hls(src, cache_root=cache, headers=headers)
    except UnsupportedPlaylistError as exc:
        raise CliError(ExitCode.UNSUPPORTED, str(exc)) from None
    except AesKeyError as exc:
        raise CliError(ExitCode.INTEGRITY, str(exc)) from None
    except FetchError as exc:
        raise CliError(ExitCode.DOWNLOAD, str(exc)) from None


def _check_source(src: str) -> None:
    try:
        normalized = normalize_source(src)
    except ValueError as exc:
        raise CliError(ExitCode.UNSUPPORTED, f"unsupported source: {exc}") from None
    if not normalized.startswith(("http://", "https://")) and not Path(normalized).is_file():
        raise CliError(ExitCode.USAGE, "source file does not exist")


@app.command()
def scan(
    src: Annotated[str, typer.Argument(help="HLS playlist URL or local path (master or media).")],
    out: Annotated[Path, typer.Option("--out", help="Where to write the SkipList JSON.")],
    json_events: Annotated[
        bool, typer.Option("--json-events", help="Print the full Timeline to stdout instead.")
    ] = False,
    header: Annotated[
        list[str] | None, typer.Option("--header", help="Request header 'Name: value'.")
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option("--cache-dir", help="Segment cache root (default $SKUA_CACHE_DIR)."),
    ] = None,
) -> None:
    """Detect ad segments of one HLS source and write its SkipList (M0: evidence A only)."""
    started = time.monotonic()
    _check_source(src)
    headers = parse_headers(header or [])
    hls = asyncio.run(_open(src, cache_dir or default_cache_root(), headers))
    evidence_started = time.monotonic()
    segments = rule_decode(hls.timeline, structural_evidence(hls.timeline))
    evidence_s = time.monotonic() - evidence_started
    skiplist = SkipList(hls.media_id, 1, segments)
    _write_atomic(out, to_json(skiplist, indent=2))
    print(to_json(hls.timeline if json_events else skiplist), file=sys.stdout)
    total = time.monotonic() - started
    shots = len(hls.timeline.shots)
    cache = "cold" if hls.downloaded else "hot"
    print(
        f"skua scan: {shots} segments, {hls.downloaded} downloaded ({cache} cache); "
        f"fetch {hls.fetch_s:.3f} s, analysis {hls.analysis_s:.3f} s, "
        f"evidence {evidence_s:.3f} s; hot-path {hls.analysis_s + evidence_s:.3f} s, "
        f"total {total:.3f} s; {len(segments)} ad segment(s)",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return its exit code (SPEC 3.8)."""
    logging.basicConfig(level=os.environ.get("SKUA_LOG_LEVEL", "WARNING"), stream=sys.stderr)
    try:
        app(args=list(argv) if argv is not None else None, prog_name="skua", standalone_mode=False)
    except CliError as exc:
        print(f"skua: {redact_urls(str(exc))}", file=sys.stderr)
        return int(exc.code)
    except typer.Exit as exc:
        return int(exc.exit_code)
    except typer.Abort:
        return int(ExitCode.USAGE)
    except _USAGE_ERROR as exc:
        show = getattr(exc, "show", None)
        if callable(show):
            show()  # usage line and the error, like click's standalone mode
        return int(ExitCode.USAGE)
    except Exception as exc:  # anything unexpected is an internal error, never a silent "no ads"
        log.debug("internal error", exc_info=True)
        print(
            f"skua: internal error: {type(exc).__name__}: {redact_urls(str(exc))}", file=sys.stderr
        )
        return int(ExitCode.INTERNAL)
    return int(ExitCode.OK)


def run() -> None:
    """Console-script entry point."""
    raise SystemExit(main())
