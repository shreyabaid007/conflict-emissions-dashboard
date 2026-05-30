"""Tests for wced.ai.claude_client."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, Field

from wced.ai.claude_client import AnthropicClient, ClaudeCallError
from wced.models.provenance import SourceType
from wced.settings import Settings, _SecretStr


def _make_settings(key: str = "test-key", **kwargs: Any) -> Settings:
    defaults: dict[str, Any] = {
        "anthropic_api_key": _SecretStr(key),
        "anthropic_default_model": "claude-opus-4-7",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


def _text_response(text: str, input_tokens: int = 10, output_tokens: int = 5) -> SimpleNamespace:
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _tool_response(tool_input: dict, name: str = "record_structured_output") -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name=name, input=tool_input)
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(input_tokens=20, output_tokens=8),
    )


class Verdict(BaseModel):
    label: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=1.0)


class TestTextCall:
    def test_returns_text_and_records_source(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _text_response("hello world")
        client = AnthropicClient(settings=_make_settings(), client=sdk)

        result = client.call("ping")

        assert result == "hello world"
        # SDK called once with our messages.
        sdk.messages.create.assert_called_once()
        kwargs = sdk.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-opus-4-7"
        assert kwargs["messages"][0]["role"] == "user"
        assert kwargs["messages"][0]["content"][0]["text"] == "ping"
        # Source is DERIVED and includes the model + prompt hash in identifier.
        src = client.last_source
        assert src is not None
        assert src.source_type is SourceType.DERIVED
        assert src.identifier.startswith("claude:anthropic:claude-opus-4-7:")
        assert src.metadata["input_tokens"] == 10
        assert src.metadata["output_tokens"] == 5
        assert src.metadata["response_model"] is None


class TestStructuredCall:
    def test_parses_tool_use_into_pydantic_model(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_response(
            {"label": "CONFIRMED_FIRE", "score": 0.9}
        )
        client = AnthropicClient(settings=_make_settings(), client=sdk)

        result = client.call("classify this", response_model=Verdict)

        assert isinstance(result, Verdict)
        assert result.label == "CONFIRMED_FIRE"
        assert result.score == pytest.approx(0.9)
        # Tool config is set up with the right name.
        kwargs = sdk.messages.create.call_args.kwargs
        assert kwargs["tools"][0]["name"] == "record_structured_output"
        assert kwargs["tool_choice"]["name"] == "record_structured_output"

    def test_raises_when_tool_block_missing(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _text_response("no tool call here")
        client = AnthropicClient(settings=_make_settings(), client=sdk)

        with pytest.raises(ClaudeCallError, match="tool_use"):
            client.call("classify", response_model=Verdict)

    def test_raises_on_validation_failure(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_response({"label": "", "score": 9.0})
        client = AnthropicClient(settings=_make_settings(), client=sdk)

        with pytest.raises(ClaudeCallError, match="validation"):
            client.call("classify", response_model=Verdict)


class TestErrors:
    def test_wraps_sdk_exception(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = RuntimeError("boom")
        client = AnthropicClient(settings=_make_settings(), client=sdk)

        with pytest.raises(ClaudeCallError, match="boom"):
            client.call("ping")


class TestOpenRouterProvider:
    def test_openrouter_client_construction(self) -> None:
        """OpenRouter config sets base_url, api_key, and default headers."""
        settings = _make_settings(
            key="",
            anthropic_api_key=_SecretStr(""),
            ai_provider="openrouter",
            openrouter_api_key=_SecretStr("sk-or-test-key"),
        )
        with patch("wced.ai.claude_client.Anthropic") as MockAnthropic:
            MockAnthropic.return_value = MagicMock()
            client = AnthropicClient(settings=settings)

            MockAnthropic.assert_called_once_with(
                api_key="sk-or-test-key",
                base_url="https://openrouter.ai/api/v1",
                default_headers={
                    "HTTP-Referer": "https://wced.org",
                    "X-Title": "War Carbon Emissions Dashboard",
                },
            )
            assert client._provider == "openrouter"

    def test_openrouter_model_resolution(self) -> None:
        """Model strings without a provider prefix get ``anthropic/`` prepended."""
        sdk = MagicMock()
        sdk.messages.create.return_value = _text_response("ok")
        settings = _make_settings(
            ai_provider="openrouter",
            openrouter_api_key=_SecretStr("sk-or-key"),
        )
        client = AnthropicClient(settings=settings, client=sdk)

        client.call("ping", model="claude-opus-4-7")

        kwargs = sdk.messages.create.call_args.kwargs
        assert kwargs["model"] == "anthropic/claude-opus-4-7"

    def test_openrouter_model_already_prefixed(self) -> None:
        """Model strings that already contain ``/`` are passed through."""
        sdk = MagicMock()
        sdk.messages.create.return_value = _text_response("ok")
        settings = _make_settings(
            ai_provider="openrouter",
            openrouter_api_key=_SecretStr("sk-or-key"),
        )
        client = AnthropicClient(settings=settings, client=sdk)

        client.call("ping", model="anthropic/claude-sonnet-4")

        kwargs = sdk.messages.create.call_args.kwargs
        assert kwargs["model"] == "anthropic/claude-sonnet-4"

    def test_openrouter_provenance_identifier(self) -> None:
        """Provenance identifier includes ``openrouter`` as the provider."""
        sdk = MagicMock()
        sdk.messages.create.return_value = _text_response("ok")
        settings = _make_settings(
            ai_provider="openrouter",
            openrouter_api_key=_SecretStr("sk-or-key"),
        )
        client = AnthropicClient(settings=settings, client=sdk)
        client.call("ping")

        src = client.last_source
        assert src is not None
        assert src.identifier.startswith("claude:openrouter:anthropic/claude-opus-4-7:")

    def test_fallback_to_openrouter_when_no_anthropic_key(self) -> None:
        """When ai_provider is 'anthropic' but only openrouter key exists, fall back."""
        settings = _make_settings(
            key="",
            anthropic_api_key=_SecretStr(""),
            ai_provider="anthropic",
            openrouter_api_key=_SecretStr("sk-or-fallback"),
        )
        sdk = MagicMock()
        client = AnthropicClient(settings=settings, client=sdk)
        assert client._provider == "openrouter"

    def test_custom_base_url_override(self) -> None:
        """WCED_AI_BASE_URL overrides the default OpenRouter URL."""
        settings = _make_settings(
            key="",
            anthropic_api_key=_SecretStr(""),
            ai_provider="openrouter",
            openrouter_api_key=_SecretStr("sk-or-key"),
            ai_base_url="https://custom-proxy.example.com/v1",
        )
        with patch("wced.ai.claude_client.Anthropic") as MockAnthropic:
            MockAnthropic.return_value = MagicMock()
            AnthropicClient(settings=settings)

            call_kwargs = MockAnthropic.call_args.kwargs
            assert call_kwargs["base_url"] == "https://custom-proxy.example.com/v1"

    def test_openrouter_missing_key_raises(self) -> None:
        """Requesting openrouter without a key raises a clear error."""
        settings = _make_settings(
            key="",
            anthropic_api_key=_SecretStr(""),
            ai_provider="openrouter",
            openrouter_api_key=_SecretStr(""),
        )
        with patch("wced.ai.claude_client.Anthropic") as MockAnthropic:
            with pytest.raises(ClaudeCallError, match="OpenRouter API key"):
                AnthropicClient(settings=settings)
