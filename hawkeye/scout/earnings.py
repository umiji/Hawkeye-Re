"""Earnings-surprise screening (pure functions, no network).

The scout's first filter is mechanical: EPS/revenue surprise magnitudes from
the earnings calendar. No human picks the universe — that is the point.
Manual candidate entry remains possible via `hawkeye evaluate`, but scouted
and manual candidates are distinguishable in the ledger, so the experiment
can always separate "system-sourced" from "human-sourced" performance.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class EarningsEvent:
    ticker: str
    day: date
    eps_actual: Optional[float]
    eps_estimate: Optional[float]
    revenue_actual: Optional[float]
    revenue_estimate: Optional[float]


def parse_calendar(raw: list[dict]) -> list[EarningsEvent]:
    """Parse Finnhub earnings-calendar entries; skip malformed rows."""
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
            revenue_estimate=row.get("revenueEstimate")))
    return events


def _surprise_pct(actual: Optional[float],
                  estimate: Optional[float]) -> Optional[float]:
    if actual is None or estimate is None or estimate == 0:
        return None
    return (actual - estimate) / abs(estimate) * 100.0


def eps_surprise_pct(event: EarningsEvent) -> Optional[float]:
    return _surprise_pct(event.eps_actual, event.eps_estimate)


def revenue_surprise_pct(event: EarningsEvent) -> Optional[float]:
    return _surprise_pct(event.revenue_actual, event.revenue_estimate)


def screen_events(events: list[EarningsEvent],
                  min_eps_surprise_pct: float,
                  min_revenue_surprise_pct: float
                  ) -> list[tuple[EarningsEvent, float, Optional[float]]]:
    """Keep events with a reported EPS beat above threshold (and, when
    revenue data exists, a revenue print above its threshold). Returns
    (event, eps_surprise, revenue_surprise) sorted by EPS surprise desc.

    An event with no reported EPS actual/estimate is dropped — missing data
    cannot pass a screen.
    """
    kept = []
    for event in events:
        eps_s = eps_surprise_pct(event)
        if eps_s is None or eps_s < min_eps_surprise_pct:
            continue
        rev_s = revenue_surprise_pct(event)
        if rev_s is not None and rev_s < min_revenue_surprise_pct:
            continue
        kept.append((event, eps_s, rev_s))
    kept.sort(key=lambda t: -t[1])
    return kept
