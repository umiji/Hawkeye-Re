"""EDGAR XBRL as the free, deterministic check on everything else (§5.3(5)).

Its job is narrow and it matters: an extraction from a press release is
accepted only if the GAAP EPS it produced equals the value SEC holds. The
most likely way a reader — human or model — misreads an earnings release is
taking the year-ago column, and that failure is caught here for free, because
last year's figure can never equal this quarter's XBRL value.

Where XBRL is absent (26% of the measured sample: banks, insurers, REITs, and
annual periods) the answer is `None`, and `None` means unverified. It never
means zero and never means agreement.
"""
from __future__ import annotations

from datetime import date

from hawkeye.marketdata.edgar_facts import (
    EdgarFacts,
    extraction_matches_filing,
)


def amzn_eps_response() -> dict:
    return {"cik": 1018724, "tag": "EarningsPerShareDiluted",
            "units": {"USD/shares": [
                # the year-ago quarter
                {"start": "2025-04-01", "end": "2025-06-30", "val": 1.26,
                 "form": "10-Q", "filed": "2025-08-01", "fy": 2025, "fp": "Q2"},
                # the six-month total: same period end, wrong duration
                {"start": "2026-01-01", "end": "2026-06-30", "val": 8.42,
                 "form": "10-Q", "filed": "2026-07-31", "fy": 2026, "fp": "Q2"},
                # the quarter itself
                {"start": "2026-04-01", "end": "2026-06-30", "val": 5.75,
                 "form": "10-Q", "filed": "2026-07-31", "fy": 2026, "fp": "Q2"},
                # a later quarter, not yet reported at the date we ask about
                {"start": "2026-07-01", "end": "2026-09-30", "val": 2.10,
                 "form": "10-Q", "filed": "2026-10-30", "fy": 2026, "fp": "Q3"},
            ]}}


def facts(response) -> EdgarFacts:
    return EdgarFacts(fetcher=lambda cik, tag: response)


def test_the_quarter_wins_over_the_year_to_date_figure_with_the_same_end():
    """Both cover a period ending 2026-06-30. Taking the six-month total
    would read as a 360% beat against a quarterly consensus."""
    got = facts(amzn_eps_response()).quarterly(
        "0001018724", "EarningsPerShareDiluted", date(2026, 7, 31))

    assert got is not None and got.value == 5.75
    assert got.period_start == date(2026, 4, 1)


def test_a_quarter_that_had_not_been_reported_yet_is_not_used():
    got = facts(amzn_eps_response()).quarterly(
        "0001018724", "EarningsPerShareDiluted", date(2026, 7, 31))
    assert got.period_end == date(2026, 6, 30)


def test_a_missing_concept_is_unverified_not_zero():
    """Banks, insurers and REITs frequently have no such tag — 26% of the
    measured sample. That is a coverage limit, not a value."""
    empty = EdgarFacts(fetcher=lambda cik, tag: {"units": {}})
    assert empty.quarterly("0001018724", "Revenues", date(2026, 7, 31)) is None


def test_an_unreachable_edgar_is_unverified_too():
    def broken(cik, tag):
        raise OSError("network down")

    assert EdgarFacts(fetcher=broken).quarterly(
        "0001018724", "EarningsPerShareDiluted", date(2026, 7, 31)) is None


def test_the_fetch_is_cached_per_company_and_tag():
    calls = []

    def fetcher(cik, tag):
        calls.append((cik, tag))
        return amzn_eps_response()

    edgar = EdgarFacts(fetcher=fetcher)
    edgar.quarterly("0001018724", "EarningsPerShareDiluted", date(2026, 7, 31))
    edgar.quarterly("0001018724", "EarningsPerShareDiluted", date(2026, 4, 30))
    assert len(calls) == 1


# --- the anti-hallucination guard ------------------------------------------

def test_an_extraction_matching_the_filing_is_accepted():
    assert extraction_matches_filing(5.75, 5.75) is True
    assert extraction_matches_filing(5.7500, 5.75) is True


def test_reading_the_year_ago_column_is_rejected():
    assert extraction_matches_filing(1.26, 5.75) is False


def test_an_extraction_cannot_be_accepted_without_a_filing_value():
    """No XBRL means the extraction stays unverified. Treating "nothing to
    compare against" as a pass is the exact failure invariant 6 forbids."""
    assert extraction_matches_filing(5.75, None) is False
