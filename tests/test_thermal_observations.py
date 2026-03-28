"""Tests for thermal observation recording (Phase 5G).

Tests LearningEngine.record_thermal_observation() and the coordinator's
_record_thermal_observation() method.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import THERMAL_OBS_CAP
from custom_components.climate_advisor.learning import LearningEngine, LearningState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TODAY = "2026-03-28"
_TODAY_DATE = date(2026, 3, 28)
_OLD_DATE = "2025-12-01"  # > 90 days ago


def _make_obs(obs_date: str = _TODAY, mode: str = "heat", rate: float = 2.0) -> dict:
    return {
        "timestamp": f"{obs_date}T10:00:00",
        "date": obs_date,
        "hvac_mode": mode,
        "session_minutes": 30.0,
        "rate_f_per_hour": rate,
        "outdoor_temp_f": 40.0,
        "start_indoor_f": 65.0,
        "end_indoor_f": 66.0,
    }


def _make_engine(tmp_path: Path) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


def _patch_learning_dt(today: date):
    """Patch dt_util in learning.py so now().date() returns a real date."""
    mock_dt = MagicMock()
    mock_dt.now.return_value.date.return_value = today
    return patch("custom_components.climate_advisor.learning.dt_util", mock_dt)


# ---------------------------------------------------------------------------
# TestThermalObservationStorage — exercises LearningEngine directly
# ---------------------------------------------------------------------------


class TestThermalObservationStorage:
    """Tests for record_thermal_observation() on LearningEngine."""

    def test_observation_appended(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        obs = _make_obs()
        with _patch_learning_dt(_TODAY_DATE):
            engine.record_thermal_observation(obs)
        assert obs in engine._state.thermal_observations

    def test_observations_capped_at_200(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        with _patch_learning_dt(_TODAY_DATE):
            for i in range(205):
                engine.record_thermal_observation(_make_obs(obs_date=_TODAY, rate=float(i + 1)))
        assert len(engine._state.thermal_observations) == THERMAL_OBS_CAP

    def test_90_day_trim(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # Add old observations directly (bypass trim by inserting without call)
        for _ in range(5):
            engine._state.thermal_observations.append(_make_obs(obs_date=_OLD_DATE))
        # Now add a recent one — the call should trim old ones
        with _patch_learning_dt(_TODAY_DATE):
            engine.record_thermal_observation(_make_obs(obs_date=_TODAY))
        remaining = [o for o in engine._state.thermal_observations if o["date"] == _OLD_DATE]
        assert len(remaining) == 0

    def test_missing_thermal_observations_key_defaults_to_empty(self, tmp_path: Path):
        """LearningState constructed from a dict without thermal_observations key."""
        state = LearningState(
            **{
                "records": [],
                "active_suggestions": [],
                "dismissed_suggestions": [],
                "settings_history": [],
            }
        )
        assert state.thermal_observations == []


# ---------------------------------------------------------------------------
# TestThermalObservationRecordingViaCoordinator
# ---------------------------------------------------------------------------


def _make_coordinator(tmp_path: Path):
    """Construct a minimal ClimateAdvisorCoordinator with mocked HA internals."""
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
        "indoor_temp_source": "climate_fallback",
        "temp_unit": "fahrenheit",
    }

    coordinator = ClimateAdvisorCoordinator(hass, config)
    coordinator._async_save_state = MagicMock(return_value=None)
    coordinator.automation_engine = MagicMock()
    coordinator.automation_engine._thermal_model = {}
    return coordinator


def _now_mock(fixed_now: datetime):
    """Return a MagicMock for dt_util with now() → fixed_now."""
    mock_dt = MagicMock()
    mock_dt.now.return_value = fixed_now
    return mock_dt


class TestThermalObservationRecordingViaCoordinator:
    """Tests for _record_thermal_observation() in the coordinator."""

    def _make_coordinator_with_learning(self, tmp_path: Path):
        coord = _make_coordinator(tmp_path)
        coord.learning = _make_engine(tmp_path)
        return coord

    def test_observation_recorded_on_hvac_stop(self, tmp_path: Path):
        coord = self._make_coordinator_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        start = now - timedelta(minutes=15)
        coord._hvac_on_since = start
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            _patch_learning_dt(now.date()),
        ):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) == 1

    def test_observation_skipped_if_session_too_short(self, tmp_path: Path):
        coord = self._make_coordinator_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        # 5 minutes — below MIN_THERMAL_SESSION_MINUTES (10)
        coord._hvac_on_since = now - timedelta(minutes=5)
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)

        mock_dt = _now_mock(now)
        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) == 0

    def test_observation_skipped_if_no_start_temp(self, tmp_path: Path):
        coord = self._make_coordinator_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        coord._hvac_on_since = now - timedelta(minutes=20)
        coord._hvac_session_start_indoor_temp = None  # No start temp
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)

        mock_dt = _now_mock(now)
        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) == 0

    def test_observation_skipped_if_rate_too_high(self, tmp_path: Path):
        """Rate > MAX_THERMAL_RATE_F_PER_HOUR → skip."""
        coord = self._make_coordinator_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        # 15 min, start=60, end=65 → rate = 5/(15/60) = 20°F/hr > 15
        coord._hvac_on_since = now - timedelta(minutes=15)
        coord._hvac_session_start_indoor_temp = 60.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=65.0)

        mock_dt = _now_mock(now)
        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) == 0

    def test_observation_skipped_if_rate_too_low(self, tmp_path: Path):
        """Rate < MIN_THERMAL_RATE_F_PER_HOUR → skip."""
        coord = self._make_coordinator_with_learning(tmp_path)
        now = datetime(2026, 3, 28, 12, 0, 0)
        # 60 min, start=65.0, end=65.0 → rate=0 < 0.1
        coord._hvac_on_since = now - timedelta(minutes=60)
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=65.0)

        mock_dt = _now_mock(now)
        with patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt):
            coord._record_thermal_observation(MagicMock())

        assert len(coord.learning._state.thermal_observations) == 0

    def test_today_record_session_count_incremented(self, tmp_path: Path):
        coord = self._make_coordinator_with_learning(tmp_path)
        from custom_components.climate_advisor.learning import DailyRecord

        now = datetime(2026, 3, 28, 12, 0, 0)
        coord._hvac_on_since = now - timedelta(minutes=20)
        coord._hvac_session_start_indoor_temp = 65.0
        coord._hvac_session_start_outdoor_temp = 40.0
        coord._hvac_session_mode = "heat"
        coord._get_indoor_temp = MagicMock(return_value=67.0)
        coord._today_record = DailyRecord(date="2026-03-28", day_type="cold", trend_direction="stable")

        mock_dt = _now_mock(now)
        with (
            patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
            _patch_learning_dt(now.date()),
        ):
            coord._record_thermal_observation(MagicMock())

        assert coord._today_record.thermal_session_count == 1

    def test_today_record_peak_rate_tracked(self, tmp_path: Path):
        coord = self._make_coordinator_with_learning(tmp_path)
        from custom_components.climate_advisor.learning import DailyRecord

        coord._today_record = DailyRecord(date="2026-03-28", day_type="cold", trend_direction="stable")

        def _run_session(start_temp: float, end_temp: float, minutes: float):
            now = datetime(2026, 3, 28, 12, 0, 0)
            coord._hvac_on_since = now - timedelta(minutes=minutes)
            coord._hvac_session_start_indoor_temp = start_temp
            coord._hvac_session_start_outdoor_temp = 40.0
            coord._hvac_session_mode = "heat"
            coord._get_indoor_temp = MagicMock(return_value=end_temp)
            mock_dt = _now_mock(now)
            with (
                patch("custom_components.climate_advisor.coordinator.dt_util", mock_dt),
                _patch_learning_dt(now.date()),
            ):
                coord._record_thermal_observation(MagicMock())

        # Session 1: rate = 1°F / (20/60h) = 3°F/hr
        _run_session(65.0, 66.0, 20.0)
        # Session 2: rate = 2°F / (20/60h) = 6°F/hr
        _run_session(65.0, 67.0, 20.0)

        assert coord._today_record.peak_hvac_rate_f_per_hour == pytest.approx(6.0, abs=0.1)
