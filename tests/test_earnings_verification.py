"""The source split: discovery from the calendar, EPS from Yahoo.

Fully offline — the Yahoo side is a stub shaped like the real DataFrame
(`_StubFrame`), so the rules these tests pin are the ones in our code, not
Yahoo's availability on the day the suite runs.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from hawkeye.config import HawkeyeConfig
from hawkeye.marketdata.yahoo_earnings import YahooEarningsSource
from hawkeye.scout.earnings import (
    EarningsEvent,
    eps_surprise_pct,
    parse_calendar,
    screen_events,
)
from hawkeye.scout.verify import verification_targets, verify_events

NAN = float("nan")


# --- a stand-in for yfinance's earnings_dates DataFrame ---------------------

class _StubFrame:
    """Just enough of a pandas frame: `.empty` and `.iterrows()` yielding
    (tz-aware timestamp, row-mapping) exactly as the real one does."""

    def __init__(self, rows):
        self._rows = rows

    @property
    def empty(self) -> bool:
        return not self._rows

    def iterrows(self):
        for stamp, values in self._rows:
            yield stamp, values


def _stamp(day: date, hour: int = 16) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)


def _source(rows_by_ticker: dict, calls: list | None = None
            ) -> YahooEarningsSource:
    def factory(ticker: str):
        if calls is not None:
            calls.append(ticker)

        class _T:
            @staticmethod
            def get_earnings_dates(limit: int = 8):
                return _StubFrame(rows_by_ticker.get(ticker, []))
        return _T()
    return YahooEarningsSource(ticker_factory=factory)


def _bjri_rows():
    """BJRI's real 2026-07-30 print. The displayed estimate is rounded to
    0.90 but the published surprise is +4.95%, which the two displayed
    numbers alone give as +4.44%."""
    return [(_stamp(date(2026, 10, 29)),
             {"EPS Estimate": 0.14, "Reported EPS": NAN, "Surprise(%)": NAN}),
            (_stamp(date(2026, 7, 30)),
             {"EPS Estimate": 0.90, "Reported EPS": 0.94, "Surprise(%)": 4.95})]


def _event(ticker="BJRI", day=date(2026, 7, 30), actual=0.94, estimate=0.1282,
           **kw) -> EarningsEvent:
    return EarningsEvent(ticker=ticker, day=day, eps_actual=actual,
                         eps_estimate=estimate, revenue_actual=388_888_000,
                         revenue_estimate=380_000_000, **kw)


# --- YahooEarningsSource ---------------------------------------------------

def test_returns_the_published_surprise_rather_than_recomputing_it():
    # The whole reason EPS moved to Yahoo: recomputing 0.94 vs 0.90 gives
    # +4.44%, understating a beat whose true consensus was ~0.8957.
    found = _source({"BJRI": _bjri_rows()}).verified_earnings(
        "BJRI", date(2026, 7, 30))
    assert found is not None
    assert found.surprise_pct == pytest.approx(4.95)
    assert found.surprise_pct != pytest.approx(
        (0.94 - 0.90) / 0.90 * 100, abs=0.1)


def test_a_scheduled_but_unreported_date_is_not_a_verified_print():
    # NaN actual = the next quarter's date, which carries an estimate. It
    # must not surface as a print with a missing number.
    assert _source({"BJRI": _bjri_rows()}).verified_earnings(
        "BJRI", date(2026, 10, 29)) is None


def test_a_one_day_gap_still_matches_but_a_larger_one_does_not():
    # Yahoo timestamps in exchange time and the calendar carries a plain
    # date; pre-market releases land on either side of the boundary.
    src = _source({"BJRI": _bjri_rows()})
    assert src.verified_earnings("BJRI", date(2026, 7, 31)) is not None
    assert src.verified_earnings("BJRI", date(2026, 8, 3)) is None


def test_a_scraper_failure_reads_as_unverified_not_as_agreement():
    class _Boom:
        @staticmethod
        def get_earnings_dates(limit: int = 8):
            raise ImportError("Import lxml failed")

    src = YahooEarningsSource(ticker_factory=lambda t: _Boom())
    assert src.verified_earnings("BJRI", date(2026, 7, 30)) is None


def test_no_rows_at_all_is_unverified():
    assert _source({}).verified_earnings("XYZ", date(2026, 7, 30)) is None


# --- the surprise-preference rule inside EarningsEvent ---------------------

def test_a_published_surprise_wins_over_the_computed_one():
    event = _event(eps_surprise_pct_reported=4.95, actual=0.94, estimate=0.90)
    assert eps_surprise_pct(event) == pytest.approx(4.95)


def test_without_a_published_surprise_it_is_computed_as_before():
    assert eps_surprise_pct(_event(actual=1.1, estimate=1.0)) == pytest.approx(10.0)


# --- which names get verified ---------------------------------------------

def test_screen_survivors_come_first_and_the_budget_caps_the_rest():
    kept = _event("AAA", eps_surprise_pct_reported=None, actual=1.2, estimate=1.0)
    dropped = _event("ZZZ", actual=1.0, estimate=1.0, conflicting_estimates=True)
    screened = screen_events([kept], 5.0, 0.0, 0.10, 50.0)
    assert verification_targets([kept, dropped], screened, limit=1) == [
        ("AAA", date(2026, 7, 30))]


def test_a_name_the_screen_dropped_on_conflicting_rows_is_still_verified():
    # The asymmetric failure mode: collapsing BJRI's contradictory rows to
    # the conservative reading pushed its real +3.5% below the 5% screen, so
    # the CORRECT reading was the one that vanished. A false negative leaves
    # no trace, so this tier is the only thing that can catch it.
    dropped = _event("ZZZ", actual=1.0, estimate=1.0, conflicting_estimates=True)
    assert verification_targets([dropped], screened=[], limit=10) == [
        ("ZZZ", date(2026, 7, 30))]


def test_a_name_the_screen_dropped_without_a_conflict_is_left_alone():
    quiet = _event("ZZZ", actual=1.0, estimate=1.0)
    assert verification_targets([quiet], screened=[], limit=10) == []


def test_a_print_an_earlier_scan_recorded_is_never_verified_again():
    """Scan windows overlap by design, so on a 7-day window run daily six of
    every seven prints arrive again. Verifying them spends a request per name
    to re-derive a number an earlier scan already recorded and the dedup is
    about to discard, so the skip set bounds BOTH tiers — the screen's
    survivors and the conflicting-row suspects."""
    kept = _event("AAA", actual=1.2, estimate=1.0)
    suspect = _event("ZZZ", actual=1.0, estimate=1.0, conflicting_estimates=True)
    screened = screen_events([kept], 5.0, 0.0, 0.10, 50.0)
    day = date(2026, 7, 30)

    assert verification_targets([kept, suspect], screened, limit=10,
                                skip={("AAA", day)}) == [("ZZZ", day)]
    assert verification_targets([kept, suspect], screened, limit=10,
                                skip={("ZZZ", day)}) == [("AAA", day)]


def test_a_named_print_is_verified_even_when_an_earlier_scan_saw_it():
    """`always` is a person asking about one stock outright. The skip set
    exists to stop the scan re-reading its own overlap, not to refuse a
    direct question."""
    event = _event("AAA", actual=1.2, estimate=1.0)
    day = date(2026, 7, 30)
    assert verification_targets([event], screened=[], limit=10,
                                always=[("AAA", day)],
                                skip={("AAA", day)}) == [("AAA", day)]


def test_a_skipped_print_costs_no_request():
    a = _event("AAA", actual=1.2, estimate=1.0, conflicting_estimates=True)
    b = _event("BBB", actual=1.2, estimate=1.0, conflicting_estimates=True)
    calls: list[str] = []
    out, stats = verify_events([a, b], [], _source({}, calls), limit=10,
                               skip={("AAA", date(2026, 7, 30))})
    assert calls == ["BBB"]
    assert stats.attempted == 1
    assert [e.ticker for e in out] == ["AAA", "BBB"]   # universe unchanged


def test_budget_exhausted_distinguishes_not_asked_from_asked_and_agreed():
    a = _event("AAA", actual=1.2, estimate=1.0, conflicting_estimates=True)
    b = _event("BBB", actual=1.2, estimate=1.0, conflicting_estimates=True)
    _, stats = verify_events([a, b], [], _source({}), limit=1)
    assert (stats.attempted, stats.budget_exhausted) == (1, True)

    _, stats = verify_events([a, b], [], _source({}), limit=5)
    assert (stats.attempted, stats.budget_exhausted) == (2, False)


# --- substitution ----------------------------------------------------------

def test_verification_replaces_the_eps_figures_and_keeps_the_old_reading():
    event = _event()                      # calendar says 0.94 vs 0.1282
    screened = screen_events([event], 5.0, 0.0, 0.10, 50.0)
    out, stats = verify_events([event], screened,
                               _source({"BJRI": _bjri_rows()}), limit=10)
    verified = out[0]
    assert verified.eps_source == "yahoo"
    assert eps_surprise_pct(verified) == pytest.approx(4.95)
    # What the calendar had claimed is preserved, not overwritten — the
    # disagreement rate between the sources is itself data.
    assert verified.calendar_eps_surprise_pct == pytest.approx(633.2, abs=0.5)
    assert (stats.verified, stats.disagreed) == (1, 1)


def test_an_unverified_name_keeps_the_calendar_numbers_and_says_so():
    event = _event()
    screened = screen_events([event], 5.0, 0.0, 0.10, 50.0)
    out, stats = verify_events([event], screened, _source({}), limit=10)
    assert out[0] == event                # untouched
    assert out[0].eps_source == "calendar"
    assert (stats.verified, stats.unverified) == (0, 1)


def test_events_outside_the_verification_set_are_still_returned():
    # Returning only the verified ones would silently narrow the universe.
    kept = _event("AAA", actual=1.2, estimate=1.0)
    other = _event("ZZZ", actual=0.5, estimate=1.0)
    out, _ = verify_events([kept, other],
                           screen_events([kept], 5.0, 0.0, 0.10, 50.0),
                           _source({}), limit=10)
    assert [e.ticker for e in out] == ["AAA", "ZZZ"]


def test_without_a_source_nothing_changes():
    event = _event()
    out, stats = verify_events([event], [], None, limit=10)
    assert out == [event] and stats.attempted == 0


# --- the end-to-end effect the split exists for ---------------------------

def test_a_screen_rejection_now_rests_on_the_companys_own_number():
    """BJRI 2026-07-30, as Finnhub actually returned it.

    Two rows for one print: +3.5% (estimate 0.9085) and +633.2% (estimate
    0.1282). Whichever won, the screen's decision rested on a number nobody
    could stand behind. Yahoo reads +4.95%, matching the company's release
    ($0.94 adjusted vs a ~$0.8957 consensus).

    The candidate is still dropped — 4.95% is genuinely under the 5% bar —
    and that is the point. A rejection on the right number is a different
    event from a rejection on a contradictory one, and only the first can
    ever be reviewed honestly.
    """
    raw = [{"symbol": "BJRI", "date": "2026-07-30", "epsActual": 0.94,
            "epsEstimate": 0.9085, "revenueActual": 388_888_000,
            "revenueEstimate": 380_000_000},
           {"symbol": "BJRI", "date": "2026-07-30", "epsActual": 0.94,
            "epsEstimate": 0.1282, "revenueActual": 388_888_000,
            "revenueEstimate": 380_000_000}]
    events = parse_calendar(raw)
    assert events[0].conflicting_estimates is True
    assert eps_surprise_pct(events[0]) == pytest.approx(3.47, abs=0.05)

    config = HawkeyeConfig()
    provisional = screen_events(events, config.scout_min_eps_surprise_pct,
                                config.scout_min_revenue_surprise_pct,
                                config.scout_min_abs_eps_estimate,
                                config.scout_max_trusted_revenue_surprise_pct)
    assert provisional == []              # dropped by the 5% screen

    verified, stats = verify_events(events, provisional,
                                    _source({"BJRI": _bjri_rows()}),
                                    config.scout_max_verify)
    assert stats.verified == 1
    final = screen_events(verified, config.scout_min_eps_surprise_pct,
                          config.scout_min_revenue_surprise_pct,
                          config.scout_min_abs_eps_estimate,
                          config.scout_max_trusted_revenue_surprise_pct)
    assert final == []
    assert eps_surprise_pct(verified[0]) == pytest.approx(4.95)
    assert verified[0].eps_source == "yahoo"
    assert verified[0].calendar_eps_surprise_pct == pytest.approx(3.47, abs=0.05)


def test_verification_recovers_a_name_the_calendar_wrongly_screened_out():
    """AAPL 2026-07-30 — the false negative the second tier exists for.

    Finnhub stamped the PRIOR quarter's EPS (1.91) onto the row while
    carrying a current-quarter consensus, reading as a 6.9% MISS: dropped by
    the screen, with no trace anywhere that a candidate had been lost.
    Yahoo reads 2.02 vs 1.89 (+6.74%), matching Apple's own release. Because
    the row arrived with contradictory consensus figures, it is verified
    despite having failed the screen — and comes back as a candidate.
    """
    raw = [{"symbol": "AAPL", "date": "2026-07-30", "epsActual": 1.91,
            "epsEstimate": 2.0512, "revenueActual": 109_417_000_000,
            "revenueEstimate": 105_000_000_000},
           {"symbol": "AAPL", "date": "2026-07-30", "epsActual": 1.91,
            "epsEstimate": 0.1282, "revenueActual": 109_417_000_000,
            "revenueEstimate": 105_000_000_000}]
    config = HawkeyeConfig()
    events = parse_calendar(raw)
    provisional = screen_events(events, config.scout_min_eps_surprise_pct,
                                config.scout_min_revenue_surprise_pct,
                                config.scout_min_abs_eps_estimate,
                                config.scout_max_trusted_revenue_surprise_pct)
    assert provisional == []              # a beat, read as a miss, silently

    rows = [(_stamp(date(2026, 7, 30)),
             {"EPS Estimate": 1.89, "Reported EPS": 2.02, "Surprise(%)": 6.74})]
    verified, stats = verify_events(events, provisional, _source({"AAPL": rows}),
                                    config.scout_max_verify)
    assert stats.verified == 1
    final = screen_events(verified, config.scout_min_eps_surprise_pct,
                          config.scout_min_revenue_surprise_pct,
                          config.scout_min_abs_eps_estimate,
                          config.scout_max_trusted_revenue_surprise_pct)
    assert [s.event.ticker for s in final] == ["AAPL"]
    assert final[0].eps_surprise_pct == pytest.approx(6.74)
    assert final[0].event.eps_source == "yahoo"


def test_a_near_zero_consensus_stays_untrusted_after_verification():
    """Verification fixes the SOURCE, not the arithmetic. A REIT whose GAAP
    EPS consensus sits near zero still produces a percentage that measures
    the denominator; that judgement belongs to the trust band and must
    survive a second source confirming the same small numbers."""
    event = _event("RIET", actual=0.05, estimate=0.001)
    rows = [(_stamp(date(2026, 7, 30)),
             {"EPS Estimate": 0.001, "Reported EPS": 0.05,
              "Surprise(%)": 4900.0})]
    out, _ = verify_events([event], screen_events([event], 5.0, 0.0, 0.10, 50.0),
                           _source({"RIET": rows}), limit=10)
    screened = screen_events(out, 5.0, 0.0, 0.10, 50.0)
    assert screened[0].event.eps_source == "yahoo"
    assert screened[0].eps_surprise_trusted is False
    assert screened[0].scored_eps_pct is None


def test_each_verified_name_costs_exactly_one_lookup():
    calls: list[str] = []
    events = [_event("AAA", actual=1.2, estimate=1.0),
              replace(_event("BBB", actual=1.3, estimate=1.0))]
    screened = screen_events(events, 5.0, 0.0, 0.10, 50.0)
    verify_events(events, screened, _source({}, calls), limit=10)
    assert sorted(calls) == ["AAA", "BBB"]
