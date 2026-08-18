"""Feeding the cause reader from the company's own release (T-008).

The reader was starved, not broken: 0 of 30 prints yielded a reason, because
the only text it was ever shown — the vendor's summary — states the result,
the outlook and the analysts' figure, and never why the quarter came out
where it did (measured 2026-08-17, sample 30).

The company's own earnings release does say why, but it is ~25,900 characters
of which most is financial tables. So a cheaper reader is asked first, for
the BLOCKS that explain the quarter, COPIED VERBATIM — and this module is the
machinery that refuses to take its word for it.

That refusal is not a formality. Measured 2026-08-17 over 13 names and 81
blocks, one block (AII) did not match: the extractor restated a real
220-character sentence and changed one FIGURE inside it, `$75 million` to
`$65 million`, while under explicit instruction to copy character for
character. Had the extractor been asked to SUMMARISE instead, that altered
number would have reached the tribunal as the company's own, past the one
hallucination check this system has.

(Re-measured 2026-08-18: the rest of that sentence is verbatim, longest run
210 of 220. This file previously described it as a sentence composed out of
scattered facts on a longest run of 25 — a claim that came from comparing it
against a bullet in the release's opening summary instead of searching the
whole 38,000-character article. Corrected by T-013.)

The second rejection was the opposite error and matters just as much: the
release wrote a curly quote and the extractor a straight one, so a real
passage was thrown away over a character nobody can see. Hence the
normalisation below — loosened for typography, never for words — and, since
T-013, the alignment that finds the passage anyway and cuts the RELEASE's
characters at it.
"""
from __future__ import annotations

import pytest

from hawkeye.scout.cause_source import build_cause_text

# The AII release, trimmed to the sentence the extractor rewrote.
RELEASE = (
    "Successfully renewed 2026-2027 catastrophe excess of loss reinsurance "
    "program on June 1, 2026 with a risk-adjusted rate decrease at the upper "
    "end of 15-20% declines\n\n"
    "Gross premiums written grew 14% year-over-year to $327 million during "
    "the quarter\n\n"
    "Robert Ritchie, Chief Executive Officer, commented, “We produced "
    "record voluntary new business policies and pre-tax earnings in the "
    "second quarter.”")

REAL = ("Gross premiums written grew 14% year-over-year to $327 million "
        "during the quarter")

# Verbatim except for the quote characters — the DFDV failure, which must NOT
# be treated as a fabrication.
CURLY = ("Robert Ritchie, Chief Executive Officer, commented, \"We produced "
         "record voluntary new business policies and pre-tax earnings in the "
         "second quarter.\"")

# What the extractor actually returned for AII on 2026-08-17. Against the
# TRIMMED release above it resembles no passage at all (aligns at 0.38, far
# under the 0.95 bar), which is the case this fixture is for. The full
# article is a different story and has its own fixture below.
UNRELATED = (
    "The Company benefitted from the upper end of 15-20% CAT XOL "
    "risk-adjusted rate decreases, while retaining its 1-in-130 year probable "
    "maximum loss level and reducing its aggregate retention from $95 million "
    "to $65 million")


def test_a_verbatim_block_is_kept():
    built = build_cause_text(RELEASE, [REAL])
    assert REAL in built.excerpt
    assert built.reason == ""
    assert built.rejected == ()


def test_a_block_resembling_no_passage_is_refused():
    """The one thing the alignment must never rescue: text that is not here.

    Since T-013 a refusal means the block resembles NO passage in the release
    — not that it differed from one by a space, which is what it used to mean
    and what made the count unreadable.
    """
    built = build_cause_text(RELEASE, [REAL, UNRELATED])
    assert REAL in built.excerpt
    assert UNRELATED not in built.excerpt
    assert built.rejected == (UNRELATED,)
    # Kept, not discarded: an extractor returning text from nowhere is a
    # measurement, and one that has to stay visible.
    assert built.reason == ""


def test_every_block_from_nowhere_is_named_as_a_reader_failure():
    """Nothing survived. That is not "the release explains nothing"."""
    built = build_cause_text(RELEASE, [UNRELATED])
    assert built.excerpt == ""
    assert built.reason == "extractor_invented_every_block"


def test_an_empty_extraction_says_the_release_explains_nothing():
    built = build_cause_text(RELEASE, [])
    assert built.excerpt == ""
    assert built.reason == "no_cause_in_release"


def test_typography_alone_never_rejects_a_real_passage():
    """The DFDV case: curly vs straight quotes, same words.

    Rejecting this would throw away real explanations over an invisible
    character, and would teach us to loosen the check that catches AII.
    """
    built = build_cause_text(RELEASE, [CURLY])
    assert built.rejected == ()
    # Carried in the RELEASE's characters, so the curly quotes come back —
    # the extractor's straight ones never reach the reader, and a quote taken
    # from this excerpt therefore still matches the release downstream.
    assert built.excerpt in RELEASE
    assert "“We produced" in built.excerpt


def test_the_release_itself_travels_beside_the_excerpt():
    """The quote check downstream matches against the RELEASE, not this cut.

    If it matched the excerpt, a broken extraction step would define what
    counts as the company's words.
    """
    built = build_cause_text(RELEASE, [REAL])
    assert built.source_text == RELEASE


def test_no_release_at_all_is_a_fact_about_us_not_the_company():
    built = build_cause_text("", [REAL])
    assert built.excerpt == ""
    assert built.reason == "no_release_from_feed"


def test_blocks_keep_the_order_the_release_states_them_in():
    """Read back-to-front, an excerpt reads like a different quarter."""
    built = build_cause_text(RELEASE, [CURLY, REAL])
    assert built.excerpt.index(REAL) < built.excerpt.index("“We produced")


def test_a_duplicate_block_is_only_carried_once():
    built = build_cause_text(RELEASE, [REAL, REAL])
    assert built.excerpt.count(REAL) == 1


@pytest.mark.parametrize("blank", ["", "   ", "\n\n"])
def test_a_blank_block_is_ignored_rather_than_counted_as_invented(blank):
    built = build_cause_text(RELEASE, [REAL, blank])
    assert built.rejected == ()


# --- the excerpt must be the RELEASE's characters, not the extractor's ------
#
# Measured 2026-08-17 over the 30-name sample: three blocks failed verbatim
# matching, and only ONE of them was a rewrite.
#
#   HQI  "year-on-year"   where the release says "year-over-year"   ← a rewrite
#   HLIT ...Rest-of-Market."  where the release says ...Rest-of-Market," said
#   SGA  "Net revenue decreased"  where the release says "net revenue decreased"
#
# The last two are the extractor tidying a fragment into a standalone
# sentence: same words, changed capital, changed closing punctuation. Thrown
# away, those are real explanations lost — the DFDV lesson again.
#
# Accepting them is not enough either. If the EXCERPT kept the extractor's
# punctuation, the cause reader would quote it faithfully and the quote check
# against the release would then fail, discarding the reading one step later
# for the same invisible reason. So a matched block is carried in the words
# the RELEASE uses, which makes the excerpt a literal substring of the
# release and the downstream check true by construction.

TIDIED_PUNCTUATION = ("Our strong business momentum continued in the second "
                      "quarter.")
TIDIED_CAPITAL = "Gross premiums written grew 14% year-over-year to $327 million"
REWORDED = ("Gross premiums written grew 14% year-on-year to $327 million "
            "during the quarter")

MOMENTUM = ('He said, "Our strong business momentum continued in the second '
            'quarter," and left.')


def test_a_block_the_extractor_repunctuated_is_still_the_companys_words():
    built = build_cause_text(MOMENTUM, [TIDIED_PUNCTUATION])
    assert built.rejected == ()
    # The RELEASE's characters, so it ends with a comma, not a full stop.
    assert built.excerpt in MOMENTUM
    assert not built.excerpt.endswith(".")


def test_a_block_the_extractor_recapitalised_is_still_the_companys_words():
    built = build_cause_text(RELEASE, [TIDIED_CAPITAL])
    assert built.rejected == ()
    assert built.excerpt in RELEASE


def test_a_block_with_a_substituted_word_is_carried_in_the_release_s_word():
    """"year-on-year" for "year-over-year": the passage is real, the word is
    not the company's.

    Refusing here cost the tribunal a real explanation over one word, so
    since T-013 the passage is found and the RELEASE's characters are cut at
    it — the reader sees "year-over-year". What the extractor typed is
    counted, not carried.
    """
    built = build_cause_text(RELEASE, [REWORDED])
    assert built.rejected == ()
    assert "year-over-year" in built.excerpt
    assert "year-on-year" not in built.excerpt
    assert built.excerpt in RELEASE
    # A WORD changed, so this is the alteration count and not the repair one.
    assert built.repaired == ()
    assert built.altered == ((REWORDED, built.excerpt),)


@pytest.mark.parametrize("block", [REAL, CURLY, TIDIED_CAPITAL])
def test_every_kept_block_is_a_literal_substring_of_the_release(block):
    """The property the whole downstream check rests on."""
    built = build_cause_text(RELEASE, [block])
    for piece in built.excerpt.split("\n\n"):
        assert piece in RELEASE


# --- rescuing a near miss (T-013) -------------------------------------------
#
# Every block refused up to T-012 was a real passage the check could not
# recognise: 12 of 13 differed from the release by a space our own HTML
# conversion had inserted, and the 13th (AII) by one digit the extractor
# changed. Both cost the tribunal an explanation the company actually wrote.
#
# So a block that matches nothing exactly is aligned against the release, and
# if it sits on a passage closely enough (`_RESCUE_BAR`, 0.95, set from
# measured controls) the RELEASE's characters at that passage are cut and
# used. What the extractor typed is never carried, so the rescue cannot put a
# word the company did not write in front of the tribunal — it can only pick
# the wrong passage, which is what the bar is measured against.
#
# The two counts are separate because they are two different pieces of news:
# a repair is almost always OUR defect (chase it), an alteration is the
# extractor rewriting the company (count it, and never let it hide inside the
# repairs).

DIGIT_CHANGED = ("Gross premiums written grew 14% year-over-year to $427 "
                 "million during the quarter")

# The T-012 shape, in the direction it actually happened: OUR conversion put
# the stray spaces into the release, and the extractor copied the sentence as
# a person would write it. SDRL's block is the real one, trimmed.
STRAY_SPACE_RELEASE = (
    "Second quarter 2026 Total operating revenues increased to $449 million , "
    "compared to $358 million in the prior quarter , driven by higher "
    "dayrates on the West Tellus .")
STRAY_SPACE_BLOCK = (
    "Total operating revenues increased to $449 million, compared to $358 "
    "million in the prior quarter, driven by higher dayrates on the West "
    "Tellus")


def test_a_block_our_conversion_broke_is_repaired_not_refused():
    """The T-012 class: spaces in front of punctuation, and nothing else.

    Twelve of the thirteen blocks lost before T-012 were exactly this, and
    each one was a sentence the company had written.
    """
    built = build_cause_text(STRAY_SPACE_RELEASE, [STRAY_SPACE_BLOCK])
    assert built.rejected == ()
    assert built.altered == ()
    assert len(built.repaired) == 1
    # The RELEASE's characters, stray spaces and all — this excerpt has to
    # stay a literal substring of the text the quote check runs against, even
    # while the text itself is the thing that is wrong.
    assert built.excerpt in STRAY_SPACE_RELEASE
    assert "$449 million , compared" in built.excerpt


def test_a_changed_figure_is_rescued_in_the_release_s_own_digits():
    """The AII class. The tribunal must read $327 million, never $427.

    This is the case that makes the rescue worth having AND the case that
    makes the separate count mandatory: the excerpt is correct either way,
    so nothing on the page would otherwise say the extractor had changed a
    number.
    """
    built = build_cause_text(RELEASE, [DIGIT_CHANGED])
    assert "$327 million" in built.excerpt
    assert "$427" not in built.excerpt
    assert built.excerpt in RELEASE
    assert built.repaired == ()
    assert built.altered == ((DIGIT_CHANGED, REAL),)


def test_a_rescued_block_is_still_a_literal_substring_of_the_release():
    """The property T-013 is not allowed to break, on the rescue path too."""
    for block in (DIGIT_CHANGED, REWORDED):
        built = build_cause_text(RELEASE, [block])
        for piece in built.excerpt.split("\n\n"):
            assert piece in RELEASE
    built = build_cause_text(STRAY_SPACE_RELEASE, [STRAY_SPACE_BLOCK])
    assert built.excerpt in STRAY_SPACE_RELEASE


def test_kept_counts_every_block_that_reached_the_excerpt():
    """The denominator the verbatim rate is quoted against."""
    built = build_cause_text(RELEASE, [REAL, CURLY, UNRELATED])
    assert built.kept == 2
    assert len(built.rejected) == 1


def test_a_block_rescued_onto_a_passage_already_kept_is_not_repeated():
    """Two spellings of one passage are one passage, however differently
    they were typed."""
    built = build_cause_text(RELEASE, [REAL, DIGIT_CHANGED])
    assert built.excerpt.count(REAL) == 1
    assert built.kept == 1


# --- T-011: a failure that can be diagnosed ---------------------------------

class _Feed:
    def __init__(self, article):
        self._article = article

    def article(self, ticker, article_id):
        if isinstance(self._article, Exception):
            raise self._article
        return self._article


class _Extractor:
    available = True

    def __init__(self, outcome):
        self._outcome = outcome

    def blocks(self, release, ticker, fiscal_quarter):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def test_a_failed_extraction_keeps_the_named_reason():
    """The vocabulary downstream classifies on must not change. `detail` is
    added beside it, never instead of it."""
    from hawkeye.scout.cause_source import ReleaseCauseSource

    built = ReleaseCauseSource(
        _Feed(RELEASE), _Extractor(RuntimeError("quota"))
    ).text_for("AII", "2608137728", "2026-Q2")
    assert built.reason == "extractor_call_failed"


def test_a_failed_extraction_also_keeps_what_went_wrong():
    """Two sessions were spent guessing at a limit whose answer was in the
    body of the reply that got thrown away here (2026-08-17 429, 2026-08-18
    400). The named reason says WHICH step failed; this says why."""
    from hawkeye.scout.cause_source import ReleaseCauseSource

    built = ReleaseCauseSource(
        _Feed(RELEASE),
        _Extractor(RuntimeError("the extractor answered 429: daily quota, "
                                "limit: 20"))
    ).text_for("AII", "2608137728", "2026-Q2")
    assert "limit: 20" in built.detail


def test_a_failed_release_fetch_also_keeps_what_went_wrong():
    from hawkeye.scout.cause_source import ReleaseCauseSource

    built = ReleaseCauseSource(
        _Feed(RuntimeError("EW answered 503")), _Extractor([])
    ).text_for("AII", "2608137728", "2026-Q2")
    assert built.reason == "release_fetch_failed"
    assert "503" in built.detail


def test_a_success_carries_no_detail():
    """An empty detail is what "nothing went wrong" looks like — a leftover
    message beside a good excerpt would read as a warning about it."""
    built = build_cause_text(RELEASE, [REAL])
    assert built.detail == ""
