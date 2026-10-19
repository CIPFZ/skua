"""tests: shared fixtures; every test runs with SKUA_WORK_DIR pointing at a fresh temp dir."""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from aiohttp import web
from make_synth import H264_ENCODER, PROBED_ENCODERS, SynthResult, ensure_synth, probe_encoder

# CI sets this so the h264 group can never be silently skipped there (AGENT.md 6.1).
REQUIRE_H264_ENV = "SKUA_REQUIRE_H264"


@pytest.fixture(autouse=True)
def skua_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep work/ and cache/ out of the repo (AGENT.md 2.6)."""
    work = tmp_path / "skua_work"
    work.mkdir()
    monkeypatch.setenv("SKUA_WORK_DIR", str(work))
    monkeypatch.setenv("SKUA_CACHE_DIR", str(tmp_path / "skua_cache"))
    return work


def pytest_report_header() -> str:
    probed = ", ".join(
        f"{n}={'ok' if probe_encoder(n) else 'unavailable'}" for n in PROBED_ENCODERS
    )
    return f"skua synth encoders (trial encode): {probed}"


@pytest.fixture(scope="session")
def synth_root(request: pytest.FixtureRequest) -> Path:
    """Content-keyed product cache in .pytest_cache: reused across runs until make_synth.py,
    a layout parameter or the ffmpeg version changes (AGENT.md 6.1)."""
    return Path(request.config.cache.mkdir("skua-synth"))


@pytest.fixture(scope="session")
def synth_ad_break(synth_root: Path) -> SynthResult:
    """C1 | D C2 | D AD | D C3 in mpeg2video, shared by every test in the session."""
    return ensure_synth(synth_root, layout="ad-break")


@pytest.fixture(scope="session")
def synth_same_uri(synth_root: Path) -> SynthResult:
    """Same layout, but the ad keeps the content URI template."""
    return ensure_synth(synth_root, layout="ad-break-same-uri")


@pytest.fixture(scope="session")
def synth_marked(synth_root: Path) -> SynthResult:
    """ad-break with a 10 s ad (4 + 4 + 2 s) carrying EXT-X-DATERANGE CLASS="ad"."""
    return ensure_synth(synth_root, layout="ad-break-marked")


@pytest.fixture(scope="session")
def synth_aes(synth_root: Path) -> SynthResult:
    """ad-break with AES-128: C1, C2 on key k1 (explicit IV), clear ad, C3 on k2 (implicit IV)."""
    return ensure_synth(synth_root, layout="ad-break-aes")


@pytest.fixture(scope="session")
def synth_h264(synth_root: Path) -> SynthResult:
    """ad-break layout in libx264; skipped locally when unavailable, a failure in CI."""
    if not probe_encoder(H264_ENCODER):
        reason = f"{H264_ENCODER} failed the trial encode in this ffmpeg build"
        if os.environ.get(REQUIRE_H264_ENV) == "1":
            pytest.fail(f"{reason}, but {REQUIRE_H264_ENV}=1", pytrace=False)
        pytest.skip(reason)
    return ensure_synth(synth_root, layout="ad-break", video_encoder=H264_ENCODER)


# --- local HLS server (AGENT.md 6.1: 127.0.0.1, random port, aiohttp) -------------------------


@dataclass
class HlsServer:
    """Static file server over ``root`` with fault injection and request bookkeeping."""

    root: Path
    base: str = ""
    requests: Counter[str] = field(default_factory=Counter)
    ranges: list[tuple[str, str]] = field(default_factory=list)
    headers_seen: list[dict[str, str]] = field(default_factory=list)
    fail: dict[str, int] = field(default_factory=dict)  # path -> remaining 503 responses
    status: dict[str, int] = field(default_factory=dict)  # path -> fixed error status
    truncate: dict[str, int] = field(default_factory=dict)  # path -> remaining cut bodies
    ignore_range: bool = False
    delay_s: float = 0.0
    in_flight: int = 0
    max_in_flight: int = 0

    def url(self, rel: str) -> str:
        return f"{self.base}/{rel}"

    async def handle(self, request: web.Request) -> web.StreamResponse:
        rel = request.match_info["path"]
        self.requests[rel] += 1
        self.headers_seen.append(dict(request.headers))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if rel in self.status:
                return web.Response(status=self.status[rel])
            if self.fail.get(rel, 0) > 0:
                self.fail[rel] -= 1
                return web.Response(status=503)
            path = (self.root / rel).resolve()
            if self.root.resolve() not in path.parents or not path.is_file():
                return web.Response(status=404)
            data = path.read_bytes()
            status, start = 200, 0
            range_header = request.headers.get("Range")
            if range_header:
                self.ranges.append((rel, range_header))
            if range_header and not self.ignore_range:
                start = int(range_header.removeprefix("bytes=").split("-", 1)[0])
                if start >= len(data):
                    return web.Response(status=416)
                status = 206
            body = data[start:]
            resp = web.StreamResponse(status=status)
            resp.content_length = len(body)
            if status == 206:
                resp.headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
            await resp.prepare(request)
            if self.truncate.get(rel, 0) > 0:
                self.truncate[rel] -= 1
                await resp.write(body[: len(body) // 2])
                assert request.transport is not None
                request.transport.close()  # client sees a short body
                return resp
            await resp.write(body)
            await resp.write_eof()
            return resp
        finally:
            self.in_flight -= 1


@pytest.fixture
async def serve() -> AsyncIterator[Callable[[Path], Awaitable[HlsServer]]]:
    """``server = await serve(root)`` starts a server on 127.0.0.1 with a random port."""
    runners: list[web.AppRunner] = []

    async def start(root: Path) -> HlsServer:
        server = HlsServer(root)
        app = web.Application()
        app.router.add_get("/{path:.+}", server.handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = runner.addresses[0][1]  # the OS-assigned port of the only site
        server.base = f"http://127.0.0.1:{port}"
        return server

    yield start
    for runner in runners:
        await runner.cleanup()
