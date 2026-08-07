"""Pre-registering the consensus before the print (§5.3 決定4 / §6.1(D)).

The point of capturing early is that after the release there is no second
source for consensus anywhere — Yahoo's earnings history carries no revenue
line and EDGAR holds no estimates — so a snapshot not taken before the print
can never be reconstructed with the same standing. Everything here is
offline: the calendar rows and the Yahoo frames are injected.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from hawkeye.contracts.stocks import SnapshotKind, Stock
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.consensus import ConsensusReading, YahooConsensusSource
from hawkeye.marketdata.edgar import EdgarDirectory
from hawkeye.scout.prereg import (
    UpcomingPrint,
    capture_consensus,
    upcoming_prints,
)

JST = timezone(timedelta(hours=9))


def calendar_rows() -> list[dict]:
    return [
        # reports Monday; no actual yet — this is what we want to capture
        {"symbol": "AMZN", "date": "2026-08-03", "year": 2026, "quarter": 2,
         "epsEstimate": 1.83, "revenueEstimate": 1.62e11,
         "epsActual": None, "revenueActual": None},
        # reports Tuesday
        {"symbol": "BIIB", "date": "2026-08-04", "year": 2026, "quarter": 2,
         "epsEstimate": 2.15, "revenueEstimate": 2.4e9,
         "epsActual": None, "revenueActual": None},
        # too far out for a 2-business-day window
        {"symbol": "LATE", "date": "2026-08-07", "year": 2026, "quarter": 2,
         "epsEstimate": 0.5, "revenueEstimate": 1e9,
         "epsActual": None, "revenueActual": None},
        # already reported: capturing "consensus" now would be reconstruction
        {"symbol": "DONE", "date": "2026-08-03", "year": 2026, "quarter": 2,
         "epsEstimate": 1.0, "revenueEstimate": 1e9,
         "epsActual": 1.2, "revenueActual": 1.1e9},
    ]


class StubConsensus:
    """A consensus source that answers from a dict, and fails for others the
    way yfinance does — by returning nothing at all."""

    def __init__(self, readings: dict[str, ConsensusReading]):
        self.readings = readings
        self.asked: list[str] = []

    def consensus(self, ticker: str):
        self.asked.append(ticker)
        return self.readings.get(ticker)


def reading(**overrides) -> ConsensusReading:
    base = dict(eps_avg=1.83, eps_low=1.55, eps_high=2.10, eps_analysts=42,
                revenue_avg=1.62e11, revenue_low=1.55e11, revenue_high=1.7e11,
                revenue_analysts=38, next_quarter_eps_avg=2.05,
                next_quarter_revenue_avg=1.7e11)
    base.update(overrides)
    return ConsensusReading(**base)


# --- which names are in the window ----------------------------------------

def test_the_window_is_business_days_so_a_friday_run_reaches_tuesday():
    """A fixed "tomorrow only" window loses a print permanently whenever a
    run is missed, and runs here are manual — same reasoning as the scan
    window in §5.2(1)."""
    friday = date(2026, 7, 31)

    names = [p.ticker for p in upcoming_prints(
        [{"symbol": "MON", "date": "2026-08-03", "epsEstimate": 1.0},
         {"symbol": "TUE", "date": "2026-08-04", "epsEstimate": 1.0},
         {"symbol": "WED", "date": "2026-08-05", "epsEstimate": 1.0}],
        today=friday, business_days=2)]

    assert names == ["MON", "TUE"]


def test_a_print_that_already_reported_is_not_pre_registered():
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)
    assert [p.ticker for p in prints] == ["AMZN", "BIIB"]
    assert prints[0].fiscal_quarter == "2026-Q2"


def test_a_calendar_row_with_no_fiscal_label_gets_none_invented_for_it():
    """This used to fall back to the calendar quarter of the report date,
    which named the quarter AFTER the one being reported for anyone whose
    period does not end in the month they announce it. See
    tests/test_fiscal_quarter_label.py for the whole rule (EW移行 §2)."""
    prints = upcoming_prints(
        [{"symbol": "NOQ", "date": "2026-08-03", "epsEstimate": 1.0}],
        today=date(2026, 8, 2), business_days=2)
    assert prints[0].fiscal_quarter == ""


# --- today's own prints, after a gap ---------------------------------------

def test_the_window_starts_tomorrow_when_the_run_is_daily():
    """The normal case, unchanged. Today's US prints land this evening JST
    and yesterday's run already pre-registered them; asking again would
    spend ~150 lookups to re-read rows we hold."""
    from hawkeye.scout.prereg import capture_window

    today = date(2026, 8, 3)
    yesterday = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)

    window = capture_window(today, yesterday, business_days=2, gap_days=2)

    assert window == [date(2026, 8, 4), date(2026, 8, 5)]


def test_a_gap_since_the_last_run_pulls_todays_prints_back_in():
    """The window is derived from the machine's LOCAL date (JST) while the
    calendar's dates are US market dates, and `business_days_ahead` always
    starts counting from tomorrow — so a 10:08 JST run on 2026-08-03 asked
    about US 08-04..05, and US 08-03's prints (which begin ~20:00 JST that
    evening) were never captured and never could be. After a gap, today is
    included; it is safe by construction because a row carrying an actual is
    dropped anyway, so a print that has already happened cannot slip in as a
    "pre-registration"."""
    from hawkeye.scout.prereg import capture_window

    today = date(2026, 8, 3)
    three_days_ago = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)

    window = capture_window(today, three_days_ago, business_days=2,
                            gap_days=2)

    assert window == [today, date(2026, 8, 4), date(2026, 8, 5)]


def test_the_first_run_ever_includes_today():
    from hawkeye.scout.prereg import capture_window

    window = capture_window(date(2026, 8, 3), None, business_days=2,
                            gap_days=2)

    assert window[0] == date(2026, 8, 3)


def test_a_print_landing_today_is_pre_registered_only_when_asked_for():
    rows = [{"symbol": "TODAY", "date": "2026-08-03", "epsEstimate": 1.0},
            {"symbol": "TOMORROW", "date": "2026-08-04", "epsEstimate": 1.0}]

    without = upcoming_prints(rows, today=date(2026, 8, 3), business_days=2)
    with_today = upcoming_prints(rows, today=date(2026, 8, 3),
                                 business_days=2, include_today=True)

    assert [p.ticker for p in without] == ["TOMORROW"]
    assert [p.ticker for p in with_today] == ["TODAY", "TOMORROW"]


def test_todays_print_that_already_reported_is_still_refused():
    """The safety this whole rule rests on: including today cannot admit a
    reconstruction, because a row with an actual on it never survives."""
    rows = [{"symbol": "DONE", "date": "2026-08-03", "epsEstimate": 1.0,
             "epsActual": 1.2}]

    prints = upcoming_prints(rows, today=date(2026, 8, 3), business_days=2,
                             include_today=True)

    assert prints == []


def test_the_last_capture_is_read_from_the_pre_registered_rows(tmp_path):
    """No new table for "when did capture last run": the newest
    pre-registered snapshot answers it. A run that captured nothing leaves
    the clock where it was, which errs toward INCLUDING today — the safe
    direction, since a missed snapshot is unrecoverable and a wasted lookup
    is not."""
    from hawkeye.contracts.stocks import ConsensusSnapshot, SnapshotKind
    from hawkeye.ledger.stocks import StockStore

    store = StockStore(str(tmp_path / "hawkeye.db"))
    assert store.last_pre_registration_at() is None

    store.capture_consensus(ConsensusSnapshot(
        stock_id="cik:0000000001", ticker="AAA", fiscal_quarter="2026-Q3",
        captured_at=datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc),
        kind=SnapshotKind.PRE_REGISTERED, eps_avg=1.0))
    # A reconstruction is written by the funnel AFTER a print, so it says
    # nothing about when pre-registration last ran.
    store.capture_consensus(ConsensusSnapshot(
        stock_id="cik:0000000002", ticker="BBB", fiscal_quarter="2026-Q3",
        captured_at=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
        kind=SnapshotKind.RECONSTRUCTED, eps_avg=1.0))

    assert store.last_pre_registration_at() == datetime(
        2026, 8, 2, 9, 0, tzinfo=timezone.utc)


# --- capture --------------------------------------------------------------

def make_store(tmp_path) -> StockStore:
    return StockStore(str(tmp_path / "hawkeye.db"))


def test_capture_records_both_sources_and_marks_them_pre_registered(tmp_path):
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)
    source = StubConsensus({"AMZN": reading(), "BIIB": reading(eps_avg=3.98)})

    report = capture_consensus(store, prints, source,
                               captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    assert report.captured == 2
    stock_id = store.stock_by_ticker("AMZN").id
    snapshot = store.consensus_in_force(stock_id, "2026-Q2")
    assert snapshot.kind is SnapshotKind.PRE_REGISTERED
    assert snapshot.eps_avg == 1.83 and snapshot.eps_analysts == 42
    assert snapshot.eps_calendar == 1.83          # the calendar's point estimate
    assert snapshot.revenue_calendar == 1.62e11
    assert snapshot.next_quarter_eps_avg == 2.05  # the guidance yardstick
    assert snapshot.expected_report_date == date(2026, 8, 3)


def test_running_the_job_twice_the_same_day_writes_no_second_row(tmp_path):
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)
    source = StubConsensus({"AMZN": reading(), "BIIB": reading()})

    capture_consensus(store, prints, source,
                      captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))
    second = capture_consensus(store, prints, source,
                               captured_at=datetime(2026, 8, 2, 18, tzinfo=JST))

    stock_id = store.stock_by_ticker("AMZN").id
    assert second.captured == 0 and second.unchanged == 2
    assert len(store.consensus_snapshots(stock_id, "2026-Q2")) == 1


def test_a_moved_estimate_is_captured_as_a_second_row(tmp_path):
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)
    capture_consensus(store, prints, StubConsensus({"BIIB": reading(eps_avg=3.98)}),
                      captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))
    capture_consensus(store, prints, StubConsensus({"BIIB": reading(eps_avg=2.15)}),
                      captured_at=datetime(2026, 8, 3, 9, tzinfo=JST))

    stock_id = store.stock_by_ticker("BIIB").id
    assert [s.eps_avg for s in store.consensus_snapshots(stock_id, "2026-Q2")] \
        == [3.98, 2.15]


def test_a_yahoo_failure_still_pre_registers_the_calendar_estimate(tmp_path):
    """Degrading to one source is not the same as capturing nothing: the
    Finnhub point estimate is still a pre-registered number, and the absent
    distribution is what makes the pair unverifiable later — so it has to be
    visible in the row rather than inferred from silence."""
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)

    report = capture_consensus(store, prints, StubConsensus({}),
                               captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    stock_id = store.stock_by_ticker("AMZN").id
    snapshot = store.consensus_in_force(stock_id, "2026-Q2")
    assert report.captured == 2 and report.yahoo_missing == 2
    assert snapshot.eps_calendar == 1.83
    assert snapshot.eps_avg is None and snapshot.eps_analysts is None


def test_a_capture_with_no_numbers_at_all_writes_nothing(tmp_path):
    """A live run covers ~560 names over two business days, and plenty of
    them have neither a calendar estimate nor a Yahoo reading. An empty row
    is not "the estimate did not move" — it is nothing at all, and writing
    one would make the master look covered where it is blank."""
    store = make_store(tmp_path)
    prints = [UpcomingPrint(ticker="EMPTY", report_date=date(2026, 8, 3),
                            fiscal_quarter="2026-Q2", eps_estimate=None,
                            revenue_estimate=None)]

    report = capture_consensus(store, prints, StubConsensus({}),
                               captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    assert report.captured == 0 and report.nothing_to_record == 1
    stock_id = store.stock_by_ticker("EMPTY").id
    assert store.consensus_snapshots(stock_id, "2026-Q2") == []


def test_capture_stops_once_that_quarters_print_is_recorded(tmp_path):
    """Snapshotting for quarter Q ends when Q's result exists; anything
    later belongs to Q+1 (§6.1(D))."""
    from hawkeye.contracts.stocks import EarningsPrint, PrintSource

    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)
    stock_id = store.put_stock(Stock(cik="0001018724", ticker="AMZN"))
    store.record_print(EarningsPrint(
        stock_id=stock_id, fiscal_quarter="2026-Q2",
        report_date=date(2026, 8, 3), source=PrintSource.WHISPERS))

    report = capture_consensus(store, prints, StubConsensus({"AMZN": reading()}),
                               captured_at=datetime(2026, 8, 3, 22, tzinfo=JST))

    assert report.skipped_already_reported == 1
    assert store.consensus_snapshots(stock_id, "2026-Q2") == []


def test_a_known_cik_keys_the_row_and_an_unknown_one_stays_provisional(tmp_path):
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)
    directory = EdgarDirectory(fetcher=lambda: [
        {"cik_str": 1018724, "ticker": "AMZN", "title": "AMAZON COM INC"}])

    capture_consensus(store, prints, StubConsensus({}), directory=directory,
                      captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    assert store.stock_by_ticker("AMZN").id == "cik:0001018724"
    assert store.stock_by_ticker("AMZN").name == "AMAZON COM INC"
    assert store.stock_by_ticker("BIIB").id == "prov:BIIB"


# --- the adapters ---------------------------------------------------------

class StubTicker:
    """Shaped like yfinance's Ticker: frames keyed by period label."""

    def __init__(self, symbol: str):
        self.symbol = symbol

    @property
    def earnings_estimate(self):
        return {"0q": {"avg": 1.83, "low": 1.55, "high": 2.10,
                       "numberOfAnalysts": 42},
                "+1q": {"avg": 2.05, "low": 1.9, "high": 2.3,
                        "numberOfAnalysts": 40}}

    @property
    def revenue_estimate(self):
        return {"0q": {"avg": 1.62e11, "low": 1.55e11, "high": 1.7e11,
                       "numberOfAnalysts": 38},
                "+1q": {"avg": 1.7e11, "low": 1.6e11, "high": 1.8e11,
                        "numberOfAnalysts": 36}}


def test_the_yahoo_source_reads_this_quarter_and_the_next():
    got = YahooConsensusSource(ticker_factory=StubTicker).consensus("AMZN")

    assert got.eps_avg == 1.83 and got.eps_analysts == 42
    assert got.revenue_avg == 1.62e11 and got.revenue_analysts == 38
    assert got.next_quarter_eps_avg == 2.05
    assert got.next_quarter_revenue_avg == 1.7e11


def test_a_reading_taken_after_the_print_is_one_quarter_out():
    """Measured on AMZN, 2026-08-02, three days after its Q2 release: the
    `0q` row read 1.956 while the Q2 consensus the print was judged against
    was 1.83, and its YoY growth field said +0.3% where Q2 grew 242%. The
    labels are relative to TODAY, not to the last print — so once a quarter
    has reported, `0q` is the quarter now in progress.

    Using it as "what was expected of the quarter just reported" silently
    compares a result against the WRONG quarter's consensus. What it IS good
    for is the guidance yardstick: the quarter now in progress is exactly the
    one guidance covers."""
    from hawkeye.marketdata.consensus import shift_after_print

    shifted = shift_after_print(reading(eps_avg=1.956, revenue_avg=2.022e11,
                                        next_quarter_eps_avg=2.435))

    assert shifted.next_quarter_eps_avg == 1.956
    assert shifted.next_quarter_revenue_avg == 2.022e11
    # the reported quarter's own consensus is NOT in this response at all
    assert shifted.eps_avg is None and shifted.revenue_avg is None
    assert shifted.eps_analysts is None


def test_the_yahoo_source_returns_nothing_when_the_scrape_breaks():
    """yfinance scrapes a site that changes without notice. Every failure
    degrades to None so the caller keeps one source and says so — missing
    data is never a silent pass (invariant 6)."""
    class Broken:
        def __init__(self, symbol):
            raise RuntimeError("scrape failed")

    assert YahooConsensusSource(ticker_factory=Broken).consensus("AMZN") is None


def test_the_edgar_directory_normalises_the_cik_and_is_fetched_once():
    calls = []

    def fetcher():
        calls.append(1)
        return [{"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}]

    directory = EdgarDirectory(fetcher=fetcher)
    assert directory.cik_for("aapl") == "0000320193"
    assert directory.cik_for("AAPL") == "0000320193"
    assert directory.cik_for("NOPE") is None
    assert len(calls) == 1


def test_an_unreachable_edgar_leaves_every_lookup_unknown():
    def fetcher():
        raise OSError("network down")

    assert EdgarDirectory(fetcher=fetcher).cik_for("AAPL") is None
