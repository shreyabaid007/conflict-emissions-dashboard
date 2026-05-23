"""Runtime settings loaded from environment variables.

A tiny wrapper so callers never read ``os.environ`` directly and so tests can
inject overrides via the ``Settings`` constructor without touching the
process environment.

Environment variables (prefix ``WCED_``):
  WCED_ANTHROPIC_API_KEY         — secret key for the Anthropic API
  WCED_ANTHROPIC_DEFAULT_MODEL   — model id, defaults to claude-opus-4-7
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class _SecretStr:
    """Minimal SecretStr stand-in so the value is never printed accidentally."""

    _value: str = field(repr=False)

    def get_secret_value(self) -> str:
        return self._value

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "SecretStr('***')"

    def __bool__(self) -> bool:
        return bool(self._value)


@dataclass(frozen=True)
class Settings:
    """Application-wide settings."""

    anthropic_api_key: _SecretStr = field(default_factory=lambda: _SecretStr(""))
    anthropic_default_model: str = "claude-opus-4-7"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            anthropic_api_key=_SecretStr(os.environ.get("WCED_ANTHROPIC_API_KEY", "")),
            anthropic_default_model=os.environ.get(
                "WCED_ANTHROPIC_DEFAULT_MODEL", "claude-opus-4-7"
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance built from the current environment."""
    return Settings.from_env()
