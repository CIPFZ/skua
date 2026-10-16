"""tests/scripts: unit tests for the secret and media guard behind scripts/check_secrets.sh."""

from __future__ import annotations

from pathlib import Path

import pytest
from _check_secrets import (
    check_denylist,
    check_file,
    check_media,
    check_messages,
    check_text,
    load_denylist,
    normalize_host,
    registrable_domain,
)

# Fake "real" hosts are assembled at runtime so this file never contains the literal.
SITE = "streamsite" + ".com"
REAL = "cdn." + SITE
SHOW = "Fake" + "showname"


def rules(text: str, *, lockfile: bool = False) -> list[str]:
    return [v.rule for v in check_text("f.txt", text, lockfile=lockfile)]


@pytest.mark.parametrize(
    "text",
    [
        "http://127.0.0.1:8080/live/index.m3u8",
        "http://localhost:7788/hls?url=x",
        "http://[::1]:9000/a.m3u8",
        "https://example.com/master.m3u8",
        "https://media.example.org/x",
        "http://origin.test/seg1.ts",
        "https://datatracker.ietf.org/doc/html/rfc8216#section-4.3.2.3",
        "https://www.rfc-editor.org/rfc/rfc8216",
        "https://github.com/PyAV-Org/PyAV/issues/1",
        "f'http://{host}:{port}/index.m3u8'",
        "url = f'http://127.0.0.1:{port}/' + name",
        "the `http(s)://` literal rule",
        "playlist index.m3u8 and ads/ad000.m3u8 and ../v1/prog_index.m3u8",
        "a `.m3u8` string",
    ],
)
def test_allowed_text(text: str) -> None:
    assert rules(text) == []


@pytest.mark.parametrize(
    "text",
    [
        f"https://{REAL}/v/index.m3u8",
        "see http://" + REAL + "/x",
        "HTTPS://" + REAL.upper() + "/x",
        "http://user:pw@" + REAL + ":8443/x",
        "https://example.com." + REAL + "/x",  # suffix trick
        "https:" + "//notexample.com/x",
        "https:" + "//203.0.113.9/x" + ".m3u8",  # IP literals are not allowlisted either
    ],
)
def test_real_url_rejected(text: str) -> None:
    assert rules(text)


@pytest.mark.parametrize(
    "text",
    [
        REAL + "/v/index.m3u8",
        "//" + REAL + "/v/index.m3u8",
        "'" + REAL + ":8080/a/b.m3u8'",
    ],
)
def test_schemeless_m3u8_with_host_rejected(text: str) -> None:
    assert rules(text) == ["m3u8 reference with a host part"]


def test_lockfile_only_allows_pypi() -> None:
    # Assembled at runtime: PyPI hosts are allowlisted only inside uv.lock itself.
    lock = 'source = { registry = "https:' + '//pypi.org/simple" }\n'
    lock += 'url = "https:' + '//files.pythonhosted.org/packages/aa/bb/av.whl"\n'
    assert rules(lock, lockfile=True) == []
    assert rules('url = "https://example.com/x.whl"', lockfile=True)


def test_normalize_host() -> None:
    assert normalize_host("User@Host.Example.COM:443") == "host.example.com"
    assert normalize_host("[::1]:80") == "::1"
    assert normalize_host("example.com.") == "example.com"


def test_violation_has_line_number() -> None:
    (hit,) = check_text("doc.md", "ok\nok\nhttp://" + REAL + "/\n")
    assert hit.line == 3


TS = (b"\x47" + bytes(187)) * 4


@pytest.mark.parametrize(
    ("path", "head"),
    [
        ("tests/fixtures/seg0.ts", b"anything"),
        ("x/clip.MP4", b"anything"),
        ("tests/fixtures/blob.bin", TS),
        ("tests/fixtures/blob.bin", b"\x00\x00\x00\x18ftypisom"),
        ("tests/fixtures/blob.bin", b"\x1a\x45\xdf\xa3rest"),
        ("tests/fixtures/blob.bin", b"RIFF\x00\x00\x00\x00WAVEfmt "),
        ("tests/fixtures/blob.bin", b"ID3\x04rest"),
        ("tests/fixtures/blob.bin", b"\xff\xf1\x50\x80"),
        ("docs/screenshot.png", b"\x89PNG\r\n\x1a\nrest"),
        ("docs/blob.dat", b"\x89PNG\r\n\x1a\nrest"),
        ("docs/frame.jpg", b"\xff\xd8\xff\xe0"),
    ],
)
def test_media_rejected(path: str, head: bytes) -> None:
    assert check_media(path, head, size=len(head))


@pytest.mark.parametrize(
    ("path", "head"),
    [
        ("assets/icon/pack/skua-icon-16.png", b"\x89PNG\r\n\x1a\nrest"),
        ("assets/icon/pack/skua.icns", b"icns\x00\x00"),
        ("README.md", b"# Skua\n"),
        ("docs/notes.md", ("G" + "x" * 187) * 3),  # text that happens to have TS sync bytes
    ],
)
def test_non_media_passes(path: str, head: bytes | str) -> None:
    data = head.encode() if isinstance(head, str) else head
    assert check_media(path, data, size=len(data)) == []


def test_denylist_from_env_local() -> None:
    env = "\n".join(
        [
            "# comment",
            f"SKUA_DENYLIST={SHOW}, Other Show ,x",
            "export SKUA_SRC_1='https:" + "//v1." + REAL + "/a/index.m3u8?token=1'",
            "SKUA_LOCAL=http://127.0.0.1:8000/a.m3u8",
            "NOT_A_PAIR",
        ]
    )
    terms = load_denylist(env)
    assert SHOW.lower() in terms
    assert "other show" in terms
    assert "x" not in terms  # too short to be useful, too noisy
    assert "v1." + REAL in terms
    assert SITE in terms
    assert "127.0.0.1" not in terms


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("a.b." + SITE, SITE),
        (SITE, SITE),
        ("cdn.v1." + SITE + ".cn", SITE + ".cn"),
        ("x.y.co.uk", "y.co.uk"),
        ("someone.github.io", "someone.github.io"),
        ("deep.someone.github.io", "someone.github.io"),
        ("bucket.s3.amazonaws.com", "bucket.s3.amazonaws.com"),
        ("d111.cloudfront.net", "d111.cloudfront.net"),
        ("203.0.113.9", "203.0.113.9"),
        ("localhost", "localhost"),
    ],
)
def test_registrable_domain_stops_at_public_suffix_plus_one(host: str, expected: str) -> None:
    assert registrable_domain(host) == expected


def test_denylist_never_contains_bare_public_suffix() -> None:
    env = "A=https:" + "//v.someone.github.io/x\nB=https:" + "//d1.cloudfront.net/y\n"
    terms = load_denylist(env)
    assert "someone.github.io" in terms
    assert "d1.cloudfront.net" in terms
    for suffix in ("github.io", "cloudfront.net", "io", "net"):
        assert suffix not in terms


def test_denylist_match_is_case_insensitive_and_not_echoed() -> None:
    terms = load_denylist(f"SKUA_DENYLIST={SHOW}")
    (hit,) = check_denylist("doc.md", f"about {SHOW.upper()} ep 3", terms)
    assert SHOW.lower() not in str(hit).lower()
    assert "#1" in hit.rule


def test_commit_messages() -> None:
    blob = "abc123\nadapter: add parser\n\0def456\nci: test https://" + REAL + "/x\n\0"
    hits = check_messages(blob, [])
    assert [h.where for h in hits] == ["commit def456"]
    assert check_messages("abc\nnotes on " + SHOW + "\n\0", [SHOW.lower()])


def test_check_file_skips_binary_text_rules(tmp_path: Path) -> None:
    (tmp_path / "assets" / "icon").mkdir(parents=True)
    icon = tmp_path / "assets" / "icon" / "a.png"
    icon.write_bytes(b"\x89PNG\r\n\x1a\n" + b"http://" + REAL.encode() + b"\x00")
    assert check_file(tmp_path, "assets/icon/a.png", []) == []
    assert check_file(tmp_path, "missing.txt", []) == []
