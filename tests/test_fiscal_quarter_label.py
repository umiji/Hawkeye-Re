"""Which quarter a print belongs to — decided in ONE place
(docs/backlog/EW_MIGRATION_DB_REVIEW_v2.ja.md §2).

Everything that joins a print to its consensus runs off this label:
`consensus_in_force`, `already_reviewed`, and the unique index
`(stock_id, fiscal_quarter, depth)`. A label that is merely plausible is worse
than none at all, because a wrong join is silent while a missing one is not.

The rule these tests pin down is that the calendar quarter OF THE REPORT DATE
is never a basis. A quarter ending in June is reported in August, so that
fallback lands one quarter ahead for almost every company (ADM below is the
worked example: 2026-Q2 reported on 2026-08-04, which the old fallback called
2026-Q3).
"""
import json
import pathlib
from datetime import date

import pytest

from hawkeye.contracts.stocks import (
    QuarterBasis,
    resolve_fiscal_quarter,
)
from hawkeye.ledger.stocks import StockStore
from hawkeye.marketdata.whispers import parse_details
from hawkeye.scout.earnings import EarningsEvent
from hawkeye.scout.prereg import capture_consensus, upcoming_prints
from hawkeye.scout.quality import print_from_event, reconstructed_consensus


# -- the resolver itself ---------------------------------------------------

def test_the_quarter_is_counted_back_from_the_companys_own_year_end():
    """ADM: quarter ending June 2026 inside a year ending December 2026."""
    label = resolve_fiscal_quarter(quarter_end=date(2026, 6, 30),
                                   year_end=date(2026, 12, 31))

    assert label.label == "2026-Q2"
    assert label.basis is QuarterBasis.FISCAL_REFERENCES


def test_a_january_year_end_puts_an_april_quarter_in_the_next_fiscal_year():
    """NVDA: the calendar quarter of 2026-04-30 is 2026-Q2, which is a
    quarter the company never reports."""
    label = resolve_fiscal_quarter(quarter_end=date(2026, 4, 30),
                                   year_end=date(2027, 1, 31))

    assert label.label == "2027-Q1"


def test_a_year_end_print_is_the_fourth_quarter_of_the_year_that_just_ended():
    """At a company's LAST quarter the feed's year-end reference has already
    rolled forward: PG's quarter ended June 2026 and its `fY1Ref` says June
    2027, twelve months apart. Read as "not a quarter of this year" it left
    every fiscal Q4 — one print in four, for every company — unlabelled, and
    an unlabelled print is refused by pre-registration and recorded with an
    empty quarter.

    Twelve months before a year end is unambiguous: it is the last quarter of
    the year before it.
    """
    label = resolve_fiscal_quarter(quarter_end=date(2026, 6, 30),
                                   year_end=date(2027, 6, 30),
                                   quarter_text=" fiscal fourth quarter "
                                                "ended June 2026")

    assert label.label == "2026-Q4"
    assert label.basis is QuarterBasis.FISCAL_REFERENCES


def test_a_year_end_reference_that_has_not_rolled_forward_is_the_same_quarter():
    """The other spelling of the same print: some rows report the year that
    just ended rather than the next one. Both have to land on Q4."""
    label = resolve_fiscal_quarter(quarter_end=date(2026, 6, 30),
                                   year_end=date(2026, 6, 30))

    assert label.label == "2026-Q4"


def test_a_quarter_word_that_contradicts_the_derived_number_withholds_it():
    """Two independent statements of the same fact; when they disagree the
    system does not get to pick one."""
    label = resolve_fiscal_quarter(quarter_end=date(2026, 6, 30),
                                   year_end=date(2026, 12, 31),
                                   quarter_text=" third quarter ended June 2026")

    assert label.label == ""
    assert label.basis is QuarterBasis.WITHHELD
    assert label.reason == "quarter_text_mismatch"


def test_a_quarter_word_that_agrees_leaves_the_derived_label_standing():
    label = resolve_fiscal_quarter(quarter_end=date(2026, 6, 30),
                                   year_end=date(2026, 12, 31),
                                   quarter_text=" second quarter ended June 2026")

    assert label.label == "2026-Q2"


def test_the_sources_own_year_and_quarter_are_used_when_no_reference_exists():
    label = resolve_fiscal_quarter(source_year=2026, source_quarter=2)

    assert label.label == "2026-Q2"
    assert label.basis is QuarterBasis.SOURCE_LABEL


def test_a_quarter_end_on_its_own_is_the_weakest_basis_and_says_so():
    """Right for a December year end, wrong for any other — so the basis is
    recorded rather than the label passed off as equally trustworthy."""
    label = resolve_fiscal_quarter(quarter_end=date(2026, 6, 30))

    assert label.label == "2026-Q2"
    assert label.basis is QuarterBasis.QUARTER_END


def test_nothing_to_derive_from_withholds_the_label_rather_than_guessing():
    label = resolve_fiscal_quarter()

    assert label.label == ""
    assert label.basis is QuarterBasis.WITHHELD
    assert label.reason == "no_fiscal_reference"


def test_the_resolver_takes_no_report_date_at_all():
    """The abolished fallback cannot be re-introduced by passing the report
    date in: there is no parameter for it."""
    with pytest.raises(TypeError):
        resolve_fiscal_quarter(report_date=date(2026, 8, 4))


# -- the feed --------------------------------------------------------------

def test_the_feeds_quarter_prose_is_checked_against_its_own_numbers():
    row = parse_details({"ticker": "ADM", "q1Ref": "202606", "fY1Ref": "202612",
                         "quarter": " third quarter ended June 2026"})

    assert row.fiscal_quarter is None
    assert "quarter_label_withheld" in row.gaps


def test_the_feeds_quarter_prose_agreeing_leaves_the_label_intact():
    row = parse_details({"ticker": "ADM", "q1Ref": "202606", "fY1Ref": "202612",
                         "quarter": " second quarter ended June 2026"})

    assert row.fiscal_quarter == "2026-Q2"
    assert "quarter_label_withheld" not in row.gaps


# -- the two places that used to guess from the report date ----------------

def _unlabelled_row() -> list[dict]:
    """ADM's real shape minus the calendar's own labelling: reported
    2026-08-04 for the quarter ending June. The old fallback called it
    2026-Q3, and the print that arrived labelled 2026-Q2 could then never
    find its own pre-registered consensus."""
    return [{"symbol": "ADM", "date": "2026-08-04", "epsEstimate": 1.42}]


_YEAR_END = pathlib.Path(__file__).parent / "fixtures" / "whispers_year_end"

# Recorded 2026-08-09, and kept APART from the 47-name corpus rather than
# merged into it: that corpus is a fixed sample taken on 2026-08-05 whose
# counts several measurements quote, and it contains no year-end print at all
# — which is why this shape went unnoticed until a live check found it.
_YEAR_END_QUARTERS = {"PG": "2026-Q4", "SJM": "2026-Q4", "CAG": "2026-Q4"}


@pytest.mark.parametrize("ticker,expected", sorted(_YEAR_END_QUARTERS.items()))
def test_a_recorded_year_end_print_gets_its_quarter(ticker, expected):
    record = parse_details(json.loads(
        (_YEAR_END / f"{ticker}.json").read_text(encoding="utf-8",
                                                 errors="replace")))

    assert record.fiscal_quarter == expected


def test_an_unlabelled_print_never_occupies_the_empty_quarter_slot(tmp_path):
    """One active row per (company, quarter) is what stops a print being
    recorded twice. An unlabelled row makes "" that company's quarter — so the
    NEXT print that also fails to get a label looks like a row already there,
    and the scan skips recording it entirely. A print nothing can join to its
    consensus is not recorded at all, the same way pre-registration already
    refuses one.
    """
    from hawkeye.contracts.stocks import EarningsPrint, PrintSource, Stock
    from hawkeye.scout.scout import _QuarterContext, _record_print

    store = StockStore(tmp_path / "s.db")
    stock_id = store.put_stock(Stock(cik=None, ticker="TEST", name="Test"))
    snapshot = reconstructed_consensus(
        EarningsEvent(ticker="TEST", day=date(2026, 5, 1), eps_actual=1.1,
                      eps_estimate=1.0, revenue_actual=1.0e9,
                      revenue_estimate=0.9e9), stock_id)
    consensus_id = store.capture_consensus(snapshot)

    def unlabelled(day: date, eps: float) -> _QuarterContext:
        return _QuarterContext(
            stock_id=stock_id, consensus_id=consensus_id,
            consensus=store.consensus(consensus_id),
            print_row=EarningsPrint(stock_id=stock_id, ticker="TEST",
                                    fiscal_quarter="", report_date=day,
                                    source=PrintSource.WHISPERS,
                                    eps_actual=eps))

    _record_print(store, unlabelled(date(2026, 5, 1), 1.10))
    _record_print(store, unlabelled(date(2026, 8, 1), 1.30))   # must not raise

    assert store.active_print(stock_id, "") is None


def test_a_calendar_row_without_a_fiscal_label_stays_unlabelled():
    prints = upcoming_prints(_unlabelled_row(), today=date(2026, 8, 3),
                             business_days=2)

    assert [p.fiscal_quarter for p in prints] == [""]


def test_a_calendar_row_with_a_fiscal_label_keeps_it():
    prints = upcoming_prints(
        [{"symbol": "ADM", "date": "2026-08-04", "epsEstimate": 1.42,
          "year": 2026, "quarter": 2}],
        today=date(2026, 8, 3), business_days=2)

    assert [p.fiscal_quarter for p in prints] == ["2026-Q2"]


def test_an_unlabelled_print_is_not_pre_registered_but_is_counted(tmp_path):
    """Not written, and not silently dropped either: a consensus row under a
    label nothing will ever join to is worse than no row, but a run that
    quietly skipped names would hide how often the calendar omits them."""
    store = StockStore(str(tmp_path / "hawkeye.db"))
    prints = upcoming_prints(_unlabelled_row(), today=date(2026, 8, 3),
                             business_days=2)

    report = capture_consensus(store, prints, source=None)

    assert report.captured == 0
    assert report.skipped_unlabelled == 1
    assert report.as_dict()["skipped_unlabelled"] == 1


def _adm_event() -> EarningsEvent:
    return EarningsEvent(ticker="ADM", day=date(2026, 8, 4), eps_actual=1.84,
                         eps_estimate=1.42, revenue_actual=None,
                         revenue_estimate=None)


def test_a_print_row_is_left_unlabelled_rather_than_labelled_from_its_date():
    row = print_from_event(_adm_event(), stock_id="cik:0000007084")

    assert row.fiscal_quarter == ""


def test_a_reconstructed_consensus_is_left_unlabelled_the_same_way():
    snapshot = reconstructed_consensus(_adm_event(), stock_id="cik:0000007084")

    assert snapshot.fiscal_quarter == ""
