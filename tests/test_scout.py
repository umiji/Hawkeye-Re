from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from hawkeye.contracts.models import (
    AnalystTrend,
    DecisionType,
    GateReport,
    InsiderActivity,
    NewsItem,
    Recommendation,
    RecommendationStatus,
    Verdict,
)
from hawkeye.marketdata.base import StaticProvider
from hawkeye.reports.render_ja import render_scout_ja
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
    scan_window,
    score_candidate,
)
from tests.conftest import make_bars, make_brief


def ev(ticker="AAA", day=None, eps_a=1.10, eps_e=1.00,
       rev_a=105.0, rev_e=100.0) -> EarningsEvent:
    return EarningsEvent(ticker=ticker, day=day or date.today(),
                         eps_actual=eps_a, eps_estimate=eps_e,
                         revenue_actual=rev_a, revenue_estimate=rev_e)


def screen(events, min_eps=5.0, min_rev=0.0,
           min_abs_eps_estimate=0.0, max_trusted_rev=1e9):
    """Screen with every trust guard wide open unless a test narrows one, so
    a threshold test stays a threshold test."""
    return screen_events(events, min_eps, min_rev,
                         min_abs_eps_estimate, max_trusted_rev)


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
    kept = screen(events)
    assert [k.event.ticker for k in kept] == ["BIG", "MID"]


# --- surprise-percentage trust (2026-08-01) ---------------------------------
# A percentage surprise is only as good as its denominator. Three separate
# defects let a meaningless number decide which candidates were examined:
# conflicting calendar rows, actual/estimate on different accounting bases,
# and a near-zero consensus inflating the ratio without adding information.

def test_conflicting_calendar_rows_collapse_to_the_conservative_read():
    """Finnhub returned two rows for BJRI's 2026-07-30 print with different
    consensus figures (0.9085 -> +3.5%, 0.1282 -> +633.2%). Ranked by
    surprise, the broken row always won — and the correct one then fell below
    the 5% screen, so the only reading that survived was the wrong one."""
    raw = [
        {"symbol": "BJRI", "date": "2026-07-30", "epsEstimate": 0.9085,
         "epsActual": 0.94, "revenueEstimate": 384615990,
         "revenueActual": 388888000},
        {"symbol": "BJRI", "date": "2026-07-30", "epsEstimate": 0.1282,
         "epsActual": 0.94, "revenueEstimate": 349062258,
         "revenueActual": 388890000},
    ]
    events = parse_calendar(raw)
    assert len(events) == 1
    assert events[0].eps_estimate == 0.9085          # the conservative row
    assert events[0].conflicting_estimates is True   # and it says so


def test_identical_duplicate_rows_are_not_flagged_as_conflicting():
    row = {"symbol": "AAA", "date": "2026-07-30", "epsEstimate": 1.0,
           "epsActual": 1.1}
    events = parse_calendar([row, dict(row)])
    assert len(events) == 1
    assert events[0].conflicting_estimates is False


def test_near_zero_consensus_makes_the_eps_percentage_untrusted():
    """CRI printed $0.26 against a $0.06 consensus: +317%, which is a fact
    about the denominator, not a bigger surprise than a genuine +40%."""
    kept = screen([ev("CRI", eps_a=0.26, eps_e=0.0623)],
                  min_abs_eps_estimate=0.10)
    assert kept[0].eps_surprise_trusted is False
    assert kept[0].scored_eps_pct is None       # earns no ranking score


def test_revenue_actual_and_estimate_on_different_bases_are_untrusted():
    """ABR's revenueActual is gross interest income (230.9M) while its
    revenueEstimate is a net figure (50.7M). +355% is a basis mismatch."""
    kept = screen([ev("ABR", eps_a=0.10, eps_e=0.055,
                      rev_a=230_860_000, rev_e=50_702_000)],
                  min_abs_eps_estimate=0.10, max_trusted_rev=50.0)
    assert kept[0].revenue_surprise_trusted is False
    assert kept[0].eps_surprise_trusted is False      # 0.055 consensus


def test_untrusted_surprises_earn_no_score():
    assert score_candidate(None, None, None) == 0.0
    assert score_candidate(None, 5.0, None) < score_candidate(20.0, 5.0, None)


def test_enrichment_order_is_by_capped_score_not_raw_percentage():
    """The enrichment cap decides which candidates are ever looked at. Ranked
    by raw percentage, near-zero-consensus names (REITs reporting FFO, and the
    like) monopolised every slot — CORT +6958%, SONO +5194%, LXP +3459%."""
    artifact = ev("CORT", eps_a=0.71, eps_e=0.01, rev_a=105.0, rev_e=100.0)
    genuine = ev("REAL", eps_a=1.40, eps_e=1.00, rev_a=105.0, rev_e=100.0)
    kept = screen([artifact, genuine], min_abs_eps_estimate=0.10)
    assert [k.event.ticker for k in kept] == ["REAL", "CORT"]


def test_screen_revenue_miss_drops():
    events = [ev("REVMISS", rev_a=95.0, rev_e=100.0)]  # EPS beat, revenue miss
    assert screen(events) == []
    # ...but an event with NO revenue data survives on EPS alone
    events = [ev("NOREV", rev_a=None, rev_e=None)]
    assert len(screen(events)) == 1


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


# --- scan window (§5.2(1)) --------------------------------------------------
# The window used to be a fixed `today - scout_days_back .. today`. Runs are
# manual and irregular (no cron/Actions exist), so a fixed narrow window drops
# the earnings of any day the user didn't run, permanently. The window is now
# derived from the previous run.

def test_scan_window_covers_the_gap_since_the_last_run(config):
    today = date(2026, 7, 29)                     # Wednesday
    last = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)   # Monday
    w = scan_window(today, last, config)
    assert w.end == date(2026, 7, 28)             # previous business day
    assert w.start == date(2026, 7, 26)           # one day of overlap
    assert w.truncated is False


def test_scan_window_ends_on_the_previous_business_day_over_a_weekend(config):
    today = date(2026, 8, 3)                      # Monday
    w = scan_window(today, datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
                    config)
    assert w.end == date(2026, 7, 31)             # Friday, not Sunday


def test_scan_window_is_capped_and_flags_the_lost_days(config):
    today = date(2026, 7, 29)
    last = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)   # a long gap
    w = scan_window(today, last, config)
    assert w.days == config.scout_days_back
    assert w.truncated is True    # earlier earnings days were NOT scanned


def test_scan_window_without_a_previous_scan_uses_the_full_span(config):
    w = scan_window(date(2026, 7, 29), None, config)
    assert w.days == config.scout_days_back
    assert w.truncated is False   # no prior run means nothing was skipped


def test_scan_window_same_day_rerun_stays_valid(config):
    today = date(2026, 7, 29)
    w = scan_window(today, datetime(2026, 7, 29, 9, 0, tzinfo=timezone.utc),
                    config)
    assert w.start <= w.end


# --- full scout run (offline) -----------------------------------------------

class FakeCalendar:
    def __init__(self, entries):
        self.entries = entries
        self.asked: list[tuple] = []

    def earnings_calendar(self, start, end):
        self.asked.append((start, end))
        return self.entries


def test_distrusted_surprise_never_reaches_the_tribunal_as_a_fact(config):
    """The Bull and Adversary are told to prefer structured surprise fields
    over prose, so putting a number the screen distrusts into one would
    launder it into a fact. It stays out of the snapshot; the catalyst text
    still says what was measured and why it is not stood behind, and the
    Japanese shortlist flags it."""
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [{"symbol": "REIT", "date": event_day.isoformat(),
                "epsActual": 0.71, "epsEstimate": 0.01,   # +7000%, meaningless
                "revenueActual": 105e6, "revenueEstimate": 100e6}]
    provider = StaticProvider(bars=make_bars(30, start_price=40.0,
                                             volume=2_000_000),
                              profile_data={"market_cap": 5e9})

    result = run_scout(FakeCalendar(entries), provider, config, today=today)

    candidate = result.passed[0]
    assert candidate.eps_surprise_trusted is False
    assert candidate.eps_surprise_pct == 7000.0          # measured, recorded
    assert candidate.brief.snapshot.eps_surprise_pct is None   # not asserted
    assert "UNVERIFIED" in candidate.brief.catalyst.description
    assert "⚠" in render_scout_ja(result)


def test_run_scout_skips_events_already_recorded(config):
    """Overlapping windows are deliberate (the calendar back-fills), so the
    same earnings event will be seen twice. Re-evaluating it would double-
    count it in the drop statistics and waste enrichment calls."""
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [
        {"symbol": "SEEN", "date": event_day.isoformat(),
         "epsActual": 1.30, "epsEstimate": 1.00},
        {"symbol": "NEW", "date": event_day.isoformat(),
         "epsActual": 1.20, "epsEstimate": 1.00},
    ]
    bars = make_bars(30, start_price=40.0, volume=2_000_000)
    provider = StaticProvider(bars=bars, profile_data={"market_cap": 5e9})

    result = run_scout(FakeCalendar(entries), provider, config, today=today,
                       already_seen={("SEEN", event_day)})

    assert result.duplicates == 1
    assert result.enriched == 1
    assert [c.ticker for c in result.passed] == ["NEW"]


# --- the walk down the ranked screen (2026-08-02) --------------------------

class PerTickerProvider:
    """Same shape as StaticProvider, but the profile varies by ticker so a
    test can make specific names fail an entry gate."""

    def __init__(self, bars, market_caps: dict, default_cap: float = 5e9):
        self._bars = bars
        self._caps = market_caps
        self._default = default_cap
        self.enriched: list[str] = []

    def daily_history(self, ticker: str, days: int = 365):
        if days > 5:                     # the cheap price-only fetch is days=5
            self.enriched.append(ticker)
        return self._bars[-days:]

    def profile(self, ticker: str) -> dict:
        return {"market_cap": self._caps.get(ticker, self._default)}

    def news(self, ticker: str, limit: int = 10):
        return []


def _entries(today, tickers_by_surprise):
    """One calendar row per ticker, EPS surprise descending in list order."""
    day = (today - timedelta(days=3)).isoformat()
    return [{"symbol": t, "date": day, "epsActual": 1.0 + (0.50 - i * 0.01),
             "epsEstimate": 1.00}
            for i, t in enumerate(tickers_by_surprise)]


def test_a_gate_rejection_promotes_the_next_candidate_up(config):
    """The defect this replaced: enrichment took a fixed slice of the ranked
    screen, so a day where the top names failed the gates sent almost nothing
    to the tribunal while the candidates just below sat untouched."""
    today = date.today()
    entries = _entries(today, ["AAA", "BBB", "CCC"])
    provider = PerTickerProvider(
        make_bars(30, start_price=40.0, volume=2_000_000),
        market_caps={"AAA": 1e6})        # AAA is far under min_market_cap

    result = run_scout(FakeCalendar(entries), provider,
                       replace(config, scout_target_gate_passed=1),
                       today=today)

    assert [c.ticker for c in result.rejected] == ["AAA"]
    assert [c.ticker for c in result.passed] == ["BBB"]   # promoted from #2
    assert result.enriched == 2                           # AAA tried, then BBB
    assert [c.ticker for c in result.capped] == ["CCC"]   # never reached


def test_the_walk_stops_once_the_pool_is_full(config):
    today = date.today()
    entries = _entries(today, ["AAA", "BBB", "CCC", "DDD"])
    provider = PerTickerProvider(
        make_bars(30, start_price=40.0, volume=2_000_000), market_caps={})

    result = run_scout(FakeCalendar(entries), provider,
                       replace(config, scout_target_gate_passed=2),
                       today=today)

    assert len(result.passed) == 2
    assert result.enriched == 2
    assert provider.enriched == ["AAA", "BBB"]    # CCC/DDD never enriched
    assert result.enrichment_ceiling_hit is False


def test_the_attempt_ceiling_stops_a_bad_day_and_says_so(config):
    """A short shortlist because the budget ran out must not read the same as
    a short one because the calendar was quiet."""
    today = date.today()
    entries = _entries(today, ["AAA", "BBB", "CCC", "DDD"])
    provider = PerTickerProvider(
        make_bars(30, start_price=40.0, volume=2_000_000),
        market_caps={t: 1e6 for t in ("AAA", "BBB", "CCC", "DDD")})

    result = run_scout(FakeCalendar(entries), provider,
                       replace(config, scout_target_gate_passed=3,
                               scout_max_enrich=2),
                       today=today)

    assert result.passed == []
    assert result.enriched == 2
    assert result.enrichment_ceiling_hit is True
    assert [c.ticker for c in result.capped] == ["CCC", "DDD"]
    assert "ceiling" in result.capped[0].reject_reason
    assert "試行上限" in render_scout_ja(result)


def test_candidates_below_the_stop_point_are_still_recorded(config):
    """Whatever the walk did not reach still needs a row, or the drop-review
    denominator is wrong and "3 got away" reads as neither good nor bad."""
    today = date.today()
    entries = _entries(today, ["AAA", "BBB", "CCC"])
    provider = PerTickerProvider(
        make_bars(30, start_price=40.0, volume=2_000_000), market_caps={})

    result = run_scout(FakeCalendar(entries), provider,
                       replace(config, scout_target_gate_passed=1),
                       today=today)

    assert [c.ticker for c in result.capped] == ["BBB", "CCC"]
    assert all(c.price is not None for c in result.capped)   # cheap fetch ran
    assert all(c.score_version == "partial_no_gap" for c in result.capped)


class CountingNumbers:
    """A second source that records who it was asked about, so a test can
    assert on the requests a scan does NOT make."""

    def __init__(self):
        self.asked: list[str] = []

    def verified_earnings(self, ticker, day):
        self.asked.append(ticker)
        return None


def test_a_duplicate_costs_no_second_source_request(config):
    """Dedup used to run AFTER the second-source pass, so every print the
    overlapping window brought back was re-read and then thrown away. On a
    7-day window run daily that is six requests in seven wasted — which
    matters once the second source is the earnings feed rather than a
    quota-free scraper."""
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [
        {"symbol": "SEEN", "date": event_day.isoformat(),
         "epsActual": 1.30, "epsEstimate": 1.00},
        {"symbol": "NEW", "date": event_day.isoformat(),
         "epsActual": 1.20, "epsEstimate": 1.00},
    ]
    bars = make_bars(30, start_price=40.0, volume=2_000_000)
    provider = StaticProvider(bars=bars, profile_data={"market_cap": 5e9})
    numbers = CountingNumbers()

    result = run_scout(FakeCalendar(entries), provider, config, today=today,
                       already_seen={("SEEN", event_day)},
                       numbers_source=numbers)

    assert numbers.asked == ["NEW"]
    assert result.duplicates == 1
    assert [c.ticker for c in result.passed] == ["NEW"]


def test_already_seen_is_per_event_not_per_ticker(config):
    """The same ticker reporting a *different* quarter is a new candidate."""
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [{"symbol": "AAA", "date": event_day.isoformat(),
                "epsActual": 1.30, "epsEstimate": 1.00}]
    bars = make_bars(30, start_price=40.0, volume=2_000_000)
    provider = StaticProvider(bars=bars, profile_data={"market_cap": 5e9})

    result = run_scout(FakeCalendar(entries), provider, config, today=today,
                       already_seen={("AAA", event_day - timedelta(days=90))})

    assert result.duplicates == 0
    assert [c.ticker for c in result.passed] == ["AAA"]


def test_run_scout_asks_the_calendar_for_the_derived_window(config):
    today = date(2026, 7, 29)
    cal = FakeCalendar([])
    window = scan_window(today, datetime(2026, 7, 27, 9, 0,
                                         tzinfo=timezone.utc), config)

    result = run_scout(cal, StaticProvider(), config, today=today,
                       window=window)

    assert cal.asked == [(window.start, window.end)]
    assert result.scan_start == window.start and result.scan_end == window.end
    assert result.window_truncated is False


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
    assert result.funnel() == {"scanned": 3, "screened": 2, "duplicates": 0,
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


def test_screened_candidates_keep_the_qualitative_data_they_were_judged_on(config):
    """§5.2(5): news/insider/analyst are fetched during enrichment and were
    then thrown away for every dropped candidate. Without them, a later drop
    review can't reconstruct what was visible at decision time — and no extra
    API call is needed to keep them."""
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [
        {"symbol": "SMALL", "date": event_day.isoformat(),    # gate reject
         "epsActual": 1.30, "epsEstimate": 1.00},
    ]
    bars = make_bars(30, start_price=40.0, volume=2_000_000)
    insider = InsiderActivity(window_days=90, net_shares=-5000,
                              buyers=1, sellers=3)
    analyst = AnalystTrend(period=date(2026, 7, 1), strong_buy=5, buy=10,
                           hold=3, sell=1, strong_sell=0)
    news = [NewsItem(headline="Q2 beat", source="wire", url="http://x")]
    provider = StaticProvider(bars=bars, profile_data={"market_cap": 50e6},
                              news_items=news, insider=insider,
                              analyst=analyst)

    result = run_scout(FakeCalendar(entries), provider, config, today=today)
    assert [c.ticker for c in result.rejected] == ["SMALL"]

    rows = build_screened_candidates(result, scan_id=7)
    row = next(r for r in rows if r.ticker == "SMALL")
    assert [n.headline for n in row.news] == ["Q2 beat"]
    assert row.insider_activity == insider
    assert row.analyst_trend == analyst


def test_enrichment_capped_candidates_have_no_qualitative_data(config):
    """Candidates dropped before enrichment never had news fetched — the
    fields must come back empty rather than crash or invent anything."""
    from dataclasses import replace as dc_replace
    cfg = dc_replace(config, scout_max_enrich=1)
    today = date.today()
    event_day = today - timedelta(days=3)
    entries = [
        {"symbol": "TOP", "date": event_day.isoformat(),
         "epsActual": 1.30, "epsEstimate": 1.00},
        {"symbol": "CAPPED", "date": event_day.isoformat(),
         "epsActual": 1.15, "epsEstimate": 1.00},
    ]
    bars = make_bars(30, start_price=40.0, volume=2_000_000)
    provider = StaticProvider(bars=bars, profile_data={"market_cap": 5e9},
                              news_items=[NewsItem(headline="x", source="s",
                                                   url="u")])
    result = run_scout(FakeCalendar(entries), provider, cfg, today=today)

    rows = build_screened_candidates(result, scan_id=8)
    capped_row = next(r for r in rows if r.ticker == "CAPPED")
    assert capped_row.news == []
    assert capped_row.insider_activity is None
    assert capped_row.analyst_trend is None


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
