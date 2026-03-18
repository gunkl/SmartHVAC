"""Tests for door/window sensor group resolution, polarity, debounce, and grace periods.

These tests validate the algorithms used by the coordinator for resolving
binary_sensor groups and interpreting sensor polarity. Since the coordinator
cannot be instantiated without a live Home Assistant instance, we replicate
the logic inline and test it directly.

For debounce and grace period tests, we test the AutomationEngine directly
with mocked HA dependencies.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.const import (
    CONF_AUTOMATION_GRACE_NOTIFY,
    CONF_AUTOMATION_GRACE_PERIOD,
    CONF_MANUAL_GRACE_NOTIFY,
    CONF_MANUAL_GRACE_PERIOD,
    CONF_SENSOR_DEBOUNCE,
    CONF_SENSOR_POLARITY_INVERTED,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
)


# ---------------------------------------------------------------------------
# Replicate coordinator logic for unit testing
# ---------------------------------------------------------------------------

def _resolve_monitored_sensors(
    door_window_sensors: list[str],
) -> list[str]:
    """Resolve all monitored sensor entity IDs.

    This mirrors ClimateAdvisorCoordinator._resolve_monitored_sensors().
    Binary sensor groups are themselves binary_sensor entities, so no
    expansion is needed — they are monitored directly.
    """
    return list(door_window_sensors)


def _is_sensor_open(
    hass_states_get,
    entity_id: str,
    polarity_inverted: bool,
) -> bool:
    """Check if a sensor is open, respecting polarity.

    This mirrors ClimateAdvisorCoordinator._is_sensor_open().
    """
    state = hass_states_get(entity_id)
    if not state:
        return False
    if polarity_inverted:
        return state.state == "off"
    return state.state == "on"


def _make_state(state_value: str, attributes: dict | None = None) -> MagicMock:
    """Create a mock HA state object."""
    mock = MagicMock()
    mock.state = state_value
    mock.attributes = attributes or {}
    return mock


def _states_getter(state_map: dict[str, MagicMock]):
    """Return a callable that looks up entity states from a dict."""
    return lambda eid: state_map.get(eid)


# ---------------------------------------------------------------------------
# Group resolution tests
# ---------------------------------------------------------------------------

class TestResolveMonitoredSensors:
    """Tests for sensor resolution logic.

    Binary sensor groups in HA are themselves binary_sensor entities, so they
    appear in the single door_window_sensors list alongside individual sensors.
    """

    def test_returns_configured_sensors(self):
        result = _resolve_monitored_sensors(
            ["binary_sensor.front_door", "binary_sensor.back_door"],
        )
        assert result == ["binary_sensor.front_door", "binary_sensor.back_door"]

    def test_empty_config(self):
        result = _resolve_monitored_sensors([])
        assert result == []

    def test_includes_group_entities(self):
        """Binary sensor groups are binary_sensor entities and included directly."""
        result = _resolve_monitored_sensors(
            ["binary_sensor.front_door", "binary_sensor.all_windows"],
        )
        assert "binary_sensor.all_windows" in result
        assert "binary_sensor.front_door" in result

    def test_returns_copy_not_original(self):
        """Returned list should be a copy, not the original."""
        original = ["binary_sensor.a"]
        result = _resolve_monitored_sensors(original)
        result.append("binary_sensor.b")
        assert len(original) == 1


# ---------------------------------------------------------------------------
# Polarity tests
# ---------------------------------------------------------------------------

class TestIsSensorOpen:
    """Tests for polarity-aware sensor open check."""

    def test_standard_on_is_open(self):
        get = _states_getter({"binary_sensor.door": _make_state("on")})
        assert _is_sensor_open(get, "binary_sensor.door", False) is True

    def test_standard_off_is_closed(self):
        get = _states_getter({"binary_sensor.door": _make_state("off")})
        assert _is_sensor_open(get, "binary_sensor.door", False) is False

    def test_inverted_off_is_open(self):
        get = _states_getter({"binary_sensor.door": _make_state("off")})
        assert _is_sensor_open(get, "binary_sensor.door", True) is True

    def test_inverted_on_is_closed(self):
        get = _states_getter({"binary_sensor.door": _make_state("on")})
        assert _is_sensor_open(get, "binary_sensor.door", True) is False

    def test_unavailable_sensor_is_not_open(self):
        get = _states_getter({})
        assert _is_sensor_open(get, "binary_sensor.missing", False) is False

    def test_unavailable_sensor_inverted_is_not_open(self):
        get = _states_getter({})
        assert _is_sensor_open(get, "binary_sensor.missing", True) is False


# ---------------------------------------------------------------------------
# All-closed logic tests
# ---------------------------------------------------------------------------

class TestAllClosedCheck:
    """Tests for the all-closed check across multiple sensors with polarity."""

    def test_all_closed_standard(self):
        get = _states_getter({
            "binary_sensor.a": _make_state("off"),
            "binary_sensor.b": _make_state("off"),
        })
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        all_closed = all(not _is_sensor_open(get, s, False) for s in sensors)
        assert all_closed is True

    def test_one_open_standard(self):
        get = _states_getter({
            "binary_sensor.a": _make_state("off"),
            "binary_sensor.b": _make_state("on"),
        })
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        all_closed = all(not _is_sensor_open(get, s, False) for s in sensors)
        assert all_closed is False

    def test_all_closed_inverted(self):
        get = _states_getter({
            "binary_sensor.a": _make_state("on"),
            "binary_sensor.b": _make_state("on"),
        })
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        all_closed = all(not _is_sensor_open(get, s, True) for s in sensors)
        assert all_closed is True

    def test_one_open_inverted(self):
        get = _states_getter({
            "binary_sensor.a": _make_state("on"),
            "binary_sensor.b": _make_state("off"),  # off = open when inverted
        })
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        all_closed = all(not _is_sensor_open(get, s, True) for s in sensors)
        assert all_closed is False


# ---------------------------------------------------------------------------
# Config migration tests
# ---------------------------------------------------------------------------

class TestConfigMigration:
    """Tests for v2->v3 config migration defaults."""

    def test_v2_config_gets_polarity_default(self):
        v2_data = {
            "door_window_sensors": ["binary_sensor.front_door"],
            "wake_time": "06:30",
        }
        new_data = {**v2_data}
        new_data.pop("door_window_groups", None)
        new_data.setdefault(CONF_SENSOR_POLARITY_INVERTED, False)

        assert "door_window_groups" not in new_data
        assert new_data[CONF_SENSOR_POLARITY_INVERTED] is False
        assert new_data["door_window_sensors"] == ["binary_sensor.front_door"]

    def test_v2_migration_removes_legacy_groups_key(self):
        v2_data = {
            "door_window_sensors": ["binary_sensor.front_door"],
            "door_window_groups": ["group.old_group"],
        }
        new_data = {**v2_data}
        new_data.pop("door_window_groups", None)
        new_data.setdefault(CONF_SENSOR_POLARITY_INVERTED, False)

        assert "door_window_groups" not in new_data

    def test_v2_config_preserves_polarity_if_set(self):
        v2_data = {
            CONF_SENSOR_POLARITY_INVERTED: True,
        }
        new_data = {**v2_data}
        new_data.setdefault(CONF_SENSOR_POLARITY_INVERTED, False)

        assert new_data[CONF_SENSOR_POLARITY_INVERTED] is True


# ---------------------------------------------------------------------------
# Helpers for AutomationEngine tests
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


# ---------------------------------------------------------------------------
# Debounce constant/config tests
# ---------------------------------------------------------------------------

class TestSensorDebounceConfig:
    """Tests for debounce configuration defaults and values."""

    def test_default_debounce_is_five_minutes(self):
        assert DEFAULT_SENSOR_DEBOUNCE_SECONDS == 300

    def test_config_key_name(self):
        assert CONF_SENSOR_DEBOUNCE == "sensor_debounce_seconds"

    def test_engine_reads_debounce_from_config(self):
        engine = _make_automation_engine({CONF_SENSOR_DEBOUNCE: 120})
        assert engine.config[CONF_SENSOR_DEBOUNCE] == 120

    def test_engine_uses_default_when_not_configured(self):
        engine = _make_automation_engine()
        debounce = engine.config.get(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS)
        assert debounce == 300


# ---------------------------------------------------------------------------
# Grace period — AutomationEngine tests
# ---------------------------------------------------------------------------

class TestGracePeriodState:
    """Tests for grace period state management on the AutomationEngine."""

    def test_initial_state_no_grace(self):
        engine = _make_automation_engine()
        assert engine._grace_active is False
        assert engine._last_resume_source is None
        assert engine._manual_grace_cancel is None
        assert engine._automation_grace_cancel is None

    def test_is_paused_by_door_property(self):
        engine = _make_automation_engine()
        assert engine.is_paused_by_door is False
        engine._paused_by_door = True
        assert engine.is_paused_by_door is True


class TestHandleDoorWindowOpenWithGrace:
    """Tests for handle_door_window_open respecting grace periods."""

    def test_skips_pause_when_grace_active(self):
        engine = _make_automation_engine()
        engine._grace_active = True
        engine._last_resume_source = "automation"

        # Set up state so it would normally pause
        engine.hass.states.get.return_value = _make_state("heat")

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        # Should NOT have paused
        assert engine._paused_by_door is False
        engine.hass.services.async_call.assert_not_called()

    def test_pauses_when_no_grace(self):
        engine = _make_automation_engine()
        engine.hass.states.get.return_value = _make_state("heat")

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ):
            asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        assert engine._paused_by_door is True

    def test_idempotent_when_already_paused(self):
        engine = _make_automation_engine()
        engine._paused_by_door = True

        asyncio.run(engine.handle_door_window_open("binary_sensor.front_door"))

        # Should not have called any service
        engine.hass.services.async_call.assert_not_called()


class TestHandleAllDoorsWindowsClosed:
    """Tests for handle_all_doors_windows_closed starting grace periods."""

    def test_resume_starts_automation_grace(self):
        engine = _make_automation_engine()
        engine._paused_by_door = True
        engine._pre_pause_mode = "heat"

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ) as mock_call_later:
            mock_call_later.return_value = MagicMock()  # cancel callback
            asyncio.run(engine.handle_all_doors_windows_closed())

        assert engine._paused_by_door is False
        assert engine._grace_active is True
        assert engine._last_resume_source == "automation"
        mock_call_later.assert_called_once()

    def test_no_resume_when_not_paused(self):
        engine = _make_automation_engine()
        engine._paused_by_door = False

        asyncio.run(engine.handle_all_doors_windows_closed())

        engine.hass.services.async_call.assert_not_called()

    def test_resume_restores_pre_pause_mode(self):
        engine = _make_automation_engine()
        engine._paused_by_door = True
        engine._pre_pause_mode = "cool"

        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            return_value=MagicMock(),
        ):
            asyncio.run(engine.handle_all_doors_windows_closed())

        # Should have called set_hvac_mode with "cool"
        engine.hass.services.async_call.assert_any_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": "climate.thermostat", "hvac_mode": "cool"},
        )


class TestManualOverrideDuringPause:
    """Tests for handle_manual_override_during_pause."""

    def test_starts_manual_grace(self):
        engine = _make_automation_engine()
        engine._paused_by_door = True
        engine._pre_pause_mode = "heat"

        with patch(
            "custom_components.climate_advisor.automation.async_call_later",
            return_value=MagicMock(),
        ):
            asyncio.run(engine.handle_manual_override_during_pause())

        assert engine._paused_by_door is False
        assert engine._pre_pause_mode is None
        assert engine._grace_active is True
        assert engine._last_resume_source == "manual"

    def test_noop_when_not_paused(self):
        engine = _make_automation_engine()
        engine._paused_by_door = False

        asyncio.run(engine.handle_manual_override_during_pause())

        assert engine._grace_active is False


class TestGracePeriodDuration:
    """Tests for configurable grace period durations."""

    def test_manual_grace_uses_config_value(self):
        engine = _make_automation_engine({CONF_MANUAL_GRACE_PERIOD: 600})
        engine._paused_by_door = True

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ) as mock_call_later:
            mock_call_later.return_value = MagicMock()
            asyncio.run(engine.handle_manual_override_during_pause())

        # Second arg to async_call_later is the duration
        mock_call_later.assert_called_once()
        call_args = mock_call_later.call_args
        assert call_args[0][1] == 600  # duration

    def test_automation_grace_uses_config_value(self):
        engine = _make_automation_engine({CONF_AUTOMATION_GRACE_PERIOD: 1200})
        engine._paused_by_door = True
        engine._pre_pause_mode = "heat"

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ) as mock_call_later:
            mock_call_later.return_value = MagicMock()
            asyncio.run(engine.handle_all_doors_windows_closed())

        mock_call_later.assert_called_once()
        call_args = mock_call_later.call_args
        assert call_args[0][1] == 1200

    def test_zero_duration_disables_grace(self):
        engine = _make_automation_engine({CONF_MANUAL_GRACE_PERIOD: 0})
        engine._paused_by_door = True

        with patch(
            "custom_components.climate_advisor.automation.async_call_later"
        ) as mock_call_later:
            asyncio.run(engine.handle_manual_override_during_pause())

        # Should not have started a timer
        mock_call_later.assert_not_called()
        assert engine._grace_active is False

    def test_default_manual_grace_is_30_min(self):
        assert DEFAULT_MANUAL_GRACE_SECONDS == 1800

    def test_default_automation_grace_is_60_min(self):
        assert DEFAULT_AUTOMATION_GRACE_SECONDS == 3600


class TestGracePeriodNotifications:
    """Tests for grace period notification toggles."""

    def test_manual_grace_notify_default_off(self):
        engine = _make_automation_engine()
        assert engine.config.get(CONF_MANUAL_GRACE_NOTIFY, False) is False

    def test_automation_grace_notify_default_on(self):
        engine = _make_automation_engine()
        # Default is True when not in config
        assert engine.config.get(CONF_AUTOMATION_GRACE_NOTIFY, True) is True

    def test_manual_grace_notify_configurable(self):
        engine = _make_automation_engine({CONF_MANUAL_GRACE_NOTIFY: True})
        assert engine.config[CONF_MANUAL_GRACE_NOTIFY] is True

    def test_automation_grace_notify_configurable(self):
        engine = _make_automation_engine({CONF_AUTOMATION_GRACE_NOTIFY: False})
        assert engine.config[CONF_AUTOMATION_GRACE_NOTIFY] is False


# ---------------------------------------------------------------------------
# Timer cleanup tests
# ---------------------------------------------------------------------------

class TestTimerCleanup:
    """Tests for timer cleanup on engine disposal."""

    def test_cleanup_cancels_grace_timers(self):
        engine = _make_automation_engine()
        mock_manual_cancel = MagicMock()
        mock_auto_cancel = MagicMock()
        engine._manual_grace_cancel = mock_manual_cancel
        engine._automation_grace_cancel = mock_auto_cancel
        engine._grace_active = True

        engine.cleanup()

        mock_manual_cancel.assert_called_once()
        mock_auto_cancel.assert_called_once()
        assert engine._grace_active is False

    def test_cleanup_handles_no_active_timers(self):
        engine = _make_automation_engine()
        # Should not raise
        engine.cleanup()
        assert engine._grace_active is False

    def test_cancel_grace_timers_resets_state(self):
        engine = _make_automation_engine()
        engine._grace_active = True
        engine._last_resume_source = "manual"
        engine._manual_grace_cancel = MagicMock()

        engine._cancel_grace_timers()

        assert engine._grace_active is False
        assert engine._last_resume_source is None
        assert engine._manual_grace_cancel is None


# ---------------------------------------------------------------------------
# Config migration v3 → v4 tests
# ---------------------------------------------------------------------------

class TestConfigMigrationV3ToV4:
    """Tests for v3->v4 config migration defaults."""

    def test_v3_config_gets_new_defaults(self):
        v3_data = {
            "door_window_sensors": ["binary_sensor.front_door"],
            "door_window_groups": [],
            CONF_SENSOR_POLARITY_INVERTED: False,
        }
        new_data = {**v3_data}
        new_data.setdefault(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS)
        new_data.setdefault(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS)
        new_data.setdefault(CONF_MANUAL_GRACE_NOTIFY, False)
        new_data.setdefault(CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS)
        new_data.setdefault(CONF_AUTOMATION_GRACE_NOTIFY, True)

        assert new_data[CONF_SENSOR_DEBOUNCE] == 300
        assert new_data[CONF_MANUAL_GRACE_PERIOD] == 1800
        assert new_data[CONF_MANUAL_GRACE_NOTIFY] is False
        assert new_data[CONF_AUTOMATION_GRACE_PERIOD] == 3600
        assert new_data[CONF_AUTOMATION_GRACE_NOTIFY] is True
        # Existing keys preserved
        assert new_data["door_window_sensors"] == ["binary_sensor.front_door"]
        assert new_data[CONF_SENSOR_POLARITY_INVERTED] is False

    def test_v3_config_preserves_custom_values(self):
        v3_data = {
            CONF_SENSOR_DEBOUNCE: 120,
            CONF_MANUAL_GRACE_PERIOD: 900,
            CONF_MANUAL_GRACE_NOTIFY: True,
            CONF_AUTOMATION_GRACE_PERIOD: 1800,
            CONF_AUTOMATION_GRACE_NOTIFY: False,
        }
        new_data = {**v3_data}
        new_data.setdefault(CONF_SENSOR_DEBOUNCE, DEFAULT_SENSOR_DEBOUNCE_SECONDS)
        new_data.setdefault(CONF_MANUAL_GRACE_PERIOD, DEFAULT_MANUAL_GRACE_SECONDS)
        new_data.setdefault(CONF_MANUAL_GRACE_NOTIFY, False)
        new_data.setdefault(CONF_AUTOMATION_GRACE_PERIOD, DEFAULT_AUTOMATION_GRACE_SECONDS)
        new_data.setdefault(CONF_AUTOMATION_GRACE_NOTIFY, True)

        assert new_data[CONF_SENSOR_DEBOUNCE] == 120
        assert new_data[CONF_MANUAL_GRACE_PERIOD] == 900
        assert new_data[CONF_MANUAL_GRACE_NOTIFY] is True
        assert new_data[CONF_AUTOMATION_GRACE_PERIOD] == 1800
        assert new_data[CONF_AUTOMATION_GRACE_NOTIFY] is False
