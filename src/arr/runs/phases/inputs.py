"""Inputs of an action, feedback of a repeated pass, the summary for the user (W4b-02b §6.3,
§6.7, §6.8; docs/10 §4.4).

An input item is a submission as the receiving agent may see it:

    {"source", "id", "phase", "attempt", "from", "format", "status", "summary", "path",
     "name"?, "version"?, "entry"?, "files"?, "score"?, "text"?, "note"?}

`source` is `phase:<name>` (standard inputs and `from`), `name:<name>` or `channel:<name>`
(then `messages` instead of a submission). `from` is the author as the receiver sees it
(`ctx.shown_name`, never the real ID behind an alias; `arr` for a seed). `path` is the
absolute folder of the submission below `output/`. `text` only with `content: full`.

Isolation: a submission another agent handed in that is still sealed is never an input; only
`peers: summary` shows summaries of the running pass. Everything an agent wrote is cleaned and
limited (`arr.runs.phases.text`).
"""

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from arr.models import GateRule, GateType, InputContent, Pick, ResolvedInput, ResolvedPhase
from arr.room.store import ORCHESTRATOR
from arr.room.types import Submission
from arr.runs.finalize import one_line
from arr.runs.phases.text import (
    MAX_CHANNEL_MESSAGES,
    MAX_COMMENT_CHARS,
    MAX_FULL_CHARS,
    MAX_HINT_CHARS,
    MAX_INBOX_CHARS,
    MAX_INBOX_ITEMS,
    MAX_INBOX_LINE,
    MAX_INPUT_ITEMS,
    MAX_MESSAGE_CHARS,
    MAX_NAME_CHARS,
    MAX_OBJECTION_CHARS,
    MAX_SUMMARY_CHARS,
    TRUNCATED,
    block,
    line,
)

if TYPE_CHECKING:
    from arr.runs.context import RunContext
    from arr.runs.phases.state import PassRecord, RunState

__all__ = [
    "build_feedback",
    "build_inputs",
    "inbox_summary",
    "is_work",
    "item",
    "newest_per_action",
    "shown",
    "visible",
]

MAX_FILES_LISTED = 20


def is_work(s: Submission) -> bool:
    """A submission of an action (`work`, `work-<n>`), not of a gate's rating action."""
    return s.action is not None and (s.action == "work" or s.action.startswith("work-"))


def visible(s: Submission, viewer: str) -> bool:
    """False for a submission of another agent that is still sealed (isolation)."""
    return not s.sealed or s.agent == viewer


def shown(ctx: "RunContext", viewer: str, agent: str, fallback: str | None = None) -> str:
    """The name of `agent` as `viewer` sees it; `arr` for arr's own versions (seeds)."""
    if agent == ORCHESTRATOR:
        return line(fallback or "arr", MAX_NAME_CHARS)
    try:
        return line(ctx.shown_name(viewer, agent), MAX_NAME_CHARS)
    except Exception:  # an agent the view does not know: its ID is no secret then
        return line(agent, MAX_NAME_CHARS)


def newest_per_action(subs: Iterable[Submission]) -> list[Submission]:
    """The newest submission per agent and action (over all passes), oldest first."""
    newest: dict[tuple[str, str | None], Submission] = {}
    for s in subs:
        key = (s.agent, s.action)
        if key not in newest or s.id > newest[key].id:
            newest[key] = s
    return sorted(newest.values(), key=lambda s: s.id)


def _entry_number(s: Submission) -> int | None:
    if s.action and s.action.startswith("work-"):
        try:
            return int(s.action.removeprefix("work-"))
        except ValueError:
            return None
    return None


def _full_text(ctx: "RunContext", s: Submission) -> str:
    parts: list[str] = []
    if s.body:
        parts.append(s.body)
    for name in s.files:
        text = ctx.read_output(f"{s.path}/files/{name}", MAX_FULL_CHARS * 4)
        title = line(name, MAX_NAME_CHARS)
        if text is None:
            parts.append(f"[Datei „{title}“: kein lesbarer Text oder zu groß]")
        else:
            parts.append(f"[Datei „{title}“]\n{text}")
        if sum(len(p) for p in parts) > MAX_FULL_CHARS:
            break
    return block("\n\n".join(parts), MAX_FULL_CHARS)


def item(
    ctx: "RunContext",
    viewer: str,
    s: Submission,
    *,
    source: str,
    content: InputContent = InputContent.SUMMARY,
    score: float | None = None,
) -> dict[str, Any]:
    """A submission as an input of `viewer` (see the module docstring)."""
    out: dict[str, Any] = {
        "source": source,
        "id": s.id,
        "phase": s.stage,
        "attempt": s.attempt,
        "from": shown(ctx, viewer, s.agent, s.shown_as),
        "format": s.format,
        "status": s.status,
        "summary": line(s.summary, MAX_SUMMARY_CHARS),
    }
    if s.name:
        out["name"], out["version"] = s.name, s.version
    entry = _entry_number(s)
    if entry is not None:
        out["entry"] = entry
    if s.sealed:
        out["path"] = None
        out["note"] = "Deine Abgabe ist noch versiegelt; sie liegt nach der Phase unter output/."
    else:
        out["path"] = str(ctx.run_folder / s.path)
    if s.files:
        out["files"] = [line(f, MAX_NAME_CHARS) for f in s.files[:MAX_FILES_LISTED]]
    if score is not None:
        out["score"] = round(score, 2)
    if content is InputContent.FULL:
        out["text"] = _full_text(ctx, s)
    return out


def _best(state: "RunState", subs: Sequence[Submission]) -> Submission | None:
    rated = [s for s in subs if s.id in state.scores]
    if not rated:
        return None
    return max(rated, key=lambda s: (state.scores[s.id], s.id))


def _from_phase(
    ctx: "RunContext",
    state: "RunState",
    run_phase: ResolvedPhase | None,
    name: str,
    pick: Pick,
    viewer: str,
) -> tuple[list[Submission], str | None]:
    subs = [s for s in ctx.submissions(stage=name) if is_work(s) and visible(s, viewer)]
    if not subs:
        return [], f"Die Phase „{name}“ hat noch keine Abgabe."
    if pick is Pick.ALL:
        return subs, None
    if pick is Pick.BEST and run_phase is not None and run_phase.gate.type is GateType.JUDGE:
        if run_phase.gate.rule is GateRule.RANK:
            record = state.last(name)
            ranking = record.outcome.ranking if record is not None else []
            by_id = {s.id: s for s in subs}
            if ranking and ranking[0] in by_id:
                return [by_id[ranking[0]]], None
        best = _best(state, subs)
        if best is not None:
            return [best], None
    latest = newest_per_action(subs)
    if pick is Pick.BEST:
        return latest, "Keine Bewertung vorhanden: die neuesten Abgaben."
    return latest, None


def _from_name(
    ctx: "RunContext", state: "RunState", name: str, pick: Pick, viewer: str
) -> tuple[list[Submission], str | None]:
    subs = [s for s in ctx.submissions(name=name) if visible(s, viewer)]
    if not subs:
        return [], f"Von „{name}“ gibt es noch keine Fassung."
    if pick is Pick.ALL:
        return subs, None
    if pick is Pick.BEST:
        best = _best(state, subs)
        if best is not None:
            return [best], None
        return [subs[-1]], "Keine Bewertung vorhanden: die neueste Fassung."
    return [subs[-1]], None


def _channel(ctx: "RunContext", name: str, viewer: str) -> dict[str, Any]:
    out: dict[str, Any] = {"source": f"channel:{name}"}
    try:
        members = ctx.config.channel(name).members
    except KeyError:
        return {**out, "note": f"Den Kanal „{name}“ gibt es nicht."}
    if viewer not in members:
        return {**out, "note": f"Du bist nicht Mitglied von #{name}; den Verlauf siehst du nicht."}
    messages = ctx.channel_messages(name)[-MAX_CHANNEL_MESSAGES:]
    out["messages"] = [
        {
            "id": m.id,
            "from": shown(ctx, viewer, m.sender) if m.sender in ctx.agents else m.sender,
            "kind": str(m.kind),
            "text": block(m.body, MAX_MESSAGE_CHARS),
        }
        for m in messages
    ]
    return out


def build_inputs(
    ctx: "RunContext", state: "RunState", phase: ResolvedPhase, viewer: str
) -> tuple[list[dict[str, Any]], bool]:
    """The inputs of `viewer` in `phase` and whether it asks for `peers: summary`.

    Without `inputs`: per phase finished before (in the order they were finished last, not
    this phase) summary and path of its valid submissions (docs/10 §4.4, Entscheidung 6).
    With `inputs`: exactly the sources named."""
    out: list[dict[str, Any]] = []
    if phase.inputs is None:
        for name in state.finished:
            if name == phase.name:
                continue
            for s in state.valid.get(name, []):
                if visible(s, viewer):
                    out.append(
                        item(ctx, viewer, s, source=f"phase:{name}", score=state.scores.get(s.id))
                    )
        return out[:MAX_INPUT_ITEMS], False
    peers = False
    for spec in phase.inputs:
        if spec.peers is not None:
            peers = True
            continue
        out += _source(ctx, state, spec, viewer)
        if len(out) >= MAX_INPUT_ITEMS:
            break
    return out[:MAX_INPUT_ITEMS], peers


def _source(
    ctx: "RunContext", state: "RunState", spec: ResolvedInput, viewer: str
) -> list[dict[str, Any]]:
    if spec.channel is not None:
        return [_channel(ctx, spec.channel, viewer)]
    pick = spec.pick or Pick.LATEST
    if spec.from_ is not None:
        try:
            run_phase = ctx.config.phase(spec.from_)
        except KeyError:
            run_phase = None
        subs, note = _from_phase(ctx, state, run_phase, spec.from_, pick, viewer)
        source = f"phase:{spec.from_}"
    else:
        assert spec.name is not None
        subs, note = _from_name(ctx, state, spec.name, pick, viewer)
        source = f"name:{spec.name}"
    items = [
        item(ctx, viewer, s, source=source, content=spec.content, score=state.scores.get(s.id))
        for s in subs[:MAX_INPUT_ITEMS]
    ]
    if note is not None:
        items.append({"source": source, "note": note})
    return items


def _objections_of(s: Submission) -> list[dict[str, Any]]:
    entries = s.fields.get("objections")
    out = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        one = {"text": block(entry.get("text") or "", MAX_OBJECTION_CHARS)}
        if isinstance(entry.get("ref"), str):
            one["ref"] = line(entry["ref"], 64)
        if isinstance(entry.get("status"), str):
            one["status"] = line(entry["status"], 20)
        out.append(one)
    return out[:100]


def build_feedback(
    ctx: "RunContext", record: "PassRecord", viewer: str, hint: str | None
) -> dict[str, Any]:
    """Feedback of the last pass for a repeated one (Entscheidung 16): summaries of its
    submissions, the result of the gate, scores and comments of the raters, the objections of
    the reviews and the user's hint (as the user's text)."""
    outcome = record.outcome
    if outcome.result is not None:
        result = outcome.result
    elif outcome.tie:
        result = "Gleichstand"
    else:
        result = "kein Ergebnis"
    out: dict[str, Any] = {
        "attempt": record.attempt,
        "gate": record.gate,
        "result": result,
        "submissions": [
            item(ctx, viewer, s, source=f"phase:{record.phase}", score=outcome.scores.get(s.id))
            for s in record.latest
            if visible(s, viewer)
        ],
    }
    if outcome.score is not None:
        out["score"] = round(outcome.score, 2)
    if record.missing:
        out["missing"] = len(record.missing)
    if record.ratings:
        out["ratings"] = [
            {
                "rater": shown(ctx, viewer, r["rater"]),
                "ref": r["ref"],
                "score": r["score"],
                **({"comment": block(r["comment"], MAX_COMMENT_CHARS)} if r.get("comment") else {}),
            }
            for r in record.ratings
        ]
    reviews = [s for s in [*record.latest, *record.rater_reviews] if s.format == "review"]
    if reviews:
        out["reviews"] = [
            {
                "from": shown(ctx, viewer, s.agent, s.shown_as),
                "verdict": line(s.fields.get("verdict") or "", 20) or None,
                "objections": _objections_of(s),
            }
            for s in reviews
            if visible(s, viewer)
        ]
    if hint:
        out["user_hint"] = block(hint, MAX_HINT_CHARS)
    return out


REASONS = {
    "gate_user": "Gate „user“: Du entscheidest, wie es weitergeht.",
    "escalate": "Route „escalate“: Das Gate gibt die Entscheidung an dich.",
    "exhausted": "Die Durchläufe der Phase „{phase}“ sind aufgebraucht („on_exhausted: escalate“).",
    "no_result": "Das Gate kann kein Ergebnis bilden: {reason}",
}


def inbox_summary(
    phase: str, attempt: int, reason: str, subs: Sequence[Submission], detail: str = ""
) -> str:
    """The text of the inbox item: phase, pass, why, one line per submission (agent text
    cleaned, on one line, Markdown marks escaped, at most `MAX_INBOX_LINE` characters; at most
    `MAX_INBOX_ITEMS`), the whole at most `MAX_INBOX_CHARS`."""
    why = REASONS[reason].format(phase=phase, reason=one_line(line(detail, 300), 300))
    lines = [f"Phase „{phase}“, Durchlauf {attempt}. {why}"]
    if subs:
        lines.append("Abgaben:")
    for s in subs[:MAX_INBOX_ITEMS]:
        summary = one_line(line(s.summary, MAX_INBOX_LINE), MAX_INBOX_LINE)
        who = one_line(line(s.agent, 40), 40)
        lines.append(f"- #{s.id} {who} ({s.format}, {s.status}): {summary}")
    if len(subs) > MAX_INBOX_ITEMS:
        lines.append(f"… und {len(subs) - MAX_INBOX_ITEMS} weitere")
    if not subs:
        lines.append("Keine Abgaben.")
    text = "\n".join(lines)
    if len(text) > MAX_INBOX_CHARS:
        text = text[: MAX_INBOX_CHARS - len(TRUNCATED)] + TRUNCATED
    return text
