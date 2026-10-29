"""Turn a raw recording of `e19_live.py` (or a smoke test) into a sanitized fixture.

Home path → `~`, the user's temp folders → `<user-tmp>` / `/tmp/claude-<uid>`, account and
installation data → placeholders. Afterwards the fixture is
searched for the user name, e-mail addresses and the temp folders; any hit aborts.

    uv run python spikes/w2_02_codex/make_fixture.py tmp/out/e19_v1.raw.jsonl read_allowlist
    uv run python spikes/w2_02_codex/make_fixture.py --blocked <raw> <rollout> blocked_commands

`--blocked` keeps only what the rollout reconciliation needs: the stream's `turn/started`
and `commandExecution` items, the rollout's turn markers and tool calls with their output.
Lines are `{"source": "stream" | "rollout", "line": …}`.
"""

import getpass
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from common import FIXTURES

HOME = str(Path.home())
USER_TMP = [os.path.realpath(tempfile.gettempdir()), tempfile.gettempdir().rstrip("/")]
CLAUDE_TMP = f"/tmp/claude-{os.getuid()}"
REDACT = {"email", "accountId", "userId", "installationId", "serverName", "chatgptUserId"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _secret(key: str) -> bool:
    return key in REDACT or key.lower().endswith(("accountid", "userid"))


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("<redacted>" if _secret(k) and v else clean(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str):
        text = value.replace(f"/System/Volumes/Data{HOME}", "/System/Volumes/Data~")
        for tmp in USER_TMP:
            text = text.replace(tmp, "<user-tmp>")
        return text.replace(CLAUDE_TMP, "/tmp/claude-<uid>").replace(HOME, "~")
    return value


def _read(path: Path) -> list[Any]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _blocked(raw: Path, rollout: Path) -> list[Any]:
    keep: list[Any] = []
    for line in _read(raw):
        msg = line["msg"]
        item = (msg.get("params") or {}).get("item") or {}
        if msg.get("method") == "turn/started" or item.get("type") == "commandExecution":
            keep.append({"source": "stream", "line": line})
    kinds = {"task_started", "task_complete", "custom_tool_call", "custom_tool_call_output"}
    kinds |= {"function_call", "function_call_output"}
    for line in _read(rollout):
        if (line.get("payload") or {}).get("type") in kinds:
            keep.append({"source": "rollout", "line": line})
    return keep


def main() -> None:
    if sys.argv[1] == "--blocked":
        lines = _blocked(Path(sys.argv[2]), Path(sys.argv[3]).expanduser())
        name = sys.argv[4]
    else:
        lines, name = _read(Path(sys.argv[1])), sys.argv[2]
    out = [json.dumps(clean(line), ensure_ascii=False) for line in lines]
    text = "\n".join(out) + "\n"
    user = getpass.getuser()
    hits = [w for w in (user, "/private/tmp", "/var/folders", CLAUDE_TMP) if w in text]
    hits += [m for m in EMAIL.findall(text) if not m.endswith("@example.com")]
    if hits:
        sys.exit(f"Bereinigung unvollständig: {sorted(set(hits))[:5]}")
    target = FIXTURES / f"{name}.jsonl"
    target.write_text(text, encoding="utf-8")
    print(f"ok {target.name}: {len(out)} Zeilen")


if __name__ == "__main__":
    main()
