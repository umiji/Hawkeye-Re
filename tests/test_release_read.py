"""Reading the company's own release when the two vendors' actuals disagree
(docs/MASTER_OVERVIEW.ja.md §5.3 実装順序(c) — the escalation at the tail of
the funnel).

The measured base rate is 21% (10 of 48 names): the two vendors report a
different EPS actual, the leg goes unverified, and nothing in the system can
lift it again. Only the company's own release settles it, because the
question is not "which number was filed" but "which basis is the consensus
on" — AAPL filed 2.02 while the street compared against 1.91, the difference
being a $0.11 tariff refund the release names and XBRL cannot see.

So the extraction is never trusted on sight. Its GAAP EPS must equal the
value SEC holds for that quarter or the whole document is thrown away, which
catches the commonest misreading (the year-ago column) for free.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import date, timedelta

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    EpsBasis,
    PrintDepth,
    SnapshotKind,
    Stock,
)
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.base import StaticProvider
from hawkeye.marketdata.yahoo_earnings import VerifiedEarnings
from hawkeye.scout.quality import LegStatus, assess_earnings
from hawkeye.scout.release import apply_release, needs_release_read
from hawkeye.scout.scout import run_scout
from tests.conftest import make_bars


def _config() -> HawkeyeConfig:
    return HawkeyeConfig()


def _row(**overrides) -> EarningsPrint:
    """AAPL's shape: Yahoo has the filed GAAP figure, the calendar the
    street's adjusted one, and they are 5.4% / $0.11 apart."""
    base = dict(stock_id="cik:0000320193", ticker="AAPL",
                fiscal_quarter="2026-Q3", report_date=date(2026, 7, 31),
                depth=PrintDepth.VERIFIED, eps_yahoo=2.02, eps_finnhub=[1.91])
    base.update(overrides)
    return EarningsPrint(**base)


def _consensus(**overrides) -> ConsensusSnapshot:
    base = dict(stock_id="cik:0000320193", ticker="AAPL",
                fiscal_quarter="2026-Q3", kind=SnapshotKind.PRE_REGISTERED,
                eps_avg=1.89, eps_finnhub=1.89, eps_analysts=25)
    base.update(overrides)
    return ConsensusSnapshot(**base)


def _extraction(**overrides) -> dict:
    base = {"gaap_eps_diluted": 2.02, "non_gaap_eps": None,
            "one_off_per_share": None, "one_off_description": "",
            "guidance": None, "source_url": "https://www.sec.gov/…"}
    base.update(overrides)
    return base


# --- the guard: an extraction is checked before it is believed -------------

def test_an_extraction_that_disagrees_with_the_filing_is_not_stored():
    """The year-ago column reads like a plausible EPS. Nothing else in the
    pipeline can catch that mistake, so it has to be caught here."""
    row = _row()

    outcome = apply_release(row, _extraction(gaap_eps_diluted=1.40),
                            filed_eps=2.02)

    assert not outcome.accepted
    assert outcome.row.depth is PrintDepth.VERIFIED     # unchanged
    assert outcome.row.eps_release is None
    assert outcome.row.guidance is None
    # The dispute is untouched, which is the point: nothing was settled, so
    # the leg still says the two vendors disagree and still asks for a
    # document. (Since 2026-08-03(b) it is scored on the conservative
    # reading meanwhile rather than being withheld — see
    # `config.earnings_actual_dispute_blocks`.)
    quality = assess_earnings(outcome.row, _consensus(), _config())
    assert "actual_disputed" in quality.eps.flags
    assert needs_release_read(quality)


def test_an_extraction_with_nothing_to_check_it_against_is_not_believed():
    """No XBRL for this filer (26% of the measured sample: banks, insurers,
    REITs). That is a coverage limit, never a pass (invariant 6)."""
    outcome = apply_release(_row(), _extraction(), filed_eps=None)

    assert not outcome.accepted
    assert outcome.row.eps_release is None


# --- what an accepted extraction is allowed to settle ----------------------

def test_a_disclosed_per_share_one_off_settles_the_disputed_actual():
    """Apple's release states the $0.11 tariff refund, and 2.02 − 0.11 is
    exactly the 1.91 the street compared against."""
    outcome = apply_release(
        _row(), _extraction(one_off_per_share=0.11,
                            one_off_description="tariff refunds"),
        filed_eps=2.02)

    assert outcome.accepted
    assert outcome.row.depth is PrintDepth.RELEASE_READ
    assert outcome.row.eps_release == 1.91
    assert outcome.row.eps_basis is EpsBasis.ADJUSTED
    assert outcome.row.eps_xbrl_diluted == 2.02
    quality = assess_earnings(outcome.row, _consensus(), _config())
    assert quality.eps.status is LegStatus.BEAT
    assert "actual_disputed" not in quality.eps.flags


def test_a_published_non_gaap_figure_is_read_rather_than_computed():
    """Where the company publishes its own adjusted number, that number is
    the answer — subtracting a one-off from GAAP ourselves would be computing
    a non-GAAP figure, which §5.3 決定1 forbids."""
    outcome = apply_release(
        _row(), _extraction(non_gaap_eps=1.95, one_off_per_share=0.11),
        filed_eps=2.02)

    assert outcome.row.eps_release == 1.95
    assert outcome.row.eps_basis is EpsBasis.ADJUSTED


def test_a_release_naming_no_adjustment_leaves_the_dispute_open():
    """The rejected shortcut, pinned as a test: XBRL confirms WHICH value was
    filed, never which basis the consensus is on. A release that names no
    adjustment therefore cannot settle the disagreement — but it does record
    that we looked, which is a different fact from never having looked."""
    outcome = apply_release(_row(), _extraction(), filed_eps=2.02)

    assert outcome.accepted
    assert outcome.row.depth is PrintDepth.RELEASE_READ
    assert outcome.row.eps_release is None
    assert outcome.row.eps_basis is EpsBasis.UNADJUSTED
    assert "no_published_adjustment" in outcome.row.contamination_flags
    quality = assess_earnings(outcome.row, _consensus(), _config())
    assert "actual_disputed" in quality.eps.flags       # still not settled
    assert quality.eps.basis is EpsBasis.UNADJUSTED


def test_guidance_from_the_release_survives_an_unsettled_eps_leg():
    """The two are independent readings of one verified document: the EPS
    dispute is about basis, guidance is a range the company printed."""
    outcome = apply_release(
        _row(), _extraction(guidance={"period": "2026-Q4",
                                      "revenue_low": 1.74e11,
                                      "revenue_high": 1.80e11}),
        filed_eps=2.02)

    assert outcome.row.guidance is not None
    assert outcome.row.guidance.revenue_midpoint == 1.77e11


# --- who gets escalated ----------------------------------------------------

def test_only_a_disputed_actual_is_worth_reading_a_release_for():
    """~8,900 tokens per name. A leg that is merely thin, or already
    confirmed, gains nothing a document can give it."""
    disputed = assess_earnings(_row(), _consensus(), _config())
    agreed = assess_earnings(_row(eps_finnhub=[2.02]), _consensus(), _config())
    thin = assess_earnings(_row(eps_finnhub=[2.02]),
                           _consensus(eps_analysts=1), _config())

    assert needs_release_read(disputed)
    assert not needs_release_read(agreed)
    assert not needs_release_read(thin)


# --- the funnel ------------------------------------------------------------

class FakeCalendar:
    def __init__(self, entries):
        self.entries = entries

    def earnings_calendar(self, start, end):
        return self.entries


class FakeNumbers:
    def __init__(self, found: dict):
        self.found = found

    def verified_earnings(self, ticker, day):
        return self.found.get(ticker)


class FakeDirectory:
    def __init__(self, ciks: dict):
        self.ciks = ciks

    def cik_for(self, ticker):
        return self.ciks.get(ticker)

    def name_for(self, ticker):
        return ticker


class FakeFacts:
    """EDGAR XBRL, injected. Returns whatever the filing says."""

    def __init__(self, filed: dict):
        self.filed = filed

    def quarterly(self, cik, tag, report_date):
        value = self.filed.get(cik)
        if value is None:
            return None
        return type("Fact", (), {"value": value, "period_end": report_date})()


class FakeReleases:
    def __init__(self, extractions: dict):
        self.extractions = extractions
        self.calls: list[str] = []

    def release(self, ticker, report_date):
        self.calls.append(ticker)
        return self.extractions.get(ticker)


def _provider() -> StaticProvider:
    return StaticProvider(bars=make_bars(30, start_price=40.0,
                                         volume=2_000_000),
                          profile_data={"market_cap": 5e9})


def _disputed_entry(ticker: str, day: date) -> dict:
    """The calendar's (street) reading; Yahoo's filed one differs by $0.11."""
    return {"symbol": ticker, "date": day.isoformat(), "year": 2026,
            "quarter": 3, "epsActual": 1.91, "epsEstimate": 1.80,
            "revenueActual": 1.05e9, "revenueEstimate": 1.0e9}


def _yahoo(ticker: str, day: date) -> VerifiedEarnings:
    return VerifiedEarnings(ticker=ticker, report_date=day, eps_actual=2.02,
                            eps_estimate=1.89, surprise_pct=6.88)


def test_the_funnel_lifts_a_disputed_leg_by_reading_the_release(tmp_path):
    today = date.today()
    day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    releases = FakeReleases({"AAPL": _extraction(one_off_per_share=0.11)})

    result = run_scout(
        FakeCalendar([_disputed_entry("AAPL", day)]), _provider(), _config(),
        today=today, stock_store=store,
        numbers_source=FakeNumbers({"AAPL": _yahoo("AAPL", day)}),
        directory=FakeDirectory({"AAPL": "0000320193"}),
        facts=FakeFacts({"0000320193": 2.02}), release_reader=releases)

    assert releases.calls == ["AAPL"]
    candidate = result.passed[0]
    assert candidate.quality.eps.status is LegStatus.BEAT
    row = store.latest_print(store.stock_by_ticker("AAPL").id, "2026-Q3")
    assert row.depth is PrintDepth.RELEASE_READ
    assert row.eps_release == 1.91


def test_a_name_whose_sources_agree_is_never_escalated(tmp_path):
    today = date.today()
    day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    releases = FakeReleases({})

    run_scout(FakeCalendar([{"symbol": "CONF", "date": day.isoformat(),
                             "year": 2026, "quarter": 3, "epsActual": 1.20,
                             "epsEstimate": 1.00, "revenueActual": 1.05e9,
                             "revenueEstimate": 1.0e9}]),
              _provider(), _config(), today=today, stock_store=store,
              directory=FakeDirectory({"CONF": "0000000001"}),
              facts=FakeFacts({"0000000001": 1.20}), release_reader=releases)

    assert releases.calls == []


def test_the_release_budget_bounds_how_many_names_are_escalated(tmp_path):
    """A per-run ceiling, because the read is the most expensive thing in the
    funnel and a bad day could otherwise escalate dozens of names."""
    today = date.today()
    day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    releases = FakeReleases({t: _extraction(one_off_per_share=0.11)
                             for t in tickers})
    config = dataclasses.replace(_config(), scout_max_release_reads=2)

    run_scout(FakeCalendar([_disputed_entry(t, day) for t in tickers]),
              _provider(), config, today=today, stock_store=store,
              numbers_source=FakeNumbers({t: _yahoo(t, day) for t in tickers}),
              directory=FakeDirectory({t: f"000000000{i}"
                                       for i, t in enumerate(tickers, 1)}),
              facts=FakeFacts({f"000000000{i}": 2.02
                               for i in range(1, len(tickers) + 1)}),
              release_reader=releases)

    assert len(releases.calls) == 2


def test_a_rejected_extraction_leaves_the_recorded_reading_shallow(tmp_path):
    """An extraction that fails the filing check must not deepen the row —
    `release_read` would then mean "somebody read something", which is the
    opposite of what the depth is for."""
    today = date.today()
    day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    releases = FakeReleases({"AAPL": _extraction(gaap_eps_diluted=1.40)})

    result = run_scout(
        FakeCalendar([_disputed_entry("AAPL", day)]), _provider(), _config(),
        today=today, stock_store=store,
        numbers_source=FakeNumbers({"AAPL": _yahoo("AAPL", day)}),
        directory=FakeDirectory({"AAPL": "0000320193"}),
        facts=FakeFacts({"0000320193": 2.02}), release_reader=releases)

    row = store.latest_print(store.stock_by_ticker("AAPL").id, "2026-Q3")
    assert row.depth is PrintDepth.VERIFIED
    assert "actual_disputed" in result.passed[0].quality.eps.flags
    # A document WAS read here, so this is not the "nobody has read it yet"
    # case and the print is not re-requested. The rejection reason goes to
    # stderr; a wait already open stays open until the file is corrected or
    # the age bound closes it.
    assert result.release_wanted == []
    assert result.release_settled == []


def test_a_print_nobody_has_read_yet_is_reported_rather_than_guessed(tmp_path):
    """The read happens outside the process (an agent or a human opens the
    8-K), so the funnel's job is to name the prints that need one. A name
    with no extraction available must not consume the read budget either —
    nothing was read."""
    today = date.today()
    day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    tickers = ["AAA", "BBB"]

    result = run_scout(
        FakeCalendar([_disputed_entry(t, day) for t in tickers]), _provider(),
        dataclasses.replace(_config(), scout_max_release_reads=1),
        today=today, stock_store=store,
        numbers_source=FakeNumbers({t: _yahoo(t, day) for t in tickers}),
        directory=FakeDirectory({t: f"000000000{i}"
                                 for i, t in enumerate(tickers, 1)}),
        facts=FakeFacts({}), release_reader=FakeReleases({}))

    # The print, not merely the company: the file the next run looks for is
    # named for the quarter, and a name alone would not say which one.
    assert sorted(result.release_wanted) == [f"AAA_{day.isoformat()}",
                                             f"BBB_{day.isoformat()}"]


def test_the_directory_reader_matches_an_extraction_to_its_print(tmp_path):
    """Named by ticker AND report date: a company files four of these a year
    and the wrong quarter's document would pass the GAAP check against the
    wrong filing."""
    from hawkeye.scout.release import DirectoryReleaseReader

    (tmp_path / "AAPL_2026-07-31.json").write_text(
        json.dumps(_extraction(one_off_per_share=0.11)), encoding="utf-8")
    reader = DirectoryReleaseReader(tmp_path)

    assert reader.release("AAPL", date(2026, 7, 31))["one_off_per_share"] == 0.11
    assert reader.release("AAPL", date(2026, 4, 30)) is None
    assert reader.release("MSFT", date(2026, 7, 31)) is None


def test_the_funnel_still_runs_without_a_release_reader(tmp_path):
    today = date.today()
    day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    store.put_stock(Stock(cik="0000320193", ticker="AAPL"))

    result = run_scout(
        FakeCalendar([_disputed_entry("AAPL", day)]), _provider(), _config(),
        today=today, stock_store=store,
        numbers_source=FakeNumbers({"AAPL": _yahoo("AAPL", day)}))

    assert "actual_disputed" in result.passed[0].quality.eps.flags
    assert result.release_wanted == []      # nowhere to send the request
