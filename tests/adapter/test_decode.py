"""tests/adapter: PyAV demux probing, parameter changes, frame and PCM iterators (decode.py).

Ground truth is the ffprobe view recorded by make_synth; timestamps are compared modulo 2**33
because ffprobe itself does not report them consistently near the wrap (AGENT.md 6.2).
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from make_synth import AD_SIZE, CONTENT_SIZE, SegmentProbe, SynthResult

from skua.adapter.decode import (
    PTS_WRAP,
    AesParams,
    DecodeError,
    DecodeUnit,
    DemuxProbe,
    iter_frames,
    iter_pcm,
    param_changes,
    probe_segment,
    pts_extent,
    unwrap,
)

TICKS = 90_000
SEG_TICKS = 4 * TICKS


def truth_first_pts(probe: SegmentProbe) -> int:
    anchor = probe.video_packets[0].pts % PTS_WRAP
    return min(unwrap(p.pts % PTS_WRAP, anchor) for p in probe.video_packets) % PTS_WRAP


def demux_all(result: SynthResult) -> list[DemuxProbe]:
    out = []
    for truth in result.segments:
        aes = AesParams(truth.key_hex, truth.iv_hex or "") if truth.key_hex else None
        out.append(probe_segment(result.out_dir / truth.uri, aes))
    return out


@pytest.fixture(scope="module")
def plain(synth_ad_break: SynthResult) -> list[DemuxProbe]:
    return demux_all(synth_ad_break)


# --- unwrap ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ticks", "reference", "expected"),
    [
        (100, 50, 100),
        (5, PTS_WRAP - 10, PTS_WRAP + 5),  # just after the wrap, seen from before it
        (PTS_WRAP - 10, 5, -10),  # just before the wrap, seen from after it
        (PTS_WRAP + 7, 0, 7),
    ],
)
def test_unwrap_picks_nearest_representative(ticks: int, reference: int, expected: int) -> None:
    assert unwrap(ticks, reference) == expected


def test_pts_extent_uses_earliest_presentation_time() -> None:
    # Decode order I B B P with leading B-frames shown before the I-frame (open GOP).
    packets = [(1000, 100), (800, 100), (900, 100), (1300, 100)]
    assert pts_extent(packets) == (800, 600)


def test_pts_extent_across_the_wrap() -> None:
    packets = [(PTS_WRAP - 200, 100), (PTS_WRAP - 300, 100), (PTS_WRAP - 100, 100), (50, 100)]
    assert pts_extent(packets) == (PTS_WRAP - 300, 450)
    # libav's default overflow correction reports pre-wrap packets as negative numbers.
    assert pts_extent([(-200, 100), (-300, 100), (50, 100)]) == (PTS_WRAP - 300, 450)


# --- probe_segment ---------------------------------------------------------------------------


def test_probe_matches_ffprobe_on_every_segment(
    synth_ad_break: SynthResult, plain: list[DemuxProbe]
) -> None:
    for truth, ref, got in zip(synth_ad_break.segments, synth_ad_break.probes, plain, strict=True):
        assert got.first_pts == truth_first_pts(ref), truth.uri
        assert got.span_ticks == SEG_TICKS, truth.uri  # 100 frames x 3600 ticks
        assert got.packets == len(ref.video_packets) == 100
        assert got.starts_with_keyframe
        assert got.time_base == Fraction(1, TICKS)
        assert got.streams == tuple(sorted((s.pid, s.codec_type) for s in ref.streams))


def test_probe_reports_parsed_parameters(plain: list[DemuxProbe]) -> None:
    content, ad = plain[0], plain[12]
    assert content.video is not None and ad.video is not None and content.audio is not None
    assert (content.video.codec, content.video.width, content.video.height) == (
        "mpeg2video",
        *CONTENT_SIZE,
    )
    assert (ad.video.width, ad.video.height) == AD_SIZE
    assert content.video.profile == "Main" and content.video.pix_fmt == "yuv420p"
    assert content.video.fps == 25.0
    assert (content.audio.codec, content.audio.sample_rate, content.audio.channels) == (
        "aac",
        48000,
        2,
    )
    assert content.audio.profile == "LC"


def test_probe_across_the_wrap_is_raw_and_continuous(plain: list[DemuxProbe]) -> None:
    c3 = plain[15:21]
    assert all(p.first_pts < PTS_WRAP for p in c3)
    # Consecutive C3 segments are one segment apart modulo the wrap, including the one that
    # contains the wrap itself (seg017).
    steps = [(b.first_pts - a.first_pts) % PTS_WRAP for a, b in zip(c3, c3[1:], strict=False)]
    assert steps == [SEG_TICKS] * 5
    assert c3[2].first_pts > PTS_WRAP - SEG_TICKS and c3[3].first_pts < SEG_TICKS


def test_probe_decrypts_aes_segments(synth_aes: SynthResult) -> None:
    probes = demux_all(synth_aes)
    for truth, ref, got in zip(synth_aes.segments, synth_aes.probes, probes, strict=True):
        assert got.first_pts == truth_first_pts(ref), truth.uri
        assert got.packets == 100
    assert synth_aes.segments[0].key_iv is not None  # explicit IV
    assert synth_aes.segments[15].key_iv is None  # implicit IV = sequence number


def test_probe_errors(synth_aes: SynthResult, tmp_path: Path) -> None:
    seg = synth_aes.segments[0]
    path = synth_aes.out_dir / seg.uri
    wrong = AesParams("ff" * 16, seg.iv_hex or "")
    with pytest.raises(DecodeError):
        probe_segment(path, wrong)
    with pytest.raises(DecodeError):
        probe_segment(path)  # encrypted bytes read as plain TS
    junk = tmp_path / "junk.ts"
    junk.write_bytes(b"not a transport stream" * 100)
    with pytest.raises(DecodeError):
        probe_segment(junk)
    with pytest.raises(DecodeError):
        probe_segment(tmp_path / "missing.ts")


# --- param_changes ---------------------------------------------------------------------------


def changes_along(probes: list[DemuxProbe]) -> list[tuple[int, str, list[str]]]:
    out = []
    for i in range(1, len(probes)):
        for kind, payload in param_changes(probes[i - 1], probes[i]):
            out.append((i, kind, payload.get("changed", [])))
    return out


def test_param_changes_only_at_the_ad_boundaries(plain: list[DemuxProbe]) -> None:
    # C1 -> C2 is a DISCONTINUITY with identical parameters: nothing to report there.
    assert changes_along(plain) == [
        (12, "video_params", ["width", "height"]),
        (12, "pid_change", []),
        (15, "video_params", ["width", "height"]),
        (15, "pid_change", []),
    ]


def test_param_change_payload(plain: list[DemuxProbe]) -> None:
    (video, pids) = param_changes(plain[11], plain[12])
    assert video[1]["before"]["width"] == CONTENT_SIZE[0]
    assert video[1]["after"]["width"] == AD_SIZE[0]
    assert pids[1]["before"] == [[0x100, "video"], [0x101, "audio"]]
    assert pids[1]["after"] == [[0x200, "video"], [0x201, "audio"]]


# --- iterators -------------------------------------------------------------------------------


def units_for(result: SynthResult, probes: list[DemuxProbe], idx: range) -> list[DecodeUnit]:
    out = []
    for i in idx:
        truth = result.segments[i]
        aes = AesParams(truth.key_hex, truth.iv_hex or "") if truth.key_hex else None
        out.append(DecodeUnit(result.out_dir / truth.uri, truth.start, probes[i].first_pts, aes))
    return out


def test_iter_frames_on_a_grid_across_the_wrap(
    synth_ad_break: SynthResult, plain: list[DemuxProbe]
) -> None:
    units = units_for(synth_ad_break, plain, range(15, 19))  # seg017 contains the wrap
    frames = list(iter_frames(units, fps=1.0, size=(64, 36)))
    times = [t for t, _ in frames]
    assert times == pytest.approx([60.0 + k for k in range(16)], abs=1e-6)
    assert all(img.shape == (36, 64, 3) and img.dtype == np.uint8 for _, img in frames)


def test_iter_frames_at_requested_times(
    synth_ad_break: SynthResult, plain: list[DemuxProbe]
) -> None:
    units = units_for(synth_ad_break, plain, range(11, 13))  # last content + first ad segment
    # 25 fps: frames sit on a 0.04 s grid, so 45.5 is served by 45.52 (first frame at or after).
    frames = list(iter_frames(units, ts=[45.5, 49.02, 46.0], size=(32, 18)))
    assert [t for t, _ in frames] == pytest.approx([45.52, 46.0, 49.04], abs=1e-6)
    # The ad is a flat coral picture, the content a test pattern.
    ad_img = frames[2][1].reshape(-1, 3).astype(float)
    assert ad_img.std(axis=0).max() < 25 and ad_img.mean(axis=0)[0] > ad_img.mean(axis=0)[2]
    assert frames[0][1].reshape(-1, 3).std(axis=0).max() > 25


def test_iter_frames_requires_exactly_one_selector(
    synth_ad_break: SynthResult, plain: list[DemuxProbe]
) -> None:
    units = units_for(synth_ad_break, plain, range(0, 1))
    with pytest.raises(ValueError):
        list(iter_frames(units, size=(8, 8)))
    with pytest.raises(ValueError):
        list(iter_frames(units, fps=1.0, ts=[0.0], size=(8, 8)))


def test_iter_frames_decrypts(synth_aes: SynthResult) -> None:
    probes = demux_all(synth_aes)
    frames = list(iter_frames(units_for(synth_aes, probes, range(15, 16)), fps=5.0, size=(8, 8)))
    assert [t for t, _ in frames] == pytest.approx([60.0 + k / 5 for k in range(20)], abs=1e-6)


def test_iter_pcm_mono_16k(synth_ad_break: SynthResult, plain: list[DemuxProbe]) -> None:
    chunks = list(iter_pcm(units_for(synth_ad_break, plain, range(0, 1))))
    assert all(c.dtype == np.float32 and c.ndim == 1 for _, c in chunks)
    samples = np.concatenate([c for _, c in chunks])
    assert abs(len(samples) - 4 * 16_000) < 0.05 * 16_000  # AAC frames do not align to 4 s
    # Content audio is a 440 Hz sine.
    spectrum = np.abs(np.fft.rfft(samples[:16_000]))
    assert abs(np.argmax(spectrum) - 440) <= 2
    starts = [t for t, _ in chunks]
    assert starts == sorted(starts) and abs(starts[0] - 0.0) < 0.05


def test_iter_pcm_noise_and_stereo(synth_ad_break: SynthResult, plain: list[DemuxProbe]) -> None:
    ad = np.concatenate([c for _, c in iter_pcm(units_for(synth_ad_break, plain, range(12, 13)))])
    spectrum = np.abs(np.fft.rfft(ad[:16_000]))
    assert spectrum.max() < 20 * np.median(spectrum)  # white noise: no dominant tone
    (t, stereo), *_ = iter_pcm(units_for(synth_ad_break, plain, range(0, 1)), mono=False)
    assert stereo.shape[0] == 2 and t == pytest.approx(0.0, abs=0.05)


# --- h264 group ------------------------------------------------------------------------------


@pytest.mark.h264
def test_h264_parameters_and_changes(synth_h264: SynthResult) -> None:
    probes = demux_all(synth_h264)
    content, ad = probes[0].video, probes[12].video
    assert content is not None and ad is not None
    assert (content.codec, content.profile) == ("h264", "Main")
    assert content.level is not None and ad.level is not None
    assert content.pix_fmt == "yuv420p" and content.fps == 25.0
    kinds = [(i, k) for i, k, _ in changes_along(probes)]
    assert kinds == [
        (12, "video_params"),
        (12, "pid_change"),
        (15, "video_params"),
        (15, "pid_change"),
    ]
    changed = changes_along(probes)[0][2]
    assert {"width", "height"} <= set(changed)
    for truth, ref, got in zip(synth_h264.segments, synth_h264.probes, probes, strict=True):
        assert got.first_pts == truth_first_pts(ref), truth.uri
