"""E19 live result as a test without a model (W2-01 A2): the recording of the read
allowlist test (`tests/fixtures/claude/read_allowlist.jsonl`, spike
`spikes/w2_01_claude/e19_read_allowlist.py`) matches the expected table L1-L15 and the
sandbox-only cases."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "claude" / "read_allowlist.jsonl"

# Bash probe: case label → expected exit code (0 = works, anything else = blocked).
BLOCKED = {
    "head -c 0 ~/.ssh/known_hosts",
    "ls ~/.ssh",
    "head -c 0 ~/.SSH/known_hosts (case)",
    "python3 open(~/.arr/canary)",
    "cat ~/.arr/canary",
    "cat other project canary",
    "cat <wf>/.arr/canary",
    "cat other project, case variant",
    "write output/ (not writable)",
    "write into read dir",
    "cat read-dir/geheim/canary",
    "python3 read-dir/geheim/canary",
    "ls read-dir/geheim",
    "ln outside hl_bash.txt",
    "cat hl_bash.txt",
    "python os.link outside hl_py.txt",
    "python read hl_py.txt",
    "cat sl.txt (symlink)",
    "cat <run>/skills/probe/SKILL.md",
    "cat foreign channel attachment",
    "ls attachments root",
    "cd outside && cat",
    "$'\\x73udo' -n true (runtime name)",
    "sudo -n true",
    "python3 write outside",
    "touch /tmp marker",
    "cd outside && echo >",
    "sed -i outside",
    "chmod outside",
    "python urllib example.com",
    "python raw socket 1.1.1.1:80",
    "open -g -j -a Calculator (LaunchServices)",
    "git push (network)",
}
WORKS = {
    "cat notes.md",
    "python3 read notes.md",
    "write work file",
    "cat output/summary.md",
    "write $TMPDIR",
    "$TMPDIR resolves into Run-tmp",
    "cat paper.tex",
    "python3 is the run venv",
    "import sympy (run venv over base venv)",
    "import ssl",
    "cat SystemVersion.plist",
    "ln -s outside sl.txt",
    "run own plugin script",
    "cat own channel attachment",
    "grep -rl CANARY over tmp root (N6)",  # exit 0 = zero hits
    "ls /tmp (system temp readable)",
}

# File tools: path suffix → "rule" (deny rule), "callback" (can_use_tool) or "ok".
FILE_TOOLS = {
    ("Read", "-missing"): "rule",
    ("Read", ".txt", "/.arr/"): "rule",
    ("Grep", "/projects/other"): "callback",
    ("Glob", "/projects/other"): "callback",
    ("Grep", '"path": "~"'): "callback",
    ("Read", "notes.md"): "ok",
    ("Read", "/output/summary.md"): "ok",
    ("Read", "/literatur/paper.tex"): "ok",
    ("Grep", "/literatur"): "ok",
    ("Write", "/literatur/new_by_tool.txt"): "rule",
    ("Read", "/sympy/__init__.py"): "ok",
    ("Read", "SystemVersion.plist"): "callback",  # PolicyEngine allows system paths
    ("Read", "/geheim/canary.txt"): "rule",
    ("Read", "sl.txt"): "callback",
    ("Read", "/skills/probe/SKILL.md"): "callback",
    ("Skill", "arr:probe"): "ok",
    ("Read", "/beweis/7/lemma.txt"): "ok",
    ("Read", "/kritik/8/notiz.txt"): "callback",
    ("Write", "/outside/written_by_tool.txt"): "callback",
    ("Write", "ok_by_tool.txt"): "ok",
}


def _rows() -> list[dict[str, Any]]:
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return " ".join(part.get("text", "") for part in content or [])


def _calls_and_results() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    asked: set[str] = set()
    for row in _rows():
        msg = row["msg"]
        if row["dir"] == "in" and msg.get("type") == "assistant":
            for block in msg["message"]["content"]:
                if block.get("type") == "tool_use":
                    calls[block["id"]] = block
        elif row["dir"] == "in" and msg.get("type") == "user":
            content = msg["message"]["content"]
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result":
                    results[block["tool_use_id"]] = block
        elif row["dir"] == "in" and msg.get("type") == "control_request":
            request = msg.get("request", {})
            if request.get("subtype") == "can_use_tool":
                asked.add(request.get("tool_use_id") or request.get("input", {}).get("host", ""))
    return calls, results, asked


def probe_lines() -> dict[str, int]:
    calls, results, _ = _calls_and_results()
    bash = next(i for i, c in calls.items() if c["name"] == "Bash")
    lines = {}
    for line in _text(results[bash]["content"]).splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            lines[parts[1]] = int(parts[2])
    return lines


def test_e19_bash_probe_matches_the_table() -> None:
    lines = probe_lines()
    assert set(lines) == BLOCKED | WORKS
    for label in BLOCKED:
        assert lines[label] != 0, label
    for label in WORKS:
        assert lines[label] == 0, label


def _matches(call: dict[str, Any], key: tuple[str, ...]) -> bool:
    if call["name"] != key[0]:
        return False
    target = json.dumps(call["input"], ensure_ascii=False)
    return all(part in target for part in key[1:])


@pytest.mark.parametrize("key", list(FILE_TOOLS), ids=lambda k: " ".join(k))
def test_e19_file_tools_match_the_table(key: tuple[str, ...]) -> None:
    calls, results, asked = _calls_and_results()
    found = [i for i, c in calls.items() if _matches(c, key)]
    assert found, key
    call_id = found[-1]
    result = results[call_id]
    text = _text(result.get("content"))
    expected = FILE_TOOLS[key]
    if expected == "ok":
        assert not result.get("is_error") and call_id not in asked, text
    elif expected == "rule":
        assert result.get("is_error") and "denied by your permission settings" in text
        assert call_id not in asked
    else:
        assert result.get("is_error") and call_id in asked, text


def test_e19_network_went_to_the_callback_and_nothing_leaked() -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    assert '"subtype": "can_use_tool"' in text and "SandboxNetworkAccess" in text
    assert "deny network-outbound example.com:443" in text
    assert not re.search(r"CANARY-[0-9a-f]{16}", text)  # no canary content reached the model
    assert "user@example.com" in text  # account block replaced
