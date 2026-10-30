"""Area `approvals`: the approval inbox of the service (W3-03, docs/07 §6, docs/08 §5.2).

Reading: `ApprovalReader` builds the `ApprovalItem`s of a run from its `approvals.jsonl`, the
same way for runs of this service and of `arr run`. Only lines with an `item_id` are items, so
`allow` of fixed rules (one line per tool call) never shows. A run that is not running any
more has no open items: what is left open counts as obsolete.

Deciding: the only way to a granted item is `decide_approval`, which the GUI calls after a
click behind the guard of W3-01; it puts a decision of `user` into the `ApprovalInbox` of the
run's `RunManager`. The inbox wakes the waiting agent. `GuiDecider` only keeps the item open
for that; without a decision it expires in the inbox (counts as denied). Runs of `arr run` are
decided in their terminal, here they are read-only.
"""

import logging
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from arr.errors import ArrError
from arr.models import Provider, TopologyKind
from arr.permissions.inbox import ApprovalInbox, install_hosts
from arr.permissions.policy import AlwaysRule, is_codex_escalation
from arr.permissions.types import (
    ApprovalItem as CoreApprovalItem,
)
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
from arr.runs.prepare import AgentFile, load_agent_file
from arr.service.api_models import (
    ApiException,
    ApprovalAgentRef,
    ApprovalItem,
    ApprovalRunRef,
    Attempt,
    Consequences,
    DecisionInfo,
    DecisionRequest,
    DecisionResponse,
    ErrorCode,
    EscalationOptionInfo,
    LastRating,
    RiskInfo,
    RiskLevel,
)
from arr.service.real_api.base import ServiceState, not_allowed, not_found
from arr.service.real_api.rundir import (
    RunRecord,
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
_UNKNOWN_RISK = RiskInfo(level=RiskLevel.MEDIUM, text="Das Risiko ist nicht bekannt.")
_ACTIONS = {
    ApprovalType.PERMISSION: {DecisionAction.ALLOW, DecisionAction.DENY},
    ApprovalType.ESCALATION: {DecisionAction.OPTION},
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
        if not item_id:
            continue
        event = entry.get("event")
        if event == "open":
            if item_id not in items:
                items[item_id] = _Lines(entry, number=escalations)
                escalations += entry.get("request") is None
        elif item_id not in items:
            continue
        elif event == "decision":
            items[item_id].count += 1
        elif event in ("decided", "expired", "obsolete"):
            items[item_id].closed = entry
        elif event == "revoked":
            items[item_id].revoked = entry
    return items


def _rule_label(request: Mapping[str, Any]) -> str | None:
    hosts = request.get("hosts") or []
    if hosts:
        return "Netzwerk zu " + ", ".join(hosts)
    paths = request.get("paths") or []
    if paths:
        return "Lesen in " + ", ".join(paths)
    return request.get("command")


def _install_note(request: Mapping[str, Any]) -> str:
    """„ und darf dafür pypi.org … erreichen“ for what `ApprovalInbox` lets an install reach."""
    try:
        hosts = install_hosts(PermissionRequest.model_validate(request))
    except ValueError:
        return ""
    if not hosts:
        return ""
    return f" und darf dafür {' und '.join(sorted(hosts, reverse=True))} erreichen"


def _consequences(request: Mapping[str, Any], agent: str) -> Consequences:
    categories = request.get("categories") or []
    if is_codex_escalation(request):
        allow = (
            "Der Befehl läuft einmal ohne Sandbox, also ohne Schutz für Dateien und Netz. "
            "Gleiche Befehle fragen wieder."
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
    return DecisionInfo(
        action=DecisionAction(data.get("action")),
        scope=DecisionScope(data.get("scope", DecisionScope.ONCE)),
        option_key=data.get("option"),
        comment=data.get("comment"),
        decided_by=decided_by,
        decided_at=float(data.get("decided_at") or entry.get("ts") or 0.0),  # type: ignore[union-attr]
        rationale=None if decided_by is DecidedBy.USER else data.get("reason") or None,
    )


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


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
        entries = record.approvals.entries
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
            except (ArrError, ValueError):  # a damaged line must not hide the rest
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
        request = lines.opened.get("request")
        if isinstance(request, Mapping):
            return self._permission(record, lines, request, common)
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
            rule_label=_rule_label(request),
            no_sandbox=is_codex_escalation(request),
            **common,
        )

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
        handle = self.state.registry.get(run_id)
        if handle is None or handle.done or handle.manager is None:
            return None
        inbox = handle.manager.inbox
        if inbox is not None:
            inbox.on_rule = self._keep_rule
        return inbox

    def _keep_rule(self, rule: AlwaysRule) -> None:
        """A new „Immer“ rule holds for all runs: `arr.yaml`, and the running ones at once."""
        try:
            store_always_rule(self.state.workfolder, rule)
            self.state.reload_settings()
        except (OSError, ValueError, ArrError):
            log.exception("Feste Regel konnte nicht in arr.yaml geschrieben werden")
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
        if request.action not in _ACTIONS.get(item.type, set()):
            raise ApiException.of(
                ErrorCode.INVALID, f"„{request.action}“ passt nicht zu dieser Freigabe."
            )
        if item.type is not ApprovalType.PERMISSION and request.scope is not DecisionScope.ONCE:
            raise ApiException.of(ErrorCode.INVALID, "Eskalationen gelten einmalig (E15).")
        if item.no_sandbox and request.scope is not DecisionScope.ONCE:
            raise not_allowed(
                "Diese Anfrage läuft ohne Sandbox. Du kannst sie nur einmalig erlauben."
            )
        inbox = self._inbox(item.run.id)
        if inbox is None:
            raise not_allowed(TERMINAL_HINT)
        decision = _decision(item, request)
        try:
            inbox.resolve(approval_id, decision)
        except ArrError as exc:
            still_open = any(i.id == approval_id for i in inbox.pending())
            code = ErrorCode.INVALID if still_open else ErrorCode.CONFLICT
            raise ApiException.of(code, exc.message) from exc
        record = self.state.store.get(item.run.id)
        decided = self._fresh_items(record).get(approval_id) if record is not None else None
        if decided is None:
            raise not_found("Freigabe", approval_id)
        return DecisionResponse(
            item=decided, run_id=item.run.id, message=_message(decided, request)
        )

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
    if request.action is DecisionAction.OPTION:
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


def _message(item: ApprovalItem, request: DecisionRequest) -> str:
    """Text of the toast after a decision (docs/08 §5.2 „abgeschlossen“)."""
    decision = item.decision
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
