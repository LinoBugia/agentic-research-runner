"""Which permission requests the user decides because of `decider: user` (W4b-09, P14).

An agent or a phase can only tighten the decider of the run (docs/10 §4.11): with
`decider: user` its requests go to the user, never to the supervising agent. `tightened_by`
is the rule as a pure function of the resolved run and the phase the agent entered last; the
inbox asks it (through a callback of the `RunManager`) the moment a request arrives, and once
more before it takes an answer of the supervising agent.

1. No agent and no phase of the run has `decider: user`: nothing is tightened and nothing is
   looked up (the behaviour before W4b-09).
2. The agent has `decider: user`: source `agent`, in every phase and in the old topologies.
3. Else the phase the agent entered last has `decider: user`: source `phase`, with its name
   and pass.
4. Else, when in doubt, tightened with source `unknown`: the agent is not in the run, the
   lookup of its phase raised, or a phase of the run has `decider: user` and the agent's phase
   is not known (none entered yet, a name that is no phase of the run, no lookup at all).
5. Else the decider of the run holds.

This module imports nothing from `arr.runs`: the phase lookup comes in as a callable.
"""

import logging
from collections.abc import Callable, Sequence

from arr.models import AgentConfig, ResolvedPhase
from arr.permissions.types import TightenedBy, TightenedSource

__all__ = ["PhaseLookup", "Tightening", "tightened_by"]

logger = logging.getLogger(__name__)

USER = "user"

PhaseLookup = Callable[[str], tuple[str, int] | None]
"""Phase and pass an agent entered last (`Orchestrator.agent_phase`), None before."""

Tightening = Callable[[str], TightenedBy | None]
"""What the inbox asks for the agent of a request: the tightening, or None."""

_UNKNOWN = TightenedBy(source=TightenedSource.UNKNOWN)


def tightened_by(
    agents: Sequence[AgentConfig],
    phases: Sequence[ResolvedPhase],
    agent_id: str,
    phase_of: PhaseLookup | None,
) -> TightenedBy | None:
    """The tightening of a request of `agent_id`, None if the run's decider holds (see above).

    `agents` and `phases` are those of the resolved run; `phase_of` looks up the phase the
    agent entered last (None: no lookup possible, e.g. no orchestrator yet).
    """
    tight_phases = {p.name for p in phases if p.decider == USER}
    if not tight_phases and not any(a.decider == USER for a in agents):
        return None  # rule 1: nothing to look up
    agent = next((a for a in agents if a.id == agent_id), None)
    if agent is None:
        return _UNKNOWN  # rule 4: an agent the run does not know
    if agent.decider == USER:
        return TightenedBy(source=TightenedSource.AGENT)  # rule 2
    if not tight_phases:
        return None  # rule 5: only other agents are tightened
    if phase_of is None:
        return _UNKNOWN
    try:
        entered = phase_of(agent_id)
    except Exception:
        logger.exception("Phase von %s ließ sich nicht nachschlagen", agent_id)
        return _UNKNOWN
    if entered is None:
        return _UNKNOWN  # no phase entered yet, e.g. a continued run before `enter_phase`
    stage, attempt = entered
    if stage not in {p.name for p in phases}:
        return _UNKNOWN
    if stage in tight_phases:
        return TightenedBy(source=TightenedSource.PHASE, stage=stage, attempt=attempt)  # rule 3
    return None  # rule 5
