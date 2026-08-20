"""The name the scan's full-inventory CSV is saved under (T-019).

That file is the only complete record of a scan the user can open — the
screen omits both names and columns — so the name has to say WHEN the scan
ran. The ledger's scan number alone does not: it is a running count, and
`scan-2.csv` could be yesterday's or last month's.
"""
from __future__ import annotations

import argparse

import pytest

from hawkeye import cli
from hawkeye.cli import _scan_csv_name, cmd_report_scan
from hawkeye.ledger.store import Ledger
from hawkeye.paths import db_path, reports_dir

# The real scan 3, as the ledger holds it: a JST timestamp, and an id that is
# a count rather than a date.
SCAN_3 = {"id": 3, "ts": "2026-08-19T00:24:22.068839+09:00"}


def test_scan_csv_name_dates_the_file_by_the_day_the_scan_ran():
    assert _scan_csv_name(SCAN_3) == \
        "2026-08-19-03-hawkeye-earnings-research.csv"


def test_scan_csv_name_does_not_date_the_file_by_today():
    """`hawkeye report scan --scan-id 3` re-run weeks later must still name
    the day of the scan — the file describes that scan, not this rendering."""
    name = _scan_csv_name({"id": 1, "ts": "2026-08-17T13:14:07.646439+09:00"})

    assert name.startswith("2026-08-17-")


@pytest.mark.parametrize("scan_id,expected", [(3, "-03-"), (12, "-12-"),
                                              (100, "-100-")])
def test_scan_csv_name_pads_the_scan_number_to_two_digits(scan_id, expected):
    """Zero-padded so a day's files sort in scan order; a three-digit scan
    simply widens rather than being truncated."""
    name = _scan_csv_name({"id": scan_id, "ts": SCAN_3["ts"]})

    assert expected in name


def _one_scan_in_a_fresh_ledger(tmp_path, monkeypatch) -> int:
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    ledger = Ledger(db_path())
    return ledger.record_scan({"window_start": "2026-08-18",
                               "window_end": "2026-08-18"},
                              scanned=1, screened=0, enriched=0,
                              gate_passed=0, tickers=[])


def test_report_scan_saves_under_the_new_default_name(tmp_path, monkeypatch,
                                                      capsys):
    scan_id = _one_scan_in_a_fresh_ledger(tmp_path, monkeypatch)

    assert cmd_report_scan(argparse.Namespace(
        scan_id=scan_id, top=3, csv=None)) == 0

    written = list(reports_dir().glob("*.csv"))
    assert len(written) == 1
    assert written[0].name.endswith(f"-{scan_id:02d}-hawkeye-earnings-research.csv")
    assert str(written[0]) in capsys.readouterr().out


def test_report_scan_still_honours_an_explicit_csv_path(tmp_path, monkeypatch):
    """`--csv` is untouched by the rename: an operator who names a path gets
    that path, and no second copy appears under the default name."""
    scan_id = _one_scan_in_a_fresh_ledger(tmp_path, monkeypatch)
    chosen = tmp_path / "somewhere" / "mine.csv"

    assert cmd_report_scan(argparse.Namespace(
        scan_id=scan_id, top=3, csv=str(chosen))) == 0

    assert chosen.is_file()
    assert list(reports_dir().glob("*.csv")) == []
