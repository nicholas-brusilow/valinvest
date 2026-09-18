"""Persistence and validation for the dashboard's screening criteria.

This module is intentionally UI-free (no Streamlit import) so it can be
unit-tested headlessly.  :data:`DEFAULT_SETTINGS` mirrors the sidebar widget
defaults in ``app.py`` -- keep the two in sync when a control changes.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import tempfile
from dataclasses import dataclass

SETTINGS_VERSION = 1
DEFAULT_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "saved_settings.json"
)

DEFAULT_SETTINGS: dict[str, object] = {
    "freq": "Quarterly",
    "pe_range": (0.0, 15.0),
    "mcap_min": 0.0,
    "mcap_max": 100.0,
    "div_yield": 0.0,
    "require_pos_eps4": True,
    "vol_filter": "No limit",
    "min_ret": -100,
    "start_year": "Earliest available",
    "log_scale": False,
}


@dataclass(frozen=True)
class SavedSettings:
    """A settings payload loaded from disk."""

    settings: dict
    saved_at: str | None = None
    version: int = SETTINGS_VERSION


def settings_path() -> str:
    """Location of the settings file (``VALINVEST_SETTINGS_PATH`` overrides)."""
    return os.environ.get("VALINVEST_SETTINGS_PATH", DEFAULT_SETTINGS_PATH)


def save_settings(
    path: str, settings: dict, saved_at: str | None = None
) -> None:
    """Atomically write ``settings`` as JSON to ``path``.

    The temp file is created in the *target* directory (not the system temp
    dir) so ``os.replace`` stays on one filesystem and never fails with
    ``OSError [Errno 18] Invalid cross-device link``.
    """
    payload = {
        "version": SETTINGS_VERSION,
        "saved_at": saved_at
        or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "settings": dict(settings),
    }

    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".saved_settings_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_settings(path: str) -> SavedSettings | None:
    """Load and lightly validate a settings file; ``None`` when unusable."""
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        return None

    saved_at = payload.get("saved_at")
    if not isinstance(saved_at, str):
        saved_at = None
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        version = SETTINGS_VERSION

    return SavedSettings(settings=settings, saved_at=saved_at, version=version)


def _clamp(value: float, lo: float | None, hi: float | None) -> float:
    """Clamp ``value`` to ``[lo, hi]``; ``None`` means unbounded."""
    if lo is not None:
        value = max(value, lo)
    if hi is not None:
        value = min(value, hi)
    return value


def _coerce_bool(value, default):
    return value if isinstance(value, bool) else default


def _coerce_tuple(value, default, bounds):
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        return default
    if len(value) != 2:
        return default
    try:
        lo_v, hi_v = float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return default
    if not (math.isfinite(lo_v) and math.isfinite(hi_v)):
        return default
    lo_b, hi_b = bounds if bounds is not None else (None, None)
    lo_v = _clamp(lo_v, lo_b, hi_b)
    hi_v = _clamp(hi_v, lo_b, hi_b)
    return tuple(sorted((lo_v, hi_v)))


def _coerce_int(value, default, bounds):
    if isinstance(value, bool):
        return default
    result = int(round(float(value)))
    lo, hi = bounds if bounds is not None else (None, None)
    return int(_clamp(result, lo, hi))


def _coerce_float(value, default, bounds):
    if isinstance(value, bool):
        return default
    result = float(value)
    if not math.isfinite(result):
        return default
    lo, hi = bounds if bounds is not None else (None, None)
    return _clamp(result, lo, hi)


def _coerce_str(value, default, choices):
    if not isinstance(value, str):
        return default
    if choices is not None and value not in choices:
        return default
    return value


def sanitize(
    raw,
    choices: dict | None = None,
    bounds: dict | None = None,
    defaults: dict = DEFAULT_SETTINGS,
) -> dict:
    """Return a fresh, validated settings dict built from ``raw``.

    Unknown keys are dropped and missing keys are filled from ``defaults``;
    each value is coerced according to the type of its default and clamped to
    the per-key ``bounds`` / restricted to the per-key ``choices``.  Any
    coercion failure falls back to that key's default.  ``defaults`` is never
    mutated.
    """
    out = dict(defaults)
    if not isinstance(raw, dict):
        return out

    choices = choices or {}
    bounds = bounds or {}
    for key, default in defaults.items():
        if key not in raw:
            continue
        value = raw[key]
        key_bounds = bounds.get(key)
        try:
            if isinstance(default, bool):
                out[key] = _coerce_bool(value, default)
            elif isinstance(default, tuple):
                out[key] = _coerce_tuple(value, default, key_bounds)
            elif isinstance(default, int):
                out[key] = _coerce_int(value, default, key_bounds)
            elif isinstance(default, float):
                out[key] = _coerce_float(value, default, key_bounds)
            elif isinstance(default, str):
                out[key] = _coerce_str(value, default, choices.get(key))
        except (TypeError, ValueError, OverflowError):
            out[key] = default
    return out
