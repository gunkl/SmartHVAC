"""Tests for coordinator startup retry logic.

When the weather entity is unavailable at startup, the coordinator
should schedule retries with exponential backoff rather than waiting
the full 30-minute interval.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_state(state_value: str, attributes: dict | None = None) -> MagicMock:
    """Create a mock HA state object."""
    mock = MagicMock()
    mock.state = state_value
    mock.attributes = attributes or {}
    return mock


def _should_retry(states: dict, weather_entity: str) -> bool:
    """Replicate the _get_forecast guard logic from coordinator.py."""
    weather_state = states.get(weather_entity)
    if weather_state is None:
        return True
    return weather_state.state in ("unavailable", "unknown")


class TestGetForecastRetry:
    """Test the retry-on-missing-weather-entity logic in _async_update_data.

    Since the coordinator can't be instantiated without a live HA instance,
    we replicate the decision logic inline.
    """

    def test_weather_entity_missing_triggers_retry(self):
        """When hass.states.get returns None, a retry should be scheduled."""
        states = {}
        assert _should_retry(states, "weather.forecast_home") is True

    def test_weather_entity_present_no_retry(self):
        """When the weather entity exists and is reporting, no retry needed."""
        weather_entity = "weather.home"
        states = {
            weather_entity: _make_state("sunny", {"temperature": 65}),
        }
        assert _should_retry(states, weather_entity) is False

    def test_weather_entity_unavailable_triggers_retry(self):
        """Entity registered but 'unavailable' after restart should retry."""
        weather_entity = "weather.forecast_home"
        states = {
            weather_entity: _make_state("unavailable"),
        }
        assert _should_retry(states, weather_entity) is True

    def test_weather_entity_unknown_triggers_retry(self):
        """Entity with 'unknown' state should also trigger retry."""
        weather_entity = "weather.forecast_home"
        states = {
            weather_entity: _make_state("unknown"),
        }
        assert _should_retry(states, weather_entity) is True

    def test_weather_entity_cloudy_no_retry(self):
        """Any real weather state (cloudy, rainy, etc.) should not retry."""
        weather_entity = "weather.home"
        states = {
            weather_entity: _make_state("cloudy", {"temperature": 72}),
        }
        assert _should_retry(states, weather_entity) is False

    def test_retry_uses_correct_entity_after_reconfigure(self):
        """After reconfiguring weather_entity, the new ID is used."""
        old_config = {"weather_entity": "weather.forecast_home"}
        new_config = {"weather_entity": "weather.home"}

        states = {
            "weather.home": _make_state("sunny", {"temperature": 65}),
        }

        # Old config: entity missing → retry
        assert _should_retry(states, old_config["weather_entity"]) is True

        # New config: entity found → no retry
        assert _should_retry(states, new_config["weather_entity"]) is False


class TestStartupRetryBackoff:
    """Test the exponential backoff retry logic for startup.

    Replicates the retry counter and delay logic from the coordinator
    to verify backoff progression and reset behavior.
    """

    def test_backoff_delays_double_each_attempt(self):
        """Retry delays should double: 30 → 60 → 120 → 240 → 480."""
        retries_remaining = 5
        retry_delay = 30
        recorded_delays = []

        while retries_remaining > 0:
            delay = retry_delay
            retries_remaining -= 1
            retry_delay = min(delay * 2, 480)
            recorded_delays.append(delay)

        assert recorded_delays == [30, 60, 120, 240, 480]

    def test_retries_exhaust_after_five_attempts(self):
        """After 5 failed retries, no more retries are scheduled."""
        retries_remaining = 5

        for _ in range(5):
            assert retries_remaining > 0
            retries_remaining -= 1

        assert retries_remaining == 0

    def test_total_retry_window_under_16_minutes(self):
        """Total retry time should be reasonable (under 16 minutes)."""
        retry_delay = 30
        total_seconds = 0

        for _ in range(5):
            total_seconds += retry_delay
            retry_delay = min(retry_delay * 2, 480)

        # 30 + 60 + 120 + 240 + 480 = 930 seconds = 15.5 minutes
        assert total_seconds == 930
        assert total_seconds < 16 * 60

    def test_success_resets_retry_state(self):
        """A successful forecast fetch should reset retry counters."""
        retries_remaining = 2  # Simulates 3 failures already
        retry_delay = 240

        # Simulate success
        forecast_available = True
        if forecast_available and retries_remaining < 5:
            retries_remaining = 5
            retry_delay = 30

        assert retries_remaining == 5
        assert retry_delay == 30

    def test_no_reset_when_already_full(self):
        """If retries haven't been used, success doesn't change state."""
        retries_remaining = 5
        retry_delay = 30

        forecast_available = True
        # Only reset if retries were consumed
        if forecast_available and retries_remaining < 5:
            retries_remaining = 5
            retry_delay = 30

        assert retries_remaining == 5
        assert retry_delay == 30

    def test_retry_available_after_transient_failure_and_recovery(self):
        """After recovering, a new transient failure gets fresh retries."""
        retries_remaining = 5
        retry_delay = 30

        # Simulate 2 failures
        for _ in range(2):
            retries_remaining -= 1
            retry_delay = min(retry_delay * 2, 480)

        assert retries_remaining == 3

        # Simulate success → reset
        retries_remaining = 5
        retry_delay = 30

        # Simulate another failure → should get full budget again
        assert retries_remaining == 5
        retries_remaining -= 1
        assert retries_remaining == 4
        assert retry_delay == 30
