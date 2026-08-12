"""Reading the company's forward statement with an agent (task 8.7 layer 2).

The agent reads; this parser decides what is allowed through. Everything here
is offline — a dict stands in for the reply, so what these tests pin is the
gate, not the model.

Three things the gate exists to stop, and they are different in kind:

- **A figure nobody published.** The reply must quote the sentence it read,
  and the quote must be findable in the summary character for character.
  That is the only hallucination check available: nothing else here can tell
  a range the company gave from a range that reads plausibly.
- **The wrong sentence.** The summary states the company's guidance, the
  company's PREVIOUS guidance, and the analysts' consensus in adjacent
  sentences, all in the same shape. Reading the second or third would look
  exactly like reading the first.
- **A unit slip.** The vendor writes revenue in millions and billions and
  every contract in this system is in dollars, so the agent states the unit
  it read and the parser does the multiplication. A model returning "2.85"
  for $2.85 billion cannot then be a 1,000,000,000x error.
"""
from __future__ import annotations

import pytest

from hawkeye.scout.guidance_agent import (
    GuidanceRequest,
    build_schema,
    parse_reply,
    render_request,
)

# ACA's real summary: the company's own range, its previous range, and the
# analysts' figure — three ranges, one of which is guidance.
ACA_SUMMARY = (
    "Arcosa reported first quarter earnings of $1.02 per share. "
    "The company said it expects 2026 revenue of $2.60 billion to $2.70 "
    "billion, excluding its barge business. The company's previous guidance "
    "was revenue of $2.54 billion to $2.67 billion, excluding its barge "
    "business, and the current consensus revenue estimate, which includes "
    "its barge business, is $3.02 billion for the year ending December 31, "
    "2026.")

# ALGT's, which no regular expression can turn into a range: a loss of $1.00
# to breakeven is -1.00 to 0.00, and only a reader who understands the words
# knows that.
ALGT_SUMMARY = (
    "Allegiant reported second quarter earnings of $1.55 per share. "
    "The company said it expects third quarter results to range from a loss "
    "of $1.00 per share to breakeven and 2026 earnings of more than $6.00 "
    "per share.")


def a_request(summary=ACA_SUMMARY, **kw) -> GuidanceRequest:
    base = dict(ticker="ACA", fiscal_quarter="2026-Q1",
                next_quarter="2026-Q2", summary=summary)
    base.update(kw)
    return GuidanceRequest(**base)


# --- what the agent is given -----------------------------------------------

def test_the_package_carries_the_whole_summary_verbatim():
    """Cutting it down to "the guidance sentence" would need the reading this
    step exists to produce."""
    assert ACA_SUMMARY in render_request(a_request())


def test_the_package_never_shows_the_agent_what_it_would_be_compared_against():
    """The consensus figure decides whether this reading becomes a beat. An
    extractor that can see the bar it is about to clear is not extracting."""
    text = render_request(a_request())

    assert "consensus" not in text.lower().replace(ACA_SUMMARY.lower(), "")


def test_the_package_names_the_quarter_that_follows_the_one_reported():
    # "third quarter" in the prose means nothing without it.
    assert "2026-Q2" in render_request(a_request())


# --- an ordinary reading ---------------------------------------------------

def test_a_quoted_full_year_range_is_accepted():
    out = parse_reply({
        "guided": True, "period": "FY2026",
        "revenue_low": 2.60, "revenue_high": 2.70, "revenue_unit": "billion",
        "quote": "2026 revenue of $2.60 billion to $2.70 billion",
    }, a_request(), model="test-model")

    assert out.reason == ""
    assert out.reading.period == "FY2026"
    assert out.reading.revenue_low == pytest.approx(2.60e9)
    assert out.reading.revenue_high == pytest.approx(2.70e9)


def test_the_unit_the_agent_read_is_what_converts_the_figure():
    """The vendor writes millions and billions in the same corpus and every
    contract downstream is in dollars. A model that returned raw 2.85 with no
    unit would be a 1,000,000,000x error nothing else could catch."""
    out = parse_reply({
        "guided": True, "period": "FY2026",
        "revenue_low": 275.0, "revenue_high": 295.0, "revenue_unit": "million",
        "quote": "2026 revenue of $2.60 billion to $2.70 billion",
    }, a_request(), model="m")

    assert out.reading.revenue_low == pytest.approx(275e6)


def test_a_reading_records_who_read_it():
    out = parse_reply({
        "guided": True, "period": "FY2026",
        "revenue_low": 2.60, "revenue_high": 2.70, "revenue_unit": "billion",
        "quote": "2026 revenue of $2.60 billion to $2.70 billion",
    }, a_request(), model="claude-opus-4-8")

    assert out.reading.extractor == "agent"
    assert out.reading.extractor_model == "claude-opus-4-8"


def test_a_range_written_high_first_is_ordered_not_refused():
    out = parse_reply({
        "guided": True, "period": "FY2026",
        "revenue_low": 2.70, "revenue_high": 2.60, "revenue_unit": "billion",
        "quote": "2026 revenue of $2.60 billion to $2.70 billion",
    }, a_request(), model="m")

    assert out.reading.revenue_low == pytest.approx(2.60e9)
    assert out.reading.revenue_high == pytest.approx(2.70e9)


# --- the reading no regular expression can produce -------------------------

def test_a_loss_to_breakeven_becomes_a_negative_range():
    """The whole reason this layer exists. "a loss of $1.00 per share to
    breakeven" is -1.00 to 0.00, and the words are what say so."""
    out = parse_reply({
        "guided": True, "period": "2026-Q3",
        "eps_low": -1.00, "eps_high": 0.0,
        "quote": ("third quarter results to range from a loss of $1.00 per "
                  "share to breakeven"),
    }, a_request(ticker="ALGT", fiscal_quarter="2026-Q2",
                 next_quarter="2026-Q3", summary=ALGT_SUMMARY), model="m")

    assert out.reason == ""
    assert out.reading.eps_low == pytest.approx(-1.00)
    assert out.reading.eps_high == pytest.approx(0.0)


def test_an_open_ended_range_is_still_refused():
    """"more than $6.00 per share" has no top. Reading $6.00 as the midpoint
    understates a floor the company deliberately left open, and inventing a
    top is the one thing this parser exists to prevent — the agent naming it
    is the improvement, not the agent guessing past it."""
    out = parse_reply({
        "guided": True, "period": "FY2026", "eps_low": 6.00, "eps_high": None,
        "open_ended": True,
        "quote": "2026 earnings of more than $6.00 per share",
    }, a_request(ticker="ALGT", summary=ALGT_SUMMARY), model="m")

    assert out.reading is None
    assert out.reason == "open_ended_range"


# --- the hallucination gate ------------------------------------------------

def test_a_quote_that_is_not_in_the_summary_voids_the_whole_reading():
    out = parse_reply({
        "guided": True, "period": "FY2026",
        "revenue_low": 3.10, "revenue_high": 3.30, "revenue_unit": "billion",
        "quote": "2026 revenue of $3.10 billion to $3.30 billion",
    }, a_request(), model="m")

    assert out.reading is None
    assert out.reason == "quote_not_in_source"


def test_a_quote_is_matched_through_reformatted_whitespace():
    """The summary arrives with line breaks and doubled spaces in it. A
    reading thrown away over an invisible character would train us to
    loosen the check that matters."""
    out = parse_reply({
        "guided": True, "period": "FY2026",
        "revenue_low": 2.60, "revenue_high": 2.70, "revenue_unit": "billion",
        "quote": "2026   revenue of\n$2.60 billion to $2.70 billion",
    }, a_request(), model="m")

    assert out.reason == ""


def test_quoting_the_previous_guidance_is_refused():
    """It is in the same summary, in the same shape, one sentence later. ACA's
    previous range is $2.54-$2.67 billion against a current $2.60-$2.70."""
    out = parse_reply({
        "guided": True, "period": "FY2026",
        "revenue_low": 2.54, "revenue_high": 2.67, "revenue_unit": "billion",
        "quote": "revenue of $2.54 billion to $2.67 billion",
    }, a_request(), model="m")

    assert out.reading is None
    assert out.reason == "quoted_the_wrong_sentence"


def test_quoting_the_analyst_consensus_is_refused():
    """The worst of the three: the consensus IS the bar, so reading it as
    guidance produces a beat of exactly zero on every name it happens to."""
    out = parse_reply({
        "guided": True, "period": "FY2026",
        "revenue_low": 3.02, "revenue_high": 3.02, "revenue_unit": "billion",
        "quote": "the current consensus revenue estimate, which includes its "
                 "barge business, is $3.02 billion",
    }, a_request(), model="m")

    assert out.reading is None
    assert out.reason == "quoted_the_wrong_sentence"


# --- the condition (layer 3) -----------------------------------------------

def test_a_condition_the_agent_reports_must_also_be_in_the_summary():
    out = parse_reply({
        "guided": True, "period": "FY2026",
        "revenue_low": 2.60, "revenue_high": 2.70, "revenue_unit": "billion",
        "quote": "2026 revenue of $2.60 billion to $2.70 billion",
        "qualifier": "excluding its barge business",
    }, a_request(), model="m")

    assert out.reading.qualifier == "excluding its barge business"


def test_a_condition_the_agent_invented_voids_the_reading():
    """A fabricated condition is not the safe direction. It reads as a
    refusal, so it would silently delete a real guidance beat, and nobody
    checking the numbers would see anything wrong."""
    out = parse_reply({
        "guided": True, "period": "FY2026",
        "revenue_low": 2.60, "revenue_high": 2.70, "revenue_unit": "billion",
        "quote": "2026 revenue of $2.60 billion to $2.70 billion",
        "qualifier": "excluding restructuring charges",
    }, a_request(), model="m")

    assert out.reading is None
    assert out.reason == "quote_not_in_source"


# --- the period ------------------------------------------------------------

def test_a_quarterly_period_that_is_not_the_next_quarter_is_refused():
    """Same rule the code path has: guidance for a period other than the one
    a yardstick exists for is a comparison across periods, and ADM's version
    of that error read as a +348% beat."""
    out = parse_reply({
        "guided": True, "period": "2026-Q4",
        "eps_low": 1.0, "eps_high": 1.2,
        "quote": "2026 revenue of $2.60 billion to $2.70 billion",
    }, a_request(), model="m")

    assert out.reading is None
    assert out.reason == "period_not_next_quarter"


def test_a_period_in_no_known_shape_is_refused():
    out = parse_reply({
        "guided": True, "period": "next year",
        "eps_low": 1.0, "eps_high": 1.2,
        "quote": "2026 revenue of $2.60 billion to $2.70 billion",
    }, a_request(), model="m")

    assert out.reading is None
    assert out.reason == "period_unreadable"


# --- nothing to read -------------------------------------------------------

def test_a_company_that_guided_nothing_is_not_a_failure():
    out = parse_reply({"guided": False}, a_request(), model="m")

    assert out.reading is None
    assert out.reason == "no_guidance_in_source"


def test_a_reading_with_no_figure_at_all_is_refused():
    out = parse_reply({
        "guided": True, "period": "FY2026",
        "quote": "2026 revenue of $2.60 billion to $2.70 billion",
    }, a_request(), model="m")

    assert out.reading is None
    assert out.reason == "no_number_in_source"


# --- which of the three kinds of failure it was ----------------------------
#
# The three were fixed on 2026-08-09: the source held no number, the reader
# could not read it, or the call never completed. They have to stay
# distinguishable — "we found no guidance" and "our extractor broke" look
# identical in a report that only counts blanks.

@pytest.mark.parametrize("reason,kind", [
    ("no_guidance_in_source", "absent_in_source"),
    ("open_ended_range", "absent_in_source"),
    ("no_number_in_source", "absent_in_source"),
    ("quote_not_in_source", "reader_failed"),
    ("quoted_the_wrong_sentence", "reader_failed"),
    ("period_unreadable", "reader_failed"),
    ("period_not_next_quarter", "reader_failed"),
    ("extraction_call_failed", "call_failed"),
    ("pending_extraction", "not_yet_read"),
])
def test_every_refusal_maps_to_one_of_the_three_kinds(reason, kind):
    from hawkeye.scout.guidance_agent import failure_kind
    assert failure_kind(reason) == kind


def test_an_unknown_reason_is_never_silently_binned():
    from hawkeye.scout.guidance_agent import failure_kind
    with pytest.raises(ValueError):
        failure_kind("something_new")


# --- the schema ------------------------------------------------------------

def test_the_schema_forbids_fields_nobody_reviewed():
    schema = build_schema()
    assert schema["additionalProperties"] is False
    assert "quote" in schema["required"]


# --- the tally -------------------------------------------------------------

def test_the_three_kinds_are_counted_apart():
    """"We found no guidance" and "our extractor broke" are the two facts a
    single blank count cannot separate, and only one of them is our
    problem."""
    from hawkeye.scout.guidance_agent import GuidanceStats

    stats = GuidanceStats()
    stats.record("")
    stats.record("no_guidance_in_source")
    stats.record("quote_not_in_source")
    stats.record("extraction_call_failed")

    assert stats.as_dict() == {"guidance_attempted": 4, "guidance_read": 1,
                               "guidance_absent_in_source": 1,
                               "guidance_reader_failed": 1,
                               "guidance_call_failed": 1,
                               "guidance_staged": 0}


# --- the real ALGT response, which broke both checks at once ---------------
#
# Found by running the session-mode loop against the recorded response on
# 2026-08-10, after the tests above all passed on a hand-written summary. Two
# defects, and the hand-written summary could not have shown either:
#
# - the vendor separates paragraphs with `<br /><br />` and NO space after the
#   preceding period, so a splitter that breaks on ". " glued the results
#   paragraph onto the guidance paragraph;
# - the results paragraph says the company "beat consensus estimates by
#   72.44%", which is about the quarter just reported. Treating the substring
#   "consensus estimate" as the analysts' forward sentence made that a decoy,
#   and the glued paragraph then failed the wrong-sentence check.
#
# Together they refused a reading that was correct, and named the failure
# `quoted_the_wrong_sentence` — a refusal blaming the agent for our own bug.

ALGT_REAL = (
    "Allegiant Travel (ALGT) reported earnings of $2.19 per share on revenue "
    "of $943.49 million for the  second quarter ended June 2026.  The "
    "consensus earnings estimate was $1.27 per share on  revenue of $1.03 "
    "billion. The company beat consensus estimates by 72.44% while revenue "
    "grew 36.86% on a year-over-year basis.<br /><br />The company  said it "
    "expects  third quarter  results to range from a loss of $1.00 per share "
    "to  breakeven and 2026 earnings of more than $6.00 per share. The "
    "current consensus estimate is earnings of $0.08 per share for the "
    "quarter ending September 30, 2026 and earnings of $7.50 per share for "
    "the year ending December 31, 2026.")


def test_the_vendors_paragraph_break_ends_a_sentence():
    out = parse_reply({
        "guided": True, "period": "2026-Q3", "eps_low": -1.00, "eps_high": 0.0,
        "quote": ("third quarter  results to range from a loss of $1.00 per "
                  "share to  breakeven"),
    }, a_request(ticker="ALGT", fiscal_quarter="2026-Q2",
                 next_quarter="2026-Q3", summary=ALGT_REAL), model="m")

    assert out.reason == ""
    assert out.reading.eps_low == pytest.approx(-1.00)


def test_the_quarter_just_reported_is_not_the_analysts_forward_sentence():
    """"beat consensus estimates by 72.44%" describes what already happened.
    Only "the current consensus ... is" is the bar this reading would be
    measured against."""
    out = parse_reply({
        "guided": True, "period": "2026-Q3", "eps_low": -1.00, "eps_high": 0.0,
        "quote": "a loss of $1.00 per share to  breakeven",
    }, a_request(ticker="ALGT", fiscal_quarter="2026-Q2",
                 next_quarter="2026-Q3", summary=ALGT_REAL), model="m")

    assert out.reason == ""


def test_the_forward_consensus_sentence_is_still_a_decoy():
    out = parse_reply({
        "guided": True, "period": "2026-Q3", "eps_low": 0.08, "eps_high": 0.08,
        "quote": ("The current consensus estimate is earnings of $0.08 per "
                  "share for the quarter ending September 30, 2026"),
    }, a_request(ticker="ALGT", fiscal_quarter="2026-Q2",
                 next_quarter="2026-Q3", summary=ALGT_REAL), model="m")

    assert out.reason == "quoted_the_wrong_sentence"
