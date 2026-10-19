"""tests/evidence: structural features and hard priors (evidence A, SPEC 3.3)."""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from make_synth import SynthResult

from skua.adapter.hls import open_hls
from skua.evidence.structural import (
    STRUCT_FIELDS,
    StructuralConfig,
    discontinuity_blocks,
    is_ad_marker,
    struct_features,
    structural_evidence,
)
from skua.schema import ContainerEvent, PlaylistEvent, Shot, Timeline, features_to_table

MID = "0123456789abcdef"
COL = {name: i for i, name in enumerate(STRUCT_FIELDS)}


def timeline_of(result: SynthResult, cache: Path) -> Timeline:
    return asyncio.run(open_hls(str(result.playlist), cache_root=cache)).timeline


@pytest.fixture(scope="module")
def ad_break_tl(synth_ad_break: SynthResult, tmp_path_factory: pytest.TempPathFactory) -> Timeline:
    return timeline_of(synth_ad_break, tmp_path_factory.mktemp("c"))


# --- hand-built timelines --------------------------------------------------------------------


def make_tl(
    durations: list[float],
    events: list[tuple[int, str, dict[str, Any]]],
    *,
    offset: dict[int, float] | None = None,
) -> Timeline:
    """Shots s0..sN-1 back to back; events as (shot index, kind, extra payload)."""
    shots, t = [], 0.0
    for i, d in enumerate(durations):
        shots.append(Shot(i, t, t + d, [t + d / 2], [f"s{i}.ts"]))
        t += d
    playlist, container = [], []
    for idx, kind, extra in events:
        ts = shots[idx].start + (offset or {}).get(idx, 0.0)
        payload = {"unit": f"s{idx}.ts", **extra}
        if kind in ("pts_jump", "video_params", "audio_params", "pid_change", "bitrate_outlier"):
            container.append(ContainerEvent(ts, kind, payload))  # type: ignore[arg-type]
        else:
            playlist.append(PlaylistEvent(ts, kind, payload))  # type: ignore[arg-type]
    playlist.sort(key=lambda e: e.ts)
    container.sort(key=lambda e: e.ts)
    return Timeline(MID, t, 25.0, shots, container, playlist)


def ad_block_events(first: int, n: int) -> list[tuple[int, str, dict[str, Any]]]:
    """A structurally perfect ad block: disc + video_params at its head, URI break all over."""
    out: list[tuple[int, str, dict[str, Any]]] = [
        (first, "discontinuity", {}),
        (first, "video_params", {"changed": ["width"]}),
    ]
    out += [
        (i, "uri_pattern_break", {"signature": "x", "majority": "y"})
        for i in range(first, first + n)
    ]
    return out


def closing(at: int) -> list[tuple[int, str, dict[str, Any]]]:
    return [(at, "discontinuity", {}), (at, "video_params", {"changed": ["width"]})]


# --- synthetic streams -----------------------------------------------------------------------


def test_synth_ad_block_gets_the_block_prior(ad_break_tl: Timeline) -> None:
    ev = structural_evidence(ad_break_tl)
    assert ev.hard_prior == {12: 4.0, 13: 4.0, 14: 4.0}
    (rng,) = ev.ranges
    assert (rng.source, rng.first_unit, rng.last_unit) == ("block", "ads/ad012.ts", "ads/ad014.ts")
    assert (rng.start, rng.end) == pytest.approx((48.0, 60.0))
    c = rng.conditions
    assert c is not None and c.met
    assert (c.start_discontinuity, c.start_video_params, c.uri_pattern_break_hits, c.segments) == (
        True, True, 3, 3,
    )  # fmt: skip
    assert c.within_max_duration and not c.is_largest_block


def test_synth_content_blocks_fail_for_the_right_reasons(ad_break_tl: Timeline) -> None:
    blocks = discontinuity_blocks(ad_break_tl)
    c1, c2, _, c3 = (b.conditions for b in blocks)
    assert not c1.start_discontinuity  # playlist head
    assert c2.start_discontinuity and not c2.start_video_params  # same parameters as C1
    # C3 changes resolution back after the ad, but its URIs are the majority template.
    assert c3.start_discontinuity and c3.start_video_params and c3.uri_pattern_break_hits == 0
    assert c1.is_largest_block and c2.is_largest_block and c3.is_largest_block  # tied at 24 s


def test_synth_same_uri_has_no_prior(synth_same_uri: SynthResult, tmp_path: Path) -> None:
    ev = structural_evidence(timeline_of(synth_same_uri, tmp_path))
    assert ev.hard_prior == {} and ev.ranges == []
    ad = discontinuity_blocks(timeline_of(synth_same_uri, tmp_path))[2].conditions
    assert (ad.uri_pattern_break_hits, ad.segments) == (0, 3)


def test_synth_marked_has_daterange_and_block_priors(
    synth_marked: SynthResult, tmp_path: Path
) -> None:
    ev = structural_evidence(timeline_of(synth_marked, tmp_path))
    assert ev.hard_prior == {12: 4.0, 13: 4.0, 14: 4.0}
    assert [r.source for r in ev.ranges] == ["block", "daterange"]
    dr = next(r for r in ev.ranges if r.source == "daterange")
    assert (dr.first_unit, dr.last_unit) == ("ads/ad012.ts", "ads/ad014.ts")
    assert dr.daterange is not None and dr.daterange["class"] == "ad"
    assert dr.daterange["anchored_by"] == "program-date-time"


def test_synth_aes_keys_show_up_in_struct(synth_aes: SynthResult, tmp_path: Path) -> None:
    ev = structural_evidence(timeline_of(synth_aes, tmp_path))
    assert ev.hard_prior == {12: 4.0, 13: 4.0, 14: 4.0}
    keyed = [f.shot_idx for f in ev.features if f.struct[COL["key_change"]] == 1.0]
    assert keyed == [12, 15]  # clear ad, then key k2


def expected_rate_dims(tl: Timeline, shot: Shot) -> tuple[float, float]:
    """Independent re-derivation of the encoder-dependent dims from the Timeline's events.

    Bitrate outliers depend on the exact bytes the encoder produced (CI's ffmpeg 6.1 and a
    local 7.0 differ), so tests derive these two dims instead of hard-coding them.
    """
    zs = [
        abs(float(e.payload["z"]))
        for e in tl.container_events
        if e.kind == "bitrate_outlier" and e.payload["unit"] in shot.source_units
    ]
    stamps = [e.ts for e in [*tl.playlist_events, *tl.container_events]]
    nearest = min((abs(t - shot.start) for t in stamps), default=tl.duration)
    return max(zs, default=0.0), math.log1p(nearest)


def test_struct_vectors(ad_break_tl: Timeline) -> None:
    feats = struct_features(ad_break_tl)
    assert len(feats) == 21 and all(f.struct.shape == (10,) for f in feats)
    assert all(f.struct.dtype == np.float32 for f in feats)
    ad0 = dict(zip(STRUCT_FIELDS, feats[12].struct.tolist(), strict=True))
    # Structural dims are fixed by the layout and identical in every environment.
    assert {k: ad0[k] for k in STRUCT_FIELDS if "bitrate" not in k and "nearest" not in k} == {
        "start_discontinuity": 1.0,
        "start_pts_jump": 1.0,
        "start_video_params": 1.0,
        "start_audio_params": 0.0,
        "duration_outlier_max_abs_z": 0.0,
        "uri_pattern_break": 1.0,
        "key_change": 0.0,
        "daterange_marker": 0.0,
    }
    for shot, feat in zip(ad_break_tl.shots, feats, strict=True):
        bitrate, nearest = expected_rate_dims(ad_break_tl, shot)
        assert feat.struct[COL["bitrate_outlier_max_abs_z"]] == pytest.approx(bitrate, rel=1e-6)
        assert feat.struct[COL["log1p_nearest_event_s"]] == pytest.approx(nearest, rel=1e-6)
    # Every segment of the ad body is far below the stream's bitrate in any environment.
    assert feats[13].struct[COL["bitrate_outlier_max_abs_z"]] > 3
    assert feats[12].struct[COL["log1p_nearest_event_s"]] == 0.0  # events at the ad head
    features_to_table(feats)  # valid FEATURE_SCHEMA rows


# --- event attribution -----------------------------------------------------------------------


@pytest.mark.parametrize(("offset", "hit"), [(0.0, True), (0.4, True), (-0.5, True), (0.6, False)])
def test_event_window_is_start_plus_minus_half_a_second(offset: float, hit: bool) -> None:
    tl = make_tl([4.0] * 4, [(2, "discontinuity", {})], offset={2: offset})
    feats = struct_features(tl)
    assert bool(feats[2].struct[COL["start_discontinuity"]]) is hit
    assert feats[1].struct[COL["start_discontinuity"]] == 0.0  # never the previous shot


def test_window_comes_from_config() -> None:
    tl = make_tl([4.0] * 4, [(2, "pts_jump", {"delta_s": 1.0, "tagged": True})], offset={2: 0.6})
    assert (
        struct_features(tl, StructuralConfig(event_window_s=1.0))[2].struct[COL["start_pts_jump"]]
        == 1.0
    )


def test_distance_without_any_event_is_capped_by_duration() -> None:
    tl = make_tl([4.0] * 3, [])
    feats = struct_features(tl)
    assert all(
        f.struct[COL["log1p_nearest_event_s"]] == pytest.approx(math.log1p(12.0)) for f in feats
    )


# --- block conditions, one at a time (SPEC 3.3) ----------------------------------------------


def priors(tl: Timeline) -> list[int]:
    return sorted(structural_evidence(tl).hard_prior)


def test_perfect_block_gets_prior() -> None:
    tl = make_tl([4.0] * 20, ad_block_events(8, 3) + closing(11))
    assert priors(tl) == [8, 9, 10]


def test_block_needs_every_segment_to_break_the_uri_pattern() -> None:
    events = [
        e
        for e in ad_block_events(8, 3) + closing(11)
        if not (e[0] == 9 and e[1] == "uri_pattern_break")
    ]
    tl = make_tl([4.0] * 20, events)
    assert priors(tl) == []
    (block,) = [b for b in discontinuity_blocks(tl) if b.units[0] == "s8.ts"]
    assert (block.conditions.uri_pattern_break_hits, block.conditions.segments) == (2, 3)


@pytest.mark.parametrize("drop", ["discontinuity", "video_params"])
def test_block_head_needs_discontinuity_and_video_params(drop: str) -> None:
    events = [e for e in ad_block_events(8, 3) + closing(11) if not (e[0] == 8 and e[1] == drop)]
    tl = make_tl([4.0] * 20, events)
    assert priors(tl) == []


def test_block_longer_than_180_s_gets_no_prior() -> None:
    tl = make_tl([4.0] * 120, ad_block_events(10, 46) + closing(56))  # 46 x 4 = 184 s
    assert priors(tl) == []
    tl_ok = make_tl([4.0] * 120, ad_block_events(10, 45) + closing(55))  # 180 s: allowed
    assert priors(tl_ok) == list(range(10, 55))


def test_largest_block_gets_no_prior_and_ties_count_as_largest() -> None:
    # Blocks: [0-1] 8 s, [2-11] 40 s (the "ad"), [12-13] 8 s: the ad block is the largest.
    tl = make_tl([4.0] * 14, ad_block_events(2, 10) + closing(12))
    assert priors(tl) == []
    # Content 0-9 (40 s) and ad 10-19 (40 s) tie: both count as largest.
    tie = make_tl([4.0] * 20, ad_block_events(10, 10))
    assert priors(tie) == []


def test_uri_signal_disabled_means_no_block_prior() -> None:
    # A disabled URI signal emits no uri_pattern_break at all (SPEC 3.1 60 % gate).
    events = [e for e in ad_block_events(8, 3) + closing(11) if e[1] != "uri_pattern_break"]
    assert priors(make_tl([4.0] * 20, events)) == []


def test_first_block_never_gets_a_block_prior() -> None:
    events = (
        [(i, "uri_pattern_break", {}) for i in range(3)] + [(0, "video_params", {})] + closing(3)
    )
    assert priors(make_tl([4.0] * 20, events)) == []


def test_hard_prior_value_comes_from_config() -> None:
    tl = make_tl([4.0] * 20, ad_block_events(8, 3) + closing(11))
    ev = structural_evidence(tl, StructuralConfig(hard_prior=2.5))
    assert set(ev.hard_prior.values()) == {2.5}


# --- daterange markers (SPEC 3.3 v0.3) -------------------------------------------------------


def dr(
    cls: str | None = None,
    scte35: list[str] | None = None,
    duration: float | None = None,
    id_: str = "a",
) -> dict[str, Any]:
    return {
        "id": id_, "class": cls, "scte35": scte35 or [], "anchor": "x",
        "anchored_by": "program-date-time", "duration": duration, "duration_source": None,
    }  # fmt: skip


def test_class_ad_marks_every_covered_shot() -> None:
    tl = make_tl([4.0] * 10, [(i, "daterange", dr("ad", duration=8.0)) for i in (3, 4)])
    ev = structural_evidence(tl)
    assert sorted(ev.hard_prior) == [3, 4]
    (rng,) = ev.ranges
    assert rng.source == "daterange" and rng.daterange is not None and rng.daterange["id"] == "a"
    assert all(ev.features[i].struct[COL["daterange_marker"]] == 1.0 for i in (3, 4))


def test_open_scte35_out_is_closed_by_the_next_in() -> None:
    events = [(3, "daterange", dr(scte35=["out"])), (7, "daterange", dr(scte35=["in"]))]
    ev = structural_evidence(make_tl([4.0] * 10, events))
    assert sorted(ev.hard_prior) == [3, 4, 5, 6]  # the IN shot itself is content again
    assert ev.ranges[0].daterange is not None and ev.ranges[0].daterange["closed_by_scte35_in"]


def test_open_scte35_out_prefers_the_in_with_the_same_id() -> None:
    events = [
        (2, "daterange", dr(scte35=["out"], id_="x")),
        (4, "daterange", dr(scte35=["in"], id_="other")),
        (6, "daterange", dr(scte35=["in"], id_="x")),
    ]
    assert priors(make_tl([4.0] * 10, events)) == [2, 3, 4, 5]


def test_open_scte35_out_without_in_covers_only_its_anchor() -> None:
    assert priors(make_tl([4.0] * 10, [(3, "daterange", dr(scte35=["out"]))])) == [3]


def test_scte35_in_and_cmd_are_never_hard_priors() -> None:
    tl = make_tl(
        [4.0] * 10,
        [
            (2, "daterange", dr(scte35=["in"], id_="i")),
            (4, "daterange", dr(scte35=["cmd"], id_="c")),
            (6, "daterange", dr("ad", scte35=["in"], id_="ai")),  # IN wins over CLASS=ad
        ],
    )
    ev = structural_evidence(tl)
    assert ev.hard_prior == {}
    marker = [int(f.struct[COL["daterange_marker"]]) for f in ev.features]
    assert marker == [0, 0, 0, 0, 1, 0, 0, 0, 0, 0]  # CMD is recorded as a feature only


def test_is_ad_marker() -> None:
    def ev(**kw: Any) -> PlaylistEvent:
        return PlaylistEvent(0.0, "daterange", {"unit": "u", **dr(**kw)})

    assert is_ad_marker(ev(cls="AD")) and is_ad_marker(ev(scte35=["out", "cmd"]))
    assert not is_ad_marker(ev(cls="chapter")) and not is_ad_marker(ev(scte35=["cmd"]))
    assert not is_ad_marker(ev(scte35=["out", "in"]))
    assert not is_ad_marker(PlaylistEvent(0.0, "discontinuity", {"unit": "u"}))
