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
