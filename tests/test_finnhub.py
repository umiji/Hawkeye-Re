"""Finnhub insider-transactions / recommendation-trends parsing (offline —
monkeypatches the private _get() so no network call is made)."""
from datetime import date

from hawkeye.marketdata.finnhub import FinnhubProvider


def make_provider(responses: dict) -> FinnhubProvider:
    p = FinnhubProvider(api_key="test-key")
    p._get = lambda path, **params: responses[path]  # type: ignore[method-assign]
    return p


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
