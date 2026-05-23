"""Anthropic Claude client wrapper with provenance and call logging.

Every call through ``AnthropicClient.call`` is metered (prompt hash, model,
tokens, latency, response hash) and emits a ``Source`` record of type
``DERIVED`` so that any downstream emission estimate fed by an LLM output can
be traced back to the specific prompt/model combination that produced it.

When ``response_model`` is supplied, the call uses Anthropic tool-use with a
single tool whose ``input_schema`` is the Pydantic model's JSON Schema; the
response is then parsed via ``response_model.model_validate(tool_input)``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any, TypeVar, overload

from pydantic import BaseModel

from wced.models.provenance import Source, SourceType
from wced.settings import Settings, get_settings

try:  # pragma: no cover - import-time only
    from anthropic import Anthropic
except ImportError:  # pragma: no cover
    Anthropic = None  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_STRUCTURED_TOOL_NAME = "record_structured_output"


class ClaudeCallError(RuntimeError):
    """Raised when a Claude call fails or returns an unparseable response."""


def _sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _strip_schema_keys(schema: Any) -> Any:
    """Remove JSON Schema keys that the Anthropic tool-use validator rejects.

    Pydantic emits ``$defs``/``title``/``$ref`` blocks; the tool input_schema
    only needs ``type``/``properties``/``required``/``items``. We recursively
    drop unused metadata fields while preserving the structural ones.
    """
    if isinstance(schema, dict):
        out = {
            k: _strip_schema_keys(v)
            for k, v in schema.items()
            if k not in {"title", "$defs", "definitions"}
        }
        return out
    if isinstance(schema, list):
        return [_strip_schema_keys(v) for v in schema]
    return schema


class AnthropicClient:
    """Thin wrapper around ``anthropic.Anthropic`` that adds provenance.

    Parameters
    ----------
    settings : Settings or None
        Override the global settings object. Defaults to ``get_settings()``.
    client : anthropic.Anthropic or None
        Pre-built SDK client (used by tests to inject a stub).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Anthropic | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        if client is not None:
            self._client = client
        else:
            if Anthropic is None:
                raise ClaudeCallError(
                    "anthropic package is not installed; "
                    "install it or inject a stub client."
                )
            api_key = self._settings.anthropic_api_key.get_secret_value()
            if not api_key:
                raise ClaudeCallError(
                    "Anthropic API key is not configured "
                    "(set WCED_ANTHROPIC_API_KEY)."
                )
            self._client = Anthropic(api_key=api_key)
        self._last_source: Source | None = None

    # ------------------------------------------------------------------ public

    @property
    def last_source(self) -> Source | None:
        """The provenance ``Source`` produced by the most recent call (or None)."""
        return self._last_source

    @overload
    def call(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        model: str = ...,
        temperature: float = ...,
        max_tokens: int = ...,
        response_model: type[T],
        system: str | None = ...,
    ) -> T: ...

    @overload
    def call(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        model: str = ...,
        temperature: float = ...,
        max_tokens: int = ...,
        response_model: None = ...,
        system: str | None = ...,
    ) -> str: ...

    def call(
        self,
        prompt: str | list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        response_model: type[T] | None = None,
        system: str | None = None,
    ) -> T | str:
        """Invoke Claude and return a parsed model or raw text.

        Parameters
        ----------
        prompt : str or list of content blocks
            User-message content. A bare string is wrapped into a single text
            block; a list is passed through unchanged (used for image+text
            multimodal prompts).
        model : str
            Model identifier. Defaults to the setting's default.
        temperature : float
            Sampling temperature. Defaults to 0 (deterministic).
        max_tokens : int
            Output cap.
        response_model : Pydantic BaseModel subclass or None
            When given, the response is forced through a tool call whose input
            schema is the model's JSON Schema and the tool input is parsed via
            ``response_model.model_validate``.
        system : str or None
            Optional system prompt.

        Returns
        -------
        T or str
            A ``response_model`` instance when one was supplied, otherwise the
            assistant's concatenated text content.
        """
        model_id = model or self._settings.anthropic_default_model
        messages = self._build_messages(prompt)
        prompt_hash = _sha256_hex(json.dumps(messages, sort_keys=True, default=str))

        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        if system is not None:
            kwargs["system"] = system

        if response_model is not None:
            schema = _strip_schema_keys(response_model.model_json_schema())
            kwargs["tools"] = [
                {
                    "name": _STRUCTURED_TOOL_NAME,
                    "description": (
                        f"Record a structured {response_model.__name__} result. "
                        "Always call this tool exactly once."
                    ),
                    "input_schema": schema,
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": _STRUCTURED_TOOL_NAME}

        t0 = time.perf_counter()
        try:
            response = self._client.messages.create(**kwargs)
        except Exception as exc:
            log.exception("anthropic.call failed model=%s prompt_hash=%s", model_id, prompt_hash[:12])
            raise ClaudeCallError(f"Anthropic API call failed: {exc}") from exc
        latency_ms = (time.perf_counter() - t0) * 1000.0

        result, response_text = self._parse_response(response, response_model)
        response_hash = _sha256_hex(response_text)

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
        output_tokens = getattr(usage, "output_tokens", 0) if usage else 0

        log.info(
            "claude.call model=%s prompt_hash=%s response_hash=%s "
            "input_tokens=%d output_tokens=%d latency_ms=%.1f structured=%s",
            model_id,
            prompt_hash[:12],
            response_hash[:12],
            input_tokens,
            output_tokens,
            latency_ms,
            response_model is not None,
        )

        self._last_source = Source(
            source_type=SourceType.DERIVED,
            identifier=f"claude:{model_id}:{prompt_hash}",
            retrieved_at=datetime.now(tz=UTC),
            retrieved_by="wced.ai.claude_client",
            content_hash=response_hash,
            metadata={
                "model": model_id,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
                "response_model": (
                    response_model.__name__ if response_model is not None else None
                ),
            },
        )
        return result

    # ----------------------------------------------------------------- private

    @staticmethod
    def _build_messages(prompt: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
        if isinstance(prompt, str):
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        else:
            content = prompt
        return [{"role": "user", "content": content}]

    @staticmethod
    def _parse_response(
        response: Any,
        response_model: type[T] | None,
    ) -> tuple[T | str, str]:
        blocks = list(getattr(response, "content", []) or [])

        if response_model is not None:
            for block in blocks:
                if getattr(block, "type", None) == "tool_use" and getattr(
                    block, "name", None
                ) == _STRUCTURED_TOOL_NAME:
                    tool_input = getattr(block, "input", None) or {}
                    try:
                        parsed = response_model.model_validate(tool_input)
                    except Exception as exc:
                        raise ClaudeCallError(
                            f"Structured response failed validation against "
                            f"{response_model.__name__}: {exc}"
                        ) from exc
                    return parsed, json.dumps(tool_input, sort_keys=True, default=str)
            raise ClaudeCallError(
                f"Expected a tool_use block named {_STRUCTURED_TOOL_NAME!r} "
                "but none was present in the response."
            )

        texts: list[str] = []
        for block in blocks:
            if getattr(block, "type", None) == "text":
                texts.append(getattr(block, "text", "") or "")
        joined = "".join(texts)
        return joined, joined
