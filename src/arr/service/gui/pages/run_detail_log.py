"""Run detail, view „Ablauf“: the entries of `orchestrator.jsonl` as a list (docs/08 §6.5 [S]).

Stages, assignments, wake-ups, rating decisions, interventions and errors in order; the first
error is marked and a failed run opens there (§6.7). Filters and the full log come with W5-04.
"""

from typing import TYPE_CHECKING

from arr.service.api_models import LogEntry
from arr.service.gui import format as fmt
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import esc, raw

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


class LogView:
    """The view „Ablauf“; drawn again when the run moves on."""

    def __init__(self, page: "RunDetailPage") -> None:
        self.page = page
        self.body = None
        self.html = ""

    async def build(self) -> None:
        page = self.page
        entries = await page.api.get_log(page.run_id)
        self.html = log_html(entries)
        open_at = ERROR_ROW if f'id="{ERROR_ROW}"' in self.html else ""
        self.body = raw(self.html, classes="rd-scroll pad").props(
            f'data-follow="0" data-open-at="{open_at}" aria-label="Ablauf" tabindex="0"'
        )

    async def refresh(self) -> None:
        if self.body is None:
            return
        html = log_html(await self.page.api.get_log(self.page.run_id))
        if html != self.html:
            self.html = html
            self.body.set_content(html)
