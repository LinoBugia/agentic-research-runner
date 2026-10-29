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
from collections.abc import Mapping, Sequence
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
    MAX_COUNT,
    PLACEHOLDER_RE,
    AgentConfig,
    AgentDefaults,
    AgentSpec,
    ApprovalSettings,
    GroupChatConfig,
    GroupSpec,
    Limits,
    ProjectSettings,
    Provider,
    ResolvedRun,
    RunInput,
    StageSpec,
    TopologyConfig,
    WorkfolderSettings,
)

__all__ = [
    "ConfigError",
    "ConfigIssue",
    "check_group_spec",
    "dump_yaml_model",
    "load_group_spec",
    "load_run_input",
    "load_yaml_model",
    "resolve_run",
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


def _to_plain(node: Any, loc: Loc, lines: dict[Loc, int]) -> Any:
    """Convert ruamel round-trip nodes to plain Python values and record key lines."""
    if isinstance(node, CommentedMap):
        out: dict[Any, Any] = {}
        for key, value in node.items():
            child = (*loc, key)
            with contextlib.suppress(KeyError, TypeError, AttributeError):
                lines[child] = node.lc.key(key)[0] + 1
            out[key] = _to_plain(value, child, lines)
        return out
    if isinstance(node, CommentedSeq):
        items = []
        for index, value in enumerate(node):
            child = (*loc, index)
            with contextlib.suppress(KeyError, TypeError, AttributeError, IndexError):
                lines[child] = node.lc.item(index)[0] + 1
            items.append(_to_plain(value, child, lines))
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
    try:
        raw = YAML(typ="rt").load(text)
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
    data = _to_plain(raw, (), lines)
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
        raise ConfigError(validation_issues(exc, lines=lines), source=source) from None


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
    """Format a location tuple as a path: `("agents", 1, "model")` → `agents[1].model`."""
    out = ""
    for segment in loc:
        if isinstance(segment, int):
            out += f"[{segment}]"
        elif not _is_type_tag(segment):
            out += f".{segment}" if out else str(segment)
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
        text = f"Unbekanntes Feld „{ctx['field']}“."
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
) -> list[ConfigIssue]:
    """Translate a pydantic `ValidationError` into German `ConfigIssue`s with paths.

    `prefix` is prepended to every location (e.g. `("agents", 1)` when a single entry was
    validated). `lines` maps locations to YAML line numbers. Duplicates per path are dropped.
    """
    issues: list[ConfigIssue] = []
    seen: set[str] = set()
    for error in exc.errors():
        loc = _clean_loc((*prefix, *error["loc"]))
        if error["type"] == "unknown_field":
            loc = (*loc, error["ctx"]["field"])
        path = format_loc(loc)
        if path in seen:
            continue
        seen.add(path)
        issues.append(ConfigIssue(path, _message(error), _line_for(loc, lines)))
    return issues


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
            known = ", ".join(sorted(params)) or "keine"
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


def _check_when(spec: AgentSpec, params: Mapping[str, Any], loc: Loc) -> ConfigIssue | None:
    if spec.when is None or spec.when.param in params:
        return None
    known = ", ".join(sorted(params)) or "keine"
    return ConfigIssue(
        format_loc((*loc, "when", "param")),
        f"„when“ bei „{spec.id}“ prüft den unbekannten Parameter „{spec.when.param}“. "
        f"Lege ihn unter „params“ an oder korrigiere den Namen (vorhanden: {known}).",
    )


def _template_sections(container: GroupSpec | RunInput) -> list[tuple[Loc, Any]]:
    """Sections of a group/run spec in which placeholders may occur, with their locations."""
    sections: list[tuple[Loc, Any]] = []
    if container.defaults is not None:
        sections.append((("defaults",), _dump(container.defaults)))
    sections += [(("agents", i), _dump(a)) for i, a in enumerate(container.agents)]
    sections += [(("groupchats", i), _dump(c)) for i, c in enumerate(container.groupchats)]
    sections += [(("stages", i), _dump(s)) for i, s in enumerate(container.stages)]
    sections += [(("raters", i), r) for i, r in enumerate(container.raters)]
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
    known = ", ".join(sorted(declared)) or "keine"
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
    available = ", ".join(active)
    for index, entry in enumerate(entries):
        path = format_loc((*loc, index))
        if _is_pattern(entry):
            hits = [a for a in active if fnmatchcase(a, entry)]
            if not hits and not disabled.matches(entry):
                issues.append(
                    ConfigIssue(
                        path,
                        f"Das Muster „{entry}“ passt auf keinen Agenten. Vorhanden: {available}.",
                    )
                )
        elif entry in active:
            hits = [entry]
        elif entry in disabled:
            hits = []
        else:
            hits = []
            hint = ""
            if any(a.startswith(f"{entry}-") for a in active):
                hint = f" Meintest du „{entry}-*“ (Replikate aus „count“)?"
            else:
                close = difflib.get_close_matches(entry, active, n=1)
                if close:
                    hint = f" Meintest du „{close[0]}“?"
            issues.append(
                ConfigIssue(
                    path,
                    f"Unbekannter Agent „{entry}“.{hint} Vorhanden: {available}. "
                    "Prüfe die Schreibweise oder lege den Agenten unter „agents“ an.",
                )
            )
        for hit in hits:
            if hit not in result:
                result.append(hit)
    return result


def _resolve_channels(
    chats: Sequence[GroupChatConfig],
    source: str,
    params: Mapping[str, Any],
    active: Sequence[str],
    disabled: _Disabled,
) -> list[GroupChatConfig]:
    issues: list[ConfigIssue] = []
    resolved: list[GroupChatConfig] = []
    names: set[str] = set()
    for index, original in enumerate(chats):
        loc: Loc = ("groupchats", index)
        before = len(issues)
        data = _substitute(_dump(original), params, loc, issues)
        if len(issues) > before:
            continue
        try:
            chat = GroupChatConfig.model_validate(data)
        except ValidationError as exc:
            issues += validation_issues(exc, prefix=loc)
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

    def pick[T](own: list[T], group_attr: str) -> tuple[list[T], str]:
        if own or group_spec is None:
            return own, RUN_SOURCE
        return getattr(group_spec, group_attr), GROUP_SOURCE

    chats, chats_source = pick(run_input.groupchats, "groupchats")
    channels = _resolve_channels(chats, chats_source, params, active, disabled)

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
        rubric=run_input.rubric or (group_spec.rubric if group_spec else None),
        stages=stages,
        raters=raters,
        limits=limits,
        approvals=approvals,
        agents=agents,
        groupchats=channels,
    )
