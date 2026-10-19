"""tests/scripts: synthetic HLS generator (scripts/make_synth.py) and its probe parsers.

Every assertion outside the ``h264`` group must hold identically in every environment: the base
set always uses mpeg2video (AGENT.md 6.1).
"""

from __future__ import annotations

from pathlib import Path

import m3u8
import make_synth
import pytest
from make_synth import (
    AD_PID,
    AD_SIZE,
    CONTENT_SIZE,
    PTS_HZ,
    PTS_WRAP,
    PTS_WRAP_S,
    EncoderUnavailableError,
    SegmentProbe,
    SynthResult,
    main,
    parse_ffmpeg_version,
    parse_ffprobe_segment,
    probe_segment,
    synthesize,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "proc"
SEG = 4
FPS = 25
TICKS_PER_SEG = SEG * PTS_HZ


def steps(values: list[int]) -> list[int]:
    """Tick differences between consecutive values, modulo the 33-bit wrap."""
    return [(b - a) % PTS_WRAP for a, b in zip(values, values[1:], strict=False)]


def probes(result: SynthResult) -> list[SegmentProbe]:
    return result.probes  # probed once during synthesis


# --- pure parsers on recorded real output ---------------------------------------------------


def test_parse_ffmpeg_version() -> None:
    assert parse_ffmpeg_version((FIXTURES / "ffmpeg_version.txt").read_text()) == "7.0.2"
    assert parse_ffmpeg_version("ffmpeg version 6.1.1-3ubuntu5 Copyright") == "6.1.1-3ubuntu5"
    with pytest.raises(ValueError):
        parse_ffmpeg_version("ffprobe 7")


def test_parse_ffprobe_segment_keeps_raw_wrapped_timestamps() -> None:
    probe = parse_ffprobe_segment((FIXTURES / "ffprobe_segment_wrap.json").read_text())
    video, audio = probe.stream("video"), probe.stream("audio")
    assert (video.codec_name, video.width, video.height, video.pid) == (
        "mpeg2video",
        320,
        180,
        0x100,
    )
    assert (audio.codec_name, audio.sample_rate, audio.channels, audio.pid) == (
        "aac",
        48000,
        2,
        0x101,
    )
    pts = [p.pts for p in probe.video_packets]
    assert len(pts) == 8  # audio packets are filtered out
    assert probe.video_packets[0].keyframe and not probe.video_packets[1].keyframe
    # Verbatim from ffprobe 7.0.2 with -correct_ts_overflow 0: the packets at the wrap come back
    # half unwrapped (2**33 + 3600) and half raw (0, 14400). The parser must not "fix" this.
    assert pts[4:] == [8589938192, 8589930992, 0, 14400]
    assert [x % PTS_WRAP for x in pts[4:]] == [3600, 8589930992, 0, 14400]


# --- base set: mpeg2video, identical in every environment -----------------------------------


def test_manifest_records_trial_encode_probe(synth_ad_break: SynthResult) -> None:
    import json

    m = json.loads(synth_ad_break.manifest.read_text())
    assert m["encoders"]["video"] == "mpeg2video" == synth_ad_break.video_encoder
    assert m["encoders"]["audio"] == "aac"
    assert set(m["encoders"]["probed"]) == {"mpeg2video", "libx264", "aac"}
    assert m["encoders"]["probed"]["mpeg2video"] is True
    assert m["ffmpeg_version"]
    assert m["ads"] == [[48.0, 60.0]]


def test_recorded_probes_match_a_fresh_probe(synth_ad_break: SynthResult) -> None:
    for i in (0, 12, 17):
        uri = synth_ad_break.segments[i].uri
        assert probe_segment(synth_ad_break.out_dir / uri) == synth_ad_break.probes[i]


def test_media_playlist_layout(synth_ad_break: SynthResult) -> None:
    pl = m3u8.load(str(synth_ad_break.playlist))
    assert pl.is_endlist and pl.playlist_type == "vod" and pl.target_duration == SEG
    assert len(pl.segments) == 21
    assert [i for i, s in enumerate(pl.segments) if s.discontinuity] == [6, 12, 15]
    assert all(s.duration == SEG for s in pl.segments)
    uris = [s.uri for s in pl.segments]
    assert uris[12:15] == ["ads/ad012.ts", "ads/ad013.ts", "ads/ad014.ts"]
    assert all(u == f"seg{i:03d}.ts" for i, u in enumerate(uris) if not 12 <= i < 15)
    assert [s.uri for s in synth_ad_break.segments] == uris
    assert [s.start for s in synth_ad_break.segments] == [4.0 * i for i in range(21)]


def test_master_playlist_lists_highest_variant_last(synth_ad_break: SynthResult) -> None:
    master = m3u8.load(str(synth_ad_break.master))
    assert master.is_variant
    bandwidths = {p.uri: p.stream_info.bandwidth for p in master.playlists}
    assert list(bandwidths) == ["low.m3u8", "index.m3u8"]
    assert max(bandwidths, key=bandwidths.__getitem__) == "index.m3u8"
    low = (synth_ad_break.out_dir / "low.m3u8").read_text()
    assert low == synth_ad_break.playlist.read_text()


def test_segment_streams_and_gop(synth_ad_break: SynthResult) -> None:
    for truth, probe in zip(synth_ad_break.segments, probes(synth_ad_break), strict=True):
        video, audio = probe.stream("video"), probe.stream("audio")
        is_ad = truth.kind == "ad"
        assert video.codec_name == "mpeg2video", truth.uri
        assert (video.width, video.height) == (AD_SIZE if is_ad else CONTENT_SIZE), truth.uri
        assert (video.pid, audio.pid) == ((AD_PID, AD_PID + 1) if is_ad else (0x100, 0x101))
        assert (audio.codec_name, audio.sample_rate, audio.channels) == ("aac", 48000, 2)
        packets = probe.video_packets
        assert len(packets) == SEG * FPS, truth.uri
        # One closed GOP per segment: the first packet is the only keyframe.
        assert packets[0].keyframe and sum(p.keyframe for p in packets) == 1, truth.uri
        # B-frames: decode order differs from presentation order.
        assert any(p.pts != p.dts for p in packets), truth.uri


def test_pts_layout_across_parts(synth_ad_break: SynthResult) -> None:
    pts0 = [p.video_packets[0].pts for p in probes(synth_ad_break)]
    c1, c2, ad, c3 = pts0[0:6], pts0[6:12], pts0[12:15], pts0[15:21]
    # Inside a part PTS advance by exactly one segment per segment (modulo the 33-bit wrap).
    for part in (c1, c2, ad, c3):
        assert set(steps(part)) == {TICKS_PER_SEG}
    mux_delay = c1[0]  # whatever this ffmpeg's mpegts muxer adds; identical for every part
    assert c2[0] - mux_delay == 1000 * PTS_HZ  # tagged discontinuity, PTS restarts forward
    assert ad[0] == mux_delay  # ad restarts near zero: a PTS jump backwards
    assert abs(c3[0] - mux_delay - round((PTS_WRAP_S - 10) * PTS_HZ)) <= 1


def test_pts_wraps_inside_last_content_part(synth_ad_break: SynthResult) -> None:
    c3 = probes(synth_ad_break)[15:21]
    near_wrap = PTS_WRAP - 20 * PTS_HZ
    assert all(p.video_packets[0].pts > near_wrap for p in c3[:3])
    assert all(p.video_packets[0].pts < 20 * PTS_HZ for p in c3[3:])
    straddling = [
        p
        for p in c3
        if max(x.pts for x in p.video_packets) - min(x.pts for x in p.video_packets) > PTS_WRAP // 2
    ]
    assert len(straddling) == 1  # exactly one segment contains the wrap itself


def test_same_uri_layout_keeps_content_template(synth_same_uri: SynthResult) -> None:
    uris = [s.uri for s in synth_same_uri.segments]
    assert uris == [f"seg{i:03d}.ts" for i in range(21)]
    assert synth_same_uri.ads == [(48.0, 60.0)]


def test_cli_prints_probe_to_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--out", str(tmp_path / "tiny"), "--layout", "tiny"]) == 0
    err = capsys.readouterr().err
    assert "probe mpeg2video=ok" in err and "libx264=" in err and "aac=ok" in err
    assert "using video=mpeg2video audio=aac" in err
    assert (tmp_path / "tiny" / "manifest.json").is_file()


def test_unavailable_encoder_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(make_synth, "probe_encoder", lambda name: name != "mpeg2video")
    with pytest.raises(EncoderUnavailableError, match="mpeg2video failed the trial encode"):
        synthesize(tmp_path / "a", layout="tiny")
    assert main(["--out", str(tmp_path / "b"), "--layout", "tiny"]) == 3
    assert "mpeg2video=unavailable" in capsys.readouterr().err


def test_refuses_non_empty_output_dir(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("x")
    with pytest.raises(FileExistsError):
        synthesize(tmp_path, layout="tiny")


def test_aes_segments_decrypt_to_the_plaintext_probe(synth_aes: SynthResult) -> None:
    for truth, plain in zip(synth_aes.segments, synth_aes.probes, strict=True):
        path = synth_aes.out_dir / truth.uri
        if truth.key_uri is None:
            assert truth.part == "ad" and truth.iv_hex is None
            assert probe_segment(path) == plain
            continue
        # Encrypted bytes are not a transport stream; decrypted they are the original one.
        data = path.read_bytes()
        assert data[0] != 0x47 and len(data) % 16 == 0, truth.uri  # PKCS7-padded blocks
        assert probe_segment(path, key_hex=truth.key_hex, iv_hex=truth.iv_hex) == plain


def test_aes_key_and_iv_layout(synth_aes: SynthResult) -> None:
    segs = synth_aes.segments
    assert {s.key_uri for s in segs[:12]} == {"keys/k1.key"}
    assert {s.key_iv for s in segs[:12]} == {"0x" + "00" * 15 + "2a"}
    assert all(s.key_uri is None for s in segs[12:15])
    assert {s.key_uri for s in segs[15:]} == {"keys/k2.key"}
    # Implicit IV = media sequence number (RFC 8216 5.2).
    assert [s.iv_hex for s in segs[15:]] == [f"{n:032x}" for n in range(15, 21)]
    assert (synth_aes.out_dir / "keys" / "k1.key").read_bytes() == bytes(range(16))
    text = synth_aes.playlist.read_text()
    assert text.count("#EXT-X-KEY:") == 3  # k1 once (C2 inherits it), NONE, k2


# --- h264 group: CI increment, skipped locally when libx264 is unavailable ------------------


@pytest.mark.h264
def test_h264_segments_have_same_structure(synth_h264: SynthResult) -> None:
    assert synth_h264.video_encoder == "libx264"
    pl = m3u8.load(str(synth_h264.playlist))
    assert [i for i, s in enumerate(pl.segments) if s.discontinuity] == [6, 12, 15]
    assert all(s.duration == SEG for s in pl.segments)
    for truth, probe in zip(synth_h264.segments, probes(synth_h264), strict=True):
        video = probe.stream("video")
        assert (video.codec_name, video.profile) == ("h264", "Main"), truth.uri
        assert (video.width, video.height) == (AD_SIZE if truth.kind == "ad" else CONTENT_SIZE)
        packets = probe.video_packets
        assert len(packets) == SEG * FPS, truth.uri
        assert packets[0].keyframe and sum(p.keyframe for p in packets) == 1, truth.uri
        assert any(p.pts != p.dts for p in packets), truth.uri


@pytest.mark.h264
def test_h264_pts_layout_matches_mpeg2(synth_h264: SynthResult) -> None:
    pts0 = [p.video_packets[0].pts for p in probes(synth_h264)]
    for part in (pts0[0:6], pts0[6:12], pts0[12:15], pts0[15:21]):
        assert set(steps(part)) == {TICKS_PER_SEG}
    assert pts0[6] - pts0[0] == 1000 * PTS_HZ
    assert pts0[12] == pts0[0]


# --- content-keyed product cache (AGENT.md 6.1) ---------------------------------------------


def test_ensure_synth_reuses_only_a_matching_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = make_synth.ensure_synth(tmp_path, layout="tiny")
    assert first.out_dir.parent == tmp_path and first.out_dir.name.startswith("tiny-mpeg2video-")

    def no_rebuild(*args: object, **kwargs: object) -> None:
        raise AssertionError("a cached product must not be regenerated")

    monkeypatch.setattr(make_synth, "synthesize", no_rebuild)
    again = make_synth.ensure_synth(tmp_path, layout="tiny")
    assert again.out_dir == first.out_dir
    assert again.segments == first.segments and again.probes == first.probes


def test_synth_key_changes_with_script_parameters_and_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_synth.SynthConfig()
    base = make_synth.synth_key("tiny", "mpeg2video", cfg, "7.0.2")
    assert base == make_synth.synth_key("tiny", "mpeg2video", cfg, "7.0.2")
    assert base != make_synth.synth_key("ad-break", "mpeg2video", cfg, "7.0.2")
    assert base != make_synth.synth_key("tiny", "libx264", cfg, "7.0.2")
    assert base != make_synth.synth_key(
        "tiny", "mpeg2video", make_synth.SynthConfig(fps=30), "7.0.2"
    )
    assert base != make_synth.synth_key("tiny", "mpeg2video", cfg, "6.1.1")
    edited = tmp_path / "make_synth.py"
    edited.write_bytes(Path(make_synth.__file__).read_bytes() + b"\n# edited\n")
    monkeypatch.setattr(make_synth, "__file__", str(edited))
    assert base != make_synth.synth_key("tiny", "mpeg2video", cfg, "7.0.2")


def test_edited_script_never_reads_the_old_product(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = make_synth.ensure_synth(tmp_path, layout="tiny")
    edited = tmp_path / "make_synth.py"
    edited.write_bytes(Path(make_synth.__file__).read_bytes() + b"\n# edited\n")
    monkeypatch.setattr(make_synth, "__file__", str(edited))
    new = make_synth.ensure_synth(tmp_path, layout="tiny")
    assert new.out_dir != old.out_dir


def test_load_synth_round_trips_the_session_product(synth_ad_break: SynthResult) -> None:
    loaded = make_synth.load_synth(synth_ad_break.out_dir)
    assert loaded == synth_ad_break
