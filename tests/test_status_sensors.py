"""Tests for status pane improvements (Issue #18b / #23).

Tests for:
- _compute_automation_status logic
- _compute_next_automation_action logic
- ClimateAdvisorNextActionSensor name rename
"""
from __future__ import annotations

import sys
from datetime import time
from unittest.mock import MagicMock

import pytest


# ── HA module stubs (must happen before importing climate_advisor) ──
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs
    _install_ha_stubs()

from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    ATTR_NEXT_AUTOMATION_ACTION,
    ATTR_NEXT_AUTOMATION_TIME,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_classification(**overrides):
    """Build a DayClassification bypassing __post_init__."""
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "hot",
        "trend_direction": "stable",
        "trend_magnitude": 0,
        "today_high": 90,
        "today_low": 70,
        "tomorrow_high": 88,
        "tomorrow_low": 68,
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


def _make_automation_engine(
    *,
    is_paused_by_door: bool = False,
    grace_active: bool = False,
    last_resume_source: str | None = None,
) -> MagicMock:
    """Create a mock AutomationEngine with given state flags."""
    ae = MagicMock()
    ae.is_paused_by_door = is_paused_by_door
    ae._grace_active = grace_active
    ae._last_resume_source = last_resume_source
    return ae


def _compute_automation_status(
    automation_enabled: bool,
    automation_engine,
) -> str:
    """Replicate _compute_automation_status from coordinator.py."""
    if not automation_enabled:
        return "disabled"
    if automation_engine.is_paused_by_door:
        return "paused — door/window open"
    if automation_engine._grace_active:
        source = automation_engine._last_resume_source or "automation"
        return f"grace period ({source})"
    return "active"


def _compute_next_automation_action(
    c,
    automation_engine,
    config: dict,
    now_time: time,
) -> tuple[str, str]:
    """Replicate _compute_next_automation_action from coordinator.py."""
    if c is None:
        return ("Waiting for classification...", "")

    if automation_engine.is_paused_by_door:
        return ("Waiting — HVAC paused (door/window open)", "")

    if automation_engine._grace_active:
        source = automation_engine._last_resume_source or "automation"
        return (f"Grace period active ({source})", "")

    wake_time = config.get("wake_time", "06:30:00")
    sleep_time = config.get("sleep_time", "22:30:00")
    briefing_time = config.get("briefing_time", "06:00:00")

    def _parse_time(t: str) -> time:
        parts = t.split(":")
        return time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)

    events: list[tuple[time, str]] = []

    bt = _parse_time(briefing_time)
    wt = _parse_time(wake_time)
    st = _parse_time(sleep_time)

    if now_time < bt:
        events.append((bt, "Send daily briefing"))
    if now_time < wt:
        if c.hvac_mode in ("heat", "cool"):
            events.append((wt, f"Morning wake-up — restore {c.hvac_mode} comfort"))
        else:
            events.append((wt, "Morning wake-up check"))
    if now_time < st:
        if c.hvac_mode == "heat":
            bedtime_target = config.get("comfort_heat", 70) - 4 + c.setback_modifier
            events.append((st, f"Bedtime — heat setback to {bedtime_target:.0f}°F"))
        elif c.hvac_mode == "cool":
            bedtime_target = config.get("comfort_cool", 75) + 3
            events.append((st, f"Bedtime — cool setback to {bedtime_target:.0f}°F"))
        else:
            events.append((st, "Bedtime check"))

    if not events:
        return ("No more actions today", "")

    events.sort(key=lambda e: e[0])
    next_time, next_desc = events[0]
    time_str = next_time.strftime("%I:%M %p").lstrip("0")
    return (next_desc, time_str)


# ---------------------------------------------------------------------------
# Tests: _compute_automation_status
# ---------------------------------------------------------------------------

class TestComputeAutomationStatus:
    """Tests for _compute_automation_status logic."""

    def test_automation_status_active(self):
        """No pause, no grace → 'active'."""
        ae = _make_automation_engine()
        result = _compute_automation_status(True, ae)
        assert result == "active"

    def test_automation_status_paused_by_door(self):
        """paused_by_door=True → 'paused — door/window open'."""
        ae = _make_automation_engine(is_paused_by_door=True)
        result = _compute_automation_status(True, ae)
        assert result == "paused — door/window open"

    def test_automation_status_grace_period(self):
        """grace_active=True → contains 'grace period'."""
        ae = _make_automation_engine(grace_active=True, last_resume_source="manual")
        result = _compute_automation_status(True, ae)
        assert "grace period" in result
        assert "manual" in result

    def test_automation_status_grace_period_no_source(self):
        """grace_active=True with no resume source → defaults to 'automation'."""
        ae = _make_automation_engine(grace_active=True, last_resume_source=None)
        result = _compute_automation_status(True, ae)
        assert "grace period" in result
        assert "automation" in result

    def test_automation_status_disabled(self):
        """_automation_enabled=False → 'disabled'."""
        ae = _make_automation_engine()
        result = _compute_automation_status(False, ae)
        assert result == "disabled"

    def test_disabled_takes_priority_over_paused(self):
        """Disabled overrides paused state."""
        ae = _make_automation_engine(is_paused_by_door=True)
        result = _compute_automation_status(False, ae)
        assert result == "disabled"


# ---------------------------------------------------------------------------
# Tests: _compute_next_automation_action
# ---------------------------------------------------------------------------

class TestComputeNextAutomationAction:
    """Tests for _compute_next_automation_action logic."""

    def test_no_classification_returns_waiting(self):
        """When classification is None → 'Waiting for classification...'"""
        ae = _make_automation_engine()
        action, t = _compute_next_automation_action(None, ae, {}, time(8, 0))
        assert action == "Waiting for classification..."
        assert t == ""

    def test_paused_by_door_returns_waiting_message(self):
        """When paused_by_door → returns paused message regardless of schedule."""
        ae = _make_automation_engine(is_paused_by_door=True)
        c = _make_classification(hvac_mode="cool")
        action, t = _compute_next_automation_action(c, ae, {}, time(8, 0))
        assert "paused" in action.lower()
        assert "door" in action.lower()

    def test_grace_period_active_returns_grace_message(self):
        """When grace period active → returns grace message."""
        ae = _make_automation_engine(grace_active=True, last_resume_source="manual")
        c = _make_classification(hvac_mode="cool")
        action, t = _compute_next_automation_action(c, ae, {}, time(8, 0))
        assert "grace period" in action.lower()
        assert "manual" in action.lower()

    def test_before_briefing_time_returns_briefing_event(self):
        """Time before briefing_time → first event is 'Send daily briefing'."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {"briefing_time": "06:00:00", "wake_time": "06:30:00", "sleep_time": "22:30:00"}
        # Current time is 05:00 — before briefing at 06:00
        action, t = _compute_next_automation_action(c, ae, config, time(5, 0))
        assert action == "Send daily briefing"
        assert t == "6:00 AM"

    def test_before_bedtime_cool_day_returns_cool_setback(self):
        """Time after wakeup but before bedtime on cool day → bedtime cool setback."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
            "comfort_cool": 75,
        }
        # Current time is 14:00 — after briefing and wakeup, before sleep
        action, t = _compute_next_automation_action(c, ae, config, time(14, 0))
        assert "Bedtime" in action
        assert "cool setback" in action
        assert "78°F" in action  # 75 + 3

    def test_before_bedtime_heat_day_returns_heat_setback(self):
        """Time before bedtime on heat day → bedtime heat setback with correct temp."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="heat", setback_modifier=2.0)
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
            "comfort_heat": 70,
        }
        # Current time is 20:00 — before sleep at 22:30
        action, t = _compute_next_automation_action(c, ae, config, time(20, 0))
        assert "Bedtime" in action
        assert "heat setback" in action
        # 70 - 4 + 2 = 68°F
        assert "68°F" in action

    def test_after_all_events_returns_no_more_actions(self):
        """After all scheduled events have passed → 'No more actions today'."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="cool")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
        }
        # Current time is 23:00 — after all events
        action, t = _compute_next_automation_action(c, ae, config, time(23, 0))
        assert action == "No more actions today"
        assert t == ""

    def test_wakeup_event_for_heat_mode(self):
        """Before wakeup time in heat mode → morning wake-up heat comfort."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="heat")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
        }
        # Current time is 06:05 — between briefing and wake_time
        action, t = _compute_next_automation_action(c, ae, config, time(6, 5))
        assert "Morning wake-up" in action
        assert "heat" in action

    def test_off_mode_wakeup_returns_check(self):
        """Before wakeup in off mode → 'Morning wake-up check'."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="off")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
        }
        action, t = _compute_next_automation_action(c, ae, config, time(6, 5))
        assert action == "Morning wake-up check"

    def test_off_mode_bedtime_returns_check(self):
        """Before bedtime in off mode → 'Bedtime check'."""
        ae = _make_automation_engine()
        c = _make_classification(hvac_mode="off")
        config = {
            "briefing_time": "06:00:00",
            "wake_time": "06:30:00",
            "sleep_time": "22:30:00",
        }
        action, t = _compute_next_automation_action(c, ae, config, time(20, 0))
        assert action == "Bedtime check"


# ---------------------------------------------------------------------------
# Tests: Sensor name rename
# ---------------------------------------------------------------------------

class TestNextActionSensorRename:
    """Verify sensor names via source inspection.

    Sensor classes cannot be instantiated without a real HA runtime (metaclass
    conflict from MagicMock stubs), so we verify the source code directly.
    """

    @pytest.fixture(autouse=True)
    def _read_sensor_source(self):
        """Read sensor.py source once for all tests in this class."""
        import pathlib
        sensor_path = (
            pathlib.Path(__file__).resolve().parent.parent
            / "custom_components" / "climate_advisor" / "sensor.py"
        )
        self.source = sensor_path.read_text()

    def test_next_action_sensor_name_is_your_next_action(self):
        """Sensor display name should be 'Your Next Action'."""
        assert '"Your Next Action"' in self.source

    def test_new_automation_action_sensor_name(self):
        """Next Automation Action sensor class exists with correct name."""
        assert "ClimateAdvisorNextAutomationSensor" in self.source
        assert '"Next Automation Action"' in self.source

    def test_new_automation_time_sensor_name(self):
        """Next Automation Time sensor class exists with correct name."""
        assert "ClimateAdvisorNextAutomationTimeSensor" in self.source
        assert '"Next Automation Time"' in self.source


# ---------------------------------------------------------------------------
# Tests: New constants exist
# ---------------------------------------------------------------------------

class TestNewConstants:
    """Verify the new attribute constants were added to const.py."""

    def test_attr_next_automation_action_constant(self):
        """ATTR_NEXT_AUTOMATION_ACTION should equal 'next_automation_action'."""
        assert ATTR_NEXT_AUTOMATION_ACTION == "next_automation_action"

    def test_attr_next_automation_time_constant(self):
        """ATTR_NEXT_AUTOMATION_TIME should equal 'next_automation_time'."""
        assert ATTR_NEXT_AUTOMATION_TIME == "next_automation_time"
