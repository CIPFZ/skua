"""skua.evidence.rule_decoder: M0 temporary decoder (M3 删除 / removed in M3) — runs of shots with
an ``ad`` hard prior become ``Segment(label="ad", confidence=0.95)`` (SPEC 3.3).

Replaced by the Semi-Markov decoder in M3 (SPEC 3.6); do not extend it. M0 has no model, so the
``evidence`` of every Segment is the only answer to "why was this skipped" and the entry point
for chasing false positives during golden acceptance: it lists every hard-prior source and the
observed value of each block condition.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from skua.evidence.structural import PriorRange, StructuralEvidence
from skua.schema import Segment, Timeline


@dataclass(frozen=True)
class RuleDecoderConfig:
    """SPEC 3.3. Skip-policy related: not to be tuned without the owner (CLAUDE.md)."""

    confidence: float = 0.95  # SPEC 3.3: Segment(label="ad", confidence=0.95)


CONFIG = RuleDecoderConfig()
DECODER = "m0_rule_decoder"


def _runs(idxs: Sequence[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for i in sorted(idxs):
        if runs and runs[-1][-1] == i - 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return runs


def source_evidence(rng: PriorRange) -> dict[str, Any]:
    """JSON evidence of one hard-prior source (SPEC 3.3)."""
    out: dict[str, Any] = {
        "source": rng.source,
        "first_unit": rng.first_unit,
        "last_unit": rng.last_unit,
        "duration_s": rng.duration_s,
    }
    if rng.conditions is not None:
        c = rng.conditions
        out["conditions"] = {
            "start_discontinuity": c.start_discontinuity,
            "start_video_params": c.start_video_params,
            "uri_pattern_break_hits": c.uri_pattern_break_hits,
            "segments": c.segments,
            "within_max_duration": c.within_max_duration,
            "is_largest_block": c.is_largest_block,
        }
    if rng.daterange is not None:
        out["daterange"] = dict(rng.daterange)
    return out


def rule_decode(
    timeline: Timeline, evidence: StructuralEvidence, config: RuleDecoderConfig = CONFIG
) -> list[Segment]:
    """Merge consecutive hard-prior shots into ``ad`` Segments; nothing else is ever output."""
    segments = []
    for run in _runs(list(evidence.hard_prior)):
        members = set(run)
        sources = [r for r in evidence.ranges if members.issuperset(r.shots)]
        first, last = timeline.shots[run[0]], timeline.shots[run[-1]]
        segments.append(
            Segment(
                start=first.start,
                end=last.end,
                label="ad",
                confidence=config.confidence,
                evidence={
                    "decoder": DECODER,
                    "hard_prior": max(evidence.hard_prior[i] for i in run),
                    "sources": [source_evidence(r) for r in sources],
                },
            )
        )
    return segments
