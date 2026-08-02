"""Finnhub insider-transactions / recommendation-trends parsing (offline —
monkeypatches the private _get() so no network call is made)."""
from datetime import date, datetime, timedelta, timezone

from hawkeye.marketdata.finnhub import FinnhubProvider


def make_provider(responses: dict) -> FinnhubProvider:
    p = FinnhubProvider(api_key="test-key")
    p._get = lambda path, **params: responses[path]  # type: ignore[method-assign]
    return p


def make_recording_provider(items: list) -> tuple[FinnhubProvider, dict]:
    """Provider that records the query params it was called with."""
    captured: dict = {}
    p = FinnhubProvider(api_key="test-key")

    def fake_get(path, **params):
        captured.update(params)
        return items

    p._get = fake_get  # type: ignore[method-assign]
    return p, captured


def news_row(day: date, headline: str) -> dict:
    ts = datetime(day.year, day.month, day.day, 13, 0, tzinfo=timezone.utc)
    return {"headline": headline, "source": "wire", "url": "http://x",
            "datetime": int(ts.timestamp()), "summary": ""}


def test_insider_activity_nets_purchases_and_sales_only():
    p = make_provider({"stock/insider-transactions": {"data": [
        {"name": "Alice", "transactionCode": "P", "change": 1000},
        {"name": "Alice", "transactionCode": "P", "change": 500},
        {"name": "Bob", "transactionCode": "S", "change": -2000},
        {"name": "Carol", "transactionCode": "S", "change": -300},
        {"name": "Dave", "transactionCode": "A", "change": 9999},  # grant: ignored
        {"name": "Eve", "transactionCode": "M", "change": 5000},   # option ex: ignored
    ]}})
    activity = p.insider_activity("TEST")
    assert activity is not None
    assert activity.buyers == 1 and activity.sellers == 2
    assert activity.net_shares == 1500 - 2000 - 300


def test_insider_activity_none_when_no_relevant_rows():
    p = make_provider({"stock/insider-transactions": {"data": [
        {"name": "Dave", "transactionCode": "A", "change": 100},
    ]}})
    assert p.insider_activity("TEST") is None


def test_insider_activity_none_when_endpoint_empty():
    p = make_provider({"stock/insider-transactions": {"data": []}})
    assert p.insider_activity("TEST") is None


def test_insider_activity_none_when_unavailable():
    p = FinnhubProvider(api_key="")
    assert p.insider_activity("TEST") is None


def test_analyst_trend_latest_vs_prior():
    p = make_provider({"stock/recommendation": [
        {"period": "2026-07-01", "strongBuy": 5, "buy": 10, "hold": 3,
         "sell": 1, "strongSell": 0},
        {"period": "2026-06-01", "strongBuy": 3, "buy": 8, "hold": 5,
         "sell": 2, "strongSell": 1},
    ]})
    trend = p.analyst_trend("TEST")
    assert trend is not None
    assert trend.period == date(2026, 7, 1)
    assert trend.strong_buy == 5
    assert trend.prior_period == date(2026, 6, 1)
    assert trend.prior_strong_buy == 3


def test_analyst_trend_none_when_empty():
    p = make_provider({"stock/recommendation": []})
    assert p.analyst_trend("TEST") is None


def test_analyst_trend_handles_single_period():
    p = make_provider({"stock/recommendation": [
        {"period": "2026-07-01", "strongBuy": 5, "buy": 10, "hold": 3,
         "sell": 1, "strongSell": 0},
    ]})
    trend = p.analyst_trend("TEST")
    assert trend is not None and trend.prior_period is None


# --- news window (§5.2(5)) --------------------------------------------------
# The fetch window used to be a fixed "today - 14 days .. today", which is
# unanchored to the catalyst: a candidate whose earnings landed 10 days ago
# (still inside the freshness gate) could have its earnings coverage pushed
# out of the window's item cap by unrelated newer headlines.

def test_news_window_starts_before_the_event_when_an_event_date_is_given():
    p, captured = make_recording_provider([])
    p.news("TEST", event_date=date(2026, 7, 10), lead_days=3)
    assert captured["from"] == "2026-07-07"


def test_news_window_falls_back_to_recent_days_without_an_event_date():
    p, captured = make_recording_provider([])
    p.news("TEST")
    today = date.today()
    assert captured["to"] == today.isoformat()
    assert captured["from"] == (today - timedelta(days=14)).isoformat()


def test_news_keeps_event_coverage_when_more_items_than_the_limit():
    event_day = date(2026, 7, 10)
    # Finnhub returns newest first: 8 unrelated later headlines bury the
    # earnings coverage, which sits at the very end of the response.
    items = [news_row(event_day + timedelta(days=i), f"noise {i}")
             for i in range(8, 0, -1)]
    items.append(news_row(event_day, "Q2 earnings beat"))
    p, _ = make_recording_provider(items)

    out = p.news("TEST", limit=3, event_date=event_day)

    assert len(out) == 3
    assert "Q2 earnings beat" in [n.headline for n in out]


def test_news_returns_newest_first():
    event_day = date(2026, 7, 10)
    items = [news_row(event_day + timedelta(days=i), f"day+{i}")
             for i in (0, 2, 1)]
    p, _ = make_recording_provider(items)

    out = p.news("TEST", limit=3, event_date=event_day)

    assert [n.headline for n in out] == ["day+2", "day+1", "day+0"]


def test_news_without_event_date_keeps_the_newest_items():
    today = date.today()
    items = [news_row(today - timedelta(days=i), f"d{i}") for i in range(5)]
    p, _ = make_recording_provider(items)

    out = p.news("TEST", limit=2)

    assert [n.headline for n in out] == ["d0", "d1"]
