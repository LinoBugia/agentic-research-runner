"""Run detail, right column „Überblick“ (docs/08-ux-konzept.md §6.5, from 1440 px).

Static parts of the overview as HTML: questions to you (K15, they do not count as waiting),
open questions between agents, results in short with score bar and pass mark, agents by
state and provider. The compact approval cards (K5) are built by the page, because they are
interactive.
"""

from arr.models import Provider
from arr.service.api_models import AgentDisplayState, AgentState, Message, Result
from arr.service.gui import format as fmt
from arr.service.gui.components.base import esc
from arr.service.gui.pages.run_detail_results import RESULT_STATUS

_STATE_WORDS = {
    AgentDisplayState.WORKING: ("arbeitet", "arbeiten"),
    AgentDisplayState.WAITING_USER: ("wartet auf dich", "warten auf dich"),
    AgentDisplayState.BLOCKED: ("blockiert", "blockiert"),
    AgentDisplayState.ERROR: ("mit Fehler", "mit Fehler"),
    AgentDisplayState.IDLE: ("wartet", "warten"),
    AgentDisplayState.DONE: ("fertig", "fertig"),
    AgentDisplayState.NOT_STARTED: ("nicht gestartet", "nicht gestartet"),
}
_PROVIDER = {Provider.CLAUDE: "Claude", Provider.CODEX: "ChatGPT"}


def _excerpt(text: str, limit: int = 70) -> str:
    line = " ".join(text.replace("$", "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _section(title: str, count: int | None, inner: str, *, ident: str = "") -> str:
    n = f" · {count}" if count is not None else ""
    ident_attr = f' id="{ident}"' if ident else ""
    return f'<div class="side-h"{ident_attr}>{esc(title)}{n}</div>{inner}'


def _msg_link(msg: Message, head: str, *, since: bool = True) -> str:
    target = f"msg:{msg.channel}:{msg.id}"
    tail = f" · seit {fmt.clock(msg.ts)}" if since else ""
    return (
        f'<a class="res-item" href="?kanal={esc(msg.channel)}&nachricht={msg.id}" '
        f'data-sel="{esc(target)}"><span class="tt">{head}</span>'
        f'<span class="mm">„{esc(_excerpt(msg.body))}“{tail}</span></a>'
    )


def questions_to_user_html(questions: list[Message], count: int) -> str:
    """„Fragen an dich · n“: sender → du · #channel, excerpt (K15)."""
    if not count:
        return ""
    items = "".join(
        _msg_link(
            m,
            f'<span class="mono">{esc(m.sender)}</span> → du · <span class="mono">#'
            f"{esc(m.channel)}</span>",
            since=False,
        )
        for m in questions
    )
    note = '<p class="small faint">Zählen nicht als „wartet auf dich“. Antworte im Kanal.</p>'
    return _section("Fragen an dich", count, items + note, ident="ov-fragen")


def open_questions_html(questions: list[Message], count: int) -> str:
    """„Offene Fragen · n“: sender → addressee, excerpt, since when."""
    if not count:
        return _section("Offene Fragen", 0, '<p class="small muted">Keine offenen Fragen.</p>')
    items = "".join(
        _msg_link(
            m,
            f'<span class="mono">{esc(m.sender)}</span> → <span class="mono">'
            f"{esc(', '.join(m.to))}</span>",
        )
        for m in questions
    )
    rest = count - len(questions)
    more = f'<p class="small muted">und {rest} weitere in den Kanälen</p>' if rest > 0 else ""
    return _section("Offene Fragen", count, items + more)


def results_short_html(results: list[Result], empty_text: str | None) -> str:
    """„Ergebnisse · n“ with score bar and pass mark per result (newest stage last)."""
    if not results:
        text = empty_text or "Noch keine Ergebnisse."
        return _section("Ergebnisse", 0, f'<p class="small muted">{esc(text)}</p>')
    rows = []
    for r in results[-6:]:
        status = RESULT_STATUS[r.status]
        bar = ""
        if r.avg_score is not None:
            width = max(0.0, min(1.0, r.avg_score / 10)) * 100
            mark = ""
            if r.pass_score is not None:
                mark = f'<span class="pass" style="left:{r.pass_score * 10:.0f}%"></span>'
            bar = (
                f'<span class="scorebar" aria-hidden="true"><i style="width:{width:.0f}%"></i>'
                f"{mark}</span>"
            )
        score = fmt.score(r.avg_score) if r.avg_score is not None else "–"
        rows.append(
            f'<a class="res-item" href="?ansicht=ergebnisse&ergebnis={r.id}" '
            f'data-sel="ergebnis:{r.id}"><span class="tt">{esc(r.stage_label or r.title)} '
            f'V{r.attempt}</span><span class="mm"><span class="mono">{esc(r.agent)}</span>'
            f'<span class="score">{score}</span><span class="{status.css} st-word">'
            f"{esc(status.word)}</span></span>{bar}</a>"
        )
    more = ""
    if len(results) > 6:
        more = f'<p class="small muted">und {len(results) - 6} ältere unter „Ergebnisse“</p>'
    limit = next((r.pass_score for r in results if r.pass_score is not None), None)
    legend = (
        f'<p class="small faint">Strich = bestanden ab {fmt.threshold(limit)}.</p>'
        if limit is not None
        else ""
    )
    return _section("Ergebnisse", len(results), "".join(rows) + more + legend)


def agents_html(agents: list[AgentState]) -> str:
    """„Agenten · n“: counts by state and by provider."""
    counts: dict[AgentDisplayState, int] = {}
    providers: dict[Provider, int] = {}
    for agent in agents:
        counts[agent.state] = counts.get(agent.state, 0) + 1
        providers[agent.provider] = providers.get(agent.provider, 0) + 1
    states = " · ".join(
        f"{n} {_STATE_WORDS[s][0 if n == 1 else 1]}"
        for s in _STATE_WORDS
        if (n := counts.get(s, 0))
    )
    vendors = ", ".join(f"{n} {_PROVIDER.get(p, p.value)}" for p, n in providers.items())
    return _section(
        "Agenten", len(agents), f'<p class="small muted">{esc(states)}<br>{esc(vendors)}</p>'
    )
