"""Reading the company's own forward statement with an agent (task 8.7 layer 2).

Of the three legs a quarter is judged on, guidance is the only one that
arrives as prose. EPS and revenue come as numbers in numbered fields; the
company's outlook exists in one English sentence written by whoever wrote the
release, and there is no structured source for it on any free tier.

A regular expression read that sentence until 2026-08-10 and read it well
enough to fool the measurement: 23 of the 25 recorded names that said
something forward-looking yielded a range. What the count hid is that the
regular expression can only ever read sentences shaped like the ones it was
written against. The two it failed on are the shape of the problem:

    "third quarter results to range from a loss of $1.00 per share to
     breakeven"                                                     (ALGT)
    "2026 revenue of $3.54 billion to $2.67 billion"                (ADV)

The first is -1.00 to 0.00 and only a reader who understands "a loss of" and
"breakeven" knows it. The second is a range with its top written first, and
the pattern that decided it was a typo was guessing. Neither is exotic
English; both are ordinary sentences a pattern cannot generalise to. So
guidance is read by an agent, all of it, and this module is the gate that
decides what the agent is allowed to have read (User decision, 2026-08-10).

**The agent never sees what its reading will be compared against.** The
package holds the vendor's summary, the ticker, and the label of the quarter
that follows — not the consensus, not the surprise, not the score. An
extractor shown the bar it is about to clear has stopped extracting
(invariant 4, applied one step outside the tribunal).

**Nothing the agent returns reaches a contract model unparsed** (invariant 3).
Four things are checked, and each stops a failure the others cannot see:

1. **The quote has to exist.** The reply names the sentence it read and that
   text must be findable in the summary, character for character once
   whitespace is normalised. It is the only hallucination check available
   here: no other signal separates a range the company gave from a range that
   merely reads plausibly.
2. **The quote has to be the RIGHT sentence.** The summary states the
   company's guidance, the company's PREVIOUS guidance, and the analysts'
   consensus in adjacent sentences of identical shape. Reading the second
   understates or overstates by however much the company revised; reading the
   third produces a beat of exactly zero, on every name it happens to.
3. **The unit is stated, and this module multiplies.** The vendor writes
   revenue in millions and in billions and every contract downstream is in
   dollars. A model returning a bare 2.85 for $2.85 billion would be a
   1,000,000,000x error with nothing to catch it.
4. **The period must be one a yardstick exists for.** Guidance for a period
   the consensus does not cover is a cross-period comparison, and ADM's
   version of that error read as a +348% beat the company never gave.

Everything refused is refused by NAME, and every name maps to one of three
kinds — the source held no number, the reader could not read it, or the call
never completed (User decision, 2026-08-09). A report that only counts blanks
cannot tell "this company guided nothing" from "our extractor broke".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from hawkeye.contracts.stocks import GuidanceReading


@dataclass(frozen=True)
class GuidanceRequest:
    """One print's forward statement, and the little context needed to read it.

    `next_quarter` is here because the prose says "third quarter" and nothing
    in the sentence says which year's third quarter, or whether it is even the
    one that follows what was just reported.
    """
    ticker: str
    fiscal_quarter: str        # the quarter just reported, e.g. 2026-Q1
    next_quarter: str          # the one the guidance should be about
    summary: str               # the vendor's summary, verbatim


@dataclass(frozen=True)
class GuidanceExtraction:
    """A reading, or the named reason there is none. Never both, never neither."""
    reading: Optional[GuidanceReading]
    reason: str


# --- what the agent is told -------------------------------------------------

GUIDANCE_SYSTEM = """You read one earnings summary and report the FORWARD-LOOKING RANGE THE COMPANY ITSELF GAVE. Nothing else.

The summary can contain three ranges that look alike. Only the first is yours:

1. What the company says it NOW expects — this is what you report.
2. What the company PREVIOUSLY expected ("The company's previous guidance was ...") — never report this.
3. What ANALYSTS expect ("The current consensus ... estimate is ...") — never report this. It is the figure your answer will be measured against, and reporting it produces a meaningless comparison of a number with itself.

Rules:

- QUOTE the exact words you read the numbers from, copied character for character from the summary. A quote that cannot be found in the summary voids your whole answer. Do not paraphrase, do not tidy, do not translate.
- Quote from the company's own sentence only. If your quote sits in the previous-guidance sentence or the consensus sentence, the answer is void.
- Report the numbers as the summary states them, and state the UNIT you read for revenue: "million", "billion", or "dollars". Do not convert.
- READ THE WORDS, not just the digits. "a loss of $1.00 per share to breakeven" is a low of -1.00 and a high of 0.00. "a loss of $0.03 per share" is -0.03. A range may be written with its top first; report the two numbers you read and do not reorder them.
- If the range has no top ("more than $6.00", "at least $2.00 billion"), set open_ended to true. Do NOT invent the missing end.
- If the company attached a CONDITION to the range ("excluding its barge business", "assuming no further acquisitions", "in constant currency"), copy that condition verbatim into qualifier. Copy it from the summary — an invented condition voids the answer.
- period: "FY2026" for a full financial year, or "2026-Q3" for a single quarter. The user message names which quarter follows the one just reported; a quarterly range must be that quarter. If you cannot tell which period the company means, say so rather than guessing.
- If the company gave no forward range at all, set guided to false and stop. That is a normal, common answer and is not a failure.

You are not judging the company. You are not deciding whether this is good news. You are copying one range out of one sentence."""


def build_schema() -> dict:
    """The reply shape, enforced at the API rather than trusted from prose.

    `additionalProperties: false` matters more than it looks: a field nobody
    reviewed is a field nobody parses, and it would arrive looking like
    information.
    """
    number = {"type": ["number", "null"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["guided", "quote"],
        "properties": {
            "guided": {"type": "boolean"},
            "period": {"type": ["string", "null"]},
            "eps_low": number,
            "eps_high": number,
            "revenue_low": number,
            "revenue_high": number,
            "revenue_unit": {"type": ["string", "null"],
                             "enum": ["million", "billion", "dollars", None]},
            "open_ended": {"type": ["boolean", "null"]},
            "qualifier": {"type": ["string", "null"]},
            "quote": {"type": "string"},
        },
    }


_ASK = """Company: {ticker}
Quarter just reported: {fiscal_quarter}
The quarter that FOLLOWS it: {next_quarter}
  (so a range the company gives "for the third quarter" belongs to
   {next_quarter}; if the range is for a different quarter, say so)

--- the vendor's summary, verbatim ---
{summary}
--- end of summary ---

Report the forward-looking range THIS COMPANY gave, quoting the words you
read it from."""


def render_request(request: GuidanceRequest) -> str:
    """The package one agent reads. Deliberately the WHOLE summary.

    Cutting it down to "the guidance sentence" would need the reading this
    step exists to produce, and a wrong cut is invisible: the agent would
    faithfully report a range out of whatever sentence survived the cut.
    """
    return _ASK.format(ticker=request.ticker,
                       fiscal_quarter=request.fiscal_quarter,
                       next_quarter=request.next_quarter,
                       summary=request.summary)


# --- the gate ---------------------------------------------------------------

_SCALE = {"million": 1_000_000.0, "billion": 1_000_000_000.0,
          "dollars": 1.0}
_PERIOD = re.compile(r"^(?:FY\d{4}|\d{4}-Q[1-4])$")
# The two sentences that are shaped like guidance and are not it. Both are
# matched on the words that make them what they are and nothing wider: the
# results paragraph says the company "beat consensus estimates by 72.44%",
# which is about the quarter just reported, and a marker of "consensus
# estimate" made THAT a decoy and refused a correct reading (found on ALGT's
# real response, 2026-08-10).
_WRONG_SENTENCE = ("previous guidance", "current consensus")
# The vendor separates paragraphs with `<br />` and puts NO space after the
# preceding full stop, so a splitter that breaks on ". " glues the results
# paragraph onto the guidance paragraph — and then judges the second by what
# is written in the first.
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


def _pair(low, high) -> tuple[Optional[float], Optional[float]]:
    """Two ends of a range, ordered. A range written top-first is a range."""
    values = [v for v in (low, high) if isinstance(v, (int, float))]
    if not values:
        return None, None
    return min(values), max(values)


def _scaled(value: Optional[float], unit: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    return value * _SCALE[unit]


def parse_reply(reply: dict, request: GuidanceRequest,
                model: str = "") -> GuidanceExtraction:
    """One agent reply, either as a reading or as a named refusal.

    Order matters here. The quote is checked BEFORE the numbers, because a
    reply whose quote is absent tells us nothing about its numbers either —
    reporting "the range looked odd" for an answer that was invented would
    name the wrong failure.
    """
    if not reply.get("guided"):
        return GuidanceExtraction(None, "no_guidance_in_source")

    quote = str(reply.get("quote") or "")
    sentence = _sentence_holding(quote, request.summary)
    if sentence is None:
        return GuidanceExtraction(None, "quote_not_in_source")
    if any(marker in sentence.lower() for marker in _WRONG_SENTENCE):
        return GuidanceExtraction(None, "quoted_the_wrong_sentence")

    # A condition is evidence like the quote is, and gets the same treatment:
    # an invented one would read as a refusal downstream, which silently
    # deletes a real guidance beat and looks like nothing at all.
    qualifier = str(reply.get("qualifier") or "").strip()
    if qualifier and _flat(qualifier) not in _flat(request.summary):
        return GuidanceExtraction(None, "quote_not_in_source")

    period = str(reply.get("period") or "").strip()
    if not _PERIOD.match(period):
        return GuidanceExtraction(None, "period_unreadable")
    if not period.startswith("FY") and period != request.next_quarter:
        return GuidanceExtraction(None, "period_not_next_quarter")

    if reply.get("open_ended"):
        return GuidanceExtraction(None, "open_ended_range")

    eps_low, eps_high = _pair(reply.get("eps_low"), reply.get("eps_high"))
    rev_low, rev_high = _pair(reply.get("revenue_low"),
                              reply.get("revenue_high"))
    unit = reply.get("revenue_unit") or "dollars"
    if rev_low is not None and unit not in _SCALE:
        return GuidanceExtraction(None, "period_unreadable")
    if eps_low is None and rev_low is None:
        return GuidanceExtraction(None, "no_number_in_source")

    return GuidanceExtraction(GuidanceReading(
        period=period,
        eps_low=eps_low, eps_high=eps_high,
        revenue_low=_scaled(rev_low, unit),
        revenue_high=_scaled(rev_high, unit),
        source_excerpt=quote.strip(), qualifier=qualifier,
        extractor="agent", extractor_model=model), "")


# --- naming the failure -----------------------------------------------------
#
# Fixed on 2026-08-09: a blank guidance leg has three completely different
# causes and a report that counts blanks cannot tell them apart. The mapping
# is explicit and total, so a new refusal cannot join without someone deciding
# which kind it is.

_FAILURE_KIND = {
    # The source holds no number to read. Normal, and not a defect.
    "no_guidance_in_source": "absent_in_source",
    "open_ended_range": "absent_in_source",
    "no_number_in_source": "absent_in_source",
    # The reader produced something this gate would not accept. THIS is the
    # number to watch: it rising means the prompt or the gate is wrong.
    "quote_not_in_source": "reader_failed",
    "quoted_the_wrong_sentence": "reader_failed",
    "period_unreadable": "reader_failed",
    "period_not_next_quarter": "reader_failed",
    # The call never completed.
    "extraction_call_failed": "call_failed",
    # Not a failure at all: session mode wrote the sentence down and nobody
    # has read it yet. It is in this table so that every reason a guidance leg
    # can be empty has to be named and translated — a blank the reader cannot
    # account for is the thing the table exists to prevent.
    "pending_extraction": "not_yet_read",
}


def failure_kind(reason: str) -> str:
    """Which of the three kinds a refusal is. Raises on anything unmapped."""
    try:
        return _FAILURE_KIND[reason]
    except KeyError:
        raise ValueError(
            f"unclassified guidance refusal {reason!r} — add it to "
            "_FAILURE_KIND and decide which of the three kinds it is") from None


@dataclass
class GuidanceStats:
    """What the extraction step did on one scan, in the three kinds.

    `reader_failed` is the one to watch. The other two are facts about the
    world — companies that guided nothing, calls that did not complete — while
    this one is a fact about US: the prompt or the gate is wrong, and it is
    invisible in a count of how many legs came back blank.
    """
    attempted: int = 0
    read: int = 0
    absent_in_source: int = 0
    reader_failed: int = 0
    call_failed: int = 0
    # Sentences written down for an agent this process could not call
    # (session mode). Not a failure and not a reading — work outstanding.
    staged: int = 0

    def record(self, reason: str) -> None:
        self.attempted += 1
        if not reason:
            self.read += 1
            return
        setattr(self, failure_kind(reason),
                getattr(self, failure_kind(reason)) + 1)

    def as_dict(self) -> dict:
        return {"guidance_attempted": self.attempted,
                "guidance_read": self.read,
                "guidance_absent_in_source": self.absent_in_source,
                "guidance_reader_failed": self.reader_failed,
                "guidance_call_failed": self.call_failed,
                "guidance_staged": self.staged}
