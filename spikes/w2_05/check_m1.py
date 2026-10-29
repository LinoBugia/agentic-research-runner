"""Check an M1 run folder against the criteria of docs/05 Phase 1 (W2-05 A7, throwaway code).

    uv run python spikes/w2_05/check_m1.py <run-ordner>

Checks what can be read off the folder: criteria 1 (mixed agents, two channels, waterfall,
rework), 2 (folder complete, every tool call has its result, every channel an export,
summary.md), 5 (secret scan clean), 6 (questions with `to` answered: waiting agent via the next
inbox, working agent via a hint mid-turn, attachment read by another agent) and 7 (Python script
without approval, write into the original project refused with a reason, `pip install` asks).
Criterion 3 (Ctrl-C) is checked by the second run, criterion 4 by the FakeBackend tests.
Prints a table with ✅/❌ and the figures for the report (requests, cost, duration).
Exit code 1 if any check fails. Never imported by arr.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arr.models import ResolvedRun
from arr.resolve import load_yaml_model
from arr.room.store import RoomStore
from arr.room.types import Message, MessageKind
from arr.runs.eventlog import read_events
from arr.runs.orchestrator import OrchestratorLog
from arr.runs.prepare import load_run_meta
from arr.runs.secret_scan import scan_run_dir

REQUIRED_FILES = (
    "run.yaml",
    "run.input.yaml",
    "task.md",
    "meta.json",
    "orchestrator.jsonl",
    "approvals.jsonl",
    "usage.json",
    "output/summary.md",
    "output/results.json",
    "output/ratings.json",
)
REQUIRED_DIRS = ("base", "skills", "agents", "groupchats/attachments", "output/results")
AGENT_FILES = (
    "agent.json",
    "role.md",
    "transcript.jsonl",
    "raw.jsonl",
    "transcript.md",
    "changes.diff",
    "session.json",
)
ROOM_READS = ("mcp__arr-room__read_messages", "mcp__arr-room__inbox")
PYTHON_RE = re.compile(r"^\s*python3?\s+\S+\.py\b")
SHELL_WRAP_RE = re.compile(r"^/bin/\w+\s+-lc\s+['\"](.*)['\"]\s*$", re.DOTALL)


def unwrap(command: str) -> str:
    """`/bin/zsh -lc '<cmd>'` of Codex → `<cmd>`; other commands unchanged."""
    match = SHELL_WRAP_RE.match(command.strip())
    return match.group(1) if match else command


OUTSIDE_RE = re.compile(r"projects/collatz-mini|\.\./\.\./\.\./\.\./")


@dataclass
class Check:
    number: str
    label: str
    ok: bool
    detail: str = ""


class M1Folder:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.meta = load_run_meta(run_dir)
        self.resolved = load_yaml_model(run_dir / "run.yaml", ResolvedRun)
        self.entries = OrchestratorLog(run_dir).entries()
        self.transcripts = {
            a.id: read_events(run_dir / "agents" / a.id / "transcript.jsonl")
            for a in self.resolved.agents
        }
        self.approvals = self._approvals()
        self.messages: list[Message] = []
        if (run_dir / "groupchats" / "room.sqlite").is_file():
            self.messages = RoomStore(run_dir).messages_after(0)

    def _approvals(self) -> list[dict[str, Any]]:
        path = self.run_dir / "approvals.jsonl"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # -- helpers --------------------------------------------------------------------------

    def turns(self, agent: str) -> list[tuple[int, float, float]]:
        """(turn, start ts, end ts) per turn of an agent."""
        starts: dict[int, float] = {}
        turns = []
        for event in self.transcripts.get(agent, []):
            if event["kind"] == "turn_start":
                starts[event["turn"]] = event["ts"]
            elif event["kind"] == "turn_end":
                turns.append((event["turn"], starts.get(event["turn"], event["ts"]), event["ts"]))
        return turns

    def turn_at(self, agent: str, ts: float) -> int | None:
        for number, start, end in self.turns(agent):
            if start <= ts <= end:
                return number
        return None

    def events_of_turn(self, agent: str, turn: int) -> list[dict[str, Any]]:
        return [e for e in self.transcripts.get(agent, []) if e["turn"] == turn]

    def answered(self, question: Message) -> Message | None:
        [agent] = question.to[:1] or [None]
        for msg in self.messages:
            if msg.sender != agent or msg.id <= question.id or msg.channel != question.channel:
                continue
            if msg.reply_to == question.id or msg.kind is MessageKind.ANSWER:
                return msg
        later = [
            m
            for m in self.messages
            if m.sender == agent and m.id > question.id and m.channel == question.channel
        ]
        return later[0] if later else None

    def questions(self) -> list[Message]:
        return [m for m in self.messages if m.kind is MessageKind.QUESTION and m.to]

    # -- criteria -------------------------------------------------------------------------

    def criterion_1(self) -> list[Check]:
        providers = {a.provider.value for a in self.resolved.agents}
        chats = self.resolved.groupchats
        members = [frozenset(c.members) for c in chats]
        decisions = [
            e["data"].get("decision")
            for e in self.entries
            if e["kind"] == "checkpoint" and "decision" in e["data"]
        ]
        return [
            Check(
                "1a",
                "Claude- und Codex-Agenten",
                providers == {"claude", "codex"},
                str(sorted(providers)),
            ),
            Check(
                "1b",
                "zwei Kanäle mit verschiedenen Mitgliedern, einer im Modus event",
                len(chats) >= 2
                and len(set(members)) >= 2
                and any(c.mode.value == "event" for c in chats),
                ", ".join(f"#{c.name} ({c.mode.value}: {', '.join(c.members)})" for c in chats),
            ),
            Check("1c", "Topologie waterfall", self.resolved.topology.kind.value == "waterfall"),
            Check(
                "1d",
                "mindestens eine Nacharbeit",
                "rework" in decisions,
                f"Entscheidungen: {decisions}",
            ),
            Check(
                "1e",
                "Run regulär beendet (done)",
                self.meta.status.value == "done",
                self.meta.status.value,
            ),
        ]

    def criterion_2(self) -> list[Check]:
        missing = [f for f in REQUIRED_FILES if not (self.run_dir / f).is_file()]
        missing += [d for d in REQUIRED_DIRS if not (self.run_dir / d).is_dir()]
        for agent in self.resolved.agents:
            folder = self.run_dir / "agents" / agent.id
            missing += [f"agents/{agent.id}/{f}" for f in AGENT_FILES if not (folder / f).is_file()]
        unanswered = {}
        for agent_id, events in self.transcripts.items():
            calls = {e["data"]["id"] for e in events if e["kind"] == "tool_call"}
            results = {e["data"]["id"] for e in events if e["kind"] == "tool_result"}
            if calls - results:
                unanswered[agent_id] = sorted(calls - results)
        exports = [
            name
            for c in self.resolved.groupchats
            for name in (f"{c.name}.md", f"{c.name}.jsonl")
            if not (self.run_dir / "groupchats" / name).is_file()
            or not (self.run_dir / "groupchats" / name).read_text(encoding="utf-8").strip()
        ]
        calls_total = sum(
            1 for ev in self.transcripts.values() for e in ev if e["kind"] == "tool_call"
        )
        return [
            Check(
                "2a",
                "Run-Ordner vollständig (docs/02 §4)",
                not missing,
                f"fehlt: {missing}" if missing else "alle Dateien da",
            ),
            Check(
                "2b",
                "jeder Tool-Call hat sein Ergebnis",
                not unanswered and calls_total > 0,
                f"{calls_total} Tool-Calls" + (f", offen: {unanswered}" if unanswered else ""),
            ),
            Check(
                "2c",
                "jeder Kanal hat einen lesbaren Export",
                not exports,
                f"fehlt: {exports}" if exports else "md + jsonl je Kanal",
            ),
            Check("2d", "summary.md vorhanden", (self.run_dir / "output/summary.md").is_file()),
        ]

    def criterion_5(self) -> list[Check]:
        patterns = scan_run_dir(self.run_dir)
        scans = [e["data"] for e in self.entries if e["kind"] == "secret_scan"]
        last = scans[-1] if scans else None
        return [
            Check(
                "5a",
                "Muster-Scan über den Run-Ordner sauber",
                patterns.clean,
                f"{patterns.files} Dateien"
                + (
                    ""
                    if patterns.clean
                    else f", Funde: {[(f.file, f.line, f.kind) for f in patterns.findings]}"
                ),
            ),
            Check(
                "5b",
                "Secret-Scan von arr run (Muster und Profil-Werte) sauber",
                bool(last and last.get("clean")),
                f"{last.get('files')} Dateien, {last.get('secrets')} Profil-Werte"
                if last
                else "kein Eintrag secret_scan",
            ),
        ]

    def criterion_6(self) -> list[Check]:
        waiting_ok, waiting_detail = False, "keine Frage an einen wartenden Agenten"
        hint_ok, hint_detail = False, "kein Hinweis zu einer Frage mit to"
        followed_ok, followed_detail = False, "–"
        hints = [e for e in self.entries if e["kind"] == "hint"]
        by_id = {m.id: m for m in self.messages}
        for q in self.questions():
            [agent] = q.to[:1]
            turn = self.turn_at(agent, q.ts)
            answer = self.answered(q)
            if turn is None and not waiting_ok:
                waiting_detail = f"#{q.id} von {q.sender} an {agent} (wartend)" + (
                    f" → Antwort #{answer.id} in Turn {self.turn_at(agent, answer.ts)}"
                    if answer
                    else " → keine Antwort"
                )
                waiting_ok = answer is not None
        for hint in hints:
            agent = hint["data"]["agent"]
            ids = hint["data"].get("messages") or []
            asked = [
                by_id[i]
                for i in ids
                if i in by_id and by_id[i].kind is MessageKind.QUESTION and agent in by_id[i].to
            ]
            if not asked:
                continue
            q = asked[0]
            turn = self.turn_at(agent, hint["ts"])
            hint_ok = True
            hint_detail = f"Frage #{q.id} von {q.sender} an {agent} als Hinweis in Turn {turn}"
            if turn is None:
                continue
            events = self.events_of_turn(agent, turn)
            hint_seq = next((e["seq"] for e in events if e["kind"] == "hint" and q.id in ids), None)
            read_after = [
                e
                for e in events
                if e["kind"] == "tool_call"
                and e["data"]["name"] in ROOM_READS
                and hint_seq is not None
                and e["seq"] > hint_seq
            ]
            answer = self.answered(q)
            same_turn = answer is not None and self.turn_at(agent, answer.ts) == turn
            if read_after and answer is not None:
                followed_ok = True
                tool = read_after[0]["data"]["name"]
                when = "im selben Turn" if same_turn else "später"
                followed_detail = (
                    f"{agent} liest nach dem Hinweis ({tool}), antwortet #{answer.id} {when}"
                )
                break
            read = "gelesen" if read_after else "nicht gelesen"
            answered = "beantwortet" if answer else "keine Antwort"
            followed_detail = f"{agent}: {read}, {answered}"
        attachment_ok, attachment_detail = False, "kein get_attachment eines anderen Agenten"
        senders = {m.id: m.sender for m in self.messages if m.refs}
        for agent_id, events in self.transcripts.items():
            results = {e["data"]["id"]: e for e in events if e["kind"] == "tool_result"}
            for e in events:
                if e["kind"] != "tool_call" or e["data"]["name"] != "mcp__arr-room__get_attachment":
                    continue
                msg_id = (e["data"].get("input") or {}).get("msg_id")
                result = results.get(e["data"]["id"])
                if result is None or result["data"].get("is_error"):
                    continue
                sender = senders.get(int(msg_id)) if str(msg_id).isdigit() else None
                if sender and sender != agent_id:
                    attachment_ok = True
                    attachment_detail = f"{agent_id} liest Anhang von #{msg_id} ({sender})"
        return [
            Check(
                "6a",
                "Frage mit to an wartenden Agenten: beantwortet im nächsten Turn",
                waiting_ok,
                waiting_detail,
            ),
            Check(
                "6b",
                "Frage mit to an arbeitenden Agenten: Hinweis mitten im Turn zugestellt",
                hint_ok,
                hint_detail,
            ),
            Check(
                "6c",
                "Hinweis befolgt: Agent liest die Frage und antwortet",
                followed_ok,
                followed_detail,
            ),
            Check("6d", "Anhang für den anderen Agenten lesbar", attachment_ok, attachment_detail),
        ]

    def criterion_7(self) -> list[Check]:
        opened = [e for e in self.approvals if e.get("event") == "open"]
        open_commands = [str((e.get("request") or {}).get("command") or "") for e in opened]
        script_ok, script_detail = False, "kein Python-Aufruf gefunden"
        for agent_id, events in self.transcripts.items():
            results = {e["data"]["id"]: e for e in events if e["kind"] == "tool_result"}
            for e in events:
                raw = (
                    (e["data"].get("input") or {}).get("command")
                    if e["kind"] == "tool_call"
                    else ""
                )
                command = unwrap(str(raw or ""))
                if not PYTHON_RE.search(command) or "pip" in command:
                    continue
                result = results.get(e["data"]["id"])
                if result is None or result["data"].get("is_error"):
                    continue
                if any(command.strip() and command.strip() in c for c in open_commands):
                    continue
                script_ok = True
                script_detail = f"{agent_id}: {command.strip()[:60]} → ok, keine Anfrage"
        decisions = [e for e in self.approvals if (e.get("decision") or {}).get("action") == "deny"]
        write_denied = [
            e
            for e in decisions
            if "write_outside" in ((e.get("request") or {}).get("categories") or [])
            or "außerhalb" in str((e.get("decision") or {}).get("reason") or "")
        ]
        write_ok = bool(write_denied)
        write_detail = "keine abgelehnte Schreibanfrage"
        if write_denied:
            request = write_denied[0].get("request") or {}
            reason = str((write_denied[0].get("decision") or {}).get("reason"))[:90]
            write_detail = f"{request.get('agent_id')} · {request.get('tool')}: {reason}"
        else:
            for agent_id, events in self.transcripts.items():
                results = {e["data"]["id"]: e for e in events if e["kind"] == "tool_result"}
                for e in events:
                    if e["kind"] != "tool_call":
                        continue
                    text = json.dumps(e["data"].get("input") or {}, ensure_ascii=False)
                    result = results.get(e["data"]["id"])
                    if OUTSIDE_RE.search(text) and result and result["data"].get("is_error"):
                        content = str(result["data"].get("content") or "")
                        if (
                            "nicht erlaubt" in content
                            or "not permitted" in content
                            or "denied" in content.lower()
                        ):
                            write_ok, write_detail = (
                                True,
                                f"{agent_id}: Sandbox/Regel: {content[:90]}",
                            )
        pip = [
            e
            for e in opened
            if "pip install" in str((e.get("request") or {}).get("command") or "")
            or "install" in ((e.get("request") or {}).get("categories") or [])
            or "network" in ((e.get("request") or {}).get("categories") or [])
        ]
        pip_detail = "keine Anfrage"
        if pip:
            request = pip[0].get("request") or {}
            what = request.get("command") or request.get("hosts")
            who = f"{request.get('agent_id')} · {request.get('tool')}"
            pip_detail = f"{who} · {what} → {pip[0].get('item_id')}"
        return [
            Check(
                "7a", "Python-Skript im Arbeitsordner läuft ohne Freigabe", script_ok, script_detail
            ),
            Check(
                "7b",
                "Schreiben ins Original-Projekt mit Begründung abgelehnt",
                write_ok,
                write_detail,
            ),
            Check("7c", "pip install erzeugt eine Nachfrage", bool(pip), pip_detail),
        ]

    # -- figures --------------------------------------------------------------------------

    def figures(self) -> list[str]:
        lines = []
        for agent in self.resolved.agents:
            events = self.transcripts.get(agent.id, [])
            ends = [e for e in events if e["kind"] == "turn_end"]
            requests = sum(int(e["data"].get("num_turns") or 0) for e in ends)
            cost = sum(float(e["data"].get("cost_usd") or 0) for e in ends)
            statuses = [e["data"].get("status") for e in ends]
            who = f"{agent.id} ({agent.provider.value}, {agent.model})"
            lines.append(
                f"  {who}: {len(ends)} Turns {statuses}, {requests} Anfragen, {cost:.4f} $"
            )
        usage_file = self.run_dir / "usage.json"
        if usage_file.is_file():
            total = json.loads(usage_file.read_text(encoding="utf-8")).get("total", {})
            estimated = total.get("cost_estimated_usd")
            lines.append(
                f"  usage.json: {total.get('turns')} Turns, rechnerisch {estimated} $, "
                f"Tokens {total.get('usage', {})}"
            )
        if self.meta.started and self.meta.ended:
            seconds = self.meta.ended - self.meta.started
            lines.append(f"  Dauer: {seconds:.0f} s, Status {self.meta.status.value}")
        return lines


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    run_dir = Path(argv[0]).expanduser()
    folder = M1Folder(run_dir)
    checks = folder.criterion_1() + folder.criterion_2() + folder.criterion_5()
    checks += folder.criterion_6() + folder.criterion_7()
    print(f"M1-Prüfung von {run_dir.name}")
    print("| Nr. | Kriterium | Ergebnis | Nachweis |")
    print("|---|---|---|---|")
    for check in checks:
        mark = "✅" if check.ok else "❌"
        print(f"| {check.number} | {check.label} | {mark} | {check.detail} |")
    print("Kennzahlen:")
    print("\n".join(folder.figures()))
    failed = [c.number for c in checks if not c.ok]
    print(
        "Ergebnis: " + ("alle Prüfungen bestanden" if not failed else f"nicht bestanden: {failed}")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
