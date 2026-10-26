"""K10 key figure with reference: label, „x von y“, bar (docs/08-ux-konzept.md K10, G1, §6.5).

The value is always text; the bar only repeats it. From 80 % of a limit the bar turns orange,
above the limit red (word „überschritten“ added, never colour alone, §7.7).
"""

from arr.backends.base import CostKind
from arr.service.api_models import RunLimits
from arr.service.gui import format as fmt
from arr.service.gui.components.base import esc

WARN_RATIO = 0.8


def meter_html(
    label: str, value: str, ratio: float | None, *, css: str = "", title: str = ""
) -> str:
    """One key figure; `ratio` is used/limit (None without a limit: no bar)."""
    state = ""
    if ratio is not None and ratio > 1:
        state = " over"
        value += " · überschritten"
    elif ratio is not None and ratio >= WARN_RATIO:
        state = " warn"
    bar = ""
    if ratio is not None:
        width = max(0.0, min(1.0, ratio)) * 100
        bar = f'<span class="bar" aria-hidden="true"><i style="width:{width:.0f}%"></i></span>'
    tip = f' title="{esc(title)}"' if title else ""
    return (
        f'<div class="meter{state} {esc(css)}"{tip}><span class="lbl"><span>{esc(label)}</span>'
        f"<b>{esc(value)}</b></span>{bar}</div>"
    )


def run_meters_html(limits: RunLimits, cost_kind: CostKind | None) -> str:
    """Time, cost (≈ for subscriptions) and turns of a run, each with its limit (§6.5 row 2)."""
    elapsed = fmt.duration_hm(limits.elapsed_s)
    time_ratio = limits.elapsed_s / limits.wall_time_s if limits.wall_time_s else None
    time_text = f"{elapsed} von {fmt.limit_hours(limits.wall_time_s)}"
    if limits.wall_time_s is None:
        time_text = elapsed
    money = fmt.money(limits.spent_usd, cost_kind)
    cost_ratio = None
    cost_text = money
    if limits.budget_usd:
        cost_text = f"{money} von {fmt.budget(limits.budget_usd)}"
        cost_ratio = (limits.spent_usd or 0.0) / limits.budget_usd
    cost_title = (
        "Rechnerische Kosten (Abo): zählen gegen das Budget des Runs"
        if cost_kind is CostKind.ESTIMATED
        else ""
    )
    parts = [
        meter_html("Zeit", time_text, time_ratio),
        meter_html("Kosten", cost_text, cost_ratio, title=cost_title),
    ]
    if limits.turns_per_agent:
        parts.append(
            meter_html(
                "Turns",
                f"höchstens {limits.max_turns_used} von {limits.turns_per_agent} je Agent",
                limits.max_turns_used / limits.turns_per_agent,
                css="m-turns",
            )
        )
    return "".join(parts)
