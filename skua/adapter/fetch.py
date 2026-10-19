"""skua.adapter.fetch: media_id and concurrent, cached, resumable segment download (SPEC 2, 3.1).

Segments land in ``<cache root>/<media_id>/seg<N>.ts``. A download first goes to ``.part`` and is
renamed atomically when complete, so a rerun skips finished files and resumes an interrupted one
with an HTTP Range request. Request headers (``--header``) are passed through but never stored or
logged, and URLs only ever reach logs and errors as a host hash (AGENT.md 5.6).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from urllib.parse import unquote, urlsplit, urlunsplit

import aiohttp

from skua.adapter.playlist import PlaylistSegment
from skua.adapter.proc import host_hash, redact_urls

log = logging.getLogger("skua.adapter")

T = TypeVar("T")

CACHE_ENV = "SKUA_CACHE_DIR"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_RETRY_STATUS = frozenset({408, 429})  # plus every 5xx


@dataclass(frozen=True)
class FetchConfig:
    """Download parameters; SPEC 3.1 where noted, the rest are implementation constants."""

    concurrency: int = 8  # SPEC 3.1: 8 concurrent downloads
    retries: int = 3  # SPEC 3.1: retry 3 times with exponential backoff
    backoff_s: float = 0.5  # first retry delay, doubled per attempt
    timeout_s: float = 60.0  # per request
    chunk_bytes: int = 1 << 16
    media_id_hex_len: int = 16  # SPEC 2: sha256 prefix


CONFIG = FetchConfig()


class FetchError(RuntimeError):
    """A resource could not be fetched after all retries (message is URL-redacted)."""


@dataclass(frozen=True)
class FetchedSegment:
    """A segment available on local disk."""

    index: int
    uri: str  # as written in the playlist
    path: Path
    bytes: int
    cached: bool  # already complete before this run (or a local source, never downloaded)


# --- media_id --------------------------------------------------------------------------------


def _is_http(src: str) -> bool:
    return urlsplit(src).scheme.lower() in _DEFAULT_PORTS


def normalize_source(src: str) -> str:
    """Canonical form of a playlist location (SPEC 2).

    URLs: scheme and host lowercased, default port, userinfo, query and fragment dropped, empty
    path becomes ``/``. Local files (plain or ``file:`` paths): absolute, normalized path.
    """
    parts = urlsplit(src)
    scheme = parts.scheme.lower()
    if scheme in _DEFAULT_PORTS:
        host = parts.hostname or ""
        if not host:
            raise ValueError("URL without a host")
        if ":" in host:
            host = f"[{host}]"  # IPv6 literal
        port = parts.port  # raises ValueError for a malformed port
        netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
        return urlunsplit((scheme, netloc, parts.path or "/", "", ""))
    if scheme == "file":
        return os.path.abspath(unquote(parts.path))
    if scheme and len(scheme) > 1:  # one letter is a Windows drive, not a scheme
        raise ValueError(f"unsupported source scheme {scheme!r}")
    return os.path.abspath(os.path.expanduser(src))


def media_id(src: str, config: FetchConfig = CONFIG) -> str:
    """sha256 of the normalized source, first 16 hex chars (SPEC 2). Needs no download."""
    digest = hashlib.sha256(normalize_source(src).encode("utf-8")).hexdigest()
    return digest[: config.media_id_hex_len]


def default_cache_root() -> Path:
    """``$SKUA_CACHE_DIR`` or ``./cache`` (gitignored)."""
    return Path(os.environ.get(CACHE_ENV) or "cache")


def segment_path(cache_dir: Path, index: int) -> Path:
    """``cache/<media_id>/seg<N>.ts`` (SPEC 3.1)."""
    return cache_dir / f"seg{index}.ts"


def _local_path(uri: str) -> Path:
    parts = urlsplit(uri)
    return Path(unquote(parts.path) if parts.scheme.lower() == "file" else uri)


# --- HTTP ------------------------------------------------------------------------------------


class _RetryableError(Exception):
    """A transient failure: worth another attempt."""


async def _with_retries(
    what: str,
    url: str,
    attempt_fn: Callable[[], Awaitable[T]],
    config: FetchConfig,
) -> T:
    last: BaseException | None = None
    for attempt in range(config.retries + 1):
        if attempt:
            await asyncio.sleep(config.backoff_s * 2 ** (attempt - 1))
        try:
            return await attempt_fn()
        except (_RetryableError, aiohttp.ClientError, TimeoutError) as exc:
            last = exc
            log.info(
                "fetch attempt failed",
                extra={"what": what, "host": host_hash(url), "attempt": attempt + 1},
            )
    reason = type(last).__name__ if last is not None else "unknown"
    detail = str(last) if isinstance(last, _RetryableError) else reason
    raise FetchError(
        f"{what}: giving up after {config.retries + 1} attempts ({redact_urls(detail)}) "
        f"for {redact_urls(url)}"
    )


def _check_status(what: str, url: str, status: int) -> None:
    if status >= 500 or status in _RETRY_STATUS:
        raise _RetryableError(f"HTTP {status}")
    if status >= 400:
        raise FetchError(f"{what}: HTTP {status} for {redact_urls(url)}")


async def fetch_bytes(
    url: str,
    session: aiohttp.ClientSession,
    *,
    headers: Mapping[str, str] | None = None,
    what: str = "resource",
    config: FetchConfig = CONFIG,
) -> bytes:
    """GET a small resource (playlist, key) into memory, with retries; local paths are read."""
    if not _is_http(url):
        return _local_path(url).read_bytes()
    timeout = aiohttp.ClientTimeout(total=config.timeout_s)

    async def attempt() -> bytes:
        async with session.get(url, headers=dict(headers or {}), timeout=timeout) as resp:
            _check_status(what, url, resp.status)
            return await resp.read()

    return await _with_retries(what, url, attempt, config)


async def _download(
    url: str,
    dest: Path,
    session: aiohttp.ClientSession,
    headers: Mapping[str, str],
    what: str,
    config: FetchConfig,
) -> None:
    part = dest.with_name(dest.name + ".part")
    timeout = aiohttp.ClientTimeout(total=config.timeout_s)

    async def attempt() -> None:
        have = part.stat().st_size if part.exists() else 0
        request_headers = dict(headers)
        if have:
            request_headers["Range"] = f"bytes={have}-"
        async with session.get(url, headers=request_headers, timeout=timeout) as resp:
            if resp.status == 416 and have:
                part.unlink()  # our partial file is bogus for this resource: start over
                raise _RetryableError("HTTP 416 on resume")
            _check_status(what, url, resp.status)
            resume = have and resp.status == 206 and _range_start(resp) == have
            expected = resp.content_length
            written = 0
            with part.open("ab" if resume else "wb") as fh:
                async for chunk in resp.content.iter_chunked(config.chunk_bytes):
                    fh.write(chunk)
                    written += len(chunk)
            # Defensive: aiohttp 3.14 already raises ClientPayloadError on a short body.
            if expected is not None and written != expected:
                raise _RetryableError(f"short body: {written} of {expected} bytes")
        os.replace(part, dest)

    await _with_retries(what, url, attempt, config)


def _range_start(resp: aiohttp.ClientResponse) -> int | None:
    value = resp.headers.get("Content-Range", "")
    # "bytes <start>-<end>/<total>" (RFC 9110 14.4)
    try:
        unit, rest = value.split(" ", 1)
        return int(rest.split("-", 1)[0]) if unit == "bytes" else None
    except ValueError:
        return None


async def iter_segments(
    segments: Sequence[PlaylistSegment],
    cache_dir: Path,
    *,
    session: aiohttp.ClientSession | None = None,
    headers: Mapping[str, str] | None = None,
    config: FetchConfig = CONFIG,
) -> AsyncIterator[FetchedSegment]:
    """Fetch with ``config.concurrency`` workers and yield segments in playlist order as soon as
    each prefix is on disk, so analysis can start before the download ends (SPEC 3.1)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    own_session = session is None
    client = session or aiohttp.ClientSession()
    gate = asyncio.Semaphore(config.concurrency)
    hdrs = dict(headers or {})

    async def one(seg: PlaylistSegment) -> FetchedSegment:
        if not _is_http(seg.absolute_uri):
            path = _local_path(seg.absolute_uri)
            return FetchedSegment(seg.index, seg.uri, path, path.stat().st_size, True)
        dest = segment_path(cache_dir, seg.index)
        if dest.exists():
            return FetchedSegment(seg.index, seg.uri, dest, dest.stat().st_size, True)
        async with gate:
            started = time.monotonic()
            await _download(seg.absolute_uri, dest, client, hdrs, f"segment {seg.index}", config)
        size = dest.stat().st_size
        log.info(
            "segment fetched",
            extra={
                "segment": seg.index,
                "host": host_hash(seg.absolute_uri),
                "bytes": size,
                "elapsed_s": round(time.monotonic() - started, 3),
            },
        )
        return FetchedSegment(seg.index, seg.uri, dest, size, False)

    tasks = [asyncio.create_task(one(seg)) for seg in segments]
    try:
        for task in tasks:
            yield await task
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if own_session:
            await client.close()


async def fetch_segments(
    segments: Sequence[PlaylistSegment],
    cache_dir: Path,
    *,
    session: aiohttp.ClientSession | None = None,
    headers: Mapping[str, str] | None = None,
    config: FetchConfig = CONFIG,
) -> list[FetchedSegment]:
    """Fetch every segment; see ``iter_segments``."""
    return [
        s
        async for s in iter_segments(
            segments, cache_dir, session=session, headers=headers, config=config
        )
    ]
