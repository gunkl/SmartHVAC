"""Tests for Phase 1 (ramp anchor), Phase 2B (gate bridge), and Phase 2C
(per-hour ventilation wiring) fixes in _build_predicted_indoor_future()
(Issue #126).

Phase 1 — Ramp anchor fix (coordinator.py line ~4339):
  Off-day setpoint-schedule fallback anchors to current_indoor_temp when
  available, rather than outdoor+2°F.  The off-day branch triggers when
  THRESHOLD_MILD (60°F) <= forecast high < THRESHOLD_WARM (75°F).

Phase 2B — Gate bridge (coordinator.py lines ~4196-4214):
  When k_passive is absent but k_vent_window is learned, the ODE activates
  using k_vent_window as a proxy, so thermally inert homes get physics
  prediction instead of the ramp fallback.

Phase 2C — Per-hour ventilation wiring (coordinator.py _build_predicted_indoor_future):
  For forecast hours where classification recommends windows open, k_vent_window
  is substituted as the effective k_passive in the ODE (replacement semantics —
  k_vent_window is the total measured k during ventilated conditions).
  Gate bridge guard: when _k_passive_via_bridge=True (k_passive was None,
  k_vent_window already used as proxy for all hours), per-hour substitution does
  not fire to avoid double-counting.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
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


# ---------------------------------------------------------------------------
# Phase 2C — Per-hour ventilation wiring
# ---------------------------------------------------------------------------


def _make_classification(
    *,
    windows_recommended: bool = False,
    window_open_time: time | None = None,
    window_close_time: time | None = None,
) -> MagicMock:
    """Build a minimal classification mock with window schedule attributes."""
    cls = MagicMock()
    cls.windows_recommended = windows_recommended
    cls.window_open_time = window_open_time
    cls.window_close_time = window_close_time
    return cls


def _call_vc(
    entries,
    *,
    now=_NOW,
    indoor: float,
    model: dict,
    classification=None,
    config=None,
):
    """Call _build_predicted_indoor_future with optional classification arg."""
    cfg = config or _PRED_CONFIG
    with patch("custom_components.climate_advisor.coordinator.dt_util", _make_dt_util_mock(now)):
        return _build_predicted_indoor_future(
            entries,
            cfg,
            now,
            current_indoor_temp=indoor,
            thermal_model=model,
            classification=classification,
        )


class TestPerHourVentilationWiring:
    """Per-hour k_vent_window substitution in the physics ODE (Phase 2C / Issue #126).

    For forecast hours where the classification recommends windows open,
    k_vent_window replaces k_passive as the effective decay rate in the ODE.
    k_vent_window is the TOTAL measured k during ventilated conditions
    (replacement semantics, not addition).
    """

    # Base "warm off-day" timestamps: outlook 06:00–23:00 local today.
    # Outdoor=60°F sits in THRESHOLD_MILD(60) ≤ T < THRESHOLD_WARM(75) → mode="off".
    # All entries are in the future relative to _NOW (noon UTC = noon local via mock).
    _OUTDOOR = 60.0
    _INDOOR = 72.0
    # Use a fresh NOW so "future" entries are unambiguous
    _NOW_VC = datetime(2026, 4, 20, 8, 0, 0, tzinfo=UTC)

    def _entries(self, now: datetime, hours: list[int]) -> list[dict]:
        """Build forecast entries at specified UTC hours (same day as now)."""
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return [_entry(base + timedelta(hours=h), self._OUTDOOR) for h in hours]

    def test_window_open_hours_use_k_vent_window(self):
        """During window-open hours, k_vent_window drives a weaker decay than k_passive.

        Setup: k_passive=-0.30 (strong decay toward outdoor at 60°F),
               k_vent_window=-0.10 (weaker decay — insulated by window schedule),
               windows_recommended=True, open 10:00–17:00, outdoor=60°F, indoor=72°F.

        k_vent_window=-0.10 is a weaker negative, so indoor stays HIGHER during
        window hours than with k_passive=-0.30 (which drives indoor toward 60°F faster).

        Assertion: temps during 10:00–17:00 are higher in the k_vent_window run
        than in a baseline run without k_vent_window.
        Pre-window temps (09:00–10:00) should be approximately equal (same k_passive).

        Note: _NOW_VC is 08:00, so hour-8 entry equals now and is filtered out.
        Entries start at hour 9 (strictly after now).
        """
        now = self._NOW_VC
        # Hours: 9 (before window), 10–16 (window open), 17–18 (after)
        hours = list(range(9, 19))
        entries = self._entries(now, hours)

        model_with_kv = {
            "confidence": "low",
            "k_passive": -0.30,
            "k_vent_window": -0.10,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        model_without_kv = {
            "confidence": "low",
            "k_passive": -0.30,
            "k_vent_window": None,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        classification = _make_classification(
            windows_recommended=True,
            window_open_time=time(10, 0),
            window_close_time=time(17, 0),
        )

        result_with = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_with_kv, classification=classification
        )
        result_without = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_without_kv, classification=classification
        )

        assert len(result_with) == len(result_without) == len(hours)

        # Build lookup by hour
        def _hour(ts_str: str) -> int:
            return datetime.fromisoformat(ts_str).hour

        with_by_h = {_hour(r["ts"]): r["temp"] for r in result_with}
        without_by_h = {_hour(r["ts"]): r["temp"] for r in result_without}

        # During window-open hours: k_vent_window (-0.10) is weaker decay than k_passive (-0.30)
        # so indoor stays higher (closer to 72°F than to outdoor 60°F).
        for h in range(10, 17):
            assert with_by_h[h] > without_by_h[h], (
                f"Hour {h}: k_vent_window run should be warmer than k_passive run; "
                f"got {with_by_h[h]:.2f} vs {without_by_h[h]:.2f}"
            )

        # Before the window: same k_passive used in both — temps should be approximately equal.
        # Only hour 9 is strictly before the window (hour 8 equals now and is filtered).
        for h in [9]:
            assert abs(with_by_h[h] - without_by_h[h]) < 0.5, (
                f"Hour {h}: pre-window temps should match; got {with_by_h[h]:.2f} vs {without_by_h[h]:.2f}"
            )

    def test_window_closed_hours_use_k_passive_unchanged(self):
        """When windows_recommended=False, no substitution fires — all hours use k_passive.

        Same k_passive and k_vent_window as above, but classification has
        windows_recommended=False.  Predictions must match a run with no k_vent_window.
        """
        now = self._NOW_VC
        hours = list(range(9, 18))
        entries = self._entries(now, hours)

        model_with_kv = {
            "confidence": "low",
            "k_passive": -0.30,
            "k_vent_window": -0.10,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        model_without_kv = {
            "confidence": "low",
            "k_passive": -0.30,
            "k_vent_window": None,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        classification_closed = _make_classification(
            windows_recommended=False,
            window_open_time=time(10, 0),
            window_close_time=time(17, 0),
        )

        result_with = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_with_kv, classification=classification_closed
        )
        result_without = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_without_kv, classification=classification_closed
        )

        assert len(result_with) == len(result_without)
        for r_w, r_wo in zip(result_with, result_without, strict=True):
            assert r_w["temp"] == pytest.approx(r_wo["temp"], abs=0.01), (
                f"windows_recommended=False: temps should be identical at {r_w['ts']}; "
                f"got {r_w['temp']} vs {r_wo['temp']}"
            )

    def test_k_vent_window_none_no_change(self):
        """k_vent_window=None prevents substitution even if windows_recommended=True.

        Guard condition: `_k_vent_window is not None` must be checked before
        any per-hour substitution.  With k_vent_window=None, all hours must use
        k_passive and predictions must equal a windows_recommended=False run.
        """
        now = self._NOW_VC
        hours = list(range(9, 18))
        entries = self._entries(now, hours)

        model_kv_none = {
            "confidence": "low",
            "k_passive": -0.30,
            "k_vent_window": None,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        classification_open = _make_classification(
            windows_recommended=True,
            window_open_time=time(10, 0),
            window_close_time=time(17, 0),
        )
        classification_closed = _make_classification(windows_recommended=False)

        result_open = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_kv_none, classification=classification_open
        )
        result_closed = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model_kv_none, classification=classification_closed
        )

        assert len(result_open) == len(result_closed)
        for r_o, r_c in zip(result_open, result_closed, strict=True):
            assert r_o["temp"] == pytest.approx(r_c["temp"], abs=0.01), (
                f"k_vent_window=None guard: temps should match at {r_o['ts']}; got {r_o['temp']} vs {r_c['temp']}"
            )

    def test_inert_home_window_open_solar_drives_warming(self):
        """k_vent_window=0.0 (perfectly inert) + k_solar: solar gain drives indoor up.

        With k_vent_window=0.0, the passive ODE term vanishes: dT/dt ≈ k_solar*solar_factor.
        During daytime window hours (peak solar_factor > 0), indoor should rise above seed.
        After window close (no solar at hour 20), indoor should stay near the noon peak.

        This validates that solar+ventilation interact correctly without requiring
        k_vent_window != 0 to see any movement.
        """
        now = self._NOW_VC
        # Daytime hours inside window (8–17) and one post-close hour (20)
        # Outdoor=65°F; high=65°F → off-day mode → HVAC off → pure passive+solar ODE.
        outdoor = 65.0
        hours = list(range(9, 21))
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        entries = [_entry(base + timedelta(hours=h), outdoor) for h in hours]

        model = {
            "confidence": "low",
            "k_passive": -0.20,
            "k_vent_window": 0.0,
            "k_solar": 2.5,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        classification = _make_classification(
            windows_recommended=True,
            window_open_time=time(8, 0),
            window_close_time=time(18, 0),
        )

        result = _call_vc(entries, now=now, indoor=70.0, model=model, classification=classification)

        assert len(result) == len(hours)
        temps_by_h = {datetime.fromisoformat(r["ts"]).hour: r["temp"] for r in result}

        # At noon (hour 12), solar_factor should be near peak — indoor should be above seed (70°F).
        assert temps_by_h[12] > 70.0, (
            f"Inert home with solar should warm above 70°F by noon; got {temps_by_h[12]:.2f}°F"
        )

    def test_gate_bridge_home_no_double_counting(self):
        """When gate bridge is active (k_passive=None), per-hour substitution must NOT fire.

        The gate bridge sets k_passive = k_vent_window for ALL hours.
        If per-hour substitution also fires, k_vent_window would be used twice
        (no change in value, but the semantics would be wrong if k_vent_window
        were ever replaced with a different value in the bridge path).

        The observable guard: predictions with windows_recommended=True and
        windows_recommended=False must be IDENTICAL when the bridge is active.
        This confirms the `not _k_passive_via_bridge` guard is working.
        """
        now = self._NOW_VC
        hours = list(range(9, 18))
        entries = self._entries(now, hours)

        # k_passive=None → gate bridge activates, uses k_vent_window=-0.20 as proxy
        model = {
            "confidence": "low",
            "k_passive": None,
            "k_vent_window": -0.20,
            "k_active_heat": None,
            "k_active_cool": None,
        }
        classification_open = _make_classification(
            windows_recommended=True,
            window_open_time=time(10, 0),
            window_close_time=time(17, 0),
        )
        classification_closed = _make_classification(windows_recommended=False)

        result_open = _call_vc(entries, now=now, indoor=self._INDOOR, model=model, classification=classification_open)
        result_closed = _call_vc(
            entries, now=now, indoor=self._INDOOR, model=model, classification=classification_closed
        )

        assert len(result_open) == len(result_closed)
        for r_o, r_c in zip(result_open, result_closed, strict=True):
            assert r_o["temp"] == pytest.approx(r_c["temp"], abs=0.01), (
                f"Gate bridge guard: temps must be identical at {r_o['ts']} "
                f"regardless of windows_recommended; got {r_o['temp']} vs {r_c['temp']}"
            )
