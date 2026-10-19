"""skua.evidence.structural: evidence A — 10 structural features per shot and ad hard priors
(SPEC 3.3).

Input is the Timeline only: shots, their ``source_units`` and the events' ``unit`` fields, never
segment numbers or PTS (AGENT.md 5.2). This module only reports facts and hard priors; turning
priors into skip segments is ``rule_decoder.py`` (M0) and the Semi-Markov decoder (M3).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from skua.schema import CONFIG as SCHEMA_CONFIG
from skua.schema import ContainerEvent, PlaylistEvent, Shot, ShotFeatures, Timeline

Event = PlaylistEvent | ContainerEvent

# Column order of ShotFeatures.struct (SPEC 3.3, in the order the SPEC lists them).
STRUCT_FIELDS: tuple[str, ...] = (
    "start_discontinuity",
    "start_pts_jump",
    "start_video_params",
    "start_audio_params",
    "duration_outlier_z",
    "uri_pattern_break",
    "key_change",
    "bitrate_outlier_z",
    "log1p_nearest_event_s",
    "daterange_marker",
)


@dataclass(frozen=True)
class StructuralConfig:
    """SPEC 3.3 parameters. Skip-policy related: not to be tuned without the owner (CLAUDE.md)."""

    event_window_s: float = 0.5  # SPEC 3.3: event belongs to a shot if ts is within start ± 0.5 s
    block_max_s: float = 180.0  # SPEC 3.3: blocks longer than this never get a block prior
    hard_prior: float = 4.0  # SPEC 3.3: decoder log-likelihood bonus of a hard prior
    daterange_ad_class: str = "ad"  # SPEC 3.1 / 3.3: CLASS=ad
    struct_dim: int = SCHEMA_CONFIG.struct_dim  # SPEC 3.3: 10


CONFIG = StructuralConfig()


@dataclass(frozen=True)
class BlockConditions:
    """The four block conditions of SPEC 3.3, each with its observed value."""

    start_discontinuity: bool
    start_video_params: bool
    uri_pattern_break_hits: int
    segments: int
    duration_s: float
    within_max_duration: bool
    is_largest_block: bool

    @property
    def met(self) -> bool:
        return (
            self.start_discontinuity
            and self.start_video_params
            and self.uri_pattern_break_hits == self.segments
            and self.within_max_duration
            and not self.is_largest_block
        )


@dataclass(frozen=True)
class PriorRange:
    """A run of shots that receives the ``ad`` hard prior, with why."""

    source: Literal["daterange", "block"]
    shots: tuple[int, ...]
    first_unit: str
    last_unit: str
    start: float
    end: float
    conditions: BlockConditions | None = None  # block source
    daterange: dict[str, Any] | None = None  # daterange source: id, class, scte35, ...

    @property
    def duration_s(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class Block:
    """A discontinuity block: shots between two DISCONTINUITY tags or the playlist ends."""

    shots: tuple[int, ...]
    units: tuple[str, ...]
    conditions: BlockConditions


@dataclass(frozen=True)
class StructuralEvidence:
    """Evidence A for one Timeline."""

    features: list[ShotFeatures]
    hard_prior: dict[int, float]  # shot_idx -> log-likelihood bonus (AGENT.md 10.4)
    ranges: list[PriorRange]
    blocks: list[Block] = field(default_factory=list)


# --- helpers ---------------------------------------------------------------------------------


def _all_events(tl: Timeline) -> list[Event]:
    return [*tl.playlist_events, *tl.container_events]


def _units_with(events: Iterable[Event], kind: str) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = {}
    for ev in events:
        if ev.kind == kind:
            out.setdefault(str(ev.payload["unit"]), []).append(ev)
    return out


def _at_start(shot: Shot, events: Iterable[Event], kind: str, window: float) -> bool:
    # Only the shot start counts: a tag applies to the segment after it, so the same event
    # never lands on two shots (SPEC 3.3).
    return any(ev.kind == kind and abs(ev.ts - shot.start) <= window for ev in events)


def _signed_extreme_z(shot: Shot, by_unit: dict[str, list[Event]]) -> float:
    """The z of largest magnitude, sign kept: short segments and low bitrates (typical ads)
    must not look like the high-bitrate GOP head of a content block (SPEC 3.3)."""
    zs = [float(ev.payload["z"]) for u in shot.source_units for ev in by_unit.get(u, [])]
    return max(zs, key=abs, default=0.0)


def _hit(shot: Shot, by_unit: dict[str, list[Event]]) -> bool:
    return any(u in by_unit for u in shot.source_units)


def _scte35(ev: Event) -> list[str]:
    return [str(x) for x in ev.payload.get("scte35") or []]


def is_ad_marker(ev: Event, config: StructuralConfig = CONFIG) -> bool:
    """CLASS=ad or SCTE35-OUT, and never anything carrying SCTE35-IN (SPEC 3.3)."""
    if ev.kind != "daterange":
        return False
    scte35 = _scte35(ev)
    if "in" in scte35:
        return False  # marks where content resumes: never a hard prior
    klass = ev.payload.get("class")
    is_ad_class = isinstance(klass, str) and klass.lower() == config.daterange_ad_class
    return is_ad_class or "out" in scte35


def _is_daterange_feature(ev: Event, config: StructuralConfig) -> bool:
    # Feature dim 9: ad markers, plus SCTE35-CMD which M0 records but does not act on.
    return ev.kind == "daterange" and (is_ad_marker(ev, config) or "cmd" in _scte35(ev))


# --- blocks and priors -----------------------------------------------------------------------


def discontinuity_blocks(tl: Timeline, config: StructuralConfig = CONFIG) -> list[Block]:
    """Split the shots into discontinuity blocks and evaluate the SPEC 3.3 block conditions."""
    events = _all_events(tl)
    disc = _units_with(events, "discontinuity")
    vparams = _units_with(events, "video_params")
    breaks = _units_with(events, "uri_pattern_break")

    groups: list[list[Shot]] = []
    for shot in tl.shots:
        if not groups or shot.source_units[0] in disc:
            groups.append([])
        groups[-1].append(shot)

    durations = [g[-1].end - g[0].start for g in groups]
    longest = max(durations, default=0.0)
    blocks = []
    for group, duration in zip(groups, durations, strict=True):
        units = tuple(u for s in group for u in s.source_units)
        head = group[0].source_units[0]
        conditions = BlockConditions(
            start_discontinuity=head in disc,
            start_video_params=head in vparams,
            uri_pattern_break_hits=sum(u in breaks for u in units),
            segments=len(units),
            duration_s=duration,
            within_max_duration=duration <= config.block_max_s,
            # Ties count as largest: fewer priors is the safe side.
            is_largest_block=duration >= longest,
        )
        blocks.append(Block(tuple(s.idx for s in group), units, conditions))
    return blocks


def _daterange_ranges(tl: Timeline, config: StructuralConfig) -> list[PriorRange]:
    """Contiguous shot runs covered by ad markers, per DATERANGE id.

    An SCTE35-OUT without any length covers only its anchor segment in the playlist layer;
    here a later SCTE35-IN (same ID first, else the next one) closes it instead (SPEC 3.3).
    """
    shot_of_unit = {u: s for s in tl.shots for u in s.source_units}
    order = [s.idx for s in tl.shots]
    dateranges = [e for e in tl.playlist_events if e.kind == "daterange"]
    ins = [e for e in dateranges if "in" in _scte35(e)]

    covered: dict[str, list[int]] = {}
    meta: dict[str, dict[str, Any]] = {}
    for ev in dateranges:
        if not is_ad_marker(ev, config):
            continue
        key = str(ev.payload.get("id") or f"@{ev.ts}")
        shot = shot_of_unit.get(str(ev.payload["unit"]))
        if shot is None:
            continue
        covered.setdefault(key, []).append(shot.idx)
        meta.setdefault(
            key,
            {
                "id": ev.payload.get("id"),
                "class": ev.payload.get("class"),
                "scte35": _scte35(ev),
                "anchored_by": ev.payload.get("anchored_by"),
                "duration_source": ev.payload.get("duration_source"),
                "closed_by_scte35_in": False,
            },
        )
        open_ended = ev.payload.get("duration") is None and "out" in _scte35(ev)
        if open_ended:
            later = [i for i in ins if i.ts > ev.ts]
            same_id = [i for i in later if i.payload.get("id") == ev.payload.get("id")]
            candidates = same_id or later
            closer = candidates[0] if candidates else None
            if closer is not None and str(closer.payload["unit"]) in shot_of_unit:
                end_idx = shot_of_unit[str(closer.payload["unit"])].idx
                covered[key].extend(i for i in order if shot.idx < i < end_idx)
                meta[key]["closed_by_scte35_in"] = True

    ranges = []
    for key, idxs in covered.items():
        for run in _runs(sorted(set(idxs))):
            first, last = tl.shots[run[0]], tl.shots[run[-1]]
            ranges.append(
                PriorRange(
                    "daterange",
                    tuple(run),
                    first.source_units[0],
                    last.source_units[-1],
                    first.start,
                    last.end,
                    daterange=meta[key],
                )
            )
    return ranges


def _runs(idxs: Sequence[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for i in idxs:
        if runs and runs[-1][-1] == i - 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


# --- features --------------------------------------------------------------------------------


def _nearest_event_s(shot: Shot, event_ts: Sequence[float], fallback: float) -> float:
    if not event_ts:
        return fallback  # no events at all: the farthest any shot can be
    return min(abs(t - shot.start) for t in event_ts)


def struct_features(tl: Timeline, config: StructuralConfig = CONFIG) -> list[ShotFeatures]:
    """The 10 SPEC 3.3 dims per shot, column order ``STRUCT_FIELDS``."""
    events = _all_events(tl)
    w = config.event_window_s
    dur_z = _units_with(events, "duration_outlier")
    breaks = _units_with(events, "uri_pattern_break")
    keys = _units_with(events, "key_change")
    bitrate = _units_with(events, "bitrate_outlier")
    marks = {str(e.payload["unit"]) for e in tl.playlist_events if _is_daterange_feature(e, config)}
    event_ts = sorted(e.ts for e in events)
    out = []
    for shot in tl.shots:
        row = [
            _at_start(shot, events, "discontinuity", w),
            _at_start(shot, events, "pts_jump", w),
            _at_start(shot, events, "video_params", w),
            _at_start(shot, events, "audio_params", w),
            _signed_extreme_z(shot, dur_z),
            _hit(shot, breaks),
            _hit(shot, keys),
            _signed_extreme_z(shot, bitrate),
            math.log1p(_nearest_event_s(shot, event_ts, tl.duration)),
            any(u in marks for u in shot.source_units),
        ]
        vector = np.asarray([float(x) for x in row], dtype=np.float32)
        out.append(ShotFeatures(shot_idx=shot.idx, struct=vector))
    return out


def structural_evidence(tl: Timeline, config: StructuralConfig = CONFIG) -> StructuralEvidence:
    """Features, hard priors and their provenance for one Timeline (SPEC 3.3)."""
    blocks = discontinuity_blocks(tl, config)
    ranges = _daterange_ranges(tl, config)
    for block in blocks:
        if block.conditions.met:
            first, last = tl.shots[block.shots[0]], tl.shots[block.shots[-1]]
            ranges.append(
                PriorRange(
                    "block",
                    block.shots,
                    block.units[0],
                    block.units[-1],
                    first.start,
                    last.end,
                    conditions=block.conditions,
                )
            )
    ranges.sort(key=lambda r: (r.start, r.source))
    prior = {idx: config.hard_prior for r in ranges for idx in r.shots}
    return StructuralEvidence(struct_features(tl, config), prior, ranges, blocks)
