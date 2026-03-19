"""REST API views for the Climate Advisor dashboard panel."""
from __future__ import annotations

import logging
from dataclasses import asdict

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    API_AUTOMATION_STATE,
    API_BRIEFING,
    API_CHART_DATA,
    API_FORCE_RECLASSIFY,
    API_LEARNING,
    API_RESPOND_SUGGESTION,
    API_SEND_BRIEFING,
    API_STATUS,
    ATTR_AUTOMATION_STATUS,
    ATTR_COMPLIANCE_SCORE,
    ATTR_DAY_TYPE,
    ATTR_NEXT_ACTION,
    ATTR_TREND,
    ATTR_TREND_MAGNITUDE,
    DOMAIN,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)


def _get_coordinator(hass: HomeAssistant):
    """Get the first (and usually only) Climate Advisor coordinator."""
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        return None
    return next(iter(entries.values()))


class ClimateAdvisorStatusView(HomeAssistantView):
    """Return current system status."""

    url = API_STATUS
    name = "api:climate_advisor:status"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        data = coordinator.data or {}
        climate_state = hass.states.get(coordinator.config.get("climate_entity", ""))
        hvac_mode = climate_state.state if climate_state else "unknown"

        return self.json({
            "version": VERSION,
            "day_type": data.get(ATTR_DAY_TYPE, "unknown"),
            "trend_direction": data.get(ATTR_TREND, "unknown"),
            "trend_magnitude": data.get(ATTR_TREND_MAGNITUDE, 0),
            "hvac_mode": hvac_mode,
            "automation_status": data.get(ATTR_AUTOMATION_STATUS, "unknown"),
            "compliance_score": data.get(ATTR_COMPLIANCE_SCORE, 1.0),
            "next_action": data.get(ATTR_NEXT_ACTION, ""),
            "automation_enabled": coordinator.automation_enabled,
        })


class ClimateAdvisorBriefingView(HomeAssistantView):
    """Return the current daily briefing text."""

    url = API_BRIEFING
    name = "api:climate_advisor:briefing"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        return self.json({
            "briefing": coordinator._last_briefing,
            "briefing_sent_today": coordinator._briefing_sent_today,
        })


class ClimateAdvisorChartDataView(HomeAssistantView):
    """Return chart data for the temperature overlay chart."""

    url = API_CHART_DATA
    name = "api:climate_advisor:chart_data"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        return self.json(coordinator.get_chart_data())


class ClimateAdvisorAutomationStateView(HomeAssistantView):
    """Return automation engine debug state."""

    url = API_AUTOMATION_STATE
    name = "api:climate_advisor:automation_state"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        return self.json(coordinator.get_debug_state())


class ClimateAdvisorLearningView(HomeAssistantView):
    """Return learning engine state and today's record."""

    url = API_LEARNING
    name = "api:climate_advisor:learning"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        today_record = None
        if coordinator.today_record:
            today_record = asdict(coordinator.today_record)

        return self.json({
            "today_record": today_record,
            "suggestions": coordinator.learning.generate_suggestions(),
            "compliance": coordinator.learning.get_compliance_summary(),
        })


class ClimateAdvisorForceReclassifyView(HomeAssistantView):
    """Force a coordinator data refresh (reclassification)."""

    url = API_FORCE_RECLASSIFY
    name = "api:climate_advisor:force_reclassify"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        await coordinator.async_request_refresh()
        return self.json({"status": "ok", "message": "Reclassification triggered"})


class ClimateAdvisorSendBriefingView(HomeAssistantView):
    """Force-send the daily briefing."""

    url = API_SEND_BRIEFING
    name = "api:climate_advisor:send_briefing"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        coordinator._briefing_sent_today = False
        from homeassistant.util import dt as dt_util
        await coordinator._async_send_briefing(dt_util.now())
        return self.json({"status": "ok", "message": "Briefing sent"})


class ClimateAdvisorRespondSuggestionView(HomeAssistantView):
    """Accept or dismiss a learning suggestion."""

    url = API_RESPOND_SUGGESTION
    name = "api:climate_advisor:respond_suggestion"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        coordinator = _get_coordinator(hass)
        if not coordinator:
            return self.json({"error": "Climate Advisor not loaded"}, status_code=503)

        try:
            body = await request.json()
        except Exception:
            return self.json({"error": "Invalid JSON body"}, status_code=400)

        action = body.get("action")
        suggestion_key = body.get("suggestion_key")

        if action not in ("accept", "dismiss") or not suggestion_key:
            return self.json(
                {"error": "Required: action (accept/dismiss), suggestion_key"},
                status_code=400,
            )

        if action == "accept":
            changes = coordinator.learning.accept_suggestion(suggestion_key)
            await hass.async_add_executor_job(coordinator.learning.save_state)
            coordinator.config.update(changes)
            return self.json({"status": "ok", "changes": changes})
        else:
            coordinator.learning.dismiss_suggestion(suggestion_key)
            await hass.async_add_executor_job(coordinator.learning.save_state)
            return self.json({"status": "ok", "dismissed": suggestion_key})


# All views to register
API_VIEWS = [
    ClimateAdvisorStatusView,
    ClimateAdvisorBriefingView,
    ClimateAdvisorChartDataView,
    ClimateAdvisorAutomationStateView,
    ClimateAdvisorLearningView,
    ClimateAdvisorForceReclassifyView,
    ClimateAdvisorSendBriefingView,
    ClimateAdvisorRespondSuggestionView,
]
