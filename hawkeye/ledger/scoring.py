"""Scoring: calibration (Brier) and skill-vs-luck attribution.

The whole project rests on being able to answer, years later:
"was that decision skill or luck?" The mechanism:

1. Every thesis pre-registers claims with probabilities and deadlines.
2. Claims resolve TRUE/FALSE at horizon (recorded in the ledger journal).
3. Brier score measures calibration of the stated probabilities.
4. The 2x2 of (thesis right/wrong) x (made/lost money) classifies each
   closed trade into skill_win / lucky_win / unlucky_loss / deserved_loss.

A profitable book full of lucky_wins is a warning, not a success.
"""
from __future__ import annotations

from hawkeye.contracts.models import OutcomeQuadrant

# (stated probability, resolved outcome) pairs
ResolvedClaim = tuple[float, bool]


def brier_score(resolved: list[ResolvedClaim]) -> float | None:
    """Mean squared error of stated probabilities. 0 = perfect, 0.25 = coin flip."""
    if not resolved:
        return None
    return sum((p - (1.0 if o else 0.0)) ** 2 for p, o in resolved) / len(resolved)


def thesis_accuracy(resolved: list[ResolvedClaim]) -> float | None:
    """Fraction of resolved claims that came true."""
    if not resolved:
        return None
    return sum(1 for _, o in resolved if o) / len(resolved)


def classify_outcome(pnl_pct: float, accuracy: float | None,
                     accuracy_threshold: float = 0.6) -> OutcomeQuadrant | None:
    if accuracy is None:
        return None
    thesis_right = accuracy >= accuracy_threshold
    won = pnl_pct >= 0
    if thesis_right and won:
        return OutcomeQuadrant.SKILL_WIN
    if not thesis_right and won:
        return OutcomeQuadrant.LUCKY_WIN
    if thesis_right and not won:
        return OutcomeQuadrant.UNLUCKY_LOSS
    return OutcomeQuadrant.DESERVED_LOSS


def calibration_table(resolved: list[ResolvedClaim],
                      edges: tuple[float, ...] = (0.0, 0.5, 0.7, 0.85, 1.01)
                      ) -> list[dict]:
    """Bucketed calibration: within each stated-probability band, how often
    did claims actually come true? Divergence = systematic over/underconfidence."""
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        bucket = [(p, o) for p, o in resolved if lo <= p < hi]
        rows.append({
            "band": f"{lo:.2f}-{min(hi, 1.0):.2f}",
            "n": len(bucket),
            "avg_stated": (sum(p for p, _ in bucket) / len(bucket)) if bucket else None,
            "freq_true": (sum(1 for _, o in bucket if o) / len(bucket)) if bucket else None,
        })
    return rows
