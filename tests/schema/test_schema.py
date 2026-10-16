"""tests/schema: SPEC chapter 2 dataclasses, strict JSON round trip, golden metadata, parquet."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from skua import schema
from skua.schema import (
    CONFIG,
    FEATURE_SCHEMA,
    SCHEMA_VERSION,
    ContainerEvent,
    GoldenMeta,
    PlaylistEvent,
    SchemaError,
    Segment,
    Shot,
    ShotFeatures,
    SkipList,
    Timeline,
    decode,
    features_to_table,
    from_json,
    read_features,
    to_json,
    validate_feature_table,
    write_features,
)

MID = "0123456789abcdef"


def struct(seed: float = 0.0) -> np.ndarray:
    return (np.arange(CONFIG.struct_dim, dtype=np.float32) + np.float32(seed)) / np.float32(3)


def make_timeline() -> Timeline:
    return Timeline(
        media_id=MID,
        duration=12.0,
        fps=25.0,
        shots=[
            Shot(0, 0.0, 4.0, [1.0, 2.0, 3.0], ["seg0.ts"]),
            Shot(1, 4.0, 8.0, [6.0], ["seg1.ts"]),
            Shot(2, 8.0, 12.0, [9.0, 10.0, 11.0], ["ads/ad000.ts", "ads/ad001.ts"]),
        ],
        container_events=[
            ContainerEvent(
                8.0, "pts_jump", {"unit": "ads/ad000.ts", "delta_s": -3.5, "tagged": True}
            ),
            ContainerEvent(8.0, "video_params", {"unit": "ads/ad000.ts", "width": [1280, 640]}),
        ],
        playlist_events=[
            PlaylistEvent(8.0, "discontinuity", {"unit": "ads/ad000.ts"}),
            PlaylistEvent(8.0, "uri_pattern_break", {"unit": "ads/ad000.ts", "sig": "h/2/ad#.ts"}),
        ],
    )


def make_skiplist(version: int = 1) -> SkipList:
    return SkipList(
        media_id=MID,
        version=version,
        segments=[
            Segment(8.0, 12.0, "ad", 0.95, {"structural": {"hard_prior": 4.0, "block": [2]}}),
            Segment(20.0, 30.5, "ad", 0.95, {}),
        ],
    )


def golden_skiplist() -> SkipList:
    return SkipList(MID, 1, [Segment(8.0, 12.0, "ad", 1.0, {}), Segment(40, 75, "outro", 1.0, {})])


def raw(obj: Any, **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(to_json(obj))
    doc.update(extra)
    return doc


def dumps(doc: dict[str, Any]) -> str:
    return json.dumps(doc)


# --- round trip ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "obj",
    [
        make_timeline(),
        make_skiplist(),
        Shot(0, 0.0, 1.5, [0.75], ["seg0.ts"]),
        ContainerEvent(1.0, "bitrate_outlier", {"unit": "seg3.ts", "z": 4.2}),
        PlaylistEvent(0.0, "daterange", {"unit": "seg0.ts", "class": "ad", "duration": 30.0}),
        Segment(0.0, 5.0, "intro", 0.5, {}),
        ShotFeatures(3, struct()),
        Timeline(MID, 0.0, 0.0, [], [], []),
        SkipList(MID, 7, []),
    ],
    ids=lambda o: type(o).__name__,
)
def test_round_trip_is_equal(obj: Any) -> None:
    text = to_json(obj)
    back = from_json(type(obj), text)
    assert back == obj
    assert to_json(back) == text  # byte-stable


def test_round_trip_preserves_float32_bits() -> None:
    awkward = np.array(
        [0.1, 1 / 3, 1e-38, 3.4e38, -0.0, 7.0, 2**-149, 0.2, 0.3, 123456.789], dtype=np.float32
    )
    back = from_json(ShotFeatures, to_json(ShotFeatures(0, awkward)))
    assert back.struct.dtype == np.float32
    assert back.struct.tobytes() == awkward.tobytes()


def test_round_trip_through_indent_and_bytes() -> None:
    tl = make_timeline()
    assert from_json(Timeline, to_json(tl, indent=2).encode()) == tl


def test_numpy_scalars_are_normalized_to_python() -> None:
    seg = Segment(np.float32(1.5), np.float64(2.0), "ad", np.float32(0.5), {})
    assert type(seg.start) is float and type(seg.confidence) is float
    shot = Shot(np.int64(0), 0.0, 1.0, [np.float32(0.5)], ["s.ts"])
    assert type(shot.idx) is int and type(shot.keyframe_ts[0]) is float
    assert from_json(Shot, to_json(shot)) == shot


# --- ShotFeatures: None fields (SPEC v0.2) ---------------------------------------------------

OPTIONAL_FEATURE_FIELDS = [
    f.name for f in dataclasses.fields(ShotFeatures) if f.name not in ("shot_idx", "struct")
]


def test_shot_features_optional_fields_default_to_none() -> None:
    sf = ShotFeatures(0, struct())
    assert OPTIONAL_FEATURE_FIELDS == [
        "fp_video_hit", "fp_audio_hit", "fp_hit_label", "vis_emb", "aud_emb",
        "loudness", "ood_vis", "ood_aud", "face_outlier", "ocr_flags",
    ]  # fmt: skip
    assert all(getattr(sf, name) is None for name in OPTIONAL_FEATURE_FIELDS)


def test_shot_features_none_round_trip_writes_explicit_null() -> None:
    sf = ShotFeatures(5, struct(1))
    doc = json.loads(to_json(sf))
    assert all(doc[name] is None for name in OPTIONAL_FEATURE_FIELDS)
    assert from_json(ShotFeatures, to_json(sf)) == sf


def test_shot_features_null_and_missing_decode_identically() -> None:
    full = raw(ShotFeatures(5, struct(1)))
    missing = {k: v for k, v in full.items() if k not in OPTIONAL_FEATURE_FIELDS}
    some_missing = {k: v for k, v in full.items() if k not in ("vis_emb", "ood_aud")}
    a = from_json(ShotFeatures, dumps(full))
    b = from_json(ShotFeatures, dumps(missing))
    c = from_json(ShotFeatures, dumps(some_missing))
    assert a == b == c
    assert to_json(a) == to_json(b) == to_json(c)


def test_shot_features_populated_optional_fields_round_trip() -> None:
    sf = ShotFeatures(
        2,
        struct(),
        fp_video_hit=0.75,
        fp_audio_hit=0,
        fp_hit_label="ad",
        vis_emb=np.ones(384, np.float32),
        loudness=np.array([-23.0, 7.5, -1.0, 0.1], np.float32),
        face_outlier=1.0,
    )
    back = from_json(ShotFeatures, to_json(sf))
    assert back == sf
    assert back.aud_emb is None and back.ood_vis is None and back.ocr_flags is None
    assert back.vis_emb is not None and back.vis_emb.dtype == np.float32


def test_shot_features_struct_is_required_and_never_null() -> None:
    doc = raw(ShotFeatures(0, struct()))
    with pytest.raises(SchemaError, match="null is not allowed"):
        from_json(ShotFeatures, dumps({**doc, "struct": None}))
    del doc["struct"]
    with pytest.raises(SchemaError, match="missing required field 'struct'"):
        from_json(ShotFeatures, dumps(doc))


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (np.zeros(9, np.float32), "expected 10 dims"),
        (np.zeros(10, np.float64), "float32"),
        (np.zeros((2, 5), np.float32), "1-D"),
        (np.full(10, np.nan, np.float32), "finite"),
        ([0.0] * 10, "numpy.ndarray"),
    ],
)
def test_shot_features_struct_validation(value: Any, match: str) -> None:
    with pytest.raises(SchemaError, match=match):
        ShotFeatures(0, value)


def test_shot_features_equality_is_value_based() -> None:
    with pytest.raises(TypeError):
        hash(ShotFeatures(0, struct()))
    assert ShotFeatures(0, struct()) == ShotFeatures(0, struct())
    assert ShotFeatures(0, struct()) != ShotFeatures(0, struct(1))
    assert ShotFeatures(0, struct()) != ShotFeatures(0, struct(), ood_vis=0.0)
    assert ShotFeatures(0, struct(), vis_emb=np.zeros(3, np.float32)) != ShotFeatures(0, struct())


# --- schema_version vs SkipList.version (SPEC v0.2) ------------------------------------------


def test_schema_version_is_top_level_metadata_not_a_field() -> None:
    doc = raw(make_skiplist(version=3))
    assert doc["schema_version"] == SCHEMA_VERSION == 1
    assert doc["version"] == 3
    assert "schema_version" not in {f.name for f in dataclasses.fields(SkipList)}


def test_skiplist_version_is_independent_of_schema_version() -> None:
    for revision in (1, 2, 42):
        d = decode(SkipList, to_json(make_skiplist(version=revision)))
        assert d.value.version == revision
        assert d.schema_version == SCHEMA_VERSION


@pytest.mark.parametrize("bad", [0, -1, 1.0, "1", True, None])
def test_skiplist_version_must_be_positive_int(bad: Any) -> None:
    with pytest.raises(SchemaError, match="version"):
        from_json(SkipList, dumps(raw(make_skiplist(), version=bad)))


@pytest.mark.parametrize("bad", [0, -1, 1.0, "1", True, None])
def test_schema_version_must_be_positive_int(bad: Any) -> None:
    with pytest.raises(SchemaError, match="schema_version"):
        from_json(SkipList, dumps(raw(make_skiplist(), schema_version=bad)))


def test_schema_version_required_on_every_document() -> None:
    for obj in (make_timeline(), make_skiplist(), ShotFeatures(0, struct())):
        doc = raw(obj)
        del doc["schema_version"]
        with pytest.raises(SchemaError, match="missing 'schema_version'"):
            from_json(type(obj), dumps(doc))


def test_newer_schema_version_is_rejected() -> None:
    with pytest.raises(SchemaError, match="newer than supported"):
        from_json(SkipList, dumps(raw(make_skiplist(), schema_version=SCHEMA_VERSION + 1)))


def test_migration_chain_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate a v2 format that renamed Segment.evidence; a v1 file must still load."""
    seen: list[str] = []

    def v1_to_v2(type_name: str, doc: dict[str, Any]) -> dict[str, Any]:
        seen.append(type_name)
        return {**doc, "version": doc["version"] + 100}

    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(schema.MIGRATIONS, 1, v1_to_v2)
    v1 = dumps(raw(make_skiplist(version=1), schema_version=1))
    d = decode(SkipList, v1)
    assert seen == ["SkipList"]
    assert d.schema_version == 1  # what the file said
    assert d.value.version == 101  # migrated content


def test_missing_migration_step_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema, "SCHEMA_VERSION", 2)
    with pytest.raises(SchemaError, match="no migration from 1 to 2"):
        from_json(SkipList, dumps(raw(make_skiplist(), schema_version=1)))


# --- golden files (SPEC v0.2) ----------------------------------------------------------------

GOLDEN = GoldenMeta(annotator="owner", annotated_at="2026-10-16T09:30:00+08:00")


def test_golden_metadata_is_accepted_and_returned() -> None:
    text = to_json(golden_skiplist(), golden=GOLDEN)
    doc = json.loads(text)
    assert doc["annotator"] == "owner" and doc["annotated_at"] == GOLDEN.annotated_at
    d = decode(SkipList, text)
    assert d.golden == GOLDEN
    assert d.value == golden_skiplist()
    assert from_json(SkipList, text) == golden_skiplist()  # plain loader also accepts it


def test_golden_file_written_by_hand_is_accepted() -> None:
    text = """{
      "schema_version": 1, "annotator": "owner", "annotated_at": "2026-10-16",
      "media_id": "0123456789abcdef", "version": 1,
      "segments": [{"start": 8, "end": 12.5, "label": "ad", "confidence": 1.0, "evidence": {}}]
    }"""
    d = decode(SkipList, text)
    assert d.golden == GoldenMeta("owner", "2026-10-16")
    assert d.value.segments[0].end == 12.5


def test_plain_skiplist_has_no_golden_metadata() -> None:
    assert decode(SkipList, to_json(make_skiplist())).golden is None


def test_golden_metadata_needs_both_fields() -> None:
    for key in ("annotator", "annotated_at"):
        doc = raw(golden_skiplist(), annotator="owner", annotated_at="2026-10-16")
        del doc[key]
        with pytest.raises(SchemaError, match="golden metadata needs both"):
            from_json(SkipList, dumps(doc))


@pytest.mark.parametrize(
    ("annotator", "annotated_at"),
    [("", "2026-10-16"), ("  ", "2026-10-16"), (3, "2026-10-16"), ("o", "16/10/2026"), ("o", 1)],
)
def test_golden_metadata_values_are_validated(annotator: Any, annotated_at: Any) -> None:
    doc = raw(golden_skiplist(), annotator=annotator, annotated_at=annotated_at)
    with pytest.raises(SchemaError, match="annotat"):
        from_json(SkipList, dumps(doc))


def test_golden_metadata_only_valid_on_skiplist() -> None:
    doc = raw(make_timeline(), annotator="owner", annotated_at="2026-10-16")
    with pytest.raises(SchemaError, match="unknown field"):
        from_json(Timeline, dumps(doc))
    with pytest.raises(SchemaError, match="only valid on a SkipList"):
        to_json(make_timeline(), golden=GOLDEN)


def test_golden_segments_must_have_confidence_one_and_no_evidence() -> None:
    with pytest.raises(SchemaError, match="confidence"):
        to_json(make_skiplist(), golden=GOLDEN)
    bad = SkipList(MID, 1, [Segment(0, 1, "ad", 1.0, {"x": 1})])
    with pytest.raises(SchemaError, match="evidence"):
        to_json(bad, golden=GOLDEN)
    doc = raw(make_skiplist(), annotator="owner", annotated_at="2026-10-16")
    with pytest.raises(SchemaError, match="golden files use 1.0"):
        from_json(SkipList, dumps(doc))


# --- strictness: unknown fields, types, malformed JSON ---------------------------------------


@pytest.mark.parametrize(
    ("path", "cls"),
    [
        ((), SkipList),
        (("segments", 0), SkipList),
        ((), Timeline),
        (("shots", 1), Timeline),
        (("container_events", 0), Timeline),
        (("playlist_events", 1), Timeline),
        ((), ShotFeatures),
    ],
)
def test_unknown_field_is_an_error_at_every_level(path: tuple[Any, ...], cls: type) -> None:
    obj: Any = {SkipList: make_skiplist(), Timeline: make_timeline()}.get(
        cls, ShotFeatures(0, struct())
    )
    doc = raw(obj)
    target: Any = doc
    for key in path:
        target = target[key]
    target["surprise"] = 1
    with pytest.raises(SchemaError, match=r"unknown field\(s\) \['surprise'\]"):
        from_json(cls, dumps(doc))


def test_free_form_dicts_accept_any_keys() -> None:
    doc = raw(make_skiplist())
    doc["segments"][0]["evidence"]["new_signal"] = {"weight": 1.5, "hits": [1, 2]}
    doc["segments"][0]["evidence"]["note"] = None
    seg = from_json(SkipList, dumps(doc)).segments[0]
    assert seg.evidence["new_signal"] == {"weight": 1.5, "hits": [1, 2]}


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d["segments"][0].update(label="Ad"), "is not one of"),
        (lambda d: d["segments"][0].update(label="sponsor"), "is not one of"),
        (lambda d: d["segments"][0].update(start="8"), "expected number"),
        (lambda d: d["segments"][0].update(start=True), "expected number"),
        (lambda d: d["segments"][0].update(confidence=1.5), r"within \[0, 1\]"),
        (lambda d: d["segments"][0].update(end=8.0), "must be > start"),
        (lambda d: d["segments"][0].update(start=-1.0), ">= 0"),
        (lambda d: d["segments"][0].update(evidence=[]), "expected object"),
        (lambda d: d["segments"][1].update(start=10.0), "non-overlapping"),
        (lambda d: d.update(segments={}), "expected array"),
        (lambda d: d.update(media_id="0123456789ABCDEF"), "lowercase hex"),
        (lambda d: d.update(media_id="0123"), "lowercase hex"),
        (lambda d: d.pop("segments"), "missing required field 'segments'"),
    ],
)
def test_invalid_skiplist_is_rejected(mutate: Any, match: str) -> None:
    doc = raw(make_skiplist())
    mutate(doc)
    with pytest.raises(SchemaError, match=match):
        from_json(SkipList, dumps(doc))


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d["shots"][0].update(idx=5), r"shots\[0\]\.idx: expected 0"),
        (lambda d: d["shots"][0].update(keyframe_ts=[]), "1–3 items"),
        (lambda d: d["shots"][0].update(keyframe_ts=[1.0, 1.5, 2.0, 2.5]), "1–3 items"),
        (lambda d: d["shots"][0].update(keyframe_ts=[5.0]), "outside"),
        (lambda d: d["shots"][0].update(source_units=[]), "at least one"),
        (lambda d: d["shots"][0].update(source_units=[""]), "at least one"),
        (lambda d: d["container_events"][0].update(kind="scte35"), "is not one of"),
        (lambda d: d["playlist_events"][0].update(kind="pts_jump"), "is not one of"),
        (lambda d: d["playlist_events"][0].update(payload={}), "unit"),
        (lambda d: d["container_events"][0]["payload"].update(unit=3), "unit"),
        (lambda d: d["playlist_events"][1].update(ts=1.0), "sorted by time"),
        (lambda d: d.update(fps=-25.0), ">= 0"),
        (lambda d: d.update(duration=None), "null is not allowed"),
    ],
)
def test_invalid_timeline_is_rejected(mutate: Any, match: str) -> None:
    doc = raw(make_timeline())
    mutate(doc)
    with pytest.raises(SchemaError, match=match):
        from_json(Timeline, dumps(doc))


def test_error_message_carries_path() -> None:
    doc = raw(make_timeline())
    doc["shots"][2]["keyframe_ts"][1] = "x"
    with pytest.raises(SchemaError, match=r"Timeline\.shots\[2\]\.keyframe_ts\[1\]"):
        from_json(Timeline, dumps(doc))


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"schema_version": 1, "media_id": "0123456789abcdef", "version": 1, "segments": [], '
        '"version": 2}',
        '{"schema_version": 1, "media_id": "0123456789abcdef", "version": 1, "segments": ['
        '{"start": NaN, "end": 1, "label": "ad", "confidence": 1, "evidence": {}}]}',
        '{"schema_version": 1, "media_id": "0123456789abcdef", "version": 1, "segments": ['
        '{"start": 0, "end": Infinity, "label": "ad", "confidence": 1, "evidence": {}}]}',
    ],
    ids=["garbage", "array", "duplicate-key", "nan", "infinity"],
)
def test_malformed_json_is_rejected(text: str) -> None:
    with pytest.raises(SchemaError):
        from_json(SkipList, text)


def test_to_json_rejects_objects_mutated_into_invalid_state() -> None:
    seg = Segment(0.0, 1.0, "ad", 0.9, {})
    skiplist = SkipList(MID, 1, [seg])
    seg.label = "sponsor"  # type: ignore[assignment]
    with pytest.raises(SchemaError, match="is not one of"):
        to_json(skiplist)


def test_to_json_rejects_non_json_payload_values() -> None:
    with pytest.raises(SchemaError, match="not a JSON value"):
        ContainerEvent(0.0, "pts_jump", {"unit": "s.ts", "delta": np.float32(1.0)})
    with pytest.raises(SchemaError, match="not a JSON value"):
        Segment(0.0, 1.0, "ad", 0.9, {"blocks": (1, 2)})
    with pytest.raises(SchemaError, match="finite"):
        Segment(0.0, 1.0, "ad", 0.9, {"z": float("inf")})


def test_to_json_rejects_non_schema_objects() -> None:
    with pytest.raises(SchemaError, match="not a SPEC chapter 2 type"):
        to_json({"media_id": MID})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        from_json(dict, "{}")  # type: ignore[type-var]


def test_literal_sets_match_spec() -> None:
    assert schema.SEGMENT_LABELS == ("content", "ad", "intro", "recap", "outro", "trailer")
    assert schema.CONTAINER_EVENT_KINDS == (
        "pts_jump", "video_params", "audio_params", "pid_change", "bitrate_outlier",
    )  # fmt: skip
    assert schema.PLAYLIST_EVENT_KINDS == (
        "discontinuity", "key_change", "duration_outlier", "uri_pattern_break", "daterange",
    )  # fmt: skip


# --- parquet FEATURE_SCHEMA ------------------------------------------------------------------


def test_feature_schema_m0_has_only_struct() -> None:
    assert FEATURE_SCHEMA.names == ["shot_idx", "struct"]
    struct_type = FEATURE_SCHEMA.field("struct").type
    assert struct_type.list_size == 10
    assert struct_type.value_type == pa.float32()
    assert not struct_type.value_field.nullable  # null elements are rejected, not stored
    assert not FEATURE_SCHEMA.field("struct").nullable
    assert FEATURE_SCHEMA.metadata == {b"skua_schema_version": b"1"}


def test_parquet_round_trip(skua_work_dir: Path) -> None:
    rows = [ShotFeatures(i, struct(i)) for i in range(5)]
    path = skua_work_dir / "features.parquet"
    write_features(path, rows)
    assert not path.with_name(path.name + ".part").exists()
    assert read_features(path) == rows
    assert pq.read_schema(path).equals(FEATURE_SCHEMA, check_metadata=True)


def test_parquet_round_trip_empty(skua_work_dir: Path) -> None:
    path = skua_work_dir / "empty.parquet"
    write_features(path, [])
    assert read_features(path) == []


def test_parquet_refuses_to_drop_populated_future_columns() -> None:
    with pytest.raises(SchemaError, match="'ood_vis' has no column"):
        features_to_table([ShotFeatures(0, struct(), ood_vis=0.3)])


def test_parquet_rejects_duplicate_shot_idx() -> None:
    with pytest.raises(SchemaError, match="unique"):
        features_to_table([ShotFeatures(0, struct()), ShotFeatures(0, struct(1))])


STRUCT_TYPE = FEATURE_SCHEMA.field("struct").type


def _good_columns() -> list[pa.Array]:
    zeros = pa.array(np.zeros(CONFIG.struct_dim, np.float32))
    return [pa.array([0], pa.int32()), pa.FixedSizeListArray.from_arrays(zeros, type=STRUCT_TYPE)]


def _table_with(
    *,
    field_idx: int | None = None,
    new_field: Any = None,
    column: Any = None,
    metadata: Any = FEATURE_SCHEMA.metadata,
    order: tuple[int, int] = (0, 1),
) -> pa.Table:
    """FEATURE_SCHEMA-conformant table with exactly one deliberate deviation."""
    fields = list(FEATURE_SCHEMA)
    columns = _good_columns()
    if field_idx is not None:
        fields[field_idx] = new_field
        columns[field_idx] = column
    schema = pa.schema([fields[i] for i in order], metadata=metadata)
    return pa.Table.from_arrays([columns[i] for i in order], schema=schema)


def test_good_table_passes_validation() -> None:
    validate_feature_table(_table_with())


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (
            {
                "field_idx": 0,
                "new_field": pa.field("shot_idx", pa.int64(), nullable=False),
                "column": pa.array([0], pa.int64()),
            },
            "'shot_idx': expected int32",
        ),
        (
            {
                "field_idx": 0,
                "new_field": pa.field("shot_idx", pa.int32(), nullable=True),
                "column": pa.array([0], pa.int32()),
            },
            "nullable=True",
        ),
        (
            {
                "field_idx": 1,
                "new_field": pa.field("struct", pa.list_(pa.float32(), 9), nullable=False),
                "column": pa.FixedSizeListArray.from_arrays(pa.array(np.zeros(9, np.float32)), 9),
            },
            "'struct': expected",
        ),
        (
            {
                "field_idx": 1,
                "new_field": pa.field("struct", pa.list_(pa.float64(), 10), nullable=False),
                "column": pa.FixedSizeListArray.from_arrays(pa.array(np.zeros(10)), 10),
            },
            "'struct': expected",
        ),
        ({"order": (1, 0)}, "columns"),
        ({"metadata": None}, "metadata"),
        ({"metadata": {b"skua_schema_version": b"2"}}, "metadata"),
    ],
    ids=["int64-idx", "nullable-idx", "9-dim", "float64", "column-order", "no-version", "v2"],
)
def test_validate_feature_table_rejects_mismatches(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(SchemaError, match=match):
        validate_feature_table(_table_with(**kwargs))


def test_validate_feature_table_rejects_nan_and_null_values() -> None:
    nan = np.zeros(CONFIG.struct_dim, np.float32)
    nan[3] = np.nan
    bad_struct = pa.FixedSizeListArray.from_arrays(pa.array(nan), type=STRUCT_TYPE)
    table = pa.Table.from_arrays([pa.array([0], pa.int32()), bad_struct], schema=FEATURE_SCHEMA)
    with pytest.raises(SchemaError, match="finite"):
        validate_feature_table(table)
    # Table.from_arrays does not enforce nullable=False, so the validator must.
    good_struct = _good_columns()[1]
    nulls = pa.Table.from_arrays([pa.array([None], pa.int32()), good_struct], schema=FEATURE_SCHEMA)
    with pytest.raises(SchemaError, match="nulls"):
        validate_feature_table(nulls)


def test_read_features_validates_foreign_files(skua_work_dir: Path) -> None:
    path = skua_work_dir / "foreign.parquet"
    pq.write_table(pa.table({"shot_idx": [0], "struct": [[0.0] * 10]}), path)
    with pytest.raises(SchemaError):
        read_features(path)
