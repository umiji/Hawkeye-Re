from datetime import date, timedelta

from hawkeye.marketdata.base import Bar, StaticProvider
from hawkeye.marketdata.snapshot import (
    atr_pct,
    avg_dollar_volume,
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
