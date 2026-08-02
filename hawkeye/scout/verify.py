"""Replacing the calendar's EPS figures with Yahoo's, before ranking.

Placement is the whole design here. The 2026-08-01 investigation found that
the ranking metric was broken AND that it was the metric deciding which
candidates were ever examined — so verifying after the ranking would leave
the original defect fully intact. Verification therefore runs between the
provisional screen and the final one, and the shortlist is re-derived from
the corrected numbers.

That costs one Yahoo call per verified name (~1s; 120 sequential calls
measured 2026-08-02 at 62 req/min with no rate limiting, so the bound below
is about run duration, not a quota). Verifying every calendar row is not
affordable — an earnings-season week returns thousands — so the set is
chosen deliberately:

1. **Everything the provisional screen kept**, best score first. These are
   the only names that can win an enrichment slot, so a wrong number here
   directly buys or blocks a tribunal seat.
2. **Everything the screen DROPPED that arrived with contradictory consensus
   rows.** This tier exists because the known failure mode is asymmetric:
   collapsing BJRI's contradictory rows to the conservative reading pushed
   its real +3.5% below the 5% screen, so the correct reading was the one
   that disappeared. Without this tier the source split would fix only the
   false positives and leave every false negative invisible — and a false
   negative leaves no trace at all to notice later.

Names beyond the budget keep the calendar's numbers. They are not dropped
and not treated as verified; `eps_source` stays "calendar" so the record
says which reading a decision was actually made on.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Optional, Protocol

from hawkeye.scout.earnings import (
    EarningsEvent,
    ScreenedEvent,
    eps_surprise_pct,
)


class EarningsNumberSource(Protocol):
    def verified_earnings(self, ticker: str, day):
        """VerifiedEarnings or None. None always means "not verified"."""
        ...


@dataclass(frozen=True)
class VerificationStats:
    """What one scan's verification pass actually managed.

    `budget_exhausted` matters as much as the counts: it is the difference
    between "the calendar agreed with Yahoo" and "we never asked", and those
    two must never read the same way in a later review.
    """
    attempted: int = 0
    verified: int = 0
    unverified: int = 0
    disagreed: int = 0            # verified AND materially different
    budget_exhausted: bool = False

    def as_dict(self) -> dict:
        return {"verify_attempted": self.attempted,
                "verify_verified": self.verified,
                "verify_unverified": self.unverified,
                "verify_disagreed": self.disagreed,
                "verify_budget_exhausted": self.budget_exhausted}


# A verified reading that lands this far from the calendar's is a real
# disagreement rather than a rounding difference. Only a counter — nothing
# is accepted or rejected on it, so it is not a doctrine number.
_MATERIAL_DISAGREEMENT_PCT = 1.0


def verification_targets(events: list[EarningsEvent],
                         screened: list[ScreenedEvent],
                         limit: int) -> list[tuple[str, date]]:
    """(ticker, day) keys to verify, in priority order, capped at `limit`."""
    kept = [(s.event.ticker, s.event.day) for s in screened]
    kept_set = set(kept)
    suspect = [(e.ticker, e.day) for e in events
               if e.conflicting_estimates and (e.ticker, e.day) not in kept_set]
    return (kept + suspect)[:max(limit, 0)]


def verify_events(events: list[EarningsEvent],
                  screened: list[ScreenedEvent],
                  source: Optional[EarningsNumberSource],
                  limit: int,
                  ) -> tuple[list[EarningsEvent], VerificationStats]:
    """Return `events` with verified EPS substituted, plus what happened.

    Every event is returned whether or not it was verified — the caller
    re-screens the whole list, so dropping unverified ones here would
    silently narrow the universe.
    """
    if source is None or limit <= 0:
        return events, VerificationStats()

    targets = verification_targets(events, screened, limit)
    eligible = len(verification_targets(events, screened, len(events) + 1))
    wanted = set(targets)
    attempted = verified = unverified = disagreed = 0

    out: list[EarningsEvent] = []
    for event in events:
        if (event.ticker, event.day) not in wanted:
            out.append(event)
            continue
        attempted += 1
        found = source.verified_earnings(event.ticker, event.day)
        if found is None:
            unverified += 1
            out.append(event)
            continue
        verified += 1
        calendar_pct = eps_surprise_pct(event)
        if (calendar_pct is not None
                and abs(found.surprise_pct - calendar_pct)
                >= _MATERIAL_DISAGREEMENT_PCT):
            disagreed += 1
        out.append(replace(
            event,
            eps_actual=found.eps_actual,
            eps_estimate=found.eps_estimate,
            eps_source="yahoo",
            eps_surprise_pct_reported=found.surprise_pct,
            calendar_eps_surprise_pct=(
                round(calendar_pct, 2) if calendar_pct is not None else None),
            # Kept, not discarded: "both sources agree" is what a beat now
            # rests on, and it cannot be checked against an overwritten value.
            calendar_eps_actual=event.eps_actual,
            calendar_eps_estimate=event.eps_estimate))

    return out, VerificationStats(
        attempted=attempted, verified=verified, unverified=unverified,
        disagreed=disagreed, budget_exhausted=eligible > len(targets))
