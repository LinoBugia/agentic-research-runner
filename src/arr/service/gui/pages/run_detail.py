"""Run detail – placeholder until W1-07 (docs/08 §5.5, §6).

Keeps the deep link `/runs/<run-id>` stable: status, name (focus target, §4.3), ID, project,
skill, starter and a note that the control centre follows in W1-07.
"""

from nicegui import ui

from arr.service.api_models import ApiException
from arr.service.gui import format as fmt
from arr.service.gui.app import get_api
from arr.service.gui.components.badge import badge_html
from arr.service.gui.components.banner import banner, empty_state
from arr.service.gui.components.base import esc, raw
from arr.service.gui.components.copy_field import copy_html
from arr.service.gui.layout import Frame
from arr.service.gui.texts import STARTER_PHRASE, run_status


def register() -> None:
    """Register `/runs/{run_id}`."""

    @ui.page("/runs/{run_id}")
    async def run_detail(run_id: str) -> None:
        api = get_api()
        frame = Frame(
            api,
            active="runs",
            title="Run",
            crumbs=[("Runs", "/runs"), ("Run", None)],
            focus="#run-title",
        )
        await frame.start()
        with frame.content:
            try:
                detail = await api.get_run(run_id)
            except ApiException as exc:
                raw('<h1 id="run-title" tabindex="-1">Run nicht gefunden</h1>')
                banner(
                    "err",
                    exc.error.message,
                    " Vielleicht wurde er gelöscht.",
                    [("Zu den Runs", "/runs")],
                )
                return
            run = detail.run
            ui.page_title(f"{run.name} – arr")
            starter = STARTER_PHRASE[run.starter]
            since = fmt.when(run.started_at or run.created_at, api.now())
            raw(
                '<div class="page-head">'
                f"{badge_html(run_status(run), large=True)}"
                f'<h1 id="run-title" tabindex="-1">{esc(run.name)}</h1>'
                f"{copy_html(run.id, label='Run-ID kopieren')}"
                f'<span class="small muted">{esc(run.project)} · {esc(run.skill or "frei")} · '
                f"seit {esc(since)}, {esc(starter)}</span></div>"
            )
            if detail.open_approvals_top:
                top = detail.open_approvals_top
                more = f" · und {top.more} weitere" if top.more else ""
                banner(
                    "warn",
                    "Wartet auf dich",
                    f" {top.summary}{more}",
                    [("Ansehen", f"/freigaben/{top.id}")],
                )
            ui.element("div").style("height:16px")
            empty_state(
                "dashboard",
                "Das Run-Detail kommt im nächsten Schritt",
                "Kanäle, Agenten, Verlauf und Ergebnisse dieses Runs zeigt der Leitstand "
                "(Auftrag W1-07).",
                [("Zurück zu den Runs", "/runs", False)],
            )
