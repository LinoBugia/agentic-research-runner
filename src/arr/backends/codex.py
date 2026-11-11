"""ChatGPT backend adapter based on `openai-codex` (ChatGPT subscription).

One `AsyncCodex` (= one app-server process) per agent, with `CODEX_HOME` = the login folder of
its auth profile (docs/spikes/S2_codex.md §15). The process is started anew for every turn and
the thread continued with `thread/resume`, so `auth.json` and the permission profile are read
fresh per turn (docs/07 §3 ①, `codex_config`). The backend writes no transcript; it
yields `AgentEvent`s with the raw notification attached.

- Settings: `codex_config.build_launch` (environment, isolation, permission profile, room
  server, role text), approvals through `codex_approvals.ApprovalBridge` and the SDK layer
  `codex_sdk` (reviewer `user`; without the internals `never` = sandbox only).
- Events: `codex_events.TurnEvents`; after the turn the rollout fills in the commands the
  stream missed. `rate_limit` comes from the raw message hook (it is not routed to a turn).
- Project context: Codex reads `AGENTS.md` in its `cwd` itself; `context.md` goes into the
  first prompt only in workspace mode `shared` (docstring of `AgentFile`).
- Project settings: Codex starts MCP servers and hooks of `<cwd>/.codex` outside the sandbox.
  `start` keeps the state of that folder; a thread starts only while it is unchanged (W3-06.4).
- Hints: `deliver_hint` steers the running turn and emits a `hint` event; a rejected request's
  reason reaches the agent the same way. `interrupt` ends a waiting approval first.

See docs/01-architektur.md §4.2.
"""

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

from arr.backends.base import (
    AgentEvent,
    ErrorData,
    EventKind,
    HintData,
    TurnEndData,
    TurnStartData,
    TurnStatus,
    UsageData,
)
from arr.backends.codex_approvals import ApprovalBridge
from arr.backends.codex_config import (
    CodexLaunch,
    build_launch,
    codex_home_for,
    command_home,
    project_config_state,
    server_home,
)
from arr.backends.codex_events import TurnEvents, read_rollout, rollout_calls
from arr.backends.codex_sdk import (
    RpcError,
    install_approval_handler,
    install_message_hooks,
    internals_available,
    resume_thread,
    start_thread,
)
from arr.errors import ArrError
from arr.models import AuthProfile, ResolvedRun, WorkspaceMode
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import RunPaths
from arr.resolve import load_yaml_model
from arr.runs.prepare import RUN_SOURCE, PreparedAgent

__all__ = ["CodexBackend", "login_hint"]

logger = logging.getLogger(__name__)

_AUTH_FAILURE_RE = re.compile(
    r"(?i)unauthori[sz]ed|\b401\b|not logged in|logged out|login required|re-?login|"
    r"refresh token|token (?:is |has )?(?:expired|invalid)|invalid (?:api key|token)|"
    r"authentication (?:failed|error|required)|not authenticated"
)


def login_hint(auth: AuthProfile, message: str) -> str:
    """Put „Profil neu anmelden“ in front of an error that looks like a failed login.

    A Codex turn that fails at the login (expired or revoked ChatGPT login, HTTP 401) then
    shows in the terminal and in `summary.md` how to log the profile in again (review
    W2-02 Q13, W2-05). Other messages are returned unchanged.
    """
    if not _AUTH_FAILURE_RE.search(message):
        return message
    return (
        f"Profil „{auth.name}“ neu anmelden: uv run arr auth codex-login {auth.name} "
        f"(Codex meldet: {message})"
    )


CodexFactory = Callable[[Any], Any]
"""Builds an `AsyncCodex` from a `CodexConfig`; tests pass a fake."""

_DENIED = "Abgelehnt: {reason}"
CLOSED = "Das Codex-Backend ist geschlossen. Es startet keinen Turn mehr."
_END = object()


def _default_factory(config: Any) -> Any:
    from openai_codex import AsyncCodex

    return AsyncCodex(config)


def _codex_config(launch: CodexLaunch) -> Any:
    from openai_codex import CodexConfig

    return CodexConfig(env=launch.env, config_overrides=launch.config_overrides)


def _run_channels(run_dir: Path) -> list[str]:
    """All channels of the run from `run.yaml` (their attachments are denied unless own)."""
    path = run_dir / RUN_SOURCE
    if not path.is_file():
        return []
    return [chat.name for chat in load_yaml_model(path, ResolvedRun).groupchats]


def _payload(note: Any) -> dict[str, Any]:
    payload = getattr(note, "payload", None)
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json", by_alias=True)
    return dict(payload or {})


class CodexBackend:
    """`AgentBackend` for one Codex agent (see module docstring)."""

    def __init__(
        self,
        agent: PreparedAgent,
        *,
        run_id: str,
        inbox: ApprovalInbox,
        auth: AuthProfile,
        factory: CodexFactory | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.agent = agent
        self.config = agent.file.config
        self.run_id = run_id
        self.inbox = inbox
        self.engine = inbox.engine
        self.profile = self.engine.profile(agent.id)
        self.auth = auth
        self.codex_home = codex_home_for(auth)
        self.run_paths = RunPaths(self.profile.workfolder, agent.run_dir)
        self._factory = factory or _default_factory
        self._environ = environ
        self._started = False
        self._closed = False
        self._turn = 0
        self._thread_id: str | None = None
        self._rollout: str | None = None
        self._codex: Any = None
        self._bridge: ApprovalBridge | None = None
        self._handle: Any = None
        self._queue: asyncio.Queue[Any] | None = None
        self._interrupted = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._run_channels: list[str] = []
        self._project_state: str | None = None

    # -- lifecycle --------------------------------------------------------------------------

    async def start(self) -> None:
        """Check the login folder and prepare the agent's folders (the process starts per turn)."""
        if self._closed:
            raise RuntimeError("Das Codex-Backend ist geschlossen.")
        if not (self.codex_home / "auth.json").is_file():
            raise ArrError(
                f"Das Profil „{self.auth.name}“ ist nicht bei ChatGPT angemeldet. Melde es einmal "
                f"an: uv run arr auth codex-login {self.auth.name}"
            )
        for home in (command_home(self.agent, self.profile), server_home(self.agent, self.profile)):
            home.mkdir(parents=True, exist_ok=True)
        self._run_channels = _run_channels(self.agent.run_dir)
        self._project_state = project_config_state(self.profile.work_dir)
        self._started = True

    async def close(self) -> None:
        """Stop a running turn and end the app-server; safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        await self.interrupt()
        await self._shutdown()

    async def reset(self) -> None:
        """A fresh session with the next turn (W4-03 F5): the next turn starts a new thread
        instead of resuming this one; in `shared` mode its first prompt carries `context.md`
        again (`AGENTS.md` Codex reads itself). A no-op after `close()`; never during a turn."""
        if self._closed:
            return
        if self._queue is not None:
            raise RuntimeError("Reset während eines Turns")
        self._thread_id = None
        self._rollout = None
        logger.info("Codex-Agent %s: neuer Thread ab dem nächsten Turn", self.agent.id)

    def session_ref(self) -> dict[str, str]:
        """Thread ID and rollout path for `session.json` (resume, S2 §9)."""
        ref = {"thread_id": self._thread_id} if self._thread_id else {}
        if self._rollout:
            ref["rollout"] = self._rollout
        return ref

    async def interrupt(self) -> None:
        """Stop the running turn: end a waiting approval first, then `turn/interrupt`."""
        if self._queue is None:
            return
        self._interrupted = True
        if self._bridge is not None:
            self._bridge.cancel_waiting()
        if self._handle is not None:
            try:
                await self._handle.interrupt()
            except Exception:  # noqa: BLE001 - the turn may have ended in the meantime
                logger.info("Codex-Turn von %s ließ sich nicht unterbrechen", self.agent.id)

    async def deliver_hint(self, text: str) -> bool:
        """Steer a short chat hint into the running turn; False without an active turn."""
        if self._handle is None or self._queue is None or self._closed:
            return False
        try:
            await self._handle.steer(text)
        except Exception:  # noqa: BLE001 - e.g. „no active turn to steer“
            return False
        hint = HintData(text=text, channel=None, method="steer")
        self._queue.put_nowait(self._event(EventKind.HINT, dict(hint)))
        return True

    # -- turn -------------------------------------------------------------------------------

    def run_turn(self, prompt: str, *, stage: str | None = None) -> AsyncIterator[AgentEvent]:
        """Run one turn in a fresh app-server; ends with `turn_end` also after errors.

        After `close()` no app-server starts: the turn ends at once with `error` and
        `turn_end`, also for a turn created before `close()` and iterated after it.
        """
        if self._closed:
            self._turn += 1
            return self._closed_turn(prompt, stage)
        if not self._started:
            raise RuntimeError("Das Codex-Backend ist nicht gestartet.")
        if self._queue is not None:
            raise RuntimeError("Es läuft schon ein Turn.")
        self._queue = asyncio.Queue()
        self._turn += 1
        self._interrupted = False
        return self._run(prompt, stage)

    async def _closed_turn(self, prompt: str, stage: str | None) -> AsyncIterator[AgentEvent]:
        events = TurnEvents(model=self.config.model, status=TurnStatus.ERROR)
        yield self._event(EventKind.TURN_START, dict(TurnStartData(prompt=prompt, stage=stage)))
        data = ErrorData(message=CLOSED, type="closed", recoverable=False)
        yield self._event(EventKind.ERROR, dict(data))
        yield self._turn_end(events)

    async def _run(self, prompt: str, stage: str | None) -> AsyncIterator[AgentEvent]:
        events = TurnEvents(model=self.config.model)
        try:
            yield self._event(EventKind.TURN_START, dict(TurnStartData(prompt=prompt, stage=stage)))
            if self._closed:  # created before close(), iterated after it
                events.status = TurnStatus.ERROR
                yield self._event(
                    EventKind.ERROR,
                    dict(ErrorData(message=CLOSED, type="closed", recoverable=False)),
                )
                yield self._turn_end(events)
                return
            try:
                async for event in self._play(self._first_prompt(prompt), events):
                    yield event
            except Exception as exc:  # noqa: BLE001 - every turn ends with turn_end
                if self._interrupted:  # stopped while the app-server started
                    events.status = TurnStatus.INTERRUPTED
                else:
                    logger.warning("Codex-Turn von %s fehlgeschlagen: %s", self.agent.id, exc)
                    events.status = TurnStatus.ERROR
                    data = ErrorData(
                        message=login_hint(self.auth, str(exc) or type(exc).__name__),
                        type=type(exc).__name__,
                        recoverable=False,
                    )
                    yield self._event(EventKind.ERROR, dict(data))
            await self._shutdown()
            yield self._turn_end(events)
        finally:
            await self._shutdown()
            self._queue = None

    def _first_prompt(self, prompt: str) -> str:
        # the first turn of a thread: the run's first one or the first after `reset` (F5)
        if self._thread_id is not None or self.config.workspace.mode is not WorkspaceMode.SHARED:
            return prompt
        context = self.agent.run_dir / self.agent.file.context_file
        try:
            text = context.read_text(encoding="utf-8").strip()
        except OSError:
            return prompt
        return f"{text}\n\n{prompt}" if text else prompt

    async def _play(self, prompt: str, events: TurnEvents) -> AsyncIterator[AgentEvent]:
        queue = self._queue
        assert queue is not None
        thread = await self._open(queue)
        if self._interrupted or self._closed:  # stopped while the app-server started
            events.status = TurnStatus.INTERRUPTED
            return
        effort = self.config.effort.value if self.config.effort else None
        self._handle = await thread.turn(prompt, effort=effort, summary="detailed")
        if self._interrupted:
            await self.interrupt()
        pump = asyncio.create_task(self._pump(self._handle, queue))
        try:
            while (entry := await queue.get()) is not _END:
                if isinstance(entry, AgentEvent):
                    yield entry
                    continue
                method, params = entry
                for kind, data in events.feed(method, params):
                    if kind is EventKind.ERROR:
                        data["message"] = login_hint(self.auth, str(data.get("message", "")))
                    yield self._event(kind, data, raw={"method": method, "params": params})
            await pump
        finally:
            self._handle = None
            if not pump.done():
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pump
        if events.status is None:
            events.status = TurnStatus.INTERRUPTED if self._interrupted else TurnStatus.ERROR
            if events.status is TurnStatus.ERROR:
                events.error = "Der Turn endete ohne Abschluss von Codex."
        for event in self._rollout_events(events):
            yield event
        if events.status is TurnStatus.ERROR:
            data = ErrorData(
                message=login_hint(self.auth, events.error or "Codex-Fehler"),
                type="turn_failed",
                recoverable=False,
            )
            yield self._event(EventKind.ERROR, dict(data))

    async def _open(self, queue: asyncio.Queue[Any]) -> Any:
        """Start this turn's app-server and start or resume the thread."""
        self._check_project_config()
        launch = build_launch(
            self.agent,
            self.profile,
            self.run_paths,
            codex_home=self.codex_home,
            environ=self._environ,
            run_channels=self._run_channels,
        )
        codex = self._factory(_codex_config(launch))
        self._codex = codex
        approvals = internals_available(codex)
        loop = asyncio.get_running_loop()
        if approvals:
            bridge = ApprovalBridge(
                self.inbox,
                run_id=self.run_id,
                agent_id=self.agent.id,
                role=self.config.role,
                work_dir=launch.cwd,
                loop=loop,
                on_denied=lambda reason: loop.call_soon_threadsafe(self._steer_reason, reason),
                on_refused=lambda method, params, answer: loop.call_soon_threadsafe(
                    queue.put_nowait, self._refused_event(method, params, answer)
                ),
            )
            self._bridge = bridge
            install_approval_handler(codex, bridge)

            def on_in(message: dict[str, Any]) -> None:
                bridge.observe(message)
                if message.get("method") == "account/rateLimits/updated":
                    entry = ("account/rateLimits/updated", message.get("params") or {})
                    loop.call_soon_threadsafe(queue.put_nowait, entry)

            install_message_hooks(codex, on_in=on_in)
        else:
            logger.warning("Codex-Freigaben für %s nur über die Sandbox (deny_all)", self.agent.id)
        settings = {
            "cwd": launch.cwd,
            "model": self.config.model,
            "developer_instructions": launch.developer_instructions,
            "config": launch.thread_config,
        }
        if self._thread_id is None:
            thread, started = await start_thread(codex, approvals=approvals, **settings)
        else:
            thread, started = await resume_thread(
                codex, self._thread_id, approvals=approvals, **settings
            )
        self._thread_id = started.thread.id
        self._rollout = getattr(started.thread, "path", None) or self._rollout
        return thread

    def _check_project_config(self) -> None:
        """Fail closed: no thread once `<cwd>/.codex` differs from its state at the start."""
        if project_config_state(self.profile.work_dir) == self._project_state:
            return
        folder = self.profile.work_dir / ".codex"
        logger.warning("Projekt-Konfiguration %s seit dem Start verändert", folder)
        raise ArrError(
            f"Der Codex-Agent {self.agent.id} arbeitet nicht weiter: Die Projekt-Konfiguration "
            f"{folder} hat sich seit dem Start des Runs verändert. Aus ihr würde Codex Programme "
            "außerhalb der Sandbox starten (MCP-Server, Hooks). Prüfe den Ordner und starte "
            "den Run neu."
        )

    def _refused_event(
        self, method: str, params: dict[str, Any], answer: dict[str, Any]
    ) -> AgentEvent:
        """A server request other than shell/file, refused by the bridge (W2-02 Auftrag §6, G3)."""
        data = ErrorData(
            message=f"Codex-Anfrage „{method}“ abgelehnt (in arr nicht vorgesehen).",
            type="server_request",
            recoverable=True,
        )
        sent = {"error": dict(answer)} if isinstance(answer, RpcError) else answer
        raw = {"request": {"method": method, "params": params}, "answer": sent}
        return self._event(EventKind.ERROR, dict(data), raw=raw)

    async def _pump(self, handle: Any, queue: asyncio.Queue[Any]) -> None:
        try:
            async for note in handle.stream():
                queue.put_nowait((note.method, _payload(note)))
        finally:
            queue.put_nowait(_END)

    def _steer_reason(self, reason: str) -> None:
        handle = self._handle
        if handle is None or self._interrupted:
            return
        task = asyncio.create_task(self._steer(handle, _DENIED.format(reason=reason)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _steer(self, handle: Any, text: str) -> None:
        try:
            await handle.steer(text)
        except Exception:  # noqa: BLE001 - the turn may already be over
            logger.info("Begründung für %s nicht zugestellt", self.agent.id)

    def _rollout_events(self, events: TurnEvents) -> list[AgentEvent]:
        """Blocked commands from the rollout that the stream missed (S2 §15)."""
        if not self._rollout or not events.turn_id:
            return []
        calls = rollout_calls(read_rollout(self._rollout), events.turn_id)
        return [
            self._event(kind, data, raw={"rollout": data["id"]})
            for kind, data in events.reconcile(calls)
        ]

    async def _shutdown(self) -> None:
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            bridge.cancel_waiting()
        codex, self._codex = self._codex, None
        if codex is not None:
            try:
                await codex.close()
            except Exception:  # noqa: BLE001
                logger.warning("Codex-Prozess von %s ließ sich nicht sauber beenden", self.agent.id)

    # -- events -----------------------------------------------------------------------------

    def _event(self, kind: EventKind, data: dict[str, Any], raw: Any = None) -> AgentEvent:
        return AgentEvent(
            run_id=self.run_id,
            agent_id=self.agent.id,
            turn=self._turn,
            kind=kind,
            data=data,
            raw=raw,
        )

    def _turn_end(self, events: TurnEvents) -> AgentEvent:
        usage = events.usage()
        data = TurnEndData(
            status=(events.status or TurnStatus.ERROR).value,
            usage=UsageData(**usage.tokens(), model=self.config.model),
            cost_usd=None,  # subscription without a price per token (docs/08 §5.0)
            cost_kind=None,
            session=self._thread_id,
            num_turns=events.requests or None,
        )
        return self._event(EventKind.TURN_END, dict(data))
