"""Finnhub provider (free API key via FINNHUB_API_KEY).

Supplies what Yahoo's free endpoints cannot: company profile (market cap,
name, sector), company news, and the next earnings date.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

from hawkeye.contracts.models import AnalystTrend, InsiderActivity, NewsItem
from hawkeye.marketdata.base import Bar, CalendarUnavailable

_INSIDER_BUY_SELL_CODES = {"P", "S"}  # open-market purchase / sale only

_BASE = "https://finnhub.io/api/v1"

# Sorts undated items last without special-casing them at every comparison.
_UNDATED = datetime.min.replace(tzinfo=timezone.utc)
_FAR_AWAY = 10**6  # days; ranking distance for an item with no timestamp


def _parse_news(item: dict) -> NewsItem:
    published = None
    if item.get("datetime"):
        published = datetime.fromtimestamp(item["datetime"], tz=timezone.utc)
    return NewsItem(
        headline=item.get("headline", ""),
        source=item.get("source", "finnhub"),
        url=item.get("url", ""),
        published_at=published,
        summary=item.get("summary", ""))


def _rank_news(items: list[NewsItem], limit: int,
               event_date: Optional[date]) -> list[NewsItem]:
    """Keep `limit` items, then present them newest first.

    Which ones are kept is the point: with a catalyst date, the items
    nearest it win, so the earnings coverage survives a burst of newer
    headlines. Without one there is nothing to be near, so the newest win.
    """
    if event_date is not None:
        def distance(n: NewsItem) -> int:
            if n.published_at is None:
                return _FAR_AWAY
            return abs((n.published_at.date() - event_date).days)

        items = sorted(items, key=distance)[:limit]
    else:
        items = items[:limit]
    return sorted(items, key=lambda n: n.published_at or _UNDATED, reverse=True)


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

    def earnings_calendar(self, start: date, end: date) -> list[dict]:
        """Raw earnings-calendar entries (all symbols) for a date range.

        Raises CalendarUnavailable rather than returning [] when the feed
        cannot be read. An empty list is a factual claim — "no company
        reported in this window" — and the whole funnel acts on it; a
        request that never answered has made no claim at all.
        """
        if not self.available:
            raise CalendarUnavailable("FINNHUB_API_KEY が未設定です")
        try:
            cal = self._get("calendar/earnings",
                            **{"from": start.isoformat(), "to": end.isoformat()})
        except httpx.HTTPError as exc:
            raise CalendarUnavailable(
                f"決算カレンダーを取得できません ({start} 〜 {end}): "
                f"{type(exc).__name__}") from exc
        if not isinstance(cal, dict) or "earningsCalendar" not in cal:
            raise CalendarUnavailable(
                f"決算カレンダーの応答が想定の形式ではありません ({start} 〜 {end})")
        return cal.get("earningsCalendar") or []

    def earnings_history(self, ticker: str,
                         limit: int = 4) -> Optional[list[dict]]:
        """Past quarters' EPS actual and the calendar's point estimate.

        None when the call did not complete, `[]` when it answered with
        nothing. The two must not collapse: an unreachable ticker is worth
        retrying and a company with no history is not (invariant 6).

        Two limits of this endpoint, probed live on 2026-08-10 against AAPL
        and MSFT rather than taken from the vendor's docs:

        - It returns FOUR rows whatever `limit` says (tried 8, 20, and
          omitted). `limit` is still sent, so a smaller ask stays honest if
          the tier ever changes.
        - The rows carry `actual` and `estimate` only — no revenue. The
          earnings-calendar endpoint does carry revenue but answers a PAST
          window with zero rows on this key, so there is no second call that
          would complete the picture.
        """
        if not self.available:
            return None
        try:
            rows = self._get("stock/earnings", symbol=ticker, limit=limit)
        except httpx.HTTPError:
            return None
        return rows if isinstance(rows, list) else None

    def insider_activity(self, ticker: str,
                         window_days: int = 90) -> Optional[InsiderActivity]:
        """Net open-market insider buying/selling over the trailing window.

        Requires a Finnhub tier with insider-transactions access; returns
        None (never a silent zero) if the endpoint is unavailable so the
        tribunal sees "unverified", not "no insider activity".
        """
        if not self.available:
            return None
        today = date.today()
        try:
            data = self._get("stock/insider-transactions", symbol=ticker,
                             **{"from": (today - timedelta(days=window_days)).isoformat(),
                                "to": today.isoformat()})
        except httpx.HTTPError:
            return None
        rows = data.get("data", []) if isinstance(data, dict) else []
        if not rows:
            return None
        net_by_insider: dict[str, float] = {}
        for row in rows:
            code = row.get("transactionCode")
            change = row.get("change")
            name = row.get("name", "?")
            if code not in _INSIDER_BUY_SELL_CODES or change is None:
                continue
            net_by_insider[name] = net_by_insider.get(name, 0.0) + float(change)
        if not net_by_insider:
            return None
        return InsiderActivity(
            window_days=window_days,
            net_shares=sum(net_by_insider.values()),
            buyers=sum(1 for v in net_by_insider.values() if v > 0),
            sellers=sum(1 for v in net_by_insider.values() if v < 0))

    def analyst_trend(self, ticker: str) -> Optional[AnalystTrend]:
        """Latest analyst recommendation counts vs. the prior period.

        Requires a Finnhub tier with recommendation-trends access; returns
        None if unavailable.
        """
        if not self.available:
            return None
        try:
            rows = self._get("stock/recommendation", symbol=ticker)
        except httpx.HTTPError:
            return None
        if not isinstance(rows, list) or not rows:
            return None
        rows = sorted(rows, key=lambda r: r.get("period", ""), reverse=True)
        latest = rows[0]
        try:
            period = date.fromisoformat(latest["period"])
        except (KeyError, ValueError):
            return None
        prior = rows[1] if len(rows) > 1 else None
        prior_period = None
        if prior is not None:
            try:
                prior_period = date.fromisoformat(prior["period"])
            except (KeyError, ValueError):
                prior = None
        return AnalystTrend(
            period=period,
            strong_buy=int(latest.get("strongBuy", 0)),
            buy=int(latest.get("buy", 0)),
            hold=int(latest.get("hold", 0)),
            sell=int(latest.get("sell", 0)),
            strong_sell=int(latest.get("strongSell", 0)),
            prior_period=prior_period,
            prior_strong_buy=prior.get("strongBuy") if prior else None,
            prior_buy=prior.get("buy") if prior else None,
            prior_hold=prior.get("hold") if prior else None,
            prior_sell=prior.get("sell") if prior else None,
            prior_strong_sell=prior.get("strongSell") if prior else None)

    def news(self, ticker: str, limit: int = 10,
             event_date: Optional[date] = None,
             lead_days: int = 3) -> list[NewsItem]:
        """Company news, anchored on the catalyst when one is known.

        The window used to be a fixed `today - 14 days .. today`, which is
        unanchored to the reason the candidate exists. A candidate whose
        earnings landed near the freshness limit could have its earnings
        coverage pushed out of `limit` by newer, unrelated headlines — so
        the tribunal argued over a candidate without ever seeing the report
        it was supposed to be reacting to (docs/design/MASTER_OVERVIEW.ja.md
        §5.2(5)). With `event_date` the window starts just before the event
        and the items kept are the ones nearest to it.
        """
        if not self.available:
            return []
        today = date.today()
        if event_date is not None:
            start, end = event_date - timedelta(days=lead_days), max(today, event_date)
        else:
            start, end = today - timedelta(days=14), today
        try:
            items = self._get("company-news", symbol=ticker,
                              **{"from": start.isoformat(), "to": end.isoformat()})
        except httpx.HTTPError:
            return []
        return _rank_news([_parse_news(item) for item in items], limit, event_date)


class CompositeProvider:
    """Yahoo for prices; Finnhub (when a key exists) for profile/news."""

    def __init__(self, yahoo, finnhub: Optional[FinnhubProvider] = None):
        self._yahoo = yahoo
        self._finnhub = finnhub or FinnhubProvider()

    def daily_history(self, ticker: str, days: int = 365) -> list[Bar]:
        return self._yahoo.daily_history(ticker, days)

    def profile(self, ticker: str) -> dict:
        return self._finnhub.profile(ticker) if self._finnhub.available else {}

    def news(self, ticker: str, limit: int = 10,
             event_date: Optional[date] = None,
             lead_days: int = 3) -> list[NewsItem]:
        items = (self._finnhub.news(ticker, limit, event_date=event_date,
                                    lead_days=lead_days)
                 if self._finnhub.available else [])
        # Yahoo has no window/anchor controls — it is a last resort, not an
        # equivalent source.
        return items or self._yahoo.news(ticker, limit)

    def insider_activity(self, ticker: str) -> Optional[InsiderActivity]:
        return (self._finnhub.insider_activity(ticker)
                if self._finnhub.available else None)

    def analyst_trend(self, ticker: str) -> Optional[AnalystTrend]:
        return (self._finnhub.analyst_trend(ticker)
                if self._finnhub.available else None)
