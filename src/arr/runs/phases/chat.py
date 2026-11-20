"""One pass of a phase `chat` (W4b-03 §6.5; docs/10 §4.5, Entscheidungen 16, 39).

- The agents with an action discuss in the first channel of the phase (`phase.channels[0]`;
  `check` makes sure they are members), with that channel's pacing (`rounds` or `event`) and
  `phase.rounds` as the number of rounds or idle rounds. The rules are those of
  `RunContext.discuss` (docs/06 §5, docs/09 §4): kick-off, rounds, wake-ups, L10, L13, L15, L19,
  `quiet`, and B3 for the questions of the discussing agents in that channel.
- Agents on standby in the discussion channel get neither kick-off nor round and do not count
  for „all done“; they are woken only by a direct message (`arr.runs.phases.wake`). Over the
  other open channels of the phase the discussing agents get inbox and hints.
- Every discussing agent has one action (`work`). At the start of the pass `transition` (only
  on a normal entry) and `enter_phase`, as in `work`. Every turn of it in the discussion
  (kick-off, round, wake-up, after the user's answer, the follow-up question) carries the
  context package of its action, so `submit` knows the expected submission; the first one shows
  the assignment and its format (`render_assignment`), the others the short texts that name
  `submit`.
- „Done“ means: a submission is there or the agent reported `done`. The newest submission of
  the pass counts. After the end of the discussion (not after a stop or a limit) every
  discussing agent without a submission gets exactly one follow-up question, all at the same
  time; without a submission after it the gate counts it as missing. `turns` of an action does
  not count in `chat`: the pacing decides the turns.
- Then `runner.after_actions` (no more wake-ups, B3 for questions in the other channels and the
  private ones), and gate, rating, routes and feedback as in `solo` and `parallel`.
"""

from typing import TYPE_CHECKING

from arr.models import ChannelMode
from arr.plan.read import ACTION_WORK
from arr.room.address import is_user_question
from arr.runs.context import DiscussionEnd, DiscussionSeats
from arr.runs.phases.state import HOW_NORMAL, Pass, Slot
from arr.runs.phases.view import ask_again_prompt, render_assignment
from arr.runs.phases.work import together

if TYPE_CHECKING:
    from arr.runs.phases.runner import PhaseRunner

__all__ = ["run_pass"]


async def run_pass(runner: "PhaseRunner", pass_: Pass) -> None:
    """Run the discussion of a pass of a phase `chat` and the follow-up questions."""
    ctx = runner.ctx
    phase = ctx.config.phase(pass_.phase)
    actions = {a.agent: a for a in phase.actions}
    slots = [
        Slot(agent, ACTION_WORK, actions[agent], runner.task_text(phase, actions[agent]))
        for agent in phase.agents
    ]
    pass_.slots = slots

    async def enter(slot: Slot) -> None:
        if ctx.stop_requested:
            return
        if pass_.how == HOW_NORMAL:
            await runner.transition(slot.agent, phase, pass_.attempt)
        await ctx.enter_phase(phase.name, pass_.attempt, [slot.agent])

    await together([enter(slot) for slot in slots])
    if ctx.stop_requested or not slots:
        return
    mark = runner.mark()
    for slot in slots:
        slot.context = runner.package(phase, pass_, slot, repetition=1, mark=mark)
        slot.mark = mark
        runner.acted.add(slot.agent)
    channel = phase.channels[0]
    config = ctx.config.channel(channel)
    waker = runner.waker
    shared = waker is not None and config.mode is ChannelMode.EVENT
    members = [s.agent for s in slots]
    by_agent = {s.agent: s for s in slots}
    room = getattr(ctx, "room", None)

    def finished(agent: str) -> bool:
        """Handed in, and no open question to another discussing agent (a question to an
        agent on standby or not in the phase holds nothing, §6.4, Entscheidung 64)."""
        if runner._newest(by_agent[agent], phase.name, pass_.attempt, mark) is None:
            return False
        asked = room.open_questions_by(agent) if room is not None else []
        return not any(set(q.to) & set(members) for q in asked if not is_user_question(q))

    seats = DiscussionSeats(
        members=members,
        contexts={s.agent: s.context for s in slots if s.context is not None},
        first={s.agent: render_assignment(s.context) for s in slots if s.context is not None},
        ping_pong=waker.trackers.get(channel) if shared and waker is not None else None,
        finished=finished,
    )
    if shared and waker is not None:
        waker.delegated.add(channel)  # the discussion counts the exchanges in its channel
    try:
        outcome = await ctx.discuss(channel, stage=phase.name, rounds=phase.rounds, seats=seats)
    finally:
        if waker is not None:
            waker.delegated.discard(channel)
    for slot in slots:
        slot.found = runner._newest(slot, phase.name, pass_.attempt, mark)
    if ctx.stop_requested or outcome.ended_by in (DiscussionEnd.STOPPED, DiscussionEnd.LIMIT):
        return

    async def ask(slot: Slot) -> None:
        assert slot.context is not None
        if ctx.stop_requested:
            return
        prompt = ask_again_prompt(slot.context)
        await ctx.turn(slot.agent, prompt, stage=phase.name, context=slot.context)
        slot.turns += 1
        slot.found = runner._newest(slot, phase.name, pass_.attempt, mark)

    await together([ask(slot) for slot in slots if slot.found is None])
    await runner.after_actions(phase, pass_)
