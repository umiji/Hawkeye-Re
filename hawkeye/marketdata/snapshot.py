"""Indicator computation and CandidateBrief assembly.

Pure functions over daily bars — no network, fully unit-testable. The
snapshot feeds the entry gates and the tribunal; the CLI can override any
field for offline/manual runs.
"""
from __future__ import annotations

import inspect
from datetime import date
from typing import Optional

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import (
    CandidateBrief,
    Catalyst,
    MarketSnapshot,
    NewsItem,
    SectorContext,
)
from hawkeye.marketdata.base import Bar, MarketDataProvider
from hawkeye.marketdata.sector_etf import etf_for_industry


def avg_dollar_volume(bars: list[Bar], n: int = 20) -> Optional[float]:
    window = bars[-n:]
    if not window:
        return None
    return sum(b.close * b.volume for b in window) / len(window)


def atr_pct(bars: list[Bar], n: int = 14) -> Optional[float]:
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-(n + 1):-1], bars[-n:]):
        tr = max(cur.high - cur.low,
                 abs(cur.high - prev.close),
                 abs(cur.low - prev.close))
        trs.append(tr)
    last_close = bars[-1].close
    if last_close <= 0:
        return None
    return (sum(trs) / len(trs)) / last_close * 100.0


def event_stats(bars: list[Bar], event_date: date
                ) -> tuple[Optional[float], Optional[float], Optional[int]]:
    """(gap_on_event_pct, change_since_event_pct, trading_days_since_event).

    The event-day move is measured close-to-close on the first trading day
    on or after ``event_date`` (after-hours announcements reprice next day).
    """
    idx = next((i for i, b in enumerate(bars) if b.day >= event_date), None)
    if idx is None or idx == 0:
        return None, None, None
    prev_close = bars[idx - 1].close
    event_close = bars[idx].close
    last_close = bars[-1].close
    gap = (event_close / prev_close - 1.0) * 100.0 if prev_close > 0 else None
    change = (last_close / event_close - 1.0) * 100.0 if event_close > 0 else None
    days_since = len(bars) - 1 - idx
    return gap, change, days_since


def build_snapshot(ticker: str, bars: list[Bar], profile: dict,
                   event_date: Optional[date] = None,
                   overrides: Optional[dict] = None) -> MarketSnapshot:
    if not bars:
        raise ValueError(f"no price history available for {ticker}")
    last = bars[-1]
    gap = change = None
    days_since = None
    if event_date is not None:
        gap, change, days_since = event_stats(bars, event_date)
    year = bars[-252:]
    snapshot = MarketSnapshot(
        ticker=ticker,
        price=last.close,
        prev_close=bars[-2].close if len(bars) >= 2 else None,
        market_cap=profile.get("market_cap"),
        avg_dollar_volume_20d=avg_dollar_volume(bars),
        atr_pct_14d=atr_pct(bars),
        gap_on_event_pct=gap,
        change_since_event_pct=change,
        days_since_event=days_since,
        next_earnings_date=profile.get("next_earnings_date"),
        high_52w=max(b.high for b in year),
        low_52w=min(b.low for b in year),
    )
    if overrides:
        snapshot = snapshot.model_copy(update={
            k: v for k, v in overrides.items() if v is not None})
    return snapshot


def _optional_call(provider: MarketDataProvider, method: str, ticker: str):
    """Duck-typed enrichment: call provider.<method>(ticker) if it exists.

    Not every provider implements insider_activity/analyst_trend (Yahoo
    doesn't). Missing capability or a failed call both yield None — the
    tribunal then sees the field absent, same as any other unverified data,
    never a silent zero.
    """
    fn = getattr(provider, method, None)
    if not callable(fn):
        return None
    try:
        return fn(ticker)
    except Exception:
        return None


def _fetch_news(provider: MarketDataProvider, ticker: str,
                event_date: Optional[date], limit: int,
                lead_days: int) -> list[NewsItem]:
    """Ask for news anchored on the catalyst, when the provider can do that.

    Not every provider can: Yahoo's news() is `(ticker, limit)` only, and
    passing the anchor to it would raise. Probing the signature (rather
    than catching TypeError) keeps a genuine TypeError raised *inside* the
    provider visible instead of being silently read as "old signature".
    """
    fn = provider.news
    try:
        accepts_anchor = "event_date" in inspect.signature(fn).parameters
    except (TypeError, ValueError):       # builtins/C callables have no signature
        accepts_anchor = False
    if not accepts_anchor:
        return fn(ticker, limit)
    return fn(ticker, limit, event_date=event_date, lead_days=lead_days)


def _excess(candidate: Optional[float], sector: Optional[float]
            ) -> Optional[float]:
    """Candidate move minus sector move — None unless BOTH sides were
    measured. Treating a missing side as 0 would report an unmeasured stock
    as having moved exactly with its sector."""
    if candidate is None or sector is None:
        return None
    return round(candidate - sector, 2)


def build_sector_context(raw_sector: str, snapshot: MarketSnapshot,
                         provider: MarketDataProvider,
                         event_date: Optional[date]) -> Optional[SectorContext]:
    """The candidate's sector move over the same window, via its SPDR ETF.

    Returns None — no comparison material, read downstream as unverified —
    when the industry label maps to no ETF, when there is no catalyst date
    to measure a window against, or when the ETF's history cannot be
    fetched. None of those may cost us the candidate itself, so the fetch
    failure is swallowed here rather than raised into build_brief().
    """
    if event_date is None:
        return None
    resolved = etf_for_industry(raw_sector)
    if resolved is None:
        return None
    sector, etf = resolved
    try:
        bars = provider.daily_history(etf)
    except Exception:
        return None
    if not bars:
        return None
    etf_gap, etf_change, _ = event_stats(bars, event_date)
    return SectorContext(
        sector=sector,
        raw_sector=raw_sector,
        etf_ticker=etf,
        etf_gap_on_event_pct=(round(etf_gap, 2) if etf_gap is not None else None),
        etf_change_since_event_pct=(round(etf_change, 2)
                                    if etf_change is not None else None),
        excess_gap_on_event_pct=_excess(snapshot.gap_on_event_pct, etf_gap),
        excess_change_since_event_pct=_excess(
            snapshot.change_since_event_pct, etf_change),
    )


def build_brief(ticker: str, catalyst: Catalyst, provider: MarketDataProvider,
                notes: str = "", overrides: Optional[dict] = None,
                config: Optional[HawkeyeConfig] = None) -> CandidateBrief:
    config = config or HawkeyeConfig()
    bars = provider.daily_history(ticker)
    profile = provider.profile(ticker)
    snapshot = build_snapshot(ticker, bars, profile,
                              event_date=catalyst.event_date, overrides=overrides)
    raw_sector = profile.get("sector", "")
    return CandidateBrief(
        ticker=ticker,
        company_name=profile.get("name", ""),
        sector=raw_sector,
        snapshot=snapshot,
        catalyst=catalyst,
        news=_fetch_news(provider, ticker, catalyst.event_date,
                         config.news_max_items, config.news_lead_days),
        insider_activity=_optional_call(provider, "insider_activity", ticker),
        analyst_trend=_optional_call(provider, "analyst_trend", ticker),
        sector_context=build_sector_context(raw_sector, snapshot, provider,
                                            catalyst.event_date),
        notes=notes,
    )
