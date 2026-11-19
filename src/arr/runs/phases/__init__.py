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
- `runner`: the run over all phases: seeds, entering phases, gates, routes, loops,
  checkpoints, the user's choice.

Hooks for the later orders: a topology of a phase is one function `run_pass(runner, pass_)`
registered in `runner.PASSES`. W4b-03 (`chat`, `standby`) and W4b-04 (`moderated`) add a module
each, register it there and remove their entry from `start.NOT_YET` (and, for `standby`,
`start.STANDBY_READY`). Nothing else of the runner changes.

This `__init__` imports nothing, so `arr.room.server` can import `view` without the runner.
"""
