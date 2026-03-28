# Climate Advisor — Learning Engine Design

## Purpose

The learning engine is what makes Climate Advisor more than a fancy scheduler. It observes the gap between what was recommended and what actually happened, and uses that gap to suggest improvements. The long-term goal is a system that adapts to the household's real behavior rather than insisting on idealized behavior.

## Core Principle

**If you give people a behavior to perform and they rarely do it, calculate a better alternative that can be performed and ask the user if they'd like to switch.**

## Data Collection

Every day, a `DailyRecord` is saved with:

### What Was Recommended
- Day type and trend
- Whether windows were recommended (and suggested open/close times)
- Recommended HVAC mode

### What Actually Happened
- Whether windows were opened (and actual times)
- Total HVAC runtime in minutes
- Time spent away from home
- Number of door/window pause events
- Number of manual thermostat overrides

### Outcomes
- Average indoor temperature
- Minutes spent outside comfort range (comfort violations)
- Estimated energy cost (future: from utility rate integration)

### Suggestion Tracking
- Was a suggestion sent today?
- User response: accepted, dismissed, or ignored

## Storage

- JSON file in HA config directory (`climate_advisor_learning.json`)
- Rolling 90-day window (older records pruned automatically)
- Persists across HA restarts

## Suggestion Generation

Suggestions are generated when `generate_suggestions()` is called (typically during the daily briefing). Requires minimum 14 days of data.

### Pattern: Low Window Compliance

**Detection:** Of days where windows were recommended, compliance < 30%.
**Suggestion:** "Would you like Climate Advisor to stop suggesting window actions and instead rely on HVAC with optimized schedules?"
**If accepted:** `disable_window_recommendations = True` — the briefing stops suggesting window actions, and the automation engine uses HVAC for those day types instead.
**Trade-off:** Slightly higher energy use, zero human effort required.

### Pattern: Frequent Manual Overrides

**Detection:** More than 10 manual thermostat adjustments in 14 days.
**Suggestion:** "Would you like Climate Advisor to analyze the override patterns and suggest new setpoints?"
**If accepted:** System flags for setpoint analysis (future: actually analyze the override direction and magnitude to suggest specific new setpoints).
**Trade-off:** May result in slightly different comfort baseline.

### Pattern: High Runtime on Mild/Warm Days

**Detection:** Average HVAC runtime > 120 minutes on mild/warm day types.
**Suggestion:** "Would you like to add more door/window sensors, or adjust the setpoints for mild days?"
**Root cause:** Usually indicates doors/windows being left open without sensors, or setpoints that are too aggressive for the house's thermal characteristics.

### Pattern: Short Departures

**Detection:** More than 5 departures of 30–45 minutes in 14 days.
**Suggestion:** "Would you like to shorten the setback delay from 15 minutes to 5 minutes, or skip setback for departures under 1 hour?"
**Insight:** The current 15-minute delay means the setback barely takes effect before the person returns, wasting the recovery energy. Options: faster setback (saves more) or skip setback (saves the recovery penalty).

### Pattern: Comfort Violations

**Detection:** Indoor temp outside comfort range for > 30 minutes on 5+ of the last 14 days.
**Suggestion:** "Would you like to reduce the setback aggressiveness, or start the morning warm-up earlier?"
**If accepted:** Setback modifier increases by 2°F (less aggressive), and/or morning pre-heat starts 15 minutes earlier.

### Pattern: Frequent Door Pauses

**Detection:** More than 20 HVAC pause events from door/window sensors in 14 days.
**Suggestion:** "If a specific door is the main culprit, would you like to extend the pause delay for that door, or exclude it from monitoring?"
**Future enhancement:** Track which specific sensor triggers most pauses and name it in the suggestion.

## Suggestion Lifecycle

1. **Generated:** During daily briefing generation, based on pattern analysis
2. **Delivered:** Appended to the daily briefing email/notification
3. **Response:** User can accept, dismiss, or ignore
   - **Accept:** Config changes are applied, recorded in settings_history
   - **Dismiss:** Suggestion key added to dismissed list, won't reappear for SUGGESTION_COOLDOWN_DAYS (7)
   - **Ignore:** Suggestion reappears next day until acted on or pattern resolves

## Thermal Model

The thermal model records how quickly the house heats and cools under HVAC control. Observations accumulate over time to produce heating and cooling rate estimates that are used to compute adaptive bedtime setback depth and pre-heat start time.

### `ThermalObservation` Dataclass

| Field | Type | Purpose |
|---|---|---|
| `timestamp` | `datetime` | UTC time the observation was recorded |
| `mode` | `str` | `"heat"` or `"cool"` — which HVAC mode was active |
| `start_temp_f` | `float` | Indoor temperature (°F) at the start of the HVAC run |
| `end_temp_f` | `float` | Indoor temperature (°F) at the end of the HVAC run |
| `duration_minutes` | `float` | Length of the HVAC run in minutes |
| `outdoor_temp_f` | `float \| None` | Outdoor temperature at observation time (for future stratification) |

### `record_thermal_observation(mode, start_temp_f, end_temp_f, duration_minutes, outdoor_temp_f=None)`

Called by the coordinator at the end of a completed heating or cooling run. Appends a `ThermalObservation` to the rolling history (capped at 90 observations). The observation is only recorded when `duration_minutes > 0` and the temperature delta is in the expected direction (indoor temp rose for heat, fell for cool).

### `get_thermal_model() -> dict`

Computes and returns the current thermal model from all stored observations.

**Output dict structure:**

```python
{
    "heating_rate_f_per_hour": float | None,   # degrees F gained per hour under heat mode
    "cooling_rate_f_per_hour": float | None,   # degrees F lost per hour under cool mode
    "confidence": str,                          # "none" | "low" | "medium" | "high"
    "observation_count_heat": int,             # number of heat observations used
    "observation_count_cool": int,             # number of cool observations used
}
```

**Confidence levels:**

| Level | Condition |
|---|---|
| `"none"` | Fewer than 3 observations of the relevant mode |
| `"low"` | 3–9 observations |
| `"medium"` | 10–19 observations |
| `"high"` | 20+ observations |

Rates are computed as the median of all per-observation rates (°F delta / duration hours) to reduce sensitivity to outliers.

### `get_weather_bias() -> dict`

Analyzes historical records where both forecast and observed temperatures are present to detect systematic forecast error (the weather service consistently running warm or cool).

**Output dict structure:**

```python
{
    "high_bias": float,       # mean forecast high error in °F (positive = forecast runs warm)
    "low_bias": float,        # mean forecast low error in °F (positive = forecast runs warm)
    "confidence": str,        # "none" | "low" | "medium" | "high"
    "sample_count": int,      # number of days used to compute the bias
}
```

**Confidence levels:** same thresholds as `get_thermal_model()` (3/10/20 observations).

### Suggestion Keys for Thermal Learning

| Suggestion Key | Detection | Suggestion Text |
|---|---|---|
| `thermal_model_ready` | `get_thermal_model()` confidence transitions from `"none"` to `"low"` or better for both heat and cool | "Your home's thermal profile is ready. Bedtime setback depth and pre-heat timing are now tuned to your home's actual heating performance." |
| `forecast_bias_significant` | `get_weather_bias()` high or low bias magnitude exceeds 3°F at `"medium"` or higher confidence | "Your weather service appears to run [warm/cool] by about X°F on average. Climate Advisor has adjusted its timing calculations to compensate." |

### Sensor Attributes (Compliance Sensor)

The following attributes are added to `sensor.climate_advisor_comfort_score` when the thermal model is available:

| Attribute | Type | Description |
|---|---|---|
| `thermal_heating_rate` | `float \| None` | Heating rate in user's configured unit per hour (`None` if no data) |
| `thermal_cooling_rate` | `float \| None` | Cooling rate in user's configured unit per hour (`None` if no data) |
| `thermal_confidence` | `str` | Confidence level: `"none"`, `"low"`, `"medium"`, or `"high"` |
| `thermal_observation_count` | `int` | Total heat + cool observations recorded |
| `forecast_high_bias` | `float` | Forecast high bias in user's configured unit (0.0 if no data) |
| `forecast_low_bias` | `float` | Forecast low bias in user's configured unit (0.0 if no data) |
| `forecast_bias_confidence` | `str` | Confidence level for weather bias estimate |

---

## Future Learning Capabilities (v0.3+)

### ~~Thermal Model Learning~~ _(Complete — see Thermal Model section above)_
~~Track how quickly the house heats/cools under different conditions to build a simple thermal model.~~ Implemented in Phase 5. The thermal model now drives adaptive bedtime setback depth and pre-heat start time, and exposes rates via the compliance sensor.

### Seasonal Baselines
After a full year of data, establish seasonal baselines for runtime, comfort scores, and energy use. Detect anomalies (e.g., "Your heating runtime this November is 30% higher than last November — possible insulation issue or thermostat drift").

### Override Pattern Analysis
Instead of just counting overrides, analyze their direction and timing:
- "You consistently raise the temperature 2°F around 3pm" → suggest a scheduled bump
- "You lower the temperature every night before your usual bedtime" → suggest moving bedtime setback earlier

### Energy Cost Integration
With utility rate data, convert runtime minutes to estimated cost and show daily/weekly/monthly savings compared to "no automation" baseline.

## Configuration Changes Applied by Suggestions

| Suggestion Key | Config Changes |
|---------------|---------------|
| `low_window_compliance` | `disable_window_recommendations: true` |
| `frequent_overrides` | `request_setpoint_analysis: true` |
| `short_departures` | `occupancy_setback_minutes: 5` |
| `comfort_violations` | `setback_modifier: +2`, `morning_preheat_offset_minutes: 15` |
| `frequent_door_pauses` | `door_pause_seconds: 300` |
| `thermal_model_ready` | No config change — informational notification only |
| `forecast_bias_significant` | No config change — bias is applied internally at runtime |

## Compliance Summary API

The `get_compliance_summary()` method returns a dict suitable for sensor attributes:

```python
{
    "status": "active",            # or "collecting_data"
    "days_recorded": 28,
    "window_compliance": 0.35,     # 35% of recommended days
    "avg_daily_hvac_runtime_minutes": 145.5,
    "comfort_score": 0.92,         # 92% of time in comfort range
    "total_manual_overrides": 8,
    "pending_suggestions": 2,
}
```

### Metric Definitions

#### Comfort Violations (`comfort_violations_minutes`)
**Unit:** minutes
**What it means:** Time during the day when the indoor temperature was outside the configured
comfort range (`comfort_heat`–`comfort_cool` settings, default 70–75°F).
**How it accumulates:** The coordinator checks indoor temperature on every data update.
Each check adds the **actual elapsed time** since the previous check (capped at 30 minutes)
when the temperature is outside the comfort range. This prevents double-counting when the
coordinator is refreshed more frequently than the 30-minute scheduled interval (e.g., after
door/window events or automation revisits).
**Maximum per day:** 1440 minutes (24 hours). Values above 1440 in historical records
indicate data recorded before v0.2.x (fixed-30-min bug) and should be disregarded.

#### Comfort Score (`comfort_score`)
**Formula:** `1 − (sum of daily violation_minutes / (days_recorded × 1440))`
**Range:** 0.0 (always outside range) to 1.0 (always within range), reported as a percentage
**Example:** 3 days, total 2160 violation minutes → `1 − (2160 / 4320)` = 0.50 = 50%
**Sensor:** `sensor.climate_advisor_comfort_score` reports this as a percentage (0–100%)
**Trigger:** More than 5 days with over 30 violation minutes triggers the `comfort_violations`
suggestion, which reduces setback aggressiveness and starts morning pre-heat 15 minutes earlier.
