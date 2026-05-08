#!/usr/bin/env python3
"""
thermal_replay.py — Replay historical HA sensor data through the thermal learning OLS.

Fetches state history for indoor/outdoor temp and window sensors, reconstructs
ventilated_decay observation windows, runs the same OLS as the live learning
engine (2-param primary when sf_range >= 0.30, 1-param fallback), and merges
results into the HA learning DB.

Requirements:
  - tools/.env (or .env) with HA_URL and HA_TOKEN
  - tools/.deploy.env (or .deploy.env) with SSH config (for --write)

Usage:
  python tools/thermal_replay.py \\
      --climate-entity climate.ecobee \\
      --outdoor-entity sensor.outdoor_temp \\
      --window-entities binary_sensor.back_door \\
      --dry-run

  python tools/thermal_replay.py ... --days 14 --write

Entity hints (check HA → Developer Tools → States):
  --climate-entity   Thermostat (indoor temp from current_temperature attribute)
  --outdoor-entity   Outdoor sensor (numeric state, e.g. sensor.weather_temperature)
  --window-entities  Comma-separated binary_sensors: state on=open, off=closed
"""

import argparse
import json
import math
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_dotenv(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return result


def load_config() -> dict[str, str]:
    # env first (HA_URL, HA_TOKEN), deploy for SSH (HA_HOST, HA_SSH_*)
    env = _load_dotenv("tools/.env") or _load_dotenv(".env")
    deploy = _load_dotenv("tools/.deploy.env") or _load_dotenv(".deploy.env")
    merged = {**deploy, **env}
    # Fall back: derive HA_URL from HA_HOST if HA_URL not set
    if not merged.get("HA_URL") and merged.get("HA_HOST"):
        merged["HA_URL"] = f"http://{merged['HA_HOST']}:8123"
    # Fall back: HA_API_TOKEN → HA_TOKEN if HA_TOKEN not set
    if not merged.get("HA_TOKEN") and merged.get("HA_API_TOKEN"):
        merged["HA_TOKEN"] = merged["HA_API_TOKEN"]
    return merged


# ---------------------------------------------------------------------------
# HA REST API
# ---------------------------------------------------------------------------


def _ha_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def fetch_entity_history(base_url: str, token: str, entity_ids: list[str], days: int) -> dict[str, list[dict]]:
    """Return {entity_id: [state_dicts]} for the requested entities and window."""
    start = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    entity_param = ",".join(urllib.parse.quote(e) for e in entity_ids)
    url = (
        f"{base_url}/api/history/period/{urllib.parse.quote(start)}"
        f"?filter_entity_id={entity_param}&minimal_response=false&significant_changes_only=false"
    )
    req = urllib.request.Request(url, headers=_ha_headers(token))
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    result: dict[str, list[dict]] = {}
    for entity_states in data:
        if entity_states:
            eid = entity_states[0].get("entity_id", "")
            result[eid] = entity_states
    return result


def fetch_ha_config(base_url: str, token: str) -> dict:
    """Fetch HA system config (for unit_system)."""
    req = urllib.request.Request(f"{base_url}/api/config", headers=_ha_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# State interpolation
# ---------------------------------------------------------------------------


def _parse_ts(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def build_time_grid(start: datetime, end: datetime, interval_minutes: int = 5) -> list[datetime]:
    grid: list[datetime] = []
    t = start
    while t <= end:
        grid.append(t)
        t += timedelta(minutes=interval_minutes)
    return grid


def _advance_state_index(sorted_states: list[dict], j: int, ts: datetime) -> int:
    while j + 1 < len(sorted_states) and _parse_ts(sorted_states[j + 1]["last_changed"]) <= ts:
        j += 1
    return j


def interpolate_numeric(states: list[dict], timestamps: list[datetime], attr: str | None = None) -> list[float | None]:
    sorted_states = sorted(states, key=lambda s: s["last_changed"])
    result: list[float | None] = []
    j = 0
    first_ts = _parse_ts(sorted_states[0]["last_changed"])
    for ts in timestamps:
        j = _advance_state_index(sorted_states, j, ts)
        if first_ts > ts:
            result.append(None)
            continue
        state = sorted_states[j]
        try:
            raw = state["attributes"].get(attr) if attr else state["state"]
            result.append(float(raw))
        except (ValueError, TypeError, KeyError):
            result.append(None)
    return result


def interpolate_categorical(
    states: list[dict], timestamps: list[datetime], attr: str | None = None
) -> list[str | None]:
    sorted_states = sorted(states, key=lambda s: s["last_changed"])
    result: list[str | None] = []
    j = 0
    first_ts = _parse_ts(sorted_states[0]["last_changed"])
    for ts in timestamps:
        j = _advance_state_index(sorted_states, j, ts)
        if first_ts > ts:
            result.append(None)
            continue
        state = sorted_states[j]
        if attr:
            result.append(str(state["attributes"].get(attr, "")))
        else:
            result.append(str(state["state"]))
    return result


# ---------------------------------------------------------------------------
# Solar factor (mirrors learning.py _solar_factor)
# ---------------------------------------------------------------------------


def _solar_factor(hour: float) -> float:
    if hour < 8 or hour >= 18:
        return 0.0
    return round(math.sin(math.pi * (hour - 8) / 10.0), 4)


# ---------------------------------------------------------------------------
# OLS (mirrors learning.py — pure Python, no HA imports)
# ---------------------------------------------------------------------------

_K_PASSIVE_MIN = -0.5
_K_PASSIVE_MAX = -0.001
_MIN_R2 = 0.20
_MIN_SAMPLES = 4
_SF_RANGE_MIN = 0.30
_K_SOLAR_MAX = 8.0


def _smooth_temps(temps: list[float]) -> list[float]:
    if len(temps) < 3:
        return list(temps)
    smoothed = [temps[0]]
    for i in range(1, len(temps) - 1):
        smoothed.append((temps[i - 1] + temps[i] + temps[i + 1]) / 3.0)
    smoothed.append(temps[-1])
    return smoothed


def compute_k_passive(samples: list[dict]) -> tuple[float | None, float, str | None]:
    if len(samples) < _MIN_SAMPLES + 1:
        return None, 0.0, "too_few_samples"
    indoor_raw = [s["indoor_temp_f"] for s in samples]
    outdoor = [s["outdoor_temp_f"] for s in samples]
    elapsed = [s["elapsed_minutes"] for s in samples]
    indoor = _smooth_temps(indoor_raw)
    rates: list[float] = []
    deltas: list[float] = []
    for i in range(len(indoor) - 1):
        dt_h = (elapsed[i + 1] - elapsed[i]) / 60.0
        if dt_h <= 0:
            continue
        rate = (indoor[i + 1] - indoor[i]) / dt_h
        delta = ((indoor[i] + indoor[i + 1]) / 2.0) - ((outdoor[i] + outdoor[i + 1]) / 2.0)
        rates.append(rate)
        deltas.append(delta)
    if len(rates) < _MIN_SAMPLES:
        return None, 0.0, "too_few_samples"
    sum_rd = sum(r * d for r, d in zip(rates, deltas, strict=False))
    sum_d2 = sum(d * d for d in deltas)
    if sum_d2 == 0:
        return None, 0.0, "small_delta"
    k_p = sum_rd / sum_d2
    if k_p > 0:
        return None, 0.0, "ols_wrong_sign"
    if not (_K_PASSIVE_MIN <= k_p <= _K_PASSIVE_MAX):
        return None, 0.0, "ols_bounds"
    ss_res = sum((r - k_p * d) ** 2 for r, d in zip(rates, deltas, strict=False))
    ss_tot = sum(r * r for r in rates)
    r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    if r2 < _MIN_R2:
        return None, r2, "ols_bad_fit"
    return k_p, r2, None


def compute_k_env_solar(samples: list[dict]) -> tuple[float | None, float | None, float | None]:
    pairs: list[tuple[float, float, float]] = []
    for i in range(len(samples) - 1):
        s0, s1 = samples[i], samples[i + 1]
        dt_h = (s1["elapsed_minutes"] - s0["elapsed_minutes"]) / 60.0
        if dt_h <= 0:
            continue
        rate = (s1["indoor_temp_f"] - s0["indoor_temp_f"]) / dt_h
        delta = ((s0["indoor_temp_f"] - s0["outdoor_temp_f"]) + (s1["indoor_temp_f"] - s1["outdoor_temp_f"])) / 2.0
        sf = (s0["solar_factor"] + s1["solar_factor"]) / 2.0
        pairs.append((rate, delta, sf))
    if len(pairs) < 4:
        return None, None, None
    rates = [p[0] for p in pairs]
    deltas = [p[1] for p in pairs]
    sfs = [p[2] for p in pairs]
    sf_range = max(sfs) - min(sfs)
    if sf_range < _SF_RANGE_MIN:
        return None, None, None
    x1x1 = sum(d * d for d in deltas)
    x2x2 = sum(s * s for s in sfs)
    x1x2 = sum(d * s for d, s in zip(deltas, sfs, strict=False))
    x1y = sum(d * r for d, r in zip(deltas, rates, strict=False))
    x2y = sum(s * r for s, r in zip(sfs, rates, strict=False))
    det = x1x1 * x2x2 - x1x2 * x1x2
    if abs(det) < 1e-12:
        return None, None, None
    k_env = (x2x2 * x1y - x1x2 * x2y) / det
    k_sol = (x1x1 * x2y - x1x2 * x1y) / det
    mean_r = sum(rates) / len(rates)
    ss_res = sum((r - k_env * d - k_sol * s) ** 2 for r, d, s in zip(rates, deltas, sfs, strict=False))
    ss_tot = sum((r - mean_r) ** 2 for r in rates)
    r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    return k_env, k_sol, r2


# ---------------------------------------------------------------------------
# Window detection
# ---------------------------------------------------------------------------

_IDLE_ACTIONS = {"idle", "off", ""}


def find_ventilated_windows(
    timestamps: list[datetime],
    indoor_temps: list[float | None],
    outdoor_temps: list[float | None],
    hvac_actions: list[str | None],
    window_open: list[bool | None],
    min_window_minutes: int = 60,
    max_window_minutes: int = 240,
    sample_interval: int = 5,
) -> list[list[dict]]:
    windows: list[list[dict]] = []
    current: list[dict] = []

    def _flush():
        if len(current) * sample_interval >= min_window_minutes:
            windows.append(list(current))
        current.clear()

    for i, ts in enumerate(timestamps):
        in_t = indoor_temps[i]
        out_t = outdoor_temps[i]
        hvac = hvac_actions[i]
        win = window_open[i]

        if in_t is None or out_t is None:
            _flush()
            continue

        # HVAC idle = not actively heating or cooling
        hvac_idle = (hvac in _IDLE_ACTIONS) or (hvac is not None and "heat" not in hvac and "cool" not in hvac)
        win_open = win is True

        if hvac_idle and win_open:
            elapsed = len(current) * sample_interval
            current.append(
                {
                    "indoor_temp_f": in_t,
                    "outdoor_temp_f": out_t,
                    "elapsed_minutes": elapsed,
                    "solar_factor": _solar_factor(ts.hour),
                    "timestamp": ts.isoformat(),
                }
            )
            if len(current) * sample_interval >= max_window_minutes:
                _flush()
        else:
            _flush()

    _flush()
    return windows


# ---------------------------------------------------------------------------
# OLS dispatch — mirrors Phase D 2-param primary path
# ---------------------------------------------------------------------------


def run_ols_on_window(window: list[dict]) -> dict | None:
    sf_vals = [s["solar_factor"] for s in window]
    sf_range = max(sf_vals) - min(sf_vals) if len(sf_vals) >= 2 else 0.0
    ts_str = window[-1]["timestamp"]
    date_str = ts_str[:10]

    # 2-param primary
    if sf_range >= _SF_RANGE_MIN:
        k_env, k_sol, r2 = compute_k_env_solar(window)
        if (
            k_env is not None
            and k_sol is not None
            and r2 is not None
            and _K_PASSIVE_MIN <= k_env <= 0.001
            and 0.0 <= k_sol <= _K_SOLAR_MAX
            and r2 >= _MIN_R2
        ):
            return {
                "event_id": str(uuid.uuid4()),
                "timestamp": ts_str,
                "date": date_str,
                "hvac_mode": "ventilated",
                "k_passive": round(k_env, 5),
                "k_solar": round(k_sol, 3),
                "k_active": None,
                "r_squared_passive": round(r2, 3),
                "r_squared_active": None,
                "sample_count_post": len(window),
                "confidence_grade": "low",
                "schema_version": 2,
                "two_param": True,
                "source": "replay",
            }

    # 1-param fallback
    k_p, r2_p, _ = compute_k_passive(window)
    if k_p is None:
        return None
    return {
        "event_id": str(uuid.uuid4()),
        "timestamp": ts_str,
        "date": date_str,
        "hvac_mode": "ventilated",
        "k_passive": round(k_p, 5),
        "k_active": None,
        "r_squared_passive": round(r2_p, 3),
        "r_squared_active": None,
        "sample_count_post": len(window),
        "confidence_grade": "low",
        "schema_version": 2,
        "source": "replay",
    }


# ---------------------------------------------------------------------------
# EWMA model cache rebuild — mirrors learning.py _update_thermal_model_cache
# ---------------------------------------------------------------------------

_ALPHA_MAP = {"high": 0.3, "medium": 0.15, "low": 0.05}
_THERMAL_OBS_CAP = 500


def _ewma(old: float | None, new: float, alpha: float) -> float:
    return new if old is None else (1.0 - alpha) * old + alpha * new


def rebuild_model_cache(all_obs: list[dict]) -> dict:
    """Recompute thermal_model_cache EWMA from all obs in chronological order.

    Mirrors _update_thermal_model_cache logic exactly:
    - passive/heat/cool modes update k_passive
    - fan_only updates k_vent
    - ventilated updates k_vent_window (and k_solar when two_param=True)
    - solar updates k_solar
    """
    sorted_obs = sorted(all_obs, key=lambda o: o.get("timestamp", o.get("date", "")))
    cache: dict = {
        "k_passive": None,
        "k_active_heat": None,
        "k_active_cool": None,
        "k_vent": None,
        "k_vent_window": None,
        "k_solar": None,
        "observation_count_heat": 0,
        "observation_count_cool": 0,
        "observation_count_passive": 0,
        "observation_count_fan_only": 0,
        "observation_count_vent": 0,
        "observation_count_solar": 0,
        "avg_r_squared_passive": None,
        "last_observation_date": None,
        "confidence_k_passive": "none",
        "confidence_k_hvac": "none",
    }

    for obs in sorted_obs:
        grade = obs.get("confidence_grade", "low")
        alpha = _ALPHA_MAP.get(grade, 0.05)
        mode = obs.get("hvac_mode", "")
        k_p = obs.get("k_passive")
        k_a = obs.get("k_active")
        k_sol = obs.get("k_solar")
        r2_p = obs.get("r_squared_passive")

        envelope_mode = mode not in ("fan_only", "ventilated")

        if k_p is not None and envelope_mode:
            cache["k_passive"] = _ewma(cache["k_passive"], k_p, alpha)
        if r2_p is not None and envelope_mode:
            cache["avg_r_squared_passive"] = _ewma(cache["avg_r_squared_passive"], r2_p, alpha)

        if mode == "heat" and k_a is not None:
            cache["k_active_heat"] = _ewma(cache["k_active_heat"], k_a, alpha)
            cache["observation_count_heat"] += 1
        elif mode == "cool" and k_a is not None:
            cache["k_active_cool"] = _ewma(cache["k_active_cool"], k_a, alpha)
            cache["observation_count_cool"] += 1
        elif mode == "passive":
            cache["observation_count_passive"] += 1
        elif mode == "fan_only":
            if k_p is not None:
                cache["k_vent"] = _ewma(cache["k_vent"], k_p, alpha)
            cache["observation_count_fan_only"] += 1
        elif mode == "ventilated":
            if k_p is not None:
                cache["k_vent_window"] = _ewma(cache["k_vent_window"], k_p, alpha)
            if k_sol is not None and obs.get("two_param"):
                cache["k_solar"] = _ewma(cache["k_solar"], k_sol, alpha)
            cache["observation_count_vent"] += 1
        elif mode == "solar":
            if k_sol is not None:
                cache["k_solar"] = _ewma(cache["k_solar"], k_sol, alpha)
            cache["observation_count_solar"] += 1

        cache["last_observation_date"] = obs.get("date")

    # Confidence grades
    n_hvac = cache["observation_count_heat"] + cache["observation_count_cool"]
    cache["confidence_k_hvac"] = (
        "high" if n_hvac >= 20 else "medium" if n_hvac >= 10 else "low" if n_hvac >= 1 else "none"
    )
    n_passive = cache["observation_count_passive"]
    cache["confidence_k_passive"] = (
        "high" if n_passive >= 10 else "medium" if n_passive >= 5 else "low" if n_passive >= 1 else "none"
    )

    # Round for readability
    for k in (
        "k_passive",
        "k_active_heat",
        "k_active_cool",
        "k_vent",
        "k_vent_window",
        "k_solar",
        "avg_r_squared_passive",
    ):
        if cache[k] is not None:
            cache[k] = round(cache[k], 5)

    return cache


# ---------------------------------------------------------------------------
# SSH read/write
# ---------------------------------------------------------------------------

_LEARNING_DB_REMOTE = "/config/climate_advisor_learning.json"
_CHART_LOG_REMOTE = "/config/climate_advisor_chart_log.json"


def _ssh_args(config: dict) -> list[str]:
    host = config.get("HA_HOST", "homeassistant.local")
    user = config.get("HA_SSH_USER", "root")
    key = config.get("HA_SSH_KEY", "")
    port = str(config.get("HA_SSH_PORT", "22"))
    args = ["ssh", f"-p{port}", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        args += ["-i", key]
    args.append(f"{user}@{host}")
    return args


def fetch_learning_json_ssh(config: dict) -> dict:
    result = subprocess.run(
        [*_ssh_args(config), f"cat {_LEARNING_DB_REMOTE}"],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH read failed: {result.stderr.decode()}")
    return json.loads(result.stdout)


def write_learning_json_ssh(config: dict, data: dict) -> None:
    json_bytes = json.dumps(data, indent=2).encode()
    tmp = f"{_LEARNING_DB_REMOTE}.replay_tmp"
    cmd = f"cat > {tmp} && mv {tmp} {_LEARNING_DB_REMOTE}"
    result = subprocess.run([*_ssh_args(config), cmd], input=json_bytes, capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"SSH write failed: {result.stderr.decode()}")


def fetch_chart_log_ssh(config: dict) -> list[dict]:
    """SSH into HA and read the chart_log JSON. Returns the entries list."""
    result = subprocess.run(
        [*_ssh_args(config), f"cat {_CHART_LOG_REMOTE}"],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH read failed: {result.stderr.decode()}")
    data = json.loads(result.stdout)
    return data.get("entries", [])


def build_windows_from_chart_log(
    entries: list[dict],
    days: int = 30,
    min_window_minutes: int = 60,
    max_window_minutes: int = 240,
) -> list[list[dict]]:
    """Convert chart_log entries into ventilated_decay sample windows.

    Chart_log entries have fields: ts, hvac, fan, indoor, outdoor, windows_open.
    A window is a contiguous run where HVAC is idle and windows_open is True.
    Each sample dict matches the format expected by compute_k_passive/compute_k_env_solar.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)

    windows: list[list[dict]] = []
    current: list[dict] = []

    def _flush() -> None:
        if current:
            elapsed_min = (len(current) - 1) * _avg_interval(current)
            if elapsed_min >= min_window_minutes:
                windows.append(list(current))
            current.clear()

    def _avg_interval(samples: list[dict]) -> float:
        if len(samples) < 2:
            return 30.0
        ts0 = datetime.fromisoformat(samples[0]["timestamp"])
        ts1 = datetime.fromisoformat(samples[-1]["timestamp"])
        return (ts1 - ts0).total_seconds() / 60.0 / max(len(samples) - 1, 1)

    for entry in entries:
        ts_str = entry.get("ts", "")
        if not ts_str:
            continue
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if ts < cutoff:
            continue

        indoor = entry.get("indoor")
        outdoor = entry.get("outdoor")
        hvac = entry.get("hvac", "")
        windows_open = entry.get("windows_open")

        if indoor is None or outdoor is None:
            _flush()
            continue

        hvac_idle = hvac in ("idle", "off", "", "fan") or ("heat" not in (hvac or "") and "cool" not in (hvac or ""))
        # Exclude fan-only: fan=True but hvac=idle means CA-driven fan, which is ventilated
        # We include fan=True entries as long as HVAC isn't actively heating/cooling
        if not hvac_idle or not windows_open:
            _flush()
            continue

        elapsed = 0.0
        if current:
            ts0 = datetime.fromisoformat(current[0]["timestamp"])
            elapsed = (ts - ts0).total_seconds() / 60.0

        current.append(
            {
                "indoor_temp_f": float(indoor),
                "outdoor_temp_f": float(outdoor),
                "elapsed_minutes": elapsed,
                "solar_factor": _solar_factor(ts.hour + ts.minute / 60.0),
                "timestamp": ts_str,
            }
        )

        if elapsed >= max_window_minutes:
            _flush()

    _flush()
    return windows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay historical HA sensor data through the thermal learning OLS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--chart-log",
        action="store_true",
        help="Use CA chart_log JSON (via SSH) instead of HA state history. "
        "Recommended: chart_log has real indoor+outdoor temps at ~30min resolution. "
        "Does not require --climate-entity / --outdoor-entity / --window-entities.",
    )
    parser.add_argument(
        "--climate-entity",
        default=None,
        help="Thermostat entity ID (e.g. climate.ecobee_thermostat) — not needed with --chart-log",
    )
    parser.add_argument(
        "--outdoor-entity", default=None, help="Outdoor temperature sensor entity ID — not needed with --chart-log"
    )
    parser.add_argument(
        "--window-entities", default=None, help="Comma-separated binary_sensor entity IDs — not needed with --chart-log"
    )
    parser.add_argument("--days", type=int, default=30, help="Days of history to use (default: 30)")
    parser.add_argument(
        "--min-window-minutes", type=int, default=60, help="Min obs window length in minutes (default: 60)"
    )
    parser.add_argument(
        "--temp-unit",
        choices=["F", "C"],
        default=None,
        help="Force temperature unit (default: auto-detect from HA config)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show results without writing to HA")
    parser.add_argument("--write", action="store_true", help="Merge replay obs into HA learning DB via SSH")
    args = parser.parse_args()

    if not args.dry_run and not args.write:
        parser.error("Specify --dry-run to preview or --write to commit. Start with --dry-run.")

    if not args.chart_log and not (args.climate_entity and args.outdoor_entity and args.window_entities):
        parser.error("Either --chart-log or all of --climate-entity, --outdoor-entity, --window-entities are required.")

    config = load_config()
    ha_url = config.get("HA_URL", "").rstrip("/")
    ha_token = config.get("HA_TOKEN") or config.get("HA_API_TOKEN", "")

    # Auto-detect temperature unit from HA
    temp_unit = args.temp_unit
    if temp_unit is None:
        ha_cfg = fetch_ha_config(ha_url, ha_token)
        temp_unit = ha_cfg.get("unit_system", {}).get("temperature", "°F")
        temp_unit = "C" if "C" in temp_unit else "F"
        print(f"HA temperature unit: {temp_unit}")

    if args.chart_log:
        # --- Chart-log path (SSH) ---
        print(f"\nFetching chart_log from HA via SSH ({args.days} days)...")
        chart_entries = fetch_chart_log_ssh(config)
        total = len(chart_entries)
        print(f"  chart_log: {total} total entries")

        print(f"\nDetecting ventilated_decay windows (min {args.min_window_minutes} min)...")
        windows = build_windows_from_chart_log(
            chart_entries,
            days=args.days,
            min_window_minutes=args.min_window_minutes,
        )
        print(f"  Found {len(windows)} candidate windows")

    else:
        # --- Entity state history path (REST API) ---
        if not ha_url or not ha_token:
            sys.exit("ERROR: Set HA_URL and HA_TOKEN in tools/.env (not needed with --chart-log)")

        window_entities = [e.strip() for e in args.window_entities.split(",") if e.strip()]
        all_entities = [args.climate_entity, args.outdoor_entity] + window_entities

        print(f"\nFetching {args.days} days of history for {len(all_entities)} entities...")
        history = fetch_entity_history(ha_url, ha_token, all_entities, args.days)

        climate_states = history.get(args.climate_entity, [])
        outdoor_states = history.get(args.outdoor_entity, [])
        if not climate_states:
            sys.exit(f"ERROR: No history for climate entity '{args.climate_entity}'")
        if not outdoor_states:
            sys.exit(f"ERROR: No history for outdoor entity '{args.outdoor_entity}'")

        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(days=args.days)
        timestamps = build_time_grid(start_time, end_time, interval_minutes=5)
        print(f"  Time grid: {len(timestamps)} points × 5 min ({args.days}d)")

        indoor_temps = interpolate_numeric(climate_states, timestamps, attr="current_temperature")
        outdoor_temps = interpolate_numeric(outdoor_states, timestamps)

        if temp_unit == "C":
            indoor_temps = [t * 9 / 5 + 32 if t is not None else None for t in indoor_temps]
            outdoor_temps = [t * 9 / 5 + 32 if t is not None else None for t in outdoor_temps]

        hvac_actions = interpolate_categorical(climate_states, timestamps, attr="hvac_action")

        window_series: list[bool | None] = [None] * len(timestamps)
        for ent in window_entities:
            ent_states = history.get(ent, [])
            if not ent_states:
                print(f"  WARNING: No history for window entity '{ent}' — skipping")
                continue
            vals = interpolate_categorical(ent_states, timestamps)
            for i, v in enumerate(vals):
                if v == "on":
                    window_series[i] = True
                elif v == "off" and window_series[i] is None:
                    window_series[i] = False

        print(f"\nDetecting ventilated_decay windows (min {args.min_window_minutes} min)...")
        windows = find_ventilated_windows(
            timestamps,
            indoor_temps,
            outdoor_temps,
            hvac_actions,
            window_series,
            min_window_minutes=args.min_window_minutes,
        )
        print(f"  Found {len(windows)} candidate windows")

    # Run OLS on each window
    print("\nRunning OLS...")
    obs_list: list[dict] = []
    rejected = 0
    for window in windows:
        obs = run_ols_on_window(window)
        if obs:
            sf_vals = [s["solar_factor"] for s in window]
            sf_range = max(sf_vals) - min(sf_vals) if sf_vals else 0.0
            path = "2p" if obs.get("two_param") else "1p"
            k_sol_str = f" k_solar={obs['k_solar']:+.3f}" if obs.get("k_solar") is not None else ""
            print(
                f"  [{path}] {obs['date']} {obs['timestamp'][11:16]} "
                f"k_env={obs['k_passive']:+.4f}{k_sol_str} "
                f"R²={obs['r_squared_passive']:.3f} n={obs['sample_count_post']} "
                f"sf_range={sf_range:.2f}"
            )
            obs_list.append(obs)
        else:
            rejected += 1

    two_param_count = sum(1 for o in obs_list if o.get("two_param"))
    print(f"\nSummary: {len(obs_list)} committed / {rejected} rejected")
    print(f"  2-param (k_solar learned): {two_param_count}")
    print(f"  1-param (k_env only):      {len(obs_list) - two_param_count}")

    if not obs_list:
        print("\nNo obs to write — done.")
        return

    if args.dry_run:
        print("\n(dry-run) Use --write to merge into HA learning DB.")
        return

    # Write path
    print("\nFetching current learning DB from HA via SSH...")
    db = fetch_learning_json_ssh(config)
    existing_obs: list[dict] = db.get("thermal_observations", [])
    print(f"  Existing obs: {len(existing_obs)}")

    # Deduplicate by source+timestamp (replay obs have source="replay")
    existing_replay_keys = {
        (o.get("timestamp"), o.get("hvac_mode")) for o in existing_obs if o.get("source") == "replay"
    }
    new_obs = [o for o in obs_list if (o["timestamp"], o["hvac_mode"]) not in existing_replay_keys]
    print(f"  New replay obs (after dedup): {len(new_obs)}")

    merged = existing_obs + new_obs
    merged.sort(key=lambda o: o.get("timestamp", ""))

    # 90-day rolling trim
    cutoff = (datetime.now().date() - timedelta(days=90)).isoformat()
    merged = [o for o in merged if o.get("date", "") >= cutoff]

    # Hard cap
    if len(merged) > _THERMAL_OBS_CAP:
        merged = merged[-_THERMAL_OBS_CAP:]

    # Rebuild EWMA model cache from all obs
    print("\nRebuilding EWMA model cache from all obs...")
    new_model = rebuild_model_cache(merged)
    db["thermal_observations"] = merged
    db["thermal_model_cache"] = new_model

    print("\nNew model after rebuild:")
    for k in ("k_passive", "k_vent_window", "k_solar", "confidence_k_passive", "confidence_k_hvac"):
        print(f"  {k}: {new_model.get(k)}")

    print(f"\nWriting {len(merged)} obs to HA learning DB...")
    write_learning_json_ssh(config, db)
    print("Done. Restart Climate Advisor integration in HA to activate the updated model.")
    print("  (HA > Settings > Devices & Services > Climate Advisor > Reload)")


if __name__ == "__main__":
    main()
