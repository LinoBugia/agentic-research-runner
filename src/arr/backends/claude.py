"""Claude backend adapter based on `claude-agent-sdk` (docs/01-architektur.md §4.1, S1 §3).

One `ClaudeSDKClient` (one CLI process) per agent. The options come from
`claude_options.build_options` (sandbox with the read allowlist, E19). Permissions:

- `PreToolUse` hook → `PolicyEngine.hard_deny` for every tool call: hard bans are refused
  with their German reason, which the model sees as `PreToolUse:<Tool> hook error: …`.
- `can_use_tool` → `ApprovalInbox.handle`: the grey zone (network per host, reading outside
  the allowlist with the file tools, writing outside) goes to the PolicyEngine and, on `ask`,
  into the inbox; the agent waits, the others keep working.
- `PostToolUse`/`PostToolUseFailure` hooks: only `additionalContext` for chat hints
  (`deliver_hint`).

Project context (E10): `context.md` of the agent goes in front of the first prompt and again
after every compaction, because `setting_sources=[]` loads no `CLAUDE.md` (S1 §3.3).

The SDK client must be connected and disconnected in the same task (anyio task group), so
a small owner task holds it from `start()` to `close()`; turns run in the caller's task.
"""

import asyncio
import contextlib
import logging
import os
import warnings
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
)
from claude_agent_sdk.types import (
    CanUseToolShadowedWarning,
    HookContext,
    PermissionResult,
    ToolPermissionContext,
)

from arr.auth import SecretReader, claude_env, clean_process_env, is_billed
from arr.backends.base import AgentEvent, CostKind, EventKind
from arr.backends.claude_events import ClaudeEvents
from arr.backends.claude_options import build_options, remove_tmp_link, tmp_link
from arr.models import AuthProfile, Limits, Provider
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import NETWORK_TOOLS, READ_TOOLS, SHELL_TOOLS, WRITE_TOOLS
from arr.permissions.types import DecisionAction, PermissionRequest
from arr.runs.prepare import PreparedAgent

log = logging.getLogger(__name__)

HINT_METHOD = "post_tool_use"
CONTEXT_HEADING = "## Projektkontext"
TASK_HEADING = "## Auftrag"
_NO_DECISION = "Keine Freigabe erhalten, arbeite ohne weiter oder frage im Chat nach."


class ClaudeClient(Protocol):
    """The part of `ClaudeSDKClient` the backend uses (a fake in tests)."""

    async def connect(self, prompt: Any = None) -> None: ...
    async def disconnect(self) -> None: ...
    async def query(self, prompt: str) -> None: ...
    def receive_response(self) -> AsyncIterator[Any]: ...
    async def interrupt(self) -> None: ...


ClientFactory = Callable[[ClaudeAgentOptions], ClaudeClient]


def tool_request(
    tool: str,
    tool_input: dict[str, Any],
    *,
    run_id: str,
    agent_id: str,
    role: str | None,
    work_dir: str,
    tool_call_id: str | None = None,
) -> PermissionRequest:
    """Normalize a tool call of Claude into a `PermissionRequest` (docs/07 §7).

    Bash → `command`; file tools → `paths` (Glob: base joined with the pattern, Grep: the
    search root); `SandboxNetworkAccess` → `hosts`; `WebFetch` → `urls`. The agent's own
    justification is the `description` of a Bash call.
    """
    command = None
    paths: list[str] = []
    hosts: list[str] = []
    urls: list[str] = []
    if tool in SHELL_TOOLS:
        command = str(tool_input.get("command") or "")
    elif tool == "Glob":
        pattern = str(tool_input.get("pattern") or "")
        base = tool_input.get("path")
        paths = [os.path.join(str(base), pattern) if base else pattern]
    elif tool == "Grep":
        paths = [str(tool_input.get("path") or ".")]
    elif tool in READ_TOOLS or tool in WRITE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        paths = [str(path)] if path else []
    elif tool in NETWORK_TOOLS:
        host = tool_input.get("host")
        hosts = [str(host)] if host else []
    elif tool == "WebFetch" and tool_input.get("url"):
        urls = [str(tool_input["url"])]
    description = tool_input.get("description")
    return PermissionRequest(
        run_id=run_id,
        agent_id=agent_id,
        role=role,
        provider=Provider.CLAUDE,
        tool=tool,
        tool_call_id=tool_call_id,
        command=command,
        paths=paths,
        hosts=hosts,
        urls=urls,
        work_dir=work_dir,
        reason=str(description) if description else None,
    )


def with_context(prompt: str, context: str) -> str:
    """Prompt with the project context block in front (E10)."""
    if not context.strip():
        return prompt
    return f"{CONTEXT_HEADING}\n\n{context.strip()}\n\n{TASK_HEADING}\n\n{prompt}"


class ClaudeBackend:
    """`AgentBackend` for one Claude research agent."""

    def __init__(
        self,
        agent: PreparedAgent,
        *,
        run_id: str,
        inbox: ApprovalInbox,
        auth: AuthProfile,
        limits: Limits | None = None,
        secret_reader: SecretReader | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.agent = agent
        self.config = agent.file.config
        self.run_id = run_id
        self.inbox = inbox
        self.auth = auth
        self.limits = limits
        self._secret_reader = secret_reader
        self._client_factory: ClientFactory = client_factory or (
            lambda options: ClaudeSDKClient(options=options)
        )
        cost_kind = CostKind.BILLED if is_billed(auth) else CostKind.ESTIMATED
        self._events = ClaudeEvents(run_id, agent.id, self.config.model, cost_kind)
        self._client: ClaudeClient | None = None
        self._owner: asyncio.Task[None] | None = None
        self._closing = asyncio.Event()
        self._tmp_link: Path | None = None
        self._turn = 0
        self._running = False
        self._interrupted = False
        self._send_context = True
        self._hints: list[tuple[str, asyncio.Future[bool]]] = []
        self._side_events: list[AgentEvent] = []
        self._closed = False

    # ----------------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------------

    async def start(self) -> None:
        """Clean the process environment, build the options and connect the CLI."""
        if self._client is not None:
            return
        removed = clean_process_env()
        if removed:
            log.info("Umgebung bereinigt: %s", ", ".join(removed))
        profile = self.inbox.engine.profile(self.agent.id)
        env = claude_env(self.auth, self._secret_reader)
        self._tmp_link = tmp_link(profile.tmp_dir, f"{self.run_id}/{self.agent.id}")
        options = build_options(
            self.agent,
            profile,
            env=env,
            can_use_tool=self._can_use_tool,
            hooks={
                "PreToolUse": [HookMatcher(matcher=None, hooks=[self._pre_tool_use])],
                "PostToolUse": [HookMatcher(matcher=None, hooks=[self._post_tool_use])],
                "PostToolUseFailure": [HookMatcher(matcher=None, hooks=[self._post_tool_use])],
            },
            max_budget_usd=self.limits.budget_usd if self.limits else None,
            tmpdir=self._tmp_link,
            stderr=self._on_stderr,
        )
        self._client = self._client_factory(options)
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._owner = asyncio.create_task(self._own(self._client, ready))
        try:
            await ready
        except BaseException:
            self._client = None
            await self._release_link()
            raise
        log.info("Claude-Agent %s gestartet (Profil %s)", self.agent.id, self.auth.name)

    async def _own(self, client: ClaudeClient, ready: asyncio.Future[None]) -> None:
        """Connect, wait for `close()`, disconnect – all in this one task."""
        try:
            with warnings.catch_warnings():
                # `mcp__arr-room__*` is allowed on purpose; room tools need no approval.
                warnings.simplefilter("ignore", CanUseToolShadowedWarning)
                await client.connect()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            return
        ready.set_result(None)
        try:
            await self._closing.wait()
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()

    async def close(self) -> None:
        """Disconnect the CLI and remove the temp alias; safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        self._fail_hints()
        self._closing.set()
        if self._owner is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._owner
        self._client = None
        await self._release_link()

    async def _release_link(self) -> None:
        if self._tmp_link is not None:
            remove_tmp_link(self._tmp_link)
            self._tmp_link = None

    def session_ref(self) -> dict[str, str]:
        """`{"session_id": …}` of the CLI session, empty before the first turn."""
        session = self._events.session_id
        return {"session_id": session} if session else {}

    # ----------------------------------------------------------------------------------------
    # Turns
    # ----------------------------------------------------------------------------------------

    async def run_turn(self, prompt: str, *, stage: str | None = None) -> AsyncIterator[AgentEvent]:
        """Run one turn; always ends with `turn_end` (also after `interrupt()` and errors)."""
        if self._client is None:
            await self.start()
        client = self._client
        assert client is not None
        self._turn += 1
        self._events.start_turn(self._turn)
        self._interrupted = False
        sent = prompt
        if self._send_context or self._events.compacted:
            sent = with_context(prompt, self._context())
            self._send_context = self._events.compacted = False
        yield self._events.event(EventKind.TURN_START, {"prompt": sent, "stage": stage})
        error: str | None = None
        self._running = True
        try:
            await client.query(sent)
            async for msg in client.receive_response():
                for event in self._drain() + self._events.feed(msg):
                    yield event
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Turn %d von %s abgebrochen: %s", self._turn, self.agent.id, exc)
            error = f"Die Claude-CLI hat den Turn abgebrochen ({type(exc).__name__})."
        finally:
            self._running = False
            self._fail_hints()
        for event in self._drain() + self._events.finish(
            interrupted=self._interrupted, error=error
        ):
            yield event

    def _context(self) -> str:
        path = self.agent.run_dir / self.agent.file.context_file
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            log.warning(
                "context.md von %s fehlt, der Turn läuft ohne Projektkontext", self.agent.id
            )
            return ""

    async def interrupt(self) -> None:
        """Stop the running turn; `run_turn` then ends with status `interrupted`."""
        if not self._running or self._client is None:
            return
        self._interrupted = True
        with contextlib.suppress(Exception):
            await self._client.interrupt()

    async def deliver_hint(self, text: str) -> bool:
        """Attach `text` to the next tool result of the running turn (`additionalContext`).

        Waits until the hint is attached (True) or the turn ends first (False, the hint stays
        in the inbox). arr logs the hint as a `hint` event, it is not in the SDK stream.
        """
        if not self._running:
            return False
        waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._hints.append((text, waiter))
        return await waiter

    def _fail_hints(self) -> None:
        hints, self._hints = self._hints, []
        for _, waiter in hints:
            if not waiter.done():
                waiter.set_result(False)

    def _drain(self) -> list[AgentEvent]:
        events, self._side_events = self._side_events, []
        return events

    # ----------------------------------------------------------------------------------------
    # Permission callbacks and hooks
    # ----------------------------------------------------------------------------------------

    def _request(self, tool: str, tool_input: dict[str, Any], call_id: str | None):
        return tool_request(
            tool,
            tool_input,
            run_id=self.run_id,
            agent_id=self.agent.id,
            role=self.config.role,
            work_dir=str(self.agent.work_dir),
            tool_call_id=call_id,
        )

    async def _can_use_tool(
        self, tool: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResult:
        """Grey zone → `ApprovalInbox.handle` (PolicyEngine, then the inbox on `ask`)."""
        request = self._request(tool, tool_input, context.tool_use_id)
        try:
            decision = await self.inbox.handle(request)
        except Exception:
            log.exception("Freigabe für %s von %s fehlgeschlagen", tool, self.agent.id)
            return PermissionResultDeny(message=_NO_DECISION)
        if decision.action is DecisionAction.ALLOW:
            return PermissionResultAllow()
        return PermissionResultDeny(message=decision.reason or _NO_DECISION)

    async def _pre_tool_use(
        self, hook_input: Any, tool_use_id: str | None, context: HookContext
    ) -> dict[str, Any]:
        """Hard bans for every tool call, with the German reason for the model (S1 §2.8)."""
        tool = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input") or {}
        try:
            decision = self.inbox.engine.hard_deny(self._request(tool, tool_input, tool_use_id))
        except Exception:
            log.exception("Prüfung von %s für %s fehlgeschlagen", tool, self.agent.id)
            return _pre_deny(
                "Die Prüfung dieses Aufrufs ist fehlgeschlagen. Arbeite anders weiter."
            )
        if decision is None:
            return {}
        return _pre_deny(decision.reason)

    async def _post_tool_use(
        self, hook_input: Any, tool_use_id: str | None, context: HookContext
    ) -> dict[str, Any]:
        """Attach pending chat hints to this tool result (docs/06 §4).

        Also registered for `PostToolUseFailure`, so a failed call (exit code ≠ 0, a refusal)
        carries the hint too; `PostToolUse` fires only for successful calls (W2-01 live test).
        """
        if not self._hints:
            return {}
        hints, self._hints = self._hints, []
        text = "\n".join(t for t, _ in hints)
        for hint, waiter in hints:
            self._side_events.append(
                self._events.event(
                    EventKind.HINT, {"text": hint, "channel": None, "method": HINT_METHOD}
                )
            )
            if not waiter.done():
                waiter.set_result(True)
        event_name = hook_input.get("hook_event_name") or "PostToolUse"
        output = {"hookEventName": event_name, "additionalContext": text}
        return {"hookSpecificOutput": output}

    def _on_stderr(self, line: str) -> None:
        log.debug("claude-cli %s: %s", self.agent.id, line.rstrip())


def _pre_deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
