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
from hawkeye.marketdata.yahoo_earnings import VerifiedEarnings


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


class _StubNumbers:
    """A Yahoo numbers source with canned readings, shaped like the real one."""

    def __init__(self, by_key: dict):
        self._by_key = by_key
        self.raw_rows: list[dict] = []
        self.asked: list[tuple] = []

    def verified_earnings(self, ticker: str, day: date):
        self.asked.append((ticker, day))
        return self._by_key.get((ticker, day))


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


# --- the two sources side by side -------------------------------------------

def _aapl_rows():
    """The 2026-07-30 print as Finnhub returned it: an EPS actual belonging
    to the PREVIOUS quarter (1.91; Apple reported 2.02), on two rows whose
    consensus figures contradict each other."""
    return [{"symbol": "AAPL", "date": "2026-07-30", "epsActual": 1.91,
             "epsEstimate": 2.0512, "revenueActual": 109_417_000_000,
             "revenueEstimate": 105_000_000_000},
            {"symbol": "AAPL", "date": "2026-07-30", "epsActual": 1.91,
             "epsEstimate": 0.1282, "revenueActual": 109_417_000_000,
             "revenueEstimate": 105_000_000_000}]


def test_the_yahoo_reading_sits_beside_the_calendars_and_marks_the_gaps():
    numbers = _StubNumbers({("AAPL", date(2026, 7, 30)): VerifiedEarnings(
        ticker="AAPL", report_date=date(2026, 7, 30), eps_actual=2.02,
        eps_estimate=1.89, surprise_pct=6.74)})

    view = build_prints(_aapl_rows(), CONFIG, bars=[], numbers_source=numbers)[0]

    assert view["screened"] is False              # the calendar read a miss
    yahoo = view["yahoo"]
    assert yahoo["verified"] is True
    assert yahoo["eps_surprise_pct"] == pytest.approx(6.74)
    assert yahoo["screened"] is True              # the same screen, other data
    # The verdict flipping is the finding, so it has to be named as a
    # difference and not left for the reader to spot across two rows.
    assert set(yahoo["differs"]) >= {"eps_actual", "eps_estimate",
                                     "eps_surprise_pct", "screened"}


def test_the_published_surprise_is_shown_not_one_recomputed_from_the_columns():
    """Yahoo rounds the estimate it displays but not the surprise it
    publishes. Recomputing 0.94 vs 0.90 gives +4.44%; the truth is +4.95%."""
    numbers = _StubNumbers({("BJRI", date(2026, 7, 30)): VerifiedEarnings(
        ticker="BJRI", report_date=date(2026, 7, 30), eps_actual=0.94,
        eps_estimate=0.90, surprise_pct=4.95)})
    rows = [{"symbol": "BJRI", "date": "2026-07-30", "epsActual": 0.94,
             "epsEstimate": 0.9085}]

    yahoo = build_prints(rows, CONFIG, bars=[],
                         numbers_source=numbers)[0]["yahoo"]

    assert yahoo["eps_surprise_pct"] == pytest.approx(4.95)


def test_a_name_yahoo_has_nothing_for_is_reported_as_unconfirmed():
    """Not the same as agreement — the page must not let a failed lookup
    read as a second source confirming the calendar."""
    view = build_prints(_aapl_rows(), CONFIG, bars=[],
                        numbers_source=_StubNumbers({}))[0]

    assert view["yahoo"]["verified"] is False
    assert view["yahoo"]["reason"]
    assert "eps_surprise_pct" not in view["yahoo"]


def test_without_a_numbers_source_the_view_is_unchanged():
    view = build_prints(_aapl_rows(), CONFIG, bars=[])[0]
    assert "yahoo" not in view


def test_a_scheduled_print_is_not_looked_up():
    """A future date has no reported actual on either side; asking Yahoo
    about it would spend a call to learn nothing."""
    numbers = _StubNumbers({})
    future = (date.today() + timedelta(days=30)).isoformat()
    rows = [{"symbol": "AAPL", "date": future, "epsEstimate": 2.01}]

    view = build_prints(rows, CONFIG, bars=[], numbers_source=numbers)[0]

    assert view["scheduled"] is True
    assert numbers.asked == []


# --- plumbing ---------------------------------------------------------------

def test_not_a_number_never_reaches_the_page_as_bare_nan():
    """Yahoo's rows carry NaN for a print that has not reported yet.
    `json.dumps` writes that out as a bare `NaN`, which `JSON.parse` rejects
    — so one of them anywhere in the payload blanks the entire page rather
    than one cell (2026-08-02). It has to travel as null."""
    import json

    rendered = jsonable({"rows": [{"Reported EPS": float("nan"),
                                   "Surprise(%)": float("nan"),
                                   "EPS Estimate": 0.14},
                                  {"big": float("inf")}]})

    assert rendered["rows"][0]["Reported EPS"] is None
    assert rendered["rows"][0]["EPS Estimate"] == 0.14
    assert rendered["rows"][1]["big"] is None
    # allow_nan=False is what the server serialises with; this is the check
    # that would have caught it.
    assert json.loads(json.dumps(rendered, allow_nan=False)) == rendered


def test_the_captured_yahoo_rows_survive_serialisation():
    """The rows are captured raw and unfiltered on purpose, so the guard has
    to sit in the serialiser, not in the capture."""
    import json

    from debug.probe import _RecordingYahooEarnings

    source = _RecordingYahooEarnings()
    source.raw_rows = [{"Earnings Date": "2026-10-29",
                        "EPS Estimate": 0.14,
                        "Reported EPS": float("nan"),
                        "Surprise(%)": float("nan")}]

    body = json.dumps(jsonable({"yahoo_rows": source.raw_rows}),
                      allow_nan=False)

    assert json.loads(body)["yahoo_rows"][0]["Reported EPS"] is None


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
