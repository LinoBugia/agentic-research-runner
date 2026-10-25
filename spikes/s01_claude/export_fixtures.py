"""Export selected raw recordings as sanitised fixtures to tests/fixtures/claude/.

Sanitising: home path -> "~" (also in the dash-encoded project key), the account block of
the initialize response -> placeholder, e-mail addresses -> user@example.com. Afterwards
the files are checked for the home path, the user name, e-mail addresses and "sk-ant".

Run: uv run --group spikes python spikes/s01_claude/export_fixtures.py
"""

import json
import re
from pathlib import Path

from common import RUNS, SPIKE_DIR

TARGET = SPIKE_DIR.parent.parent / "tests" / "fixtures" / "claude"

FIXTURES = {
    "q01_solo": "solo_three_turns_thinking_tools.jsonl",
    "q06_sandbox": "sandbox_violation_and_network_request.jsonl",
    "q07_can_use_tool": "can_use_tool_deny_after_wait.jsonl",
    "q08_pretooluse_deny": "pretooluse_deny_with_reason.jsonl",
    "q09_additional_context_convention": "posttooluse_hint_and_mcp_tool.jsonl",
    "q10_user_agent": "project_mcp_json_and_skill.jsonl",
}

HOME = str(Path.home())
HOME_KEY = HOME.replace("/", "-")  # project keys encode /Users/x as -Users-x
USER = Path.home().name
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ACCOUNT = {
    "email": "user@example.com",
    "organization": "Example Organization",
    "subscriptionType": "Claude Max",
    "apiProvider": "firstParty",
}


def scrub(obj):
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key == "account" and isinstance(value, dict) and "email" in value:
                out[key] = dict(ACCOUNT)
            else:
                out[key] = scrub(value)
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        text = obj.replace(HOME, "~").replace(HOME_KEY, "-~")
        return EMAIL.sub("user@example.com", text)
    return obj


def check(path: Path) -> list[str]:
    text = path.read_text()
    problems = []
    for needle in (HOME, HOME_KEY, "sk-ant", f"/{USER}/", f"-{USER}-"):
        if needle in text:
            problems.append(needle)
    problems += [m for m in set(EMAIL.findall(text)) if m != "user@example.com"]
    return problems


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    for run, name in FIXTURES.items():
        rows = [json.loads(line) for line in (RUNS / f"{run}.jsonl").read_text().splitlines()]
        t0 = rows[0]["t"]
        out = TARGET / name
        with out.open("w") as fh:
            for row in rows:
                row = {"dir": row["dir"], "t": round(row["t"] - t0, 3), "msg": scrub(row["msg"])}
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        problems = check(out)
        status = "ok" if not problems else f"PROBLEME: {problems}"
        print(f"{name}: {len(rows)} Events, {out.stat().st_size // 1024} KiB, {status}")


if __name__ == "__main__":
    main()
