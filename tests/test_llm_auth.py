"""Credential resolution for AnthropicLLM — fully offline.

Verifies that CLAUDE_CODE_OAUTH_TOKEN (Claude Code subscription token) is
mapped to Bearer auth + the oauth beta header, and that precedence and the
no-credentials error message are correct.
"""
from __future__ import annotations

import pytest

from hawkeye.tribunal.llm import AnthropicLLM, LLMError, resolve_client_kwargs

OAT = "sk-ant-oat01-test-token"


def test_api_key_takes_precedence_and_defers_to_sdk():
    env = {"ANTHROPIC_API_KEY": "sk-ant-api03-x", "CLAUDE_CODE_OAUTH_TOKEN": OAT}
    assert resolve_client_kwargs(env) == {}


def test_claude_code_oauth_token_uses_bearer_and_beta_header():
    kwargs = resolve_client_kwargs({"CLAUDE_CODE_OAUTH_TOKEN": OAT})
    assert kwargs["auth_token"] == OAT
    assert kwargs["default_headers"] == {"anthropic-beta": "oauth-2025-04-20"}


def test_auth_token_env_precedes_claude_code_token():
    env = {"ANTHROPIC_AUTH_TOKEN": "other-bearer", "CLAUDE_CODE_OAUTH_TOKEN": OAT}
    assert resolve_client_kwargs(env)["auth_token"] == "other-bearer"


def test_non_oauth_bearer_gets_no_beta_header():
    kwargs = resolve_client_kwargs({"ANTHROPIC_AUTH_TOKEN": "gateway-bearer"})
    assert kwargs == {"auth_token": "gateway-bearer"}


def test_oauth_shaped_auth_token_gets_beta_header():
    kwargs = resolve_client_kwargs({"ANTHROPIC_AUTH_TOKEN": OAT})
    assert kwargs["default_headers"] == {"anthropic-beta": "oauth-2025-04-20"}


def test_empty_env_defers_to_sdk_chain():
    assert resolve_client_kwargs({}) == {}


def test_client_constructed_from_claude_code_token(monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAT)
    llm = AnthropicLLM(model="claude-opus-4-8")
    assert llm._client.auth_token == OAT
    assert llm.model == "claude-opus-4-8"


def test_missing_credentials_raise_helpful_error(monkeypatch):
    anthropic = pytest.importorskip("anthropic")

    def no_credentials(**kwargs):
        raise TypeError("Could not resolve authentication method")

    monkeypatch.setattr(anthropic, "Anthropic", no_credentials)
    with pytest.raises(LLMError) as excinfo:
        AnthropicLLM()
    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(excinfo.value)
    assert "claude setup-token" in str(excinfo.value)
