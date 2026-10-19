"""tests/adapter: unified time axis and Timeline assembly (timeline.py).

Truth (AGENT.md 6.2): shot starts are re-derived by ``truth_axis`` below — a separate pure
function over the raw first PTS that PyAV reports (via decode.probe_segment), not timeline.py.
ffprobe is used once, as a cross-check of that truth, never as the CI truth itself.
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

import pytest
from make_synth import SynthResult

from skua.adapter.decode import PTS_WRAP, DemuxProbe, probe_segment
from skua.adapter.playlist import parse_media_playlist
from skua.adapter.timeline import (
    TimelineBuild,
    TimelineConfig,
    build_timeline,
    keyframe_times,
)
from skua.schema import ContainerEvent, Timeline, from_json, to_json

MID = "0123456789abcdef"
FRAME = 1 / 25
TB = Fraction(1, 90_000)


def nearest(ticks: int, ref: int) -> int:
    d = (ticks - ref) % PTS_WRAP
    return ref + (d - PTS_WRAP if d >= PTS_WRAP // 2 else d)


def truth_axis(firsts: list[int], spans: list[int], discontinuity: list[bool]) -> list[float]:
    """Independent truth: between DISCONTINUITY tags segments share one PTS clock; each run
    starts where the previous run's last segment ends."""
    starts: list[float] = []
    run_start, run_pts0, prev = 0.0, 0, 0
    for i, first in enumerate(firsts):
        if i == 0 or discontinuity[i]:
            run_start = starts[-1] + float(spans[i - 1] * TB) if i else 0.0
            run_pts0 = prev = first
        prev = nearest(first, prev)
        starts.append(run_start + float((prev - run_pts0) * TB))
    return starts


def build(result: SynthResult, text: str | None = None) -> tuple[TimelineBuild, list[DemuxProbe]]:
    pl = result.playlist
    mp = parse_media_playlist(text if text is not None else pl.read_text(), str(pl))
    probes = [probe_segment(result.out_dir / s.uri) for s in mp.segments]
    sizes = [(result.out_dir / s.uri).stat().st_size for s in mp.segments]
    return build_timeline(MID, mp.segments, probes, sizes, mp.events), probes


def pyav_truth(result: SynthResult, probes: list[DemuxProbe]) -> list[float]:
    disc = [s.discontinuity for s in result.segments]
    return truth_axis([p.first_pts for p in probes], [p.span_ticks for p in probes], disc)


def events(tl: Timeline, kind: str) -> list[ContainerEvent]:
    return [e for e in tl.container_events if e.kind == kind]


@pytest.fixture(scope="module")
def ad_break(synth_ad_break: SynthResult) -> tuple[TimelineBuild, list[DemuxProbe]]:
    return build(synth_ad_break)


# --- the axis --------------------------------------------------------------------------------


def test_truth_from_pyav_agrees_with_ffprobe_once(
    synth_ad_break: SynthResult, ad_break: tuple[TimelineBuild, list[DemuxProbe]]
) -> None:
    _, probes = ad_break
    ff_firsts = []
    for ref in synth_ad_break.probes:
        anchor = ref.video_packets[0].pts % PTS_WRAP
        ff_firsts.append(
            min(nearest(p.pts % PTS_WRAP, anchor) for p in ref.video_packets) % PTS_WRAP
        )
    assert [p.first_pts for p in probes] == ff_firsts  # modulo 2**33, AGENT.md 6.2


def test_shot_starts_match_truth_within_one_frame(
    synth_ad_break: SynthResult, ad_break: tuple[TimelineBuild, list[DemuxProbe]]
) -> None:
    build_, probes = ad_break
    truth = pyav_truth(synth_ad_break, probes)
    got = [s.start for s in build_.timeline.shots]
    assert len(got) == 21
    assert all(abs(g - t) <= FRAME for g, t in zip(got, truth, strict=True)), (got, truth)
    # By construction the synthetic axis is also the EXTINF axis: 0, 4, ..., 80.
    assert truth == pytest.approx([4.0 * i for i in range(21)], abs=1e-9)


def test_axis_continues_through_the_pts_wrap(
    ad_break: tuple[TimelineBuild, list[DemuxProbe]],
) -> None:
    build_, probes = ad_break
    c3 = build_.timeline.shots[15:]
    assert [s.start for s in c3] == pytest.approx([60.0, 64.0, 68.0, 72.0, 76.0, 80.0])
    assert probes[17].first_pts > PTS_WRAP - 4 * 90_000 and probes[18].first_pts < 4 * 90_000
    assert not [e for e in events(build_.timeline, "pts_jump") if e.ts > 60.0]


def test_timeline_fields(ad_break: tuple[TimelineBuild, list[DemuxProbe]]) -> None:
    tl = ad_break[0].timeline
    assert tl.media_id == MID and tl.duration == pytest.approx(84.0) and tl.fps == 25.0
    shot = tl.shots[2]
    assert (shot.start, shot.end) == pytest.approx((8.0, 12.0))
    assert shot.keyframe_ts == pytest.approx([9.0, 10.0, 11.0])  # SPEC 3.2: 25/50/75 %
    assert tl.shots[12].source_units == ["ads/ad012.ts"]
    assert from_json(Timeline, to_json(tl)) == tl  # a valid schema object


def test_axis_records_origin_pts_for_decoders(
    ad_break: tuple[TimelineBuild, list[DemuxProbe]],
) -> None:
    build_, probes = ad_break
    assert [a.origin_pts for a in build_.axis] == [p.first_pts for p in probes]
    assert all(a.time_base == TB for a in build_.axis)


# --- container events ------------------------------------------------------------------------


def test_tagged_pts_jumps_at_every_discontinuity(
    ad_break: tuple[TimelineBuild, list[DemuxProbe]],
) -> None:
    jumps = events(ad_break[0].timeline, "pts_jump")
    assert [(e.ts, e.payload["unit"], e.payload["tagged"]) for e in jumps] == [
        (24.0, "seg006.ts", True),
        (48.0, "ads/ad012.ts", True),
        (60.0, "seg015.ts", True),
    ]
    deltas = [e.payload["delta_s"] for e in jumps]
    # C2 restarts 1000 s on; the ad restarts near 0; C3 starts 10 s below the wrap, which the
    # nearest-representative unwrap sees as a small backwards jump, not ~26.5 hours.
    assert deltas == pytest.approx([1000.0 - 24.0, -(1000.0 + 24.0), -22.0], abs=FRAME)


def test_parameter_events_only_at_the_ad_boundaries(
    ad_break: tuple[TimelineBuild, list[DemuxProbe]],
) -> None:
    tl = ad_break[0].timeline
    kinds = [(e.ts, e.kind) for e in tl.container_events if e.kind != "bitrate_outlier"]
    assert [k for k in kinds if k[1] != "pts_jump"] == [
        (48.0, "video_params"),
        (48.0, "pid_change"),
        (60.0, "video_params"),
        (60.0, "pid_change"),
    ]
    assert all(e.payload["unit"] for e in tl.container_events)


def test_bitrate_outliers_include_the_ad(ad_break: tuple[TimelineBuild, list[DemuxProbe]]) -> None:
    flagged = {
        e.payload["unit"]: e.payload["z"] for e in events(ad_break[0].timeline, "bitrate_outlier")
    }
    assert {"ads/ad012.ts", "ads/ad013.ts", "ads/ad014.ts"} <= set(flagged)
    assert all(flagged[u] < -3 for u in ("ads/ad012.ts", "ads/ad013.ts", "ads/ad014.ts"))
    # Observed, not asserted away: the first segment of every separately encoded synthetic
    # part overshoots the rate control, so seg000 / seg006 / seg015 are flagged too.


def test_playlist_events_sit_on_the_corrected_axis(
    ad_break: tuple[TimelineBuild, list[DemuxProbe]],
) -> None:
    tl = ad_break[0].timeline
    starts = {s.source_units[0]: s.start for s in tl.shots}
    assert tl.playlist_events
    assert all(e.ts == starts[e.payload["unit"]] for e in tl.playlist_events)


# --- robustness ------------------------------------------------------------------------------


def test_pts_corrects_rounded_extinf(synth_ad_break: SynthResult) -> None:
    # Real packagers round EXTINF; small errors must not move shots off the PTS axis.
    text = synth_ad_break.playlist.read_text()
    lines = text.splitlines()
    sloppy = [0.0, 0.3, -0.3, 0.2]
    k = 0
    for i, line in enumerate(lines):
        if line.startswith("#EXTINF:"):
            lines[i] = f"#EXTINF:{4.0 + sloppy[k % len(sloppy)]:.3f},"
            k += 1
    build_, probes = build(synth_ad_break, "\n".join(lines) + "\n")
    truth = pyav_truth(synth_ad_break, probes)
    got = [s.start for s in build_.timeline.shots]
    assert all(abs(g - t) <= FRAME for g, t in zip(got, truth, strict=True))
    assert len(events(build_.timeline, "pts_jump")) == 3  # still only the tagged ones


def test_pts_gap_inside_a_run_moves_later_shots(synth_ad_break: SynthResult) -> None:
    # Packagers drop or repeat a frame at segment joins; the axis must follow PTS, not the
    # accumulated lengths. Shift seg003..seg005 (same run as seg000) by 0.2 s.
    pl = synth_ad_break.playlist
    mp = parse_media_playlist(pl.read_text(), str(pl))
    real = [probe_segment(synth_ad_break.out_dir / s.uri) for s in mp.segments]
    gap = 18_000  # 0.2 s at 90 kHz, inside the 0.5 s tolerance
    probes = [
        replace(p, first_pts=(p.first_pts + gap) % PTS_WRAP) if 3 <= i <= 5 else p
        for i, p in enumerate(real)
    ]
    build_ = build_timeline(MID, mp.segments, list(probes), [1000] * 21, mp.events)
    truth = truth_axis(
        [p.first_pts for p in probes],
        [p.span_ticks for p in probes],
        [s.discontinuity for s in synth_ad_break.segments],
    )
    got = [s.start for s in build_.timeline.shots]
    assert got[3:6] == pytest.approx([12.2, 16.2, 20.2])
    assert all(abs(g - t) <= FRAME for g, t in zip(got, truth, strict=True))
    assert not [e for e in events(build_.timeline, "pts_jump") if not e.payload["tagged"]]


def test_untagged_pts_jump(synth_ad_break: SynthResult) -> None:
    # Drop the DISCONTINUITY before the ad: the PTS restart is now untagged.
    text = synth_ad_break.playlist.read_text()
    marker = "#EXT-X-DISCONTINUITY\n#EXTINF:4.000000,\nads/ad012.ts"
    assert marker in text
    build_, _ = build(synth_ad_break, text.replace(marker, marker.split("\n", 1)[1]))
    jumps = events(build_.timeline, "pts_jump")
    at_ad = [e for e in jumps if e.payload["unit"] == "ads/ad012.ts"]
    assert len(at_ad) == 1 and at_ad[0].payload["tagged"] is False
    assert at_ad[0].payload["delta_s"] == pytest.approx(-(1000.0 + 24.0), abs=FRAME)
    assert [s.start for s in build_.timeline.shots] == pytest.approx([4.0 * i for i in range(21)])


def test_undemuxable_segment_falls_back_to_extinf(
    synth_ad_break: SynthResult, caplog: pytest.LogCaptureFixture
) -> None:
    pl = synth_ad_break.playlist
    mp = parse_media_playlist(pl.read_text(), str(pl))
    probes: list[DemuxProbe | None] = [
        probe_segment(synth_ad_break.out_dir / s.uri) for s in mp.segments
    ]
    probes[3] = None
    sizes = [1000] * len(probes)
    build_ = build_timeline(MID, mp.segments, probes, sizes, mp.events)
    assert [s.start for s in build_.timeline.shots] == pytest.approx([4.0 * i for i in range(21)])
    assert build_.axis[3].origin_pts is None
    assert "EXTINF fallback" in caplog.text
    # Re-anchoring after the gap is not a PTS jump.
    assert [e.ts for e in events(build_.timeline, "pts_jump")] == [24.0, 48.0, 60.0]


def test_tolerance_comes_from_config(synth_ad_break: SynthResult) -> None:
    pl = synth_ad_break.playlist
    text = pl.read_text().replace("#EXTINF:4.000000,\nseg003.ts", "#EXTINF:4.300000,\nseg003.ts")
    mp = parse_media_playlist(text, str(pl))
    probes: list[DemuxProbe | None] = [
        probe_segment(synth_ad_break.out_dir / s.uri) for s in mp.segments
    ]
    sizes = [1000] * len(probes)
    tight = build_timeline(
        MID, mp.segments, probes, sizes, mp.events, TimelineConfig(pts_tolerance_s=0.1)
    )
    loose = build_timeline(MID, mp.segments, probes, sizes, mp.events)
    # With 0.1 s the 4.3 s EXTINF is kept and seg004's PTS looks like an untagged jump.
    assert [e.ts for e in events(tight.timeline, "pts_jump") if not e.payload["tagged"]] == [16.3]
    assert not [e for e in events(loose.timeline, "pts_jump") if not e.payload["tagged"]]


def test_keyframe_times() -> None:
    assert keyframe_times(10.0, 11.5) == [10.75]  # < 2 s: midpoint
    assert keyframe_times(0.0, 4.0) == [1.0, 2.0, 3.0]


def test_bitrate_outlier_needs_spread(synth_ad_break: SynthResult) -> None:
    pl = synth_ad_break.playlist
    mp = parse_media_playlist(pl.read_text(), str(pl))
    probes: list[DemuxProbe | None] = [None] * len(mp.segments)
    flat = build_timeline(MID, mp.segments, probes, [1000] * 21, mp.events)
    assert not events(flat.timeline, "bitrate_outlier")
    spiky = build_timeline(MID, mp.segments, probes, [1000] * 20 + [9000], mp.events)
    assert [e.payload["unit"] for e in events(spiky.timeline, "bitrate_outlier")] == ["seg020.ts"]


def test_input_validation(synth_ad_break: SynthResult) -> None:
    pl = synth_ad_break.playlist
    mp = parse_media_playlist(pl.read_text(), str(pl))
    with pytest.raises(ValueError):
        build_timeline(MID, mp.segments, [None], [1], mp.events)


# --- h264 group ------------------------------------------------------------------------------


@pytest.mark.h264
def test_h264_axis_matches_truth(synth_h264: SynthResult) -> None:
    build_, probes = build(synth_h264)
    truth = pyav_truth(synth_h264, probes)
    got = [s.start for s in build_.timeline.shots]
    assert all(abs(g - t) <= FRAME for g, t in zip(got, truth, strict=True))
    assert [e.payload["tagged"] for e in events(build_.timeline, "pts_jump")] == [True] * 3
