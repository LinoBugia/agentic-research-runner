"""Area `approvals`: the approval inbox of the service (W3-03, docs/07 §6, docs/08 §5.2).

Reading: `ApprovalReader` builds the `ApprovalItem`s of a run from lines of `approvals.jsonl`.
For a run of `arr run` and a finished run these come from the file. For a run this service
executes they come from its `ApprovalLog` in memory, the same source `decide_approval` acts on,
and so do its `approval.*` events and counters: a line somebody else adds to the file changes
no card, blocks none and moves no number. Only lines with an
`item_id` are items, so `allow` of fixed rules (one line per tool call) never shows. A run that
is not running any more has no open items: what is left open counts as obsolete.

Deciding: the only way to a granted item is `decide_approval`, which the GUI calls after a
click behind the guard of W3-01; it puts a decision of `user` into the `ApprovalInbox` of the
run's `RunManager`. The inbox wakes the waiting agent. `GuiDecider` only keeps the item open
for that; without a decision it expires in the inbox (counts as denied). Runs of `arr run` are
decided in their terminal, here they are read-only.

Questions of agents to the user (`@user`, docs/09 L16) are items „Frage“: `decide_approval`
with `answer` posts the text as the user's answer into the channel, with `reply_to` on the
question and to the asking agent, so `wait_for_messages` hands it over and a finished agent is
woken (L13); then it closes the item. If the post fails, the item stays open (W3-09.1, B6).
"""

import logging
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from arr.errors import ArrError
from arr.models import Decider, Provider, Starter, TopologyKind
from arr.permissions.inbox import ApprovalInbox, install_hosts
from arr.permissions.policy import AlwaysRule, is_codex_escalation
from arr.permissions.types import ApprovalItem as CoreApprovalItem
from arr.permissions.types import (
    ApprovalStatus,
    ApprovalType,
    DecidedBy,
    Decision,
    DecisionAction,
    DecisionScope,
    EscalationOption,
    PermissionRequest,
    RequestCategory,
)
from arr.room.store import RoomStore
from arr.room.types import MessageKind, MessageVia
from arr.runs.manager import QUESTION_CLOSED
from arr.runs.prepare import AgentFile, load_agent_file
from arr.service.api_models import (
    ApiException,
    ApprovalAgentRef,
    ApprovalItem,
    ApprovalRunRef,
    Attempt,
    CapacityInfo,
    Consequences,
    DecisionInfo,
    DecisionRequest,
    DecisionResponse,
    Deviation,
    ErrorCode,
    EscalationOptionInfo,
    Estimate,
    LastRating,
    LimitsInfo,
    RiskInfo,
    RiskLevel,
    RunStartAgent,
    RunStartChannel,
)
from arr.service.real_api.base import ServiceState, not_allowed, not_found
from arr.service.real_api.run_starts import RUN_START_KEY, change_infos, is_run_start
from arr.service.real_api.rundir import (
    RunRecord,
    item_entries,
    item_question,
    item_title,
    model_label,
    role_label,
    run_alive,
    run_results,
    stage_infos,
    vendor_of,
)

log = logging.getLogger("arr.service.approvals")

TERMINAL_HINT = (
    "Dieser Run läuft im Terminal (arr run). Entscheide die Anfrage dort, in der Oberfläche "
    "siehst du sie nur."
)
NOT_RUNNING = "Der Run läuft nicht mehr."
EXPIRE_TEXT = "Ohne Entscheidung gilt die Anfrage nach der Frist als abgelehnt."
DENY_REASON = "Der User hat abgelehnt. Arbeite ohne weiter oder frage im Chat nach."
NO_RULE = (
    "Aus dieser Anfrage wird keine feste Regel: Sie hat ein hohes Risiko, enthält ein Geheimnis "
    "oder lässt sich nicht als Regel fassen. Entscheide sie einmalig oder für diesen Run."
)
RULE_NOT_STORED = (
    "Die Regel ließ sich nicht in arr.yaml speichern. Sie gilt nur für die laufenden Runs. "
    "Prüfe die Datei arr.yaml."
)
_UNKNOWN_RISK = RiskInfo(level=RiskLevel.MEDIUM, text="Das Risiko ist nicht bekannt.")
_ACTIONS = {
    ApprovalType.PERMISSION: {DecisionAction.ALLOW, DecisionAction.DENY},
    ApprovalType.ESCALATION: {DecisionAction.OPTION},
    ApprovalType.QUESTION: {DecisionAction.ANSWER},
}
_SCOPE_WORDS = {
    DecisionScope.ONCE: "einmalig",
    DecisionScope.RUN: "für diesen Run",
    DecisionScope.ALWAYS: "immer",
}


class GuiDecider:
    """`ApprovalDecider` of the runs of the service: the item stays open for `decide_approval`.

    `approval.created` comes from the watcher, as for runs of `arr run`. Nothing here can
    answer an item; without a click the inbox lets it expire.
    """

    async def decide(self, item: CoreApprovalItem) -> Decision | None:
        return None


# --------------------------------------------------------------------------------------------
# From approvals.jsonl to items
# --------------------------------------------------------------------------------------------


@dataclass
class _Lines:
    """The lines of `approvals.jsonl` that belong to one item."""

    opened: Mapping[str, Any]
    closed: Mapping[str, Any] | None = None
    revoked: Mapping[str, Any] | None = None
    count: int = 1
    number: int = 0
    """Position among the escalations of the run."""


def _item_lines(entries: Sequence[Mapping[str, Any]]) -> dict[str, _Lines]:
    items: dict[str, _Lines] = {}
    escalations = 0
    for entry in entries:
        item_id = entry.get("item_id")
        if not item_id or not isinstance(item_id, str):
            continue
        event = entry.get("event")
        if event == "open":
            if item_id not in items:
                items[item_id] = _Lines(entry, number=escalations)
                escalations += (
                    entry.get("request") is None
                    and entry.get("question") is None
                    and not is_run_start(entry)
                )
        elif item_id not in items:
            continue
        elif event == "decision":
            items[item_id].count += 1
        elif event in ("decided", "expired", "obsolete"):
            items[item_id].closed = entry
        elif event == "revoked":
            items[item_id].revoked = entry
    return items


def _rule_text(rule: AlwaysRule) -> tuple[str, list[str]]:
    """Name of a rule and what it allows, from the rule itself, not from the request."""
    labels: list[str] = []
    effects: list[str] = []
    if rule.hosts:
        hosts = ", ".join(rule.hosts)
        labels.append(f"Netzwerk zu {hosts}")
        effects.append(
            f"Gilt für jede Anfrage an {hosts}, nicht nur für diesen Befehl: von allen Agenten, "
            "in allen Runs, auch zum Hochladen."
        )
    if rule.paths:
        paths = ", ".join(rule.paths)
        labels.append(f"Lesen in {paths}")
        effects.append(f"Alle Agenten dürfen in allen Runs alles in {paths} lesen.")
    if rule.commands:
        labels.append(", ".join(rule.commands))
        effects.append("Gilt nur für genau diesen Befehl, aber von allen Agenten, in allen Runs.")
    if rule.tools:
        tools = ", ".join(rule.tools)
        labels.append(f"Werkzeug {tools}")
        effects.append(f"Alle Agenten dürfen {tools} in allen Runs ohne Frage nutzen.")
    return " und ".join(labels), effects


def _package_hosts(request: Mapping[str, Any]) -> list[str]:
    """The hosts `ApprovalInbox` lets an allowed installation reach."""
    try:
        return sorted(install_hosts(PermissionRequest.model_validate(request)), reverse=True)
    except ValueError:
        return []


def _install_note(request: Mapping[str, Any]) -> str:
    """„ und darf dafür pypi.org … erreichen“ for what `ApprovalInbox` lets an install reach."""
    hosts = _package_hosts(request)
    return f" und darf dafür {' und '.join(hosts)} erreichen" if hosts else ""


def _consequences(request: Mapping[str, Any], agent: str) -> Consequences:
    categories = request.get("categories") or []
    if is_codex_escalation(request):
        allow = (
            "Der Befehl läuft einmal mit erhöhten Rechten. Was ihn dann noch begrenzt, kann arr "
            "nicht zusagen, und er kann trotz Freigabe scheitern. Gleiche Befehle fragen wieder."
        )
    elif RequestCategory.INSTALL.value in categories:
        allow = f"Die Installation läuft in der Run-venv{_install_note(request)}."
    elif request.get("hosts") or request.get("urls"):
        allow = f"{agent} darf diese Adresse erreichen."
    else:
        allow = f"{agent} darf diese Aktion ausführen."
    return Consequences(
        allow=allow,
        deny=f"{agent} erfährt die Ablehnung und arbeitet ohne diese Aktion weiter.",
        expire=EXPIRE_TEXT,
    )


def _decision_info(entry: Mapping[str, Any] | None) -> DecisionInfo | None:
    data = entry.get("decision") if entry is not None else None
    if not isinstance(data, Mapping):
        return None
    decided_by = DecidedBy(data.get("decided_by"))
    changes = data.get("changes")
    return DecisionInfo(
        action=DecisionAction(data.get("action")),
        scope=DecisionScope(data.get("scope", DecisionScope.ONCE)),
        option_key=data.get("option"),
        changes=change_infos(changes) if isinstance(changes, list) else [],
        comment=data.get("comment"),
        decided_by=decided_by,
        decided_at=float(data.get("decided_at") or entry.get("ts") or 0.0),  # type: ignore[union-attr]
        rationale=None if decided_by is DecidedBy.USER else data.get("reason") or None,
    )


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def live_inbox(state: ServiceState, run_id: str) -> ApprovalInbox | None:
    """The inbox of a run this service executes (None for runs of `arr run`)."""
    handle = state.registry.get(run_id)
    if handle is None or handle.done or handle.manager is None:
        return None
    return handle.manager.inbox


def own_lines(state: ServiceState, run_id: str) -> list[dict[str, Any]] | None:
    """The item lines of a run this service executes, from memory; None for other runs."""
    inbox = live_inbox(state, run_id)
    return inbox.log.item_lines() if inbox is not None and inbox.log is not None else None


@dataclass
class _Cached:
    key: tuple[Any, ...]
    items: dict[str, ApprovalItem]


@dataclass
class ApprovalReader:
    """`ApprovalItem`s of the run folders; items of a run are kept until a line changes."""

    state: ServiceState
    _cache: dict[str, _Cached] = field(default_factory=dict)
    _agents: dict[tuple[str, str], ApprovalAgentRef] = field(default_factory=dict)

    def items(self, record: RunRecord) -> dict[str, ApprovalItem]:
        """All items of a run by ID (`approvals.jsonl` of the record must be fresh)."""
        entries = item_entries(record)
        if not entries:
            return {}
        lines = _item_lines(entries)
        still_open = any(i.closed is None for i in lines.values())
        alive = still_open and run_alive(record)
        owned = self.state.registry.owns(record.run_id)
        key = (len(entries), record.status, alive, owned)
        cached = self._cache.get(record.run_id)
        if cached is not None and cached.key == key:
            return cached.items
        if any(i.opened.get("request") is None for i in lines.values()):
            record.log.refresh()
        items: dict[str, ApprovalItem] = {}
        for item_id, item_lines in lines.items():
            try:
                items[item_id] = self._build(record, item_id, item_lines, alive, owned)
            except (ArrError, ValueError, TypeError):  # a damaged line must not hide the rest
                log.warning("Freigabe %s in %s nicht lesbar", item_id, record.run_id)
        self._cache[record.run_id] = _Cached(key, items)
        return items

    def _build(
        self, record: RunRecord, item_id: str, lines: _Lines, alive: bool, owned: bool
    ) -> ApprovalItem:
        meta = record.meta
        assert meta is not None
        created = float(lines.opened.get("ts") or meta.created)
        common: dict[str, Any] = {
            "id": item_id,
            "created_at": created,
            "gui_url": self.state.gui_url(f"/freigaben/{item_id}"),
            "run": ApprovalRunRef(
                id=record.run_id,
                name=meta.name or f"{meta.skill or 'Run'} · {meta.project}",
                project=meta.project,
                gui_url=self.state.run_url(record.run_id),
            ),
            "count": lines.count,
            **self._state_fields(lines, alive, owned),
        }
        common["terminal"] = common["status"] is ApprovalStatus.OPEN and not owned
        start = lines.opened.get(RUN_START_KEY)
        if isinstance(start, Mapping):
            return self._run_start(lines, start, common)
        request = lines.opened.get("request")
        if isinstance(request, Mapping):
            return self._permission(record, lines, request, common)
        question = item_question(lines.opened)
        if question is not None:
            return self._question(record, lines, question, common)
        return self._escalation(record, lines, common)

    @staticmethod
    def _state_fields(lines: _Lines, alive: bool, owned: bool) -> dict[str, Any]:
        closed = lines.closed
        event = closed.get("event") if closed is not None else None
        if closed is None:
            if alive:
                return {"status": ApprovalStatus.OPEN}
            return {
                "status": ApprovalStatus.OBSOLETE,
                "status_text": f"Nicht mehr nötig: {NOT_RUNNING}",
            }
        ts = float(closed.get("ts") or 0.0)
        if event == "expired":
            return {
                "status": ApprovalStatus.EXPIRED,
                "deadline": ts,
                "status_text": f"abgelaufen, als abgelehnt gewertet ({_clock(ts)})",
            }
        if event == "obsolete":
            note = closed.get("note") or NOT_RUNNING
            return {"status": ApprovalStatus.OBSOLETE, "status_text": f"Nicht mehr nötig: {note}"}
        decision = _decision_info(closed)
        if lines.revoked is not None:
            at = float(lines.revoked.get("ts") or ts)
            return {
                "status": ApprovalStatus.REVOKED,
                "decision": decision,
                "status_text": f"zurückgezogen um {_clock(at)}",
            }
        revocable = (
            owned
            and decision is not None
            and decision.action in (DecisionAction.ALLOW, DecisionAction.DENY)
            and (
                decision.decided_by is DecidedBy.GATEKEEPER
                or (
                    decision.decided_by is DecidedBy.USER
                    and decision.scope in (DecisionScope.AGENT, DecisionScope.RUN)
                )
            )
        )
        return {"status": ApprovalStatus.DECIDED, "decision": decision, "revocable": revocable}

    def _agent_ref(self, record: RunRecord, request: Mapping[str, Any]) -> ApprovalAgentRef:
        agent_id = str(request.get("agent_id"))
        key = (record.run_id, agent_id)
        ref = self._agents.get(key)
        if ref is not None:
            return ref
        file: AgentFile | None
        try:
            file = load_agent_file(record.run_dir, agent_id)
        except (OSError, ValueError):
            file = None
        if file is not None:
            config = file.config
            provider, model = config.provider, config.model
            label = role_label(config.role, config.base_id)
        else:
            provider = Provider(request.get("provider") or Provider.CLAUDE)
            model, label = "unbekannt", role_label(request.get("role"), agent_id)
        ref = ApprovalAgentRef(
            id=agent_id,
            role_label=label,
            model=model,
            model_label=model_label(model),
            provider=provider,
            vendor=vendor_of(provider),
        )
        if file is not None:
            self._agents[key] = ref
        return ref

    def _permission(
        self,
        record: RunRecord,
        lines: _Lines,
        request: Mapping[str, Any],
        common: dict[str, Any],
    ) -> ApprovalItem:
        agent = self._agent_ref(record, request)
        assessment = lines.opened.get("decision")
        assessment = assessment if isinstance(assessment, Mapping) else {}
        risk = assessment.get("risk")
        categories = [RequestCategory(c) for c in request.get("categories") or []]
        status = common["status"]
        if status is ApprovalStatus.OPEN:
            common["deadline"] = common["created_at"] + self._timeout(record)
        urls = [*(request.get("urls") or []), *(request.get("hosts") or [])]
        reason = assessment.get("reason") or "Braucht eine Freigabe."
        rule = self._rule(record.run_id, common["id"])
        label, effects = _rule_text(rule) if rule is not None else (None, [])
        return ApprovalItem(
            type=ApprovalType.PERMISSION,
            title=item_title(lines.opened),
            agent=agent,
            tool=request.get("tool"),
            command=request.get("command") or None,
            path=", ".join(request.get("paths") or []) or None,
            url=", ".join(urls) or None,
            work_dir=request.get("work_dir"),
            reason=request.get("reason"),
            risk=RiskInfo.model_validate(risk) if isinstance(risk, Mapping) else _UNKNOWN_RISK,
            categories=categories,
            why=f"{reason} Feste Regeln entscheiden das nicht allein, darum fragt arr dich.",
            consequences=_consequences(request, agent.id),
            content_to=[agent.vendor] if RequestCategory.READ_OUTSIDE in categories else [],
            rule_label=label,
            rule_effects=effects,
            also_hosts=_package_hosts(request),
            no_sandbox=is_codex_escalation(request),
            **common,
        )

    def _question(
        self,
        record: RunRecord,
        lines: _Lines,
        question: Mapping[str, Any],
        common: dict[str, Any],
    ) -> ApprovalItem:
        """An item „Frage“: who asks, in which channel, the text and the answer (L16)."""
        agent = self._agent_ref(record, {"agent_id": question.get("agent_id")})
        decision = common.get("decision")
        answer = decision.comment if isinstance(decision, DecisionInfo) else None
        if common["status"] is ApprovalStatus.OBSOLETE and lines.closed is None:
            common["status_text"] = f"Nicht mehr nötig: {QUESTION_CLOSED}"
        msg_id = question.get("msg_id")
        return ApprovalItem(
            type=ApprovalType.QUESTION,
            title=item_title(lines.opened),
            agent=agent,
            question_text=str(question.get("text") or "") or "(ohne Text)",
            channel=str(question.get("channel") or "") or None,
            msg_id=msg_id if isinstance(msg_id, int) else None,
            answer=answer,
            **common,
        )

    @staticmethod
    def _run_start(lines: _Lines, start: Mapping[str, Any], common: dict[str, Any]) -> ApprovalItem:
        """A run start of Claude Code waiting for the user (W4-01, docs/08 §5.2)."""
        run = common["run"]
        skill = start.get("skill")
        what = f"„{skill}“" if skill else "einen Run ohne Skill"
        decider = start.get("decider")
        return ApprovalItem(
            type=ApprovalType.RUN_START,
            title=f"Claude Code möchte {what} in {run.project} starten",
            requested_by=Starter.USER_AGENT,
            skill=skill,
            topology=start.get("topology"),
            params=dict(start.get("params") or {}),
            task_md=start.get("task_md"),
            agents=[RunStartAgent.model_validate(a) for a in start.get("agents") or []],
            channels=[RunStartChannel.model_validate(c) for c in start.get("channels") or []],
            limits=LimitsInfo.model_validate(start["limits"]) if start.get("limits") else None,
            decider=Decider(decider) if decider else None,
            estimate=Estimate.model_validate(start["estimate"]) if start.get("estimate") else None,
            capacity=[CapacityInfo.model_validate(c) for c in start.get("capacity") or []],
            deviations=[Deviation.model_validate(d) for d in start.get("deviations") or []],
            standard_notes=[str(n) for n in start.get("standard_notes") or []],
            **common,
        )

    def _rule(self, run_id: str, item_id: str) -> AlwaysRule | None:
        """The rule „immer“ would make; only a run of this service can make one."""
        inbox = live_inbox(self.state, run_id)
        if inbox is None:
            return None
        request = inbox.get(item_id).request
        return inbox.engine.rule_for(request) if request is not None else None

    @staticmethod
    def _timeout(record: RunRecord) -> float:
        resolved = record.load_resolved()
        return resolved.approvals.timeout.total_seconds() if resolved is not None else 20 * 60.0

    def _escalation(self, record: RunRecord, lines: _Lines, common: dict[str, Any]) -> ApprovalItem:
        logged = [e for e in record.log.entries if e.get("kind") == "escalation"]
        data = logged[lines.number].get("data") if lines.number < len(logged) else None
        data = data if isinstance(data, Mapping) else {}
        stage = str(data.get("stage") or "") or None
        keys = [EscalationOption(o) for o in data.get("options") or list(EscalationOption)]
        peek = record.peek()
        attempts: list[Attempt] = []
        ratings: list[LastRating] = []
        result_id = None
        worker = "Der Agent"
        if common["status"] is ApprovalStatus.OPEN:
            resolved = record.load_resolved()
            for info in stage_infos(record, resolved):
                if info.key == stage:
                    attempts = info.attempts
            if resolved is not None:
                worker = next((s.agent for s in resolved.stages if s.name == stage), worker)
            of_stage = [r for r in run_results(record, resolved).items if r.stage == stage]
            if of_stage:
                result_id = of_stage[-1].id
                ratings = [
                    LastRating(rater=r.rater, score=r.score, comment=r.comment)
                    for r in of_stage[-1].ratings
                ]
        return ApprovalItem(
            type=ApprovalType.ESCALATION,
            title=item_title(lines.opened, stage=stage, attempts=len(attempts)),
            topology=peek.topology if peek is not None else TopologyKind.WATERFALL,
            stage=stage,
            reason=data.get("summary") or lines.opened.get("note"),
            attempts=attempts,
            last_ratings=ratings,
            options=_option_infos(keys, worker, stage),
            result_id=result_id,
            **common,
        )


def _option_infos(
    keys: Iterable[EscalationOption], worker: str, stage: str | None
) -> list[EscalationOptionInfo]:
    texts = {
        EscalationOption.RETRY: (
            "Noch ein Versuch",
            f"Die Nacharbeit wird um 1 erhöht. {worker} überarbeitet die Stufe.",
        ),
        EscalationOption.CONTINUE: (
            "Trotzdem weiter",
            f"Der Run geht mit dem besten Versuch von „{stage}“ weiter.",
        ),
        EscalationOption.END: (
            "Run beenden",
            "Die Zusammenfassung wird erstellt, das Protokoll bleibt vollständig.",
        ),
    }
    return [EscalationOptionInfo(key=k, label=texts[k][0], help=texts[k][1]) for k in keys]


# --------------------------------------------------------------------------------------------
# The area
# --------------------------------------------------------------------------------------------


def store_always_rule(workfolder: Path, rule: AlwaysRule) -> None:
    """Append a new „Immer“ rule to `permissions.always` of `arr.yaml` (docs/07 §3 ④)."""
    path = workfolder / "arr.yaml"
    yaml = YAML(typ="rt")
    data = yaml.load(path.read_text("utf-8")) or {}
    permissions = data.setdefault("permissions", {})
    permissions.setdefault("always", []).append(rule.model_dump(mode="json", exclude_defaults=True))
    temp = path.with_suffix(".yaml.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)
    os.replace(temp, path)


class ApprovalsArea:
    """Inbox methods of `ApiClient` and the decider of the runs of the service."""

    def __init__(self, state: ServiceState) -> None:
        self.state = state
        self.decider = GuiDecider()
        self.reader = ApprovalReader(state)
        self._rule_stored = True
        self.run_starts: Any = None
        """`RunStartsArea` (W4-01), set by `RealApi`; decides items `run_start`."""
        state.store.item_lines = self._own_lines

    def _own_lines(self, run_id: str) -> list[dict[str, Any]] | None:
        """Item lines of a run of this service from memory: its run start (W4-01), then the
        lines of its inbox; None for other runs (they are read from the file)."""
        starts = self.run_starts.lines_of(run_id) if self.run_starts is not None else None
        inbox = own_lines(self.state, run_id)
        if starts is None:
            return inbox
        return [*starts, *(inbox or [])]

    def _fresh_items(self, record: RunRecord) -> dict[str, ApprovalItem]:
        if record.status.is_active:
            record.refresh_meta()
        record.approvals.refresh()
        return self.reader.items(record)

    async def list_approvals(
        self,
        status: Iterable[ApprovalStatus] | None = None,
        run_id: str | None = None,
        type: ApprovalType | None = None,  # noqa: A002 - mirrors the REST parameter
        since: float | None = None,
    ) -> list[ApprovalItem]:
        """Inbox items of all runs; open ones deadline first then oldest, others newest first."""
        wanted = set(status) if status is not None else None
        found = []
        for record in self.state.store.scan(refresh=False):
            if run_id is not None and record.run_id != run_id:
                continue
            for item in self._fresh_items(record).values():
                when = item.decision.decided_at if item.decision else item.created_at
                if (
                    (wanted is None or item.status in wanted)
                    and (type is None or item.type is type)
                    and (since is None or when >= since)
                ):
                    found.append(item)
        waiting = sorted(
            (i for i in found if i.status is ApprovalStatus.OPEN),
            key=lambda i: (i.deadline is None, i.deadline or 0.0, i.created_at),
        )
        rest = sorted(
            (i for i in found if i.status is not ApprovalStatus.OPEN),
            key=lambda i: i.decision.decided_at if i.decision else (i.deadline or i.created_at),
            reverse=True,
        )
        return waiting + rest

    async def get_approval(self, approval_id: str) -> ApprovalItem:
        for record in self.state.store.scan(refresh=False):
            item = self._fresh_items(record).get(approval_id)
            if item is not None:
                return item
        raise not_found("Freigabe", approval_id)

    def _inbox(self, run_id: str) -> ApprovalInbox | None:
        """The inbox of a run this service executes (None for runs of `arr run`)."""
        inbox = live_inbox(self.state, run_id)
        if inbox is not None:
            inbox.on_rule = self._keep_rule
        return inbox

    def _keep_rule(self, rule: AlwaysRule) -> None:
        """A new „Immer“ rule holds for all runs: `arr.yaml`, and the running ones at once."""
        try:
            store_always_rule(self.state.workfolder, rule)
            self.state.reload_settings()
        except (OSError, ValueError, TypeError, AttributeError, ArrError, YAMLError):
            log.exception("Feste Regel konnte nicht in arr.yaml geschrieben werden")
            self._rule_stored = False
        for handle in self.state.registry.active():
            inbox = handle.manager.inbox if handle.manager is not None else None
            if inbox is not None and rule not in inbox.engine.rules.always:
                inbox.engine.rules.always.append(rule)

    async def decide_approval(self, approval_id: str, request: DecisionRequest) -> DecisionResponse:
        """Answer an open item with a decision of the user (GUI only, E15; docs/07 §6)."""
        item = await self.get_approval(approval_id)
        if item.status is not ApprovalStatus.OPEN:
            raise ApiException.of(
                ErrorCode.CONFLICT,
                item.status_text or "Diese Freigabe ist schon entschieden oder nicht mehr nötig.",
                gui_url=item.gui_url,
            )
        if item.type is ApprovalType.RUN_START:
            if self.run_starts is None:
                raise not_allowed(NOT_RUNNING)
            return await self.run_starts.decide(item, request)
        if request.action not in _ACTIONS.get(item.type, set()):
            raise ApiException.of(
                ErrorCode.INVALID, f"„{request.action}“ passt nicht zu dieser Freigabe."
            )
        if item.type is not ApprovalType.PERMISSION and request.scope is not DecisionScope.ONCE:
            raise ApiException.of(ErrorCode.INVALID, "Eskalationen gelten einmalig (E15).")
        if item.no_sandbox and request.scope is not DecisionScope.ONCE:
            raise not_allowed(
                "Diese Anfrage bekommt erhöhte Rechte. Du kannst sie nur einmalig erlauben."
            )
        if request.scope is DecisionScope.ALWAYS and item.rule_label is None:
            raise not_allowed(NO_RULE)
        inbox = self._inbox(item.run.id)
        if inbox is None:
            raise not_allowed(TERMINAL_HINT)
        room = self._answer_room(item) if item.type is ApprovalType.QUESTION else None
        decision = _decision(item, request)
        self._rule_stored = True
        if room is not None:  # into the channel first: if that fails, the item stays open (B6)
            _post_answer(room, item, decision.comment or "")
        try:
            inbox.resolve(approval_id, decision)
        except ArrError as exc:
            still_open = any(i.id == approval_id for i in inbox.pending())
            if room is not None and inbox.get(approval_id).status is ApprovalStatus.DECIDED:
                pass  # the orchestrator saw the answer in the channel first: answered
            else:
                code = ErrorCode.INVALID if still_open else ErrorCode.CONFLICT
                raise ApiException.of(code, exc.message) from exc
        record = self.state.store.get(item.run.id)
        decided = self._fresh_items(record).get(approval_id) if record is not None else None
        if decided is None:
            raise not_found("Freigabe", approval_id)
        message = _message(decided, request)
        if not self._rule_stored:
            message = f"{message}. {RULE_NOT_STORED}"
        return DecisionResponse(item=decided, run_id=item.run.id, message=message)

    def _answer_room(self, item: ApprovalItem) -> RoomStore:
        """The room an answer to a question goes into (L16); only of a running run."""
        handle = self.state.registry.get(item.run.id)
        room = handle.manager.room if handle is not None and handle.manager is not None else None
        if room is None or item.agent is None or item.channel is None:
            raise not_allowed(NOT_RUNNING)
        return room

    async def revoke_approval(self, approval_id: str) -> ApprovalItem:
        """Withdraw a decision of a running run; later equal requests ask again (docs/07 §4)."""
        item = await self.get_approval(approval_id)
        inbox = self._inbox(item.run.id)
        if not item.revocable or inbox is None:
            raise not_allowed(
                "Diese Entscheidung lässt sich nicht zurückziehen. Zurückziehen geht bei "
                "Entscheidungen mit Reichweite Agent oder Run, solange der Run läuft."
            )
        try:
            inbox.revoke(approval_id)
        except ArrError as exc:
            raise ApiException.of(ErrorCode.CONFLICT, exc.message) from exc
        record = self.state.store.get(item.run.id)
        revoked = self._fresh_items(record).get(approval_id) if record is not None else None
        if revoked is None:
            raise not_found("Freigabe", approval_id)
        return revoked


def _decision(item: ApprovalItem, request: DecisionRequest) -> Decision:
    comment = (request.comment or "").strip() or None
    if request.action is DecisionAction.ANSWER:
        reason = "In der Inbox beantwortet."
    elif request.action is DecisionAction.OPTION:
        reason = "In der Oberfläche gewählt."
    elif request.action is DecisionAction.ALLOW:
        reason = "In der Oberfläche erlaubt."
    else:
        reason = f"Abgelehnt: {comment}" if comment else DENY_REASON
    return Decision(
        action=request.action,
        scope=request.scope,
        decided_by=DecidedBy.USER,
        reason=reason,
        comment=comment,
        option=request.option_key,
    )


def _post_answer(room: RoomStore, item: ApprovalItem, text: str) -> None:
    """The user's answer from the inbox as a message in the channel: `answer` with `reply_to` on
    the question and to the asking agent, so it reaches it (L16)."""
    assert item.agent is not None and item.channel is not None
    try:
        room.post(
            item.channel,
            text,
            sender="user",
            kind=MessageKind.ANSWER,
            to=[item.agent.id],
            reply_to=item.msg_id,
            via=MessageVia.GUI,
        )
    except ArrError as exc:
        log.warning("Antwort auf %s nicht in den Kanal gestellt: %s", item.id, exc.message)
        raise ApiException.of(ErrorCode.INVALID, exc.message) from exc


def _message(item: ApprovalItem, request: DecisionRequest) -> str:
    """Text of the toast after a decision (docs/08 §5.2 „abgeschlossen“)."""
    decision = item.decision
    if item.type is ApprovalType.QUESTION:
        who = item.agent.id if item.agent else "den Agenten"
        return f"Antwort an {who} gesendet: {item.run.name}"
    if item.type is ApprovalType.ESCALATION:
        label = next((o.label for o in item.options if o.key is request.option_key), "")
        return f"{label}: {item.run.name}"
    who = item.agent.id if item.agent else "Agent"
    target = item.rule_label or item.command or item.title
    more = f" · {item.count} gleiche Anfragen" if item.count > 1 else ""
    if request.action is DecisionAction.DENY:
        return f"Abgelehnt: {who} · {target}{more}"
    scope = decision.scope if decision is not None else request.scope
    words = {**_SCOPE_WORDS, DecisionScope.AGENT: f"für {who} in diesem Run"}
    text = f"Erlaubt ({words[scope]}): {who} · {target}{more}"
    if scope is not request.scope:
        text += f". „{words[request.scope]}“ ging hier nicht, die Entscheidung gilt {words[scope]}."
    return text
