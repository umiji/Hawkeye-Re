"""A scan has to survive the wait for guidance byte-for-byte (task: rank
after guidance, docs/design/RANK_AFTER_GUIDANCE.ja.md). `hawkeye scout`
writes the whole `ScoutResult` to disk instead of recording it, and
`hawkeye rank` reads it back — so anything lost in that round trip is a
candidate the ranking step judges on less than the scan actually saw.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from hawkeye.contracts.stocks import ConsensusSnapshot, SnapshotKind, Stock
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.base import StaticProvider
from hawkeye.scout.scan_store import (
    _result_to_dict,
    discard_scan_result,
    has_pending_scan,
    load_scan_result,
    save_scan_result,
)
from hawkeye.scout.scout import run_scout
from tests.conftest import FakeWhispers, make_bars, make_whispers
from tests.test_scout_quality_wiring import FakeCalendar

JST = timezone(timedelta(hours=9))


def _config():
    from hawkeye.config import HawkeyeConfig
    return HawkeyeConfig()


def _provider() -> StaticProvider:
    return StaticProvider(bars=make_bars(30, start_price=40.0,
                                         volume=2_000_000),
                          profile_data={"market_cap": 5e9})


_SUMMARY = (
    "Test Corp reported second quarter earnings of $1.20 per share. "
    "The company said it expects third quarter results to range from a loss "
    "of $1.00 per share to breakeven. The current consensus estimate is "
    "earnings of $0.08 per share for the quarter ending September 30, 2026.")


def _scanned_result(tmp_path):
    today = date.today()
    event_day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    stock_id = store.put_stock(Stock(cik="0001018724", ticker="AMZN"))
    store.capture_consensus(ConsensusSnapshot(
        stock_id=stock_id, ticker="AMZN", fiscal_quarter="2026-Q2",
        captured_at=datetime.combine(event_day - timedelta(days=1),
                                     datetime.min.time(), tzinfo=JST),
        kind=SnapshotKind.PRE_REGISTERED, eps_avg=1.00, eps_calendar=1.00,
        eps_analysts=25, revenue_avg=1.0e9, revenue_calendar=1.0e9,
        revenue_analysts=20))
    entries = [{"symbol": "AMZN", "date": event_day.isoformat(),
               "year": 2026, "quarter": 2,
               "epsActual": 1.20, "epsEstimate": 1.00,
               "revenueActual": 1.05e9, "revenueEstimate": 1.0e9}]
    feed = FakeWhispers({"AMZN": make_whispers(
        "AMZN", announced=event_day, summary=_SUMMARY)})
    result = run_scout(FakeCalendar(entries), _provider(), _config(),
                       today=today, stock_store=store,
                       numbers_source=feed)
    return result, store


def test_a_scan_survives_the_round_trip_unchanged(tmp_path):
    result, _ = _scanned_result(tmp_path)

    path = save_scan_result(result, tmp_path / "scan" / "pending.json")
    loaded = load_scan_result(path)

    # Byte-for-byte on the serialized form: any field the (de)serializer
    # drops or mangles shows up here without having to name it.
    assert _result_to_dict(loaded) == _result_to_dict(result)
    assert loaded.passed[0].ticker == "AMZN"
    assert loaded.passed[0].quality is not None
    assert loaded.passed[0].quality.eps.status.value == "beat"
    assert loaded.passed[0].consensus is not None
    assert loaded.passed[0].stock_id


def test_saving_over_a_pending_scan_is_refused(tmp_path):
    result, _ = _scanned_result(tmp_path)
    path = tmp_path / "scan" / "pending.json"
    save_scan_result(result, path)

    with pytest.raises(FileExistsError):
        save_scan_result(result, path)


def test_discard_then_has_pending_scan(tmp_path, monkeypatch):
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    result, _ = _scanned_result(tmp_path)

    assert has_pending_scan() is False
    save_scan_result(result)
    assert has_pending_scan() is True

    assert discard_scan_result() is True
    assert has_pending_scan() is False
    assert discard_scan_result() is False   # nothing left to discard


def test_the_release_cut_counts_survive_the_round_trip(tmp_path,
                                                       monkeypatch):
    """The scan writes this file and `hawkeye rank` reads it back hours
    later. A count lost here reaches the ledger as a zero, and a zero is how
    the report says "the release was never read" — so the loss would be
    reported as a fact about the scan rather than about the file (T-013)."""
    from hawkeye.scout.scan_store import _candidate_from_dict, _candidate_to_dict
    from hawkeye.scout.scout import ScoutCandidate

    candidate = ScoutCandidate(
        ticker="AAA", event_date=date(2026, 8, 14),
        eps_surprise_pct=12.5, revenue_surprise_pct=3.0,
        cause_blocks_kept=8, cause_blocks_repaired=2,
        cause_blocks_altered=1, cause_blocks_refused=3)
    back = _candidate_from_dict(_candidate_to_dict(candidate))
    assert (back.cause_blocks_kept, back.cause_blocks_repaired,
            back.cause_blocks_altered, back.cause_blocks_refused) \
        == (8, 2, 1, 3)


def test_a_pending_file_written_before_the_counts_existed_still_loads():
    """Invariant 1 in miniature: an older file is read as "never counted",
    which is what it is."""
    from hawkeye.scout.scan_store import _candidate_from_dict, _candidate_to_dict
    from hawkeye.scout.scout import ScoutCandidate

    older = _candidate_to_dict(ScoutCandidate(
        ticker="AAA", event_date=date(2026, 8, 14),
        eps_surprise_pct=12.5, revenue_surprise_pct=3.0))
    for key in list(older):
        if key.startswith("cause_blocks_"):
            del older[key]
    back = _candidate_from_dict(older)
    assert back.cause_blocks_kept == 0
    assert back.cause_blocks_refused == 0
