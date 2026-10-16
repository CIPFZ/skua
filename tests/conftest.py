"""tests: shared fixtures; every test runs with SKUA_WORK_DIR pointing at a fresh temp dir."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def skua_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep work/ and cache/ out of the repo (AGENT.md 2.6)."""
    work = tmp_path / "skua_work"
    work.mkdir()
    monkeypatch.setenv("SKUA_WORK_DIR", str(work))
    return work
