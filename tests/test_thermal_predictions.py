"""Tests for Phase 1 (ramp anchor) and Phase 2B (gate bridge) fixes in
_build_predicted_indoor_future() (Issue #126).

Phase 1 — Ramp anchor fix (coordinator.py line ~4339):
  Off-day setpoint-schedule fallback anchors to current_indoor_temp when
  available, rather than outdoor+2°F.  The off-day branch triggers when
  THRESHOLD_MILD (60°F) <= forecast high < THRESHOLD_WARM (75°F).

Phase 2B — Gate bridge (coordinator.py lines ~4196-4214):
  When k_passive is absent but k_vent_window is learned, the ODE activates
  using k_vent_window as a proxy, so thermally inert homes get physics
  prediction instead of the ramp fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from custom_components.climate_advisor.coordinator import _build_predicted_indoor_future

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_PRED_CONFIG = {
    "comfort_heat": 70,
    "comfort_cool": 75,
    "setback_heat": 60,
    "setback_cool": 80,
    "wake_time": "06:30",
    "sleep_time": "22:30",
}

# Off-day outdoor temperature: THRESHOLD_MILD (60) <= temp < THRESHOLD_WARM (75)
# so _day_mode() returns "off".  Use 65°F to sit clearly in the mild band.
_OFF_DAY_OUTDOOR_F = 65.0

# Timestamps start at noon UTC to be comfortably inside the awake band
# (wake_time=06:30, sleep_time=22:30).
_NOW = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)


def _make_dt_util_mock(now: datetime) -> MagicMock:
    mock = MagicMock()
    mock.now.return_value = now
    mock.as_local.side_effect = lambda dt: dt
    mock.parse_datetime.side_effect = lambda s: datetime.fromisoformat(s) if s else None
    return mock


def _entry(dt: datetime, temp: float) -> dict:
    return {"datetime": dt.isoformat(), "temperature": temp}


def _call(entries, *, now=_NOW, indoor=None, model=None, config=None):
    cfg = config or _PRED_CONFIG
    with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
        return _build_predicted_indoor_future(
            entries,
            cfg,
            now,
            current_indoor_temp=indoor,
            thermal_model=model,
        )


# ---------------------------------------------------------------------------
# Phase 1 — Ramp anchor fix
# ---------------------------------------------------------------------------


class TestRampAnchor:
    """Off-day setpoint fallback anchors to current_indoor_temp when available.

    An off-day is one where THRESHOLD_MILD (60°F) <= forecast high < THRESHOLD_WARM (75°F).
    In that mode the HVAC is off and the old code anchored to outdoor+2°F, which was
    wrong by ~9°F for a thermally inert home sitting at 69°F with outdoor at 65°F.
    """

    def test_off_day_fallback_uses_indoor_seed_not_outdoor_plus_2(self):
        """When indoor=69°F and outdoor=65°F, fallback returns ~69°F not ~67°F.

        Prior behaviour: max(setback_heat=60, outdoor+2=67) = 67°F.
        Fixed behaviour: _t_current (69°F) used as anchor.
        """
        now = _NOW
        indoor_f = 69.0
        # Outdoor 65°F: off-day (THRESHOLD_MILD=60 ≤ 65 < THRESHOLD_WARM=75)
        entries = [_entry(now + timedelta(hours=i), _OFF_DAY_OUTDOOR_F) for i in range(1, 5)]

        result = _call(entries, now=now, indoor=indoor_f, model=None)

        assert len(result) == 4
        for point in result:
            # All off-day fallback entries should be anchored to indoor seed (69°F),
            # not outdoor+2 (= 67°F).
            assert point["temp"] == pytest.approx(indoor_f, abs=0.05), (
                f"Expected indoor anchor {indoor_f}°F, got {point['temp']}°F"
                f" (outdoor+2 would be {_OFF_DAY_OUTDOOR_F + 2.0}°F)"
            )

    def test_off_day_fallback_outdoor_plus_2_when_no_indoor(self):
        """When current_indoor_temp=None, fallback reverts to outdoor+2°F (original)."""
        now = _NOW
        entries = [_entry(now + timedelta(hours=i), _OFF_DAY_OUTDOOR_F) for i in range(1, 4)]

        result = _call(entries, now=now, indoor=None, model=None)

        assert len(result) == 3
        expected_temp = max(float(_PRED_CONFIG["setback_heat"]), _OFF_DAY_OUTDOOR_F + 2.0)
        for point in result:
            assert point["temp"] == pytest.approx(expected_temp, abs=0.05), (
                f"Expected outdoor+2 fallback {expected_temp}°F, got {point['temp']}°F"
            )

    def test_off_day_ramp_anchor_uses_indoor_seed_verbatim(self):
        """Indoor seed is used as-is without flooring to setback_heat.

        The ramp anchor does NOT apply the setback_heat floor — it uses the
        actual current indoor reading.  (setback_heat guards the outdoor+2°F
        branch only.)  Here indoor=62°F > setback_heat=60°F to confirm the
        value passes through correctly.
        """
        now = _NOW
        indoor_f = 62.0
        entries = [_entry(now + timedelta(hours=i), _OFF_DAY_OUTDOOR_F) for i in range(1, 4)]

        result = _call(entries, now=now, indoor=indoor_f, model=None)

        assert len(result) == 3
        for point in result:
            assert point["temp"] == pytest.approx(indoor_f, abs=0.05), (
                f"Expected indoor anchor {indoor_f}°F verbatim; got {point['temp']}°F"
            )


# ---------------------------------------------------------------------------
# Phase 2B — Gate bridge
# ---------------------------------------------------------------------------


class TestGateBridge:
    """k_vent_window acts as proxy k_passive when k_passive is absent."""

    def _vent_only_model(self, k_vent_window: float) -> dict:
        """Thermal model with ventilated observations only — no k_passive."""
        return {
            "confidence": "low",
            "k_passive": None,
            "k_vent_window": k_vent_window,
            "k_active_heat": None,
            "k_active_cool": None,
        }

    def test_gate_bridge_activates_physics_for_inert_home(self):
        """k_passive=None + k_vent_window=-0.001 → physics activates, not ramp fallback.

        Physics prediction for a near-inert home produces a flat trajectory
        close to the indoor seed.  The old ramp fallback (outdoor+2°F = 67°F)
        would be wrong by ~2°F for a home sitting at 69°F.
        """
        now = _NOW
        indoor_f = 69.0
        # Off-day: THRESHOLD_MILD(60) <= outdoor(65) < THRESHOLD_WARM(75)
        entries = [_entry(now + timedelta(hours=i), _OFF_DAY_OUTDOOR_F) for i in range(1, 6)]
        model = self._vent_only_model(k_vent_window=-0.001)

        result = _call(entries, now=now, indoor=indoor_f, model=model)

        assert len(result) == 5
        temps = [r["temp"] for r in result]
        # Physics with k≈0: indoor barely moves.  All temps should stay very close
        # to the seed (within 1°F over 5 hours for k=-0.001).
        for t in temps:
            assert abs(t - indoor_f) < 1.0, (
                f"Physics with k_vent_window≈0 should hold indoor near seed {indoor_f}°F; got {t}°F"
            )

    def test_gate_bridge_k_vent_window_zero_gives_flat_prediction(self):
        """k_vent_window=0.0 (perfectly inert home) → ODE is flat; indoor unchanged.

        With k_passive=0.0, exp(0*dt)=1 so the ODE reduces to T(t+dt)=T(t).
        """
        now = _NOW
        indoor_f = 72.0
        entries = [_entry(now + timedelta(hours=i), _OFF_DAY_OUTDOOR_F) for i in range(1, 5)]
        model = self._vent_only_model(k_vent_window=0.0)

        result = _call(entries, now=now, indoor=indoor_f, model=model)

        assert len(result) == 4
        for point in result:
            assert point["temp"] == pytest.approx(indoor_f, abs=0.1), (
                f"Perfectly inert home (k=0) should stay at {indoor_f}°F; got {point['temp']}°F"
            )

    def test_gate_bridge_does_not_activate_when_k_vent_window_positive(self):
        """k_vent_window > 0 is physically invalid → no bridge, ramp fallback used.

        The gate bridge guard `k_vent_window <= 0` rejects positive values, so
        the setpoint-schedule fallback runs instead.  On an off-day with a valid
        indoor seed the ramp anchor (Phase 1) returns the indoor seed.
        """
        now = _NOW
        indoor_f = 69.0
        entries = [_entry(now + timedelta(hours=i), _OFF_DAY_OUTDOOR_F) for i in range(1, 4)]
        # Positive k_vent_window — should NOT activate bridge
        model = {
            "confidence": "low",
            "k_passive": None,
            "k_vent_window": 0.05,  # positive, physically invalid
            "k_active_heat": None,
            "k_active_cool": None,
        }

        result = _call(entries, now=now, indoor=indoor_f, model=model)

        assert len(result) == 3
        # No bridge → setpoint-schedule fallback → off-day → ramp anchor returns indoor seed.
        for point in result:
            assert point["temp"] == pytest.approx(indoor_f, abs=0.05), (
                f"Fallback (no bridge): expected indoor anchor {indoor_f}°F; got {point['temp']}°F"
            )

    def test_gate_bridge_does_not_contaminate_when_k_passive_already_present(self):
        """When k_passive is already set, k_vent_window is not used as proxy.

        The bridge only activates when k_passive is None.  If a valid k_passive
        exists, the trajectory must follow k_passive, not k_vent_window.
        """
        now = _NOW
        indoor_f = 65.0
        # Heat day: outdoor 40°F < THRESHOLD_MILD(60) → mode="heat"
        outdoor_heat_day = 40.0
        entries = [_entry(now + timedelta(hours=i), outdoor_heat_day) for i in range(1, 4)]
        model = {
            "confidence": "low",
            "k_passive": -0.05,  # valid k_passive present
            "k_vent_window": -0.5,  # very different — must not override k_passive
            "k_active_heat": 3.0,
            "k_active_cool": -2.5,
        }

        result = _call(entries, now=now, indoor=indoor_f, model=model)

        assert len(result) == 3
        temps = [r["temp"] for r in result]
        # With k_passive=-0.05 and k_active_heat=3.0 on a heat day, indoor=65°F
        # below comfort_heat=70°F → HVAC heats → trajectory rises toward 70°F.
        # With k_passive=-0.5 (k_vent_window) heating would be far more aggressive
        # (loses heat rapidly → HVAC runs harder → overshoots faster).
        # Moderate rise confirms k_passive=-0.05 was used, not -0.5.
        assert temps[-1] > indoor_f, "Heating mode should raise indoor temps"
        assert temps[0] < 70.0, "k_passive=-0.05 path should not reach comfort_heat setpoint in 1hr"
