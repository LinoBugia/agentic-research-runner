"""The phase toolkit as data: check, JSON schema, estimate and reading helpers (docs/10 §4.10,
W4b-02a).

The models live in `arr.models` (one tree, one source); this package works on them:

- `check.check(resolved)`: every static rule (W4b-02a §6.4, §6.5) as a pure function, errors
  and hints apart (`PlanCheck`).
- `schema.json_schema(target)`: the JSON schema of a `phases` group (`group.yaml`, `config`).
- `estimate.turn_range(resolved)`: fewest and most turns of every topology, `phases` with loops.
- `read`: the expected submission of an action (`expectation`), where a route leads (`follow`).
- `graph`: the routes as a graph (L1–L4, bounds of the estimate).
- `legacy.old_topology_message(kind)`: the message for groups of the old topologies (W4b-06).

The package imports only `arr.models`, `arr.errors` and `arr.submissions`, nothing from
`arr.runs`, `arr.room`, `arr.service`, `arr.backends` or `arr.permissions` (W4b-02a §6.1). The
runner of W4b-02b lives in `arr.runs.phases`.
"""
