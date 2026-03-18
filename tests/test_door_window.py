"""Tests for door/window sensor group resolution and polarity logic.

These tests validate the algorithms used by the coordinator for resolving
binary_sensor groups and interpreting sensor polarity. Since the coordinator
cannot be instantiated without a live Home Assistant instance, we replicate
the logic inline and test it directly.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.climate_advisor.const import CONF_SENSOR_POLARITY_INVERTED


# ---------------------------------------------------------------------------
# Replicate coordinator logic for unit testing
# ---------------------------------------------------------------------------

def _resolve_monitored_sensors(
    hass_states_get,
    door_window_sensors: list[str],
    door_window_groups: list[str],
) -> list[str]:
    """Resolve all monitored sensor entity IDs, expanding groups.

    This mirrors ClimateAdvisorCoordinator._resolve_monitored_sensors().
    """
    individual = list(door_window_sensors)

    for group_id in door_window_groups:
        state = hass_states_get(group_id)
        if state:
            members = state.attributes.get("entity_id", [])
            for member in members:
                if member not in individual:
                    individual.append(member)

    return individual


def _is_sensor_open(
    hass_states_get,
    entity_id: str,
    polarity_inverted: bool,
) -> bool:
    """Check if a sensor is open, respecting polarity.

    This mirrors ClimateAdvisorCoordinator._is_sensor_open().
    """
    state = hass_states_get(entity_id)
    if not state:
        return False
    if polarity_inverted:
        return state.state == "off"
    return state.state == "on"


def _make_state(state_value: str, attributes: dict | None = None) -> MagicMock:
    """Create a mock HA state object."""
    mock = MagicMock()
    mock.state = state_value
    mock.attributes = attributes or {}
    return mock


def _states_getter(state_map: dict[str, MagicMock]):
    """Return a callable that looks up entity states from a dict."""
    return lambda eid: state_map.get(eid)


# ---------------------------------------------------------------------------
# Group resolution tests
# ---------------------------------------------------------------------------

class TestResolveMonitoredSensors:
    """Tests for group resolution logic."""

    def test_individual_sensors_only(self):
        get = _states_getter({})
        result = _resolve_monitored_sensors(
            get,
            ["binary_sensor.front_door", "binary_sensor.back_door"],
            [],
        )
        assert result == ["binary_sensor.front_door", "binary_sensor.back_door"]

    def test_group_expands_members(self):
        get = _states_getter({
            "group.windows": _make_state(
                "on",
                {"entity_id": ["binary_sensor.window_1", "binary_sensor.window_2"]},
            ),
        })
        result = _resolve_monitored_sensors(get, [], ["group.windows"])
        assert "binary_sensor.window_1" in result
        assert "binary_sensor.window_2" in result

    def test_deduplication(self):
        get = _states_getter({
            "group.all_openings": _make_state(
                "on",
                {"entity_id": ["binary_sensor.front_door", "binary_sensor.window_1"]},
            ),
        })
        result = _resolve_monitored_sensors(
            get,
            ["binary_sensor.front_door"],
            ["group.all_openings"],
        )
        assert result.count("binary_sensor.front_door") == 1
        assert "binary_sensor.window_1" in result

    def test_unavailable_group_skipped(self):
        get = _states_getter({})
        result = _resolve_monitored_sensors(get, [], ["group.missing"])
        assert result == []

    def test_empty_config(self):
        get = _states_getter({})
        result = _resolve_monitored_sensors(get, [], [])
        assert result == []

    def test_mixed_individual_and_groups(self):
        get = _states_getter({
            "group.bedroom_windows": _make_state(
                "on",
                {"entity_id": ["binary_sensor.window_3"]},
            ),
        })
        result = _resolve_monitored_sensors(
            get,
            ["binary_sensor.back_door"],
            ["group.bedroom_windows"],
        )
        assert "binary_sensor.back_door" in result
        assert "binary_sensor.window_3" in result

    def test_multiple_groups(self):
        get = _states_getter({
            "group.g1": _make_state("on", {"entity_id": ["binary_sensor.a"]}),
            "group.g2": _make_state("on", {"entity_id": ["binary_sensor.b"]}),
        })
        result = _resolve_monitored_sensors(get, [], ["group.g1", "group.g2"])
        assert set(result) == {"binary_sensor.a", "binary_sensor.b"}

    def test_group_with_no_entity_id_attribute(self):
        get = _states_getter({
            "group.empty": _make_state("on", {}),
        })
        result = _resolve_monitored_sensors(get, [], ["group.empty"])
        assert result == []


# ---------------------------------------------------------------------------
# Polarity tests
# ---------------------------------------------------------------------------

class TestIsSensorOpen:
    """Tests for polarity-aware sensor open check."""

    def test_standard_on_is_open(self):
        get = _states_getter({"binary_sensor.door": _make_state("on")})
        assert _is_sensor_open(get, "binary_sensor.door", False) is True

    def test_standard_off_is_closed(self):
        get = _states_getter({"binary_sensor.door": _make_state("off")})
        assert _is_sensor_open(get, "binary_sensor.door", False) is False

    def test_inverted_off_is_open(self):
        get = _states_getter({"binary_sensor.door": _make_state("off")})
        assert _is_sensor_open(get, "binary_sensor.door", True) is True

    def test_inverted_on_is_closed(self):
        get = _states_getter({"binary_sensor.door": _make_state("on")})
        assert _is_sensor_open(get, "binary_sensor.door", True) is False

    def test_unavailable_sensor_is_not_open(self):
        get = _states_getter({})
        assert _is_sensor_open(get, "binary_sensor.missing", False) is False

    def test_unavailable_sensor_inverted_is_not_open(self):
        get = _states_getter({})
        assert _is_sensor_open(get, "binary_sensor.missing", True) is False


# ---------------------------------------------------------------------------
# All-closed logic tests
# ---------------------------------------------------------------------------

class TestAllClosedCheck:
    """Tests for the all-closed check across multiple sensors with polarity."""

    def test_all_closed_standard(self):
        get = _states_getter({
            "binary_sensor.a": _make_state("off"),
            "binary_sensor.b": _make_state("off"),
        })
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        all_closed = all(not _is_sensor_open(get, s, False) for s in sensors)
        assert all_closed is True

    def test_one_open_standard(self):
        get = _states_getter({
            "binary_sensor.a": _make_state("off"),
            "binary_sensor.b": _make_state("on"),
        })
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        all_closed = all(not _is_sensor_open(get, s, False) for s in sensors)
        assert all_closed is False

    def test_all_closed_inverted(self):
        get = _states_getter({
            "binary_sensor.a": _make_state("on"),
            "binary_sensor.b": _make_state("on"),
        })
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        all_closed = all(not _is_sensor_open(get, s, True) for s in sensors)
        assert all_closed is True

    def test_one_open_inverted(self):
        get = _states_getter({
            "binary_sensor.a": _make_state("on"),
            "binary_sensor.b": _make_state("off"),  # off = open when inverted
        })
        sensors = ["binary_sensor.a", "binary_sensor.b"]
        all_closed = all(not _is_sensor_open(get, s, True) for s in sensors)
        assert all_closed is False


# ---------------------------------------------------------------------------
# Config migration tests
# ---------------------------------------------------------------------------

class TestConfigMigration:
    """Tests for v2->v3 config migration defaults."""

    def test_v2_config_gets_new_defaults(self):
        v2_data = {
            "door_window_sensors": ["binary_sensor.front_door"],
            "wake_time": "06:30",
        }
        new_data = {**v2_data}
        new_data.setdefault("door_window_groups", [])
        new_data.setdefault(CONF_SENSOR_POLARITY_INVERTED, False)

        assert new_data["door_window_groups"] == []
        assert new_data[CONF_SENSOR_POLARITY_INVERTED] is False
        assert new_data["door_window_sensors"] == ["binary_sensor.front_door"]

    def test_v2_config_preserves_existing_keys(self):
        v2_data = {
            "door_window_groups": ["group.custom"],
            CONF_SENSOR_POLARITY_INVERTED: True,
        }
        new_data = {**v2_data}
        new_data.setdefault("door_window_groups", [])
        new_data.setdefault(CONF_SENSOR_POLARITY_INVERTED, False)

        assert new_data["door_window_groups"] == ["group.custom"]
        assert new_data[CONF_SENSOR_POLARITY_INVERTED] is True
