"""tests/adapter: the open_hls facade — variant selection, AES glue, index.json (hls.py).

The end-to-end AES cases use nothing but what the m3u8 says (no synthetic truth): keys come from
the KEY URI over HTTP, IVs from the IV attribute or the media sequence number (RFC 8216 5.2).
"""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from make_synth import SynthResult

from skua.adapter.decode import (
    crc32_mpeg2,
    head_section_crc_ok,
    iter_frames,
)
from skua.adapter.fetch import FetchConfig, FetchError, media_id
from skua.adapter.hls import AesIntegrityError, AesKeyError, iv_hex, key_ranges, open_hls
from skua.adapter.playlist import KeyState, parse_media_playlist
from tests.conftest import HlsServer

Serve = Callable[[Path], Awaitable[HlsServer]]
FAST = FetchConfig(backoff_s=0.0, timeout_s=10.0)
KEY1 = bytes(range(16))
KEY2 = bytes(range(16, 32))


def key_state(iv: str | None) -> KeyState:
    return KeyState("AES-128", "k.key", "https://example.com/k.key", iv, None, None)


# --- IV rule and integrity check (pure) ------------------------------------------------------


def test_iv_rule() -> None:
    assert iv_hex(key_state(None), 15) == f"{15:032x}"  # implicit: media sequence number
    assert iv_hex(key_state(None), 2**64 + 1) == f"{2**64 + 1:032x}"
    assert iv_hex(key_state("0x2A"), 0) == "0" * 30 + "2a"  # explicit wins, zero-padded
    assert iv_hex(key_state("0x" + "ab" * 16), 7) == "ab" * 16
    for bad in ("0x", "0xZZ", "0x" + "00" * 17):
        with pytest.raises(AesKeyError):
            iv_hex(key_state(bad), 0)


def test_crc32_mpeg2_check_value() -> None:
    assert crc32_mpeg2(b"123456789") == 0x0376E6E7  # catalogue check value


def test_head_section_crc_on_plain_segments(synth_ad_break: SynthResult) -> None:
    head = (synth_ad_break.out_dir / "seg000.ts").read_bytes()[:188]
    assert head_section_crc_ok(head) is True
    broken = bytearray(head)
    broken[12] ^= 0x01
    assert head_section_crc_ok(bytes(broken)) is False
    assert head_section_crc_ok(b"\x00" * 188) is False  # no sync byte
    pes = bytes([0x47, 0x41, 0x00, 0x10]) + b"\x00" * 184  # PID 0x100: not a PSI table
    assert head_section_crc_ok(pes) is None


# --- open_hls over HTTP ----------------------------------------------------------------------


async def test_open_master_over_http(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path
) -> None:
    server = await serve(synth_ad_break.out_dir)
    src = server.url("master.m3u8")
    hls = await open_hls(src, cache_root=tmp_path, config=FAST)
    assert hls.media_id == media_id(src)
    assert server.requests["index.m3u8"] == 1 and server.requests["low.m3u8"] == 0
    assert [s.start for s in hls.timeline.shots] == pytest.approx([4.0 * i for i in range(21)])
    assert hls.index_path == tmp_path / hls.media_id / "index.json"
    index = json.loads(hls.index_path.read_text())
    assert index["media_id"] == hls.media_id and len(index["segments"]) == 21
    assert {row["encrypted"] for row in index["segments"]} == {False}
    assert len(hls.units) == 21


async def test_aes_end_to_end_from_the_playlist_alone(
    serve: Serve,
    synth_aes: SynthResult,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="skua.adapter")
    server = await serve(synth_aes.out_dir)
    hls = await open_hls(server.url("master.m3u8"), cache_root=tmp_path, config=FAST)
    # Every segment demuxed after decryption, and every encrypted head passed its CRC.
    assert all(a.origin_pts is not None for a in hls.axis)
    assert "fails its CRC" not in caplog.text
    assert [s.start for s in hls.timeline.shots] == pytest.approx([4.0 * i for i in range(21)])
    keys = [
        (e.ts, e.payload["method"]) for e in hls.timeline.playlist_events if e.kind == "key_change"
    ]
    assert keys == [(48.0, "NONE"), (60.0, "AES-128")]
    assert server.requests["keys/k1.key"] == 1 and server.requests["keys/k2.key"] == 1
    # C3 decrypts with the implicit IV and decodes across the PTS wrap.
    c3 = [u for u in hls.units if u.start >= 60.0][:3]
    times = [t for t, _ in iter_frames(c3, fps=1.0, size=(16, 16))]
    assert times == pytest.approx([60.0 + k for k in range(12)], abs=1e-6)


async def test_keys_never_reach_disk_or_logs(
    serve: Serve, synth_aes: SynthResult, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="skua")
    server = await serve(synth_aes.out_dir)
    hls = await open_hls(server.url("index.m3u8"), cache_root=tmp_path, config=FAST)
    cache = tmp_path / hls.media_id
    assert not list(cache.rglob("*.key"))
    for key in (KEY1, KEY2):
        assert key.hex() not in hls.index_path.read_text()
        assert all(key not in f.read_bytes() for f in cache.iterdir())
        logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert key.hex() not in logged
    # Segments are cached as the ciphertext the server sent (SPEC 3.1).
    row = json.loads(hls.index_path.read_text())["segments"][0]
    assert row["encrypted"] is True
    assert Path(row["path"]).read_bytes() == (synth_aes.out_dir / "seg000.ts").read_bytes()
    assert set(row) == {
        "index", "sequence", "uri", "path", "bytes", "encrypted", "head_crc_ok",
        "start", "length", "origin_pts", "time_base",
    }  # fmt: skip


async def test_wrong_media_sequence_aborts_with_the_key_range(
    serve: Serve, synth_aes: SynthResult, tmp_path: Path
) -> None:
    # Shift EXT-X-MEDIA-SEQUENCE: C1/C2 carry an explicit IV and do not care; C3 relies on the
    # implicit sequence-number IV, so all six C3 heads fail: a range-wide error (SPEC 3.1).
    root = tmp_path / "srv"
    shutil.copytree(synth_aes.out_dir, root)
    pl = root / "index.m3u8"
    pl.write_text(pl.read_text().replace("#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-MEDIA-SEQUENCE:5"))
    server = await serve(root)
    with pytest.raises(AesIntegrityError, match=r"segments 15-20 \(media sequence 20-25") as info:
        await open_hls(server.url("index.m3u8"), cache_root=tmp_path / "c", config=FAST)
    assert "6 of 6" in str(info.value) and "127.0.0.1" not in str(info.value)


def damage_first_block(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[5] ^= 0xFF  # ciphertext block 1 -> plaintext block 1 garbled
    path.write_bytes(bytes(data))


async def test_one_damaged_segment_only_warns_and_is_marked(
    serve: Serve, synth_aes: SynthResult, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "srv"
    shutil.copytree(synth_aes.out_dir, root)
    damage_first_block(root / "seg003.ts")
    server = await serve(root)
    hls = await open_hls(server.url("index.m3u8"), cache_root=tmp_path / "c", config=FAST)
    (record,) = [r for r in caplog.records if "fails its CRC" in r.getMessage()]
    assert record.__dict__["segments"] == [3]
    heads = [row["head_crc_ok"] for row in json.loads(hls.index_path.read_text())["segments"]]
    assert heads[3] is False
    assert all(h is True for i, h in enumerate(heads[:12]) if i != 3)
    assert heads[12:15] == [None, None, None]  # clear ad
    assert all(h is True for h in heads[15:])
    assert len(hls.timeline.shots) == 21  # timing is still usable


async def test_two_damaged_segments_in_one_key_range_abort(
    serve: Serve, synth_aes: SynthResult, tmp_path: Path
) -> None:
    root = tmp_path / "srv"
    shutil.copytree(synth_aes.out_dir, root)
    damage_first_block(root / "seg003.ts")
    damage_first_block(root / "seg009.ts")  # C2 shares C1's KEY range (k1, explicit IV)
    server = await serve(root)
    with pytest.raises(AesIntegrityError, match=r"segments 0-11 .*2 of 12"):
        await open_hls(server.url("index.m3u8"), cache_root=tmp_path / "c", config=FAST)


async def test_damage_in_two_different_key_ranges_only_warns(
    serve: Serve, synth_aes: SynthResult, tmp_path: Path
) -> None:
    root = tmp_path / "srv"
    shutil.copytree(synth_aes.out_dir, root)
    damage_first_block(root / "seg003.ts")  # k1 range
    damage_first_block(root / "seg016.ts")  # k2 range
    server = await serve(root)
    hls = await open_hls(server.url("index.m3u8"), cache_root=tmp_path / "c", config=FAST)
    heads = [row["head_crc_ok"] for row in json.loads(hls.index_path.read_text())["segments"]]
    assert [i for i, h in enumerate(heads) if h is False] == [3, 16]


async def test_wrong_key_aborts_even_though_probes_fail_first(
    serve: Serve, synth_aes: SynthResult, tmp_path: Path
) -> None:
    # A wrong key also breaks the PKCS7 padding, so demuxing fails outright; the head check
    # runs independently of the probe and still counts every segment of the range.
    root = tmp_path / "srv"
    shutil.copytree(synth_aes.out_dir, root)
    (root / "keys" / "k2.key").write_bytes(bytes(16))
    server = await serve(root)
    with pytest.raises(AesIntegrityError, match=r"segments 15-20 .*6 of 6"):
        await open_hls(server.url("index.m3u8"), cache_root=tmp_path / "c", config=FAST)


def test_key_ranges(synth_aes: SynthResult) -> None:
    pl = synth_aes.playlist
    segs = parse_media_playlist(pl.read_text(), str(pl)).segments
    assert key_ranges(segs) == [(0, 11), (15, 20)]


async def test_key_errors(serve: Serve, synth_aes: SynthResult, tmp_path: Path) -> None:
    root = tmp_path / "srv"
    shutil.copytree(synth_aes.out_dir, root)
    server = await serve(root)
    server.status["keys/k2.key"] = 404
    with pytest.raises(FetchError, match="key: HTTP 404"):
        await open_hls(server.url("index.m3u8"), cache_root=tmp_path / "a", config=FAST)
    server.status.clear()
    (root / "keys" / "k2.key").write_bytes(b"short")
    with pytest.raises(AesKeyError, match="5 bytes"):
        await open_hls(server.url("index.m3u8"), cache_root=tmp_path / "b", config=FAST)


async def test_headers_reach_playlist_key_and_segment_requests(
    serve: Serve, synth_aes: SynthResult, tmp_path: Path
) -> None:
    server = await serve(synth_aes.out_dir)
    await open_hls(
        server.url("master.m3u8"), cache_root=tmp_path, headers={"X-Skua-Test": "1"}, config=FAST
    )
    assert len(server.headers_seen) == 2 + 2 + 21  # master, media, 2 keys, 21 segments
    assert all(h.get("X-Skua-Test") == "1" for h in server.headers_seen)


async def test_second_open_reuses_the_cache(
    serve: Serve, synth_ad_break: SynthResult, tmp_path: Path
) -> None:
    server = await serve(synth_ad_break.out_dir)
    first = await open_hls(server.url("index.m3u8"), cache_root=tmp_path, config=FAST)
    again = await open_hls(server.url("index.m3u8"), cache_root=tmp_path, config=FAST)
    assert server.requests["seg000.ts"] == 1
    assert again.timeline == first.timeline


async def test_local_master_playlist(synth_ad_break: SynthResult, tmp_path: Path) -> None:
    hls = await open_hls(str(synth_ad_break.master), cache_root=tmp_path)
    assert hls.media_id == media_id(str(synth_ad_break.master))
    rows = json.loads(hls.index_path.read_text())["segments"]
    assert [Path(r["path"]) for r in rows] == [
        synth_ad_break.out_dir / s.uri for s in synth_ad_break.segments
    ]
    assert len(hls.timeline.shots) == 21
