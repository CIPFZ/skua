"""tests: shared fixtures; every test runs with SKUA_WORK_DIR pointing at a fresh temp dir."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from make_synth import H264_ENCODER, PROBED_ENCODERS, SynthResult, probe_encoder, synthesize

# CI sets this so the h264 group can never be silently skipped there (AGENT.md 6.1).
REQUIRE_H264_ENV = "SKUA_REQUIRE_H264"


@pytest.fixture(autouse=True)
def skua_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep work/ and cache/ out of the repo (AGENT.md 2.6)."""
    work = tmp_path / "skua_work"
    work.mkdir()
    monkeypatch.setenv("SKUA_WORK_DIR", str(work))
    return work


def pytest_report_header() -> str:
    probed = ", ".join(
        f"{n}={'ok' if probe_encoder(n) else 'unavailable'}" for n in PROBED_ENCODERS
    )
    return f"skua synth encoders (trial encode): {probed}"


@pytest.fixture(scope="session")
def synth_ad_break(tmp_path_factory: pytest.TempPathFactory) -> SynthResult:
    """C1 | D C2 | D AD | D C3 in mpeg2video, shared by every test in the session."""
    return synthesize(tmp_path_factory.mktemp("synth") / "ad-break", layout="ad-break")


@pytest.fixture(scope="session")
def synth_same_uri(tmp_path_factory: pytest.TempPathFactory) -> SynthResult:
    """Same layout, but the ad keeps the content URI template."""
    out = tmp_path_factory.mktemp("synth") / "same-uri"
    return synthesize(out, layout="ad-break-same-uri")


@pytest.fixture(scope="session")
def synth_h264(tmp_path_factory: pytest.TempPathFactory) -> SynthResult:
    """ad-break layout in libx264; skipped locally when unavailable, a failure in CI."""
    if not probe_encoder(H264_ENCODER):
        reason = f"{H264_ENCODER} failed the trial encode in this ffmpeg build"
        if os.environ.get(REQUIRE_H264_ENV) == "1":
            pytest.fail(f"{reason}, but {REQUIRE_H264_ENV}=1", pytrace=False)
        pytest.skip(reason)
    out = tmp_path_factory.mktemp("synth") / "h264"
    return synthesize(out, layout="ad-break", video_encoder=H264_ENCODER)
