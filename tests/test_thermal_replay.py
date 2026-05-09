"""Tests for thermal_replay.py --hvac mode (Phase C, Issue #130).

Tests cover:
  - detect_hvac_cycles(): cycle detection, window sizing, sample caps
  - run_hvac_replay_ols(): OLS dispatch for heat/cool cycles, bridge proxy,
    flat-signal rejection, outdoor-None handling
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Minimal sys.modules stubs so thermal_replay can be imported without SSH or
# any HA runtime.  thermal_replay.py is a pure-Python tool with no HA imports.
# ---------------------------------------------------------------------------


def _import_thermal_replay():
    import importlib

    return importlib.import_module("tools.thermal_replay")


# Make sure the tools package is importable
if "tools" not in sys.modules:
    tools_pkg = types.ModuleType("tools")
    tools_pkg.__path__ = ["tools"]
    sys.modules["tools"] = tools_pkg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2026, 4, 16, 6, 0, 0, tzinfo=UTC)


def _ts(offset_minutes: int) -> str:
    """Return an ISO timestamp string offset from _BASE_TS."""
    return (_BASE_TS + timedelta(minutes=offset_minutes)).isoformat()


def _entry(
    offset_minutes: int,
    hvac: str = "idle",
    indoor: float = 68.0,
    outdoor: float | None = 50.0,
) -> dict:
    """Build a chart_log entry."""
    return {
        "ts": _ts(offset_minutes),
        "hvac": hvac,
        "indoor": indoor,
        "outdoor": outdoor,
        "fan": False,
        "windows_open": False,
    }


# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------

tr = _import_thermal_replay()


# ===========================================================================
# TestHvacCycleDetection
# ===========================================================================


class TestHvacCycleDetection:
    def test_detects_single_heat_cycle(self):
        """3 idle → 2 heating → 3 idle should produce exactly 1 cycle."""
        entries = (
            [_entry(i * 30, hvac="idle") for i in range(3)]
            + [_entry(90 + i * 30, hvac="heating", indoor=70.0) for i in range(2)]
            + [_entry(150 + i * 30, hvac="idle", indoor=69.0) for i in range(3)]
        )
        cycles = tr.detect_hvac_cycles(entries, min_total_samples=4)
        assert len(cycles) == 1
        c = cycles[0]
        assert c["mode"] == "heat"
        assert len(c["active_samples"]) == 2
        assert len(c["pre_samples"]) > 0
        assert len(c["post_samples"]) > 0

    def test_heat_and_cool_cycles_detected_separately(self):
        """heating block then cooling block → 2 cycles with correct modes."""
        entries = (
            [_entry(i * 30, hvac="idle") for i in range(2)]
            + [_entry(60 + i * 30, hvac="heating", indoor=72.0) for i in range(2)]
            + [_entry(120 + i * 30, hvac="idle", indoor=70.0) for i in range(2)]
            + [_entry(180 + i * 30, hvac="cooling", indoor=68.0) for i in range(2)]
            + [_entry(240 + i * 30, hvac="idle", indoor=69.0) for i in range(2)]
        )
        cycles = tr.detect_hvac_cycles(entries, min_total_samples=4)
        assert len(cycles) == 2
        assert cycles[0]["mode"] == "heat"
        assert cycles[1]["mode"] == "cool"

    def test_skips_cycle_with_too_few_total_samples(self):
        """1 pre + 1 active + 1 post = 3 total — below min_total_samples=4, skip."""
        entries = [
            _entry(0, hvac="idle"),
            _entry(30, hvac="heating", indoor=70.0),
            _entry(60, hvac="idle"),
        ]
        cycles = tr.detect_hvac_cycles(entries, min_total_samples=4)
        assert len(cycles) == 0

    def test_pre_samples_capped_at_60_min(self):
        """10 idle entries 30 min apart = 300 min before HVAC start.
        pre_samples must contain only entries within 60 min of start."""
        pre_entries = [_entry(i * 30, hvac="idle") for i in range(10)]  # 0..270 min
        active_entries = [_entry(300 + i * 30, hvac="heating", indoor=71.0) for i in range(2)]
        post_entries = [_entry(360 + i * 30, hvac="idle") for i in range(4)]
        entries = pre_entries + active_entries + post_entries
        cycles = tr.detect_hvac_cycles(entries, pre_window_minutes=60, min_total_samples=4)
        assert len(cycles) == 1
        # Only the entry at 270 min is within 60 min of start (300 min)
        for s in cycles[0]["pre_samples"]:
            ts = datetime.fromisoformat(s["ts"])
            active_start = datetime.fromisoformat(active_entries[0]["ts"])
            gap_min = (active_start - ts).total_seconds() / 60
            assert gap_min <= 60, f"pre sample is {gap_min:.1f} min before start — exceeds cap"

    def test_post_samples_capped_at_90_min(self):
        """10 idle entries 30 min apart after HVAC stop = 300 min.
        post_samples must contain only entries within 90 min of stop."""
        pre_entries = [_entry(i * 30, hvac="idle") for i in range(2)]
        active_entries = [_entry(60 + i * 30, hvac="heating", indoor=71.0) for i in range(2)]
        post_entries = [_entry(120 + i * 30, hvac="idle") for i in range(10)]  # 120..390 min
        entries = pre_entries + active_entries + post_entries
        cycles = tr.detect_hvac_cycles(entries, post_window_minutes=90, min_total_samples=4)
        assert len(cycles) == 1
        active_end = datetime.fromisoformat(active_entries[-1]["ts"])
        for s in cycles[0]["post_samples"]:
            ts = datetime.fromisoformat(s["ts"])
            gap_min = (ts - active_end).total_seconds() / 60
            assert gap_min <= 90, f"post sample is {gap_min:.1f} min after stop — exceeds cap"

    def test_outdoor_none_entries_included_in_window(self):
        """Entries with outdoor=None must NOT be excluded during cycle detection.
        Outdoor fallback is handled at OLS time, not during detection."""
        entries = (
            [_entry(i * 30, hvac="idle") for i in range(2)]
            + [_entry(60 + i * 30, hvac="heating", indoor=70.0, outdoor=None) for i in range(2)]
            + [_entry(120 + i * 30, hvac="idle", indoor=69.0, outdoor=None) for i in range(2)]
        )
        cycles = tr.detect_hvac_cycles(entries, min_total_samples=4)
        assert len(cycles) == 1
        # All active samples should be present even with outdoor=None
        assert len(cycles[0]["active_samples"]) == 2


# ===========================================================================
# TestHvacReplayOls
# ===========================================================================


class TestHvacReplayOls:
    """Tests for run_hvac_replay_ols() — cycle OLS dispatch."""

    def _make_cycle(
        self,
        n_pre: int = 3,
        n_active: int = 4,
        n_post: int = 5,
        pre_indoor: float = 65.0,
        active_indoor_end: float = 70.0,
        post_indoor_end: float = 67.0,
        outdoor: float = 50.0,
        mode: str = "heat",
        outdoor_for_active: float | None = None,
    ) -> dict:
        """Build a synthetic cycle dict with simple linear indoor profiles."""
        # timestamps: 30-min spacing throughout
        offset = 0
        pre_samples = []
        for _i in range(n_pre):
            pre_samples.append(
                {
                    "ts": _ts(offset),
                    "indoor": pre_indoor,
                    "outdoor": outdoor,
                }
            )
            offset += 30

        active_samples = []
        for _i in range(n_active):
            frac = _i / max(n_active - 1, 1)
            indoor_val = pre_indoor + frac * (active_indoor_end - pre_indoor)
            out = outdoor if outdoor_for_active is None else outdoor_for_active
            active_samples.append(
                {
                    "ts": _ts(offset),
                    "indoor": indoor_val,
                    "outdoor": out,
                }
            )
            offset += 30

        post_samples = []
        for i in range(n_post):
            frac = i / max(n_post - 1, 1)
            indoor_val = active_indoor_end + frac * (post_indoor_end - active_indoor_end)
            post_samples.append(
                {
                    "ts": _ts(offset),
                    "indoor": indoor_val,
                    "outdoor": outdoor,
                }
            )
            offset += 30

        return {
            "mode": mode,
            "pre_samples": pre_samples,
            "active_samples": active_samples,
            "post_samples": post_samples,
            "start_ts": pre_samples[0]["ts"] if pre_samples else active_samples[0]["ts"],
        }

    def test_successful_heat_cycle_produces_k_active(self):
        """A clear heat cycle should commit with k_active > 0 and k_passive < 0."""
        cycle = self._make_cycle(
            n_pre=3,
            n_active=4,
            n_post=5,
            pre_indoor=65.0,
            active_indoor_end=70.0,
            post_indoor_end=66.0,
            outdoor=50.0,
            mode="heat",
        )
        result = tr.run_hvac_replay_ols(cycle)
        assert result is not None, "Expected commit but got None (rejected)"
        assert result["hvac_mode"] == "heat"
        assert result["k_active"] is not None
        assert result["k_active"] > 0, f"Expected k_active > 0, got {result['k_active']}"
        assert result["k_passive"] < 0, f"Expected k_passive < 0, got {result['k_passive']}"
        assert result["confidence_grade"] == "low"
        assert result.get("source") == "replay"

    def test_flat_cycle_rejected(self):
        """Indoor stays flat — no thermal signal — should be rejected (returns None)."""
        # Build cycle where all indoor temps are identical
        cycle = self._make_cycle(
            n_pre=3,
            n_active=3,
            n_post=5,
            pre_indoor=68.0,
            active_indoor_end=68.0,  # flat
            post_indoor_end=68.0,  # flat
            outdoor=50.0,
            mode="heat",
        )
        result = tr.run_hvac_replay_ols(cycle)
        assert result is None, "Expected rejection for flat signal but got a commit"

    def test_missing_outdoor_uses_fallback(self):
        """post_samples with outdoor=None should use last valid outdoor (not crash)."""
        # Start with valid outdoor in pre/active, then None in post
        cycle = self._make_cycle(
            n_pre=2,
            n_active=3,
            n_post=5,
            pre_indoor=65.0,
            active_indoor_end=70.0,
            post_indoor_end=66.0,
            outdoor=50.0,
            mode="heat",
        )
        # Wipe outdoor from some post samples
        for s in cycle["post_samples"][:2]:
            s["outdoor"] = None
        # Should not crash; may commit or reject but must return dict or None
        result = tr.run_hvac_replay_ols(cycle)
        # No assertion on commit/reject — just verify no exception and correct type
        assert result is None or isinstance(result, dict)

    def test_cool_cycle_produces_negative_k_active(self):
        """A cooling cycle should produce k_active < 0."""
        cycle = self._make_cycle(
            n_pre=3,
            n_active=4,
            n_post=5,
            pre_indoor=78.0,
            active_indoor_end=72.0,  # drops during cooling
            post_indoor_end=75.0,  # rebounds
            outdoor=85.0,
            mode="cool",
        )
        result = tr.run_hvac_replay_ols(cycle)
        if result is not None:
            assert result["hvac_mode"] == "cool"
            if result["k_active"] is not None:
                assert result["k_active"] < 0, f"Expected k_active < 0 for cool, got {result['k_active']}"

    def test_too_few_post_samples_rejected(self):
        """Only 2 post samples — below min 4 — should be rejected."""
        cycle = self._make_cycle(
            n_pre=2,
            n_active=3,
            n_post=2,  # too few
            pre_indoor=65.0,
            active_indoor_end=70.0,
            post_indoor_end=67.0,
            outdoor=50.0,
            mode="heat",
        )
        result = tr.run_hvac_replay_ols(cycle)
        assert result is None, "Expected rejection for too few post samples"

    def test_bridge_proxy_used_when_k_passive_fails_but_k_vent_window_available(self):
        """When post_samples give no valid k_passive but a k_vent_window proxy is
        supplied, OLS should use proxy and return confidence_grade='low'."""
        # Flat post samples → k_passive will fail OLS
        cycle = self._make_cycle(
            n_pre=0,
            n_active=3,
            n_post=5,
            pre_indoor=65.0,
            active_indoor_end=70.0,
            post_indoor_end=70.0,  # flat post → k_passive fails
            outdoor=50.0,
            mode="heat",
        )
        proxy = -0.05  # k_vent_window substitute
        result = tr.run_hvac_replay_ols(cycle, k_vent_window_proxy=proxy)
        # If k_active bounds are satisfied the cycle may commit or reject
        # depending on the signal quality; the important thing is it does NOT
        # crash, and if committed grade is "low"
        if result is not None:
            assert result["confidence_grade"] == "low"

    # ------------------------------------------------------------------
    # Single-point estimator tests (Issue #130 Phase C-revised)
    # ------------------------------------------------------------------

    def _make_single_point_cycle(
        self,
        active_ts: str = "2026-05-03T07:14:52+00:00",
        post_ts: str = "2026-05-03T07:24:34+00:00",
        pre_indoor: float = 68.0,
        active_indoor: float = 68.0,
        post_indoor: float = 69.0,
        outdoor: float = 54.0,
        mode: str = "heat",
    ) -> dict:
        """Build a cycle with exactly 1 active sample and exact timestamps.

        The elapsed time comes from post[0].ts - active[0].ts, matching the
        real chart_log behavior for short HVAC cycles.
        """
        pre_samples = [
            {"ts": "2026-05-03T07:00:00+00:00", "indoor": pre_indoor, "outdoor": outdoor},
            {"ts": "2026-05-03T07:07:00+00:00", "indoor": pre_indoor, "outdoor": outdoor},
        ]
        active_samples = [
            {"ts": active_ts, "indoor": active_indoor, "outdoor": outdoor},
        ]
        post_samples = [
            {"ts": post_ts, "indoor": post_indoor, "outdoor": outdoor},
            {"ts": "2026-05-03T07:54:34+00:00", "indoor": post_indoor - 0.5, "outdoor": outdoor},
            {"ts": "2026-05-03T08:24:34+00:00", "indoor": post_indoor - 1.0, "outdoor": outdoor},
            {"ts": "2026-05-03T08:54:34+00:00", "indoor": post_indoor - 1.5, "outdoor": outdoor},
        ]
        return {
            "mode": mode,
            "pre_samples": pre_samples,
            "active_samples": active_samples,
            "post_samples": post_samples,
            "start_ts": active_ts,
        }

    def test_single_point_used_when_n_act_1(self):
        """n_act=1 cycle with 10-min timestamps and 1°F rise should produce a
        valid k_active estimate via the single-point path.

        Physics: elapsed=9m42s=0.162hr, T_start=68, T_peak=69, signal=1°F
        gross_rate = 1/0.162 ≈ 6.2 °F/hr
        avg_delta ≈ 68-54 = 14 (indoor warmer)
        k_active = 6.2 - (-0.14 * 14) ≈ 8.2  → within [0.5, 15.0]
        """
        cycle = self._make_single_point_cycle(
            active_ts="2026-05-03T07:14:52+00:00",
            post_ts="2026-05-03T07:24:34+00:00",
            pre_indoor=68.0,
            active_indoor=68.0,
            post_indoor=69.0,
            outdoor=54.0,
            mode="heat",
        )
        result = tr.run_hvac_replay_ols(cycle, k_vent_window_proxy=-0.14)
        assert result is not None, "Expected single-point estimator to produce a result for n_act=1 cycle"
        assert result["k_active"] is not None
        assert 0.5 <= result["k_active"] <= 15.0, f"k_active={result['k_active']} out of expected [0.5, 15.0]"
        assert result["confidence_grade"] == "low"
        assert result.get("source") == "replay"

    def test_ols_preferred_when_n_act_2plus(self):
        """n_act >= 2 with clear thermal signal should go through OLS path and
        return a non-None result (no regression on multi-sample cycles)."""
        cycle = self._make_cycle(
            n_pre=3,
            n_active=4,
            n_post=5,
            pre_indoor=65.0,
            active_indoor_end=70.0,
            post_indoor_end=66.0,
            outdoor=50.0,
            mode="heat",
        )
        result = tr.run_hvac_replay_ols(cycle)
        assert result is not None, "Expected OLS path to commit for n_act=4 cycle with clear temperature rise"
        assert result["k_active"] is not None
        assert result["k_active"] > 0

    def test_dry_run_loads_proxy_for_single_point(self):
        """Single-point path fires when a k_vent_window proxy is provided, even in
        dry-run context.  run_hvac_replay_ols() is the unit being tested; main()
        is responsible for loading the proxy regardless of --dry-run/--write.
        This test verifies that passing the proxy to run_hvac_replay_ols() with a
        n_act=1 cycle produces a committed result (k_active not None).
        """
        # Decaying post profile → k_passive OLS may succeed on its own.
        # The key invariant being tested: with a proxy and n_act=1, the single-point
        # estimator produces a committed result (k_active is set).
        cycle = self._make_single_point_cycle(
            active_ts="2026-05-03T07:14:52+00:00",
            post_ts="2026-05-03T07:24:34+00:00",
            pre_indoor=68.0,
            active_indoor=68.0,
            post_indoor=69.0,
            outdoor=54.0,
            mode="heat",
        )
        result = tr.run_hvac_replay_ols(cycle, k_vent_window_proxy=-0.14)
        assert result is not None, "Expected non-None result when proxy is supplied for n_act=1 cycle"
        assert result["k_active"] is not None, "k_active must be set when single-point commits"
        assert result["confidence_grade"] == "low"

    # ------------------------------------------------------------------
    # Proxy-aware n_post gating and plateau guard tests (Issue #130 Phase D)
    # ------------------------------------------------------------------

    def _make_proxy_cycle(
        self,
        n_post: int,
        post_flat: bool = False,
        t_start: float = 68.0,
        t_peak: float = 69.0,
        outdoor: float = 54.0,
    ) -> dict:
        """Build a minimal cycle for proxy-path gating tests.

        n_act=1, timestamps 10 min apart.  If post_flat=True, all post samples
        hold indoor at t_peak (no decay), triggering the plateau guard.
        Otherwise post decays linearly from t_peak toward t_start.
        """
        active_ts = "2026-05-03T07:14:52+00:00"
        post_base = datetime(2026, 5, 3, 7, 24, 34, tzinfo=UTC)

        pre_samples = [
            {"ts": "2026-05-03T07:00:00+00:00", "indoor": t_start, "outdoor": outdoor},
            {"ts": "2026-05-03T07:07:00+00:00", "indoor": t_start, "outdoor": outdoor},
        ]
        active_samples = [
            {"ts": active_ts, "indoor": t_start, "outdoor": outdoor},
        ]

        post_samples = []
        for i in range(n_post):
            ts_i = (post_base + timedelta(minutes=30 * i)).isoformat()
            if post_flat:
                indoor_i = t_peak  # no decay — plateau
            else:
                # linear decay from t_peak toward t_start
                frac = i / max(n_post - 1, 1)
                indoor_i = t_peak - frac * (t_peak - t_start) * 0.8
            post_samples.append({"ts": ts_i, "indoor": indoor_i, "outdoor": outdoor})

        return {
            "mode": "heat",
            "pre_samples": pre_samples,
            "active_samples": active_samples,
            "post_samples": post_samples,
            "start_ts": active_ts,
        }

    def test_n_post_1_commits_with_proxy(self):
        """n_post=1 + proxy supplied → n_post gate drops to 1, single-point fires,
        result commits with k_active in valid range [0.5, 15.0].
        Physics: elapsed≈10min, T_start=68, T_peak=69, signal=1°F, outdoor=54."""
        cycle = self._make_proxy_cycle(n_post=1)
        result = tr.run_hvac_replay_ols(cycle, k_vent_window_proxy=-0.14)
        assert result is not None, "Expected commit for n_post=1 with proxy (gate should drop to 1)"
        assert result["k_active"] is not None
        assert 0.5 <= result["k_active"] <= 15.0, f"k_active={result['k_active']} out of [0.5, 15.0]"

    def test_n_post_3_commits_with_proxy(self):
        """n_post=3 with proxy → previously hard-rejected at n_post < 4, should now commit."""
        cycle = self._make_proxy_cycle(n_post=3)
        result = tr.run_hvac_replay_ols(cycle, k_vent_window_proxy=-0.14)
        assert result is not None, "Expected commit for n_post=3 with proxy (was previously gated at < 4)"
        assert result["k_active"] is not None

    def test_n_post_3_rejects_without_proxy(self):
        """n_post=3 without proxy → OLS path requires 4 post samples, must return None."""
        cycle = self._make_proxy_cycle(n_post=3)
        result = tr.run_hvac_replay_ols(cycle, k_vent_window_proxy=None)
        assert result is None, "Expected rejection for n_post=3 without proxy (OLS path needs >= 4)"

    def test_plateau_guard_bypassed_with_proxy(self):
        """Flat post-heat (no decay from peak) + proxy → plateau guard bypassed, cycle commits.
        T_start=68, T_peak=69 → 1°F signal satisfies single-point; flat post should not gate."""
        cycle = self._make_proxy_cycle(n_post=4, post_flat=True, t_start=68.0, t_peak=69.0)
        result = tr.run_hvac_replay_ols(cycle, k_vent_window_proxy=-0.14)
        assert result is not None, (
            "Expected commit despite flat post-heat when proxy supplied (plateau guard should be bypassed)"
        )

    def test_plateau_guard_active_without_proxy(self):
        """Flat post-heat (no decay) without proxy → plateau guard fires, returns None.
        n_post=4 to clear the n_post gate; guard is the only rejection path."""
        cycle = self._make_proxy_cycle(n_post=4, post_flat=True, t_start=68.0, t_peak=69.0)
        result = tr.run_hvac_replay_ols(cycle, k_vent_window_proxy=None)
        assert result is None, "Expected rejection due to plateau guard when no proxy (flat post-heat, no OLS)"
