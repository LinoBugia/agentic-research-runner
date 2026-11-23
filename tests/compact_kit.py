"""Helpers for the W4b-05b tests (auto-compact, `compact_session`): FakeBackend steps for the
fill level and note turns, runs with a threshold on each layer, and readers of the logs.

Builds on `tests/run_kit.py`, which it uses unchanged."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from run_kit import FAST, Topology, events, fake_factory, log_entries, make_run_input, room

from arr.backends.fake import FakeBackend
from arr.models import GroupSpec, WorkfolderSettings
from arr.resolve import resolve_run, validate_model
from arr.runs.compact import NOTE_HEADING
from arr.runs.manager import RunManager

NOTE = "### Stand\nLemma 1 halb bewiesen, Induktion über n.\n### Offene Punkte\nFall n = 1."
"""The note the FakeBackend writes in a note turn."""

LOW = {"at": 500}
"""A threshold in tokens that `fill(600)` reaches."""


def fill(tokens: int, *, cost_usd: float | None = None, **extra: Any) -> dict[str, Any]:
    """A request whose whole input is `tokens`: the fill level of the session after it."""
    step: dict[str, Any] = {"do": "usage", "input": tokens, "output": 5, **extra}
    if cost_usd is not None:
        step["cost_usd"] = cost_usd
    return step


def say(text: str) -> dict[str, Any]:
    return {"do": "text", "text": text}


def submit(**args: Any) -> dict[str, Any]:
    return room("submit", **args)


def good_note(tokens: int = 650, **extra: Any) -> list[dict[str, Any]]:
    """Steps of a note turn that succeeds."""
    return [say(NOTE), fill(tokens, **extra)]


def script(
    *turn_steps: list[dict[str, Any]],
    note_turns: list[list[dict[str, Any]]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A FakeBackend script: one list of steps per turn; `note_turns` are the steps of its
    note turns (the last one repeats); `extra` e.g. `repeat_last`."""
    data: dict[str, Any] = {"turns": [{"steps": steps} for steps in turn_steps], **extra}
    if note_turns is not None:
        data["notes"] = {"repeat_last": True, "turns": [{"steps": s} for s in note_turns]}
    return data


# --------------------------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------------------------


def prompts(run_dir: Path, agent_id: str) -> list[str]:
    """The prompts of the agent's turns as they reached the backend (`turn_start`)."""
    return [e["data"]["prompt"] for e in events(run_dir, agent_id) if e["kind"] == "turn_start"]


def starts(run_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    return [e for e in events(run_dir, agent_id) if e["kind"] == "turn_start"]


def ends(run_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    return [e["data"] for e in events(run_dir, agent_id) if e["kind"] == "turn_end"]


def compacts(run_dir: Path) -> list[dict[str, Any]]:
    """The data of the entries `compact` in `orchestrator.jsonl`."""
    return [e["data"] for e in log_entries(run_dir, "compact")]


def turn_log(run_dir: Path, agent_id: str) -> list[tuple[str, int, str | None]]:
    """`(kind, turn, purpose)` of the entries `turn` and `turn_end` of an agent, plus
    `("compact", 0, reason)` for its compactions, in the order of `orchestrator.jsonl`."""
    out = []
    for entry in log_entries(run_dir):
        data = entry["data"]
        if data.get("agent") != agent_id:
            continue
        if entry["kind"] in ("turn", "turn_end"):
            out.append((entry["kind"], data["turn"], data.get("purpose")))
        elif entry["kind"] == "compact":
            out.append(("compact", 0, data["reason"]))
    return out


def is_note_prompt(prompt: str) -> bool:
    return prompt.startswith(NOTE_HEADING)


# --------------------------------------------------------------------------------------------
# A threshold on each layer (A3)
# --------------------------------------------------------------------------------------------

LAYERS = ("arr.yaml", "group defaults", "group mix", "group agent", "run defaults", "run agent")


def layered_manager(
    workfolder: Path,
    layer: str,
    topology: Topology,
    scripts: Mapping[str, Mapping[str, Any]],
    compact: Mapping[str, Any] = LOW,
) -> RunManager:
    """A run whose agent `solo` (in `group mix`: `solo-1`) gets `compact` from one `layer`;
    every other layer keeps the default (60 % of the FakeBackend's window of 1 000 000)."""
    defaults = {"provider": "claude", "model": "fake-model", "auth": "fake-auth"}
    settings = WorkfolderSettings.model_validate(
        {"toolchain": {"run_venv": "off"}, **({"compact": compact} if layer == "arr.yaml" else {})}
    )
    agent: dict[str, Any] = {"id": "solo"}
    if layer == "group mix":
        agent.update(count=1, mix=[{"compact": dict(compact)}])
    elif layer in ("group agent", "run agent"):
        agent["compact"] = dict(compact)
    group = None
    if layer.startswith("group") or layer == "arr.yaml":
        group_defaults = {**defaults, **({"compact": compact} if layer == "group defaults" else {})}
        group = validate_model(
            GroupSpec,
            {"topology": "phases", "defaults": group_defaults, "agents": [agent]},
            source="group.yaml",
        )
        run_input = make_run_input([], defaults=defaults)
    else:
        run_defaults = {**defaults, **({"compact": compact} if layer == "run defaults" else {})}
        run_input = make_run_input([agent], defaults=run_defaults)
    resolved = resolve_run(run_input, group, settings)
    return RunManager(
        workfolder,
        run_input,
        resolved,
        topology=topology,
        backend_factory=fake_factory(scripts),
        settings=settings,
        pacing=FAST,
    )


# --------------------------------------------------------------------------------------------
# FakeBackends with one difference (W4b-05b.1)
# --------------------------------------------------------------------------------------------


class UsageWindowBackend(FakeBackend):
    """Reports the window only with a request, never at the turn end (like Codex without the
    window at `turn_end`): only `usage.context_window` can tell the orchestrator."""

    def _turn_end(self, state: Any) -> Any:
        event = super()._turn_end(state)
        event.data["context_window"] = None
        return event


class FailingResetBackend(FakeBackend):
    """`reset()` raises: a new session cannot be started."""

    async def reset(self) -> None:
        raise RuntimeError("reset kaputt")


class FailingStartBackend(FakeBackend):
    """The `fails`-th turn fails before its `turn_start` (like a CLI that does not start)."""

    def __init__(self, *args: Any, fails: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fails = fails
        self.calls = 0

    def run_turn(self, prompt: str, *, stage: str | None = None) -> Any:
        self.calls += 1
        if self.calls == self.fails:
            return self._broken()
        return super().run_turn(prompt, stage=stage)

    async def _broken(self) -> Any:
        raise RuntimeError("Die CLI startet nicht.")
        yield  # pragma: no cover - makes this an async generator


def swap_backends(manager: RunManager, cls: type[FakeBackend], **kwargs: Any) -> RunManager:
    """Build the run's FakeBackends as `cls` (same script, room and config)."""
    inner = manager.backend_factory

    def factory(prepared: Any) -> FakeBackend:
        made = inner(prepared)
        return cls(made.config, made.script, run_id=made.run_id, room=made.room, **kwargs)

    manager.backend_factory = factory
    return manager
