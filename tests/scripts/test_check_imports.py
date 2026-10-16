"""tests/scripts: unit tests for the AST import guard behind scripts/check_imports.sh."""

from __future__ import annotations

from pathlib import Path

import pytest
from _check_imports import check_repo, check_source

DECODE = "skua/adapter/decode.py"
PROC = "skua/adapter/proc.py"
OTHER = "skua/evidence/structural.py"


def rules(path: str, src: str) -> list[str]:
    return [v.rule for v in check_source(path, src)]


@pytest.mark.parametrize(
    "src",
    [
        "import av",
        "import av.container",
        "import av as _av",
        "from av import open as av_open",
        "from av.video.frame import VideoFrame",
        "import json, av",
        "from importlib import import_module\nimport_module('av')",
        "__import__('av.codec')",
    ],
)
def test_av_banned_outside_decode(src: str) -> None:
    assert rules(OTHER, src)
    assert rules(PROC, src), "proc.py is not exempt from the av rule"
    assert rules("tests/adapter/test_x.py", src)


@pytest.mark.parametrize("src", ["import av", "from av import open", "import av.datasets"])
def test_av_allowed_in_decode(src: str) -> None:
    assert rules(DECODE, src) == []


@pytest.mark.parametrize(
    "src",
    [
        "import subprocess",
        "from subprocess import run",
        "import subprocess as sp",
        "import importlib\nimportlib.import_module('subprocess')",
    ],
)
def test_subprocess_only_in_proc(src: str) -> None:
    assert rules(PROC, src) == []
    assert rules(DECODE, src)
    assert rules("scripts/make_synth.py", src)


@pytest.mark.parametrize(
    "src",
    [
        "import os\nos.system('ls')",
        "import os as o\no.popen('ls')",
        "import os\nos.execvp('ffmpeg', ['ffmpeg'])",
        "import os\nos.spawnlp(0, 'ffmpeg')",
        "import os\nos.posix_spawn('ffmpeg', [], {})",
        "from os import system",
        "from os import spawnv as s",
        "from os import *",
        "import asyncio\nasyncio.create_subprocess_exec('ffmpeg')",
        "from asyncio import create_subprocess_shell",
        "import pty",
        "import pexpect",
        "import ffmpeg",
        "import moviepy.editor",
    ],
)
def test_process_spawning_banned_everywhere(src: str) -> None:
    for path in (OTHER, DECODE, PROC, "tests/x.py"):
        assert rules(path, src), path


def test_shell_true_banned_even_in_proc() -> None:
    assert rules(PROC, "import subprocess\nsubprocess.run(['x'], shell=True)")
    assert rules(PROC, "import subprocess\nflag = True\nsubprocess.run(['x'], shell=flag)")
    assert rules(PROC, "import subprocess\nsubprocess.run(['x'], shell=False)") == []


def test_dynamic_import_with_non_literal_name_banned() -> None:
    assert rules(OTHER, "import importlib\nname = 'a' + 'v'\nimportlib.import_module(name)")


@pytest.mark.parametrize(
    "src",
    [
        "import os\nos.path.join('a', 'b')",
        "import os\nos.environ.get('SKUA_WORK_DIR')",
        "from skua.adapter.decode import iter_frames",
        "from skua.adapter import proc\nproc.run",
        "import importlib\nimportlib.import_module('json')",
        "import asyncio\nasyncio.run(main())",
        "from concurrent.futures import ProcessPoolExecutor",
        "available = 1  # mentions import av in a comment only",
        "s = 'import subprocess'",
    ],
)
def test_legitimate_code_passes(src: str) -> None:
    assert rules(OTHER, src) == []


def test_violation_reports_line_number() -> None:
    (hit,) = check_source(OTHER, "x = 1\n\nimport av\n")
    assert (hit.path, hit.line) == (OTHER, 3)
    assert str(hit).startswith(f"{OTHER}:3: ")


def test_syntax_error_is_a_violation() -> None:
    assert rules(OTHER, "def broken(:\n")


def test_check_repo_scans_skua_scripts_tests(tmp_path: Path) -> None:
    (tmp_path / "skua" / "adapter").mkdir(parents=True)
    (tmp_path / "skua" / "adapter" / "decode.py").write_text("import av\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text("import subprocess\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "snippet.py").write_text("import av\n")  # outside scanned dirs
    hits = check_repo(tmp_path)
    assert [h.path for h in hits] == ["scripts/tool.py"]


def test_this_repository_is_clean() -> None:
    repo = Path(__file__).resolve().parents[2]
    assert [str(h) for h in check_repo(repo)] == []
