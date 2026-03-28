"""Tests for LearningEngine.get_thermal_model() and get_weather_bias() (Phase 5G)."""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.climate_advisor.learning import LearningEngine, LearningState

_TODAY = "2026-03-27"


def _make_engine(tmp_path: Path) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


def _make_heat_obs(rate: float = 2.0) -> dict:
    return {
        "timestamp": f"{_TODAY}T10:00:00",
        "date": _TODAY,
        "hvac_mode": "heat",
        "session_minutes": 30.0,
        "rate_f_per_hour": rate,
        "outdoor_temp_f": 40.0,
        "start_indoor_f": 65.0,
        "end_indoor_f": 66.0,
    }


def _make_cool_obs(rate: float = 3.0) -> dict:
    return {
        "timestamp": f"{_TODAY}T14:00:00",
        "date": _TODAY,
        "hvac_mode": "cool",
        "session_minutes": 30.0,
        "rate_f_per_hour": rate,
        "outdoor_temp_f": 90.0,
        "start_indoor_f": 76.0,
        "end_indoor_f": 75.0,
    }


def _make_daily_record(
    date: str,
    forecast_high: float | None = None,
    observed_high: float | None = None,
    forecast_low: float | None = None,
    observed_low: float | None = None,
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
# TestThermalModelComputation
# ---------------------------------------------------------------------------


class TestThermalModelComputation:
    """Tests for get_thermal_model()."""

    def test_none_confidence_when_no_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        model = engine.get_thermal_model()
        assert model["confidence"] == "none"
        assert model["heating_rate_f_per_hour"] is None
        assert model["cooling_rate_f_per_hour"] is None

    def test_low_confidence_at_5_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        for _ in range(5):
            engine._state.thermal_observations.append(_make_heat_obs())
        model = engine.get_thermal_model()
        assert model["confidence"] == "low"

    def test_medium_confidence_at_10_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        for _ in range(10):
            engine._state.thermal_observations.append(_make_heat_obs())
        model = engine.get_thermal_model()
        assert model["confidence"] == "medium"

    def test_high_confidence_at_20_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        for _ in range(20):
            engine._state.thermal_observations.append(_make_heat_obs())
        model = engine.get_thermal_model()
        assert model["confidence"] == "high"

    def test_heating_rate_is_mean_of_heat_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        rates = [2.0, 4.0, 6.0]
        for r in rates:
            engine._state.thermal_observations.append(_make_heat_obs(rate=r))
        model = engine.get_thermal_model()
        expected = sum(rates) / len(rates)
        assert model["heating_rate_f_per_hour"] == pytest.approx(expected, abs=0.01)

    def test_cooling_rate_uses_cool_observations_only(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # Add heat obs with rate 2.0 and cool obs with rate 5.0
        for _ in range(5):
            engine._state.thermal_observations.append(_make_heat_obs(rate=2.0))
        for _ in range(5):
            engine._state.thermal_observations.append(_make_cool_obs(rate=5.0))
        model = engine.get_thermal_model()
        assert model["cooling_rate_f_per_hour"] == pytest.approx(5.0, abs=0.01)
        assert model["heating_rate_f_per_hour"] == pytest.approx(2.0, abs=0.01)

    def test_none_rate_when_no_obs_for_mode(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        for _ in range(5):
            engine._state.thermal_observations.append(_make_heat_obs())
        model = engine.get_thermal_model()
        assert model["cooling_rate_f_per_hour"] is None

    def test_uses_only_last_30_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # Add 35 heat obs: first 5 rate=100.0, last 30 rate=2.0
        for _ in range(5):
            engine._state.thermal_observations.append(_make_heat_obs(rate=100.0))
        for _ in range(30):
            engine._state.thermal_observations.append(_make_heat_obs(rate=2.0))
        model = engine.get_thermal_model()
        # Only the last THERMAL_MODEL_MAX_OBS (30) are used — all rate=2.0
        assert model["heating_rate_f_per_hour"] == pytest.approx(2.0, abs=0.01)


# ---------------------------------------------------------------------------
# TestWeatherBiasComputation
# ---------------------------------------------------------------------------


class TestWeatherBiasComputation:
    """Tests for get_weather_bias()."""

    def test_none_confidence_when_no_records_with_forecast_data(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # Records without forecast fields
        for i in range(10):
            engine._state.records.append({"date": f"2026-03-{i + 1:02d}", "day_type": "mild"})
        bias = engine.get_weather_bias()
        assert bias["confidence"] == "none"

    def test_low_confidence_at_7_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        for i in range(7):
            engine._state.records.append(
                _make_daily_record(
                    f"2026-03-{i + 1:02d}",
                    forecast_high=75.0,
                    observed_high=77.0,
                    forecast_low=55.0,
                    observed_low=56.0,
                )
            )
        bias = engine.get_weather_bias()
        assert bias["confidence"] == "low"

    def test_high_bias_is_mean_of_high_errors(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # 5 records: observed - forecast = [2, 3, 4, 5, 6] → mean = 4.0
        errors = [2.0, 3.0, 4.0, 5.0, 6.0]
        for i, err in enumerate(errors):
            engine._state.records.append(
                _make_daily_record(
                    f"2026-03-{i + 1:02d}",
                    forecast_high=70.0,
                    observed_high=70.0 + err,
                    forecast_low=50.0,
                    observed_low=51.0,
                )
            )
        # 5 records is below MIN_WEATHER_BIAS_OBSERVATIONS (7), so confidence is "none"
        # Add 2 more with same pattern
        for i, err in enumerate([2.0, 3.0]):
            engine._state.records.append(
                _make_daily_record(
                    f"2026-03-{i + 6:02d}",
                    forecast_high=70.0,
                    observed_high=70.0 + err,
                    forecast_low=50.0,
                    observed_low=51.0,
                )
            )
        bias = engine.get_weather_bias()
        expected_mean = sum([2.0, 3.0, 4.0, 5.0, 6.0, 2.0, 3.0]) / 7
        assert bias["high_bias"] == pytest.approx(expected_mean, abs=0.01)

    def test_bias_skips_records_missing_any_forecast_field(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # Mix: some complete, some with None
        for i in range(7):
            if i < 3:
                # Missing observed_high_f
                engine._state.records.append(
                    _make_daily_record(
                        f"2026-03-{i + 1:02d}",
                        forecast_high=70.0,
                        observed_high=None,
                        forecast_low=50.0,
                        observed_low=51.0,
                    )
                )
            else:
                engine._state.records.append(
                    _make_daily_record(
                        f"2026-03-{i + 1:02d}",
                        forecast_high=70.0,
                        observed_high=72.0,
                        forecast_low=50.0,
                        observed_low=51.0,
                    )
                )
        bias = engine.get_weather_bias()
        # Only 4 complete records — below MIN_WEATHER_BIAS_OBSERVATIONS (7)
        assert bias["confidence"] == "none"

    def test_uses_only_last_30_records_for_bias(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # First 5: high error 20°F (outliers)
        for i in range(5):
            engine._state.records.append(
                _make_daily_record(
                    f"2026-01-{i + 1:02d}",
                    forecast_high=70.0,
                    observed_high=90.0,
                    forecast_low=50.0,
                    observed_low=51.0,
                )
            )
        # Next 30: high error 2°F
        for i in range(30):
            engine._state.records.append(
                _make_daily_record(
                    f"2026-02-{i + 1:02d}",
                    forecast_high=70.0,
                    observed_high=72.0,
                    forecast_low=50.0,
                    observed_low=51.0,
                )
            )
        bias = engine.get_weather_bias()
        # Only last WEATHER_BIAS_MAX_OBS (30) used — all have error=2.0
        assert bias["high_bias"] == pytest.approx(2.0, abs=0.01)


# ---------------------------------------------------------------------------
# TestThermalModelBackwardCompat
# ---------------------------------------------------------------------------


class TestThermalModelBackwardCompat:
    """Backward compatibility when thermal_observations key is absent from state."""

    def test_load_state_without_thermal_observations_key(self, tmp_path: Path):
        """LearningState without thermal_observations → get_thermal_model confidence == 'none'."""
        engine = LearningEngine(tmp_path)
        # Manually set state without thermal_observations
        engine._state = LearningState(
            records=[],
            active_suggestions=[],
            dismissed_suggestions=[],
            settings_history=[],
            thermal_observations=[],
        )
        model = engine.get_thermal_model()
        assert model["confidence"] == "none"
