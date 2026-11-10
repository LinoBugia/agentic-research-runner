"""Lock probe of W4-03 F4: no judge reads another judge's sealed review (A3).

A review submitted in the sealed phase `pruefung` lies in `groupchats/sealed/` until arr
releases it. The probe runs in the lab of the login guard (`login_guard_kit.py`): arr's real
profiles, the real Claude CLI against a fake model, `codex sandbox` with the agent's real
permission map, a test `HOME`. It tries every way an agent has: the shell of both backends and
Claude's Read tool, the latter even with an approval. Counter-checks show that each way works
and that the probe sees the review as soon as the protection is gone: after the release
(the file in `output/results/`, as without sealing) and with the policy rule switched off.
"""

import asyncio
import json
import secrets
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from arr.permissions import policy
from arr.permissions.policy import AlwaysRule, PermissionRules, profile_for
from arr.permissions.types import DecidedBy, Decision, DecisionAction
from arr.room.store import RoomStore

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt exists on macOS only"),
]

OTHER = "judge-x"


class Allow:
    """A decider that approves every request (the user or a supervising agent says yes)."""

    async def decide(self, item: Any) -> Decision:
        return Decision(action=DecisionAction.ALLOW, decided_by=DecidedBy.USER, reason="Test")


def seal_a_review(lab: Any) -> tuple[RoomStore, Path, str]:
    """Another judge hands in its review as a file in the sealed phase; returns the file."""
    bait = f"GUTACHTEN-{secrets.token_hex(8)}"
    run_dir = lab.run.run_dir
    work = run_dir / "agents" / OTHER / "work"
    work.mkdir(parents=True)
    (work / "review.md").write_text(f"Urteil: major\n{bait}\n")
    transcript = {"kind": "tool_call", "data": {"input": {"body": f"Urteil: major {bait}"}}}
    (work.parent / "transcript.jsonl").write_text(json.dumps(transcript) + "\n")
    store = RoomStore(run_dir)
    store.seal("pruefung", None)
    store.add_assignment(OTHER, 1, "Prüfe.", stage="pruefung")
    result = store.for_agent(OTHER, work).submit_result("review", "Gutachten", path="review.md")
    assert result.path and result.path.startswith("groupchats/sealed/")
    assert not list((run_dir / "output" / "results").iterdir())
    return store, run_dir / result.path, bait


def released_path(store: RoomStore) -> Path:
    [result] = store.release("pruefung")
    assert result.path and result.path.startswith("output/results/")
    return store.run_dir / result.path


PROBE = (
    "import json, os, sys; target = open('target.txt').read().strip(); out = {}\n"
    "try:\n    out['read'] = open(target).read()\nexcept OSError as exc:\n"
    "    out['read'] = 'denied: ' + type(exc).__name__\n"
    "try:\n    out['list'] = os.listdir(os.path.dirname(target))\nexcept OSError as exc:\n"
    "    out['list'] = 'denied: ' + type(exc).__name__\n"
    "open(sys.argv[1], 'w').write(json.dumps(out))\n"
)
"""The probe in the agent's shell. It gets the path from a file, so no command text names it
and only the sandbox decides (not arr's or the CLI's check of the command line)."""


def place_probe(lab: Any, path: Path) -> tuple[str, Path]:
    work = lab.agent.work_dir
    (work / "probe_seal.py").write_text(PROBE)
    (work / "target.txt").write_text(str(path))
    answer = work / f"answer-{secrets.token_hex(4)}.json"
    return f"python3 probe_seal.py {answer.name}", answer


def claude_shell(
    kit: Any, lab: Any, path: Path, allow_in: dict[str, str] | None = None
) -> dict[str, Any]:
    """The probe in the real Claude sandbox (Bash of the real CLI); `allow_in` adds a folder
    to `allowRead` (counter-checks)."""
    from fake_messages_api import FakeMessagesApi

    command, answer = place_probe(lab, path)
    with FakeMessagesApi([kit.bash(command)], credential=lab.token) as api:
        asyncio.run(asyncio.wait_for(kit._claude_turn(lab, api, allow_in=allow_in), 240))
    assert not any(api.errors), [text[-200:] for text in api.results]
    return json.loads(answer.read_text())


def codex_shell(kit: Any, lab: Any, path: Path) -> dict[str, Any]:
    """The probe through `codex sandbox` with the agent's real permission map."""
    command, answer = place_probe(lab, path)
    real, env, cwd = kit.codex_entries(lab)
    done = kit.in_codex_sandbox(lab, real, env, cwd, command)
    assert done.returncode == 0, done.stderr[-300:]
    return json.loads(answer.read_text())


def read_tool(kit: Any, lab: Any, path: Path, decider: Any = None) -> list[str]:
    from fake_messages_api import ToolCall

    api = kit.run_file_tools(lab, [ToolCall("Read", {"file_path": str(path)})], decider)
    return list(api.results)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a3_the_shell_reads_no_sealed_review(provider: str) -> None:
    import login_guard_kit as kit

    shell = claude_shell if provider == "claude" else codex_shell
    with kit.open_lab(provider) as lab:
        store, sealed, bait = seal_a_review(lab)
        seen = shell(kit, lab, sealed)
        assert seen == {"read": "denied: PermissionError", "list": "denied: PermissionError"}
        # counter-check: released (where an unsealed review lies), the same way reads it
        released = released_path(store)
        seen = shell(kit, lab, released)
        assert bait in seen["read"] and released.name in seen["list"]


def test_a3_the_read_tool_reads_no_sealed_review_not_even_with_an_approval() -> None:
    import login_guard_kit as kit

    with kit.open_lab("claude") as lab:
        store, sealed, bait = seal_a_review(lab)
        for decider in (None, Allow()):
            results = read_tool(kit, lab, sealed, decider)
            assert len(results) == 1 and bait not in results[0], results
        database = store.db_path
        assert all("SQLite" not in r for r in read_tool(kit, lab, database, Allow()))
        # counter-check 1: without the policy rule an approval would open the sealed file
        open_rules = replace(
            lab,
            profile=_profile_without_hidden_paths(lab),
        )
        assert bait in read_tool(kit, open_rules, sealed, Allow())[0]
        # counter-check 2: released, the Read tool reads it without asking
        assert bait in read_tool(kit, lab, released_path(store))[0]


def _profile_without_hidden_paths(lab: Any) -> Any:
    saved = policy.RUN_HIDDEN_PATHS
    policy.RUN_HIDDEN_PATHS = ()
    try:
        return profile_for(lab.agent.file.config, lab.run_paths, PermissionRules(), home=lab.home)
    finally:
        policy.RUN_HIDDEN_PATHS = saved


# --------------------------------------------------------------------------------------------
# W4-03.1 H1 (User 10.11.): the other judge's draft and transcript, before the sealing
# --------------------------------------------------------------------------------------------


def foreign_targets(lab: Any) -> dict[str, Path]:
    home = lab.run.run_dir / "agents" / OTHER
    return {"Entwurf": home / "work" / "review.md", "Transkript": home / "transcript.jsonl"}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_h1_the_shell_reads_no_folder_of_another_agent(provider: str) -> None:
    """The draft lies in the other judge's working copy before `submit_result`, the text of
    the review in its transcript. Counter-check: the same way reads a file of the agent's own
    working folder."""
    import login_guard_kit as kit

    shell = claude_shell if provider == "claude" else codex_shell
    with kit.open_lab(provider) as lab:
        _, _, bait = seal_a_review(lab)
        for name, target in foreign_targets(lab).items():
            seen = shell(kit, lab, target)
            assert seen["read"] == "denied: PermissionError", (name, seen["read"][:80])
            assert bait not in json.dumps(seen)
        own = lab.agent.work_dir / "eigen.md"
        own.write_text(f"{bait}\n")
        assert bait in shell(kit, lab, own)["read"]


def test_h1_read_and_grep_read_no_folder_of_another_agent_not_even_with_an_approval() -> None:
    import login_guard_kit as kit
    from fake_messages_api import ToolCall

    with kit.open_lab("claude") as lab:
        _, _, bait = seal_a_review(lab)
        targets = foreign_targets(lab)
        calls = [ToolCall("Read", {"file_path": str(p)}) for p in targets.values()]
        calls.append(
            ToolCall(
                "Grep",
                {
                    "pattern": bait,
                    "path": str(targets["Entwurf"].parent.parent),
                    "output_mode": "content",
                },
            )
        )
        for decider in (None, Allow()):
            for call in calls:
                [answer] = kit.run_file_tools(lab, [call], decider).results
                assert bait not in answer, (call.name, answer[:120])
        # counter-check: without the rule an approval opens the other agent's folder
        opened = replace(lab, profile=replace(lab.profile, private_roots=()))
        [answer] = kit.run_file_tools(opened, [calls[0]], Allow()).results
        assert bait in answer


# --------------------------------------------------------------------------------------------
# W4-03.2 N1: a rule „immer“ for the run folder opens no other agent's folder in the shell
# W4-03.2 N2: Run-tmp per agent, another agent's is foreign (User 10.11.)
# --------------------------------------------------------------------------------------------


def with_rule_for_the_run(lab: Any, *, old_map: bool = False) -> Any:
    """The lab agent with a read rule „immer“ for the whole run folder (as a decision „Immer“
    on a read request for it would make). `old_map`: without the denies of W4-03.2 (the
    counter-check shows the probe sees the difference)."""
    rule = AlwaysRule(action="allow", tools=["Read"], paths=[str(lab.run.run_dir)])
    profile = profile_for(
        lab.agent.file.config, lab.run_paths, PermissionRules(always=[rule]), home=lab.home
    )
    if old_map:
        private = set(profile.private_roots)
        profile = replace(
            profile, read_deny=tuple(d for d in profile.read_deny if d not in private)
        )
    return replace(lab, profile=profile)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_n1_a_rule_for_the_run_opens_no_other_agents_folder_in_the_shell(provider: str) -> None:
    import login_guard_kit as kit

    shell = claude_shell if provider == "claude" else codex_shell
    with kit.open_lab(provider) as lab:
        _, _, bait = seal_a_review(lab)
        ruled = with_rule_for_the_run(lab)
        for name, target in foreign_targets(lab).items():
            seen = shell(kit, ruled, target)
            assert seen["read"] == "denied: PermissionError", (name, seen["read"][:80])
        output = lab.run.run_dir / "output" / "results"
        (output / "frei.md").write_text(f"{bait}\n")
        assert bait in shell(kit, ruled, output / "frei.md")["read"], "the rule still opens output/"
        # counter-check: the sandbox lists before W4-03.2 opened it (review N1; for Claude
        # measured here: the run folder of the rule in `allowRead`)
        if provider == "codex":
            old = with_rule_for_the_run(lab, old_map=True)
            assert bait in codex_shell(kit, old, foreign_targets(lab)["Entwurf"])["read"]
        else:
            run_open = {"path": str(lab.run.run_dir)}
            assert (
                bait in claude_shell(kit, ruled, foreign_targets(lab)["Entwurf"], run_open)["read"]
            )


TMP_PROBE = (
    "import json, os, sys, tempfile\n"
    "fd, name = tempfile.mkstemp(); os.write(fd, b'eigen'); os.close(fd)\n"
    "out = {'tmpdir': os.environ.get('TMPDIR', ''), 'mkstemp': name, 'own': open(name).read()}\n"
    "other = open('other.txt').read().strip()\n"
    "try:\n    out['other'] = open(other).read()\nexcept OSError as exc:\n"
    "    out['other'] = 'denied: ' + type(exc).__name__\n"
    "open(sys.argv[1], 'w').write(json.dumps(out))\n"
)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_n2_another_agents_run_tmp_is_closed_the_own_one_works(provider: str) -> None:
    """judge-x left a draft in its Run-tmp (`$TMPDIR/entwurf.md` of judge-x); the lab agent's
    shell cannot read it. Counter-check: `tempfile` in the own Run-tmp works and is readable,
    and `TMPDIR` is the agent's own `tmp/<id>/` (or its short alias for Claude)."""
    import login_guard_kit as kit

    with kit.open_lab(provider) as lab:
        bait = f"ENTWURF-{secrets.token_hex(6)}"
        draft = lab.run.run_dir / "tmp" / OTHER / "entwurf.md"
        draft.parent.mkdir(parents=True)
        draft.write_text(f"{bait}\n")
        work = lab.agent.work_dir
        (work / "tmp_probe.py").write_text(TMP_PROBE)
        (work / "other.txt").write_text(str(draft))
        answer = work / "tmp_answer.json"
        command = f"python3 tmp_probe.py {answer.name}"
        if provider == "claude":
            from fake_messages_api import FakeMessagesApi

            with FakeMessagesApi([kit.bash(command)], credential=lab.token) as api:
                asyncio.run(asyncio.wait_for(kit._claude_turn(lab, api), 240))
        else:
            real, env, cwd = kit.codex_entries(lab)
            assert kit.in_codex_sandbox(lab, real, env, cwd, command).returncode == 0
        seen = json.loads(answer.read_text())
        assert seen["other"] == "denied: PermissionError", seen["other"][:80]
        assert seen["own"] == "eigen"
        own_tmp = lab.profile.tmp_dir
        assert own_tmp == lab.run.run_dir / "tmp" / lab.agent.id
        # Claude's TMPDIR is a short alias of it below /tmp (sockets, `tmp_link`), gone after
        # the turn: the file must lie in the own Run-tmp
        made = Path(seen["mkstemp"])
        assert [p for p in own_tmp.rglob(made.name) if p.read_text() == "eigen"], made


def test_n2_the_output_of_a_background_command_stays_in_the_own_run_tmp() -> None:
    """From the review's `probe_runtmp_cli.py`: the CLI writes the output of a background
    command below `CLAUDE_CODE_TMPDIR`. It lands in the agent's own `tmp/<id>/`, which no other
    agent reads (`test_w4_03_2_n2_another_agents_run_tmp_is_foreign`); nothing of it lies in
    the shared Run-tmp or another agent's."""
    import login_guard_kit as kit
    from fake_messages_api import FakeMessagesApi, ToolCall

    with kit.open_lab("claude") as lab:
        bait = f"HINTERGRUND-{secrets.token_hex(6)}"
        script = [
            ToolCall(
                "Bash",
                {
                    "command": f"echo 'Gutachten {bait}'; sleep 1",
                    "description": "hinten",
                    "run_in_background": True,
                },
            ),
            kit.bash("sleep 3; echo fertig", "warten"),
        ]
        with FakeMessagesApi(script, credential=lab.token) as api:
            asyncio.run(asyncio.wait_for(kit._claude_turn(lab, api), 240))
        run_tmp = lab.run.run_dir / "tmp"
        holding = [
            p
            for p in run_tmp.rglob("*")
            if p.is_file() and not p.is_symlink() and bait.encode() in p.read_bytes()
        ]
        assert holding, "the CLI wrote the output into Run-tmp"
        own = lab.profile.tmp_dir
        assert all(own in p.parents for p in holding), holding
        profile = profile_for(
            lab.agent.file.config.model_copy(update={"id": OTHER}),
            lab.run_paths,
            PermissionRules(),
            home=lab.home,
        )
        assert all(profile.is_foreign(p) and not profile.can_read(p) for p in holding)
