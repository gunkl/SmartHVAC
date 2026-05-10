<!-- Nav: ← [05-LEARNING-ENGINE-DESIGN.md](05-LEARNING-ENGINE-DESIGN.md) | → [learning.py](../custom_components/climate_advisor/learning.py) + [coordinator.py](../custom_components/climate_advisor/coordinator.py) | ↔ [08-COMPUTATION-REFERENCE.md](08-COMPUTATION-REFERENCE.md) -->

# Thermal Model v3 — Territory Spec (Tier 3)

## Anchors

| Question | Short answer (≤2 sentences) | → Full answer |
|---|---|---|
| What triggers a `passive_decay` observation and what does it measure? | Starts when HVAC off, fan off, windows closed, and `\|T_indoor − T_outdoor\| ≥ 3°F`; measures `k_passive` (envelope decay rate, hr⁻¹, always negative). Requires 30 samples minimum (long-window path) or 5 samples (rolling-window path). | [§Observation Types](#observation-types) |
| When does the gate bridge activate and what does it do? | Activates when `k_passive is None OR confidence_k_passive == "none"` AND `k_vent_window` is not None and `≤ 0`. Promotes `k_vent_window` as a proxy `k_passive` so physics prediction can run on bridge-only homes without a data wipe. | [§Gate Bridge](#gate-bridge) |
| What are the three rolling-window thresholds and what happens at each? | `THERMAL_ROLLING_MIN_WINDOW_MINUTES` (30 min): earliest commit attempt; `THERMAL_ROLLING_MAX_WINDOW_MINUTES` (240 min): hard cap — commit or abandon regardless of signal; signal check (`THERMAL_ROLLING_MIN_DELTA_T_F = 0.2°F`): required indoor ΔT to commit between the two limits. | [§Rolling Window Constraints](#rolling-window-constraints) |
| How does `compute_k_passive` reject an observation and what codes does it emit? | Five rejection codes: `REJECT_TOO_FEW_SAMPLES`, `REJECT_SMALL_DELTA` (Σδ² = 0), `REJECT_OLS_WRONG_SIGN` (k > 0), `REJECT_OLS_BOUNDS` (k outside [-0.5, -0.001]), `REJECT_OLS_BAD_FIT` (R² < 0.20). Exactly one of `k_passive` or `rejection_code` is non-None in the return tuple. | [§OLS Functions — compute\_k\_passive](#compute_k_passive) |
| What invariant must hold for every committed observation's `k_passive`? | `k_passive < 0` for all non-bridge envelope modes; `k_solar ≥ 0` for all committed solar observations. The bridge proxy (`k_vent_window`) may equal 0.0 exactly (perfectly inert home) but never > 0. | [§Invariants](#invariants) |
| How does `ventilated_decay` commit path choose between 1-param and 2-param OLS? | When solar factor range across samples ≥ 0.30, the 2-param path fires first and — if bounds pass — commits both `k_env` and `k_solar`, bypassing 1-param entirely. If 2-param fails bounds/R², the 1-param path runs as fallback. | [§OLS Functions — compute\_k\_env\_solar](#compute_k_env_solar) |
| What is the EWMA alpha for each confidence grade and which observation types write `k_passive`? | Alpha: high = 0.30, medium = 0.15, low = 0.05. Only `passive`, `heat`, and `cool` modes write `k_passive`; `fan_only` writes `k_vent`; `ventilated` writes `k_vent_window`; `solar` writes `k_solar`. | [§EWMA Update](#ewma-update-_update_thermal_model_cache) |

---

## Scope

**Files:**
- `learning.py` — OLS functions (`compute_k_passive`, `compute_k_env_solar`, `compute_k_active`, `compute_k_active_single_point`), commit routing (`_commit_event_from_dict`), EWMA update (`_update_thermal_model_cache`), model output (`get_thermal_model`, `record_thermal_observation`)
- `coordinator.py` — observation orchestration (`_sample_all_observations`, `_start_hvac_observation`, `_start_decay_observation`, `_end_hvac_active_phase`, `_check_hvac_stabilization`, `_evaluate_rolling_window`, `_commit_rolling_window_obs`, `_commit_observation_if_sufficient`, `_abandon_observation`, `_commit_observation`), ODE prediction (`_build_predicted_indoor_future`, `_simulate_indoor_physics`, `_simulate_indoor_physics_v3`)

**Line ranges (verified against source):**

| Function | File | Start line |
|---|---|---|
| `compute_k_passive` | learning.py | 185 |
| `compute_k_env_solar` | learning.py | 283 |
| `compute_k_active` | learning.py | 359 |
| `compute_k_active_single_point` | learning.py | 418 |
| `record_thermal_observation` | learning.py | 693 |
| `_update_thermal_model_cache` | learning.py | 713 |
| `get_thermal_model` | learning.py | 831 |
| `_commit_event_from_dict` | learning.py | 974 |
| `_start_hvac_observation` | coordinator.py | 2495 |
| `_sample_all_observations` | coordinator.py | 2595 |
| `_check_hvac_stabilization` | coordinator.py | 2931 |
| `_evaluate_rolling_window` | coordinator.py | 3232 |
| `_commit_rolling_window_obs` | coordinator.py | 3303 |
| `_build_predicted_indoor_future` | coordinator.py | 4245 |
| `_simulate_indoor_physics` | coordinator.py | 4037 |
| `_simulate_indoor_physics_v3` | coordinator.py | 4094 |

**Out of scope for this spec:** suggestion generation, weather bias, daily record lifecycle, automation engine, briefing text.

---

## Observation Types

All six types run concurrently in `_pending_observations: dict[str, PendingObservation]`. The dict is keyed by the `OBS_TYPE_*` string constant.

| obs_type | Trigger conditions | Target parameter(s) | Min samples (long-window path) | Can run concurrently with? |
|---|---|---|---|---|
| `hvac_heat` | `hvac_action = "heating"` transitions to active | `k_active_heat`, `k_passive` (via pre-heat buffer) | 4 post-heat samples (`THERMAL_MIN_POST_HEAT_SAMPLES`) | Nothing HVAC; all passive types commit/abandon on HVAC start |
| `hvac_cool` | `hvac_action = "cooling"` transitions to active | `k_active_cool`, `k_passive` | 4 post-heat samples | Same as hvac_heat |
| `passive_decay` | HVAC off, fan off, windows closed, `\|T_indoor − T_outdoor\| ≥ THERMAL_PASSIVE_MIN_DELTA_F (3.0°F)` | `k_passive` | 30 (`THERMAL_PASSIVE_MIN_SAMPLES`) | `fan_only_decay`, `ventilated_decay`, `solar_gain` — but mutual exclusion via trigger conditions prevents concurrent passive + fan or passive + vent |
| `fan_only_decay` | Fan active (`_fan_active` or thermostat `fan_only` state), HVAC off, no open sensors | `k_vent` | 15 (`THERMAL_FAN_MIN_SAMPLES`) | `solar_gain` is not started while fan is active |
| `ventilated_decay` | Any door/window sensor open, HVAC off, `\|T_indoor − T_outdoor\| ≥ THERMAL_VENTILATED_MIN_DELTA_F (1.0°F)` | `k_vent_window` (and optionally `k_solar` via 2-param path) | 20 (`THERMAL_VENT_MIN_SAMPLES`) | Can coexist with anything not HVAC; but fan trigger and sensor-open trigger are mutually exclusive |
| `solar_gain` | HVAC off, fan off, windows closed, `T_indoor > T_outdoor`, daytime (local 08:00–18:00, `THERMAL_SOLAR_DAYTIME_START_H` / `END_H`) | `k_solar` | 20 (`THERMAL_SOLAR_MIN_SAMPLES`) | `passive_decay` may also be running (different parameter) |

**HVAC contamination rule:** When HVAC starts (`_start_hvac_observation()`), all four non-HVAC types in `_pending_observations` are committed via `_commit_observation_if_sufficient()` (which commits if `len(samples) >= min_samples`, else abandons). The contamination check fires before the new HVAC observation is started.

---

## Observation Lifecycle

### State Machine

```
(start trigger fires)
        ↓
   status = "monitoring"
        ↓
   samples accumulate via _sample_all_observations()
        ↓
   ┌─── commit condition ────┐    ┌─── abandon condition ───┐
   │ status → "committing"   │    │ popped from dict         │
   │ _commit_observation()   │    │ rejection_log appended   │
   │ → COMMITTED             │    │ → ABANDONED              │
   └─────────────────────────┘    └──────────────────────────┘
```

Terminal states: committed (observation recorded in `thermal_observations`) or abandoned (rejection event logged in `rejection_log`). No re-entry once a type is removed from `_pending_observations`.

### Rolling Decay Types (passive_decay, fan_only_decay, ventilated_decay, solar_gain)

**Start:** `_start_decay_observation(obs_type)` fires from Section B of `_sample_all_observations()` when trigger conditions are first met. Creates the observation dict with `status="monitoring"`, empty `samples`, and `flags_at_start`.

**Sample accumulation:** Section A of `_sample_all_observations()` iterates all monitoring observations. Each type has a per-type decimation gate:

| Type | Gate constant | Interval |
|---|---|---|
| `passive_decay` | `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` | 5 min |
| `fan_only_decay` | `THERMAL_FAN_SAMPLE_INTERVAL_S` | 2 min |
| `ventilated_decay` | `THERMAL_PASSIVE_SAMPLE_INTERVAL_S` | 5 min |
| `solar_gain` | `THERMAL_SOLAR_SAMPLE_INTERVAL_S` | 5 min |

A sample is appended only when `elapsed_since_last_sample >= interval_s`. Each sample is a dict with `timestamp`, `indoor_temp_f`, `outdoor_temp_f`, `elapsed_minutes`. For `ventilated_decay`, `solar_factor` (from `_solar_factor(now.hour)`) is also recorded at collection time. The hard cap per observation is `THERMAL_MAX_OBS_SAMPLES = 200`.

**Commit decision (`_evaluate_rolling_window`):** Called from Section C for each rolling type. Two-threshold logic:

1. `elapsed < THERMAL_ROLLING_MIN_WINDOW_MINUTES (30)` AND signal not sufficient → keep collecting (return `False`)
2. `elapsed >= THERMAL_ROLLING_MIN_WINDOW_MINUTES` AND `signal_sufficient=True` → commit via `_commit_rolling_window_obs()`
3. `elapsed >= THERMAL_ROLLING_MAX_WINDOW_MINUTES (240)` → commit if `len(samples) >= THERMAL_MIN_DECAY_SAMPLES + 1 (= 5)`, else abandon with reason `"max_window_exceeded"`
4. Between min and max, signal not sufficient → log and keep alive (samples trimmed to last 96 if > 96)

**Solar keep-alive guard (ventilated_decay only):** During daytime hours (08:00–18:00), if `sf_range < THERMAL_SOLAR_FACTOR_MIN_RANGE (0.30)`, `_vent_signal_sufficient` is forced to `False`, suppressing early commit even after the 30-min minimum. This prevents a 1-param commit before the 2-param OLS can distinguish `k_env` from `k_solar`. The 240-min hard cap overrides the guard.

**Hard cap behavior:** When `elapsed >= THERMAL_ROLLING_MAX_WINDOW_MINUTES`, `_evaluate_rolling_window` commits unconditionally (with `skip_delta_guard=True`) if `len(samples) >= 5`, otherwise abandons with `reason_code="max_window_exceeded"`.

### HVAC Types (hvac_heat, hvac_cool)

**Start:** `_start_hvac_observation(session_mode)` fires when `hvac_action` changes to `"heating"` or `"cooling"`. Creates the observation with `_phase="active"`, `active_samples=[]`, `pre_heat_samples` from the `_pre_heat_sample_buffer`, and `status="monitoring"`.

**Sample accumulation:** Active phase samples every coordinator poll (no decimation gate). Post-heat phase uses `THERMAL_HVAC_POST_HEAT_SAMPLE_INTERVAL_S` (5 min). Post-heat samples go into `post_heat_samples`.

**Active → post_heat transition:** `_end_hvac_active_phase(obs_type)` fires when `hvac_action` leaves `"heating"`/`"cooling"`. Sets `_phase="post_heat"`, records `active_end`, computes `session_minutes`.

**Commit decision (`_check_hvac_stabilization`):** Minimum post-heat samples before commit attempt:
- No proxy: `THERMAL_MIN_POST_HEAT_SAMPLES = 4`
- Proxy available (`k_vent_window < 0` in cache): 1 sample

Plateau guard (non-proxy path only): requires `peak_indoor_f − end_indoor_f >= THERMAL_HVAC_MIN_DECAY_F (0.3°F)`. If not met, abandons with reason `"plateau guard: insufficient post-heat decay"`.

When commit conditions are met, `obs["status"] = "stabilized"` is set and `_commit_observation(obs_type)` is called.

Post-heat timeout: if `elapsed_post > THERMAL_POST_HEAT_TIMEOUT_MINUTES (45)`, the observation is abandoned.

---

## OLS Functions

### compute_k_passive()

**Signature:** `compute_k_passive(post_samples, pre_samples=None, min_samples=None) -> tuple[float | None, float, str | None]`

**Input:** `post_samples` — list of sample dicts (mandatory). `pre_samples` — optional pre-heat buffer samples processed as a separate window to avoid spurious rates at the pre→post boundary. `min_samples` — overrides the OLS floor (default: `THERMAL_MIN_POST_HEAT_SAMPLES = 4`; pass `THERMAL_MIN_DECAY_SAMPLES = 4` for rolling-window decay types).

**Pre-condition:** `total_samples = len(post_samples) + len(pre_samples) >= min_samples + 1`

**Computation:** 1-param OLS forced through origin:
```
k = Σ(rate_i × delta_i) / Σ(delta_i²)
```
where for each consecutive sample pair `(i, i+1)`:
- `rate_i = (T_indoor[i+1] − T_indoor[i]) / dt_hours`
- `delta_i = midpoint(T_indoor) − midpoint(T_outdoor)` over the interval
- `T_indoor` values are first passed through a 3-sample centered moving average (`_smooth_temps`) to reduce 1°F quantisation noise; edge samples are unchanged

Each pre/post window is processed independently. Pairs are built within each window only (no cross-boundary pairs).

**R² formula (forced-through-origin model):**
```
R² = 1 − SS_res / SS_tot
where SS_res = Σ(rate_i − k × delta_i)²
      SS_tot = Σ(rate_i²)
```
R² is clamped to [0.0, ∞).

**Rejection codes (in evaluation order):**

| Code | Condition |
|---|---|
| `REJECT_TOO_FEW_SAMPLES` | `len(rates) < min_samples` after building pairs |
| `REJECT_SMALL_DELTA` | `Σ(delta_i²) == 0` (all indoor/outdoor differentials are zero) |
| `REJECT_OLS_WRONG_SIGN` | `k > 0` (physics requires negative k for passive cooling toward outdoor) |
| `REJECT_OLS_BOUNDS` | `k` outside `[THERMAL_K_PASSIVE_MIN (-0.5), THERMAL_K_PASSIVE_MAX (-0.001)]` |
| `REJECT_OLS_BAD_FIT` | `R² < THERMAL_MIN_R_SQUARED (0.20)` |

**Post-condition:** Returns `(k_passive, r_squared, rejection_code)`. Exactly one of `k_passive` or `rejection_code` is non-None. On success: `k_passive < 0`, `rejection_code = None`. On failure: `k_passive = None`, `rejection_code` is one of the `REJECT_*` constants, `r_squared` is the computed value (may be 0.0 if OLS never ran).

---

### compute_k_env_solar()

**Signature:** `compute_k_env_solar(samples, min_samples=4) -> tuple[float | None, float | None, float | None]`

**Input:** `samples` — list of sample dicts with `indoor_temp_f`, `outdoor_temp_f`, `elapsed_minutes`, `solar_factor`. Consecutive pairs are used.

**Pre-condition:** `len(pairs) >= min_samples` (4 by default) AND `sf_range = max(sfs) − min(sfs) >= THERMAL_SOLAR_FACTOR_MIN_RANGE (0.30)`.

**Computation:** 2-param OLS via 2×2 normal equations (no scipy):
```
[x1x1  x1x2] [k_env  ]   [x1y]
[x1x2  x2x2] [k_solar] = [x2y]

where:
  x1 = delta_i = midpoint(T_in − T_out)
  x2 = sf_i = midpoint(solar_factor)
  y  = rate_i = (T_in[i+1] − T_in[i]) / dt_hours
```
Solved as:
```
det = x1x1 * x2x2 − x1x2²
k_env   = (x2x2 * x1y − x1x2 * x2y) / det
k_solar = (x1x1 * x2y − x1x2 * x1y) / det
```

**R² (mean-centered, 2-param):**
```
R² = 1 − Σ(rate_i − k_env×delta_i − k_solar×sf_i)² / Σ(rate_i − mean_rate)²
```

**Fallback conditions (returns `(None, None, None)`):**
- Fewer than `min_samples` pairs
- `sf_range < THERMAL_SOLAR_FACTOR_MIN_RANGE` — insufficient solar variation for 2-param separation (returns without emitting a rejection code; caller falls back to 1-param)
- `abs(det) < 1e-12` — numerical near-singular matrix
- Bounds fail or R² below threshold — rejection handled by caller (`_commit_event_from_dict`) which falls through to 1-param

**Accepted bounds (checked in `_commit_event_from_dict`):**
- `k_env` in `[THERMAL_K_PASSIVE_MIN (-0.5), 0.001]`
- `k_solar` in `[0.0, THERMAL_K_SOLAR_MAX_F_PER_HR (8.0)]`
- `R² >= THERMAL_MIN_R_SQUARED (0.20)`

**Post-condition:** Returns `(k_env, k_solar, r_squared)` on success, or `(None, None, None)` on any failure. Note: does not emit a `REJECT_*` string — the `None` tuple is the failure signal.

---

## Commit Routing (_commit_event_from_dict)

`_commit_event_from_dict(event, force_grade, obs_type)` selects the commit path based on `obs_type`. Returns `(obs_dict | None, reject_code | None, r_squared | None)`.

| obs_type | Commit path | Cache keys written by `_update_thermal_model_cache` | `hvac_mode` tag in committed obs dict |
|---|---|---|---|
| `passive_decay` | 1-param OLS (`compute_k_passive`) on `event["samples"]`; min `THERMAL_MIN_DECAY_SAMPLES (4)` | `k_passive`, `avg_r_squared_passive`, `observation_count_passive` | `"passive"` |
| `fan_only_decay` | 1-param OLS on `event["samples"]` | `k_vent`, `observation_count_fan_only` | `"fan_only"` |
| `ventilated_decay` | 2-param OLS attempted first (when `sf_range >= 0.30`); if fails, 1-param fallback | `k_vent_window`, `observation_count_vent`; `k_solar` additionally when `two_param=True` | `"ventilated"` |
| `solar_gain` | Mean rate: `(T_last − T_first) / total_hours`; reject if rate < 0 | `k_solar`, `observation_count_solar` | `"solar"` |
| `hvac_heat` | 2-param path: `compute_k_passive(post_samples, pre_samples)` → `compute_k_active(active_samples, k_p)`; bridge proxy and single-point fallback applied when OLS returns None | `k_active_heat`, `k_passive` (when not from proxy), `observation_count_heat`, `swing_heat_f` | `"heat"` |
| `hvac_cool` | Same as hvac_heat; `session_mode = "cool"` | `k_active_cool`, `k_passive` (when not from proxy), `observation_count_cool`, `swing_cool_f` | `"cool"` |

**Bridge proxy (hvac_heat/hvac_cool only, D17):** If `compute_k_passive()` returns `None` and `k_vent_window < 0` exists in `thermal_model_cache`, `k_vent_window` is used as proxy `k_passive` with `force_grade = "low"`. The committed obs dict writes `k_passive = None` (D21) so the proxy value never contaminates the envelope EWMA.

**Single-point fallback (D19):** If `k_active` is `None` after `compute_k_active()` (n_active < 2) and `k_p` is available (real or proxy), `compute_k_active_single_point()` is called with `T_start`, `T_peak`, `session_minutes / 60`, `k_p`, and `avg(T_in − T_out)`. Forces `grade = "low"`.

---

## EWMA Update (_update_thermal_model_cache)

Called by `record_thermal_observation()` on every successful commit. Applies one observation to the in-memory `thermal_model_cache` dict.

**Alpha lookup by confidence grade:**

| Grade | Alpha |
|---|---|
| `"high"` | 0.30 |
| `"medium"` | 0.15 |
| `"low"` | 0.05 |
| (unknown) | 0.05 |

**Confidence grade thresholds — `confidence_k_passive`** (counts from `observation_count_passive` + `observation_count_heat` + `observation_count_cool`):

| Observation count | Grade |
|---|---|
| < 5 | `"none"` |
| 5 – 14 | `"low"` |
| 15 – 29 | `"medium"` |
| ≥ 30 | `"high"` |

**Confidence grade thresholds — `confidence` (HVAC)** (counts from `observation_count_heat` + `observation_count_cool`):

| Observation count | Grade |
|---|---|
| < 5 | `"none"` |
| 5 – 9 | `"low"` |
| 10 – 19 | `"medium"` |
| ≥ 20 | `"high"` |

**EWMA formula for all continuous parameters:**
```
new_value = (1 − alpha) × old_value + alpha × observed_value
```
First observation initialises the cache field directly (no EWMA).

**Parameter routing by `obs["hvac_mode"]` (the tag written at commit time):**

| `hvac_mode` tag | Updates | Guard |
|---|---|---|
| `"heat"` | `k_passive` (EWMA if `k_p` not None), `avg_r_squared_passive`, `k_active_heat` (EWMA if `k_a` not None), `observation_count_heat`, `swing_heat_f` (if `swing_f` present) | `_envelope_modes = True` — k_passive EWMA runs |
| `"cool"` | `k_passive`, `avg_r_squared_passive`, `k_active_cool`, `observation_count_cool`, `swing_cool_f` | `_envelope_modes = True` |
| `"passive"` | `k_passive`, `avg_r_squared_passive`, `observation_count_passive` | `_envelope_modes = True` |
| `"fan_only"` | `k_vent` (EWMA of `obs["k_passive"]`), `observation_count_fan_only` | `_envelope_modes = False` — k_passive EWMA does NOT run |
| `"ventilated"` | `k_vent_window` (EWMA of `obs["k_passive"]`), `k_solar` (EWMA of `obs["k_solar"]` when `two_param=True`), `observation_count_vent` | `_envelope_modes = False` |
| `"solar"` | `k_solar` (EWMA of `obs["k_solar"]`), `observation_count_solar` | `_envelope_modes = False` |

**Swing update:** Applied for `"heat"` and `"cool"` modes only. Both `swing_heat_f` / `swing_cool_f` and their counters (`observation_count_swing_heat` / `observation_count_swing_cool`) are updated with the same alpha as the primary parameters.

---

## Rolling Window Constraints

| Constant | Value | Effect |
|---|---|---|
| `THERMAL_ROLLING_MIN_WINDOW_MINUTES` | 30 min | No commit attempt before this elapsed time; observation keeps accumulating regardless of signal |
| `THERMAL_ROLLING_MAX_WINDOW_MINUTES` | 240 min (4h) | Hard cap: forces commit if `len(samples) >= 5`, else abandons unconditionally; `skip_delta_guard=True` |
| `THERMAL_ROLLING_MIN_DELTA_T_F` | 0.2°F | Minimum indoor temperature range required to commit at min-window point (passive_decay, solar_gain); skipped for fan_only_decay and ventilated_decay (`skip_delta_guard=True`) |
| `THERMAL_MIN_DECAY_SAMPLES` | 4 | OLS pair floor for rolling-window commits; `_commit_rolling_window_obs` requires `len(samples) >= 5` (= 4 + 1) to guarantee 4 pairs |

**Early commit condition:** `elapsed >= THERMAL_ROLLING_MIN_WINDOW_MINUTES` AND `signal_sufficient=True`. Signal is type-specific:
- `passive_decay`: `max(indoor_temps) − min(indoor_temps) >= THERMAL_ROLLING_MIN_DELTA_T_F`
- `fan_only_decay`: same range check, but `skip_delta_guard=True` so this check is bypassed in `_commit_rolling_window_obs`
- `ventilated_decay`: indoor range check, additionally suppressed when solar keep-alive guard applies (daytime AND `sf_range < 0.30`)
- `solar_gain`: indoor range check

**Solar keep-alive guard:** Active during hours 08:00–17:59 when `sf_range < THERMAL_SOLAR_FACTOR_MIN_RANGE (0.30)`. Forces `_vent_signal_sufficient = False` for `ventilated_decay`, deferring early commit until `sf_range` meets threshold or the 240-min hard cap fires.

---

## Gate Bridge

**Activation condition (in `_build_predicted_indoor_future`):**
```python
if (_k_passive is None or _conf_k_passive == "none") and _k_vent_window is not None and _k_vent_window <= 0:
    _k_passive = _k_vent_window
    _k_passive_via_bridge = True
```

The bridge also fires when `_k_passive is not None` but `_conf_k_passive == "none"` (Bug A fix from Issue #126): the passive estimate exists but confidence is too low without the bridge.

**Proxy semantics:** `k_vent_window` is an overestimate of `k_passive` because it includes ventilation effect. Force grade `"low"` is used for any HVAC observation that uses the proxy. In `_commit_event_from_dict`, committed obs writes `k_passive=None` (D21) so the proxy never enters the envelope EWMA.

**`physics_eligible` flag (exact code, coordinator.py ~L4371):**
```python
_physics_eligible = (
    (
        _conf != "none"
        or (_conf_k_passive is not None and _conf_k_passive not in (None, "none"))
        or _k_passive_via_bridge  # bridge-provided k needs no confidence count
    )
    and _k_passive is not None
    and (_k_passive < 0 or _k_passive_via_bridge)
)
```

When `_k_passive_via_bridge=True`, the confidence count requirement is bypassed — physics activates even with `conf="none"` and `conf_k_passive="none"`.

**ODE path when bridge is active:** `_k_passive_via_bridge=True` sets a secondary guard (`_bridge_guard_applies`) that disables per-hour `k_vent_window` substitution on window-open hours when a window schedule exists. This prevents double-application: the bridge already uses `k_vent_window` as the base; substituting it again for open-window hours would overcorrect.

**Bridge guard condition:**
```python
_bridge_guard_applies = (
    _k_passive_via_bridge
    and _windows_recommended          # classification has a window schedule
    and not _hour_windows_open        # current hour is outside the open window
)
```
When the guard applies, ramp interpolation is used for that hour. When windows are NOT recommended (no schedule), the bridge runs for all hours without guard interference.

---

## Swing Detection

**Formula:** `swing_f = abs(T_end − T_start) / 2`

**T_start:** `event["start_indoor_f"]` — indoor temperature at HVAC-on event.

**T_end definitions by mode:**
- `hvac_heat`: `active_samples[-1]["indoor_temp_f"]` — temperature at HVAC shutoff (last active sample), NOT the global peak. Using the global peak would include post-heat overshoot and bias swing high.
- `hvac_cool`: `min(s["indoor_temp_f"] for s in active_samples)` — trough temperature during active cooling.

**Minimum signal gate:** `abs(T_end − T_start) >= THERMAL_HVAC_MIN_SIGNAL_F (0.5°F)`. If the delta is below this threshold, no swing value is written.

**Valid range:** `[THERMAL_SWING_MIN_F (0.1°F), THERMAL_SWING_MAX_F (5.0°F)]`. Values outside this range are discarded.

**Storage:** `swing_heat_f` and `swing_cool_f` fields in `thermal_model_cache`. Both are EWMA-accumulated independently using the same alpha as k_active for that observation.

**Default for display:** `THERMAL_SWING_DEFAULT_F = 1.5°F` — used when the observed value is None (`swing_heat_f_display` / `swing_cool_f_display` in `get_thermal_model()` output).

**Confidence tiers:**

| `observation_count_swing_heat` or `_cool` | Grade |
|---|---|
| 0 (`< THERMAL_SWING_CONF_LOW = 1`) | `"none"` |
| 1–2 (`< THERMAL_SWING_CONF_MEDIUM = 3`) | `"low"` |
| 3–9 (`< THERMAL_SWING_CONF_HIGH = 10`) | `"medium"` |
| 10+ | `"high"` |

---

## Invariants

The following conditions must always hold after a successful commit and EWMA update:

1. **k_passive sign:** Every value written to `cache["k_passive"]` via `_update_thermal_model_cache` is negative (`k_p < 0`). The `REJECT_OLS_WRONG_SIGN` check in `compute_k_passive` enforces this before any value reaches the cache. The only exception is the bridge proxy path in `_commit_event_from_dict` — but in that path `k_passive=None` is written to the obs dict (D21), so the cache is never updated with the proxy value.

2. **k_vent_window sign:** `k_vent_window` in cache is always ≤ 0 when valid. The bridge activation check (`_k_vent_window <= 0`) enforces this. A value of exactly 0.0 is physically valid (perfectly inert home with zero ventilation effect) and produces a flat ODE prediction.

3. **k_solar sign:** `k_solar` is always non-negative. The bounds check `0.0 <= k_solar <= THERMAL_K_SOLAR_MAX_F_PER_HR` in `_commit_event_from_dict` and the mean-rate sign check (`if mean_rate < 0: reject`) for solar_gain observations enforce this.

4. **Separation of envelope and ventilation:** The guard `_envelope_modes = mode not in ("fan_only", "ventilated")` in `_update_thermal_model_cache` ensures that `fan_only` and `ventilated` observations never write to `cache["k_passive"]`. Only `"heat"`, `"cool"`, and `"passive"` modes update the envelope decay estimate.

5. **Rejection code exclusivity:** `compute_k_passive` returns exactly one of `(k_passive, rejection_code)` as non-None. The function never returns both `k_passive` and a `rejection_code` as non-None simultaneously.

6. **Obs cap:** `thermal_observations` list in `LearningState` never exceeds `THERMAL_OBS_CAP (200)` entries. The 90-day rolling trim runs first; the hard cap enforces the absolute maximum.

7. **Rejection log cap:** Each obs_type bucket in `rejection_log` is capped at 100 entries. Both `_abandon_observation()` in coordinator and `load_state()` enforce this cap.

8. **Bridge does not contaminate k_passive EWMA:** When `_k_p_from_proxy=True` in `_commit_event_from_dict`, `obs["k_passive"] = None` is set before calling `record_thermal_observation()`. This ensures `_update_thermal_model_cache` sees `k_p = None` and skips the `k_passive` EWMA update.

9. **ODE k_passive must be negative for exponential decay:** `_simulate_indoor_physics` and `_simulate_indoor_physics_v3` use `exp(k_passive * dt_hours)`. With `k_passive < 0` this decays toward `t_outdoor`; with `k_passive = 0` the division-by-zero branch uses linear extrapolation (`t_start + q * dt_hours`). The bridge allows `k_vent_window = 0.0` exactly, which routes to this linear branch — correct for a perfectly inert home.
