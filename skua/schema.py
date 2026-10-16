"""skua.schema: SPEC chapter 2 data model, strict JSON serialization, parquet FEATURE_SCHEMA.

Every module talks to every other module only through the dataclasses defined here (AGENT.md 5.2).
Serialization is strict on purpose: unknown fields, wrong types, non-finite numbers and duplicate
keys are errors, never silently dropped or coerced, so that a producer bug cannot quietly turn
into a wrong skip decision downstream.

JSON documents carry a top-level ``schema_version`` (format of the file, ``SCHEMA_VERSION``);
this is unrelated to ``SkipList.version`` (revision number of one media's skip list, SPEC 2).
Golden annotation files are SkipList documents with two extra top-level metadata fields,
``annotator`` and ``annotated_at`` (SPEC 2, 3.7).
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar, Union, get_args, get_origin, get_type_hints

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SchemaConfig:
    """Fixed dimensions of the data contract; changing any of them requires a schema bump."""

    struct_dim: int = 10  # SPEC 2 ShotFeatures.struct, SPEC 3.3 (10 dims)
    media_id_hex_len: int = 16  # SPEC 2 Timeline.media_id (sha256 prefix, 16 hex)
    min_keyframes: int = 1  # SPEC 2 Shot.keyframe_ts ("1–3 个代表帧")
    max_keyframes: int = 3  # SPEC 2 Shot.keyframe_ts
    golden_confidence: float = 1.0  # SPEC 2 Segment.confidence ("golden 标注中固定为 1.0")


CONFIG = SchemaConfig()

ContainerEventKind = Literal[
    "pts_jump", "video_params", "audio_params", "pid_change", "bitrate_outlier"
]
PlaylistEventKind = Literal[
    "discontinuity", "key_change", "duration_outlier", "uri_pattern_break", "daterange"
]
SegmentLabel = Literal["content", "ad", "intro", "recap", "outro", "trailer"]

CONTAINER_EVENT_KINDS: tuple[str, ...] = get_args(ContainerEventKind)
PLAYLIST_EVENT_KINDS: tuple[str, ...] = get_args(PlaylistEventKind)
SEGMENT_LABELS: tuple[str, ...] = get_args(SegmentLabel)

SCHEMA_VERSION_KEY = "schema_version"
GOLDEN_META_KEYS: tuple[str, str] = ("annotator", "annotated_at")
PARQUET_VERSION_KEY = b"skua_schema_version"

_MEDIA_ID_RE = re.compile(rf"[0-9a-f]{{{CONFIG.media_id_hex_len}}}")

Float32Array = npt.NDArray[np.float32]


class SchemaError(ValueError):
    """Input does not conform to the SPEC chapter 2 data model."""


# --- validation helpers ---------------------------------------------------------------------


_NUMBER_TYPES = (int, float, np.integer, np.floating)


def _number(name: str, value: object) -> float:
    # numpy scalars are accepted and converted, so producers need no casts and JSON stays clean.
    if isinstance(value, bool | np.bool_) or not isinstance(value, _NUMBER_TYPES):
        raise SchemaError(f"{name}: expected a number, got {type(value).__name__}")
    out = float(value)
    if not math.isfinite(out):
        raise SchemaError(f"{name}: must be finite, got {out}")
    return out


def _non_negative(name: str, value: object) -> float:
    out = _number(name, value)
    if out < 0:
        raise SchemaError(f"{name}: must be >= 0, got {out}")
    return out


def _int(name: str, value: object, *, minimum: int) -> int:
    if isinstance(value, bool | np.bool_) or not isinstance(value, int | np.integer):
        raise SchemaError(f"{name}: expected int, got {type(value).__name__}")
    out = int(value)
    if out < minimum:
        raise SchemaError(f"{name}: must be >= {minimum}, got {out}")
    return out


def _choice(name: str, value: object, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise SchemaError(f"{name}: {value!r} is not one of {list(allowed)}")


def _media_id(name: str, value: object) -> None:
    if not isinstance(value, str) or not _MEDIA_ID_RE.fullmatch(value):
        raise SchemaError(f"{name}: expected {CONFIG.media_id_hex_len} lowercase hex chars")


def _json_value(path: str, value: object) -> None:
    """Free-form payloads must survive a JSON round trip unchanged (no tuples, numpy, NaN)."""
    if value is None or isinstance(value, bool | str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaError(f"{path}: must be finite, got {value}")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _json_value(f"{path}[{i}]", item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaError(f"{path}: keys must be str, got {type(key).__name__}")
            _json_value(f"{path}.{key}", item)
        return
    raise SchemaError(f"{path}: {type(value).__name__} is not a JSON value")


def _json_object(name: str, value: object) -> None:
    if not isinstance(value, dict):
        raise SchemaError(f"{name}: expected an object, got {type(value).__name__}")
    _json_value(name, value)


def _event_payload(name: str, payload: object) -> None:
    _json_object(name, payload)
    assert isinstance(payload, dict)
    unit = payload.get("unit")
    if not isinstance(unit, str) or not unit:
        raise SchemaError(f"{name}: must contain a non-empty 'unit' (SPEC 2)")


def _float32_vector(name: str, value: object, dim: int | None) -> None:
    if not isinstance(value, np.ndarray):
        raise SchemaError(f"{name}: expected numpy.ndarray, got {type(value).__name__}")
    if value.dtype != np.float32 or value.ndim != 1:
        raise SchemaError(f"{name}: expected 1-D float32, got {value.ndim}-D {value.dtype}")
    if dim is not None and value.shape[0] != dim:
        raise SchemaError(f"{name}: expected {dim} dims, got {value.shape[0]}")
    if not np.isfinite(value).all():
        raise SchemaError(f"{name}: must be finite")


def _items_of(name: str, items: object, cls: type) -> list[Any]:
    if not isinstance(items, list):
        raise SchemaError(f"{name}: expected a list, got {type(items).__name__}")
    for i, item in enumerate(items):
        if not isinstance(item, cls):
            raise SchemaError(f"{name}[{i}]: expected {cls.__name__}, got {type(item).__name__}")
    return items


def _non_decreasing(name: str, values: Sequence[float]) -> None:
    for i in range(1, len(values)):
        if values[i] < values[i - 1]:
            raise SchemaError(f"{name}: must be sorted by time, item {i} goes backwards")


# --- SPEC chapter 2 dataclasses -------------------------------------------------------------


@dataclass
class Shot:
    """A run of frames on the unified timeline; M0 uses one shot per HLS segment."""

    idx: int
    start: float
    end: float  # unified timeline, seconds
    keyframe_ts: list[float]  # 1–3 representative frame timestamps
    source_units: list[str]  # covered segment URIs (HLS) or byte ranges (MP4)

    def __post_init__(self) -> None:
        self.idx = _int("idx", self.idx, minimum=0)
        self.start = _non_negative("start", self.start)
        self.end = _number("end", self.end)
        if not self.start < self.end:
            raise SchemaError(f"end: must be > start ({self.start}), got {self.end}")
        if not isinstance(self.keyframe_ts, list):
            raise SchemaError("keyframe_ts: expected a list")
        n = len(self.keyframe_ts)
        if not CONFIG.min_keyframes <= n <= CONFIG.max_keyframes:
            raise SchemaError(
                f"keyframe_ts: expected {CONFIG.min_keyframes}–{CONFIG.max_keyframes} items, "
                f"got {n}"
            )
        self.keyframe_ts = [_number(f"keyframe_ts[{i}]", t) for i, t in enumerate(self.keyframe_ts)]
        for i, t in enumerate(self.keyframe_ts):
            if not self.start <= t <= self.end:
                raise SchemaError(f"keyframe_ts[{i}]: {t} outside [{self.start}, {self.end}]")
        _items_of("source_units", self.source_units, str)
        if not self.source_units or not all(self.source_units):
            raise SchemaError("source_units: expected at least one non-empty unit")


@dataclass
class ContainerEvent:
    """Event found while demuxing (SPEC 3.1); ``payload['unit']`` names the segment."""

    ts: float
    kind: ContainerEventKind
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        self.ts = _non_negative("ts", self.ts)
        _choice("kind", self.kind, CONTAINER_EVENT_KINDS)
        _event_payload("payload", self.payload)


@dataclass
class PlaylistEvent:
    """Event found in the playlist text (SPEC 3.1); ``payload['unit']`` names the segment."""

    ts: float
    kind: PlaylistEventKind
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        self.ts = _non_negative("ts", self.ts)
        _choice("kind", self.kind, PLAYLIST_EVENT_KINDS)
        _event_payload("payload", self.payload)


@dataclass
class Timeline:
    """Unified time axis of one media plus all structural events on it (SPEC 2, 3.1)."""

    media_id: str
    duration: float  # seconds
    fps: float
    shots: list[Shot]
    container_events: list[ContainerEvent]
    playlist_events: list[PlaylistEvent]  # HLS/DASH only

    def __post_init__(self) -> None:
        _media_id("media_id", self.media_id)
        self.duration = _non_negative("duration", self.duration)
        # 0 is allowed: an audio-only source has no frame rate.
        self.fps = _non_negative("fps", self.fps)
        shots = _items_of("shots", self.shots, Shot)
        for i, shot in enumerate(shots):
            if shot.idx != i:
                raise SchemaError(f"shots[{i}].idx: expected {i}, got {shot.idx}")
        _non_decreasing("shots", [s.start for s in shots])
        container = _items_of("container_events", self.container_events, ContainerEvent)
        _non_decreasing("container_events", [e.ts for e in container])
        playlist = _items_of("playlist_events", self.playlist_events, PlaylistEvent)
        _non_decreasing("playlist_events", [e.ts for e in playlist])


@dataclass(eq=False)
class ShotFeatures:
    """One parquet row per shot. Only ``struct`` exists in M0; the rest stays None (SPEC 2)."""

    shot_idx: int
    struct: Float32Array  # evidence A, 10 dims (SPEC 3.3)
    fp_video_hit: float | None = None  # evidence B (M1)
    fp_audio_hit: float | None = None
    fp_hit_label: str | None = None
    vis_emb: Float32Array | None = None  # DINOv2 ViT-S/14 (M2)
    aud_emb: Float32Array | None = None  # BEATs (M2)
    loudness: Float32Array | None = None  # LUFS-I, LRA, peak, silence ratio (M2)
    ood_vis: float | None = None  # per-episode OOD (M2)
    ood_aud: float | None = None
    face_outlier: float | None = None
    ocr_flags: Float32Array | None = None  # QR / ad keywords / price-phone patterns (M2)

    def __post_init__(self) -> None:
        self.shot_idx = _int("shot_idx", self.shot_idx, minimum=0)
        _float32_vector("struct", self.struct, CONFIG.struct_dim)
        for f in dataclasses.fields(self):
            value = getattr(self, f.name)
            if f.name in ("shot_idx", "struct") or value is None:
                continue
            if f.name == "fp_hit_label":
                if not isinstance(value, str):
                    raise SchemaError(f"fp_hit_label: expected str, got {type(value).__name__}")
            elif isinstance(value, np.ndarray):
                # Dimensions of the M1/M2 columns are fixed when they join FEATURE_SCHEMA.
                _float32_vector(f.name, value, None)
            else:
                setattr(self, f.name, _number(f.name, value))

    # numpy arrays make the generated __eq__ ambiguous; compare dtype and values explicitly.
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ShotFeatures):
            return NotImplemented
        for f in dataclasses.fields(self):
            a, b = getattr(self, f.name), getattr(other, f.name)
            if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
                if not (
                    isinstance(a, np.ndarray)
                    and isinstance(b, np.ndarray)
                    and a.dtype == b.dtype
                    and np.array_equal(a, b)
                ):
                    return False
            elif a != b:
                return False
        return True

    # Value equality on a mutable object: unhashable, like the generated dataclasses above.
    __hash__ = None  # type: ignore[assignment]


@dataclass
class Segment:
    """A labeled interval of the output skip list (SPEC 2)."""

    start: float
    end: float
    label: SegmentLabel
    confidence: float  # calibrated posterior; fixed 1.0 in golden files
    evidence: dict[str, Any]  # per-evidence contributions; empty in golden files

    def __post_init__(self) -> None:
        self.start = _non_negative("start", self.start)
        self.end = _number("end", self.end)
        if not self.start < self.end:
            raise SchemaError(f"end: must be > start ({self.start}), got {self.end}")
        _choice("label", self.label, SEGMENT_LABELS)
        self.confidence = _number("confidence", self.confidence)
        if not 0.0 <= self.confidence <= 1.0:
            raise SchemaError(f"confidence: must be within [0, 1], got {self.confidence}")
        _json_object("evidence", self.evidence)


@dataclass
class SkipList:
    """Skip list of one media (SPEC 2); same schema as a golden annotation file."""

    media_id: str
    version: int  # revision of this media's skip list, starts at 1; not the schema version
    segments: list[Segment]

    def __post_init__(self) -> None:
        _media_id("media_id", self.media_id)
        self.version = _int("version", self.version, minimum=1)
        segments = _items_of("segments", self.segments, Segment)
        for i in range(1, len(segments)):
            if segments[i].start < segments[i - 1].end:
                raise SchemaError(
                    f"segments[{i}]: starts at {segments[i].start}, before segments[{i - 1}] "
                    f"ends at {segments[i - 1].end} (segments must be sorted, non-overlapping)"
                )


@dataclass(frozen=True)
class GoldenMeta:
    """Top-level metadata that turns a SkipList document into a golden annotation (SPEC 2)."""

    annotator: str
    annotated_at: str  # ISO 8601, e.g. "2026-10-16" or "2026-10-16T09:30:00+08:00"

    def __post_init__(self) -> None:
        if not isinstance(self.annotator, str) or not self.annotator.strip():
            raise SchemaError("annotator: expected a non-empty string")
        if not isinstance(self.annotated_at, str):
            raise SchemaError("annotated_at: expected an ISO 8601 string")
        try:
            datetime.fromisoformat(self.annotated_at)
        except ValueError:
            raise SchemaError(f"annotated_at: not ISO 8601: {self.annotated_at!r}") from None


SchemaObject = Timeline | Shot | ContainerEvent | PlaylistEvent | ShotFeatures | Segment | SkipList
TOP_LEVEL_TYPES: tuple[type, ...] = (
    Timeline,
    Shot,
    ContainerEvent,
    PlaylistEvent,
    ShotFeatures,
    Segment,
    SkipList,
)

T = TypeVar("T", Timeline, Shot, ContainerEvent, PlaylistEvent, ShotFeatures, Segment, SkipList)


@dataclass(frozen=True)
class Document(Generic[T]):
    """A decoded JSON document: the value plus its top-level metadata."""

    value: T
    schema_version: int  # version found in the input, before migration
    golden: GoldenMeta | None = None


# --- migrations ------------------------------------------------------------------------------

Migration = Callable[[str, dict[str, Any]], dict[str, Any]]

# MIGRATIONS[n] upgrades a raw document of type name ``str`` from schema version n to n + 1.
# Version 1 is the first released format, so there is nothing to migrate yet.
MIGRATIONS: dict[int, Migration] = {}


def _migrate(type_name: str, raw: dict[str, Any], found: int) -> dict[str, Any]:
    target = SCHEMA_VERSION
    if found > target:
        raise SchemaError(
            f"{SCHEMA_VERSION_KEY}: {found} is newer than supported {target}; upgrade skua"
        )
    for version in range(found, target):
        step = MIGRATIONS.get(version)
        if step is None:
            raise SchemaError(f"{SCHEMA_VERSION_KEY}: no migration from {version} to {version + 1}")
        raw = step(type_name, raw)
    return raw


# --- JSON encoding ---------------------------------------------------------------------------


def _encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, np.ndarray):
        # float32 -> Python float is exact and repr() round-trips, so JSON is lossless here.
        return [float(x) for x in value.tolist()]
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


def to_json(
    obj: SchemaObject, *, golden: GoldenMeta | None = None, indent: int | None = None
) -> str:
    """Serialize a top-level schema object; ``golden`` is only valid for a SkipList.

    The output is decoded again before returning, so a value mutated into an invalid state
    after construction can never be written (write-side validation, AGENT.md 5.2).
    """
    if not isinstance(obj, TOP_LEVEL_TYPES):
        raise SchemaError(f"cannot serialize {type(obj).__name__}: not a SPEC chapter 2 type")
    doc: dict[str, Any] = {SCHEMA_VERSION_KEY: SCHEMA_VERSION}
    if golden is not None:
        if not isinstance(obj, SkipList):
            raise SchemaError("golden metadata is only valid on a SkipList")
        _check_golden_segments(obj)
        doc.update(annotator=golden.annotator, annotated_at=golden.annotated_at)
    doc.update(_encode(obj))
    try:
        text = json.dumps(doc, ensure_ascii=False, allow_nan=False, indent=indent)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"{type(obj).__name__}: not serializable: {exc}") from None
    cls: Any = type(obj)  # T is constrained; the isinstance check above guarantees membership
    decode(cls, text)
    return text


# --- JSON decoding ---------------------------------------------------------------------------


def _reject_duplicates(pairs: list[tuple[Any, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise SchemaError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise SchemaError(f"non-finite number {name} is not valid JSON")


@cache
def _hints(cls: type) -> dict[str, Any]:
    return get_type_hints(cls)


def _decode(tp: Any, value: Any, path: str) -> Any:
    if tp is Any:
        _json_value(path, value)
        return value
    origin = get_origin(tp)
    if origin is Union or origin is types.UnionType:
        args = get_args(tp)
        if value is None and type(None) in args:
            return None
        (inner,) = (a for a in args if a is not type(None))
        return _decode(inner, value, path)
    if value is None:
        raise SchemaError(f"{path}: null is not allowed")
    if origin is Literal:
        if not isinstance(value, str) or value not in get_args(tp):
            raise SchemaError(f"{path}: {value!r} is not one of {list(get_args(tp))}")
        return value
    if tp is int:
        if type(value) is not int:
            raise SchemaError(f"{path}: expected int, got {type(value).__name__}")
        return value
    if tp is float:
        if type(value) not in (int, float):
            raise SchemaError(f"{path}: expected number, got {type(value).__name__}")
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise SchemaError(f"{path}: expected string, got {type(value).__name__}")
        return value
    if origin is list:
        if not isinstance(value, list):
            raise SchemaError(f"{path}: expected array, got {type(value).__name__}")
        (item_tp,) = get_args(tp)
        return [_decode(item_tp, v, f"{path}[{i}]") for i, v in enumerate(value)]
    if origin is dict:
        if not isinstance(value, dict):
            raise SchemaError(f"{path}: expected object, got {type(value).__name__}")
        _, value_tp = get_args(tp)
        return {k: _decode(value_tp, v, f"{path}.{k}") for k, v in value.items()}
    if origin is np.ndarray:
        if not isinstance(value, list):
            raise SchemaError(f"{path}: expected array of numbers, got {type(value).__name__}")
        for i, v in enumerate(value):
            if type(v) not in (int, float):
                raise SchemaError(f"{path}[{i}]: expected number, got {type(v).__name__}")
        return np.asarray(value, dtype=np.float32)
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return _decode_dataclass(tp, value, path)
    raise TypeError(f"{path}: unsupported annotation {tp!r}")  # programming error, not input


def _decode_dataclass(cls: type, value: Any, path: str) -> Any:
    if not isinstance(value, dict):
        raise SchemaError(f"{path}: expected object, got {type(value).__name__}")
    hints = _hints(cls)
    fields = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(value) - set(fields))
    if unknown:
        raise SchemaError(f"{path}: unknown field(s) {unknown}")
    kwargs: dict[str, Any] = {}
    for name, f in fields.items():
        if name in value:
            kwargs[name] = _decode(hints[name], value[name], f"{path}.{name}")
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise SchemaError(f"{path}: missing required field {name!r}")
        # Absent optional fields take their default, exactly like an explicit null (SPEC 2).
    try:
        return cls(**kwargs)
    except SchemaError as exc:
        raise SchemaError(f"{path}.{exc}") from None


def _check_golden_segments(skiplist: SkipList) -> None:
    for i, seg in enumerate(skiplist.segments):
        if seg.confidence != CONFIG.golden_confidence:
            raise SchemaError(
                f"segments[{i}].confidence: golden files use {CONFIG.golden_confidence}, "
                f"got {seg.confidence}"
            )
        if seg.evidence:
            raise SchemaError(f"segments[{i}].evidence: must be empty in golden files")


def decode(cls: type[T], data: str | bytes) -> Document[T]:
    """Parse a top-level JSON document of type ``cls`` with its metadata; strict."""
    if cls not in TOP_LEVEL_TYPES:
        raise TypeError(f"{cls!r} is not a SPEC chapter 2 type")
    name = cls.__name__
    try:
        raw = json.loads(
            data, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{name}: invalid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise SchemaError(f"{name}: expected a JSON object, got {type(raw).__name__}")

    if SCHEMA_VERSION_KEY not in raw:
        raise SchemaError(f"{name}: missing {SCHEMA_VERSION_KEY!r}")
    found = raw.pop(SCHEMA_VERSION_KEY)
    if type(found) is not int or found < 1:
        raise SchemaError(f"{name}.{SCHEMA_VERSION_KEY}: expected a positive int, got {found!r}")

    golden: GoldenMeta | None = None
    present = [k for k in GOLDEN_META_KEYS if k in raw]
    if present:
        if cls is not SkipList:
            raise SchemaError(f"{name}: unknown field(s) {sorted(present)}")
        if len(present) != len(GOLDEN_META_KEYS):
            raise SchemaError(f"{name}: golden metadata needs both {list(GOLDEN_META_KEYS)}")
        meta = {k: raw.pop(k) for k in GOLDEN_META_KEYS}
        try:
            golden = GoldenMeta(**meta)
        except SchemaError as exc:
            raise SchemaError(f"{name}.{exc}") from None

    raw = _migrate(name, raw, found)
    value = _decode_dataclass(cls, raw, name)
    if golden is not None:
        try:
            _check_golden_segments(value)
        except SchemaError as exc:
            raise SchemaError(f"{name}.{exc}") from None
    return Document(value=value, schema_version=found, golden=golden)


def from_json(cls: type[T], data: str | bytes) -> T:
    """Parse a top-level JSON document of type ``cls``; strict, see module docstring."""
    return decode(cls, data).value


# --- parquet: FEATURE_SCHEMA -----------------------------------------------------------------

# M0 only carries evidence A (AGENT.md 10.2). Columns for M1/M2 fields join here with a
# SCHEMA_VERSION bump and a migration.
FEATURE_SCHEMA = pa.schema(
    [
        pa.field("shot_idx", pa.int32(), nullable=False),
        # Child named "element" because that is what parquet writes back (LIST logical type),
        # so the schema read from disk is identical to this one, not merely type-equal.
        pa.field(
            "struct",
            pa.list_(pa.field("element", pa.float32(), nullable=False), CONFIG.struct_dim),
            nullable=False,
        ),
    ],
    metadata={PARQUET_VERSION_KEY: str(SCHEMA_VERSION).encode()},
)
_FEATURE_COLUMNS = frozenset(FEATURE_SCHEMA.names)


def features_to_table(rows: Sequence[ShotFeatures]) -> pa.Table:
    """Build a validated ``pyarrow.Table`` in FEATURE_SCHEMA; unknown populated fields error."""
    for row in rows:
        if not isinstance(row, ShotFeatures):
            raise SchemaError(f"expected ShotFeatures, got {type(row).__name__}")
        for f in dataclasses.fields(row):
            if f.name not in _FEATURE_COLUMNS and getattr(row, f.name) is not None:
                raise SchemaError(
                    f"shot {row.shot_idx}: field {f.name!r} has no column in FEATURE_SCHEMA "
                    f"v{SCHEMA_VERSION}; refusing to drop it silently"
                )
    idx = pa.array([r.shot_idx for r in rows], type=pa.int32())
    flat = np.concatenate([r.struct for r in rows]) if rows else np.empty(0, np.float32)
    struct = pa.FixedSizeListArray.from_arrays(
        pa.array(flat, pa.float32()), type=FEATURE_SCHEMA.field("struct").type
    )
    table = pa.Table.from_arrays([idx, struct], schema=FEATURE_SCHEMA)
    validate_feature_table(table)
    return table


def validate_feature_table(table: pa.Table) -> None:
    """Raise SchemaError unless ``table`` matches FEATURE_SCHEMA exactly (names, types, nulls)."""
    schema = table.schema
    if schema.names != FEATURE_SCHEMA.names:
        raise SchemaError(f"columns: expected {FEATURE_SCHEMA.names}, got {schema.names}")
    for want in FEATURE_SCHEMA:
        got = schema.field(want.name)
        if not got.type.equals(want.type) or got.nullable != want.nullable:
            raise SchemaError(
                f"column {want.name!r}: expected {want.type} (nullable={want.nullable}), "
                f"got {got.type} (nullable={got.nullable})"
            )
    version = (schema.metadata or {}).get(PARQUET_VERSION_KEY)
    if version != str(SCHEMA_VERSION).encode():
        raise SchemaError(
            f"metadata {PARQUET_VERSION_KEY!r}: expected {SCHEMA_VERSION}, got {version!r}"
        )
    for name in FEATURE_SCHEMA.names:
        if table.column(name).null_count:
            raise SchemaError(f"column {name!r}: contains nulls")
    values = table.column("struct").combine_chunks().flatten()
    if values.null_count:
        raise SchemaError("column 'struct': contains null elements")
    if not np.isfinite(values.to_numpy(zero_copy_only=False)).all():
        raise SchemaError("column 'struct': must be finite")
    idx = table.column("shot_idx").to_numpy()
    if len(np.unique(idx)) != len(idx):
        raise SchemaError("column 'shot_idx': must be unique")


def write_features(path: Path, rows: Sequence[ShotFeatures]) -> None:
    """Validate and write ``rows`` to parquet atomically (``.part`` + rename)."""
    table = features_to_table(rows)
    tmp = path.with_name(path.name + ".part")
    pq.write_table(table, tmp)
    os.replace(tmp, path)


def read_features(path: Path) -> list[ShotFeatures]:
    """Read and validate a FEATURE_SCHEMA parquet file."""
    table = pq.read_table(path)
    validate_feature_table(table)
    idx = table.column("shot_idx").to_numpy()
    flat = table.column("struct").combine_chunks().flatten().to_numpy(zero_copy_only=False)
    matrix = np.asarray(flat, dtype=np.float32).reshape(-1, CONFIG.struct_dim)
    return [ShotFeatures(shot_idx=int(i), struct=matrix[row].copy()) for row, i in enumerate(idx)]


__all__ = [
    "CONFIG",
    "CONTAINER_EVENT_KINDS",
    "FEATURE_SCHEMA",
    "GOLDEN_META_KEYS",
    "MIGRATIONS",
    "PLAYLIST_EVENT_KINDS",
    "SCHEMA_VERSION",
    "SEGMENT_LABELS",
    "ContainerEvent",
    "ContainerEventKind",
    "Document",
    "GoldenMeta",
    "PlaylistEvent",
    "PlaylistEventKind",
    "SchemaConfig",
    "SchemaError",
    "SchemaObject",
    "Segment",
    "SegmentLabel",
    "Shot",
    "ShotFeatures",
    "SkipList",
    "Timeline",
    "decode",
    "features_to_table",
    "from_json",
    "read_features",
    "to_json",
    "validate_feature_table",
    "write_features",
]
