"""Earnings-surprise screening (pure functions, no network).

The scout's first filter is mechanical: EPS/revenue surprise magnitudes from
the earnings calendar. No human picks the universe — that is the point.
Manual candidate entry remains possible via `hawkeye evaluate`, but scouted
and manual candidates are distinguishable in the ledger, so the experiment
can always separate "system-sourced" from "human-sourced" performance.

A percentage surprise is only as trustworthy as its denominator, and this
module is where that is decided. Three ways it can lie, all found on
2026-08-01 and all pushing the same direction — inflating candidates whose
number means the least:

1. The calendar returns several rows for one print with different consensus
   figures. Ranked by surprise, the row with the most wrong estimate wins.
   `parse_calendar` collapses them to the conservative reading.
2. `revenueActual` and `revenueEstimate` can be on different accounting
   bases (gross vs net for lenders), which reads as a several-hundred-percent
   beat. Beyond a sanity band the number is marked untrusted.
3. A near-zero consensus makes the ratio explode without adding information
   (REITs report FFO, so GAAP EPS consensus sits near zero). Below a minimum
   absolute estimate the percentage is marked untrusted.

Untrusted never means "silently dropped" and never means "treated as a
beat": it means the number earns no ranking score, so it cannot crowd a
genuine surprise out of the scarce enrichment slots.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import NamedTuple, Optional

from hawkeye.contracts.stocks import GuidanceReading, resolve_fiscal_quarter


@dataclass(frozen=True)
class EarningsEvent:
    ticker: str
    day: date
    eps_actual: Optional[float]
    eps_estimate: Optional[float]
    revenue_actual: Optional[float]
    revenue_estimate: Optional[float]
    conflicting_estimates: bool = False   # the calendar disagreed with itself
    # Which vendor ALL FOUR figures above came from: "calendar" (the Finnhub
    # earnings calendar) or "whispers" (EarningsWhispers). One print stands on
    # one vendor, both legs — a ratio whose actual and consensus come from
    # different vendors compares an adjusted-basis estimate against a possibly
    # GAAP actual (hawkeye/scout/numbers.py).
    numbers_source: str = "calendar"
    # Why the feed's figures are NOT the ones above, when it was asked. Empty
    # means either "the feed supplied them" (numbers_source == "whispers") or
    # "nobody asked" — which `numbers_source` tells apart. Named rather than
    # boolean because "has not ingested this print yet" is worth waiting for
    # and "could not reach the site" is not a fact about the company at all.
    numbers_reason: str = ""
    # The surprise as the source PUBLISHED it, when it published one on the
    # same basis we measure on. Vendors compute from full precision while
    # displaying a rounded estimate, so recomputing from the two displayed
    # numbers understates the beat (BJRI 2026-07-30: 0.90/0.94 displayed,
    # +4.95% published, +4.44% if recomputed). When this is set it wins over
    # any computation — enforced here rather than left to callers to remember.
    eps_surprise_pct_reported: Optional[float] = None
    # What the calendar read before the feed's figures replaced it. Kept so
    # the disagreement rate between the two sources accumulates as data
    # instead of being overwritten silently.
    calendar_eps_surprise_pct: Optional[float] = None
    # The calendar's own four figures, kept alongside the chosen pair rather
    # than replaced by them (2026-08-02). How far the vendors disagree is a
    # measurement this system takes, and it is impossible against a value that
    # was overwritten.
    calendar_eps_actual: Optional[float] = None
    calendar_eps_estimate: Optional[float] = None
    calendar_revenue_actual: Optional[float] = None
    calendar_revenue_estimate: Optional[float] = None
    # When the release actually crossed the wire, from the feed. The clock a
    # 48-hour hold would run on is NOT this one (a held print is one the feed
    # has not ingested, so its timestamp belongs to the previous quarter) —
    # this is here for the record and for the report.
    announced_at: Optional[datetime] = None
    # The third leg, read deterministically off the same response that carried
    # the numbers. `guidance_reason` names what could not be read, so a
    # company that guided in an unhandled form is never indistinguishable from
    # one that guided nothing (invariant 6).
    guidance: Optional[GuidanceReading] = None
    guidance_reason: str = ""
    # The analysts' FULL-YEAR figures, read from the same sentence that named
    # the guidance above. They travel together because they only mean anything
    # together: a full-year range judged against next quarter's consensus is
    # the +348% ADM never guided, and judged against nothing at all it is
    # recorded as a company that published no outlook. `full_year_period`
    # is what lets the comparison refuse a yardstick from another year.
    full_year_eps_estimate: Optional[float] = None
    full_year_revenue_estimate: Optional[float] = None
    full_year_period: str = ""
    # The same thing one period down, for a company that guided the quarter
    # rather than the year. It used to come from a separate Yahoo lookup taken
    # at a different moment than the guidance it judges; it is in this
    # response's own prose, so it now travels with it and costs no request.
    next_quarter_eps_estimate: Optional[float] = None
    next_quarter_revenue_estimate: Optional[float] = None
    next_quarter_period: str = ""
    # The source's own fiscal label (`2026-Q2`), when the calendar gave one.
    # Preferred over the calendar quarter of the report date, which is wrong
    # for any company whose fiscal year does not end in December.
    fiscal_quarter: Optional[str] = None
    # Every distinct EPS actual the calendar returned for this one print.
    # AMZN's came back as 1.88 on one row and 1.97 on another; collapsing to
    # the conservative row is right for ranking, but a print row holding only
    # one of them would read as a clean single-source figure. Two values here
    # means the source contradicts itself, and its actual is unusable.
    all_eps_actuals: tuple[float, ...] = ()


class ScreenedEvent(NamedTuple):
    """An event that survived the screen, with how far its numbers are
    trusted. `scored_*` is what ranking may use — an untrusted number
    contributes nothing rather than a large bogus bonus."""
    event: EarningsEvent
    eps_surprise_pct: Optional[float]
    revenue_surprise_pct: Optional[float]
    eps_surprise_trusted: bool
    revenue_surprise_trusted: bool

    @property
    def scored_eps_pct(self) -> Optional[float]:
        return self.eps_surprise_pct if self.eps_surprise_trusted else None

    @property
    def scored_revenue_pct(self) -> Optional[float]:
        return (self.revenue_surprise_pct
                if self.revenue_surprise_trusted else None)


def _surprise_pct(actual: Optional[float],
                  estimate: Optional[float]) -> Optional[float]:
    if actual is None or estimate is None or estimate == 0:
        return None
    return (actual - estimate) / abs(estimate) * 100.0


def eps_surprise_pct(event: EarningsEvent) -> Optional[float]:
    """The published surprise when the source gave one, else computed.

    The preference is not a convenience: recomputing from a rounded estimate
    is wrong by up to half a cent of consensus, which on a $0.90 bar is a
    whole percentage point of "beat" (see EarningsEvent.eps_surprise_pct_reported).
    """
    if event.eps_surprise_pct_reported is not None:
        return event.eps_surprise_pct_reported
    return _surprise_pct(event.eps_actual, event.eps_estimate)


def revenue_surprise_pct(event: EarningsEvent) -> Optional[float]:
    return _surprise_pct(event.revenue_actual, event.revenue_estimate)


def _conservative_first(event: EarningsEvent) -> tuple[bool, float]:
    """Order rows for one print: smallest computable surprise first, rows
    with no computable surprise last."""
    surprise = eps_surprise_pct(event)
    return (surprise is None, surprise if surprise is not None else 0.0)


def _collapse_duplicates(events: list[EarningsEvent]) -> list[EarningsEvent]:
    """One earnings print is one event.

    The calendar can return several rows for the same (ticker, day) carrying
    different fiscal-quarter labels and materially different consensus
    figures — BJRI's 2026-07-30 print arrived as both +3.5% (estimate
    $0.9085) and +633.2% (estimate $0.1282). Because the screen ranked by
    surprise, the broken row always won; worse, the correct row then fell
    below the minimum-surprise threshold, so the only reading that survived
    was the wrong one.

    Keep the most conservative reading and record that the source disagreed
    with itself, so the tribunal is told rather than shown a clean beat.
    Conflicting data must not read as verified (invariant 6's spirit).
    """
    grouped: dict[tuple[str, date], list[EarningsEvent]] = {}
    for event in events:
        grouped.setdefault((event.ticker, event.day), []).append(event)

    collapsed: list[EarningsEvent] = []
    for group in grouped.values():
        actuals = tuple(sorted({round(e.eps_actual, 6) for e in group
                                if e.eps_actual is not None}))
        if len(group) == 1:
            collapsed.append(replace(group[0], all_eps_actuals=actuals))
            continue
        readings = {round(s, 6) for s in map(eps_surprise_pct, group)
                    if s is not None}
        collapsed.append(replace(min(group, key=_conservative_first),
                                 conflicting_estimates=len(readings) > 1,
                                 all_eps_actuals=actuals))
    return collapsed


def _fiscal_quarter_label(row: dict) -> Optional[str]:
    """`2026-Q2` from the calendar's own year/quarter fields, or None.

    Decided by the shared resolver rather than formatted here, so this stays
    one of the four bases in a single fixed order instead of a fourth private
    rule (EW移行 §2). None rather than a guess: there is no fallback left for
    the caller to reach for.
    """
    return resolve_fiscal_quarter(source_year=row.get("year"),
                                  source_quarter=row.get("quarter")).label or None


def parse_calendar(raw: list[dict]) -> list[EarningsEvent]:
    """Parse Finnhub earnings-calendar entries; skip malformed rows and
    collapse several rows describing the same print into one event."""
    events: list[EarningsEvent] = []
    for row in raw:
        symbol = (row.get("symbol") or "").strip().upper()
        if not symbol or "." in symbol:  # skip foreign/secondary listings
            continue
        try:
            day = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            continue
        events.append(EarningsEvent(
            ticker=symbol, day=day,
            eps_actual=row.get("epsActual"),
            eps_estimate=row.get("epsEstimate"),
            revenue_actual=row.get("revenueActual"),
            revenue_estimate=row.get("revenueEstimate"),
            fiscal_quarter=_fiscal_quarter_label(row)))
    return _collapse_duplicates(events)


_EPS_POINT_CAP = 50.0
_REVENUE_WEIGHT = 2.0
_REVENUE_POINT_CAP = 20.0


def eps_points(surprise: Optional[float]) -> float:
    """Ranking points an EPS surprise is worth, capped both ways.

    Symmetric by construction: a leg that missed subtracts what the same
    magnitude of beat would have added. Without that, a miss scores like
    missing data, and a name that beat hugely on EPS while missing on
    revenue outranks one that beat on both — which is the opposite of the
    reader's own definition of a good quarter.
    """
    if surprise is None:
        return 0.0
    return max(min(surprise, _EPS_POINT_CAP), -_EPS_POINT_CAP)


def revenue_points(surprise: Optional[float]) -> float:
    if surprise is None:
        return 0.0
    return max(min(surprise * _REVENUE_WEIGHT, _REVENUE_POINT_CAP),
               -_REVENUE_POINT_CAP)


def score_candidate(eps_surprise: Optional[float],
                    revenue_surprise: Optional[float],
                    gap_on_event_pct: Optional[float]) -> float:
    """Deterministic ranking score.

    Rewards surprise magnitude, and an event-day reaction that CONFIRMS the
    surprise without exhausting it (the classic drift setup): a modest
    positive gap beats both a negative reaction (market disagrees with the
    print — respect that) and a huge gap (repricing likely complete).

    `None` means unverified, and unverified earns nothing. Both surprise
    components are capped, so a number that is merely enormous cannot buy a
    ranking slot away from a genuine one.
    """
    score = eps_points(eps_surprise)
    if revenue_surprise is not None and revenue_surprise > 0:
        score += revenue_points(revenue_surprise)
    if gap_on_event_pct is not None:
        if 2.0 <= gap_on_event_pct <= 15.0:
            score += 15.0
        elif 0.0 <= gap_on_event_pct < 2.0:
            score += 5.0
        elif gap_on_event_pct < 0.0 or gap_on_event_pct > 25.0:
            score -= 10.0
    return round(score, 2)


def _eps_trusted(event: EarningsEvent, min_abs_estimate: float) -> bool:
    return (event.eps_estimate is not None
            and abs(event.eps_estimate) >= min_abs_estimate)


def _revenue_trusted(surprise: Optional[float], max_trusted_pct: float) -> bool:
    return surprise is not None and abs(surprise) <= max_trusted_pct


def screen_events(events: list[EarningsEvent],
                  min_eps_surprise_pct: float,
                  min_revenue_surprise_pct: float,
                  min_abs_eps_estimate: float,
                  max_trusted_revenue_surprise_pct: float
                  ) -> list[ScreenedEvent]:
    """Keep events with a reported EPS beat above threshold (and, when
    revenue data exists, a revenue print above its threshold), ranked by the
    capped score that decides which candidates are enriched.

    Ranking used to be by raw EPS surprise, which handed every enrichment
    slot to whichever names had the smallest consensus denominator — a run
    on 2026-08-01 was topped by +6958%, +5194% and +3459% readings, almost
    all of them REITs. Ranking by the capped score means an untrusted
    percentage sorts last instead of first; the candidate is still recorded,
    it just no longer displaces a genuine surprise.

    An event with no reported EPS actual/estimate is dropped — missing data
    cannot pass a screen.
    """
    kept: list[ScreenedEvent] = []
    for event in events:
        eps_s = eps_surprise_pct(event)
        if eps_s is None or eps_s < min_eps_surprise_pct:
            continue
        rev_s = revenue_surprise_pct(event)
        if rev_s is not None and rev_s < min_revenue_surprise_pct:
            continue
        kept.append(ScreenedEvent(
            event=event,
            eps_surprise_pct=eps_s,
            revenue_surprise_pct=rev_s,
            eps_surprise_trusted=_eps_trusted(event, min_abs_eps_estimate),
            revenue_surprise_trusted=_revenue_trusted(
                rev_s, max_trusted_revenue_surprise_pct)))
    kept.sort(key=lambda k: (-score_candidate(k.scored_eps_pct,
                                              k.scored_revenue_pct, None),
                             k.event.ticker))
    return kept
