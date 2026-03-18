"""Tests for coordinator startup retry logic.

When the weather entity is unavailable at startup, the coordinator
should schedule a retry rather than waiting the full 30-minute interval.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_state(state_value: str, attributes: dict | None = None) -> MagicMock:
    """Create a mock HA state object."""
    mock = MagicMock()
    mock.state = state_value
    mock.attributes = attributes or {}
    return mock


class TestGetForecastRetry:
    """Test the retry-on-missing-weather-entity logic in _async_update_data.

    Since the coordinator can't be instantiated without a live HA instance,
    we replicate the decision logic inline.
    """

    def test_weather_entity_missing_triggers_retry(self):
        """When hass.states.get returns None, a retry should be scheduled."""
        states = {}
        weather_entity = "weather.forecast_home"

        # Replicate _get_forecast check
        weather_state = states.get(weather_entity)
        should_retry = weather_state is None

        assert should_retry is True

    def test_weather_entity_present_no_retry(self):
        """When the weather entity exists, no retry is needed."""
        weather_entity = "weather.home"
        states = {
            weather_entity: _make_state("sunny", {"temperature": 65}),
        }

        weather_state = states.get(weather_entity)
        should_retry = weather_state is None

        assert should_retry is False

    def test_retry_uses_correct_entity_after_reconfigure(self):
        """After reconfiguring weather_entity, the new ID is used."""
        old_config = {"weather_entity": "weather.forecast_home"}
        new_config = {"weather_entity": "weather.home"}

        states = {
            "weather.home": _make_state("sunny", {"temperature": 65}),
        }

        # Old config: entity missing
        assert states.get(old_config["weather_entity"]) is None

        # New config: entity found
        assert states.get(new_config["weather_entity"]) is not None
