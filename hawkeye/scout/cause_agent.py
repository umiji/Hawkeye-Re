"""Reading why the quarter came out where it did (T-003).

A headline EPS beat standing beside flat revenue is the shape almost every
candidate this system finds has: of the 19 recorded names, the 18 with data
all looked like UNH (EPS +28.8%, revenue +0.1%) or LBRT (+659.5% / +7.9%)
(docs/design/RETURN_TARGET_STRUCTURAL_GAPS.ja.md, 障害D). That shape has two
completely different meanings — a tax item or a one-time gain that will not
repeat, or a margin the company actually earned — and **nothing in the
numbers separates them.**

So the tribunal guessed. On PGY the record shows the guess written down as
though it were established: "explicable by tax effects or revaluation
gains", with nothing behind it. The company's own summary often says which
it was, in one sentence, and until now nobody read that sentence.

This module is the gate on what an agent is allowed to have read out of it.
It is deliberately the same shape as `hawkeye/scout/guidance_agent.py`, and
differs in exactly two ways worth stating:

- **What is asked.** That one asks what the company expects NEXT quarter;
  this one asks what the company said about the quarter it just reported.
  The same paragraph holds both, and the sentences look alike.
- **What is returned.** A quote, and only the little that can be checked
  about it. Whether a tax benefit means the beat should be discounted is the
  Judge's call — a row that had already made it would remove the judgment
  this system exists to preserve.

**The agent never sees the surprise it is being asked to explain** (User
decision, 2026-08-17). Told "EPS beat by 28% while revenue moved 0.1%, why?"
an extractor has been handed the premise that a reason exists, and the one
failure this whole task is about is a plausible reason that was never in the
source. It is shown the ticker, the quarter, and the summary. Nothing else.

Four checks, the same four the guidance gate applies, each stopping a
failure the others cannot see:

1. **The quote has to exist**, character for character once whitespace is
   normalised. It is the only hallucination check available: no other signal
   separates an explanation the company gave from one that reads plausibly.
2. **The quote has to be the RIGHT sentence.** The summary states the
   quarter's result, the company's outlook for the NEXT quarter, and the
   analysts' figure in adjacent sentences of identical shape. A reason lifted
   out of the outlook sentence explains a quarter that has not happened yet.
3. **A magnitude states its unit.** "$0.12" beside an EPS figure is twelve
   cents; "0.12" could be twelve percent of revenue. A bare number would be
   read by three roles as whichever one fits their argument.
4. **The period must be the quarter just reported.** Summaries discuss the
   year-ago quarter in the same breath, and a year-ago one-off explains
   nothing about this print.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from hawkeye.contracts.stocks import CauseReading


@dataclass(frozen=True)
class CauseRequest:
    """One print's summary, and the little context needed to read it.

    No surprise figures, by design — see the module docstring.
    """
    ticker: str
    fiscal_quarter: str        # the quarter just reported, e.g. 2026-Q1
    summary: str               # the vendor's summary, verbatim


@dataclass(frozen=True)
class CauseExtraction:
    """A reading, or the named reason there is none. Never both, never neither."""
    reading: Optional[CauseReading]
    reason: str


# --- what the agent is told -------------------------------------------------

CAUSE_SYSTEM = """You read one earnings summary and report WHAT THE COMPANY SAID EXPLAINS THE QUARTER IT JUST REPORTED. Nothing else.

You are looking for a stated reason the reported profit or margin came out where it did — a tax item, a gain or charge on an asset, a legal settlement, a revaluation, a restructuring charge, a cost reduction, a pricing or mix change, a volume change.

The summary contains sentences that look like an explanation and are not. None of these is yours:

1. What the company expects NEXT quarter or next year ("The company said it expects ...") — that is an outlook, not an explanation of what just happened.
2. What ANALYSTS expect ("The current consensus ... estimate is ...").
3. What the company previously expected ("The company's previous guidance was ...").
4. Anything describing the YEAR-AGO quarter rather than this one.

Rules:

- QUOTE the exact words you read it from, copied character for character from the summary. A quote that cannot be found in the summary voids your whole answer. Do not paraphrase, do not tidy, do not translate.
- If the company put a NUMBER on it, report that number and state its UNIT: "per_share", "million", "billion", or "percent". Report the number as the summary states it; do not convert. If the summary gives no number, leave both empty — an invented magnitude is worse than none.
- nature: "one_off" if what you quoted will not repeat by its own description (tax item, gain or charge, settlement, revaluation, one-time cost); "operating" if it is the business itself (pricing, cost, mix, volume, margin); "unclear" if the sentence does not say which. "unclear" is a normal answer and is not a failure — guessing between the two is.
- period: the quarter this explanation is about, as "2026-Q2". The user message names the quarter just reported. If what you found explains a different period, say that period rather than relabelling it.
- If the summary gives NO reason at all — only the figures — set explained to false and stop. That is a common, normal answer.

You are not judging the company. You are not deciding whether the beat was real, whether it will repeat, or whether anyone should buy. You are copying one sentence out of one summary."""


def build_schema() -> dict:
    """The reply shape, enforced at the API rather than trusted from prose.

    `additionalProperties: false` matters more than it looks: a field nobody
    reviewed is a field nobody parses, and it would arrive looking like
    information.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["explained", "quote"],
        "properties": {
            "explained": {"type": "boolean"},
            "nature": {"type": ["string", "null"],
                       "enum": ["one_off", "operating", "unclear", None]},
            "magnitude": {"type": ["number", "null"]},
            "magnitude_unit": {
                "type": ["string", "null"],
                "enum": ["per_share", "million", "billion", "percent", None]},
            "period": {"type": ["string", "null"]},
            "quote": {"type": "string"},
        },
    }


_ASK = """Company: {ticker}
Quarter just reported: {fiscal_quarter}

--- the vendor's summary, verbatim ---
{summary}
--- end of summary ---

Report what THIS COMPANY said explains the quarter above, quoting the words
you read it from. If the summary states no reason, say so."""


def render_request(request: CauseRequest) -> str:
    """The package one agent reads. Deliberately the WHOLE summary.

    Cutting it down to "the explanation sentence" would need the reading this
    step exists to produce, and a wrong cut is invisible: the agent would
    faithfully report a reason out of whatever sentence survived the cut.
    """
    return _ASK.format(ticker=request.ticker,
                       fiscal_quarter=request.fiscal_quarter,
                       summary=request.summary)


# --- the gate ---------------------------------------------------------------

_UNITS = ("per_share", "million", "billion", "percent")
_NATURES = ("one_off", "operating", "unclear")
_PERIOD = re.compile(r"^\d{4}-Q[1-4]$")
# The three sentences shaped like an explanation that are not one. Matched on
# the words that make them what they are and nothing wider, for the reason the
# guidance gate learned the hard way: a marker of "consensus estimate" alone
# also matches the results paragraph, which IS about the reported quarter.
_WRONG_SENTENCE = ("previous guidance", "current consensus", "it expects")
# The vendor separates paragraphs with `<br />` and puts NO space after the
# preceding full stop, so a splitter that breaks on ". " glues two paragraphs
# together — and then judges the second by what is written in the first.
_SENTENCE_END = re.compile(r"(?<=\.)\s+|<br\s*/?>")


def _flat(text: str) -> str:
    """Whitespace normalised and lowercased, for finding one string in another.

    The summary arrives with line breaks and doubled spaces in it, and a
    reading discarded over an invisible character would teach us to loosen the
    check that actually matters.
    """
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _sentence_holding(quote: str, summary: str) -> Optional[str]:
    """The sentence of `summary` that contains `quote`, or None if none does."""
    flat_quote = _flat(quote)
    if not flat_quote:
        return None
    for sentence in _SENTENCE_END.split(re.sub(r"\s+", " ", summary or "")):
        if flat_quote in _flat(sentence):
            return sentence
    # A quote spanning a sentence boundary still has to exist somewhere; it
    # just cannot be attributed to one sentence, so it fails the check below
    # rather than passing on a technicality.
    return None


def parse_reply(reply: dict, request: CauseRequest,
                model: str = "") -> CauseExtraction:
    """One agent reply, either as a reading or as a named refusal.

    Order matters. The quote is checked BEFORE the magnitude, because a reply
    whose quote is absent tells us nothing about its number either — reporting
    "the unit was missing" for an answer that was invented would name the
    wrong failure, and the wrong failure is the one that gets fixed.
    """
    if not reply.get("explained"):
        return CauseExtraction(None, "no_cause_in_source")

    quote = str(reply.get("quote") or "")
    sentence = _sentence_holding(quote, request.summary)
    if sentence is None:
        return CauseExtraction(None, "quote_not_in_source")
    if any(marker in sentence.lower() for marker in _WRONG_SENTENCE):
        return CauseExtraction(None, "quoted_the_wrong_sentence")

    magnitude = reply.get("magnitude")
    unit = str(reply.get("magnitude_unit") or "")
    if isinstance(magnitude, bool) or not isinstance(magnitude, (int, float)):
        magnitude, unit = None, ""
    elif unit not in _UNITS:
        return CauseExtraction(None, "magnitude_unit_missing")

    period = str(reply.get("period") or "").strip()
    if not _PERIOD.match(period):
        return CauseExtraction(None, "period_unreadable")
    if period != request.fiscal_quarter:
        return CauseExtraction(None, "period_not_reported_quarter")

    # The one field no check can confirm against the text. An answer outside
    # the three labels is not refused — the QUOTE is the evidence and it has
    # already been verified — it simply reverts to the honest one, which is
    # that this reading does not say which kind it was.
    nature = str(reply.get("nature") or "").strip()
    return CauseExtraction(CauseReading(
        nature=nature if nature in _NATURES else "unclear",
        magnitude=magnitude, magnitude_unit=unit,
        period=period, source_excerpt=quote.strip(),
        extractor="agent", extractor_model=model), "")


# --- naming the failure -----------------------------------------------------
#
# The same three kinds the guidance gate splits its blanks into, for the same
# reason: a report that counts blanks cannot tell "this summary explains
# nothing" from "our extractor broke", and only the second is ours to fix.

_FAILURE_KIND = {
    # The source states no reason. Common, and not a defect.
    "no_cause_in_source": "absent_in_source",
    # There was no prose at all to read — the vendor returned no summary for
    # this print, or was never asked about this name. A fact about the feed,
    # not about the company, and kept apart from "the summary explains
    # nothing" for exactly that reason.
    "no_summary_from_feed": "source_absent",
    "feed_not_asked": "source_absent",
    # The reader produced something this gate would not accept. THIS is the
    # number to watch: it rising means the prompt or the gate is wrong.
    "quote_not_in_source": "reader_failed",
    "quoted_the_wrong_sentence": "reader_failed",
    "magnitude_unit_missing": "reader_failed",
    "period_unreadable": "reader_failed",
    "period_not_reported_quarter": "reader_failed",
    # The call never completed.
    "extraction_call_failed": "call_failed",
    # Not a failure at all: session mode staged the summary and nobody has
    # read it yet. Named here so that every reason this can be empty has to be
    # classified — a blank the reader cannot account for is what the table
    # exists to prevent.
    "pending_extraction": "not_yet_read",
}


def failure_kind(reason: str) -> str:
    """Which of the four kinds a refusal is. Raises on anything unmapped."""
    try:
        return _FAILURE_KIND[reason]
    except KeyError:
        raise ValueError(
            f"unclassified cause refusal {reason!r} — add it to _FAILURE_KIND "
            "and decide which kind it is") from None
