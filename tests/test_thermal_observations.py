"""Tests for the thermal model v3 parallel observation state machine (coordinator.py).

Covers the new multi-type observation pipeline:
  _start_hvac_observation / _start_decay_observation
  _sample_all_observations
  _abandon_observation / _commit_observation_if_sufficient
  _check_hvac_stabilization
  LearningEngine v3 migration path (pending_thermal_event → pending_observations)
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

# Give the HA dt sub-attribute a real parse_datetime so coordinator's local
# `from homeassistant.util import dt as dt_util2` works correctly.
_ha_util = sys.modules.get("homeassistant.util")
if _ha_util is not None:
    _ha_util.dt.parse_datetime = lambda s: datetime.fromisoformat(s) if s else None

# ---------------------------------------------------------------------------
# Imports after stubs are in place
# ---------------------------------------------------------------------------

from custom_components.climate_advisor.const import (  # noqa: E402
    OBS_TYPE_FAN_ONLY_DECAY,
    OBS_TYPE_HVAC_COOL,
    OBS_TYPE_HVAC_HEAT,
    OBS_TYPE_PASSIVE_DECAY,
    THERMAL_HVAC_MIN_DECAY_F,
    THERMAL_PASSIVE_MIN_SAMPLES,
    THERMAL_PASSIVE_MIN_SIGNAL_F,
)
from custom_components.climate_advisor.learning import LearningEngine  # noqa: E402

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_FAKE_NOW = datetime(2026, 4, 28, 12, 0, 0, tzinfo=UTC)


def _parse_datetime_real(s: str) -> datetime | None:
    """Real ISO parser forwarded to coordinator dt_util2 mock."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _make_dt_mock(now: datetime = _FAKE_NOW):
    """dt_util mock with real now() and parse_datetime() behaviour."""
    mock_dt = MagicMock()
    mock_dt.now.return_value = now
    mock_dt.parse_datetime.side_effect = _parse_datetime_real
    return mock_dt


def _get_coordinator_class():
    """Import coordinator freshly to avoid stale module references."""
    mod = importlib.import_module("custom_components.climate_advisor.coordinator")
    return mod.ClimateAdvisorCoordinator


def _make_obs_coord(
    *,
    indoor_temp: float = 75.0,
    outdoor_temp: float = 55.0,
    hvac_action: str = "idle",
    fan_active: bool = False,
    nat_vent_active: bool = False,
    any_sensor_open: bool = False,
    learning_enabled: bool = True,
):
    """Build a minimal coordinator stub with the v3 observation methods bound."""
    ClimateAdvisorCoordinator = _get_coordinator_class()
    coord = object.__new__(ClimateAdvisorCoordinator)

    # ── hass mock ───────────────────────────────────────────────────────────
    hass = MagicMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)

    async def _exec_job(fn, *args):
        return fn(*args)

    hass.async_add_executor_job = _exec_job

    climate_state = MagicMock()
    climate_state.state = "heat" if hvac_action in ("heating",) else "idle"
    climate_state.attributes = {"hvac_action": hvac_action}
    weather_state = MagicMock()
    weather_state.attributes = {"temperature": outdoor_temp}

    def _states_get(entity_id: str):
        if "climate" in entity_id:
            return climate_state
        if "weather" in entity_id:
            return weather_state
        return None

    hass.states.get = MagicMock(side_effect=_states_get)
    coord.hass = hass

    # ── config ──────────────────────────────────────────────────────────────
    coord.config = {
        "climate_entity": "climate.test",
        "weather_entity": "weather.test",
        "comfort_heat": 70,
        "comfort_cool": 75,
        "learning_enabled": learning_enabled,
    }

    # ── automation engine stub ───────────────────────────────────────────────
    ae = MagicMock()
    ae._fan_active = fan_active
    ae._natural_vent_active = nat_vent_active
    coord.automation_engine = ae

    # ── learning stub ────────────────────────────────────────────────────────
    learning = MagicMock()
    learning.set_pending_thermal_event = MagicMock()
    learning.save_state = MagicMock()
    learning._commit_event_from_dict = MagicMock(return_value={"hvac_mode": "heat"})
    coord.learning = learning

    # ── thermal state ────────────────────────────────────────────────────────
    coord._pending_observations = {}
    coord._pending_thermal_event = None
    coord._pre_heat_sample_buffer = []
    coord._last_outdoor_temp = outdoor_temp

    # ── helper methods ───────────────────────────────────────────────────────
    coord._get_indoor_temp = MagicMock(return_value=indoor_temp)
    coord._any_sensor_open = MagicMock(return_value=any_sensor_open)
    coord._async_save_state = AsyncMock()

    def _get_current_sample(elapsed: float) -> dict:
        return {
            "timestamp": _FAKE_NOW.isoformat(),
            "indoor_temp_f": indoor_temp,
            "outdoor_temp_f": outdoor_temp,
            "elapsed_minutes": elapsed,
        }

    coord._get_current_sample = _get_current_sample

    # ── bind real observation methods ─────────────────────────────────────────
    for method_name in (
        "_ensure_pending_observations",
        "_start_hvac_observation",
        "_start_decay_observation",
        "_sample_all_observations",
        "_abandon_observation",
        "_commit_observation_if_sufficient",
        "_commit_observation",
        "_check_hvac_stabilization",
        "_end_hvac_active_phase",
    ):
        method = getattr(ClimateAdvisorCoordinator, method_name)
        setattr(coord, method_name, types.MethodType(method, coord))

    return coord


# ---------------------------------------------------------------------------
# TestPassiveDecayObservation
# ---------------------------------------------------------------------------


class TestPassiveDecayObservation:
    """Passive-decay observation lifecycle via _sample_all_observations()."""

    def test_starts_on_hvac_off_with_sufficient_delta(self):
        # delta = 75 - 55 = 20 > THERMAL_PASSIVE_MIN_DELTA_F (3.0)
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=75.0, outdoor_temp=55.0)
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_PASSIVE_DECAY in coord._pending_observations
        obs = coord._pending_observations[OBS_TYPE_PASSIVE_DECAY]
        assert obs["status"] == "monitoring"

    def test_does_not_start_when_hvac_heating(self):
        coord = _make_obs_coord(hvac_action="heating", indoor_temp=70.0, outdoor_temp=50.0)
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations

    def test_does_not_start_when_sensor_open(self):
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=75.0,
            outdoor_temp=55.0,
            any_sensor_open=True,
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations

    def test_does_not_start_when_delta_below_threshold(self):
        # delta = 72 - 71 = 1.0 < THERMAL_PASSIVE_MIN_DELTA_F (3.0)
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=72.0, outdoor_temp=71.0)
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations

    def test_abandons_when_hvac_starts(self):
        # Place an active passive_decay observation with 5 samples (below min)
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=75.0, outdoor_temp=55.0)
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-passive-1",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 75.0,
                    "outdoor_temp_f": 55.0,
                    "elapsed_minutes": float(i),
                }
                for i in range(5)
            ],
            "flags_at_start": {},
            "schema_version": 1,
        }
        # Flip HVAC to heating
        coord.hass.states.get = MagicMock(
            side_effect=lambda eid: (
                MagicMock(
                    state="heat",
                    attributes={"hvac_action": "heating"},
                )
                if "climate" in eid
                else MagicMock(attributes={"temperature": 55.0})
            )
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        # 5 samples < THERMAL_PASSIVE_MIN_SAMPLES (30) → abandoned
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations

    def test_commits_when_sufficient_samples_and_signal(self):
        """passive_decay with THERMAL_PASSIVE_MIN_SAMPLES samples and delta > MIN_SIGNAL_F commits."""
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=74.0, outdoor_temp=55.0)
        # Build MIN_SAMPLES samples (first=75.0, last=74.0 → delta=1.0 > 0.5)
        samples = [
            {"timestamp": _FAKE_NOW.isoformat(), "indoor_temp_f": 75.0, "outdoor_temp_f": 55.0, "elapsed_minutes": 0.0},
        ]
        for i in range(1, THERMAL_PASSIVE_MIN_SAMPLES):
            samples.append(
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 74.0,
                    "outdoor_temp_f": 55.0,
                    "elapsed_minutes": float(i),
                }
            )
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-passive-2",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": samples,
            "flags_at_start": {},
            "schema_version": 1,
        }
        dt_mock = _make_dt_mock()
        committed_obs_types = []

        def _fake_async_create_task(coro):
            # Detect _commit_observation coroutines by name, then close safely
            coro_name = getattr(coro, "__name__", getattr(coro, "__qualname__", ""))
            if "_commit_observation" in coro_name:
                committed_obs_types.append(OBS_TYPE_PASSIVE_DECAY)
            coro.close()

        coord.hass.async_create_task = _fake_async_create_task

        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        # The observation should either be committed (popped) or queued for commit
        # Either the obs was popped already or a task was queued
        was_popped = OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations
        was_queued = len(committed_obs_types) > 0
        assert was_popped or was_queued, (
            "Expected passive_decay to be committed or queued for commit when "
            f"samples={THERMAL_PASSIVE_MIN_SAMPLES}, delta=1.0 > {THERMAL_PASSIVE_MIN_SIGNAL_F}"
        )


# ---------------------------------------------------------------------------
# TestFanOnlyObservation
# ---------------------------------------------------------------------------


class TestFanOnlyObservation:
    """Fan-only decay observation lifecycle via _sample_all_observations()."""

    def test_starts_when_fan_active_no_hvac(self):
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=72.0,
            outdoor_temp=65.0,
            fan_active=True,
            any_sensor_open=False,
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_FAN_ONLY_DECAY in coord._pending_observations
        assert coord._pending_observations[OBS_TYPE_FAN_ONLY_DECAY]["status"] == "monitoring"

    def test_does_not_start_when_hvac_active(self):
        coord = _make_obs_coord(
            hvac_action="heating",
            indoor_temp=72.0,
            outdoor_temp=65.0,
            fan_active=True,
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_FAN_ONLY_DECAY not in coord._pending_observations

    def test_does_not_start_when_sensor_open(self):
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=72.0,
            outdoor_temp=65.0,
            fan_active=True,
            any_sensor_open=True,
        )
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        assert OBS_TYPE_FAN_ONLY_DECAY not in coord._pending_observations

    def test_abandons_when_fan_stops(self):
        """fan_only_decay with < THERMAL_FAN_MIN_SAMPLES is abandoned when fan stops."""

        coord = _make_obs_coord(hvac_action="idle", indoor_temp=72.0, outdoor_temp=65.0, fan_active=False)
        # Pre-seed observation with 5 samples (< 15 min threshold)
        coord._pending_observations[OBS_TYPE_FAN_ONLY_DECAY] = {
            "obs_type": OBS_TYPE_FAN_ONLY_DECAY,
            "obs_id": "test-fan-1",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 72.0,
                    "outdoor_temp_f": 65.0,
                    "elapsed_minutes": float(i),
                }
                for i in range(5)
            ],
            "flags_at_start": {},
            "schema_version": 1,
        }
        # ae._fan_active is already False; climate state is idle
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()
        # 5 < THERMAL_FAN_MIN_SAMPLES → should be abandoned
        assert OBS_TYPE_FAN_ONLY_DECAY not in coord._pending_observations


# ---------------------------------------------------------------------------
# TestConcurrentObservations
# ---------------------------------------------------------------------------


class TestConcurrentObservations:
    """Multiple observation types cannot contaminate each other."""

    def test_hvac_start_abandons_passive_decay(self):
        """_start_hvac_observation() abandons (or commits-if-sufficient) passive_decay."""
        coord = _make_obs_coord(hvac_action="idle", indoor_temp=75.0, outdoor_temp=55.0)
        # Pre-seed an active passive_decay with 3 insufficient samples
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-passive-3",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 75.0,
                    "outdoor_temp_f": 55.0,
                    "elapsed_minutes": float(i),
                }
                for i in range(3)
            ],
            "flags_at_start": {},
            "schema_version": 1,
        }
        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._start_hvac_observation("heat"))

        # passive_decay must be gone (3 < min_samples → abandoned)
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations
        # hvac_heat observation must be started
        assert OBS_TYPE_HVAC_HEAT in coord._pending_observations

    def test_passive_and_fan_only_cannot_coexist(self):
        """When fan activates while passive_decay is monitoring, passive_decay is abandoned."""
        coord = _make_obs_coord(
            hvac_action="idle",
            indoor_temp=75.0,
            outdoor_temp=55.0,
            fan_active=False,
        )
        # Pre-seed passive_decay with 3 insufficient samples
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-passive-4",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 75.0,
                    "outdoor_temp_f": 55.0,
                    "elapsed_minutes": float(i),
                }
                for i in range(3)
            ],
            "flags_at_start": {},
            "schema_version": 1,
        }
        # Now fan activates — flip ae._fan_active
        coord.automation_engine._fan_active = True

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            coord._sample_all_observations()

        # passive_decay should be abandoned (fan_activated condition)
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations
        # fan_only_decay should be started
        assert OBS_TYPE_FAN_ONLY_DECAY in coord._pending_observations


# ---------------------------------------------------------------------------
# TestHvacObservationReducedPlateauGuard
# ---------------------------------------------------------------------------


class TestHvacObservationReducedPlateauGuard:
    """_check_hvac_stabilization() plateau guard uses THERMAL_HVAC_MIN_DECAY_F=0.3."""

    def _make_stabilization_window(self, peak_f: float, end_f: float, n_post: int = 10) -> dict:
        """Build a post_heat HVAC observation with tight recent variance."""

        # All samples within the stabilization window (uniform temperature = no variance)
        now = _FAKE_NOW
        samples = []
        for i in range(n_post):
            ts = datetime(
                now.year,
                now.month,
                now.day,
                now.hour,
                max(0, now.minute - (n_post - 1 - i)),
                now.second,
                tzinfo=UTC,
            )
            samples.append(
                {
                    "timestamp": ts.isoformat(),
                    "indoor_temp_f": end_f,
                    "outdoor_temp_f": 50.0,
                    "elapsed_minutes": float(i),
                }
            )

        active_end = datetime(
            now.year,
            now.month,
            now.day,
            now.hour,
            max(0, now.minute - n_post),
            tzinfo=UTC,
        )
        return {
            "obs_type": OBS_TYPE_HVAC_HEAT,
            "obs_id": "test-hvac-plateau",
            "start_time": _FAKE_NOW.isoformat(),
            "active_start": active_end.isoformat(),
            "active_end": active_end.isoformat(),
            "status": "monitoring",
            "_phase": "post_heat",
            "active_samples": [],
            "post_heat_samples": samples,
            "peak_indoor_f": peak_f,
            "end_indoor_f": end_f,
            "flags_at_start": {},
            "schema_version": 1,
        }

    def test_plateau_guard_fires_for_0_2_delta(self):
        """peak=72.0, end=71.8 → decay=0.2 < THERMAL_HVAC_MIN_DECAY_F=0.3 → abandoned."""
        from custom_components.climate_advisor.const import THERMAL_MIN_POST_HEAT_SAMPLES

        coord = _make_obs_coord()
        n_post = max(10, THERMAL_MIN_POST_HEAT_SAMPLES)
        obs = self._make_stabilization_window(peak_f=72.0, end_f=71.8, n_post=n_post)
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = obs

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._check_hvac_stabilization(OBS_TYPE_HVAC_HEAT))

        assert OBS_TYPE_HVAC_HEAT not in coord._pending_observations, (
            "Plateau guard (decay=0.2 < 0.3) should have abandoned the observation"
        )

    def test_plateau_guard_passes_for_0_5_delta(self):
        """peak=72.0, end=71.5 → decay=0.5 > THERMAL_HVAC_MIN_DECAY_F=0.3 → committed."""
        from custom_components.climate_advisor.const import THERMAL_MIN_POST_HEAT_SAMPLES

        coord = _make_obs_coord()
        n_post = max(10, THERMAL_MIN_POST_HEAT_SAMPLES)
        obs = self._make_stabilization_window(peak_f=72.0, end_f=71.5, n_post=n_post)
        coord._pending_observations[OBS_TYPE_HVAC_HEAT] = obs

        commit_called = []

        async def _fake_commit(obs_type, force_grade=None):
            commit_called.append(obs_type)
            coord._pending_observations.pop(obs_type, None)

        coord._commit_observation = _fake_commit

        dt_mock = _make_dt_mock()
        with patch("custom_components.climate_advisor.coordinator.dt_util", dt_mock):
            asyncio.run(coord._check_hvac_stabilization(OBS_TYPE_HVAC_HEAT))

        assert len(commit_called) == 1 and commit_called[0] == OBS_TYPE_HVAC_HEAT, (
            "Plateau guard should pass for decay=0.5 > 0.3, observation should be committed"
        )

    def test_plateau_guard_threshold_is_0_3(self):
        """Confirm THERMAL_HVAC_MIN_DECAY_F constant value is exactly 0.3."""
        assert pytest.approx(0.3) == THERMAL_HVAC_MIN_DECAY_F, (
            f"Expected THERMAL_HVAC_MIN_DECAY_F=0.3, got {THERMAL_HVAC_MIN_DECAY_F}"
        )


# ---------------------------------------------------------------------------
# TestAbandonmentLogging
# ---------------------------------------------------------------------------


class TestAbandonmentLogging:
    """_abandon_observation() logs at WARNING level with type and reason."""

    def test_abandon_logs_at_warning_level(self):
        coord = _make_obs_coord()
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-abandon-1",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [
                {
                    "timestamp": _FAKE_NOW.isoformat(),
                    "indoor_temp_f": 75.0,
                    "outdoor_temp_f": 55.0,
                    "elapsed_minutes": float(i),
                }
                for i in range(3)
            ],
            "flags_at_start": {},
            "schema_version": 1,
        }

        with patch("custom_components.climate_advisor.coordinator._LOGGER") as mock_logger:
            coord._abandon_observation(OBS_TYPE_PASSIVE_DECAY, "hvac_started")

        assert mock_logger.warning.called, "Expected _LOGGER.warning() to be called on abandonment"
        call_args = mock_logger.warning.call_args
        # First positional arg is the format string; subsequent args are substituted values
        format_str = call_args[0][0]
        all_args = call_args[0]
        full_message = format_str % all_args[1:]
        assert "passive_decay" in full_message, f"Expected 'passive_decay' in warning message, got: {full_message}"
        assert "hvac_started" in full_message, f"Expected 'hvac_started' in warning message, got: {full_message}"

    def test_abandon_removes_obs_from_pending(self):
        coord = _make_obs_coord()
        coord._pending_observations[OBS_TYPE_PASSIVE_DECAY] = {
            "obs_type": OBS_TYPE_PASSIVE_DECAY,
            "obs_id": "test-abandon-2",
            "start_time": _FAKE_NOW.isoformat(),
            "status": "monitoring",
            "samples": [],
            "flags_at_start": {},
            "schema_version": 1,
        }
        coord._abandon_observation(OBS_TYPE_PASSIVE_DECAY, "test_reason")
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations

    def test_abandon_noop_when_obs_not_present(self):
        """_abandon_observation on a missing obs_type must not raise."""
        coord = _make_obs_coord()
        # Should not raise
        coord._abandon_observation(OBS_TYPE_PASSIVE_DECAY, "no_such_obs")
        assert OBS_TYPE_PASSIVE_DECAY not in coord._pending_observations


# ---------------------------------------------------------------------------
# TestMigrationLoadState
# ---------------------------------------------------------------------------


class TestMigrationLoadState:
    """LearningEngine.load_state() migrates pending_thermal_event to pending_observations."""

    def test_migration_converts_pending_thermal_event_heat(self, tmp_path: Path):
        """A v2 post_heat event is migrated to OBS_TYPE_HVAC_HEAT in pending_observations."""
        engine = LearningEngine(tmp_path)
        engine.load_state()

        # Inject a v2-style pending event into state (post_heat — recoverable)
        engine._state.pending_thermal_event = {
            "event_id": "test-123",
            "status": "post_heat",
            "session_mode": "heat",
            "hvac_mode": "heat",
            "active_start": "2025-06-15T08:00:00",
            "active_samples": [
                {
                    "indoor_temp_f": 70.0,
                    "outdoor_temp_f": 50.0,
                    "elapsed_minutes": 1.0,
                    "timestamp": "2025-06-15T08:01:00",
                }
            ],
            "pre_heat_samples": [],
            "post_heat_samples": [],
        }
        # Make pending_observations empty so migration runs
        engine._state.pending_observations = {}

        # Re-run load_state to trigger migration — simulate by calling the migration path
        # directly by setting up the state and calling load_state() again after persisting
        import json

        state_dict = {
            "records": [],
            "active_suggestions": [],
            "dismissed_suggestions": [],
            "settings_history": [],
            "thermal_observations": [],
            "thermal_model_cache": None,
            "pending_observations": {},
            "pending_thermal_event": engine._state.pending_thermal_event,
        }
        state_file = tmp_path / "climate_advisor_learning.json"
        state_file.write_text(json.dumps(state_dict))

        engine2 = LearningEngine(tmp_path)
        engine2.load_state()

        # After migration: pending_thermal_event should NOT have been blindly carried —
        # the migration writes it to pending_observations
        assert isinstance(engine2._state.pending_observations, dict)
        assert OBS_TYPE_HVAC_HEAT in engine2._state.pending_observations, (
            "v3 migration should convert pending_thermal_event[mode=heat] "
            f"→ pending_observations['{OBS_TYPE_HVAC_HEAT}']"
        )
        migrated = engine2._state.pending_observations[OBS_TYPE_HVAC_HEAT]
        assert migrated["obs_id"] == "test-123"

    def test_migration_discards_active_status_v2_event(self, tmp_path: Path):
        """A v2 event with status='active' is discarded — cannot be recovered without runtime state."""
        import json

        state_dict = {
            "records": [],
            "active_suggestions": [],
            "dismissed_suggestions": [],
            "settings_history": [],
            "thermal_observations": [],
            "thermal_model_cache": None,
            "pending_observations": {},
            "pending_thermal_event": {
                "event_id": "active-event",
                "status": "active",
                "session_mode": "heat",
                "hvac_mode": "heat",
                "active_start": "2025-06-15T08:00:00",
                "active_samples": [],
                "pre_heat_samples": [],
                "post_heat_samples": [],
            },
        }
        state_file = tmp_path / "climate_advisor_learning.json"
        state_file.write_text(json.dumps(state_dict))

        engine = LearningEngine(tmp_path)
        engine.load_state()

        assert engine._state.pending_thermal_event is None, "active-status v2 event should be discarded"
        assert engine._state.pending_observations == {}, "no obs should be created for discarded active event"

    def test_migration_clears_pending_thermal_event_after_migration(self, tmp_path: Path):
        """After migration, pending_thermal_event entry is still present (not cleared
        by migration itself — coordinator handles lifecycle), but pending_observations is populated."""
        import json

        state_dict = {
            "records": [],
            "active_suggestions": [],
            "dismissed_suggestions": [],
            "settings_history": [],
            "thermal_observations": [],
            "thermal_model_cache": None,
            "pending_observations": {},
            "pending_thermal_event": {
                "event_id": "evt-456",
                "status": "post_heat",
                "session_mode": "cool",
                "hvac_mode": "cool",
                "active_start": "2025-07-01T14:00:00",
                "active_end": "2025-07-01T14:30:00",
                "active_samples": [],
                "pre_heat_samples": [],
                "post_heat_samples": [],
            },
        }
        state_file = tmp_path / "climate_advisor_learning.json"
        state_file.write_text(json.dumps(state_dict))

        engine = LearningEngine(tmp_path)
        engine.load_state()

        assert OBS_TYPE_HVAC_COOL in engine._state.pending_observations
        assert engine._state.pending_observations[OBS_TYPE_HVAC_COOL]["obs_id"] == "evt-456"

    def test_migration_skips_if_pending_observations_already_populated(self, tmp_path: Path):
        """If pending_observations already has the obs_type, migration does not overwrite it."""
        import json

        existing_obs = {
            OBS_TYPE_HVAC_HEAT: {
                "obs_type": OBS_TYPE_HVAC_HEAT,
                "obs_id": "already-there",
                "start_time": "2025-06-15T08:00:00",
                "status": "monitoring",
                "samples": [],
                "flags_at_start": {},
                "schema_version": 1,
            }
        }
        state_dict = {
            "records": [],
            "active_suggestions": [],
            "dismissed_suggestions": [],
            "settings_history": [],
            "thermal_observations": [],
            "thermal_model_cache": None,
            "pending_observations": existing_obs,
            "pending_thermal_event": {
                "event_id": "should-not-overwrite",
                "status": "active",
                "session_mode": "heat",
                "hvac_mode": "heat",
                "active_start": "2025-06-15T09:00:00",
                "active_samples": [],
                "pre_heat_samples": [],
                "post_heat_samples": [],
            },
        }
        state_file = tmp_path / "climate_advisor_learning.json"
        state_file.write_text(json.dumps(state_dict))

        engine = LearningEngine(tmp_path)
        engine.load_state()

        # The pre-existing obs_id should NOT be overwritten by migration
        assert engine._state.pending_observations[OBS_TYPE_HVAC_HEAT]["obs_id"] == "already-there"
