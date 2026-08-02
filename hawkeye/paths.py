"""Where the system's own output goes.

The repository is split by *who writes the file*, not by what it contains:

  strategy/  investment knowledge a human writes or approves (tracked)
  docs/      system design and development notes (tracked)
  var/       everything the system emits while running (NOT tracked)

This module is the single answer to "where does var/ actually live". Each
location takes a dedicated environment variable, and `HAWKEYE_VAR` moves
the whole tree at once — the test suite uses that to keep runs off the
real ledger without having to know every individual name.
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_VAR_ROOT = "var"


def var_root() -> Path:
    """Root of the runtime-data tree (git-ignored)."""
    return Path(os.environ.get("HAWKEYE_VAR", _DEFAULT_VAR_ROOT))


def _dir(env_var: str, name: str) -> Path:
    override = os.environ.get(env_var)
    return Path(override) if override else var_root() / name


def db_path() -> str:
    """SQLite ledger. Returned as str — sqlite3.connect takes a path string."""
    override = os.environ.get("HAWKEYE_DB")
    return override if override else str(var_root() / "hawkeye.db")


def cases_dir() -> Path:
    """Tribunal case files (one JSON per evaluated candidate)."""
    return _dir("HAWKEYE_CASES", "cases")


def drops_dir() -> Path:
    """Drop-candidate measurements awaiting investigation."""
    return _dir("HAWKEYE_DROPS", "drops")


def reports_dir() -> Path:
    """Rendered run reports."""
    return _dir("HAWKEYE_REPORTS", "reports")
