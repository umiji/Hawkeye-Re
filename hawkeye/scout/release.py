"""The escalation at the tail of the funnel: read the company's own release
when the two vendors disagree about what it said (§5.3 実装順序(c)).

Placed at the tail on purpose. The read costs ~8,900 tokens a name and the
disagreement it settles occurs in 21% of prints, so running it over the whole
calendar would dominate the run; running it over the ten or fifteen names
that survived every cheaper filter costs about one escalation per run.

Two things this module deliberately does NOT do:

- **It never settles the dispute with XBRL alone.** XBRL says which value was
  FILED; the disagreement is about which basis the CONSENSUS is on. Apple
  filed 2.02 while the street compared against 1.91, the $0.11 difference
  being a tariff refund named only in the release prose. Trusting XBRL here
  would produce a confident comparison of two different quantities.
- **It never computes a non-GAAP figure.** A published adjusted number is
  read; a per-share one-off the company itself quantified may be subtracted
  from GAAP because the company did the arithmetic's judgment part. Where
  neither exists the print stays `unadjusted` and the leg stays unverified —
  the row deepens anyway, because "we read it and it named nothing" is a
  different fact from "we never read it" (invariant 6).

The extraction arrives from outside — an agent or a human reading the
release, since no structured source for guidance or non-GAAP exists on any
free tier. It is never trusted on sight: its GAAP EPS must equal the value
SEC holds for that quarter or the entire document is discarded, which catches
the commonest misreading (taking the year-ago column) mechanically.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, Protocol

from hawkeye.contracts.stocks import (
    EarningsPrint,
    EpsBasis,
    GuidanceReading,
    PrintDepth,
)
from hawkeye.marketdata.edgar_facts import extraction_matches_filing

_EPS_TAG = "EarningsPerShareDiluted"


class ReleaseReader(Protocol):
    def release(self, ticker: str, report_date: date) -> Optional[dict]:
        """The extracted release document, or None when none was obtained."""
        ...


@dataclass(frozen=True)
class ReleaseOutcome:
    """The reading after the escalation, and whether it changed anything.

    `row` is always usable: on rejection it is the untouched original, so a
    caller never has to remember which branch it took.
    """
    row: EarningsPrint
    accepted: bool
    reason: str


def needs_release_read(quality) -> bool:
    """Whether a document could lift this leg.

    Narrow by design. A thin consensus, a near-zero denominator or a
    single-source estimate are all unverified for reasons no release can
    address — escalating them would spend the budget where it cannot help.
    """
    return "actual_disputed" in quality.eps.flags


def filed_eps(facts, cik: Optional[str], report_date: date) -> Optional[float]:
    """The diluted EPS SEC holds for that quarter, or None.

    None when there is no XBRL, no CIK, or no facts source — each of which
    means the extraction cannot be checked, and an unverifiable extraction is
    refused rather than believed.
    """
    if facts is None or not cik:
        return None
    fact = facts.quarterly(cik, _EPS_TAG, report_date)
    return fact.value if fact is not None else None


def _guidance_of(extraction: dict) -> Optional[GuidanceReading]:
    payload = extraction.get("guidance") or {}
    return GuidanceReading(**payload) if payload else None


def _adjusted_eps(extraction: dict) -> tuple[Optional[float], EpsBasis, str]:
    """What the company itself said its comparable EPS was.

    Published figure first. A one-off is only usable when the company put a
    per-share number on it — Amazon disclosed $53.4B pre-tax at the
    consolidated level and no per-share figure anywhere, and turning that into
    an EPS adjustment would be our arithmetic wearing the company's authority.
    """
    published = extraction.get("non_gaap_eps")
    if published is not None:
        return float(published), EpsBasis.ADJUSTED, ""
    one_off = extraction.get("one_off_per_share")
    gaap = extraction.get("gaap_eps_diluted")
    if one_off is not None and gaap is not None:
        return round(float(gaap) - float(one_off), 4), EpsBasis.ADJUSTED, ""
    return None, EpsBasis.UNADJUSTED, "no_published_adjustment"


def apply_release(row: EarningsPrint, extraction: Optional[dict],
                  filed_eps: Optional[float]) -> ReleaseOutcome:
    """Fold a validated reading of the release into the print row."""
    if not extraction:
        return ReleaseOutcome(row=row, accepted=False, reason="no_extraction")
    if not extraction_matches_filing(extraction.get("gaap_eps_diluted"),
                                     filed_eps):
        return ReleaseOutcome(
            row=row, accepted=False,
            reason=(f"extracted GAAP EPS "
                    f"{extraction.get('gaap_eps_diluted')} does not equal the "
                    f"filed {filed_eps}; the whole extraction is discarded"))
    eps_release, basis, flag = _adjusted_eps(extraction)
    flags = list(row.contamination_flags) + ([flag] if flag else [])
    deepened = row.model_copy(update={
        "depth": PrintDepth.RELEASE_READ,
        "eps_xbrl_diluted": filed_eps,
        "eps_release": eps_release,
        "eps_basis": basis,
        "one_off_per_share": extraction.get("one_off_per_share"),
        "guidance": _guidance_of(extraction) or row.guidance,
        "contamination_flags": flags,
        "notes": (row.notes or extraction.get("source_url") or "")})
    return ReleaseOutcome(row=deepened, accepted=True, reason="release_read")


def release_key(row: EarningsPrint) -> str:
    """How one print's extraction is named, everywhere.

    Ticker AND report date, because a company files four of these a year and
    the wrong quarter's document would be checked against the wrong filing —
    rejected, but for a reason nobody could read off the message.
    """
    return f"{row.ticker.upper()}_{row.report_date.isoformat()}"


def parse_release_key(key: str) -> tuple[str, date]:
    """The inverse of `release_key`, for the record that holds a print open.

    The ledger stores the ticker and the report date as columns rather than
    the joined string, so the age bound is a date comparison rather than
    string surgery — and so a malformed key fails here, where the caller can
    see it, instead of silently never matching anything.
    """
    ticker, _, day = key.rpartition("_")
    if not ticker:
        raise ValueError(f"not a release key: {key!r}")
    return ticker.upper(), date.fromisoformat(day)


class DirectoryReleaseReader:
    """Extractions dropped in a folder, one JSON per print.

    The read itself happens outside this process — an agent or a human opens
    the 8-K, because no free structured source for guidance or non-GAAP EPS
    exists — so the funnel's side of it is a lookup. The file is keyed by
    ticker AND report date: a company files four of these a year, and last
    quarter's document would be checked against this quarter's filing and
    rejected for the wrong reason.
    """

    def __init__(self, directory) -> None:
        self.directory = Path(directory)

    def path_for(self, ticker: str, report_date: date) -> Path:
        return (self.directory /
                f"{ticker.upper()}_{report_date.isoformat()}.json")

    def release(self, ticker: str, report_date: date) -> Optional[dict]:
        path = self.path_for(ticker, report_date)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:              # noqa: BLE001
            print(f"{path} を読めませんでした ({exc}) — この銘柄のEPSは"
                  f"未検証のままにします", file=sys.stderr)
            return None


def read_release(row: EarningsPrint, quality, reader,
                 facts, cik: Optional[str]) -> Optional[ReleaseOutcome]:
    """Escalate one print, or return None when no escalation was warranted."""
    if reader is None or not needs_release_read(quality):
        return None
    return apply_release(row, reader.release(row.ticker, row.report_date),
                         filed_eps(facts, cik, row.report_date))
