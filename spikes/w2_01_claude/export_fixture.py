"""Export the final E19 recording as a sanitised fixture (same rules as spike S1).

Home path -> "~" (also in the dash-encoded project key), account block of the initialize
response -> placeholder, e-mail addresses -> user@example.com. Then the file is checked
for the home path, the user name, e-mail addresses and "sk-ant".

Run: uv run python spikes/w2_01_claude/export_fixture.py [recording] [fixture-name]
"""

import json
import re
import sys
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
TARGET = SPIKE.parent.parent / "tests" / "fixtures" / "claude"
HOME = str(Path.home())
HOME_KEY = HOME.replace("/", "-")
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
        return {
            k: dict(ACCOUNT) if k == "account" and isinstance(v, dict) else scrub(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        text = obj.replace(HOME, "~").replace(HOME_KEY, "-~")
        return EMAIL.sub("user@example.com", text)
    return obj


def check(path: Path) -> list[str]:
    text = path.read_text()
    problems = [n for n in (HOME, HOME_KEY, "sk-ant", f"/{USER}/", f"-{USER}-") if n in text]
    return problems + [m for m in set(EMAIL.findall(text)) if m != "user@example.com"]


def main() -> None:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else SPIKE / "tmp/runs/e19_read_allowlist.jsonl"
    name = sys.argv[2] if len(sys.argv) > 2 else "read_allowlist.jsonl"
    rows = [json.loads(line) for line in source.read_text().splitlines()]
    out = TARGET / name
    with out.open("w") as fh:
        for row in rows:
            fh.write(json.dumps({**row, "msg": scrub(row["msg"])}, ensure_ascii=False) + "\n")
    problems = check(out)
    print(f"{name}: {len(rows)} Zeilen, {out.stat().st_size // 1024} KiB,", problems or "ok")


if __name__ == "__main__":
    main()
