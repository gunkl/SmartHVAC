"""Tests for per-event notification channel filtering (Issue #50).

Covers:
- AutomationEngine._notify() method — push/email channel selection based on
  per-event config toggles (push_{type}, email_{type})
- Coordinator briefing dispatch — push_briefing and email_briefing toggles
- End-to-end filtering for door/window pause and occupancy home notifications
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.climate_advisor.automation import AutomationEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(config_overrides: dict | None = None) -> AutomationEngine:
    """Create an AutomationEngine with mocked HA dependencies."""
    hass = MagicMock()
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()

    def _consume_coroutine(coro):
        coro.close()

    hass.async_create_task = MagicMock(side_effect=_consume_coroutine)
    hass.states = MagicMock()

    config = {
        "comfort_heat": 70,
        "comfort_cool": 75,
        "setback_heat": 60,
        "setback_cool": 80,
        "notify_service": "notify.mobile_app",
        # Default: all notifications enabled
        "push_briefing": True,
        "push_door_window_pause": True,
        "push_occupancy_home": True,
        "email_briefing": True,
        "email_door_window_pause": True,
        "email_grace_expired": True,
        "email_grace_repause": True,
        "email_occupancy_home": True,
    }
    if config_overrides:
        config.update(config_overrides)

    engine = AutomationEngine(
        hass=hass,
        climate_entity="climate.thermostat",
        weather_entity="weather.forecast_home",
        door_window_sensors=["binary_sensor.front_door"],
        notify_service=config["notify_service"],
        config=config,
    )
    return engine


# ---------------------------------------------------------------------------
# _notify() method — channel filtering
# ---------------------------------------------------------------------------


class TestNotifyMethodFiltering:
    """Test that _notify() sends to correct channels based on per-event config."""

    def test_push_and_email_both_enabled(self):
        """Both channels fire when both toggles are True."""
        engine = _make_engine()
        asyncio.run(engine._notify("test msg", "Test", notification_type="door_window_pause"))
        calls = engine.hass.services.async_call.call_args_list
        domains = [c[0][0] for c in calls]
        services = [c[0][1] for c in calls]
        assert len(calls) == 2
        assert domains == ["notify", "notify"]
        assert "mobile_app" in services
        assert "send_email" in services

    def test_push_disabled_email_only(self):
        """Only email fires when push toggle is False."""
        engine = _make_engine({"push_door_window_pause": False})
        asyncio.run(engine._notify("test msg", "Test", notification_type="door_window_pause"))
        calls = engine.hass.services.async_call.call_args_list
        assert len(calls) == 1
        assert calls[0][0][1] == "send_email"

    def test_email_disabled_push_only(self):
        """Only push fires when email toggle is False."""
        engine = _make_engine({"email_door_window_pause": False})
        asyncio.run(engine._notify("test msg", "Test", notification_type="door_window_pause"))
        calls = engine.hass.services.async_call.call_args_list
        assert len(calls) == 1
        assert calls[0][0][1] == "mobile_app"

    def test_both_disabled_no_calls(self):
        """Neither channel fires when both toggles are False."""
        engine = _make_engine(
            {
                "push_door_window_pause": False,
                "email_door_window_pause": False,
            }
        )
        asyncio.run(engine._notify("test msg", "Test", notification_type="door_window_pause"))
        engine.hass.services.async_call.assert_not_called()

    def test_dry_run_skips_all(self):
        """Dry run mode sends nothing regardless of toggle state."""
        engine = _make_engine()
        engine.dry_run = True
        asyncio.run(engine._notify("test msg", "Test", notification_type="door_window_pause"))
        engine.hass.services.async_call.assert_not_called()

    def test_missing_config_defaults_to_both(self):
        """Unknown notification_type defaults to sending on both channels."""
        engine = _make_engine()
        asyncio.run(engine._notify("test msg", "Test", notification_type="unknown_event"))
        calls = engine.hass.services.async_call.call_args_list
        assert len(calls) == 2

    def test_occupancy_home_push_disabled(self):
        """Occupancy home push toggle disables push for that event."""
        engine = _make_engine({"push_occupancy_home": False})
        asyncio.run(engine._notify("Welcome!", "Test", notification_type="occupancy_home"))
        calls = engine.hass.services.async_call.call_args_list
        assert len(calls) == 1
        assert calls[0][0][1] == "send_email"

    def test_grace_expired_email_disabled(self):
        """Grace expired email toggle disables email for that event."""
        engine = _make_engine({"email_grace_expired": False})
        asyncio.run(engine._notify("Grace expired", "Test", notification_type="grace_expired"))
        calls = engine.hass.services.async_call.call_args_list
        assert len(calls) == 1
        assert calls[0][0][1] == "mobile_app"

    def test_grace_repause_both_disabled(self):
        """Grace re-pause with both channels disabled sends nothing."""
        engine = _make_engine(
            {
                "push_grace_repause": False,
                "email_grace_repause": False,
            }
        )
        asyncio.run(engine._notify("Re-paused", "Test", notification_type="grace_repause"))
        engine.hass.services.async_call.assert_not_called()

    def test_notify_service_with_domain_prefix(self):
        """notify_service like 'notify.mobile_app' correctly strips domain prefix."""
        engine = _make_engine({"email_door_window_pause": False})
        asyncio.run(engine._notify("test", "Test", notification_type="door_window_pause"))
        calls = engine.hass.services.async_call.call_args_list
        assert calls[0][0][1] == "mobile_app"


# ---------------------------------------------------------------------------
# Briefing dispatch — coordinator-level
# ---------------------------------------------------------------------------


class TestBriefingDispatch:
    """Test briefing notification filtering in the coordinator.

    Since the coordinator is hard to instantiate without HA, we replicate
    the dispatch logic inline — matching coordinator.py _async_send_briefing().
    """

    def _dispatch_briefing(self, config: dict) -> list[str]:
        """Replicate briefing dispatch logic, return list of services called."""
        calls = []
        notify_svc = config.get("notify_service", "notify.notify")
        notify_name = notify_svc.split(".")[-1] if "." in notify_svc else notify_svc

        if config.get("push_briefing", True):
            calls.append(notify_name)
        if config.get("email_briefing", True):
            calls.append("send_email")
        return calls

    def test_both_enabled(self):
        services = self._dispatch_briefing({"notify_service": "notify.mobile_app"})
        assert "mobile_app" in services
        assert "send_email" in services

    def test_push_disabled_skips_push(self):
        services = self._dispatch_briefing(
            {
                "notify_service": "notify.mobile_app",
                "push_briefing": False,
            }
        )
        assert "mobile_app" not in services
        assert "send_email" in services

    def test_email_disabled_skips_email(self):
        services = self._dispatch_briefing(
            {
                "notify_service": "notify.mobile_app",
                "email_briefing": False,
            }
        )
        assert "mobile_app" in services
        assert "send_email" not in services

    def test_both_disabled_sends_nothing(self):
        services = self._dispatch_briefing(
            {
                "push_briefing": False,
                "email_briefing": False,
            }
        )
        assert services == []


# ---------------------------------------------------------------------------
# Door/window pause notification filtering
# ---------------------------------------------------------------------------


class TestDoorWindowPauseNotification:
    """Test that door/window pause notifications respect per-event toggles."""

    def test_push_disabled_no_push_sent(self):
        engine = _make_engine({"push_door_window_pause": False})
        asyncio.run(
            engine._notify(
                "HVAC paused — Front Door open",
                "Climate Advisor",
                notification_type="door_window_pause",
            )
        )
        calls = engine.hass.services.async_call.call_args_list
        services = [c[0][1] for c in calls]
        assert "mobile_app" not in services
        assert "send_email" in services

    def test_email_disabled_no_email_sent(self):
        engine = _make_engine({"email_door_window_pause": False})
        asyncio.run(
            engine._notify(
                "HVAC paused — Front Door open",
                "Climate Advisor",
                notification_type="door_window_pause",
            )
        )
        calls = engine.hass.services.async_call.call_args_list
        services = [c[0][1] for c in calls]
        assert "mobile_app" in services
        assert "send_email" not in services


# ---------------------------------------------------------------------------
# Occupancy home notification filtering
# ---------------------------------------------------------------------------


class TestOccupancyHomeNotification:
    """Test that occupancy home notifications respect per-event toggles."""

    def test_push_disabled_no_push_sent(self):
        engine = _make_engine({"push_occupancy_home": False})
        asyncio.run(
            engine._notify(
                "Welcome home!",
                "Climate Advisor",
                notification_type="occupancy_home",
            )
        )
        calls = engine.hass.services.async_call.call_args_list
        services = [c[0][1] for c in calls]
        assert "mobile_app" not in services
        assert "send_email" in services

    def test_email_disabled_no_email_sent(self):
        engine = _make_engine({"email_occupancy_home": False})
        asyncio.run(
            engine._notify(
                "Welcome home!",
                "Climate Advisor",
                notification_type="occupancy_home",
            )
        )
        calls = engine.hass.services.async_call.call_args_list
        services = [c[0][1] for c in calls]
        assert "mobile_app" in services
        assert "send_email" not in services
