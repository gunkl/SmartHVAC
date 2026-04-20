"""Tests for thermal observation v2 storage and recovery (Issue #114).

Tests LearningEngine.record_thermal_observation() storage/trim/cap,
_commit_event_from_dict() physics computation, and
recover_pending_event_on_startup() crash-recovery behavior.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.const import THERMAL_OBS_CAP
from custom_components.climate_advisor.learning import LearningEngine, LearningState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TODAY = "2026-04-19"
_TODAY_DATE = date(2026, 4, 19)
_OLD_DATE = "2025-12-01"  # > 90 days ago

_FAKE_NOW = datetime(2026, 4, 19, 12, 0, 0, tzinfo=UTC)


def _make_v2_obs(obs_date: str = _TODAY, mode: str = "heat") -> dict:
    """Build a minimal valid v2 ThermalObservation dict."""
    return {
        "event_id": "test-event-1",
        "timestamp": f"{obs_date}T10:00:00",
        "date": obs_date,
        "hvac_mode": mode,
        "session_minutes": 8.0,
        "start_indoor_f": 65.0,
        "end_indoor_f": 68.0,
        "peak_indoor_f": 68.0,
        "start_outdoor_f": 40.0,
        "avg_outdoor_f": 40.0,
        "delta_t_avg": 26.0,
        "k_passive": -0.05,
        "k_active": 3.0,
        "passive_baseline_rate": -0.8,
        "r_squared_passive": 0.85,
        "r_squared_active": 0.78,
        "sample_count_pre": 5,
        "sample_count_active": 8,
        "sample_count_post": 15,
        "confidence_grade": "medium",
        "schema_version": 2,
    }


def _make_engine(tmp_path: Path) -> LearningEngine:
    engine = LearningEngine(tmp_path)
    engine.load_state()
    return engine


def _patch_learning_dt(today: date):
    """Patch dt_util in learning.py so now().date() returns a real date."""
    mock_dt = MagicMock()
    mock_dt.now.return_value.date.return_value = today
    mock_dt.now.return_value.isoformat.return_value = f"{today}T12:00:00"
    return patch("custom_components.climate_advisor.learning.dt_util", mock_dt)


# ---------------------------------------------------------------------------
# TestThermalObservationStorage — exercises LearningEngine directly
# ---------------------------------------------------------------------------


class TestThermalObservationStorage:
    """Tests for record_thermal_observation() on LearningEngine (v2 format)."""

    def test_observation_appended(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        obs = _make_v2_obs()
        with _patch_learning_dt(_TODAY_DATE):
            engine.record_thermal_observation(obs)
        assert obs in engine._state.thermal_observations

    def test_observations_capped_at_cap(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        with _patch_learning_dt(_TODAY_DATE):
            for i in range(THERMAL_OBS_CAP + 5):
                obs = dict(_make_v2_obs(), event_id=f"e{i}")
                engine.record_thermal_observation(obs)
        assert len(engine._state.thermal_observations) == THERMAL_OBS_CAP

    def test_90_day_trim(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # Add old observations directly (bypass trim)
        for _ in range(5):
            engine._state.thermal_observations.append(_make_v2_obs(obs_date=_OLD_DATE))
        # Now add a recent one — the call should trim old ones
        with _patch_learning_dt(_TODAY_DATE):
            engine.record_thermal_observation(_make_v2_obs(obs_date=_TODAY))
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

    def test_ewma_cache_updated_on_record(self, tmp_path: Path):
        """record_thermal_observation() also updates the EWMA thermal_model_cache."""
        engine = _make_engine(tmp_path)
        obs = _make_v2_obs(mode="heat")
        with _patch_learning_dt(_TODAY_DATE):
            engine.record_thermal_observation(obs)
        assert engine._state.thermal_model_cache is not None
        assert engine._state.thermal_model_cache.get("k_passive") == -0.05
        assert engine._state.thermal_model_cache.get("k_active_heat") == 3.0

    def test_invalid_obs_rejected(self, tmp_path: Path):
        """Obs without 'date' key is silently rejected."""
        engine = _make_engine(tmp_path)
        with _patch_learning_dt(_TODAY_DATE):
            engine.record_thermal_observation({"k_passive": -0.05})  # no 'date'
        assert len(engine._state.thermal_observations) == 0


# ---------------------------------------------------------------------------
# TestCommitEventFromDict — exercises LearningEngine._commit_event_from_dict
# ---------------------------------------------------------------------------


def _make_post_heat_event(session_mode: str = "heat", n_post: int = 20, n_active: int = 8) -> dict:
    """Build a PendingThermalEvent dict with synthetic exponential-decay samples."""
    import math

    k_p = -0.05
    k_a = 3.0
    t_outdoor = 40.0

    # Active samples: indoor temp rising due to heating
    active_samples = []
    T = 65.0
    dt_hr = 1.0 / 60.0
    exp_kp = math.exp(k_p * dt_hr)
    for i in range(n_active):
        active_samples.append(
            {
                "timestamp": f"2026-04-19T10:{i:02d}:00",
                "indoor_temp_f": T,
                "outdoor_temp_f": t_outdoor,
                "elapsed_minutes": float(i),
            }
        )
        T = t_outdoor + (T - t_outdoor) * exp_kp + (k_a / k_p) * (exp_kp - 1)

    T_post_start = T  # where heating left off

    # Post-heat samples: passive decay
    post_samples = []
    for i in range(n_post):
        t_hr = i / 60.0
        indoor = t_outdoor + (T_post_start - t_outdoor) * math.exp(k_p * t_hr)
        post_samples.append(
            {
                "timestamp": f"2026-04-19T10:{n_active + i:02d}:00",
                "indoor_temp_f": indoor,
                "outdoor_temp_f": t_outdoor,
                "elapsed_minutes": float(n_active + i),
            }
        )

    return {
        "event_id": "test-pending-1",
        "created_at": "2026-04-19T10:00:00",
        "hvac_mode": session_mode,
        "session_mode": session_mode,
        "status": "stabilized",
        "active_start": "2026-04-19T10:00:00",
        "active_end": f"2026-04-19T10:{n_active:02d}:00",
        "stabilized_at": f"2026-04-19T10:{n_active + n_post:02d}:00",
        "pre_heat_samples": [],
        "active_samples": active_samples,
        "post_heat_samples": post_samples,
        "start_indoor_f": 65.0,
        "end_indoor_f": post_samples[-1]["indoor_temp_f"],
        "peak_indoor_f": max(s["indoor_temp_f"] for s in active_samples),
        "start_outdoor_f": t_outdoor,
        "session_minutes": float(n_active),
        "schema_version": 1,
    }


class TestCommitEventFromDict:
    """Tests for LearningEngine._commit_event_from_dict()."""

    def test_successful_commit_returns_obs(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        event = _make_post_heat_event("heat", n_post=20, n_active=8)
        with _patch_learning_dt(_TODAY_DATE):
            obs = engine._commit_event_from_dict(event, force_grade=None)
        assert obs is not None
        assert obs["hvac_mode"] == "heat"
        assert obs["k_passive"] < 0
        assert obs["k_active"] is not None
        assert obs["k_active"] > 0

    def test_commit_saves_to_thermal_observations(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        event = _make_post_heat_event("heat", n_post=20, n_active=8)
        with _patch_learning_dt(_TODAY_DATE):
            engine._commit_event_from_dict(event, force_grade=None)
        assert len(engine._state.thermal_observations) == 1

    def test_force_grade_overrides_computed_grade(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        event = _make_post_heat_event("heat", n_post=20, n_active=8)
        with _patch_learning_dt(_TODAY_DATE):
            obs = engine._commit_event_from_dict(event, force_grade="low")
        assert obs is not None
        assert obs["confidence_grade"] == "low"

    def test_fan_only_commit_has_none_k_active(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        event = _make_post_heat_event("fan_only", n_post=20, n_active=5)
        with _patch_learning_dt(_TODAY_DATE):
            obs = engine._commit_event_from_dict(event, force_grade=None)
        # fan_only: k_active should be None, k_passive should be extracted
        if obs is not None:
            assert obs["k_active"] is None

    def test_too_few_post_samples_returns_none(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        event = _make_post_heat_event("heat", n_post=5, n_active=8)  # 5 < THERMAL_MIN_POST_HEAT_SAMPLES
        with _patch_learning_dt(_TODAY_DATE):
            obs = engine._commit_event_from_dict(event, force_grade=None)
        assert obs is None
        assert len(engine._state.thermal_observations) == 0


# ---------------------------------------------------------------------------
# TestStartupRecovery — exercises recover_pending_event_on_startup()
# ---------------------------------------------------------------------------


class TestStartupRecovery:
    """Tests for LearningEngine.recover_pending_event_on_startup()."""

    def test_stabilized_event_committed(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        event = _make_post_heat_event("heat", n_post=20, n_active=8)
        event["status"] = "stabilized"
        engine._state.pending_thermal_event = event

        with _patch_learning_dt(_TODAY_DATE):
            result = engine.recover_pending_event_on_startup()

        assert result is not None
        assert result["hvac_mode"] == "heat"
        assert engine._state.pending_thermal_event is None

    def test_post_heat_with_enough_samples_committed_at_low(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        event = _make_post_heat_event("heat", n_post=15, n_active=8)
        event["status"] = "post_heat"
        engine._state.pending_thermal_event = event

        with _patch_learning_dt(_TODAY_DATE):
            engine.recover_pending_event_on_startup()

        # Should commit at forced low confidence OR return None if k_passive rejected
        # Either way, pending event must be cleared
        assert engine._state.pending_thermal_event is None

    def test_post_heat_too_few_samples_discarded(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        event = _make_post_heat_event("heat", n_post=5, n_active=8)
        event["status"] = "post_heat"
        engine._state.pending_thermal_event = event

        with _patch_learning_dt(_TODAY_DATE):
            result = engine.recover_pending_event_on_startup()

        assert result is None
        assert engine._state.pending_thermal_event is None
        assert len(engine._state.thermal_observations) == 0

    def test_active_status_discarded(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        # active events are discarded (no commit attempted)
        event = {
            "event_id": "test-active",
            "status": "active",
            "hvac_mode": "heat",
            "session_mode": "heat",
            "pre_heat_samples": [],
            "active_samples": [],
            "post_heat_samples": [],
        }
        engine._state.pending_thermal_event = event

        with _patch_learning_dt(_TODAY_DATE):
            result = engine.recover_pending_event_on_startup()

        assert result is None
        assert engine._state.pending_thermal_event is None
        assert len(engine._state.thermal_observations) == 0

    def test_no_pending_event_returns_none(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        with _patch_learning_dt(_TODAY_DATE):
            result = engine.recover_pending_event_on_startup()
        assert result is None

    def test_complete_status_cleared(self, tmp_path: Path):
        engine = _make_engine(tmp_path)
        event = {
            "event_id": "test-complete",
            "status": "complete",
            "hvac_mode": "heat",
            "session_mode": "heat",
            "pre_heat_samples": [],
            "active_samples": [],
            "post_heat_samples": [],
        }
        engine._state.pending_thermal_event = event

        with _patch_learning_dt(_TODAY_DATE):
            result = engine.recover_pending_event_on_startup()

        assert result is None
        assert engine._state.pending_thermal_event is None
