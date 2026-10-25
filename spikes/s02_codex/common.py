"""Shared helpers for the S2 Codex spike (throwaway code, never imported by arr).

- `make_codex()` builds an `AsyncCodex` with overrides that keep the user's global
  ~/.codex config (default model, MCP servers, plugins, notify hook) out of spike runs.
- `RawTap` tees every JSON-RPC line between SDK and app-server into a JSONL file.
- `record_usage()` appends one line per turn to tmp/usage.jsonl (turn budget, §6).
- `sanitize()` scrubs home paths, ids and token-like strings before saving fixtures.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from openai_codex import AsyncCodex, CodexConfig

SPIKE_DIR = Path(__file__).resolve().parent
TMP = SPIKE_DIR / "tmp"
OUT = TMP / "out"
WORK = TMP / "work"
USAGE_LOG = TMP / "usage.jsonl"
FIXTURES = SPIKE_DIR.parents[1] / "tests" / "fixtures" / "codex"

# Smallest suitable model from q02_models.py; effort stays low (assignment §6).
MODEL = os.environ.get("S2_MODEL", "gpt-6-luna")
EFFORT = os.environ.get("S2_EFFORT", "low")
MAX_TURNS = 40

# The user's global config sets a large default model, priority tier, its own MCP
# servers, plugins and a notify hook. None of that belongs in a research agent.
ISOLATION_OVERRIDES: tuple[str, ...] = (
    f'model="{MODEL}"',
    f'model_reasoning_effort="{EFFORT}"',
    'service_tier="default"',
    "notify=[]",
    'web_search="disabled"',
    "features.plugins=false",
    "features.apps=false",
    "features.computer_use=false",
    "features.browser_use=false",
    "features.image_generation=false",
    "features.multi_agent=false",
    "features.tool_suggest=false",
    "features.goals=false",
    # Not here: features.code_mode_host=false. It removes the shell entirely (q11 run 2).
    "mcp_servers.computer-use.enabled=false",
    "mcp_servers.node_repl.enabled=false",
)


class RawTap:
    """Record raw JSON-RPC traffic of one AsyncCodex client (wire format, unparsed)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("w", encoding="utf-8")
        self.t0 = time.monotonic()

    def write(self, direction: str, message: dict[str, Any]) -> None:
        line = {"t": round(time.monotonic() - self.t0, 3), "dir": direction, "msg": message}
        self._fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        self._fh.flush()

    def attach(self, codex: AsyncCodex) -> None:
        sync = codex._client._sync  # private: the only place that sees raw lines
        read, write = sync._read_message, sync._write_message

        def tapped_read() -> dict[str, Any]:
            msg = read()
            self.write("in", msg)
            return msg

        def tapped_write(payload: dict[str, Any]) -> None:
            self.write("out", payload)
            write(payload)

        sync._read_message = tapped_read
        sync._write_message = tapped_write

    def close(self) -> None:
        self._fh.close()


def make_codex(
    *,
    tap: RawTap | None = None,
    env: dict[str, str] | None = None,
    extra_overrides: tuple[str, ...] = (),
    approval_handler: Callable[[str, dict | None], dict] | None = None,
) -> AsyncCodex:
    """AsyncCodex with spike isolation; optional raw tap and approval handler."""
    overrides = ISOLATION_OVERRIDES
    if env and "CODEX_HOME" in env:
        # In a fresh home these would create half server entries ("invalid transport").
        overrides = tuple(o for o in overrides if not o.startswith("mcp_servers."))
    config = CodexConfig(
        config_overrides=overrides + extra_overrides,
        env=env,
    )
    codex = AsyncCodex(config=config)
    if tap is not None:
        tap.attach(codex)
    if approval_handler is not None:
        # AsyncCodex has no public hook; CodexClient takes one, AsyncCodexClient drops it.
        codex._client._sync._approval_handler = approval_handler
    return codex


def turns_used() -> int:
    if not USAGE_LOG.exists():
        return 0
    return sum(1 for line in USAGE_LOG.read_text().splitlines() if line.strip())


def check_budget(planned: int) -> None:
    used = turns_used()
    if used + planned > MAX_TURNS:
        sys.exit(f"Turn-Budget: {used} verbraucht + {planned} geplant > {MAX_TURNS}")


def record_usage(script: str, label: str, result: Any, model: str = MODEL) -> None:
    """Append one turn's usage (ThreadTokenUsage.last/total) to tmp/usage.jsonl."""
    usage = getattr(result, "usage", None)
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "script": script,
        "label": label,
        "model": model,
        "effort": EFFORT,
        "status": str(getattr(result, "status", None)),
        "duration_ms": getattr(result, "duration_ms", None),
        "last": usage.last.model_dump(by_alias=True) if usage else None,
        "total": usage.total.model_dump(by_alias=True) if usage else None,
    }
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with USAGE_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def fresh_dir(name: str) -> Path:
    """Empty working folder under tmp/work/ (only place where spike agents work)."""
    path = WORK / name
    if path.exists():
        import shutil

        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def dump(name: str, data: Any) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), "utf-8")
    return path


# --- sanitizing -------------------------------------------------------------------

_HOME = str(Path.home())
REDACT_KEYS = {
    "accountId",
    "account_id",
    "userId",
    "user_id",
    "email",
    "chatgptAccountId",
    "planId",
    "orgId",
    "organizationId",
    "loginId",
    "authUrl",
    "accessToken",
    "refreshToken",
    "idToken",
    "apiKey",
    "installationId",
    "serverName",
    "userCode",
    "verificationUrl",
    "originUrl",
}
_PATTERNS = [
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]+"), "<jwt>"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "<api-key>"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<email>"),
    # Skill-sync bucket ~/.agents/skills/synced/<org-uuid>_<account-uuid>/ (+ its marker file)
    (re.compile(r"skills/synced/[^/\"\\\s]+"), "skills/synced/<org-id>_<account-id>"),
    (re.compile(r"\.bucket-[^/\"\\\s]+"), ".bucket-<org-id>_<account-id>"),
]


def _scrub_str(value: str) -> str:
    value = value.replace(_HOME, "~")
    for pattern, repl in _PATTERNS:
        value = pattern.sub(repl, value)
    return value


def sanitize(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if k in REDACT_KEYS and v is not None else sanitize(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, str):
        return _scrub_str(obj)
    return obj


def save_fixture(raw: Path, name: str, keep: Callable[[dict], bool] | None = None) -> Path:
    """Copy a RawTap log into tests/fixtures/codex/<name>.jsonl, sanitized."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    target = FIXTURES / f"{name}.jsonl"
    lines = []
    for line in raw.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if keep is not None and not keep(entry):
            continue
        lines.append(json.dumps(sanitize(entry), ensure_ascii=False))
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def method_counts(raw: Path) -> dict[str, int]:
    """Count methods of incoming notifications/requests in a RawTap log."""
    counts: dict[str, int] = {}
    for line in raw.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        msg = entry["msg"]
        if entry["dir"] == "in" and "method" in msg:
            key = msg["method"] + (" (server request)" if "id" in msg else "")
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def item_types(raw: Path) -> dict[str, int]:
    """Count ThreadItem types in item/completed notifications."""
    counts: dict[str, int] = {}
    for line in raw.read_text(encoding="utf-8").splitlines():
        msg = json.loads(line)["msg"]
        if msg.get("method") == "item/completed":
            t = msg["params"]["item"].get("type", "?")
            counts[t] = counts.get(t, 0) + 1
    return dict(sorted(counts.items()))
