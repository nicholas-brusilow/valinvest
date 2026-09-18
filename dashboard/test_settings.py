"""Plain-python test runner for :mod:`settings_store` (no pytest dependency).

Run inside the etl container:
    python dashboard/test_settings.py

Exits non-zero if any case fails.  All file tests use temporary directories;
the real settings file is never touched.
"""
from __future__ import annotations

import copy
import glob
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings_store  # noqa: E402

PASSES: list[str] = []
FAILURES: list[str] = []

CHOICES = {
    "freq": ("Quarterly", "Semiannually (Q2/Q4)", "Annually (Q4)"),
    "vol_filter": ("No limit", "≤ 30%", "≤ 40%", "≤ 50%", "≤ 60%", "≤ 80%"),
    "start_year": ("Earliest available", "2008", "2009"),
}
BOUNDS = {
    "pe_range": (0.0, 60.0),
    "mcap_min": (0.0, None),
    "mcap_max": (0.0, None),
    "div_yield": (0.0, 10.0),
    "min_ret": (-100.0, 0.0),
}


def check(name: str, fn) -> None:
    try:
        fn()
    except AssertionError as exc:
        FAILURES.append(name)
        print(f"FAIL {name}: {exc}")
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(name)
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc()
    else:
        PASSES.append(name)
        print(f"PASS {name}")


# --------------------------------------------------------------------------- #
# 1. settings_path
# --------------------------------------------------------------------------- #


def test_settings_path_default():
    path = settings_store.settings_path()
    assert path == settings_store.DEFAULT_SETTINGS_PATH, path
    assert os.path.isabs(path), path
    assert path.endswith(os.path.join("dashboard", "saved_settings.json")), path


def test_settings_path_env_override():
    old = os.environ.get("VALINVEST_SETTINGS_PATH")
    os.environ["VALINVEST_SETTINGS_PATH"] = "/tmp/x.json"
    try:
        assert settings_store.settings_path() == "/tmp/x.json", settings_store.settings_path()
    finally:
        if old is None:
            os.environ.pop("VALINVEST_SETTINGS_PATH", None)
        else:
            os.environ["VALINVEST_SETTINGS_PATH"] = old


# --------------------------------------------------------------------------- #
# 2. save / load
# --------------------------------------------------------------------------- #


def test_save_creates_parents_and_version():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sub", "dir", "settings.json")
        settings_store.save_settings(path, settings_store.DEFAULT_SETTINGS)
        assert os.path.isfile(path), path
        loaded = settings_store.load_settings(path)
        assert loaded is not None
        assert loaded.version == settings_store.SETTINGS_VERSION, loaded.version


def test_roundtrip_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "settings.json")
        settings_store.save_settings(path, settings_store.DEFAULT_SETTINGS)
        loaded = settings_store.load_settings(path)
        assert loaded is not None
        clean = settings_store.sanitize(
            loaded.settings, choices=CHOICES, bounds=BOUNDS
        )
        assert clean == settings_store.DEFAULT_SETTINGS, clean
        assert isinstance(clean["pe_range"], tuple), clean["pe_range"]


def test_save_sets_saved_at():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "settings.json")
        settings_store.save_settings(
            path, settings_store.DEFAULT_SETTINGS,
            saved_at="2026-01-01T00:00:00+00:00",
        )
        loaded = settings_store.load_settings(path)
        assert loaded is not None
        assert loaded.saved_at == "2026-01-01T00:00:00+00:00", loaded.saved_at

        settings_store.save_settings(path, settings_store.DEFAULT_SETTINGS)
        auto = settings_store.load_settings(path)
        assert auto is not None
        assert isinstance(auto.saved_at, str) and auto.saved_at, auto.saved_at


def test_save_overwrites_atomically():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "settings.json")
        first = dict(settings_store.DEFAULT_SETTINGS)
        first["min_ret"] = -50
        settings_store.save_settings(path, first)
        second = dict(settings_store.DEFAULT_SETTINGS)
        second["min_ret"] = -25
        settings_store.save_settings(path, second)

        loaded = settings_store.load_settings(path)
        assert loaded is not None
        assert loaded.settings["min_ret"] == -25, loaded.settings["min_ret"]
        leftovers = glob.glob(os.path.join(tmp, "*.tmp"))
        assert leftovers == [], leftovers


def test_load_missing_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "does_not_exist.json")
        assert settings_store.load_settings(path) is None


def test_load_corrupt_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not valid json")
        assert settings_store.load_settings(path) is None

        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[]")
        assert settings_store.load_settings(path) is None

        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"settings": 5}')
        assert settings_store.load_settings(path) is None

        settings_store.save_settings(path, settings_store.DEFAULT_SETTINGS)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"version": 1, "saved_at": "x", "settings": ')
        assert settings_store.load_settings(path) is None


# --------------------------------------------------------------------------- #
# 3. sanitize
# --------------------------------------------------------------------------- #


def test_sanitize_bool_rejected_for_numeric():
    clean = settings_store.sanitize(
        {"min_ret": True, "log_scale": 1}, choices=CHOICES, bounds=BOUNDS
    )
    assert clean["min_ret"] == -100, clean["min_ret"]
    assert clean["log_scale"] is False, clean["log_scale"]

    clean = settings_store.sanitize(
        {"log_scale": True}, choices=CHOICES, bounds=BOUNDS
    )
    assert clean["log_scale"] is True, clean["log_scale"]


def test_sanitize_coercion_failures_fall_back():
    clean = settings_store.sanitize(
        {"mcap_max": "abc", "mcap_min": None}, choices=CHOICES, bounds=BOUNDS
    )
    assert clean["mcap_max"] == 100.0, clean["mcap_max"]
    assert clean["mcap_min"] == 0.0, clean["mcap_min"]


def test_sanitize_clamps_bounds():
    clean = settings_store.sanitize(
        {
            "pe_range": [-5, 999],
            "div_yield": 50,
            "min_ret": -500,
        },
        choices=CHOICES,
        bounds=BOUNDS,
    )
    assert clean["pe_range"] == (0.0, 60.0), clean["pe_range"]
    assert clean["div_yield"] == 10.0, clean["div_yield"]
    assert clean["min_ret"] == -100, clean["min_ret"]

    clean = settings_store.sanitize(
        {"min_ret": -50.7}, choices=CHOICES, bounds=BOUNDS
    )
    assert clean["min_ret"] == -51, clean["min_ret"]
    assert isinstance(clean["min_ret"], int), type(clean["min_ret"])


def test_sanitize_pe_range_forms():
    clean = settings_store.sanitize(
        {"pe_range": [25, 5]}, choices=CHOICES, bounds=BOUNDS
    )
    assert clean["pe_range"] == (5.0, 25.0), clean["pe_range"]

    for bad in ("ab", [1], [1, 2, 3], [float("nan"), 5]):
        clean = settings_store.sanitize(
            {"pe_range": bad}, choices=CHOICES, bounds=BOUNDS
        )
        assert clean["pe_range"] == (0.0, 15.0), (bad, clean["pe_range"])


def test_sanitize_choices_and_unknown_keys():
    clean = settings_store.sanitize(
        {"freq": "Nope", "vol_filter": 123, "bogus": 7},
        choices=CHOICES,
        bounds=BOUNDS,
    )
    assert clean["freq"] == "Quarterly", clean["freq"]
    assert clean["vol_filter"] == "No limit", clean["vol_filter"]
    assert "bogus" not in clean, clean
    assert set(clean) == set(settings_store.DEFAULT_SETTINGS), set(clean)


def test_sanitize_never_mutates_defaults():
    snapshot = copy.deepcopy(settings_store.DEFAULT_SETTINGS)
    clean = settings_store.sanitize(None, choices=CHOICES, bounds=BOUNDS)
    assert clean == settings_store.DEFAULT_SETTINGS, clean
    assert settings_store.DEFAULT_SETTINGS == snapshot, settings_store.DEFAULT_SETTINGS
    assert clean is not settings_store.DEFAULT_SETTINGS
    # A partial dict must not leak into the module-level defaults either.
    settings_store.sanitize({"min_ret": -3}, choices=CHOICES, bounds=BOUNDS)
    assert settings_store.DEFAULT_SETTINGS == snapshot, settings_store.DEFAULT_SETTINGS


# --------------------------------------------------------------------------- #

TESTS = [
    ("settings_path_default", test_settings_path_default),
    ("settings_path_env_override", test_settings_path_env_override),
    ("save_creates_parents_and_version", test_save_creates_parents_and_version),
    ("roundtrip_defaults", test_roundtrip_defaults),
    ("save_sets_saved_at", test_save_sets_saved_at),
    ("save_overwrites_atomically", test_save_overwrites_atomically),
    ("load_missing_returns_none", test_load_missing_returns_none),
    ("load_corrupt_returns_none", test_load_corrupt_returns_none),
    ("sanitize_bool_rejected_for_numeric", test_sanitize_bool_rejected_for_numeric),
    ("sanitize_coercion_failures_fall_back", test_sanitize_coercion_failures_fall_back),
    ("sanitize_clamps_bounds", test_sanitize_clamps_bounds),
    ("sanitize_pe_range_forms", test_sanitize_pe_range_forms),
    ("sanitize_choices_and_unknown_keys", test_sanitize_choices_and_unknown_keys),
    ("sanitize_never_mutates_defaults", test_sanitize_never_mutates_defaults),
]


def main() -> int:
    for name, fn in TESTS:
        check(name, fn)
    print()
    print(f"summary: {len(PASSES)} passed, {len(FAILURES)} failed")
    if FAILURES:
        print("failed: " + ", ".join(FAILURES))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
