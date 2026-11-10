"""Run starts of Claude Code that wait for the user's approval (W4-01; E15, docs/07 §5,
docs/04 §2, docs/08 §5.2 „Run-Start“, §5.4 „Anpassen“).

`start_run` over MCP (`arr.service.user_api`) never starts a run by itself. `request` plans it
like „Neuer Run“ (an invalid skill is refused right here, W4-02), reserves its folder in status
`awaiting_approval` with the run's lock held (so the run counts as alive and no other process
takes it), and writes an item `run_start` into `approvals.jsonl`. The item carries what the
card shows: project, skill, task, agents with provider, model and profile, channels, limits,
decider, the estimate of W4-02, the free capacity and the deviations from your standards. The
plan itself stays in memory: only a decision of the user in the GUI (`decide`, reached only
through `ApprovalsArea.decide_approval` behind the guard) lets the run go on.

- **Freigeben** (`allow`): the run is queued and starts with the reserved folder and the held
  lock, waiting for capacity like any other run.
- **Anpassen** (`adjust`): „Neuer Run“ (`?von=<item>`) sends your changes as `config` (paths
  `agents.<id>.tools.web`, `limits.<key>`, `decider`, …). They go on top of the request of
  Claude Code, the run ID stays, the decision is logged as `adjust` with `changes`, and
  `run_status`/`wait_run` report `adjusted_by_user: true` with the changes.
- **Ablehnen** (`deny`): the run ends `stopped`; your message goes to Claude Code.

Run starts have no deadline. A waiting run start lives in this process: when the service stops
(or the workfolder changes) it ends `stopped` with a note, and at the next start a folder left
`awaiting_approval` without a lock does the same (`recover`).

Deviations (`deviations`) compare the requested run with the same skill, project and task
without `config` and `overrides`, and the limits with `arr.yaml` over `BUILTIN_LIMITS` (a
`group.yaml` can switch a limit off, Codeplan §8 W1-01.1). Rights that grow are `sensitive`:
web, extra read folders, another working-folder mode, another auth profile, more agents, a
weaker decider; limits above the standard are listed as not sensitive.
"""

import asyncio
import contextlib
import copy
import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arr.errors import ArrError
from arr.models import (
    BUILTIN_LIMITS,
    AgentConfig,
    Decider,
    Limits,
    ResolvedRun,
    RunStatus,
    Starter,
    WorkfolderSettings,
    WorkspaceMode,
)
from arr.permissions.inbox import item_id_prefix
from arr.permissions.policy import _redact_data
from arr.permissions.types import (
    ApprovalStatus,
    Change,
    DecidedBy,
    Decision,
    DecisionAction,
    DecisionScope,
)
from arr.runs.inspect import RunLock, run_lock_held
from arr.runs.launch import Launch
from arr.runs.manager import set_run_status
from arr.runs.orchestrator import OrchestratorLog
from arr.runs.prepare import load_run_meta
from arr.service.api_models import (
    ApiException,
    ApprovalItem,
    CapacityInfo,
    ChangeInfo,
    DecisionRequest,
    DecisionResponse,
    Deviation,
    ErrorCode,
    LimitsInfo,
    ProviderShare,
    RunStartAgent,
    RunStartChannel,
    StartRunResponse,
    Vendor,
)

log = logging.getLogger("arr.service.run_starts")

RUN_START_KEY = "run_start"
"""Key of the card data in an `open` line of `approvals.jsonl`, and the mark of its later lines."""
VIA = "claude_code"
APPROVALS_FILE = "approvals.jsonl"
DENIED = "Run-Start von dir abgelehnt."
STOPPED_WAITING = (
    "arr wurde beendet, während der Run-Start auf deine Freigabe wartete. Claude Code kann den "
    "Run neu anfragen."
)
WITHDRAWN = "Der Run-Start wurde vor der Freigabe gestoppt."
NOTHING_CHANGED = "Du hast nichts geändert. Nimm „Freigeben“, wenn der Run so starten soll."
DECIDER_TEXT = {
    Decider.USER.value: "immer du",
    Decider.GATEKEEPER_THEN_USER.value: "Aufsichts-Agent, bei Zweifel du",
    Decider.GATEKEEPER.value: "nur Aufsichts-Agent",
}
_DECIDER_STRENGTH = {
    Decider.USER.value: 2,
    Decider.GATEKEEPER_THEN_USER.value: 1,
    Decider.GATEKEEPER.value: 0,
}
_LIMIT_KEYS = (
    ("wall_time_s", "Laufzeit"),
    ("turns_per_agent", "Turns pro Agent"),
    ("budget_usd", "Budget"),
)
_WORKSPACE_TEXT = {
    WorkspaceMode.COPY: "eigene Kopie",
    WorkspaceMode.SHARED: "geteilte Kopie",
    WorkspaceMode.EMPTY: "leer",
}
ADJUST_PATHS = ("task_md", "decider")
"""Paths of `config` beside `agents.<id>.(tools.web|read_dirs|model)`, `limits.<key>`,
`params.<name>` (what „Neuer Run“ can change, `neuer_run._changes`)."""


# --------------------------------------------------------------------------------------------
# Texts
# --------------------------------------------------------------------------------------------


def _num(value: float | None) -> str:
    if value is None:
        return "–"
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}".replace(".", ",")


def limit_text(key: str, value: float | None) -> str:
    """A limit as the GUI writes it: „6 h“, „90 min“, „60“, „30 $“; „aus“ without a limit."""
    if value is None:
        return "aus"
    if key == "wall_time_s":
        return f"{_num(value / 3600)} h" if value % 1800 == 0 else f"{int(value // 60)} min"
    if key == "budget_usd":
        return f"{_num(value)} $"
    return _num(value)


def change_label(path: str, old: Any, new: Any) -> str:
    """German label of one change, e.g. „Web judge-1: an → aus“ (docs/08 §5.0 `changes`)."""
    parts = path.split(".")

    def dirs(value: Any) -> str:
        return ", ".join(value) if value else "keiner"

    if parts[0] == "agents" and parts[-1] == "web":
        return f"Web {parts[1]}: {'an' if old else 'aus'} → {'an' if new else 'aus'}"
    if parts[0] == "agents" and parts[-1] == "read_dirs":
        return f"Lese-Ordner {parts[1]}: {dirs(old)} → {dirs(new)}"
    if parts[0] == "agents" and parts[-1] == "model":
        return f"Modell {parts[1]}: {old} → {new}"
    if parts[0] == "limits" and len(parts) == 2:
        label = dict(_LIMIT_KEYS).get(parts[1], parts[1])
        old_value = old if isinstance(old, int | float) else None
        new_value = new if isinstance(new, int | float) else None
        return f"{label}: {limit_text(parts[1], old_value)} → {limit_text(parts[1], new_value)}"
    if parts[0] == "decider":
        return (
            f"Entscheider: {DECIDER_TEXT.get(str(old), str(old))} → "
            f"{DECIDER_TEXT.get(str(new), str(new))}"
        )
    if parts[0] == "task_md":
        return "Aufgabe geändert"
    if parts[0] == "params" and len(parts) == 2:
        return f"{parts[1]}: {old} → {new}"
    return f"{path}: {old} → {new}"


def change_infos(changes: Sequence[Mapping[str, Any]]) -> list[ChangeInfo]:
    """`changes` of a logged decision as `ChangeInfo` with the German label."""
    out = []
    for change in changes:
        path = str(change.get("path") or "")
        old, new = change.get("from"), change.get("to")
        out.append(ChangeInfo(path=path, from_=old, to=new, label=change_label(path, old, new)))
    return out


# --------------------------------------------------------------------------------------------
# Deviations from your standards (docs/07 §5)
# --------------------------------------------------------------------------------------------


def _vendor(agent: AgentConfig) -> Vendor:
    from arr.service.real_api.rundir import vendor_of

    return vendor_of(agent.provider)


def _shares(agents: Sequence[AgentConfig]) -> list[ProviderShare]:
    shares: dict[Vendor, list[str]] = {}
    for agent in agents:
        shares.setdefault(_vendor(agent), []).append(agent.id)
    return [ProviderShare(vendor=v, agents=ids) for v, ids in shares.items()]


def standard_limits(settings: WorkfolderSettings) -> dict[str, float | None]:
    """Your standard limits: `arr.yaml` over `BUILTIN_LIMITS`, in seconds, turns and dollars."""
    merged = Limits.model_validate(
        {
            **BUILTIN_LIMITS.model_dump(exclude_unset=False),
            **settings.limits.model_dump(exclude_unset=True),
        }
    )
    return _limits_values(merged)


def _limits_values(limits: Limits) -> dict[str, float | None]:
    wall = limits.wall_time.total_seconds() if limits.wall_time is not None else None
    return {
        "wall_time_s": wall,
        "turns_per_agent": limits.turns_per_agent,
        "budget_usd": limits.budget_usd,
    }


def deviations(
    requested: ResolvedRun, standard: ResolvedRun | None, settings: WorkfolderSettings
) -> list[Deviation]:
    """What the requested run changes against your standards and the skill (docs/07 §5).

    `standard` is the same skill, project and task without `config` and `overrides` (None when
    it does not resolve, then every agent is compared with the provider standards of
    `arr.yaml`).
    """
    out: list[Deviation] = []
    base = {a.id: a for a in standard.agents} if standard is not None else {}
    agents = list(requested.agents)
    std_auth = {p: d.auth for p, d in settings.defaults.items() if d.auth}

    def reference(agent: AgentConfig) -> AgentConfig | None:
        return base.get(agent.id)

    def had(agent: AgentConfig, tool: str) -> bool:
        ref = reference(agent)
        return ref is not None and bool(getattr(ref.tools, tool))

    web = [a for a in agents if a.tools.web and not had(a, "web")]
    if web:
        out.append(
            Deviation(
                path="agents.tools.web",
                label="Web-Zugriff an",
                value_text=f"für {', '.join(a.id for a in web)}",
                default_text="aus",
                default=False,
                value=True,
                sensitive=True,
            )
        )
    shell = [
        a for a in agents if a.tools.shell and reference(a) is not None and not had(a, "shell")
    ]
    if shell:
        out.append(
            Deviation(
                path="agents.tools.shell",
                label="Terminal an",
                value_text=f"für {', '.join(a.id for a in shell)}",
                default_text="aus",
                default=False,
                value=True,
                sensitive=True,
            )
        )
    folders: dict[str, list[AgentConfig]] = {}
    for agent in agents:
        ref = reference(agent)
        known = {str(d) for d in ref.read_dirs} if ref is not None else set()
        for folder in agent.read_dirs:
            if str(folder) not in known:
                folders.setdefault(str(folder), []).append(agent)
    if folders:
        readers: dict[str, AgentConfig] = {}
        parts = []
        for folder, who in folders.items():
            names = "alle Agenten" if len(who) == len(agents) else ", ".join(a.id for a in who)
            parts.append(f"{folder} für {names}")
            readers.update({a.id: a for a in who})
        out.append(
            Deviation(
                path="agents.read_dirs",
                label="Zusätzlicher Lese-Ordner"
                if len(folders) == 1
                else "Zusätzliche Lese-Ordner",
                value_text="; ".join(parts),
                default_text="keiner",
                default=[],
                value=list(folders),
                sensitive=True,
                providers=_shares(list(readers.values())),
            )
        )

    def other_mode(agent: AgentConfig) -> bool:
        ref = reference(agent)
        if ref is None:
            return agent.workspace.mode is WorkspaceMode.SHARED
        return ref.workspace.mode is not agent.workspace.mode

    modes = [a for a in agents if other_mode(a)]
    if modes:
        out.append(
            Deviation(
                path="agents.workspace",
                label="Anderer Arbeitsordner",
                value_text=", ".join(f"{a.id}: {_WORKSPACE_TEXT[a.workspace.mode]}" for a in modes),
                default_text=", ".join(
                    sorted(
                        {
                            _WORKSPACE_TEXT[r.workspace.mode]
                            if (r := reference(a)) is not None
                            else _WORKSPACE_TEXT[WorkspaceMode.COPY]
                            for a in modes
                        }
                    )
                ),
                sensitive=True,
            )
        )
    other = []
    for agent in agents:
        ref = reference(agent)
        expected = ref.auth if ref is not None else std_auth.get(agent.provider)
        if expected and agent.auth != expected:
            other.append((agent, expected))
    if other:
        out.append(
            Deviation(
                path="agents.profile",
                label="Anderes Auth-Profil",
                value_text=", ".join(f"{a.id}: {a.auth}" for a, _ in other),
                default_text=", ".join(sorted({e for _, e in other})),
                sensitive=True,
            )
        )
    if standard is not None:
        extra = [a for a in agents if a.id not in base]
        if extra:
            out.append(
                Deviation(
                    path="agents",
                    label="Zusätzliche Agenten" if len(extra) > 1 else "Zusätzlicher Agent",
                    value_text=", ".join(f"{a.id} ({a.model})" for a in extra),
                    default_text=f"{len(base)} Agenten aus dem Skill",
                    sensitive=True,
                )
            )
    decider = requested.approvals.decider.value
    std_decider = (
        standard.approvals.decider.value
        if standard is not None
        else settings.approvals.decider.value
    )
    if decider != std_decider:
        out.append(
            Deviation(
                path="decider",
                label="Entscheider für Berechtigungsanfragen",
                value_text=DECIDER_TEXT.get(decider, decider),
                default_text=DECIDER_TEXT.get(std_decider, std_decider),
                default=std_decider,
                value=decider,
                sensitive=_DECIDER_STRENGTH.get(decider, 0) < _DECIDER_STRENGTH.get(std_decider, 2),
            )
        )
    std = standard_limits(settings)
    have = _limits_values(requested.limits)
    for key, label in _LIMIT_KEYS:
        value, default = have[key], std[key]
        if default is None:
            continue
        if value is None or value > default:
            out.append(
                Deviation(
                    path=f"limits.{key}",
                    label=f"{label} {limit_text(key, value)}",
                    value_text="",
                    default_text=limit_text(key, default),
                    default=default,
                    value=value,
                    sensitive=False,
                )
            )
    return out


def standard_notes(requested: ResolvedRun, devs: Sequence[Deviation]) -> list[str]:
    """„Wie Standard: …“ lines of the card (docs/08 §5.2), with the vendors of a copy (E19)."""
    notes = []
    copies = [a for a in requested.agents if a.workspace.mode is not WorkspaceMode.EMPTY]
    if copies and not any(d.path == "agents.workspace" for d in devs):
        names = {Vendor.ANTHROPIC: "Anthropic", Vendor.OPENAI: "OpenAI"}
        vendors = " und ".join(names[s.vendor] for s in _shares(copies))
        notes.append(f"Arbeitsordner (Kopie von {requested.project}, Inhalte gehen an {vendors})")
    return notes


# --------------------------------------------------------------------------------------------
# Lines of approvals.jsonl
# --------------------------------------------------------------------------------------------


class RunStartLog:
    """Writes the lines of a run-start item into `approvals.jsonl` and keeps them in memory.

    Like `ApprovalLog`: every string is redacted before writing; the memory copy is what the
    service shows (a line somebody else adds to the file changes nothing, W3-03.1).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._lines: list[dict[str, Any]] = []

    def write(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        data = _redact_data({"ts": time.time(), **entry})
        line = json.dumps(data, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._lines.append(data)
        return data

    def lines(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._lines)


def is_run_start(entry: Mapping[str, Any]) -> bool:
    """True for a line of a run-start item (its card data or its mark)."""
    return entry.get(RUN_START_KEY) is not None


# --------------------------------------------------------------------------------------------
# „Anpassen“: the form of „Neuer Run“ from a card, and its changes
# --------------------------------------------------------------------------------------------


def item_form(item: ApprovalItem, fallback_decider: str = Decider.USER.value) -> dict[str, Any]:
    """The input „Neuer Run“ builds from a run-start card (`neuer_run._init_input`)."""
    limits = item.limits or LimitsInfo()
    return {
        "project": item.run.project,
        "skill": item.skill,
        "name": item.run.name,
        "task_md": item.task_md or "",
        "params": dict(item.params),
        "agents": {
            a.id: {"model": a.model, "web": a.tools.web, "read_dirs": list(a.read_dirs)}
            for a in item.agents
        },
        "limits": {
            "wall_time_s": limits.wall_time_s,
            "turns_per_agent": limits.turns_per_agent,
            "budget_usd": limits.budget_usd,
        },
        "decider": item.decider.value if item.decider else fallback_decider,
    }


def form_changes(original: Mapping[str, Any], form: Mapping[str, Any]) -> dict[str, Any]:
    """Your changes of the form against the card, as `config` of `adjust` (path → value)."""
    out: dict[str, Any] = {}
    now_agents = form.get("agents") or {}
    for aid, was in (original.get("agents") or {}).items():
        now = now_agents.get(aid) or {}
        if "web" in now and bool(now["web"]) != bool(was.get("web")):
            out[f"agents.{aid}.tools.web"] = bool(now["web"])
        if "read_dirs" in now and list(now["read_dirs"] or []) != list(was.get("read_dirs") or []):
            out[f"agents.{aid}.read_dirs"] = list(now["read_dirs"] or [])
        if "model" in now and now["model"] and now["model"] != was.get("model"):
            out[f"agents.{aid}.model"] = now["model"]
    limits = form.get("limits") or {}
    for key, _label in _LIMIT_KEYS:
        if key in limits and limits[key] != (original.get("limits") or {}).get(key):
            out[f"limits.{key}"] = limits[key]
    if form.get("decider") and form["decider"] != original.get("decider"):
        out["decider"] = form["decider"]
    if "task_md" in form and form["task_md"] != original.get("task_md"):
        out["task_md"] = form["task_md"]
    for name, value in (form.get("params") or {}).items():
        if value != (original.get("params") or {}).get(name):
            out[f"params.{name}"] = value
    return out


def check_adjust_paths(config: Mapping[str, Any], agents: Sequence[str]) -> None:
    """Only what „Neuer Run“ can change; anything else is refused (no hidden widening)."""
    for path in config:
        parts = path.split(".")
        ok = (
            path in ADJUST_PATHS
            or (len(parts) == 2 and parts[0] == "limits" and parts[1] in dict(_LIMIT_KEYS))
            or (len(parts) == 2 and parts[0] == "params" and parts[1])
            or (
                parts[0] == "agents"
                and len(parts) >= 3
                and parts[1] in agents
                and ".".join(parts[2:]) in ("tools.web", "read_dirs", "model")
            )
        )
        if not ok:
            raise ApiException.of(
                ErrorCode.INVALID, f"„{path}“ lässt sich beim Anpassen nicht ändern."
            )


def requested_value(item: ApprovalItem, path: str) -> Any:
    """Value of the request at `path`, for the `from` of a change."""
    return dict_path(item_form(item), _form_path(path))


def _form_path(path: str) -> str:
    parts = path.split(".")
    if parts[0] == "agents" and parts[-1] == "web":
        return f"agents.{parts[1]}.web"
    return path


def dict_path(data: Mapping[str, Any], path: str) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return copy.deepcopy(node)


# --------------------------------------------------------------------------------------------
# The area
# --------------------------------------------------------------------------------------------


@dataclass
class PendingStart:
    """A run start waiting for the user (memory only, see the module docstring)."""

    run_id: str
    run_dir: Path
    item_id: str
    spec: dict[str, Any]
    launch: Launch
    lock: RunLock
    log: RunStartLog
    decided: asyncio.Future[Launch | None]
    created: float = field(default_factory=time.time)


Planner = Callable[[Mapping[str, Any], Mapping[str, Any] | None], Launch]
"""`plan(spec, adjust_config)`: a `Launch` from a request of Claude Code (`RunsArea`)."""


class RunStartsArea:
    """Run starts of Claude Code: request, decide, recover (see the module docstring)."""

    def __init__(self, state: Any, runs: Any) -> None:
        self.state = state
        self.runs = runs
        self.pending: dict[str, PendingStart] = {}
        self.logs: dict[str, RunStartLog] = {}
        """Run ID → its run-start lines in memory (also after the decision)."""

    # -- lines for the reader ----------------------------------------------------------------

    def lines_of(self, run_id: str) -> list[dict[str, Any]] | None:
        log_ = self.logs.get(run_id)
        return log_.lines() if log_ is not None else None

    def pending_of(self, run_id: str) -> PendingStart | None:
        return self.pending.get(run_id)

    # -- request (start_run of Claude Code) --------------------------------------------------

    async def request(self, spec: Mapping[str, Any]) -> StartRunResponse:
        """Plan the run, reserve it `awaiting_approval` and open the item (E15)."""
        from arr.runs.launch import reserve_run

        spec = dict(spec)
        workfolder = self.state.workfolder
        launch, card = await asyncio.to_thread(self._plan_with_card, spec, None)
        if self.state.workfolder != workfolder:
            raise ApiException.of(
                ErrorCode.NOT_ALLOWED,
                "Der Workfolder wurde gewechselt, während arr den Run geprüft hat. Frag den Run "
                "erneut an.",
            )
        run_dir = reserve_run(
            launch, starter=Starter.USER_AGENT, status=RunStatus.AWAITING_APPROVAL
        )
        lock = RunLock(run_dir)
        try:
            lock.acquire()
        except ArrError:
            raise ApiException.of(ErrorCode.CONFLICT, "Der Run-Ordner ist belegt.") from None
        run_id = run_dir.name
        item_id = f"{item_id_prefix(run_id)}_0001"
        log_ = RunStartLog(run_dir / APPROVALS_FILE)
        log_.write({"event": "open", "item_id": item_id, RUN_START_KEY: card})
        self.logs[run_id] = log_
        loop = asyncio.get_running_loop()
        pending = PendingStart(
            run_id=run_id,
            run_dir=run_dir,
            item_id=item_id,
            spec=spec,
            launch=launch,
            lock=lock,
            log=log_,
            decided=loop.create_future(),
        )
        self.pending[run_id] = pending
        self.runs.add_waiting(run_id, run_dir, launch, self._wait_then_run(pending))
        log.info("[%s] Run-Start von Claude Code wartet auf Freigabe (%s)", run_id, item_id)
        return StartRunResponse(
            run_id=run_id,
            status=RunStatus.AWAITING_APPROVAL,
            gui_url=self.state.gui_url(f"/freigaben/{item_id}"),
        )

    def _plan_with_card(
        self, spec: Mapping[str, Any], adjust: Mapping[str, Any] | None
    ) -> tuple[Launch, dict[str, Any]]:
        launch = self.runs.plan_request(spec, adjust)
        return launch, self.card(launch, spec, adjust)

    def card(
        self, launch: Launch, spec: Mapping[str, Any], adjust: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """What the card shows, as plain data for the `open` line."""
        from arr.service.real_api.rundir import model_label, role_label, vendor_of
        from arr.service.real_api.runs import estimate_run

        resolved = launch.resolved
        standard = self.runs.standard_of(spec, adjust)
        devs = deviations(resolved, standard, launch.settings)
        limits = _limits_values(resolved.limits)
        agents = [
            RunStartAgent(
                id=a.id,
                role_label=role_label(a.role, a.base_id),
                provider=a.provider,
                vendor=vendor_of(a.provider),
                model=a.model,
                model_label=model_label(a.model),
                profile=a.auth,
                tools={"shell": a.tools.shell, "web": a.tools.web},
                read_dirs=[str(d) for d in a.read_dirs],
                workspace_mode=a.workspace.mode,
                skills=list(a.skills),
            )
            for a in resolved.agents
        ]
        channels = [
            RunStartChannel(
                name=c.name,
                members=list(c.members),
                mode=c.mode,
                anonymous=c.anonymous,
                open_from=c.open_from,
            )
            for c in resolved.groupchats
        ]
        capacity: list[CapacityInfo] = self.runs.capacity(launch)
        task = resolved.task or ""
        return {
            "via": VIA,
            "name": resolved.name,
            "skill": resolved.skill,
            "topology": resolved.topology.kind.value,
            "params": dict(resolved.params),
            "task_md": task,
            "agents": [a.model_dump(mode="json") for a in agents],
            "channels": [c.model_dump(mode="json") for c in channels],
            "limits": LimitsInfo(
                wall_time_s=int(limits["wall_time_s"]) if limits["wall_time_s"] else None,
                turns_per_agent=resolved.limits.turns_per_agent,
                budget_usd=resolved.limits.budget_usd,
            ).model_dump(mode="json"),
            "decider": resolved.approvals.decider.value,
            "estimate": estimate_run(resolved).model_dump(mode="json"),
            "capacity": [c.model_dump(mode="json") for c in capacity],
            "deviations": [d.model_dump(mode="json") for d in devs],
            "standard_notes": standard_notes(resolved, devs),
        }

    # -- the decision (GUI only) -------------------------------------------------------------

    async def decide(self, item: ApprovalItem, request: DecisionRequest) -> DecisionResponse:
        """Freigeben, Anpassen or Ablehnen of an open run start (E15: only from the GUI)."""
        if request.action not in (DecisionAction.ALLOW, DecisionAction.DENY, DecisionAction.ADJUST):
            raise ApiException.of(
                ErrorCode.INVALID, f"„{request.action}“ passt nicht zu einem Run-Start."
            )
        if request.scope is not DecisionScope.ONCE:
            raise ApiException.of(ErrorCode.INVALID, "Run-Starts gelten einmalig (E15).")
        pending = self.pending.get(item.run.id)
        if pending is None or pending.item_id != item.id or pending.decided.done():
            raise ApiException.of(
                ErrorCode.CONFLICT,
                item.status_text or "Dieser Run-Start ist schon entschieden oder nicht mehr nötig.",
                gui_url=item.gui_url,
            )
        comment = (request.comment or "").strip() or None
        launch: Launch | None = pending.launch
        changes: list[Change] = []
        if request.action is DecisionAction.ADJUST:
            config = dict(request.config or {})
            check_adjust_paths(config, [a.id for a in item.agents])
            for path, value in config.items():
                old = requested_value(item, path)
                if old != value:
                    changes.append(Change.model_validate({"path": path, "from": old, "to": value}))
            if not changes:
                raise ApiException.of(ErrorCode.INVALID, NOTHING_CHANGED)
            adjust = {c.path: c.to for c in changes}
            try:
                launch = await asyncio.to_thread(self.runs.plan_request, pending.spec, adjust)
            except ApiException:
                raise
            if pending.decided.done():  # decided in another tab meanwhile
                raise ApiException.of(
                    ErrorCode.CONFLICT,
                    "Dieser Run-Start ist schon entschieden.",
                    gui_url=item.gui_url,
                )
        if request.action is DecisionAction.DENY:
            reason = f"Abgelehnt: {comment}" if comment else DENIED
            launch = None
        elif request.action is DecisionAction.ADJUST:
            reason = "In „Neuer Run“ angepasst und freigegeben."
        else:
            reason = "In der Oberfläche freigegeben."
        decision = Decision(
            action=request.action,
            scope=DecisionScope.ONCE,
            decided_by=DecidedBy.USER,
            reason=reason,
            comment=comment,
            changes=changes,
        )
        pending.log.write(
            {
                "event": "decided",
                "item_id": pending.item_id,
                RUN_START_KEY: True,
                "decision": decision.model_dump(mode="json", by_alias=True, exclude_none=True),
            }
        )
        status = RunStatus.STOPPED if launch is None else RunStatus.QUEUED
        with contextlib.suppress(OSError, ValueError):
            set_run_status(pending.run_dir, status)
            OrchestratorLog(pending.run_dir).write(
                "status",
                status=status.value,
                previous=RunStatus.AWAITING_APPROVAL.value,
                reason=DENIED if launch is None else None,
            )
        pending.decided.set_result(launch)
        self.pending.pop(pending.run_id, None)
        record = self.state.store.get(item.run.id)
        decided = None
        if record is not None:
            record.refresh_meta()
            record.approvals.refresh()
            decided = self.runs.approvals.reader.items(record).get(item.id)
        if decided is None:
            raise ApiException.of(ErrorCode.NOT_FOUND, f"Freigabe „{item.id}“ nicht gefunden.")
        infos = change_infos([c.model_dump(mode="json", by_alias=True) for c in changes])
        if request.action is DecisionAction.DENY:
            message = "Run-Start abgelehnt. Claude Code erhält deine Nachricht."
        elif request.action is DecisionAction.ADJUST:
            message = f"Angepasst und freigegeben: {item.run.name}"
        else:
            message = f"Run freigegeben: {item.run.name}"
        return DecisionResponse(item=decided, run_id=item.run.id, changes=infos, message=message)

    async def _wait_then_run(self, pending: PendingStart) -> None:
        """The task of a waiting run start: wait for the user, then run or end."""
        try:
            launch = await pending.decided
        except asyncio.CancelledError:
            self._end_waiting(pending, STOPPED_WAITING)
            raise
        if launch is None:
            pending.lock.release()
            return
        await self.runs.execute_approved(pending.run_id, launch, pending.lock)

    def _end_waiting(self, pending: PendingStart, note: str) -> None:
        """A waiting start that cannot go on: obsolete item, run `stopped`, lock released."""
        self.pending.pop(pending.run_id, None)
        if not pending.decided.done():
            pending.decided.cancel()
        with contextlib.suppress(OSError, ValueError):
            pending.log.write(
                {"event": "obsolete", "item_id": pending.item_id, RUN_START_KEY: True, "note": note}
            )
            if load_run_meta(pending.run_dir).status is RunStatus.AWAITING_APPROVAL:
                set_run_status(pending.run_dir, RunStatus.STOPPED)
                OrchestratorLog(pending.run_dir).write(
                    "status",
                    status=RunStatus.STOPPED.value,
                    previous=RunStatus.AWAITING_APPROVAL.value,
                    reason=note,
                )
        pending.lock.release()

    async def withdraw(self, run_id: str) -> bool:
        """`control_run(stop)` of a waiting start: Claude Code takes it back."""
        pending = self.pending.get(run_id)
        if pending is None:
            return False
        self._end_waiting(pending, WITHDRAWN)
        return True

    def recover(self) -> list[str]:
        """Folders left `awaiting_approval` without a lock (the service ended): `stopped`."""
        runs_dir = Path(self.state.workfolder) / "runs"
        ended = []
        for meta_path in sorted(runs_dir.glob("*/meta.json")):
            run_dir = meta_path.parent
            try:
                meta = load_run_meta(run_dir)
            except (OSError, ValueError):
                continue
            if meta.status is not RunStatus.AWAITING_APPROVAL or run_dir.name in self.pending:
                continue
            if run_lock_held(run_dir):
                continue
            try:
                entries = [
                    json.loads(line)
                    for line in (run_dir / APPROVALS_FILE).read_text("utf-8").splitlines()
                    if line.strip()
                ]
            except (OSError, ValueError):
                entries = []
            opened = {
                e.get("item_id")
                for e in entries
                if isinstance(e, dict) and e.get("event") == "open" and is_run_start(e)
            }
            closed = {
                e.get("item_id")
                for e in entries
                if isinstance(e, dict) and e.get("event") in ("decided", "expired", "obsolete")
            }
            with contextlib.suppress(OSError, ValueError):
                writer = RunStartLog(run_dir / APPROVALS_FILE)
                for item_id in sorted(i for i in opened - closed if isinstance(i, str)):
                    writer.write(
                        {
                            "event": "obsolete",
                            "item_id": item_id,
                            RUN_START_KEY: True,
                            "note": STOPPED_WAITING,
                        }
                    )
                set_run_status(run_dir, RunStatus.STOPPED)
                OrchestratorLog(run_dir).write(
                    "status",
                    status=RunStatus.STOPPED.value,
                    previous=RunStatus.AWAITING_APPROVAL.value,
                    reason=STOPPED_WAITING,
                )
                ended.append(run_dir.name)
        return ended


def decision_of(entries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The logged decision of the run start among the lines of a run, if any."""
    for entry in entries:
        if is_run_start(entry) and entry.get("event") == "decided":
            data = entry.get("decision")
            if isinstance(data, Mapping):
                return data
    return None


def closing_note(entries: Sequence[Mapping[str, Any]]) -> str | None:
    """Why a run start ended without a decision (obsolete line), if it did."""
    for entry in entries:
        if is_run_start(entry) and entry.get("event") == "obsolete":
            return str(entry.get("note") or "") or None
    return None


def status_of(entries: Sequence[Mapping[str, Any]]) -> ApprovalStatus | None:
    """State of the run-start item among the lines of a run; None without one."""
    opened = closed = False
    for entry in entries:
        if not is_run_start(entry):
            continue
        if entry.get("event") == "open":
            opened = True
        elif entry.get("event") == "decided":
            return ApprovalStatus.DECIDED
        elif entry.get("event") == "obsolete":
            closed = True
    if closed:
        return ApprovalStatus.OBSOLETE
    return ApprovalStatus.OPEN if opened else None


__all__ = [
    "RUN_START_KEY",
    "PendingStart",
    "RunStartLog",
    "RunStartsArea",
    "change_infos",
    "change_label",
    "closing_note",
    "decision_of",
    "deviations",
    "form_changes",
    "is_run_start",
    "item_form",
    "standard_limits",
    "status_of",
]
