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
    PrintDepth,
)
from hawkeye.reports.quality_ja import render_quality_ja
from hawkeye.scout.quality import assess_earnings

CONFIG = HawkeyeConfig()


def amzn_like():
    """AMZN's real shape: one usable actual, two contradictory Finnhub rows,
    and a huge headline beat."""
    return assess_earnings(
        EarningsPrint(stock_id="cik:0001018724", ticker="AMZN",
                      fiscal_quarter="2026-Q2",
                      report_date=date(2026, 7, 30),
                      depth=PrintDepth.VERIFIED, eps_yahoo=5.75,
                      eps_finnhub=[1.88, 1.97]),
        ConsensusSnapshot(stock_id="cik:0001018724", ticker="AMZN",
                          fiscal_quarter="2026-Q2", eps_avg=1.956,
                          eps_finnhub=1.94, eps_analysts=44),
        CONFIG)


def test_the_contradictory_source_is_named_in_plain_language():
    text = render_quality_ja(amzn_like())

    assert "Finnhub" in text
    assert "使用不能" in text
    assert "finnhub_actual_conflict" not in text      # never a bare identifier


def test_the_reader_is_told_the_actual_rests_on_one_source():
    """With Finnhub's rows unusable the beat stands on Yahoo alone. That is a
    real weakening of the evidence, so it has to be on the page — otherwise a
    single-source reading and a two-source one look identical."""
    text = render_quality_ja(amzn_like())

    assert "実績値のソースが1つだけ" in text
    assert "single_source_actual" not in text


def test_the_headline_number_is_shown_next_to_both_readings():
    text = render_quality_ja(amzn_like())

    assert "+194" in text or "+193" in text
    assert "アナリスト44人" in text
