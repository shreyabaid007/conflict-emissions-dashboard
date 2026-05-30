"""Backward-compatibility shim — delegates to ``wced.verify.corroboration``.

All new code should import from ``wced.verify.corroboration`` directly.
This module re-exports ``find_acled_corroboration`` and the public constants
so existing imports continue to work.
"""
from wced.verify.corroboration import (  # noqa: F401
    DEFAULT_SPACE_WINDOW_M,
    DEFAULT_TIME_WINDOW_H,
    CorroborationMatch,
    _haversine_m,
    find_acled_corroboration,
    find_corroboration,
)
