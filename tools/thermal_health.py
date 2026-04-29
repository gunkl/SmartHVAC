#!/usr/bin/env python3
"""Thermal learning health diagnostic tool for Climate Advisor.

Fetches the compliance sensor from Home Assistant and prints a per-obs-type
breakdown of thermal observation attempts, commits, and rejections.

Usage:
    export HA_URL=http://homeassistant.local:8123
    export HA_TOKEN=<your-long-lived-access-token>
    python3 tools/thermal_health.py

Exit codes:
    0 = report printed successfully
    1 = configuration or connection error
    2 = integration data not available (sensor missing or attribute absent)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

SENSOR_ENTITY_ID = "sensor.climate_advisor_compliance"
OBS_TYPES = [
    "passive_decay",
    "fan_only_decay",
    "ventilated_decay",
    "solar_gain",
    "hvac_heat",
    "hvac_cool",
]

COL_OBS = 20
COL_ATT = 10
COL_COM = 10
COL_REJ = 12
COL_LAST = 30

DIVIDER = "─" * (COL_OBS + COL_ATT + COL_COM + COL_REJ + COL_LAST + 4)
HEADER_LINE = "═" * (COL_OBS + COL_ATT + COL_COM + COL_REJ + COL_LAST + 4)


def _pad(text: str, width: int) -> str:
    return str(text).ljust(width)


def _fetch_sensor(ha_url: str, ha_token: str) -> dict:
    """Fetch the compliance sensor state from HA REST API."""
    url = f"{ha_url}/api/states/{SENSOR_ENTITY_ID}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print("Error: HA_TOKEN is invalid or expired (HTTP 401).")
        elif exc.code == 404:
            print(f"Error: Sensor '{SENSOR_ENTITY_ID}' not found (HTTP 404).")
            print("  Is the Climate Advisor integration installed and loaded?")
        else:
            print(f"Error: HTTP {exc.code} fetching sensor state.")
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Error: Could not connect to Home Assistant: {exc.reason}")
        print(f"  URL: {url}")
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("Error: Unexpected non-JSON response from Home Assistant.")
        sys.exit(1)

    if not isinstance(data, dict):
        print("Error: Sensor response has unexpected structure.")
        sys.exit(1)

    return data


def _print_report(health: dict) -> None:
    """Print the formatted health report table."""
    print()
    print("Thermal Learning Health Report")
    print(HEADER_LINE)
    header = (
        _pad("obs_type", COL_OBS)
        + _pad("attempts", COL_ATT)
        + _pad("committed", COL_COM)
        + _pad("rejections", COL_REJ)
        + "last rejection"
    )
    print(header)
    print(DIVIDER)

    for obs_type in OBS_TYPES:
        h = health.get(obs_type) or {}
        attempts = h.get("attempts", 0)
        committed = h.get("committed", 0)
        rejections = h.get("rejections") or {}
        total_rej = sum(rejections.values()) if isinstance(rejections, dict) else 0
        last_rejection = h.get("last_rejection") or {}
        last_reason = last_rejection.get("reason_code", "—") if isinstance(last_rejection, dict) else "—"
        if not last_reason:
            last_reason = "—"

        row = (
            _pad(obs_type, COL_OBS)
            + _pad(attempts, COL_ATT)
            + _pad(committed, COL_COM)
            + _pad(total_rej, COL_REJ)
            + last_reason
        )
        print(row)

    print()

    # Rejection breakdown per type (only for types with rejections)
    any_rejections = False
    for obs_type in OBS_TYPES:
        h = health.get(obs_type) or {}
        rejections = h.get("rejections") or {}
        if isinstance(rejections, dict) and rejections:
            if not any_rejections:
                print("Rejection breakdown:")
                any_rejections = True
            counts = sorted(rejections.items(), key=lambda x: -x[1])
            reasons_str = ", ".join(f"{code}: {count}" for code, count in counts)
            print(f"  {obs_type}: {reasons_str}")

    if any_rejections:
        print()


def main() -> None:
    ha_url = os.environ.get("HA_URL", "").rstrip("/")
    ha_token = os.environ.get("HA_TOKEN", "")

    if not ha_url or not ha_token:
        print("Error: HA_URL and HA_TOKEN environment variables are required.")
        print()
        print("  export HA_URL=http://homeassistant.local:8123")
        print("  export HA_TOKEN=<your-long-lived-access-token>")
        sys.exit(1)

    print(f"Fetching {SENSOR_ENTITY_ID} from {ha_url} ...")
    sensor_data = _fetch_sensor(ha_url, ha_token)

    attributes = sensor_data.get("attributes")
    if not isinstance(attributes, dict):
        print("Error: Sensor response missing 'attributes' field.")
        sys.exit(2)

    health = attributes.get("thermal_learning_health")
    if health is None:
        print()
        print("thermal_learning_health not found in sensor attributes. Is the integration running v0.3.30+?")
        print()
        print("Available attribute keys:")
        for key in sorted(attributes.keys()):
            print(f"  {key}")
        sys.exit(2)

    if not isinstance(health, dict):
        print("Error: thermal_learning_health attribute has unexpected type.")
        sys.exit(2)

    _print_report(health)


if __name__ == "__main__":
    main()
