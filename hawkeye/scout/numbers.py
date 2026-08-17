"""Deciding whose figures a print stands on, before the ranking.

**One print, one vendor, both legs.** A surprise ratio whose numerator and
denominator come from different vendors is arithmetic without meaning: the
earnings feed publishes an adjusted-basis consensus while the calendar's
actual may be GAAP, so "actual beat consensus by 8%" built from one of each is
a number with no referent. The choice is therefore made once, for the whole
print, and recorded on the event (EW移行 Ver2 §1).

Placement is the other half of the design, and it is unchanged from the pass
this replaced. The 2026-08-01 investigation found that the ranking metric was
broken AND that it was the metric deciding which candidates were ever
examined — so reading the real numbers after the ranking would leave the
original defect fully intact. The read therefore runs between the provisional
screen and the final one, and the shortlist is re-derived from what the feed
returned.

Who gets read is chosen deliberately, because the read costs one request per
name and an earnings-season week returns thousands of calendar rows:

1. **Everything the provisional screen kept**, best score first. These are the
   only names that can win an enrichment slot, so a wrong number here directly
   buys or blocks a tribunal seat.
2. **Everything the screen DROPPED that arrived with contradictory consensus
   rows.** The known failure mode is asymmetric: collapsing BJRI's
   contradictory rows to the conservative reading pushed its real +3.5% below
   the 5% screen, so the correct reading was the one that disappeared. Without
   this tier only false positives get fixed — and a false negative leaves no
   trace at all to notice later.

Names beyond the budget, and prints an earlier scan already recorded, keep the
calendar's numbers with `numbers_source` still reading "calendar", so the
record says which reading a decision was actually made on.

Why the feed can decline, and what each answer means, is kept as a NAMED
reason rather than a bare failure. "The feed has not ingested this print yet"
is worth waiting for; "the calendar's date was wrong" is not; "we could not
reach the site" is a fact about the connection and not about the company. The
48-hour hold reads these names (task 6).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Optional, Protocol

from hawkeye.marketdata.whispers import (
    WhispersRecord,
    WhispersUnavailable,
    read_consensus,
)
from hawkeye.scout.earnings import (
    EarningsEvent,
    ScreenedEvent,
    eps_surprise_pct,
)


class WhispersReader(Protocol):
    def details(self, ticker: str) -> Optional[WhispersRecord]:
        """This company's most recent print, or None when it has no record.

        Raises WhispersUnavailable when the feed could not be read — the two
        are never conflated, because only the first is a fact about the
        company.
        """
        ...


@dataclass(frozen=True)
class NumbersStats:
    """What one scan's read of the earnings feed actually managed.

    The three ways it can decline are counted apart because they mean
    different things and lead to different actions. Lumping them into one
    "unverified" tally would hide the only one that is worth waiting on.
    """
    attempted: int = 0
    from_whispers: int = 0        # the feed supplied both legs; it decides
    fell_back: int = 0            # answered, unusably -> the calendar stands
    stale: int = 0                # answered about a different quarter
    unreachable: int = 0          # the connection failed, not the company
    budget_exhausted: bool = False

    def as_dict(self) -> dict:
        return {"numbers_attempted": self.attempted,
                "numbers_from_whispers": self.from_whispers,
                "numbers_fell_back": self.fell_back,
                "numbers_stale": self.stale,
                "numbers_unreachable": self.unreachable,
                "numbers_budget_exhausted": self.budget_exhausted}


def numbers_targets(events: list[EarningsEvent],
                    screened: list[ScreenedEvent],
                    limit: int,
                    always: Optional[list[tuple[str, date]]] = None,
                    skip: Optional[set[tuple[str, date]]] = None
                    ) -> list[tuple[str, date]]:
    """(ticker, day) keys to read, in priority order, capped at `limit`.

    `always` goes first and is the caller naming a print outright — a person
    asking about one stock. The screen decides who is worth looking at when
    nobody asked; once someone has asked, that question is answered.

    `skip` is what an earlier scan already recorded. It bounds both tiers,
    because a print the dedup is about to discard cannot change anything this
    run decides — and a request spent on it is a request not spent on a name
    that can. `always` outranks it: the skip set exists to stop the scan
    re-reading its own window overlap, not to refuse a direct question.
    """
    skipped = skip or set()
    kept = list(always or []) + [(s.event.ticker, s.event.day)
                                 for s in screened
                                 if (s.event.ticker, s.event.day) not in skipped]
    kept = list(dict.fromkeys(kept))
    kept_set = set(kept)
    suspect = [(e.ticker, e.day) for e in events
               if e.conflicting_estimates and (e.ticker, e.day) not in kept_set
               and (e.ticker, e.day) not in skipped]
    return (kept + suspect)[:max(limit, 0)]


def _read_one(source: WhispersReader,
              event: EarningsEvent) -> tuple[Optional[WhispersRecord], str]:
    """The feed's answer for one print, and why it cannot be used — "" when
    it can. Every rejection is named; none is a bare None."""
    try:
        record = source.details(event.ticker)
    except WhispersUnavailable as exc:
        # Two different facts wearing one exception. A connection that failed
        # says nothing about the company and is worth waiting on; a server
        # error the same ticker reproduces every time is the feed refusing
        # this name, and waiting buys the same refusal until the wait runs out.
        return None, ("whispers_unreachable" if getattr(exc, "transient", True)
                      else "whispers_server_error")
    if record is None:
        return None, "whispers_no_record"
    stale = record.staleness_reason(event.day)
    if stale:
        return record, f"whispers_{stale}"
    # Both legs or neither. A record with an EPS pair and no revenue pair is
    # the common case (7 of 47 measured) and is exactly the shape that tempts
    # a half-substitution — which is the vendor mixing this module exists to
    # prevent.
    if record.eps_actual is None or record.eps_consensus is None:
        return record, "whispers_eps_incomplete"
    if record.revenue_actual is None or record.revenue_consensus is None:
        return record, "whispers_revenue_incomplete"
    return record, ""


def _forward_legs(record: WhispersRecord) -> dict:
    """What the summary says about the FUTURE: the analysts' yardsticks, and
    the prose the company's own guidance still has to be read out of.

    The yardsticks are read here, by pattern, because the vendor generates
    that sentence from its own numbers and it therefore has exactly one shape.
    The company's guidance is not, and is no longer read here at all — an
    agent reads it, all of it (hawkeye/scout/guidance_agent.py, User decision
    2026-08-10). What travels from this module is the sentence itself.

    They still travel together, which was the point of reading them in one
    pass: a guidance without its yardstick is scored against whatever else is
    lying around — which for a full-year range used to be next quarter's
    consensus, and read as a four-fold beat.
    """
    consensus = read_consensus(record)
    return {"summary": record.summary or "",
            # Not prose, and not read here: the address of the company's own
            # release, which the cause step fetches later (T-008).
            "article_id": record.file_name or "",
            "full_year_eps_estimate": consensus.full_year_eps,
            "full_year_revenue_estimate": consensus.full_year_revenue,
            "full_year_period": consensus.full_year_period,
            "next_quarter_eps_estimate": consensus.next_quarter_eps,
            "next_quarter_revenue_estimate": consensus.next_quarter_revenue,
            "next_quarter_period": consensus.next_quarter_period}


def _substituted(event: EarningsEvent,
                 record: WhispersRecord) -> EarningsEvent:
    """`event` restated on the feed's figures, with the calendar's kept."""
    calendar_pct = eps_surprise_pct(event)
    # The vendor publishes its own surprise, but not always against
    # consensus: where a whisper number exists it measures against THAT
    # instead, and AMD 2026-08-04 reads -1.78% from the vendor against +2.47%
    # from actual over consensus. Taking it only when the basis is consensus
    # is what keeps a beat from arriving labelled as a miss.
    reported = (record.vendor_eps_surprise_pct
                if record.vendor_surprise_basis == "consensus" else None)
    return replace(
        event,
        **_forward_legs(record),
        eps_actual=record.eps_actual,
        eps_estimate=record.eps_consensus,
        revenue_actual=record.revenue_actual,
        revenue_estimate=record.revenue_consensus,
        # Travels only on this branch, where the feed's own actual is what the
        # event now stands on. The declined branch below deliberately does not
        # carry it: a whisper beside a calendar actual is the cross-vendor
        # comparison this module exists to prevent.
        whisper=record.whisper,
        numbers_source="whispers",
        numbers_reason="",
        eps_surprise_pct_reported=reported,
        # Kept, not discarded: how far the two vendors disagree is itself
        # data, and it cannot be measured against an overwritten value.
        calendar_eps_surprise_pct=(round(calendar_pct, 2)
                                   if calendar_pct is not None else None),
        calendar_eps_actual=event.eps_actual,
        calendar_eps_estimate=event.eps_estimate,
        calendar_revenue_actual=event.revenue_actual,
        calendar_revenue_estimate=event.revenue_estimate,
        # The feed derives its label from the company's OWN period and year
        # ends, which is the strongest of the four bases; the calendar's
        # year/quarter fields are the second. Prefer the stronger one.
        fiscal_quarter=record.fiscal_quarter or event.fiscal_quarter,
        announced_at=record.announced_at)


def _declined(event: EarningsEvent, record: Optional[WhispersRecord],
              reason: str) -> EarningsEvent:
    """`event` on the calendar's figures, keeping whatever the feed's answer
    still carries that the numbers rule does not govern.

    Guidance is the case that matters. The one-vendor rule is about the
    SURPRISE RATIO: its actual and its consensus have to come from the same
    place, because one vendor's adjusted-basis estimate over another's GAAP
    actual is a percentage with no referent. Guidance is a third leg measured
    against a third figure entirely — next quarter's consensus — so reading it
    off the feed's prose while the ratio stands on the calendar's numbers
    mixes nothing.

    Discarding it was measurable waste: 8 of 50 names on a live run declined
    for a missing revenue consensus while the same response held their
    guidance sentence, and guidance has no other free source anywhere.

    A record for a DIFFERENT print is the exception. Its summary describes the
    previous quarter's guidance, so `covers()` gates this.
    """
    if record is None or not record.covers(event.day):
        return replace(event, numbers_reason=reason)
    return replace(event, numbers_reason=reason,
                   announced_at=record.announced_at or event.announced_at,
                   **_forward_legs(record))


def read_numbers(events: list[EarningsEvent],
                 screened: list[ScreenedEvent],
                 source: Optional[WhispersReader],
                 limit: int,
                 always: Optional[list[tuple[str, date]]] = None,
                 skip: Optional[set[tuple[str, date]]] = None,
                 ) -> tuple[list[EarningsEvent], NumbersStats]:
    """Return `events` restated on the feed's figures, plus what happened.

    Every event is returned whether or not it was read — the caller re-screens
    the whole list, so dropping the unread ones here would silently narrow the
    universe. That holds for skipped and unreachable names too.
    """
    if source is None or limit <= 0:
        return events, NumbersStats()

    targets = numbers_targets(events, screened, limit, always, skip)
    eligible = len(numbers_targets(events, screened, len(events) + 1,
                                   always, skip))
    wanted = set(targets)
    attempted = chosen = fell_back = stale = unreachable = 0

    out: list[EarningsEvent] = []
    for event in events:
        if (event.ticker, event.day) not in wanted:
            out.append(event)
            continue
        attempted += 1
        record, reason = _read_one(source, event)
        if not reason:
            chosen += 1
            out.append(_substituted(event, record))
            continue
        if reason in ("whispers_unreachable", "whispers_server_error"):
            unreachable += 1
        elif reason in ("whispers_previous_quarter", "whispers_later_print",
                        "whispers_announcement_time_missing"):
            stale += 1
        else:
            fell_back += 1
        out.append(_declined(event, record, reason))

    return out, NumbersStats(
        attempted=attempted, from_whispers=chosen, fell_back=fell_back,
        stale=stale, unreachable=unreachable,
        budget_exhausted=eligible > len(targets))
