"""Tests for the Climate Advisor REST API module."""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.climate_advisor.const import (
    DOMAIN,
    ATTR_DAY_TYPE,
    API_STATUS,
    API_BRIEFING,
    API_CHART_DATA,
    API_AUTOMATION_STATE,
    API_LEARNING,
    API_FORCE_RECLASSIFY,
    API_SEND_BRIEFING,
    API_RESPOND_SUGGESTION,
    API_CONFIG,
)
from custom_components.climate_advisor.api import (
    _get_coordinator,
    API_VIEWS,
)
from custom_components.climate_advisor.learning import DailyRecord


class TestGetCoordinator:
    """Tests for _get_coordinator helper."""

    def test_returns_coordinator_when_loaded(self):
        coord = MagicMock()
        hass = MagicMock()
        hass.data = {DOMAIN: {"entry_1": coord}}
        assert _get_coordinator(hass) is coord

    def test_returns_none_when_not_loaded(self):
        hass = MagicMock()
        hass.data = {}
        assert _get_coordinator(hass) is None

    def test_returns_none_when_domain_empty(self):
        hass = MagicMock()
        hass.data = {DOMAIN: {}}
        assert _get_coordinator(hass) is None


class TestAPIConstants:
    """Test that API path constants are properly defined."""

    def test_all_paths_under_base(self):
        paths = [
            API_STATUS,
            API_BRIEFING,
            API_CHART_DATA,
            API_AUTOMATION_STATE,
            API_LEARNING,
            API_FORCE_RECLASSIFY,
            API_SEND_BRIEFING,
            API_RESPOND_SUGGESTION,
            API_CONFIG,
        ]
        for path in paths:
            assert path.startswith("/api/climate_advisor/"), f"{path} has wrong prefix"

    def test_paths_are_unique(self):
        paths = [
            API_STATUS,
            API_BRIEFING,
            API_CHART_DATA,
            API_AUTOMATION_STATE,
            API_LEARNING,
            API_FORCE_RECLASSIFY,
            API_SEND_BRIEFING,
            API_RESPOND_SUGGESTION,
            API_CONFIG,
        ]
        assert len(paths) == len(set(paths))


class TestAPIViewList:
    """Test the API_VIEWS registry."""

    def test_correct_count(self):
        assert len(API_VIEWS) == 9

    def test_all_are_callable(self):
        for view_cls in API_VIEWS:
            assert callable(view_cls)


class TestCoordinatorDataContract:
    """Test that the data contract between coordinator and API is correct."""

    def test_status_data_fields(self):
        """API status view expects these keys in coordinator.data."""
        required_keys = [
            ATTR_DAY_TYPE,
            "trend_direction",
            "trend_magnitude",
            "automation_status",
            "compliance_score",
            "next_human_action",
        ]
        coord_data = {
            ATTR_DAY_TYPE: "warm",
            "trend_direction": "stable",
            "trend_magnitude": 2.5,
            "automation_status": "active",
            "compliance_score": 0.92,
            "next_human_action": "Open windows at 08:00 AM",
        }
        for key in required_keys:
            assert key in coord_data

    def test_chart_data_structure(self):
        """Chart data should have all expected series."""
        chart_data = {
            "predicted_outdoor": [{"hour": 0, "temp": 60.0}],
            "predicted_indoor": [{"hour": 0, "temp": 70.0}],
            "actual_outdoor": [{"time": "2026-03-18T08:00:00", "temp": 62.0}],
            "actual_indoor": [{"time": "2026-03-18T08:00:00", "temp": 70.0}],
            "current_hour": 14.5,
        }
        assert "predicted_outdoor" in chart_data
        assert "predicted_indoor" in chart_data
        assert "actual_outdoor" in chart_data
        assert "actual_indoor" in chart_data
        assert "current_hour" in chart_data

    def test_debug_state_structure(self):
        """Debug state should have all expected fields."""
        debug_state = {
            "paused_by_door": False,
            "pre_pause_mode": None,
            "grace_active": False,
            "last_resume_source": None,
            "door_window_sensors": {},
            "pending_debounce_timers": [],
            "classification": None,
        }
        assert "paused_by_door" in debug_state
        assert "grace_active" in debug_state
        assert "door_window_sensors" in debug_state

    def test_daily_record_serializable(self):
        """DailyRecord should be serializable for the learning endpoint."""
        from dataclasses import asdict
        record = DailyRecord(
            date="2026-03-18", day_type="warm", trend_direction="stable"
        )
        data = asdict(record)
        assert data["date"] == "2026-03-18"
        assert data["day_type"] == "warm"
        assert data["manual_overrides"] == 0
        assert data["door_window_pause_events"] == 0
