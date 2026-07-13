from datetime import date, timedelta

from hawkeye.marketdata.base import StaticProvider
from hawkeye.scout.benchmark import cohort_stats, forward_return
from hawkeye.scout.earnings import (
    EarningsEvent,
    eps_surprise_pct,
    parse_calendar,
    screen_events,
)
from hawkeye.scout.scout import run_scout, score_candidate
from tests.conftest import make_bars


def ev(ticker="AAA", day=None, eps_a=1.10, eps_e=1.00,
       rev_a=105.0, rev_e=100.0) -> EarningsEvent:
    return EarningsEvent(ticker=ticker, day=day or date.today(),
                         eps_actual=eps_a, eps_estimate=eps_e,
                         revenue_actual=rev_a, revenue_estimate=rev_e)


# --- earnings screen --------------------------------------------------------

def test_surprise_math_and_guards():
    assert abs(eps_surprise_pct(ev(eps_a=1.10, eps_e=1.00)) - 10.0) < 1e-9
    # negative estimate beaten to positive = large positive surprise
    assert eps_surprise_pct(ev(eps_a=0.10, eps_e=-0.10)) == 200.0
    assert eps_surprise_pct(ev(eps_e=0.0)) is None
    assert eps_surprise_pct(ev(eps_a=None)) is None


def test_screen_thresholds_and_sort():
    events = [ev("BIG", eps_a=1.30, eps_e=1.00),    # +30%
              ev("SMALL", eps_a=1.02, eps_e=1.00),  # +2% -> dropped
              ev("MID", eps_a=1.10, eps_e=1.00),    # +10%
              ev("NODATA", eps_a=None)]             # dropped
    kept = screen_events(events, min_eps_surprise_pct=5.0,
                         min_revenue_surprise_pct=0.0)
    assert [e.ticker for e, _, _ in kept] == ["BIG", "MID"]


def test_screen_revenue_miss_drops():
    events = [ev("REVMISS", rev_a=95.0, rev_e=100.0)]  # EPS beat, revenue miss
    assert screen_events(events, 5.0, 0.0) == []
    # ...but an event with NO revenue data survives on EPS alone
    events = [ev("NOREV", rev_a=None, rev_e=None)]
    assert len(screen_events(events, 5.0, 0.0)) == 1


def test_parse_calendar_skips_foreign_and_malformed():
    raw = [
        {"symbol": "aaa", "date": "2026-07-01", "epsActual": 1.1,
         "epsEstimate": 1.0},
        {"symbol": "BAD.TO", "date": "2026-07-01"},   # foreign listing
        {"symbol": "", "date": "2026-07-01"},
        {"symbol": "NODATE"},
    ]
    events = parse_calendar(raw)
    assert [e.ticker for e in events] == ["AAA"]


# --- ranking ----------------------------------------------------------------

def test_score_prefers_confirming_but_not_exhausted_reaction():
    ideal = score_candidate(20.0, 5.0, gap_on_event_pct=8.0)
    negative = score_candidate(20.0, 5.0, gap_on_event_pct=-3.0)
    exhausted = score_candidate(20.0, 5.0, gap_on_event_pct=30.0)
    assert ideal > negative and ideal > exhausted


# --- full scout run (offline) -----------------------------------------------

class FakeCalendar:
    def __init__(self, entries):
        self.entries = entries

    def earnings_calendar(self, start, end):
        return self.entries


def test_run_scout_funnel(config):
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [
        {"symbol": "GOOD", "date": event_day.isoformat(),
         "epsActual": 1.20, "epsEstimate": 1.00,
         "revenueActual": 110.0, "revenueEstimate": 100.0},
        {"symbol": "TINY", "date": event_day.isoformat(),   # will fail gates
         "epsActual": 1.50, "epsEstimate": 1.00},
        {"symbol": "WEAK", "date": event_day.isoformat(),   # below screen
         "epsActual": 1.01, "epsEstimate": 1.00},
    ]
    # provider serves the same rich bars for every ticker; TINY's profile
    # would come back identical, so distinguish via market cap override:
    bars = make_bars(300, start_price=40.0, volume=2_000_000)

    class PerTickerProvider(StaticProvider):
        def profile(self, ticker):
            if ticker == "TINY":
                return {"market_cap": 50e6}     # below 300M -> gate reject
            return {"market_cap": 5e9}

    provider = PerTickerProvider(bars=bars)
    result = run_scout(FakeCalendar(entries), provider, config, today=today)

    assert result.scanned == 3
    assert result.screened == 2                     # WEAK dropped by screen
    assert [c.ticker for c in result.passed] == ["GOOD"]
    assert [c.ticker for c in result.rejected] == ["TINY"]
    assert "gate" in result.rejected[0].reject_reason
    assert result.funnel() == {"scanned": 3, "screened": 2,
                               "enriched": 2, "gate_passed": 1}
    # the passed candidate carries a ready-to-evaluate brief
    good = result.passed[0]
    assert good.brief is not None
    assert good.brief.catalyst.event_date == event_day
    assert good.score > 0


def test_run_scout_enrichment_failure_is_visible(config):
    today = date.today()
    entries = [{"symbol": "ERR", "date": (today - timedelta(days=2)).isoformat(),
                "epsActual": 1.2, "epsEstimate": 1.0}]

    class BrokenProvider(StaticProvider):
        def daily_history(self, ticker, days=365):
            raise RuntimeError("network down")

    result = run_scout(FakeCalendar(entries), BrokenProvider(), config,
                       today=today)
    assert result.passed == []
    assert "enrichment failed" in result.rejected[0].reject_reason


# --- benchmark --------------------------------------------------------------

def test_forward_return():
    bars = make_bars(60, start_price=100.0, daily_move=0.01)
    start = bars[10].day
    ret = forward_return(bars, start, horizon_days=14)
    assert ret is not None and ret > 0
    # horizon beyond available data -> None
    assert forward_return(bars, bars[-1].day, horizon_days=30) is None


def test_cohort_stats():
    samples = [("BUY", 10.0), ("BUY", -2.0),
               ("TRIBUNAL_PASS", 1.0), ("TRIBUNAL_PASS", -1.0),
               ("GATE_REJECT", -5.0)]
    stats = cohort_stats(samples)
    assert stats["BUY"]["n"] == 2 and stats["BUY"]["mean"] == 4.0
    assert stats["BUY"]["win_rate"] == 0.5
    assert stats["TRIBUNAL_PASS"]["median"] == 0.0
    assert stats["GATE_REJECT"]["n"] == 1
