"""Hand-supplied market data, for environments with no provider access.

Why this exists: `build_brief()` always calls a live provider, and
`build_snapshot()` raises on an empty bar list, so a network-restricted
environment (egress policy, no API key) cannot run a candidate at all — the
`--price/--market-cap/...` flags on `case open` are *overrides applied on
top of* provider data, not a replacement for it.

What this is NOT: a way to assert past a gate. Anything absent from the
file stays `None` and is flagged `unverified` by the gates exactly as a
provider gap would be, so a hard gate still fails closed (invariant 6).
The gates are untouched; only the source of the numbers changes.

Because a human typed these numbers, the record has to say so forever:
`provenance_note()` returns a line carrying the file's SHA-256, which the
CLI appends to the brief's notes before anything reaches the ledger. A
run whose inputs nobody can re-derive must at least be identifiable as
one.

Activated only by setting HAWKEYE_OFFLINE_DATA to the file path; the CLI
prints a warning on every run that uses it.

File format (JSON)::

    {
      "ticker": "SPCX",
      "profile": {"name": ..., "sector": ..., "market_cap": 1.43e12,
                  "next_earnings_date": "2026-11-04"},
      "bars": [{"day": "2026-07-06", "open": 165.95, "high": 167.90,
                "low": 155.04, "close": 160.42, "volume": 188831328}],
      "news": [{"headline": ..., "source": ..., "url": ...,
                "published_at": "2026-08-04T20:05:00+00:00",
                "summary": ...}]
    }

`bars` may be given newest-first; they are sorted oldest-first on load.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from hawkeye.contracts.models import NewsItem
from hawkeye.marketdata.base import Bar, StaticProvider

ENV_VAR = "HAWKEYE_OFFLINE_DATA"

_BAR_FIELDS = ("open", "high", "low", "close", "volume")


class OfflineDataError(ValueError):
    """The offline data file is missing, unreadable, or malformed."""


def offline_path() -> Optional[Path]:
    """The configured offline data file, or None when not in offline mode."""
    raw = os.environ.get(ENV_VAR)
    return Path(raw) if raw else None


def file_digest(path: Path) -> str:
    """SHA-256 of the file, so a stored record can name its own input."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def provenance_note(path: Path) -> str:
    """One line to append to a brief's notes.

    The point is that a reader of the ledger years later can tell this
    record's market data was hand-supplied, and can check whether a file
    they are holding is the one that produced it.
    """
    return (f"[OFFLINE DATA] market data hand-supplied, not fetched from a "
            f"provider. source file: {path} sha256: {file_digest(path)}")


def _parse_bar(row: dict, index: int) -> Bar:
    try:
        day = date.fromisoformat(str(row["day"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise OfflineDataError(
            f"bars[{index}]: missing or malformed 'day' (want YYYY-MM-DD)"
        ) from exc
    values = {}
    for field in _BAR_FIELDS:
        try:
            values[field] = float(row[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise OfflineDataError(
                f"bars[{index}] ({day}): missing or non-numeric '{field}'"
            ) from exc
    if values["high"] < values["low"]:
        raise OfflineDataError(f"bars[{index}] ({day}): high < low")
    return Bar(day=day, **values)


def _parse_news(rows: list) -> list[NewsItem]:
    items: list[NewsItem] = []
    for row in rows:
        published = row.get("published_at")
        if isinstance(published, str):
            try:
                published = datetime.fromisoformat(published)
            except ValueError:
                published = None
        items.append(NewsItem(
            headline=str(row.get("headline", "")),
            source=str(row.get("source", "")),
            url=str(row.get("url", "")),
            published_at=published,
            summary=str(row.get("summary", "")),
        ))
    return items


def load_offline_provider(path: Path) -> StaticProvider:
    """Build a StaticProvider from a hand-written JSON file.

    Raises OfflineDataError with an actionable message rather than letting
    a KeyError surface — the person hitting this is hand-editing the file.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OfflineDataError(f"{ENV_VAR} points at a missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OfflineDataError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise OfflineDataError(f"{path}: top level must be a JSON object")

    bar_rows = raw.get("bars")
    if not isinstance(bar_rows, list) or not bar_rows:
        raise OfflineDataError(
            f"{path}: 'bars' must be a non-empty list of daily OHLCV rows")
    bars = sorted((_parse_bar(row, i) for i, row in enumerate(bar_rows)),
                  key=lambda b: b.day)

    days = [b.day for b in bars]
    if len(set(days)) != len(days):
        raise OfflineDataError(f"{path}: 'bars' contains duplicate days")

    profile = raw.get("profile") or {}
    if not isinstance(profile, dict):
        raise OfflineDataError(f"{path}: 'profile' must be a JSON object")
    profile = dict(profile)
    next_earnings = profile.get("next_earnings_date")
    if isinstance(next_earnings, str):
        try:
            profile["next_earnings_date"] = date.fromisoformat(next_earnings)
        except ValueError as exc:
            raise OfflineDataError(
                f"{path}: 'next_earnings_date' must be YYYY-MM-DD") from exc

    news_rows = raw.get("news") or []
    if not isinstance(news_rows, list):
        raise OfflineDataError(f"{path}: 'news' must be a list")

    return StaticProvider(bars=bars, profile_data=profile,
                          news_items=_parse_news(news_rows))
