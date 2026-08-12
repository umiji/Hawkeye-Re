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
    """SQLite ledger. Returned as str — sqlite3.connect takes a path string.

    The containing directory is created here. SQLite will make the file but
    not the folder, and `var/` only happens to exist in a working checkout —
    so pointing `HAWKEYE_VAR` at a fresh location used to fail every command
    with "unable to open database file" before it did anything at all.
    """
    override = os.environ.get("HAWKEYE_DB")
    # The override is returned verbatim — callers pass it to sqlite3, and
    # round-tripping it through Path would rewrite separators on Windows.
    resolved = override if override else str(var_root() / "hawkeye.db")
    parent = Path(resolved).parent
    if str(parent) and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    return resolved


def cases_dir() -> Path:
    """Tribunal case files (one JSON per evaluated candidate)."""
    return _dir("HAWKEYE_CASES", "cases")


def drops_dir() -> Path:
    """Drop-candidate measurements awaiting investigation."""
    return _dir("HAWKEYE_DROPS", "drops")


def guidance_dir() -> Path:
    """Forward statements a scan read but has not had extracted yet.

    Only session mode fills this: with an API key the extraction happens
    inside the scan and nothing is ever staged. The files are what let an
    interrupted round resume instead of re-running the scan, exactly as
    `drops/` does for the drop reviews.
    """
    return _dir("HAWKEYE_GUIDANCE", "guidance")


def reports_dir() -> Path:
    """Rendered run reports."""
    return _dir("HAWKEYE_REPORTS", "reports")


def scan_dir() -> Path:
    """A scan awaiting ranking (docs/design/RANK_AFTER_GUIDANCE.ja.md).

    `hawkeye scout` judges every candidate the moment it walks past it, which
    is before the guidance queue above can possibly be empty — so the score
    it computes is provisional. It writes the whole `ScoutResult` here
    instead of recording it, and `hawkeye rank` reads it back once the queue
    is drained, re-scores, and only THEN commits to the ledger. One scan at a
    time, same as the guidance queue it waits on.
    """
    return _dir("HAWKEYE_SCAN", "scan")


def scan_work_path() -> Path:
    """The one pending scan, if any."""
    return scan_dir() / "pending.json"
