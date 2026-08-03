"""The Japanese rendering has to carry the warnings, not just the numbers.

Found on the live AMZN dry run (2026-08-02): the判定 printed a +194% EPS beat
with no mention that the figure was unadjusted — the one thing that whole
investigation was about. A warning stored but never displayed is the same as
no warning.
"""
from __future__ import annotations

from datetime import date

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    EpsBasis,
    PrintDepth,
)
from hawkeye.reports.quality_ja import render_quality_ja
from hawkeye.scout.quality import assess_earnings

CONFIG = HawkeyeConfig()


def amzn_like():
    """AMZN's real shape: one usable actual, two contradictory Finnhub rows,
    a huge headline beat, and no quantified one-off."""
    return assess_earnings(
        EarningsPrint(stock_id="cik:0001018724", ticker="AMZN",
                      fiscal_quarter="2026-Q2",
                      report_date=date(2026, 7, 30),
                      depth=PrintDepth.XBRL_VALIDATED, eps_yahoo=5.75,
                      eps_finnhub=[1.88, 1.97], eps_xbrl_diluted=5.75,
                      eps_basis=EpsBasis.UNADJUSTED),
        ConsensusSnapshot(stock_id="cik:0001018724", ticker="AMZN",
                          fiscal_quarter="2026-Q2", eps_avg=1.956,
                          eps_finnhub=1.94, eps_analysts=44),
        CONFIG)


def test_the_unadjusted_warning_is_visible_to_the_reader():
    text = render_quality_ja(amzn_like())

    assert "未調整" in text
    assert "一時要因" in text


def test_the_contradictory_source_is_named_in_plain_language():
    text = render_quality_ja(amzn_like())

    assert "Finnhub" in text
    assert "finnhub_actual_conflict" not in text      # never a bare identifier


def test_the_headline_number_is_shown_next_to_both_readings():
    text = render_quality_ja(amzn_like())

    assert "+194" in text or "+193" in text
    assert "アナリスト44人" in text


def test_the_prints_awaiting_a_release_read_are_named_with_what_to_do():
    """A two-pass workflow only works if the first pass says exactly which
    file the second one will look for. "Some names need a release read" is
    an observation; a path is an instruction."""
    from hawkeye.reports.quality_ja import render_release_requests_ja

    text = render_release_requests_ja(["AAPL_2026-07-31"], "var/releases")

    assert "AAPL" in text
    assert "var/releases" in text and "AAPL_2026-07-31.json" in text
    assert render_release_requests_ja([], "var/releases") == ""
