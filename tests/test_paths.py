"""Runtime-data paths all live under var/ and stay overridable.

The split this locks in: anything a human writes or approves is tracked in
git (`strategy/`, `docs/`); anything the system emits at run time goes to
`var/` and is ignored by git. A default that silently drops the SQLite
ledger back in the repo root would put machine output under version
control, which is the exact confusion this layout exists to remove.
"""
from __future__ import annotations

from pathlib import Path

from hawkeye import paths


def test_db_defaults_under_var(monkeypatch):
    monkeypatch.delenv("HAWKEYE_DB", raising=False)
    monkeypatch.delenv("HAWKEYE_VAR", raising=False)
    assert Path(paths.db_path()) == Path("var/hawkeye.db")


def test_db_env_override_wins(monkeypatch):
    monkeypatch.setenv("HAWKEYE_DB", "/tmp/other.db")
    assert paths.db_path() == "/tmp/other.db"


def test_runtime_dirs_default_under_var(monkeypatch):
    monkeypatch.delenv("HAWKEYE_VAR", raising=False)
    for name in ("HAWKEYE_CASES", "HAWKEYE_DROPS", "HAWKEYE_REPORTS"):
        monkeypatch.delenv(name, raising=False)
    assert paths.cases_dir() == Path("var/cases")
    assert paths.drops_dir() == Path("var/drops")
    assert paths.reports_dir() == Path("var/reports")


def test_var_root_env_relocates_everything(monkeypatch, tmp_path):
    """One switch moves the whole runtime tree — used by the test suite."""
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    for name in ("HAWKEYE_DB", "HAWKEYE_CASES", "HAWKEYE_DROPS", "HAWKEYE_REPORTS"):
        monkeypatch.delenv(name, raising=False)
    assert Path(paths.db_path()) == tmp_path / "hawkeye.db"
    assert paths.cases_dir() == tmp_path / "cases"
    assert paths.drops_dir() == tmp_path / "drops"
    assert paths.reports_dir() == tmp_path / "reports"


def test_per_dir_env_overrides_var_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HAWKEYE_VAR", str(tmp_path))
    monkeypatch.setenv("HAWKEYE_CASES", str(tmp_path / "elsewhere"))
    assert paths.cases_dir() == tmp_path / "elsewhere"


def test_casefile_reuses_the_same_resolver(monkeypatch, tmp_path):
    """casefile must not keep its own copy of the default (it would drift)."""
    from hawkeye.tribunal import casefile

    monkeypatch.setenv("HAWKEYE_CASES", str(tmp_path / "cases"))
    assert casefile.cases_dir() == paths.cases_dir()
