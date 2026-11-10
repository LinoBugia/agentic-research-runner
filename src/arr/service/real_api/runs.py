"""Area `runs`: runs of the workfolder, their detail views, actions and starting runs (W3-01,
docs/08-ux-konzept.md §5.3, §6.9; docs/02-workfolder-und-runs.md §7).

Data comes from the run folders through the `RunStore` (`rundir`), so runs of the service,
runs of `arr run` and finished runs look the same. Runs started here (`start_run`) run as a
task in the service's event loop through `run_launch` (reservation with `starter: gui`,
`RunLock`, backends, finalizer, secret scan); the `RunManager` of such a run is reachable
through the `RunRegistry` for pause, resume, stop and messages. A run of another process
(`arr run`) can only be stopped (`stop.request`); pause, resume and messages answer
`not_allowed` with the hint that it runs in the terminal.

„Neuer Run“ sends the input of its form (`form_input`), a run file's content stays valid. Both
are checked like the preparation will (`_check_prepared`), so a problem stands at its field and
the preview is what `run.yaml` will hold. Delivery states of `user` messages change rows the
watcher has already seen; `_follow_delivery` publishes them again (W3-02).
"""

import asyncio
import contextlib
import copy
import dataclasses
import logging
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from arr.errors import ArrError, ConfigError, ConfigIssue
from arr.models import (
    AgentConfig,
    ChannelMode,
    GroupChatConfig,
    ParamSpec,
    ProfilesFile,
    Provider,
    ProviderDefaults,
    ResolvedRun,
    RunInput,
    RunStatus,
    Starter,
    TopologyKind,
    WorkfolderSettings,
)
from arr.permissions.policy import REDACTED, _redact_data, redact
from arr.resolve import dump_yaml_model, load_group_spec, validate_model
from arr.room.types import DeliveryStatus, MessageVia
from arr.room.types import Message as RoomMessage
from arr.runs.launch import Launch, plan_run_input, request_stop, reserve_run, run_launch
from arr.runs.manager import RunManager, set_run_status
from arr.runs.orchestrator import OrchestratorLog
from arr.runs.prepare import _check_codex_toolchain, _make_plan, load_run_meta
from arr.service.api_client import count_active, count_runs_waiting
from arr.service.api_models import (
    REPEAT_SUFFIX,
    Action,
    ActionResult,
    AgentChanges,
    AgentState,
    ApiException,
    CapacityInfo,
    ChannelInfo,
    ChannelMessageEvent,
    CountsChangedEvent,
    Deviation,
    ErrorCode,
    Estimate,
    FieldIssue,
    LogEntry,
    Message,
    MessagesPage,
    PostMessageRequest,
    Range,
    ResolveResult,
    RunAction,
    RunCounts,
    RunDetail,
    RunFilter,
    RunInputAgent,
    RunInputChannel,
    RunInputParam,
    RunInputs,
    RunResults,
    RunsPage,
    RunStartAgent,
    RunStartChannel,
    RunStatusEvent,
    RunSummary,
    StartRunResponse,
    TranscriptKind,
    TranscriptPage,
)
from arr.service.gui import format as fmt
from arr.service.real_api.approvals import ApprovalsArea
from arr.service.real_api.base import RunHandle, ServiceState, not_allowed, not_found
from arr.service.real_api.catalog import check_group
from arr.service.real_api.profiles import provider_of as _profile_provider
from arr.service.real_api.rundir import (
    RunRecord,
    agent_changes,
    agent_states,
    channel_infos,
    log_entries,
    messages_page,
    model_label,
    open_escalation_id,
    role_label,
    run_detail,
    run_results,
    run_summary,
    to_message,
    transcript_page,
    vendor_of,
)
from arr.service.real_api.setup import toolchain_notice
from arr.service.real_api.storage import STORAGE_ACTIONS, StorageArea
from arr.service.real_api.workfolder import counts
from arr.skills import discover_skills
from arr.workfolder import load_workfolder_settings, os_reason, project_dir

log = logging.getLogger("arr.service.runs")

TERMINAL_HINT = (
    "Der Run läuft im Terminal („arr run“), nicht in arr selbst. Von hier geht nur Stoppen; "
    "Pausieren, Fortsetzen und Nachrichten gehen bei Runs, die du in arr gestartet hast."
)
ESCALATION_HINT = (
    "Der Run wartet auf eine Entscheidung in der GUI. Weiter geht es nur über die Eskalation."
)
FINISHED_HINT = "Der Run ist beendet. {what} geht nur bei einem laufenden Run."
TASK_MISSING = "Die Aufgabe fehlt. Schreib, woran die Agenten arbeiten sollen."
TASK_PLACEHOLDER = "(noch keine Aufgabe)"
"""Stands in for an empty task while the form is checked: the rest can already be shown."""
NO_PROFILE = FieldIssue(
    path="profile",
    message="Es gibt noch kein Auth-Profil. Ohne Zugang können keine Agenten starten, das "
    "Einrichten dauert etwa 2 Minuten.",
    action=Action(label="Profil anlegen", url="/profile?neu=1"),
)
"""`load_profiles` explains a missing file for the terminal; here a link does that (E18)."""
SWITCHED = (
    "Der Workfolder wurde gewechselt, während arr den Run vorbereitet hat. Der Run ist nicht "
    "gestartet. Prüfe die Angaben im neuen Workfolder und starte ihn erneut."
)
STANDARDS_PLACE = "„Einstellungen → Standards und Limits“"
STANDARD_MODELS = {Provider.CLAUDE: "claude-sonnet-5-5", Provider.CODEX: "gpt-5.5"}
"""Model of a run from the form while `arr.yaml` has no standard for the provider."""
SOLO_AGENT = "solver"
"""The one agent of a run without a skill (docs/08 §5.4 „Ohne Skill“)."""
DELIVERY_POLL_S = 0.25
DELIVERY_POLL_MAX_S = 1.0
_AGENT_PATH = re.compile(r"agents\[(\d+)\]\.(\w+)")
_FOREIGN_PATH = re.compile(r"(?<![\w./:-])~?/[^\s„“\"'`,;:()]+")
_MISSING_STANDARD = re.compile(r"Für (\w+) fehlt ")
_MISSING_KEY = re.compile(r"agents\[\d+\]\.(model|auth)$")
_FILE_HINT = re.compile(r"Ergänze „\w+“ .*? fest\.")
_FORM_PATHS = {"task": "task_md", "task_file": "task_md", "limits.wall_time": "limits.wall_time_s"}


def _task_missing(run_input: Mapping[str, Any]) -> bool:
    return "task_md" in run_input and not str(run_input["task_md"] or "").strip()


def form_input(
    run_input: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """A run input as `RunInput` data, start parameters and overrides per agent.

    The form of „Neuer Run“ sends `project`, `skill`, `name`, `task_md`, `params`, `agents` (per
    agent `model`, `web`, `read_dirs`; a repeat also `auth` and `effort`), `limits`
    (`wall_time_s`, `turns_per_agent`,
    `budget_usd`) and `decider` (see `MockApi.resolve_run`); only what differs from the
    standards counts. The content of a run file has no `task_md` and passes as it is.
    """
    data = dict(run_input)
    params = data.pop("params", None)
    params = dict(params) if isinstance(params, Mapping) else {}
    if "task_md" not in data:
        return data, params, {}
    data["task"] = TASK_PLACEHOLDER if _task_missing(run_input) else data["task_md"]
    del data["task_md"]
    overrides = data.pop("agents", None) or {}
    decider = data.pop("decider", None)
    if decider:
        data["approvals"] = {"decider": decider}
    limits = dict(data.pop("limits", None) or {})
    if "wall_time_s" in limits:
        limits["wall_time"] = limits.pop("wall_time_s")
    if limits:
        data["limits"] = limits
    for key in ("name", "skill"):
        if not data.get(key):
            data.pop(key, None)
    if "skill" not in data:
        data["topology"] = {"kind": "solo"}
        data["agents"] = [{"id": SOLO_AGENT}]
        params = {}
    return data, params, {k: v for k, v in overrides.items() if v}


def _provider_of(model: str) -> Provider:
    return Provider.CLAUDE if model.startswith("claude") else Provider.CODEX


def _start_failure(exc: Exception, workfolder: Path) -> str:
    """Why a run could not be prepared, for the page: German, no traceback, no foreign path.

    A file outside the workfolder is named by its last part only (N8, W3-04.4): a path with
    spaces would otherwise keep everything after the first space.
    """
    roots = (workfolder, Path(os.path.realpath(workfolder)))
    if isinstance(exc, ArrError):
        text = exc.message
    elif isinstance(exc, OSError) and exc.filename:
        name = str(exc.filename)
        if not any(name.startswith(f"{root}/") for root in roots):
            name = Path(name).name
        text = (
            f"Kein Zugriff auf {name} ({os_reason(exc)}). Prüfe die Rechte und starte "
            "den Run erneut."
        )
    else:
        text = "Bei der Vorbereitung ist ein interner Fehler aufgetreten. Starte den Run erneut."
    text = redact(text)
    for root in roots:
        text = text.replace(f"{root}/", "")
    return _FOREIGN_PATH.sub(lambda m: Path(m[0]).name, text)


def _first_profiles(profiles: ProfilesFile) -> dict[Provider, str]:
    """The first profile of each provider, in the order of `profiles.yaml`."""
    first: dict[Provider, str] = {}
    for profile in profiles.profiles:
        first.setdefault(_profile_provider(profile.kind), profile.name)
    return first


def _without_file_hint(exc: ConfigError) -> ConfigError:
    """The form never sends anyone to `arr.yaml`: the settings page sets the standards."""
    hint = f"Lege ein Profil an und wähle es unter {STANDARDS_PLACE}."
    issues = [ConfigIssue(i.path, _FILE_HINT.sub(hint, i.message), i.line) for i in exc.issues]
    return ConfigError(issues, source=exc.source)


def _agent_specs(
    resolved: ResolvedRun,
    overrides: Mapping[str, Any],
    settings: WorkfolderSettings,
    profiles: ProfilesFile,
) -> list[dict[str, Any]]:
    """The resolved agents as entries of `run.yaml`, with the changes of the form.

    Agents of a run replace those of its skill as a whole (docs/02 §3.3), so all of them are
    listed. A model of the other provider takes profile and effort from `arr.yaml`, without a
    standard the first profile of that provider.
    """
    first = _first_profiles(profiles)
    specs = []
    for agent in resolved.agents:
        spec = agent.model_dump(
            mode="json", exclude={"base_id", "replica", "channels"}, exclude_none=True
        )
        change = overrides.get(agent.id, {})
        model = change.get("model")
        if model and model != agent.model:
            spec["model"] = model
            provider = _provider_of(model)
            if provider is not agent.provider:
                standard = settings.defaults.get(provider)
                spec["provider"] = provider.value
                spec.pop("auth", None)
                spec.pop("effort", None)
                auth = (standard.auth if standard is not None else None) or first.get(provider)
                if auth:
                    spec["auth"] = auth
                if standard is not None and standard.effort:
                    spec["effort"] = standard.effort.value
        # profile and effort of a run that is repeated (W3-10 V8): they win over the standards
        if change.get("auth"):
            spec["auth"] = change["auth"]
        if "effort" in change:
            if change["effort"]:
                spec["effort"] = change["effort"]
            else:
                spec.pop("effort", None)
        if "web" in change:
            spec["tools"] = {**spec.get("tools", {}), "web": bool(change["web"])}
        if "read_dirs" in change:
            spec["read_dirs"] = list(change["read_dirs"])
        specs.append(spec)
    return specs


def _named(exc: ConfigError, agents: Sequence[str]) -> ConfigError:
    """`agents[1].auth` → `agents.<id>.profile`: the paths the fields of the form carry."""

    def path(text: str) -> str:
        match = _AGENT_PATH.match(text)
        if match is None or int(match[1]) >= len(agents):
            return text
        field = "profile" if match[2] == "auth" else match[2]
        return f"agents.{agents[int(match[1])]}.{field}"

    issues = [ConfigIssue(path(i.path), i.message, i.line) for i in exc.issues]
    return ConfigError(issues, source=exc.source)


def _field_issues(exc: ConfigError) -> list[FieldIssue]:
    out = []
    for issue in exc.issues:
        path = _FORM_PATHS.get(issue.path, issue.path)
        out.append(
            FieldIssue(
                path="decider" if path.startswith("approvals") else path, message=issue.message
            )
        )
    return out


def _delivery_settled(message: RoomMessage) -> bool:
    """True if the state cannot change: „geweckt“ stays, a hint to the only addressee too.
    Otherwise another addressee can still be woken, up to the end of the run."""
    status = message.delivery.status if message.delivery is not None else None
    return status is DeliveryStatus.WOKEN or (
        status is DeliveryStatus.HINT_MID_TURN and len(message.to) == 1
    )


SECRET_NOTE = (
    "Ein Wert der Eingaben (Name, Parameter, Agent oder Kanal) sah wie ein Geheimnis aus und "
    f"steht hier als „{REDACTED}“; trag ihn bei Bedarf neu ein."
)


def _without_secrets(inputs: RunInputs) -> RunInputs:
    """Every text of the answer through the secret filter (V9 „ein Secret steht nie darin“,
    W3-10.1): names, parameter values, agents, channels and the repeat, not only the task.
    `run.yaml` is filtered value by value (`_redact_yaml`) already."""
    data = inputs.model_dump(mode="json", exclude={"run_yaml", "repeat_notes", "task_md"})
    clean = _redact_data(data)
    for agent in clean["agents"]:  # derived from the role file: „Sk-ant-…“ is no key any more
        if agent.get("role"):
            agent["role_label"] = role_label(agent["role"], agent["role_label"])
    notes = list(inputs.repeat_notes)
    if clean != data:  # the task in `repeat` is filtered already and does not count again
        notes.append(SECRET_NOTE)
    return RunInputs.model_validate(
        {**clean, "task_md": inputs.task_md, "run_yaml": inputs.run_yaml, "repeat_notes": notes}
    )


def _redact_yaml(text: str) -> str:
    """`redact` for each value of a YAML file: across lines, `fake-auth` followed by the next
    key looks like the flag `-auth <value>` and would hide the next line."""
    if "PRIVATE KEY-----" in text:
        text = redact(text)  # a key block spans lines
    out = []
    for line in text.splitlines(keepends=True):
        key, sep, value = line.partition(": ")
        out.append(f"{key}{sep}{redact(value)}" if sep else redact(line))
    return "".join(out)


def _keep_origin(launch: Launch, origin: ResolvedRun) -> Launch:
    """Agents changed in the form keep `base_id` and `replica` of the skill (W3-10 V8).

    The changes make them explicit agents of the run, which would turn `m-1` of `m: count 2`
    into its own base: another role name in the prompt and another `run.yaml` than the same
    run without changes.
    """
    before = {a.id: a for a in origin.agents}
    agents = [
        a.model_copy(update={"base_id": before[a.id].base_id, "replica": before[a.id].replica})
        if a.id in before
        else a
        for a in launch.resolved.agents
    ]
    resolved = launch.resolved.model_copy(update={"agents": agents})
    return dataclasses.replace(launch, resolved=resolved)


def _agent_choice(agent: AgentConfig) -> dict[str, Any]:
    """What „Neuer Run“ can set per agent, as a change of the form (`_agent_specs`)."""
    return {
        "model": agent.model,
        "auth": agent.auth,
        "effort": agent.effort.value if agent.effort is not None else None,
        "web": agent.tools.web,
        "read_dirs": [str(d) for d in agent.read_dirs],
    }


def _agent_rest(agent: AgentConfig) -> tuple[Any, ...]:
    """What the form cannot set: it comes from the skill."""
    return (agent.role, agent.workspace, tuple(agent.skills), agent.tools.shell)


def _input_agent(agent: AgentConfig) -> RunInputAgent:
    return RunInputAgent(
        id=agent.id,
        role_label=role_label(agent.role, agent.base_id),
        role=agent.role,
        provider=agent.provider,
        vendor=vendor_of(agent.provider),
        model=agent.model,
        model_label=model_label(agent.model),
        profile=agent.auth,
        effort=agent.effort,
        workspace_mode=agent.workspace.mode,
        workspace_name=agent.workspace.name,
        web=agent.tools.web,
        shell=agent.tools.shell,
        read_dirs=[str(d) for d in agent.read_dirs],
        skills=list(agent.skills),
        channels=list(agent.channels),
    )


def _input_channel(channel: GroupChatConfig) -> RunInputChannel:
    return RunInputChannel(
        name=channel.name,
        purpose=channel.purpose,
        members=list(channel.members),
        mode=channel.mode,
        order=channel.order.value,
        moderator=channel.moderator,
        open_from=channel.open_from,
        anonymous=channel.anonymous,
        wake_on=[w.value for w in channel.wake_on],
        debounce_s=channel.debounce_s,
        idle_after_s=channel.idle_after_s,
        quiet_after_s=channel.quiet_after_s,
        max_wakeups_per_agent=channel.max_wakeups_per_agent,
    )


def stored_run(resolved: ResolvedRun) -> ResolvedRun:
    """`resolved` as `prepare_run` writes it to `run.yaml`."""
    return resolved.model_copy(update={"task": None, "task_file": "task.md"})


def _check_prepared(launch: Launch) -> None:
    """The checks of `prepare_run` before anything exists: roles, skills, profiles, toolchain.

    Later they could only end the task of the run, after the user has left this page.
    """
    root = launch.workfolder
    resolved = launch.resolved
    project = project_dir(root, resolved.project)
    catalog = discover_skills(root / "skills")
    _make_plan(root, project, resolved, catalog, None, launch.profiles, False)
    _check_codex_toolchain(root, resolved, launch.settings)


# --------------------------------------------------------------------------------------------
# Requests of Claude Code (W4-01, docs/04 §2 `start_run`/`estimate_run`)
# --------------------------------------------------------------------------------------------

USER_AGENT_SOURCE = "Claude Code"
OVERRIDE_KEYS = ("name", "limits", "approvals", "defaults", "agents")
"""`overrides` is a partial `RunInput`: these keys, `agents` merged per agent ID (Codeplan §8,
W1-01 review)."""


def _deep_merge(base: Mapping[str, Any], change: Mapping[str, Any]) -> dict[str, Any]:
    """`change` over `base`: mappings merge key by key, everything else replaces."""
    out = dict(base)
    for key, value in change.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _issue(path: str, message: str) -> ConfigError:
    return ConfigError.single(path, message, source=USER_AGENT_SOURCE)


def _agent_overrides(raw: Any) -> dict[str, dict[str, Any]]:
    """`overrides.agents` as ID → partial agent: a mapping, or a list of entries with `id`."""
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str):
                raise _issue(f"overrides.agents[{index}]", "Jeder Eintrag braucht „id“.")
            items.append((entry["id"], {k: v for k, v in entry.items() if k != "id"}))
    else:
        raise _issue("overrides.agents", "„agents“ ist eine Zuordnung Agent-ID → Änderungen.")
    out: dict[str, dict[str, Any]] = {}
    for agent_id, change in items:
        if not isinstance(change, Mapping):
            raise _issue(f"overrides.agents.{agent_id}", "Die Änderungen sind eine Zuordnung.")
        out[str(agent_id)] = copy.deepcopy(dict(change))
    return out


def _limit_override(raw: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    if "wall_time_s" in out:
        out["wall_time"] = out.pop("wall_time_s")
    return out


def _apply_adjust(
    data: dict[str, Any],
    params: dict[str, Any],
    overrides: dict[str, Any],
    agents: dict[str, dict[str, Any]],
    adjust: Mapping[str, Any],
) -> None:
    """Your changes of „Anpassen“ (`config` paths) on top of the request (docs/08 §5.4)."""
    for path, value in adjust.items():
        parts = path.split(".")
        if path == "task_md":
            data["task"] = value
        elif path == "decider":
            overrides["approvals"] = {**(overrides.get("approvals") or {}), "decider": value}
        elif parts[0] == "params" and len(parts) == 2:
            params[parts[1]] = value
        elif parts[0] == "limits" and len(parts) == 2:
            limits = dict(overrides.get("limits") or {})
            limits.update(_limit_override({parts[1]: value}))
            overrides["limits"] = limits
        elif parts[0] == "agents" and len(parts) >= 3:
            change = agents.setdefault(parts[1], {})
            rest = parts[2:]
            if rest == ["tools", "web"]:
                change["tools"] = {**(change.get("tools") or {}), "web": bool(value)}
            elif rest == ["read_dirs"]:
                change["read_dirs"] = list(value or [])
            elif rest == ["model"]:
                change["model"] = value
                change.pop("provider", None)
                change.pop("auth", None)
                change.pop("effort", None)


def _merged_agents(
    resolved: ResolvedRun,
    changes: Mapping[str, Mapping[str, Any]],
    settings: WorkfolderSettings,
    profiles: ProfilesFile,
) -> list[dict[str, Any]]:
    """The resolved agents as `run.yaml` entries with the changes merged per agent ID.

    A model of the other provider takes profile and effort from `arr.yaml` (like the form),
    unless the change names them.
    """
    known = [a.id for a in resolved.agents]
    for agent_id in changes:
        if agent_id not in known:
            raise _issue(
                f"overrides.agents.{agent_id}",
                f"Den Agenten „{agent_id}“ gibt es in diesem Run nicht (Agenten: "
                f"{', '.join(known)}). Neue Agenten gehören in „config“.",
            )
    first = _first_profiles(profiles)
    specs = []
    for agent in resolved.agents:
        spec = agent.model_dump(
            mode="json", exclude={"base_id", "replica", "channels"}, exclude_none=True
        )
        change = changes.get(agent.id)
        if change:
            spec = _deep_merge(spec, change)
            model = change.get("model")
            if model and "provider" not in change:
                provider = _provider_of(str(model))
                if provider is not agent.provider:
                    standard = settings.defaults.get(provider)
                    spec["provider"] = provider.value
                    if "auth" not in change:
                        spec.pop("auth", None)
                        auth = (standard.auth if standard is not None else None) or first.get(
                            provider
                        )
                        if auth:
                            spec["auth"] = auth
                    if "effort" not in change:
                        spec.pop("effort", None)
                        if standard is not None and standard.effort:
                            spec["effort"] = standard.effort.value
        specs.append(spec)
    return specs


def request_data(
    spec: Mapping[str, Any], adjust: Mapping[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    """`RunInput` data, parameters and agent changes of a request of Claude Code.

    `spec` holds `project` (from the folder Claude Code works in), `task`, `skill`, `config`
    (a whole `run.yaml` as data), `params`, `overrides` and `name` (docs/04 §2). `adjust` are
    your changes of „Anpassen“.
    """
    config = spec.get("config")
    if config is not None and not isinstance(config, Mapping):
        raise _issue("config", "„config“ ist der Inhalt einer run.yaml als Zuordnung.")
    data: dict[str, Any] = copy.deepcopy(dict(config or {}))
    project = str(spec["project"])
    if data.get("project") not in (None, project):
        raise _issue(
            "config.project",
            f"„config“ nennt das Projekt „{data['project']}“, Claude Code arbeitet in "
            f"„{project}“. Runs gehen nur im eigenen Projekt.",
        )
    data["project"] = project
    if data.get("task_file") is not None:
        raise _issue(
            "config.task_file",
            "Gib die Aufgabe als Text an („task“). Eine Datei liest arr für Claude Code nicht.",
        )
    if spec.get("task"):
        data["task"] = str(spec["task"])
    skill = spec.get("skill")
    if skill:
        if data.get("skill") not in (None, skill):
            raise _issue("skill", "„skill“ und „config.skill“ nennen verschiedene Skills.")
        data["skill"] = skill
    if spec.get("name"):
        data["name"] = str(spec["name"])
    params = dict(spec.get("params") or {})
    raw = spec.get("overrides") or {}
    if not isinstance(raw, Mapping):
        raise _issue("overrides", "„overrides“ ist eine Zuordnung (Teil einer run.yaml).")
    overrides = copy.deepcopy(dict(raw))
    unknown = sorted(set(overrides) - set(OVERRIDE_KEYS))
    if unknown:
        raise _issue(
            f"overrides.{unknown[0]}",
            f"„{unknown[0]}“ lässt sich über „overrides“ nicht ändern. Erlaubt sind "
            f"{', '.join(OVERRIDE_KEYS)}; alles andere gehört in „config“.",
        )
    agents = _agent_overrides(overrides.pop("agents", None))
    if adjust:
        _apply_adjust(data, params, overrides, agents, adjust)
    if "name" in overrides:
        data["name"] = overrides.pop("name")
    if overrides.get("limits") is not None:
        if not isinstance(overrides["limits"], Mapping):
            raise _issue("overrides.limits", "„limits“ ist eine Zuordnung.")
        data["limits"] = {
            **_limit_override(data.get("limits") or {}),
            **_limit_override(overrides["limits"]),
        }
    for key in ("approvals", "defaults"):
        if overrides.get(key) is not None:
            if not isinstance(overrides[key], Mapping):
                raise _issue(f"overrides.{key}", f"„{key}“ ist eine Zuordnung.")
            data[key] = _deep_merge(data.get(key) or {}, overrides[key])
    if not data.get("skill") and not data.get("agents") and not data.get("topology"):
        data["topology"] = {"kind": "solo"}
        data["agents"] = [{"id": SOLO_AGENT}]
    if not data.get("task"):
        raise _issue("task", TASK_MISSING)
    return data, params, agents


# --------------------------------------------------------------------------------------------
# Estimate (W4-02, docs/08 §5.4 „Schätzung“, docs/04 §2 `estimate_run`)
# --------------------------------------------------------------------------------------------


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return int(value)


def _topology_turns(resolved: ResolvedRun) -> tuple[int, int | None, list[str]]:
    """Fewest and most turns the topology itself gives (None: open), with German notes.

    Follows the topologies (docs/03 §4): `solo` asks again up to `max_turns`; `roundtable`
    has one turn per member and round (`event`: kick-off, at most `max_wakeups_per_agent`
    wake-ups and `rounds` idle rounds) plus the summarizer; `waterfall` one attempt per stage
    (author + raters, each asked once more at most), up to `1 + max_rework` attempts.
    """
    params = resolved.params
    agents = len(resolved.agents)
    kind = resolved.topology.kind
    if kind is TopologyKind.SOLO:
        most = max(1, _int(params.get("max_turns"), 3))
        if params.get("require_result") is False:
            most = 1
        return 1, most, []
    if kind is TopologyKind.ROUNDTABLE:
        rounds = max(1, _int(params.get("rounds"), 3))
        name = params.get("channel")
        if name is None and len(resolved.groupchats) == 1:
            name = resolved.groupchats[0].name
        try:
            channel = resolved.channel(str(name))
        except KeyError:
            return agents, None, []
        members = len(channel.members)
        extra = 1 if params.get("summarizer") else 0
        if channel.mode is ChannelMode.ROUNDS:
            return members + extra, members * rounds + extra, []
        wakeups = channel.max_wakeups_per_agent
        return (
            members + extra,
            members * (1 + wakeups + rounds) + extra,
            ["Ereignisgesteuert: Wie oft Agenten geweckt werden, hängt von der Diskussion ab."],
        )
    if kind is TopologyKind.WATERFALL:
        rework = max(0, _int(params.get("max_rework"), 2))
        least = most = 0
        for stage in resolved.stages:
            raters = sum(1 for r in resolved.raters if r != stage.agent)
            least += 1 + raters
            most += (1 + rework) * (2 + 2 * raters)
        notes = []
        if str(params.get("on_fail") or "continue") == "escalate":
            notes.append("Nach einer Eskalation kann es weitere Versuche geben.")
            return least, None, notes
        return least, most, notes
    return agents, None, []


def estimate_run(resolved: ResolvedRun) -> Estimate:
    """Rough ranges for turns, cost and duration of a resolved run, without invented prices.

    Turns come from the topology and are capped by `turns_per_agent` × agents. Costs: arr
    knows no prices, so the only honest number is the budget as an upper bound, and only when
    every agent reports costs (Claude); with Codex agents or without budget the cost is
    unknown. The duration is at most `wall_time`.
    """
    limits = resolved.limits
    agents = resolved.agents
    least, most, notes = _topology_turns(resolved)
    cap = len(agents) * limits.turns_per_agent if limits.turns_per_agent else None
    if most is None or (cap is not None and cap < most):
        most = cap
    turns_open = most is None
    if most is None:
        most = least
        notes.append("Ohne Limit für Turns pro Agent ist die Obergrenze offen.")
    least = min(least, most)
    budget = limits.budget_usd
    codex = any(a.provider is Provider.CODEX for a in agents)
    if budget is not None and not codex:
        cost = Range(min=0, max=budget)
        cost_basis = "budget"
        notes.append(
            f"Kosten: höchstens das Budget von {fmt.budget(budget)}. Preise schätzt arr nicht, "
            "bei Abo-Profilen zählt der rechnerische Preis."
        )
    else:
        cost = Range(min=0, max=0)
        cost_basis = "unknown"
        notes.append(
            "Kosten unbekannt: Codex-Agenten melden keine Kosten, sie begrenzen Turns und Laufzeit."
            if codex
            else "Kosten unbekannt: Der Run hat kein Budget."
        )
    wall = limits.wall_time
    if wall is not None:
        duration = Range(min=0, max=wall.total_seconds())
        duration_basis = "wall_time"
    else:
        duration = Range(min=0, max=0)
        duration_basis = "unknown"
        notes.append("Dauer unbekannt: Der Run hat keine Laufzeitgrenze.")
    return Estimate(
        turns=Range(min=least, max=most),
        cost_usd=cost,
        duration_s=duration,
        turns_open=turns_open,
        cost_basis=cost_basis,  # type: ignore[arg-type]
        duration_basis=duration_basis,  # type: ignore[arg-type]
        notes=notes,
    )


class _NoProfile(ArrError):
    """`profiles.yaml` does not exist yet."""


class RunsArea:
    """The run methods of `ApiClient` (see the module docstring)."""

    def __init__(self, state: ServiceState, approvals: ApprovalsArea, storage: StorageArea) -> None:
        self.state = state
        self.approvals = approvals
        self.storage = storage
        self.run_starts: Any = None
        """`RunStartsArea` of the service (W4-01), set by `RealApi`."""
        self._followers: set[asyncio.Task[None]] = set()

    # -- helpers ----------------------------------------------------------------------------

    def _record(self, run_id: str) -> RunRecord:
        record = self.state.store.get(run_id)
        if record is None:
            handle = self.state.registry.get(run_id)
            if handle is not None and handle.start_failure:
                raise ApiException.of(ErrorCode.START_FAILED, handle.start_failure)
            raise not_found("Run", run_id)
        if not record.settled:
            record.refresh()
        return record

    def _summary(self, record: RunRecord, *, with_agents: bool) -> RunSummary:
        agents = agent_states(record) if with_agents and record.agents else None
        return run_summary(
            record, base_url=self.state.base_url, now=self.state.now(), agents=agents
        )

    # -- lists and detail --------------------------------------------------------------------

    async def list_runs(
        self,
        status: RunFilter = RunFilter.ALL,
        project: str | None = None,
        starter: Starter | None = None,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> RunsPage:
        records = self.state.store.scan()
        for record in records:
            if not record.settled:
                record.refresh()
        summaries = [self._summary(r, with_agents=r.status.is_active) for r in records]
        base = [
            s
            for s in summaries
            if (project is None or s.project == project)
            and (starter is None or s.starter == starter)
        ]
        needle = (q or "").strip().lower()
        if needle:
            base = [s for s in base if needle in s.name.lower() or needle in s.id.lower()]
        counts = RunCounts(
            all=len(base),
            active=count_active(base),
            waiting=count_runs_waiting(base),
            done=sum(1 for s in base if s.status is RunStatus.DONE),
            error=sum(1 for s in base if s.status in (RunStatus.FAILED, RunStatus.INTERRUPTED)),
            archived=sum(1 for s in base if s.archived),
        )
        test = {
            RunFilter.ALL: lambda s: True,
            RunFilter.ACTIVE: lambda s: s.active,
            RunFilter.WAITING: lambda s: s.open_approvals > 0,
            RunFilter.DONE: lambda s: s.status is RunStatus.DONE,
            RunFilter.ERROR: lambda s: s.status in (RunStatus.FAILED, RunStatus.INTERRUPTED),
            RunFilter.ARCHIVED: lambda s: s.archived,
        }[status]
        rows = [s for s in base if test(s)]
        rows.sort(key=lambda s: (not s.active, -(s.created_at if s.active else (s.ended_at or 0))))
        try:
            offset = max(0, int(cursor or 0))
        except ValueError:
            offset = 0
        limit = max(1, min(limit, 1000))
        page = rows[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(rows) else None
        return RunsPage(
            items=page,
            counts=counts,
            total=len(rows),
            next_cursor=next_cursor,
            projects=sorted({s.project for s in summaries}),
        )

    async def get_run(self, run_id: str) -> RunDetail:
        record = self._record(run_id)
        return run_detail(
            record,
            base_url=self.state.base_url,
            now=self.state.now(),
            owned=self.state.registry.owns(run_id),
            agents=agent_states(record) if record.agents else None,
        )

    async def list_channels(self, run_id: str) -> list[ChannelInfo]:
        return channel_infos(self._record(run_id))

    async def get_messages(
        self,
        run_id: str,
        channel: str,
        before_id: int | None = None,
        since_id: int | None = None,
        limit: int = 50,
    ) -> MessagesPage:
        record = self._record(run_id)
        page = messages_page(record, channel, before_id=before_id, since_id=since_id, limit=limit)
        if page is None:
            raise not_found("Kanal", f"#{channel}")
        return page

    async def list_agents(self, run_id: str) -> list[AgentState]:
        return agent_states(self._record(run_id))

    async def get_transcript(
        self,
        run_id: str,
        agent_id: str,
        from_seq: int | None = None,
        limit: int = 200,
        kinds: Iterable[TranscriptKind] | None = None,
    ) -> TranscriptPage:
        record = self._record(run_id)
        page = transcript_page(record, agent_id, from_seq=from_seq, limit=limit, kinds=kinds)
        if page is None:
            raise not_found("Agent", agent_id)
        return page

    async def get_results(self, run_id: str) -> RunResults:
        return run_results(self._record(run_id))

    async def get_log(self, run_id: str) -> list[LogEntry]:
        return log_entries(self._record(run_id))

    async def get_run_inputs(self, run_id: str) -> RunInputs:
        """What the run was started with (V9) and the input of „Neuer Run“ that repeats it
        (V8): `run.yaml`, `task.md`, the parameters with the docs of the skill."""
        record = self._record(run_id)
        return await asyncio.to_thread(lambda: _without_secrets(self._run_inputs(record)))

    def _run_inputs(self, record: RunRecord) -> RunInputs:
        meta = record.meta
        resolved = record.load_resolved()
        project = resolved.project if resolved is not None else (meta.project if meta else "")
        skill = resolved.skill if resolved is not None else (meta.skill if meta else None)
        topology = resolved.topology.kind if resolved is not None else None
        name = (meta.name if meta else None) or (
            f"{skill or (topology.value if topology else 'solo')} · {project}"
        )
        task = record.task_md
        task_md = redact(task) if task is not None else None
        if resolved is None:
            return RunInputs(
                run_id=record.run_id,
                name=name,
                available=False,
                project=project,
                skill=skill,
                task_md=task_md,
            )
        specs = self._param_specs(skill)
        params = [
            RunInputParam(
                name=key,
                value=value,
                default=specs[key].default if key in specs else None,
                doc=specs[key].doc if key in specs else None,
                changed=key in specs and specs[key].default != value,
            )
            for key, value in resolved.params.items()
        ]
        limits = resolved.limits
        wall = int(limits.wall_time.total_seconds()) if limits.wall_time else None
        text = record.run_yaml_text  # read once by `load_resolved`, with its guards (W4-01)
        repeat, notes = self._repeat_input(resolved, name, task_md or "")
        if task is not None and task_md != task:
            notes.append(
                "Die Aufgabe enthielt etwas, das wie ein Geheimnis aussieht. Es ist hier durch "
                "einen Platzhalter ersetzt; trag es bei Bedarf neu ein."
            )
        return RunInputs(
            run_id=record.run_id,
            name=name,
            available=True,
            project=project,
            skill=skill,
            topology=topology,
            task_md=task_md,
            params=params,
            agents=[_input_agent(a) for a in resolved.agents],
            channels=[_input_channel(c) for c in resolved.groupchats],
            limits={
                "wall_time_s": wall,
                "turns_per_agent": limits.turns_per_agent,
                "budget_usd": limits.budget_usd,
            },
            decider=resolved.approvals.decider,
            approval_timeout_s=int(resolved.approvals.timeout.total_seconds()),
            run_yaml=_redact_yaml(text) if text is not None else None,
            repeat=repeat,
            repeat_notes=notes,
        )

    def _param_specs(self, skill: str | None) -> dict[str, ParamSpec]:
        """Declared parameters of the skill as it is now; none without the skill."""
        if not skill:
            return {}
        info = discover_skills(self.state.workfolder / "skills").skills.get(skill)
        if info is None or info.group_file is None:
            return {}
        try:
            return dict(load_group_spec(info.group_file).params)
        except ArrError:
            return {}

    def _repeat_input(
        self, resolved: ResolvedRun, name: str, task_md: str
    ) -> tuple[dict[str, Any], list[str]]:
        """The form input that gives the same `run.yaml` (V8), with what it cannot carry.

        Project, skill, parameters, task, limits and decider go as they are. For the agents
        only what differs from the skill as it resolves now goes as a change, so a run
        without changes repeats with none. Without that resolution (project, skill or profile
        gone) every agent keeps its model, profile, effort, web and read folders; the form
        then shows the missing piece at its field (docs/08 §8.4).
        """
        limits = resolved.limits
        data: dict[str, Any] = {
            "project": resolved.project,
            "skill": resolved.skill or "",
            "name": f"{name.removesuffix(REPEAT_SUFFIX)}{REPEAT_SUFFIX}",  # not twice
            "task_md": task_md,
            "params": dict(resolved.params) if resolved.skill else {},
            "agents": {},
            "limits": {
                "wall_time_s": int(limits.wall_time.total_seconds()) if limits.wall_time else None,
                "turns_per_agent": limits.turns_per_agent,
                "budget_usd": limits.budget_usd,
            },
            "decider": resolved.approvals.decider.value,
        }
        notes: list[str] = []
        try:
            base = self._plan({**data, "skill": data["skill"] or None}).resolved
        except (ArrError, OSError):
            base = None
        now = {a.id: a for a in base.agents} if base is not None else {}
        for agent in resolved.agents:
            current = now.get(agent.id)
            if base is not None and current is None:
                notes.append(
                    f"Den Agenten „{agent.id}“ gibt es im Skill nicht mehr; die Wiederholung "
                    "läuft ohne ihn."
                )
                continue
            wanted = _agent_choice(agent)
            have = _agent_choice(current) if current is not None else {}
            change = {k: v for k, v in wanted.items() if have.get(k) != v}
            if change:
                data["agents"][agent.id] = change
            if current is not None and _agent_rest(current) != _agent_rest(agent):
                notes.append(
                    f"Rolle, Arbeitsordner oder Skills von „{agent.id}“ kommen jetzt aus dem "
                    "Skill und weichen vom alten Run ab."
                )
        if base is not None:
            old_ids = {a.id for a in resolved.agents}
            for agent_id in now:
                if agent_id not in old_ids:
                    notes.append(
                        f"„{agent_id}“ ist neu im Skill und läuft in der Wiederholung mit."
                    )
            if [c.model_dump(mode="json") for c in base.groupchats] != [
                c.model_dump(mode="json") for c in resolved.groupchats
            ]:
                notes.append(
                    "Die Gruppenchats kommen aus dem Skill und haben sich seitdem geändert."
                )
        return data, notes

    async def get_changes(self, run_id: str) -> list[AgentChanges]:
        return agent_changes(self._record(run_id))

    # -- actions ----------------------------------------------------------------------------

    async def run_action(
        self, run_id: str, action: RunAction, dry_run: bool = False
    ) -> ActionResult:
        if action in STORAGE_ACTIONS:
            raise self.storage.action_error(action)
        record = self._record(run_id)
        if action is RunAction.SLIM:
            return await self._slim(record, dry_run=dry_run)
        status = record.status
        words = {RunAction.PAUSE: "Pausieren", RunAction.RESUME: "Fortsetzen"}
        what = words.get(action, "Stoppen")
        if status.is_final:
            raise not_allowed(FINISHED_HINT.format(what=what))
        handle = self.state.registry.get(run_id)
        owned = self.state.registry.owns(run_id)
        if status is RunStatus.AWAITING_APPROVAL:
            message = await self._waiting_action(record, action, dry_run=dry_run)
            record.refresh_meta()
            return ActionResult(
                ok=True, message=message, run=self._summary(record, with_agents=False)
            )
        if owned and handle is not None:
            if action is RunAction.RESUME:
                self._refuse_open_escalation(record)
            message = self._own_action(handle, action, status, dry_run=dry_run)
        else:
            message = self._foreign_action(record, action, dry_run=dry_run)
        record.refresh_meta()
        return ActionResult(ok=True, message=message, run=self._summary(record, with_agents=False))

    async def _waiting_action(self, record: RunRecord, action: RunAction, *, dry_run: bool) -> str:
        """A run start waiting for approval can only be stopped (taken back), never resumed."""
        if action is not RunAction.STOP:
            raise not_allowed(
                "Der Run wartet auf die Freigabe in der GUI. Pausieren und Fortsetzen gehen erst, "
                "wenn er läuft."
            )
        if dry_run:
            return "Der Run-Start würde zurückgenommen, der Run endet als „Gestoppt“."
        if self.run_starts is None or not await self.run_starts.withdraw(record.run_id):
            raise not_allowed("Dieser Run-Start wartet nicht in diesem arr.")
        return "Run-Start zurückgenommen: Der Run endet als „Gestoppt“."

    async def stop_for_profile(self, run_id: str) -> None:
        """The Not-Aus: the normal stop of a run, with finalizer and scan, of either source."""
        await self.run_action(run_id, RunAction.STOP)

    async def _slim(self, record: RunRecord, *, dry_run: bool) -> ActionResult:
        result = await self.storage.slim(record, dry_run=dry_run)
        summary = self._summary(record, with_agents=False)
        if not dry_run:
            # a finished run is not watched any more: tell the open pages here
            hub = self.state.hub
            hub.publish(
                RunStatusEvent(
                    seq=hub.next_seq(), ts=time.time(), run_id=record.run_id, run=summary
                )
            )
        return result.model_copy(update={"run": summary})

    def _refuse_open_escalation(self, record: RunRecord) -> None:
        """Only the card decides an escalation (docs/07 §6, docs/08 §6.7)."""
        item_id = open_escalation_id(record)
        if item_id is not None:
            url = f"/freigaben/{item_id}"
            raise ApiException.of(
                ErrorCode.NOT_ALLOWED,
                ESCALATION_HINT,
                gui_url=f"{self.state.base_url}{url}",
                action=Action(label="Eskalation ansehen", url=url),
            )

    def _own_action(
        self, handle: RunHandle, action: RunAction, status: RunStatus, *, dry_run: bool
    ) -> str:
        manager = handle.manager
        if action is RunAction.PAUSE:
            if status is not RunStatus.RUNNING:
                raise not_allowed("Pausieren geht nur, solange der Run läuft.")
            if not dry_run and manager is not None:
                manager.pause()
            return "Pausiert: Laufende Turns enden noch, danach startet kein neuer."
        if action is RunAction.RESUME:
            if status is not RunStatus.PAUSED:
                raise not_allowed("Fortsetzen geht nur bei einem pausierten Run.")
            if not dry_run and manager is not None:
                manager.resume()
            return "Fortgesetzt: Die Agenten arbeiten weiter."
        if dry_run:
            return (
                "Der Run würde gestoppt: Laufende Turns werden unterbrochen, danach läuft der "
                "Finalizer."
            )
        if manager is not None:
            manager.stop()
        elif handle.task is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(OSError, ValueError):
                set_run_status(handle.run_dir, RunStatus.STOPPED)
        return "Stopp angefordert: Laufende Turns werden unterbrochen, danach läuft der Finalizer."

    def _foreign_action(self, record: RunRecord, action: RunAction, *, dry_run: bool) -> str:
        if action is not RunAction.STOP:
            raise not_allowed(TERMINAL_HINT)
        if dry_run:
            return "Der Prozess, der den Run ausführt, würde ihn in wenigen Sekunden stoppen."
        try:
            new = request_stop(record.run_dir, source="gui")
        except ArrError as exc:
            raise not_allowed(exc.message) from None
        if not new:
            return "Für diesen Run liegt schon eine Stopp-Anforderung vor."
        return (
            "Stopp angefordert: Der Prozess, der den Run ausführt (arr run), stoppt ihn "
            "innerhalb weniger Sekunden, danach läuft der Finalizer."
        )

    async def post_message(
        self,
        run_id: str,
        channel: str,
        request: PostMessageRequest,
        via: MessageVia | str = MessageVia.GUI,
    ) -> Message:
        """A message of the user into a channel; the same rules from the GUI and from Claude
        Code (`via: user_agent`, `post_to_run`, docs/09 L17)."""
        record = self._record(run_id)
        if record.status.is_final:
            raise not_allowed("Der Run ist beendet. Nachrichten gehen nur an laufende Runs.")
        handle = self.state.registry.get(run_id)
        if not self.state.registry.owns(run_id) or handle is None or handle.manager is None:
            raise not_allowed(TERMINAL_HINT)
        room = handle.manager.room
        if room is None:
            raise not_allowed("Der Run wird noch vorbereitet; der Gruppenchat ist noch nicht da.")
        try:
            posted = room.post(
                channel,
                request.body,
                sender="user",
                kind=request.kind,
                to=list(request.to),
                reply_to=getattr(request, "reply_to", None),  # `PostReplyRequest` (W3-10.1)
                via=MessageVia(via),
            )
        except ArrError as exc:
            raise ApiException.of(ErrorCode.INVALID, exc.message) from None
        follower = asyncio.create_task(self._follow_delivery(handle, posted.id))
        self._followers.add(follower)
        follower.add_done_callback(self._followers.discard)
        return to_message(posted)

    async def _follow_delivery(self, handle: RunHandle, msg_id: int) -> None:
        """Publish a `user` message again when its delivery state changes, until the state is
        settled or the run is over. The watcher only reports new message IDs."""
        record = self.state.store.get(handle.run_id)
        shown = None
        delay = DELIVERY_POLL_S
        while record is not None:
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, DELIVERY_POLL_MAX_S)
            store = record.room.store
            try:
                message = store.message(msg_id) if store is not None else None
            except ArrError:
                message = None
            delivery = message.delivery if message is not None else None
            if message is not None and delivery is not None and delivery.status != shown:
                shown = delivery.status
                delay = DELIVERY_POLL_S
                hub = self.state.hub
                hub.publish(
                    ChannelMessageEvent(
                        seq=hub.next_seq(),
                        ts=time.time(),
                        run_id=handle.run_id,
                        message=to_message(message),
                    )
                )
            if (message is not None and _delivery_settled(message)) or handle.done:
                return

    # -- resolving and starting --------------------------------------------------------------

    def _profiles(self) -> ProfilesFile:
        try:
            return self.state.profiles()
        except ConfigError:
            raise
        except ArrError as exc:
            raise _NoProfile(exc.message) from None

    def _form_standards(self, exc: ConfigError) -> dict[str, str] | None:
        """What a run of the form takes from the profiles while `arr.yaml` names no standard.

        Only what is missing goes in: run `defaults` rank above `group.yaml`. The providers of
        the problems come first; a user with only a ChatGPT profile gets a run with it.
        """
        missing = {m[1] for i in exc.issues if (m := _MISSING_KEY.search(i.path))}
        named = [m[1] for i in exc.issues if (m := _MISSING_STANDARD.match(i.message))]
        if not missing or not named:
            return None
        settings = load_workfolder_settings(self.state.workfolder)
        first = _first_profiles(self._profiles())
        for provider in dict.fromkeys([*map(Provider, named), *Provider]):
            standard = settings.defaults.get(provider, ProviderDefaults())
            auth = standard.auth or first.get(provider)
            if auth:
                values = {"model": standard.model or STANDARD_MODELS[provider], "auth": auth}
                return {"provider": provider.value, **{k: values[k] for k in sorted(missing)}}
        return None

    def _refuse_invalid_skill(self, name: str) -> None:
        """A group skill whose check finds problems cannot start (W4-02, docs/08 §5.7)."""
        catalog = discover_skills(self.state.workfolder / "skills")
        info = catalog.skills.get(name)
        if info is None or not info.is_group:
            return  # unknown or broken skills are reported by the resolution itself
        try:
            settings = load_workfolder_settings(self.state.workfolder)
        except ArrError:
            return  # the resolution reports a broken `arr.yaml` itself
        _spec, issues = check_group(info.path, catalog, settings)
        if not issues:
            return
        first = issues[0]
        where = f"{first.file}, Zeile {first.line}: " if first.line else f"{first.file}: "
        more = f" (und {len(issues) - 1} weitere)" if len(issues) > 1 else ""
        raise ConfigError.single(
            "skill",
            f"Der Skill „{name}“ hat Fehler und lässt sich nicht starten: {where}"
            f"{first.message}{more} Behebe sie unter Skills, Reiter „Prüfung“.",
            source="Neuer Run",
        )

    def _plan(self, run_input: dict[str, Any]) -> Launch:
        """A `Launch` from a run input; `ConfigError`/`ArrError` for problems."""
        data, params, overrides = form_input(run_input)
        from_form = "task_md" in run_input

        def plan() -> Launch:
            return plan_run_input(
                validate_model(RunInput, data, source="Neuer Run"),
                workfolder=self.state.workfolder,
                params=params,
                profiles=self._profiles(),
                source="Neuer Run",
            )

        try:
            project_dir(self.state.workfolder, str(data.get("project")))
        except ArrError as exc:
            raise ConfigError.single("project", exc.message, source="Neuer Run") from None
        try:
            try:
                launch = plan()
            except ConfigError as exc:
                standards = self._form_standards(exc) if from_form else None
                if standards is None:
                    raise
                data["defaults"] = standards
                launch = plan()
        except ConfigError as exc:
            raise (_without_file_hint(exc) if from_form else exc) from None
        ids = [a.id for a in launch.resolved.agents]
        try:
            if overrides:
                resolved = launch.resolved
                data["agents"] = _agent_specs(resolved, overrides, launch.settings, launch.profiles)
                data["groupchats"] = [c.model_dump(mode="json") for c in resolved.groupchats]
                data["stages"] = [s.model_dump(mode="json") for s in resolved.stages]
                data["raters"] = list(resolved.raters)
                launch = _keep_origin(plan(), resolved)
            _check_prepared(launch)
        except ConfigError as exc:
            raise _named(exc, ids) from None
        if data.get("skill"):
            # last: what the resolution finds itself stands at its field (e.g. an agent's role)
            self._refuse_invalid_skill(str(data["skill"]))
        return launch

    # -- requests of Claude Code (W4-01) --------------------------------------------------------

    def _plan_data(self, data: dict[str, Any], params: dict[str, Any]) -> Launch:
        def plan() -> Launch:
            return plan_run_input(
                validate_model(RunInput, data, source=USER_AGENT_SOURCE),
                workfolder=self.state.workfolder,
                params=params,
                profiles=self._profiles(),
                source=USER_AGENT_SOURCE,
            )

        try:
            return plan()
        except ConfigError as exc:
            standards = None if data.get("defaults") else self._form_standards(exc)
            if standards is None:
                raise
            data["defaults"] = standards
            return plan()

    def plan_request(
        self, spec: Mapping[str, Any], adjust: Mapping[str, Any] | None = None
    ) -> Launch:
        """A `Launch` from a request of Claude Code, checked like „Neuer Run“ (W4-01).

        `ApiException` (`config_invalid`) with the problems at their paths; an invalid group
        skill is refused here, before anything is reserved (W4-02).
        """
        try:
            data, params, agents = request_data(spec, adjust)
            try:
                project_dir(self.state.workfolder, str(data["project"]))
            except ArrError as exc:
                raise _issue("project", exc.message) from None
            launch = self._plan_data(data, params)
            if agents:
                resolved = launch.resolved
                data["agents"] = _merged_agents(resolved, agents, launch.settings, launch.profiles)
                data["groupchats"] = [c.model_dump(mode="json") for c in resolved.groupchats]
                data["stages"] = [s.model_dump(mode="json") for s in resolved.stages]
                data["raters"] = list(resolved.raters)
                launch = _keep_origin(self._plan_data(data, params), resolved)
            _check_prepared(launch)
            if data.get("skill"):
                self._refuse_invalid_skill(str(data["skill"]))
        except ConfigError as exc:
            raise ApiException.of(
                ErrorCode.CONFIG_INVALID, exc.issues[0].message, issues=_field_issues(exc)
            ) from None
        except _NoProfile:
            raise ApiException.of(
                ErrorCode.CONFIG_INVALID, NO_PROFILE.message, issues=[NO_PROFILE]
            ) from None
        except ArrError as exc:
            raise ApiException.of(ErrorCode.CONFIG_INVALID, exc.message) from None
        return launch

    def standard_of(
        self, spec: Mapping[str, Any], adjust: Mapping[str, Any] | None = None
    ) -> ResolvedRun | None:
        """The same skill, project and task without `config` and `overrides`: what the
        deviations are measured against (None without a skill or when it does not resolve)."""
        config = spec.get("config") if isinstance(spec.get("config"), Mapping) else {}
        skill = spec.get("skill") or (config or {}).get("skill")
        if not skill:
            return None
        bare = {
            "project": spec["project"],
            "skill": skill,
            "task": spec.get("task") or (config or {}).get("task") or TASK_PLACEHOLDER,
            "params": spec.get("params") or {},
        }
        try:
            data, params, _agents = request_data(bare, None)
            return self._plan_data(data, params).resolved
        except (ArrError, OSError, ValueError):
            return None

    def capacity(self, launch: Launch) -> list[CapacityInfo]:
        """Free places per profile against what the run needs (docs/08 §5.0 `capacity`)."""
        needed: dict[str, int] = {}
        for agent in launch.resolved.agents:
            needed[agent.auth] = needed.get(agent.auth, 0) + 1
        out = []
        for profile, count in needed.items():
            limit = launch.settings.concurrency.get(profile)
            gate = self.state.gates.get(profile)
            in_use = gate.active if gate is not None else 0
            free = max(0, (limit - in_use)) if limit is not None else count
            out.append(
                CapacityInfo(
                    profile=profile,
                    free=free,
                    needed=count,
                    limit=limit if limit is not None else count,
                )
            )
        return out

    def add_waiting(self, run_id: str, run_dir: Path, launch: Launch, waiting: Any) -> None:
        """A run start of Claude Code waits in this service (task: wait, then run)."""
        handle = RunHandle(run_id=run_id, run_dir=run_dir, launch=launch)
        handle.task = asyncio.create_task(waiting, name=f"arr-run-{run_id}")
        self.state.registry.add(handle)
        self.state.store.get(run_id)

    async def execute_approved(self, run_id: str, launch: Launch, lock: Any) -> None:
        """Run a run start after the user's approval, in its reserved folder with its lock."""
        handle = self.state.registry.get(run_id)
        if handle is None:  # pragma: no cover - the waiting task registered it
            lock.release()
            return
        handle.launch = launch
        await self._execute(handle, lock=lock, starter=Starter.USER_AGENT)

    async def _resolve_adjust(self, item_id: str, run_input: dict[str, Any]) -> ResolveResult:
        """„Neuer Run“ in „Anpassen“: the request with your changes, and its deviations."""
        from arr.service.real_api.run_starts import check_adjust_paths, form_changes, item_form

        try:
            item = await self.approvals.get_approval(item_id)
        except ApiException as exc:
            return ResolveResult(
                resolved_run_yaml="", errors=[FieldIssue(path="", message=exc.error.message)]
            )
        pending = self.run_starts.pending_of(item.run.id) if self.run_starts else None
        if pending is None or pending.item_id != item.id:
            return ResolveResult(
                resolved_run_yaml="",
                errors=[FieldIssue(path="", message="Dieser Run-Start ist schon entschieden.")],
            )
        form = {k: v for k, v in run_input.items() if k != "von"}
        changes = form_changes(item_form(item), form)
        try:
            check_adjust_paths(changes, [a.id for a in item.agents])
            launch, card = await asyncio.to_thread(
                self.run_starts._plan_with_card, pending.spec, changes
            )
        except ApiException as exc:
            return ResolveResult(
                resolved_run_yaml="",
                errors=list(exc.error.issues) or [FieldIssue(path="", message=exc.error.message)],
            )
        return ResolveResult(
            resolved_run_yaml=dump_yaml_model(stored_run(launch.resolved)),
            agents=[RunStartAgent.model_validate(a) for a in card["agents"]],
            channels=[RunStartChannel.model_validate(c) for c in card["channels"]],
            capacity=[CapacityInfo.model_validate(c) for c in card["capacity"]],
            estimate=Estimate.model_validate(card["estimate"]),
            deviations=[Deviation.model_validate(d) for d in card["deviations"]],
        )

    async def resolve_run(self, run_input: dict[str, Any]) -> ResolveResult:
        if run_input.get("von"):
            return await self._resolve_adjust(str(run_input["von"]), run_input)
        missing = []
        if _task_missing(run_input):
            missing.append(FieldIssue(path="task_md", message=TASK_MISSING))
        try:
            launch = await asyncio.to_thread(self._plan, run_input)
        except ConfigError as exc:
            return ResolveResult(resolved_run_yaml="", errors=[*missing, *_field_issues(exc)])
        except _NoProfile:
            return ResolveResult(resolved_run_yaml="", errors=[*missing, NO_PROFILE])
        except ArrError as exc:
            return ResolveResult(
                resolved_run_yaml="", errors=[FieldIssue(path="", message=exc.message)]
            )
        resolved = launch.resolved
        capacity = self.capacity(launch)
        warnings = [
            f"{c.profile}: {c.free} von {c.limit} Plätzen frei, der Run braucht {c.needed}. Seine "
            "Agenten warten, bis Plätze frei werden."
            for c in capacity
            if c.needed > c.free
        ]
        if any(a.provider is Provider.CODEX for a in resolved.agents):
            notice = toolchain_notice(launch.settings, self.state.workfolder)
            if notice is not None:
                warnings.append(f"{notice.title} {notice.text}")
        if launch.run_input.defaults is not None and "task_md" in run_input:
            used = dict.fromkeys(
                f"{model_label(a.model)} mit dem Profil „{a.auth}“" for a in resolved.agents
            )
            warnings.append(
                f"In diesem Workfolder fehlen Standards, dieser Run nutzt {' und '.join(used)}. "
                f"Festlegen kannst du sie unter {STANDARDS_PLACE}."
            )
        return ResolveResult(
            resolved_run_yaml=dump_yaml_model(stored_run(resolved)),
            errors=missing,
            warnings=warnings,
            agents=[
                RunStartAgent(
                    id=a.id,
                    role_label=role_label(a.role, a.base_id),
                    provider=a.provider,
                    vendor=vendor_of(a.provider),
                    model=a.model,
                    model_label=model_label(a.model),
                    profile=a.auth,
                    tools={"shell": a.tools.shell, "web": a.tools.web},
                    read_dirs=[str(d) for d in a.read_dirs],
                    workspace_mode=a.workspace.mode,
                    skills=list(a.skills),
                )
                for a in resolved.agents
            ],
            channels=[
                RunStartChannel(
                    name=c.name,
                    members=list(c.members),
                    mode=c.mode,
                    anonymous=c.anonymous,
                    open_from=c.open_from,
                )
                for c in resolved.groupchats
            ],
            capacity=capacity,
            estimate=estimate_run(resolved),
        )

    async def start_run(self, run_input: dict[str, Any]) -> StartRunResponse:
        if _task_missing(run_input):
            raise ApiException.of(
                ErrorCode.CONFIG_INVALID,
                TASK_MISSING,
                issues=[FieldIssue(path="task_md", message=TASK_MISSING)],
            )
        workfolder = self.state.workfolder
        try:
            launch = await asyncio.to_thread(self._plan, run_input)
        except ConfigError as exc:
            raise ApiException.of(
                ErrorCode.CONFIG_INVALID, exc.issues[0].message, issues=_field_issues(exc)
            ) from None
        except _NoProfile:
            raise ApiException.of(
                ErrorCode.CONFIG_INVALID, NO_PROFILE.message, issues=[NO_PROFILE]
            ) from None
        except ArrError as exc:
            raise ApiException.of(ErrorCode.CONFIG_INVALID, exc.message) from None
        if self.state.workfolder != workfolder:  # the switch sees a run only from here on
            raise not_allowed(SWITCHED)
        run_dir = reserve_run(launch, starter=Starter.GUI)
        handle = RunHandle(run_id=run_dir.name, run_dir=run_dir, launch=launch)
        handle.task = asyncio.create_task(self._execute(handle), name=f"arr-run-{run_dir.name}")
        self.state.registry.add(handle)
        self.state.store.get(run_dir.name)
        return StartRunResponse(
            run_id=run_dir.name, status=RunStatus.QUEUED, gui_url=self.state.run_url(run_dir.name)
        )

    async def _execute(
        self, handle: RunHandle, *, lock: Any = None, starter: Starter = Starter.GUI
    ) -> None:
        run_id = handle.run_id

        def on_manager(manager: RunManager) -> None:
            handle.manager = manager

        def report(line: str) -> None:
            if line:
                log.info("[%s] %s", run_id, line)

        try:
            await run_launch(
                handle.launch,
                decider=self.approvals.decider,
                report=report,
                builders=self.state.builders,
                signals=False,
                scan=self.state.scan,
                secret_reader=self.state.secret_reader,
                run_dir=handle.run_dir,
                starter=starter,
                on_manager=on_manager,
                lock=lock,
                gates=self.state.gates,
                **self.state.manager_kwargs,
            )
        except asyncio.CancelledError:
            raise
        except ArrError as exc:
            log.warning("[%s] Run nicht gestartet: %s", run_id, exc.message)
            self._note_failed_start(handle, exc)
        except Exception as exc:
            log.exception("[%s] Run im Service fehlgeschlagen", run_id)
            self._note_failed_start(handle, exc)

    def _note_failed_start(self, handle: RunHandle, exc: Exception) -> None:
        """Keep the reason for the page when `run_launch` removed the reserved folder.

        A run whose `RunManager` had prepared it is not a failed start (N9, W3-04.4): a running
        run deleted by hand is gone, not „konnte nicht vorbereitet werden“. A run start of
        Claude Code keeps its folder (its approval lines stay): it ends `failed` with the reason.
        """
        manager = handle.manager
        if manager is not None and manager.prepared is not None:
            return
        reason = _start_failure(exc, handle.launch.workfolder)
        if handle.run_dir.exists():
            self._fail_reserved(handle.run_dir, reason)
            return
        handle.start_failure = reason
        hub = self.state.hub
        hub.publish(
            CountsChangedEvent(
                seq=hub.next_seq(), ts=time.time(), run_id=None, counts=counts(self.state)
            )
        )

    @staticmethod
    def _fail_reserved(run_dir: Path, reason: str) -> None:
        """A reserved folder that never got prepared but stays (approval lines): `failed`."""
        try:
            meta = load_run_meta(run_dir)
        except (OSError, ValueError):
            return
        if meta.status not in (RunStatus.QUEUED, RunStatus.AWAITING_APPROVAL):
            return
        with contextlib.suppress(OSError, ValueError):
            set_run_status(run_dir, RunStatus.FAILED)
            OrchestratorLog(run_dir).write(
                "status", status=RunStatus.FAILED.value, previous=meta.status.value, reason=reason
            )

    async def stop_all(self) -> None:
        """Stop every run of the service and wait for the finalizers and scans (Ctrl-C)."""
        handles = self.state.registry.active()
        for handle in handles:
            if handle.manager is not None:
                handle.manager.stop()
            elif handle.task is not None:
                handle.task.cancel()
        tasks = [h.task for h in handles if h.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for follower in list(self._followers):
            follower.cancel()
