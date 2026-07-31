"""Drop-candidate review — docs/MASTER_OVERVIEW.ja.md §5.2(3).

Fully offline: every price series is synthetic and the provider is a stub.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from hawkeye.marketdata.base import Bar
from hawkeye.scout.drop_review import (
    BETA_WINDOW_TRADING_DAYS,
    CHECKPOINT_TRADING_DAYS,
    CheckpointResult,
    TrackedCandidate,
    attribute_by_cohort,
    attribute_by_gate,
    atr_pct_at,
    collect_checkpoints,
    market_beta,
    outliers,
    with_peer_baseline,
    z_score,
)


# --- helpers ---------------------------------------------------------------

def _business_days_ending(end: date, n: int) -> list[date]:
    days: list[date] = []
    day = end
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def bars_from_returns(returns: list[float], end: date,
                      start_price: float = 100.0) -> list[Bar]:
    """Oldest-first bars whose day-over-day closes reproduce `returns`.

    len(bars) == len(returns) + 1: the first bar is the base close the first
    return is measured from. high/low are pinned to the close so the true
    range of each bar is exactly the day's move, which keeps ATR analytic.
    """
    closes = [start_price]
    for r in returns:
        closes.append(closes[-1] * (1.0 + r))
    days = _business_days_ending(end, len(closes))
    return [Bar(day=d, open=c, high=c, low=c, close=c, volume=1_000_000.0)
            for d, c in zip(days, closes)]


class StubProvider:
    """daily_history() from a canned per-ticker series; raises for tickers
    listed in `failing` (delisting / ticker change / API outage)."""

    def __init__(self, series: dict[str, list[Bar]],
                 failing: frozenset[str] = frozenset()):
        self._series = series
        self._failing = failing

    def daily_history(self, ticker: str, days: int = 365) -> list[Bar]:
        if ticker in self._failing:
            raise RuntimeError(f"no price history for {ticker}")
        return self._series.get(ticker, [])


def tracked(ticker: str, cohort: str, decision_date: date,
            **kw) -> TrackedCandidate:
    return TrackedCandidate(
        ticker=ticker, cohort=cohort, scan_id=kw.pop("scan_id", 1),
        decision_date=decision_date,
        reject_reason=kw.pop("reject_reason", ""),
        failed_gates=kw.pop("failed_gates", ()), score=kw.pop("score", 0.0))


# 300 alternating days: enough overlap to clear BETA_MIN_DAYS, and the
# alternation keeps the index variance non-zero so beta is defined.
ALT = [0.01, -0.01] * 150
TODAY = date(2026, 7, 31)
DECISION_DAY = date(2026, 4, 1)


# --- fixed design constants ------------------------------------------------

def test_checkpoints_are_t5_and_t10_trading_days_only():
    """§5.2(3): 'T+5営業日 → T+10営業日で確定、以降は再チェックしない'. A
    horizon that can be re-picked after seeing the data is a horizon picked
    to flatter the data."""
    assert CHECKPOINT_TRADING_DAYS == {"t5": 5, "t10": 10}
    assert BETA_WINDOW_TRADING_DAYS == 250


# --- market beta -----------------------------------------------------------

def test_beta_is_one_when_the_stock_tracks_the_index_exactly():
    index = bars_from_returns(ALT, TODAY)
    stock = bars_from_returns(ALT, TODAY)
    assert market_beta(stock, index, asof=TODAY) == pytest.approx(1.0)


def test_beta_is_two_when_the_stock_moves_twice_the_index():
    index = bars_from_returns(ALT, TODAY)
    stock = bars_from_returns([r * 2 for r in ALT], TODAY)
    assert market_beta(stock, index, asof=TODAY) == pytest.approx(2.0)


def test_beta_is_none_without_enough_overlapping_days():
    index = bars_from_returns(ALT[:10], TODAY)
    stock = bars_from_returns(ALT[:10], TODAY)
    assert market_beta(stock, index, asof=TODAY) is None


def test_beta_uses_only_bars_up_to_the_decision_day():
    """Estimating beta from returns that postdate the decision would leak the
    very window being measured into the yardstick measuring it."""
    index = bars_from_returns(ALT, TODAY)
    pre = list(ALT[:200])
    post = [r * 10 for r in ALT[200:]]
    stock = bars_from_returns(pre + post, TODAY)
    asof = stock[200].day
    assert market_beta(stock, index, asof=asof) == pytest.approx(1.0)


# --- ATR and z -------------------------------------------------------------

def test_atr_pct_is_the_typical_daily_move_as_a_share_of_price():
    bars = bars_from_returns(ALT, TODAY)
    assert atr_pct_at(bars, TODAY) == pytest.approx(1.0, abs=0.05)


def test_atr_pct_is_none_before_a_full_period_of_history():
    bars = bars_from_returns(ALT[:5], TODAY)
    assert atr_pct_at(bars, TODAY) is None


def test_z_normalizes_alpha_by_the_stocks_own_move_over_the_span():
    """Without this, a volatile small cap and a mega cap moving the same %
    look like the same size of miss, and the volatile names would fill every
    review queue purely by being volatile."""
    # 3% alpha on a 1%/day name over 5 days -> 3 / (1 * sqrt(5)) = 1.34
    assert z_score(3.0, 1.0, 5) == pytest.approx(1.3416, abs=1e-3)
    # Same alpha on a 3%/day name is only a third as remarkable.
    assert z_score(3.0, 3.0, 5) == pytest.approx(0.4472, abs=1e-3)


def test_z_is_none_when_volatility_is_unusable():
    assert z_score(3.0, 0.0, 5) is None


# --- checkpoint collection -------------------------------------------------

def test_candidate_whose_checkpoint_has_not_elapsed_is_pending_not_censored():
    provider = StubProvider({"AAA": bars_from_returns(ALT, TODAY),
                             "SPY": bars_from_returns(ALT, TODAY)})
    results, pending, censored = collect_checkpoints(
        [tracked("AAA", "GATE_REJECT", TODAY - timedelta(days=1))],
        provider, today=TODAY, index_ticker="SPY", checkpoint="t5")
    assert results == []
    assert pending == 1
    assert sum(censored.values()) == 0


def test_candidate_whose_price_fetch_fails_is_censored_not_silently_dropped():
    provider = StubProvider({"SPY": bars_from_returns(ALT, TODAY)},
                            failing=frozenset({"GONE"}))
    results, pending, censored = collect_checkpoints(
        [tracked("GONE", "GATE_REJECT", DECISION_DAY)],
        provider, today=TODAY, index_ticker="SPY", checkpoint="t5")
    assert results == []
    assert pending == 0
    assert censored["GATE_REJECT"] == 1


def test_alpha_subtracts_beta_times_the_benchmark_move():
    """Stock and index move identically with beta 1.0, so alpha is zero — a
    raw +N% here is pure market beta, not selection skill."""
    index = bars_from_returns(ALT, TODAY)
    stock = bars_from_returns(ALT, TODAY)
    provider = StubProvider({"AAA": stock, "SPY": index})
    decision_day = stock[260].day
    results, _, _ = collect_checkpoints(
        [tracked("AAA", "RANKING_CUTOFF", decision_day)],
        provider, today=TODAY, index_ticker="SPY", checkpoint="t5")
    assert len(results) == 1
    r = results[0]
    assert r.beta == pytest.approx(1.0)
    assert r.alpha_pct == pytest.approx(0.0, abs=1e-6)
    assert r.horizon_days == 5
    assert r.checkpoint == "t5"


def test_checkpoint_records_every_input_needed_to_reproduce_the_verdict():
    """§5.2(3): beta is re-estimated from a rolling window, so alpha and z
    alone leave no way to audit the original call months later."""
    index = bars_from_returns(ALT, TODAY)
    stock = bars_from_returns(ALT, TODAY)
    provider = StubProvider({"AAA": stock, "SPY": index})
    results, _, _ = collect_checkpoints(
        [tracked("AAA", "GATE_REJECT", stock[260].day)],
        provider, today=TODAY, index_ticker="SPY", checkpoint="t10")
    r = results[0]
    assert r.price_at_decision is not None
    assert r.price_at_checkpoint is not None
    assert r.checkpoint_date is not None
    assert r.benchmark_return_pct is not None
    assert r.beta_window == 250
    assert r.atr_pct is not None


# --- peer baseline (third layer) ------------------------------------------

def _cp(ticker, cohort, alpha, z=None, failed_gates=(), scan_id=1,
        raw=None) -> CheckpointResult:
    return CheckpointResult(
        ticker=ticker, cohort=cohort, scan_id=scan_id,
        decision_date=DECISION_DAY, checkpoint="t5",
        checkpoint_date=DECISION_DAY, horizon_days=5,
        price_at_decision=100.0, price_at_checkpoint=100.0,
        raw_return_pct=alpha if raw is None else raw,
        benchmark_return_pct=0.0, beta=1.0, beta_window=250, atr_pct=1.0,
        alpha_pct=alpha, z=z, direction=None if z is None else
        ("up" if z >= 0 else "down"), failed_gates=failed_gates)


def test_peer_baseline_is_the_equal_weight_mean_of_the_same_scan():
    out = {r.ticker: r for r in with_peer_baseline([
        _cp("AAA", "GATE_REJECT", 10.0, scan_id=1),
        _cp("BBB", "RANKING_CUTOFF", 0.0, scan_id=1),
        _cp("CCC", "GATE_REJECT", 99.0, scan_id=2),
    ])}
    assert out["AAA"].peer_baseline_pct == pytest.approx(5.0)
    assert out["AAA"].peer_excess_pct == pytest.approx(5.0)
    assert out["BBB"].peer_excess_pct == pytest.approx(-5.0)
    # A single-member scan is its own population: "better than itself" is
    # not a finding.
    assert out["CCC"].peer_excess_pct == pytest.approx(0.0)


# --- attribution -----------------------------------------------------------

def test_cohort_attribution_reports_both_directions_separately():
    """§5.2(3): counting only the risers and loosening the gate that dropped
    them is a guaranteed way to make performance worse."""
    table = attribute_by_cohort([
        _cp("AAA", "GATE_REJECT", 10.0, z=2.0),
        _cp("BBB", "GATE_REJECT", -8.0, z=-2.5),
        _cp("CCC", "GATE_REJECT", 1.0, z=0.3),
        _cp("DDD", "BUY", 4.0, z=1.0),
    ])
    assert table["GATE_REJECT"]["n"] == 3
    assert table["GATE_REJECT"]["mean_alpha"] == pytest.approx(1.0)
    assert table["GATE_REJECT"]["up_outliers"] == 1
    assert table["GATE_REJECT"]["down_outliers"] == 1
    assert table["BUY"]["n"] == 1
    assert table["ENRICHMENT_CAP"]["n"] == 0


def test_gate_attribution_names_the_specific_gate_that_dropped_an_outlier():
    """A cohort mean says 'the gates are costing us'; only this says *which*
    gate to look at."""
    table = attribute_by_gate([
        _cp("AAA", "GATE_REJECT", 20.0, z=3.0,
            failed_gates=("min_market_cap",)),
        _cp("BBB", "GATE_REJECT", -5.0, z=-1.0,
            failed_gates=("min_dollar_volume",)),
        _cp("CCC", "GATE_REJECT", 8.0, z=1.6,
            failed_gates=("min_market_cap", "min_dollar_volume")),
    ])
    assert table["min_market_cap"]["n"] == 2
    assert table["min_market_cap"]["mean_alpha"] == pytest.approx(14.0)
    assert table["min_market_cap"]["up_outliers"] == 2
    assert table["min_dollar_volume"]["n"] == 2
    assert table["min_dollar_volume"]["mean_alpha"] == pytest.approx(1.5)


def test_gate_attribution_ignores_candidates_that_never_reached_the_gates():
    assert attribute_by_gate([_cp("AAA", "ENRICHMENT_CAP", 30.0, z=4.0)]) == {}


def test_outliers_include_both_tails_ranked_by_absolute_z():
    out = outliers([
        _cp("AAA", "GATE_REJECT", 4.0, z=1.0),
        _cp("BBB", "GATE_REJECT", 25.0, z=2.0),
        _cp("CCC", "GATE_REJECT", -12.0, z=-3.0),
    ])
    assert [r.ticker for r in out] == ["CCC", "BBB"]


def test_outliers_can_be_restricted_to_one_direction():
    out = outliers([
        _cp("BBB", "GATE_REJECT", 25.0, z=2.0),
        _cp("CCC", "GATE_REJECT", -12.0, z=-3.0),
    ], direction="up")
    assert [r.ticker for r in out] == ["BBB"]
