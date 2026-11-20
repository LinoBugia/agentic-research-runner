"""Helpers for the tests of W4b-09 (`decider: user` at agents and phases).

Inbox level: the `Env` of `test_gatekeeper` (engine, mandate, `GatekeeperStage` with a fake
model) plus the rule of `arr.permissions.tightening` over hand-made agents and phases, with the
phase of each agent in a `Where` the test changes. Run level: a `phases` run (or an old
topology) with the real `RunManager`, `Orchestrator` and phase runner, whose FakeBackend agents
ask for a permission at the start of a turn (`AskingPolicyBackend`), a fake model for the
supervising agent and a `User` who decides what reaches the user. No model, no CLI.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from phases_kit import FAKE_DEFAULTS, TASK, Log, PolicyBackend, silent, task_files
from phases_kit import write_project_tasks as write_tasks
from run_kit import FAST, make_settings
from test_gatekeeper import Env, FakeModel

from arr.backends.fake import FakeBackend
from arr.models import (
    AgentConfig,
    AuthKind,
    AuthProfile,
    ProfilesFile,
    ResolvedAction,
    ResolvedPhase,
    ResolvedSubmission,
    RunInput,
)
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.mandate import standard_text
from arr.permissions.types import (
    ApprovalItem,
    ApprovalType,
    DecidedBy,
    Decision,
    DecisionAction,
    DecisionScope,
    PermissionRequest,
)
from arr.resolve import resolve_run, validate_model
from arr.room.store import RoomStore
from arr.runs.manager import RunManager
from arr.runs.prepare import PreparedAgent

if TYPE_CHECKING:
    from arr.permissions.types import TightenedBy

PROFILE = "fake-auth"
GATEKEEPER = {"model": "fake-haiku", "mandate": "mandates/standard.md", "auth": PROFILE}

# --------------------------------------------------------------------------------------------
# The rule over hand-made agents and phases
# --------------------------------------------------------------------------------------------


def cfg(agent_id: str, decider: str | None = None) -> AgentConfig:
    return AgentConfig(
        id=agent_id, base_id=agent_id, provider="claude", model="m", auth="max", decider=decider
    )


def phase(name: str, agents: list[str], decider: str | None = None, index: int = 0) -> Any:
    actions = [
        ResolvedAction(
            agent=a, key=a, task=f"tasks/{name}.md", submission=ResolvedSubmission(format="text")
        )
        for a in agents
    ]
    return ResolvedPhase(name=name, index=index, topology="solo", actions=actions, decider=decider)


@dataclass
class Where:
    """The phase and pass each agent entered last, as `Orchestrator.agent_phase` says it;
    `broken` makes the lookup raise, `lookups` counts them."""

    phases: dict[str, tuple[str, int]] = field(default_factory=dict)
    broken: bool = False
    lookups: int = 0

    def __call__(self, agent_id: str) -> tuple[str, int] | None:
        self.lookups += 1
        if self.broken:
            raise RuntimeError("Nachschlagen kaputt")
        return self.phases.get(agent_id)

    def enter(self, agent_id: str, stage: str, attempt: int = 1) -> None:
        self.phases[agent_id] = (stage, attempt)


AGENTS = [cfg("explorer-1"), cfg("codex-1")]
"""The agents of `test_gatekeeper.Env`."""
TIGHT_RUN = [
    phase("exploration", ["explorer-1", "codex-1"]),
    phase("beweis", ["explorer-1"], "user", 1),
]
"""Phases of a run where `beweis` lets the user decide."""


def rule(
    where: Where, agents: list[AgentConfig] | None = None, phases: list[Any] | None = None
) -> Callable[[str], "TightenedBy | None"]:
    """The inbox's callback as the `RunManager` builds it."""
    # imported here: the run helpers below also run on a checkout before W4b-09 (A10)
    from arr.permissions.tightening import tightened_by

    agents = AGENTS if agents is None else agents
    phases = TIGHT_RUN if phases is None else phases
    return lambda agent_id: tightened_by(agents, phases, agent_id, where)


def tight_inbox(
    env: Env,
    model: Any,
    where: Where,
    *,
    decider: Any = None,
    timeout: float = 5,
    agents: list[AgentConfig] | None = None,
    phases: list[Any] | None = None,
    gatekeeper: bool = True,
    **stage: Any,
) -> ApprovalInbox:
    """An inbox of `env` with the rule; `gatekeeper=False` is a run with `decider: user`."""
    return ApprovalInbox(
        env.engine,
        decider=decider,
        timeout=timeout,
        screener=env.stage(model, **stage) if gatekeeper else None,
        tightened=rule(where, agents, phases),
    )


class User:
    """The user's decider: records every item it sees and answers permissions with `action`
    (None: no answer, the item waits); `seen` is set at every new item."""

    def __init__(self, action: DecisionAction | None = DecisionAction.DENY, **kw: Any) -> None:
        self.action = action
        self.kw = kw
        self.items: list[ApprovalItem] = []
        self.seen = asyncio.Event()
        self.calls_at_open: list[int] = []
        self.model: FakeModel | None = None

    async def decide(self, item: ApprovalItem) -> Decision | None:
        self.items.append(item)
        if self.model is not None:
            self.calls_at_open.append(len(self.model.prompts))
        self.seen.set()
        if item.type is not ApprovalType.PERMISSION or self.action is None:
            return None
        return Decision(
            action=self.action,
            decided_by=DecidedBy.USER,
            reason="Vom User entschieden.",
            **self.kw,
        )


async def until(check: Callable[[], bool], steps: int = 2000) -> None:
    """Let the loop run until `check` holds: steps of the loop, never a wall-clock wait."""
    for _ in range(steps):
        if check():
            return
        await asyncio.sleep(0)
    raise AssertionError("Zustand nicht erreicht")


class GatedModel(FakeModel):
    """A fake model that answers only when `release` is set: a screen held open on a hook."""

    def __init__(self, *answers: str | BaseException) -> None:
        super().__init__(*answers)
        self.release = asyncio.Event()
        self.asked = asyncio.Event()

    async def ask(self, system: str, prompt: str) -> Any:
        self.asked.set()
        await self.release.wait()
        return await super().ask(system, prompt)


def lines(env: Env) -> list[dict[str, Any]]:
    """Item lines of `approvals.jsonl`."""
    return [e for e in env.log() if e.get("item_id")]


def calls(env: Env) -> int:
    path = env.run_dir / "gatekeeper.jsonl"
    return len(path.read_text().splitlines()) if path.exists() else 0


# --------------------------------------------------------------------------------------------
# Runs whose agents ask during a turn
# --------------------------------------------------------------------------------------------

Want = Callable[[str, str | None], PermissionRequest | None]
"""Which request an agent makes at the start of a turn in a stage (None: none)."""


@dataclass
class Asked:
    agent: str
    stage: str | None
    phase: tuple[str, int] | None
    decision: Decision


class Asking:
    """Mixin for FakeBackends: at the start of every turn (not a note turn) the agent asks
    the inbox for what `want` names and waits for the decision, like an adapter does."""

    want: Want
    asked: list[Asked]
    manager: Callable[[], RunManager]

    async def _play(self, state: Any, stage: str | None, *, note: bool = False) -> Any:
        request = None if note else self.want(self.config.id, stage)  # type: ignore[attr-defined]
        if request is not None:
            mgr = self.manager()
            assert mgr.inbox is not None and mgr.orchestrator is not None
            request = request.model_copy(update={"run_id": self.run_id})  # type: ignore[attr-defined]
            entered = mgr.orchestrator.agent_phase(request.agent_id)
            decision = await mgr.inbox.handle(request)
            self.asked.append(Asked(request.agent_id, stage, entered, decision))
        async for event in super()._play(state, stage, note=note):  # type: ignore[misc]
            yield event


class AskingPolicyBackend(Asking, PolicyBackend):
    pass


class AskingFakeBackend(Asking, FakeBackend):
    pass


def inside(agent: str) -> PermissionRequest:
    """`curl https://arxiv.org/…`: in the grey zone and inside the standard mandate (runs of
    the tests have no run venv, so an installation is denied by a fixed rule)."""
    return PermissionRequest(
        run_id="-",
        agent_id=agent,
        role="explorer",
        provider="claude",
        tool="Bash",
        command="curl https://arxiv.org/abs/2101.00001",
        reason="Ich lese ein Paper.",
    )


@dataclass
class AskingRun:
    mgr: RunManager
    log: Log
    asked: list[Asked]
    model: FakeModel
    user: User


def prepare_workfolder(workfolder: Path, arr_home: Path) -> None:
    """Mandate and profile of the supervising agent."""
    (workfolder / "mandates").mkdir(exist_ok=True)
    (workfolder / "mandates" / "standard.md").write_text(standard_text(), "utf-8")
    arr_home.mkdir(parents=True, exist_ok=True)


def asking_run(
    workfolder: Path,
    data: Mapping[str, Any],
    *,
    want: Want,
    decider: str,
    topology: Callable[[Any], Awaitable[None]],
    policies: Mapping[str, Any] | None = None,
    scripts: Mapping[str, Any] | None = None,
    model: FakeModel | None = None,
    user: User | None = None,
    timeout: str = "20m",
) -> AskingRun:
    """A RunManager whose agents ask `want` at every turn. `policies` (phase runs) or `scripts`
    (old topologies) write the turns; `decider` is the run's `approvals.decider`."""
    model = model or FakeModel()
    user = user or User()
    user.model = model
    log, asked = Log(), []
    if "phases" in data:
        write_tasks(workfolder, {t: f"Auftrag {t}." for t in task_files(data)})
    approvals: dict[str, Any] = {"decider": decider, "timeout": timeout}
    if decider != "user":
        approvals["gatekeeper"] = GATEKEEPER
    if "phases" in data and "topology" not in data:
        data = {**data, "topology": {"kind": "phases"}}
    full = {
        "project": "demo",
        "task": TASK,
        "defaults": FAKE_DEFAULTS,
        "approvals": approvals,
        **data,
    }
    run_input = validate_model(RunInput, full)
    resolved = resolve_run(run_input, None, make_settings())
    holder: dict[str, RunManager] = {}

    def factory(prepared: PreparedAgent) -> Any:
        view = RoomStore(prepared.run_dir).for_agent(prepared.id, prepared.work_dir)
        common = {"run_id": prepared.run_dir.name, "room": view}
        backend: Any
        if scripts is not None:
            backend = AskingFakeBackend(prepared.file.config, dict(scripts[prepared.id]), **common)
        else:
            policy = (policies or {}).get(prepared.id, silent)
            backend = AskingPolicyBackend(
                prepared.file.config, {"turns": []}, policy=policy, log=log, **common
            )
        backend.want, backend.asked, backend.manager = want, asked, lambda: holder["mgr"]
        return backend

    profiles = ProfilesFile(profiles=[AuthProfile(name=PROFILE, kind=AuthKind.CLAUDE_OAUTH)])
    mgr = RunManager(
        workfolder,
        run_input,
        resolved,
        topology=topology,
        backend_factory=factory,
        settings=make_settings(),
        pacing=FAST,
        decider=user,
        profiles=profiles,
        gatekeeper_model=lambda *_a: model,
    )
    holder["mgr"] = mgr
    return AskingRun(mgr, log, asked, model, user)


def user_allows(scope: DecisionScope = DecisionScope.ONCE) -> User:
    return User(DecisionAction.ALLOW, scope=scope)


# --------------------------------------------------------------------------------------------
# A10: the lines of a run without `decider` at agents and phases, compared with `main`
# --------------------------------------------------------------------------------------------

MAIN_LINES = Path(__file__).parent / "fixtures" / "decider" / "main_lines.json"
"""Normalized lines of `comparison_run` as `main` at 5333531 (before W4b-09) wrote them."""

DECIDERS = ("user", "gatekeeper", "gatekeeper_then_user")
KINDS = ("phases", "solo")


def outside(agent: str) -> PermissionRequest:
    """`curl https://oeis.org/…`: in the grey zone and outside the mandate."""
    return inside(agent).model_copy(update={"command": "curl https://oeis.org/A000001"})


def _strip(data: Any, drop: frozenset[str]) -> Any:
    if isinstance(data, dict):
        return {k: _strip(v, drop) for k, v in data.items() if k not in drop}
    if isinstance(data, list):
        return [_strip(v, drop) for v in data]
    return data


def normalized(run_dir: Path) -> dict[str, list[Any]]:
    """`approvals.jsonl` and `gatekeeper.jsonl` without times, with item IDs by order."""
    ids: dict[str, str] = {}
    out: dict[str, list[Any]] = {}
    for name in ("approvals.jsonl", "gatekeeper.jsonl"):
        path = run_dir / name
        entries = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
        rows = []
        for entry in entries:
            entry = _strip(entry, frozenset({"ts", "decided_at", "seconds"}))
            if isinstance(entry.get("request"), dict):
                entry["request"]["run_id"] = "<run>"
            if entry.get("item_id"):
                entry["item_id"] = ids.setdefault(entry["item_id"], f"item-{len(ids) + 1}")
            rows.append(entry)
        out[name] = rows
    return out


async def comparison_run(workfolder: Path, arr_home: Path, decider: str, kind: str) -> Any:
    """A run without `decider` at agents and phases whose agent asks in every turn: inside the
    mandate in the first phase (and in `solo`), outside in the second. Returns its lines."""
    from phases_kit import hand_in, solo

    from arr.runs.phases import runner as phase_runner
    from arr.runs.topologies import solo as solo_topology

    prepare_workfolder(workfolder, arr_home)
    if kind == "phases":
        data: dict[str, Any] = {
            "agents": [{"id": "p"}, {"id": "r1"}],
            "topology": {"kind": "phases"},
            "phases": [
                {
                    **solo("erkunden", "p"),
                    "gate": {
                        "type": "judge",
                        "raters": ["r1"],
                        "action": {
                            "task": "tasks/bewerten.md",
                            "submission": {"format": "rating"},
                            "turns": 1,
                        },
                        "rule": "rank",
                        "routes": {"ranked": "next"},
                    },
                },
                solo("beweisen", "p"),
            ],
        }
        policies = {"p": hand_in, "r1": hand_in}
        run = asking_run(
            workfolder,
            data,
            want=lambda a, s: inside(a) if s == "erkunden" else outside(a),
            decider=decider,
            topology=phase_runner.run,
            policies=policies,
        )
    else:
        data = {
            "agents": [{"id": "p"}],
            "topology": {"kind": "solo", "params": {"require_result": False, "max_turns": 2}},
        }
        script = {"turns": [{"steps": [{"do": "text", "text": "ok"}]}], "repeat_last": True}
        run = asking_run(
            workfolder,
            data,
            want=lambda a, _s: inside(a),
            decider=decider,
            topology=solo_topology.run,
            scripts={"p": script},
        )
    status = await asyncio.wait_for(run.mgr.run(), 60)
    return status, normalized(run.mgr.run_dir), run
