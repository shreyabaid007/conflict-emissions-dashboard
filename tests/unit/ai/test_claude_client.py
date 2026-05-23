"""Tests for wced.ai.claude_client."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, Field

from wced.ai.claude_client import AnthropicClient, ClaudeCallError
from wced.models.provenance import SourceType
from wced.settings import Settings, _SecretStr


def _make_settings(key: str = "test-key") -> Settings:
    return Settings(
        anthropic_api_key=_SecretStr(key),
        anthropic_default_model="claude-opus-4-7",
    )


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
        assert src.identifier.startswith("claude:claude-opus-4-7:")
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
