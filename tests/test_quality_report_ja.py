"""The Japanese rendering has to carry the warnings, not just the numbers.

Found on the live AMZN dry run (2026-08-02): the判定 printed a +194% EPS beat
with no mention of what was wrong with the figure — the one thing that whole
investigation was about. A warning stored but never displayed is the same as
no warning.

One of those warnings is gone rather than moved. "This headline beat is GAAP
and may contain a one-off" could only ever be known by reading the company's
own release, and the scan no longer does (tests/test_removed_escalations.py).
What survives for AMZN is the warning that its two vendor rows contradict each
other, which is what actually makes the +194% unusable.
"""
from __future__ import annotations

from datetime import date

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    PrintSource,
)
from hawkeye.reports.quality_ja import render_quality_ja
from hawkeye.scout.quality import assess_earnings

CONFIG = HawkeyeConfig()


def amzn_like():
    """AMZN's real shape: the feed's actual, two contradictory calendar rows,
    and a huge headline beat."""
    return assess_earnings(
        EarningsPrint(stock_id="cik:0001018724", ticker="AMZN",
                      fiscal_quarter="2026-Q2",
                      report_date=date(2026, 7, 30),
                      source=PrintSource.WHISPERS, eps_actual=5.75,
                      eps_actual_rows=[1.88, 1.97]),
        ConsensusSnapshot(stock_id="cik:0001018724", ticker="AMZN",
                          fiscal_quarter="2026-Q2", eps_avg=1.956,
                          eps_calendar=1.94, eps_analysts=44),
        CONFIG)


def aapl_like():
    """AAPL's real shape: both vendors have a usable actual and they differ
    ($2.02 against $1.91, a tariff-refund one-off)."""
    return assess_earnings(
        EarningsPrint(stock_id="cik:0000320193", ticker="AAPL",
                      fiscal_quarter="2026-Q3",
                      report_date=date(2026, 7, 31),
                      source=PrintSource.WHISPERS, eps_actual=2.02,
                      eps_actual_rows=[1.91]),
        ConsensusSnapshot(stock_id="cik:0000320193", ticker="AAPL",
                          fiscal_quarter="2026-Q3", eps_avg=1.89,
                          eps_calendar=1.89, eps_analysts=28),
        CONFIG)


def test_the_contradictory_source_is_named_in_plain_language():
    text = render_quality_ja(amzn_like())

    assert "Finnhub" in text
    assert "使用不能" in text
    assert "finnhub_actual_conflict" not in text      # never a bare identifier


def test_the_reader_is_told_which_vendor_the_percentage_was_built_from():
    """Every percentage is one vendor's actual over that same vendor's
    consensus, so which vendor it was IS part of what the number means. A
    page that omits it makes two incomparable readings look identical."""
    text = render_quality_ja(amzn_like())

    assert "決算専門サイトの実績と予想" in text
    assert "whispers" not in text                     # never a bare identifier


def test_a_differing_actual_from_the_other_vendor_shows_both_figures():
    """It does not change the reading, but the reader cannot check the work
    without seeing what the two vendors actually reported."""
    text = render_quality_ja(aapl_like())

    assert "2.02" in text and "1.91" in text
    assert "判定に使用" in text
    assert "vendors_report_different_actuals" not in text


def test_the_headline_number_is_shown_next_to_the_analyst_count():
    text = render_quality_ja(amzn_like())

    assert "+194" in text or "+193" in text
    assert "アナリスト44人" in text


def aca_like():
    """ACA's real shape: guidance fenced with a condition the analysts' figure
    for the same year does not share."""
    from hawkeye.contracts.stocks import GuidanceReading
    return assess_earnings(
        EarningsPrint(stock_id="cik:0001739445", ticker="ACA",
                      fiscal_quarter="2026-Q1",
                      report_date=date(2026, 8, 1),
                      source=PrintSource.WHISPERS, eps_actual=1.20,
                      eps_actual_rows=[1.20], revenue_actual=6.5e8,
                      guidance=GuidanceReading(
                          period="FY2026", revenue_low=2.60e9,
                          revenue_high=2.70e9,
                          qualifier="excluding its barge business")),
        ConsensusSnapshot(stock_id="cik:0001739445", ticker="ACA",
                          fiscal_quarter="2026-Q1", eps_avg=1.00,
                          eps_calendar=1.00, eps_analysts=9,
                          revenue_avg=6.0e8, revenue_analysts=9,
                          full_year_revenue_avg=3.02e9,
                          full_year_period="FY2026"),
        CONFIG)


def test_a_refused_guidance_says_so_in_words_and_quotes_the_condition():
    """The count of refused comparisons is what tells us whether this rule is
    too blunt. A reader who cannot see the phrase cannot judge that."""
    text = render_quality_ja(aca_like())

    assert "条件" in text
    assert "excluding its barge business" in text
    assert "guidance_scope_qualified" not in text     # never a bare identifier
