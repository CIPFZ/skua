"""skua.adapter.playlist: parse HLS playlists and derive the five PlaylistEvent kinds (SPEC 3.1).

Pure functions over playlist text: fetching is ``fetch.py``'s job. Event timestamps here are on
the EXTINF-accumulated axis; ``timeline.py`` later moves them onto the PTS-corrected axis by
their ``unit``. Segment indices exist only inside this package (AGENT.md 5.2).
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import m3u8

from skua.adapter.proc import host_hash
from skua.schema import PlaylistEvent

log = logging.getLogger("skua.adapter")


class UnsupportedPlaylistError(ValueError):
    """A playlist feature outside the M0 scope (SPEC 3.1): fMP4, SAMPLE-AES, live, ..."""


@dataclass(frozen=True)
class PlaylistConfig:
    """Playlist-event parameters; defaults are SPEC 3.1 unless noted."""

    outlier_z: float = 3.0  # SPEC 3.1 duration_outlier: |z| > 3
    # Consistency constant of the standard MAD robust z-score, (x - median) / (1.4826 * MAD);
    # not a tuning knob: it makes the scale equal sigma for normal data.
    mad_to_sigma: float = 1.4826
    meanad_to_sigma: float = 1.2533  # SPEC 3.1 fallback when MAD = 0 (mean |x - median|)
    template_min_len: int = 8  # SPEC 3.1: hex / base64 runs of >= 8 chars become '#'
    signature_majority_min: float = 0.6  # SPEC 3.1: below this the URI signal is disabled
    daterange_ad_class: str = "ad"  # SPEC 3.1: EXT-X-DATERANGE CLASS=ad


CONFIG = PlaylistConfig()

_UNSUPPORTED_KEY_METHODS = frozenset({"SAMPLE-AES", "SAMPLE-AES-CTR"})
_SUPPORTED_KEY_METHODS = frozenset({"AES-128"})
_DIGITS = re.compile(r"[0-9]+")
# Base64 alphabet without '/', which cannot occur inside a file name; hex is a subset of it.
_B64_RUN = re.compile(r"[A-Za-z0-9+=]+")
_SCTE35_ATTRS = (("scte35_out", "out"), ("scte35_cmd", "cmd"), ("scte35_in", "in"))
_EPS = 1e-6  # float slack when comparing EXTINF sums


@dataclass(frozen=True)
class KeyState:
    """Effective EXT-X-KEY of a segment; METHOD=NONE is represented as no key (None)."""

    method: str
    uri: str | None
    iv: str | None
    keyformat: str | None
    keyformatversions: str | None


@dataclass(frozen=True)
class PlaylistSegment:
    """One media segment. Adapter-internal: never crosses the adapter boundary."""

    index: int
    uri: str  # as written in the playlist; this is the event / shot ``unit``
    absolute_uri: str
    duration: float  # EXTINF
    start: float  # EXTINF-accumulated axis, seconds
    discontinuity: bool  # EXT-X-DISCONTINUITY applies to this segment (RFC 8216 4.3.2.3)
    key: KeyState | None


@dataclass(frozen=True)
class MediaPlaylist:
    """Parsed media playlist plus its playlist events, sorted by ``ts``."""

    segments: list[PlaylistSegment]
    events: list[PlaylistEvent]
    target_duration: float | None
    uri_signature_enabled: bool  # False when no signature reaches the majority threshold


# --- master playlist ------------------------------------------------------------------------


def is_master_playlist(text: str) -> bool:
    """True for a master (multivariant) playlist."""
    return bool(m3u8.loads(text).is_variant)


def select_variant(text: str, base_uri: str) -> str:
    """Absolute URI of the highest-BANDWIDTH variant (SPEC 3.1); first wins on ties."""
    pl = m3u8.loads(text, uri=base_uri)
    if not pl.is_variant:
        raise ValueError("not a master playlist")
    variants = [p for p in pl.playlists if p.stream_info.bandwidth is not None]
    if not variants:
        raise UnsupportedPlaylistError("master playlist has no variant with BANDWIDTH")
    # I-frame playlists live in pl.iframe_playlists and are never candidates.
    best = max(variants, key=lambda p: int(p.stream_info.bandwidth))
    return str(best.absolute_uri)


# --- URI signature (uri_pattern_break) ------------------------------------------------------


def filename_template(name: str, config: PlaylistConfig = CONFIG) -> str:
    """Template of a file name: hex / base64 runs of >= N chars with a digit, then digit runs,
    become '#' (SPEC 3.1)."""

    def hashy(m: re.Match[str]) -> str:
        run = m.group(0)
        if len(run) >= config.template_min_len and any(c.isdigit() for c in run):
            return "#"
        return run

    return _DIGITS.sub("#", _B64_RUN.sub(hashy, name))


def uri_signature(absolute_uri: str, config: PlaylistConfig = CONFIG) -> str:
    """``h:<host hash>|d:<directory depth>|t:<file name template>``, query/fragment dropped.

    The host enters only as a hash so signatures can be stored and logged (AGENT.md 5.6).
    """
    parts = urlsplit(absolute_uri)  # query and fragment are separate fields, never used
    path = parts.path
    dirs, _, name = path.rpartition("/")
    depth = len([d for d in dirs.split("/") if d])
    host = host_hash(f"{parts.scheme}://{parts.netloc}/") if parts.netloc else "local"
    return f"h:{host}|d:{depth}|t:{filename_template(name, config)}"


def _uri_breaks(
    segments: Sequence[PlaylistSegment], config: PlaylistConfig
) -> tuple[bool, list[PlaylistEvent]]:
    sigs = [uri_signature(s.absolute_uri, config) for s in segments]
    if not sigs:
        return True, []
    majority, count = Counter(sigs).most_common(1)[0]
    share = count / len(sigs)
    if share < config.signature_majority_min:
        # Unreliable signatures: report nothing rather than noise (SPEC 3.1, 3.3).
        log.warning(
            "uri_pattern_break disabled: majority signature covers too few segments",
            extra={"share": round(share, 3), "threshold": config.signature_majority_min},
        )
        return False, []
    events = [
        PlaylistEvent(
            seg.start,
            "uri_pattern_break",
            {"unit": seg.uri, "signature": sig, "majority": majority},
        )
        for seg, sig in zip(segments, sigs, strict=True)
        if sig != majority
    ]
    return True, events


# --- EXTINF outliers (duration_outlier) -----------------------------------------------------


def robust_z(values: Sequence[float], config: PlaylistConfig = CONFIG) -> list[float] | None:
    """MAD robust z-scores; MAD = 0 falls back to mean absolute deviation; None if both are 0."""
    if not values:
        return None
    med = statistics.median(values)
    deviations = [abs(v - med) for v in values]
    mad = statistics.median(deviations)
    if mad > 0:
        scale = config.mad_to_sigma * mad
    else:
        mean_ad = statistics.fmean(deviations)
        if mean_ad <= 0:
            return None
        scale = config.meanad_to_sigma * mean_ad
    return [(v - med) / scale for v in values]


def _duration_outliers(
    segments: Sequence[PlaylistSegment], config: PlaylistConfig
) -> list[PlaylistEvent]:
    # The last segment is legitimately short in HLS: it neither enters the statistics nor is
    # judged (SPEC 3.1).
    judged = segments[:-1]
    zs = robust_z([s.duration for s in judged], config)
    if zs is None:
        return []
    return [
        PlaylistEvent(seg.start, "duration_outlier", {"unit": seg.uri, "z": z})
        for seg, z in zip(judged, zs, strict=True)
        if abs(z) > config.outlier_z
    ]


# --- keys, discontinuities, dateranges ------------------------------------------------------


def _key_state(key: Any) -> KeyState | None:
    if key is None or key.method is None or str(key.method).upper() == "NONE":
        return None
    method = str(key.method).upper()
    if method in _UNSUPPORTED_KEY_METHODS:
        raise UnsupportedPlaylistError(f"EXT-X-KEY METHOD={method} is not supported in M0")
    if method not in _SUPPORTED_KEY_METHODS:
        raise UnsupportedPlaylistError(f"unknown EXT-X-KEY METHOD={method}")
    return KeyState(method, key.uri, key.iv, key.keyformat, key.keyformatversions)


def _key_changes(segments: Sequence[PlaylistSegment]) -> list[PlaylistEvent]:
    # The first segment's key state is the baseline (the first KEY is not a change); every
    # later change counts, including switching between encrypted and clear (SPEC 3.1).
    return [
        PlaylistEvent(
            cur.start,
            "key_change",
            {"unit": cur.uri, "method": cur.key.method if cur.key else "NONE"},
        )
        for prev, cur in zip(segments, segments[1:], strict=False)
        if cur.key != prev.key
    ]


def _parse_date(value: object) -> datetime | None:
    """ISO 8601 / RFC 3339 date-time with an offset; anything else is unusable (None)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _span(dr: Any) -> tuple[float | None, str | None]:
    """Range length in seconds and its source, in RFC 8216 4.3.2.7 order (SPEC 3.1)."""
    if dr.duration is not None:
        return float(dr.duration), "DURATION"
    start, end = _parse_date(dr.start_date), _parse_date(dr.end_date)
    if start is not None and end is not None:
        return (end - start).total_seconds(), "END-DATE"
    if dr.planned_duration is not None:
        return float(dr.planned_duration), "PLANNED-DURATION"
    return None, None


def _covered_by_wall_clock(
    start: datetime,
    span: float | None,
    segments: Sequence[PlaylistSegment],
    walls: Sequence[datetime | None],
) -> list[PlaylistSegment]:
    covered = []
    for seg, wall in zip(segments, walls, strict=True):
        if wall is None:
            continue  # before the first PROGRAM-DATE-TIME: not mappable
        begin = (wall - start).total_seconds()  # segment start relative to START-DATE
        if span is None or span <= 0:
            # A point (no length, or DURATION=0): the segment containing START-DATE.
            if begin <= _EPS and begin + seg.duration > _EPS:
                covered.append(seg)
        elif begin < span - _EPS and begin + seg.duration > _EPS:
            covered.append(seg)
    return covered


def _covered_by_position(
    i: int, span: float | None, segments: Sequence[PlaylistSegment]
) -> list[PlaylistSegment]:
    anchor = segments[i]
    if span is None or span <= 0:
        return [anchor]
    end = anchor.start + span
    return [s for s in segments[i:] if s.start < end - _EPS]


def _dateranges(
    pl: Any, segments: Sequence[PlaylistSegment], config: PlaylistConfig
) -> list[PlaylistEvent]:
    """One event per covered segment for every DATERANGE with CLASS=ad or any SCTE35 attribute.

    START-DATE is wall-clock time: with EXT-X-PROGRAM-DATE-TIME present it is mapped onto
    segments through the PDT timeline and the tag's position is ignored (RFC 8216 4.3.2.7);
    only a playlist without any PDT falls back to the segment the tag precedes (SPEC 3.1).
    Facts only: which SCTE35 attributes are present is kept in the payload; deciding which of
    them become hard labels is evidence/structural's job (SPEC 3.3).
    """
    walls: list[datetime | None] = [raw.current_program_date_time for raw in pl.segments]
    has_pdt = any(w is not None for w in walls)
    events: list[PlaylistEvent] = []
    for i, raw in enumerate(pl.segments):
        for dr in raw.dateranges or []:
            klass = dr.class_
            scte35 = [name for attr, name in _SCTE35_ATTRS if getattr(dr, attr, None)]
            is_ad_class = klass is not None and klass.lower() == config.daterange_ad_class
            if not (is_ad_class or scte35):
                continue
            span, span_source = _span(dr)
            if has_pdt:
                start = _parse_date(dr.start_date)
                if start is None or any(w is not None and w.tzinfo is None for w in walls):
                    log.warning("DATERANGE skipped: START-DATE or PDT lacks a usable offset")
                    continue
                covered = _covered_by_wall_clock(start, span, segments, walls)
                anchored_by = "program-date-time"
            else:
                covered = _covered_by_position(i, span, segments)
                anchored_by = "position"
            if not covered:
                log.warning("DATERANGE skipped: START-DATE maps to no segment of this playlist")
                continue
            for seg in covered:
                events.append(
                    PlaylistEvent(
                        seg.start,
                        "daterange",
                        {
                            "unit": seg.uri,
                            "id": dr.id,
                            "class": klass,
                            "scte35": scte35,
                            "anchor": covered[0].uri,
                            "anchored_by": anchored_by,
                            "duration": span,
                            "duration_source": span_source,
                        },
                    )
                )
    return events


# --- media playlist -------------------------------------------------------------------------

_KIND_ORDER = {
    "discontinuity": 0,
    "key_change": 1,
    "uri_pattern_break": 2,
    "duration_outlier": 3,
    "daterange": 4,
}


def parse_media_playlist(
    text: str, base_uri: str, config: PlaylistConfig = CONFIG
) -> MediaPlaylist:
    """Parse a VOD media playlist (MPEG-TS, clear or AES-128) and derive its events."""
    pl = m3u8.loads(text, uri=base_uri)
    if pl.is_variant:
        raise ValueError("master playlist given; call select_variant first")
    if not pl.is_endlist:
        raise UnsupportedPlaylistError("live / EVENT playlists without EXT-X-ENDLIST (SPEC 0)")
    if pl.segment_map or any(s.init_section is not None for s in pl.segments):
        raise UnsupportedPlaylistError("fMP4 (EXT-X-MAP) is not supported until M1 (SPEC 3.1)")
    if any(s.byterange for s in pl.segments):
        raise UnsupportedPlaylistError("EXT-X-BYTERANGE segments are not supported in M0")
    if not pl.segments:
        raise UnsupportedPlaylistError("media playlist has no segments")

    segments: list[PlaylistSegment] = []
    clock = 0.0
    for i, raw in enumerate(pl.segments):
        duration = float(raw.duration or 0.0)
        if duration <= 0:
            raise UnsupportedPlaylistError(f"segment {i}: non-positive EXTINF {raw.duration!r}")
        segments.append(
            PlaylistSegment(
                index=i,
                uri=str(raw.uri),
                absolute_uri=str(raw.absolute_uri),
                duration=duration,
                start=clock,
                discontinuity=bool(raw.discontinuity),
                key=_key_state(raw.key),
            )
        )
        clock += duration

    enabled, breaks = _uri_breaks(segments, config)
    events = [
        PlaylistEvent(s.start, "discontinuity", {"unit": s.uri})
        for s in segments
        if s.discontinuity
    ]
    events += _key_changes(segments)
    events += breaks
    events += _duration_outliers(segments, config)
    events += _dateranges(pl, segments, config)
    events.sort(key=lambda e: (e.ts, _KIND_ORDER[e.kind]))
    target = float(pl.target_duration) if pl.target_duration is not None else None
    return MediaPlaylist(segments, events, target, enabled)
