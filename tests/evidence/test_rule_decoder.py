"""tests/evidence: the M0 rule decoder and its Segment.evidence contract (SPEC 3.3)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from make_synth import SynthResult

from skua.adapter.hls import open_hls
from skua.evidence import rule_decoder
from skua.evidence.rule_decoder import RuleDecoderConfig, rule_decode
from skua.evidence.structural import structural_evidence
from skua.schema import Segment, SkipList, Timeline, from_json, to_json
from tests.evidence.test_structural import ad_block_events, closing, dr, make_tl

TOP_KEYS = {"decoder", "hard_prior", "sources"}
SOURCE_KEYS = {"source", "first_unit", "last_unit", "duration_s"}
CONDITION_KEYS = {
    "start_discontinuity",
    "start_video_params",
    "uri_pattern_break_hits",
    "segments",
    "within_max_duration",
    "is_largest_block",
}
DATERANGE_KEYS = {"id", "class", "scte35", "anchored_by", "duration_source", "closed_by_scte35_in"}


def decode_synth(result: SynthResult, cache: Path) -> tuple[Timeline, list[Segment]]:
    tl = asyncio.run(open_hls(str(result.playlist), cache_root=cache)).timeline
    return tl, rule_decode(tl, structural_evidence(tl))


def test_module_is_marked_for_removal_in_m3() -> None:
    assert "M3 删除" in Path(rule_decoder.__file__).read_text(encoding="utf-8").splitlines()[0]


def test_synth_ad_break_yields_exactly_the_ad(synth_ad_break: SynthResult, tmp_path: Path) -> None:
    tl, segs = decode_synth(synth_ad_break, tmp_path)
    (seg,) = segs
    assert (seg.start, seg.end, seg.label, seg.confidence) == pytest.approx(
        (48.0, 60.0, "ad", 0.95)
    )
    assert set(seg.evidence) == TOP_KEYS
    assert seg.evidence["decoder"] == "m0_rule_decoder" and seg.evidence["hard_prior"] == 4.0
    (src,) = seg.evidence["sources"]
    assert set(src) == SOURCE_KEYS | {"conditions"}
    assert (src["source"], src["first_unit"], src["last_unit"]) == (
        "block", "ads/ad012.ts", "ads/ad014.ts",
    )  # fmt: skip
    assert src["duration_s"] == pytest.approx(12.0)
    assert src["conditions"] == {
        "start_discontinuity": True,
        "start_video_params": True,
        "uri_pattern_break_hits": 3,
        "segments": 3,
        "within_max_duration": True,
        "is_largest_block": False,
    }
    assert set(src["conditions"]) == CONDITION_KEYS


def test_synth_marked_lists_both_sources(synth_marked: SynthResult, tmp_path: Path) -> None:
    _, segs = decode_synth(synth_marked, tmp_path)
    (seg,) = segs
    assert (seg.start, seg.end) == pytest.approx((48.0, 58.0))
    block, daterange = seg.evidence["sources"]
    assert block["source"] == "block" and set(block) == SOURCE_KEYS | {"conditions"}
    assert daterange["source"] == "daterange" and set(daterange) == SOURCE_KEYS | {"daterange"}
    assert set(daterange["daterange"]) == DATERANGE_KEYS
    assert daterange["daterange"]["class"] == "ad"


@pytest.mark.parametrize("fixture", ["synth_same_uri"])
def test_no_prior_no_segment(fixture: str, request: pytest.FixtureRequest, tmp_path: Path) -> None:
    _, segs = decode_synth(request.getfixturevalue(fixture), tmp_path)
    assert segs == []


def test_separate_priors_give_separate_segments() -> None:
    events = ad_block_events(4, 2) + closing(6) + ad_block_events(12, 3) + closing(15)
    tl = make_tl([4.0] * 30, events)
    segs = rule_decode(tl, structural_evidence(tl))
    assert [(s.start, s.end) for s in segs] == [(16.0, 24.0), (48.0, 60.0)]


def test_adjacent_daterange_and_block_merge_into_one_segment() -> None:
    events = [(i, "daterange", dr("ad", duration=8.0)) for i in (6, 7)]
    events += ad_block_events(8, 3) + closing(11)
    tl = make_tl([4.0] * 30, events)
    (seg,) = rule_decode(tl, structural_evidence(tl))
    assert (seg.start, seg.end) == (24.0, 44.0)
    assert [s["source"] for s in seg.evidence["sources"]] == ["daterange", "block"]


def test_output_is_a_valid_skiplist() -> None:
    events = ad_block_events(4, 2) + closing(6) + ad_block_events(12, 3) + closing(15)
    tl = make_tl([4.0] * 30, events)
    sl = SkipList(tl.media_id, 1, rule_decode(tl, structural_evidence(tl)))
    assert from_json(SkipList, to_json(sl)) == sl
    assert all(s.label == "ad" for s in sl.segments)


def test_confidence_comes_from_config() -> None:
    tl = make_tl([4.0] * 20, ad_block_events(8, 3) + closing(11))
    (seg,) = rule_decode(tl, structural_evidence(tl), RuleDecoderConfig(confidence=0.5))
    assert seg.confidence == 0.5
