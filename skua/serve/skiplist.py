"""skua.serve.skiplist: versioned local SQLite storage of SkipLists (SPEC 3.8).

Table ``skiplist(media_id, version, schema_version, created_at, body_json)``, primary key
``(media_id, version)``. Each save of a media appends the next version; older versions are kept
for user appeals and rollback. Reads return the highest version unless one is asked for. Local
only: nothing here ever talks to a network (AGENT.md 3).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from skua.schema import SCHEMA_VERSION, SchemaError, SkipList, from_json, to_json

DB_ENV = "SKUA_DB"
DB_NAME = "skua.sqlite"

_DDL = """
CREATE TABLE IF NOT EXISTS skiplist (
    media_id       TEXT    NOT NULL,
    version        INTEGER NOT NULL CHECK (version >= 1),
    schema_version INTEGER NOT NULL,
    created_at     TEXT    NOT NULL,
    body_json      TEXT    NOT NULL,
    PRIMARY KEY (media_id, version)
)
"""


def default_db_path() -> Path:
    """``$SKUA_DB``, else ``$SKUA_WORK_DIR/skua.sqlite``, else ``./work/skua.sqlite``."""
    explicit = os.environ.get(DB_ENV)
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("SKUA_WORK_DIR") or "work") / DB_NAME


class SkipListStore:
    """Versioned SkipList store over one SQLite file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_DDL)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # One short-lived connection per operation: safe across threads and processes.
        with closing(sqlite3.connect(self.path, timeout=30.0)) as conn, conn:
            yield conn

    def save(self, skiplist: SkipList) -> SkipList:
        """Store ``skiplist`` as the next version of its media and return it with that version.

        The incoming ``version`` is ignored: the store owns the revision counter (SPEC 2, 3.8).
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")  # serialize concurrent writers on the version
            (latest,) = conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM skiplist WHERE media_id = ?",
                (skiplist.media_id,),
            ).fetchone()
            stored = replace(skiplist, version=int(latest) + 1)
            conn.execute(
                "INSERT INTO skiplist (media_id, version, schema_version, created_at, body_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    stored.media_id,
                    stored.version,
                    SCHEMA_VERSION,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    to_json(stored),
                ),
            )
        return stored

    def load(self, media_id: str, version: int | None = None) -> SkipList | None:
        """The highest version (or ``version``) of ``media_id``; None if absent."""
        with self._connect() as conn:
            if version is None:
                row = conn.execute(
                    "SELECT body_json FROM skiplist WHERE media_id = ?"
                    " ORDER BY version DESC LIMIT 1",
                    (media_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT body_json FROM skiplist WHERE media_id = ? AND version = ?",
                    (media_id, version),
                ).fetchone()
        if row is None:
            return None
        # from_json checks the stored schema_version and migrates older bodies (AGENT.md 5.2).
        loaded = from_json(SkipList, row[0])
        if version is not None and loaded.version != version:
            raise SchemaError(f"row {media_id}/{version} holds version {loaded.version}")
        return loaded

    def versions(self, media_id: str) -> list[int]:
        """All stored versions of ``media_id``, ascending."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT version FROM skiplist WHERE media_id = ? ORDER BY version", (media_id,)
            ).fetchall()
        return [int(r[0]) for r in rows]
