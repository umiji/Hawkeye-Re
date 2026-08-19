"""One release, several outlooks (T-020).

A company routinely publishes TWO forward statements in the same release —
next quarter and the full year — and until this task the record held one of
them. `GuidanceReading` carried a single `period`, the reader was never told
which to pick when both were there, and the one it did not pick was not
recorded anywhere. On 2026-08-19 that decided a case: AS guided next-quarter
EPS 17.9% BELOW consensus and full-year EPS 4.5% ABOVE it, having RAISED the
full-year range from its own previous one. Only the quarter was kept; it
became the Adversary's leading attack and the Judge's stated reason to pass.

The pattern is not rare. Of the 47 vendor summaries kept as fixtures, 21
state a company outlook and 5 of those state both a quarterly and a full-year
range (AGNT, AKA, ALGT, ALIT, AME — measured 2026-08-19).

AME is the case used throughout here because it is the AS shape in real
recorded text: the quarter below consensus, the year above it.

Two rules are pinned, and they are separate:

- **Every period the company gave is read and recorded**, and a period the
  gate refuses is recorded as a refusal rather than dropped. What the old
  code did — keep one, silently discard the rest — is the defect itself.
- **The worst period governs the score** (User decision, 2026-08-19). A
  period is judged by the existing within-period rule first (EPS up and
  sales down still nets to inline), then the worst of the periods decides,
  so adding a flattering period can never raise the score.
"""
from __future__ import annotations

import json
from datetime import date

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    GuidanceReading,
    PrintSource,
    SnapshotKind,
)
from hawkeye.scout.guidance_agent import GuidanceRequest, build_schema, parse_reply
from hawkeye.scout.quality import LegStatus, assess_earnings

CONFIG = HawkeyeConfig()

# AME's real summary, verbatim from tests/fixtures/whispers/AME.json. Both
# ranges sit in one sentence: "third quarter earnings of $1.85 to $1.87 per
# share AND 2026 earnings of $8.20 to $8.30 per share".
AME_SUMMARY = (
    "AMETEK (AME) reported earnings of $2.09 per share on revenue of $2.04 "
    "billion for the  second quarter ended June 2026.  The consensus earnings "
    "estimate was $1.99 per share on  revenue of $1.96 billion. The company "
    "beat consensus estimates by 5.03% while revenue grew 14.98% on a "
    "year-over-year basis.<br /><br />The company  said it expects  third "
    "quarter  earnings of $1.85 to $1.87 per share and 2026 earnings of $8.20 "
    "to $8.30 per share.The company's rpevious guidance was 2026 earnings of "
    "$7.94 to $8.14 per share. The current consensus estimate is earnings of "
    "$2.04 per share for the quarter ending September 30, 2026 and earnings of "
    "$8.14 per share for the year ending December 31, 2026.")

AME_QUARTER_QUOTE = "third quarter  earnings of $1.85 to $1.87 per share"
AME_YEAR_QUOTE = "2026 earnings of $8.20 to $8.30 per share"


def a_request(**kw) -> GuidanceRequest:
    base = dict(ticker="AME", fiscal_quarter="2026-Q2",
                next_quarter="2026-Q3", summary=AME_SUMMARY)
    base.update(kw)
    return GuidanceRequest(**base)


def a_reply(*periods, guided=True) -> dict:
    return {"guided": guided, "periods": list(periods)}


def quarter_entry(**kw) -> dict:
    base = dict(period="2026-Q3", eps_low=1.85, eps_high=1.87,
                quote=AME_QUARTER_QUOTE)
    base.update(kw)
    return base


def year_entry(**kw) -> dict:
    base = dict(period="FY2026", eps_low=8.20, eps_high=8.30,
                quote=AME_YEAR_QUOTE)
    base.update(kw)
    return base


def a_print(**overrides) -> EarningsPrint:
    base = dict(stock_id="cik:0001037868", ticker="AME",
                fiscal_quarter="2026-Q2", report_date=date(2026, 8, 4),
                source=PrintSource.WHISPERS,
                eps_actual=2.09, eps_actual_rows=[2.09], revenue_actual=2.044e9)
    base.update(overrides)
    return EarningsPrint(**base)


def a_consensus(**overrides) -> ConsensusSnapshot:
    """AME's own yardsticks, as the same summary states them: $2.04 for the
    quarter ending September 2026 and $8.14 for the year."""
    base = dict(stock_id="cik:0001037868", ticker="AME",
                fiscal_quarter="2026-Q2", kind=SnapshotKind.PRE_REGISTERED,
                eps_avg=1.99, eps_calendar=1.99, eps_analysts=12,
                revenue_avg=1.96e9, revenue_calendar=1.96e9,
                next_quarter_eps_avg=2.04, full_year_eps_avg=8.14,
                full_year_period="FY2026")
    base.update(overrides)
    return ConsensusSnapshot(**base)


# --- the reading: every period the company gave -----------------------------

def test_both_periods_in_one_release_are_read_and_kept():
    """The defect in one line. AME guided the quarter and the year in the same
    sentence; the old shape could hold one of them."""
    out = parse_reply(a_reply(quarter_entry(), year_entry()), a_request(),
                      model="m")

    assert out.reason == ""
    assert [r.period for r in out.readings] == ["2026-Q3", "FY2026"]
    assert out.readings[0].eps_low == 1.85
    assert out.readings[1].eps_high == 8.30


def test_each_period_carries_the_sentence_it_was_read_from():
    """One excerpt for the whole reply would attribute both ranges to whichever
    sentence happened to be quoted first."""
    out = parse_reply(a_reply(quarter_entry(), year_entry()), a_request(),
                      model="m")

    assert out.readings[0].source_excerpt == AME_QUARTER_QUOTE
    assert out.readings[1].source_excerpt == AME_YEAR_QUOTE


def test_a_refused_period_does_not_void_the_period_beside_it():
    """A quote that is not in the summary voids ITS OWN period. Voiding the
    whole reply would throw away a reading that passed every check."""
    out = parse_reply(
        a_reply(quarter_entry(),
                year_entry(quote="2026 earnings of $9.00 to $9.50 per share")),
        a_request(), model="m")

    assert [r.period for r in out.readings] == ["2026-Q3"]
    assert out.reason == ""
    assert out.refusals == ("quote_not_in_source",)


def test_every_refused_period_is_recorded_rather_than_dropped():
    """The prohibition this task was written under: a period nobody kept has
    to leave a trace, or the record cannot say a second one was ever there."""
    out = parse_reply(
        a_reply(quarter_entry(period="2027-Q1"),
                year_entry(quote="2026 earnings of $9.00 to $9.50 per share")),
        a_request(), model="m")

    assert out.readings == ()
    assert out.refusals == ("period_not_next_quarter", "quote_not_in_source")


def test_when_every_period_is_refused_the_first_reason_is_the_row_reason():
    """A print row still needs ONE reason to render, and it stays the reason a
    single-period reply would have produced."""
    out = parse_reply(a_reply(quarter_entry(open_ended=True)), a_request(),
                      model="m")

    assert out.readings == ()
    assert out.reason == "open_ended_range"


def test_a_period_reported_twice_is_refused_the_second_time():
    """Two entries for one period would count the same statement twice in the
    scoring, and nothing downstream could tell they were the same."""
    out = parse_reply(a_reply(year_entry(), year_entry(eps_low=8.00)),
                      a_request(), model="m")

    assert [r.period for r in out.readings] == ["FY2026"]
    assert out.refusals == ("duplicate_period",)


def test_no_outlook_at_all_is_still_the_ordinary_answer():
    out = parse_reply(a_reply(guided=False), a_request(), model="m")

    assert out.readings == ()
    assert out.reason == "no_guidance_in_source"


def test_the_reply_shape_asks_for_a_list_of_periods():
    """The schema is what actually constrains the reader — prose asking for
    "all of them" beside a single-period shape would be a request the shape
    refuses to carry."""
    schema = build_schema()

    assert schema["properties"]["periods"]["type"] == "array"
    entry = schema["properties"]["periods"]["items"]
    assert entry["required"] == ["period", "quote"]
    assert entry["additionalProperties"] is False


# --- what the record holds --------------------------------------------------

def test_a_print_holds_every_reading_it_was_given():
    row = a_print(guidance_readings=[
        GuidanceReading(period="2026-Q3", eps_low=1.85, eps_high=1.87),
        GuidanceReading(period="FY2026", eps_low=8.20, eps_high=8.30)])

    assert [g.period for g in row.guidance_readings] == ["2026-Q3", "FY2026"]


def test_a_row_recorded_before_this_task_still_reads():
    """Every guidance already in the ledger was written as a single `guidance`
    object. A shape that could not read those rows would make the existing
    ledger unreadable, which is this task's stop condition."""
    legacy = json.dumps({
        "stock_id": "cik:0001037868", "ticker": "AME",
        "fiscal_quarter": "2026-Q2", "report_date": "2026-08-04",
        "source": "whispers",
        "guidance": {"period": "2026-Q3", "eps_low": 1.85, "eps_high": 1.87,
                     "source_excerpt": AME_QUARTER_QUOTE,
                     "extractor": "agent", "extractor_model": "claude-opus-5"},
        "guidance_reason": ""})

    row = EarningsPrint.model_validate_json(legacy)

    assert [g.period for g in row.guidance_readings] == ["2026-Q3"]
    assert row.guidance_readings[0].extractor_model == "claude-opus-5"


def test_the_refused_periods_are_kept_on_the_row():
    row = a_print(guidance_readings=[GuidanceReading(period="FY2026",
                                                     eps_low=8.2, eps_high=8.3)],
                  guidance_refusals=["quote_not_in_source"])

    assert row.guidance_refusals == ["quote_not_in_source"]


# --- the scoring: the worst period governs ----------------------------------

def test_the_worst_period_decides_and_the_better_one_cannot_lift_it():
    """AME guided the quarter 8.8% BELOW consensus and the year 1.4% above it.
    The quarter governs: a company cannot buy its way out of a shortfall by
    also publishing a year that clears the bar."""
    quality = assess_earnings(
        a_print(guidance_readings=[
            GuidanceReading(period="2026-Q3", eps_low=1.85, eps_high=1.87),
            GuidanceReading(period="FY2026", eps_low=8.20, eps_high=8.30)]),
        a_consensus(), CONFIG)

    assert quality.guidance.status is LegStatus.MISS
    assert quality.guidance.period == "2026-Q3"
    assert round(quality.guidance.surprise_pct, 1) == -8.8
    assert quality.breakdown.guidance == -CONFIG.guidance_miss_penalty


def test_the_period_that_did_not_govern_is_still_on_the_verdict():
    """Recording it is half the fix; the reader has to be able to SEE it, or
    the full-year raise is invisible exactly where it matters."""
    quality = assess_earnings(
        a_print(guidance_readings=[
            GuidanceReading(period="2026-Q3", eps_low=1.85, eps_high=1.87),
            GuidanceReading(period="FY2026", eps_low=8.20, eps_high=8.30)]),
        a_consensus(), CONFIG)

    periods = {p.period: p for p in quality.guidance.periods}
    assert set(periods) == {"2026-Q3", "FY2026"}
    assert periods["FY2026"].status is LegStatus.BEAT
    assert round(periods["FY2026"].surprise_pct, 1) == 1.4
    assert round(periods["2026-Q3"].surprise_pct, 1) == -8.8


def test_two_beating_periods_still_beat():
    quality = assess_earnings(
        a_print(guidance_readings=[
            GuidanceReading(period="2026-Q3", eps_low=2.20, eps_high=2.30),
            GuidanceReading(period="FY2026", eps_low=8.20, eps_high=8.30)]),
        a_consensus(), CONFIG)

    assert quality.guidance.status is LegStatus.BEAT
    assert quality.breakdown.guidance == CONFIG.guidance_beat_score


def test_a_period_with_no_yardstick_is_flagged_and_the_other_still_scores():
    """A full-year range whose consensus names another year cannot be compared
    (ADM's version of that read as a +348% beat). Refusing it must not take the
    quarter down with it."""
    quality = assess_earnings(
        a_print(guidance_readings=[
            GuidanceReading(period="2026-Q3", eps_low=2.20, eps_high=2.30),
            GuidanceReading(period="FY2026", eps_low=8.20, eps_high=8.30)]),
        a_consensus(full_year_period="FY2027"), CONFIG)

    assert quality.guidance.status is LegStatus.BEAT
    assert quality.guidance.period == "2026-Q3"
    refused = [p for p in quality.guidance.periods if p.period == "FY2026"]
    assert refused and refused[0].status is LegStatus.ABSENT
    assert "full_year_consensus_is_another_year" in refused[0].flags


def test_a_qualified_period_is_declined_on_its_own_terms():
    """The condition fences that period's range, not the company's other one."""
    quality = assess_earnings(
        a_print(guidance_readings=[
            GuidanceReading(period="2026-Q3", eps_low=2.20, eps_high=2.30),
            GuidanceReading(period="FY2026", eps_low=8.20, eps_high=8.30,
                            qualifier="excluding its barge business")]),
        a_consensus(), CONFIG)

    assert quality.guidance.status is LegStatus.BEAT
    declined = [p for p in quality.guidance.periods if p.period == "FY2026"]
    assert "guidance_scope_qualified" in declined[0].flags


def test_a_single_period_reading_is_judged_exactly_as_before():
    """The regression that matters most: 20 of the 21 measured summaries state
    one range, and their score must not move because a second slot exists."""
    quality = assess_earnings(
        a_print(guidance_readings=[GuidanceReading(period="2026-Q3",
                                                   eps_low=1.85,
                                                   eps_high=1.87)]),
        a_consensus(), CONFIG)

    assert quality.guidance.status is LegStatus.MISS
    assert round(quality.guidance.surprise_pct, 1) == -8.8
    assert quality.breakdown.guidance == -CONFIG.guidance_miss_penalty


def test_no_outlook_is_still_free():
    quality = assess_earnings(a_print(), a_consensus(), CONFIG)

    assert quality.guidance.status is LegStatus.ABSENT
    assert quality.breakdown.guidance == 0.0


# --- what the reader and the tribunal are shown -----------------------------

def test_the_tribunal_is_shown_every_period_the_company_guided():
    """The harm on 2026-08-19 was not only the score: the three roles argued
    the case having been shown one of the two statements the company made."""
    from hawkeye.scout.quality import describe_quality_en

    text = describe_quality_en(assess_earnings(
        a_print(guidance_readings=[
            GuidanceReading(period="2026-Q3", eps_low=1.85, eps_high=1.87),
            GuidanceReading(period="FY2026", eps_low=8.20, eps_high=8.30)]),
        a_consensus(), CONFIG))

    assert "2026-Q3" in text and "FY2026" in text
    assert "-8.8%" in text and "+1.4%" in text


def test_the_japanese_report_shows_every_period_with_its_own_difference():
    from hawkeye.reports.quality_ja import render_quality_ja

    text = render_quality_ja(assess_earnings(
        a_print(guidance_readings=[
            GuidanceReading(period="2026-Q3", eps_low=1.85, eps_high=1.87),
            GuidanceReading(period="FY2026", eps_low=8.20, eps_high=8.30)]),
        a_consensus(), CONFIG))

    assert "2026-Q3" in text and "FY2026" in text
    assert "-8.8%" in text and "+1.4%" in text
