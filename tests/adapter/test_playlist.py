"""tests/adapter: HLS playlist parsing and the five PlaylistEvent kinds (SPEC 3.1).

Each event kind has a synthetic-HLS positive case and a case that must not trigger (AGENT.md 6.2);
edge cases of the pure functions use hand-written playlist text.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from make_synth import SynthResult

from skua.adapter.playlist import (
    CONFIG,
    PlaylistConfig,
    UnsupportedPlaylistError,
    filename_template,
    is_master_playlist,
    parse_media_playlist,
    robust_z,
    select_variant,
    uri_signature,
)
from skua.schema import PLAYLIST_EVENT_KINDS, PlaylistEvent

BASE = "https://example.com/show/v1/index.m3u8"


def parse_file(path: Path) -> list[PlaylistEvent]:
    return parse_media_playlist(path.read_text(), str(path)).events


def kinds(events: list[PlaylistEvent], kind: str) -> list[tuple[float, str]]:
    return [(e.ts, e.payload["unit"]) for e in events if e.kind == kind]


def media(*entries: str, header: str = "", end: bool = True) -> str:
    """Media playlist from entries: 'D' = discontinuity, 'K:<attrs>' = key, 'R:<attrs>' =
    daterange, '#...' = verbatim tag, '<dur> <uri>' = segment."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10", header]
    for entry in entries:
        if entry == "D":
            lines.append("#EXT-X-DISCONTINUITY")
        elif entry.startswith("K:"):
            lines.append(f"#EXT-X-KEY:{entry[2:]}")
        elif entry.startswith("R:"):
            lines.append(f"#EXT-X-DATERANGE:{entry[2:]}")
        elif entry.startswith("#"):
            lines.append(entry)  # any other tag, verbatim
        else:
            dur, uri = entry.split()
            lines += [f"#EXTINF:{dur},", uri]
    if end:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(line for line in lines if line) + "\n"


def segs(n: int, dur: str = "4.0", tpl: str = "seg{:03d}.ts") -> list[str]:
    return [f"{dur} {tpl.format(i)}" for i in range(n)]


# --- synthetic HLS: positive cases -----------------------------------------------------------


def test_synth_event_sequence(synth_ad_break: SynthResult) -> None:
    mp = parse_media_playlist(synth_ad_break.playlist.read_text(), str(synth_ad_break.playlist))
    assert [s.uri for s in mp.segments] == [s.uri for s in synth_ad_break.segments]
    assert [s.start for s in mp.segments] == [s.start for s in synth_ad_break.segments]
    assert mp.uri_signature_enabled
    seq = [(e.ts, e.kind, e.payload["unit"]) for e in mp.events]
    assert seq == [
        (24.0, "discontinuity", "seg006.ts"),
        (48.0, "discontinuity", "ads/ad012.ts"),
        (48.0, "uri_pattern_break", "ads/ad012.ts"),
        (52.0, "uri_pattern_break", "ads/ad013.ts"),
        (56.0, "uri_pattern_break", "ads/ad014.ts"),
        (60.0, "discontinuity", "seg015.ts"),
    ]


def test_synth_daterange_is_placed_by_start_date_not_tag_position(
    synth_marked: SynthResult,
) -> None:
    text = synth_marked.playlist.read_text()
    lines = text.splitlines()
    # The tag physically precedes seg000 ...
    tag = next(i for i, line in enumerate(lines) if line.startswith("#EXT-X-DATERANGE"))
    assert lines.index("seg000.ts") > tag
    assert all(not line.startswith("#EXTINF") for line in lines[:tag])
    # ... yet it covers the ad, because START-DATE is mapped through the PDT.
    events = parse_media_playlist(text, str(synth_marked.playlist)).events
    ranges = [e for e in events if e.kind == "daterange"]
    assert [e.payload["unit"] for e in ranges] == ["ads/ad012.ts", "ads/ad013.ts", "ads/ad014.ts"]
    assert {e.payload["anchored_by"] for e in ranges} == {"program-date-time"}
    # Counterfactual: the same playlist without its PDT can only anchor by position.
    no_pdt = "\n".join(x for x in lines if not x.startswith("#EXT-X-PROGRAM-DATE-TIME")) + "\n"
    fallback = parse_media_playlist(no_pdt, str(synth_marked.playlist)).events
    units = [e.payload["unit"] for e in fallback if e.kind == "daterange"]
    assert units == ["seg000.ts", "seg001.ts", "seg002.ts"]


def test_synth_marked_daterange_and_duration_outlier(synth_marked: SynthResult) -> None:
    events = parse_file(synth_marked.playlist)
    ad_units = ["ads/ad012.ts", "ads/ad013.ts", "ads/ad014.ts"]
    assert kinds(events, "daterange") == list(zip([48.0, 52.0, 56.0], ad_units, strict=True))
    assert all(e.payload["class"] == "ad" for e in events if e.kind == "daterange")
    assert all(e.payload["duration"] == 10.0 for e in events if e.kind == "daterange")
    # The 2 s ad segment is a non-final outlier; the final c3 segment is never judged.
    (outlier,) = [e for e in events if e.kind == "duration_outlier"]
    assert outlier.payload["unit"] == "ads/ad014.ts" and outlier.payload["z"] < -CONFIG.outlier_z
    assert kinds(events, "discontinuity")[-1] == (58.0, "seg015.ts")


# --- synthetic HLS: cases that must not trigger ----------------------------------------------


def test_synth_same_uri_has_no_uri_pattern_break(synth_same_uri: SynthResult) -> None:
    events = parse_file(synth_same_uri.playlist)
    assert kinds(events, "uri_pattern_break") == []
    assert len(kinds(events, "discontinuity")) == 3  # structure otherwise identical


def test_synth_unmarked_has_no_daterange_key_change_or_outlier(
    synth_ad_break: SynthResult,
) -> None:
    events = parse_file(synth_ad_break.playlist)
    for kind in ("daterange", "key_change", "duration_outlier"):
        assert kinds(events, kind) == [], kind


def test_synth_aes_key_changes(synth_aes: SynthResult) -> None:
    events = parse_file(synth_aes.playlist)
    # C2 inherits k1 across the discontinuity: no change there; the clear ad and k2 are changes.
    assert kinds(events, "key_change") == [(48.0, "ads/ad012.ts"), (60.0, "seg015.ts")]
    methods = [e.payload["method"] for e in events if e.kind == "key_change"]
    assert methods == ["NONE", "AES-128"]


def test_every_event_kind_has_a_synthetic_positive(
    synth_ad_break: SynthResult, synth_marked: SynthResult, synth_aes: SynthResult
) -> None:
    all_events = [
        e for r in (synth_ad_break, synth_marked, synth_aes) for e in parse_file(r.playlist)
    ]
    assert {e.kind for e in all_events} == set(PLAYLIST_EVENT_KINDS)


# --- master playlist -------------------------------------------------------------------------


def test_select_highest_variant_from_synth(synth_ad_break: SynthResult) -> None:
    master = synth_ad_break.master
    text = master.read_text()
    assert is_master_playlist(text)
    assert not is_master_playlist(synth_ad_break.playlist.read_text())
    assert select_variant(text, str(master)) == str(synth_ad_break.playlist)


def test_select_variant_ignores_iframe_and_resolves_relative() -> None:
    text = "\n".join(
        [
            "#EXTM3U",
            "#EXT-X-STREAM-INF:BANDWIDTH=900000,AVERAGE-BANDWIDTH=100",
            "hi/index.m3u8",
            "#EXT-X-STREAM-INF:BANDWIDTH=800000,AVERAGE-BANDWIDTH=850000",
            "lo/index.m3u8",
            '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=99000000,URI="iframe.m3u8"',
            "#EXT-X-STREAM-INF:BANDWIDTH=900000",
            "tie/index.m3u8",
        ]
    )
    assert select_variant(text, "https://example.com/v/master.m3u8") == (
        "https://example.com/v/hi/index.m3u8"
    )


def test_select_variant_rejects_media_playlist() -> None:
    with pytest.raises(ValueError, match="not a master"):
        select_variant(media(*segs(2)), BASE)


# --- uri_pattern_break signature -------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "template"),
    [
        ("seg001.ts", "seg#.ts"),
        ("segment_12_720p.ts", "segment_#_#p.ts"),
        ("index0.ts", "index#.ts"),
        # The SPEC 3.1 v0.3 pair: digits glued to a word no longer swallow the word.
        ("segment00012.ts", "segment#.ts"),
        ("advert00012.ts", "advert#.ts"),
        ("3f9a1c2b7d.ts", "#.ts"),  # whole hex word >= 8
        ("deadbeef.ts", "#.ts"),  # whole hex word >= 8, digits not required
        ("cafe1234.ts", "#.ts"),
        ("abc1234.ts", "abc#.ts"),  # hex but 7 chars: only digits replaced
        ("aGVsbG8gd29ybGQh.ts", "#.ts"),  # 16-char mixed-case base64 word
        ("Q2h1bmsrMTIzNDU2Nzg=.ts", "#.ts"),  # with '+'-free padding '='
        ("aGVsbG8gd29ybGQ.ts", "aGVsbG#gd#ybGQ.ts"),  # 15 chars: too short for base64
        ("abcdefghijk12345.ts", "abcdefghijk#.ts"),  # 16 chars but no uppercase
        ("ABCDEFGHIJK12345.ts", "ABCDEFGHIJK#.ts"),  # 16 chars but no lowercase
        ("Chunk-7f3a9e21-0042.ts", "Chunk-#-#.ts"),
        ("media_b2000000_12.ts", "media_#_#.ts"),  # 'b2000000' is an 8-char hex word
        ("media_k2000000_12.ts", "media_k#_#.ts"),  # 'k' is not hex: digits only
        ("seg.ts", "seg.ts"),
    ],
)
def test_filename_template(name: str, template: str) -> None:
    assert filename_template(name) == template


def test_glued_digit_names_still_break() -> None:
    # Before SPEC v0.3 both names collapsed to '#.ts' and the ad went unnoticed.
    entries = segs(6, tpl="segment{:05d}.ts") + segs(2, tpl="advert{:05d}.ts")
    mp = parse_media_playlist(media(*entries), BASE)
    assert [u for _, u in kinds(mp.events, "uri_pattern_break")] == [
        "advert00000.ts",
        "advert00001.ts",
    ]


def test_signature_drops_query_and_fragment() -> None:
    a = uri_signature("https://cdn.example.com/v/seg001.ts?token=abc123def456&exp=1700000000")
    b = uri_signature("https://cdn.example.com/v/seg002.ts#t=5")
    c = uri_signature("https://cdn.example.com/v/seg003.ts")
    assert a == b == c


def test_signature_separates_host_depth_and_template() -> None:
    base = uri_signature("https://cdn.example.com/a/b/seg001.ts")
    assert base != uri_signature("https://ads.example.com/a/b/seg001.ts")
    assert base != uri_signature("https://cdn.example.com/a/seg001.ts")
    assert base != uri_signature("https://cdn.example.com/a/b/ad001.ts")
    assert base == uri_signature("https://CDN.example.com:443/a/b/seg999.ts")
    assert "example" not in base  # hosts appear only as a hash


def test_hash_named_segments_do_not_all_break() -> None:
    names = [f"{i:04x}a7c9e3f1b2d{i:02d}.ts" for i in range(10)]  # unique hash-like names
    mp = parse_media_playlist(media(*[f"4.0 v/{n}" for n in names]), BASE)
    assert mp.uri_signature_enabled
    assert kinds(mp.events, "uri_pattern_break") == []


def test_uri_signal_disabled_below_majority_threshold(caplog: pytest.LogCaptureFixture) -> None:
    # 5 of 10 (50 %) share the majority signature: below 60 %, so the whole signal is off.
    entries = segs(5) + [f"4.0 x{i}/s.ts" for i in range(5)]
    mp = parse_media_playlist(media(*entries), BASE)
    assert not mp.uri_signature_enabled
    assert kinds(mp.events, "uri_pattern_break") == []
    assert "uri_pattern_break disabled" in caplog.text


def test_uri_signal_enabled_at_majority_threshold() -> None:
    # 6 of 10 (60 %) is enough; the four odd ones out are reported.
    entries = segs(6) + [f"4.0 ads/ad{i}.ts" for i in range(4)]
    mp = parse_media_playlist(media(*entries), BASE)
    assert mp.uri_signature_enabled
    assert [u for _, u in kinds(mp.events, "uri_pattern_break")] == [
        f"ads/ad{i}.ts" for i in range(4)
    ]


# --- duration_outlier ------------------------------------------------------------------------


def test_robust_z_uses_mad() -> None:
    z = robust_z([4.0, 4.0, 4.1, 3.9, 4.0, 2.0])
    assert z is not None
    mad = 0.05  # median 4.0; |dev| = 0, 0, .1, .1, 0, 2 -> median .05
    assert z[-1] == pytest.approx((2.0 - 4.0) / (1.4826 * mad))


def test_robust_z_falls_back_to_mean_absolute_deviation_when_mad_is_zero() -> None:
    values = [4.0] * 9 + [2.0]  # MAD = 0
    z = robust_z(values)
    assert z is not None
    mean_ad = 2.0 / 10
    assert z[-1] == pytest.approx(-2.0 / (1.2533 * mean_ad))
    assert z[0] == 0.0


def test_robust_z_none_when_all_equal() -> None:
    assert robust_z([4.0] * 5) is None
    assert robust_z([]) is None


def test_last_segment_is_neither_counted_nor_judged() -> None:
    # A short final segment is legal HLS: no event, even though it is far from the others.
    events = parse_media_playlist(media(*segs(9), "0.5 seg009.ts"), BASE).events
    assert kinds(events, "duration_outlier") == []


def test_last_segment_does_not_shift_statistics() -> None:
    # Without the final 30 s segment the MAD is 0 and the fallback kicks in; with it counted
    # the 2 s segment would look less extreme. Either way the final one is not judged.
    entries = segs(9) + ["2.0 seg009.ts", "4.0 seg010.ts", "30.0 seg011.ts"]
    events = parse_media_playlist(media(*entries), BASE).events
    assert [u for _, u in kinds(events, "duration_outlier")] == ["seg009.ts"]


def test_no_outlier_when_durations_vary_mildly() -> None:
    entries = [f"{d} seg{i:03d}.ts" for i, d in enumerate([4.0, 3.96, 4.04, 4.0, 3.92, 4.08, 4.0])]
    assert kinds(parse_media_playlist(media(*entries), BASE).events, "duration_outlier") == []


def test_outlier_threshold_comes_from_config() -> None:
    entries = [*segs(5), "4.4 seg005.ts", "4.0 seg006.ts", "1.0 last.ts"]
    loose = parse_media_playlist(media(*entries), BASE, PlaylistConfig(outlier_z=1e9)).events
    strict = parse_media_playlist(media(*entries), BASE).events
    assert kinds(loose, "duration_outlier") == []
    assert [u for _, u in kinds(strict, "duration_outlier")] == ["seg005.ts"]


# --- discontinuity ---------------------------------------------------------------------------


def test_discontinuity_applies_to_following_segment() -> None:
    events = parse_media_playlist(media("4.0 a.ts", "D", "4.0 b.ts", "4.0 c.ts"), BASE).events
    assert kinds(events, "discontinuity") == [(4.0, "b.ts")]


def test_no_discontinuity_without_tag() -> None:
    assert kinds(parse_media_playlist(media(*segs(4)), BASE).events, "discontinuity") == []


# --- key_change ------------------------------------------------------------------------------

K1 = 'K:METHOD=AES-128,URI="k1.key",IV=0x01'
K2 = 'K:METHOD=AES-128,URI="k2.key",IV=0x01'


def test_first_key_is_not_a_change() -> None:
    events = parse_media_playlist(media(K1, *segs(4)), BASE).events
    assert kinds(events, "key_change") == []


def test_key_changes_including_switch_to_and_from_clear() -> None:
    text = media(
        "4.0 a.ts",
        K1,
        "4.0 b.ts",
        "4.0 c.ts",
        K2,
        "4.0 d.ts",
        "K:METHOD=NONE",
        "4.0 e.ts",
    )
    events = parse_media_playlist(text, BASE).events
    assert kinds(events, "key_change") == [(4.0, "b.ts"), (12.0, "d.ts"), (16.0, "e.ts")]
    methods = [e.payload["method"] for e in events if e.kind == "key_change"]
    assert methods == ["AES-128", "AES-128", "NONE"]


def test_repeated_identical_key_tag_is_not_a_change() -> None:
    events = parse_media_playlist(media(K1, "4.0 a.ts", K1, "4.0 b.ts"), BASE).events
    assert kinds(events, "key_change") == []


def test_iv_change_is_a_key_change() -> None:
    k1b = 'K:METHOD=AES-128,URI="k1.key",IV=0x02'
    events = parse_media_playlist(media(K1, "4.0 a.ts", k1b, "4.0 b.ts"), BASE).events
    assert kinds(events, "key_change") == [(4.0, "b.ts")]


# --- daterange -------------------------------------------------------------------------------

PDT = "#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00.000Z"


def dr(attrs: str) -> str:
    return f'R:ID="x",START-DATE="2026-01-01T00:00:04.000Z",{attrs}'


def test_daterange_class_ad_covers_duration() -> None:
    text = media("4.0 a.ts", dr('CLASS="ad",DURATION=8.0'), *segs(4), header=PDT)
    events = parse_media_playlist(text, BASE).events
    assert kinds(events, "daterange") == [(4.0, "seg000.ts"), (8.0, "seg001.ts")]


def test_daterange_planned_duration_and_partial_overlap() -> None:
    text = media("4.0 a.ts", dr('CLASS="AD",PLANNED-DURATION=9'), *segs(4), header=PDT)
    covered = kinds(parse_media_playlist(text, BASE).events, "daterange")
    assert [u for _, u in covered] == ["seg000.ts", "seg001.ts", "seg002.ts"]


def test_daterange_without_duration_covers_anchor_only() -> None:
    text = media("4.0 a.ts", dr("SCTE35-OUT=0xFC30"), *segs(3), header=PDT)
    events = [e for e in parse_media_playlist(text, BASE).events if e.kind == "daterange"]
    assert [(e.ts, e.payload["unit"]) for e in events] == [(4.0, "seg000.ts")]
    assert events[0].payload["scte35"] == ["out"] and events[0].payload["class"] is None


def ranges(text: str) -> list[PlaylistEvent]:
    return [e for e in parse_media_playlist(text, BASE).events if e.kind == "daterange"]


def at(seconds: int, offset: str = "Z") -> str:
    return f"2026-01-01T00:00:{seconds:02d}.000{offset}"


def test_daterange_start_date_wins_over_tag_position() -> None:
    # Tag attached to seg000, START-DATE pointing into seg002 (PDT 00:00:00 on seg000).
    tag = f'R:ID="x",CLASS="ad",START-DATE="{at(9)}",DURATION=6'
    events = ranges(media(tag, *segs(5), header=PDT))
    assert [(e.ts, e.payload["unit"]) for e in events] == [(8.0, "seg002.ts"), (12.0, "seg003.ts")]
    assert events[0].payload["anchor"] == "seg002.ts"
    assert events[0].payload["anchored_by"] == "program-date-time"


def test_daterange_follows_pdt_jump_at_discontinuity() -> None:
    # The second epoch restarts the wall clock one hour later; START-DATE lands in it.
    text = media(
        "4.0 a.ts",
        "4.0 b.ts",
        "D",
        "#EXT-X-PROGRAM-DATE-TIME:2026-01-01T01:00:00.000Z",
        'R:ID="x",CLASS="ad",START-DATE="2026-01-01T01:00:04.000Z",DURATION=4',
        "4.0 c.ts",
        "4.0 d.ts",
        "4.0 e.ts",
        header=PDT,
    )
    assert [(e.ts, e.payload["unit"]) for e in ranges(text)] == [(12.0, "d.ts")]


def test_daterange_without_pdt_falls_back_to_tag_position() -> None:
    tag = f'R:ID="x",CLASS="ad",START-DATE="{at(9)}",DURATION=6'
    events = ranges(media("4.0 a.ts", tag, *segs(4)))
    assert [e.payload["unit"] for e in events] == ["seg000.ts", "seg001.ts"]
    assert events[0].payload["anchored_by"] == "position"


@pytest.mark.parametrize(
    ("attrs", "units", "source"),
    [
        (f'DURATION=4,END-DATE="{at(16)}",PLANNED-DURATION=20', ["seg001.ts"], "DURATION"),
        (
            f'END-DATE="{at(16)}",PLANNED-DURATION=20',
            ["seg001.ts", "seg002.ts", "seg003.ts"],
            "END-DATE",
        ),
        ("PLANNED-DURATION=8", ["seg001.ts", "seg002.ts"], "PLANNED-DURATION"),
        ("SCTE35-OUT=0xFC", ["seg001.ts"], None),
        ("DURATION=0", ["seg001.ts"], "DURATION"),
    ],
    ids=["duration-first", "end-date", "planned", "none", "zero-is-a-point"],
)
def test_daterange_span_priority(attrs: str, units: list[str], source: str | None) -> None:
    tag = f'R:ID="x",CLASS="ad",START-DATE="{at(4)}",{attrs}'
    events = ranges(media(tag, *segs(6), header=PDT))
    assert [e.payload["unit"] for e in events] == units
    assert {e.payload["duration_source"] for e in events} == {source}


def test_daterange_outside_playlist_or_without_offset_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    outside = 'R:ID="x",CLASS="ad",START-DATE="2026-01-02T00:00:00.000Z",DURATION=4'
    assert ranges(media(outside, *segs(3), header=PDT)) == []
    assert "maps to no segment" in caplog.text
    naive = 'R:ID="y",CLASS="ad",START-DATE="2026-01-01T00:00:04.000",DURATION=4'
    assert ranges(media(naive, *segs(3), header=PDT)) == []
    assert "lacks a usable offset" in caplog.text


def test_segments_before_first_pdt_are_not_mappable() -> None:
    text = media(
        "4.0 pre0.ts",
        "4.0 pre1.ts",
        "#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:00.000Z",
        'R:ID="x",CLASS="ad",START-DATE="2025-12-31T23:59:56.000Z",DURATION=8',
        *segs(2),
    )
    # Only the part of the range that reaches PDT-mapped segments counts.
    assert [e.payload["unit"] for e in ranges(text)] == ["seg000.ts"]


def test_daterange_without_ad_marker_is_ignored() -> None:
    text = media("4.0 a.ts", dr('CLASS="chapter",DURATION=8'), *segs(3), header=PDT)
    assert kinds(parse_media_playlist(text, BASE).events, "daterange") == []


# --- unsupported inputs (SPEC 3.1 M0 scope) --------------------------------------------------


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (media(*segs(2), header='#EXT-X-MAP:URI="init.mp4"'), "fMP4"),
        (media('K:METHOD=SAMPLE-AES,URI="k.key"', *segs(2)), "SAMPLE-AES"),
        (media('K:METHOD=SAMPLE-AES-CTR,URI="k"', *segs(2)), "SAMPLE-AES"),
        (media(*segs(2), end=False), "ENDLIST"),
        (media(*segs(2), header="#EXT-X-BYTERANGE:100@0"), "BYTERANGE"),
        (media(), "no segments"),
    ],
    ids=["fmp4", "sample-aes", "sample-aes-ctr", "live", "byterange", "empty"],
)
def test_unsupported_playlists_fail_loudly(text: str, match: str) -> None:
    with pytest.raises(UnsupportedPlaylistError, match=match):
        parse_media_playlist(text, BASE)


def test_master_given_to_media_parser() -> None:
    with pytest.raises(ValueError, match="master"):
        parse_media_playlist("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\na.m3u8\n", BASE)


def test_events_are_valid_schema_objects_sorted_by_time(synth_marked: SynthResult) -> None:
    events = parse_file(synth_marked.playlist)
    assert all(isinstance(e, PlaylistEvent) and e.payload["unit"] for e in events)
    assert [e.ts for e in events] == sorted(e.ts for e in events)
