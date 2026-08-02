"""Ticker -> SEC registrant number (CIK), from EDGAR's own directory.

The CIK is the stock master's key (§6.1): a ticker is reused after a
delisting and changes on a rename, so keying on one silently merges two
companies' histories. The CIK is also the address every EDGAR request needs,
so it is a value the system has to hold anyway.

A lookup that fails returns `None` and the caller falls back to a provisional
id. That is deliberate: refusing to record a company because EDGAR was
unreachable would lose the print, and a print not captured before its release
can never be captured at all.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Callable, Optional

_DIRECTORY_URL = "https://www.sec.gov/files/company_tickers.json"
# SEC requires a contact address in the User-Agent and rejects requests
# without one. Overridable so a fork does not announce someone else's email.
_DEFAULT_UA = os.environ.get("HAWKEYE_SEC_USER_AGENT",
                             "Hawkeye research tool (contact: hawkeye@example.com)")


def _fetch_directory() -> list[dict]:
    request = urllib.request.Request(_DIRECTORY_URL,
                                     headers={"User-Agent": _DEFAULT_UA})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    return list(payload.values()) if isinstance(payload, dict) else list(payload)


class EdgarDirectory:
    """Lazily-loaded ticker directory. Fetched at most once per instance."""

    def __init__(self, fetcher: Optional[Callable[[], list[dict]]] = None):
        self._fetcher = fetcher or _fetch_directory
        self._entries: Optional[dict[str, dict]] = None

    def _load(self) -> dict[str, dict]:
        if self._entries is not None:
            return self._entries
        try:
            rows = self._fetcher()
        except Exception as exc:                   # noqa: BLE001
            print(f"EDGAR の銘柄一覧を取得できませんでした ({exc}) — "
                  f"CIK 不明の銘柄は暫定IDで記録します", file=sys.stderr)
            rows = []
        entries: dict[str, dict] = {}
        for row in rows:
            ticker = str(row.get("ticker") or "").strip().upper()
            if ticker:
                entries.setdefault(ticker, row)
        self._entries = entries
        return entries

    def cik_for(self, ticker: str) -> Optional[str]:
        """The zero-padded 10-digit CIK, or None when EDGAR has no match."""
        entry = self._load().get(ticker.strip().upper())
        if entry is None:
            return None
        raw = str(entry.get("cik_str") or entry.get("cik") or "").strip()
        return raw.lstrip("0").zfill(10) if raw.strip("0") else None

    def name_for(self, ticker: str) -> str:
        entry = self._load().get(ticker.strip().upper())
        return str(entry.get("title") or "") if entry else ""
