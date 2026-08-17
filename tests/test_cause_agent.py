"""Reading why the quarter came out where it did (T-003).

The problem these tests exist for is not a missing feature — it is a
recorded false statement. Of the 19 names the tribunal has argued, the 18
with data all had the same shape: a large EPS surprise beside a flat revenue
line (UNH +28.8% / +0.1%, LBRT +659.5% / +7.9%). That shape means either an
item that will not repeat or a margin the company earned, and the numbers do
not say which — so the roles filled the gap themselves. On PGY the record
reads "explicable by tax effects or revaluation gains", with nothing behind
it.

So the company's own sentence is read, and what a reading may claim is
fenced by the same four checks the guidance gate applies. Every case below
is one of those checks failing in a way the others would let through, plus
the two things that must remain true afterwards: no score moves, and an
absent reading says so out loud rather than reading as "there was nothing to
explain".
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from hawkeye.config import HawkeyeConfig
from hawkeye.contracts.stocks import CauseReading
from hawkeye.ledger.stocks import StockStore
from hawkeye.scout import cause_case, guidance_case
from hawkeye.scout.cause_agent import (
    CauseRequest,
    failure_kind,
    parse_reply,
    render_request,
)
from hawkeye.scout.quality import LegStatus, assess_earnings, describe_quality_en
from hawkeye.scout.scout import run_scout

from tests.test_earnings_quality import a_consensus, a_print
from tests.test_scout_quality_wiring import (
    FakeCalendar,
    _config,
    _entries,
    _feed,
    _provider,
)

CONFIG = HawkeyeConfig()

# A summary in the vendor's real shape: the result, the reason for it, the
# company's outlook, and the analysts' figure — four sentences that look
# alike and mean entirely different things.
SUMMARY = (
    "Test Corp reported second quarter earnings of $1.20 per share, "
    "beating consensus estimates by 20.00%. "
    "The company said the quarter included a one-time tax benefit of $0.30 "
    "per share related to the resolution of a prior-year audit. "
    "The company said it expects third quarter earnings of $0.90 to $1.00 "
    "per share. "
    "The current consensus earnings estimate is $0.95 per share for the "
    "quarter ending September 30, 2026.")

QUOTE = ("the quarter included a one-time tax benefit of $0.30 per share "
         "related to the resolution of a prior-year audit")

GOOD_REPLY = {"explained": True, "nature": "one_off", "magnitude": 0.30,
              "magnitude_unit": "per_share", "period": "2026-Q2",
              "quote": QUOTE}


def a_request(summary=SUMMARY, **kw) -> CauseRequest:
    base = dict(ticker="TEST", fiscal_quarter="2026-Q2", summary=summary)
    base.update(kw)
    return CauseRequest(**base)


# --- what the agent is shown, and what it is NOT shown ---------------------

def test_the_package_carries_the_whole_summary():
    """Cutting it down to "the explanation sentence" needs the reading this
    step exists to produce, and a wrong cut is invisible."""
    assert SUMMARY in render_request(a_request())


def test_the_package_never_shows_the_surprise_it_asks_about():
    """An extractor told "EPS beat by 20% while revenue was flat, why?" has
    been handed the premise that a reason exists — and a reason that was
    never in the source is the single failure this whole task is about."""
    text = render_request(a_request())

    for figure in ("20.0", "20%", "surprise", "consensus estimate is"):
        assert figure not in text.replace(SUMMARY, "")


# --- check 1: the quote has to exist ---------------------------------------

def test_a_quote_that_is_not_in_the_summary_voids_the_answer():
    out = parse_reply({**GOOD_REPLY,
                       "quote": "the quarter benefited from strong pricing"},
                      a_request())

    assert out.reading is None
    assert out.reason == "quote_not_in_source"


def test_a_quote_matches_across_reformatted_whitespace():
    """The summary arrives with line breaks and doubled spaces in it, and a
    reading discarded over an invisible character would teach us to loosen
    the check that actually matters."""
    messy = SUMMARY.replace("a one-time tax benefit",
                            "a one-time\n   tax   benefit")

    out = parse_reply(GOOD_REPLY, a_request(summary=messy))

    assert out.reading is not None


# --- check 2: it has to be the RIGHT sentence ------------------------------

def test_a_reason_lifted_out_of_the_outlook_sentence_is_refused():
    """"expects third quarter earnings of $0.90 to $1.00" explains a quarter
    that has not happened yet. Read as an explanation of the one just
    reported it would be a fact about the future presented as a cause."""
    out = parse_reply({**GOOD_REPLY, "nature": "operating", "magnitude": None,
                       "magnitude_unit": None,
                       "quote": "it expects third quarter earnings of $0.90 "
                                "to $1.00 per share"}, a_request())

    assert out.reading is None
    assert out.reason == "quoted_the_wrong_sentence"


def test_a_reason_lifted_out_of_the_consensus_sentence_is_refused():
    out = parse_reply({**GOOD_REPLY, "magnitude": 0.95,
                       "quote": "The current consensus earnings estimate is "
                                "$0.95 per share"}, a_request())

    assert out.reading is None
    assert out.reason == "quoted_the_wrong_sentence"


# --- check 3: a magnitude states its unit ----------------------------------

def test_a_number_without_a_unit_is_refused():
    """"0.30" beside an EPS figure is thirty cents; it is also a plausible
    thirty percent. Three roles would each read whichever fits."""
    out = parse_reply({**GOOD_REPLY, "magnitude_unit": None}, a_request())

    assert out.reading is None
    assert out.reason == "magnitude_unit_missing"


def test_a_unit_outside_the_four_is_refused():
    out = parse_reply({**GOOD_REPLY, "magnitude_unit": "dollars"}, a_request())

    assert out.reason == "magnitude_unit_missing"


def test_a_reason_with_no_number_at_all_is_a_perfectly_good_reading():
    """Plenty of companies name a cause without sizing it, and refusing those
    would describe our appetite for numbers rather than the company."""
    out = parse_reply({**GOOD_REPLY, "magnitude": None,
                       "magnitude_unit": None}, a_request())

    assert out.reading is not None
    assert out.reading.magnitude is None
    assert out.reading.magnitude_unit == ""


# --- check 4: the period is the quarter just reported ----------------------

def test_an_explanation_of_a_different_quarter_is_refused():
    """Summaries discuss the year-ago quarter in the same breath, and a
    year-ago one-off explains nothing about this print."""
    out = parse_reply({**GOOD_REPLY, "period": "2025-Q2"}, a_request())

    assert out.reading is None
    assert out.reason == "period_not_reported_quarter"


def test_an_unreadable_period_is_refused():
    out = parse_reply({**GOOD_REPLY, "period": "last quarter"}, a_request())

    assert out.reason == "period_unreadable"


# --- the accepted reading, and the one field nothing can check -------------

def test_an_accepted_reading_carries_the_quote_the_size_and_who_read_it():
    out = parse_reply(GOOD_REPLY, a_request(), model="test-model")

    assert out.reason == ""
    assert out.reading.source_excerpt == QUOTE
    assert out.reading.nature == "one_off"
    assert out.reading.magnitude == 0.30
    assert out.reading.magnitude_unit == "per_share"
    assert out.reading.period == "2026-Q2"
    assert out.reading.extractor == "agent"
    assert out.reading.extractor_model == "test-model"


def test_a_nature_outside_the_three_labels_reverts_to_unclear():
    """The label is the one field no check can confirm against the text. An
    answer outside the three is not thrown away — the quote is the evidence
    and it has already been verified — it reverts to the honest label."""
    out = parse_reply({**GOOD_REPLY, "nature": "definitely_good"}, a_request())

    assert out.reading is not None
    assert out.reading.nature == "unclear"


def test_a_summary_that_explains_nothing_is_a_normal_answer():
    out = parse_reply({"explained": False, "quote": ""}, a_request())

    assert out.reading is None
    assert out.reason == "no_cause_in_source"
    assert failure_kind(out.reason) == "absent_in_source"


@pytest.mark.parametrize("reason,kind", [
    ("no_cause_in_source", "absent_in_source"),
    ("no_summary_from_feed", "source_absent"),
    ("feed_not_asked", "source_absent"),
    ("quote_not_in_source", "reader_failed"),
    ("quoted_the_wrong_sentence", "reader_failed"),
    ("magnitude_unit_missing", "reader_failed"),
    ("period_unreadable", "reader_failed"),
    ("period_not_reported_quarter", "reader_failed"),
    ("extraction_call_failed", "call_failed"),
    ("pending_extraction", "not_yet_read"),
])
def test_every_refusal_is_classified(reason, kind):
    """A blank has several causes and only some are ours to fix. A report
    that counts blanks cannot tell them apart."""
    assert failure_kind(reason) == kind


def test_an_unclassified_refusal_raises_rather_than_passing_quietly():
    with pytest.raises(ValueError, match="unclassified"):
        failure_kind("something_new")


# --- what reaches the three legs, and what must not move -------------------

def a_cause(**kw) -> CauseReading:
    base = dict(nature="one_off", magnitude=0.30, magnitude_unit="per_share",
                period="2026-Q2", source_excerpt=QUOTE)
    base.update(kw)
    return CauseReading(**base)


def test_the_explanation_reaches_the_eps_and_revenue_legs():
    """The question it answers — is this beat a margin or an item that will
    not repeat — is a question about those two numbers, so it sits on them
    the way the guidance leg's condition sits on the guidance leg."""
    quality = assess_earnings(
        a_print(eps_actual=1.20, revenue_actual=1.0e9, cause=a_cause()),
        a_consensus(), CONFIG)

    assert quality.eps.excerpt == QUOTE
    assert quality.revenue.excerpt == QUOTE


def test_attaching_an_explanation_moves_no_score_and_no_status():
    """It is what the company SAID about the figures, never a correction to
    them. A reading that could move the ranking would be this step deciding
    what it was built to hand to the Judge."""
    without = assess_earnings(
        a_print(eps_actual=1.20, revenue_actual=1.0e9), a_consensus(), CONFIG)
    with_cause = assess_earnings(
        a_print(eps_actual=1.20, revenue_actual=1.0e9, cause=a_cause()),
        a_consensus(), CONFIG)

    assert with_cause.score == without.score
    assert with_cause.eps.status is without.eps.status
    assert with_cause.eps.surprise_pct == without.eps.surprise_pct
    assert with_cause.revenue.surprise_pct == without.revenue.surprise_pct
    assert with_cause.verdict is without.verdict


def test_the_tribunal_is_given_the_quote_and_told_what_it_is_worth():
    text = describe_quality_en(assess_earnings(
        a_print(eps_actual=1.20, revenue_actual=1.0e9, cause=a_cause()),
        a_consensus(), CONFIG))

    assert "the company's own explanation of this quarter" in text
    assert "0.3 (per_share)" in text
    # Once. It is one long sentence and it is about both legs at once, so
    # printing it on each of them and again in the NOTE would put it into the
    # dossier three times over.
    assert text.count(QUOTE) == 1
    # Verified to EXIST, and nothing further. The roles are entitled to
    # attack whether it is true or whether it covers the whole surprise.
    assert "nothing here checked whether it is true" in text


def test_a_guidance_condition_is_never_described_as_an_explanation():
    """Read as an explanation, a condition says the figure was qualified —
    a different and stronger claim than the company made."""
    from hawkeye.contracts.stocks import GuidanceReading

    text = describe_quality_en(assess_earnings(
        a_print(eps_actual=1.20, revenue_actual=1.0e9,
                guidance=GuidanceReading(period="FY2026", revenue_low=2.6e9,
                                         revenue_high=2.7e9,
                                         qualifier="excluding its barge business")),
        a_consensus(), CONFIG))

    assert "the company's own condition" in text
    assert ("excluding its barge business — the company's own explanation"
            not in text)


def test_an_unread_explanation_is_stated_as_unverified_not_as_silence():
    """Silence reads as "there was nothing to explain". That is the specific
    false impression the whole feature exists to remove, and the roles are
    told in the same breath what they may not do about it."""
    text = describe_quality_en(assess_earnings(
        a_print(eps_actual=1.20, revenue_actual=1.0e9,
                cause_reason="pending_extraction"),
        a_consensus(), CONFIG))

    assert "UNVERIFIED" in text
    assert "it has not been read yet" in text
    assert "do not argue a one-off, a tax effect or a margin improvement" in text


def test_a_summary_the_feed_never_supplied_is_not_a_company_that_explained_nothing():
    """A feed outage and a company that named no cause are different facts,
    and the drop record keeps whichever one is written here permanently."""
    text = describe_quality_en(assess_earnings(
        a_print(eps_actual=1.20, revenue_actual=1.0e9,
                cause_reason="no_summary_from_feed"),
        a_consensus(), CONFIG))

    assert "the vendor supplied no summary to read" in text


def test_both_roles_are_told_they_may_not_invent_a_cause():
    """Invariant 3: a prompt rule and the code enforcing it only mean
    something together. Here the code cannot overturn a sentence, so the
    instruction has to be explicit in both roles' text."""
    from hawkeye.tribunal.prompts import ADVERSARY_SYSTEM, BULL_SYSTEM

    for prompt in (BULL_SYSTEM, ADVERSARY_SYSTEM):
        assert "UNVERIFIED and must be treated as such" in prompt
    assert "Asserting a specific" in ADVERSARY_SYSTEM


# --- the session-mode queue ------------------------------------------------

def _scan(tmp_path, summary=SUMMARY):
    today = date.today()
    event_day = today - timedelta(days=3)
    store = StockStore(str(tmp_path / "hawkeye.db"))
    result = run_scout(FakeCalendar(_entries(event_day)), _provider(),
                       _config(), today=today, stock_store=store,
                       numbers_source=_feed(event_day, summary))
    return result, store


def test_the_scan_stages_the_same_sentence_for_a_second_question(tmp_path):
    """Two agents rather than one: an extractor with two jobs can satisfy the
    easier one and call it an answer, and each is checked against a different
    set of decoy sentences."""
    _scan(tmp_path)

    cases = cause_case.list_cases()
    assert len(cases) == 1
    assert cases[0].summary == SUMMARY
    assert cases[0].request().fiscal_quarter == "2026-Q2"
    assert len(guidance_case.list_cases()) == 1


def test_a_submitted_reading_reaches_the_print_row(tmp_path):
    _, store = _scan(tmp_path)
    case = cause_case.list_cases()[0]

    cause_case.attach(store, case,
                      parse_reply(GOOD_REPLY, case.request(), model="test-model"))

    row = store.active_print(store.stock_by_ticker("AMZN").id, "2026-Q2")
    assert row.cause is not None
    assert row.cause.source_excerpt == QUOTE
    assert row.cause.extractor_model == "test-model"
    assert row.cause_reason == ""


def test_either_queue_may_be_submitted_first(tmp_path):
    """Both queues revise the same row, so whichever submits second finds a
    different active row id. Pinning that id was always a proxy for "is this
    still the print the summary described" — and the proxy stopped holding
    the moment a second queue existed."""
    _, store = _scan(tmp_path)
    guidance = guidance_case.list_cases()[0]
    cause = cause_case.list_cases()[0]

    from hawkeye.scout.guidance_agent import parse_reply as parse_guidance
    assert cause_case.attach(
        store, cause, parse_reply(GOOD_REPLY, cause.request())) is not None
    assert guidance_case.attach(store, guidance, parse_guidance({
        "guided": True, "period": "2026-Q3",
        "eps_low": 0.90, "eps_high": 1.00,
        "quote": "it expects third quarter earnings of $0.90 to $1.00 per share",
    }, guidance.request())) is not None

    row = store.active_print(store.stock_by_ticker("AMZN").id, "2026-Q2")
    assert row.cause is not None          # the first reading survived
    assert row.guidance is not None       # and the second landed beside it


def test_a_restated_actual_still_refuses_the_reading(tmp_path):
    """The rule the id check was protecting has to keep holding: a vendor
    restatement means the summary this reading came from described a print
    that no longer stands."""
    from hawkeye.contracts.models import new_id, now

    _, store = _scan(tmp_path)
    case = cause_case.list_cases()[0]
    active = store.active_print(store.stock_by_ticker("AMZN").id, "2026-Q2")
    store.revise_print(active.model_copy(update={
        "id": new_id("ern"), "recorded_at": now(), "eps_actual": 9.99}))

    assert cause_case.attach(
        store, case, parse_reply(GOOD_REPLY, case.request())) is None


def test_the_reading_reaches_the_paragraph_the_tribunal_reads(tmp_path):
    """The end of the path this task exists for: from one sentence in the
    vendor's summary to the dossier three roles argue from."""
    from hawkeye.scout.scout import rerank_after_guidance

    result, store = _scan(tmp_path)
    case = cause_case.list_cases()[0]
    cause_case.attach(store, case, parse_reply(GOOD_REPLY, case.request()))
    rerank_after_guidance(store, result, _config())

    description = result.passed[0].brief.catalyst.description
    assert QUOTE in description
    assert result.passed[0].quality.eps.status is LegStatus.BEAT
