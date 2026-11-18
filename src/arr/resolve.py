"""Load `run.yaml` / `group.yaml` and resolve them into a `ResolvedRun`.

Resolution (docs/02-workfolder-und-runs.md §3.3, docs/03-skills-und-research-groups.md §3):
parameters against their declaration, `{{ params.<name> }}` placeholders, `when`, defaults,
`count` (→ `<id>-1 … <id>-n`), `mix` (round-robin), patterns in `members` (`fnmatch`).
All problems are reported as `ConfigError` with a path such as `agents[1].model` and a German
message following docs/08-ux-konzept.md §8.4.

Layers for agent fields, lowest first: built-in, `project.yaml read_dirs`, `arr.yaml
defaults.<provider>`, `group.yaml defaults`, `run.yaml defaults`, the agent, its `mix` entry.
`model`, `auth` and `effort` come only from layers whose effective provider equals the
agent's final provider (see `_merge_agent`). `tools` merges field by field, all other
fields replace. Limits merge field by field over `BUILTIN_LIMITS`.
"""

import contextlib
import difflib
import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import MarkedYAMLError

from arr.errors import ConfigError, ConfigIssue
from arr.models import (
    BUILTIN_LIMITS,
    DEFAULT_CHAT_ROUNDS,
    DEFAULT_TURNS,
    MAX_COUNT,
    PLACEHOLDER_RE,
    ROUTE_HELP,
    ActionSpec,
    AgentConfig,
    AgentDefaults,
    AgentSpec,
    ApprovalSettings,
    ChannelMode,
    ContextMode,
    FolderMode,
    FormatSpec,
    GateRule,
    GateSpec,
    GateType,
    Goto,
    GroupChatConfig,
    GroupSpec,
    InputContent,
    Limits,
    PhaseSpec,
    PhaseTopology,
    Pick,
    ProjectSettings,
    Provider,
    ResolvedPhase,
    ResolvedRun,
    ResolvedSeed,
    Route,
    RouteKeyword,
    Rubric,
    RunInput,
    SeedSpec,
    StageSpec,
    TopologyConfig,
    TopologyKind,
    WhenCondition,
    WorkfolderSettings,
    WorkspaceMode,
    is_placeholder,
)

__all__ = [
    "ConfigError",
    "ConfigIssue",
    "check_group_spec",
    "dump_yaml_model",
    "load_group_spec",
    "load_run_input",
    "line_of_path",
    "load_yaml_model",
    "parse_yaml_text",
    "resolve_run",
    "too_nested",
    "validate_model",
    "validation_issues",
]

Loc = tuple[str | int, ...]

_PARAM_REF_RE = re.compile(r"^params\.([A-Za-z_][A-Za-z0-9_-]*)$")
_PROVIDER_BOUND = ("model", "auth", "effort")
_SPEC_ONLY = {"id", "count", "mix", "when"}

RUN_SOURCE = "run.yaml"
GROUP_SOURCE = "group.yaml"
OVERRIDES_SOURCE = "Start-Parameter"

# --------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------


MAX_YAML_DEPTH = 64
"""Deepest nesting of sections and lists arr accepts in a YAML file (W4-02.1)."""
MAX_YAML_NODES = 50_000
"""Most values a YAML file may expand to, with its aliases (`*name`) resolved (W4-02.1)."""
TOO_NESTED = (
    "Die Datei ist zu verschachtelt: mehr als {depth} Ebenen oder mehr als {nodes} Einträge, "
    "wenn man die Verweise (&name, *name) auflöst. Vereinfache die Struktur und verzichte auf "
    "verschachtelte Verweise."
).format(depth=MAX_YAML_DEPTH, nodes=f"{MAX_YAML_NODES:,}".replace(",", "."))
"""Message for a YAML alias bomb or a nesting deeper than `MAX_YAML_DEPTH`."""


class _TooNested(Exception):
    """A YAML document exceeds `MAX_YAML_DEPTH` or `MAX_YAML_NODES`."""


class _Budget:
    """Counts the values `_to_plain` creates; aliases count every time they are used."""

    def __init__(self) -> None:
        self.nodes = 0

    def take(self, depth: int) -> None:
        self.nodes += 1
        if self.nodes > MAX_YAML_NODES or depth > MAX_YAML_DEPTH:
            raise _TooNested


def _to_plain(
    node: Any, loc: Loc, lines: dict[Loc, int], budget: _Budget | None = None, depth: int = 0
) -> Any:
    """Convert ruamel round-trip nodes to plain Python values and record key lines.

    Every value counts against `budget`, also each use of an alias, so a small file whose
    aliases expand to millions of values (an alias bomb) stops early with `_TooNested`.
    """
    budget = budget if budget is not None else _Budget()
    budget.take(depth)
    if isinstance(node, CommentedMap):
        out: dict[Any, Any] = {}
        for key, value in node.items():
            child = (*loc, key)
            with contextlib.suppress(KeyError, TypeError, AttributeError):
                lines[child] = node.lc.key(key)[0] + 1
            out[key] = _to_plain(value, child, lines, budget, depth + 1)
        return out
    if isinstance(node, CommentedSeq):
        items = []
        for index, value in enumerate(node):
            child = (*loc, index)
            with contextlib.suppress(KeyError, TypeError, AttributeError, IndexError):
                lines[child] = node.lc.item(index)[0] + 1
            items.append(_to_plain(value, child, lines, budget, depth + 1))
        return items
    if isinstance(node, bool):
        return bool(node)
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if isinstance(node, str):
        return str(node)
    return node


def too_nested(data: Any) -> bool:
    """True if plain data (e.g. from a safe YAML load) is deeper than `MAX_YAML_DEPTH` or has
    more than `MAX_YAML_NODES` values, shared objects counted at every use. Iterative, so it
    never recurses itself."""
    stack: list[tuple[Any, int]] = [(data, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_YAML_NODES or depth > MAX_YAML_DEPTH:
            return True
        if isinstance(value, dict):
            stack.extend((v, depth + 1) for v in value.values())
            stack.extend((k, depth + 1) for k in value if isinstance(k, tuple))
        elif isinstance(value, list | tuple):
            stack.extend((v, depth + 1) for v in value)
    return False


def _read_yaml(path: Path, source: str) -> tuple[Any, dict[Loc, int]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError.single(
            "", f"Datei nicht gefunden: {path}. Prüfe den Pfad.", source=source
        ) from None
    except OSError as exc:
        raise ConfigError.single(
            "", f"Datei nicht lesbar: {path} ({exc.strerror}).", source=source
        ) from None
    return parse_yaml_text(text, source)


def parse_yaml_text(text: str, source: str) -> tuple[Any, dict[Loc, int]]:
    """Parse YAML text into plain values and the 1-based line of every key and list item.

    Raises `ConfigError` (with the line) for invalid YAML, an empty text or a text that is no
    section of `key: value` entries. Used for files and for the skill check (W4-02).
    """
    try:
        raw = YAML(typ="rt").load(text)
    except RecursionError:
        raise ConfigError.single("", TOO_NESTED, source=source) from None
    except DuplicateKeyError as exc:
        line = exc.problem_mark.line + 1 if exc.problem_mark else None
        raise ConfigError.single(
            "",
            "Ein Schlüssel kommt doppelt vor. Entferne oder benenne einen der beiden Einträge.",
            source=source,
            line=line,
        ) from None
    except MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        line = mark.line + 1 if mark else None
        raise ConfigError.single(
            "",
            "Die Datei ist kein gültiges YAML. Prüfe in dieser Zeile Einrückung, "
            "Doppelpunkte und Anführungszeichen.",
            source=source,
            line=line,
        ) from None
    lines: dict[Loc, int] = {}
    try:
        data = _to_plain(raw, (), lines)
    except (_TooNested, RecursionError):
        raise ConfigError.single("", TOO_NESTED, source=source) from None
    if data is None:
        raise ConfigError.single("", "Die Datei ist leer.", source=source)
    if not isinstance(data, dict):
        raise ConfigError.single(
            "", "Die Datei muss aus Einträgen der Form „schlüssel: wert“ bestehen.", source=source
        )
    return data, lines


def validate_model[M: BaseModel](
    model: type[M],
    data: Any,
    *,
    source: str | None = None,
    lines: Mapping[Loc, int] | None = None,
) -> M:
    """Validate plain data (e.g. a `config` dict from `start_run`) into `model`.

    Raises `ConfigError` with German messages instead of pydantic's `ValidationError`.
    """
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        issues = validation_issues(exc, lines=lines, channels=_channels_spelling(data))
        raise ConfigError(issues, source=source) from None


def _channels_spelling(data: Any) -> str:
    """`channels` if the input names its channels so (and not also `groupchats`): an error in
    that section then names the key that is in the file (W4b-02a.1, M1)."""
    if isinstance(data, dict) and "channels" in data and "groupchats" not in data:
        return "channels"
    return "groupchats"


def load_yaml_model[M: BaseModel](
    path: str | Path, model: type[M], *, source: str | None = None
) -> M:
    """Load a YAML file into `model`; raise `ConfigError` with paths and line numbers.

    Usable for every user file (`arr.yaml` → `WorkfolderSettings`, `project.yaml` →
    `ProjectSettings`, `profiles.yaml` → `ProfilesFile`, …).
    """
    path = Path(path)
    source = source or str(path)
    data, lines = _read_yaml(path, source)
    return validate_model(model, data, source=source, lines=lines)


def load_run_input(path: str | Path) -> RunInput:
    """Load a `run.yaml` as given by the user (docs/02 §3)."""
    return load_yaml_model(path, RunInput)


def load_group_spec(path: str | Path) -> GroupSpec:
    """Load a `group.yaml` and check placeholders and `when` against its `params` (docs/03 §3)."""
    path = Path(path)
    spec = load_yaml_model(path, GroupSpec)
    check_group_spec(spec, source=str(path))
    return spec


def dump_yaml_model(model: BaseModel, *, exclude_unset: bool = False) -> str:
    """Serialize a model as YAML (JSON-compatible values, field order kept, aliases used).

    Loading the text with `load_yaml_model` gives an equal model, e.g. for `run.yaml`.
    With `exclude_unset`, only fields given in the input are written (`run.input.yaml`).
    """
    yaml = YAML(typ="rt")
    yaml.default_flow_style = False
    yaml.allow_unicode = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    buffer = io.StringIO()
    data = model.model_dump(mode="json", by_alias=True, exclude_unset=exclude_unset)
    yaml.dump(data, buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------------------------
# Validation errors → German issues
# --------------------------------------------------------------------------------------------

_TYPE_TAG_NAMES = {"int", "str", "float", "bool", "none", "list", "dict", "tuple", "set"}


def _is_type_tag(segment: str | int) -> bool:
    return isinstance(segment, str) and (
        segment in _TYPE_TAG_NAMES or any(ch in segment for ch in "[]()'")
    )


def format_loc(loc: Sequence[str | int]) -> str:
    """Format a location tuple as a path: `("agents", 1, "model")` → `agents[1].model`.

    A key from a file is written without control characters and line breaks and cut after 60
    characters (W4b-02a.1, G8); keys of arr's own files are short and stay as they are."""
    out = ""
    for segment in loc:
        if isinstance(segment, int):
            out += f"[{segment}]"
        elif not _is_type_tag(segment):
            text = str(segment)
            if len(text) > 60 or not text.isprintable():
                text = _short(text, 60)
            out += f".{text}" if out else text
    return out


def _clean_loc(loc: Sequence[str | int]) -> Loc:
    return tuple(s for s in loc if not _is_type_tag(s))


def _describe(value: Any) -> str:
    if isinstance(value, str):
        text = value if len(value) <= 40 else value[:37] + "…"
        return f"„{text}“"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "leer"
    if isinstance(value, dict):
        return "ein Abschnitt"
    if isinstance(value, list):
        return "eine Liste"
    return str(value)


def _expected(ctx: Mapping[str, Any]) -> str:
    text = str(ctx.get("expected", ""))
    return text.replace("' or '", ", ").replace("', '", ", ").replace("'", "")


def _message(error: Mapping[str, Any]) -> str:
    kind = error["type"]
    ctx = error.get("ctx") or {}
    value = error.get("input")
    if kind == "unknown_field":
        text = f"Unbekanntes Feld „{_short(ctx['field'], 60)}“."
        if ctx.get("suggestion"):
            text += f" Meintest du „{ctx['suggestion']}“?"
        return text + f" Erlaubt sind: {ctx['allowed']}."
    if kind == "extra_forbidden":
        return "Unbekanntes Feld. Entferne es oder prüfe die Schreibweise."
    if kind == "missing":
        return "Pflichtfeld fehlt. Trage einen Wert ein."
    if kind in ("value_error", "assertion_error"):
        return str(ctx.get("error", error["msg"]))
    if kind in ("int_type", "int_parsing", "int_from_float"):
        return f"Falscher Typ: erwartet eine ganze Zahl, gefunden {_describe(value)}."
    if kind in ("float_type", "float_parsing", "finite_number"):
        return f"Falscher Typ: erwartet eine Zahl, gefunden {_describe(value)}."
    if kind in ("time_delta_type", "time_delta_parsing"):
        what = (
            "Die Dauer fehlt." if value is None else f"Die Dauer {_describe(value)} ist ungültig."
        )
        return (
            f"{what} Gib sie z. B. als „2h“, „90m“, „1h30m“ oder „45s“ an oder als Zahl in "
            "Sekunden."
        )
    if kind in ("bool_type", "bool_parsing"):
        return f"Falscher Typ: erwartet true oder false, gefunden {_describe(value)}."
    if kind in ("string_type", "string_sub_type"):
        return f"Falscher Typ: erwartet einen Text, gefunden {_describe(value)}."
    if kind in ("list_type", "tuple_type", "set_type"):
        return f"Falscher Typ: erwartet eine Liste, gefunden {_describe(value)}."
    if kind in ("dict_type", "model_type", "model_attributes_type"):
        return (
            "Falscher Typ: erwartet einen Abschnitt mit Einträgen „schlüssel: wert“, "
            f"gefunden {_describe(value)}."
        )
    if kind in ("enum", "literal_error"):
        return f"Ungültiger Wert {_describe(value)}. Erlaubt sind: {_expected(ctx)}."
    if kind == "greater_than_equal":
        return f"Der Wert muss mindestens {ctx['ge']} sein, gefunden {_describe(value)}."
    if kind == "greater_than":
        return f"Der Wert muss größer als {ctx['gt']} sein, gefunden {_describe(value)}."
    if kind == "less_than_equal":
        return f"Der Wert darf höchstens {ctx['le']} sein, gefunden {_describe(value)}."
    if kind == "less_than":
        return f"Der Wert muss kleiner als {ctx['lt']} sein, gefunden {_describe(value)}."
    if kind == "too_short":
        if ctx.get("min_length") == 1:
            return "Darf nicht leer sein. Trage mindestens einen Eintrag ein."
        return f"Zu wenige Einträge: mindestens {ctx.get('min_length')}."
    if kind == "too_long":
        return f"Zu viele Einträge: höchstens {ctx.get('max_length')}."
    return f"Ungültiger Wert {_describe(value)} ({error['msg']})."


def validation_issues(
    exc: ValidationError,
    *,
    prefix: Loc = (),
    lines: Mapping[Loc, int] | None = None,
    channels: str = "groupchats",
) -> list[ConfigIssue]:
    """Translate a pydantic `ValidationError` into German `ConfigIssue`s with paths.

    `prefix` is prepended to every location (e.g. `("agents", 1)` when a single entry was
    validated). `lines` maps locations to YAML line numbers. Duplicates per path are dropped.
    `channels` is the spelling of the channel list in the input (`groupchats` in the model).
    """
    issues: list[ConfigIssue] = []
    seen: set[str] = set()
    for error in exc.errors():
        loc = _clean_loc((*prefix, *error["loc"]))
        if channels != "groupchats" and loc[:1] == ("groupchats",):
            loc = (channels, *loc[1:])
        if error["type"] == "unknown_field":
            loc = (*loc, error["ctx"]["field"])
        path = format_loc(loc)
        if path in seen:
            continue
        seen.add(path)
        issues.append(ConfigIssue(path, _message(error), _line_for(loc, lines)))
    return issues


_PATH_PART_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def line_of_path(path: str, lines: Mapping[Loc, int] | None) -> int | None:
    """The line of a path like `agents[1].role` (or of its nearest parent) in a YAML file.

    `lines` comes from `parse_yaml_text`. Used to give every issue of a check a line (W4-02).
    """
    loc: list[str | int] = []
    for match in _PATH_PART_RE.finditer(path):
        loc.append(int(match[2]) if match[2] is not None else match[1])
    return _line_for(tuple(loc), lines)


def _line_for(loc: Loc, lines: Mapping[Loc, int] | None) -> int | None:
    if not lines:
        return None
    for end in range(len(loc), 0, -1):
        line = lines.get(loc[:end])
        if line is not None:
            return line
    return None


# --------------------------------------------------------------------------------------------
# Placeholders and parameters
# --------------------------------------------------------------------------------------------


def _param_name(expression: str) -> str | None:
    match = _PARAM_REF_RE.fullmatch(expression)
    return match.group(1) if match else None


def _as_text(name: str, value: Any, loc: Loc, issues: list[ConfigIssue]) -> str | None:
    """Text of a parameter value inside a longer string; `None` (+ issue) if it has none."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        issues.append(
            ConfigIssue(
                format_loc(loc),
                f"Parameter „{name}“ hat keinen Wert und kann nicht in den Text eingesetzt "
                "werden. Gib beim Start einen Wert an oder lege unter „params“ einen "
                "Standardwert fest.",
            )
        )
        return None
    if isinstance(value, list | dict):
        what = "eine Liste" if isinstance(value, list) else "ein Abschnitt"
        issues.append(
            ConfigIssue(
                format_loc(loc),
                f"Parameter „{name}“ ist {what} und kann nicht in einen Text eingesetzt werden. "
                "Verwende den Platzhalter als ganzen Wert oder einen anderen Parameter.",
            )
        )
        return None
    return str(value)


def _substitute(
    value: Any,
    params: Mapping[str, Any],
    loc: Loc,
    issues: list[ConfigIssue],
    *,
    check_values: bool = True,
) -> Any:
    """Replace `{{ params.<name> }}` in all strings of `value`.

    A string that is exactly one placeholder becomes the parameter's value (keeps its type).
    Inside longer text, numbers and texts are inserted as text and booleans as
    `true`/`false`; `None`, lists and sections are errors. With `check_values=False` only the
    names are checked (used for `group.yaml` before the final values are known).
    """
    if isinstance(value, dict):
        return {
            k: _substitute(v, params, (*loc, k), issues, check_values=check_values)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _substitute(v, params, (*loc, i), issues, check_values=check_values)
            for i, v in enumerate(value)
        ]
    if not isinstance(value, str):
        return value
    matches = list(PLACEHOLDER_RE.finditer(value))
    if not matches:
        return value
    names = []
    for match in matches:
        name = _param_name(match.group(1))
        if name is None:
            issues.append(
                ConfigIssue(
                    format_loc(loc),
                    f"Ungültiger Platzhalter „{match.group(0)}“. Erlaubt ist nur "
                    "„{{ params.<name> }}“.",
                )
            )
            return value
        if name not in params:
            known = _listing(sorted(params))
            issues.append(
                ConfigIssue(
                    format_loc(loc),
                    f"Der Platzhalter „{match.group(0)}“ verweist auf keinen Parameter. "
                    f"Lege „{name}“ unter „params“ an oder korrigiere den Namen "
                    f"(vorhanden: {known}).",
                )
            )
            return value
        names.append(name)
    if len(matches) == 1 and matches[0].group(0) == value.strip():
        return params[names[0]]
    if not check_values:
        return value
    texts = [_as_text(name, params[name], loc, issues) for name in names]
    if any(text is None for text in texts):
        return value
    parts = iter(texts)
    return PLACEHOLDER_RE.sub(lambda _: next(parts) or "", value)


def _check_when(
    spec: AgentSpec | PhaseSpec, params: Mapping[str, Any], loc: Loc
) -> ConfigIssue | None:
    if spec.when is None or spec.when.param in params:
        return None
    known = _listing(sorted(params))
    who = spec.id if isinstance(spec, AgentSpec) else spec.name
    return ConfigIssue(
        format_loc((*loc, "when", "param")),
        f"„when“ bei „{who}“ prüft den unbekannten Parameter „{_short(spec.when.param)}“. "
        f"Lege ihn unter „params“ an oder korrigiere den Namen (vorhanden: {known}).",
    )


def _template_sections(container: GroupSpec | RunInput) -> list[tuple[Loc, Any]]:
    """Sections of a group/run spec in which placeholders may occur, with their locations."""
    sections: list[tuple[Loc, Any]] = []
    if container.defaults is not None:
        sections.append((("defaults",), _dump(container.defaults)))
    sections += [(("agents", i), _dump(a)) for i, a in enumerate(container.agents)]
    key = container.channels_key
    sections += [((key, i), _dump(c)) for i, c in enumerate(container.groupchats)]
    sections += [(("stages", i), _dump(s)) for i, s in enumerate(container.stages)]
    sections += [(("raters", i), r) for i, r in enumerate(container.raters)]
    sections += [(("phases", i), _dump(p)) for i, p in enumerate(container.phases)]
    sections += [(("formats", n), _plain(f)) for n, f in container.formats.items()]
    sections += [(("seed", n), _dump(e)) for n, e in container.seed.items()]
    return sections


def check_group_spec(spec: GroupSpec, *, source: str = GROUP_SOURCE) -> None:
    """Check that placeholders and `when` only refer to declared parameters (docs/03 §3).

    Raises `ConfigError`. Called by `load_group_spec`; `resolve_run` repeats the checks for
    the final parameter values.
    """
    params = {name: decl.default for name, decl in spec.params.items()}
    issues: list[ConfigIssue] = []
    for loc, data in _template_sections(spec):
        _substitute(data, params, loc, issues, check_values=False)
    for i, agent in enumerate(spec.agents):
        issue = _check_when(agent, params, ("agents", i))
        if issue:
            issues.append(issue)
    for i, phase in enumerate(spec.phases):
        issue = _check_when(phase, params, ("phases", i))
        if issue:
            issues.append(issue)
    if issues:
        raise ConfigError(issues, source=source)


def _resolve_params(
    run_input: RunInput, group_spec: GroupSpec | None, overrides: Mapping[str, Any]
) -> dict[str, Any]:
    run_params = run_input.topology.params if run_input.topology else {}
    if group_spec is None:
        return {**run_params, **overrides}
    declared = group_spec.params
    values = {name: decl.default for name, decl in declared.items()}
    known = _listing(sorted(declared))
    layers = (
        (("topology", "params"), run_params, RUN_SOURCE),
        (("params",), overrides, OVERRIDES_SOURCE),
    )
    for loc_prefix, given, source in layers:
        issues: list[ConfigIssue] = []
        for name, value in given.items():
            path = format_loc((*loc_prefix, name))
            if name not in declared:
                issues.append(
                    ConfigIssue(
                        path,
                        f"Unbekannter Parameter „{name}“. Der Skill kennt: {known}.",
                    )
                )
                continue
            problem = declared[name].problem(value)
            if problem:
                issues.append(ConfigIssue(path, f"Parameter „{name}“: {problem}"))
                continue
            values[name] = value
        if issues:
            raise ConfigError(issues, source=source)
    return values


# --------------------------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------------------------


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(exclude_unset=True, by_alias=True)


def _plain(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    """An own format as data: a `FormatSpec`, or still plain data with placeholders."""
    return value if isinstance(value, dict) else _dump(value)


def _short(value: Any, limit: int = 40) -> str:
    """A text from a file for a message: on one line, without control characters and at most
    `limit` characters."""
    text = " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


MAX_LISTED = 10
"""Names a message lists at most; the rest is counted (W4b-02a.1, M2)."""


def _listing(names: Iterable[Any], empty: str = "keine") -> str:
    """Names for a message: at most `MAX_LISTED`, each cut short, then „… und N weitere“."""
    items = [_short(n) for n in names]
    if not items:
        return empty
    shown = ", ".join(items[:MAX_LISTED])
    rest = len(items) - MAX_LISTED
    return f"{shown} … und {rest} weitere" if rest > 0 else shown


def _apply(target: dict[str, Any], layer: Mapping[str, Any]) -> None:
    for key, value in layer.items():
        if key == "tools" and isinstance(value, dict):
            target["tools"] = {**target.get("tools", {}), **value}
        else:
            target[key] = value


@dataclass
class _Merged:
    """Result of merging the layers of one agent replica."""

    data: dict[str, Any]
    provider: Provider
    switched_by_mix: bool  # the mix entry changed the provider of the agent
    skipped: dict[str, tuple[Any, Provider]]  # provider-bound values of other providers


def _merge_agent(
    spec: dict[str, Any],
    mix: dict[str, Any] | None,
    defaults_layers: Sequence[dict[str, Any]],
    settings: WorkfolderSettings,
    project_settings: ProjectSettings | None,
) -> _Merged:
    """Merge the layers of one replica (see the module docstring).

    Every layer has an effective provider: `defaults` their own `provider`, else that of the
    layers below (built-in: claude); the agent its own, else that of the `defaults`; a `mix`
    entry its own, else the agent's. `model`, `auth` and `effort` are only taken from layers
    whose effective provider equals the final provider.
    """
    own = {k: v for k, v in spec.items() if k not in _SPEC_ONLY}
    effective = Provider.CLAUDE
    layers: list[tuple[Mapping[str, Any], Provider]] = []
    for layer in defaults_layers:
        if layer.get("provider") is not None:
            effective = Provider(layer["provider"])
        layers.append((layer, effective))
    if own.get("provider") is not None:
        effective = Provider(own["provider"])
    agent_provider = effective
    layers.append((own, agent_provider))
    final = agent_provider
    if mix:
        final = Provider(mix["provider"]) if mix.get("provider") is not None else agent_provider
        layers.append((mix, final))

    merged: dict[str, Any] = {
        "workspace": {"mode": "copy"},
        "tools": {},
        "skills": [],
        "read_dirs": list(project_settings.read_dirs) if project_settings else [],
        "compact": settings.compact.model_dump(),
    }
    provider_defaults = settings.defaults.get(final)
    if provider_defaults is not None:
        _apply(merged, provider_defaults.model_dump(exclude_none=True))
    skipped: dict[str, tuple[Any, Provider]] = {}
    for layer, provider in layers:
        if provider is not final:
            skipped.update({k: (layer[k], provider) for k in _PROVIDER_BOUND if layer.get(k)})
            layer = {k: v for k, v in layer.items() if k not in _PROVIDER_BOUND}
        _apply(merged, layer)
    merged["provider"] = final
    return _Merged(merged, final, final is not agent_provider, skipped)


@dataclass
class _Disabled:
    """Agents switched off by `when` or `count: 0`; they drop out of members silently."""

    ids: set[str] = field(default_factory=set)
    count_bases: set[str] = field(default_factory=set)  # every `<base>-<n>` is off

    def add(self, spec: AgentSpec) -> None:
        self.ids.add(spec.id)
        if spec.count is not None:
            self.count_bases.add(spec.id)

    def __contains__(self, agent_id: str) -> bool:
        if agent_id in self.ids:
            return True
        base, _, number = agent_id.rpartition("-")
        return base in self.count_bases and number.isdigit()

    def matches(self, pattern: str) -> bool:
        """True if the pattern matches any switched-off ID (replicas up to `MAX_COUNT`)."""
        candidates = list(self.ids)
        for base in self.count_bases:
            candidates += [f"{base}-{n}" for n in range(1, MAX_COUNT + 1)]
        return any(fnmatchcase(candidate, pattern) for candidate in candidates)


def _count_param(spec: AgentSpec) -> str | None:
    """Name of the parameter in `count: "{{ params.<name> }}"`, else None."""
    if not isinstance(spec.count, str):
        return None
    match = PLACEHOLDER_RE.fullmatch(spec.count.strip())
    return _param_name(match.group(1)) if match else None


def _count_issue(issue: ConfigIssue, name: str, value: Any) -> ConfigIssue:
    """A `count` error caused by a parameter names the parameter (W1-01.1 review N2)."""
    if not issue.path.endswith(".count"):
        return issue
    return ConfigIssue(
        issue.path,
        f"Parameter „{name}“ = {_describe(value)} wird als „count“ benutzt und muss eine ganze "
        f"Zahl von 0 bis {MAX_COUNT} sein. Ändere den Wert von „{name}“.",
        issue.line,
    )


def _resolve_agents(
    specs: Sequence[AgentSpec],
    source: str,
    params: Mapping[str, Any],
    defaults_layers: Sequence[dict[str, Any]],
    settings: WorkfolderSettings,
    project_settings: ProjectSettings | None,
) -> tuple[list[AgentConfig], _Disabled]:
    """Expand and merge agent specs. Returns active agents and the switched-off ones."""
    issues: list[ConfigIssue] = []
    agents: list[AgentConfig] = []
    disabled = _Disabled()
    positions: dict[str, Loc] = {}
    for index, original in enumerate(specs):
        loc: Loc = ("agents", index)
        before = len(issues)
        data = _substitute(_dump(original), params, loc, issues)
        if len(issues) > before:
            continue
        count_param = _count_param(original)
        if count_param and isinstance(data.get("count"), float) and data["count"].is_integer():
            data["count"] = int(data["count"])  # `judges: 2.0` is 2 replicas
        try:
            spec = AgentSpec.model_validate(data)
        except ValidationError as exc:
            found = validation_issues(exc, prefix=loc)
            if count_param:
                found = [_count_issue(i, count_param, data.get("count")) for i in found]
            issues += found
            continue
        when_issue = _check_when(spec, params, loc)
        if when_issue:
            issues.append(when_issue)
            continue
        if isinstance(spec.count, str):  # unresolved placeholder; reported above
            continue
        count = spec.count
        replicas: list[tuple[str, int | None, int | None]]  # id, replica, mix index
        if count is None:
            replicas = [(spec.id, None, None)]
        else:
            mix_len = len(spec.mix or [])
            replicas = [
                (f"{spec.id}-{k}", k, (k - 1) % mix_len if mix_len else None)
                for k in range(1, count + 1)
            ]
        included = spec.when.matches(params[spec.when.param]) if spec.when else True
        if not included or not replicas:
            disabled.add(spec)
            continue
        spec_data = _dump(spec)
        for agent_id, replica, mix_index in replicas:
            mix_entry = spec.mix[mix_index] if spec.mix and mix_index is not None else None
            merged = _merge_agent(
                spec_data,
                _dump(mix_entry) if mix_entry else None,
                defaults_layers,
                settings,
                project_settings,
            )
            mix_loc = (*loc, "mix", mix_index) if merged.switched_by_mix else loc
            missing = _missing_required(agent_id, merged, mix_loc)
            if missing:
                issues += missing
                continue
            data = {**merged.data, "id": agent_id, "base_id": spec.id, "replica": replica}
            try:
                agent = AgentConfig.model_validate(data)
            except ValidationError as exc:
                issues += validation_issues(exc, prefix=loc)
                continue
            if agent.id in positions:
                issues.append(
                    ConfigIssue(
                        format_loc((*loc, "id")),
                        f"Die Agent-ID „{agent.id}“ gibt es mehrfach (auch durch „count“ "
                        "entstandene IDs zählen, erste Stelle: "
                        f"{format_loc(positions[agent.id])}). Benenne einen der Agenten um.",
                    )
                )
                continue
            positions[agent.id] = loc
            agents.append(agent)
    if issues:
        raise ConfigError(issues, source=source)
    if not agents:
        raise ConfigError.single(
            "agents",
            "Nach der Auflösung bleibt kein Agent übrig. Prüfe „count“ und „when“.",
            source=source,
        )
    return agents, disabled


def _missing_required(agent_id: str, merged: _Merged, loc: Loc) -> list[ConfigIssue]:
    """Report a missing `model` / `auth` for the final provider (docs/08 §8.4 pattern).

    `loc` points to the mix entry when that entry switched the provider.
    """
    provider = merged.provider
    where = "im mix-Eintrag" if merged.switched_by_mix else "beim Agenten bzw. unter „defaults“"
    issues = []
    for key, what in (("model", "ein Modell"), ("auth", "ein Auth-Profil")):
        if merged.data.get(key):
            continue
        text = (
            f"Für {provider} fehlt {what} („{agent_id}“). Ergänze „{key}“ {where} oder lege "
            f"in arr.yaml „defaults.{provider}“ fest."
        )
        if key in merged.skipped:
            value, other = merged.skipped[key]
            text += f" „{value}“ gilt für {other} und wird deshalb nicht übernommen."
        issues.append(ConfigIssue(format_loc((*loc, key)), text))
    return issues


# --------------------------------------------------------------------------------------------
# Members, channels, stages
# --------------------------------------------------------------------------------------------


def _is_pattern(entry: str) -> bool:
    return any(ch in entry for ch in "*?[")


def _match_agents(
    entries: Sequence[str],
    loc: Loc,
    active: Sequence[str],
    disabled: _Disabled,
    issues: list[ConfigIssue],
) -> list[str]:
    """Resolve IDs and `fnmatch` patterns to active agent IDs in a stable order.

    Entries that only match agents switched off by `when` or `count: 0` contribute nothing.
    Entries that match no agent at all are errors.
    """
    result: list[str] = []
    for index, entry in enumerate(entries):
        for hit in _match_entry(entry, (*loc, index), active, disabled, issues):
            if hit not in result:
                result.append(hit)
    return result


def _match_entry(
    entry: str,
    loc: Loc,
    active: Sequence[str],
    disabled: _Disabled,
    issues: list[ConfigIssue],
) -> list[str]:
    """The active agents one ID or pattern names (see `_match_agents`), issue at `loc`."""
    path = format_loc(loc)
    available = _listing(active)
    if _is_pattern(entry):
        hits = [a for a in active if fnmatchcase(a, entry)]
        if not hits and not disabled.matches(entry):
            issues.append(
                ConfigIssue(
                    path,
                    f"Das Muster „{_short(entry)}“ passt auf keinen Agenten. "
                    f"Vorhanden: {available}.",
                )
            )
        return hits
    if entry in active:
        return [entry]
    if entry in disabled:
        return []
    hint = ""
    if any(a.startswith(f"{entry}-") for a in active):
        hint = f" Meintest du „{_short(entry)}-*“ (Replikate aus „count“)?"
    else:
        close = difflib.get_close_matches(entry, active, n=1)
        if close:
            hint = f" Meintest du „{_short(close[0])}“?"
    issues.append(
        ConfigIssue(
            path,
            f"Unbekannter Agent „{_short(entry)}“.{hint} Vorhanden: {available}. "
            "Prüfe die Schreibweise oder lege den Agenten unter „agents“ an.",
        )
    )
    return []


def _resolve_channels(
    chats: Sequence[GroupChatConfig],
    source: str,
    params: Mapping[str, Any],
    active: Sequence[str],
    disabled: _Disabled,
    *,
    key: str = "groupchats",
    phases: bool = False,
) -> list[GroupChatConfig]:
    """Channels with concrete members; `key` is the spelling of the list in the input
    (`channels` or `groupchats`). With `phases`, `open_from` and `mode: moderated` are errors
    that name their replacement (S2 of W4b-02a)."""
    issues: list[ConfigIssue] = []
    resolved: list[GroupChatConfig] = []
    names: set[str] = set()
    for index, original in enumerate(chats):
        loc: Loc = (key, index)
        before = len(issues)
        data = _substitute(_dump(original), params, loc, issues)
        if len(issues) > before:
            continue
        try:
            chat = GroupChatConfig.model_validate(data)
        except ValidationError as exc:
            issues += validation_issues(exc, prefix=loc)
            continue
        if phases and chat.open_from is not None:
            issues.append(
                ConfigIssue(
                    format_loc((*loc, "open_from")),
                    f"„open_from“ gibt es bei „topology: phases“ nicht (Kanal „{chat.name}“). "
                    "Nenne den Kanal stattdessen unter „channels“ der Phasen, in denen er offen "
                    "ist.",
                )
            )
        if phases and chat.mode is ChannelMode.MODERATED:
            issues.append(
                ConfigIssue(
                    format_loc((*loc, "mode")),
                    f"Ein Kanal mit „mode: moderated“ gibt es bei „topology: phases“ nicht "
                    f"(Kanal „{chat.name}“). Nimm eine Phase mit „topology: moderated“; der "
                    "Kanal bekommt „mode: rounds“ oder „event“.",
                )
            )
        if len(issues) > before:
            continue
        if chat.name in names:
            issues.append(
                ConfigIssue(
                    format_loc((*loc, "name")),
                    f"Den Kanal „{chat.name}“ gibt es mehrfach. Benenne einen um.",
                )
            )
            continue
        names.add(chat.name)
        members = _match_agents(chat.members, (*loc, "members"), active, disabled, issues)
        if len(issues) > before:
            continue
        if not members:
            continue  # all members switched off by `when` / `count: 0`
        if chat.moderator is not None and chat.moderator not in members:
            issues.append(
                ConfigIssue(
                    format_loc((*loc, "moderator")),
                    f"Der Moderator „{chat.moderator}“ ist kein Mitglied von „{chat.name}“. "
                    "Nimm ihn in „members“ auf oder wähle ein Mitglied als Moderator.",
                )
            )
            continue
        resolved.append(chat.model_copy(update={"members": members}))
    if issues:
        raise ConfigError(issues, source=source)
    return resolved


def _resolve_stages(
    stages: Sequence[StageSpec],
    source: str,
    params: Mapping[str, Any],
    active: Sequence[str],
    disabled: _Disabled,
) -> list[StageSpec]:
    issues: list[ConfigIssue] = []
    resolved: list[StageSpec] = []
    for index, original in enumerate(stages):
        loc: Loc = ("stages", index)
        before = len(issues)
        data = _substitute(_dump(original), params, loc, issues)
        if len(issues) > before:
            continue
        try:
            stage = StageSpec.model_validate(data)
        except ValidationError as exc:
            issues += validation_issues(exc, prefix=loc)
            continue
        if stage.agent in active:
            resolved.append(stage)
            continue
        if stage.agent in disabled:
            issues.append(
                ConfigIssue(
                    format_loc((*loc, "agent")),
                    f"Die Stufe „{stage.name}“ nennt „{stage.agent}“, der durch „when“ bzw. "
                    "„count“ ausgeschaltet ist. Passe die Bedingung an oder entferne die Stufe.",
                )
            )
        else:
            issues.append(
                ConfigIssue(
                    format_loc((*loc, "agent")),
                    f"Die Stufe „{stage.name}“ nennt den unbekannten Agenten „{stage.agent}“. "
                    f"Vorhanden: {', '.join(active)}.",
                )
            )
    if issues:
        raise ConfigError(issues, source=source)
    return resolved


# --------------------------------------------------------------------------------------------
# Phases, own formats and seed (`topology: phases`, docs/10 §4.10, W4b-02a §6.3)
# --------------------------------------------------------------------------------------------

_ROUTE_WORDS = tuple(word.value for word in RouteKeyword)


def _raw_at(raw: Any, path: str, skip: int) -> Any:
    """The value in `raw` at `path` (a message path) without its first `skip` parts."""
    parts: list[str | int] = []
    for match in _PATH_PART_RE.finditer(path):
        parts.append(int(match[2]) if match[2] is not None else match[1])
    value = raw
    for part in parts[skip:]:
        in_list = isinstance(value, list) and isinstance(part, int) and 0 <= part < len(value)
        if not in_list and not (isinstance(value, dict) and part in value):
            return None
        value = value[part]
    return value


def _validate_inserted[M: BaseModel](
    model: type[M], data: Any, raw: Any, loc: Loc, issues: list[ConfigIssue]
) -> M | None:
    """Validate a section after its placeholders were inserted. A bad value that came from a
    placeholder names it, so the user knows which parameter to change (W4b-02a §6.2)."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        for issue in validation_issues(exc, prefix=loc):
            issues.append(_with_hint(issue, raw, len(loc)))
        return None


def _with_hint(issue: ConfigIssue, raw: Any, skip: int) -> ConfigIssue:
    """The issue, and if the value at its path came from a placeholder, which one (W4b-02a
    §6.2): the user knows which parameter to change."""
    original = _raw_at(raw, issue.path, skip)
    if isinstance(original, dict) and set(original) == {"goto"}:
        original = original["goto"]  # `{ goto: {{ params.x }} }`
    if not is_placeholder(original) or "Der Wert kommt aus" in issue.message:
        return issue
    return ConfigIssue(
        issue.path,
        f"{issue.message} Der Wert kommt aus „{_short(original)}“; ändere den Parameter oder "
        "den Eintrag.",
        issue.line,
    )


@dataclass
class _Names:
    """What the input names (W4b-02a §6.3): resolve reports a reference to a name that exists
    nowhere in the input and rewrites or drops one to a phase, channel or submission that
    resolution dropped, so the resolved run only refers to what it has."""

    phases: list[str]
    kept: list[str]
    channels: set[str]
    kept_channels: set[str]
    names: set[str]  # submission names of every phase in the input, and every seed key
    kept_names: set[str]  # submission names of kept phases, and the seeds that stay

    def after(self, name: str) -> str | None:
        """The first kept phase after `name` in the input (Entscheidung 37), else None."""
        index = self.phases.index(name)
        return next((p for p in self.phases[index + 1 :] if p in self.kept), None)


def _suggestion(value: str, known: Sequence[str]) -> str:
    close = difflib.get_close_matches(value, list(known), n=1, cutoff=0.5)
    return f" Meintest du „{_short(close[0])}“?" if close else ""


def _route(value: Any, loc: Loc, names: _Names, issues: list[ConfigIssue]) -> Route | None:
    """A route of the input as a resolved route (G1): a keyword, or `{ goto: <phase> }` to a
    kept phase; a goto to a dropped phase leads to the next kept one, or ends the run."""
    path = format_loc(loc)
    if isinstance(value, Goto):
        target = value.goto
        if target not in names.phases:
            issues.append(
                ConfigIssue(
                    path,
                    f"Die Phase „{_short(target)}“ gibt es nicht."
                    f"{_suggestion(target, names.phases)} Phasen: {_listing(names.phases)}.",
                )
            )
            return None
        if target in names.kept:
            return Goto(goto=target)
        after = names.after(target)
        return Goto(goto=after) if after else RouteKeyword.STOP
    word = str(value)
    if word in _ROUTE_WORDS:
        return RouteKeyword(word)
    if word in names.phases:
        text = (
            f"„{word}“ ist der Name einer Phase, keine Route. Schreibe „{{ goto: {word} }}“, "
            "damit eine Phase nicht mit next, retry, stop oder escalate verwechselt wird."
        )
    else:
        hint = _suggestion(word, _ROUTE_WORDS)
        if not hint and names.phases:
            close = difflib.get_close_matches(word, names.phases, n=1, cutoff=0.5)
            hint = f" Meintest du „{{ goto: {_short(close[0])} }}“?" if close else ""
        text = f"Unbekannte Route „{_short(word)}“.{hint} {ROUTE_HELP}"
    issues.append(ConfigIssue(path, text))
    return None


def _task(spec: ActionSpec) -> dict[str, Any]:
    return {
        "task": spec.task,
        "submission": _dump(spec.submission),
        "turns": spec.turns if spec.turns is not None else DEFAULT_TURNS,
    }


def _modes(
    setting: Any,
    loc: Loc,
    participants: Sequence[str],
    active: Sequence[str],
    disabled: _Disabled,
    issues: list[ConfigIssue],
) -> dict[str, Any]:
    """`context` or `folder` as agent → value: one value for every participant, or the
    entries of a mapping (IDs or patterns; agents that are no participants stay for I3)."""
    if setting is None:
        return {}
    if not isinstance(setting, dict):
        return {agent: setting for agent in participants}
    out: dict[str, Any] = {}
    for key, mode in setting.items():
        for agent in _match_entry(key, (*loc, key), active, disabled, issues):
            out[agent] = mode
    return out


def _phase_inputs(
    spec: PhaseSpec, loc: Loc, names: _Names, issues: list[ConfigIssue]
) -> list[dict[str, Any]] | None:
    """Inputs with `pick` and `content` written out (`content: full` for a channel, else
    `summary`; `pick: latest` for `from` and `name`). An input from a dropped phase, a dropped
    channel or a name nothing produces any more is left out; if no source is left, the phase
    gets the standard inputs (None, Entscheidung 45)."""
    if spec.inputs is None:
        return None
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(spec.inputs):
        here = (*loc, "inputs", index)
        source = entry.from_
        if source is not None:
            if source not in names.phases:
                issues.append(
                    ConfigIssue(
                        format_loc((*here, "from")),
                        f"Die Phase „{_short(source)}“ gibt es nicht."
                        f"{_suggestion(source, names.phases)} Phasen: "
                        f"{_listing(names.phases)}.",
                    )
                )
                continue
            if source not in names.kept:
                continue
        if entry.name is not None:
            if entry.name not in names.names:
                known = _listing(sorted(names.names))
                issues.append(
                    ConfigIssue(
                        format_loc((*here, "name")),
                        f"Eine Abgabe oder einen Gegenstand „{_short(entry.name)}“ gibt es "
                        f"nicht.{_suggestion(entry.name, sorted(names.names))} Namen: {known}.",
                    )
                )
                continue
            if entry.name not in names.kept_names:
                continue
        if entry.channel is not None:
            if entry.channel not in names.channels:
                known = _listing(sorted(names.channels))
                issues.append(
                    ConfigIssue(
                        format_loc((*here, "channel")),
                        f"Den Kanal „{_short(entry.channel)}“ gibt es nicht."
                        f"{_suggestion(entry.channel, sorted(names.channels))} Kanäle: {known}.",
                    )
                )
                continue
            if entry.channel not in names.kept_channels:
                continue
        data = _dump(entry)
        named = source is not None or entry.name is not None
        data["pick"] = entry.pick or (Pick.LATEST if named else None)
        talk = InputContent.FULL if entry.channel is not None else InputContent.SUMMARY
        data["content"] = entry.content or talk
        out.append(data)
    if spec.inputs and not out:
        return None  # every source dropped: the standard inputs (Entscheidung 45)
    return out


def _phase_channels(
    spec: PhaseSpec, loc: Loc, names: _Names, issues: list[ConfigIssue]
) -> list[str]:
    out = []
    for index, name in enumerate(spec.channels or []):
        if name not in names.channels:
            known = _listing(sorted(names.channels))
            issues.append(
                ConfigIssue(
                    format_loc((*loc, "channels", index)),
                    f"Den Kanal „{_short(name)}“ gibt es nicht."
                    f"{_suggestion(name, sorted(names.channels))} Kanäle: {known}. Lege ihn "
                    "unter „channels“ der Group an.",
                )
            )
        elif name in names.kept_channels and name not in out:
            out.append(name)
    return out


def _resolve_gate(
    spec: GateSpec | None,
    loc: Loc,
    names: _Names,
    active: Sequence[str],
    disabled: _Disabled,
    rubric: Rubric | None,
    issues: list[ConfigIssue],
) -> dict[str, Any]:
    if spec is None:
        return {"type": GateType.DONE, "routes": {"done": RouteKeyword.NEXT}, "raters": []}
    gate: dict[str, Any] = {
        "type": spec.type,
        "field": spec.field,
        "rule": spec.rule,
        "pass_score": spec.pass_score,
        "criteria": spec.criteria,
    }
    routes: dict[str, Any] = {}
    for outcome, value in (spec.routes or {}).items():
        route = _route(value, (*loc, "routes", outcome), names, issues)
        if route is not None:
            routes[outcome] = route
    if spec.routes is None and spec.type is GateType.DONE:
        routes = {"done": RouteKeyword.NEXT}
    gate["routes"] = routes
    if spec.on_tie is not None:
        gate["on_tie"] = _route(spec.on_tie, (*loc, "on_tie"), names, issues)
    gate["raters"] = _match_agents(spec.raters or [], (*loc, "raters"), active, disabled, issues)
    if spec.action is not None:
        gate["action"] = _task(spec.action)
    if spec.rule is GateRule.MEAN and spec.pass_score is None and rubric is not None:
        gate["pass_score"] = rubric.pass_score
    return gate


@dataclass
class _Pass1:
    """A phase after inserting the parameters and resolving the agents of its actions."""

    index: int
    spec: PhaseSpec
    actions: list[dict[str, Any]]
    raw: dict[str, Any]


def _insert_phase(
    index: int,
    original: PhaseSpec,
    params: Mapping[str, Any],
    active: Sequence[str],
    disabled: _Disabled,
    issues: list[ConfigIssue],
) -> tuple[_Pass1 | None, bool]:
    """Insert the parameters into a phase and resolve its actions. Returns the phase (None
    after an error) and whether it stays (`when` holds, an agent with an action is left).

    A phase that `when` drops does not exist in this run: the values inserted into it are not
    checked (`rounds: {{ params.discussion_rounds }}` may be 0 where the phase only exists for
    1), but its agents are, because a name that exists nowhere is a typo for every parameter.
    """
    loc: Loc = ("phases", index)
    raw = _dump(original)
    before = len(issues)
    data = _substitute(raw, params, loc, issues)
    if len(issues) > before:
        return None, True
    when_issue = _check_when(original, params, loc)
    if when_issue:
        issues.append(when_issue)
        return None, True
    when = original.when
    if isinstance(data, dict) and isinstance(data.get("when"), dict):
        try:
            when = WhenCondition.model_validate(data["when"])
        except ValidationError as exc:
            issues += validation_issues(exc, prefix=(*loc, "when"))
            return None, True
    if when is not None and not when.matches(params[when.param]):
        for key in original.actions:
            _match_entry(key, (*loc, "actions", key), active, disabled, issues)
        return _Pass1(index, original, [], raw), False
    spec = _validate_inserted(PhaseSpec, data, raw, loc, issues)
    if spec is None:
        return None, True
    actions = []
    for key, action in spec.actions.items():
        for agent in _match_entry(key, (*loc, "actions", key), active, disabled, issues):
            actions.append({**_task(action), "agent": agent, "key": key})
    return _Pass1(index, spec, actions, raw), bool(actions)


def _submission_names(spec: PhaseSpec) -> set[str]:
    found = {a.submission.name for a in spec.actions.values() if a.submission.name}
    if spec.gate is not None and spec.gate.action is not None and spec.gate.action.submission.name:
        found.add(spec.gate.action.submission.name)
    return {str(name) for name in found}


def _finish_phase(
    first: _Pass1,
    names: _Names,
    active: Sequence[str],
    disabled: _Disabled,
    rubric: Rubric | None,
    shared: set[str],
    issues: list[ConfigIssue],
) -> ResolvedPhase | None:
    spec, loc = first.spec, ("phases", first.index)
    before = len(issues)
    standby = _match_agents(spec.standby or [], (*loc, "standby"), active, disabled, issues)
    gate = _resolve_gate(spec.gate, (*loc, "gate"), names, active, disabled, rubric, issues)
    agents = list(dict.fromkeys(a["agent"] for a in first.actions))
    participants = list(dict.fromkeys([*agents, *standby, *gate["raters"]]))
    context = {agent: ContextMode.KEEP for agent in participants}
    context.update(_modes(spec.context, (*loc, "context"), participants, active, disabled, issues))
    folder = {  # the folder follows the session; a shared folder never becomes fresh (I2)
        agent: FolderMode.FRESH
        if mode is ContextMode.RESET and agent not in shared
        else FolderMode.KEEP
        for agent, mode in context.items()
    }
    folder.update(_modes(spec.folder, (*loc, "folder"), participants, active, disabled, issues))
    rounds = spec.rounds
    if rounds is None and spec.topology is PhaseTopology.CHAT:
        rounds = DEFAULT_CHAT_ROUNDS
    if spec.over is not None and spec.over not in names.names:
        known = _listing(sorted(names.names))
        issues.append(
            ConfigIssue(
                format_loc((*loc, "over")),
                f"Eine Liste „{_short(spec.over)}“ gibt es nicht."
                f"{_suggestion(spec.over, sorted(names.names))} „over“ nennt den Namen einer "
                f"Abgabe im Format „list“ oder eines „seed“. Namen: {known}.",
            )
        )
    data = {
        "name": spec.name,
        "index": first.index,
        "topology": spec.topology,
        "actions": first.actions,
        "standby": standby,
        "channels": _phase_channels(spec, loc, names, issues),
        "rounds": rounds,
        "isolated": bool(spec.isolated),
        "over": spec.over,
        "repeat": spec.repeat or 1,
        "context": context,
        "folder": folder,
        "inputs": _phase_inputs(spec, loc, names, issues),
        "max_visits": spec.max_visits,
        "max_assignments": spec.max_assignments,
        "decider": spec.decider,
        "gate": gate,
    }
    if spec.on_exhausted is not None:
        data["on_exhausted"] = _route(spec.on_exhausted, (*loc, "on_exhausted"), names, issues)
    if len(issues) > before:
        issues[before:] = [_with_hint(i, first.raw, len(loc)) for i in issues[before:]]
        return None
    try:
        return ResolvedPhase.model_validate(data)
    except ValidationError as exc:
        issues += validation_issues(exc, prefix=loc)
        return None


def _resolve_formats(
    formats: Mapping[str, Any], params: Mapping[str, Any], issues: list[ConfigIssue]
) -> dict[str, FormatSpec]:
    out: dict[str, FormatSpec] = {}
    for name, value in formats.items():
        loc: Loc = ("formats", name)
        raw = _plain(value)
        before = len(issues)
        data = _substitute(raw, params, loc, issues)
        if len(issues) > before:
            continue
        spec = _validate_inserted(FormatSpec, data, raw, loc, issues)
        if spec is not None:
            out[name] = spec
    return out


def _resolve_seed(
    seed: Mapping[str, SeedSpec], params: Mapping[str, Any], issues: list[ConfigIssue]
) -> dict[str, ResolvedSeed]:
    """Seeds with the path or the list of their parameter; an empty value creates nothing."""
    out: dict[str, ResolvedSeed] = {}
    known = _listing(sorted(params))
    for name, entry in seed.items():
        loc: Loc = ("seed", name)
        if entry.param is not None:
            if entry.param not in params:
                issues.append(
                    ConfigIssue(
                        format_loc((*loc, "param")),
                        f"„seed.{name}“ nennt den unbekannten Parameter „{_short(entry.param)}“."
                        f" Lege ihn unter „params“ an oder korrigiere den Namen "
                        f"(vorhanden: {known}).",
                    )
                )
                continue
            value = params[entry.param]
            if value is None or value == [] or value == "":
                continue
            items = value if isinstance(value, list) else None
            out[name] = ResolvedSeed(param=entry.param, items=items)
            continue
        before = len(issues)
        file = _substitute(entry.file, params, (*loc, "file"), issues)
        if len(issues) > before or file is None or (isinstance(file, str) and not file.strip()):
            continue
        if not isinstance(file, str):
            issue = ConfigIssue(
                format_loc((*loc, "file")),
                f"„file“ ist ein Pfad im Projekt, gefunden {_describe(file)}.",
            )
            issues.append(_with_hint(issue, {"file": entry.file}, 2))
            continue
        out[name] = ResolvedSeed(file=file)
    return out


def _resolve_phases(
    specs: Sequence[PhaseSpec],
    issues: list[ConfigIssue],
    params: Mapping[str, Any],
    active: Sequence[str],
    disabled: _Disabled,
    chats: Sequence[GroupChatConfig],
    channels: Sequence[GroupChatConfig],
    seed_keys: Sequence[str],
    seeds: Mapping[str, ResolvedSeed],
    rubric: Rubric | None,
    shared: set[str],
) -> list[ResolvedPhase]:
    """Resolve the phases (W4b-02a §6.3): insert parameters, apply `when`, resolve agents,
    write out defaults, drop phases and rewrite routes and inputs to them. Problems go to
    `issues`, those of every phase at once; after a problem the result is incomplete."""
    seen: dict[str, int] = {}
    for index, spec in enumerate(specs):
        if spec.name in seen:
            issues.append(
                ConfigIssue(
                    format_loc(("phases", index, "name")),
                    f"Die Phase „{spec.name}“ gibt es mehrfach (erste Stelle: "
                    f"phases[{seen[spec.name]}]). Phasennamen sind eindeutig, weil Routen und "
                    "Eingaben sie nennen. Benenne eine um.",
                )
            )
        seen.setdefault(spec.name, index)
    firsts: list[_Pass1] = []
    kept: list[str] = []
    names: set[str] = set(seed_keys)
    kept_names: set[str] = set(seeds)
    for index, original in enumerate(specs):
        first, keep = _insert_phase(index, original, params, active, disabled, issues)
        if first is None:
            continue
        produced = _submission_names(first.spec)
        names |= produced
        if keep:
            firsts.append(first)
            kept.append(first.spec.name)
            kept_names |= produced
    if issues:
        return []
    table = _Names(
        phases=[s.name for s in specs],
        kept=kept,
        channels={c.name for c in chats},
        kept_channels={c.name for c in channels},
        names=names,
        kept_names=kept_names,
    )
    resolved = []
    for first in firsts:
        phase = _finish_phase(first, table, active, disabled, rubric, shared, issues)
        if phase is not None:
            resolved.append(phase)
    return resolved


def _only_for_phases(kind: TopologyKind, sections: Sequence[tuple[str, Any, str]]) -> None:
    """S1: `phases`, `formats` and `seed` belong to `topology: phases` only."""
    if kind is TopologyKind.PHASES:
        return
    for key, value, source in sections:
        if value:
            raise ConfigError.single(
                key,
                f"„{key}“ gibt es nur bei „topology: phases“, diese Group hat „{kind.value}“. "
                f"Entferne „{key}“ oder stelle die Group auf „topology: phases“ um.",
                source=source,
            )


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------


def _merged_model[M: BaseModel](
    model: type[M], layers: Sequence[BaseModel | None], path: str, source: str | None = None
) -> M:
    """Merge partial layers field by field (fields given in a layer win) and validate once."""
    data: dict[str, Any] = {}
    for layer in layers:
        if layer is not None:
            data.update(layer.model_dump(exclude_unset=True))
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(validation_issues(exc, prefix=(path,)), source=source) from None


def resolve_run(
    run_input: RunInput,
    group_spec: GroupSpec | None,
    workfolder_settings: WorkfolderSettings,
    param_overrides: Mapping[str, Any] | None = None,
    *,
    project_settings: ProjectSettings | None = None,
) -> ResolvedRun:
    """Resolve a run's input (and its group skill, if any) into a `ResolvedRun`.

    `param_overrides` are the parameters given at start (`start_run(params=…)`, GUI form) and
    win over `run.yaml topology.params`. With a `group_spec`, every parameter must be declared
    there and is checked against `default`, `min`, `max` and `choices`; without one,
    parameters are free. `project_settings` contributes default `read_dirs`.

    Role files, skills and auth profiles are only named here; their existence is checked by
    the RunPreparer. Raises `ConfigError` with paths relative to `run.yaml` or `group.yaml`.
    See docs/02-workfolder-und-runs.md §3.3.
    """
    overrides = dict(param_overrides or {})
    params = _resolve_params(run_input, group_spec, overrides)

    run_kind = run_input.topology.kind if run_input.topology else None
    group_kind = group_spec.topology if group_spec else None
    if run_kind and group_kind and run_kind is not group_kind:
        raise ConfigError.single(
            "topology.kind",
            f"Die Topologie „{run_kind}“ passt nicht zum Skill (dort „{group_kind}“). "
            "Entferne „topology.kind“ oder wähle einen passenden Skill.",
            source=RUN_SOURCE,
        )
    kind = run_kind or group_kind
    if kind is None:
        raise ConfigError.single(
            "topology",
            "Es fehlt „topology“. Gib z. B. „topology: {kind: solo}“ an oder starte den Run "
            "aus einem Group-Skill.",
            source=RUN_SOURCE,
        )

    if run_input.agents:
        agent_specs, agents_source = run_input.agents, RUN_SOURCE
    elif group_spec is not None:
        agent_specs, agents_source = group_spec.agents, GROUP_SOURCE
    else:
        raise ConfigError.single(
            "agents",
            "Es sind keine Agenten angegeben. Lege unter „agents“ mindestens einen an.",
            source=RUN_SOURCE,
        )

    defaults_layers: list[dict[str, Any]] = []
    for source, defaults in (
        (GROUP_SOURCE, group_spec.defaults if group_spec else None),
        (RUN_SOURCE, run_input.defaults),
    ):
        if defaults is None:
            continue
        issues: list[ConfigIssue] = []
        data = _substitute(_dump(defaults), params, ("defaults",), issues)
        if issues:
            raise ConfigError(issues, source=source)
        try:
            defaults_layers.append(_dump(AgentDefaults.model_validate(data)))
        except ValidationError as exc:
            raise ConfigError(validation_issues(exc, prefix=("defaults",)), source=source) from None

    agents, disabled = _resolve_agents(
        agent_specs, agents_source, params, defaults_layers, workfolder_settings, project_settings
    )
    active = [agent.id for agent in agents]

    def pick[T](own: T, group_attr: str) -> tuple[T, str]:
        if own or group_spec is None:
            return own, RUN_SOURCE
        return getattr(group_spec, group_attr), GROUP_SOURCE

    phases_kind = kind is TopologyKind.PHASES
    chats, chats_source = pick(run_input.groupchats, "groupchats")
    owner = run_input if chats_source == RUN_SOURCE or group_spec is None else group_spec
    channels = _resolve_channels(
        chats,
        chats_source,
        params,
        active,
        disabled,
        key=owner.channels_key,
        phases=phases_kind,
    )

    stage_specs, stages_source = pick(run_input.stages, "stages")
    stages = _resolve_stages(stage_specs, stages_source, params, active, disabled)

    rater_entries, raters_source = pick(run_input.raters, "raters")
    issues = []
    rater_entries = [
        _substitute(entry, params, ("raters", i), issues) for i, entry in enumerate(rater_entries)
    ]
    raters = _match_agents([str(e) for e in rater_entries], ("raters",), active, disabled, issues)
    if issues:
        raise ConfigError(issues, source=raters_source)

    limits = _merged_model(
        Limits,
        [
            BUILTIN_LIMITS,
            workfolder_settings.limits,
            group_spec.limits if group_spec else None,
            run_input.limits,
        ],
        "limits",
    )
    approvals = _merged_model(
        ApprovalSettings,
        [workfolder_settings.approvals, run_input.approvals],
        "approvals",
        source=RUN_SOURCE,
    )

    phase_specs, phases_source = pick(run_input.phases, "phases")
    format_specs, formats_source = pick(run_input.formats, "formats")
    seed_specs, seed_source = pick(run_input.seed, "seed")
    _only_for_phases(
        kind,
        [
            ("phases", phase_specs, phases_source),
            ("formats", format_specs, formats_source),
            ("seed", seed_specs, seed_source),
        ],
    )
    rubric = run_input.rubric or (group_spec.rubric if group_spec else None)
    # formats, seed and phases report together (W4b-02a.1, G4); the file of the first problem
    # names the source (they come from the same file unless a run replaces only some of them)
    plan_issues: list[tuple[list[ConfigIssue], str]] = [([], formats_source), ([], seed_source)]
    formats = _resolve_formats(format_specs, params, plan_issues[0][0])
    seeds = _resolve_seed(seed_specs, params, plan_issues[1][0])
    plan_issues.append(([], phases_source))
    phases = _resolve_phases(
        phase_specs,
        plan_issues[2][0],
        params,
        active,
        disabled,
        chats,
        channels,
        list(seed_specs),
        seeds,
        rubric,
        {a.id for a in agents if a.workspace.mode is WorkspaceMode.SHARED},
    )
    found = [issue for found, _source in plan_issues for issue in found]
    if found:
        source = next(src for found, src in plan_issues if found)
        raise ConfigError(found, source=source)

    agents = [
        agent.model_copy(update={"channels": [c.name for c in channels if agent.id in c.members]})
        for agent in agents
    ]
    return ResolvedRun(
        name=run_input.name,
        project=run_input.project,
        skill=run_input.skill,
        task=run_input.task,
        task_file=run_input.task_file,
        topology=TopologyConfig(kind=kind, params=params),
        rubric=rubric,
        stages=stages,
        raters=raters,
        limits=limits,
        approvals=approvals,
        agents=agents,
        groupchats=channels,
        phases=phases,
        formats=formats,
        seed=seeds,
    )
