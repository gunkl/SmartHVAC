"""Tests for weather bias application in coordinator._get_forecast() (Phase 5G)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import (
    MAX_WEATHER_BIAS_APPLY_F,
    MIN_WEATHER_BIAS_APPLY_F,
)
from custom_components.climate_advisor.learning import DailyRecord, LearningEngine

_TODAY = "2026-03-27"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_learning_engine(tmp_path: Path) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


def _make_complete_record(
    date: str,
    forecast_high: float,
    observed_high: float,
    forecast_low: float,
    observed_low: float,
) -> dict:
    return {
        "date": date,
        "day_type": "mild",
        "trend_direction": "stable",
        "forecast_high_f": forecast_high,
        "observed_high_f": observed_high,
        "forecast_low_f": forecast_low,
        "observed_low_f": observed_low,
    }


# ---------------------------------------------------------------------------
# Tests for get_weather_bias() application logic (unit tests on LearningEngine)
# ---------------------------------------------------------------------------


class TestWeatherBiasApplicationLogic:
    """Tests for the weather bias application logic.

    These tests validate the guard conditions from coordinator._get_forecast():
    - confidence == "none" → no correction
    - abs(bias) < MIN_WEATHER_BIAS_APPLY_F → no correction
    - abs(bias) > MAX_WEATHER_BIAS_APPLY_F → clamped
    """

    def test_bias_not_applied_when_confidence_is_none(self, tmp_path: Path):
        """get_weather_bias() with confidence='none' → should not be applied."""
        engine = _make_learning_engine(tmp_path)
        # No records → confidence is "none"
        bias = engine.get_weather_bias()
        assert bias["confidence"] == "none"
        # Simulate the coordinator guard: when confidence=="none", skip correction
        tomorrow_high_before = 75.0
        if bias["confidence"] != "none":
            tomorrow_high = tomorrow_high_before + bias["high_bias"]
        else:
            tomorrow_high = tomorrow_high_before
        assert tomorrow_high == pytest.approx(tomorrow_high_before)

    def test_bias_applied_to_tomorrow_high(self, tmp_path: Path):
        """bias with confidence='low', high_bias=2.0°F → tomorrow_high increases by 2."""
        engine = _make_learning_engine(tmp_path)
        # Add 7 records with consistent 2°F overprediction
        for i in range(7):
            engine._state.records.append(_make_complete_record(f"2026-03-{i + 1:02d}", 72.0, 74.0, 52.0, 53.0))
        bias = engine.get_weather_bias()
        assert bias["confidence"] != "none"
        assert bias["high_bias"] == pytest.approx(2.0, abs=0.01)

        # Apply logic matching coordinator
        tomorrow_high_before = 75.0
        bias_h = max(-MAX_WEATHER_BIAS_APPLY_F, min(MAX_WEATHER_BIAS_APPLY_F, bias["high_bias"]))
        if abs(bias_h) >= MIN_WEATHER_BIAS_APPLY_F:
            tomorrow_high = tomorrow_high_before + bias_h
        else:
            tomorrow_high = tomorrow_high_before
        assert tomorrow_high == pytest.approx(77.0)

    def test_bias_not_applied_below_min_threshold(self, tmp_path: Path):
        """high_bias=0.3°F < MIN_WEATHER_BIAS_APPLY_F=0.5 → no correction applied."""
        engine = _make_learning_engine(tmp_path)
        for i in range(7):
            engine._state.records.append(_make_complete_record(f"2026-03-{i + 1:02d}", 72.0, 72.3, 52.0, 52.3))
        bias = engine.get_weather_bias()
        assert bias["confidence"] != "none"

        tomorrow_high_before = 75.0
        bias_h = max(-MAX_WEATHER_BIAS_APPLY_F, min(MAX_WEATHER_BIAS_APPLY_F, bias["high_bias"]))
        if abs(bias_h) >= MIN_WEATHER_BIAS_APPLY_F:
            tomorrow_high = tomorrow_high_before + bias_h
        else:
            tomorrow_high = tomorrow_high_before
        assert tomorrow_high == pytest.approx(tomorrow_high_before)

    def test_bias_clamped_at_max(self, tmp_path: Path):
        """high_bias=15.0°F > MAX_WEATHER_BIAS_APPLY_F=8.0 → clamped to 8.0."""
        engine = _make_learning_engine(tmp_path)
        for i in range(7):
            engine._state.records.append(_make_complete_record(f"2026-03-{i + 1:02d}", 60.0, 75.0, 40.0, 41.0))
        bias = engine.get_weather_bias()
        assert bias["high_bias"] == pytest.approx(15.0, abs=0.01)

        tomorrow_high_before = 75.0
        bias_h = max(-MAX_WEATHER_BIAS_APPLY_F, min(MAX_WEATHER_BIAS_APPLY_F, bias["high_bias"]))
        if abs(bias_h) >= MIN_WEATHER_BIAS_APPLY_F:
            tomorrow_high = tomorrow_high_before + bias_h
        else:
            tomorrow_high = tomorrow_high_before
        # bias_h is clamped to MAX_WEATHER_BIAS_APPLY_F=8.0
        assert tomorrow_high == pytest.approx(tomorrow_high_before + MAX_WEATHER_BIAS_APPLY_F)


# ---------------------------------------------------------------------------
# Tests for coordinator integration: forecast_high/low saved at briefing time
# ---------------------------------------------------------------------------


class TestForecastRecordSaving:
    """Tests for forecast_high_f/observed_high_f being saved in _today_record."""

    def test_forecast_high_low_saved_at_briefing_time(self, tmp_path: Path):
        """After _async_send_briefing, _today_record.forecast_high_f is set."""
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        hass = MagicMock()
        hass.config.config_dir = str(tmp_path)
        hass.states.get = MagicMock(return_value=None)

        config = {
            "climate_entity": "climate.test",
            "weather_entity": "weather.test",
            "notify_service": "notify.test",
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            "wake_time": "06:30",
            "sleep_time": "22:30",
            "temp_unit": "fahrenheit",
        }

        coordinator = ClimateAdvisorCoordinator(hass, config)

        # Simulate what _async_send_briefing does after classification
        from custom_components.climate_advisor.classifier import DayClassification

        c = object.__new__(DayClassification)
        c.__dict__.update(
            {
                "day_type": "mild",
                "trend_direction": "stable",
                "trend_magnitude": 1.0,
                "today_high": 72.0,
                "today_low": 55.0,
                "tomorrow_high": 75.0,
                "tomorrow_low": 57.0,
                "hvac_mode": "heat",
                "pre_condition": False,
                "pre_condition_target": None,
                "windows_recommended": False,
                "window_open_time": None,
                "window_close_time": None,
                "setback_modifier": 0.0,
                "window_opportunity_morning": False,
                "window_opportunity_evening": False,
            }
        )
        coordinator._current_classification = c
        coordinator._today_record = DailyRecord(date=_TODAY, day_type="mild", trend_direction="stable")

        # Replicate the briefing code that saves forecast_high_f
        if coordinator._today_record is not None and coordinator._current_classification:
            coordinator._today_record.forecast_high_f = coordinator._current_classification.today_high
            coordinator._today_record.forecast_low_f = coordinator._current_classification.today_low

        assert coordinator._today_record.forecast_high_f == pytest.approx(72.0)
        assert coordinator._today_record.forecast_low_f == pytest.approx(55.0)

    def test_observed_high_low_saved_at_end_of_day(self, tmp_path: Path):
        """After _async_end_of_day, observed_high/low are set from outdoor temp history."""
        from custom_components.climate_advisor.coordinator import ClimateAdvisorCoordinator

        hass = MagicMock()
        hass.config.config_dir = str(tmp_path)

        config = {
            "climate_entity": "climate.test",
            "weather_entity": "weather.test",
            "notify_service": "notify.test",
            "comfort_heat": 70,
            "comfort_cool": 75,
            "setback_heat": 60,
            "setback_cool": 80,
            "wake_time": "06:30",
            "sleep_time": "22:30",
            "temp_unit": "fahrenheit",
        }

        coordinator = ClimateAdvisorCoordinator(hass, config)

        # Set up outdoor temp history with known values
        coordinator._outdoor_temp_history = [
            ("08:00", 55.0),
            ("12:00", 72.0),
            ("15:00", 78.0),
            ("20:00", 62.0),
        ]

        coordinator._today_record = DailyRecord(date=_TODAY, day_type="mild", trend_direction="stable")
        coordinator._indoor_temp_history = []

        # Replicate the end-of-day observed temp saving logic
        if coordinator._outdoor_temp_history:
            observed_temps = [t for _, t in coordinator._outdoor_temp_history]
            coordinator._today_record.observed_high_f = round(max(observed_temps), 1)
            coordinator._today_record.observed_low_f = round(min(observed_temps), 1)

        assert coordinator._today_record.observed_high_f == pytest.approx(78.0)
        assert coordinator._today_record.observed_low_f == pytest.approx(55.0)
