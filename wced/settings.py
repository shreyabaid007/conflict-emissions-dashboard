"""Runtime settings loaded from environment variables.

A tiny wrapper so callers never read ``os.environ`` directly and so tests can
inject overrides via the ``Settings`` constructor without touching the
process environment.

Environment variables (prefix ``WCED_``):
  WCED_ANTHROPIC_API_KEY         — secret key for the Anthropic API
  WCED_ANTHROPIC_DEFAULT_MODEL   — model id, defaults to claude-opus-4-7
  WCED_AI_PROVIDER               — "anthropic" (default) or "openrouter"
  WCED_OPENROUTER_API_KEY        — secret key for OpenRouter (optional)
  WCED_AI_BASE_URL               — custom base URL override (optional)
  WCED_ENABLE_ACLED              — "1"/"true"/"yes" to enable ACLED connector
                                   (default: off; retained for future funded access)
  WCED_CROSS_METHOD_TOLERANCE    — max acceptable bottom-up/top-down agreement
                                   ratio for auto-publish (default: 2.0). An
                                   event auto-publishes only when its FRP and
                                   inventory estimates satisfy
                                   1/tol ≤ ρ ≤ tol (methodology §3.5).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal


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
    ai_provider: Literal["anthropic", "openrouter"] = "anthropic"
    openrouter_api_key: _SecretStr = field(default_factory=lambda: _SecretStr(""))
    ai_base_url: str | None = None
    enable_acled: bool = False
    cross_method_tolerance: float = 2.0

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            anthropic_api_key=_SecretStr(os.environ.get("WCED_ANTHROPIC_API_KEY", "")),
            anthropic_default_model=os.environ.get(
                "WCED_ANTHROPIC_DEFAULT_MODEL", "claude-opus-4-7"
            ),
            ai_provider=os.environ.get("WCED_AI_PROVIDER", "anthropic"),  # type: ignore[arg-type]
            openrouter_api_key=_SecretStr(os.environ.get("WCED_OPENROUTER_API_KEY", "")),
            ai_base_url=os.environ.get("WCED_AI_BASE_URL") or None,
            enable_acled=os.environ.get("WCED_ENABLE_ACLED", "").lower() in ("1", "true", "yes"),
            cross_method_tolerance=float(
                os.environ.get("WCED_CROSS_METHOD_TOLERANCE", "2.0")
            ),
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance built from the current environment."""
    return Settings.from_env()
