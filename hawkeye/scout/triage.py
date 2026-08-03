"""Deciding whether a company is worth following at all
(docs/design/MASTER_OVERVIEW.ja.md §6.1(E)).

This is not a judgment about a quarter — that is `quality.py`. It is the
cheaper question underneath it: could a position in this name ever exist?
Below the price floor, below the market-cap floor, or too thinly traded to
leave at the pre-registered stop, and the answer is no however good the
print. Names like that are most of what the earnings calendar holds, and
pre-registering consensus for them costs a lookup a day forever.

Two rules keep the saving from turning into damage:

- **Only the gates that describe the COMPANY count.** A stale catalyst or a
  crowded gap is a fact about one print; excluding an issuer for it would
  drop an investable company on the strength of a day.
- **Unverified is never a verdict.** A missing market cap is a free-tier gap,
  and turning it into a permanent exclusion is exactly the silent pass in
  reverse (invariant 6).

The verdict also expires (`config.stock_triage_ttl_days`). A snapshot not
taken can never be taken afterwards, so a wrong exclusion is the one error
here with no recovery — and a $3 company can be a $9 one next quarter.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from hawkeye.contracts.models import GateReport
from hawkeye.contracts.stocks import Stock

# The gates that describe the issuer rather than the event. Deliberately a
# fixed list rather than "every hard gate": `catalyst_freshness` is hard too,
# and it says nothing about the company.
STRUCTURAL_GATES = ("min_price", "min_market_cap", "min_avg_dollar_volume")


@dataclass(frozen=True)
class TriageVerdict:
    is_target: bool
    reason: str = ""


def triage_from_gates(report: GateReport) -> Optional[TriageVerdict]:
    """What this run's entry gates say about the company, or None.

    None means "no opinion" and is the common case: the gates ran on data we
    could not verify, or none of the structural ones ran at all. It leaves
    the master untouched, which leaves the name included.
    """
    checked = [r for r in report.results
               if r.name in STRUCTURAL_GATES and not r.unverified]
    if not checked:
        return None
    failed = [r for r in checked if not r.passed]
    if failed:
        return TriageVerdict(is_target=False,
                             reason="gate: " + ", ".join(r.name
                                                         for r in failed))
    return TriageVerdict(is_target=True)


def rebuild_triage(store) -> int:
    """Recreate every master's triage verdict from the recorded gate reports.

    Required for the same reason as the review projection: a projection that
    cannot be rebuilt quietly becomes a second source of truth, and a
    disagreement between it and the ledger then has no resolution rule. The
    entry-gate report frozen into each dropped-candidate record is the fact;
    this is a reading of it.

    The verdict is dated by the record it came from, never by today, so the
    expiry stays honest — a judgment made in May must not look like one made
    this morning.
    """
    rebuilt = 0
    for stock in store.stocks():
        history = store.history(stock.id)
        if history is None:
            continue
        latest = _latest_gate_report(history.screened)
        if latest is None:
            continue
        recorded_at, report = latest
        verdict = triage_from_gates(report)
        if verdict is None:
            continue
        store.record_triage(stock.id, verdict.is_target, verdict.reason,
                            on=recorded_at)
        rebuilt += 1
    return rebuilt


def _latest_gate_report(screened: list[dict]
                        ) -> Optional[tuple[date, GateReport]]:
    """The newest recorded gate report for one company, with its date."""
    for row in reversed(screened):          # store.history sorts oldest first
        payload = row.get("gate_report")
        if not payload:
            continue
        try:
            report = GateReport.model_validate(payload)
            day = date.fromisoformat(str(row["recorded_at"])[:10])
        except (ValueError, TypeError, KeyError):
            continue
        return day, report
    return None


def is_investigation_target(stock: Optional[Stock], today: date,
                            config) -> bool:
    """Whether this name is still worth spending a lookup on.

    Fails OPEN in every uncertain direction — no master row, no verdict, or a
    verdict old enough to be wrong — because the cost of a wrong exclusion is
    a consensus history that can never be rebuilt, against one wasted call
    for a wrong inclusion.
    """
    if stock is None or stock.investigation_target is not False:
        return True
    checked = stock.investigation_checked_at
    if checked is None:
        return True
    age = (today - checked.date()).days
    return age > config.stock_triage_ttl_days
