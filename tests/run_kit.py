"""Helpers for the RunManager and pacing tests: a small workfolder, runs with FakeBackend agents
and the real room (W2-03). Imported by `test_run_manager*.py` and `test_pacing*.py`."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from arr.backends.fake import FakeBackend
from arr.models import RunInput, Toolchain, WorkfolderSettings
from arr.permissions.types import PermissionRequest
from arr.resolve import resolve_run, validate_model
from arr.room.store import RoomStore
from arr.runs.context import RunContext
from arr.runs.eventlog import read_events
from arr.runs.manager import RunManager
from arr.runs.orchestrator import OrchestratorLog
from arr.runs.pacing import PacingSettings
from arr.runs.prepare import PreparedAgent
from arr.workfolder import init_workfolder

FAST = PacingSettings(poll_interval_s=0.01, hint_interval_s=60, hint_tool_calls=5)
"""Pacing for tests: the room is polled every 10 ms."""

Script = Mapping[str, Any] | Callable[[], Mapping[str, Any]]


def make_workfolder(root: Path) -> Path:
    """Workfolder with project `demo` (`notes.md`, `check.py`)."""
    workfolder = init_workfolder(root / "wf")
    project = workfolder / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "notes.md").write_text("# Notizen\nLemma A gilt für n ≡ 1 mod 4.\n", "utf-8")
    (project / "check.py").write_text("print('ok')\n", "utf-8")
    return workfolder


def make_settings(**concurrency: int) -> WorkfolderSettings:
    """Settings without run venv; `concurrency` per auth profile."""
    return WorkfolderSettings(toolchain=Toolchain(run_venv="off"), concurrency=dict(concurrency))


def agent(agent_id: str, **fields: Any) -> dict[str, Any]:
    """An agent entry for `run.yaml` (FakeBackend, profile `fake-auth` unless given)."""
    return {"id": agent_id, **fields}


def channel(name: str, members: list[str], **fields: Any) -> dict[str, Any]:
    """A group chat entry for `run.yaml`."""
    return {"name": name, "members": members, **fields}


def make_run_input(
    agents: list[dict[str, Any]],
    groupchats: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> RunInput:
    data: dict[str, Any] = {
        "project": "demo",
        "task": "Teste arr.",
        "topology": {"kind": "solo"},
        "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
        "agents": agents,
        "groupchats": groupchats or [],
        **extra,
    }
    return validate_model(RunInput, data)


def fake_factory(scripts: Mapping[str, Script]) -> Callable[[PreparedAgent], FakeBackend]:
    """Backend factory: a FakeBackend per agent with its script and its real room view.

    A script may be a callable; it is called when the backend is built (e.g. for times).
    """

    def factory(prepared: PreparedAgent) -> FakeBackend:
        script = scripts[prepared.id]
        if callable(script):
            script = script()
        room = RoomStore(prepared.run_dir).for_agent(prepared.id, prepared.work_dir)
        return FakeBackend(
            prepared.file.config, dict(script), run_id=prepared.run_dir.name, room=room
        )

    return factory


Topology = Callable[[RunContext], Awaitable[None]]


def make_manager(
    workfolder: Path,
    topology: Topology,
    scripts: Mapping[str, Script],
    *,
    agents: list[dict[str, Any]] | None = None,
    groupchats: list[dict[str, Any]] | None = None,
    settings: WorkfolderSettings | None = None,
    run_extra: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> RunManager:
    """A RunManager for a run with FakeBackend agents (default: one agent per script)."""
    settings = settings or make_settings()
    agents = agents if agents is not None else [agent(a) for a in scripts]
    run_input = make_run_input(agents, groupchats, **dict(run_extra or {}))
    resolved = resolve_run(run_input, None, settings)
    kwargs.setdefault("pacing", FAST)
    return RunManager(
        workfolder,
        run_input,
        resolved,
        topology=topology,
        backend_factory=fake_factory(scripts),
        settings=settings,
        **kwargs,
    )


def turns(*turn_steps: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """A FakeBackend script from lists of steps."""
    return {"turns": [{"steps": steps} for steps in turn_steps], **extra}


def say(text: str) -> dict[str, Any]:
    return {"do": "text", "text": text}


def wait(seconds: float) -> dict[str, Any]:
    """Interruptible wait (hints are emitted meanwhile)."""
    return {"do": "delay", "seconds": seconds}


def room(action: str, save_as: str | None = None, **args: Any) -> dict[str, Any]:
    step: dict[str, Any] = {"do": "room", "action": action, "args": args}
    if save_as:
        step["save_as"] = save_as
    return step


def post(channel_name: str, body: str, **args: Any) -> dict[str, Any]:
    save_as = args.pop("save_as", None)
    return room("post_message", save_as, channel=channel_name, body=body, **args)


def done(note: str = "fertig") -> dict[str, Any]:
    return room("set_status", status="done", note=note)


def cost(usd: float) -> dict[str, Any]:
    return {"do": "usage", "input": 10, "output": 5, "cost_usd": usd}


def events(run_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    """Normalized transcript of an agent."""
    return read_events(run_dir / "agents" / agent_id / "transcript.jsonl")


def log_entries(run_dir: Path, kind: str | None = None) -> list[dict[str, Any]]:
    """Entries of `orchestrator.jsonl`, optionally of one kind."""
    entries = OrchestratorLog(run_dir).entries()
    return [e for e in entries if kind is None or e["kind"] == kind]


async def eventually(check: Callable[[], bool], within: float = 5.0) -> None:
    """Wait until `check()` is true (polling every 10 ms); fail after `within` seconds."""
    end = time.monotonic() + within
    while not check():
        if time.monotonic() > end:
            raise AssertionError("Bedingung nicht rechtzeitig erfüllt")
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------------------------
# W2-05: `arr run` with FakeBackend agents through the launcher's backend builders
# --------------------------------------------------------------------------------------------


class AskingBackend(FakeBackend):
    """A FakeBackend that sends the requests `asks` to the inbox before its turn, like an adapter.

    Each entry of `asks` holds fields of a `PermissionRequest`; with `parallel` they wait
    together. The decisions land in `answers`, and the answers are posted as turn text.
    """

    def __init__(self, *args: Any, inbox: Any, asks: list[dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.inbox = inbox
        self.asks = list(asks)
        self.answers: list[Any] = []

    async def run_turn(self, *args: Any, **kwargs: Any) -> Any:
        asks, self.asks = self.asks, []
        requests = [
            PermissionRequest(run_id=self.run_id, agent_id=self.config.id, **ask) for ask in asks
        ]
        results = await asyncio.gather(*(self.inbox.handle(r) for r in requests))
        self.answers.extend(results)
        async for event in super().run_turn(*args, **kwargs):
            yield event


def fake_builders(scripts: Mapping[str, Script]) -> dict[Any, Callable[..., FakeBackend]]:
    """Replacement for `arr.runs.launch.BACKEND_BUILDERS`: a FakeBackend per agent.

    Every call is recorded in the returned mapping under the key `"calls"` as
    `(provider, agent_id, kwargs)`; `kwargs` holds `run_id`, `inbox`, `auth` and `limits`. A
    script with the key `asks` makes an `AskingBackend`.
    """
    from arr.models import Provider

    calls: list[tuple[str, str, dict[str, Any]]] = []
    asks = {
        agent_id: script["asks"]
        for agent_id, script in scripts.items()
        if isinstance(script, Mapping) and "asks" in script
    }
    inner = fake_factory(
        {
            agent_id: {k: v for k, v in script.items() if k != "asks"}
            if isinstance(script, Mapping)
            else script
            for agent_id, script in scripts.items()
        }
    )

    def builder_for(provider: Provider) -> Callable[..., FakeBackend]:
        def build(prepared: PreparedAgent, **kwargs: Any) -> FakeBackend:
            calls.append((provider.value, prepared.id, kwargs))
            backend = inner(prepared)
            backend.run_id = kwargs.get("run_id", backend.run_id)
            if prepared.id in asks:
                return AskingBackend(
                    backend.config,
                    backend.script,
                    run_id=backend.run_id,
                    room=backend.room,
                    inbox=kwargs["inbox"],
                    asks=asks[prepared.id],
                )
            return backend

        return build

    builders: dict[Any, Any] = {p: builder_for(p) for p in Provider}
    builders["calls"] = calls
    return builders


def write_profiles(arr_home: Path, profiles: Mapping[str, str]) -> Path:
    """`profiles.yaml` in the (test) app folder: `name → kind`."""
    arr_home.mkdir(parents=True, exist_ok=True)
    lines = ["version: 1", "profiles:"]
    lines += [f"  - {{name: {name}, kind: {kind}}}" for name, kind in profiles.items()]
    path = arr_home / "profiles.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
