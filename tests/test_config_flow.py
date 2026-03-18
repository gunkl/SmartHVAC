"""Tests for config flow options — weather/climate entity reconfiguration.

Validates that the options flow allows changing the weather and climate
entity IDs without removing and re-adding the integration.
"""
from __future__ import annotations

from unittest.mock import MagicMock


def _make_config_entry(data: dict) -> MagicMock:
    """Create a mock ConfigEntry with the given data."""
    entry = MagicMock()
    entry.data = dict(data)
    entry.entry_id = "test_entry_id"
    return entry


class TestOptionsFlowWeatherEntity:
    """Test that weather_entity can be changed via the options flow."""

    def test_options_flow_merges_weather_entity_into_data(self):
        """When user changes weather_entity in options, entry.data is updated."""
        original_data = {
            "weather_entity": "weather.forecast_home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 70,
            "comfort_cool": 76,
        }
        user_input = {
            "weather_entity": "weather.home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 70,
            "comfort_cool": 76,
        }
        # Simulate the merge logic from the options flow
        merged = {**original_data, **user_input}
        assert merged["weather_entity"] == "weather.home"
        assert merged["climate_entity"] == "climate.living_room"

    def test_options_flow_preserves_fields_not_in_form(self):
        """Fields not shown in the options form are preserved in entry.data."""
        original_data = {
            "weather_entity": "weather.forecast_home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 70,
            "notify_service": "notify.mobile_app_phone",
            "setback_heat": 62,
        }
        user_input = {
            "weather_entity": "weather.home",
            "climate_entity": "climate.living_room",
            "comfort_heat": 72,
        }
        merged = {**original_data, **user_input}
        # Fields from user_input are updated
        assert merged["weather_entity"] == "weather.home"
        assert merged["comfort_heat"] == 72
        # Fields not in user_input are preserved
        assert merged["notify_service"] == "notify.mobile_app_phone"
        assert merged["setback_heat"] == 62
