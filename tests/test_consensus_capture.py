"""Pre-registering the consensus before the print (§5.3 決定4 / §6.1(D)).

The point of capturing early is that after the release there is no second
source for consensus anywhere — the earnings feed's after-the-print endpoint
states what was expected of the quarter that just reported only while it is
the latest one, and EDGAR holds no estimates at all — so a snapshot not taken
before the print can never be reconstructed with the same standing.

The source is the earnings feed's forward endpoint (`WhispersSource.forecast`,
see test_whispers_forward). It replaced Yahoo on 2026-08-09 so that a print's
consensus and its actual come from one vendor. Everything here is offline: the
calendar rows and the feed's answers are injected.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from hawkeye.contracts.stocks import SnapshotKind, Stock
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.edgar import EdgarDirectory
from hawkeye.marketdata.whispers import WhispersForecast, WhispersUnavailable
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
    """The forward endpoint, answering from a dict. A ticker it has no entry
    for comes back as None, which is the feed's own "no row for this
    company"; a stored exception is raised, which is how the live reader
    reports that it could not read the feed at all."""

    def __init__(self, readings: dict):
        self.readings = readings
        self.asked: list[str] = []

    def forecast(self, ticker: str):
        self.asked.append(ticker)
        answer = self.readings.get(ticker)
        if isinstance(answer, Exception):
            raise answer
        return answer


def reading(**overrides) -> WhispersForecast:
    base = dict(ticker="AMZN", eps_estimate=1.83, revenue_estimate=1.62e11,
                whisper=1.95, next_report_date=date(2026, 8, 3))
    base.update(overrides)
    return WhispersForecast(**base)


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
    source = StubConsensus({"AMZN": reading(),
                            "BIIB": reading(eps_estimate=3.98)})

    report = capture_consensus(store, prints, source,
                               captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    assert report.captured == 2
    stock_id = store.stock_by_ticker("AMZN").id
    snapshot = store.consensus_in_force(stock_id, "2026-Q2")
    assert snapshot.kind is SnapshotKind.PRE_REGISTERED
    assert snapshot.eps_avg == 1.83 and snapshot.revenue_avg == 1.62e11
    assert snapshot.eps_calendar == 1.83          # the calendar's point estimate
    assert snapshot.revenue_calendar == 1.62e11
    assert snapshot.source_note == "whispers+finnhub"
    assert snapshot.expected_report_date == date(2026, 8, 3)


def test_a_pre_registered_row_carries_no_guidance_yardstick(tmp_path):
    """It cannot. The forward endpoint states the consensus for the print
    about to happen and nothing beyond it, so the bar for the guidance that
    print will GIVE does not exist yet. It is read afterwards, out of the
    summary the print itself carries (see test_scout_quality_wiring)."""
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)

    capture_consensus(store, prints, StubConsensus({"AMZN": reading()}),
                      captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    stock_id = store.stock_by_ticker("AMZN").id
    snapshot = store.consensus_in_force(stock_id, "2026-Q2")
    assert snapshot.next_quarter_eps_avg is None
    assert snapshot.full_year_eps_avg is None


def test_the_analyst_count_and_range_are_gone_and_the_row_says_so(tmp_path):
    """The forward endpoint publishes neither. Both were already decided not
    to be used, and leaving the columns blank is what keeps "we have no
    distribution" from reading as "the estimate was a point" (invariant 6)."""
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)

    capture_consensus(store, prints, StubConsensus({"AMZN": reading()}),
                      captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    snapshot = store.consensus_in_force(
        store.stock_by_ticker("AMZN").id, "2026-Q2")
    assert snapshot.eps_analysts is None and snapshot.revenue_analysts is None
    assert snapshot.eps_low is None and snapshot.eps_high is None


# --- the feed must be talking about the print we are filing under ----------
#
# Measured 2026-08-11 on 173 comparable pre-registered rows: 20 of them (11.6%)
# held the NEXT quarter's consensus under this quarter's label. All 20 were
# companies the calendar still listed as "about to report" that had in fact
# already reported, so the forward endpoint had moved on. The calendar estimate
# on those same rows was right, which is what made the damage visible at all.

def test_a_feed_answering_about_a_later_print_does_not_become_this_consensus(
        tmp_path):
    """BZH, 2026-08-09: the calendar said 2026-Q3 reports tomorrow, the company
    had reported two days earlier, and the feed answered with Q4's estimate
    (0.9 against the quarter's real -0.34). The row kept the calendar's
    number and threw the feed's away — silently, until the drift measurement
    found it."""
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)
    # The calendar schedules 2026-08-03; the feed's numbers are for a print
    # three months out, i.e. a different quarter entirely.
    source = StubConsensus({"AMZN": reading(next_report_date=date(2026, 11, 2),
                                            eps_estimate=9.99)})

    report = capture_consensus(store, prints, source,
                               captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    snapshot = store.consensus_in_force(
        store.stock_by_ticker("AMZN").id, "2026-Q2")
    assert snapshot.eps_avg is None          # NOT 9.99
    assert snapshot.revenue_avg is None
    assert snapshot.eps_whisper is None
    assert snapshot.eps_calendar == 1.83     # the calendar's is still good
    assert report.skipped_feed_other_print == 1
    # The row is still written. Dropping it would lose a snapshot that cannot
    # be retaken, and the calendar's number on it is the usable one.
    assert "AMZN" in report.tickers


def test_the_refused_row_records_which_print_the_feed_was_answering_about(
        tmp_path):
    """A refusal nobody can audit is a refusal nobody can revisit. The feed's
    own statement of its target is kept on the row whether it was accepted or
    not."""
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)
    source = StubConsensus({"AMZN": reading(next_report_date=date(2026, 11, 2),
                                            quarter_end=date(2026, 9, 30),
                                            quarter_number=3)})

    capture_consensus(store, prints, source,
                      captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    snapshot = store.consensus_in_force(
        store.stock_by_ticker("AMZN").id, "2026-Q2")
    assert snapshot.feed_quarter_end == date(2026, 9, 30)
    assert snapshot.feed_quarter_number == 3
    assert snapshot.feed_report_date == date(2026, 11, 2)
    assert snapshot.source_note == "finnhub_only:feed_other_print"


def test_two_vendors_a_couple_of_days_apart_are_still_the_same_print(tmp_path):
    """One vendor dates a print by the session it is announced in and the
    other by the morning the wires carry it, so a day or two of disagreement
    is routine. The gap this rule exists to catch is a whole quarter."""
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)
    source = StubConsensus({"AMZN": reading(next_report_date=date(2026, 8, 5))})

    report = capture_consensus(store, prints, source,
                               captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    snapshot = store.consensus_in_force(
        store.stock_by_ticker("AMZN").id, "2026-Q2")
    assert snapshot.eps_avg == 1.83
    assert report.skipped_feed_other_print == 0


def test_a_feed_that_states_no_report_date_cannot_be_matched_and_is_refused(
        tmp_path):
    """Fail closed (invariant 6). "We could not check which print this is
    about" is exactly the state that produced the twenty bad rows, and using
    the numbers anyway is what made it invisible."""
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)
    source = StubConsensus({"AMZN": reading(next_report_date=None)})

    report = capture_consensus(store, prints, source,
                               captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    snapshot = store.consensus_in_force(
        store.stock_by_ticker("AMZN").id, "2026-Q2")
    assert snapshot.eps_avg is None
    assert snapshot.source_note == "finnhub_only:feed_target_unstated"
    assert report.skipped_feed_other_print == 1


def test_the_count_of_refusals_is_printed_in_japanese():
    """A refusal that only exists in the database is a refusal the reader
    never learns the size of."""
    from hawkeye.scout.prereg import CaptureReport, report_line

    line = report_line(CaptureReport(captured=3, skipped_feed_other_print=2))

    assert "別の決算についての予想だったため使わず 2 件" in line


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
    capture_consensus(store, prints,
                      StubConsensus({"BIIB": reading(eps_estimate=3.98)}),
                      captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))
    capture_consensus(store, prints,
                      StubConsensus({"BIIB": reading(eps_estimate=2.15)}),
                      captured_at=datetime(2026, 8, 3, 9, tzinfo=JST))

    stock_id = store.stock_by_ticker("BIIB").id
    assert [s.eps_avg for s in store.consensus_snapshots(stock_id, "2026-Q2")] \
        == [3.98, 2.15]


def test_a_feed_miss_still_pre_registers_the_calendar_estimate(tmp_path):
    """Degrading to one source is not the same as capturing nothing: the
    Finnhub point estimate is still a pre-registered number, and the absent
    second reading is what makes the pair unverifiable later — so it has to
    be visible in the row rather than inferred from silence."""
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)

    report = capture_consensus(store, prints, StubConsensus({}),
                               captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    stock_id = store.stock_by_ticker("AMZN").id
    snapshot = store.consensus_in_force(stock_id, "2026-Q2")
    assert report.captured == 2 and report.consensus_missing == 2
    assert snapshot.eps_calendar == 1.83
    assert snapshot.eps_avg is None and snapshot.source_note == "finnhub_only"


def test_a_feed_that_cannot_be_READ_is_counted_apart_from_a_missing_row(
        tmp_path):
    """Two different facts. "This company has no row" is about the company,
    and the calendar's estimate stands in for it; "the feed could not be
    reached" is about the connection, and a run where every name failed that
    way has to look different from a quiet day (invariant 6). Yahoo could not
    tell them apart — every failure came back as None."""
    store = make_store(tmp_path)
    prints = upcoming_prints(calendar_rows(), today=date(2026, 8, 2),
                             business_days=2)
    source = StubConsensus({"AMZN": WhispersUnavailable("connection reset"),
                            "BIIB": reading(eps_estimate=3.98)})

    report = capture_consensus(store, prints, source,
                               captured_at=datetime(2026, 8, 2, 9, tzinfo=JST))

    assert report.consensus_unreachable == 1
    assert report.consensus_missing == 0
    # ...and the name it could not read still keeps its calendar estimate
    snapshot = store.consensus_in_force(
        store.stock_by_ticker("AMZN").id, "2026-Q2")
    assert snapshot.eps_calendar == 1.83 and snapshot.eps_avg is None


def test_a_capture_with_no_numbers_at_all_writes_nothing(tmp_path):
    """A live run covers ~560 names over two business days, and plenty of
    them have neither a calendar estimate nor a reading from the feed. An
    empty row
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

    report = capture_consensus(store, prints,
                               StubConsensus({"AMZN": reading()}),
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
