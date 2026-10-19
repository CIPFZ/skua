"""tests/adapter: allowlisted external process runner (skua.adapter.proc)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from skua.adapter import proc
from skua.adapter.proc import AllowedCommand, ProcError, ProcResult, host_hash, redact_urls, run


def test_allowlist_is_exactly_ffmpeg_and_ffprobe_in_m0() -> None:
    # fpcalc joins in M1 together with its ADR (AGENT.md 9, SPEC 3.4).
    assert {c.value for c in AllowedCommand} == {"ffmpeg", "ffprobe"}


@pytest.mark.parametrize("cmd", list(AllowedCommand))
def test_run_allowed_command(cmd: AllowedCommand) -> None:
    result = run(cmd, ["-hide_banner", "-version"], timeout_s=30)
    assert result.returncode == 0
    assert result.stdout.startswith(f"{cmd.value} version".encode())
    assert result.elapsed_s >= 0


def test_nonzero_exit_raises_with_stderr_tail(tmp_path: Path) -> None:
    missing = tmp_path / "missing.ts"
    with pytest.raises(ProcError) as info:
        run(AllowedCommand.FFPROBE, ["-v", "error", str(missing)], timeout_s=30)
    err = info.value
    assert err.cmd is AllowedCommand.FFPROBE
    assert err.result is not None and err.result.returncode != 0
    assert "exited with status" in str(err)
    assert "No such file" in str(err)


def test_check_false_returns_result(tmp_path: Path) -> None:
    result = run(AllowedCommand.FFPROBE, ["-v", "error", str(tmp_path / "x.ts")], check=False)
    assert result.returncode != 0
    assert result.stderr


def test_timeout_kills_process() -> None:
    # An endless lavfi source would run forever; the runner must stop it.
    args = ["-nostdin", "-hide_banner", "-re", "-f", "lavfi", "-i", "nullsrc", "-f", "null", "-"]
    with pytest.raises(ProcError, match="timed out after 0.5s"):
        run(AllowedCommand.FFMPEG, args, timeout_s=0.5)


@pytest.mark.parametrize(
    ("args", "exc"),
    [
        ("-version", TypeError),
        (b"-version", TypeError),
        (["-version", 3], TypeError),
        (["-i", "a\x00b"], ValueError),
    ],
)
def test_arguments_must_be_a_list_of_str(args: object, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        run(AllowedCommand.FFPROBE, args)  # type: ignore[arg-type]


def test_command_must_be_allowlisted() -> None:
    with pytest.raises(TypeError, match="AllowedCommand"):
        run("ffmpeg", ["-version"])  # type: ignore[arg-type]


def test_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc.shutil, "which", lambda name: None)
    with pytest.raises(ProcError, match="ffprobe: not found on PATH"):
        run(AllowedCommand.FFPROBE, ["-version"])


def test_shell_metacharacters_are_plain_arguments(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    result = run(
        AllowedCommand.FFPROBE, ["-v", "error", f"x; touch {marker}", "$(touch y)"], check=False
    )
    assert result.returncode != 0
    assert not marker.exists()


def test_redact_urls() -> None:
    text = "Opening 'https://user:pw@CDN.Example.com:8443/a/b.m3u8?token=s3cret' for reading"
    out = redact_urls(text)
    assert "example" not in out.lower() and "s3cret" not in out and "token" not in out
    assert out == f"Opening '<url:{host_hash('https://cdn.example.com/')}>' for reading"
    assert redact_urls("plain /local/path/seg001.ts") == "plain /local/path/seg001.ts"
    assert redact_urls("a crypto:file:///k/x b") == f"a crypto:<url:{host_hash('file:///')}> b"


def test_host_hash_ignores_case_port_and_path() -> None:
    a = host_hash("http://Media.Example.org:80/x")
    assert a == host_hash("https://media.example.org/other?q=1")
    assert a != host_hash("https://other.example.org/x")
    assert len(a) == 8


def test_error_message_is_redacted() -> None:
    result = ProcResult(1, b"", b"https://cdn.example.com/a.m3u8: Server returned 403\n", 0.1)
    err = ProcError(AllowedCommand.FFMPEG, "exited with status 1", result)
    assert "example.com" not in str(err)
    assert "<url:" in str(err) and "403" in str(err)


def test_arguments_are_never_logged(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    caplog.set_level(logging.DEBUG, logger="skua.adapter")
    # A local path stands in for a stream URL: tests never touch the network.
    secret = str(tmp_path / "SECRET-MARKER" / "index.m3u8")
    run(AllowedCommand.FFPROBE, ["-v", "quiet", secret], check=False, timeout_s=30)
    (record,) = [r for r in caplog.records if r.name == "skua.adapter"]
    assert secret not in record.getMessage()
    assert all(secret not in str(v) for v in record.__dict__.values())
    assert record.__dict__["cmd"] == "ffprobe"
