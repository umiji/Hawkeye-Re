from datetime import date, timedelta

from hawkeye.contracts.models import AnalystTrend, Catalyst, CatalystType, InsiderActivity
from hawkeye.marketdata.base import Bar, StaticProvider
from hawkeye.marketdata.snapshot import (
    atr_pct,
    avg_dollar_volume,
    build_brief,
    build_snapshot,
    event_stats,
)
from tests.conftest import make_bars


def test_avg_dollar_volume():
    bars = [Bar(day=date(2026, 1, 1), open=10, high=10, low=10,
                close=10.0, volume=1000)] * 20
    assert avg_dollar_volume(bars) == 10_000.0
    assert avg_dollar_volume([]) is None


def test_atr_pct_flat_series_is_small():
    bars = make_bars(60, daily_move=0.0)
    val = atr_pct(bars)
    assert val is not None and 0 < val < 3.0


def test_atr_needs_enough_bars():
    assert atr_pct(make_bars(10)) is None


def test_event_stats_gap_and_days():
    bars = make_bars(50, daily_move=0.0, start_price=100.0)
    event_day = bars[-4].day
    # inject a +10% gap on the event day and keep it afterwards
    boosted = []
    for bar in bars:
        if bar.day >= event_day:
            boosted.append(Bar(day=bar.day, open=bar.open, high=bar.high * 1.1,
                               low=bar.low * 1.1, close=bar.close * 1.1,
                               volume=bar.volume))
        else:
            boosted.append(bar)
    gap, change, days_since = event_stats(boosted, event_day)
    assert gap is not None and 9.5 < gap < 10.5
    assert change is not None and abs(change) < 0.5
    assert days_since == 3


def test_build_snapshot_with_overrides():
    bars = make_bars(300)
    snap = build_snapshot("TEST", bars, {"market_cap": 1e9},
                          overrides={"price": 123.0, "market_cap": None})
    assert snap.price == 123.0          # override wins
    assert snap.market_cap == 1e9       # None override ignored
    assert snap.avg_dollar_volume_20d is not None
    assert snap.high_52w is not None


def test_build_snapshot_carries_structured_surprise_overrides():
    bars = make_bars(300)
    snap = build_snapshot("TEST", bars, {},
                          overrides={"eps_surprise_pct": 22.5,
                                    "revenue_surprise_pct": None})
    assert snap.eps_surprise_pct == 22.5
    assert snap.revenue_surprise_pct is None   # None override ignored, stays None


def test_build_brief_enriches_insider_and_analyst_when_provider_supports_it():
    bars = make_bars(300)
    insider = InsiderActivity(window_days=90, net_shares=-5000, buyers=1, sellers=3)
    analyst = AnalystTrend(period=date(2026, 7, 1), strong_buy=5, buy=10,
                           hold=3, sell=1, strong_sell=0)
    provider = StaticProvider(bars=bars, insider=insider, analyst=analyst)
    catalyst = Catalyst(type=CatalystType.EARNINGS_BEAT, description="beat",
                        event_date=date.today() - timedelta(days=3))
    brief = build_brief("TEST", catalyst, provider)
    assert brief.insider_activity == insider
    assert brief.analyst_trend == analyst


def test_build_brief_omits_enrichment_when_provider_lacks_it():
    """Yahoo-only providers don't implement insider_activity/analyst_trend —
    the fields must come back None, never raise."""
    bars = make_bars(300)

    class BareProvider:
        def daily_history(self, ticker, days=365):
            return bars

        def profile(self, ticker):
            return {}

        def news(self, ticker, limit=10):
            return []

    catalyst = Catalyst(type=CatalystType.EARNINGS_BEAT, description="beat",
                        event_date=date.today() - timedelta(days=3))
    brief = build_brief("TEST", catalyst, BareProvider())
    assert brief.insider_activity is None
    assert brief.analyst_trend is None


def test_build_brief_survives_enrichment_call_raising():
    bars = make_bars(300)

    class FlakyProvider(StaticProvider):
        def insider_activity(self, ticker):
            raise RuntimeError("finnhub 403")

    catalyst = Catalyst(type=CatalystType.EARNINGS_BEAT, description="beat",
                        event_date=date.today() - timedelta(days=3))
    brief = build_brief("TEST", catalyst, FlakyProvider(bars=bars))
    assert brief.insider_activity is None  # degraded, not raised


# --- news window plumbing (§5.2(5)) -----------------------------------------

def test_build_brief_anchors_the_news_window_on_the_catalyst_date():
    """The catalyst date has to reach the provider — otherwise the news
    window is anchored on 'today' and can miss the earnings coverage that
    is the whole reason the candidate exists."""
    bars = make_bars(300)
    event_day = date.today() - timedelta(days=8)
    seen: dict = {}

    class RecordingProvider(StaticProvider):
        def news(self, ticker, limit=10, event_date=None, lead_days=3):
            seen["event_date"] = event_date
            seen["limit"] = limit
            return []

    catalyst = Catalyst(type=CatalystType.EARNINGS_BEAT, description="beat",
                        event_date=event_day)
    build_brief("TEST", catalyst, RecordingProvider(bars=bars))
    assert seen["event_date"] == event_day
    assert seen["limit"] > 10   # raised from the old default so the window
                                # cap isn't what drops the earnings coverage


def test_build_brief_still_works_with_a_provider_that_takes_no_event_date():
    """Yahoo's news() signature is (ticker, limit) only. Passing the new
    argument to it would raise, so build_brief must probe first."""
    bars = make_bars(300)
    calls: list = []

    class LegacyNewsProvider(StaticProvider):
        def news(self, ticker, limit=10):
            calls.append(limit)
            return []

    catalyst = Catalyst(type=CatalystType.EARNINGS_BEAT, description="beat",
                        event_date=date.today() - timedelta(days=3))
    brief = build_brief("TEST", catalyst, LegacyNewsProvider(bars=bars))
    assert brief.news == []
    assert len(calls) == 1
