# Climate Advisor for Home Assistant

An intelligent HVAC management integration that uses weather forecasts, occupancy, and door/window sensors to minimize energy waste while keeping your home comfortable — and learns from your household's behavior over time.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                   Climate Advisor                       │
│                                                         │
│  ┌─────────────┐   ┌──────────────┐   ┌─────────────┐   │
│  │  Classifier │─▶│  Coordinator │◀──│  Learning   │   │
│  │             │   │   (brain)    │   │  Engine     │   │
│  │ • Day type  │   │              │   │             │   │
│  │ • Trend     │   │ • Scheduling │   │ • Tracking  │   │
│  │ • Forecast  │   │ • Briefings  │   │ • Patterns  │   │
│  │   analysis  │   │ • Events     │   │ • Suggest   │   │
│  └─────────────┘   └──────┬───────┘   └─────────────┘   │
│                           │                             │
│              ┌────────────┼────────────┐                │
│              ▼            ▼            ▼                │
│  ┌──────────────┐ ┌────────────┐ ┌──────────┐           │
│  │  Automation  │ │  Briefing  │ │ Sensors  │           │
│  │  Engine      │ │ Generator  │ │ (6x)     │           │
│  │              │ │            │ │          │           │
│  │ • HVAC ctrl  │ │ • Daily    │ │ • Day    │           │
│  │ • Door/win   │ │   email    │ │   type   │           │
│  │ • Occupancy  │ │ • Actions  │ │ • Trend  │           │
│  │ • Pre-heat/  │ │ • Learning │ │ • Next   │           │
│  │   cool       │ │   tips     │ │   action │           │
│  └──────────────┘ └────────────┘ └──────────┘           │
└─────────────────────────────────────────────────────────┘
         │                │               │
         ▼                ▼               ▼
   HA Climate       HA Notify        HA Dashboard
   Entity           Service          Lovelace Cards
```

## How It Works

### Daily Cycle

1. **6:00 AM** — Coordinator pulls forecast, classifies the day, and sends the daily briefing email/notification
2. **6:30 AM** — Morning warm-up restores comfort setpoint
3. **Throughout the day** — Automation engine responds to doors, windows, occupancy, and temperature changes
4. **10:30 PM** — Bedtime setback kicks in
5. **11:59 PM** — Day's data is saved to the learning engine

### Day Types

| Type | Today's High | HVAC Strategy | Human Actions |
|------|-------------|---------------|---------------|
| Hot  | 85°F+       | AC pre-cool, maintain all day | Keep sealed, close blinds |
| Warm | 75–84°F     | Off, AC standby | Open windows morning, close evening |
| Mild | 60–74°F     | Off (heat in AM) | Open windows mid-morning |
| Cool | 45–59°F     | Heat with midday break | Keep closed |
| Cold | Below 45°F  | Heat all day, pre-heat | Keep sealed, help insulate |

### Trend Modifiers

The forecast trend (tomorrow vs. today) adjusts behavior:

- **Warming 10°F+**: More aggressive overnight setback (tomorrow's warmth will help)
- **Warming 5-10°F**: Moderate setback increase
- **Cooling 5-10°F**: Pre-heat in evening, gentler setback
- **Cooling 10°F+**: Significant pre-heat, conservative setback, bank thermal energy

### Learning Engine

After 14+ days of data, the learning engine starts analyzing patterns:

- **Window compliance**: If you rarely open windows when recommended, it offers to switch to HVAC-only strategies
- **Manual overrides**: Frequent thermostat adjustments suggest setpoints don't match preferences
- **Runtime anomalies**: High HVAC runtime on mild days may indicate sensor gaps
- **Short departures**: Adapts setback timing if you frequently leave for 30-45 minutes
- **Comfort violations**: Suggests less aggressive setbacks if the house is uncomfortable too often
- **Door pauses**: Identifies problem doors and offers to adjust monitoring

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots → Custom repositories
3. Add `https://github.com/yourgithubuser/ha-climate-advisor` as an Integration
4. Search for "Climate Advisor" and install
5. Restart Home Assistant
6. Go to Settings → Integrations → Add Integration → Climate Advisor

### Manual

1. Copy the `custom_components/climate_advisor` folder to your HA `config/custom_components/` directory
2. Restart Home Assistant
3. Go to Settings → Integrations → Add Integration → Climate Advisor

## Configuration

The setup wizard walks you through three steps:

### Step 1: Core Entities
- **Weather Entity**: Your forecast provider (e.g., `weather.home`)
- **Climate Entity**: Your thermostat (e.g., `climate.living_room`)
- **Temperature Sensors**: Optional dedicated indoor/outdoor sensors
- **Setpoints**: Your preferred comfort and setback temperatures
- **Notification Service**: Where to send briefings (e.g., `notify.mobile_app_phone`)

### Step 2: Door/Window Sensors
Select any contact sensors you want monitored. HVAC pauses when these are open.

### Step 3: Schedule
Set your wake time, bedtime, and when you want the daily briefing.

## Sensors Created

| Sensor | Description |
|--------|-------------|
| `sensor.climate_advisor_day_type` | Today's classification (hot/warm/mild/cool/cold) |
| `sensor.climate_advisor_trend` | Temperature trend direction and magnitude |
| `sensor.climate_advisor_next_action` | Next recommended human action |
| `sensor.climate_advisor_daily_briefing` | Today's briefing text (full text in attributes) |
| `sensor.climate_advisor_comfort_score` | Comfort compliance percentage |
| `sensor.climate_advisor_status` | Integration status |

## Services

### `climate_advisor.respond_to_suggestion`

Accept or dismiss a learning suggestion.

```yaml
service: climate_advisor.respond_to_suggestion
data:
  action: accept  # or "dismiss"
  suggestion_key: low_window_compliance
```

## Dashboard Example

```yaml
type: entities
title: Climate Advisor
entities:
  - entity: sensor.climate_advisor_day_type
    name: Today's Plan
  - entity: sensor.climate_advisor_trend
    name: Trend
  - entity: sensor.climate_advisor_next_action
    name: Your Next Action
  - entity: sensor.climate_advisor_comfort_score
    name: Comfort Score
  - entity: sensor.climate_advisor_status
    name: System Status
```

## Development Roadmap

See [Issue #11](https://github.com/gunkl/SmartHVAC/issues/11) for full tracking.

### Phase 1: Core (v0.1.0) — Complete
- [x] 5-level day type classification with trend analysis
- [x] Daily briefing as primary UI (email/notification)
- [x] Door/window pause automation with grace periods
- [x] Occupancy-based setback with configurable delay
- [x] Bedtime/morning scheduling with forecast-aware adjustments
- [x] Runaway protection (runtime alerts, daily budgets)
- [x] Learning engine foundation (90-day rolling window, 6 pattern detectors)
- [x] 3-step config flow wizard, 6 HA sensor entities, dashboard API
- [x] Flexible temperature source configuration
- [x] Separate comfort/setback temps for heat and cool modes

### Phase 2: Enhanced Learning & Adaptation (v0.2.x) — In Progress
- [x] Persist operational state across restarts (#10)
- [x] Populate DailyRecord fields (runtime, avg temp, comfort violations, window compliance)
- [x] Per-sensor pause tracking and granular daily records (#12)
- [x] Override direction/timing/magnitude analysis (#12)
- [ ] Thermal model learning (heat/cool rates, recovery time estimates)
- [ ] Optimized pre-heat/pre-cool timing based on thermal performance
- [ ] Setback depth optimization based on house characteristics
- [ ] Override-driven scheduling suggestions ("You raise temp 2°F at 3pm daily")

### Phase 3: Seasonal & Cost Intelligence (v0.3+) — Future
- [ ] Seasonal performance baselines (after 1 year of data)
- [ ] Anomaly detection (e.g., "heating 30% higher than last November")
- [ ] Energy cost integration (utility rates → estimated cost)
- [ ] Savings tracking vs. "no automation" baseline

### Phase 4: Multi-Zone & Advanced (v0.4+) — Future
- [ ] Multi-zone HVAC support (multiple thermostats)
- [ ] Room-level occupancy detection
- [ ] Humidity-based decisions
- [ ] Energy source cost optimization
- [ ] Advanced thermal model with per-zone coefficients

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test with a Home Assistant dev environment
5. Submit a pull request

## File Structure

```
custom_components/climate_advisor/
├── __init__.py          # Integration setup, service registration
├── manifest.json        # HA integration metadata
├── const.py             # Constants, thresholds, defaults
├── config_flow.py       # Setup wizard UI
├── strings.json         # UI text for config flow
├── coordinator.py       # Central brain — scheduling, events, data flow
├── classifier.py        # Day type and trend classification
├── briefing.py          # Daily briefing text generation
├── automation.py        # HVAC control logic
├── learning.py          # Pattern tracking and suggestion engine
└── sensor.py            # HA sensor entities for dashboards
```

## License

MIT
