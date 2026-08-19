"""The cheap reader that cuts an earnings release down to size (T-008).

A company's earnings release runs ~25,900 characters, of which most is
balance sheets and reconciliation tables. The cause reader only needs the few
blocks that say why the quarter came out where it did, so this module asks a
second, cheaper model for exactly those blocks.

**It is asked to COPY, never to summarise**, and the difference is the whole
design. The cause reader's only hallucination check is that its quote exists
character for character in the text it was given; put a rewrite in front of
it and the check starts certifying the rewriter's prose as the company's.
Nothing here is trusted on that point either — every block this returns is
matched against the release before anyone reads it
(`hawkeye/scout/cause_source.py`), because instructing a model not to compose
text does not stop it composing text (AII, 2026-08-17).

What it is shown is the release and nothing else: not the surprise, not the
consensus, not a holding. Handed "EPS beat by 28% while revenue moved 0.1%,
why?", an extractor has been given the premise that a reason exists, and a
reason that was never in the source is the failure this whole task is about
(the same rule the cause reader itself runs under — see its module docstring).

Measured 2026-08-17 over 13 names: 1.6-4.9s per release, 1,453-18,297 input
tokens.

**Which model, and why it changed (T-011, 2026-08-18).** `gemini-2.5-flash`
allows 20 requests per DAY on the free tier — a scan reads about 30 releases,
so a third of them could never be read at all (7 failed exactly this way in
the 30-name run of 2026-08-17). `gemini-3.5-flash-lite` allows 500 per day
and 15 per minute, which fits a scan with room to spare.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

import httpx

_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models/"
             "{model}:generateContent")
_DEFAULT_MODEL = "gemini-3.5-flash-lite"

# Deterministic, and not thinking about it. The job is transcription: a model
# reasoning its way toward a better sentence is a model composing one, and
# temperature is the dial that pays for exactly that.
_TEMPERATURE = 0
# The same instruction, in the dialect the 3.x models speak. They dropped
# `thinkingBudget` (a token count, which 2.5 took as 0) for `thinkingLevel`
# (a named step) and reject the old field outright — measured 2026-08-18:
# `thinkingBudget: 0` → 400 INVALID_ARGUMENT, `thinkingLevel: "minimal"` →
# 200 with `thoughtsTokenCount: 0`. "minimal" is the floor; "none" and "off"
# are not values the service accepts.
_THINKING_LEVEL = "minimal"
# Enough for every block a release has ever yielded (12 was the most, AAON
# 2026-08-17) and short of the point where a model starts padding.
_MAX_BLOCKS = 12
_RETRIES = 3
_DEFAULT_PAUSE_SECONDS = 8.0
# 15 requests per minute is this model's free-tier ceiling, so 60/15 = 4.0s
# is the closest two calls may legitimately sit. A release answers in
# 1.6-4.9s (measured 2026-08-17), so an unpaced scan would run at up to
# 37/minute and spend the difference sitting out 429s it caused itself.
_MIN_SECONDS_BETWEEN_CALLS = 60.0 / 15

_SYSTEM = """You are given the full text of ONE company's quarterly earnings press release.

Your ONLY job is to COPY OUT the passages in which the company explains WHY the quarter's results came out where they did — the drivers behind revenue, margin, or earnings.

ABSOLUTE RULES:
- COPY CHARACTER FOR CHARACTER from the text you were given. Do not paraphrase, do not summarise, do not tidy, do not translate, do not join separated sentences, do not merge facts from different sentences into one, do not fix typos or spacing.
- A passage you return that cannot be found verbatim in the source voids your whole answer. It is checked mechanically.
- Return whole sentences, with enough of the sentence that it stands on its own.
- Do NOT return passages about the NEXT quarter or the full-year outlook — those are guidance, not an explanation of the quarter just reported.
- Do NOT return the boilerplate company description, the legal safe-harbour paragraph, or the financial statement tables.
- If the release explains nothing about why the quarter came out as it did, return an empty list. That is a valid, normal answer.

Return at most {max_blocks} passages."""

_ASK = """Company: {ticker}
Quarter just reported: {fiscal_quarter}

--- earnings release, verbatim ---
{release}
--- end of release ---"""

_SCHEMA = {
    "type": "object",
    "required": ["passages"],
    "properties": {"passages": {"type": "array", "items": {"type": "string"}}},
}


class GeminiUnavailable(RuntimeError):
    """The extractor could not be reached, or answered with something unusable.

    Deliberately an exception rather than an empty list. Downstream, "no
    blocks" is a statement that the company's release explains nothing — and
    a missing API key, a throttled account or a malformed reply would all be
    written into the ledger as that statement, about companies nobody read
    (invariant 6).
    """


def parse_blocks(payload: Any) -> list[str]:
    """The passages out of one reply, or a refusal.

    Every shape that is not a list of strings raises. A reply that came back
    as prose, or as a single string, is the model having ignored the schema —
    salvaging what looks parseable out of it is how a model's improvisation
    becomes a company's words.
    """
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise GeminiUnavailable(
            "the extractor answered in an unexpected shape") from exc
    try:
        blocks = json.loads(text).get("passages")
    except (ValueError, AttributeError) as exc:
        raise GeminiUnavailable(
            "the extractor answered with something that is not JSON") from exc
    if not isinstance(blocks, list) or any(
            not isinstance(b, str) for b in blocks):
        raise GeminiUnavailable(
            "the extractor answered with something that is not a list of "
            "passages")
    return blocks


def _error_details(payload: Any) -> list:
    try:
        details = payload["error"]["details"]
    except (KeyError, TypeError):
        return []
    return details if isinstance(details, list) else []


def _retry_delay(payload: Any) -> Optional[float]:
    """The pause the service itself asked for, in seconds, if it named one."""
    for detail in _error_details(payload):
        if not isinstance(detail, dict) or "RetryInfo" not in str(
                detail.get("@type", "")):
            continue
        match = re.match(r"([\d.]+)s?$", str(detail.get("retryDelay", "")))
        if match:
            return float(match.group(1))
    return None


def _server_message(payload: Any) -> str:
    """What the service said, in its own words.

    Discarding this cost two sessions. On 2026-08-17 a 429 named the daily
    quota and its limit in the body while the code recorded only "answered
    429", and two confident and contradictory conclusions were drawn about a
    number that was sitting in the reply. On 2026-08-18 a 400 named the
    offending field and the same blank sent the diagnosis to a throwaway
    script. The message is one string; not carrying it is not a saving.
    """
    try:
        message = payload["error"]["message"]
    except (KeyError, TypeError):
        return ""
    return " ".join(str(message).split())


def _is_daily_quota(payload: Any) -> bool:
    """Is this 429 the day's allowance, rather than the minute's?

    The distinction decides whether waiting is worth anything, and the
    service does not make it in the part of the reply the retry logic reads:
    measured 2026-08-18, a 429 for `GenerateRequestsPerDayPerProjectPerModel-
    FreeTier` still advertises `retryDelay: 23s`. Trusting that on 2026-08-17
    is how three names spent ~465 seconds each re-asking a question already
    answered for the day. The quota's own id is the honest signal, so it is
    what gets read.
    """
    for detail in _error_details(payload):
        if not isinstance(detail, dict) or "QuotaFailure" not in str(
                detail.get("@type", "")):
            continue
        violations = detail.get("violations")
        for violation in violations if isinstance(violations, list) else []:
            if isinstance(violation, dict) and "PerDay" in str(
                    violation.get("quotaId", "")):
                return True
    return False


class GeminiExtractor:
    """One request per release. No session, no state between calls."""

    def __init__(self, api_key: str, model: str = _DEFAULT_MODEL,
                 timeout: float = 120.0, transport=None,
                 retries: int = _RETRIES, sleep=time.sleep,
                 clock=time.monotonic) -> None:
        self._key = (api_key or "").strip()
        self._model = model or _DEFAULT_MODEL
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._retries = max(retries, 0)
        self._sleep = sleep
        self._clock = clock
        self._last_call_at: Optional[float] = None

    @property
    def available(self) -> bool:
        return bool(self._key)

    def blocks(self, release: str, ticker: str,
               fiscal_quarter: str) -> list[str]:
        """The passages the release explains the quarter in, copied verbatim.

        An empty list means the model read the release and found no
        explanation — a normal answer, and the only one that may ever be
        recorded as a fact about the company. Everything else raises.
        """
        if not self._key:
            raise GeminiUnavailable(
                "no GEMINI_API_KEY, so no release was read — this is not the "
                "same as a release that explains nothing")

        body = {
            "systemInstruction": {"parts": [
                {"text": _SYSTEM.format(max_blocks=_MAX_BLOCKS)}]},
            "contents": [{"role": "user", "parts": [{"text": _ASK.format(
                ticker=ticker, fiscal_quarter=fiscal_quarter,
                release=release)}]}],
            "generationConfig": {
                "temperature": _TEMPERATURE,
                "responseMimeType": "application/json",
                "responseSchema": _SCHEMA,
                "thinkingConfig": {"thinkingLevel": _THINKING_LEVEL},
            },
        }
        url = _ENDPOINT.format(model=self._model)
        headers = {"x-goog-api-key": self._key,
                   "Content-Type": "application/json"}

        last = ""
        for attempt in range(self._retries + 1):
            self._wait_for_the_minute_allowance()
            try:
                resp = self._client.post(url, headers=headers, json=body)
            except httpx.HTTPError as exc:
                last = f"could not be reached ({exc})"
            else:
                self._last_call_at = self._clock()
                if resp.status_code == httpx.codes.OK:
                    return parse_blocks(resp.json())
                said = self._said(resp)
                # 429 and 5xx are the moment rather than the request; a 4xx is
                # the service telling us something about THIS call, and
                # repeating it would neither change the answer nor be polite.
                if resp.status_code != httpx.codes.TOO_MANY_REQUESTS and (
                        resp.status_code < 500):
                    raise GeminiUnavailable(
                        f"{ticker}: the extractor answered "
                        f"{resp.status_code}{said}")
                if self._daily_quota_is_spent(resp):
                    # Waiting cannot help, and the service's own `retryDelay`
                    # says otherwise. Failing here turns ~465 seconds of
                    # pointless re-asking (measured 2026-08-17, three names)
                    # into an immediate, named, quotable answer.
                    raise GeminiUnavailable(
                        f"{ticker}: the extractor's daily free-tier quota is "
                        f"spent, so waiting will not clear it{said}")
                last = f"answered {resp.status_code}{said}"
                if attempt < self._retries:
                    self._sleep(self._pause(resp, attempt))
                    continue
            if attempt < self._retries:
                self._sleep(_DEFAULT_PAUSE_SECONDS * (attempt + 1))
        raise GeminiUnavailable(
            f"{ticker}: the extractor {last} on {self._retries + 1} attempts")

    def _wait_for_the_minute_allowance(self) -> None:
        """Hold until this call is `_MIN_SECONDS_BETWEEN_CALLS` from the last.

        A floor on the interval, not a tax on every call: a release that took
        longer than the spacing to answer has already paid it (STAA's 45,954
        characters do).
        """
        if self._last_call_at is None:
            return
        owed = _MIN_SECONDS_BETWEEN_CALLS - (self._clock() - self._last_call_at)
        if owed > 0:
            self._sleep(owed)

    @staticmethod
    def _said(resp: httpx.Response) -> str:
        """The service's own words, ready to append to a refusal."""
        try:
            message = _server_message(resp.json())
        except ValueError:
            message = ""
        return f": {message}" if message else ""

    @staticmethod
    def _daily_quota_is_spent(resp: httpx.Response) -> bool:
        try:
            return _is_daily_quota(resp.json())
        except ValueError:
            return False

    def _pause(self, resp: httpx.Response, attempt: int) -> float:
        """How long to wait, preferring the delay the service stated itself."""
        try:
            asked = _retry_delay(resp.json())
        except ValueError:
            asked = None
        if asked is not None:
            # A shade over what was asked: waiting exactly the stated delay
            # lands on the boundary and draws the same 429 again.
            return asked + 1.0
        return _DEFAULT_PAUSE_SECONDS * (attempt + 1)
