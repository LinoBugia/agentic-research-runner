"""Lab probes of W4b-05a with the real Claude CLI and the real Codex app-server (A2, A4–A6, A8).

The lab of the login guard (`login_guard_kit.py`): a test `HOME` below `/private/tmp`, a
`security` stub, fake models on localhost only, no login. `Orchestrator.fresh_folder` runs
between two turns of the real backend (`folders_kit.lab_orchestrator`). Lock probes follow
`test_review_seal_probe.py`: baits with random content, results only by exit code and bait,
each with a counter-check that sees the bait once the protection is gone.
"""

import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from arr.permissions.types import DecidedBy, Decision, DecisionAction

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt exists on macOS only"),
]

OTHER = "judge-x"


class Allow:
    """A decider that approves every request (the user or a supervising agent says yes)."""

    async def decide(self, item: Any) -> Decision:
        return Decision(action=DecisionAction.ALLOW, decided_by=DecidedBy.USER, reason="Test")


def step(provider: str, command: str) -> Any:
    import login_guard_kit as kit

    return kit.bash(command) if provider == "claude" else command


def api_for(provider: str, scripts: list[list[Any]]) -> Any:
    from folders_kit import turned_messages, turned_responses

    return turned_messages(scripts) if provider == "claude" else turned_responses(scripts)


def results(api: Any, turn: int) -> list[str]:
    return api.turn_results[turn] if hasattr(api, "turn_results") else api.turn_outputs[turn]


def session_of(orchestrator: Any, agent_id: str) -> Any:
    ref = orchestrator.backends[agent_id].session_ref()
    return ref.get("session_id") or ref.get("thread_id")


# --------------------------------------------------------------------------------------------
# A2: the folder stays, the next turn works in the fresh one
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("reset", [False, True], ids=["session-kept", "after-reset"])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a2_the_turn_after_a_fresh_folder_works_in_the_same_fresh_folder(
    provider: str, reset: bool
) -> None:
    import login_guard_kit as kit
    from folders_kit import lab_orchestrator

    marker = f"NOTIZ-{secrets.token_hex(8)}"
    with kit.open_lab(provider) as lab:
        work = lab.agent.work_dir
        agent_id = lab.agent.id
        base = sorted(os.listdir(lab.run.run_dir / "base"))
        turn1 = [
            step(provider, f"mkdir -p sub && echo {marker} > notizen.md && echo {marker} > sub/x")
        ]
        turn2 = [
            step(provider, "pwd -P"),
            step(provider, "ls -A"),
            step(provider, "cat notizen.md || echo FEHLT"),
            step(provider, 'echo neu > neu.txt && echo "scratch=$ARR_SCRATCH"'),
        ]
        api = api_for(provider, [turn1, turn2])
        seen: dict[str, Any] = {}
        with api:

            async def go() -> None:
                async with lab_orchestrator(lab, api) as orchestrator:
                    first = await orchestrator.turn(agent_id, "Runde 1")
                    seen["first"] = first.status.value
                    seen["session1"] = session_of(orchestrator, agent_id)
                    if reset:
                        await orchestrator.reset_session(agent_id)
                    await orchestrator.fresh_folder(agent_id, stage="p", attempt=1)
                    second = await orchestrator.turn(agent_id, "Runde 2")
                    seen["second"] = second.status.value
                    seen["session2"] = session_of(orchestrator, agent_id)

            asyncio.run(asyncio.wait_for(go(), 300))
        out = results(api, 1)
        assert seen["first"] == seen["second"] == "ok", seen
        assert len(out) == 4, out
        assert str(Path(os.path.realpath(work))) in out[0]
        listed = out[1]
        assert "notizen.md" not in listed and "sub" not in listed.split()
        assert all(name in listed for name in base if name != "CLAUDE.md")
        assert "FEHLT" in out[2] and marker not in out[2]
        assert f"scratch={lab.run.run_dir / 'scratch' / agent_id}" in out[3]
        assert (work / "neu.txt").read_text() == "neu\n", "turn 2 wrote into the fresh folder"
        old = lab.run.run_dir / "retired" / agent_id / "001-p"
        assert (old / "notizen.md").read_text() == f"{marker}\n"
        assert not (old / "neu.txt").exists()
        assert (seen["session1"] != seen["session2"]) is reset, seen


# --------------------------------------------------------------------------------------------
# A4: retired/ is closed for its former owner, also under a rule for the run
# --------------------------------------------------------------------------------------------


def retired_bait(lab: Any) -> tuple[Path, str]:
    """A filed-away folder of the lab agent with a bait (as `fresh_folder` leaves it)."""
    bait = f"ABGELEGT-{secrets.token_hex(8)}"
    folder = lab.run.run_dir / "retired" / lab.agent.id / "001-p"
    folder.mkdir(parents=True)
    (folder / "notizen.md").write_text(f"{bait}\n")
    return folder / "notizen.md", bait


def without_new_walls(lab: Any, *, rule: bool = True) -> Any:
    """Counter-check: the profile of before W4b-05a (no `retired/` in the private roots, the
    own `agents/<id>/` in `own`), with a read rule for the run like `with_rule_for_the_run`."""
    import test_review_seal_probe as seal

    profile = (seal.with_rule_for_the_run(lab) if rule else lab).profile
    run = lab.run.run_dir
    new = {run / "retired", run / "scratch"}
    return replace(
        lab,
        profile=replace(
            profile,
            private_roots=tuple(p for p in profile.private_roots if p not in new),
            read_deny=tuple(p for p in profile.read_deny if p not in new),
            own=(*profile.own, lab.agent.agent_dir),
        ),
    )


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a4_the_shell_reads_no_filed_away_folder_not_even_its_own(provider: str) -> None:
    import login_guard_kit as kit
    import test_review_seal_probe as seal

    shell = seal.claude_shell if provider == "claude" else seal.codex_shell
    with kit.open_lab(provider) as lab:
        target, bait = retired_bait(lab)
        for probe_lab in (lab, seal.with_rule_for_the_run(lab)):
            seen = shell(kit, probe_lab, target)
            assert seen == {"read": "denied: PermissionError", "list": "denied: PermissionError"}
        own = lab.agent.work_dir / "eigen.md"
        own.write_text(f"{bait}\n")
        assert bait in shell(kit, lab, own)["read"], "the same way reads the working folder"
        # counter-check: without the new walls the rule for the run opens retired/
        if provider == "codex":
            assert bait in seal.codex_shell(kit, without_new_walls(lab), target)["read"]
        else:
            opened = {"path": str(lab.run.run_dir / "retired")}
            assert bait in seal.claude_shell(kit, lab, target, opened)["read"]


def test_a4_a6_read_and_grep_open_neither_retired_nor_the_own_transcript_with_an_approval() -> None:
    import login_guard_kit as kit
    from fake_messages_api import ToolCall

    with kit.open_lab("claude") as lab:
        target, bait = retired_bait(lab)
        agent_dir = lab.agent.agent_dir
        logs = {}
        for name in ("transcript.jsonl", "raw.jsonl", "session.json", "context.md"):
            (agent_dir / name).write_text(f'{{"text": "{bait}"}}\n')
            logs[name] = agent_dir / name
        (agent_dir / "spaeter.txt").write_text(f"{bait}\n")  # appears only now
        calls = [ToolCall("Read", {"file_path": str(p)}) for p in (target, *logs.values())]
        calls.append(ToolCall("Read", {"file_path": str(agent_dir / "spaeter.txt")}))
        for folder in (target.parent, agent_dir):
            calls.append(
                ToolCall("Grep", {"pattern": bait, "path": str(folder), "output_mode": "content"})
            )
        calls.append(ToolCall("Glob", {"pattern": "*", "path": str(agent_dir)}))
        import test_review_seal_probe as seal

        for probe_lab in (lab, seal.with_rule_for_the_run(lab)):
            for decider in (None, Allow()):
                for call in calls:
                    [answer] = kit.run_file_tools(probe_lab, [call], decider).results
                    assert bait not in answer and "transcript.jsonl" not in answer, (
                        call.name,
                        call.input,
                        answer[:160],
                    )
        rows = [json.loads(line) for line in (lab.run.run_dir / "approvals.jsonl").open()]
        assert not [r for r in rows if r.get("event") in ("asked", "open")], "never a question"
        assert {r["decision"]["decided_by"] for r in rows if r.get("decision")} == {"fixed_rule"}
        # counter-check: with the profile of before an approval opens both
        old = without_new_walls(lab, rule=False)
        for path in (target, logs["transcript.jsonl"]):
            call = ToolCall("Read", {"file_path": str(path)})
            [answer] = kit.run_file_tools(old, [call], Allow()).results
            assert bait in answer, path.name


# --------------------------------------------------------------------------------------------
# A6: the plugin stays readable, a Claude skill still starts
# --------------------------------------------------------------------------------------------


def with_skill(lab: Any) -> tuple[Any, str, str]:
    """The lab agent with a generated plugin of one skill whose text holds a marker."""
    from arr.runs.prepare import PreparedAgent
    from arr.skills import write_claude_plugin

    marker = f"SKILLTEXT-{secrets.token_hex(8)}"
    snapshot = lab.run.run_dir / "skills"
    skill = snapshot / "probe-skill"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: probe-skill\ndescription: Eine Probe für W4b-05a.\n---\n\n"
        f"# Probe\nSchreibe {marker}.\n"
    )
    plugin = lab.agent.agent_dir / "plugin"
    ids = write_claude_plugin(plugin, snapshot, ["probe-skill"])
    file = lab.agent.file.model_copy(
        update={"plugin_dir": plugin.relative_to(lab.run.run_dir).as_posix(), "skill_ids": ids}
    )
    return replace(lab, agent=PreparedAgent(file=file, run_dir=lab.run.run_dir)), ids[0], marker


def test_a6_the_plugin_stays_readable_and_a_skill_starts() -> None:
    import login_guard_kit as kit
    from fake_messages_api import ToolCall

    with kit.open_lab("claude") as lab:
        skilled, skill_id, marker = with_skill(lab)
        skill_md = next((lab.agent.agent_dir / "plugin").rglob("SKILL.md"))
        api = kit.run_file_tools(
            skilled,
            [
                ToolCall("Skill", {"skill": skill_id}),
                ToolCall("Read", {"file_path": str(skill_md)}),
            ],
        )
        assert not any(api.errors), api.results
        assert marker in api.results[1], api.results[1][:200]
        assert "probe-skill" in api.results[0], api.results[0][:200]  # the Skill tool launched it
        log = lab.run.run_dir / "approvals.jsonl"
        rows = [json.loads(line) for line in log.open()] if log.exists() else []
        assert not [r for r in rows if (r.get("decision") or {}).get("action") == "deny"], rows


# --------------------------------------------------------------------------------------------
# A5: scratch folder in the shell of both backends
# --------------------------------------------------------------------------------------------

SCRATCH_PROBE = (
    "import json, os, sys\n"
    "own = os.environ.get('ARR_SCRATCH', '')\n"
    "out = {'env': own}\n"
    "open(os.path.join(own, 'eigen.txt'), 'w').write('eigen')\n"
    "out['own'] = open(os.path.join(own, 'eigen.txt')).read()\n"
    "other = open('other.txt').read().strip()\n"
    "try:\n    out['other'] = open(other).read()\nexcept OSError as exc:\n"
    "    out['other'] = 'denied: ' + type(exc).__name__\n"
    "try:\n    open(os.path.join(os.path.dirname(other), 'x'), 'w')\n"
    "    out['write_other'] = 'ok'\n"
    "except OSError as exc:\n    out['write_other'] = 'denied: ' + type(exc).__name__\n"
    "open(sys.argv[1], 'w').write(json.dumps(out))\n"
)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a5_the_own_scratch_is_known_and_writable_another_agents_is_closed(provider: str) -> None:
    import login_guard_kit as kit

    with kit.open_lab(provider) as lab:
        bait = f"FREMD-{secrets.token_hex(6)}"
        other = lab.run.run_dir / "scratch" / OTHER / "daten.txt"
        other.parent.mkdir(parents=True)
        other.write_text(f"{bait}\n")
        work = lab.agent.work_dir
        (work / "scratch_probe.py").write_text(SCRATCH_PROBE)
        (work / "other.txt").write_text(str(other))
        answer = work / "scratch_answer.json"
        command = f"python3 scratch_probe.py {answer.name}"
        if provider == "claude":
            from fake_messages_api import FakeMessagesApi

            with FakeMessagesApi([kit.bash(command)], credential=lab.token) as api:
                asyncio.run(asyncio.wait_for(kit._claude_turn(lab, api), 240))
        else:
            real, env, cwd = kit.codex_entries(lab)
            assert kit.in_codex_sandbox(lab, real, env, cwd, command).returncode == 0
        seen = json.loads(answer.read_text())
        own = lab.run.run_dir / "scratch" / lab.agent.id
        assert seen["env"] == str(own) and seen["own"] == "eigen"
        assert (own / "eigen.txt").read_text() == "eigen"
        assert seen["other"] == "denied: PermissionError", seen["other"][:80]
        assert seen["write_other"] == "denied: PermissionError"
        assert not (other.parent / "x").exists()


# --------------------------------------------------------------------------------------------
# A8: processes that outlive the filing away (measured, Entscheidung 52)
# --------------------------------------------------------------------------------------------

SURVIVOR = r"""
import json, os, sys, time
plan = json.load(open(sys.argv[1]))
tag = sys.argv[2]
if tag == "abgeloest":
    if os.fork():
        os._exit(0)
    os.setsid()
    if os.fork():
        os._exit(0)
os.chdir("sub")
held = open(os.path.join(plan["work"], "alt.txt"))
out = os.path.join(plan["tmp"], "survivor-" + tag)
open(out + ".pid", "w").write(str(os.getpid()))
end = time.time() + 120
while not os.path.exists(plan["trigger"]) and time.time() < end:
    time.sleep(0.05)
res = {}
def attempt(name, fn):
    try:
        res[name] = fn()
    except OSError as exc:
        res[name] = "denied: " + type(exc).__name__
held.seek(0)
attempt("handle", lambda: held.read().strip())
attempt("cwd_file", lambda: open("x.txt").read().strip())
attempt("cwd_list", lambda: sorted(os.listdir(".")))
attempt("parent_file", lambda: open("../alt.txt").read().strip())
attempt("retired_path", lambda: open(plan["retired_file"]).read().strip())
attempt("retired_list", lambda: sorted(os.listdir(plan["retired_dir"])))
attempt("cwd_write", lambda: open("neu-im-alten.txt", "w").write("x"))
attempt("work_list", lambda: sorted(os.listdir(plan["work"])))
open(out + ".json", "w").write(json.dumps(res))
time.sleep(600)
"""
"""Started in turn 1 with its working directory in `sub/` and a handle on `alt.txt`; after
the filing away it tries every way back to the old content, then waits to be ended."""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _end_survivors(tmp: Path) -> None:
    """End the probe's own processes by their PID (never by pattern), checking the command."""
    for pid_file in tmp.glob("survivor-*.pid"):
        pid = int(pid_file.read_text())
        command = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
        ).stdout
        if "survivor.py" in command and _alive(pid):
            os.kill(pid, signal.SIGKILL)


def _measure(provider: str, reset: bool) -> dict[str, Any]:
    import login_guard_kit as kit
    from folders_kit import lab_orchestrator

    with kit.open_lab(provider) as lab:
        work, agent_id = lab.agent.work_dir, lab.agent.id
        tmp = lab.profile.tmp_dir
        secret = f"ALT-{secrets.token_hex(8)}"
        (work / "sub").mkdir()
        (work / "sub" / "x.txt").write_text(f"{secret}\n")
        (work / "alt.txt").write_text(f"{secret}\n")
        (work / "survivor.py").write_text(SURVIVOR)
        retired = lab.run.run_dir / "retired" / agent_id / "001-p"
        plan = {
            "work": str(work),
            "tmp": str(tmp),
            "trigger": str(tmp / "los"),
            "retired_file": str(retired / "alt.txt"),
            "retired_dir": str(retired),
        }
        (work / "plan.json").write_text(json.dumps(plan))
        tags = ["abgeloest", "nohup"]
        turn1 = [
            step(provider, "python3 survivor.py plan.json abgeloest"),
            step(provider, "nohup python3 survivor.py plan.json nohup >/dev/null 2>&1 &"),
        ]
        if provider == "claude":
            from fake_messages_api import ToolCall

            tags.append("run_in_background")
            turn1.append(
                ToolCall(
                    "Bash",
                    {
                        "command": "python3 survivor.py plan.json run_in_background",
                        "description": "hinten",
                        "run_in_background": True,
                    },
                )
            )
        else:
            tags.append("hintergrund")
            turn1.append(step(provider, "python3 survivor.py plan.json hintergrund &"))
        turn1.append(step(provider, "sleep 2; ls " + str(tmp)))
        api = api_for(provider, [turn1])
        out: dict[str, Any] = {"provider": provider, "reset": reset}
        try:
            with api:

                async def go() -> None:
                    async with lab_orchestrator(lab, api) as orchestrator:
                        await orchestrator.turn(agent_id, "Runde 1")
                        out["started"] = sorted(
                            p.name.removeprefix("survivor-").removesuffix(".pid")
                            for p in tmp.glob("survivor-*.pid")
                        )
                        if reset:
                            await orchestrator.reset_session(agent_id)
                        await orchestrator.fresh_folder(agent_id, stage="p", attempt=1)
                        (tmp / "los").write_text("")
                        end = time.monotonic() + 15
                        while time.monotonic() < end:
                            done = [
                                t for t in out["started"] if (tmp / f"survivor-{t}.json").exists()
                            ]
                            if len(done) == len(out["started"]):
                                break
                            await asyncio.sleep(0.2)

                asyncio.run(asyncio.wait_for(go(), 300))
            for tag in tags:
                answer = tmp / f"survivor-{tag}.json"
                if tag not in out["started"]:
                    out[tag] = "nicht gestartet"
                elif not answer.exists():
                    out[tag] = "beendet (keine Antwort)"
                else:
                    seen = json.loads(answer.read_text())
                    for key, value in seen.items():
                        if isinstance(value, str) and secret in value:
                            seen[key] = "ALT gelesen"
                    out[tag] = seen
            out["turn1"] = [text.strip()[-160:] for text in results(api, 0)]
            out["secret_in_fresh"] = any(
                secret in p.read_text(errors="replace")
                for p in work.rglob("*")
                if p.is_file() and not p.is_symlink()
            )
            out["new_in_retired"] = (retired / "sub" / "neu-im-alten.txt").exists()
        finally:
            _end_survivors(tmp)
        return out


@pytest.mark.parametrize("reset", [True, False], ids=["after-reset", "session-kept"])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a8_what_outlives_the_filing_away_and_what_it_still_reaches(
    provider: str, reset: bool
) -> None:
    """Measured, printed for the report. Fixed: no process reaches `retired/` by a new path
    and nothing old is in the fresh folder; what a process holds (handle, working directory)
    stays readable for it (Entscheidung 52)."""
    out = _measure(provider, reset)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    assert out["started"], out
    assert out["secret_in_fresh"] is False
    for tag, seen in out.items():
        if isinstance(seen, dict):
            assert seen["retired_path"] == "denied: PermissionError", (tag, seen)
            assert seen["retired_list"] == "denied: PermissionError", (tag, seen)
            assert "ALT gelesen" not in str(seen.get("work_list")), (tag, seen)
