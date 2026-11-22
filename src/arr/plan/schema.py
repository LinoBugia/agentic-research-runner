"""JSON schema of a group with `topology: phases` (docs/10 §4.10, P11; W4b-02a §6.7).

One model is the only source: the schema is generated from `GroupSpec` (for `group.yaml`) or
`RunInput` (for `config` of Claude Code's `start_run`) and narrowed to the phase toolkit. It
names every key of docs/10 §4.10, the built-in formats and the field kinds of own formats, and
it is stable: the same call gives the same schema. Whoever hands it to Claude Code or a
graphical editor decides W4b-07.
"""

import copy
from typing import Any, Literal

from arr.models import MAX_COMPACT_TOKENS, PLACEHOLDER_PATTERN, GroupSpec, RunInput
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
    else:  # NG6: the short form `topology: phases` as in a group, plus `params` of a run
        props["topology"] = {
            "anyOf": [
                {"const": "phases"},
                {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "phases"},
                        "params": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["kind"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ],
            "default": None,
            "description": (
                "„phases“ schaltet den Phasen-Baukasten ein (docs/10 §4.10); in der langen Form "
                "setzt „params“ Parameter des Skills. Ohne „topology“ gilt die des Skills."
            ),
        }
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
    _narrow(schema, target)
    schema["title"] = "arr research group (topology: phases)"
    schema["description"] = (
        "Eine Research-Group aus Agenten, Kanälen und Phasen (docs/10 §4.10). Werte in "
        "„phases“, „formats“ und „seed“ dürfen ganze Platzhalter sein; Muster: "
        f"{PLACEHOLDER_PATTERN}. Was das Schema nicht ausdrückt, prüft arr beim Laden und mit "
        "„check“: Namen, die es geben muss (Agenten, Phasen, Kanäle, Abgaben, Parameter), "
        "Routen-Wörter (next, retry, stop, escalate), welche Schlüssel zu welcher Topologie "
        "und welchem Gate passen, Schleifen und Isolation; Platzhalter innerhalb eines "
        'eigenen Formats; Zahlen als Text ("2") und Wahrheitswerte als Text (etwa '
        '„isolated“: "true") nimmt arr an, das Schema nicht.'
    )
    return schema


SLUG = "^[a-z0-9][a-z0-9_-]{0,63}$"
"""Pattern of a name (`arr.models.Slug`): phases, `goto`, keys of `seed` and `formats`."""
PERCENT = "^\\s*(100|[1-9][0-9]?)\\s*%\\s*$"
"""Pattern of `compact.at` as a share: 1 % to 100 %."""


def _narrow(schema: dict[str, Any], target: str) -> None:
    """Make the schema refuse what the model refuses, where a JSON schema can (W4b-02a.1,
    G11): names as slugs, `compact.at`, `seed` with exactly one source, `when` with exactly one
    test, no empty route, a run with exactly one of `task` and `task_file`."""
    defs = schema["$defs"]
    defs["PhaseSpec"]["properties"]["name"]["pattern"] = SLUG
    for option in defs["Goto"]["properties"]["goto"]["anyOf"]:
        option.setdefault("pattern", SLUG)
    defs["Compact"]["properties"]["at"]["anyOf"] = [
        {"type": "string", "pattern": PERCENT},
        {"type": "integer", "minimum": 1, "maximum": MAX_COMPACT_TOKENS},
    ]
    defs["SeedSpec"]["minProperties"] = 1
    defs["SeedSpec"]["maxProperties"] = 1
    defs["WhenCondition"]["minProperties"] = 2
    defs["WhenCondition"]["maxProperties"] = 2
    props = schema["properties"]
    seed = props["seed"]
    seed["patternProperties"] = {SLUG: seed.pop("additionalProperties")}
    seed["additionalProperties"] = False
    props["formats"]["additionalProperties"] = False
    stack: list[Any] = [schema]
    while stack:  # every route given as a word is not empty
        item = stack.pop()
        if isinstance(item, dict):
            options = item.get("anyOf")
            if isinstance(options, list) and {"$ref": "#/$defs/Goto"} in options:
                for option in options:
                    if option.get("type") == "string":
                        option["minLength"] = 1
            stack += list(item.values())
        elif isinstance(item, list):
            stack += item
    if target == "run":
        schema["oneOf"] = [{"required": ["task"]}, {"required": ["task_file"]}]
