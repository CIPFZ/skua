"""skua.adapter.hls: facade ``open_hls(src)`` — playlist, download, decryption parameters,
demux probes and the Timeline, orchestrated (SPEC 3.1). No logic of its own beyond wiring.

AES-128 boundary (SPEC 3.1): segments are cached as ciphertext; keys are fetched into memory
only and handed to decode as ``AesParams`` (libav ``crypto:`` decrypts while reading). Keys and
IVs never reach the disk, the logs or ``index.json``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from skua.adapter.decode import AesParams, DecodeError, DecodeUnit, DemuxProbe, probe_segment
from skua.adapter.fetch import (
    CONFIG as FETCH_CONFIG,
)
from skua.adapter.fetch import (
    FetchConfig,
    FetchedSegment,
    default_cache_root,
    fetch_bytes,
    fetch_segments,
    media_id,
)
from skua.adapter.playlist import (
    KeyState,
    PlaylistSegment,
    is_master_playlist,
    parse_media_playlist,
    select_variant,
)
from skua.adapter.timeline import SegmentAxis, build_timeline
from skua.schema import Timeline

log = logging.getLogger("skua.adapter")

INDEX_FORMAT = 1  # adapter-internal cache index, not the schema version
AES_KEY_BYTES = 16  # AES-128 (RFC 8216 4.3.2.4)
IV_HEX_CHARS = 32
_HEX = frozenset("0123456789abcdefABCDEF")


class AesKeyError(RuntimeError):
    """An AES-128 key could not be obtained or is malformed."""


@dataclass(frozen=True)
class HlsSource:
    """Everything downstream needs from one HLS source."""

    media_id: str
    timeline: Timeline
    axis: list[SegmentAxis]
    units: list[DecodeUnit]  # demuxable segments, for iter_frames / iter_pcm
    index_path: Path


def iv_hex(key: KeyState, sequence: int) -> str:
    """IV for a segment: the explicit IV attribute, else the media sequence number as a 128-bit
    big-endian integer (RFC 8216 5.2)."""
    if key.iv is None:
        return f"{sequence:0{IV_HEX_CHARS}x}"
    digits = key.iv.removeprefix("0x").removeprefix("0X")
    if not digits or len(digits) > IV_HEX_CHARS or any(c not in _HEX for c in digits):
        raise AesKeyError("EXT-X-KEY IV is not a hexadecimal-integer of at most 128 bits")
    return digits.lower().rjust(IV_HEX_CHARS, "0")


async def _fetch_keys(
    segments: Sequence[PlaylistSegment],
    session: aiohttp.ClientSession,
    headers: Mapping[str, str],
    config: FetchConfig,
) -> dict[str, bytes]:
    keys: dict[str, bytes] = {}
    for seg in segments:
        if seg.key is None:
            continue
        uri = seg.key.absolute_uri
        if uri is None:
            raise AesKeyError(f"segment {seg.index}: EXT-X-KEY METHOD=AES-128 without URI")
        if uri not in keys:
            data = await fetch_bytes(uri, session, headers=headers, what="key", config=config)
            if len(data) != AES_KEY_BYTES:
                raise AesKeyError(f"key for segment {seg.index} is {len(data)} bytes, not 16")
            keys[uri] = data  # memory only (SPEC 3.1)
    return keys


def _aes_for(seg: PlaylistSegment, keys: Mapping[str, bytes]) -> AesParams | None:
    if seg.key is None or seg.key.absolute_uri is None:
        return None
    return AesParams(keys[seg.key.absolute_uri].hex(), iv_hex(seg.key, seg.sequence))


def _probe(fetched: FetchedSegment, aes: AesParams | None) -> DemuxProbe | None:
    try:
        return probe_segment(fetched.path, aes)
    except DecodeError:
        # timeline.py falls back to EXTINF for this segment (SPEC 3.1).
        log.warning("segment probe failed", extra={"segment": fetched.index})
        return None


def _write_index(
    path: Path,
    mid: str,
    segments: Sequence[PlaylistSegment],
    fetched: Sequence[FetchedSegment],
    axis: Sequence[SegmentAxis],
) -> None:
    """Adapter segment table (SPEC 3.1). Deliberately no key, IV or key URI."""
    rows = [
        {
            "index": seg.index,
            "sequence": seg.sequence,
            "uri": seg.uri,
            "path": str(f.path),
            "bytes": f.bytes,
            "encrypted": seg.key is not None,
            "start": a.start,
            "length": a.length,
            "origin_pts": a.origin_pts,
            "time_base": str(a.time_base) if a.time_base is not None else None,
        }
        for seg, f, a in zip(segments, fetched, axis, strict=True)
    ]
    doc = {"format": INDEX_FORMAT, "media_id": mid, "segments": rows}
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(path)


async def open_hls(
    src: str,
    *,
    cache_root: Path | None = None,
    headers: Mapping[str, str] | None = None,
    session: aiohttp.ClientSession | None = None,
    config: FetchConfig = FETCH_CONFIG,
) -> HlsSource:
    """Open an HLS source (URL or local path, master or media playlist) and build its Timeline."""
    mid = media_id(src)
    hdrs = dict(headers or {})
    cache_dir = (cache_root or default_cache_root()) / mid
    own = session is None
    client = session or aiohttp.ClientSession()
    try:
        raw = await fetch_bytes(src, client, headers=hdrs, what="playlist", config=config)
        text = raw.decode()
        base = src
        if is_master_playlist(text):
            base = select_variant(text, src)
            raw = await fetch_bytes(base, client, headers=hdrs, what="playlist", config=config)
            text = raw.decode()
        media = parse_media_playlist(text, base)
        keys = await _fetch_keys(media.segments, client, hdrs, config)
        fetched = await fetch_segments(
            media.segments, cache_dir, session=client, headers=hdrs, config=config
        )
    finally:
        if own:
            await client.close()

    aes = [_aes_for(seg, keys) for seg in media.segments]
    probes = [_probe(f, a) for f, a in zip(fetched, aes, strict=True)]
    build = build_timeline(mid, media.segments, probes, [f.bytes for f in fetched], media.events)
    units = [
        DecodeUnit(f.path, ax.start, ax.origin_pts, a)
        for f, ax, a in zip(fetched, build.axis, aes, strict=True)
        if ax.origin_pts is not None
    ]
    cache_dir.mkdir(parents=True, exist_ok=True)
    index_path = cache_dir / "index.json"
    _write_index(index_path, mid, media.segments, fetched, build.axis)
    bad_keys = [
        f.index for f, pr in zip(fetched, probes, strict=True) if pr and pr.head_crc_ok is False
    ]
    if bad_keys:
        # The demux still works (CBC damages one block), but the key or IV rule is wrong.
        log.warning("decrypted segment head fails its CRC", extra={"segments": bad_keys})
    log.info(
        "hls source opened",
        extra={
            "media_id": mid,
            "segments": len(media.segments),
            "undemuxable": sum(p is None for p in probes),
        },
    )
    return HlsSource(mid, build.timeline, build.axis, units, index_path)
