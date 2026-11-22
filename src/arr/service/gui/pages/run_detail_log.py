"""Run detail, view „Ablauf“: the entries of `orchestrator.jsonl` as a list (docs/08 §6.5 [S]).

Stages, assignments, wake-ups, rating decisions, interventions and errors in order; the first
error is marked and a failed run opens there (§6.7). Filters and the full log come with W5-04.
"""

from typing import TYPE_CHECKING

from arr.service.api_models import LogEntry, PhasePlan
from arr.service.gui import format as fmt
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import esc, raw
from arr.service.gui.components.phase_table import phase_table_html

if TYPE_CHECKING:
    from arr.service.gui.pages.run_detail import RunDetailPage

ERROR_ROW = "log-fehler"
ERROR_KINDS = frozenset({"error", "stage_failed"})


def log_html(entries: list[LogEntry]) -> str:
    """The list; the first error carries the id the view opens at."""
    if not entries:
        return empty_state_html(
            "timeline",
            "Noch kein Ablauf",
            "Sobald der Run vorbereitet ist, stehen hier Stufen, Aufträge und Eingriffe.",
        )
    rows = []
    marked = False
    for entry in entries:
        error = entry.kind in ERROR_KINDS
        anchor = f' id="{ERROR_ROW}"' if error and not marked else ""
        marked = marked or error
        stage = f'<span class="faint"> · {esc(entry.stage)}</span>' if entry.stage else ""
        rows.append(
            f'<li class="log-row{" err" if error else ""}"{anchor}>'
            f'<span class="num faint">{esc(fmt.clock(entry.ts))}</span>'
            f"<span>{esc(entry.text)}{stage}</span></li>"
        )
    return f'<ol class="log-list">{"".join(rows)}</ol>'


def plan_html(plan: PhasePlan | None) -> str:
    """The phase plan above the list (W4b-07 §6.4): passes, gate result, usage, estimate."""
    if plan is None:
        return ""
    return (
        '<section class="card tight" aria-labelledby="h-phasenplan" data-mark="run-phase-plan">'
        '<h2 id="h-phasenplan">Phasenplan</h2>'
        + phase_table_html(plan, run=True, caption="Phasenplan des Runs")
        + '<p class="small muted">Usage je Phase aus den Turns der Phase; Budget und Limits '
        "gelten für den ganzen Run.</p></section>"
    )


class LogView:
    """The view „Ablauf“; drawn again when the run moves on."""

    def __init__(self, page: "RunDetailPage") -> None:
        self.page = page
        self.body = None
        self.html = ""

    async def build(self) -> None:
        page = self.page
        entries = await page.api.get_log(page.run_id)
        self.html = plan_html(page.detail.phase_plan) + log_html(entries)
        open_at = ERROR_ROW if f'id="{ERROR_ROW}"' in self.html else ""
        self.body = raw(self.html, classes="rd-scroll pad").props(
            f'data-follow="0" data-open-at="{open_at}" aria-label="Ablauf" tabindex="0"'
        )

    async def refresh(self) -> None:
        if self.body is None:
            return
        entries = await self.page.api.get_log(self.page.run_id)
        html = plan_html(self.page.detail.phase_plan) + log_html(entries)
        if html != self.html:
            self.html = html
            self.body.set_content(html)
