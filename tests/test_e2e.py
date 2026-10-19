"""tests: end-to-end ``skua scan`` over HTTP on the synthetic layouts (AGENT.md 10.6).

Truth is where make_synth injected the ad (``SynthResult.ads``); the M0 acceptance bound is one
segment of boundary error.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from make_synth import SynthConfig, SynthResult

from skua.cli import main
from skua.schema import SkipList, from_json
from tests.conftest import HlsServer

Serve = Callable[[Path], Awaitable[HlsServer]]
ONE_SEGMENT = float(SynthConfig().seg_s)


async def scan_over_http(
    serve: Serve, synth: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> SkipList:
    server = await serve(synth.out_dir)
    out = tmp_path / "skiplist.json"
    args = ["scan", server.url("master.m3u8"), "--out", str(out)]
    args += ["--cache-dir", str(tmp_path / "cache"), "--db", str(tmp_path / "skua.sqlite")]
    code = await asyncio.to_thread(main, args)
    stdout = capsys.readouterr().out
    assert code == 0
    printed = from_json(SkipList, stdout)
    assert from_json(SkipList, out.read_text()) == printed
    return printed


def assert_matches_injection(found: SkipList, synth: SynthResult) -> None:
    assert len(found.segments) == len(synth.ads) == 1
    seg, (start, end) = found.segments[0], synth.ads[0]
    assert seg.label == "ad" and seg.confidence == 0.95
    assert abs(seg.start - start) <= ONE_SEGMENT and abs(seg.end - end) <= ONE_SEGMENT


async def test_ad_break_yields_exactly_one_ad(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    found = await scan_over_http(serve, synth_ad_break, tmp_path, capsys)
    assert_matches_injection(found, synth_ad_break)
    assert [s["source"] for s in found.segments[0].evidence["sources"]] == ["block"]


async def test_same_uri_yields_no_segment(
    serve: Serve, synth_same_uri: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same structure but the ad keeps the content URI template: no block prior, no daterange.
    found = await scan_over_http(serve, synth_same_uri, tmp_path, capsys)
    assert found.segments == []


async def test_marked_yields_a_daterange_backed_ad(
    serve: Serve, synth_marked: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    found = await scan_over_http(serve, synth_marked, tmp_path, capsys)
    assert_matches_injection(found, synth_marked)
    sources = found.segments[0].evidence["sources"]
    daterange = [s for s in sources if s["source"] == "daterange"]
    assert len(daterange) == 1 and daterange[0]["daterange"]["anchored_by"] == "program-date-time"


async def test_aes_layout_end_to_end(
    serve: Serve, synth_aes: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    found = await scan_over_http(serve, synth_aes, tmp_path, capsys)
    assert_matches_injection(found, synth_aes)


@pytest.mark.h264
async def test_h264_ad_break_end_to_end(
    serve: Serve, synth_h264: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    found = await scan_over_http(serve, synth_h264, tmp_path, capsys)
    assert_matches_injection(found, synth_h264)
