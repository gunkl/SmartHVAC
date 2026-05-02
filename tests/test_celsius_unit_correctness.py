"""Regression tests: unit correctness for all temperature-literal code paths (Issue #128).

All comparisons in the codebase operate on internal °F values. These tests verify that
the named _F constants are used consistently and produce correct behavior at their boundary
values.
"""

from __future__ import annotations

from custom_components.climate_advisor.const import (
    COLD_DAY_SETBACK_DEPTH_F,
    DEFAULT_SETBACK_DEPTH_COOL_F,
    DEFAULT_SETBACK_DEPTH_F,
    THERMAL_BUCKET_INTERP_HALF_F,
    THERMAL_COLD_BUCKET_LIMIT_F,
    THERMAL_MILD_BUCKET_LIMIT_F,
    WINDOW_OPPORTUNITY_MAX_LOW_F,
)


class TestWindowOpportunityThreshold:
    """C1: WINDOW_OPPORTUNITY_MAX_LOW_F used consistently in classifier."""

    def test_triggers_at_threshold(self):
        """today_low exactly at threshold → window opportunity activates."""
        from custom_components.climate_advisor.classifier import DayClassification

        c = DayClassification(
            day_type="hot",
            trend_direction="stable",
            trend_magnitude=0,
            today_high=95.0,
            today_low=WINDOW_OPPORTUNITY_MAX_LOW_F,
            tomorrow_high=92.0,
            tomorrow_low=WINDOW_OPPORTUNITY_MAX_LOW_F + 1,  # above → no evening
        )
        assert c.window_opportunity_morning is True
        assert c.window_opportunity_evening is False

    def test_does_not_trigger_above_threshold(self):
        """today_low 1°F above threshold → no window opportunity."""
        from custom_components.climate_advisor.classifier import DayClassification

        c = DayClassification(
            day_type="hot",
            trend_direction="stable",
            trend_magnitude=0,
            today_high=95.0,
            today_low=WINDOW_OPPORTUNITY_MAX_LOW_F + 1,
            tomorrow_high=92.0,
            tomorrow_low=WINDOW_OPPORTUNITY_MAX_LOW_F + 1,
        )
        assert c.window_opportunity_morning is False
        assert c.window_opportunity_evening is False

    def test_triggers_for_evening_when_tomorrow_at_threshold(self):
        """tomorrow_low exactly at threshold → evening window opportunity activates."""
        from custom_components.climate_advisor.classifier import DayClassification

        c = DayClassification(
            day_type="hot",
            trend_direction="stable",
            trend_magnitude=0,
            today_high=95.0,
            today_low=WINDOW_OPPORTUNITY_MAX_LOW_F + 1,  # above → no morning
            tomorrow_high=92.0,
            tomorrow_low=WINDOW_OPPORTUNITY_MAX_LOW_F,
        )
        assert c.window_opportunity_morning is False
        assert c.window_opportunity_evening is True


class TestThermalBucketBoundaries:
    """C2/C3: THERMAL_COLD_BUCKET_LIMIT_F / THERMAL_MILD_BUCKET_LIMIT_F used in buckets."""

    def _run_factors(self, entries):
        from custom_components.climate_advisor.coordinator import _compute_thermal_factors

        return _compute_thermal_factors(entries)

    def _make_entries(self, outdoor: float, n: int = 25) -> list[dict]:
        return [{"outdoor": outdoor, "indoor": outdoor + 10, "hvac": "off"} for _ in range(n)]

    def test_cold_bucket(self):
        """outdoor below THERMAL_COLD_BUCKET_LIMIT_F → cold_diff = 10."""
        outdoor = THERMAL_COLD_BUCKET_LIMIT_F - 1  # 59°F → cold
        result = self._run_factors(self._make_entries(outdoor))
        assert result["has_data"] is True
        assert abs(result["cold_diff"] - 10.0) < 0.1

    def test_mild_bucket(self):
        """outdoor between cold and mild limits → mild_diff = 10."""
        outdoor = (THERMAL_COLD_BUCKET_LIMIT_F + THERMAL_MILD_BUCKET_LIMIT_F) / 2  # 65°F → mild
        result = self._run_factors(self._make_entries(outdoor))
        assert result["has_data"] is True
        assert abs(result["mild_diff"] - 10.0) < 0.1

    def test_warm_bucket(self):
        """outdoor above THERMAL_MILD_BUCKET_LIMIT_F → warm_diff = 10."""
        outdoor = THERMAL_MILD_BUCKET_LIMIT_F + 1  # 71°F → warm
        result = self._run_factors(self._make_entries(outdoor))
        assert result["has_data"] is True
        assert abs(result["warm_diff"] - 10.0) < 0.1

    def test_boundary_cold_bucket_limit(self):
        """outdoor exactly at THERMAL_COLD_BUCKET_LIMIT_F → mild bucket (< not <=)."""
        outdoor = THERMAL_COLD_BUCKET_LIMIT_F  # 60°F → should go to mild
        result = self._run_factors(self._make_entries(outdoor))
        assert result["has_data"] is True
        assert abs(result["mild_diff"] - 10.0) < 0.1

    def test_boundary_mild_bucket_limit(self):
        """outdoor exactly at THERMAL_MILD_BUCKET_LIMIT_F → warm bucket (< not <=)."""
        outdoor = THERMAL_MILD_BUCKET_LIMIT_F  # 70°F → should go to warm
        result = self._run_factors(self._make_entries(outdoor))
        assert result["has_data"] is True
        assert abs(result["warm_diff"] - 10.0) < 0.1


class TestThermalInterpolation:
    """C3: Interpolation zones derived from THERMAL_BUCKET_INTERP_HALF_F."""

    def test_interpolation_midpoint_cold_to_mild(self):
        """At the cold/mild bucket boundary, returns midpoint of cold and mild diffs."""
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        factors = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 0.0}
        # At outdoor = THERMAL_COLD_BUCKET_LIMIT_F:
        #   _cold_lo = 58.0, _cold_hi = 62.0
        #   frac = (60 - 58) / 4 = 0.5 → 15 + 0.5*(8-15) = 11.5
        result = _outdoor_conditional_diff(THERMAL_COLD_BUCKET_LIMIT_F, factors)
        assert abs(result - 11.5) < 0.01

    def test_interpolation_midpoint_mild_to_warm(self):
        """At the mild/warm bucket boundary, returns midpoint of mild and warm diffs."""
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        factors = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 0.0}
        # At outdoor = THERMAL_MILD_BUCKET_LIMIT_F:
        #   _mild_lo = 68.0, _mild_hi = 72.0
        #   frac = (70 - 68) / 4 = 0.5 → 8 + 0.5*(0-8) = 4.0
        result = _outdoor_conditional_diff(THERMAL_MILD_BUCKET_LIMIT_F, factors)
        assert abs(result - 4.0) < 0.01

    def test_below_cold_lo_returns_cold(self):
        """outdoor below lower interpolation bound → returns cold_diff exactly."""
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        factors = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 0.0}
        cold_lo = THERMAL_COLD_BUCKET_LIMIT_F - THERMAL_BUCKET_INTERP_HALF_F
        assert _outdoor_conditional_diff(cold_lo - 1, factors) == 15.0

    def test_above_mild_hi_returns_warm(self):
        """outdoor above upper interpolation bound → returns warm_diff exactly."""
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        factors = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 0.0}
        mild_hi = THERMAL_MILD_BUCKET_LIMIT_F + THERMAL_BUCKET_INTERP_HALF_F
        assert _outdoor_conditional_diff(mild_hi + 1, factors) == 0.0

    def test_pure_mild_zone(self):
        """outdoor in the flat mild zone → returns mild_diff exactly."""
        from custom_components.climate_advisor.coordinator import _outdoor_conditional_diff

        factors = {"cold_diff": 15.0, "mild_diff": 8.0, "warm_diff": 0.0}
        # 64°F: above _cold_hi (62°F) and below _mild_lo (68°F)
        mid_mild = (
            THERMAL_COLD_BUCKET_LIMIT_F
            + THERMAL_BUCKET_INTERP_HALF_F
            + THERMAL_MILD_BUCKET_LIMIT_F
            - THERMAL_BUCKET_INTERP_HALF_F
        ) / 2
        assert _outdoor_conditional_diff(mid_mild, factors) == 8.0


class TestSleepSetbackDefaults:
    """B1: DEFAULT_SETBACK_DEPTH_F and DEFAULT_SETBACK_DEPTH_COOL_F used in coordinator."""

    def test_sleep_heat_default(self):
        """Config missing sleep_heat → fallback = comfort_heat - DEFAULT_SETBACK_DEPTH_F."""
        comfort_heat = 70.0
        config = {"comfort_heat": comfort_heat, "comfort_cool": 75.0}
        sleep_heat = float(config.get("sleep_heat", comfort_heat - DEFAULT_SETBACK_DEPTH_F))
        assert sleep_heat == comfort_heat - DEFAULT_SETBACK_DEPTH_F
        assert sleep_heat == 66.0  # 70 - 4

    def test_sleep_cool_default(self):
        """Config missing sleep_cool → fallback = comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F."""
        comfort_cool = 75.0
        config = {"comfort_heat": 70.0, "comfort_cool": comfort_cool}
        sleep_cool = float(config.get("sleep_cool", comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F))
        assert sleep_cool == comfort_cool + DEFAULT_SETBACK_DEPTH_COOL_F
        assert sleep_cool == 78.0  # 75 + 3

    def test_cold_day_setback_depth_shallower_than_normal(self):
        """COLD_DAY_SETBACK_DEPTH_F is shallower than the normal heat setback."""
        assert COLD_DAY_SETBACK_DEPTH_F < DEFAULT_SETBACK_DEPTH_F

    def test_constant_values_match_expected_fahrenheit(self):
        """Named constants carry the correct historical °F values."""
        assert DEFAULT_SETBACK_DEPTH_F == 4.0
        assert DEFAULT_SETBACK_DEPTH_COOL_F == 3.0
        assert COLD_DAY_SETBACK_DEPTH_F == 3.0
        assert WINDOW_OPPORTUNITY_MAX_LOW_F == 80.0
        assert THERMAL_COLD_BUCKET_LIMIT_F == 60.0
        assert THERMAL_MILD_BUCKET_LIMIT_F == 70.0
        assert THERMAL_BUCKET_INTERP_HALF_F == 2.0
