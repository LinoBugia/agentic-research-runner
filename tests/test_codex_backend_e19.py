"""E19 live result as a test without a model (W2-02 A1): the recording of the read allowlist
test (`tests/fixtures/codex/read_allowlist.jsonl`, spike `spikes/w2_02_codex/e19_live.py v1`,
permission profile from `build_launch`, variant 1) matches the expected table L1-L15.
Recorded on 29.10. after W2-02.1 (shell environment `core`, user temp folders denied).

Every probe is a shell command tagged `: <case>;`. Its `commandExecution` item carries exit
code and output. Canaries hold random tokens; only the one of the own channel may appear."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "codex" / "read_allowlist.jsonl"
DENIED = ("Operation not permitted", "operation not permitted", "PermissionError")

# case → expected output fragment of a command that works (exit code 0)
WORKS = {
    "L7": "OKAPI-7",  # AGENTS.md of the working folder, by cat and Python
    "L8": "OUTPUT-OK",
    "L9": "2+2=4",
    "L10": "sympy 1.14",  # run venv over the base venv
    "L10b": "SymPy",
    "L11": "ssl ok",
    "L11b": "venv ssl ok",
    "L14b": "name: demo-skill",
    "L15b": "CANARY-",
    "T2": "/tmp\nok",
}
# cases that must be blocked by the sandbox
BLOCKED = ["L3", "L5", "L5b", "L6b", "L9w", "L9c", "L13", "L13b", "L13c", "L14", "L15", "F1", "T1"]
BY_EXIT_CODE = ["L1", "T3", "T4"]  # ~/.ssh, the arr process's temp folder, /tmp/claude-<uid>


def _messages() -> list[dict[str, Any]]:
    lines = FIXTURE.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.fixture(scope="module")
def probes() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for line in _messages():
        msg = line["msg"]
        if line["dir"] != "in" or msg.get("method") != "item/completed":
            continue
        item = msg["params"]["item"]
        if item["type"] != "commandExecution":
            continue
        tag = re.search(r": ([A-Z]\w*);", item["command"])
        if tag:
            found[tag.group(1)] = item
    return found


def test_every_case_was_run(probes: dict[str, dict[str, Any]]) -> None:
    assert set(WORKS) | set(BLOCKED) | set(BY_EXIT_CODE) | {"L6", "E1"} <= set(probes)


@pytest.mark.parametrize("case", sorted(WORKS))
def test_allowed_case_works(probes: dict[str, dict[str, Any]], case: str) -> None:
    item = probes[case]
    assert item["exitCode"] == 0, item["aggregatedOutput"]
    assert WORKS[case] in item["aggregatedOutput"]


@pytest.mark.parametrize("case", BLOCKED)
def test_blocked_case_is_blocked(probes: dict[str, dict[str, Any]], case: str) -> None:
    item = probes[case]
    assert item["exitCode"] != 0
    assert any(word in item["aggregatedOutput"] for word in DENIED), item["aggregatedOutput"]


@pytest.mark.parametrize("case", BY_EXIT_CODE)
def test_blocked_by_exit_code(probes: dict[str, dict[str, Any]], case: str) -> None:
    """Real folders are probed by exit code only, the content goes to /dev/null."""
    assert probes[case]["aggregatedOutput"].strip() == "exit=1"


def test_shell_environment_has_no_secrets(probes: dict[str, dict[str, Any]]) -> None:
    """E1 (W2-02.1 H1): the names of the shell's variables, recorded without values."""
    names = set(probes["E1"]["aggregatedOutput"].split())
    assert {"HOME", "PATH", "TMPDIR", "VIRTUAL_ENV", "UV_NO_CONFIG"} <= names
    for name in names:
        upper = name.upper()
        assert not any(word in upper for word in ("TOKEN", "SECRET", "KEY")), name
        assert not upper.startswith(("AWS_", "CLAUDE_CODE_")) and upper != "CODEX_HOME", name


def test_home_wide_search_finds_only_the_own_attachment(
    probes: dict[str, dict[str, Any]],
) -> None:
    """L6: `grep -rl CANARY- ~` finds nothing outside the allowlist."""
    hits = probes["L6"]["aggregatedOutput"].split()
    assert hits[-1] == "done"
    assert all("/groupchats/attachments/main/" in hit for hit in hits[:-1])


def test_no_canary_reaches_the_model(probes: dict[str, dict[str, Any]]) -> None:
    own = re.search(r"CANARY-\w+", probes["L15b"]["aggregatedOutput"])
    assert own is not None
    incoming = "\n".join(json.dumps(m["msg"]) for m in _messages() if m["dir"] == "in")
    assert set(re.findall(r"CANARY-[0-9a-f]{12}", incoming)) == {own.group(0)}


def test_patch_outside_was_asked_and_declined() -> None:
    messages = _messages()
    asked = [
        m["msg"] for m in messages if m["msg"].get("method") == "item/fileChange/requestApproval"
    ]
    assert len(asked) == 1
    answer = next(
        m["msg"] for m in messages if m["dir"] == "out" and m["msg"].get("id") == asked[0]["id"]
    )
    assert answer["result"] == {"decision": "decline"}


def test_parent_instructions_are_not_loaded() -> None:
    """The outer repo's `AGENTS.md` (code word HYENA-4) never reaches the model."""
    incoming = "\n".join(json.dumps(m["msg"]) for m in _messages() if m["dir"] == "in")
    assert "HYENA-4" not in incoming
    started = next(
        m["msg"]["result"]
        for m in _messages()
        if "instructionSources" in m["msg"].get("result", {})
    )
    assert [Path(p).name for p in started["instructionSources"]] == ["AGENTS.md"]
    assert started["instructionSources"][0].endswith("/agents/gpt-1/work/AGENTS.md")
