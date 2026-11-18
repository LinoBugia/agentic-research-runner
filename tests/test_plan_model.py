"""W4b-02a: the phase toolkit in `arr.models` and `arr.resolve` (docs/10 §4.9, §4.10).

A1 (the example of docs/10 §4.9 and every key of §4.10), A3 (`channels` = `groupchats`), A4
(placeholders), A5 (resolved form), A9 (JSON schema), A12 (`RunContext`), A13 (message for old
groups), A14 (imports), plus `compact` and `decider` at agents. No model, no run.
"""

import ast
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from plan_kit import FIXTURES, SETTINGS, findings, load, phase, resolve, text

import arr.plan
from arr.errors import ConfigError
from arr.models import (
    ActionSpec,
    AgentConfig,
    AgentSpec,
    Compact,
    ContextMode,
    FolderMode,
    GateRule,
    GateSpec,
    GateType,
    Goto,
    GroupSpec,
    InputContent,
    InputSpec,
    PhaseSpec,
    Pick,
    ResolvedAction,
    ResolvedGate,
    ResolvedInput,
    ResolvedPhase,
    ResolvedRun,
    ResolvedSeed,
    ResolvedSubmission,
    ResolvedTask,
    RouteKeyword,
    RunInput,
    SeedSpec,
    SubmissionSpec,
    TopologyKind,
    WorkfolderSettings,
)
from arr.plan.check import check
from arr.plan.legacy import TEMPLATES, old_topology_message
from arr.plan.schema import json_schema
from arr.resolve import (
    dump_yaml_model,
    line_of_path,
    load_group_spec,
    load_yaml_model,
    parse_yaml_text,
    resolve_run,
    validate_model,
)
from arr.runs.context import RunContext

# --------------------------------------------------------------------------------------------
# A1: the example of docs/10 §4.9; every key of §4.10 is in the model
# --------------------------------------------------------------------------------------------


def test_a1_the_example_loads_resolves_and_passes_the_check() -> None:
    spec = load_group_spec(FIXTURES / "beispiel.yaml")
    assert spec.topology is TopologyKind.PHASES and len(spec.phases) == 7
    resolved = resolve_run(RunInput(project="p", task="t"), spec, SETTINGS)
    result = check(resolved)
    assert result.errors == [] and result.warnings == []
    assert [p.name for p in resolved.phases] == [
        "ideen",
        "exploration",
        "beweis",
        "pruefung",
        "urteil",
        "revision",
        "zusammenfassung",
    ]


KEYS: list[tuple[str, type, str]] = [
    # (key of docs/10 §4.10, model in the input, field)
    ("topology: phases", GroupSpec, "topology"),
    ("channels", GroupSpec, "groupchats"),
    ("rubric", GroupSpec, "rubric"),
    ("formats", GroupSpec, "formats"),
    ("seed", GroupSpec, "seed"),
    ("seed.file", SeedSpec, "file"),
    ("seed.param", SeedSpec, "param"),
    ("compact", AgentSpec, "compact"),
    ("agent decider", AgentSpec, "decider"),
    ("compact (arr.yaml)", WorkfolderSettings, "compact"),
    ("name", PhaseSpec, "name"),
    ("topology", PhaseSpec, "topology"),
    ("when", PhaseSpec, "when"),
    ("actions", PhaseSpec, "actions"),
    ("standby", PhaseSpec, "standby"),
    ("phase channels", PhaseSpec, "channels"),
    ("rounds", PhaseSpec, "rounds"),
    ("isolated", PhaseSpec, "isolated"),
    ("over", PhaseSpec, "over"),
    ("repeat", PhaseSpec, "repeat"),
    ("context", PhaseSpec, "context"),
    ("folder", PhaseSpec, "folder"),
    ("inputs", PhaseSpec, "inputs"),
    ("max_visits", PhaseSpec, "max_visits"),
    ("on_exhausted", PhaseSpec, "on_exhausted"),
    ("max_assignments", PhaseSpec, "max_assignments"),
    ("phase decider", PhaseSpec, "decider"),
    ("gate", PhaseSpec, "gate"),
    ("task", ActionSpec, "task"),
    ("submission", ActionSpec, "submission"),
    ("turns", ActionSpec, "turns"),
    ("format", SubmissionSpec, "format"),
    ("submission name", SubmissionSpec, "name"),
    ("choices", SubmissionSpec, "choices"),
    ("from", InputSpec, "from_"),
    ("input name", InputSpec, "name"),
    ("channel", InputSpec, "channel"),
    ("peers", InputSpec, "peers"),
    ("pick", InputSpec, "pick"),
    ("content", InputSpec, "content"),
    ("gate type", GateSpec, "type"),
    ("field", GateSpec, "field"),
    ("rule", GateSpec, "rule"),
    ("routes", GateSpec, "routes"),
    ("on_tie", GateSpec, "on_tie"),
    ("raters", GateSpec, "raters"),
    ("gate action", GateSpec, "action"),
    ("pass_score", GateSpec, "pass_score"),
    ("criteria", GateSpec, "criteria"),
    ("goto", Goto, "goto"),
]

RESOLVED: list[tuple[type, set[str]]] = [
    (
        ResolvedPhase,
        {
            "name",
            "topology",
            "actions",
            "standby",
            "channels",
            "rounds",
            "isolated",
            "over",
            "repeat",
            "context",
            "folder",
            "inputs",
            "max_visits",
            "on_exhausted",
            "max_assignments",
            "decider",
            "gate",
        },
    ),
    (ResolvedAction, {"agent", "task", "submission", "turns"}),
    (ResolvedTask, {"task", "submission", "turns"}),
    (ResolvedSubmission, {"format", "name", "choices"}),
    (ResolvedInput, {"from_", "name", "channel", "peers", "pick", "content"}),
    (
        ResolvedGate,
        {"type", "field", "rule", "routes", "on_tie", "raters", "action", "pass_score", "criteria"},
    ),
    (ResolvedSeed, {"file", "param", "items"}),
    (ResolvedRun, {"phases", "formats", "seed", "groupchats"}),
    (AgentConfig, {"compact", "decider"}),
]


@pytest.mark.parametrize(("key", "model", "name"), KEYS, ids=[k for k, _m, _n in KEYS])
def test_a1_every_key_of_the_format_is_a_field(key: str, model: type, name: str) -> None:
    assert name in model.model_fields, key


@pytest.mark.parametrize(("model", "names"), RESOLVED, ids=[m.__name__ for m, _ in RESOLVED])
def test_a1_every_key_is_in_the_resolved_form(model: type, names: set[str]) -> None:
    assert names <= set(model.model_fields)


def test_a1_input_and_resolved_models_reject_unknown_keys() -> None:
    data = load()
    phase(data, "beweis")["stanby"] = ["strategist"]
    found = findings(data)
    issue = found.error("phases[2].stanby")
    assert "Meintest du „standby“?" in issue.message


# --------------------------------------------------------------------------------------------
# A3: `channels` and `groupchats`
# --------------------------------------------------------------------------------------------


def _with_typo(spelling: str) -> str:
    source = text("beispiel").replace("members: [judge-*]", "members: [judgee]")
    return source.replace("\nchannels:\n", f"\n{spelling}:\n")


@pytest.mark.parametrize("spelling", ["channels", "groupchats"])
def test_a3_both_spellings_read_the_same_and_name_the_right_line(spelling: str) -> None:
    good = text("beispiel").replace("\nchannels:\n", f"\n{spelling}:\n")
    data, _ = parse_yaml_text(good, "group.yaml")
    resolved = resolve(data)
    assert [c.name for c in resolved.groupchats] == ["rueckfrage", "judges"]
    assert resolved.agent("prover").channels == ["rueckfrage"]
    assert "groupchats:" in dump_yaml_model(resolved) and "\nchannels:" not in dump_yaml_model(
        resolved
    )
    source = _with_typo(spelling)
    data, lines = parse_yaml_text(source, "group.yaml")
    with pytest.raises(ConfigError) as info:
        resolve(data)
    [issue] = info.value.issues
    assert issue.path == f"{spelling}[1].members[0]"
    line = line_of_path(issue.path, lines)
    assert line is not None and "judgee" in source.splitlines()[line - 1]


def test_a3_both_together_are_an_error() -> None:
    data = load()
    data["groupchats"] = data["channels"]
    with pytest.raises(ConfigError) as info:
        validate_model(GroupSpec, data)
    assert info.value.path == "groupchats"
    assert "„channels“ und „groupchats“" in info.value.issue_message
    run = {"project": "p", "task": "t", "channels": [], "groupchats": []}
    with pytest.raises(ConfigError):
        validate_model(RunInput, run)
    # counter-check: each alone is fine, also in a run.yaml
    del data["groupchats"]
    assert validate_model(GroupSpec, data).channels_key == "channels"
    run = validate_model(RunInput, {"project": "p", "task": "t", "channels": []})
    assert run.channels_key == "channels"


def test_a3_channels_of_a_run_replace_those_of_the_skill() -> None:
    spec = validate_model(GroupSpec, load())
    run = validate_model(
        RunInput,
        {
            "project": "p",
            "task": "t",
            "channels": [
                {"name": "rueckfrage", "members": ["prover", "strategist"]},
                {"name": "judges", "members": ["judge-*"], "mode": "event"},
            ],
        },
    )
    resolved = resolve_run(run, spec, SETTINGS)
    assert resolved.channel("judges").mode.value == "event"


# --------------------------------------------------------------------------------------------
# A4: placeholders as whole values in phases, formats and seed; `when` at phases
# --------------------------------------------------------------------------------------------


def _templated() -> dict[str, Any]:
    data = load()
    data["params"] = {
        "runden": {"default": 2, "min": 1, "max": 5},
        "regel": {"default": "majority", "choices": ["majority", "unanimous"]},
        "name": {"default": "beweis"},
        "zeichen": {"default": 300},
        "datei": {"default": None},
        "mit_summary": {"default": True},
        "versuche": {"default": 2},
    }
    phase(data, "urteil")["rounds"] = "{{ params.runden }}"
    phase(data, "urteil")["gate"]["rule"] = "{{ params.regel }}"
    phase(data, "beweis")["actions"]["prover"]["submission"]["name"] = "{{ params.name }}"
    phase(data, "revision")["max_visits"] = "{{ params.versuche }}"
    phase(data, "zusammenfassung")["when"] = {"param": "mit_summary", "equals": True}
    data["formats"] = {
        "notiz": {"fields": {"text": {"type": "text", "max_chars": "{{ params.zeichen }}"}}}
    }
    data["seed"] = {"vorlage": {"file": "{{ params.datei }}"}}
    return data


def test_a4_placeholders_load_and_are_inserted() -> None:
    data = _templated()
    spec = validate_model(GroupSpec, data)  # before W4b-02a a number field refused this
    assert spec.phases[4].rounds == "{{ params.runden }}"
    found = findings(data)
    assert found.errors == [], found.errors
    resolved = found.resolved
    assert resolved is not None
    urteil = resolved.phase("urteil")
    assert (urteil.rounds, urteil.gate.rule) == (2, GateRule.MAJORITY)
    assert resolved.phase("beweis").actions[0].submission.name == "beweis"
    assert resolved.phase("revision").max_visits == 2
    assert resolved.formats["notiz"].fields["text"].max_chars == 300
    assert resolved.seed == {}, "an empty value creates nothing"
    other = findings(data, {"runden": 4, "regel": "unanimous", "datei": "tex/b.tex"}).resolved
    assert other is not None
    assert other.phase("urteil").rounds == 4 and other.seed["vorlage"].file == "tex/b.tex"
    assert other.phase("urteil").gate.rule is GateRule.UNANIMOUS


def test_a4_unknown_parameter_and_wrong_value_have_paths() -> None:
    data = _templated()
    phase(data, "urteil")["rounds"] = "{{ params.runde }}"
    with pytest.raises(ConfigError) as info:
        validate_model(GroupSpec, data) and resolve(data)
    assert info.value.path == "phases[4].rounds"
    assert "verweist auf keinen Parameter" in info.value.issue_message
    data = _templated()
    data["params"]["runden"] = {"default": "viele"}
    found = findings(data)
    issue = found.error("phases[4].rounds")
    assert "ganze Zahl" in issue.message and "„{{ params.runden }}“" in issue.message
    data = _templated()
    data["params"]["regel"] = {"default": "mehrheit"}
    assert "mehrheit" in findings(data).error("phases[4].gate.rule").message
    data = _templated()
    data["params"]["zeichen"] = {"default": 0}
    assert "params.zeichen" in findings(data).error("formats.notiz.fields.text.max_chars").message
    # counter-check: the values of the declaration pass
    assert findings(_templated()).errors == []


def test_a4_when_at_a_phase() -> None:
    data = _templated()
    off = findings(data, {"mit_summary": False}).resolved
    assert off is not None and "zusammenfassung" not in [p.name for p in off.phases]
    assert off.phase("urteil").gate.routes["accept"] is RouteKeyword.STOP
    data["phases"][6]["when"] = {"param": "gibtsnicht", "equals": 1}
    with pytest.raises(ConfigError) as info:
        resolve(data)
    assert info.value.path == "phases[6].when.param"


# --------------------------------------------------------------------------------------------
# A5: resolved form
# --------------------------------------------------------------------------------------------


def test_a5_agents_defaults_and_written_out_values() -> None:
    resolved = resolve(load())
    exploration = resolved.phase("exploration")
    assert [a.agent for a in exploration.actions] == ["explorer-1", "explorer-2"]
    assert {a.key for a in exploration.actions} == {"explorer-*"}
    assert exploration.gate.raters == ["judge-1", "judge-2"]
    assert exploration.gate.action is not None and exploration.gate.action.turns == 1
    pruefung = resolved.phase("pruefung")
    assert pruefung.context == {"judge-1": ContextMode.RESET, "judge-2": ContextMode.RESET}
    assert pruefung.folder == {"judge-1": FolderMode.FRESH, "judge-2": FolderMode.FRESH}
    assert pruefung.gate.type is GateType.DONE
    assert pruefung.gate.routes == {"done": RouteKeyword.NEXT}
    beweis = resolved.phase("beweis")
    assert beweis.context == {"prover": ContextMode.KEEP, "strategist": ContextMode.KEEP}
    assert beweis.folder == {"prover": FolderMode.KEEP, "strategist": FolderMode.KEEP}
    assert beweis.repeat == 1 and beweis.decider == "user"
    [entry] = beweis.inputs or []
    assert (entry.from_, entry.pick, entry.content) == (
        "exploration",
        Pick.BEST,
        InputContent.SUMMARY,
    )
    assert resolved.phase("ideen").gate.routes == {}
    assert resolved.phase("urteil").rounds == 1
    assert resolved.phase("revision").on_exhausted is RouteKeyword.NEXT
    assert resolved.agent("prover").compact == Compact(at="60%")


def test_a5_dropped_phases_and_their_routes() -> None:
    data = load()
    data["params"] = {"schreiber": {"default": 1, "min": 0, "max": 1}}
    data["agents"][5]["count"] = "{{ params.schreiber }}"  # writer → writer-1
    phase(data, "zusammenfassung")["actions"] = {
        "writer-*": phase(data, "zusammenfassung")["actions"]["writer"]
    }
    data["params"]["pruefen"] = {"default": True}
    phase(data, "pruefung")["when"] = {"param": "pruefen", "equals": True}
    resolved = resolve(data, {"schreiber": 0, "pruefen": False})
    names = [p.name for p in resolved.phases]
    assert "zusammenfassung" not in names and "pruefung" not in names
    # a goto to a dropped phase leads to the next one that stays, else the run ends
    assert resolved.phase("urteil").gate.routes["accept"] is RouteKeyword.STOP
    assert resolved.phase("revision").gate.routes["done"] == Goto(goto="urteil")
    assert check(resolved).errors == []
    # a name that does not exist at all is an error
    phase(data, "revision")["gate"]["routes"]["done"] = {"goto": "pruefng"}
    with pytest.raises(ConfigError) as info:
        resolve(data)
    assert info.value.path == "phases[5].gate.routes.done"
    assert "Meintest du „pruefung“?" in info.value.issue_message


def test_a5_resolved_run_survives_run_yaml(tmp_path: Path) -> None:
    for data in (load(), _templated()):
        resolved = resolve(data, None)
        path = tmp_path / "run.yaml"
        path.write_text(dump_yaml_model(resolved), encoding="utf-8")
        again = load_yaml_model(path, ResolvedRun)
        assert again == resolved
    seeded = resolve(load("exploration"), {"ideas": ["Induktion", "Widerspruch"]})
    path.write_text(dump_yaml_model(seeded), encoding="utf-8")
    assert load_yaml_model(path, ResolvedRun) == seeded
    assert seeded.seed["ideen"] == ResolvedSeed(param="ideas", items=["Induktion", "Widerspruch"])


def test_a5_old_run_yaml_without_phases_still_loads(tmp_path: Path) -> None:
    old = (Path(__file__).parent / "fixtures" / "config" / "run.yaml").read_text("utf-8")
    assert "phases" not in old
    spec = load_yaml_model(Path(__file__).parent / "fixtures" / "config" / "group.yaml", GroupSpec)
    assert spec.phases == [] and spec.channels_key == "groupchats"


def test_a5_phases_of_a_run_replace_those_of_the_skill() -> None:
    spec = validate_model(GroupSpec, load())
    own = [
        {
            "name": "nur",
            "topology": "solo",
            "actions": {"writer": {"task": "t.md", "submission": {"format": "text"}}},
        }
    ]
    run = validate_model(RunInput, {"project": "p", "task": "t", "phases": own})
    resolved = resolve_run(run, spec, SETTINGS)
    assert [p.name for p in resolved.phases] == ["nur"]


# --------------------------------------------------------------------------------------------
# compact and decider at agents
# --------------------------------------------------------------------------------------------


def test_compact_layers_and_values() -> None:
    data = load()
    data["defaults"] = {"compact": {"at": "70%"}}
    resolved = resolve(data)
    assert resolved.agent("prover").compact.at == "60%"  # the agent's own value wins
    assert resolved.agent("writer").compact.at == "70%"
    settings = SETTINGS.model_copy(update={"compact": Compact(at=150_000)})
    spec = validate_model(GroupSpec, load())
    other = resolve_run(RunInput(project="p", task="t"), spec, settings)
    assert other.agent("writer").compact.tokens == 150_000
    assert other.agent("writer").compact.share is None
    assert Compact().share == 0.6 and Compact(at=" 5 %").at == "5%"
    for bad in ("0%", "101%", "sechzig", 0, True, -5, 10**9):
        with pytest.raises(ValueError):
            Compact(at=bad)


def test_decider_knows_only_user() -> None:
    data = load()
    data["agents"][3]["decider"] = "user"
    assert resolve(data).agent("prover").decider == "user"
    data["agents"][3]["decider"] = "gatekeeper"
    with pytest.raises(ConfigError) as info:
        resolve(data)
    assert info.value.path == "agents[3].decider"
    data = load()
    phase(data, "beweis")["decider"] = "gatekeeper"
    with pytest.raises(ConfigError):
        validate_model(GroupSpec, data)


# --------------------------------------------------------------------------------------------
# A9: JSON schema
# --------------------------------------------------------------------------------------------


def _keys(schema: Any) -> set[str]:
    found: set[str] = set()
    stack = [schema]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            props = item.get("properties")
            if isinstance(props, dict):
                found |= set(props)
            stack += list(item.values())
        elif isinstance(item, list):
            stack += item
    return found


def test_a9_json_schema_names_every_key_and_is_stable() -> None:
    schema = json_schema()
    assert json.dumps(schema, sort_keys=True) == json.dumps(json_schema(), sort_keys=True)
    keys = _keys(schema)
    wanted = {
        "topology", "channels", "rubric", "formats", "seed", "compact", "decider", "phases",
        "name", "when", "actions", "standby", "rounds", "isolated", "over", "repeat",
        "context", "folder", "inputs", "max_visits", "on_exhausted", "max_assignments",
        "gate", "task", "submission", "turns", "format", "choices", "from", "channel",
        "peers", "pick", "content", "type", "field", "rule", "routes", "on_tie", "raters",
        "action", "pass_score", "criteria", "goto", "file", "param", "fields", "max_chars",
        "min", "max", "item", "min_items", "max_items",
    }  # fmt: skip
    assert wanted <= keys, wanted - keys
    assert {"stages", "raters"} & set(schema["properties"]) == set()
    assert "groupchats" not in schema["properties"]
    assert schema["$defs"]["FieldKind"]["enum"] == ["choice", "number", "text", "list"]
    fmt = schema["$defs"]["SubmissionSpec"]["properties"]["format"]
    assert fmt["examples"] == ["text", "artifact", "review", "rating", "list", "decision"]
    rounds = schema["$defs"]["PhaseSpec"]["properties"]["rounds"]["anyOf"]
    assert any("pattern" in option for option in rounds), "placeholders are allowed"
    run = json_schema("run")
    assert {"project", "task", "phases", "channels"} <= set(run["properties"])


def test_a9_schema_accepts_the_example() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(load(), json_schema())


# --------------------------------------------------------------------------------------------
# A12: new methods of RunContext
# --------------------------------------------------------------------------------------------

NEW_METHODS = {
    "enter_phase": ("W4b-02b", ["self", "stage", "attempt", "agents"]),
    "submissions": (
        "W4b-02b",
        ["self", "agent", "stage", "attempt", "name", "action", "format", "latest"],
    ),
    "latest_version": ("W4b-02b", ["self", "name"]),
    "add_version": (
        "W4b-02b",
        ["self", "name", "summary", "format", "body", "files", "root", "fields", "stage"],
    ),
    "fresh_folder": ("W4b-05a", ["self", "agent_id", "stage", "attempt"]),
    "compact_session": ("W4b-05b", ["self", "agent_id", "note"]),
}


async def test_a12_new_methods_are_not_abstract_and_name_their_order() -> None:
    assert not set(NEW_METHODS) & RunContext.__abstractmethods__
    names = list(vars(RunContext))
    assert names.index("read_output") < min(names.index(n) for n in NEW_METHODS)

    class Ctx(RunContext):
        pass

    Ctx.__abstractmethods__ = frozenset()
    ctx = Ctx()  # type: ignore[abstract]
    calls = {
        "enter_phase": lambda: ctx.enter_phase("p", 1, ["a"]),
        "submissions": lambda: ctx.submissions(stage="p"),
        "latest_version": lambda: ctx.latest_version("beweis"),
        "add_version": lambda: ctx.add_version("beweis", summary="s"),
        "fresh_folder": lambda: ctx.fresh_folder("a", stage="p", attempt=1),
        "compact_session": lambda: ctx.compact_session("a", note="n"),
    }
    for name, (order, params) in NEW_METHODS.items():
        assert list(inspect.signature(getattr(RunContext, name)).parameters) == params
        with pytest.raises(NotImplementedError, match=order):
            result = calls[name]()
            if inspect.iscoroutine(result):
                await result


# --------------------------------------------------------------------------------------------
# A13: message for groups of the old topologies (built, not switched on)
# --------------------------------------------------------------------------------------------


def test_a13_message_names_the_template_and_is_not_switched_on() -> None:
    for kind, (skill, _phases) in TEMPLATES.items():
        message = old_topology_message(kind)
        assert message is not None and f"„{kind.value}“ gibt es nicht mehr" in message
        assert "topology: phases" in message
        if skill:
            assert f"Start-Skill „{skill}“" in message
    assert set(TEMPLATES) == set(TopologyKind) - {TopologyKind.PHASES}
    assert old_topology_message("phases") is None and old_topology_message("xyz") is None
    src = Path(arr.plan.__file__).parent.parent
    users = [
        p.relative_to(src)
        for p in src.rglob("*.py")
        if "old_topology_message" in p.read_text(encoding="utf-8") and p.parent.name != "plan"
    ]
    assert users == [], "W4b-06 switches the message on, not W4b-02a"
    # the old topologies still load and resolve (A2)
    old = validate_model(GroupSpec, {"topology": "review", "agents": [{"id": "a"}]})
    assert old.topology is TopologyKind.REVIEW


# --------------------------------------------------------------------------------------------
# A14: imports
# --------------------------------------------------------------------------------------------

ALLOWED = ("arr.models", "arr.errors", "arr.submissions", "arr.plan")


def _arr_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
    return {name for name in found if name == "arr" or name.startswith("arr.")}


def test_a14_the_package_imports_only_models_errors_and_submissions() -> None:
    folder = Path(arr.plan.__file__).parent
    modules = sorted(folder.glob("*.py"))
    assert {p.name for p in modules} >= {"check.py", "schema.py", "estimate.py", "read.py"}
    for path in modules:
        for name in _arr_imports(path):
            assert name.startswith(ALLOWED), f"{path.name} importiert {name}"
    formats = Path(arr.plan.__file__).parent.parent / "submissions" / "formats.py"
    assert _arr_imports(formats) == set(), "formats needs nothing of arr: no cycle"


def test_a14_models_import_alone_and_old_imports_work() -> None:
    code = (
        "import sys, arr.models\n"
        "from arr.models import StrictModel, Slug, Rubric\n"
        "from arr.submissions.formats import FieldSpec, FormatSpec, Expectation\n"
        "import arr.submissions.formats as f\n"
        "assert StrictModel is f.StrictModel and Rubric is f.Rubric\n"
        "print(sorted(m for m in sys.modules if m == 'arr' or m.startswith('arr.')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    )
    assert (
        out.stdout.strip() == "['arr', 'arr.models', 'arr.submissions', 'arr.submissions.formats']"
    )
