"""Forward-looking consensus from Yahoo, via yfinance.

This is the module that makes pre-registration possible at all. After a
release there is no second source for consensus anywhere — Yahoo's earnings
history carries no revenue line, and EDGAR holds filings, not estimates — so
the only moment a distribution can be recorded is BEFORE the print.

Three fields decide the design:

1. **`numberOfAnalysts`.** Finnhub's tier gives a single point with no count
   and no range, so "at least three analysts" can only be checked here.
   INVH's EPS consensus was built from ONE analyst, and nothing in the
   Finnhub response says so.
2. **low / high.** A beat measured against a mean that spans a wide range is
   a weaker claim than the same beat against a tight one; the range is what
   lets the tribunal see the difference.
3. **`+1q`.** Guidance has no structured source at all, so the only
   mechanical yardstick for it is next quarter's consensus captured at the
   same moment (§5.3 decision 3).

yfinance scrapes a site that changes without notice, so every failure here
degrades to `None`. The caller then keeps one source and records that it had
only one — missing data is never a silent pass (invariant 6).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable, Optional

_THIS_QUARTER = "0q"
_NEXT_QUARTER = "+1q"


@dataclass(frozen=True)
class ConsensusReading:
    """One moment's consensus for a company, as Yahoo publishes it."""
    eps_avg: Optional[float] = None
    eps_low: Optional[float] = None
    eps_high: Optional[float] = None
    eps_analysts: Optional[int] = None
    revenue_avg: Optional[float] = None
    revenue_low: Optional[float] = None
    revenue_high: Optional[float] = None
    revenue_analysts: Optional[int] = None
    next_quarter_eps_avg: Optional[float] = None
    next_quarter_revenue_avg: Optional[float] = None

    @property
    def is_empty(self) -> bool:
        return self.eps_avg is None and self.revenue_avg is None


def shift_after_print(reading: ConsensusReading) -> ConsensusReading:
    """Re-label a reading taken AFTER the release it relates to.

    The period labels are relative to today, not to the last print. Measured
    on AMZN three days after its Q2 release: `0q` read 1.956 while the
    consensus that print was actually judged against was 1.83, and the row's
    own YoY growth field said +0.3% where Q2 had grown 242%. So once a
    quarter has reported, `0q` describes the quarter now in progress.

    That makes it useless as "what was expected of the quarter just
    reported" — and exactly right as the guidance yardstick, since the
    quarter now in progress is the one guidance covers. The reported
    quarter's own consensus is simply not in this response, so those fields
    come back empty rather than plausible-looking and wrong.
    """
    return ConsensusReading(
        next_quarter_eps_avg=reading.eps_avg,
        next_quarter_revenue_avg=reading.revenue_avg)


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out            # NaN is the only x != x


def _cell(frame, period: str, column: str) -> Optional[float]:
    """One cell of an estimates table, whether it is a pandas frame or a
    plain mapping. Any shape surprise reads as missing rather than raising —
    the frame layout is scraped, not contracted."""
    if frame is None:
        return None
    try:
        return _to_float(frame.loc[period][column])
    except Exception:                              # noqa: BLE001
        pass
    try:
        return _to_float(frame[period][column])
    except Exception:                              # noqa: BLE001
        return None


def _to_int(value: Optional[float]) -> Optional[int]:
    return None if value is None else int(value)


class YahooConsensusSource:
    """Per-ticker consensus lookup.

    `ticker_factory` exists so tests can inject a stub; the real one is
    `yfinance.Ticker`, imported lazily so importing this module never
    requires yfinance to be installed.
    """

    def __init__(self, ticker_factory: Optional[Callable] = None) -> None:
        self._ticker_factory = ticker_factory

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
            print("yfinance が未インストールのためコンセンサスの事前登録をスキップします",
                  file=sys.stderr)
            return None
        return yfinance.Ticker

    def consensus(self, ticker: str) -> Optional[ConsensusReading]:
        factory = self._factory()
        if factory is None:
            return None
        try:
            handle = factory(ticker)
            eps = handle.earnings_estimate
            revenue = handle.revenue_estimate
        except Exception as exc:                   # noqa: BLE001
            print(f"{ticker}: Yahoo コンセンサスの取得に失敗 ({exc})",
                  file=sys.stderr)
            return None
        reading = ConsensusReading(
            eps_avg=_cell(eps, _THIS_QUARTER, "avg"),
            eps_low=_cell(eps, _THIS_QUARTER, "low"),
            eps_high=_cell(eps, _THIS_QUARTER, "high"),
            eps_analysts=_to_int(_cell(eps, _THIS_QUARTER, "numberOfAnalysts")),
            revenue_avg=_cell(revenue, _THIS_QUARTER, "avg"),
            revenue_low=_cell(revenue, _THIS_QUARTER, "low"),
            revenue_high=_cell(revenue, _THIS_QUARTER, "high"),
            revenue_analysts=_to_int(
                _cell(revenue, _THIS_QUARTER, "numberOfAnalysts")),
            next_quarter_eps_avg=_cell(eps, _NEXT_QUARTER, "avg"),
            next_quarter_revenue_avg=_cell(revenue, _NEXT_QUARTER, "avg"))
        return None if reading.is_empty else reading
