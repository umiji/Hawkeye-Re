"""Provider interface for market data.

Free-tier constraint (see docs/DATA_SOURCES.md): Yahoo Finance (no key) for
prices, Finnhub (free key) for profile/news/earnings. Every provider degrades
gracefully — missing data surfaces as None and is flagged by the gates as
unverified rather than silently passing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Protocol, runtime_checkable

from hawkeye.contracts.models import AnalystTrend, InsiderActivity, NewsItem


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@runtime_checkable
class MarketDataProvider(Protocol):
    def daily_history(self, ticker: str, days: int = 365) -> list[Bar]:
        """Daily OHLCV bars, oldest first."""
        ...

    def profile(self, ticker: str) -> dict:
        """Best-effort: {name, sector, market_cap, next_earnings_date}."""
        ...

    def news(self, ticker: str, limit: int = 10,
             event_date: Optional[date] = None,
             lead_days: int = 3) -> list[NewsItem]:
        """`event_date` anchors the fetch window on the catalyst.

        Accepting it is OPTIONAL: Yahoo's news() takes only
        `(ticker, limit)` and is called that way. build_brief() probes the
        signature rather than assuming.
        """
        ...

    # insider_activity() and analyst_trend() are OPTIONAL, duck-typed
    # extensions — not every provider implements them (Yahoo doesn't).
    # build_brief() probes for them with getattr(); a provider without
    # them yields None fields, never an error.


@dataclass
class StaticProvider:
    """Offline/test provider fed with fixed data."""
    bars: list[Bar] = field(default_factory=list)
    profile_data: dict = field(default_factory=dict)
    news_items: list[NewsItem] = field(default_factory=list)
    insider: Optional[InsiderActivity] = None
    analyst: Optional[AnalystTrend] = None

    def daily_history(self, ticker: str, days: int = 365) -> list[Bar]:
        return self.bars[-days:]

    def profile(self, ticker: str) -> dict:
        return self.profile_data

    def news(self, ticker: str, limit: int = 10) -> list[NewsItem]:
        return self.news_items[:limit]

    def insider_activity(self, ticker: str) -> Optional[InsiderActivity]:
        return self.insider

    def analyst_trend(self, ticker: str) -> Optional[AnalystTrend]:
        return self.analyst
