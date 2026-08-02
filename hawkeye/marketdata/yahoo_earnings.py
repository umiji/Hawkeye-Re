"""Reported EPS and its consensus, from Yahoo via yfinance.

The source split (2026-08-02). Finnhub's earnings calendar answers "who
reported on day D" — it is the only free source that can, and that query IS
the discovery mechanism that keeps human taste out of the universe. It does
not reliably answer "what did they report": measured against the companies'
own releases on 2026-08-01 it stamped the PRIOR quarter's EPS onto AAPL's
current row, and returned two contradictory consensus figures for one BJRI
print. yfinance matched the primary source on 3/3 names checked.

So discovery stays with the calendar and the EPS numbers move here. Only
EPS moves: `get_earnings_dates()` carries no revenue, so revenue surprise
still comes from the calendar under its existing accounting-basis trust
band (`scout_max_trusted_revenue_surprise_pct`).

Two properties of this data decide the whole module:

1. **The published Surprise(%) must be used, never recomputed.** Yahoo
   rounds the estimate it *displays* to two places but computes the surprise
   from full precision. BJRI's 2026-07-30 print displays "estimate 0.90,
   reported 0.94" yet reports +4.95%, not the +4.44% those two numbers give
   — the true estimate is ~0.8957. Recomputing would silently understate
   every beat.
2. **A print with no reported EPS is a scheduled future date**, not a
   failure. Those rows carry an estimate and NaN actual and are skipped.

yfinance is an unofficial scraper of a site that can change without notice,
so every failure here degrades to `None` — the caller then keeps the
calendar's own number, flagged as unverified. Missing data must never read
as a pass (invariant 6).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional


@dataclass(frozen=True)
class VerifiedEarnings:
    """One past earnings print as Yahoo reports it."""
    ticker: str
    report_date: date
    eps_actual: float
    eps_estimate: float
    surprise_pct: float          # as published — see module docstring, rule 1


def _to_float(value) -> Optional[float]:
    """None for NaN/missing. `float(nan)` succeeds, so NaN needs its own
    check — a scheduled-but-unreported print is exactly a NaN actual."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out          # NaN is the only x != x


class YahooEarningsSource:
    """Per-ticker lookup of a past print's EPS actual, estimate and surprise.

    `ticker_factory` exists so tests can inject a stub: the real one is
    `yfinance.Ticker`, which is imported lazily so that importing this module
    never requires yfinance to be installed.
    """

    def __init__(self, ticker_factory: Optional[Callable] = None,
                 lookback_prints: int = 8,
                 max_date_drift_days: int = 1) -> None:
        self._ticker_factory = ticker_factory
        self._lookback_prints = lookback_prints
        # Yahoo timestamps the print in exchange time (16:00 ET for an
        # after-close release) while the calendar carries a plain date, and
        # the two disagree by a day around pre-market releases and timezone
        # boundaries. One day of tolerance; more would risk matching the
        # neighbouring quarter for a company that reports near a month end.
        self._max_date_drift_days = max_date_drift_days

    @property
    def available(self) -> bool:
        if self._ticker_factory is not None:
            return True
        try:
            import yfinance  # noqa: F401
        except ImportError:
            return False
        return True

    def _factory(self) -> Optional[Callable]:
        if self._ticker_factory is not None:
            return self._ticker_factory
        try:
            import yfinance
        except ImportError:
            print("yfinance が未インストールのため決算数値の検証をスキップします",
                  file=sys.stderr)
            return None
        return yfinance.Ticker

    def _rows(self, ticker: str) -> list[tuple[date, dict]]:
        """[(print date, {column: value})] — empty on any failure.

        `get_earnings_dates()` needs lxml and raises ImportError without it;
        Yahoo also returns nothing at all for some symbols. Both are reported
        to stderr rather than swallowed: a silent empty result here looks
        identical to "the calendar was right", which is the one conclusion it
        must not produce.
        """
        factory = self._factory()
        if factory is None:
            return []
        try:
            frame = factory(ticker).get_earnings_dates(
                limit=self._lookback_prints)
        except Exception as exc:                        # noqa: BLE001
            print(f"{ticker}: Yahoo 決算履歴の取得に失敗 ({exc})", file=sys.stderr)
            return []
        if frame is None or getattr(frame, "empty", True):
            return []
        out: list[tuple[date, dict]] = []
        for stamp, row in frame.iterrows():
            day = getattr(stamp, "date", None)
            if not callable(day):
                continue
            out.append((day(), dict(row)))

        return out

    def verified_earnings(self, ticker: str,
                          day: date) -> Optional[VerifiedEarnings]:
        """The print Yahoo has on (or within a day of) `day`, or None.

        None means "not verified" in every case — no such print, no reported
        actual yet, a scraper failure, or yfinance missing. The caller must
        treat all of them the same way: keep the calendar's number and mark
        it unverified.
        """
        best: Optional[VerifiedEarnings] = None
        best_drift = self._max_date_drift_days + 1
        for report_day, row in self._rows(ticker):
            drift = abs((report_day - day).days)
            if drift > self._max_date_drift_days or drift >= best_drift:
                continue
            actual = _to_float(row.get("Reported EPS"))
            estimate = _to_float(row.get("EPS Estimate"))
            surprise = _to_float(row.get("Surprise(%)"))
            if actual is None or estimate is None or surprise is None:
                continue        # a scheduled, not-yet-reported date
            best = VerifiedEarnings(
                ticker=ticker, report_date=report_day, eps_actual=actual,
                eps_estimate=estimate, surprise_pct=surprise)
            best_drift = drift
        return best
