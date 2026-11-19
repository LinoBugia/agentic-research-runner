"""Helpers of the W4b-05a tests: runs with FakeBackend agents whose topology calls
`fresh_folder`, a snapshot of a folder tree, user flags, an audit of opened files and fake
models that play one script per turn (for the lab with the real CLIs)."""

import contextlib
import hashlib
import json
import os
import shutil
import stat
import sys
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from run_kit import make_manager, make_workfolder, turns

from arr.models import RunStatus
from arr.runs.context import RunContext
from arr.runs.finalize import finalize_run
from arr.runs.manager import RunManager

FIXTURE_SKILLS = Path(__file__).parent / "fixtures" / "workfolder" / "skills"
SKILL = "sympy-check"

Entry = tuple[Any, ...]


def folder_workfolder(root: Path) -> Path:
    """`make_workfolder` plus a `CLAUDE.md`, a subfolder, a `.codex/` and the skill `SKILL`."""
    workfolder = make_workfolder(root)
    project = workfolder / "projects" / "demo"
    (project / "CLAUDE.md").write_text("# Projekt\nRegeln des Projekts.\n", "utf-8")
    (project / "sub").mkdir()
    (project / "sub" / "lemma.tex").write_text("\\begin{lemma}A\\end{lemma}\n", "utf-8")
    (project / ".codex").mkdir()
    (project / ".codex" / "config.toml").write_text("# leer\n", "utf-8")
    shutil.copytree(FIXTURE_SKILLS / SKILL, workfolder / "skills" / SKILL)
    return workfolder


FOUR_AGENTS = [
    {"id": "c-copy", "skills": [SKILL]},
    {
        "id": "x-copy",
        "provider": "codex",
        "model": "gpt-6.1-sol",
        "auth": "fake-codex",
        "skills": [SKILL],
    },
    {"id": "c-empty", "workspace": {"mode": "empty"}, "skills": [SKILL]},
    {
        "id": "x-empty",
        "provider": "codex",
        "model": "gpt-6.1-sol",
        "auth": "fake-codex",
        "workspace": {"mode": "empty"},
        "skills": [SKILL],
    },
]
"""Claude and Codex agents in the modes `copy` and `empty`, each with a skill."""


def manager(
    workfolder: Path,
    topology: Callable[[RunContext], Awaitable[None]],
    agents: Sequence[Mapping[str, Any]] = FOUR_AGENTS,
    *,
    finalize: bool = True,
    scripts: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> RunManager:
    """A RunManager with FakeBackend agents (a few empty turns each) and the real finalizer."""
    scripts = scripts or {a["id"]: turns([], [], [], [], repeat_last=True) for a in agents}
    if finalize:
        kwargs.setdefault("finalizer", finalize_run)
    return make_manager(workfolder, topology, scripts, agents=[dict(a) for a in agents], **kwargs)


async def run_done(mgr: RunManager) -> Path:
    status = await mgr.run()
    assert status is RunStatus.DONE, (status, mgr.error)
    return mgr.run_dir


def tree(root: Path) -> dict[str, Entry]:
    """Every entry below `root` (and `root` itself as `.`) with its type, mode and content
    hash or link target; links are not followed."""
    found: dict[str, Entry] = {}

    def entry(path: Path, rel: str) -> None:
        info = os.lstat(path)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            found[rel] = ("link", os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            found[rel] = ("dir", mode)
            for name in sorted(os.listdir(path)):
                entry(path / name, f"{rel}/{name}" if rel != "." else name)
        elif stat.S_ISREG(info.st_mode):
            found[rel] = ("file", mode, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            found[rel] = ("other", stat.S_IFMT(info.st_mode))

    entry(root, ".")
    return found


def inodes(root: Path) -> set[int]:
    """Inode numbers of every entry below `root` (not `root` itself)."""
    return {os.lstat(Path(top) / n).st_ino for top, ds, fs in os.walk(root) for n in ds + fs}


def clear_flags(root: Path) -> None:
    """Remove user flags and give the owner its rights below `root` (so pytest can delete)."""
    if not os.path.lexists(root):
        return
    if hasattr(os, "chflags"):  # macOS: user flags; Linux has none
        with contextlib.suppress(OSError):
            os.chflags(root, 0, follow_symlinks=False)
    if root.is_symlink() or not root.is_dir():
        return
    os.chmod(root, 0o700)
    for name in os.listdir(root):
        clear_flags(root / name)


class OpenAudit:
    """Records every `open` (and `os.open`) of a path below one of `roots` while `active` (an
    audit hook cannot be removed, so it stays installed and switches itself off)."""

    _hooks: list["OpenAudit"] = []

    def __init__(self, *roots: Path) -> None:
        self.roots = tuple(os.fsencode(os.path.realpath(r)) + b"/" for r in roots)
        self.opened: list[str] = []
        self.active = False
        self._lock = threading.Lock()
        if not OpenAudit._hooks:
            sys.addaudithook(OpenAudit._dispatch)
        OpenAudit._hooks.append(self)

    @staticmethod
    def _dispatch(event: str, args: tuple[Any, ...]) -> None:
        if event != "open" or not args or not isinstance(args[0], str | bytes | os.PathLike):
            return
        for audit in OpenAudit._hooks:
            if audit.active:
                audit._note(args[0])

    def _note(self, path: Any) -> None:
        raw = os.fsencode(path)
        if raw.startswith(self.roots) or os.path.realpath(raw).startswith(self.roots):
            with self._lock:
                self.opened.append(os.fsdecode(raw))

    def __enter__(self) -> "OpenAudit":
        self.active = True
        return self

    def __exit__(self, *_: object) -> None:
        self.active = False


@contextlib.contextmanager
def writer(target: Path, *, seconds: float = 30.0) -> Iterator[Any]:
    """A real process that keeps (re)creating ten files in `target` as fast as it can, never
    stopping; ended by its PID."""
    import subprocess

    program = (
        "import os, sys, time\n"
        "folder, end = sys.argv[1], time.time() + float(sys.argv[2])\n"
        "n = 0\n"
        "while time.time() < end:\n"
        "    n = (n + 1) % 10\n"
        "    try:\n"
        "        with open(os.path.join(folder, f'neu-{n}.txt'), 'w') as f: f.write('x')\n"
        "    except OSError:\n"
        "        pass\n"
    )
    process = subprocess.Popen([sys.executable, "-I", "-c", program, str(target), str(seconds)])
    try:
        yield process
    finally:
        process.kill()
        process.wait(timeout=10)


# --------------------------------------------------------------------------------------------
# Lab (real CLIs): fake models with one script per turn, an orchestrator over real backends
# --------------------------------------------------------------------------------------------


def _user_text(message: Mapping[str, Any]) -> bool:
    """A user message that holds no tool result: the prompt that opens a turn."""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return True
    return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def turned_messages(scripts: Sequence[Sequence[Any]]) -> Any:
    """A `FakeMessagesApi` that plays `scripts[n]` in the n-th turn, across a kept session and
    after a new one. `turn_results[n]` holds the tool results of turn n, `bodies` the text of
    every request (to find a skill's text)."""
    from fake_messages_api import FakeMessagesApi, _result_text, _text_stream, _tool_stream

    class Turned(FakeMessagesApi):
        def __init__(self) -> None:
            super().__init__([])
            self.scripts = [list(s) for s in scripts]
            self.turn = -1
            self.step = 0
            self.turn_results: list[list[str]] = [[] for _ in scripts]
            self.bodies: list[str] = []

        def answer(self, body: Mapping[str, Any]) -> bytes:
            model = str(body.get("model", "fake"))
            if not body.get("tools"):
                return _text_stream("ok", model)
            with self._lock:
                self.bodies.append(json.dumps(body, ensure_ascii=False))
                users = [m for m in body.get("messages") or [] if m.get("role") == "user"]
                last = users[-1] if users else {"role": "user"}
                if _user_text(last):
                    self.turn, self.step = self.turn + 1, 0
                else:
                    blocks = [b for b in last.get("content", []) if b.get("type") == "tool_result"]
                    self.turn_results[self.turn] += [_result_text(b) for b in blocks]
                    self.step += len(blocks)
                script = self.scripts[self.turn] if 0 <= self.turn < len(self.scripts) else []
                call = script[self.step] if self.step < len(script) else None
            return _tool_stream(call, model) if call else _text_stream("fertig", model)

    return Turned()


def turned_responses(scripts: Sequence[Sequence[str]]) -> Any:
    """A `FakeResponsesApi` that plays `scripts[n]` (shell lines) in the n-th turn, across a
    resumed thread and after a new one; `turn_outputs[n]` holds the outputs of turn n."""
    from fake_responses_api import _USAGE, FakeResponsesApi, _item, _sse

    class Turned(FakeResponsesApi):
        def __init__(self) -> None:
            super().__init__([])
            self.scripts = [list(s) for s in scripts]
            self.turn = -1
            self.step = 0
            self.calls = 0
            self.turn_outputs: list[list[str]] = [[] for _ in scripts]

        def answer(self, body: Mapping[str, Any]) -> bytes:
            items = [i for i in body.get("input", []) if isinstance(i, dict)]
            with self._lock:
                self.requests += 1
                last = items[-1] if items else {}
                if last.get("type") in ("function_call_output", "custom_tool_call_output"):
                    self.turn_outputs[self.turn].append(str(last.get("output", "")))
                    self.step += 1
                else:
                    self.turn, self.step = self.turn + 1, 0
                script = self.scripts[self.turn] if 0 <= self.turn < len(self.scripts) else []
                command = script[self.step] if self.step < len(script) else None
                self.calls += 1
                number = self.calls
            response = f"resp_{self.requests}"
            return (
                _sse({"type": "response.created", "response": {"id": response}})
                + _sse(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": _item(number, command),
                    }
                )
                + _sse(
                    {"type": "response.completed", "response": {"id": response, "usage": _USAGE}}
                )
            )

    return Turned()


@contextlib.asynccontextmanager
async def lab_orchestrator(lab: Any, api: Any, decider: Any = None) -> Any:
    """An `Orchestrator` over the real backend of the lab agent (Claude CLI or Codex
    app-server against `api`), real hooks, `PolicyEngine` and `EventLogger`."""
    from dataclasses import replace

    import login_guard_kit as kit

    from arr.backends.claude import ClaudeBackend
    from arr.backends.codex import CodexBackend
    from arr.permissions.inbox import ApprovalInbox
    from arr.permissions.policy import ApprovalLog, PolicyEngine
    from arr.room.store import RoomStore
    from arr.runs.eventlog import EventLogger
    from arr.runs.orchestrator import Orchestrator

    engine = PolicyEngine([lab.profile], log=ApprovalLog(lab.run.run_dir / "approvals.jsonl"))
    inbox = ApprovalInbox(engine, decider=decider, timeout=5 if decider else 0.05)
    if lab.agent.file.config.provider.value == "claude":
        from claude_agent_sdk import ClaudeSDKClient

        def client(options: Any) -> Any:
            env = {**options.env, **kit.fake_model_env(api.url)}
            options = replace(
                options, env=env, max_budget_usd=None, cli_path=str(kit.guard_start(lab))
            )
            return ClaudeSDKClient(options=options)

        backend: Any = ClaudeBackend(
            lab.agent,
            run_id=lab.run.run_id,
            inbox=inbox,
            auth=lab.auth,
            secret_reader=lambda _service, _account: lab.token,
            client_factory=client,
        )
    else:
        from openai_codex import AsyncCodex

        home = lab.base / "codex-home"
        home.mkdir(exist_ok=True)

        def codex(config: Any) -> Any:
            env = {**(config.env or {}), "CODEX_HOME": str(home)}
            overrides = (*config.config_overrides, *api.provider_overrides())
            return AsyncCodex(replace(config, env=env, config_overrides=overrides))

        backend = CodexBackend(
            lab.agent, run_id=lab.run.run_id, inbox=inbox, auth=lab.auth, factory=codex
        )
    logger = EventLogger(lab.agent.agent_dir)
    room = RoomStore(lab.run.run_dir)
    orchestrator = Orchestrator(
        lab.run,
        room=room,
        backends={lab.agent.id: backend},
        loggers={lab.agent.id: logger},
        inbox=inbox,
    )
    await backend.start()
    try:
        yield orchestrator
    finally:
        await backend.close()
        logger.close()
