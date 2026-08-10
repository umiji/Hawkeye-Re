"""The session-mode half of the guidance extraction (task 8.7 layer 2).

With an API key the scan calls the agent itself and none of this runs. Session
mode cannot: the tribunal is driven by a Claude Code session, and a Python
subprocess in the middle of a scan has no way to ask it for anything. So the
scan writes the sentence down and two CLI commands close the loop.

The cost of that, and it is the thing these tests pin: the print row is
written BEFORE the guidance is known, so attaching it retires that row and
appends a corrected one. The ledger then says what actually happened — the
shortlist was ranked on a row with no guidance — instead of a row that looks
complete in hindsight.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    PrintSource,
    RowStatus,
    Stock,
)
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.base import StaticProvider
from hawkeye.scout import guidance_case
from hawkeye.scout.scout import run_scout
from tests.conftest import FakeWhispers, make_bars, make_whispers
from tests.test_scout_quality_wiring import FakeCalendar, _entries

SUMMARY = (
    "Test Corp reported second quarter earnings of $1.20 per share. "
    "The company said it expects third quarter results to range from a loss "
    "of $1.00 per share to breakeven. The current consensus estimate is "
    "earnings of $0.08 per share for the quarter ending September 30, 2026.")

GOOD_REPLY = {
    "guided": True, "period": "2026-Q3", "eps_low": -1.00, "eps_high": 0.0,
    "quote": ("third quarter results to range from a loss of $1.00 per share "
              "to breakeven")}


def _config():
    from hawkeye.config import HawkeyeConfig
    return HawkeyeConfig()


def _scan(tmp_path) -> StockStore:
    """One scan with NO reader, i.e. session mode."""
    today = date.today()
    event_day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    run_scout(FakeCalendar(_entries(event_day)),
              StaticProvider(bars=make_bars(30, start_price=40.0,
                                            volume=2_000_000),
                             profile_data={"market_cap": 5e9}),
              _config(), today=today, stock_store=store,
              numbers_source=FakeWhispers({"AMZN": make_whispers(
                  "AMZN", announced=event_day, summary=SUMMARY)}))
    return store


# --- staging ----------------------------------------------------------------

def test_a_scan_with_no_reader_writes_the_sentence_down(tmp_path):
    _scan(tmp_path)

    cases = guidance_case.list_cases()
    assert [c.ticker for c in cases] == ["AMZN"]
    assert cases[0].summary == SUMMARY
    assert cases[0].next_quarter == "2026-Q3"


def test_the_case_points_at_the_row_the_scan_actually_wrote(tmp_path):
    """Re-deriving the row from the ticker and quarter at submit time would
    silently move the reading onto whatever is active by then — and between
    the two a vendor can restate a figure, which appends a new row."""
    store = _scan(tmp_path)

    case = guidance_case.list_cases()[0]
    assert case.print_id == store.active_print(case.stock_id, "2026-Q2").id


# --- attaching --------------------------------------------------------------

def test_the_reading_lands_on_the_print_and_retires_the_row_it_replaces(
        tmp_path):
    store = _scan(tmp_path)
    case = guidance_case.list_cases()[0]

    extraction = guidance_case.submit(case, GOOD_REPLY, model="test-model")
    guidance_case.attach(store, case, extraction)

    active = store.active_print(case.stock_id, "2026-Q2")
    assert active.guidance.eps_low == -1.00
    assert active.guidance.eps_high == 0.0
    assert active.guidance.extractor == "agent"
    assert active.id != case.print_id                    # a NEW row
    rows = store.prints(case.stock_id)
    assert len(rows) == 2
    assert {r.status for r in rows} == {RowStatus.ACTIVE, RowStatus.SUPERSEDED}


def test_the_row_ranked_on_is_still_readable_afterwards(tmp_path):
    """The retired row is the record of what the shortlist was decided on.
    Deleting it would make the run look as though the guidance had been known
    at ranking time."""
    store = _scan(tmp_path)
    case = guidance_case.list_cases()[0]

    guidance_case.attach(store, case,
                         guidance_case.submit(case, GOOD_REPLY))

    retired = [r for r in store.prints(case.stock_id)
               if r.status is RowStatus.SUPERSEDED]
    assert retired[0].id == case.print_id
    assert retired[0].guidance is None


def test_a_refusal_is_attached_too_so_the_row_says_why(tmp_path):
    """A row that stores only the blank cannot say afterwards whether the
    company guided nothing or our own extractor broke."""
    store = _scan(tmp_path)
    case = guidance_case.list_cases()[0]

    extraction = guidance_case.submit(case, {
        "guided": True, "period": "2026-Q3", "eps_low": 5.0, "eps_high": 6.0,
        "quote": "third quarter earnings of $5.00 to $6.00 per share"})
    guidance_case.attach(store, case, extraction)

    active = store.active_print(case.stock_id, "2026-Q2")
    assert active.guidance is None
    assert active.guidance_reason == "quote_not_in_source"


def test_a_restatement_landing_in_between_blocks_the_attachment(tmp_path):
    """The summary this reading came from described the row that has since
    been retired. Attaching it to the corrected row would put a reading on a
    print nobody read the prose of (invariant 6)."""
    store = _scan(tmp_path)
    case = guidance_case.list_cases()[0]
    superseding = store.active_print(case.stock_id, "2026-Q2").model_copy(
        update={"id": "ern_restated", "eps_actual": 1.42})
    store.revise_print(superseding)

    assert guidance_case.attach(
        store, case, guidance_case.submit(case, GOOD_REPLY)) is None


# --- the gate is the same one the API path runs -----------------------------

def test_submit_runs_the_same_gate_as_the_api_path():
    """Two modes that could accept different answers would produce results
    nobody can compare — the same reason the tribunal's prompts are one
    constant read by both."""
    from hawkeye.scout.guidance_agent import parse_reply

    case = guidance_case.GuidanceCase(
        stock_id="s", print_id="p", ticker="ALGT", fiscal_quarter="2026-Q2",
        next_quarter="2026-Q3", summary=SUMMARY)

    assert (guidance_case.submit(case, GOOD_REPLY).reading
            == parse_reply(GOOD_REPLY, case.request()).reading)


def test_the_package_is_the_same_text_the_api_path_sends():
    from hawkeye.scout.guidance_agent import render_request

    case = guidance_case.GuidanceCase(
        stock_id="s", print_id="p", ticker="ALGT", fiscal_quarter="2026-Q2",
        next_quarter="2026-Q3", summary=SUMMARY)

    assert guidance_case.render_input(case) == render_request(case.request())


# --- the queue --------------------------------------------------------------

def test_a_submitted_case_leaves_the_queue(tmp_path):
    store = _scan(tmp_path)
    case = guidance_case.list_cases()[0]

    guidance_case.attach(store, case, guidance_case.submit(case, GOOD_REPLY))
    guidance_case.discard(case.id)

    assert guidance_case.list_cases() == []


def test_a_reply_that_is_not_an_object_is_refused(tmp_path):
    path = tmp_path / "reply.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(ValueError):
        guidance_case.load_reply(str(path))
