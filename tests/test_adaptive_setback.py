"""Tests for compute_bedtime_setback() in automation.py (Phase 5G)."""

from __future__ import annotations

import sys

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.automation import compute_bedtime_setback
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    DEFAULT_SETBACK_DEPTH_COOL_F,
    DEFAULT_SETBACK_DEPTH_F,
    MAX_SETBACK_DEPTH_F,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "comfort_heat": 70,
    "comfort_cool": 75,
    "setback_heat": 60,
    "setback_cool": 80,
    "wake_time": "06:30",
    "sleep_time": "22:30",
    "temp_unit": "fahrenheit",
}


def _make_classification(*, hvac_mode: str = "heat", setback_modifier: float = 0.0) -> DayClassification:
    c = object.__new__(DayClassification)
    c.__dict__.update(
        {
            "day_type": "cold",
            "trend_direction": "stable",
            "trend_magnitude": 0.0,
            "today_high": 40.0,
            "today_low": 25.0,
            "tomorrow_high": 38.0,
            "tomorrow_low": 22.0,
            "hvac_mode": hvac_mode,
            "pre_condition": False,
            "pre_condition_target": None,
            "windows_recommended": False,
            "window_open_time": None,
            "window_close_time": None,
            "setback_modifier": setback_modifier,
            "window_opportunity_morning": False,
            "window_opportunity_evening": False,
        }
    )
    return c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestComputeBedtimeSetback:
    """Tests for compute_bedtime_setback()."""

    def test_setback_default_heat_when_no_model(self):
        """No thermal model → setback depth equals DEFAULT_SETBACK_DEPTH_F."""
        c = _make_classification(hvac_mode="heat")
        result = compute_bedtime_setback(_BASE_CONFIG, {}, c)
        expected = _BASE_CONFIG["comfort_heat"] - DEFAULT_SETBACK_DEPTH_F
        assert result == pytest.approx(expected)

    def test_setback_default_cool_when_no_model(self):
        """No thermal model, cool mode → setback depth equals DEFAULT_SETBACK_DEPTH_COOL_F."""
        c = _make_classification(hvac_mode="cool")
        result = compute_bedtime_setback(_BASE_CONFIG, {}, c)
        expected = _BASE_CONFIG["comfort_cool"] + DEFAULT_SETBACK_DEPTH_COOL_F
        assert result == pytest.approx(expected)

    def test_setback_capped_at_max(self):
        """Very fast house, long overnight window → depth capped at MAX_SETBACK_DEPTH_F."""
        # wake=06:30 (390min), sleep=22:30 (1350min) → overnight=480min
        # available = 480 - SETBACK_RECOVERY_BUFFER_MINUTES = 450min = 7.5hr
        # rate=10°F/hr → max_recoverable = 75°F >> MAX_SETBACK_DEPTH_F=8
        c = _make_classification(hvac_mode="heat")
        model = {"confidence": "high", "heating_rate_f_per_hour": 10.0}
        result = compute_bedtime_setback(_BASE_CONFIG, model, c)
        # depth is capped at MAX_SETBACK_DEPTH_F=8 → target = 70-8 = 62
        expected = _BASE_CONFIG["comfort_heat"] - MAX_SETBACK_DEPTH_F
        assert result == pytest.approx(expected)

    def test_setback_shallower_slow_house(self):
        """Low heating rate, short recovery window → shallower than default."""
        # wake=06:30, sleep=22:30 → overnight=480min, available=450min=7.5hr
        # rate=0.5°F/hr → max_recoverable = 0.5*7.5 = 3.75 < DEFAULT_SETBACK_DEPTH_F=4
        c = _make_classification(hvac_mode="heat")
        model = {"confidence": "low", "heating_rate_f_per_hour": 0.5}
        result = compute_bedtime_setback(_BASE_CONFIG, model, c)
        default_target = _BASE_CONFIG["comfort_heat"] - DEFAULT_SETBACK_DEPTH_F
        # Result should be shallower (higher temp) than default
        assert result > default_target

    def test_setback_floored_at_setback_heat_config(self):
        """Computed target below setback floor → clamped to floor."""
        # Use comfort_heat=70, setback_heat=68 with a fast model
        config = dict(_BASE_CONFIG, comfort_heat=70, setback_heat=68)
        # With rate=10, depth would be 8 → target=62, but floor=68
        c = _make_classification(hvac_mode="heat")
        model = {"confidence": "high", "heating_rate_f_per_hour": 10.0}
        result = compute_bedtime_setback(config, model, c)
        assert result == pytest.approx(config["setback_heat"])

    def test_setback_never_above_comfort(self):
        """Setback target must not exceed comfort temperature (heat mode)."""
        c = _make_classification(hvac_mode="heat")
        # Even with no model (depth=DEFAULT_SETBACK_DEPTH_F=4), result should be < comfort
        result = compute_bedtime_setback(_BASE_CONFIG, {}, c)
        assert result <= _BASE_CONFIG["comfort_heat"]

    def test_setback_modifier_applied(self):
        """Nonzero setback_modifier shifts the target."""
        c_default = _make_classification(hvac_mode="heat", setback_modifier=0.0)
        c_modified = _make_classification(hvac_mode="heat", setback_modifier=2.0)
        result_default = compute_bedtime_setback(_BASE_CONFIG, {}, c_default)
        result_modified = compute_bedtime_setback(_BASE_CONFIG, {}, c_modified)
        assert result_modified == pytest.approx(result_default + 2.0)

    def test_cool_setback_uses_cooling_rate(self):
        """Cool mode uses cooling_rate_f_per_hour, not heating_rate_f_per_hour."""
        c = _make_classification(hvac_mode="cool")
        # Set high cooling rate but no heating rate
        model = {"confidence": "high", "cooling_rate_f_per_hour": 10.0, "heating_rate_f_per_hour": None}
        result = compute_bedtime_setback(_BASE_CONFIG, model, c)
        # With rate=10, depth=8 (capped), cool setback goes UP: 75+8=83 > setback_cool=80 → clamped to 80
        assert result == pytest.approx(_BASE_CONFIG["setback_cool"])
