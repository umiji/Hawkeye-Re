"""Fetching the company's release, and asking a cheap reader to cut it (T-008).

Two collaborators sit between the feed and the cause reader, and each has one
way of failing that must not look like the other:

- the release may not arrive (a fact about the feed), and
- the extractor may not answer (a fact about us),

and neither may ever render as "this company explained nothing" (invariant 6).

Nothing here talks to the network. The release endpoint is answered by an
httpx MockTransport and the extractor by a scripted stand-in, which is the
same arrangement the rest of the suite uses for the feed.
"""
from __future__ import annotations

import httpx
import pytest

from hawkeye.marketdata.gemini import (
    GeminiExtractor,
    GeminiUnavailable,
    parse_blocks,
)
from hawkeye.marketdata.whispers import WhispersSource, WhispersUnavailable

ARTICLE_HTML = (
    "<p>Revenue grew 76% to $43.2 million.</p>"
    "<p>Revenue growth&nbsp;reflects stronger-than-expected performance "
    "in the Drones segment.</p>"
    "<script>var tracking = 1;</script>"
    "<table><tr><td>Total assets</td><td>1,234</td></tr></table>")


def _ew(handler) -> WhispersSource:
    return WhispersSource(transport=httpx.MockTransport(handler),
                          sleep=lambda _: None)


# --- the release ------------------------------------------------------------

def test_the_release_arrives_as_text_not_markup():
    """The reader is shown prose. Markup left in it is text the quote check
    would have to match, and no company ever wrote `&nbsp;`."""
    def handler(request):
        assert request.url.path == "/api/newsarticle/AIRO/2608137728"
        return httpx.Response(200, json={"subject": "AIRO Reports Q2",
                                         "article": ARTICLE_HTML})

    text = _ew(handler).article("AIRO", "2608137728")
    assert "Revenue grew 76% to $43.2 million." in text
    assert "<p>" not in text and "&nbsp;" not in text
    assert "var tracking" not in text          # scripts are not prose


def test_a_print_the_feed_names_no_article_for_reads_as_empty_not_missing():
    """204 is the feed saying "no article here", which is a fact about this
    print. It is not the same as the request failing."""
    def handler(request):
        return httpx.Response(204)

    assert _ew(handler).article("AIRO", "2608137728") == ""


def test_a_refused_release_raises_rather_than_returning_nothing():
    """An empty string here would be read downstream as "the company
    published no release", permanently."""
    def handler(request):
        return httpx.Response(500)

    with pytest.raises(WhispersUnavailable):
        _ew(handler).article("AIRO", "2608137728")


def test_the_article_id_travels_on_the_record():
    """It is how the release is addressed, and the feed states it in the same
    response as the figures — thrown away, there is no second way to find
    the release for this print."""
    from hawkeye.marketdata.whispers import parse_details

    record = parse_details({"ticker": "AIRO", "fileName": "2608137728",
                            "eps": 0.12, "summary": "AIRO reported..."})
    assert record.file_name == "2608137728"


def test_a_record_with_no_article_id_names_the_gap():
    from hawkeye.marketdata.whispers import parse_details

    record = parse_details({"ticker": "AIRO", "eps": 0.12, "summary": "x"})
    assert record.file_name == ""
    assert "article_id_missing" in record.gaps


# --- the space a stripped tag leaves behind (T-012) --------------------------

def test_a_stripped_tag_leaves_no_space_in_front_of_punctuation():
    """The bold figure and the comma after it are separated by a tag, and the
    tag becomes a space. What comes out has to be what the company wrote.

    Left in, our own text stops matching itself: the reader copies the natural
    `million, compared` and the quote check refuses it as absent from the
    release. Measured cost on 30 names: 3,320 characters of real explanation
    discarded, and SDRL accused of inventing every block it returned.
    """
    from hawkeye.marketdata.whispers import release_text

    text = release_text(
        "<p>Total operating expenses increased by <b>$43 million</b> to "
        "<b>$377 million</b>, compared to <b>$334 million</b>.</p>"
        "<p>Fewer operating days for the <i>West Tellus</i>; margin held.</p>")
    assert "$377 million, compared to $334 million." in text
    assert "West Tellus; margin held." in text
    assert " ," not in text and " ." not in text and " ;" not in text


def test_the_repair_leaves_alone_every_space_a_company_did_write():
    """Six characters, and no more. Each of these is text that a wider rule
    would corrupt — measured on real releases 2026-08-18, which is why `%`,
    `’` and `)` are not in the class.
    """
    from hawkeye.marketdata.whispers import release_text

    kept = [
        "2025 % Change",              # a column heading, not a percentage
        "fiscal ’27 outlook",    # 2027 abbreviated, not an apostrophe-s
        "( www.seadrill.com )",       # lopsided if only one side is closed
        "Adjusted EBITDA (non-GAAP)",
        "revenue — up 13%",
        'the chief executive said "we grew',
    ]
    for phrase in kept:
        assert phrase in release_text(f"<p>{phrase}</p>"), phrase


def test_a_newline_before_punctuation_is_left_where_it_is():
    """A block end becomes a newline, and welding two table rows together
    would invent a sentence the company never laid out. None of the six
    characters follows a newline in the measured sample, so nothing is lost
    by refusing to cross one."""
    from hawkeye.marketdata.whispers import release_text

    text = release_text("<tr><td>Net income</td></tr><tr><td>.</td></tr>")
    assert "Net income\n." in text
    assert "Net income." not in text      # the row boundary survives


# --- the extractor ----------------------------------------------------------

def _gemini(handler, **kw) -> GeminiExtractor:
    return GeminiExtractor(api_key="test-key", sleep=lambda _: None,
                           transport=httpx.MockTransport(handler), **kw)


def _reply(blocks) -> dict:
    import json
    return {"candidates": [{"content": {"parts": [
        {"text": json.dumps({"passages": blocks})}]}}]}


def test_the_blocks_come_back_in_the_order_they_were_given():
    def handler(request):
        return httpx.Response(200, json=_reply(["first", "second"]))

    assert _gemini(handler).blocks("release text", "AIRO", "2026-Q2") == [
        "first", "second"]


def test_the_release_is_the_only_thing_sent():
    """Never the surprise, never the ledger, never a holding. The reader is
    told a reason exists the moment it sees how far the print cleared
    consensus, and inventing one is the failure this task is about."""
    seen = {}

    def handler(request):
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_reply([]))

    _gemini(handler).blocks("RELEASE BODY", "AIRO", "2026-Q2")
    sent = str(seen)
    assert "RELEASE BODY" in sent
    for forbidden in ("surprise", "consensus estimate was", "nav", "position"):
        assert forbidden not in sent.lower()


def test_a_throttled_extractor_waits_the_delay_it_was_given():
    """Measured 2026-08-17: five requests in sixteen seconds is a 429, and
    the following eight all succeed once the stated delay is honoured."""
    waits, calls = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                 "retryDelay": "7s"}]}})
        return httpx.Response(200, json=_reply(["ok"]))

    extractor = GeminiExtractor(api_key="k", sleep=waits.append,
                                transport=httpx.MockTransport(handler))
    assert extractor.blocks("release", "AIRO", "2026-Q2") == ["ok"]
    assert waits and waits[0] >= 7


def test_an_extractor_that_keeps_refusing_raises_rather_than_reads_empty():
    def handler(request):
        return httpx.Response(429, json={"error": {}})

    with pytest.raises(GeminiUnavailable):
        _gemini(handler).blocks("release", "AIRO", "2026-Q2")


def test_no_key_is_a_refusal_and_not_an_empty_answer():
    """A missing key must not read as "the release explains nothing"."""
    with pytest.raises(GeminiUnavailable):
        GeminiExtractor(api_key="").blocks("release", "AIRO", "2026-Q2")


@pytest.mark.parametrize("payload", [
    {"candidates": []},
    {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]},
    {"candidates": [{"content": {"parts": [{"text": '{"passages": "x"}'}]}}]},
])
def test_an_unreadable_reply_raises_rather_than_yielding_no_blocks(payload):
    assert parse_blocks(_reply(["a"])) == ["a"]
    with pytest.raises(GeminiUnavailable):
        parse_blocks(payload)


# --- T-011: the model, the pacing, and saying what went wrong ---------------

def test_the_model_is_told_not_to_think_in_the_dialect_it_understands():
    """The 3.x models replaced `thinkingBudget` (a token count) with
    `thinkingLevel` (a named step), and sending the old field is a flat 400.

    Measured 2026-08-18 against `gemini-3.5-flash-lite`: `thinkingBudget: 0`
    → `400 INVALID_ARGUMENT`; `thinkingLevel: "minimal"` → 200 with
    `thoughtsTokenCount: 0`. The setting is not cosmetic — a model reasoning
    its way toward a better sentence is a model composing one, and composing
    is the failure this whole path exists to prevent.
    """
    import json
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=_reply([]))

    _gemini(handler).blocks("release", "AIRO", "2026-Q2")
    thinking = seen["generationConfig"]["thinkingConfig"]
    assert thinking == {"thinkingLevel": "minimal"}
    assert "thinkingBudget" not in json.dumps(seen)


def test_calls_are_spaced_to_stay_inside_the_per_minute_allowance():
    """15 requests per minute is the free-tier ceiling for this model, and a
    release takes 1.6-4.9s to answer — so back-to-back calls would run at up
    to 37/minute and draw a 429 the run then has to sit out.

    The wait is imposed here rather than in the scan because this class is
    what knows the model's limits.
    """
    clock = [1000.0]
    waits = []

    def handler(request):
        return httpx.Response(200, json=_reply(["ok"]))

    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds

    extractor = GeminiExtractor(
        api_key="k", sleep=sleep, clock=lambda: clock[0],
        transport=httpx.MockTransport(handler))

    extractor.blocks("release", "AIRO", "2026-Q2")
    assert waits == []                       # nothing to wait for on the first
    clock[0] += 0.5                          # the reply came back fast
    extractor.blocks("release", "BFRI", "2026-Q2")
    assert waits and waits[0] == pytest.approx(3.5)   # 4.0s spacing, 0.5 spent


def test_a_call_that_took_longer_than_the_spacing_waits_for_nothing():
    """The pause is a floor on the interval, not a tax on every call. STAA's
    release is 45,954 characters and takes longer than the spacing on its
    own."""
    clock = [1000.0]
    waits = []

    def handler(request):
        return httpx.Response(200, json=_reply(["ok"]))

    extractor = GeminiExtractor(
        api_key="k", sleep=waits.append, clock=lambda: clock[0],
        transport=httpx.MockTransport(handler))
    extractor.blocks("release", "STAA", "2026-Q2")
    clock[0] += 30.0
    extractor.blocks("release", "STIM", "2026-Q2")
    assert waits == []


def test_a_refusal_repeats_what_the_server_actually_said():
    """Without this the only record is `the extractor answered 400`, and the
    reason is gone.

    Measured twice: on 2026-08-17 a 429's body named the daily quota and the
    bare reason hid it, costing a session to two wrong conclusions about the
    limit. On 2026-08-18 a 400's body named `thinkingBudget` and the same
    blank sent the diagnosis to a separate throwaway script.
    """
    def handler(request):
        return httpx.Response(400, json={"error": {
            "status": "INVALID_ARGUMENT",
            "message": "Invalid value at 'generation_config.thinking_config'"}})

    with pytest.raises(GeminiUnavailable) as caught:
        _gemini(handler).blocks("release", "AIRO", "2026-Q2")
    assert "generation_config.thinking_config" in str(caught.value)


def test_a_daily_quota_is_reported_at_once_instead_of_retried():
    """The server asks for a 23-second retry on a quota that resets in a day.

    Measured 2026-08-18: a 429 for `GenerateRequestsPerDayPerProjectPerModel-
    FreeTier` carries `retryDelay: 23s`. Honouring it is how three names spent
    ~465 seconds each on 2026-08-17 re-asking a question already answered for
    the day. The daily quota is named in the body, so it is knowable, and the
    run should say so and move on.
    """
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={"error": {
            "message": "Quota exceeded ... limit: 20, model: gemini-2.5-flash",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId":
                                 "GenerateRequestsPerDayPerProjectPerModel-"
                                 "FreeTier", "quotaValue": "20"}]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                 "retryDelay": "23s"}]}})

    waits = []
    extractor = GeminiExtractor(api_key="k", sleep=waits.append,
                                transport=httpx.MockTransport(handler))
    with pytest.raises(GeminiUnavailable) as caught:
        extractor.blocks("release", "AIRO", "2026-Q2")
    assert len(calls) == 1                    # asked once, not four times
    assert waits == []
    assert "daily" in str(caught.value).lower()


def test_a_momentary_429_is_still_retried():
    """The per-minute ceiling is a different event from the daily one and
    clearing it IS just a matter of waiting."""
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {
                "message": "Quota exceeded for ... requests per minute",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                     "violations": [{"quotaId":
                                     "GenerateRequestsPerMinutePerProject"}]},
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                     "retryDelay": "7s"}]}})
        return httpx.Response(200, json=_reply(["ok"]))

    assert _gemini(handler).blocks("release", "AIRO", "2026-Q2") == ["ok"]
    assert len(calls) == 2
