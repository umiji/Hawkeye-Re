"""One active row per quarter, and what happens when the actual is revised.

`depth` is gone. It recorded how hard anyone had looked, which mattered while
four escalations could each deepen a quarter's row. With one source per print
there is nothing to deepen: a row either came from EarningsWhispers or from
the earnings calendar, and that is `source` — the same fact, held once.

What replaces the append-a-deeper-row mechanism is revision. ADEA changed its
reported EPS for the SAME quarter from $0.34 to $0.42 the day after announcing
(measured 2026-08-07; 1 of 6 settled prints in the sample). A revision appends
a new active row and retires the old one, so both of these stay answerable:

- what is true now                → the active row
- what we ranked this name on     → the row that was active at the time

Retiring is the ONE update the storage layer allows. Everything else about a
recorded row is still immutable, and a retired row can never be revived —
reviving is the in-place rewrite the append-only rule exists to forbid.
"""
from __future__ import annotations

from datetime import date

import pytest

from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    PrintSource,
    RowStatus,
    Stock,
)
from hawkeye.ledger.stocks import StockStore


def store(tmp_path) -> StockStore:
    return StockStore(str(tmp_path / "stocks.db"))


def a_stock() -> Stock:
    return Stock(cik="0001018724", ticker="ADEA", name="Adeia")


def a_print(**overrides) -> EarningsPrint:
    base = dict(stock_id="cik:0001018724", ticker="ADEA",
                fiscal_quarter="2026-Q2", report_date=date(2026, 8, 5),
                source=PrintSource.FINNHUB, eps_actual=0.34)
    base.update(overrides)
    return EarningsPrint(**base)


# --- the shape ------------------------------------------------------------

def test_depth_is_gone_from_the_contracts():
    import hawkeye.contracts.stocks as contracts
    assert not hasattr(contracts, "PrintDepth")
    assert "depth" not in EarningsPrint.model_fields


def test_a_row_says_which_vendor_it_came_from_and_whether_it_still_stands():
    row = a_print()
    assert row.source is PrintSource.FINNHUB
    assert row.status is RowStatus.ACTIVE


def test_the_source_is_a_vendor_and_never_a_conclusion():
    """A third value written by anything downstream of the ranking would make
    "what did we know when we ranked this" unanswerable."""
    assert {s.value for s in PrintSource} <= {"finnhub", "yahoo", "whispers"}


# --- one active row per quarter -------------------------------------------

def test_a_second_active_row_for_one_quarter_is_refused(tmp_path):
    st = store(tmp_path)
    st.put_stock(a_stock())
    st.record_print(a_print())

    with pytest.raises(ValueError):
        st.record_print(a_print(id="ern_second", eps_actual=0.42))


def test_a_superseded_row_does_not_block_the_new_one(tmp_path):
    """The uniqueness holds among ACTIVE rows only. A quarter accumulates one
    row per revision, and all but the newest are retired."""
    st = store(tmp_path)
    st.put_stock(a_stock())
    first = st.record_print(a_print())

    second = st.revise_print(a_print(id="ern_second", eps_actual=0.42))

    assert st.print_row(first).status is RowStatus.SUPERSEDED
    assert st.print_row(second).status is RowStatus.ACTIVE
    assert len(st.prints("cik:0001018724", "2026-Q2")) == 2


def test_the_retired_row_keeps_the_number_we_ranked_on(tmp_path):
    """ADEA entered the shortlist at $0.34. If the revision overwrote that,
    the record would read as though $0.42 had been known all along, and both
    the drop review and the thesis-accuracy scoring would be scored against a
    number nobody had."""
    st = store(tmp_path)
    st.put_stock(a_stock())
    first = st.record_print(a_print())
    st.revise_print(a_print(id="ern_second", eps_actual=0.42))

    assert st.print_row(first).eps_actual == 0.34


def test_the_active_row_is_the_one_readers_get(tmp_path):
    st = store(tmp_path)
    st.put_stock(a_stock())
    st.record_print(a_print())
    st.revise_print(a_print(id="ern_second", eps_actual=0.42))

    assert st.active_print("cik:0001018724", "2026-Q2").eps_actual == 0.42
    assert [p.eps_actual for p in st.active_prints("cik:0001018724")] == [0.42]


# --- what the storage layer still refuses ---------------------------------

def test_rewriting_a_recorded_number_is_refused(tmp_path):
    st = store(tmp_path)
    st.put_stock(a_stock())
    row_id = st.record_print(a_print())
    revised = a_print(id=row_id, eps_actual=0.42).model_dump_json()

    with pytest.raises(Exception):
        st._conn.execute("UPDATE earnings_prints SET payload = ? WHERE id = ?",
                         (revised, row_id))


def test_reviving_a_retired_row_is_refused(tmp_path):
    st = store(tmp_path)
    st.put_stock(a_stock())
    first = st.record_print(a_print())
    st.revise_print(a_print(id="ern_second", eps_actual=0.42))

    with pytest.raises(Exception):
        st._conn.execute(
            "UPDATE earnings_prints SET status = 'active' WHERE id = ?",
            (first,))


# --- the consensus row ----------------------------------------------------

def test_the_consensus_carries_a_full_year_yardstick():
    """Ten of the 47 measured names guided on the full year and gave no
    quarterly figure. Without somewhere to put the full-year consensus, their
    guidance has nothing to be judged against and the third leg reads as
    absent when the company did in fact guide (EW移行 §5)."""
    snapshot = ConsensusSnapshot(
        stock_id="cik:0001018724", ticker="ADEA", fiscal_quarter="2026-Q2",
        full_year_eps_avg=1.60, full_year_revenue_avg=3.9e8)

    assert snapshot.full_year_eps_avg == 1.60
    assert snapshot.full_year_revenue_avg == 3.9e8


def test_a_moved_full_year_consensus_counts_as_new_information():
    """`content_key` decides whether a capture is worth a row. A field left
    out of it is a field whose changes are silently discarded."""
    base = ConsensusSnapshot(stock_id="s", ticker="T", fiscal_quarter="2026-Q2",
                             full_year_eps_avg=1.60)
    moved = base.model_copy(update={"full_year_eps_avg": 1.75})

    assert base.content_key() != moved.content_key()
