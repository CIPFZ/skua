"""tests/serve: versioned SQLite SkipList storage (SPEC 3.8)."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from skua.schema import SCHEMA_VERSION, SchemaError, Segment, SkipList, to_json
from skua.serve.skiplist import SkipListStore, default_db_path

MID = "0123456789abcdef"
OTHER = "fedcba9876543210"


def sl(*spans: tuple[float, float], media: str = MID) -> SkipList:
    return SkipList(
        media, 1, [Segment(a, b, "ad", 0.95, {"n": i}) for i, (a, b) in enumerate(spans)]
    )


def test_table_layout(tmp_path: Path) -> None:
    store = SkipListStore(tmp_path / "s.sqlite")
    with sqlite3.connect(store.path) as conn:
        cols = [(r[1], r[2], r[5]) for r in conn.execute("PRAGMA table_info(skiplist)")]
    assert cols == [
        ("media_id", "TEXT", 1),
        ("version", "INTEGER", 2),
        ("schema_version", "INTEGER", 0),
        ("created_at", "TEXT", 0),
        ("body_json", "TEXT", 0),
    ]  # name, type, position in the primary key


def test_save_appends_versions_and_keeps_history(tmp_path: Path) -> None:
    store = SkipListStore(tmp_path / "s.sqlite")
    first = store.save(sl((10.0, 20.0)))
    second = store.save(sl((10.0, 22.0), (40.0, 50.0)))
    assert (first.version, second.version) == (1, 2)
    assert store.versions(MID) == [1, 2]
    assert store.load(MID) == second  # default: highest version
    assert store.load(MID, 1) == first  # history is kept, not overwritten
    assert store.load(MID, 3) is None and store.load(OTHER) is None


def test_versions_are_per_media(tmp_path: Path) -> None:
    store = SkipListStore(tmp_path / "s.sqlite")
    store.save(sl((1.0, 2.0)))
    store.save(sl((1.0, 2.0)))
    assert store.save(sl((5.0, 6.0), media=OTHER)).version == 1


def test_incoming_version_is_ignored(tmp_path: Path) -> None:
    store = SkipListStore(tmp_path / "s.sqlite")
    stored = store.save(SkipList(MID, 42, []))
    assert stored.version == 1


def test_rows_carry_schema_version_and_round_trip_json(tmp_path: Path) -> None:
    store = SkipListStore(tmp_path / "s.sqlite")
    stored = store.save(sl((10.0, 20.0)))
    with sqlite3.connect(store.path) as conn:
        (schema_version, created_at, body) = conn.execute(
            "SELECT schema_version, created_at, body_json FROM skiplist"
        ).fetchone()
    assert schema_version == SCHEMA_VERSION
    assert created_at.endswith("+00:00")
    assert body == to_json(stored) and json.loads(body)["version"] == 1


def test_store_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    SkipListStore(path).save(sl((1.0, 2.0)))
    assert SkipListStore(path).save(sl((1.0, 3.0))).version == 2


def test_concurrent_writers_get_distinct_versions(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite"
    SkipListStore(path)

    def write(i: int) -> int:
        return SkipListStore(path).save(sl((float(i), float(i) + 1))).version

    with ThreadPoolExecutor(max_workers=8) as pool:
        got = sorted(pool.map(write, range(20)))
    assert got == list(range(1, 21))


def test_corrupt_body_is_an_error_not_silence(tmp_path: Path) -> None:
    store = SkipListStore(tmp_path / "s.sqlite")
    store.save(sl((1.0, 2.0)))
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE skiplist SET body_json = ?", ('{"schema_version": 1, "x": 1}',))
    with pytest.raises(SchemaError):
        store.load(MID)


def test_default_db_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SKUA_DB", raising=False)
    monkeypatch.setenv("SKUA_WORK_DIR", str(tmp_path / "w"))
    assert default_db_path() == tmp_path / "w" / "skua.sqlite"
    monkeypatch.setenv("SKUA_DB", str(tmp_path / "x.sqlite"))
    assert default_db_path() == tmp_path / "x.sqlite"
