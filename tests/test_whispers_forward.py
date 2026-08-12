"""The earnings feed's BEFORE-the-print endpoint (`/api/getstocksdata/`).

This is the half of the feed pre-registration needs. `/api/epsdetails/` states
what a company just reported; this one states what analysts expect of the
print still ahead of it — which is the only figure that can be recorded before
a release, and therefore the only one a pre-registration can hold.

It replaced Yahoo (`YahooConsensusSource`) for that job on 2026-08-09. What
that bought: the consensus a print is judged against and the actual it is
judged with now come from the same vendor, so the surprise ratio stops being
one vendor's numerator over another's denominator. What it cost: the analyst
count and the estimate range, which this endpoint does not publish and which
were already decided not to be used.

The trap it introduces is the units. `revenueEst` here is in DOLLARS while
`revenueEstimate` on the after-the-print endpoint is in MILLIONS. Same
quantity, near-identical name, a factor of a million apart — so the two are
converted in different places on purpose, and both are pinned by a test.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from hawkeye.marketdata.whispers import (
    WhispersSource,
    WhispersUnavailable,
    parse_stocksdata,
)

FIXTURES = Path(__file__).parent / "fixtures" / "whispers_forward"
ET = ZoneInfo("America/New_York")


def payload(ticker: str) -> dict:
    return json.loads((FIXTURES / f"{ticker}.json").read_text(encoding="utf-8"))


def forecast(ticker: str):
    return parse_stocksdata(payload(ticker))


# -- the numbers -----------------------------------------------------------

def test_the_revenue_estimate_is_already_in_dollars_and_is_not_scaled():
    """The after-the-print endpoint publishes millions and this one publishes
    dollars. Applying that conversion here would turn NVDA's $90.72bn quarter
    into $90.72 quadrillion, and every later comparison against it into an
    infinite beat."""
    assert forecast("NVDA").revenue_estimate == pytest.approx(90_720_000_000.0)


def test_the_eps_estimate_is_taken_as_published():
    assert forecast("NVDA").eps_estimate == pytest.approx(2.01)


def test_the_whisper_number_is_carried_beside_the_consensus():
    """Kept apart, never merged: a whisper is not a consensus, and the feed's
    own surprise figures are measured against whichever of the two exists."""
    out = forecast("ONON")
    assert out.eps_estimate == pytest.approx(0.38)
    assert out.whisper == pytest.approx(0.46)


def test_a_company_with_no_whisper_number_reads_as_absent_not_as_zero():
    assert forecast("LCUT").whisper is None
    assert forecast("LCUT").eps_estimate == pytest.approx(0.23)


# -- the schedule ----------------------------------------------------------

def test_the_next_report_date_is_read():
    assert forecast("ONON").next_report_date == date(2026, 8, 11)


def test_a_confirmed_date_is_distinguished_from_a_projected_one():
    """`confirmDate` is the moment the company itself announced when it would
    report. Without one the date is the feed's own projection, and a
    pre-registration filed against a projected date can miss the print."""
    assert forecast("ONON").confirmed_at == datetime(
        2026, 7, 28, 16, 30, 50, 933000, tzinfo=ET)
    assert forecast("LCUT").confirmed_at is None


def test_the_quarter_end_is_read_but_no_fiscal_label_is_derived_from_it():
    """The payload states the quarter's END and its NUMBER but not the
    company's fiscal year end, and those two alone cannot name the quarter:
    NVDA's quarter ending July 2026 is its fiscal 2027 Q2, and reading the
    year off the end date would file it under 2026. The label keeps coming
    from the calendar, which states the fiscal year outright."""
    assert forecast("NVDA").quarter_end == date(2026, 7, 31)
    assert not hasattr(forecast("NVDA"), "fiscal_quarter")


def test_the_quarter_number_the_feed_states_is_kept():
    """`quarter` was read and thrown away until 2026-08-12. It is the feed's
    own statement of WHICH print its consensus is for, and discarding it left
    nothing to check the calendar's label against — which is how twenty rows
    came to hold the next quarter's estimate under this quarter's label."""
    assert forecast("NVDA").quarter_number == 2
    assert forecast("LCUT").quarter_number == 3
    # No entry at all, so nothing is stated rather than "quarter zero".
    assert forecast("AATC").quarter_number is None


# -- absence ---------------------------------------------------------------

def test_a_company_nobody_published_an_estimate_for_is_empty_not_wrong():
    out = forecast("AATC")
    assert out.eps_estimate is None and out.revenue_estimate is None
    assert out.is_empty


def test_a_forecast_with_a_figure_in_it_is_not_empty():
    assert not forecast("LCUT").is_empty


def test_a_response_that_is_not_an_object_is_a_broken_feed_not_an_empty_one():
    with pytest.raises(WhispersUnavailable):
        parse_stocksdata([1, 2, 3])


# -- the live reader -------------------------------------------------------

def _source(handler) -> WhispersSource:
    return WhispersSource(transport=httpx.MockTransport(handler),
                          server_error_retries=0, connection_retries=0,
                          sleep=lambda _: None)


def test_the_forward_reader_asks_the_pre_print_endpoint():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["referer"] = request.headers.get("referer")
        return httpx.Response(200, json=payload("ONON"))

    out = _source(handler).forecast("onon")

    assert seen["url"].endswith("/api/getstocksdata/ONON")
    assert seen["referer"].endswith("/stocks/ONON")
    assert out.eps_estimate == pytest.approx(0.38)


def test_a_company_the_feed_has_no_row_for_reads_as_none():
    out = _source(lambda r: httpx.Response(204)).forecast("ZZZZ")
    assert out is None


def test_a_feed_that_answers_html_raises_rather_than_returning_nothing():
    """The site serves the HTML shell to a request it does not recognise.
    Parsed as "no data" it would read as a fact about the company; it is a
    fact about our request (invariant 6)."""
    html = httpx.Response(200, text="<!DOCTYPE html><html></html>",
                          headers={"content-type": "text/html"})
    with pytest.raises(WhispersUnavailable):
        _source(lambda r: html).forecast("ONON")
