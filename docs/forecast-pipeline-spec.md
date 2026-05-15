<!-- Nav: → [Project Instructions](00-PROJECT-INSTRUCTIONS.md) | → [Architecture Reference](02-ARCHITECTURE-REFERENCE.md) -->

# Forecast Pipeline Spec

## Scope

Covers `_get_forecast()` in `coordinator.py`: how raw HA weather forecast data is fetched,
parsed, timezone-normalized, and returned as `(today_high, today_low, tomorrow_high, tomorrow_low)`
to the classifier and briefing.

## Anchors

| Question | Location |
|---|---|
| What datetime format does the HA weather API return? | [§ Datetime Format](#datetime-format) |
| How are today and tomorrow entries identified? | [§ Date-Keyed Matching](#date-keyed-matching) |
| What happens when today's forecast is missing from the API? | [§ Missing Today Handling](#missing-today-handling) |
| How is weather bias correction applied? | [§ Bias Correction](#bias-correction) |
| What timezone is used for date comparisons? | [§ Timezone Strategy](#timezone-strategy) |
| What is the fix history for this function? | [§ Known History](#known-history) |

## Datetime Format

HA weather integrations vary in how they encode daily forecast datetimes:

- **UTC midnight**: `2026-05-16T00:00:00+00:00` — midnight UTC is 17:00 the *previous* local
  day (PDT = UTC-7). `dt_util.as_local()` shifts the date back by one calendar day.
- **Local noon or other offsets**: no shift problem; the date component is already correct.
- **Naive datetimes** (no `tzinfo`): treated as local time; `.date()` is used directly.

The integration does not control which format the configured weather provider uses. The
date-keyed dict approach (see below) handles all variants correctly.

## Date-Keyed Matching

`_get_forecast()` builds a `forecast_by_date: dict` using `setdefault()` — the first entry
for each UTC date wins — then looks up today and tomorrow by UTC calendar date:

```python
today_fc = forecast_by_date.get(now_date)
tomorrow_fc = forecast_by_date.get(tomorrow_date)
```

`now_date = dt_util.utcnow().date()` (UTC calendar date).
`tomorrow_date = now_date + timedelta(days=1)`.
Each entry's key: `fc_obj.astimezone(UTC).date()`.

If either lookup returns `None`, the corresponding temperature defaults to `current_outdoor`
(the value obtained from the live thermostat reading). The existing `if today_fc:` / `if
tomorrow_fc:` guards below the loop handle this gracefully.

No blind-index fallback exists. Array position carries no semantic meaning.

## Missing Today Handling

When `today_fc is None` after the dict build, a WARNING log records the available UTC dates:

```
_get_forecast: no entry for today (2026-05-15 UTC); available dates: [2026-05-16, 2026-05-17, ...]
```

This is normal for weather providers that exclude the current day from the daily forecast
once it is in progress. `today_high` / `today_low` fall back to `current_outdoor`, but
the `_outdoor_temp_history` override below can correct them once observed temperatures
accumulate during the day.

An INFO log also records the raw matched temperatures each cycle:

```
_get_forecast matched: today=2026-05-15 raw_temp=72, tomorrow=2026-05-16 raw_temp=79
```

A raw datetime snapshot (first 5 entries) is logged at DEBUG:

```
_get_forecast raw datetimes (first 5): ['2026-05-16T00:00:00+00:00', ...]
```

These three log lines are the primary diagnostic for any future date-matching problems.

## Bias Correction

After the forecast entries are extracted, weather bias (learned from comparing recent
forecast-vs-actual data) is applied to `tomorrow_high` and `tomorrow_low`:

- Applied only when `learning_enabled` and `weather_bias_enabled` are both `True`
- Bias is capped at `MAX_WEATHER_BIAS_APPLY_F` (8°F)
- Only applied when `|bias| >= MIN_WEATHER_BIAS_APPLY_F` (0.5°F)
- A positive `high_bias` means the forecast has been running high; the correction subtracts it

Bias is applied to **tomorrow** only, not today. Today's forecast is corrected by the
observed temperature history guard (`_outdoor_temp_history` max/min override).

## Timezone Strategy

All date comparisons use **UTC calendar date**, not local date:

- `dt_util.utcnow().date()` — UTC calendar today
- `fc_obj.astimezone(UTC).date()` — forecast entry converted to UTC date before comparison
- Local timezone is never used for day-boundary decisions in the forecast dict

**Why UTC, not local?** HA weather integrations frequently timestamp daily forecast entries
at UTC midnight (e.g., `2026-05-16T00:00:00+00:00`). In a UTC-7 timezone (PDT), converting
that to local time gives `2026-05-15T17:00-07:00` — local date 2026-05-15 (yesterday). The
entry the API intends as "tomorrow" (2026-05-16) would be bucketed as "today," causing a
one-day off-by-one across the entire forecast. UTC date matching avoids this entirely: the
UTC midnight entry for 2026-05-16 has UTC date 2026-05-16 and correctly maps to tomorrow.

This applies regardless of the user's local timezone. An API entry for calendar day N
will always have a UTC timestamp that falls on UTC date N (midnight, noon, or any intra-day
time), making UTC the stable matching key.

This strategy was confirmed as correct by production diagnosis in v0.3.44 — the preceding
`dt_util.as_local()` approach (Fix #107, v0.3.22) introduced this shift for UTC midnight APIs.

## Known History

| Version | Change |
|---|---|
| v0.3.22 (Fix #107) | Changed forecast key from `'time'` to `'datetime'`; added `dt_util.as_local()` for timezone-aware parsing. Blind-index fallback retained. |
| v0.3.44 (Fix #143) | Replaced loop + fallback block with UTC-date-keyed dict. Removed all blind index assumptions. Switched from `dt_util.as_local()` to `astimezone(UTC)` for entry date extraction — fixes one-day off-by-one for UTC midnight forecast timestamps. Added WARNING logging for missing dates and INFO for matched raw temps. |

### Why the fallback was wrong (Fix #143)

Before v0.3.44, the fallback block was:

```python
if today_fc is None and tomorrow_fc is None and len(forecast) >= 2:
    today_fc = forecast[0]
    tomorrow_fc = forecast[1]
elif today_fc is None and tomorrow_fc is not None and len(forecast) >= 1:
    today_fc = forecast[0]   # BUG: forecast[0] may be the same entry as tomorrow_fc
```

When the weather API returned a forecast array starting from tomorrow (no today entry), the
primary loop correctly set `tomorrow_fc = forecast[0]`. The second `elif` branch then blindly
set `today_fc = forecast[0]` — the identical object. Both `today_fc` and `tomorrow_fc` pointed
to the same entry (e.g., 2026-05-16 at 80°F). The real tomorrow's data (forecast[1], e.g.,
72°F) was unreachable. The briefing faithfully reported the corrupted value as "tomorrow's high."

The code comment at the top of the variable declarations already said *"never assume index ==
day"* — the fallback contradicted its own comment.
