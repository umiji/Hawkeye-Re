from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from hawkeye.contracts.models import (
    DecisionType,
    GateReport,
    Recommendation,
    RecommendationStatus,
    Verdict,
)
from hawkeye.marketdata.base import StaticProvider
from hawkeye.scout.benchmark import (
    cohort_stats,
    collect_samples,
    forward_return,
    reason_snippet,
)
from hawkeye.scout.earnings import (
    EarningsEvent,
    eps_surprise_pct,
    parse_calendar,
    screen_events,
)
from hawkeye.scout.scout import (
    build_screened_candidates,
    run_scout,
    score_candidate,
)
from tests.conftest import make_bars, make_brief


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
    # H4/§5.1: a gate-rejected candidate must still get a real score — it
    # was only ever computed for `passed` candidates before this fix, which
    # left every dropped candidate's score at the dataclass default (0.0),
    # making later score-vs-return correlation checks meaningless for them.
    assert result.rejected[0].score > 0
    assert result.rejected[0].score_version == "full"


def test_run_scout_enrichment_cap_tier_is_recorded_with_one_price_fetch(config):
    from dataclasses import replace as dc_replace
    cfg = dc_replace(config, scout_max_enrich=1)
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [
        {"symbol": "TOP", "date": event_day.isoformat(),
         "epsActual": 1.30, "epsEstimate": 1.00},   # highest surprise, enriched
        {"symbol": "CAPPED", "date": event_day.isoformat(),
         "epsActual": 1.15, "epsEstimate": 1.00},   # sorted below scout_max_enrich=1
    ]
    bars = make_bars(30, start_price=40.0, volume=2_000_000)
    provider = StaticProvider(bars=bars)
    result = run_scout(FakeCalendar(entries), provider, cfg, today=today)

    assert [c.ticker for c in result.capped] == ["CAPPED"]
    capped = result.capped[0]
    assert capped.brief is None                 # never fully enriched
    assert capped.price == bars[-1].close        # one cheap price fetch only
    assert capped.price_asof == bars[-1].day
    assert capped.score > 0 and capped.score_version == "partial_no_gap"


def test_build_screened_candidates_tags_stage_and_ranking_cutoff(config):
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [
        {"symbol": t, "date": event_day.isoformat(),
         "epsActual": 1.10 + i * 0.05, "epsEstimate": 1.00}
        for i, t in enumerate(["A", "B", "C"])
    ]
    bars = make_bars(30, start_price=40.0, volume=2_000_000)
    provider = StaticProvider(bars=bars, profile_data={"market_cap": 5e9})
    result = run_scout(FakeCalendar(entries), provider, config, today=today)
    assert len(result.passed) == 3   # all three clear the gates

    rows = build_screened_candidates(result, scan_id=42, sent_to_tribunal_n=1)

    # only the tail beyond sent_to_tribunal_n=1 becomes ranking_cutoff
    cutoff_rows = [r for r in rows if r.stage.value == "ranking_cutoff"]
    assert len(cutoff_rows) == 2
    assert all(r.scan_id == 42 for r in rows)
    ranks = sorted(r.rank for r in cutoff_rows)
    assert ranks == [2, 3]   # 1-indexed, the top-1 already went to tribunal


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


def test_forward_return_walks_trading_days_not_calendar_days():
    # M13: horizon_days must count entries in `bars` (one per trading day),
    # not add calendar days — the two disagree by ~30% over multi-week spans.
    bars = make_bars(60, start_price=100.0, daily_move=0.0)  # flat prices
    bars[15] = replace(bars[15], close=200.0)  # marker bar, exactly +5 index
    ret = forward_return(bars, bars[10].day, horizon_days=5)
    assert ret is not None and abs(ret - 100.0) < 1e-9  # bars[10] -> bars[15]


def test_min_calendar_days_for_trading_days():
    from hawkeye.scout.benchmark import min_calendar_days_for_trading_days
    assert min_calendar_days_for_trading_days(5) == 7    # ceil(5*7/5)
    assert min_calendar_days_for_trading_days(30) == 42  # ceil(30*7/5)


def test_cohort_stats():
    samples = [("BUY", 10.0), ("BUY", -2.0),
               ("TRIBUNAL_PASS", 1.0), ("TRIBUNAL_PASS", -1.0),
               ("GATE_REJECT", -5.0)]
    stats = cohort_stats(samples)
    assert stats["BUY"]["n"] == 2 and stats["BUY"]["mean"] == 4.0
    assert stats["BUY"]["win_rate"] == 0.5
    assert stats["TRIBUNAL_PASS"]["median"] == 0.0
    assert stats["GATE_REJECT"]["n"] == 1


def scan_rec(source="scout/finnhub-earnings-calendar", ticker="TEST",
            days_ago=60, decision=DecisionType.PASS,
            thesis=None) -> Recommendation:
    # 60 calendar days safely clears the ~42-calendar-day wait a 30-
    # trading-day horizon now needs (see min_calendar_days_for_trading_days).
    brief = make_brief()
    brief.catalyst.source = source
    return Recommendation(
        ticker=ticker, brief=brief, gate_report=GateReport(), thesis=thesis,
        verdict=Verdict(decision=decision, conviction=0.6, rationale="test"),
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago))


class FailingProvider(StaticProvider):
    def daily_history(self, ticker, days=365):
        raise RuntimeError("network down")


def test_collect_samples_excludes_manual_entries_by_default():
    # ROADMAP.md: manual `evaluate` picks must never enter viability stats.
    # 100 trading-day bars gives enough room for eval_day (60 calendar days
    # back, per scan_rec's default) plus a 30-trading-day horizon forward.
    scout_rec = scan_rec(source="scout/finnhub-earnings-calendar")
    manual_rec = scan_rec(source="manual")
    bars = make_bars(100, start_price=100.0, daily_move=0.01)
    provider = StaticProvider(bars=bars)

    samples, pending, censored = collect_samples(
        [scout_rec, manual_rec], provider, today=date.today(),
        horizon_days=30, source="scout")

    assert len(samples) == 1
    assert pending == 0 and sum(censored.values()) == 0


def test_collect_samples_source_all_includes_both():
    scout_rec = scan_rec(source="scout/finnhub-earnings-calendar")
    manual_rec = scan_rec(source="manual")
    bars = make_bars(100, start_price=100.0, daily_move=0.01)
    provider = StaticProvider(bars=bars)

    samples, _, _ = collect_samples(
        [scout_rec, manual_rec], provider, today=date.today(),
        horizon_days=30, source="all")

    assert len(samples) == 2


def test_collect_samples_pending_does_not_count_as_censored():
    fresh_rec = scan_rec(days_ago=5)  # horizon (30d) hasn't elapsed
    bars = make_bars(60, start_price=100.0, daily_move=0.01)
    provider = StaticProvider(bars=bars)

    samples, pending, censored = collect_samples(
        [fresh_rec], provider, today=date.today(),
        horizon_days=30, source="scout")

    assert samples == [] and pending == 1
    assert sum(censored.values()) == 0


def test_collect_samples_fetch_failure_is_censored_not_silently_dropped():
    # A ticker whose price history can't be fetched (delisted, acquired,
    # API outage) must not just vanish — it has to be visible as censored,
    # per cohort, so survivorship bias in the comparison isn't hidden.
    buy_rec = scan_rec(decision=DecisionType.BUY)

    samples, pending, censored = collect_samples(
        [buy_rec], FailingProvider(), today=date.today(),
        horizon_days=30, source="scout")

    assert samples == [] and pending == 0
    assert censored["BUY"] == 1
    assert censored["TRIBUNAL_PASS"] == 0 and censored["GATE_REJECT"] == 0


# --- individual postmortem (review-passes) ----------------------------------

def gate_reject_rec() -> Recommendation:
    return Recommendation(
        ticker="TEST", brief=make_brief(), gate_report=GateReport(),
        verdict=Verdict(decision=DecisionType.PASS, conviction=0.0,
                        rationale="Hard entry-gate failure: min_price"))


def buy_rec() -> Recommendation:
    return Recommendation(
        ticker="TEST", brief=make_brief(), gate_report=GateReport(),
        verdict=Verdict(decision=DecisionType.BUY, conviction=0.7,
                        rationale="Edge survives attack; sizing per plan."))


def test_reason_snippet_declined_ignores_stale_buy_rationale():
    # a DECLINED rec's verdict.rationale is the original BUY case, not a
    # PASS reason — the snippet must not present it as if it were one
    text = reason_snippet(buy_rec(), RecommendationStatus.DECLINED.value)
    assert "BUY" in text or "システムはBUY" in text
    assert "Edge survives attack" not in text


def test_reason_snippet_system_pass_uses_first_rationale_line():
    rec = gate_reject_rec()
    text = reason_snippet(rec, RecommendationStatus.SYSTEM_PASS.value)
    assert text.startswith("Hard entry-gate failure")


def test_reason_snippet_truncates_long_rationale():
    rec = buy_rec()
    rec.verdict.rationale = "x" * 500
    text = reason_snippet(rec, RecommendationStatus.SYSTEM_PASS.value, max_len=50)
    assert len(text) == 50
