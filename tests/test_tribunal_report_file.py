"""A completed round (gate-only rejection or a full tribunal verdict) must
leave a Markdown document behind, not just terminal output that scrolls
away once the session ends — one document per round, with nothing silently
taking another's place on disk (T-017)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hawkeye import cli
from hawkeye.cli import _write_tribunal_report
from hawkeye.contracts.models import GateReport, GateResult
from hawkeye.reports.render_ja import render_recommendation_ja
from hawkeye.tribunal.pipeline import gate_only_recommendation
from tests.conftest import make_brief


def _gate_rejected_recommendation(ticker: str = "TEST"):
    gates = GateReport(results=[
        GateResult(name="avg_dollar_volume_20d_min", passed=False, hard=True,
                   value=180_000.0, threshold=1_000_000.0),
    ])
    brief = make_brief()
    rec = gate_only_recommendation(brief, gates)
    if ticker == rec.ticker:
        return rec
    return rec.model_copy(update={
        "ticker": ticker,
        "brief": brief.model_copy(update={"ticker": ticker})})


def _freeze_clock(monkeypatch) -> None:
    """Pin the second the filename is stamped with.

    Two rounds finishing inside one second is the collision T-017 is about,
    and reproducing it by running fast enough would make the test a race.
    """
    class _Frozen:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 8, 19, 0, 52, 6, tzinfo=timezone.utc)

    monkeypatch.setattr(cli, "datetime", _Frozen)


def test_write_tribunal_report_creates_the_reports_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HAWKEYE_REPORTS", str(tmp_path / "reports"))
    rec = _gate_rejected_recommendation()

    path = _write_tribunal_report(rec)

    assert path.parent == tmp_path / "reports"
    assert path.parent.is_dir()


def test_write_tribunal_report_filename_matches_the_agreed_convention(
        monkeypatch, tmp_path):
    monkeypatch.setenv("HAWKEYE_REPORTS", str(tmp_path))
    rec = _gate_rejected_recommendation("RNW")

    path = _write_tribunal_report(rec)

    # yymmdd-HHMMSS-TICKER-tribunal-report.md — 6 digits, 6 digits, then the
    # ticker. Not asserting the exact digits: that would pin the test to
    # whatever instant it happens to run at.
    stem = path.name.removesuffix("-tribunal-report.md")
    date_part, time_part, ticker_part = stem.split("-")
    assert len(date_part) == 6 and date_part.isdigit()
    assert len(time_part) == 6 and time_part.isdigit()
    assert ticker_part == "RNW"


def test_two_reports_finished_in_the_same_second_both_survive(
        monkeypatch, tmp_path):
    """The defect T-017 fixes: on 2026-08-19 RNW and PONY both finished at
    00:52:06 and the second write erased the first one's document."""
    monkeypatch.setenv("HAWKEYE_REPORTS", str(tmp_path))
    _freeze_clock(monkeypatch)
    first = _gate_rejected_recommendation("RNW")
    second = _gate_rejected_recommendation("PONY")

    first_path = _write_tribunal_report(first)
    second_path = _write_tribunal_report(second)

    assert first_path != second_path
    assert first_path.is_file() and second_path.is_file()
    assert first_path.read_text(encoding="utf-8") == \
        render_recommendation_ja(first)
    assert second_path.read_text(encoding="utf-8") == \
        render_recommendation_ja(second)


def test_write_tribunal_report_never_overwrites_an_existing_file(
        monkeypatch, tmp_path):
    """Same ticker, same second — the rarer collision, and the one where an
    overwrite would destroy a document that is not even distinguishable by
    name from the new one."""
    monkeypatch.setenv("HAWKEYE_REPORTS", str(tmp_path))
    _freeze_clock(monkeypatch)
    rec = _gate_rejected_recommendation("RNW")
    taken = _write_tribunal_report(rec)
    taken.write_text("EARLIER ROUND", encoding="utf-8")

    path = _write_tribunal_report(rec)

    assert path != taken
    assert taken.read_text(encoding="utf-8") == "EARLIER ROUND"
    assert path.read_text(encoding="utf-8") == render_recommendation_ja(rec)


def test_write_tribunal_report_replaces_characters_a_filename_cannot_hold(
        monkeypatch, tmp_path):
    """Tickers arrive from the vendors unchecked, and a class-share notation
    like `BF/B` would make the save raise — losing the round's only
    document (User decision 2026-08-20: substitute, do not fail)."""
    monkeypatch.setenv("HAWKEYE_REPORTS", str(tmp_path))
    rec = _gate_rejected_recommendation("BF/B")

    path = _write_tribunal_report(rec)

    assert path.is_file()
    assert path.parent == tmp_path
    assert "BF-B" in path.name


def test_write_tribunal_report_content_matches_the_rendered_report(
        monkeypatch, tmp_path):
    monkeypatch.setenv("HAWKEYE_REPORTS", str(tmp_path))
    rec = _gate_rejected_recommendation()

    path = _write_tribunal_report(rec)

    assert path.read_text(encoding="utf-8") == render_recommendation_ja(rec)


def test_gate_rejection_report_names_the_failing_gate(monkeypatch, tmp_path):
    """The user asked for a summary of which gate and why — this is already
    carried in the verdict rationale gate_only_recommendation builds, so the
    saved file inherits it for free."""
    monkeypatch.setenv("HAWKEYE_REPORTS", str(tmp_path))
    rec = _gate_rejected_recommendation()

    path = _write_tribunal_report(rec)

    text = path.read_text(encoding="utf-8")
    assert "avg_dollar_volume_20d_min" in text
    assert "180000" in text.replace(",", "").replace(".0", "")
