"""tests/adapter: media_id and cached, resumable, concurrent segment download (fetch.py)."""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp
import pytest
from make_synth import SynthResult

from skua.adapter.fetch import (
    FetchConfig,
    FetchError,
    default_cache_root,
    fetch_bytes,
    fetch_segments,
    iter_segments,
    media_id,
    normalize_source,
    segment_path,
)
from skua.adapter.playlist import PlaylistSegment, parse_media_playlist
from tests.conftest import HlsServer

Serve = Callable[[Path], Awaitable[HlsServer]]
FAST = FetchConfig(backoff_s=0.0, timeout_s=10.0)


# --- media_id (SPEC 2) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "https://example.com/v/index.m3u8",
        "HTTPS://EXAMPLE.COM/v/index.m3u8",
        "https://example.com:443/v/index.m3u8",
        "https://example.com/v/index.m3u8?token=abc&exp=1",
        "https://example.com/v/index.m3u8#t=10",
        "https://user:pw@example.com/v/index.m3u8",
    ],
)
def test_media_id_ignores_case_default_port_query_fragment(variant: str) -> None:
    assert media_id(variant) == media_id("https://example.com/v/index.m3u8")


@pytest.mark.parametrize(
    "other",
    [
        "http://example.com/v/index.m3u8",  # scheme matters
        "https://example.com:8443/v/index.m3u8",  # non-default port matters
        "https://example.com/V/index.m3u8",  # path case matters
        "https://cdn.example.com/v/index.m3u8",
    ],
)
def test_media_id_distinguishes_real_differences(other: str) -> None:
    assert media_id(other) != media_id("https://example.com/v/index.m3u8")


def test_media_id_is_sha256_prefix_of_normalized_url() -> None:
    url = "https://Example.com:443/v/index.m3u8?x=1"
    assert normalize_source(url) == "https://example.com/v/index.m3u8"
    expected = hashlib.sha256(b"https://example.com/v/index.m3u8").hexdigest()[:16]
    assert media_id(url) == expected
    assert re.fullmatch(r"[0-9a-f]{16}", media_id(url))


def test_media_id_for_local_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    absolute = str(tmp_path / "show" / "index.m3u8")
    assert normalize_source("show/index.m3u8") == absolute
    assert media_id("show/./x/../index.m3u8") == media_id(absolute)
    assert media_id(f"file://{absolute}") == media_id(absolute)


def test_media_id_edge_cases() -> None:
    assert normalize_source("http://[::1]:80") == "http://[::1]/"
    with pytest.raises(ValueError):
        media_id("ftp://example.com/x.m3u8")
    with pytest.raises(ValueError):
        media_id("https:///no-host.m3u8")


def test_default_cache_root_from_env(skua_work_dir: Path) -> None:
    assert default_cache_root().name == "skua_cache"  # conftest points it at a temp dir


# --- downloading from the local server -------------------------------------------------------


def playlist_from(server: HlsServer, synth: SynthResult) -> list[PlaylistSegment]:
    url = server.url("index.m3u8")
    return parse_media_playlist(synth.playlist.read_text(), url).segments


async def test_fetch_all_segments_byte_identical(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path
) -> None:
    server = await serve(synth_ad_break.out_dir)
    server.delay_s = 0.02  # long enough for the workers to overlap
    segs = playlist_from(server, synth_ad_break)
    got = await fetch_segments(segs, tmp_path / "c", config=FAST)
    assert [g.index for g in got] == list(range(21))
    assert [g.uri for g in got] == [s.uri for s in synth_ad_break.segments]
    for g, truth in zip(got, synth_ad_break.segments, strict=True):
        assert g.path == segment_path(tmp_path / "c", g.index)
        assert g.path.read_bytes() == (synth_ad_break.out_dir / truth.uri).read_bytes()
        assert not g.cached
    assert 1 < server.max_in_flight <= 8  # SPEC 3.1: 8 concurrent downloads
    assert not list((tmp_path / "c").glob("*.part"))


async def test_concurrency_limit_comes_from_config(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path
) -> None:
    server = await serve(synth_ad_break.out_dir)
    server.delay_s = 0.02
    await fetch_segments(
        playlist_from(server, synth_ad_break), tmp_path, config=FetchConfig(concurrency=2)
    )
    assert server.max_in_flight == 2


async def test_rerun_uses_cache(serve: Serve, synth_ad_break: SynthResult, tmp_path: Path) -> None:
    server = await serve(synth_ad_break.out_dir)
    segs = playlist_from(server, synth_ad_break)
    await fetch_segments(segs, tmp_path, config=FAST)
    first = sum(server.requests.values())
    again = await fetch_segments(segs, tmp_path, config=FAST)
    assert sum(server.requests.values()) == first == 21
    assert all(g.cached for g in again)


async def test_part_file_resumes_with_range(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path
) -> None:
    server = await serve(synth_ad_break.out_dir)
    segs = playlist_from(server, synth_ad_break)[:1]
    source = (synth_ad_break.out_dir / "seg000.ts").read_bytes()
    part = segment_path(tmp_path, 0).with_name("seg0.ts.part")
    part.write_bytes(source[:1000])
    (got,) = await fetch_segments(segs, tmp_path, config=FAST)
    assert server.ranges == [("seg000.ts", "bytes=1000-")]
    assert got.path.read_bytes() == source and not part.exists()


async def test_server_without_range_support_restarts(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path
) -> None:
    server = await serve(synth_ad_break.out_dir)
    server.ignore_range = True  # answers 200 with the whole body
    segs = playlist_from(server, synth_ad_break)[:1]
    segment_path(tmp_path, 0).with_name("seg0.ts.part").write_bytes(b"garbage" * 100)
    (got,) = await fetch_segments(segs, tmp_path, config=FAST)
    assert got.path.read_bytes() == (synth_ad_break.out_dir / "seg000.ts").read_bytes()


async def test_truncated_body_is_retried_and_resumed(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path
) -> None:
    server = await serve(synth_ad_break.out_dir)
    server.truncate["seg003.ts"] = 1
    segs = playlist_from(server, synth_ad_break)[:4]
    got = await fetch_segments(segs, tmp_path, config=FAST)
    assert got[3].path.read_bytes() == (synth_ad_break.out_dir / "seg003.ts").read_bytes()
    assert server.requests["seg003.ts"] == 2
    assert [r for r in server.ranges if r[0] == "seg003.ts"]  # the retry resumed


async def test_transient_errors_are_retried(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path
) -> None:
    server = await serve(synth_ad_break.out_dir)
    server.fail["seg001.ts"] = 3  # SPEC 3.1: 3 retries -> 4th attempt succeeds
    got = await fetch_segments(playlist_from(server, synth_ad_break)[:2], tmp_path, config=FAST)
    assert server.requests["seg001.ts"] == 4
    assert got[1].path.read_bytes() == (synth_ad_break.out_dir / "seg001.ts").read_bytes()


async def test_gives_up_after_retries_with_redacted_message(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path
) -> None:
    server = await serve(synth_ad_break.out_dir)
    server.fail["seg001.ts"] = 99
    segs = playlist_from(server, synth_ad_break)[:2]
    with pytest.raises(FetchError, match="giving up after 4 attempts") as info:
        await fetch_segments(segs, tmp_path, config=FAST)
    assert server.requests["seg001.ts"] == 4
    assert "127.0.0.1" not in str(info.value) and "<url:" in str(info.value)


async def test_client_errors_are_not_retried(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path
) -> None:
    server = await serve(synth_ad_break.out_dir)
    server.status["seg001.ts"] = 404
    with pytest.raises(FetchError, match="HTTP 404"):
        await fetch_segments(playlist_from(server, synth_ad_break)[:2], tmp_path, config=FAST)
    assert server.requests["seg001.ts"] == 1


async def test_headers_are_sent_but_never_logged(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="skua.adapter")
    server = await serve(synth_ad_break.out_dir)
    secret = {"Referer": "https://example.com/watch/SECRET-REF", "Cookie": "sid=SECRET-COOKIE"}
    await fetch_segments(
        playlist_from(server, synth_ad_break)[:2], tmp_path, headers=secret, config=FAST
    )
    assert all(h.get("Cookie") == "sid=SECRET-COOKIE" for h in server.headers_seen)
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "SECRET" not in logged and "127.0.0.1" not in logged
    assert not list(tmp_path.rglob("*SECRET*"))


async def test_iter_segments_yields_in_order_and_cancels_on_close(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path
) -> None:
    server = await serve(synth_ad_break.out_dir)
    server.delay_s = 0.05
    segs = playlist_from(server, synth_ad_break)
    stream = iter_segments(segs, tmp_path, config=FetchConfig(concurrency=2))
    first = await anext(stream)
    assert first.index == 0
    await stream.aclose()  # consumer stops early: remaining downloads are cancelled
    assert sum(server.requests.values()) < len(segs)


async def test_local_segments_are_not_copied(synth_ad_break: SynthResult, tmp_path: Path) -> None:
    pl = synth_ad_break.playlist
    segs = parse_media_playlist(pl.read_text(), str(pl)).segments[:3]
    got = await fetch_segments(segs, tmp_path / "cache", config=FAST)
    assert [g.path for g in got] == [synth_ad_break.out_dir / s.uri for s in segs]
    assert all(g.cached for g in got)
    assert not (tmp_path / "cache").exists() or not any((tmp_path / "cache").iterdir())


async def test_fetch_bytes_for_key_and_playlist(serve: Serve, synth_aes: SynthResult) -> None:
    server = await serve(synth_aes.out_dir)
    async with aiohttp.ClientSession() as session:
        key = await fetch_bytes(server.url("keys/k1.key"), session, config=FAST)
        text = await fetch_bytes(server.url("index.m3u8"), session, config=FAST)
        local = await fetch_bytes(str(synth_aes.out_dir / "keys" / "k2.key"), session)
    assert key == bytes(range(16))
    assert text == synth_aes.playlist.read_bytes()
    assert local == bytes(range(16, 32))
