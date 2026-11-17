"""Submissions of the phase toolkit (docs/10 §4.3, §4.12; W4b-01).

- `formats`: the formats as data (field kinds, the six built-in formats, own formats), no I/O.
- `check`: checks one call of the room tool `submit` against its expected format, no I/O.
- `text`: `clean_text`, the rule for text of agents (also used by `arr.service.user_api`).
- `diff`: the unified diff of two versions in a process of its own.

The room (`arr.room.store`) stores and files submissions; this package imports nothing from
`arr.room`, `arr.runs` or `arr.service`.
"""
