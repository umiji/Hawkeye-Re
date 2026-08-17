"""A completed round (gate-only rejection or a full tribunal verdict) must
leave a Markdown document behind, not just terminal output that scrolls
away once the session ends."""
from __future__ import annotations

from pathlib import Path

from hawkeye.cli import _write_tribunal_report
from hawkeye.contracts.models import GateReport, GateResult
from hawkeye.reports.render_ja import render_recommendation_ja
from hawkeye.tribunal.pipeline import gate_only_recommendation
from tests.conftest import make_brief


def _gate_rejected_recommendation():
    gates = GateReport(results=[
        GateResult(name="avg_dollar_volume_20d_min", passed=False, hard=True,
                   value=180_000.0, threshold=1_000_000.0),
    ])
    return gate_only_recommendation(make_brief(), gates)


def test_write_tribunal_report_creates_the_reports_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HAWKEYE_REPORTS", str(tmp_path / "reports"))
    rec = _gate_rejected_recommendation()

    path = _write_tribunal_report(rec)

    assert path.parent == tmp_path / "reports"
    assert path.parent.is_dir()


def test_write_tribunal_report_filename_matches_the_agreed_convention(
        monkeypatch, tmp_path):
    monkeypatch.setenv("HAWKEYE_REPORTS", str(tmp_path))
    rec = _gate_rejected_recommendation()

    path = _write_tribunal_report(rec)

    # yymmdd-HHMMSS-tribunal-report.md — 6 digits, then 6 digits, then the
    # fixed suffix. Not asserting the exact digits: that would pin the test
    # to whatever instant it happens to run at.
    stem = path.name.removesuffix("-tribunal-report.md")
    date_part, _, time_part = stem.partition("-")
    assert len(date_part) == 6 and date_part.isdigit()
    assert len(time_part) == 6 and time_part.isdigit()


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
