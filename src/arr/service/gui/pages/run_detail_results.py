"""Run detail, view „Ergebnisse“ (docs/08-ux-konzept.md §6.5, R4, T7, T8, G3, G8).

Summary (`summary.md`), the block „Übernehmen“ (arr never writes into the project: „Für
Claude Code kopieren“ and „Pfad kopieren“, G8) and the results grouped by stage, each with
origin (agent, stage, attempt), status, average and pass mark, the raters' scores per
criterion and their comments, and „Im Transkript zeigen“ (G3: one click to the source).
"""

from itertools import groupby
from typing import TYPE_CHECKING

from arr.service.api_models import Result, ResultStatus, RunResults
from arr.service.gui import format as fmt
from arr.service.gui.components.badge import badge_html
from arr.service.gui.components.banner import empty_state_html
from arr.service.gui.components.base import esc, icon, markdown_html, raw
from arr.service.gui.texts import Status

if TYPE_CHECKING:
    from arr.service.gui.pages.run_detail import RunDetailPage

RESULT_STATUS = {
    ResultStatus.ACCEPTED: Status("st-ok", "check", "bestanden"),
    ResultStatus.REWORK: Status("st-err", "close", "Nacharbeit"),
    ResultStatus.FAILED: Status("st-err", "close", "nicht bestanden"),
    ResultStatus.UNRATED: Status("st-neutral", "radio_button_unchecked", "noch nicht bewertet"),
}
"""Result words of docs/08 §8.3 with symbol and colour (§7.1)."""


def result_dom_id(result_id: int) -> str:
    """DOM id of a result card (target of the overview and of `?ergebnis=`)."""
    return f"e{result_id:03d}"


def result_badge(result: Result) -> str:
    """„✓ bestanden · Ø 8,0“."""
    status = RESULT_STATUS[result.status]
    avg = f" · Ø {fmt.score(result.avg_score)}" if result.avg_score is not None else ""
    return badge_html(status, word=status.word + avg)


def result_title(result: Result) -> str:
    """„004 · Beweis, Versuch 1“."""
    return f"{result.id:03d} · {result.title}, Versuch {result.attempt}"


def _ratings_html(result: Result) -> str:
    if not result.ratings:
        return ""
    raters = [r.rater for r in result.ratings]
    criteria: list[str] = []
    for rating in result.ratings:
        criteria += [c for c in rating.criteria if c not in criteria]
    head = "".join(f'<th class="right mono">{esc(r)}</th>' for r in raters)
    rows = "".join(
        f"<tr><td>{esc(c)}</td>"
        + "".join(
            f'<td class="right num">{fmt.score(r.criteria[c]) if c in r.criteria else "–"}</td>'
            for r in result.ratings
        )
        + "</tr>"
        for c in criteria
    )
    limit = (
        f' <span class="faint small">(bestanden ab {fmt.threshold(result.pass_score)})</span>'
        if result.pass_score is not None
        else ""
    )
    total = (
        f"<tr><td><b>Ø</b>{limit}</td>"
        + "".join(f'<td class="right num"><b>{fmt.score(r.score)}</b></td>' for r in result.ratings)
        + "</tr>"
    )
    table = (
        f'<table class="tbl compact ratings"><thead><tr><th>Kriterium</th>{head}</tr></thead>'
        f"<tbody>{rows}{total}</tbody></table>"
    )
    comments = "".join(
        f'<div><b class="mono">{esc(r.rater)}</b> ({fmt.score(r.score)}): „{esc(r.comment)}“</div>'
        for r in result.ratings
        if r.comment
    )
    return table + (f'<div class="stack small comments">{comments}</div>' if comments else "")


def result_card_html(result: Result) -> str:
    """One result with origin, status, excerpt, ratings and the way to its source."""
    meta = [f'<span class="mono">{esc(result.agent)}</span>']
    if result.stage_label:
        meta.append(f"Stufe {esc(result.stage_label)}")
    meta.append(f"Versuch {result.attempt}")
    meta.append(fmt.clock(result.ts))
    body = (
        f'<div class="prose excerpt">{markdown_html(result.body_excerpt)}</div>'
        if result.body_excerpt
        else ""
    )
    actions = []
    if result.transcript_seq is not None:
        href = f"?agent={result.agent}&seq={result.transcript_seq}"
        actions.append(
            f'<a class="btn btn-sm" href="{esc(href)}" '
            f'data-sel="seq:{esc(result.agent)}:{result.transcript_seq}">'
            f"{icon('subject', small=True)}Im Transkript zeigen</a>"
        )
    else:
        actions.append(
            '<span class="btn btn-sm" aria-disabled="true" title="Für dieses Ergebnis ist keine '
            'Stelle im Transkript bekannt.">Im Transkript zeigen</span>'
        )
    if result.path:
        actions.append(
            f'<button type="button" class="btn btn-sm" data-copy="{esc(result.path)}" '
            f'aria-label="Pfad von {esc(result_title(result))} kopieren">'
            f"{icon('content_copy', small=True)}Pfad kopieren</button>"
        )
    path = f' · <span class="mono">{esc(result.path)}</span>' if result.path else ""
    return (
        f'<article class="card result" id="{result_dom_id(result.id)}" '
        f'aria-labelledby="{result_dom_id(result.id)}-h"><div class="card-head">'
        f'<h3 id="{result_dom_id(result.id)}-h" tabindex="-1">{esc(result_title(result))}</h3>'
        f"{result_badge(result)}</div>"
        f'<div class="small muted">{" · ".join(meta)}{path}</div>{body}'
        f'{_ratings_html(result)}<div class="btn-row">{"".join(actions)}</div></article>'
    )


def handoff_html(handoff: str, run_dir: str) -> str:
    """Block „Übernehmen“: arr never writes results into the project (G8, §10.1 Nr. 2)."""
    return (
        '<section class="card tight handoff" id="uebernehmen" aria-labelledby="h-uebernehmen">'
        '<h2 id="h-uebernehmen">Übernehmen</h2>'
        '<p class="small">arr schreibt nie selbst ins Projekt. Übernimm bewusst, am besten mit '
        "Claude Code:</p>"
        f'<span class="cmd wrap">{esc(handoff)}</span><div class="btn-row">'
        f'<button type="button" class="btn btn-sm primary" data-copy="{esc(handoff)}" '
        'aria-label="Text für Claude Code kopieren">⧉ Für Claude Code kopieren</button>'
        f'<button type="button" class="btn btn-sm" data-copy="{esc(run_dir)}" '
        'aria-label="Pfad des Run-Ordners kopieren">Pfad des Run-Ordners kopieren</button>'
        "</div></section>"
    )


def results_html(results: RunResults, handoff: str, run_dir: str) -> str:
    """The whole view: summary, „Übernehmen“, results by stage."""
    parts = []
    if results.summary_md:
        path = (
            f'<span class="faint mono small">{esc(results.summary_path)}</span>'
            if results.summary_path
            else ""
        )
        parts.append(
            '<section class="card summary" aria-labelledby="h-summary"><div class="card-head">'
            f'<h2 id="h-summary">Zusammenfassung</h2>{path}</div>'
            f'<div class="prose">{markdown_html(results.summary_md)}</div></section>'
        )
    parts.append(handoff_html(handoff, run_dir))
    if not results.items:
        parts.append(
            empty_state_html(
                "emoji_events",
                "Noch keine Ergebnisse",
                results.empty_text or "Ergebnisse erscheinen hier, sobald Agenten sie abgeben.",
            )
        )
        return "".join(parts)
    for label, group in groupby(results.items, key=lambda r: r.stage_label or "Ohne Stufe"):
        cards = "".join(result_card_html(r) for r in group)
        parts.append(f'<h2 class="stage-h">Stufe {esc(label)}</h2>{cards}')
    return "".join(parts)


class ResultsView:
    """The view „Ergebnisse“; re-rendered when results or ratings arrive."""

    def __init__(self, page: "RunDetailPage", *, result_id: int | None = None) -> None:
        self.page = page
        self.result_id = result_id
        self.body = None

    async def build(self) -> None:
        page = self.page
        results = await page.api.get_results(page.run_id)
        open_at = result_dom_id(self.result_id) if self.result_id else ""
        self.body = raw(
            results_html(results, page.detail.handoff_text, page.detail.run_dir),
            classes="rd-scroll pad results",
        ).props(f'data-follow="0" data-open-at="{open_at}" aria-label="Ergebnisse" tabindex="0"')

    async def refresh(self) -> None:
        """New results or ratings arrived: render the list again (no scrolling)."""
        if self.body is None:
            return
        page = self.page
        results = await page.api.get_results(page.run_id)
        self.body.set_content(results_html(results, page.detail.handoff_text, page.detail.run_dir))
