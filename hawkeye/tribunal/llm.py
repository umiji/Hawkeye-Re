"""LLM client abstraction.

The tribunal only sees ``complete_json(system, user, schema)``. The Anthropic
implementation uses structured outputs so every agent reply is schema-valid
JSON; ScriptedLLM replays canned responses for tests and offline dry runs.
"""
from __future__ import annotations

import json
import os
from typing import Protocol, runtime_checkable


class LLMError(RuntimeError):
    pass


@runtime_checkable
class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, schema: dict,
                      max_tokens: int = 16000) -> dict:
        ...


class AnthropicLLM:
    def __init__(self, model: str | None = None):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMError(
                "anthropic SDK not installed — run: pip install 'hawkeye[llm]'"
            ) from exc
        # Anthropic() resolves credentials from ANTHROPIC_API_KEY,
        # ANTHROPIC_AUTH_TOKEN, or an `ant auth login` profile.
        self._client = anthropic.Anthropic()
        self.model = model or os.environ.get("HAWKEYE_MODEL", "claude-opus-4-8")

    def complete_json(self, system: str, user: str, schema: dict,
                      max_tokens: int = 16000) -> dict:
        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
        )
        if response.stop_reason == "refusal":
            raise LLMError("model declined the request (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise LLMError("response truncated at max_tokens — retry with a higher limit")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError("no text block in model response")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"model returned invalid JSON: {exc}") from exc


class ScriptedLLM:
    """Returns queued responses in order — for tests and dry runs."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str, schema: dict,
                      max_tokens: int = 16000) -> dict:
        self.calls.append((system, user))
        if not self._responses:
            raise LLMError("ScriptedLLM ran out of responses")
        return self._responses.pop(0)
