"""Not pre-registering consensus for names that could never pass the entry
gates (docs/MASTER_OVERVIEW.ja.md §6.1(E)).

Pre-registration costs one Yahoo call per name and covers every US company
reporting in the next two business days — 855 of them on 2026-08-03. A large
share of those can never become a position: below the price floor, below the
market-cap floor, or too thinly traded to exit at the pre-registered stop.
Asking about them every day buys nothing.

The whole feature is governed by one asymmetry: **a snapshot not taken can
never be taken.** A name wrongly excluded loses its consensus history
permanently, while a name wrongly included costs one API call. So:

- a name nobody has judged yet is INCLUDED (unknown is never "no"),
- only an explicit, recent judgment excludes,
- and the judgment goes stale, because a $3 stock can become a $9 one.

The judgment itself is free: it is read off the entry gates the funnel
already ran, from the three gates that describe the COMPANY rather than the
event.
"""
from __future__ import annotations

import dataclasses
from datetime import date, timedelta

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.models import GateReport, GateResult
from hawkeye.contracts.stocks import Stock
from hawkeye.ledger.stocks import StockStore
from hawkeye.scout.prereg import UpcomingPrint, capture_consensus, report_line
from hawkeye.scout.triage import is_investigation_target, triage_from_gates

CONFIG = HawkeyeConfig()


def _gates(**values) -> GateReport:
    """A gate report with the three structural gates set explicitly."""
    defaults = {"min_price": (True, 42.0), "min_market_cap": (True, 5e9),
                "min_avg_dollar_volume": (True, 2e7)}
    defaults.update(values)
    results = [GateResult(name=name, passed=passed, hard=True, value=value,
                          threshold=1.0)
               for name, (passed, value) in defaults.items()]
    # An event gate, which must never decide whether the company is worth
    # following: it says something about this print, not about the issuer.
    results.append(GateResult(name="catalyst_freshness", passed=False,
                              hard=True, value=30.0, threshold=10.0))
    return GateReport(results=results)


# --- reading the verdict off the gates the funnel already ran ---------------

def test_a_name_that_clears_the_structural_floors_is_worth_following():
    verdict = triage_from_gates(_gates())

    assert verdict.is_target
    assert verdict.reason == ""


def test_a_name_below_the_market_cap_floor_is_not_worth_following():
    verdict = triage_from_gates(_gates(min_market_cap=(False, 1.2e8)))

    assert not verdict.is_target
    assert "min_market_cap" in verdict.reason


def test_a_stale_catalyst_says_nothing_about_the_company():
    """`catalyst_freshness` fails on a print we simply looked at too late.
    Excluding the issuer for that would drop a perfectly investable company
    from pre-registration forever."""
    report = GateReport(results=[
        GateResult(name="catalyst_freshness", passed=False, hard=True)])

    assert triage_from_gates(report) is None


def test_missing_data_is_not_a_verdict():
    """An unverified gate is data we do not have. Recording "not a target"
    from it would turn a free-tier gap into a permanent exclusion — the one
    error this feature cannot undo (invariant 6)."""
    report = GateReport(results=[
        GateResult(name="min_market_cap", passed=True, hard=True,
                   unverified=True)])

    assert triage_from_gates(report) is None


# --- what the master does with the verdict -----------------------------------

def test_a_name_nobody_has_judged_is_still_a_target(tmp_path):
    store = StockStore(str(tmp_path / "hawkeye.db"))
    stock_id = store.put_stock(Stock(cik="0000000001", ticker="NEW"))

    assert is_investigation_target(store.stock(stock_id), date.today(), CONFIG)


def test_a_recent_exclusion_is_honoured(tmp_path):
    store = StockStore(str(tmp_path / "hawkeye.db"))
    stock_id = store.put_stock(Stock(cik="0000000001", ticker="TINY"))
    today = date(2026, 8, 3)

    store.record_triage(stock_id, False, "gate: min_market_cap", on=today)

    stock = store.stock(stock_id)
    assert not is_investigation_target(stock, today + timedelta(days=30),
                                       CONFIG)
    assert stock.investigation_reason == "gate: min_market_cap"


def test_an_exclusion_goes_stale(tmp_path):
    """A $3 company can be a $9 one next quarter. A verdict that never
    expires would quietly make the first bad day permanent."""
    store = StockStore(str(tmp_path / "hawkeye.db"))
    stock_id = store.put_stock(Stock(cik="0000000001", ticker="TINY"))
    today = date(2026, 8, 3)
    store.record_triage(stock_id, False, "gate: min_price", on=today)

    later = today + timedelta(days=CONFIG.stock_triage_ttl_days + 1)

    assert is_investigation_target(store.stock(stock_id), later, CONFIG)


def test_refreshing_a_master_row_does_not_erase_the_verdict(tmp_path):
    """`resolve_stock` re-puts the row on every sighting. If that cleared the
    triage the filter would never hold for more than one run — the same trap
    the review projection already has to avoid."""
    store = StockStore(str(tmp_path / "hawkeye.db"))
    stock_id = store.put_stock(Stock(cik="0000000001", ticker="TINY"))
    store.record_triage(stock_id, False, "gate: min_price", on=date(2026, 8, 3))

    store.put_stock(Stock(cik="0000000001", ticker="TINY", name="Tiny Inc"))

    assert store.stock(stock_id).investigation_target is False


# --- the filter, where it saves the calls ------------------------------------

class FakeConsensus:
    def __init__(self):
        self.asked: list[str] = []

    def consensus(self, ticker):
        self.asked.append(ticker)
        return None


def _upcoming(ticker: str) -> UpcomingPrint:
    return UpcomingPrint(ticker=ticker, report_date=date(2026, 8, 5),
                         fiscal_quarter="2026-Q3", eps_estimate=1.0)


def test_pre_registration_skips_a_name_it_could_never_trade(tmp_path):
    store = StockStore(str(tmp_path / "hawkeye.db"))
    tiny = store.put_stock(Stock(cik="0000000001", ticker="TINY"))
    store.put_stock(Stock(cik="0000000002", ticker="BIG"))
    store.record_triage(tiny, False, "gate: min_market_cap",
                        on=date(2026, 8, 3))
    source = FakeConsensus()

    report = capture_consensus(store, [_upcoming("TINY"), _upcoming("BIG")],
                               source, today=date(2026, 8, 3), config=CONFIG)

    assert source.asked == ["BIG"]
    assert report.skipped_not_target == 1
    assert "調査対象外" in report_line(report)


def test_the_verdict_can_be_rebuilt_from_the_recorded_gate_reports(tmp_path):
    """A projection that cannot be rebuilt is a second source of truth. The
    gate report frozen into each dropped-candidate record is the fact; the
    master only reads it — and it is dated by that record, not by today, so
    the expiry does not quietly restart on every rebuild."""
    from hawkeye.contracts.models import ScreenedCandidate, ScreenedCandidateStage
    from hawkeye.ledger.store import Ledger
    from hawkeye.scout.triage import rebuild_triage

    db = str(tmp_path / "hawkeye.db")
    ledger, store = Ledger(db), StockStore(db)
    stock_id = store.put_stock(Stock(cik="0000000001", ticker="TINY"))
    scan_id = ledger.record_scan({}, 1, 1, 1, 0, [])
    ledger.record_screened_candidates(scan_id, [ScreenedCandidate(
        scan_id=scan_id, ticker="TINY", event_date=date(2026, 8, 3),
        stage=ScreenedCandidateStage.GATE_REJECT, eps_surprise_pct=8.0,
        score=0.0, score_version="full",
        gate_report=_gates(min_market_cap=(False, 1.2e8)))])

    assert rebuild_triage(store) == 1

    stock = store.stock(stock_id)
    assert stock.investigation_target is False
    assert "min_market_cap" in stock.investigation_reason


def test_the_filter_is_off_until_a_config_says_otherwise(tmp_path):
    """Turning 855 daily lookups into 600 is a measurable change to what the
    system records, so it is a config diff like any other rule."""
    store = StockStore(str(tmp_path / "hawkeye.db"))
    tiny = store.put_stock(Stock(cik="0000000001", ticker="TINY"))
    store.record_triage(tiny, False, "gate: min_market_cap",
                        on=date(2026, 8, 3))
    source = FakeConsensus()
    off = dataclasses.replace(CONFIG, prereg_skip_non_targets=False)

    capture_consensus(store, [_upcoming("TINY")], source,
                      today=date(2026, 8, 3), config=off)

    assert source.asked == ["TINY"]
