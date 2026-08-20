"""The full-year yardstick when the feed's own year reference is a step behind.

EarningsWhispers states the company's fiscal year twice in one response — in
the prose (`for the year ending June 30, 2027`) and in the structured
`fY1Ref` field — and `read_consensus` refuses the yardstick when the two
disagree, because a yardstick from the wrong year reads downstream as a
guidance beat rather than as an error (ADM: +348% against a quarter's bar).

`fY1Ref` does not turn over the instant a company reports. It keeps naming the
year that just CLOSED for some days afterwards, so the prose is a year ahead
of it and the refusal fires on a company that stated a perfectly good
yardstick. Measured 2026-08-19 over four June-year companies: JKHY, read the
day after its print, still carried `fY1Ref=2026-06-30` against prose naming
2027; AIT, COHR and HRB, read 6-8 days after theirs, had all turned over.
Hawkeye scans within a day or two of the print, so it meets the stale value
more often than not (T-021).

One step forward is therefore accepted. Two is not — that is no longer a feed
that has not turned over, and the refusal it exists for still applies.

Offline: JKHY and AAON are responses recorded live on 2026-08-19/20 into
`tests/fixtures/whispers_year_end/`. JKHY had to be captured that day; once
the feed turns over, the defect cannot be reproduced from the live API at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hawkeye.marketdata.whispers import parse_details, read_consensus

YEAR_END = Path(__file__).parent / "fixtures" / "whispers_year_end"


def payload(ticker: str) -> dict:
    return json.loads((YEAR_END / f"{ticker}.json").read_text(
        encoding="utf-8", errors="replace"))


def record(ticker: str):
    return parse_details(payload(ticker))


def test_a_year_reference_one_step_behind_the_prose_still_yields_the_yardstick():
    """T-021 completion criterion 1 — the defect, on the response that had it.

    JKHY reported its June 2026 fourth quarter on 2026-08-18. Read on
    2026-08-19 the feed still answered `fY1Ref=2026-06-30` while its own prose
    named the year ending June 30, 2027, and the whole full-year consensus
    ($7.11 on $2.68B) was discarded — so the scan reported "no full-year
    consensus available" for a company that had one.
    """
    rec = record("JKHY")
    assert rec.year_end.year == 2026 and rec.year_end.month == 6, (
        "fixture no longer carries the stale reference the case is about")
    out = read_consensus(rec)
    assert out.full_year_period == "FY2027"
    assert out.full_year_eps == pytest.approx(7.11)
    assert out.full_year_revenue == pytest.approx(2_680_000_000.0)
    assert out.reason == ""


def test_a_year_reference_that_agrees_with_the_prose_is_untouched():
    """T-021 completion criterion 2 — the ordinary case does not move.

    AAON's second quarter of a December fiscal year: `fY1Ref=2026-12-31` and
    prose naming the year ending December 31, 2026. It was accepted before
    this change and has to stay accepted, on the same year label.
    """
    rec = record("AAON")
    assert rec.year_end.year == 2026 and rec.year_end.month == 12
    out = read_consensus(rec)
    assert out.full_year_period == "FY2026"
    assert out.full_year_revenue == pytest.approx(2_070_000_000.0)
    assert out.reason == ""


def test_a_prose_year_two_steps_ahead_of_the_reference_is_still_refused():
    """T-021 completion criterion 3 — the safety catch is loosened by one
    step and no further. Two years apart is not a feed lagging behind a
    print; it is the disagreement the refusal exists for."""
    body = payload("JKHY")
    body["summary"] = body["summary"].replace(
        "for the year ending June 30, 2027",
        "for the year ending June 30, 2028")
    out = read_consensus(parse_details(body))
    assert out.full_year_eps is None and out.full_year_revenue is None
    assert out.reason == "full_year_period_disputed"


def test_a_prose_year_behind_the_reference_is_still_refused():
    """A year already closed is not a lagging reference in either direction —
    the feed never points forward — so it stays refused."""
    body = payload("AAON")
    body["summary"] = body["summary"].replace(
        "for the year ending December 31, 2026",
        "for the year ending December 31, 2025")
    out = read_consensus(parse_details(body))
    assert out.full_year_eps is None and out.full_year_revenue is None
    assert out.reason == "full_year_period_disputed"


def test_a_different_month_end_is_refused_even_one_year_apart():
    """The step forward is a YEAR, not a whole new fiscal calendar. A company
    whose year ends in June does not start ending it in March, so a prose
    month that differs from the reference's is a disagreement about which
    company's year is being named — refused, one year apart or not."""
    body = payload("JKHY")
    body["summary"] = body["summary"].replace(
        "for the year ending June 30, 2027",
        "for the year ending March 31, 2027")
    out = read_consensus(parse_details(body))
    assert out.full_year_eps is None and out.full_year_revenue is None
    assert out.reason == "full_year_period_disputed"
