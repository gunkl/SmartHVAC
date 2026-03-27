"""Tests for comfort violation elapsed-time capping logic (Issue #56).

These tests replicate the coordinator's violation tracking logic as a pure function
to verify correctness without importing the full coordinator or HA test infrastructure.
"""

from datetime import UTC, datetime, timedelta

import pytest


def _compute_violations(
    checks: list[tuple[datetime, float]],
    comfort_low: float = 70.0,
    comfort_high: float = 75.0,
) -> float:
    """Replicate coordinator elapsed-time violation tracking.

    Each call adds actual elapsed minutes since the previous call (capped at 30 min)
    when indoor_temp is outside [comfort_low, comfort_high].
    First call always contributes 30 min (no previous reference point).
    """
    last_check: datetime | None = None
    total_violations = 0.0
    for ts, temp in checks:
        elapsed = min((ts - last_check).total_seconds() / 60, 30.0) if last_check is not None else 30.0
        last_check = ts
        if temp < comfort_low or temp > comfort_high:
            total_violations += elapsed
    return total_violations


class TestComfortViolationElapsedTime:
    """Elapsed-time tracking prevents violations from exceeding 1440 min/day."""

    def test_single_check_adds_30_on_first_call(self):
        """First call always contributes 30 min (acts as one 30-min window)."""
        t0 = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
        assert _compute_violations([(t0, 65.0)]) == 30.0

    def test_rapid_calls_dont_exceed_elapsed_time(self):
        """10 calls 30 sec apart → only ~4.5 min elapsed, not 300 min."""
        base = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
        checks = [(base + timedelta(seconds=30 * i), 65.0) for i in range(10)]
        result = _compute_violations(checks)
        # First call = 30 min; remaining 9 calls = 9 × 0.5 min = 4.5 min → 34.5 total
        assert result == pytest.approx(34.5, abs=0.1)

    def test_48_half_hour_checks_caps_at_1440(self):
        """48 checks exactly 30 min apart, all violating → exactly 1440 min."""
        base = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        checks = [(base + timedelta(minutes=30 * i), 65.0) for i in range(48)]
        assert _compute_violations(checks) == pytest.approx(1440.0)

    def test_frequent_calls_cap_elapsed_at_30_per_call(self):
        """Gaps >30 min still only add 30 min (cap prevents credit for long gaps)."""
        base = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        # 3 calls each 60 min apart → each capped at 30, total = 90
        checks = [(base + timedelta(hours=i), 65.0) for i in range(3)]
        assert _compute_violations(checks) == pytest.approx(90.0)

    def test_in_range_temp_adds_no_violations(self):
        """Temperature within range never adds violations regardless of call frequency."""
        base = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
        checks = [(base + timedelta(minutes=30 * i), 72.0) for i in range(10)]
        assert _compute_violations(checks) == 0.0

    def test_mixed_in_and_out_of_range(self):
        """Only out-of-range windows contribute; in-range windows consume elapsed time."""
        base = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
        checks = [
            (base, 65.0),  # first → +30
            (base + timedelta(minutes=30), 72.0),  # in range → +0
            (base + timedelta(minutes=60), 65.0),  # out → +30
        ]
        assert _compute_violations(checks) == pytest.approx(60.0)


class TestComfortScoreClamp:
    """Comfort score must always be in [0.0, 1.0]."""

    @staticmethod
    def _score(total_violations: float, days: int) -> float:
        total_day_minutes = days * 1440
        return max(0.0, 1 - (total_violations / total_day_minutes)) if total_day_minutes else 1.0

    def test_score_never_negative_with_inflated_violations(self):
        """Pre-fix historical data with >1440 min/day must not produce negative scores."""
        assert self._score(5000, 1) == 0.0

    def test_score_perfect_with_no_violations(self):
        assert self._score(0, 7) == 1.0

    def test_score_half_with_half_day_violated(self):
        assert self._score(720, 1) == pytest.approx(0.5)

    def test_score_zero_at_full_day_violated(self):
        assert self._score(1440, 1) == 0.0
