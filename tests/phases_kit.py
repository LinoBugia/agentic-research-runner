"""Helpers for the tests of the phase runner (W4b-02b): runs of `topology: phases` with the real
RunManager, Orchestrator and room, and FakeBackend agents whose turns a Python policy writes
from the assignment of the turn (`PolicyBackend`). No model, no CLI.

A policy gets a `TurnView` (agent, turn, stage, prompt, the context of the assignment, the
work folder) and returns the steps of the turn (`run_kit.room(...)` etc.). `answer(...)` builds
a policy that hands in what the expected submission asks for. Every turn is recorded in
`Log.turns` in the order the turns began.
"""

import asyncio
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plan_kit import SETTINGS
from run_kit import FAST, make_settings, room

from arr.backends.fake import FakeBackend, TurnScript
from arr.models import GroupSpec, RunInput, RunStatus
from arr.permissions.inbox import ApprovalItem
from arr.permissions.types import DecidedBy, Decision, DecisionAction, EscalationOption
from arr.resolve import check_group_spec, parse_yaml_text, resolve_run, validate_model
from arr.room.store import RoomStore
from arr.runs.finalize import finalize_run
from arr.runs.manager import RunManager
from arr.runs.orchestrator import OrchestratorLog
from arr.runs.phases import runner as phase_runner
from arr.runs.prepare import PreparedAgent

FIXTURES = Path(__file__).parent / "fixtures" / "phases"
TASK = "Zeige, dass jede Zahl n ≡ 1 mod 4 eine Summe zweier Quadrate ist, wenn n prim ist."
FAKE_DEFAULTS = {"provider": "claude", "model": "fake-model", "auth": "fake-auth"}


@dataclass
class TurnView:
    """What a policy sees of one turn."""

    agent: str
    turn: int
    stage: str | None
    prompt: str
    context: dict[str, Any]
    work_dir: Path

    @property
    def expected(self) -> dict[str, Any]:
        return dict(self.context.get("submission") or {})

    @property
    def action(self) -> str | None:
        return self.expected.get("action")

    @property
    def attempt(self) -> int:
        return int(self.context.get("attempt", 1))

    @property
    def repetition(self) -> int:
        rep = self.context.get("repetition")
        return int(rep["number"]) if isinstance(rep, dict) else 1

    @property
    def entry(self) -> int | None:
        entry = self.context.get("entry")
        return int(entry["number"]) if isinstance(entry, dict) else None

    @property
    def follow_up(self) -> bool:
        return self.prompt.startswith("Nachfrage") or "Nachfrage zu deiner Action" in self.prompt


Policy = Callable[[TurnView], list[dict[str, Any]]]


@dataclass
class Log:
    """Every turn in the order it began: (agent, stage, attempt, action, repetition, entry)."""

    turns: list[tuple[str, str | None, int, str | None, int, int | None]] = field(
        default_factory=list
    )
    views: list[TurnView] = field(default_factory=list)
    times: list[float] = field(default_factory=list)

    def of(self, agent: str) -> list[TurnView]:
        return [v for v in self.views if v.agent == agent]


class PolicyBackend(FakeBackend):
    """A FakeBackend whose next turn a policy writes from the current assignment."""

    def __init__(self, *args: Any, policy: Policy, log: Log, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.policy = policy
        self.turn_log = log

    def _turn_script(self, stage: str | None) -> TurnScript:
        assert self.room is not None
        assignment = self.room.get_assignment()  # type: ignore[attr-defined]
        view = TurnView(
            agent=self.config.id,
            turn=self._turn,
            stage=stage,
            prompt=self.history[-1] if self.history else "",
            context=dict(assignment.context) if assignment else {},
            work_dir=self.room.work_dir,  # type: ignore[attr-defined]
        )
        self.turn_log.views.append(view)
        self.turn_log.times.append(time.monotonic())
        self.turn_log.turns.append(
            (view.agent, stage, view.attempt, view.action, view.repetition, view.entry)
        )
        return TurnScript.model_validate({"steps": self.policy(view)})


def policy_factory(policies: Mapping[str, Policy], log: Log) -> Callable[[PreparedAgent], Any]:
    def factory(prepared: PreparedAgent) -> PolicyBackend:
        view = RoomStore(prepared.run_dir).for_agent(prepared.id, prepared.work_dir)
        return PolicyBackend(
            prepared.file.config,
            {"turns": []},
            run_id=prepared.run_dir.name,
            room=view,
            policy=policies.get(prepared.id, silent),
            log=log,
        )

    return factory


# --------------------------------------------------------------------------------------------
# Policies
# --------------------------------------------------------------------------------------------


def silent(view: TurnView) -> list[dict[str, Any]]:
    """Never hands in."""
    return [{"do": "text", "text": "Ich denke nach."}]


def submit(**args: Any) -> dict[str, Any]:
    return room("submit", **args)


def fields_for(view: TurnView, **over: Any) -> dict[str, Any]:
    """Fields that fit the expected format (overridable)."""
    exp = view.expected
    fmt = exp.get("format")
    if fmt == "review":
        return {"verdict": over.get("verdict", "accept"), "objections": over.get("objections", [])}
    if fmt == "rating":
        score = over.get("score", 8)
        return {
            "ratings": [
                {
                    "ref": ref,
                    "score": score(view, ref) if callable(score) else score,
                    "comment": over.get("comment", "ok"),
                }
                for ref in exp.get("rate") or []
            ]
        }
    if fmt == "list":
        n = over.get("items", 3)
        return {"items": [{"title": f"Idee {i + 1}", "text": f"Ansatz {i + 1}"} for i in range(n)]}
    if fmt == "decision":
        return {"choice": over.get("choice", (exp.get("choices") or ["x"])[0])}
    return dict(over.get("fields", {}))


def hand_in(
    view: TurnView,
    *,
    status: str = "done",
    summary: str | None = None,
    body: str | None = None,
    note_next: str | None = None,
    **over: Any,
) -> list[dict[str, Any]]:
    """The steps that hand in what the assignment expects (files are written first)."""
    exp = view.expected
    args: dict[str, Any] = {
        "status": status,
        "summary": summary
        if summary is not None
        else f"{view.agent} in {view.stage}, Durchlauf {view.attempt}",
    }
    fmt = exp.get("format")
    if fmt == "artifact":
        name = f"{exp.get('name') or 'werk'}.md"
        (view.work_dir / name).write_text(f"{view.agent} {view.attempt}\n", "utf-8")
        args["files"] = [name]
    elif fmt == "text":
        args["body"] = body if body is not None else f"Text von {view.agent}"
    if note_next is not None:
        args["note_next"] = note_next
    fields = fields_for(view, **over)
    if fields:
        args["fields"] = fields
    return [submit(**args)]


def answer(**over: Any) -> Policy:
    """A policy that hands in at once; values may be functions of the `TurnView`."""

    def policy(view: TurnView) -> list[dict[str, Any]]:
        values = {k: (v(view) if callable(v) and k != "score" else v) for k, v in over.items()}
        if values.pop("skip", False):
            return silent(view)
        return hand_in(view, **values)

    return policy


# --------------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------------


class Choose:
    """Decider: answers escalations with the given options (and hints), in order."""

    def __init__(self, *options: EscalationOption | tuple[EscalationOption, str]) -> None:
        self.options = list(options)
        self.asked: list[ApprovalItem] = []

    async def decide(self, item: ApprovalItem) -> Decision:
        self.asked.append(item)
        choice = self.options.pop(0) if self.options else EscalationOption.END
        option, hint = choice if isinstance(choice, tuple) else (choice, None)
        return Decision(
            action=DecisionAction.OPTION, option=option, decided_by=DecidedBy.USER, comment=hint
        )


def write_project_tasks(workfolder: Path, tasks: Mapping[str, str]) -> None:
    project = workfolder / "projects" / "demo"
    for rel, text in tasks.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def task_files(group: Mapping[str, Any]) -> list[str]:
    """Every `task` file of a group (actions and gate actions)."""
    out = []
    for phase in group.get("phases") or []:
        for action in (phase.get("actions") or {}).values():
            out.append(action["task"])
        gate = phase.get("gate") or {}
        if isinstance(gate.get("action"), dict):
            out.append(gate["action"]["task"])
    return [t for t in dict.fromkeys(out) if "{{" not in t]


def manager(
    workfolder: Path,
    group: Mapping[str, Any],
    policies: Mapping[str, Policy],
    *,
    log: Log | None = None,
    task: str = TASK,
    params: Mapping[str, Any] | None = None,
    limits: Mapping[str, Any] | None = None,
    decider: Any = None,
    tasks: Mapping[str, str] | None = None,
    finalizer: Any = None,
    stop_grace_s: float | None = None,
) -> tuple[RunManager, Log]:
    """A RunManager of a `phases` run given as run data (agents, phases, channels, seed …);
    every task file of the group is written into the project unless `tasks` names them."""
    log = log or Log()
    files = dict(tasks) if tasks is not None else {t: f"Auftrag {t}." for t in task_files(group)}
    write_project_tasks(workfolder, files)
    data = {
        "project": "demo",
        "task": task,
        "topology": {"kind": "phases", "params": dict(params or {})},
        "defaults": FAKE_DEFAULTS,
        **group,
    }
    if limits is not None:
        data["limits"] = dict(limits)
    run_input = validate_model(RunInput, data)
    resolved = resolve_run(run_input, None, make_settings())
    kwargs: dict[str, Any] = {}
    if stop_grace_s is not None:
        kwargs["stop_grace_s"] = stop_grace_s
    mgr = RunManager(
        workfolder,
        run_input,
        resolved,
        topology=phase_runner.run,
        backend_factory=policy_factory(policies, log),
        settings=make_settings(),
        pacing=FAST,
        decider=decider,
        finalizer=finalizer,
        **kwargs,
    )
    return mgr, log


def fixture_manager(
    workfolder: Path,
    name: str,
    policies: Mapping[str, Policy],
    *,
    params: Mapping[str, Any] | None = None,
    decider: Any = None,
    log: Log | None = None,
) -> tuple[RunManager, Log]:
    """A RunManager of a fixture group of `tests/fixtures/phases/` as a skill `fx-<name>`:
    roles and task files are written into the skill, the agents are FakeBackends."""
    log = log or Log()
    text = (FIXTURES / f"{name}.yaml").read_text(encoding="utf-8")
    data, _ = parse_yaml_text(text, f"{name}.yaml")
    skill = workfolder / "skills" / f"fx-{name}"
    if skill.exists():
        shutil.rmtree(skill)
    skill.mkdir(parents=True)
    (skill / "group.yaml").write_text(text, encoding="utf-8")
    (skill / "SKILL.md").write_text(
        f"---\nname: fx-{name}\ndescription: Testgruppe {name}\n---\nTest.\n", encoding="utf-8"
    )
    for agent in data["agents"]:
        for tool in agent.get("skills") or []:  # research skills the agents name: stubs
            stub = workfolder / "skills" / tool
            if not stub.exists():
                stub.mkdir(parents=True)
                (stub / "SKILL.md").write_text(
                    f"---\nname: {tool}\ndescription: Stub {tool}\n---\nStub.\n", encoding="utf-8"
                )
        if agent.get("role"):
            role = skill / agent["role"]
            role.parent.mkdir(parents=True, exist_ok=True)
            role.write_text(f"Rolle {agent['id']}.\n", encoding="utf-8")
    for rel in task_files(data):
        path = skill / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Auftrag aus {rel} (Phase-Test).\n", encoding="utf-8")
    spec = validate_model(GroupSpec, dict(data), source="group.yaml")
    check_group_spec(spec)
    run_input = RunInput(project="demo", task=TASK, skill=f"fx-{name}")
    settings = make_settings().model_copy(update={"defaults": SETTINGS.defaults})
    resolved = resolve_run(run_input, spec, settings, dict(params or {}))
    mgr = RunManager(
        workfolder,
        run_input,
        resolved,
        topology=phase_runner.run,
        backend_factory=policy_factory(policies, log),
        settings=make_settings(),
        pacing=FAST,
        decider=decider,
        finalizer=finalize_run,
    )
    return mgr, log


async def run(mgr: RunManager, within: float = 60) -> RunStatus:
    return await asyncio.wait_for(mgr.run(), within)


def checkpoints(mgr: RunManager, kind: str | None = None) -> list[dict[str, Any]]:
    """`checkpoint` data of the run, optionally of one `phase` kind (`gate`, `route` …)."""
    out = []
    for entry in OrchestratorLog(mgr.run_dir).entries():
        if entry["kind"] != "checkpoint":
            continue
        if kind is None or entry["data"].get("phase") == kind:
            out.append(entry["data"])
    return out


def entered(mgr: RunManager) -> list[str]:
    """Phases in the order their passes began."""
    return [c["stage"] for c in checkpoints(mgr, "pass_start")]


def errors(mgr: RunManager) -> list[str]:
    return [
        e["data"]["message"] for e in OrchestratorLog(mgr.run_dir).entries() if e["kind"] == "error"
    ]


def solo(phase: str, agent: str, fmt: str = "text", **extra: Any) -> dict[str, Any]:
    """A phase `solo` with one action (task `tasks/<phase>.md`); `name` and `choices` go to
    the submission."""
    submission = {"format": fmt}
    for key in ("name", "choices"):
        if key in extra:
            submission[key] = extra.pop(key)
    action: dict[str, Any] = {"task": f"tasks/{phase}.md", "submission": submission}
    if "turns" in extra:
        action["turns"] = extra.pop("turns")
    return {"name": phase, "topology": "solo", "actions": {agent: action}, **extra}


def parallel(phase: str, agents: list[str] | str, fmt: str = "text", **extra: Any) -> dict:
    """A phase `parallel` with the same action for every agent or pattern."""
    keys = [agents] if isinstance(agents, str) else agents
    submission = {"format": fmt}
    for key in ("name", "choices"):
        if key in extra:
            submission[key] = extra.pop(key)
    action: dict[str, Any] = {"task": f"tasks/{phase}.md", "submission": submission}
    if "turns" in extra:
        action["turns"] = extra.pop("turns")
    return {
        "name": phase,
        "topology": "parallel",
        "actions": {k: dict(action) for k in keys},
        **extra,
    }


def judge_gate(
    raters: list[str] | str, rule: str = "mean", fmt: str = "rating", **extra: Any
) -> dict[str, Any]:
    gate: dict[str, Any] = {
        "type": "judge",
        "raters": [raters] if isinstance(raters, str) else raters,
        "action": {"task": "tasks/bewerten.md", "submission": {"format": fmt}, "turns": 1},
        "rule": rule,
    }
    gate.update(extra)
    return gate


# --------------------------------------------------------------------------------------------
# W4b-03: channels, waking, holding
# --------------------------------------------------------------------------------------------


def private(agent: str) -> str:
    """The private channel of `agent` to the user (W4b-03)."""
    return f"user.{agent}"


def say(channel: str, body: str, **args: Any) -> dict[str, Any]:
    """A message of the agent (`post_message`)."""
    return room("post_message", channel=channel, body=body, **args)


def entries(mgr: RunManager, *kinds: str) -> list[tuple[str, dict[str, Any]]]:
    """`(kind, data)` of the log entries of these kinds, in the order they were written."""
    return [
        (e["kind"], e["data"])
        for e in OrchestratorLog(mgr.run_dir).entries()
        if not kinds or e["kind"] in kinds
    ]


def of_kind(mgr: RunManager, kind: str, **match: Any) -> list[dict[str, Any]]:
    """Data of the log entries of `kind` whose fields equal `match`."""
    return [
        d for k, d in entries(mgr, kind) if all(d.get(key) == value for key, value in match.items())
    ]


def woken(log: Log, agent: str) -> list[TurnView]:
    """The wake-up turns of a sleeper (their context says `standby`)."""
    return [v for v in log.of(agent) if v.context.get("standby")]


async def run_with(
    mgr: RunManager, helper: Callable[[RunManager], Any], within: float = 60
) -> RunStatus:
    """Run the manager with `helper(mgr)` beside it (e.g. the user answering); the helper is
    cancelled when the run ends, its error is raised."""
    import contextlib

    task = asyncio.create_task(helper(mgr))
    try:
        status = await run(mgr, within)
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return status


def waker_of(mgr: RunManager) -> Any:
    """The waker of the running pass (`arr.runs.phases.wake.Waker`), None between passes."""
    from arr.runs.phases.wake import Waker

    orch = mgr.orchestrator
    if orch is None:
        return None
    found = [w for w in orch._listeners if isinstance(w, Waker)]
    return found[0] if found else None
