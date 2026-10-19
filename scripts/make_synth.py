"""scripts: synthesize HLS test media (content, ad, discontinuities, PTS wrap) with ffmpeg.

Policy (AGENT.md 6.1): the base test set always uses ``mpeg2video`` + ``aac`` so every
environment asserts identical results; ``libx264`` is only used for the ``h264`` test group.
Encoder availability is probed by actually encoding a few frames, because a dlopen build lists
encoders it cannot load. The probe result is printed to stderr and written to manifest.json.

All ffmpeg / ffprobe calls go through ``skua.adapter.proc`` (no subprocess here).

Usage: python scripts/make_synth.py --out DIR [--layout ad-break] [--video-encoder mpeg2video]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from skua.adapter.proc import AllowedCommand, ProcError, run

DEFAULT_VIDEO_ENCODER = "mpeg2video"
H264_ENCODER = "libx264"
AUDIO_ENCODER = "aac"
PROBED_ENCODERS: tuple[str, ...] = (DEFAULT_VIDEO_ENCODER, H264_ENCODER, AUDIO_ENCODER)

PTS_HZ = 90_000  # MPEG-TS clock, ISO/IEC 13818-1 2.4.3.7
PTS_WRAP = 2**33  # 33-bit PTS/DTS field
PTS_WRAP_S = PTS_WRAP / PTS_HZ

CONTENT_SIZE = (320, 180)
AD_SIZE = (256, 144)  # different resolution, so the ad block carries video_params
AD_COLOR = "0xF0695A"  # coral, SPEC 3.8 palette
AD_PID = 0x200  # default mpegts ES PIDs start at 0x100; the ad changes them (pid_change)

PROBE_TIMEOUT_S = 30.0
ENCODE_TIMEOUT_S = 300.0


class EncoderUnavailableError(RuntimeError):
    """The requested encoder failed the trial encode in this ffmpeg build."""


@dataclass(frozen=True)
class SynthConfig:
    """Stream-wide encoding parameters (not SPEC parameters; test-fixture shape only)."""

    seg_s: int = 4  # HLS segment length; keyframes are forced at every boundary
    fps: int = 25
    bframes: int = 2  # B-frames on, so packets are reordered (pts != dts) like real streams
    video_bitrate: str = "400k"
    audio_bitrate: str = "64k"

    @property
    def gop(self) -> int:
        return self.seg_s * self.fps


@dataclass(frozen=True)
class Part:
    """A run of segments from one synthetic source; the playlist is a sequence of parts."""

    name: str
    kind: Literal["content", "ad"]
    duration_s: int
    size: tuple[int, int]
    uri_template: str  # printf-style, relative to the playlist, e.g. "ads/ad%03d.ts"
    discontinuity: bool = False  # EXT-X-DISCONTINUITY before the first segment of this part
    pts_offset_s: float = 0.0  # segment muxer initial_offset; mpegts adds its own mux delay
    start_pid: int = 0x100
    sample_rate: int = 48_000
    channels: int = 2
    daterange: bool = False  # EXT-X-DATERANGE CLASS="ad" spanning the part, on its first segment


@dataclass(frozen=True)
class SegmentTruth:
    """What make_synth knows about one generated segment (ground truth for tests)."""

    uri: str
    part: str
    kind: str
    discontinuity: bool  # tag precedes this segment
    extinf: float
    start: float  # on the EXTINF-accumulated axis, seconds
    bytes: int
    daterange: float | None = None  # DURATION of a CLASS="ad" DATERANGE attached here


# --- layouts --------------------------------------------------------------------------------


def ad_break_layout(*, same_uri: bool = False) -> list[Part]:
    """3 content parts + 1 ad (AGENT.md 10.3): C1 | D C2 | D AD | D C3.

    - C1 -> C2: DISCONTINUITY with a PTS restart but identical codec parameters (negative case
      for video_params / audio_params).
    - AD: different resolution, PIDs and URI template, PTS restarting near 0.
    - C3: PTS starts just below the 33-bit wrap, so the wrap happens inside C3.
    ``same_uri`` keeps the content URI template for the ad (negative case for uri_pattern_break).
    """
    content_uri = "seg%03d.ts"
    return [
        Part("c1", "content", 24, CONTENT_SIZE, content_uri),
        Part("c2", "content", 24, CONTENT_SIZE, content_uri, True, pts_offset_s=1000.0),
        Part(
            "ad",
            "ad",
            12,
            AD_SIZE,
            content_uri if same_uri else "ads/ad%03d.ts",
            True,
            pts_offset_s=0.0,
            start_pid=AD_PID,
        ),
        Part("c3", "content", 24, CONTENT_SIZE, content_uri, True, pts_offset_s=PTS_WRAP_S - 10),
    ]


def ad_break_marked_layout() -> list[Part]:
    """ad-break with explicit ad markers: a 10 s ad (4 + 4 + 2 s, so the 2 s segment is a
    non-final EXTINF outlier) carrying an EXT-X-DATERANGE CLASS="ad"."""
    c1, c2, ad, c3 = ad_break_layout()
    marked = Part(
        ad.name, ad.kind, 10, ad.size, ad.uri_template, ad.discontinuity,
        ad.pts_offset_s, ad.start_pid, daterange=True,
    )  # fmt: skip
    return [c1, c2, marked, c3]


def tiny_layout() -> list[Part]:
    """One content segment and one ad segment; for fast CLI and encoder tests."""
    return [
        Part("c1", "content", 4, CONTENT_SIZE, "seg%03d.ts"),
        Part("ad", "ad", 4, AD_SIZE, "ads/ad%03d.ts", True, start_pid=AD_PID),
    ]


LAYOUTS = {
    "ad-break": ad_break_layout,
    "ad-break-same-uri": lambda: ad_break_layout(same_uri=True),
    "ad-break-marked": ad_break_marked_layout,
    "tiny": tiny_layout,
}


# --- ffmpeg / ffprobe ------------------------------------------------------------------------


def parse_ffmpeg_version(text: str) -> str:
    """``ffmpeg version 7.0.2 Copyright ...`` -> ``7.0.2``."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    parts = first.split()
    if len(parts) < 3 or parts[1] != "version":
        raise ValueError(f"unexpected ffmpeg -version output: {first!r}")
    return parts[2]


def probe_encoder(name: str) -> bool:
    """Trial-encode a few frames; listing in ``-encoders`` is not proof (dlopen builds)."""
    source = (
        "sine=sample_rate=48000:duration=0.2"
        if name == AUDIO_ENCODER
        else ("testsrc2=s=64x64:r=25:d=0.2")
    )
    args = ["-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", source]
    args += ["-c:a" if name == AUDIO_ENCODER else "-c:v", name, "-f", "null", "-"]
    try:
        result = run(AllowedCommand.FFMPEG, args, timeout_s=PROBE_TIMEOUT_S, check=False)
    except ProcError:
        return False
    return result.returncode == 0


@dataclass(frozen=True)
class StreamProbe:
    """One elementary stream as reported by ffprobe."""

    index: int
    codec_type: str
    codec_name: str
    profile: str | None
    width: int | None
    height: int | None
    sample_rate: int | None
    channels: int | None
    pid: int


@dataclass(frozen=True)
class PacketProbe:
    """One packet of the first video stream; raw 90 kHz ticks, overflow correction off."""

    pts: int
    dts: int
    keyframe: bool


@dataclass(frozen=True)
class SegmentProbe:
    """ffprobe view of one TS segment."""

    streams: list[StreamProbe]
    video_packets: list[PacketProbe]

    def stream(self, codec_type: str) -> StreamProbe:
        return next(s for s in self.streams if s.codec_type == codec_type)


@dataclass(frozen=True)
class SynthResult:
    """Paths and ground truth of one synthesized HLS package."""

    out_dir: Path
    playlist: Path
    master: Path
    manifest: Path
    video_encoder: str
    probed: dict[str, bool]
    segments: list[SegmentTruth]
    probes: list[SegmentProbe]  # ffprobe view of each segment, same order as ``segments``
    ads: list[tuple[float, float]] = field(default_factory=list)


FFPROBE_SEGMENT_ARGS = [
    "-v",
    "error",
    # Turn off the file-level overflow correction so the wrap stays visible to tests. Observed
    # with ffmpeg 7.0.2: packets right at the wrap may still come back unwrapped (2**33 + x)
    # while later ones are raw, so consumers must compare timestamps modulo 2**33.
    "-correct_ts_overflow",
    "0",
    "-show_entries",
    "stream=index,codec_type,codec_name,profile,width,height,sample_rate,channels,id"
    ":packet=stream_index,pts,dts,flags",
    "-of",
    "json",
]


def parse_ffprobe_segment(text: str) -> SegmentProbe:
    """Pure parser for ``ffprobe FFPROBE_SEGMENT_ARGS <file>`` output; values kept verbatim."""
    doc: dict[str, Any] = json.loads(text)
    streams = [
        StreamProbe(
            index=int(s["index"]),
            codec_type=str(s["codec_type"]),
            codec_name=str(s["codec_name"]),
            profile=s.get("profile"),
            width=s.get("width"),
            height=s.get("height"),
            sample_rate=int(s["sample_rate"]) if "sample_rate" in s else None,
            channels=s.get("channels"),
            pid=int(str(s["id"]), 16),
        )
        for s in doc.get("streams", [])
    ]
    video = [s.index for s in streams if s.codec_type == "video"]
    first_video = video[0] if video else -1
    packets = [
        PacketProbe(int(p["pts"]), int(p["dts"]), "K" in str(p.get("flags", "")))
        for p in doc.get("packets", [])
        if int(p["stream_index"]) == first_video
    ]
    return SegmentProbe(streams, packets)


def probe_segment(path: Path) -> SegmentProbe:
    """Run ffprobe on one segment file."""
    result = run(AllowedCommand.FFPROBE, [*FFPROBE_SEGMENT_ARGS, str(path)], timeout_s=60)
    return parse_ffprobe_segment(result.stdout.decode())


def _sources(part: Part, cfg: SynthConfig) -> tuple[str, str]:
    w, h = part.size
    if part.kind == "content":
        video = f"testsrc2=s={w}x{h}:r={cfg.fps}"
        audio = f"sine=frequency=440:sample_rate={part.sample_rate}"
    else:
        video = (
            f"color=c={AD_COLOR}:s={w}x{h}:r={cfg.fps},format=yuv420p,"
            "eq=brightness='0.35*sin(4*PI*t)':eval=frame"
        )
        audio = f"anoisesrc=color=white:amplitude=0.25:sample_rate={part.sample_rate}:seed=7"
    return video, audio


def _video_args(encoder: str, cfg: SynthConfig) -> list[str]:
    common = [
        "-b:v", cfg.video_bitrate, "-g", str(cfg.gop), "-bf", str(cfg.bframes),
        "-force_key_frames", f"expr:gte(t,n_forced*{cfg.seg_s})", "-pix_fmt", "yuv420p",
    ]  # fmt: skip
    if encoder == DEFAULT_VIDEO_ENCODER:
        # Closed GOPs and no scene-cut I-frames: every segment starts with the only keyframe.
        return ["-c:v", encoder, *common, "-flags", "+cgop", "-sc_threshold", "1000000000"]
    if encoder == H264_ENCODER:
        return [
            "-c:v", encoder, "-preset", "veryfast", "-profile:v", "main", *common,
            "-keyint_min", str(cfg.gop), "-sc_threshold", "0", "-x264-params", "open_gop=0",
        ]  # fmt: skip
    raise ValueError(f"unsupported video encoder {encoder!r}")


def _encode_part(
    part: Part, cfg: SynthConfig, encoder: str, out_dir: Path, first_number: int
) -> list[str]:
    target = out_dir / part.uri_template
    target.parent.mkdir(parents=True, exist_ok=True)
    seg_list = out_dir / f".{part.name}.csv"
    video, audio = _sources(part, cfg)
    args = [
        "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio,
        "-t", str(part.duration_s), "-map", "0:v", "-map", "1:a",
        *_video_args(encoder, cfg),
        "-c:a", AUDIO_ENCODER, "-b:a", cfg.audio_bitrate,
        "-ar", str(part.sample_rate), "-ac", str(part.channels),
        "-f", "segment", "-segment_time", str(cfg.seg_s), "-segment_format", "mpegts",
        # Options of the inner mpegts muxer only reach it through segment_format_options.
        "-segment_format_options", f"mpegts_start_pid={part.start_pid}",
        # initial_offset (not output_ts_offset) so cuts stay aligned to the part's own clock.
        "-initial_offset", repr(part.pts_offset_s), "-reset_timestamps", "0",
        "-segment_start_number", str(first_number),
        "-segment_list", str(seg_list), "-segment_list_type", "csv",
        str(target),
    ]  # fmt: skip
    run(AllowedCommand.FFMPEG, args, timeout_s=ENCODE_TIMEOUT_S)
    names = [line.split(",", 1)[0] for line in seg_list.read_text().splitlines() if line]
    seg_list.unlink()
    rel_dir = Path(part.uri_template).parent
    return [(rel_dir / name).as_posix() for name in names]


def _iso(offset_s: float) -> str:
    """Wall clock of an axis offset; arbitrary epoch, RFC 8216 4.3.2.7 needs a PDT with a
    DATERANGE."""
    whole = int(offset_s)
    millis = round((offset_s - whole) * 1000)
    hours, rem = divmod(whole, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"2026-01-01T{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}Z"


def _media_playlist(segments: Sequence[SegmentTruth]) -> str:
    target = math.ceil(max((s.extinf for s in segments), default=1.0))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    marked = any(s.daterange is not None for s in segments)
    for i, seg in enumerate(segments):
        if seg.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        if marked and i == 0:
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{_iso(0.0)}")
        if seg.daterange is not None:
            lines.append(
                f'#EXT-X-DATERANGE:ID="{seg.part}-{i}",CLASS="ad",'
                f'START-DATE="{_iso(seg.start)}",DURATION={seg.daterange:.3f}'
            )
        lines += [f"#EXTINF:{seg.extinf:.6f},", seg.uri]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _master_playlist(segments: Sequence[SegmentTruth]) -> str:
    peak = max(s.bytes * 8 / s.extinf for s in segments)
    high = math.ceil(peak)
    w, h = CONTENT_SIZE
    # The lower variant is listed first so "pick the highest" is not "pick the first".
    return "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-STREAM-INF:BANDWIDTH={high // 2},RESOLUTION={w // 2}x{h // 2}",
            "low.m3u8",
            f"#EXT-X-STREAM-INF:BANDWIDTH={high},RESOLUTION={w}x{h}",
            "index.m3u8",
            "",
        ]
    )


def synthesize(
    out_dir: Path,
    *,
    layout: str = "ad-break",
    video_encoder: str = DEFAULT_VIDEO_ENCODER,
    cfg: SynthConfig | None = None,
) -> SynthResult:
    """Encode every part, write index.m3u8 / low.m3u8 / master.m3u8 and manifest.json."""
    cfg = cfg or SynthConfig()
    parts = LAYOUTS[layout]()
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} is not empty")
    out_dir.mkdir(parents=True, exist_ok=True)

    probed = {name: probe_encoder(name) for name in PROBED_ENCODERS}
    version = parse_ffmpeg_version(
        run(AllowedCommand.FFMPEG, ["-hide_banner", "-version"], timeout_s=30).stdout.decode()
    )
    summary = ", ".join(f"{k}={'ok' if v else 'unavailable'}" for k, v in probed.items())
    print(
        f"make_synth: ffmpeg {version}; probe {summary}; using video={video_encoder} "
        f"audio={AUDIO_ENCODER}",
        file=sys.stderr,
    )
    for needed in (video_encoder, AUDIO_ENCODER):
        if not probed.get(needed, False):
            raise EncoderUnavailableError(f"{needed} failed the trial encode in ffmpeg {version}")

    segments: list[SegmentTruth] = []
    probes: list[SegmentProbe] = []
    ads: list[tuple[float, float]] = []
    clock = 0.0
    for part in parts:
        uris = _encode_part(part, cfg, video_encoder, out_dir, len(segments))
        part_start = clock
        first_of_part = len(segments)
        for i, uri in enumerate(uris):
            # EXTINF = whole frames in the segment; exact because keyframes are forced on the
            # segment grid (the segment muxer's own csv times are off by one frame).
            probe = probe_segment(out_dir / uri)
            probes.append(probe)
            extinf = len(probe.video_packets) / cfg.fps
            segments.append(
                SegmentTruth(
                    uri=uri,
                    part=part.name,
                    kind=part.kind,
                    discontinuity=part.discontinuity and i == 0,
                    extinf=extinf,
                    start=clock,
                    bytes=(out_dir / uri).stat().st_size,
                )
            )
            clock += extinf
        if part.kind == "ad":
            ads.append((part_start, clock))
        if part.daterange:
            first = segments[first_of_part]
            segments[first_of_part] = replace(first, daterange=clock - part_start)

    playlist = out_dir / "index.m3u8"
    playlist.write_text(_media_playlist(segments))
    (out_dir / "low.m3u8").write_text(_media_playlist(segments))
    master = out_dir / "master.m3u8"
    master.write_text(_master_playlist(segments))
    manifest = out_dir / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "generator": "scripts/make_synth.py",
                "layout": layout,
                "ffmpeg_version": version,
                "encoders": {"probed": probed, "video": video_encoder, "audio": AUDIO_ENCODER},
                "config": asdict(cfg),
                "parts": [asdict(p) for p in parts],
                "segments": [asdict(s) for s in segments],
                "ads": [list(a) for a in ads],
                "playlist": playlist.name,
                "master": master.name,
            },
            indent=2,
        )
        + "\n"
    )
    return SynthResult(
        out_dir, playlist, master, manifest, video_encoder, probed, segments, probes, ads
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="empty or missing directory")
    parser.add_argument("--layout", choices=sorted(LAYOUTS), default="ad-break")
    parser.add_argument(
        "--video-encoder",
        choices=[DEFAULT_VIDEO_ENCODER, H264_ENCODER],
        default=DEFAULT_VIDEO_ENCODER,
    )
    args = parser.parse_args(argv)
    try:
        result = synthesize(args.out, layout=args.layout, video_encoder=args.video_encoder)
    except EncoderUnavailableError as exc:
        print(f"make_synth: {exc}", file=sys.stderr)
        return 3
    print(f"make_synth: wrote {len(result.segments)} segments to {result.out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
