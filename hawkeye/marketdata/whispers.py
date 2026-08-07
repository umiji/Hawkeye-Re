"""EarningsWhispers — the single source of earnings numbers.

`GET /api/epsdetails/{ticker}` answers with one JSON object carrying, for the
company's most recent print: EPS actual and consensus, revenue actual and
consensus, the fiscal quarter it belongs to, the announcement timestamp, and a
prose summary that contains the company's guidance. One request covers all
three legs, which is why it replaced the Yahoo/Finnhub pair for numbers
(measured 2026-08-05; see docs/backlog/EARNINGS_WHISPERS_SOURCE.ja.md).

Three properties of the feed shape everything in this module:

1. **Revenue is in millions.** Every contract in this system is in dollars, so
   the conversion happens here, once. Missing it is a 1,000,000x beat.
2. **The record is the company's LATEST print, not the print you asked
   about.** Until the feed ingests today's release it answers with last
   quarter — same shape, same fields, entirely different quarter. `covers()`
   is what refuses that row; nothing downstream can tell the difference.
3. **Absence is spelled with sentinels.** `whisper == 999` and
   `ewGrade == -999.0` mean "none", and reading them as numbers produces
   confident nonsense.

Every gap is named in `WhispersRecord.gaps` rather than left as a bare None,
so "we could not read it" is never indistinguishable from "the company did not
report it" (invariant 6).
"""
from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from hawkeye.contracts.stocks import GuidanceReading, resolve_fiscal_quarter

_BASE = "https://www.earningswhispers.com"
# The endpoint is the site's own front-end API and answers a browser request.
# A default httpx User-Agent gets the HTML shell instead of JSON.
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# The feed's timestamps are US Eastern wall-clock without an offset. Attaching
# it here is what makes "48 hours since the announcement" a real duration
# rather than eight hours of guesswork between a pre-market and an after-close
# print.
EASTERN = ZoneInfo("America/New_York")

_MILLIONS = 1_000_000.0
_NO_WHISPER = 999.0
# Vendors disagree by a day on prints released after the close, so a record
# whose announcement sits one day either side of the calendar's date is the
# same print. Two days apart is a different quarter's print.
_SAME_PRINT_DAYS = 1

# How many extra attempts a failure is worth, by kind.
#
# Measured 2026-08-08, and it corrected an earlier reading: a 50-request run
# loses 1 to 5 names to HTTP 500, and those 500s are NOT sporadic. They are a
# property of the ticker — INOD, UMAC and GAIN return the same 96KB HTML error
# page on every attempt, minutes apart, while ADM in the same loop answers
# JSON. The rate holds at 177 req/min throughout, so nothing is throttling.
#
# So a 5xx is retried ONCE, only to establish that it is the ticker and not
# the moment. A connection error is a different animal — nothing came back at
# all — and is worth two.
_SERVER_ERROR_RETRIES = 1
_CONNECTION_RETRIES = 2
_RETRY_PAUSE_SECONDS = 0.5


class WhispersUnavailable(RuntimeError):
    """The feed could not be read, or answered with something unusable.

    Distinct from an empty answer on purpose, exactly as CalendarUnavailable
    is: an empty answer means "this company has no record", a failure means
    "we do not know", and only the first of those may ever be treated as a
    fact about the company.

    `transient` says whether reading again later could plausibly succeed, and
    it decides what the funnel does next. A connection that failed is worth
    holding the print for; a server error the same ticker reproduces on every
    attempt is not — measured 2026-08-08, INOD/UMAC/GAIN return the same error
    page every time, so holding them means paying for the same refusal every
    scan for 48 hours and then giving up anyway. Those fall back to the
    calendar's figures, like any other decline the feed cannot recover from.
    """

    def __init__(self, message: str, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class WhispersRecord:
    """One print as the feed reports it, in this system's units."""
    ticker: str
    name: str
    announced_at: Optional[datetime]
    quarter_end: Optional[date]
    fiscal_quarter: Optional[str]
    eps_actual: Optional[float]
    eps_consensus: Optional[float]
    eps_consensus_high: Optional[float]
    eps_consensus_low: Optional[float]
    revenue_actual: Optional[float]          # dollars
    revenue_consensus: Optional[float]       # dollars
    whisper: Optional[float]
    # The vendor's OWN reading of the same print, recorded so the two can be
    # compared after the fact and never used to judge anything here. It is a
    # different comparison: where a whisper number exists EW measures against
    # it rather than against consensus, so AMD 2026-08-04 reads -1.78%
    # ("missed expectations") from the vendor and +2.47% from actual against
    # consensus. `vendor_surprise_basis` is what makes the gap explicable
    # instead of merely visible.
    vendor_eps_surprise_pct: Optional[float] = None
    vendor_revenue_surprise_pct: Optional[float] = None
    vendor_surprise_basis: str = ""       # "whisper" | "consensus" | ""
    summary: str = ""
    subject: str = ""
    gaps: tuple[str, ...] = ()

    def covers(self, report_date: date) -> bool:
        """Whether this row is the print that happened on `report_date`."""
        return self.staleness_reason(report_date) == ""

    def staleness_reason(self, report_date: date) -> str:
        """Why this row is not that print — "" when it is.

        Named rather than boolean because the two ways it can be wrong lead
        to different actions: an older record means the feed has not caught
        up yet and the print is worth waiting for, a newer one means the
        calendar's date was wrong and waiting will not help.
        """
        if self.announced_at is None:
            return "announcement_time_missing"
        gap = (self.announced_at.date() - report_date).days
        if abs(gap) <= _SAME_PRINT_DAYS:
            return ""
        return "previous_quarter" if gap < 0 else "later_print"


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _sentinel_free(value: Any, sentinel: float) -> Optional[float]:
    number = _number(value)
    return None if number is None or number == sentinel else number


def _dollars(value: Any) -> Optional[float]:
    number = _number(value)
    return None if number is None else number * _MILLIONS


def _month_end(reference: Any) -> Optional[date]:
    """`"202606"` -> 2026-06-30, the last day of the quarter it names."""
    text = str(reference or "").strip()
    if len(text) != 6 or not text.isdigit():
        return None
    year, month = int(text[:4]), int(text[4:])
    if not 1 <= month <= 12:
        return None
    return date(year, month, calendar.monthrange(year, month)[1])


def _announced_at(value: Any) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return moment.replace(tzinfo=EASTERN) if moment.tzinfo is None else moment


# -- guidance --------------------------------------------------------------
#
# The summary puts three sets of numbers in adjacent sentences: what the
# company now expects, what it expected BEFORE this release, and what analysts
# expect. Only the first is guidance, and the other two are the same shape —
# so the clause is cut at their first word rather than pattern-matched around.

_CLAUSE = re.compile(r"The\s+compan\w+\s+said\s+it\s+expects\s+", re.I)
_DECOYS = ("The company's previous guidance", "The companys previous guidance",
           "The current consensus", "<br")
_NUM = r"([\d,]+(?:\.\d+)?)"
_EPS_RANGE = re.compile(rf"earnings of \${_NUM} to \${_NUM} per share", re.I)
_EPS_ONE = re.compile(rf"earnings of approximately \${_NUM} per share", re.I)
_REV_RANGE = re.compile(
    rf"revenue of \${_NUM} (million|billion) to \${_NUM} (million|billion)",
    re.I)
_REV_ONE = re.compile(
    rf"revenue of approximately \${_NUM} (million|billion)", re.I)
_QUARTER_WORD = re.compile(r"\b(first|second|third|fourth) quarter\b", re.I)
_YEAR = re.compile(r"\b(?:fiscal )?(20\d\d)\b")
_OPEN_ENDED = re.compile(
    r"\b(more than|at least|in excess of|no less than|approximately \$[\d.]+ "
    r"to)\b", re.I)
_NON_NUMERIC = re.compile(r"\b(breakeven|break-even|range from a loss)\b", re.I)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_SCALE = {"million": 1_000_000.0, "billion": 1_000_000_000.0}


@dataclass(frozen=True)
class GuidanceReadout:
    """What the company said about the future, and what could not be read.

    `reading` is deliberately the NEXT-QUARTER guidance only. Full-year
    guidance is kept apart in `full_year` because the only yardstick this
    system captures is next quarter's consensus, and comparing a full-year
    range against it reads as a four-fold beat.
    """
    reading: Optional[GuidanceReading]
    full_year: Optional[GuidanceReading]
    reason: str
    excerpt: str


def _clause_of(summary: str) -> str:
    """The company's own forward sentence, cut before the two decoys."""
    text = re.sub(r"\s+", " ", summary or "")
    match = _CLAUSE.search(text)
    if match is None:
        return ""
    clause = text[match.start():]
    for decoy in _DECOYS:
        cut = clause.find(decoy)
        if cut > 0:
            clause = clause[:cut]
    return clause.strip().rstrip(".").strip()


def _amount(text: str) -> float:
    return float(text.replace(",", ""))


def _next_quarter_label(record: "WhispersRecord") -> Optional[str]:
    if not record.fiscal_quarter:
        return None
    year, _, quarter = record.fiscal_quarter.partition("-Q")
    try:
        number = int(quarter)
    except ValueError:
        return None
    return f"{int(year) + 1}-Q1" if number == 4 else f"{year}-Q{number + 1}"


def _segment_period(segment: str, record: "WhispersRecord"
                    ) -> tuple[str, str]:
    """(period label, why it has none). Quarterly wins over a year mention.

    A quarter word that does not name the quarter AFTER the one just reported
    is refused rather than relabelled: it would silently move guidance onto a
    yardstick from a different period.
    """
    quarter = _QUARTER_WORD.search(segment)
    if quarter is not None:
        expected = _next_quarter_label(record)
        if expected is None:
            return "", "quarter_reference_missing"
        if not expected.endswith(f"Q{_ORDINALS[quarter.group(1).lower()]}"):
            return "", "quarter_mismatch"
        return expected, ""
    year = _YEAR.search(segment)
    return (f"FY{year.group(1)}", "") if year else ("", "period_unstated")


def _segment_numbers(segment: str) -> tuple[dict, str]:
    """The ranges in one segment, or why none could be taken from it."""
    out: dict[str, float] = {}
    eps = _EPS_RANGE.search(segment)
    if eps is not None:
        out["eps_low"], out["eps_high"] = _amount(eps.group(1)), _amount(
            eps.group(2))
    elif (one := _EPS_ONE.search(segment)) is not None:
        out["eps_low"] = out["eps_high"] = _amount(one.group(1))
    rev = _REV_RANGE.search(segment)
    if rev is not None:
        out["revenue_low"] = _amount(rev.group(1)) * _SCALE[rev.group(2).lower()]
        out["revenue_high"] = _amount(rev.group(3)) * _SCALE[rev.group(4).lower()]
    elif (one := _REV_ONE.search(segment)) is not None:
        out["revenue_low"] = out["revenue_high"] = (
            _amount(one.group(1)) * _SCALE[one.group(2).lower()])
    if out:
        return out, ""
    if _NON_NUMERIC.search(segment):
        return {}, "non_numeric_range"
    if _OPEN_ENDED.search(segment):
        return {}, "open_ended_range"
    return {}, "unparsed_clause"


def read_guidance(record: "WhispersRecord") -> GuidanceReadout:
    """Read the company's guidance out of the summary prose.

    Deterministic by design (project rule: a parser, never raw model output,
    stands between a source and a contract model). Everything it cannot read
    is named in `reason` — a company that guided in a form this does not cover
    must not be indistinguishable from one that guided nothing.
    """
    clause = _clause_of(record.summary)
    if not clause:
        return GuidanceReadout(None, None, "no_guidance_clause", "")

    quarterly: Optional[GuidanceReading] = None
    annual: Optional[GuidanceReading] = None
    reasons: list[str] = []
    for segment in re.split(r"\band\b", clause):
        period, why = _segment_period(segment, record)
        if not period:
            reasons.append(why)
            continue
        numbers, why = _segment_numbers(segment)
        if not numbers:
            reasons.append(why)
            continue
        reading = GuidanceReading(period=period, source_excerpt=clause,
                                  **numbers)
        if period.startswith("FY"):
            annual = annual or reading
        else:
            quarterly = quarterly or reading

    if quarterly is not None:
        return GuidanceReadout(quarterly, annual, "", clause)
    if annual is not None:
        return GuidanceReadout(None, annual, "full_year_only", clause)
    return GuidanceReadout(None, None, ";".join(dict.fromkeys(reasons))
                           or "unparsed_clause", clause)


def _percent(ratio: Any) -> Optional[float]:
    """The feed's surprise ratio (0.0503) as a percentage (5.03)."""
    number = _number(ratio)
    return None if number is None else number * 100.0


def _surprise_basis(vendor_pct: Optional[float], actual: Optional[float],
                    consensus: Optional[float],
                    whisper: Optional[float]) -> str:
    """Which bar the vendor measured its own surprise against.

    Worked out by recomputing rather than assumed from the presence of a
    whisper number, so that a change in the feed's behaviour shows up as
    "unknown" instead of as a mislabelled figure. Verified on the 2026-08-05
    corpus: ADM (25.17%) and AMD (-1.78%) reproduce exactly against their
    whisper numbers, AME and ALSN against consensus.
    """
    if vendor_pct is None or actual is None:
        return ""
    for bar, label in ((whisper, "whisper"), (consensus, "consensus")):
        if bar:
            recomputed = (actual - bar) / abs(bar) * 100.0
            if abs(recomputed - vendor_pct) <= 0.5:
                return label
    return "unknown"


def parse_details(payload: Any) -> WhispersRecord:
    """One `/api/epsdetails/` response, in this system's units and vocabulary.

    Raises rather than returning a hollow record when the response is not an
    object at all: that is a broken feed, and a record full of Nones would
    enter the funnel as "this company reported nothing".
    """
    if not isinstance(payload, dict):
        raise WhispersUnavailable(
            f"expected a JSON object from the earnings feed, got "
            f"{type(payload).__name__}")

    quarter_end = _month_end(payload.get("q1Ref"))
    year_end = _month_end(payload.get("fY1Ref"))
    # The label is decided in contracts, from the same rules every other
    # source goes through, and cross-checked against the feed's own prose
    # ("second quarter ended June 2026"). Two independent statements of one
    # fact: when they disagree neither is used (EW移行 §2).
    quarter = resolve_fiscal_quarter(
        quarter_end=quarter_end, year_end=year_end,
        quarter_text=str(payload.get("quarter") or ""))
    announced_at = _announced_at(payload.get("epsDate"))
    eps_actual = _number(payload.get("eps"))
    eps_consensus = _number(payload.get("estimate"))
    revenue_actual = _dollars(payload.get("revenue"))
    revenue_consensus = _dollars(payload.get("revenueEstimate"))
    summary = str(payload.get("summary") or "")

    gaps: list[str] = []
    if quarter_end is None:
        gaps.append("quarter_reference_missing")
    elif not quarter.label:
        gaps.append("quarter_label_withheld")
    if announced_at is None:
        gaps.append("announcement_time_missing")
    if eps_actual is None:
        gaps.append("eps_actual_missing")
    if eps_consensus is None:
        gaps.append("eps_consensus_missing")
    if revenue_actual is None:
        gaps.append("revenue_actual_missing")
    if revenue_consensus is None:
        gaps.append("revenue_consensus_missing")
    if not summary:
        gaps.append("summary_missing")

    whisper = _sentinel_free(payload.get("whisper"), _NO_WHISPER)
    vendor_eps_pct = _percent(payload.get("earningsSurprise"))
    return WhispersRecord(
        ticker=str(payload.get("ticker") or "").strip().upper(),
        name=str(payload.get("name") or ""),
        announced_at=announced_at,
        quarter_end=quarter_end,
        fiscal_quarter=quarter.label or None,
        eps_actual=eps_actual,
        eps_consensus=eps_consensus,
        eps_consensus_high=_number(payload.get("highEstimate")),
        eps_consensus_low=_number(payload.get("lowEstimate")),
        revenue_actual=revenue_actual,
        revenue_consensus=revenue_consensus,
        whisper=whisper,
        vendor_eps_surprise_pct=vendor_eps_pct,
        vendor_revenue_surprise_pct=_percent(payload.get("revenueSurprise")),
        vendor_surprise_basis=_surprise_basis(vendor_eps_pct, eps_actual,
                                              eps_consensus, whisper),
        summary=summary,
        subject=str(payload.get("subject") or ""),
        gaps=tuple(gaps))


class WhispersSource:
    """The live feed. One request per ticker, no key, no session.

    The site serves this endpoint to its own front-end, so it answers only a
    request that looks like the page making it — hence the browser
    User-Agent and the per-ticker Referer. Without them the response is the
    HTML shell, which this class refuses rather than parses.
    """

    def __init__(self, timeout: float = 15.0, transport=None,
                 server_error_retries: int = _SERVER_ERROR_RETRIES,
                 connection_retries: int = _CONNECTION_RETRIES,
                 sleep=time.sleep) -> None:
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._server_error_retries = max(server_error_retries, 0)
        self._connection_retries = max(connection_retries, 0)
        self._sleep = sleep

    @property
    def available(self) -> bool:
        return True

    def _fetch(self, symbol: str) -> httpx.Response:
        """One GET, retried while reading again could plausibly help.

        Only 5xx and connection errors are retried. A 4xx is the site telling
        us something about THIS request; repeating it would neither change the
        answer nor be polite.

        The two are retried differently and raised differently, because they
        turned out to be different things (measured 2026-08-08). A 500 from
        this endpoint reproduces on every attempt for the same ticker, so one
        retry establishes that and a hold would only buy the same refusal 48
        hours running. A connection error is worth two attempts and IS worth
        holding for — nothing came back at all, so nothing was learned about
        the company.
        """
        url = f"{_BASE}/api/epsdetails/{symbol}"
        headers = {"User-Agent": _BROWSER_UA,
                   "Referer": f"{_BASE}/epsdetails/{symbol}",
                   "Accept": "application/json"}
        attempts = max(self._server_error_retries,
                       self._connection_retries) + 1
        last, transient = "", True
        for attempt in range(attempts):
            try:
                resp = self._client.get(url, headers=headers)
                if resp.status_code < 500:
                    return resp
                last, transient = f"answered {resp.status_code}", False
                budget = self._server_error_retries
            except httpx.HTTPError as exc:
                last, transient = f"could not be reached ({exc})", True
                budget = self._connection_retries
            if attempt >= budget:
                break
            self._sleep(_RETRY_PAUSE_SECONDS * (attempt + 1))
        raise WhispersUnavailable(
            f"{symbol}: the earnings feed {last} on {attempt + 1} attempts",
            transient=transient)

    def details(self, ticker: str) -> Optional[WhispersRecord]:
        """This company's most recent print, or None when it has no record.

        None is reserved for the feed's own "nothing here" (204). Every other
        way this can go wrong raises, because downstream a None is read as a
        fact about the company rather than about the connection.
        """
        symbol = ticker.strip().upper()
        resp = self._fetch(symbol)
        if resp.status_code == httpx.codes.NO_CONTENT or not resp.content:
            return None
        if resp.status_code >= 400:
            raise WhispersUnavailable(
                f"{symbol}: the earnings feed answered "
                f"{resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise WhispersUnavailable(
                f"{symbol}: the earnings feed answered "
                f"{resp.headers.get('content-type', 'an unknown type')} "
                f"instead of JSON") from exc
        return parse_details(body)
