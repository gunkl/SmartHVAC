"""Daily briefing generator for Climate Advisor.

Voice rules for the conversational body:
- First person from the system: "I'll turn on the AC", not "the system will"
- Always "you" for the user, never "the homeowner"
- Cause-and-effect: explain *why* before asking the user to do something
- Short paragraphs (2-4 sentences max)
- Numerals for all temps and times
- No emoji in body text (emoji only in the structured header and learning section)
"""
from __future__ import annotations

import logging
import platform
from datetime import time

from .classifier import DayClassification
from .const import (
    DAY_TYPE_COLD,
    DAY_TYPE_COOL,
    DAY_TYPE_HOT,
    DAY_TYPE_MILD,
    DAY_TYPE_WARM,
    DEFAULT_AUTOMATION_GRACE_SECONDS,
    DEFAULT_MANUAL_GRACE_SECONDS,
    DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    FAN_MODE_DISABLED,
)

_LOGGER = logging.getLogger(__name__)

# strftime format for 12-hour without leading zero (platform-dependent)
_FMT_HOUR = "%#I:%M %p" if platform.system() == "Windows" else "%-I:%M %p"


def generate_briefing(
    classification: DayClassification,
    comfort_heat: float,
    comfort_cool: float,
    setback_heat: float,
    setback_cool: float,
    wake_time: time,
    sleep_time: time,
    learning_suggestions: list[str] | None = None,
    debounce_seconds: int = DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    manual_grace_seconds: int = DEFAULT_MANUAL_GRACE_SECONDS,
    automation_grace_seconds: int = DEFAULT_AUTOMATION_GRACE_SECONDS,
    grace_active: bool = False,
    grace_source: str | None = None,
    verbosity: str = "normal",
    fan_mode: str = FAN_MODE_DISABLED,
) -> str:
    """Generate the daily climate briefing message.

    Args:
        classification: Today's day classification and recommendations.
        comfort_heat / comfort_cool: User's comfort setpoints.
        setback_heat / setback_cool: User's setback setpoints.
        wake_time / sleep_time: User's schedule.
        learning_suggestions: Any pending suggestions from the learning system.
        debounce_seconds: How long a door/window must be open before HVAC pauses.
        manual_grace_seconds: Grace period after a manual HVAC override.
        automation_grace_seconds: Grace period after Climate Advisor resumes HVAC.
        grace_active: Whether a grace period is currently active.
        grace_source: "manual" or "automation" if a grace period is active.
        verbosity: "tldr_only" (header + table only), "normal" (header + table + trimmed
            body), or "verbose" (header + table + full original body).
        fan_mode: Fan control mode — one of the FAN_MODE_* constants.

    Returns:
        Formatted briefing string suitable for email or notification.
    """
    c = classification
    lines: list[str] = []

    _LOGGER.debug(
        "Generating briefing — day_type=%s, trend=%s, "
        "comfort_heat=%.0f°F, comfort_cool=%.0f°F, verbosity=%s",
        c.day_type,
        c.trend_direction,
        comfort_heat,
        comfort_cool,
        verbosity,
    )

    # Structured header (kept for quick scanning)
    trend_desc = _trend_description(c)
    lines.append("🏠 Your Home Climate Plan for Today")
    lines.append(f"{'=' * 40}")
    lines.append("")
    lines.append(f"Today: High {c.today_high:.0f}°F / Low {c.today_low:.0f}°F")
    lines.append(f"Tomorrow: High {c.tomorrow_high:.0f}°F / Low {c.tomorrow_low:.0f}°F")
    lines.append(f"Day Type: {c.day_type.title()} | Trend: {trend_desc}")
    lines.append("")

    # TLDR table — always included
    config = {
        "comfort_heat": comfort_heat,
        "comfort_cool": comfort_cool,
        "setback_heat": setback_heat,
        "setback_cool": setback_cool,
        "sleep_time": sleep_time,
        "wake_time": wake_time,
    }
    lines.extend(_generate_tldr_table(c, config))
    lines.append("")

    if verbosity == "tldr_only":
        briefing_text = "\n".join(lines).rstrip()
        _LOGGER.debug(
            "Briefing generated (tldr_only) — %d chars",
            len(briefing_text),
        )
        return briefing_text

    # Conversational body
    if c.day_type == DAY_TYPE_HOT:
        lines.extend(_hot_day_plan(c, comfort_cool, setback_cool, wake_time, sleep_time, fan_mode=fan_mode))
    elif c.day_type == DAY_TYPE_WARM:
        lines.extend(_warm_day_plan(c, comfort_cool, wake_time, sleep_time, fan_mode=fan_mode))
    elif c.day_type == DAY_TYPE_MILD:
        lines.extend(_mild_day_plan(c, comfort_heat, wake_time, sleep_time))
    elif c.day_type == DAY_TYPE_COOL:
        lines.extend(_cool_day_plan(c, comfort_heat, setback_heat, wake_time, sleep_time))
    elif c.day_type == DAY_TYPE_COLD:
        lines.extend(_cold_day_plan(c, comfort_heat, setback_heat, wake_time, sleep_time))

    _LOGGER.debug("Dispatched %s day plan", c.day_type)

    lines.append("")
    lines.extend(_leaving_home_section(c, setback_heat, setback_cool))
    lines.append("")
    lines.extend(_fresh_air_section(c, comfort_heat, comfort_cool, debounce_seconds))

    # Grace period status — only shown when a grace period is active at briefing time,
    # or when grace periods are configured to a non-default value worth explaining
    grace_lines = _grace_period_section(
        debounce_seconds=debounce_seconds,
        manual_grace_seconds=manual_grace_seconds,
        automation_grace_seconds=automation_grace_seconds,
        grace_active=grace_active,
        grace_source=grace_source,
    )
    if grace_lines:
        lines.append("")
        lines.extend(grace_lines)
        _LOGGER.debug("Grace section included — source=%s", grace_source)

    lines.append("")
    lines.extend(_tonight_preview(c, comfort_heat, comfort_cool, sleep_time))

    # Learning suggestions (kept structured for accept/dismiss clarity)
    if learning_suggestions:
        lines.append("")
        lines.append("💡 Suggestions Based on Recent Patterns")
        lines.append("-" * 40)
        for suggestion in learning_suggestions:
            lines.append(f"  • {suggestion}")
        lines.append("")
        lines.append("Reply ACCEPT or DISMISS to any suggestion, or ignore to keep current behavior.")

    briefing_text = "\n".join(lines)
    _LOGGER.debug(
        "Briefing generated — %d chars, %d learning suggestions",
        len(briefing_text),
        len(learning_suggestions) if learning_suggestions else 0,
    )
    if len(briefing_text) > 250:
        _LOGGER.debug(
            "Briefing exceeds 250-char sensor state limit — "
            "full text available in sensor attribute"
        )
    return briefing_text


def _generate_tldr_table(c: DayClassification, config: dict) -> list[str]:
    """Generate a pipe-delimited markdown TLDR summary table.

    Args:
        c: Today's day classification.
        config: Dict with comfort_heat, comfort_cool, setback_heat, setback_cool,
            sleep_time, wake_time keys.

    Returns:
        List of lines forming a markdown table.
    """
    comfort_heat = config["comfort_heat"]
    comfort_cool = config["comfort_cool"]
    sleep_time = config["sleep_time"]

    # --- Day Type row ---
    day_type_val = f"{c.day_type.title()} ({c.today_high:.0f}°F)"

    # --- HVAC Mode row ---
    if c.hvac_mode == "cool":
        hvac_val = f"Cool at {comfort_cool:.0f}°F"
    elif c.hvac_mode == "heat":
        hvac_val = f"Heat at {comfort_heat:.0f}°F"
    else:
        hvac_val = "Off — windows day"

    # --- Windows row ---
    if c.windows_recommended and c.window_open_time and c.window_close_time:
        open_t = c.window_open_time.strftime(_FMT_HOUR)
        close_t = c.window_close_time.strftime(_FMT_HOUR)
        windows_val = f"Open {open_t} – {close_t}"
    elif c.window_opportunity_morning and c.window_opportunity_evening:
        windows_val = "Open morning/evening if cool enough"
    elif c.window_opportunity_morning:
        windows_val = "Open morning if cool enough"
    elif c.window_opportunity_evening:
        windows_val = "Open evening if cool enough"
    else:
        windows_val = "Closed all day"

    # --- Bedtime Setback row ---
    sleep_str = sleep_time.strftime(_FMT_HOUR)
    if c.hvac_mode == "cool":
        # setback for cool days goes up (warmer is fine when sleeping)
        bedtime_temp = comfort_cool + 3  # standard cool setback
        bedtime_val = f"{bedtime_temp:.0f}°F at {sleep_str}"
    elif c.hvac_mode == "heat":
        bedtime_temp = comfort_heat - 4  # standard heat setback
        bedtime_val = f"{bedtime_temp:.0f}°F at {sleep_str}"
    else:
        bedtime_val = "No setback"

    # --- Tomorrow row ---
    trend_desc = _trend_description(c)
    tomorrow_val = f"{trend_desc} ({c.tomorrow_high:.0f}°F)"

    col1 = 16
    col2 = 20
    header = f"| {'Setting':<{col1}} | {'Value':<{col2}} |"
    sep = f"|{'-' * (col1 + 2)}|{'-' * (col2 + 2)}|"
    rows = [
        f"| {'Day Type':<{col1}} | {day_type_val:<{col2}} |",
        f"| {'HVAC Mode':<{col1}} | {hvac_val:<{col2}} |",
        f"| {'Windows':<{col1}} | {windows_val:<{col2}} |",
        f"| {'Bedtime Setback':<{col1}} | {bedtime_val:<{col2}} |",
        f"| {'Tomorrow':<{col1}} | {tomorrow_val:<{col2}} |",
    ]
    return [header, sep] + rows


def _trend_description(c: DayClassification) -> str:
    """Human-readable trend description."""
    if c.trend_direction == "warming":
        if c.trend_magnitude >= 10:
            return f"Significantly warmer tomorrow (+{c.trend_magnitude:.0f}°F)"
        return f"Warming trend (+{c.trend_magnitude:.0f}°F)"
    elif c.trend_direction == "cooling":
        if c.trend_magnitude >= 10:
            return f"Significant cold front coming (-{c.trend_magnitude:.0f}°F)"
        return f"Cooling trend (-{c.trend_magnitude:.0f}°F)"
    return "Stable"


def _hot_day_plan(c, comfort_cool, setback_cool, wake_time, sleep_time, fan_mode: str = FAN_MODE_DISABLED) -> list[str]:
    """Conversational plan for hot days (85°F+)."""
    lines = [
        f"I pre-cooled to {comfort_cool - 2:.0f}°F this morning while outdoor air"
        f" was still cool — that banking strategy cuts energy use significantly over"
        f" the course of the day.",
        "",
        f"Today's a keep-it-sealed kind of day. Close the blinds on sun-facing"
        f" windows (especially west-facing ones after noon) and I'll handle the rest."
        f" If outdoor temps drop below {comfort_cool:.0f}°F after sunset, I'll send"
        f" a heads-up that it's safe to open up.",
    ]
    if fan_mode != FAN_MODE_DISABLED:
        lines.append("")
        lines.append(
            "When evening ventilation windows open, I'll use the fan to help"
            " pull that cool outdoor air through the house."
        )
    return lines


def _warm_day_plan(c, comfort_cool, wake_time, sleep_time, fan_mode: str = FAN_MODE_DISABLED) -> list[str]:
    """Conversational plan for warm days (75-85°F)."""
    lines = []

    if c.windows_recommended and c.window_open_time:
        open_t = c.window_open_time.strftime(_FMT_HOUR)
        lines.append(
            f"Around {open_t}, open windows on opposite sides for a cross-breeze"
            f" — natural ventilation keeps things comfortable without the AC."
        )
    else:
        lines.append("HVAC is off this morning — no action needed.")

    if fan_mode != FAN_MODE_DISABLED:
        lines.append(
            "I'll use the fan to boost cross-ventilation when windows are open."
        )

    lines.append("")

    if c.window_close_time:
        close_t = c.window_close_time.strftime(_FMT_HOUR)
        lines.append(
            f"Close up by {close_t} so I can kick on the AC if temps push above"
            f" {comfort_cool:.0f}°F — it works much better with the house sealed."
        )
    else:
        lines.append(
            f"The AC will step in above {comfort_cool:.0f}°F as a safety net if"
            f" needed, but with good airflow you probably won't need it."
        )

    return lines


def _mild_day_plan(c, comfort_heat, wake_time, sleep_time) -> list[str]:
    """Conversational plan for mild days (60-74°F)."""
    lines = [
        f"A day where the house practically takes care of itself. I warmed to"
        f" {comfort_heat:.0f}°F before sunrise — now HVAC is off and the weather"
        f" does the rest.",
    ]

    if c.windows_recommended and c.window_open_time:
        open_t = c.window_open_time.strftime(_FMT_HOUR)
        lines.append("")
        lines.append(
            f"Open south and east windows around {open_t} for a natural"
            f" cross-breeze that freshens the air and warms the house for free."
        )

    if c.window_close_time:
        close_t = c.window_close_time.strftime(_FMT_HOUR)
        lines.append("")
        lines.append(
            f"Close up by {close_t} to trap the warmth. If it dips below"
            f" {comfort_heat - 2:.0f}°F tonight, I'll bring the heater back on"
            f" automatically."
        )

    return lines


def _cool_day_plan(c, comfort_heat, setback_heat, wake_time, sleep_time) -> list[str]:
    """Conversational plan for cool days (45-59°F)."""
    return [
        f"Heater day — too cool outside for windows. I'll hold {comfort_heat:.0f}°F"
        f" through the morning, ease back a couple degrees midday to ride any solar"
        f" gain, then return to {comfort_heat:.0f}°F as the sun drops.",
        "",
        f"At bedtime I'll set back to {comfort_heat - 4:.0f}°F — most people sleep"
        f" better a little cooler.",
    ]


def _cold_day_plan(c, comfort_heat, setback_heat, wake_time, sleep_time) -> list[str]:
    """Conversational plan for cold days (below 45°F)."""
    lines = [
        "Cold day — heater runs all day. Help it out: close north-side curtains,"
        " open south-facing ones for free solar heat, and minimize time holding"
        " exterior doors open.",
    ]

    if c.pre_condition and c.trend_direction == "cooling":
        target = comfort_heat + (c.pre_condition_target or 3)
        lines.append("")
        lines.append(
            f"Tomorrow's even colder, so I'm banking extra heat this evening —"
            f" I'll bump to {target:.0f}°F around 7pm for a couple hours."
            f" If the house feels extra warm before bed, that's on purpose."
        )

    lines.append("")
    lines.append(
        f"Tonight I'm using a conservative setback — {comfort_heat - 3:.0f}°F"
        f" instead of the usual {setback_heat:.0f}°F. When it's this cold, a"
        f" deeper setback takes too long to recover from in the morning."
    )

    return lines


def _leaving_home_section(c, setback_heat, setback_cool) -> list[str]:
    """Conversational section about what happens when they leave."""
    if c.hvac_mode == "cool":
        return [
            f"If you head out, no worries. After about 15 minutes I'll let the"
            f" house drift up to {setback_cool:.0f}°F to save energy. When you're"
            f" back, I'll pull it right back down — give it 20 to 30 minutes to"
            f" feel normal again.",
        ]
    elif c.hvac_mode == "heat":
        return [
            f"If you head out, I'll drop to {setback_heat:.0f}°F after about"
            f" 15 minutes. When you get back, I'll warm things right up — should"
            f" take 20 to 30 minutes depending on how long you were gone.",
        ]
    else:
        return [
            "If you head out, nothing really changes today — the HVAC is off."
            " If it was running as a safety net, it'll set back on its own.",
        ]


def _fresh_air_section(
    c, comfort_heat: float, comfort_cool: float, debounce_seconds: int = DEFAULT_SENSOR_DEBOUNCE_SECONDS
) -> list[str]:
    """User-centric section about opening windows/doors for fresh air.

    Affirms the user's choice first, then explains impact and recovery.
    Varies by HVAC mode since the impact differs significantly.
    Uses the configured debounce duration so the timing matches actual behavior.
    """
    debounce_minutes = max(1, debounce_seconds // 60)
    debounce_desc = (
        f"{debounce_minutes} minute" if debounce_minutes == 1 else f"{debounce_minutes} minutes"
    )

    if c.hvac_mode == "cool":
        return [
            f"If you want to crack a window for some fresh air, no problem —"
            f" it's your house. I'll keep the AC running for a bit in"
            f" case it's just a quick thing, but if it stays open past {debounce_desc}"
            f" I'll shut the AC off so you're not cooling the outdoors. Once you"
            f" close up, I'll fire the AC back up right away. Just know that on a"
            f" day like today it may take a bit longer to pull back down to"
            f" {comfort_cool:.0f}°F, so if you want to minimize the impact, shorter"
            f" is better — and try to keep other windows and doors shut while"
            f" you've got one open.",
        ]
    elif c.hvac_mode == "heat":
        return [
            f"If you want to open a window for some fresh air, no problem —"
            f" go for it. I'll keep the heat running for a bit in case"
            f" you're just airing things out, but if it stays open past {debounce_desc}"
            f" I'll turn the heat off so we're not heating the neighborhood. Once"
            f" you close up, the heat kicks right back on. It'll take a little"
            f" extra energy to warm back up, so if you want to minimize the"
            f" impact, a quick burst of fresh air works great — and closing doors"
            f" to the room with the open window helps keep the rest of the house"
            f" comfortable while you do it.",
        ]
    else:
        return [
            f"If you want to open a window for some fresh air, go for it —"
            f" the HVAC is off today so there's no energy impact at all."
            f" Enjoy the breeze. If the system does need to kick on as a safety"
            f" net later and a window is still open, I'll give it {debounce_desc}"
            f" and then pause until you close up.",
        ]


def _grace_period_section(
    debounce_seconds: int = DEFAULT_SENSOR_DEBOUNCE_SECONDS,
    manual_grace_seconds: int = DEFAULT_MANUAL_GRACE_SECONDS,
    automation_grace_seconds: int = DEFAULT_AUTOMATION_GRACE_SECONDS,
    grace_active: bool = False,
    grace_source: str | None = None,
) -> list[str]:
    """Explain active grace periods and configured timings.

    Only included in the briefing when a grace period is currently active,
    so users aren't surprised that door/window sensors aren't pausing HVAC.
    Returns an empty list when there is nothing noteworthy to report.
    """
    if not grace_active or not grace_source:
        return []

    if grace_source == "manual":
        grace_minutes = max(1, manual_grace_seconds // 60)
        grace_desc = (
            f"{grace_minutes} minute" if grace_minutes == 1 else f"{grace_minutes} minutes"
        )
        return [
            f"One heads-up for this morning: you manually turned the HVAC back on"
            f" earlier, so I'm in a {grace_desc} hands-off window right now. During"
            f" that window, opening a door or window won't trigger a pause — I'm"
            f" giving you space to settle in without the system jumping in. Once the"
            f" window closes, door/window sensing goes back to normal."
        ]
    else:
        # automation grace
        grace_minutes = max(1, automation_grace_seconds // 60)
        grace_desc = (
            f"{grace_minutes} minute" if grace_minutes == 1 else f"{grace_minutes} minutes"
        )
        return [
            f"One heads-up: I just resumed the HVAC after all the doors and windows"
            f" closed, so I'm in a {grace_desc} settling period. During that time,"
            f" opening a door or window briefly won't immediately pause things again —"
            f" this prevents the system from cycling on and off if you're moving in"
            f" and out. After the settling period, normal door/window sensing resumes."
        ]


def _tonight_preview(c, comfort_heat, comfort_cool, sleep_time) -> list[str]:
    """Conversational preview of tonight and tomorrow based on trend."""
    _LOGGER.debug(
        "Tonight preview — trend=%s, magnitude=%.1f°F",
        c.trend_direction,
        c.trend_magnitude,
    )
    if c.trend_direction == "warming" and c.trend_magnitude >= 5:
        return [
            f"Looking ahead — tomorrow's warmer at {c.tomorrow_high:.0f}°F, so"
            f" I'm going to set back a bit more aggressively tonight. Less"
            f" heating needed means energy saved while you sleep.",
        ]
    elif c.trend_direction == "cooling" and c.trend_magnitude >= 5:
        return [
            f"Looking ahead — tomorrow's cooler at {c.tomorrow_high:.0f}°F, so"
            f" I'll bank some extra warmth this evening and go easy on the"
            f" overnight setback. If the house feels a touch warmer than usual"
            f" before bed, that's intentional.",
        ]
    else:
        return [
            f"Tomorrow looks pretty similar to today — {c.tomorrow_high:.0f}°F"
            f" for a high. Nothing special planned overnight.",
        ]
