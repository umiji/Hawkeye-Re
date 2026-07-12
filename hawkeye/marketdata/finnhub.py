"""Finnhub provider (free API key via FINNHUB_API_KEY).

Supplies what Yahoo's free endpoints cannot: company profile (market cap,
name, sector), company news, and the next earnings date.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from hawkeye.contracts.models import NewsItem
from hawkeye.marketdata.base import Bar

_BASE = "https://finnhub.io/api/v1"


class FinnhubProvider:
    def __init__(self, api_key: Optional[str] = None, timeout: float = 15.0):
        self.api_key = api_key or os.environ.get("FINNHUB_API_KEY", "")
        self._client = httpx.Client(timeout=timeout)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, **params) -> dict | list:
        params["token"] = self.api_key
        resp = self._client.get(f"{_BASE}/{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def daily_history(self, ticker: str, days: int = 365) -> list[Bar]:
        # Candles are not on the current free tier; Yahoo covers history.
        return []

    def profile(self, ticker: str) -> dict:
        if not self.available:
            return {}
        out: dict = {}
        try:
            p = self._get("stock/profile2", symbol=ticker)
            if p:
                out["name"] = p.get("name", "")
                out["sector"] = p.get("finnhubIndustry", "")
                mcap = p.get("marketCapitalization")
                if mcap:
                    out["market_cap"] = float(mcap) * 1e6  # reported in millions
        except httpx.HTTPError:
            pass
        try:
            today = date.today()
            cal = self._get("calendar/earnings",
                            symbol=ticker,
                            **{"from": today.isoformat(),
                               "to": (today + timedelta(days=120)).isoformat()})
            entries = cal.get("earningsCalendar", []) if isinstance(cal, dict) else []
            dates = sorted(e["date"] for e in entries if e.get("date"))
            if dates:
                out["next_earnings_date"] = date.fromisoformat(dates[0])
        except (httpx.HTTPError, ValueError):
            pass
        return out

    def news(self, ticker: str, limit: int = 10) -> list[NewsItem]:
        if not self.available:
            return []
        try:
            today = date.today()
            items = self._get("company-news", symbol=ticker,
                              **{"from": (today - timedelta(days=14)).isoformat(),
                                 "to": today.isoformat()})
        except httpx.HTTPError:
            return []
        out: list[NewsItem] = []
        for item in items[:limit]:
            published = None
            if item.get("datetime"):
                published = datetime.fromtimestamp(item["datetime"], tz=timezone.utc)
            out.append(NewsItem(
                headline=item.get("headline", ""),
                source=item.get("source", "finnhub"),
                url=item.get("url", ""),
                published_at=published,
                summary=item.get("summary", "")))
        return out


class CompositeProvider:
    """Yahoo for prices; Finnhub (when a key exists) for profile/news."""

    def __init__(self, yahoo, finnhub: Optional[FinnhubProvider] = None):
        self._yahoo = yahoo
        self._finnhub = finnhub or FinnhubProvider()

    def daily_history(self, ticker: str, days: int = 365) -> list[Bar]:
        return self._yahoo.daily_history(ticker, days)

    def profile(self, ticker: str) -> dict:
        return self._finnhub.profile(ticker) if self._finnhub.available else {}

    def news(self, ticker: str, limit: int = 10) -> list[NewsItem]:
        items = self._finnhub.news(ticker, limit) if self._finnhub.available else []
        return items or self._yahoo.news(ticker, limit)
