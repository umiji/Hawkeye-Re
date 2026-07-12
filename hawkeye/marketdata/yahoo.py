"""Yahoo Finance provider (no API key required).

Uses the public chart endpoint for OHLCV and the search endpoint for news.
Yahoo does not expose market cap or earnings dates without authenticated
endpoints, so ``profile`` here is intentionally thin — the composite
provider fills those from Finnhub when a key is available.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import httpx

from hawkeye.contracts.models import NewsItem
from hawkeye.marketdata.base import Bar

_BASE = "https://query1.finance.yahoo.com"
_HEADERS = {"User-Agent": "Mozilla/5.0 (Hawkeye research client)"}


class YahooProvider:
    def __init__(self, timeout: float = 15.0):
        self._client = httpx.Client(headers=_HEADERS, timeout=timeout,
                                    follow_redirects=True)

    def daily_history(self, ticker: str, days: int = 365) -> list[Bar]:
        rng = "2y" if days > 365 else "1y" if days > 180 else "6mo"
        resp = self._client.get(
            f"{_BASE}/v8/finance/chart/{ticker}",
            params={"range": rng, "interval": "1d", "events": "div,splits"})
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        quote = result["indicators"]["quote"][0]
        bars: list[Bar] = []
        for i, ts in enumerate(timestamps):
            o, h, l, c, v = (quote[k][i] for k in ("open", "high", "low",
                                                   "close", "volume"))
            if None in (o, h, l, c):
                continue
            bars.append(Bar(
                day=datetime.fromtimestamp(ts, tz=timezone.utc).date(),
                open=o, high=h, low=l, close=c, volume=v or 0.0))
        return bars[-days:]

    def profile(self, ticker: str) -> dict:
        return {}

    def news(self, ticker: str, limit: int = 10) -> list[NewsItem]:
        try:
            resp = self._client.get(
                f"{_BASE}/v1/finance/search",
                params={"q": ticker, "newsCount": limit, "quotesCount": 0})
            resp.raise_for_status()
            items = resp.json().get("news", [])
        except (httpx.HTTPError, KeyError, ValueError):
            return []
        out: list[NewsItem] = []
        for item in items[:limit]:
            published = None
            if item.get("providerPublishTime"):
                published = datetime.fromtimestamp(
                    item["providerPublishTime"], tz=timezone.utc)
            out.append(NewsItem(
                headline=item.get("title", ""),
                source=item.get("publisher", "yahoo"),
                url=item.get("link", ""),
                published_at=published))
        return out
