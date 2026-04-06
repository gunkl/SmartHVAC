"""Tests for warm-day comfort gap guard in apply_classification().

When the day is classified as "warm" (or any day with hvac_mode="off") and the
indoor temperature is below the configured comfort floor, the automation should
defer the HVAC-off command and heat to the comfort floor instead.

GitHub Issue: warm-day comfort gap guard
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification

AUTOMATION_LOGGER = "custom_components.climate_advisor.automation"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consume_coroutine(coro):
    """Close coroutine to prevent 'never awaited' warnings."""
    coro.close()


def _make_engine(
    comfort_heat: float = 70.0,
    config_overrides: dict | None = None,
) -> AutomationEngine:
    """Build an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": 76.0,
        "setback_heat": 60.0,
        "setback_cool": 82.0,
        "notify_service": "notify.notify",
        "temp_unit": "fahrenheit",
    }
    if config_overrides:
        config.update(config_overrides)

    return AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=[],
        notify_service=config["notify_service"],
        config=config,
    )


def _make_warm_off_classification(day_type: str = "warm") -> DayClassification:
    """Build a DayClassification with hvac_mode='off' (warm day scenario)."""
    obj = object.__new__(DayClassification)
    obj.day_type = day_type
    obj.hvac_mode = "off"
    obj.trend_direction = "stable"
    obj.trend_magnitude = 1.0
    obj.today_high = 78.0
    obj.today_low = 58.0
    obj.tomorrow_high = 79.0
    obj.tomorrow_low = 59.0
    obj.pre_condition = False
    obj.pre_condition_target = None
    obj.windows_recommended = False
    obj.window_open_time = None
    obj.window_close_time = None
    obj.setback_modifier = 0.0
    return obj


def _set_indoor_temp_via_climate(engine: AutomationEngine, temp_f: float | None) -> None:
    """Configure hass.states.get so _get_indoor_temp_f() returns temp_f.

    Uses the climate-fallback path (no indoor_temp_source in config).
    When temp_f is None, states.get returns None → _get_indoor_temp_f returns None.
    """
    if temp_f is None:
        engine.hass.states.get.return_value = None
        return
    climate_state = MagicMock()
    climate_state.attributes.get.return_value = temp_f  # current_temperature attribute
    engine.hass.states.get.return_value = climate_state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWarmDayComfortGap:
    """Guard: warm day + indoor < comfort floor → heat to comfort first."""

    def test_indoor_below_comfort_sets_heat_mode(self):
        """When indoor (68°F) < comfort floor (70°F), HVAC is set to heat."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 68.0)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "heat"

    def test_indoor_below_comfort_sets_temperature_to_comfort_floor(self):
        """When guard fires, target temperature is set to comfort_heat."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 68.0)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call.args[1] == "set_temperature"]
        assert len(temp_calls) == 1
        # comfort_heat=70 in °F → service receives 70 (fahrenheit unit, no conversion)
        assert temp_calls[0].args[2]["temperature"] == 70.0

    def test_indoor_below_comfort_emits_event(self):
        """When guard fires, warm_day_comfort_gap event is emitted."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 68.0)

        events: list[tuple[str, dict]] = []
        engine._emit_event_callback = lambda name, data: events.append((name, data))

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        gap_events = [e for e in events if e[0] == "warm_day_comfort_gap"]
        assert len(gap_events) == 1
        payload = gap_events[0][1]
        assert payload["day_type"] == "warm"
        assert payload["indoor_temp"] == 68.0
        assert payload["comfort_heat"] == 70.0

    def test_indoor_below_comfort_logs_warning(self, caplog):
        """Guard fires a WARNING log mentioning the temperature gap."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 68.0)

        c = _make_warm_off_classification()
        with caplog.at_level(logging.WARNING, logger=AUTOMATION_LOGGER):
            asyncio.run(engine.apply_classification(c))

        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        deferred_msgs = [m for m in warn_msgs if "Warm-day off deferred" in m]
        assert len(deferred_msgs) == 1
        assert "68.0" in deferred_msgs[0]
        assert "70.0" in deferred_msgs[0]

    def test_indoor_at_comfort_floor_sets_off(self):
        """When indoor equals comfort floor, guard does NOT fire — HVAC set to off."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 70.0)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "off"

    def test_indoor_above_comfort_floor_sets_off(self):
        """When indoor (72°F) > comfort floor (70°F), HVAC is set to off."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 72.0)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "off"

    def test_indoor_above_comfort_no_temp_call(self):
        """When guard does not fire, no set_temperature service call is made."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 74.0)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call.args[1] == "set_temperature"]
        assert len(temp_calls) == 0

    def test_indoor_temp_unavailable_falls_back_to_off(self):
        """Safe fallback: when indoor temp is unavailable, HVAC goes off normally."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, None)  # states.get returns None

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "off"

    def test_indoor_temp_unavailable_no_temp_call(self):
        """Safe fallback: when indoor temp is unavailable, no set_temperature call."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, None)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        temp_calls = [call for call in calls if call.args[1] == "set_temperature"]
        assert len(temp_calls) == 0

    def test_comfort_heat_not_configured_falls_back_to_off(self):
        """Safe fallback: when comfort_heat is missing from config, HVAC goes off."""
        engine = _make_engine()
        del engine.config["comfort_heat"]
        _set_indoor_temp_via_climate(engine, 65.0)

        c = _make_warm_off_classification()
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "off"

    def test_guard_does_not_fire_without_event_callback(self):
        """Guard works correctly when no event callback is registered."""
        engine = _make_engine(comfort_heat=70.0)
        engine._emit_event_callback = None
        _set_indoor_temp_via_climate(engine, 65.0)

        c = _make_warm_off_classification()
        # Should not raise — event callback being None is handled gracefully
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "heat"

    def test_guard_applies_to_any_off_day_type(self):
        """Guard fires for any day_type with hvac_mode='off', not just 'warm'."""
        engine = _make_engine(comfort_heat=70.0)
        _set_indoor_temp_via_climate(engine, 65.0)

        c = _make_warm_off_classification(day_type="mild")
        asyncio.run(engine.apply_classification(c))

        calls = engine.hass.services.async_call.call_args_list
        hvac_calls = [call for call in calls if call.args[1] == "set_hvac_mode"]
        assert len(hvac_calls) == 1
        assert hvac_calls[0].args[2]["hvac_mode"] == "heat"
