"""The stock-centric tables (docs/MASTER_OVERVIEW.ja.md §6.1).

Everything here defends one property: a decision references a consensus row
BY ID instead of copying the numbers into its payload (the user's explicit
instruction), and that is only compatible with invariant 1 — a pre-registered
recommendation is never rewritten — if the referenced row can never change.
So immutability is tested at the API level AND at the SQLite level, not left
to convention.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from hawkeye.contracts.models import (
    GateReport,
    Recommendation,
    RecommendationStatus,
    Verdict,
    DecisionType,
)
from hawkeye.contracts.stocks import (
    ConsensusSnapshot,
    EarningsPrint,
    PrintDepth,
    ReviewStage,
    SnapshotKind,
    Stock,
)
from hawkeye.ledger.store import Ledger
from hawkeye.ledger.stocks import StockStore
from hawkeye.tribunal.pipeline import parse_thesis
from tests.conftest import make_brief, thesis_payload

JST = timezone(timedelta(hours=9))


def at(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 8, day, hour, 0, tzinfo=JST)


def make_stock(**overrides) -> Stock:
    base = dict(id="cik:0001018724", cik="0001018724", ticker="AMZN",
                name="Amazon.com, Inc.", exchange="NASDAQ",
                sector="Consumer Discretionary")
    base.update(overrides)
    return Stock(**base)


def make_consensus(**overrides) -> ConsensusSnapshot:
    base = dict(stock_id="cik:0001018724", fiscal_quarter="2026-Q2",
                captured_at=at(1), kind=SnapshotKind.PRE_REGISTERED,
                eps_avg=1.83, eps_low=1.55, eps_high=2.10, eps_analysts=42,
                revenue_avg=1.62e11, revenue_analysts=38,
                eps_finnhub=1.83, revenue_finnhub=1.61e11)
    base.update(overrides)
    return ConsensusSnapshot(**base)


def store(tmp_path) -> StockStore:
    return StockStore(str(tmp_path / "hawkeye.db"))


# --- consensus: append-only, and one row per term unless it moved ----------

def test_capturing_identical_consensus_twice_writes_one_row(tmp_path):
    """A capture that adds no information adds no row (§6.1(D))."""
    st = store(tmp_path)
    st.put_stock(make_stock())

    first = st.capture_consensus(make_consensus(captured_at=at(1)))
    second = st.capture_consensus(make_consensus(captured_at=at(2)))

    rows = st.consensus_snapshots("cik:0001018724", "2026-Q2")
    assert len(rows) == 1
    assert first == second == rows[0].id


def test_a_moved_estimate_writes_a_second_row_and_keeps_the_first(tmp_path):
    """BIIB's consensus moved 3.98 -> 2.15 over 90 days; that movement is
    the evidence behind "vendor disagreement is vintage, not methodology",
    so it must survive."""
    st = store(tmp_path)
    st.put_stock(make_stock())

    old = st.capture_consensus(make_consensus(captured_at=at(1), eps_avg=3.98))
    new = st.capture_consensus(make_consensus(captured_at=at(3), eps_avg=2.15))

    rows = st.consensus_snapshots("cik:0001018724", "2026-Q2")
    assert [r.eps_avg for r in rows] == [3.98, 2.15]
    assert old != new
    assert st.consensus(old).eps_avg == 3.98      # the first row is untouched


def test_consensus_in_force_is_the_newest_capture_before_the_print(tmp_path):
    st = store(tmp_path)
    st.put_stock(make_stock())
    st.capture_consensus(make_consensus(captured_at=at(1), eps_avg=3.98))
    newest = st.capture_consensus(make_consensus(captured_at=at(3), eps_avg=2.15))
    st.capture_consensus(make_consensus(
        captured_at=at(9), eps_avg=1.10, kind=SnapshotKind.RECONSTRUCTED))

    in_force = st.consensus_in_force("cik:0001018724", "2026-Q2", as_of=at(5))

    assert in_force is not None and in_force.id == newest


def test_sql_update_of_a_captured_consensus_is_refused(tmp_path):
    """The API having no update method is not enough — the guarantee has to
    hold against someone reaching for raw SQL, because a silent repoint of a
    pre-registered recommendation leaves no trace anywhere."""
    st = store(tmp_path)
    st.put_stock(make_stock())
    st.capture_consensus(make_consensus())

    with pytest.raises(sqlite3.DatabaseError):
        st._conn.execute("UPDATE consensus_snapshots SET eps_avg = 0.01")
    with pytest.raises(sqlite3.DatabaseError):
        st._conn.execute("DELETE FROM consensus_snapshots")


def test_the_store_exposes_no_mutation_verb(tmp_path):
    forbidden = [name for name in dir(StockStore)
                 if not name.startswith("_")
                 and any(verb in name for verb in
                         ("update", "delete", "overwrite", "set_"))]
    assert forbidden == []


# --- identity: CIK, never the ticker --------------------------------------

def test_two_companies_that_shared_a_ticker_stay_separate(tmp_path):
    """Tickers are reused after a delisting. Keying on one would merge two
    companies' histories silently and re-break the survivorship-bias work."""
    st = store(tmp_path)
    st.put_stock(make_stock(id="cik:0000111111", cik="0000111111",
                            ticker="XYZ", name="Old XYZ Corp",
                            listing_status="delisted"))
    st.put_stock(make_stock(id="cik:0000222222", cik="0000222222",
                            ticker="XYZ", name="New XYZ Inc"))

    st.capture_consensus(make_consensus(stock_id="cik:0000111111"))

    assert st.stock("cik:0000222222").name == "New XYZ Inc"
    assert st.consensus_snapshots("cik:0000222222", "2026-Q2") == []
    assert len(st.consensus_snapshots("cik:0000111111", "2026-Q2")) == 1


def test_a_stock_without_a_cik_gets_a_provisional_id(tmp_path):
    st = store(tmp_path)
    stock_id = st.put_stock(Stock(ticker="NEWCO"))
    assert stock_id.startswith("prov:")
    assert st.stock(stock_id).cik is None


# --- earnings prints: deepening appends, it never rewrites -----------------

def test_a_deeper_reading_appends_and_the_shallow_row_survives(tmp_path):
    """`depth` exists so "never looked" and "looked and found nothing" stay
    distinguishable (invariant 6). Deepening by UPDATE would destroy exactly
    that, and would silently repoint any decision referencing the row."""
    st = store(tmp_path)
    st.put_stock(make_stock())
    shallow = st.record_print(EarningsPrint(
        stock_id="cik:0001018724", fiscal_quarter="2026-Q2",
        report_date=date(2026, 7, 31), depth=PrintDepth.CALENDAR_ONLY,
        eps_finnhub=[5.75]))
    deep = st.record_print(EarningsPrint(
        stock_id="cik:0001018724", fiscal_quarter="2026-Q2",
        report_date=date(2026, 7, 31), depth=PrintDepth.XBRL_VALIDATED,
        eps_yahoo=5.75, eps_finnhub=[1.88, 1.97], eps_xbrl_diluted=5.75,
        contamination_flags=["finnhub_actual_conflict"]))

    assert shallow != deep
    assert st.print_row(shallow).depth is PrintDepth.CALENDAR_ONLY
    latest = st.latest_print("cik:0001018724", "2026-Q2")
    assert latest.depth is PrintDepth.XBRL_VALIDATED
    assert latest.contamination_flags == ["finnhub_actual_conflict"]
    assert len(st.prints("cik:0001018724")) == 2


def test_recording_the_same_quarter_at_the_same_depth_twice_is_refused(tmp_path):
    st = store(tmp_path)
    st.put_stock(make_stock())
    row = EarningsPrint(stock_id="cik:0001018724", fiscal_quarter="2026-Q2",
                        report_date=date(2026, 7, 31),
                        depth=PrintDepth.CALENDAR_ONLY)
    st.record_print(row)

    with pytest.raises(ValueError):
        st.record_print(row.model_copy(update={"id": "ern_other"}))


def test_a_print_freezes_the_consensus_that_was_in_force(tmp_path):
    """The user asked for the consensus to be "fixed" after the print. That
    is done by pointer, not by overwriting the row (§6.1(D))."""
    st = store(tmp_path)
    st.put_stock(make_stock())
    st.capture_consensus(make_consensus(captured_at=at(1), eps_avg=3.98))
    in_force = st.capture_consensus(make_consensus(captured_at=at(3),
                                                   eps_avg=1.83))
    st.capture_consensus(make_consensus(captured_at=at(9), eps_avg=9.99,
                                        kind=SnapshotKind.RECONSTRUCTED))

    print_id = st.record_print(EarningsPrint(
        stock_id="cik:0001018724", fiscal_quarter="2026-Q2",
        report_date=date(2026, 7, 31), reported_at=at(5),
        depth=PrintDepth.VERIFIED, eps_yahoo=5.75))

    assert st.print_row(print_id).consensus_snapshot_id == in_force


# --- the single-stock joined read the user asked for ----------------------

def test_history_returns_prints_fixed_consensus_and_past_decisions(tmp_path):
    db = str(tmp_path / "hawkeye.db")
    ledger = Ledger(db)
    rec = Recommendation(
        ticker="AMZN", brief=make_brief(), gate_report=GateReport(),
        thesis=parse_thesis(thesis_payload()),
        verdict=Verdict(decision=DecisionType.BUY, conviction=0.6,
                        rationale="test"))
    ledger.record_recommendation(rec, RecommendationStatus.PROPOSED)

    st = StockStore(db)
    st.put_stock(make_stock())
    st.capture_consensus(make_consensus(captured_at=at(3), eps_avg=1.83))
    st.record_print(EarningsPrint(
        stock_id="cik:0001018724", fiscal_quarter="2026-Q2",
        report_date=date(2026, 7, 31), reported_at=at(5),
        depth=PrintDepth.VERIFIED, eps_yahoo=5.75))

    history = st.history("cik:0001018724")

    assert history.stock.ticker == "AMZN"
    assert [p.fiscal_quarter for p in history.prints] == ["2026-Q2"]
    assert history.consensus_for("2026-Q2").eps_avg == 1.83
    assert [d["id"] for d in history.decisions] == [rec.id]


# --- the review projection (rebuildable from the ledger) -------------------

def test_the_review_projection_answers_did_we_already_judge_this_quarter(tmp_path):
    st = store(tmp_path)
    st.put_stock(make_stock())

    st.record_review("cik:0001018724", "2026-Q2", ReviewStage.TRIBUNAL_PASS,
                     reviewed_at=at(5))

    stock = st.stock("cik:0001018724")
    assert stock.last_reviewed_fiscal_quarter == "2026-Q2"
    assert stock.last_stage_reached is ReviewStage.TRIBUNAL_PASS
    assert st.already_reviewed("cik:0001018724", "2026-Q2") is True
    assert st.already_reviewed("cik:0001018724", "2026-Q3") is False


def test_putting_a_stock_again_refreshes_attributes_but_keeps_the_projection(tmp_path):
    st = store(tmp_path)
    st.put_stock(make_stock())
    st.record_review("cik:0001018724", "2026-Q2", ReviewStage.BUY,
                     reviewed_at=at(5))

    st.put_stock(make_stock(sector="Technology"))

    stock = st.stock("cik:0001018724")
    assert stock.sector == "Technology"
    assert stock.last_reviewed_fiscal_quarter == "2026-Q2"


def test_the_projection_can_be_rebuilt_from_the_ledger(tmp_path):
    """The ledger is the record of truth; the master is a cache. A projection
    that cannot be rebuilt would quietly become an independent source of
    truth (§6.1(B))."""
    db = str(tmp_path / "hawkeye.db")
    ledger = Ledger(db)
    rec = Recommendation(
        ticker="AMZN", brief=make_brief(), gate_report=GateReport(),
        thesis=parse_thesis(thesis_payload()),
        verdict=Verdict(decision=DecisionType.BUY, conviction=0.6,
                        rationale="test"))
    ledger.record_recommendation(rec, RecommendationStatus.PROPOSED)
    st = StockStore(db)
    st.put_stock(make_stock())

    st.rebuild_projection(ledger)

    stock = st.stock("cik:0001018724")
    assert stock.last_reviewed_at is not None
    assert stock.last_stage_reached is ReviewStage.BUY


def test_rebuilding_against_a_different_database_is_refused(tmp_path):
    """The rebuild reads the decision tables through its OWN connection, so a
    ledger opened on another file would produce a confident "0 rebuilt" that
    means nothing. Better to refuse than to report success."""
    st = store(tmp_path)
    st.put_stock(make_stock())
    other = Ledger(str(tmp_path / "elsewhere.db"))

    with pytest.raises(ValueError):
        st.rebuild_projection(other)


# --- the new tables must not disturb the existing integrity guarantee -----

def test_the_hash_chain_stays_green_alongside_the_new_tables(tmp_path):
    db = str(tmp_path / "hawkeye.db")
    ledger = Ledger(db)
    st = StockStore(db)
    st.put_stock(make_stock())
    st.capture_consensus(make_consensus())
    st.record_print(EarningsPrint(
        stock_id="cik:0001018724", fiscal_quarter="2026-Q2",
        report_date=date(2026, 7, 31), depth=PrintDepth.CALENDAR_ONLY))

    assert ledger.verify_chain() is True


# --- readability: the ticker as a column ----------------------------------

def test_the_two_payload_tables_expose_the_ticker_as_a_column(tmp_path):
    """Both tables are keyed on `stock_id` (a CIK), which is unreadable at a
    SQL prompt. The column is GENERATED and VIRTUAL: no back-fill, no way to
    drift from the payload, and not one byte written to a recorded row —
    which matters, because the append-only triggers would refuse an UPDATE.
    """
    db = str(tmp_path / "hawkeye.db")
    st = StockStore(db)
    st.put_stock(make_stock())
    st.capture_consensus(make_consensus(ticker="AMZN"))
    st.record_print(EarningsPrint(
        stock_id="cik:0001018724", ticker="AMZN", fiscal_quarter="2026-Q2",
        report_date=date(2026, 7, 31), depth=PrintDepth.CALENDAR_ONLY))

    conn = sqlite3.connect(db)
    assert conn.execute(
        "SELECT ticker FROM earnings_prints").fetchone()[0] == "AMZN"
    assert conn.execute(
        "SELECT ticker FROM consensus_snapshots").fetchone()[0] == "AMZN"


def test_the_column_is_added_to_a_database_that_predates_it(tmp_path):
    """Every existing database was created without it, so the migration has
    to be the same code path as a fresh one — and running twice must not
    fail."""
    db = str(tmp_path / "hawkeye.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE earnings_prints (id TEXT PRIMARY KEY, stock_id TEXT,"
        " fiscal_quarter TEXT, report_date TEXT, depth TEXT,"
        " consensus_snapshot_id TEXT, payload TEXT);"
        "CREATE TABLE consensus_snapshots (id TEXT PRIMARY KEY,"
        " stock_id TEXT, fiscal_quarter TEXT, captured_at TEXT, kind TEXT,"
        " payload TEXT);")
    conn.commit()
    conn.close()

    StockStore(db)
    st = StockStore(db)                       # the migration runs again
    st.put_stock(make_stock())
    st.record_print(EarningsPrint(
        stock_id="cik:0001018724", ticker="AMZN", fiscal_quarter="2026-Q2",
        report_date=date(2026, 7, 31), depth=PrintDepth.CALENDAR_ONLY))

    assert sqlite3.connect(db).execute(
        "SELECT ticker FROM earnings_prints").fetchone()[0] == "AMZN"
