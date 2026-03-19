"""Tests for whole-house fan control (Issue #18 Phase 4).

Tests cover:
- _activate_fan: whole_house_fan, hvac_fan, both, disabled
- _deactivate_fan: whole_house_fan, hvac_fan, both, disabled
- switch domain detection (switch.attic_fan)
- dry_run mode skips all service calls
- fan activation integrates with economizer maintain phase
- fan deactivation integrates with economizer off
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    CONF_FAN_ENTITY,
    CONF_FAN_MODE,
    DAY_TYPE_HOT,
    FAN_MODE_BOTH,
    FAN_MODE_DISABLED,
    FAN_MODE_HVAC,
    FAN_MODE_WHOLE_HOUSE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_automation_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = MagicMock()
    hass.states = MagicMock()

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.notify",
    }
    if config_overrides:
        config.update(config_overrides)

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )
    return engine


def _make_hot_classification() -> DayClassification:
    """Build a HOT DayClassification (bypasses __post_init__)."""
    c = object.__new__(DayClassification)
    c.day_type = DAY_TYPE_HOT
    c.trend_direction = "stable"
    c.trend_magnitude = 0.0
    c.today_high = 90.0
    c.today_low = 70.0
    c.tomorrow_high = 90.0
    c.tomorrow_low = 70.0
    c.hvac_mode = "cool"
    c.pre_condition = True
    c.pre_condition_target = -2.0
    c.windows_recommended = False
    c.window_open_time = None
    c.window_close_time = None
    c.setback_modifier = 0.0
    c.window_opportunity_morning = False
    c.window_opportunity_evening = False
    return c


def _get_service_calls(engine, domain: str, service: str) -> list:
    """Extract calls matching a specific domain and service."""
    return [
        c for c in engine.hass.services.async_call.call_args_list
        if c[0][0] == domain and c[0][1] == service
    ]


# ---------------------------------------------------------------------------
# _activate_fan tests
# ---------------------------------------------------------------------------

class TestActivateFan:
    """Tests for _activate_fan."""

    def test_activate_whole_house_fan(self):
        """fan_mode=whole_house_fan, fan_entity=fan.attic → calls fan.turn_on."""
        engine = _make_automation_engine({
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "fan.attic",
        })

        asyncio.run(engine._activate_fan(reason="test"))

        calls = _get_service_calls(engine, "fan", "turn_on")
        assert len(calls) == 1
        assert calls[0][0][2]["entity_id"] == "fan.attic"
        # Should NOT call HVAC fan mode
        hvac_fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(hvac_fan_calls) == 0

    def test_activate_hvac_fan(self):
        """fan_mode=hvac_fan → calls climate.set_fan_mode with 'on'."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})

        asyncio.run(engine._activate_fan(reason="test"))

        calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(calls) == 1
        assert calls[0][0][2]["fan_mode"] == "on"
        assert calls[0][0][2]["entity_id"] == "climate.thermostat"

    def test_activate_both_fans(self):
        """fan_mode=both → calls both fan.turn_on and climate.set_fan_mode 'on'."""
        engine = _make_automation_engine({
            CONF_FAN_MODE: FAN_MODE_BOTH,
            CONF_FAN_ENTITY: "fan.attic",
        })

        asyncio.run(engine._activate_fan(reason="test"))

        fan_calls = _get_service_calls(engine, "fan", "turn_on")
        assert len(fan_calls) == 1
        assert fan_calls[0][0][2]["entity_id"] == "fan.attic"

        hvac_fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(hvac_fan_calls) == 1
        assert hvac_fan_calls[0][0][2]["fan_mode"] == "on"

    def test_fan_disabled_skips_all_activate(self):
        """fan_mode=disabled → no service calls on activate."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_DISABLED})

        asyncio.run(engine._activate_fan(reason="test"))

        engine.hass.services.async_call.assert_not_called()

    def test_fan_disabled_by_default_skips_all(self):
        """No fan_mode in config → defaults to disabled, no service calls."""
        engine = _make_automation_engine()  # no fan config at all

        asyncio.run(engine._activate_fan(reason="test"))

        engine.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# _deactivate_fan tests
# ---------------------------------------------------------------------------

class TestDeactivateFan:
    """Tests for _deactivate_fan."""

    def test_deactivate_whole_house_fan(self):
        """fan_mode=whole_house_fan, fan_entity=fan.attic → calls fan.turn_off."""
        engine = _make_automation_engine({
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "fan.attic",
        })

        asyncio.run(engine._deactivate_fan(reason="test"))

        calls = _get_service_calls(engine, "fan", "turn_off")
        assert len(calls) == 1
        assert calls[0][0][2]["entity_id"] == "fan.attic"
        # Should NOT call HVAC fan mode
        hvac_fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(hvac_fan_calls) == 0

    def test_deactivate_hvac_fan(self):
        """fan_mode=hvac_fan → calls climate.set_fan_mode with 'auto'."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_HVAC})

        asyncio.run(engine._deactivate_fan(reason="test"))

        calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(calls) == 1
        assert calls[0][0][2]["fan_mode"] == "auto"
        assert calls[0][0][2]["entity_id"] == "climate.thermostat"

    def test_deactivate_both_fans(self):
        """fan_mode=both → calls both fan.turn_off and climate.set_fan_mode 'auto'."""
        engine = _make_automation_engine({
            CONF_FAN_MODE: FAN_MODE_BOTH,
            CONF_FAN_ENTITY: "fan.attic",
        })

        asyncio.run(engine._deactivate_fan(reason="test"))

        fan_calls = _get_service_calls(engine, "fan", "turn_off")
        assert len(fan_calls) == 1
        assert fan_calls[0][0][2]["entity_id"] == "fan.attic"

        hvac_fan_calls = _get_service_calls(engine, "climate", "set_fan_mode")
        assert len(hvac_fan_calls) == 1
        assert hvac_fan_calls[0][0][2]["fan_mode"] == "auto"

    def test_fan_disabled_skips_all_deactivate(self):
        """fan_mode=disabled → no service calls on deactivate."""
        engine = _make_automation_engine({CONF_FAN_MODE: FAN_MODE_DISABLED})

        asyncio.run(engine._deactivate_fan(reason="test"))

        engine.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# Switch domain detection
# ---------------------------------------------------------------------------

class TestSwitchDomainFan:
    """Fan entity in switch domain uses switch.turn_on / switch.turn_off."""

    def test_switch_domain_activate(self):
        """fan_entity=switch.attic_fan → calls switch.turn_on."""
        engine = _make_automation_engine({
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "switch.attic_fan",
        })

        asyncio.run(engine._activate_fan(reason="test"))

        calls = _get_service_calls(engine, "switch", "turn_on")
        assert len(calls) == 1
        assert calls[0][0][2]["entity_id"] == "switch.attic_fan"

    def test_switch_domain_deactivate(self):
        """fan_entity=switch.attic_fan → calls switch.turn_off."""
        engine = _make_automation_engine({
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "switch.attic_fan",
        })

        asyncio.run(engine._deactivate_fan(reason="test"))

        calls = _get_service_calls(engine, "switch", "turn_off")
        assert len(calls) == 1
        assert calls[0][0][2]["entity_id"] == "switch.attic_fan"


# ---------------------------------------------------------------------------
# Dry run mode
# ---------------------------------------------------------------------------

class TestDryRunFan:
    """When dry_run=True, fan methods log but do not call any services."""

    def test_dry_run_skips_activate(self):
        """dry_run=True → _activate_fan logs but makes no service calls."""
        engine = _make_automation_engine({
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "fan.attic",
        })
        engine.dry_run = True

        asyncio.run(engine._activate_fan(reason="dry run test"))

        engine.hass.services.async_call.assert_not_called()

    def test_dry_run_skips_deactivate(self):
        """dry_run=True → _deactivate_fan logs but makes no service calls."""
        engine = _make_automation_engine({
            CONF_FAN_MODE: FAN_MODE_BOTH,
            CONF_FAN_ENTITY: "fan.attic",
        })
        engine.dry_run = True

        asyncio.run(engine._deactivate_fan(reason="dry run test"))

        engine.hass.services.async_call.assert_not_called()


# ---------------------------------------------------------------------------
# Economizer integration
# ---------------------------------------------------------------------------

class TestFanEconomizerIntegration:
    """Fan activates/deactivates together with the economizer."""

    def test_fan_activates_with_economizer_maintain(self):
        """When economizer enters maintain phase, fan activates."""
        engine = _make_automation_engine({
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "fan.attic",
        })
        engine._current_classification = _make_hot_classification()

        # indoor at/below comfort → maintain phase
        asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=75.0,
                windows_physically_open=True,
                current_hour=19,
            )
        )

        assert engine._economizer_phase == "maintain"
        fan_on_calls = _get_service_calls(engine, "fan", "turn_on")
        assert len(fan_on_calls) == 1
        assert fan_on_calls[0][0][2]["entity_id"] == "fan.attic"

    def test_fan_activates_with_economizer_maintain_savings_mode(self):
        """Savings mode also activates fan when entering maintain phase."""
        engine = _make_automation_engine({
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "fan.attic",
            "aggressive_savings": True,
        })
        engine._current_classification = _make_hot_classification()

        asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=80.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )

        assert engine._economizer_phase == "maintain"
        fan_on_calls = _get_service_calls(engine, "fan", "turn_on")
        assert len(fan_on_calls) == 1

    def test_fan_deactivates_with_economizer_off(self):
        """When economizer deactivates, fan deactivates too."""
        engine = _make_automation_engine({
            CONF_FAN_MODE: FAN_MODE_WHOLE_HOUSE,
            CONF_FAN_ENTITY: "fan.attic",
        })
        engine._current_classification = _make_hot_classification()
        engine._economizer_active = True
        engine._economizer_phase = "maintain"

        # Trigger deactivation: outdoor too warm
        asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=80.0,
                indoor_temp=76.0,
                windows_physically_open=True,
                current_hour=18,
            )
        )

        assert engine._economizer_active is False
        fan_off_calls = _get_service_calls(engine, "fan", "turn_off")
        assert len(fan_off_calls) == 1
        assert fan_off_calls[0][0][2]["entity_id"] == "fan.attic"

    def test_no_fan_calls_when_fan_disabled_in_economizer(self):
        """When fan_mode=disabled, economizer transitions make no fan service calls."""
        engine = _make_automation_engine()  # no fan config
        engine._current_classification = _make_hot_classification()

        asyncio.run(
            engine.check_window_cooling_opportunity(
                outdoor_temp=72.0,
                indoor_temp=75.0,
                windows_physically_open=True,
                current_hour=19,
            )
        )

        assert engine._economizer_phase == "maintain"
        # Only HVAC calls (set_hvac_mode) — no fan calls
        fan_calls = [
            c for c in engine.hass.services.async_call.call_args_list
            if c[0][0] in ("fan", "switch")
        ]
        assert len(fan_calls) == 0
