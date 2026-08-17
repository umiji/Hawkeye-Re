"""Turning an earnings release into something the cause reader can be shown.

The cause reader (`hawkeye/scout/cause_agent.py`) was starved rather than
broken: 0 of 30 prints yielded a reason because the only text it ever saw was
the vendor's summary, which states the result, the outlook and the analysts'
figure and never why the quarter came out where it did (measured 2026-08-17,
sample 30). The company's own release does say why — but it runs ~25,900
characters, most of it financial tables.

So a cheaper reader is asked first, for the BLOCKS that explain the quarter,
**copied verbatim**. This module is what refuses to take its word for it.

**Why verbatim and not a summary.** The cause reader's only hallucination
check is that its quote exists, character for character, in the text it was
given. Put a SUMMARY in front of it and the words become the summariser's:
the check still passes, and a machine-written sentence reaches the tribunal
as the company's own. The check would be measuring the wrong document.

**This is not hypothetical.** Measured 2026-08-17 over 13 names / 81 blocks:
79 verbatim, and AII returned

    "The Company benefitted from the upper end of 15-20% CAT XOL
     risk-adjusted rate decreases, while retaining its 1-in-130 year probable
     maximum loss level and reducing its aggregate retention from $95 million
     to $65 million"

where the release says only that the program was renewed "with a risk-adjusted
rate decrease at the upper end of 15-20% declines". Longest verbatim run: 25
of 220 characters. Every fact in it is somewhere in the release; the sentence
— and the claim that the company *benefitted* — is not. It was composed while
under explicit instruction to copy character for character, which is the
whole argument for checking mechanically instead of instructing harder.

**The opposite error costs just as much.** DFDV's block differed from the
release by curly versus straight quotes and nothing else. Thrown away, that
is a real explanation lost to an invisible character — and the lesson learned
would be to loosen the check that catches AII. So the comparison is normalised
for typography (quotes, dashes, non-breaking spaces) and for nothing else.

What leaves here is a pair, never a single text: the EXCERPT the reader is
shown, and the RELEASE it was cut from. The quote check downstream matches
against the release. If it matched the excerpt, a broken extraction step
would get to define what counts as the company's own words.
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass

# Typographic variants that carry no meaning. The extractor normalises them
# on its own (measured: DFDV), and a release that writes “SPS” is saying the
# same thing as one that writes "SPS". Nothing here touches a word, a digit
# or a unit — those differences are exactly what the check is for.
_TYPOGRAPHY = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-",
    " ": " ", " ": " ", " ": " ", "​": " ",
})

# What the extractor changes when it tidies a fragment into a standalone
# sentence, and nothing more. Measured 2026-08-17 over 30 names: of the three
# blocks that failed matching, two were exactly this — HLIT closed with a
# full stop where the release has `," said`, and SGA capitalised a `net` the
# release writes mid-sentence. The third (HQI: "year-on-year" for the
# release's "year-over-year") is a WORD change and stays refused, which is
# the line this tolerance must not cross.
_EDGE_PUNCTUATION = string.whitespace + "\"'`.,;:!?-()[]\u2013\u2014\u2026"
_BLOCK_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class CauseText:
    """What the cause reader is shown, and what its quote is checked against.

    `excerpt` is the reader's input; `source_text` is the release it came
    from and is the authority for every downstream check. They are carried
    together rather than derived from one another because the entire point is
    that the excerpt is NOT trusted to represent the release.

    `rejected` holds the blocks that failed verbatim matching, kept rather
    than counted. An extractor composing sentences is a measurement about the
    extractor, and a bare count would say it happened without ever saying
    what it invented.

    `reason` is empty when there is an excerpt to read. When it is set it
    names WHY there is none, and the three ways that happens are deliberately
    distinct: no release reached us, the release explained nothing, or the
    extractor returned only inventions. The first is a fact about us, the
    second about the company, the third about our reader — and only the third
    is a defect to chase (invariant 6).

    `detail` is what the failing step actually said, and it exists BESIDE
    `reason` rather than inside it: `reason` is a small fixed vocabulary the
    ledger stores and `failure_kind` classifies, and free text in that column
    would break both. Added by T-011 after two sessions were spent guessing
    at a rate limit whose limit, id and window were all stated in a reply
    this class caught and dropped (2026-08-17 429, 2026-08-18 400). Empty on
    success — a message sitting beside a good excerpt would read as a warning
    about it.
    """
    excerpt: str
    source_text: str
    reason: str = ""
    rejected: tuple[str, ...] = ()
    detail: str = ""


def _said(exc: BaseException) -> str:
    """One line naming what failed, for a person reading a scan's stderr.

    The exception type is kept: `GeminiUnavailable` and a bare `TimeoutError`
    are different stories about the same blank excerpt.
    """
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _comparable(text: str) -> str:
    """Whitespace, typography and case normalised, for finding one string in
    another.

    Case is folded to match what the cause gate's own quote check already
    does downstream. Nothing here can let a different WORD through, which is
    the only difference that matters: HQI's "year-on-year" for the release's
    "year-over-year" survives every normalisation in this module and is
    refused, while SGA's capitalised "Net" does not.
    """
    return re.sub(r"\s+", " ",
                  (text or "").translate(_TYPOGRAPHY)).strip().lower()


def _indexed(text: str) -> tuple[str, list[int]]:
    """`_comparable(text)`, plus where each of its characters came from.

    The map is what lets a match be carried back in the RELEASE's own
    characters rather than the extractor's. Without it the excerpt would keep
    whatever the extractor typed, and a reader quoting that excerpt faithfully
    would then fail the quote check against the release — the reading thrown
    away one step later, for a full stop.
    """
    chars: list[str] = []
    positions: list[int] = []
    in_space = False
    for index, char in enumerate(text):
        plain = char.translate(_TYPOGRAPHY)
        if plain.isspace():
            if chars and not in_space:
                chars.append(" ")
                positions.append(index)
            in_space = True
            continue
        in_space = False
        chars.append(plain.lower())
        positions.append(index)
    while chars and chars[-1] == " ":
        chars.pop()
        positions.pop()
    return "".join(chars), positions


def build_cause_text(release: str, blocks) -> CauseText:
    """Keep the blocks the release actually contains, in the release's order.

    Order is restored from the release rather than trusted from the reply:
    the extractor is asked for the most supporting block first, and an
    excerpt read back-to-front describes the quarter in the wrong sequence.

    Blank blocks are dropped silently. They are not inventions — there is
    nothing there to have invented — and counting them as such would inflate
    the one number that is supposed to mean "our reader is composing text".
    """
    source = release or ""
    if not source.strip():
        # Nothing arrived to read. A fact about the feed, not the company,
        # and it must never render as "this release explains nothing".
        return CauseText("", source, "no_release_from_feed")

    hay, origin = _indexed(source)
    kept: list[tuple[int, str]] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for block in blocks or []:
        text = str(block or "")
        if not text.strip():
            continue
        needle = _comparable(text)
        position = hay.find(needle)
        if position < 0:
            # Second chance, and only for the edges. An extractor asked for a
            # passage often returns a fragment tidied into a sentence, and a
            # trailing full stop the release spells as a comma is not a
            # rewrite. Nothing inside the passage is touched.
            needle = needle.strip(_EDGE_PUNCTUATION)
            position = hay.find(needle) if needle else -1
        if position < 0:
            rejected.append(text)
            continue
        if needle in seen:
            continue
        seen.add(needle)
        # The RELEASE's characters, sliced at the match, NOT the extractor's.
        # This is what makes the excerpt a literal substring of the release,
        # and the downstream quote check true by construction.
        start = origin[position]
        end = origin[position + len(needle) - 1] + 1
        kept.append((position, source[start:end]))

    if not kept:
        # Both of these mean "no excerpt", and they are not the same event.
        # An extractor that returned only inventions is broken; one that
        # returned nothing is reporting that the release is silent, which is
        # a normal and common answer.
        reason = ("extractor_invented_every_block" if rejected
                  else "no_cause_in_release")
        return CauseText("", source, reason, tuple(rejected))

    kept.sort(key=lambda pair: pair[0])
    return CauseText(_BLOCK_SEPARATOR.join(text for _, text in kept),
                     source, "", tuple(rejected))


class ReleaseCauseSource:
    """Fetch one release, have it cut, and refuse whatever was not in it.

    The three steps are joined here rather than in the scan so that each way
    they fail keeps its own name. A scan that saw one boolean could not tell
    a feed with no article from an extractor nobody paid for, and both would
    be written into the ledger as companies that explained nothing.
    """

    def __init__(self, feed, extractor) -> None:
        self._feed = feed
        self._extractor = extractor

    @property
    def available(self) -> bool:
        return bool(self._feed) and bool(
            getattr(self._extractor, "available", False))

    def text_for(self, ticker: str, article_id: str,
                 fiscal_quarter: str) -> CauseText:
        """The excerpt for one print, or the named reason there is none.

        Never raises. Every failure below is a reason a downstream row has to
        carry, and an exception escaping here would abort a scan over one
        company's press release.
        """
        if not (article_id or "").strip():
            return CauseText("", "", "no_release_from_feed")
        try:
            release = self._feed.article(ticker, article_id)
        except Exception as exc:
            # About the connection, not the company, and worth retrying —
            # which is exactly what `release_fetch_failed` is classified as.
            return CauseText("", "", "release_fetch_failed",
                             detail=_said(exc))
        if not (release or "").strip():
            return CauseText("", "", "no_release_from_feed")
        try:
            blocks = self._extractor.blocks(release, ticker, fiscal_quarter)
        except Exception as exc:
            # The release DID arrive, so it travels on: the reason names our
            # reader, and the text is still the authority a later attempt
            # would check against. What the reader SAID travels with it —
            # swallowed, it takes the quota id, the limit and the offending
            # field down with it (T-011).
            return CauseText("", release, "extractor_call_failed",
                             detail=_said(exc))
        return build_cause_text(release, blocks)
