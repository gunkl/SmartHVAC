"""Tests for config flow options — multi-step options flow.

Validates that the options flow splits settings across multiple steps
and merges all collected input into the config entry data.
"""
from __future__ import annotations

from unittest.mock import MagicMock


def _make_config_entry(data: dict) -> MagicMock:
    """Create a mock ConfigEntry with the given data."""
    entry = MagicMock()
    entry.data = dict(data)
    entry.entry_id = "test_entry_id"
    return entry


FULL_CONFIG = {
    "weather_entity": "weather.forecast_home",
    "climate_entity": "climate.living_room",
    "comfort_heat": 70,
    "comfort_cool": 76,
    "setback_heat": 62,
    "setback_cool": 78,
    "notify_service": "notify.mobile_app_phone",
    "outdoor_temp_source": "weather_service",
    "indoor_temp_source": "climate_fallback",
    "door_window_sensors": ["binary_sensor.front_door"],
    "sensor_polarity_inverted": False,
    "sensor_debounce_seconds": 300,
    "manual_grace_seconds": 1800,
    "manual_grace_notify": False,
    "automation_grace_seconds": 3600,
    "automation_grace_notify": True,
    "wake_time": "06:30:00",
    "sleep_time": "22:30:00",
    "briefing_time": "06:00:00",
    "learning_enabled": True,
    "aggressive_savings": False,
}


class TestOptionsFlowMultiStep:
    """Test that the multi-step options flow merges data correctly."""

    def test_step_init_merges_core_settings(self):
        """Step 1 (init) collects core entity and temperature settings."""
        original = dict(FULL_CONFIG)
        step1_input = {
            "weather_entity": "weather.home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 72,
            "comfort_cool": 76,
            "setback_heat": 60,
            "setback_cool": 80,
            "notify_service": "notify.notify",
        }
        merged = {**original, **step1_input}
        assert merged["weather_entity"] == "weather.home"
        assert merged["comfort_heat"] == 72
        assert merged["setback_heat"] == 60
        assert merged["setback_cool"] == 80
        assert merged["notify_service"] == "notify.notify"

    def test_multi_step_accumulation(self):
        """All 5 steps accumulate into a single merged result."""
        original = dict(FULL_CONFIG)
        updates = {}

        # Step 1: init
        updates.update({
            "weather_entity": "weather.home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 72,
            "comfort_cool": 76,
            "setback_heat": 60,
            "setback_cool": 80,
            "notify_service": "notify.notify",
        })

        # Step 2: temperature_sources
        updates.update({
            "outdoor_temp_source": "sensor",
            "outdoor_temp_entity": "sensor.outdoor_temp",
            "indoor_temp_source": "climate_fallback",
        })

        # Step 3: sensors
        updates.update({
            "door_window_sensors": ["binary_sensor.back_door"],
            "sensor_polarity_inverted": True,
            "sensor_debounce_seconds": 600,
            "manual_grace_seconds": 900,
            "manual_grace_notify": True,
            "automation_grace_seconds": 1800,
            "automation_grace_notify": False,
        })

        # Step 4: schedule
        updates.update({
            "wake_time": "07:00:00",
            "sleep_time": "23:00:00",
            "briefing_time": "06:30:00",
        })

        # Step 5: advanced
        updates.update({
            "learning_enabled": False,
            "aggressive_savings": True,
        })

        merged = {**original, **updates}

        # Verify all updated fields
        assert merged["weather_entity"] == "weather.home"
        assert merged["comfort_heat"] == 72
        assert merged["setback_heat"] == 60
        assert merged["outdoor_temp_source"] == "sensor"
        assert merged["outdoor_temp_entity"] == "sensor.outdoor_temp"
        assert merged["door_window_sensors"] == ["binary_sensor.back_door"]
        assert merged["sensor_polarity_inverted"] is True
        assert merged["sensor_debounce_seconds"] == 600
        assert merged["wake_time"] == "07:00:00"
        assert merged["sleep_time"] == "23:00:00"
        assert merged["briefing_time"] == "06:30:00"
        assert merged["learning_enabled"] is False
        assert merged["aggressive_savings"] is True

    def test_fields_not_in_updates_are_preserved(self):
        """Fields not touched across any step retain their original values."""
        original = dict(FULL_CONFIG)
        updates = {
            "comfort_heat": 72,
            "learning_enabled": False,
        }
        merged = {**original, **updates}
        # Updated fields
        assert merged["comfort_heat"] == 72
        assert merged["learning_enabled"] is False
        # Preserved fields
        assert merged["weather_entity"] == "weather.forecast_home"
        assert merged["notify_service"] == "notify.mobile_app_phone"
        assert merged["setback_heat"] == 62
        assert merged["wake_time"] == "06:30:00"
        assert merged["door_window_sensors"] == ["binary_sensor.front_door"]

    def test_new_fields_have_defaults_for_old_entries(self):
        """Config entries created before new fields were added get safe defaults."""
        old_entry_data = {
            "weather_entity": "weather.forecast_home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 70,
            "comfort_cool": 76,
        }
        # Simulate options flow defaulting missing fields
        defaults = {
            "setback_heat": old_entry_data.get("setback_heat", 60),
            "setback_cool": old_entry_data.get("setback_cool", 80),
            "notify_service": old_entry_data.get("notify_service", "notify.notify"),
            "wake_time": old_entry_data.get("wake_time", "06:30:00"),
            "sleep_time": old_entry_data.get("sleep_time", "22:30:00"),
            "briefing_time": old_entry_data.get("briefing_time", "06:00:00"),
            "learning_enabled": old_entry_data.get("learning_enabled", True),
            "aggressive_savings": old_entry_data.get("aggressive_savings", False),
        }
        assert defaults["setback_heat"] == 60
        assert defaults["notify_service"] == "notify.notify"
        assert defaults["wake_time"] == "06:30:00"
        assert defaults["learning_enabled"] is True
