"""The phase runner: runs a `topology: phases` run (docs/10 §4, W4b-02b).

The modules, from the bottom up:

- `text`: agent text as data (cleaned, one line where it is a name, every part and the whole
  context package limited) and the limits in one place.
- `view`: the context package as text, for the prompt of a turn and for `get_assignment`
  (imports nothing of the runner, so the room server can use it).
- `tasks`: the assignment files, read from the run folder at the start, placeholders
  inserted, filed under `tasks/` of the run folder.
- `gates`: what a gate computes from submissions, without a model (`signal`, `judge`), and the
  run's list of objections.
- `inputs`: the inputs, the feedback of a repeated pass and the summary for the user.
- `start`: what the runner refuses before the start (and again at its own start).
- `work`: one pass of a phase `solo` or `parallel`: actions, turns, waits, `isolated`, `over`,
  `repeat`, and the rating actions of a gate `judge`.
- `chat`: one pass of a phase `chat`: the discussion of the agents with an action in the first
  channel of the phase, one follow-up question each (W4b-03).
- `moderated`: one pass of a phase `moderated`: a lead hands out assignments to the agents on
  standby with `assign`, they hand in to it (W4b-04).
- `wake`: waking in every phase (agents on standby, the end of `wait_until` by a message) and
  holding a phase while a question to the user is open (B3) (W4b-03).
- `runner`: the run over all phases: seeds, entering phases, gates, routes, loops,
  checkpoints, the user's choice.

A topology of a phase is one function `run_pass(runner, pass_)` registered in
`runner.PASSES` (`solo`, `parallel`, `chat`, `moderated`); when its actions are done it calls
`runner.after_actions` (end of waking, B3).

This `__init__` imports nothing, so `arr.room.server` can import `view` without the runner.
"""
