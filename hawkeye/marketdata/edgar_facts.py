"""Company facts from EDGAR's XBRL API — the free, deterministic check.

Two jobs, both narrow:

1. **Validate a reading of an earnings release.** Anything extracted from
   prose is accepted only if the GAAP EPS it produced equals the value SEC
   holds for that quarter. The most common way a release is misread is by
   taking the year-ago column, and that mistake is impossible to miss here:
   last year's number cannot equal this quarter's filed one.
2. **Supply revenue where the vendors cannot.** Finnhub's revenue matched the
   filings 22/22 in the measured sample, so the two agreeing is the normal
   case — but XBRL is the one with a primary source behind it.

Duration is what makes the lookup correct. A 10-Q files the quarter AND the
year-to-date figure with the SAME period end, so matching on the end date
alone picks a six-month total roughly twice the size of the quarter, which
against a quarterly consensus reads as an enormous beat.

Absent XBRL (26% of the measured sample: banks, insurers, REITs, annual
periods) returns None. None is unverified — never zero, never agreement.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from hawkeye.marketdata.edgar import _DEFAULT_UA

_CONCEPT_URL = ("https://data.sec.gov/api/xbrl/companyconcept/"
                "CIK{cik}/us-gaap/{tag}.json")

# A quarterly period, generously bounded: 13 weeks is 91 days, and 52/53-week
# fiscal calendars shift the ends around. A year-to-date figure starts at 180+
# days, so the gap between the two is wide and unambiguous.
_MIN_QUARTER_DAYS = 60
_MAX_QUARTER_DAYS = 130

# A cent. EPS is filed to the cent, so anything closer is representation
# noise and anything further is a different number.
_EPS_TOLERANCE = 0.005


@dataclass(frozen=True)
class XbrlFact:
    tag: str
    value: float
    period_end: date
    period_start: Optional[date] = None
    form: str = ""
    filed: Optional[date] = None
    fiscal_year: Optional[int] = None
    fiscal_period: str = ""

    @property
    def duration_days(self) -> Optional[int]:
        if self.period_start is None:
            return None
        return (self.period_end - self.period_start).days


def _fetch_concept(cik: str, tag: str) -> dict:
    url = _CONCEPT_URL.format(cik=cik, tag=tag)
    request = urllib.request.Request(url, headers={"User-Agent": _DEFAULT_UA})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _as_date(value) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


class EdgarFacts:
    """One company concept at a time, cached per (CIK, tag)."""

    def __init__(self,
                 fetcher: Optional[Callable[[str, str], dict]] = None) -> None:
        self._fetcher = fetcher or _fetch_concept
        self._cache: dict[tuple[str, str], list[XbrlFact]] = {}

    def concept(self, cik: str, tag: str) -> list[XbrlFact]:
        key = (cik, tag)
        if key in self._cache:
            return self._cache[key]
        try:
            payload = self._fetcher(cik, tag)
        except Exception as exc:                   # noqa: BLE001
            print(f"EDGAR XBRL {tag} を取得できませんでした ({exc}) — "
                  f"この値は未検証のままにします", file=sys.stderr)
            payload = {}
        facts: list[XbrlFact] = []
        for rows in (payload.get("units") or {}).values():
            for row in rows:
                end = _as_date(row.get("end"))
                value = row.get("val")
                if end is None or value is None:
                    continue
                facts.append(XbrlFact(
                    tag=tag, value=float(value), period_end=end,
                    period_start=_as_date(row.get("start")),
                    form=str(row.get("form") or ""),
                    filed=_as_date(row.get("filed")),
                    fiscal_year=row.get("fy"),
                    fiscal_period=str(row.get("fp") or "")))
        self._cache[key] = facts
        return facts

    def quarterly(self, cik: str, tag: str,
                  report_date: date) -> Optional[XbrlFact]:
        """The quarter this print reported, or None.

        "The quarter" means a period of roughly 13 weeks ending on or before
        the release — not merely the newest fact, which would be the
        year-to-date figure filed alongside it.
        """
        candidates = [f for f in self.concept(cik, tag)
                      if f.period_end <= report_date
                      and f.duration_days is not None
                      and _MIN_QUARTER_DAYS <= f.duration_days
                      <= _MAX_QUARTER_DAYS]
        if not candidates:
            return None
        return max(candidates, key=lambda f: (f.period_end,
                                              f.filed or date.min))


def extraction_matches_filing(extracted: Optional[float],
                              filed: Optional[float]) -> bool:
    """Whether a value read from a release agrees with the filed one.

    False when there is nothing to compare against. That is the point: an
    unverifiable extraction must not travel as a verified one, and "no XBRL"
    is a coverage limit rather than a pass (invariant 6).
    """
    if extracted is None or filed is None:
        return False
    return abs(extracted - filed) <= _EPS_TOLERANCE
