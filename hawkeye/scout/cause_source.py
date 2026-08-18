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

**This is not hypothetical.** Measured 2026-08-17 over 13 names / 81 blocks,
one block did not match: AII returned a sentence ending

    "...reducing its aggregate retention from $95 million to $65 million"

where the release says `$75 million`. **The rest of that 220-character
sentence is the company's own, character for character** (longest verbatim
run 210; re-measured 2026-08-18 against the same article, id 2608116636).
`$65 million` appears nowhere in the release. So the failure this step
actually catches is not a composed sentence — it is the extractor restating a
real passage and changing a FIGURE inside it, under explicit instruction to
copy. That is harder to spot than invention and lands straight on a number
the tribunal reasons about, which is the whole argument for checking
mechanically instead of instructing harder.

(Until 2026-08-18 this file, three task records and two documents described
that block as invented "out of scattered facts", on a longest run of 25
characters. It was wrong: the sentence had been compared against a bullet in
the release's opening summary and the other 38,000 characters were never
searched. Corrected by T-013, whose threshold measurement is what surfaced
it — the claim it was built on could not be reproduced.)

**The opposite error costs just as much.** DFDV's block differed from the
release by curly versus straight quotes and nothing else. Thrown away, that
is a real explanation lost to an invisible character. So the comparison is
normalised for typography (quotes, dashes, non-breaking spaces) and for
nothing else — and where even that is not enough, the passage is found by
alignment and the RELEASE's characters are cut at it (T-013, `_RESCUE_BAR`).
Nothing the extractor typed ever reaches the excerpt, on any path through
this module, which is what makes rescuing safe: AII rescued puts `$75
million` in front of the tribunal, not `$65 million`.

What leaves here is a pair, never a single text: the EXCERPT the reader is
shown, and the RELEASE it was cut from. The quote check downstream matches
against the release. If it matched the excerpt, a broken extraction step
would get to define what counts as the company's own words.
"""
from __future__ import annotations

import re
import string
from dataclasses import dataclass
from difflib import SequenceMatcher

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

# How closely a block must align to a release passage before that passage is
# cut and used (T-013). The bar answers ONE question \u2014 is this the same
# passage, or a different one \u2014 so it was set from the two populations that
# can make that answer wrong, measured 2026-08-18 over five releases
# (AII/ALCO/FOSL/MSGE/SDRL, 89 sampled passages):
#
#   0.7018  the highest a passage scored against a DIFFERENT company's
#           release (356 pairs). Earnings releases share their vocabulary and
#           their boilerplate, so this is the hard negative, not a strawman.
#   0.8951  the highest a passage scored against a different passage of its
#           OWN release, once its true home was blanked out (86 cases). This
#           is the confusion that would actually hurt: ALCO states the same
#           line for "three months ended" and "nine months ended", and that
#           particular pair reached only 0.7636.
#   0.9932  the LOWEST of the 13 blocks refused for our own defect (T-012).
#
# The window is therefore (0.8951, 0.9932]; 0.95 sits near its middle, four
# points clear on each side. Below the bar nothing is cut and the block is
# refused exactly as before.
_RESCUE_BAR = 0.95


@dataclass(frozen=True)
class CauseText:
    """What the cause reader is shown, and what its quote is checked against.

    `excerpt` is the reader's input; `source_text` is the release it came
    from and is the authority for every downstream check. They are carried
    together rather than derived from one another because the entire point is
    that the excerpt is NOT trusted to represent the release.

    `rejected` holds the blocks that matched no passage at all, kept rather
    than counted. An extractor composing sentences is a measurement about the
    extractor, and a bare count would say it happened without ever saying
    what it invented.

    `repaired` and `altered` hold what was rescued, and they are two fields
    rather than one because they are two different pieces of news (T-013).
    `repaired` means the block and the release differed only in whitespace,
    punctuation or typography — almost always OUR conversion, which is how
    T-012 was found. `altered` means a letter or a digit differed: the
    extractor changed the company's words, and AII's `$75 million` returned
    as `$65 million` is what that looks like. Both were rescued, and in both
    the excerpt carries the RELEASE's characters — the alteration never
    reaches a reader. Merging the two counts would hide the second inside the
    first, which is the one thing this rescue must not do.

    `kept` counts the blocks that reached the excerpt by any route. It is
    stored rather than derived from `excerpt` because a cut passage may
    itself contain a blank line, so splitting the excerpt back apart
    undercounts — and this number is the denominator every verbatim-rate
    measurement in `docs/knowledge/MEASUREMENTS.ja.md` is quoted against.

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
    repaired: tuple[str, ...] = ()
    altered: tuple[tuple[str, str], ...] = ()
    kept: int = 0


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


def _closest_span(hay: str, needle: str) -> tuple[int, int, float] | None:
    """Where in `hay` the passage most nearly sits, and how nearly, or None.

    Both arguments are already `_comparable`. The span is returned in `hay`'s
    coordinates so the caller can cut the RELEASE's characters at it — the
    extractor's text is never what comes back, which is what keeps a rescued
    excerpt a literal substring of the release (T-013 prohibition 1).

    The score is the usual similarity ratio, but taken between the needle and
    the ALIGNED SPAN rather than the whole release: matched characters over
    the mean length of the two. Scoring against the release would make every
    block look equally unlike a 25,900-character document.

    A window is cut around the longest shared run before the alignment is
    computed, because the alignment is quadratic and the release is not
    small. The slack lets the span breathe by a quarter of the block's length
    at each end, which is far more than the differences this is for (a
    stray space, a curly quote) and far less than a neighbouring paragraph.
    """
    if not needle or not hay:
        return None
    anchor = SequenceMatcher(None, hay, needle,
                             autojunk=False).find_longest_match(
                                 0, len(hay), 0, len(needle))
    if not anchor.size:
        return None
    slack = max(len(needle) // 4, 40)
    start = max(0, anchor.a - anchor.b - slack)
    end = min(len(hay), anchor.a - anchor.b + len(needle) + slack)
    local = SequenceMatcher(None, hay[start:end], needle, autojunk=False)
    runs = [run for run in local.get_matching_blocks() if run.size]
    if not runs:
        return None
    span_start = runs[0].a
    span_end = runs[-1].a + runs[-1].size
    matched = sum(run.size for run in runs)
    score = 2 * matched / ((span_end - span_start) + len(needle))
    return start + span_start, start + span_end, score


def _differs_in_a_word(release_span: str, block: str) -> bool:
    """Does any LETTER OR DIGIT differ between the two?

    This is the line between the two kinds of rescue, and it is drawn on the
    character class rather than on a similarity number because the two mean
    different things no matter how small they are. A missing space is our
    conversion; a changed digit is the extractor rewriting the company's
    figures, and one is worth chasing while the other is worth counting.

    Measured 2026-08-18: all 13 blocks refused for the T-012 defect differ
    only by a deleted space (False here), while AII's block differs by one
    digit (True). No threshold separates those two sets — AII scored 0.9955
    against a lowest legitimate 0.9932 — and that is why this question is
    asked of the characters instead.
    """
    for op, i1, i2, j1, j2 in SequenceMatcher(
            None, release_span, block, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if any(char.isalnum() for char in release_span[i1:i2] + block[j1:j2]):
            return True
    return False


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
    repaired: list[str] = []
    altered: list[tuple[str, str]] = []
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
            trimmed = needle.strip(_EDGE_PUNCTUATION)
            position = hay.find(trimmed) if trimmed else -1
            if position >= 0:
                needle = trimmed
        if position >= 0:
            start, stop, outcome = position, position + len(needle), "exact"
        else:
            # Third chance, and the last: where does this passage MOST NEARLY
            # sit? Refusing here has never once meant the extractor invented
            # something — every refusal measured so far was our own conversion
            # (12 of 13) or a single digit the extractor changed (AII). Both
            # cost the tribunal a real explanation, so the passage is found
            # and the RELEASE's characters are cut at it.
            found = _closest_span(hay, needle)
            if found is None or found[2] < _RESCUE_BAR:
                rejected.append(text)
                continue
            start, stop, _score = found
            outcome = "altered" if _differs_in_a_word(
                hay[start:stop], needle) else "repaired"
        # Deduplicated on the RELEASE span rather than on what the extractor
        # sent: two blocks that resolve to the same passage ARE the same
        # passage, however differently they were typed.
        span = hay[start:stop]
        if span in seen:
            continue
        seen.add(span)
        # The RELEASE's characters, sliced at the match, NOT the extractor's.
        # This is what makes the excerpt a literal substring of the release,
        # and the downstream quote check true by construction. It holds for a
        # rescued block exactly as for an exact one, which is what lets the
        # rescue exist at all (T-013).
        cut = source[origin[start]:origin[stop - 1] + 1]
        kept.append((start, cut))
        if outcome == "repaired":
            repaired.append(cut)
        elif outcome == "altered":
            altered.append((text, cut))

    if not kept:
        # Both of these mean "no excerpt", and they are not the same event.
        # An extractor that returned only inventions is broken; one that
        # returned nothing is reporting that the release is silent, which is
        # a normal and common answer. The first name means more since T-013:
        # a refusal now says the block resembles NO passage in the release,
        # rather than that it differed from one by a space.
        reason = ("extractor_invented_every_block" if rejected
                  else "no_cause_in_release")
        return CauseText("", source, reason, tuple(rejected))

    kept.sort(key=lambda pair: pair[0])
    return CauseText(_BLOCK_SEPARATOR.join(text for _, text in kept),
                     source, "", tuple(rejected),
                     repaired=tuple(repaired), altered=tuple(altered),
                     kept=len(kept))


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
