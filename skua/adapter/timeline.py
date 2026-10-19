"""skua.adapter.timeline: unified time axis from EXTINF + first-frame PTS, and Timeline events
(SPEC 3.1).

The axis is EXTINF accumulated and corrected by each segment's first presentation timestamp. At
EXT-X-DISCONTINUITY the axis does not break: PTS are re-anchored and the raw jump is recorded as
``pts_jump(tagged=True)``; an untagged PTS jump becomes ``pts_jump(tagged=False)``. Timestamps
are unwrapped to the nearest representative modulo 2**33, so a PTS wrap is not a jump. A segment
that cannot be demuxed falls back to its EXTINF length and the next one re-anchors.

M0 shots are one per segment; real shot detection arrives in M2 (SPEC 3.2).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from skua.adapter.decode import DemuxProbe, param_changes, unwrap
from skua.adapter.playlist import CONFIG as PLAYLIST_CONFIG
from skua.adapter.playlist import PlaylistSegment, robust_z
from skua.schema import ContainerEvent, PlaylistEvent, Shot, Timeline

log = logging.getLogger("skua.adapter")


@dataclass(frozen=True)
class TimelineConfig:
    """Axis and container-event parameters."""

    # SPEC 3.1: PTS correction tolerance; an implementation constant, not a policy parameter.
    pts_tolerance_s: float = 0.5
    bitrate_outlier_z: float = 3.0  # SPEC 3.1: bitrate MAD z, |z| > 3
    single_keyframe_below_s: float = 2.0  # SPEC 3.2: shots < 2 s get one keyframe (midpoint)
    keyframe_fractions: tuple[float, ...] = (0.25, 0.5, 0.75)  # SPEC 3.2: otherwise 25/50/75 %


CONFIG = TimelineConfig()


@dataclass(frozen=True)
class SegmentAxis:
    """Where one segment sits on the unified axis. Adapter-internal (cache index.json)."""

    index: int
    uri: str
    start: float  # unified axis, seconds
    length: float  # seconds
    origin_pts: int | None  # raw first_pts (mod 2**33) located at ``start``; None if not demuxed
    time_base: Fraction | None


@dataclass(frozen=True)
class TimelineBuild:
    """The schema Timeline plus the adapter-internal per-segment axis."""

    timeline: Timeline
    axis: list[SegmentAxis]


def keyframe_times(start: float, end: float, config: TimelineConfig = CONFIG) -> list[float]:
    """Representative frame times of a shot (SPEC 3.2)."""
    length = end - start
    if length < config.single_keyframe_below_s:
        return [start + length / 2]
    return [start + f * length for f in config.keyframe_fractions]


def _bitrate_outliers(
    segments: Sequence[PlaylistSegment],
    sizes: Sequence[int],
    starts: Sequence[float],
    config: TimelineConfig,
) -> list[ContainerEvent]:
    rates = [8 * size / seg.duration for seg, size in zip(segments, sizes, strict=True)]
    zs = robust_z(rates, PLAYLIST_CONFIG)  # same robust z as duration_outlier (SPEC 3.1)
    if zs is None:
        return []
    return [
        ContainerEvent(start, "bitrate_outlier", {"unit": seg.uri, "z": z, "bps": round(rate)})
        for seg, start, rate, z in zip(segments, starts, rates, zs, strict=True)
        if abs(z) > config.bitrate_outlier_z
    ]


def _remap(
    events: Sequence[PlaylistEvent],
    segments: Sequence[PlaylistSegment],
    axis: Sequence[SegmentAxis],
) -> list[PlaylistEvent]:
    """Move playlist events from the EXTINF axis onto the corrected axis by their unit."""
    where = {(s.uri, s.start): a for s, a in zip(segments, axis, strict=True)}
    by_uri: dict[str, list[tuple[PlaylistSegment, SegmentAxis]]] = {}
    for s, a in zip(segments, axis, strict=True):
        by_uri.setdefault(s.uri, []).append((s, a))
    out = []
    for ev in events:
        target = where.get((ev.payload["unit"], ev.ts))
        if target is None:  # event inside a segment: nearest segment start at or before ts
            seg, target = max(
                ((s, a) for s, a in by_uri[ev.payload["unit"]] if s.start <= ev.ts),
                key=lambda pair: pair[0].start,
            )
            offset = ev.ts - seg.start
        else:
            offset = 0.0
        out.append(PlaylistEvent(target.start + offset, ev.kind, dict(ev.payload)))
    return out


def build_timeline(
    media_id: str,
    segments: Sequence[PlaylistSegment],
    probes: Sequence[DemuxProbe | None],
    sizes: Sequence[int],
    playlist_events: Sequence[PlaylistEvent],
    config: TimelineConfig = CONFIG,
) -> TimelineBuild:
    """Rebuild the unified axis (SPEC 3.1) and assemble the M0 Timeline."""
    if not (len(segments) == len(probes) == len(sizes)) or not segments:
        raise ValueError("segments, probes and sizes must be non-empty and of equal length")
    tol = config.pts_tolerance_s
    axis: list[SegmentAxis] = []
    container: list[ContainerEvent] = []
    anchor: tuple[int, float, Fraction] | None = None  # (unwrapped ticks, axis seconds, tb)
    prev: tuple[int, int, Fraction] | None = None  # previous demuxed (first, end) unwrapped
    last_probe: DemuxProbe | None = None

    for seg, probe in zip(segments, probes, strict=True):
        expected = axis[-1].start + axis[-1].length if axis else 0.0
        if probe is None:
            log.warning("segment not demuxable, EXTINF fallback", extra={"segment": seg.index})
            axis.append(SegmentAxis(seg.index, seg.uri, expected, seg.duration, None, None))
            anchor = None  # the next demuxed segment re-anchors
            prev = None
            continue

        tb = probe.time_base
        measured = float(probe.span_ticks * tb)
        # PTS corrects EXTINF rounding; a measurement far from EXTINF is not trusted.
        length = measured if abs(measured - seg.duration) <= tol else seg.duration
        raw = probe.first_pts

        if anchor is None or seg.discontinuity:
            cur = raw if prev is None else unwrap(raw, prev[1])
            if prev is not None:
                delta = float((cur - prev[1]) * tb)
                if seg.discontinuity or abs(delta) > tol:
                    container.append(
                        ContainerEvent(
                            expected,
                            "pts_jump",
                            {"unit": seg.uri, "delta_s": delta, "tagged": seg.discontinuity},
                        )
                    )
            start = expected
            anchor = (cur, start, tb)
        else:
            assert prev is not None
            cur = unwrap(raw, prev[0])
            pts_axis = anchor[1] + float((cur - anchor[0]) * anchor[2])
            if abs(pts_axis - expected) <= tol:
                start = pts_axis
            else:
                # PTS jumped without a DISCONTINUITY tag (violates RFC 8216 6.2.2, but happens).
                container.append(
                    ContainerEvent(
                        expected,
                        "pts_jump",
                        {"unit": seg.uri, "delta_s": pts_axis - expected, "tagged": False},
                    )
                )
                start = expected
                anchor = (cur, start, tb)

        if last_probe is not None:
            for kind, payload in param_changes(last_probe, probe):
                container.append(ContainerEvent(start, kind, {"unit": seg.uri, **payload}))
        last_probe = probe
        prev = (cur, cur + probe.span_ticks, tb)
        axis.append(SegmentAxis(seg.index, seg.uri, start, length, raw, tb))

    starts = [a.start for a in axis]
    container += _bitrate_outliers(segments, sizes, starts, config)
    container.sort(key=lambda e: e.ts)
    shots = [
        Shot(
            idx=i,
            start=a.start,
            end=a.start + a.length,
            keyframe_ts=keyframe_times(a.start, a.start + a.length, config),
            source_units=[a.uri],
        )
        for i, a in enumerate(axis)
    ]
    fps = next((p.video.fps for p in probes if p and p.video and p.video.fps), 0.0)
    events = sorted(_remap(playlist_events, segments, axis), key=lambda e: e.ts)
    timeline = Timeline(
        media_id=media_id,
        duration=axis[-1].start + axis[-1].length,
        fps=fps,
        shots=shots,
        container_events=container,
        playlist_events=events,
    )
    return TimelineBuild(timeline, axis)
