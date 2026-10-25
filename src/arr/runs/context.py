"""RunContext: the API that topologies use to drive agents and group chats.

A topology is `async def run(ctx: RunContext) -> None` (E8). The orchestrator (W2-03)
implements `RunContext`; tests can implement it on top of the FakeBackend.
See docs/01-architektur.md §3, §6 and docs/03-skills-und-research-groups.md §4.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arr.backends.base import TurnOutcome
from arr.models import AgentConfig, AgentStatus, ResolvedRun
from arr.permissions.types import EscalationOption
from arr.room.types import MessageKind, Rating, Result


@dataclass(slots=True)
class AgentHandle:
    """What a topology sees of an agent: its configuration and progress (docs/01 §6).

    `turns_used` and `status` are maintained by the `RunContext` implementation.
    """

    config: AgentConfig
    turns_used: int = 0
    status: AgentStatus = AgentStatus.IDLE

    @property
    def id(self) -> str:
        """Agent ID, e.g. `judge-2`."""
        return self.config.id


@dataclass(frozen=True, slots=True)
class TurnJob:
    """One turn for `RunContext.turns`: agent, prompt, optional context package (docs/03 §4.5)."""

    agent_id: str
    prompt: str
    context: Mapping[str, Any] | None = None


class RunContext(ABC):
    """Abstract API of a running run for topologies (docs/01-architektur.md §6).

    Turn prompts are short and dynamic; the role prompt stays static. Limits (wall time,
    turns per agent, budget, concurrency per auth profile) are enforced by the
    implementation: when one is reached, `turn` returns an outcome with status `limit` and
    `stop_requested` becomes true, so the topology can finish cleanly.
    """

    @property
    @abstractmethod
    def run_id(self) -> str:
        """ID of the run, e.g. `20261023-1742_collatz_exploration_a3f9` (docs/02 §4)."""

    @property
    @abstractmethod
    def config(self) -> ResolvedRun:
        """Resolved configuration of the run."""

    @property
    @abstractmethod
    def params(self) -> Mapping[str, Any]:
        """Final parameters from `group.yaml` + overrides at start (`config.topology.params`)."""

    @property
    @abstractmethod
    def agents(self) -> Mapping[str, AgentHandle]:
        """Agents by ID, in configuration order."""

    @property
    @abstractmethod
    def stop_requested(self) -> bool:
        """True after a user stop or when a run limit is reached."""

    @abstractmethod
    async def turn(
        self,
        agent_id: str,
        prompt: str,
        *,
        stage: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> TurnOutcome:
        """Give one agent a turn and wait for it, including the results it submitted.

        `stage` and `context` are stored as the agent's assignment (`get_assignment()`).
        The inbox (open questions, unread counts) is added to `prompt` by the implementation.
        """

    @abstractmethod
    async def turns(
        self, jobs: Sequence[TurnJob | tuple[str, str]], *, stage: str | None = None
    ) -> list[TurnOutcome]:
        """Run several turns in parallel within the limits; outcomes in the order of `jobs`.

        A job is a `TurnJob` or a tuple `(agent_id, prompt)`.
        """

    @abstractmethod
    async def post(
        self,
        channel: str,
        body: str,
        kind: MessageKind = MessageKind.NOTE,
        *,
        to: Sequence[str] = (),
        reply_to: int | None = None,
    ) -> int:
        """Post as `orchestrator` into a channel; return the message ID."""

    @abstractmethod
    def results(
        self,
        *,
        agent: str | None = None,
        stage: str | None = None,
        kind: str | None = None,
        attempt: int | None = None,
    ) -> list[Result]:
        """Submitted results matching all given filters, oldest first."""

    @abstractmethod
    def ratings(self, result_id: int) -> list[Rating]:
        """Ratings of one result, oldest first."""

    @abstractmethod
    async def checkpoint(self, stage: str, **info: Any) -> None:
        """Record a stage checkpoint in `orchestrator.jsonl` and notify the GUI (docs/02 §4).

        The stage `plan` with all planned stages is recorded at start (Codeplan §8, W2-03).
        """

    @abstractmethod
    async def escalate(
        self, stage: str, summary: str, options: Sequence[EscalationOption]
    ) -> EscalationOption:
        """Pause the run, put an escalation into the approval inbox and wait for the user.

        Used by `waterfall` with `on_fail: escalate` (docs/03 §4.4). Returns the chosen option.
        """
