"""Tests for coordinator helper methods (Issue #18 / Phase 1).

Tests for:
- _compute_next_action logic: HOT day morning/evening window opportunities
  and default closed-windows fallback.
"""
from __future__ import annotations

import sys
from datetime import time



# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs
    _install_ha_stubs()

from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    DAY_TYPE_HOT,
    DAY_TYPE_COLD,
    ECONOMIZER_MORNING_END_HOUR,
    ECONOMIZER_EVENING_START_HOUR,
    ECONOMIZER_TEMP_DELTA,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__."""
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": DAY_TYPE_HOT,
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 95,
        "today_low": 72,
        "tomorrow_high": 93,
        "tomorrow_low": 70,
        "hvac_mode": "cool",
        "pre_condition": False,
        "pre_condition_target": None,
        "windows_recommended": False,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
        "window_opportunity_morning": False,
        "window_opportunity_evening": False,
    }
    defaults.update(overrides)
    c.__dict__.update(defaults)
    return c


def _compute_next_action(c: DayClassification | None, config: dict, now_time: time) -> str:
    """Replicate _compute_next_action from coordinator.py."""
    if not c:
        return "Waiting for forecast data..."

    if c.windows_recommended:
        if c.window_open_time and now_time < c.window_open_time:
            return f"Open windows at {c.window_open_time.strftime('%I:%M %p')}"
        elif c.window_close_time and now_time < c.window_close_time:
            return f"Close windows by {c.window_close_time.strftime('%I:%M %p')}"

    if c.day_type == DAY_TYPE_HOT:
        comfort_cool = config.get("comfort_cool", 75)
        threshold = comfort_cool + ECONOMIZER_TEMP_DELTA
        if c.window_opportunity_morning and now_time < time(ECONOMIZER_MORNING_END_HOUR, 0):
            end_t = time(ECONOMIZER_MORNING_END_HOUR, 0).strftime("%I:%M %p").lstrip("0")
            return f"Open windows if outdoor temp is below {threshold:.0f}°F (until {end_t})"
        elif c.window_opportunity_evening and now_time >= time(ECONOMIZER_EVENING_START_HOUR, 0):
            return f"Open windows if outdoor temp is below {threshold:.0f}°F"
        return "Keep windows and blinds closed. AC is handling it."
    elif c.day_type == DAY_TYPE_COLD:
        return "Keep doors closed — help the heater out."

    return "No action needed right now. Automation is handling it."


# ---------------------------------------------------------------------------
# Tests: _compute_next_action
# ---------------------------------------------------------------------------

class TestComputeNextAction:
    """Tests for _compute_next_action logic in coordinator.py."""

    def test_next_action_no_classification_returns_waiting(self):
        """When classification is None → 'Waiting for forecast data...'."""
        result = _compute_next_action(None, {}, time(8, 0))
        assert result == "Waiting for forecast data..."

    def test_next_action_hot_morning_shows_threshold(self):
        """HOT day, morning opportunity, before 9 AM → threshold and cutoff time shown."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=True,
            window_opportunity_evening=False,
        )
        config = {"comfort_cool": 75}
        # Mock time: 7:30 AM — before ECONOMIZER_MORNING_END_HOUR (9:00 AM)
        result = _compute_next_action(c, config, time(7, 30))
        assert "78" in result          # threshold = 75 + 3
        assert "9:00 AM" in result     # ECONOMIZER_MORNING_END_HOUR formatted

    def test_next_action_hot_morning_at_exactly_morning_end_does_not_trigger(self):
        """HOT day, morning opportunity, at exactly 9:00 AM → morning branch not taken."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=True,
            window_opportunity_evening=False,
        )
        config = {"comfort_cool": 75}
        # now_time == time(9, 0) — NOT less than morning end, so morning branch skipped
        result = _compute_next_action(c, config, time(9, 0))
        assert "9:00 AM" not in result

    def test_next_action_hot_evening_shows_threshold(self):
        """HOT day, evening opportunity, at or after 5 PM → threshold shown without cutoff."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=False,
            window_opportunity_evening=True,
        )
        config = {"comfort_cool": 75}
        # Mock time: 5:00 PM — exactly ECONOMIZER_EVENING_START_HOUR (17:00)
        result = _compute_next_action(c, config, time(17, 0))
        assert "78" in result          # threshold = 75 + 3
        assert "9:00 AM" not in result  # no morning cutoff text

    def test_next_action_hot_evening_later_in_evening(self):
        """HOT day, evening opportunity, well into the evening → threshold still shown."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=False,
            window_opportunity_evening=True,
        )
        config = {"comfort_cool": 75}
        result = _compute_next_action(c, config, time(20, 0))
        assert "78" in result

    def test_next_action_hot_evening_before_start_hour_does_not_trigger(self):
        """HOT day, evening opportunity, before 5 PM → evening branch not taken."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=False,
            window_opportunity_evening=True,
        )
        config = {"comfort_cool": 75}
        # 3:00 PM — before ECONOMIZER_EVENING_START_HOUR (17:00)
        result = _compute_next_action(c, config, time(15, 0))
        assert "Keep windows and blinds closed" in result

    def test_next_action_hot_no_opportunity(self):
        """HOT day, both opportunity flags False → default closed-windows message."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=False,
            window_opportunity_evening=False,
        )
        result = _compute_next_action(c, {}, time(13, 0))
        assert "Keep windows and blinds closed" in result
        assert "AC is handling it" in result

    def test_next_action_hot_uses_custom_comfort_cool(self):
        """HOT day morning opportunity respects a non-default comfort_cool setting."""
        c = _make_classification(
            day_type=DAY_TYPE_HOT,
            window_opportunity_morning=True,
        )
        config = {"comfort_cool": 72}
        # threshold = 72 + 3 = 75
        result = _compute_next_action(c, config, time(8, 0))
        assert "75" in result

    def test_next_action_cold_day(self):
        """COLD day → keep-doors-closed message."""
        c = _make_classification(day_type=DAY_TYPE_COLD)
        result = _compute_next_action(c, {}, time(12, 0))
        assert "Keep doors closed" in result

    def test_next_action_mild_day(self):
        """Mild day (not hot/cold) → no action needed message."""
        c = _make_classification(day_type="mild")
        result = _compute_next_action(c, {}, time(12, 0))
        assert "No action needed" in result
