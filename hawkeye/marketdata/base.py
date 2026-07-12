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

from hawkeye.contracts.models import NewsItem


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

    def news(self, ticker: str, limit: int = 10) -> list[NewsItem]:
        ...


@dataclass
class StaticProvider:
    """Offline/test provider fed with fixed data."""
    bars: list[Bar] = field(default_factory=list)
    profile_data: dict = field(default_factory=dict)
    news_items: list[NewsItem] = field(default_factory=list)

    def daily_history(self, ticker: str, days: int = 365) -> list[Bar]:
        return self.bars[-days:]

    def profile(self, ticker: str) -> dict:
        return self.profile_data

    def news(self, ticker: str, limit: int = 10) -> list[NewsItem]:
        return self.news_items[:limit]
