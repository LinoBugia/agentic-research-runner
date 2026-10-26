"""K9 progress bar of the topology (docs/08-ux-konzept.md K9, §6.5 „Ablaufleiste“).

One segment per stage with symbol, status word for screen readers and a sub line: the detail
(`exploration`: „3 Ideen · Turn 5–7 von 8“) or attempt chips (`waterfall`: „V1 6,0“, current
attempt blue). A summary segment „✓ 1–3 erledigt“ with the scores of the finished stages is
always rendered but hidden; static/run_detail.js shows it instead of the finished segments when
the segments do not fit into the row (§6.8).
"""

from arr.models import TopologyKind
from arr.service.api_models import Attempt, StageInfo, StageStatus
from arr.service.gui import format as fmt
from arr.service.gui.components.base import esc, icon
from arr.service.gui.texts import TOPOLOGY_LABEL

_LOOK = {
    StageStatus.DONE: ("done", "check", "erledigt"),
    StageStatus.RUNNING: ("now", "circle", "läuft"),
    StageStatus.WAITING_USER: ("now waitme", "front_hand", "wartet auf dich"),
    StageStatus.FAILED: ("failed", "close", "fehlgeschlagen"),
    StageStatus.TODO: ("todo", "radio_button_unchecked", "offen"),
}


def attempt_chip(attempt: Attempt, *, running: bool = False) -> str:
    """„V2 8,5“ green when passed, red when not, blue „V1 läuft“ while unrated (§6.5)."""
    if attempt.avg_score is None:
        word = "läuft" if running else "offen"
        return f'<span class="att live">V{attempt.n} {word}</span>'
    cls = "pass" if attempt.passed else "fail"
    word = "bestanden" if attempt.passed else "nicht bestanden"
    return (
        f'<span class="att {cls}" title="Versuch {attempt.n}: {fmt.score(attempt.avg_score)}, '
        f'{word}">V{attempt.n} {fmt.score(attempt.avg_score)}</span>'
    )


def _segment(stage: StageInfo) -> str:
    css, ico, word = _LOOK[stage.status]
    now = stage.status in (StageStatus.RUNNING, StageStatus.WAITING_USER)
    current = ' aria-current="step"' if now else ""
    sym = icon(ico, small=True, pulse=stage.status is StageStatus.RUNNING)
    name = f"{stage.index} · {stage.label}" if stage.index else stage.label
    if stage.round:
        name += f" R{stage.round}"
    if stage.attempts:
        chips = "".join(
            attempt_chip(a, running=now and i == len(stage.attempts) - 1)
            for i, a in enumerate(stage.attempts)
        )
        sub = f'<span class="attempts">{chips}</span>'
    else:
        sub = f'<span class="sub">{esc(stage.detail or "")}</span>' if stage.detail else ""
    return (
        f'<li class="stage {css}"{current}><span class="nm">{sym}{esc(name)}'
        f'<span class="sr-only"> ({word})</span></span>{sub}</li>'
    )


def _final_score(stage: StageInfo) -> float | None:
    rated = [a for a in stage.attempts if a.avg_score is not None]
    return rated[-1].avg_score if rated else None


def _summary(done: list[StageInfo]) -> str:
    if not done:
        return ""
    first, last = done[0].index, done[-1].index
    span = f"{first}–{last}" if first != last else f"{first}"
    tips = []
    chips = []
    for stage in done:
        scores = ", ".join(
            f"V{a.n} {fmt.score(a.avg_score)}" for a in stage.attempts if a.avg_score is not None
        )
        tips.append(f"{stage.index} {stage.label}" + (f" {scores}" if scores else ""))
        final = _final_score(stage)
        if final is not None:
            chips.append(f'<span class="att pass">{fmt.score(final)}</span>')
    sub = f'<span class="attempts">{"".join(chips)}</span>' if chips else ""
    return (
        f'<li class="stage done sum" title="{esc(" · ".join(tips))}">'
        f'<span class="nm">{icon("check", small=True)}{span} erledigt</span>{sub}</li>'
    )


def stage_bar_html(stages: list[StageInfo], topology: TopologyKind) -> str:
    """The progress bar as an ordered list (`aria-current="step"` on the current stage)."""
    ordered = sorted(stages, key=lambda s: s.index)
    # only a leading run of finished stages can be summed up
    leading = []
    for stage in ordered:
        if stage.status is not StageStatus.DONE:
            break
        leading.append(stage)
    summary = _summary(leading) if len(leading) >= 2 and len(leading) < len(ordered) else ""
    segments = "".join(_segment(s) for s in ordered)
    label = f"Ablauf der Topologie {TOPOLOGY_LABEL[topology]}, {len(ordered)} Stufen"
    return f'<ol class="stagebar" aria-label="{esc(label)}">{summary}{segments}</ol>'
