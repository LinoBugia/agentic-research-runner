"""A running or finished run with `topology: phases` in the run detail (W4b-07 §6.4; docs/08
§6.5, docs/10 §4.9).

- `phase_stage_infos`: the progress bar (K9) over loops. One segment per phase in the order of
  the plan; a phase runs between its last `pass_start` and the following `route`, is done when
  no new `pass_start` came after that, „wartet auf dich“ while an escalation of it is open, and
  failed after a `stage_end` with `outcome: stage_failed`. Passes are chips with the result of
  the gate; a mean score only at a gate `judge` with `mean` (checkpoints: W4b-02b §5.2).
- `phase_usage`: usage per phase (Entscheidung 14) from `usage.json` `by_phase` once the run is
  final, else from the transcripts (`turn_start.stage`), cached per file growth.
- `run_phase_plan`: the phase plan with passes, gate result, usage and estimate per phase.

Every checkpoint is read defensively: a number instead of a text, a list instead of an object
never breaks the bar or the table.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arr.backends.base import CostKind, Usage
from arr.models import Compact, GateRule, GateType, ResolvedRun
from arr.resolve import parse_yaml_text
from arr.runs.compact import Verdict, decide, threshold
from arr.service.agent_view import block, line
from arr.service.api_models import PassInfo, PhasePlan, PhaseUsage, StageInfo, StageStatus
from arr.service.real_api.phase_plan import CHOICE_TEXT, gate_result_text, phase_plan, progress
from arr.usage import UsageTotal, aggregate_usage

log = logging.getLogger(__name__)

PHASE_NAMES_BYTES = 256_000
"""Largest `run.input.yaml` or `group.yaml` read for the names of dropped phases."""


def _data(entry: Any) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        return {}
    data = entry.get("data")
    return data if isinstance(data, Mapping) else {}


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def open_escalation_stage(entries: Sequence[Mapping[str, Any]]) -> str | None:
    """The phase of the last `escalation` without a following `escalation_decided`."""
    stage: str | None = None
    for entry in entries:
        kind = entry.get("kind") if isinstance(entry, Mapping) else None
        if kind == "escalation":
            value = _data(entry).get("stage")
            stage = value if isinstance(value, str) else ""
        elif kind == "escalation_decided":
            stage = None
    return stage


def _mean_gate(resolved: ResolvedRun | None, name: str) -> tuple[bool, float | None]:
    if resolved is None:
        return False, None
    try:
        gate = resolved.phase(name).gate
    except KeyError:
        return False, None
    mean = gate.type is GateType.JUDGE and gate.rule is GateRule.MEAN
    return mean, gate.pass_score if mean else None


def phase_stage_infos(
    entries: Sequence[Mapping[str, Any]],
    resolved: ResolvedRun | None,
    plan: Sequence[str],
    *,
    active: bool,
    failed_run: bool,
    escalation_open: bool,
    final: bool = False,
) -> list[StageInfo]:
    """The progress bar of a phases run (see the module docstring)."""
    passes: dict[str, dict[int, PassInfo]] = {}
    started: dict[str, int] = {}
    routed: dict[str, int] = {}
    failed: dict[str, int] = {}
    for pos, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or entry.get("kind") != "checkpoint":
            continue
        data = _data(entry)
        stage = data.get("stage")
        if not isinstance(stage, str) or stage == "plan":
            continue
        kind = data.get("phase")
        n = _int(data.get("attempt"))
        if kind == "pass_start":
            number = n if n is not None and n >= 1 else len(passes.get(stage, {})) + 1
            passes.setdefault(stage, {})[number] = PassInfo(n=number, running=True)
            started[stage] = pos
        elif kind == "gate" and n is not None and n >= 1:
            info = passes.setdefault(stage, {}).setdefault(n, PassInfo(n=n))
            mean, pass_score = _mean_gate(resolved, stage)
            decision = data.get("decision")
            value = gate_result_text({"value": data.get("value"), "decision": decision})
            info.value = value
            info.passed = {"passed": True, "failed": False}.get(str(decision))
            if mean:
                info.score = _float(data.get("score"))
                info.pass_score = _float(pass_score)
        elif kind == "route":
            routed[stage] = pos
            if n is not None and n in passes.get(stage, {}):
                passes[stage][n].running = False
                choice = CHOICE_TEXT.get(str(data.get("choice")))
                if choice is not None:  # gate `user`, `escalate`: what the user chose
                    passes[stage][n].value = choice
        elif kind == "stage_end" and data.get("outcome") == "stage_failed":
            failed[stage] = pos
    waiting_stage = open_escalation_stage(entries) if escalation_open else None
    out = []
    for index, name in enumerate(plan, start=1):
        start = started.get(name)
        if name in failed and (start is None or failed[name] > start):
            state = StageStatus.FAILED
        elif start is not None and routed.get(name, -1) < start:
            if not active:
                state = StageStatus.FAILED if failed_run else StageStatus.DONE
            elif escalation_open and waiting_stage in (name, ""):
                state = StageStatus.WAITING_USER
            else:
                state = StageStatus.RUNNING
        elif start is not None:
            state = StageStatus.DONE
        elif final:  # a route went past it: never entered (B8)
            state = StageStatus.SKIPPED
        else:
            state = StageStatus.TODO
        chips = [passes[name][k] for k in sorted(passes.get(name, {}))]
        if state not in (StageStatus.RUNNING, StageStatus.WAITING_USER):
            chips = [c.model_copy(update={"running": False}) for c in chips]
        agents: list[str] = []
        if resolved is not None:
            try:
                agents = list(resolved.phase(name).participants)
            except KeyError:
                agents = []
        out.append(
            StageInfo(key=name, index=index, label=name, status=state, agents=agents, passes=chips)
        )
    return out


# --------------------------------------------------------------------------------------------
# Usage per phase
# --------------------------------------------------------------------------------------------


def _tokens(usage: Usage) -> int | None:
    parts = [usage.input, usage.output, usage.cache_read, usage.cache_write, usage.reasoning]
    known = [p for p in parts if p is not None]
    return sum(known) if known else None


def phase_usage_of(total: UsageTotal) -> PhaseUsage:
    """`UsageTotal` as a cell of the plan: turns, tokens, cost (estimated: „≈“)."""
    usage = total.usage
    cost = usage.cost_usd
    kind = usage.cost_kind
    if cost is None and (total.cost_billed_usd or total.cost_estimated_usd):
        cost = total.cost_billed_usd + total.cost_estimated_usd
        kind = CostKind.ESTIMATED if total.cost_estimated_usd else CostKind.BILLED
    return PhaseUsage(turns=total.turns, tokens=_tokens(usage), cost_usd=cost, cost_kind=kind)


def phase_usage(record: Any, resolved: ResolvedRun | None) -> dict[str, PhaseUsage]:
    """Usage per phase: `usage.json` `by_phase` of a final run, else the transcripts (turns
    without a phase count only in the sum of the run)."""
    final = record.usage if record.final else None
    if final is not None and final.by_phase:
        return {name: phase_usage_of(total) for name, total in final.by_phase.items()}
    configs = {a.id: a for a in resolved.agents} if resolved is not None else {}
    records = []
    for agent_id, agent in record.agents.items():
        records.extend(agent.transcript.usage_records(configs.get(agent_id)))
    summed = aggregate_usage(records)
    return {name: phase_usage_of(total) for name, total in summed.by_phase.items()}


# --------------------------------------------------------------------------------------------
# The plan of a run
# --------------------------------------------------------------------------------------------


def _names_in(text: str) -> list[str] | None:
    data, _lines = parse_yaml_text(text, "run.input.yaml")
    phases = data.get("phases") if isinstance(data, Mapping) else None
    if not isinstance(phases, list) or not phases:
        return None
    return [p["name"] for p in phases if isinstance(p, Mapping) and isinstance(p.get("name"), str)]


def input_phase_names(record: Any, resolved: ResolvedRun) -> list[str] | None:
    """The phase names of the run's input (`run.input.yaml`) or, for a run of a skill, of its
    `group.yaml` as it is now; None if neither can be read."""
    from arr.service.real_api.rundir import read_regular

    candidates = [record.run_dir / "run.input.yaml"]
    if resolved.skill:
        candidates.append(record.run_dir.parent.parent / "skills" / resolved.skill / "group.yaml")
    for path in candidates:
        try:
            names = _names_in(read_regular(path, PHASE_NAMES_BYTES))
        except Exception:  # noqa: BLE001 - missing or odd file: no names from it
            continue
        if names:
            return names
    return None


def run_phase_plan(record: Any, resolved: ResolvedRun | None) -> PhasePlan | None:
    """The phase plan of a run with its progress (None for the old topologies). The rows and
    the estimate are built once per `run.yaml`; passes and usage on every call."""
    if resolved is None or not resolved.phases:
        return None
    cached = getattr(record, "_phase_plan", None)
    if cached is None or cached[0] is not resolved:
        try:
            base = phase_plan(resolved, names=input_phase_names(record, resolved))
        except Exception:  # noqa: BLE001 - never break the run detail
            log.warning("Phasenplan von %s nicht möglich", record.run_id, exc_info=True)
            base = None
        cached = (resolved, base)
        record._phase_plan = cached
    base = cached[1]
    if base is None:
        return None
    try:
        usage = phase_usage(record, resolved)
    except Exception:  # noqa: BLE001
        log.warning("Usage je Phase von %s nicht lesbar", record.run_id, exc_info=True)
        usage = {}
    return progress(base, record.log.entries, usage)


# --------------------------------------------------------------------------------------------
# Fill level of an agent's session (K3, Entscheidung 49, W4b-05b §7.1)
# --------------------------------------------------------------------------------------------


def _thousands(value: int) -> str:
    return f"{value:,}".replace(",", " ")


@dataclass
class ContextFill:
    """What the agent card says about the agent's context."""

    text: str
    tokens: int | None = None
    window: int | None = None
    threshold: int | None = None
    compactions: int = 0
    note_turns: int = 0


@dataclass
class _AgentLog:
    compactions: int = 0
    no_fill: bool = False
    note_turns: int = 0


def agent_logs(entries: Sequence[Mapping[str, Any]]) -> dict[str, _AgentLog]:
    """Per agent: compactions (`compact`), `compact_no_fill`, note turns (`purpose: compact`)."""
    out: dict[str, _AgentLog] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        kind = entry.get("kind")
        if kind not in ("compact", "compact_no_fill", "turn"):
            continue
        data = _data(entry)
        agent = data.get("agent")
        if not isinstance(agent, str):
            continue
        state = out.setdefault(agent, _AgentLog())
        if kind == "compact":
            state.compactions += 1
        elif kind == "compact_no_fill":
            state.no_fill = True
        elif data.get("purpose") == "compact":
            state.note_turns += 1
    return out


def context_fill(
    compact: Compact, turn_ends: Sequence[Mapping[str, Any]], log: _AgentLog | None
) -> ContextFill | None:
    """„Kontext 48 % · verdichtet ab 60 %“, „Kontext 96 000 Token · verdichtet ab 120 000“,
    „Kontext unbekannt“ (null or after `compact_no_fill`), „Verdichten aus (at: 100 %)“; the
    threshold comes from `arr.runs.compact.threshold`, „aus“ from `decide` (Verdict.OFF).
    None before the first finished turn."""
    log = log or _AgentLog()
    last: Mapping[str, Any] = {}
    for record in reversed(turn_ends):
        data = record.get("data") if isinstance(record, Mapping) else None
        if isinstance(data, Mapping):
            last = data
            break
    if "context_tokens" not in last and not log.compactions and not log.no_fill:
        return None  # runs before W4b-05b measure nothing: the card stays as it was
    tokens = _int(last.get("context_tokens"))
    window = _int(last.get("context_window"))
    tokens = tokens if tokens is not None and tokens >= 0 else None
    window = window if window is not None and window > 0 else None
    limit = threshold(compact, window)
    verdict = decide(compact, tokens=tokens, window=window, turns_left=None).verdict
    if verdict is Verdict.OFF:
        text = "Verdichten aus (at: 100 %)"
        if tokens is not None:
            text = f"Kontext {_share(tokens, window)} · " + text
    elif tokens is None or log.no_fill:
        text = "Kontext unbekannt"
        tokens = None
    else:
        text = f"Kontext {_share(tokens, window)}"
        if compact.share is not None and window is not None:
            text += f" · verdichtet ab {round(compact.share * 100)} %"
        elif limit is not None and window is not None:
            text += f" · verdichtet ab {round(limit * 100 / window)} %"
        elif limit is not None:
            text += f" · verdichtet ab {_thousands(limit)}"
        elif compact.share is not None:
            text += f" · verdichtet ab {round(compact.share * 100)} % (Fenster unbekannt)"
    if log.compactions:
        text += f" · {log.compactions}× verdichtet"
    return ContextFill(
        text=text,
        tokens=tokens,
        window=window,
        threshold=limit,
        compactions=log.compactions,
        note_turns=log.note_turns,
    )


def _share(tokens: int, window: int | None) -> str:
    if window:
        return f"{round(tokens * 100 / window)} %"
    return f"{_thousands(tokens)} Token"


# --------------------------------------------------------------------------------------------
# Submissions in the view „Ergebnisse“ (W4b-07 §6.6)
# --------------------------------------------------------------------------------------------

SUMMARY_CHARS = 1_000
FIELD_CHARS = 2_000


def _gate_facts(entries: Sequence[Mapping[str, Any]]) -> tuple[dict[int, float], set[int]]:
    """Scores per submission (`gate.scores`) and the accepted ones (`stage_end.accepted`)."""
    scores: dict[int, float] = {}
    accepted: set[int] = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("kind") != "checkpoint":
            continue
        data = _data(entry)
        if data.get("phase") == "gate" and isinstance(data.get("scores"), Mapping):
            for key, value in data["scores"].items():
                number = _float(value)
                if number is not None and str(key).isdigit():
                    scores[int(key)] = number
        elif data.get("phase") == "stage_end":
            chosen = _int(data.get("accepted"))
            if chosen is not None:
                accepted.add(chosen)
    return scores, accepted


PRIVATE_FIELDS = frozenset({"note_next"})
"""Field names never shown in „Ergebnisse“, like `user_api.PRIVATE_KEYS` (W4b-07.1, B10)."""


def _fields(value: Any) -> dict[str, Any]:
    """The fields of a submission as JSON values: texts cleaned (`agent_view`) and cut, keys
    one line, a field named `note_next` left out (agent text)."""
    if not isinstance(value, Mapping):
        return {}

    def cut(item: Any, depth: int = 0) -> Any:
        if isinstance(item, str):
            return block(item, FIELD_CHARS)
        if depth > 4:
            return "…"
        if isinstance(item, Mapping):
            return {line(k, 64): cut(v, depth + 1) for k, v in list(item.items())[:50]}
        if isinstance(item, list):
            return [cut(v, depth + 1) for v in item[:50]]
        if item is None or isinstance(item, bool | int | float):
            return item
        return block(str(item), FIELD_CHARS)

    return {
        line(k, 64): cut(v) for k, v in list(value.items())[:50] if str(k) not in PRIVATE_FIELDS
    }


def _entry_of(action: str | None) -> int | None:
    if action and action.startswith("work-") and action[5:].isdigit():
        return int(action[5:])
    return None


def submission_infos(record: Any, store: Any) -> list[Any]:
    """The submissions of a run as `SubmissionInfo`, in order, without `note_next` (the view
    „Ergebnisse“; the transcript keeps the whole `submit` call, Entscheidung 80)."""
    from arr.service.api_models import SubmissionInfo
    from arr.service.real_api.rundir import EXCERPT_CHARS

    try:
        subs = store.submissions()
    except Exception:  # noqa: BLE001 - a damaged room never breaks the view
        log.warning("Abgaben von %s nicht lesbar", record.run_id, exc_info=True)
        return []
    scores, accepted = _gate_facts(record.log.entries)
    out = []
    for sub in subs:
        out.append(
            SubmissionInfo(
                id=sub.id,
                ts=sub.ts,
                agent=sub.agent,
                alias=line(sub.shown_as, 64)
                if sub.shown_as and sub.shown_as != sub.agent
                else None,
                stage=sub.stage,
                attempt=sub.attempt,
                action=sub.action,
                format=sub.format,
                name=line(sub.name, 120) if sub.name else None,
                version=sub.version,
                status=sub.status,
                summary=line(sub.summary, SUMMARY_CHARS),
                fields=_fields(sub.fields),
                files=[line(f, 200) for f in sub.files[:100]],
                body_excerpt=line(sub.body, EXCERPT_CHARS) or None,
                path=sub.path,
                score=scores.get(sub.id),
                accepted=sub.id in accepted,
                sealed=sub.sealed,
                entry=_entry_of(sub.action),
            )
        )
    return out
