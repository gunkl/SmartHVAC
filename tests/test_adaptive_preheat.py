"""Tests for adaptive pre-heat timing in AutomationEngine._schedule_pre_condition() (Phase 5G)."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

# ── HA module stubs ──────────────────────────────────────────────────────────
if "homeassistant" not in sys.modules:
    from conftest import _install_ha_stubs

    _install_ha_stubs()

from custom_components.climate_advisor.automation import AutomationEngine
from custom_components.climate_advisor.classifier import DayClassification
from custom_components.climate_advisor.const import (
    MAX_PREHEAT_MINUTES,
    MIN_PREHEAT_MINUTES,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "climate_entity": "climate.test",
    "weather_entity": "weather.test",
    "notify_service": "notify.test",
    "comfort_heat": 70,
    "comfort_cool": 75,
    "setback_heat": 60,
    "setback_cool": 80,
    "wake_time": "06:30",
    "sleep_time": "22:30",
    "temp_unit": "fahrenheit",
}


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.services.async_call = AsyncMock()
    return hass


def _make_engine(config: dict | None = None) -> AutomationEngine:
    cfg = dict(_BASE_CONFIG)
    if config:
        cfg.update(config)
    engine = AutomationEngine(
        hass=_make_hass(),
        climate_entity=cfg["climate_entity"],
        weather_entity=cfg["weather_entity"],
        door_window_sensors=[],
        notify_service=cfg["notify_service"],
        config=cfg,
    )
    engine.dry_run = True
    return engine


def _make_classification(
    *,
    trend_direction: str = "cooling",
    pre_condition: bool = True,
    pre_condition_target: float = 2.0,
    hvac_mode: str = "heat",
    **kwargs,
) -> DayClassification:
    c = object.__new__(DayClassification)
    defaults = {
        "day_type": "cold",
        "trend_direction": trend_direction,
        "trend_magnitude": 10.0,
        "today_high": 40.0,
        "today_low": 25.0,
        "tomorrow_high": 30.0,
        "tomorrow_low": 15.0,
        "hvac_mode": hvac_mode,
        "pre_condition": pre_condition,
        "pre_condition_target": pre_condition_target,
        "windows_recommended": False,
        "window_open_time": None,
        "window_close_time": None,
        "setback_modifier": 0.0,
        "window_opportunity_morning": False,
        "window_opportunity_evening": False,
    }
    defaults.update(kwargs)
    c.__dict__.update(defaults)
    return c


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdaptivePreheat:
    """Tests for _schedule_pre_condition() with thermal model."""

    def test_preheat_uses_default_when_no_model(self):
        engine = _make_engine()
        engine._thermal_model = {}
        c = _make_classification(pre_condition_target=2.0)
        asyncio.run(engine._schedule_pre_condition(c))

        preheat = engine.config.get("_pending_preheat")
        assert preheat is not None
        # With default 120 minutes, sleep=22:30 → start at 20:30
        assert preheat["time"] == "20:30"

    def test_preheat_earlier_for_slow_house(self):
        """Low heating rate → more lead time needed → clamped to MAX_PREHEAT_MINUTES."""
        engine = _make_engine()
        # Rate=1.0°F/hr, delta=5°F → 5hr needed × 1.3 safety → 390 min → clamped to 240
        engine._thermal_model = {
            "confidence": "low",
            "heating_rate_f_per_hour": 1.0,
        }
        c = _make_classification(pre_condition_target=5.0)
        asyncio.run(engine._schedule_pre_condition(c))

        preheat = engine.config.get("_pending_preheat")
        assert preheat is not None
        # With MAX_PREHEAT_MINUTES=240, sleep=22:30 → start at 18:30
        sleep_minutes = 22 * 60 + 30
        start_minutes = sleep_minutes - MAX_PREHEAT_MINUTES
        expected_time = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
        assert preheat["time"] == expected_time

    def test_preheat_later_for_fast_house(self):
        """High heating rate → less lead time needed → clamped to MIN_PREHEAT_MINUTES."""
        engine = _make_engine()
        # Rate=10.0°F/hr, delta=2°F → 0.2hr × 60 × 1.3 = 15.6 min → clamped to 30
        engine._thermal_model = {
            "confidence": "high",
            "heating_rate_f_per_hour": 10.0,
        }
        c = _make_classification(pre_condition_target=2.0)
        asyncio.run(engine._schedule_pre_condition(c))

        preheat = engine.config.get("_pending_preheat")
        assert preheat is not None
        # 30 min before sleep_time=22:30 → 22:00
        assert preheat["time"] == "22:00"

    def test_preheat_clamped_to_min_30_minutes(self):
        """Even a very fast house clamps to MIN_PREHEAT_MINUTES (30)."""
        engine = _make_engine()
        # Rate=100°F/hr → would give ~1.5 min; must clamp to 30
        engine._thermal_model = {
            "confidence": "high",
            "heating_rate_f_per_hour": 100.0,
        }
        c = _make_classification(pre_condition_target=2.0)
        asyncio.run(engine._schedule_pre_condition(c))

        preheat = engine.config.get("_pending_preheat")
        assert preheat is not None
        sleep_minutes = 22 * 60 + 30
        start_minutes = sleep_minutes - MIN_PREHEAT_MINUTES
        expected_time = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
        assert preheat["time"] == expected_time

    def test_preheat_clamped_to_max_240_minutes(self):
        """Extremely slow house clamps to MAX_PREHEAT_MINUTES (240)."""
        engine = _make_engine()
        # Rate=0.1°F/hr, delta=100°F → huge; clamped to 240
        engine._thermal_model = {
            "confidence": "low",
            "heating_rate_f_per_hour": 0.1,
        }
        c = _make_classification(pre_condition_target=100.0)
        asyncio.run(engine._schedule_pre_condition(c))

        preheat = engine.config.get("_pending_preheat")
        assert preheat is not None
        sleep_minutes = 22 * 60 + 30
        start_minutes = sleep_minutes - MAX_PREHEAT_MINUTES
        expected_time = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
        assert preheat["time"] == expected_time

    def test_no_preheat_when_pre_condition_target_none(self):
        """pre_condition_target=None → _schedule_pre_condition does nothing."""
        engine = _make_engine()
        engine._thermal_model = {}
        # _schedule_pre_condition checks `c.pre_condition_target and c.pre_condition_target > 0`
        c = _make_classification(pre_condition_target=None)
        asyncio.run(engine._schedule_pre_condition(c))

        assert "_pending_preheat" not in engine.config

    def test_no_preheat_when_trend_not_cooling(self):
        """trend_direction != 'cooling' → no _pending_preheat set."""
        engine = _make_engine()
        engine._thermal_model = {}
        c = _make_classification(trend_direction="warming", pre_condition=True, pre_condition_target=2.0)
        asyncio.run(engine._schedule_pre_condition(c))

        assert "_pending_preheat" not in engine.config
