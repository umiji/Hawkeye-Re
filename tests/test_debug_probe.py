"""Tests for the debug UI's data layer (offline — no network).

The debug view's whole value is that it does not editorialise: it shows
the response Hawkeye received and the reading the production code made of
it. These tests pin the two properties that would silently destroy that —
the API key leaking into what the page displays, and the interpretation
drifting away from the screen it is meant to explain.
"""
from datetime import date, timedelta

import httpx
import pytest

from debug.probe import (
    ProbeError,
    _RecordingFinnhub,
    build_prints,
    jsonable,
    probe_ticker,
)
from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import NewsItem


class _StubClient:
    """Stands in for httpx.Client, returning one canned response."""

    def __init__(self, response: httpx.Response):
        self._response = response
        self.calls: list[dict] = []

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        return self._response


def _provider(response: httpx.Response) -> tuple[_RecordingFinnhub, _StubClient]:
    provider = _RecordingFinnhub(api_key="test-key")
    client = _StubClient(response)
    provider._client = client
    return provider, client


def _ok(payload) -> httpx.Response:
    return httpx.Response(200, json=payload,
                          request=httpx.Request("GET", "http://stub"))


CONFIG = HawkeyeConfig()


# --- the key must never reach the page --------------------------------------

def test_recorded_params_never_include_the_api_key():
    provider, client = _provider(_ok({"earningsCalendar": []}))

    provider.earnings_calendar_for("AAPL", date(2026, 1, 1), date(2026, 8, 1))

    sent = client.calls[0]["params"]
    assert sent["token"] == "test-key"          # it did authenticate...
    recorded = provider.calls[0]
    assert "token" not in recorded.params       # ...but the record is clean
    assert recorded.params == {"symbol": "AAPL", "from": "2026-01-01",
                               "to": "2026-08-01"}


def test_a_failed_request_is_recorded_with_its_status_not_swallowed():
    denied = httpx.Response(403, request=httpx.Request("GET", "http://stub"))
    provider, _ = _provider(denied)

    assert provider.analyst_trend("AAPL") is None   # provider degrades…

    call = provider.calls[0]                        # …but the failure is kept
    assert call.ok is False
    assert call.status == 403
    assert "token" not in call.params


# --- interpretation matches the production screen ---------------------------

def test_rows_for_one_print_collapse_to_one_entry_flagged_as_conflicting():
    """The BJRI 2026-07-30 shape: one print, two consensus figures."""
    rows = [
        {"symbol": "BJRI", "date": "2026-07-30", "epsActual": 0.94,
         "epsEstimate": 0.9085, "revenueActual": 3.4e8,
         "revenueEstimate": 3.4e8, "quarter": 2},
        {"symbol": "BJRI", "date": "2026-07-30", "epsActual": 0.94,
         "epsEstimate": 0.1282, "revenueActual": 3.4e8,
         "revenueEstimate": 3.4e8, "quarter": 3},
    ]

    prints = build_prints(rows, CONFIG, bars=[])

    assert len(prints) == 1
    view = prints[0]
    assert view["row_count"] == 2
    assert view["rows"] == rows                       # both shown, untouched
    assert view["conflicting_estimates"] is True
    assert view["eps_surprise_pct"] == pytest.approx(3.47, abs=0.01)
    # The conservative reading falls below the 5% screen — which is exactly
    # what the broken +633% row used to hide.
    assert view["screened"] is False
    assert "5.0%" in view["reason"]


def test_a_genuine_beat_passes_and_scores():
    rows = [{"symbol": "XYZ", "date": "2026-07-30", "epsActual": 1.4,
             "epsEstimate": 1.0, "revenueActual": 1.05e9,
             "revenueEstimate": 1.0e9}]

    view = build_prints(rows, CONFIG, bars=[])[0]

    assert view["screened"] is True
    assert view["eps_surprise_trusted"] is True
    assert view["revenue_surprise_trusted"] is True
    assert view["score_partial_no_gap"] == 50.0     # 40 capped-EPS + 10 rev
    assert view["score_full"] is None               # no bars, so no gap


def test_a_near_zero_consensus_is_shown_but_earns_no_score():
    rows = [{"symbol": "RRR", "date": "2026-07-30", "epsActual": 0.05,
             "epsEstimate": 0.001}]

    view = build_prints(rows, CONFIG, bars=[])[0]

    assert view["eps_surprise_pct"] == pytest.approx(4900.0)
    assert view["eps_surprise_trusted"] is False
    assert view["score_partial_no_gap"] == 0.0


def test_a_scheduled_print_is_not_reported_as_rejected():
    """A future date carries an estimate and no actual. Calling that
    "rejected" would read as a judgement on a company that has not
    reported yet."""
    ahead = date.today() + timedelta(days=30)
    rows = [{"symbol": "XYZ", "date": ahead.isoformat(), "epsActual": None,
             "epsEstimate": 2.05}]

    view = build_prints(rows, CONFIG, bars=[])[0]

    assert view["scheduled"] is True
    assert view["screened"] is False
    assert "scheduled" in view["reason"]


def test_a_past_print_with_no_actual_is_not_called_scheduled():
    rows = [{"symbol": "XYZ", "date": "2026-01-15", "epsActual": None,
             "epsEstimate": 2.05}]

    view = build_prints(rows, CONFIG, bars=[])[0]

    assert view["scheduled"] is False
    assert "no computable EPS surprise" in view["reason"]


def test_a_row_the_parser_skips_is_still_listed():
    """A foreign/secondary listing is dropped by parse_calendar. It must
    still appear, marked, rather than vanish from the inspection."""
    rows = [{"symbol": "ABC.L", "date": "2026-07-30", "epsActual": 1.0,
             "epsEstimate": 0.5}]

    view = build_prints(rows, CONFIG, bars=[])[0]

    assert view["parsed"] is False
    assert view["symbol"] == "ABC.L"
    assert "skipped" in view["reason"]


# --- plumbing ---------------------------------------------------------------

def test_json_rendering_handles_the_models_the_providers_return():
    rendered = jsonable({
        "news": [NewsItem(headline="h", source="s", url="u")],
        "day": date(2026, 7, 30),
        "nested": [{"x": date(2026, 1, 2)}],
    })

    assert rendered["day"] == "2026-07-30"
    assert rendered["nested"][0]["x"] == "2026-01-02"
    assert rendered["news"][0]["headline"] == "h"


@pytest.mark.parametrize("bad", ["", "  ", "AAPL; DROP", "../etc/passwd",
                                 "TOOLONGTICKER", "1AAPL"])
def test_an_unusable_ticker_is_refused_before_any_request(bad):
    with pytest.raises(ProbeError):
        probe_ticker(bad)
