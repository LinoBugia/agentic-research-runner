"""Rebuild tests/fixtures/codex/*.jsonl from the raw logs in tmp/out/ (sanitized).

Each fixture line is {"t": seconds, "dir": "in"|"out", "msg": <JSON-RPC message>}:
"out" = SDK -> app-server (requests, approval answers), "in" = app-server -> SDK.
Afterwards the fixtures are scanned for leftovers (home path, tokens, e-mail).
"""

import re
import sys

from common import FIXTURES, OUT, REDACT_KEYS, save_fixture

SOURCES = {
    "turn_write_and_run": "q03_raw.jsonl",
    "turn_reasoning_patch": "q03_reasoning_raw.jsonl",
    "turn_reasoning_medium_hard": "q04_gpt-6.1-sol_medium_hard_raw.jsonl",
    "turn_developer_vs_base_instructions": "q06_raw.jsonl",
    "turn_mcp_needs_approval": "q07_thread_noapprove_raw.jsonl",
    "turn_mcp_thread": "q07_thread_raw.jsonl",
    "turn_mcp_project": "q07_project_raw.jsonl",
    "turn_mcp_trusted": "q07_trusted_raw.jsonl",
    "turn_agents_md_and_skills": "q08_raw.jsonl",
    "thread_resume_after_restart": "q09_resume_raw.jsonl",
    "turn_approvals_user": "q10_user_raw.jsonl",
    "turn_approvals_auto": "q10_auto_raw.jsonl",
    "turn_sandbox_default": "q11_default_raw.jsonl",
    "turn_sandbox_config": "q11_config_raw.jsonl",
    "turn_hint_steer": "q12_steer_raw.jsonl",
    "turn_hint_external": "q12_external_raw.jsonl",
}

LEAKS = [
    re.compile(r"/Users/[^/\"]+"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+\.[A-Za-z]{2,}"),
    re.compile(r"\.local\b"),
    re.compile(r"github\.com/[^\"]+"),
    # skill-sync bucket = <org-uuid>_<account-uuid>; only the placeholder may remain
    re.compile(r"skills/synced/(?!<org-id>_<account-id>/)[^/\"]+"),
    re.compile(r"\.bucket-(?!<org-id>_<account-id>)[^/\"]+"),
    # account/user/org/installation fields must be redacted (or null)
    re.compile(r"\"(" + "|".join(sorted(REDACT_KEYS)) + r")\": (?!\"<redacted>\"|null)[^,}]+"),
]


def main() -> int:
    """Rebuild all fixtures and scan them; `--scan-only` just scans the committed ones."""
    if "--scan-only" not in sys.argv[1:]:
        for name, raw in SOURCES.items():
            print(save_fixture(OUT / raw, name).name)
    hits = 0
    for path in sorted(FIXTURES.glob("*.jsonl")):
        text = path.read_text(encoding="utf-8")
        for pattern in LEAKS:
            for match in pattern.finditer(text):
                hits += 1
                print(f"LEAK {path.name}: {pattern.pattern} -> {match.group(0)[:40]}")
    print(f"{len(list(FIXTURES.glob('*.jsonl')))} fixtures, {hits} leak hits")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
