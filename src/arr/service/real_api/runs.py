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
import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from arr.errors import ArrError, ConfigError, ConfigIssue
from arr.models import (
    ProfilesFile,
    Provider,
    ProviderDefaults,
    ResolvedRun,
    RunInput,
    RunStatus,
    Starter,
    WorkfolderSettings,
)
from arr.resolve import dump_yaml_model, validate_model
from arr.room.types import DeliveryStatus, MessageVia
from arr.room.types import Message as RoomMessage
from arr.runs.launch import Launch, plan_run_input, request_stop, reserve_run, run_launch
from arr.runs.manager import RunManager, set_run_status
from arr.runs.prepare import _check_codex_toolchain, _make_plan
from arr.service.api_client import count_active, count_runs_waiting
from arr.service.api_models import (
    Action,
    ActionResult,
    AgentChanges,
    AgentState,
    ApiException,
    CapacityInfo,
    ChannelInfo,
    ChannelMessageEvent,
    ErrorCode,
    FieldIssue,
    LogEntry,
    Message,
    MessagesPage,
    PostMessageRequest,
    ResolveResult,
    RunAction,
    RunCounts,
    RunDetail,
    RunFilter,
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
from arr.service.real_api.approvals import ApprovalsArea
from arr.service.real_api.base import RunHandle, ServiceState, not_allowed, not_found
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
from arr.skills import discover_skills
from arr.workfolder import load_workfolder_settings, project_dir

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
    agent `model`, `web`, `read_dirs`), `limits` (`wall_time_s`, `turns_per_agent`,
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


class _NoProfile(ArrError):
    """`profiles.yaml` does not exist yet."""


class RunsArea:
    """The run methods of `ApiClient` (see the module docstring)."""

    def __init__(self, state: ServiceState, approvals: ApprovalsArea, storage: StorageArea) -> None:
        self.state = state
        self.approvals = approvals
        self.storage = storage
        self._followers: set[asyncio.Task[None]] = set()

    # -- helpers ----------------------------------------------------------------------------

    def _record(self, run_id: str) -> RunRecord:
        record = self.state.store.get(run_id)
        if record is None:
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
        if owned and handle is not None:
            if action is RunAction.RESUME:
                self._refuse_open_escalation(record)
            message = self._own_action(handle, action, status, dry_run=dry_run)
        else:
            message = self._foreign_action(record, action, dry_run=dry_run)
        record.refresh_meta()
        return ActionResult(ok=True, message=message, run=self._summary(record, with_agents=False))

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

    async def post_message(self, run_id: str, channel: str, request: PostMessageRequest) -> Message:
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
                via=MessageVia.GUI,
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
                launch = plan()
            _check_prepared(launch)
        except ConfigError as exc:
            raise _named(exc, ids) from None
        return launch

    async def resolve_run(self, run_input: dict[str, Any]) -> ResolveResult:
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
        needed: dict[str, int] = {}
        for agent in resolved.agents:
            needed[agent.auth] = needed.get(agent.auth, 0) + 1
        capacity = []
        warnings = []
        for profile, count in needed.items():
            limit = launch.settings.concurrency.get(profile)
            gate = self.state.gates.get(profile)
            in_use = gate.active if gate is not None else 0
            free = max(0, (limit - in_use)) if limit is not None else count
            capacity.append(
                CapacityInfo(
                    profile=profile,
                    free=free,
                    needed=count,
                    limit=limit if limit is not None else count,
                )
            )
            if count > free:
                warnings.append(
                    f"{profile}: {free} von {limit} Plätzen frei, der Run braucht {count}. Seine "
                    "Agenten warten, bis Plätze frei werden."
                )
        if any(a.provider is Provider.CODEX for a in resolved.agents):
            notice = toolchain_notice(launch.settings, self.state.workfolder)
            if notice is not None:
                warnings.append(f"{notice.title} {notice.text}")
        standards = launch.run_input.defaults
        if standards is not None and "task_md" in run_input:
            chosen = [f"das Modell {model_label(standards.model)}"] if standards.model else []
            chosen += [f"das Profil „{standards.auth}“"] if standards.auth else []
            warnings.append(
                f"In diesem Workfolder fehlen Standards, deshalb nimmt arr {' und '.join(chosen)}. "
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

    async def _execute(self, handle: RunHandle) -> None:
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
                starter=Starter.GUI,
                on_manager=on_manager,
                gates=self.state.gates,
                **self.state.manager_kwargs,
            )
        except asyncio.CancelledError:
            raise
        except ArrError as exc:
            log.warning("[%s] Run nicht gestartet: %s", run_id, exc.message)
        except Exception:
            log.exception("[%s] Run im Service fehlgeschlagen", run_id)

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
