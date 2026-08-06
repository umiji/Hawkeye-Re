"""A calendar feed that cannot answer must not read as a quiet day.

Found live on 2026-08-03: every `calendar/earnings` request timed out while
the same key answered `quote` in 0.3s, and the funnel printed
"決算イベント 0件" — indistinguishable from a genuinely empty window. The
scan was then recorded anyway, so the days nobody managed to read were
dropped from every future window (the next window starts from the last
recorded scan). Same class as `enrichment_ceiling_hit`: "we did not look"
and "we looked and found nothing" must not print the same.
"""
from __future__ import annotations

import argparse
from datetime import date

import httpx
import pytest

from hawkeye.marketdata.base import CalendarUnavailable, StaticProvider
from hawkeye.marketdata.finnhub import FinnhubProvider
from hawkeye.scout import run_scout


# --- the provider says so instead of returning an empty list ----------------

def _provider_whose_feed_raises(exc: Exception) -> FinnhubProvider:
    p = FinnhubProvider(api_key="test-key")

    def fail(path, **params):
        raise exc

    p._get = fail  # type: ignore[method-assign]
    return p


def test_earnings_calendar_raises_when_the_feed_times_out():
    p = _provider_whose_feed_raises(httpx.ReadTimeout("timed out"))
    with pytest.raises(CalendarUnavailable):
        p.earnings_calendar(date(2026, 7, 31), date(2026, 7, 31))


def test_earnings_calendar_raises_without_an_api_key():
    """No key is another way of not having looked."""
    with pytest.raises(CalendarUnavailable):
        FinnhubProvider(api_key="").earnings_calendar(
            date(2026, 7, 31), date(2026, 7, 31))


def test_earnings_calendar_raises_on_a_malformed_payload():
    p = FinnhubProvider(api_key="test-key")
    p._get = lambda path, **params: "<html>error</html>"  # type: ignore[method-assign]
    with pytest.raises(CalendarUnavailable):
        p.earnings_calendar(date(2026, 7, 31), date(2026, 7, 31))


def test_a_genuinely_empty_window_still_returns_no_rows():
    """The distinction only means something if the quiet day still works."""
    p = FinnhubProvider(api_key="test-key")
    p._get = lambda path, **params: {"earningsCalendar": []}  # type: ignore[method-assign]
    assert p.earnings_calendar(date(2026, 7, 31), date(2026, 7, 31)) == []


# --- the funnel refuses to report a scan it could not run -------------------

class DeadCalendar:
    def earnings_calendar(self, start, end):
        raise CalendarUnavailable("calendar/earnings timed out")


def test_run_scout_surfaces_the_outage_instead_of_reporting_zero_events(config):
    with pytest.raises(CalendarUnavailable):
        run_scout(DeadCalendar(), StaticProvider(), config, today=date.today())


# --- and the watermark that decides the next window stays put ---------------

class DeadFinnhub:
    """Stands in for the whole Finnhub provider: reachable, key valid, but
    the earnings-calendar endpoint will not answer."""
    available = True

    def earnings_calendar(self, start, end):
        raise CalendarUnavailable("calendar/earnings timed out")

    def daily_history(self, ticker, days=365):
        return []

    def profile(self, ticker):
        return {}

    def news(self, ticker, limit=10, event_date=None, lead_days=3):
        return []

    def insider_activity(self, ticker, window_days=90):
        return None

    def analyst_trend(self, ticker):
        return None


def test_a_failed_scan_does_not_advance_the_next_window(tmp_path, monkeypatch):
    """The next scan's window starts from the last recorded scan. Recording
    one that read nothing makes every unread day unreachable forever."""
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    from hawkeye import cli

    monkeypatch.setattr(cli, "FinnhubProvider", lambda *a, **k: DeadFinnhub())
    assert cli._ledger().last_scan_at() is None

    rc = cli.cmd_scout(argparse.Namespace(days=None, evaluate=0, open_cases=0))

    assert rc == 1
    assert cli._ledger().last_scan_at() is None, (
        "a scan that never read the calendar must not become the anchor "
        "for the next window")


def test_a_failed_scan_says_so_on_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    from hawkeye import cli

    monkeypatch.setattr(cli, "FinnhubProvider", lambda *a, **k: DeadFinnhub())
    cli.cmd_scout(argparse.Namespace(days=None, evaluate=0, open_cases=0))

    err = capsys.readouterr().err
    assert "決算カレンダー" in err
    assert "0件" not in err
