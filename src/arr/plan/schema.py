"""JSON schema of a group with `topology: phases` (docs/10 §4.10, P11; W4b-02a §6.7).

One model is the only source: the schema is generated from `GroupSpec` (for `group.yaml`) or
`RunInput` (for `config` of Claude Code's `start_run`) and narrowed to the phase toolkit. It
names every key of docs/10 §4.10, the built-in formats and the field kinds of own formats, and
it is stable: the same call gives the same schema. Whoever hands it to Claude Code or a
graphical editor decides W4b-07.
"""

import copy
from typing import Any, Literal

from arr.models import PLACEHOLDER_PATTERN, GroupSpec, RunInput
from arr.submissions.formats import BUILTIN, PUBLIC_KINDS

__all__ = ["json_schema"]

_OLD_KEYS = ("stages", "raters")
"""Keys of the old topologies that `check` rejects with `phases` (S2); not in the schema."""


def json_schema(target: Literal["group", "run"] = "group") -> dict[str, Any]:
    """The JSON schema of a `phases` group: `group` for a `group.yaml`, `run` for `config`
    (a `run.yaml` with `project` and `task`). A new dict on every call."""
    model = GroupSpec if target == "group" else RunInput
    schema = copy.deepcopy(model.model_json_schema(mode="validation", by_alias=True))
    props = schema["properties"]
    props["channels"] = props.pop("groupchats")
    props["channels"]["description"] = (
        "Kanäle (Gruppenchat oder Direktkanal); die ältere Schreibweise „groupchats“ wird "
        "ebenso gelesen, beide zusammen sind ein Fehler."
    )
    for key in _OLD_KEYS:
        props.pop(key, None)
    if target == "group":
        props["topology"] = {
            "anyOf": [
                {"const": "phases"},
                {
                    "type": "object",
                    "properties": {"kind": {"const": "phases"}},
                    "required": ["kind"],
                    "additionalProperties": False,
                },
            ],
            "description": "„phases“ schaltet den Phasen-Baukasten ein (docs/10 §4.10).",
        }
        schema["required"] = sorted({*schema.get("required", []), "phases"})
    defs = schema["$defs"]
    defs["FieldKind"] = {
        "enum": [kind.value for kind in PUBLIC_KINDS],
        "title": "FieldKind",
        "type": "string",
        "description": "Feldarten eigener Formate.",
    }
    for key in ("content", "needs_files"):
        defs["FormatSpec"]["properties"].pop(key, None)
    defs["FormatSpec"]["description"] = (
        "Ein eigenes Format unter „formats“: nur „fields“, gebaut aus den Feldarten choice, "
        "number, text und list. Jeder Wert darf ein ganzer Platzhalter „{{ params.<name> }}“ "
        "sein."
    )
    fmt = defs["SubmissionSpec"]["properties"]["format"]
    fmt["description"] = (
        f"Eingebaut: {', '.join(BUILTIN)}; sonst der Name eines eigenen Formats unter „formats“."
    )
    fmt["examples"] = list(BUILTIN)
    schema["title"] = "arr research group (topology: phases)"
    schema["description"] = (
        "Eine Research-Group aus Agenten, Kanälen und Phasen (docs/10 §4.10). Werte in "
        "„phases“, „formats“ und „seed“ dürfen ganze Platzhalter sein; Muster: "
        f"{PLACEHOLDER_PATTERN}"
    )
    return schema
