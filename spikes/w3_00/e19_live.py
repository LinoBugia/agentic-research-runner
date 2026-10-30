"""E19 live with variant 2: the lab matrix in one real turn (W3-00 §3.7, §12).

The matrix of `lab_v2.py` runs as `probes.py` in the agent's working folder: the model only
runs `python3 probes.py` once and repeats the output, so a turn costs few model requests.
`probes.py` first waits for `.late-ready`; the harness creates the "new during the turn"
files (below `~`, in `runs/`, new run venvs with other, shorter and longer IDs, a new base
venv) only after the turn has started, then writes the marker. Fail-safe:

- every approval request is declined and only recorded,
- outside the allowlist there are only canaries with random content,
- real sensitive paths and the login folder are probed by exit code only.

Afterwards the raw stream (and for Codex the rollout) is searched for the canary tokens: only
the agent's own attachment may appear.

    uv run python spikes/w3_00/e19_live.py codex    # gpt-6-luna, effort low (Profil chatgpt-w2-02a)
    uv run python spikes/w3_00/e19_live.py claude   # claude-haiku-5-5, max_budget_usd 0.30
    uv run python spikes/w3_00/e19_live.py dry      # probes.py in `codex sandbox`, no model
"""

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from common import (
    CLAUDE_AUTH,
    CODEX_EFFORT,
    CODEX_MODEL,
    OUT,
    Lab,
    build_lab,
    check_budget,
    codex_auth,
    dump,
    record,
    remove_cleanup,
    shown,
)
from lab_v2 import cases, create_late, late_files

from arr.backends.codex_config import build_launch, codex_home_for
from arr.models import AuthKind, AuthProfile, Limits
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import PolicyEngine
from arr.permissions.types import (
    ApprovalItem,
    DecidedBy,
    Decision,
    DecisionAction,
    PermissionRequest,
)

PROBES_PY = """import json, subprocess, time
from pathlib import Path

for _ in range(240):
    if Path(".late-ready").exists():
        break
    time.sleep(0.5)
for tag, cmd in json.loads(Path("probes.json").read_text()):
    try:
        p = subprocess.run(["/bin/zsh", "-c", cmd], capture_output=True, text=True, timeout=120)
        code, out = p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        code, out = 124, "timeout"
    print(f"{tag} exit={code} " + out.replace(chr(10), " / ")[-120:])
"""

PROMPT = (
    "Das ist ein Test der Sandbox. Führe im Terminal genau einmal den Befehl "
    "`python3 probes.py` aus (er wartet kurz und braucht etwa eine Minute). Ändere nichts, "
    "führe keine anderen Befehle aus, bitte um keine Freigaben. Gib danach die Ausgabe "
    "unverändert als Codeblock wieder."
)

# The rows that also run for Claude (venv-related cases of §12 plus the basics).
CLAUDE_ROWS = {
    "Skript im Arbeitsordner",
    "import sympy (Basis-venv)",
    "pip list",
    "realpath .",
    "Schreiben in die Run-venv",
    "Lese-Ordner",
    "output/",
    "Kanarie unter ~",
    "ls ~",
    "Namen im Workfolder",
    "paralleler Run",
    "Run-venv des parallelen Runs",
    "Run-venv des parallelen Runs (ls)",
    "fremde Basis-venv",
    "Namen in <wurzel>/runs (bekannter Rest)",
    "Schreiben in die Basis-venv",
    "Schreiben in die Basis-venv (Python)",
    "~/.ssh (Exit-Code)",
}


def matrix(lab: Lab, provider: str) -> tuple[list[tuple[str, str, str]], dict[str, Path]]:
    """(tag, expected, label) rows; writes `probes.json` and `probes.py` to the work dir."""
    late = late_files(lab, create=False)
    login = codex_home_for(codex_auth())
    rows = cases(lab, late, login)
    if provider == "claude":
        rows = [r for r in rows if r[1] in CLAUDE_ROWS or r[1].startswith("neu während")]
        # Claude denies the venv root as a whole, names included (§12.2)
        rows = [
            ("❌", label, cmd) if "bekannter Rest" in label else (e, label, cmd)
            for e, label, cmd in rows
        ]
    tagged = [(f"P{i:02d}", expect, label, cmd) for i, (expect, label, cmd) in enumerate(rows)]
    work = lab.agent.work_dir
    (work / "probes.json").write_text(json.dumps([(t, cmd) for t, _, _, cmd in tagged]))
    (work / "probes.py").write_text(PROBES_PY)
    return [(t, e, label) for t, e, label, _ in tagged], late


def read_text(path: str | Path) -> str:
    """Blocking read in a sync helper (the turn is over when the reports are built)."""
    return Path(path).read_text(encoding="utf-8")


def start_late(lab: Lab, late: dict[str, Path]) -> None:
    create_late(late)
    (lab.agent.work_dir / ".late-ready").write_text("1")


def parse(text: str) -> dict[str, dict[str, Any]]:
    found = {}
    for match in re.finditer(r"(P\d\d) exit=(\d+) ?([^\n]*)", text):
        found.setdefault(match.group(1), {"exit": int(match.group(2)), "out": match.group(3)})
    return found


def evaluate(provider: str, lab: Lab, late: dict[str, Path], rows, results, texts) -> dict:
    tokens = {n: p.read_text().strip() for n, p in {**lab.canaries, **late}.items() if p.exists()}
    leaks = [
        n for n, tok in tokens.items() if n != "own_attachment" and any(tok in t for t in texts)
    ]
    table, bad = [], []
    for tag, expect, label in rows:
        r = results.get(tag)
        ok = None if r is None else r["exit"] == 0
        table.append({"tag": tag, "expect": expect, "case": label, "ok": ok, **(r or {})})
        if r is None or (expect == "✅") != ok and expect != "⚠️":
            bad.append(label)
        mark = "–" if r is None else ("läuft  " if ok else "gesperrt")
        print(f"  {expect} {mark} {label:<46} {shown((r or {}).get('out', ''))[-70:]!r}")
    print(f"{provider}: Abweichungen {len(bad)} {bad}  Kanarien im Strom: {leaks}")
    return {"rows": table, "deviations": bad, "leaks": leaks}


# --- Codex -------------------------------------------------------------------------------


class Recorder:
    def __init__(self, path: Path) -> None:
        self.fh = path.open("w", encoding="utf-8")
        self.requests: list[dict[str, Any]] = []
        self.usage_updates = 0

    def write(self, direction: str, message: dict[str, Any]) -> None:
        if direction == "in" and message.get("method") == "thread/tokenUsage/updated":
            self.usage_updates += 1
        self.fh.write(json.dumps({"dir": direction, "msg": message}, ensure_ascii=False) + "\n")
        self.fh.flush()

    def handler(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        self.requests.append({"method": method, "command": (params or {}).get("command")})
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            return {"decision": "decline"}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        return {"action": "decline"} if "elicitation" in method else {}


async def run_codex() -> dict[str, Any]:
    from openai_codex import AsyncCodex, CodexConfig

    from arr.backends.codex_sdk import (
        install_approval_handler,
        install_message_hooks,
        start_thread,
    )

    check_budget("codex", 4)
    lab = build_lab("codex")
    try:
        auth = codex_auth()
        launch = build_launch(
            lab.agent,
            lab.profile,
            lab.run_paths,
            codex_home=codex_home_for(auth),
            run_channels=["main", "secret-channel"],
        )
        rows, late = matrix(lab, "codex")
        raw_path = OUT / "e19_codex.raw.jsonl"
        OUT.mkdir(parents=True, exist_ok=True)
        rec = Recorder(raw_path)
        codex = AsyncCodex(CodexConfig(env=launch.env, config_overrides=launch.config_overrides))
        install_approval_handler(codex, rec.handler)
        install_message_hooks(
            codex, on_in=lambda m: rec.write("in", m), on_out=lambda m: rec.write("out", m)
        )
        final, outputs, end = "", [], None
        async with codex:
            if (await codex.account()).account is None:
                sys.exit(f"Profil {auth.name} ist nicht angemeldet: arr auth codex-login")
            thread, started = await start_thread(
                codex,
                approvals=True,
                cwd=launch.cwd,
                model=CODEX_MODEL,
                developer_instructions=launch.developer_instructions,
                config=launch.thread_config,
            )
            handle = await thread.turn(PROMPT, effort=CODEX_EFFORT, summary="detailed")
            start_late(lab, late)
            async for note in handle.stream():
                payload = note.payload.model_dump(mode="json", by_alias=True)
                item = payload.get("item") or {}
                if note.method == "item/completed" and item.get("type") == "agentMessage":
                    final = item.get("text", "")
                if note.method == "item/completed" and item.get("type") == "commandExecution":
                    outputs.append(item.get("aggregatedOutput") or "")
                if note.method == "turn/completed":
                    end = payload["turn"]
            rollout = started.thread.path
        rec.fh.close()
        requests = rec.usage_updates
        record("codex", "e19", requests, {"status": (end or {}).get("status")})
        raw = raw_path.read_text(encoding="utf-8")
        incoming = [json.loads(line) for line in raw.splitlines()]
        incoming_text = "\n".join(json.dumps(m["msg"]) for m in incoming if m["dir"] == "in")
        rollout_text = read_text(rollout) if rollout else ""
        room = [
            m["msg"]["params"]
            for m in incoming
            if m["msg"].get("method") == "mcpServer/startupStatus/updated"
        ]
        report = evaluate(
            "codex", lab, late, rows, parse("\n".join(outputs)), [incoming_text, rollout_text]
        )
        report |= {
            "requests": requests,
            "approval_requests": rec.requests,
            "mcp_startup": room,
            "final": final,
            "status": (end or {}).get("status"),
        }
        dump("e19_codex_report.json", report)
        return report
    finally:
        remove_cleanup(lab)


# --- Claude ------------------------------------------------------------------------------


class DenyAll:
    def __init__(self) -> None:
        self.items: list[ApprovalItem] = []

    async def decide(self, item: ApprovalItem) -> Decision:
        self.items.append(item)
        return Decision(
            action=DecisionAction.DENY, decided_by=DecidedBy.USER, reason="Abgelehnt (Test)."
        )


async def run_claude() -> dict[str, Any]:
    from arr.backends.base import EventKind
    from arr.backends.claude import ClaudeBackend
    from arr.runs.eventlog import EventLogger

    check_budget("claude", 6)
    lab = build_lab("claude")
    try:
        rows, late = matrix(lab, "claude")
        engine = PolicyEngine([lab.profile])
        probe = PermissionRequest(
            run_id=lab.run.run_id,
            agent_id=lab.agent.id,
            provider="claude",
            tool="Bash",
            command="python3 probes.py",
        )
        assert engine.evaluate(probe).action is DecisionAction.ALLOW, "probes.py would ask"
        decider = DenyAll()
        backend = ClaudeBackend(
            lab.agent,
            run_id=lab.run.run_id,
            inbox=ApprovalInbox(engine, decider=decider, timeout=60),
            auth=AuthProfile(name=CLAUDE_AUTH, kind=AuthKind.CLAUDE_OAUTH),
            limits=Limits(budget_usd=0.30),
        )
        logger = EventLogger(lab.agent.agent_dir)
        events: list[Any] = []
        started = False
        try:
            async for event in backend.run_turn(PROMPT):
                logger.log(event)
                events.append(event)
                if not started:
                    start_late(lab, late)
                    started = True
        finally:
            logger.close()
            await backend.close()
        requests = sum(1 for e in events if e.kind is EventKind.USAGE)
        end = next(e for e in reversed(events) if e.kind is EventKind.TURN_END)
        record("claude", "e19", requests, {"cost_usd": end.data.get("cost_usd")})
        outputs = [
            json.dumps(e.data.get("content"), ensure_ascii=False)
            for e in events
            if e.kind is EventKind.TOOL_RESULT
        ]
        texts = [(lab.agent.agent_dir / "transcript.jsonl").read_text(encoding="utf-8")]
        raw = lab.agent.agent_dir / "raw.jsonl"
        texts += [raw.read_text(encoding="utf-8")] if raw.exists() else []
        text = "\n".join(outputs).replace("\\n", "\n")
        report = evaluate("claude", lab, late, rows, parse(text), texts)
        calls = [e.data for e in events if e.kind is EventKind.TOOL_CALL]
        report |= {
            "requests": requests,
            "cost_usd": end.data.get("cost_usd"),
            "status": end.data.get("status"),
            "tool_calls": [{"name": c.get("name"), "input": c.get("input")} for c in calls],
            "inbox": [(i.request.tool, i.request.command) for i in decider.items],
        }
        dump("e19_claude_report.json", report)
        return report
    finally:
        remove_cleanup(lab)


def dry_run() -> dict[str, Any]:
    """`probes.py` in `codex sandbox` without a model: checks script and matrix first."""
    import os

    from lab_v2 import SBX, sandbox, write_config

    lab = build_lab("codex")
    try:
        launch = build_launch(
            lab.agent,
            lab.profile,
            lab.run_paths,
            codex_home=codex_home_for(codex_auth()),
            run_channels=["main", "secret-channel"],
        )
        rows, late = matrix(lab, "codex")
        write_config(SBX, launch.filesystem)
        start_late(lab, late)
        user = os.environ.get("USER", "")
        env = {**launch.shell_env, "CODEX_HOME": str(SBX), "USER": user, "LOGNAME": user}
        proc = sandbox(launch.cwd, env, ["python3", "probes.py"])
        text = proc.stdout + proc.stderr
        return evaluate("dry", lab, late, rows, parse(text), [text])
    finally:
        remove_cleanup(lab)


def main() -> None:
    provider = sys.argv[1] if len(sys.argv) > 1 else "codex"
    runs = {"codex": run_codex, "claude": run_claude, "dry": dry_run}
    report = asyncio.run(runs[provider]()) if provider != "dry" else dry_run()
    summary = {k: v for k, v in report.items() if k != "rows"}
    print(shown(json.dumps(summary, indent=1, ensure_ascii=False, default=str))[-3000:])


if __name__ == "__main__":
    main()
