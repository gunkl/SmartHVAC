"""Tests for LearningEngine.reset(scope)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from custom_components.climate_advisor.learning import LearningEngine, LearningState

_TODAY = "2026-03-28"


def _make_engine(tmp_path: Path) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine._state = LearningState()
    return engine


def _make_thermal_obs(hvac_mode: str = "heat") -> dict:
    return {
        "timestamp": f"{_TODAY}T10:00:00",
        "date": _TODAY,
        "hvac_mode": hvac_mode,
        "session_minutes": 30.0,
        "rate_f_per_hour": 2.5,
        "outdoor_temp_f": 40.0,
        "start_indoor_f": 65.0,
        "end_indoor_f": 70.0,
    }


def _make_record_with_forecast(date: str, day_type: str = "cold") -> dict:
    return {
        "date": date,
        "day_type": day_type,
        "trend_direction": "stable",
        "forecast_high_f": 45.0,
        "forecast_low_f": 30.0,
        "observed_high_f": 42.0,
        "observed_low_f": 28.0,
    }


def _make_record_minimal(date: str) -> dict:
    return {
        "date": date,
        "day_type": "mild",
        "trend_direction": "stable",
    }


class TestLearningReset:
    """Tests for LearningEngine.reset(scope)."""

    def test_reset_all_clears_everything(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        engine._state.thermal_observations.append(_make_thermal_obs())
        engine._state.records.append(_make_record_minimal(_TODAY))
        engine._state.dismissed_suggestions.append("some_key")
        engine._state.active_suggestions.append({"key": "other_key", "text": "..."})

        with patch.object(engine, "save_state"):
            engine.reset("all")

        assert engine._state.thermal_observations == []
        assert engine._state.records == []
        assert engine._state.dismissed_suggestions == []
        assert engine._state.active_suggestions == []

    def test_reset_thermal_model_only(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        engine._state.thermal_observations.append(_make_thermal_obs())
        engine._state.records.append(_make_record_minimal(_TODAY))

        with patch.object(engine, "save_state"):
            engine.reset("thermal_model")

        assert engine._state.thermal_observations == []
        assert len(engine._state.records) == 1

    def test_reset_weather_bias_clears_forecast_fields(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        engine._state.records.append(_make_record_with_forecast("2026-03-26", day_type="cold"))
        engine._state.records.append(_make_record_with_forecast("2026-03-27", day_type="warm"))

        with patch.object(engine, "save_state"):
            engine.reset("weather_bias")

        for record in engine._state.records:
            assert record["forecast_high_f"] is None
            assert record["forecast_low_f"] is None
            assert record["observed_high_f"] is None
            assert record["observed_low_f"] is None

        # Non-forecast fields are preserved
        assert engine._state.records[0]["day_type"] == "cold"
        assert engine._state.records[1]["day_type"] == "warm"

    def test_reset_suggestions_only(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        engine._state.dismissed_suggestions.append("dismissed_key")
        engine._state.active_suggestions.append({"key": "active_key", "text": "..."})
        engine._state.records.append(_make_record_minimal(_TODAY))
        engine._state.thermal_observations.append(_make_thermal_obs())

        with patch.object(engine, "save_state"):
            engine.reset("suggestions")

        assert engine._state.dismissed_suggestions == []
        assert engine._state.active_suggestions == []
        assert len(engine._state.records) == 1
        assert len(engine._state.thermal_observations) == 1

    def test_reset_all_saves_state(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        mock_save = MagicMock()
        with patch.object(engine, "save_state", mock_save):
            engine.reset("all")
        mock_save.assert_called_once()

    def test_reset_thermal_saves_state(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        mock_save = MagicMock()
        with patch.object(engine, "save_state", mock_save):
            engine.reset("thermal_model")
        mock_save.assert_called_once()

    def test_reset_weather_bias_saves_state(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        mock_save = MagicMock()
        with patch.object(engine, "save_state", mock_save):
            engine.reset("weather_bias")
        mock_save.assert_called_once()

    def test_reset_suggestions_saves_state(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        mock_save = MagicMock()
        with patch.object(engine, "save_state", mock_save):
            engine.reset("suggestions")
        mock_save.assert_called_once()

    def test_reset_unknown_scope_is_noop(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        engine._state.thermal_observations.append(_make_thermal_obs())
        engine._state.records.append(_make_record_minimal(_TODAY))
        engine._state.dismissed_suggestions.append("some_key")
        engine._state.active_suggestions.append({"key": "other_key", "text": "..."})

        mock_save = MagicMock()
        with patch.object(engine, "save_state", mock_save):
            engine.reset("bogus_scope")

        # State is completely unchanged
        assert len(engine._state.thermal_observations) == 1
        assert len(engine._state.records) == 1
        assert engine._state.dismissed_suggestions == ["some_key"]
        assert len(engine._state.active_suggestions) == 1

        # save_state was NOT called
        mock_save.assert_not_called()

    def test_reset_weather_bias_logs_record_count(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        for i in range(3):
            engine._state.records.append(_make_record_with_forecast(f"2026-03-{25 + i:02d}"))

        with (
            patch.object(engine, "save_state"),
            patch("custom_components.climate_advisor.learning._LOGGER") as mock_logger,
        ):
            engine.reset("weather_bias")

        # At least one INFO call must reference 3 records in the format string + arg
        found = False
        for call in mock_logger.info.call_args_list:
            args = call.args
            if len(args) >= 2 and "%d" in args[0] and args[-1] == 3:
                found = True
                break
        assert found, "Expected an INFO log referencing 3 daily records"
