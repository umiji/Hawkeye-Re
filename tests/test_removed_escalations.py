"""What the scan stopped doing when EarningsWhispers became the one source.

Two escalations were removed together, because the first existed only to serve
the second:

- **Reading the company's own release.** It fired on one condition — the two
  vendors reporting different EPS actuals — and one source per print makes that
  condition structurally unreachable. A gate that can never fire is worse than
  no gate: it reads as a safeguard.
- **Checking an extraction against EDGAR's XBRL filing.** Its only job was
  validating the release reading above. With nothing left to validate it was
  a network call in support of nothing.

These tests pin the ABSENCE. A deletion nothing asserts grows back the next
time someone needs a hook, and neither of these may: an XBRL figure is filed
on a GAAP basis while the street's consensus is adjusted, so re-introducing it
as an actual would compare two different quantities with full confidence.
"""
from __future__ import annotations

import importlib
import inspect

import pytest

from hawkeye.contracts.stocks import EarningsPrint, PrintDepth
from hawkeye.ledger.store import Ledger
from hawkeye.scout.scout import ScoutResult, run_scout


def test_the_xbrl_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hawkeye.marketdata.edgar_facts")


def test_the_ticker_to_cik_directory_survives():
    """Company identity is the SEC registrant number, not the ticker.

    Deliberately a separate module from the XBRL facts feed, and deliberately
    kept: without it a ticker reused after a delisting merges two companies'
    histories with nothing in the record to show it.
    """
    from hawkeye.marketdata.edgar import EdgarDirectory
    assert callable(EdgarDirectory)


def test_a_print_row_carries_no_release_or_xbrl_figures():
    removed = {"eps_xbrl_diluted", "revenue_xbrl", "eps_release",
               "revenue_release", "eps_basis", "one_off_per_share"}
    assert not removed & set(EarningsPrint.model_fields)


def test_only_the_two_reachable_depths_remain():
    """`xbrl_validated` was never assigned anywhere, and `release_read` can no
    longer be reached now that nothing reads a release."""
    assert [d.value for d in PrintDepth] == ["calendar_only", "verified"]


def test_the_scan_takes_no_release_reader_and_no_xbrl_facts():
    absent = {"facts", "release_reader", "held_open"}
    assert not absent & set(inspect.signature(run_scout).parameters)


def test_a_scan_result_asks_for_no_documents():
    absent = {"release_wanted", "release_settled", "reopened"}
    assert not absent & set(ScoutResult.__dataclass_fields__)


def test_a_new_ledger_has_no_release_request_table(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    tables = {row[0] for row in ledger._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "release_requests" not in tables


def test_the_ledger_exposes_no_release_request_methods():
    absent = {"request_release_reads", "open_release_requests",
              "resolve_release_reads", "expire_release_requests"}
    assert not absent & set(dir(Ledger))
