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
from arr.permissions.policy import PermissionRules, profile_for
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


def claude_shell(kit: Any, lab: Any, path: Path) -> dict[str, Any]:
    """The probe in the real Claude sandbox (Bash of the real CLI)."""
    from fake_messages_api import FakeMessagesApi

    command, answer = place_probe(lab, path)
    with FakeMessagesApi([kit.bash(command)], credential=lab.token) as api:
        asyncio.run(asyncio.wait_for(kit._claude_turn(lab, api), 240))
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
