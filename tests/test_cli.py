"""tests: ``skua scan`` — JSON-only stdout, stderr timings, exit-code contract (SPEC 3.8)."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from make_synth import SynthResult

from skua import cli
from skua.cli import ExitCode, main, parse_headers
from skua.schema import SkipList, Timeline, decode, from_json
from tests.conftest import HlsServer

Serve = Callable[[Path], Awaitable[HlsServer]]


def scan(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(["scan", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- success ---------------------------------------------------------------------------------


def test_scan_writes_skiplist_and_prints_only_json(
    synth_ad_break: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "skiplist.json"
    code, stdout, stderr = scan(
        capsys, str(synth_ad_break.master), "--out", str(out), "--cache-dir", str(tmp_path / "c")
    )
    assert code == ExitCode.OK == 0
    printed = from_json(SkipList, stdout)  # stdout is exactly one JSON document
    assert from_json(SkipList, out.read_text()) == printed
    assert [(s.start, s.end, s.label) for s in printed.segments] == [(48.0, 60.0, "ad")]
    assert "skua scan: 21 segments" in stderr and "hot-path" in stderr and "total" in stderr
    assert not list(tmp_path.glob("*.part"))


def test_json_events_prints_a_timeline_that_reads_back(
    synth_ad_break: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "s.json"
    code, stdout, _ = scan(
        capsys,
        str(synth_ad_break.playlist),
        "--out",
        str(out),
        "--json-events",
        "--cache-dir",
        str(tmp_path / "c"),
    )
    assert code == 0
    doc = decode(Timeline, stdout)  # SPEC 3.8: must round-trip through schema.decode
    assert len(doc.value.shots) == 21 and doc.value.playlist_events
    assert from_json(SkipList, out.read_text()).segments  # --out still holds the SkipList


async def test_cold_then_hot_cache_over_http(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server = await serve(synth_ad_break.out_dir)
    args = ["scan", server.url("master.m3u8"), "--out", str(tmp_path / "s.json")]
    args += ["--cache-dir", str(tmp_path / "c"), "--header", "X-Skua-Test: yes"]
    assert await asyncio.to_thread(main, args) == 0
    cold = capsys.readouterr().err
    assert await asyncio.to_thread(main, args) == 0
    hot = capsys.readouterr().err
    assert "21 downloaded (cold cache)" in cold
    assert "0 downloaded (hot cache)" in hot
    assert all(h.get("X-Skua-Test") == "yes" for h in server.headers_seen)
    assert "127.0.0.1" not in cold + hot


# --- exit codes ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        [],  # no source
        ["x.m3u8"],  # missing --out
        ["--out", "o.json", "does/not/exist.m3u8"],
        ["--bogus"],
    ],
)
def test_usage_errors_exit_1(
    args: list[str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    code, stdout, _ = scan(capsys, *args)
    assert code == ExitCode.USAGE == 1 and stdout == ""


def test_bad_header_exits_1(
    synth_ad_break: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, stdout, stderr = scan(
        capsys, str(synth_ad_break.playlist), "--out", str(tmp_path / "o"), "--header", "nocolon"
    )
    assert code == 1 and stdout == "" and "--header" in stderr


@pytest.mark.parametrize(
    ("playlist", "src"),
    [
        (
            '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MAP:URI="i.mp4"\n#EXTINF:4,\na.m4s\n#EXT-X-ENDLIST\n',
            None,
        ),
        ("#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\na.ts\n", None),  # live
        (
            '#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k"\n#EXTINF:4,\na.ts\n#EXT-X-ENDLIST\n',
            None,
        ),
        (None, "ftp://example.com/v/index.m3u8"),
    ],
    ids=["fmp4", "live", "sample-aes", "scheme"],
)
def test_unsupported_sources_exit_2(
    playlist: str | None, src: str | None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if playlist is not None:
        path = tmp_path / "index.m3u8"
        path.write_text(playlist)
        src = str(path)
    assert src is not None
    code, stdout, _ = scan(
        capsys, src, "--out", str(tmp_path / "o.json"), "--cache-dir", str(tmp_path / "c")
    )
    assert code == ExitCode.UNSUPPORTED == 2 and stdout == ""
    assert not (tmp_path / "o.json").exists()


def test_integrity_error_exits_3(
    synth_aes: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "src"
    shutil.copytree(synth_aes.out_dir, root)
    pl = root / "index.m3u8"
    pl.write_text(pl.read_text().replace("#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-MEDIA-SEQUENCE:5"))
    code, stdout, stderr = scan(
        capsys, str(pl), "--out", str(tmp_path / "o"), "--cache-dir", str(tmp_path / "c")
    )
    assert code == ExitCode.INTEGRITY == 3 and stdout == ""
    assert "segments 15-20" in stderr


async def test_download_failure_exits_4(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server = await serve(synth_ad_break.out_dir)
    server.status["seg003.ts"] = 404
    args = [
        "scan",
        server.url("index.m3u8"),
        "--out",
        str(tmp_path / "o"),
        "--cache-dir",
        str(tmp_path / "c"),
    ]
    assert await asyncio.to_thread(main, args) == ExitCode.DOWNLOAD == 4
    captured = capsys.readouterr()
    assert captured.out == "" and "HTTP 404" in captured.err and "127.0.0.1" not in captured.err


def test_internal_error_exits_5(
    synth_ad_break: SynthResult,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_: object) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(cli, "rule_decode", boom)
    code, stdout, stderr = scan(
        capsys,
        str(synth_ad_break.playlist),
        "--out",
        str(tmp_path / "o"),
        "--cache-dir",
        str(tmp_path / "c"),
    )
    assert code == ExitCode.INTERNAL == 5 and stdout == "" and "internal error" in stderr


def test_exit_codes_are_the_spec_contract() -> None:
    assert {e.name: e.value for e in ExitCode} == {
        "OK": 0, "USAGE": 1, "UNSUPPORTED": 2, "INTEGRITY": 3, "DOWNLOAD": 4, "INTERNAL": 5,
    }  # fmt: skip


def test_parse_headers() -> None:
    assert parse_headers(["Referer: https://example.com/a", "X-A:b"]) == {
        "Referer": "https://example.com/a",
        "X-A": "b",
    }
    for bad in ("nocolon", ": v", "Bad Name: v"):
        with pytest.raises(cli.CliError):
            parse_headers([bad])


def test_help_exits_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    assert "scan" in capsys.readouterr().out
