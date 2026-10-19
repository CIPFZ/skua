"""skua.adapter.decode: the only module that imports PyAV — demux probing, parameter changes,
frame and PCM iterators (SPEC 3.1, AGENT.md 5.3).

Timestamps are read raw (``correct_ts_overflow=0``): libav's own overflow correction turns the
packets before a 33-bit wrap into negative numbers, and how it does that differs between libav
versions. Raw ticks are unwrapped here, inside one segment, relative to the segment's first
packet; ``timeline.py`` unwraps across segments (ISO/IEC 13818-1 2.4.3.7).
"""

from __future__ import annotations

import logging
from collections.abc import Generator, Iterable, Iterator, Sequence
from dataclasses import dataclass, fields
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import av.error
import numpy as np
import numpy.typing as npt
from av.audio.frame import AudioFrame
from av.audio.resampler import AudioResampler
from av.container import InputContainer
from av.video.frame import VideoFrame

from skua.schema import ContainerEventKind

log = logging.getLogger("skua.adapter")

PTS_WRAP = 2**33  # 33-bit PTS/DTS in MPEG-TS
_HALF_WRAP = PTS_WRAP // 2


@dataclass(frozen=True)
class DecodeConfig:
    """Decoder defaults (AGENT.md 5.3); not SPEC policy parameters."""

    pcm_rate: int = 16_000  # iter_pcm(sr=16000)
    pcm_mono: bool = True  # iter_pcm(mono=True)


CONFIG = DecodeConfig()


class DecodeError(RuntimeError):
    """A segment could not be opened or demuxed (wrong key, not a transport stream, ...)."""


@dataclass(frozen=True)
class AesParams:
    """AES-128 parameters for libav's crypto: protocol (RFC 8216 5.2)."""

    key_hex: str  # 32 hex chars
    iv_hex: str  # 32 hex chars


@dataclass(frozen=True)
class VideoParams:
    """Parsed video parameters compared for ``video_params`` (SPEC 3.1; never extradata)."""

    codec: str
    width: int
    height: int
    profile: str | None
    level: int | None
    pix_fmt: str | None
    fps: float | None  # average frame rate


@dataclass(frozen=True)
class AudioParams:
    """Parsed audio parameters compared for ``audio_params`` (SPEC 3.1)."""

    codec: str
    sample_rate: int
    channels: int
    profile: str | None


@dataclass(frozen=True)
class DemuxProbe:
    """What one segment looks like to the demuxer. Raw 90 kHz-style ticks, adapter-internal."""

    streams: tuple[tuple[int, str], ...]  # (PID, media type), sorted
    video: VideoParams | None
    audio: AudioParams | None
    time_base: Fraction  # of the reference stream (first video, else first audio)
    first_pts: int  # earliest presentation timestamp of the reference stream, raw mod 2**33
    span_ticks: int  # last pts + duration - first_pts, unwrapped
    packets: int  # reference-stream packets with a timestamp
    starts_with_keyframe: bool


def unwrap(ticks: int, reference: int) -> int:
    """The representative of ``ticks`` (mod 2**33) nearest to ``reference``."""
    delta = (ticks - reference) % PTS_WRAP
    if delta >= _HALF_WRAP:
        delta -= PTS_WRAP
    return reference + delta


def pts_extent(packets: Sequence[tuple[int, int]]) -> tuple[int, int]:
    """``(first_pts, span)`` of ``(pts, duration)`` packets in decode order.

    ``first_pts`` is the earliest presentation time, not the first packet's: with open GOPs or
    B-pyramids leading B-frames are shown before the keyframe that is decoded first. Values are
    unwrapped against the first packet, so a segment containing the 33-bit wrap works too;
    ``first_pts`` is returned modulo 2**33.
    """
    if not packets:
        raise ValueError("no packets")
    anchor = packets[0][0] % PTS_WRAP
    starts = [unwrap(pts % PTS_WRAP, anchor) for pts, _ in packets]
    ends = [start + dur for start, (_, dur) in zip(starts, packets, strict=True)]
    lo = min(starts)
    return lo % PTS_WRAP, max(ends) - lo


def _open(path: Path, aes: AesParams | None) -> InputContainer:
    options = {"correct_ts_overflow": "0"}
    target = str(path)
    if aes is not None:
        # libav's crypto: protocol decrypts AES-128-CBC; no extra dependency (SPEC 3.1).
        options |= {"decryption_key": aes.key_hex, "decryption_iv": aes.iv_hex}
        target = f"crypto:{path}"
    try:
        return av.open(target, options=options, format="mpegts")
    except (av.error.FFmpegError, OSError) as exc:
        raise DecodeError(f"cannot open segment {path.name}: {exc}") from None


def _profile(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _video_params(stream: Any) -> VideoParams:
    cc = stream.codec_context
    level = getattr(cc, "level", None)
    rate = stream.average_rate
    return VideoParams(
        codec=str(cc.name),
        width=int(cc.width),
        height=int(cc.height),
        profile=_profile(cc.profile),
        level=int(level) if isinstance(level, int) and level >= 0 else None,
        pix_fmt=str(cc.pix_fmt) if cc.pix_fmt else None,
        fps=float(rate) if rate else None,
    )


def _audio_params(stream: Any) -> AudioParams:
    cc = stream.codec_context
    return AudioParams(
        codec=str(cc.name),
        sample_rate=int(cc.sample_rate),
        channels=int(cc.layout.nb_channels),
        profile=_profile(cc.profile),
    )


def probe_segment(path: Path, aes: AesParams | None = None) -> DemuxProbe:
    """Demux one MPEG-TS segment without decoding and summarize streams and timestamps."""
    container = _open(path, aes)
    try:
        streams = tuple(sorted((int(s.id), str(s.type)) for s in container.streams))
        videos, audios = container.streams.video, container.streams.audio
        ref = videos[0] if videos else (audios[0] if audios else None)
        if ref is None:
            raise DecodeError(f"segment {path.name}: no audio or video stream")
        stamped = [
            (int(pkt.pts), int(pkt.duration or 0), bool(pkt.is_keyframe))
            for pkt in container.demux(ref)
            if pkt.pts is not None
        ]
        if not stamped:
            raise DecodeError(f"segment {path.name}: no timestamped packets")
        first_pts, span = pts_extent([(pts, dur) for pts, dur, _ in stamped])
        return DemuxProbe(
            streams=streams,
            video=_video_params(videos[0]) if videos else None,
            audio=_audio_params(audios[0]) if audios else None,
            time_base=Fraction(ref.time_base) if ref.time_base else Fraction(1, 90_000),
            first_pts=first_pts,
            span_ticks=span,
            packets=len(stamped),
            starts_with_keyframe=stamped[0][2],
        )
    except av.error.FFmpegError as exc:
        raise DecodeError(f"cannot demux segment {path.name}: {exc}") from None
    finally:
        container.close()


def _changed(
    before: VideoParams | AudioParams | None, after: VideoParams | AudioParams | None
) -> list[str]:
    if before is None or after is None:
        return [] if before is after else ["present"]
    return [f.name for f in fields(before) if getattr(before, f.name) != getattr(after, f.name)]


def _as_dict(params: VideoParams | AudioParams | None) -> dict[str, Any] | None:
    if params is None:
        return None
    return {f.name: getattr(params, f.name) for f in fields(params)}


def param_changes(
    prev: DemuxProbe, cur: DemuxProbe
) -> list[tuple[ContainerEventKind, dict[str, Any]]]:
    """Container events between two consecutive segments: ``(kind, payload)`` without ``unit``
    (the caller knows the segment). Kinds: video_params, audio_params, pid_change (SPEC 3.1)."""
    out: list[tuple[ContainerEventKind, dict[str, Any]]] = []
    pairs: tuple[tuple[ContainerEventKind, VideoParams | AudioParams | None, Any], ...] = (
        ("video_params", prev.video, cur.video),
        ("audio_params", prev.audio, cur.audio),
    )
    for kind, before, after in pairs:
        changed = _changed(before, after)
        if changed:
            out.append(
                (kind, {"changed": changed, "before": _as_dict(before), "after": _as_dict(after)})
            )
    if prev.streams != cur.streams:
        out.append(
            (
                "pid_change",
                {
                    "before": [list(s) for s in prev.streams],
                    "after": [list(s) for s in cur.streams],
                },
            )
        )
    return out


@dataclass(frozen=True)
class DecodeUnit:
    """A segment placed on the unified axis: raw tick ``origin_pts`` sits at ``start`` seconds."""

    path: Path
    start: float
    origin_pts: int
    aes: AesParams | None = None


def _axis_time(unit: DecodeUnit, pts: int, time_base: Fraction) -> float:
    ticks = unwrap(int(pts) % PTS_WRAP, unit.origin_pts) - unit.origin_pts
    return unit.start + float(ticks * time_base)


def _video_frames(units: Iterable[DecodeUnit]) -> Generator[tuple[float, VideoFrame], None, None]:
    for unit in units:
        container = _open(unit.path, unit.aes)
        try:
            stream = container.streams.video[0]
            for frame in container.decode(stream):
                if frame.pts is not None and frame.time_base is not None:
                    yield _axis_time(unit, frame.pts, Fraction(frame.time_base)), frame
        except av.error.FFmpegError as exc:
            raise DecodeError(f"cannot decode segment {unit.path.name}: {exc}") from None
        finally:
            container.close()


def iter_frames(
    units: Sequence[DecodeUnit],
    *,
    fps: float | None = None,
    ts: Sequence[float] | None = None,
    size: tuple[int, int],
) -> Iterator[tuple[float, npt.NDArray[np.uint8]]]:
    """RGB frames ``(axis_time, HxWx3 uint8)`` resized to ``size=(width, height)``.

    Exactly one of ``fps`` (a regular grid from the first unit's start) or ``ts`` (sorted axis
    times; for each, the first frame at or after it) selects the frames (AGENT.md 5.3).
    """
    if (fps is None) == (ts is None):
        raise ValueError("pass exactly one of fps= or ts=")
    width, height = size
    targets = iter(sorted(ts)) if ts is not None else None
    step = 1.0 / fps if fps else 0.0
    next_t: float | None = units[0].start if units and fps else None
    if targets is not None:
        next_t = next(targets, None)
    eps = 1e-6
    frames = _video_frames(units)
    try:
        for t, frame in frames:
            if next_t is None:
                return
            if t + eps < next_t:
                continue
            image = frame.reformat(width=width, height=height, format="rgb24").to_ndarray()
            yield t, image.astype(np.uint8, copy=False)
            if targets is not None:
                next_t = next(targets, None)
                while next_t is not None and next_t <= t + eps:
                    next_t = next(targets, None)
            else:
                assert next_t is not None
                while next_t <= t + eps:
                    next_t += step
    finally:
        frames.close()  # closes the open container when the caller stops early


def iter_pcm(
    units: Sequence[DecodeUnit], *, sr: int = CONFIG.pcm_rate, mono: bool = CONFIG.pcm_mono
) -> Iterator[tuple[float, npt.NDArray[np.float32]]]:
    """Float32 PCM chunks ``(axis_time_of_first_sample, samples)`` resampled to ``sr``.

    Mono chunks are 1-D; otherwise ``(channels, n)`` (AGENT.md 5.3). Each segment is placed by
    the pts of its first audio frame; within the segment time advances by samples emitted,
    which is exact and independent of the resampler's own timestamps.
    """
    layout = "mono" if mono else "stereo"
    for unit in units:
        container = _open(unit.path, unit.aes)
        resampler = AudioResampler(format="fltp", layout=layout, rate=sr)
        try:
            stream = container.streams.audio[0]
            first_t: float | None = None
            emitted = 0
            frames: Iterable[AudioFrame | None] = container.decode(stream)
            for frame in [*frames, None]:  # None flushes the resampler at the segment end
                if frame is not None:
                    if frame.pts is None or frame.time_base is None:
                        continue
                    if first_t is None:
                        first_t = _axis_time(unit, frame.pts, Fraction(frame.time_base))
                if first_t is None:
                    continue
                for out in resampler.resample(frame):
                    data = out.to_ndarray().astype(np.float32, copy=False)
                    yield first_t + emitted / sr, (data[0] if mono else data)
                    emitted += data.shape[-1]
        except av.error.FFmpegError as exc:
            raise DecodeError(f"cannot decode audio of {unit.path.name}: {exc}") from None
        finally:
            container.close()
