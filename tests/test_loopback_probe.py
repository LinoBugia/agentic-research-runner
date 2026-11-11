"""Loopback probe (W4-03.2 N3, W4-01 G8): no agent reaches a service on this machine.

After the review's `probe_loopback.py`: a small HTTP service listens on 127.0.0.1 and answers
with a bait. The real Claude CLI runs the probe in its sandbox through arr's real backend
(hooks, PolicyEngine) with a decider that says yes to everything. The Claude sandbox itself
does not block loopback (measured in the review); the protection is arr's fixed rule and that
no host reaching loopback gets into `allowedDomains`, not even from a rule `hosts: ["*"]`.
Counter-check: with loopback in `allowedDomains` the same probe reaches the service.
"""

import json
import secrets
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from arr.permissions.policy import AlwaysRule, PermissionRules, profile_for
from arr.permissions.types import DecidedBy, Decision, DecisionAction

pytestmark = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt exists on macOS only"),
]

BAIT = f"LOOPBACK-{secrets.token_hex(6)}"

PROBE = r"""
import json, os, subprocess, sys, urllib.request
port = int(sys.argv[1]); bait = sys.argv[2]; out = {}
def run(cmd):
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return bait in done.stdout
    except Exception:
        return False
proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
socks = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or ""
for host in ("127.0.0.1", "localhost", "foo.localhost", "[::ffff:127.0.0.1]"):
    out[f"curl {host}"] = run(["curl", "-s", "-m", "8", f"http://{host}:{port}/"])
    if proxy:
        out[f"curl -x HTTP {host}"] = run(
            ["curl", "-s", "-m", "8", "--noproxy", "", "-x", proxy, f"http://{host}:{port}/"]
        )
    if socks:
        out[f"curl -x SOCKS {host}"] = run(
            ["curl", "-s", "-m", "8", "--noproxy", "", "-x",
             socks.replace("socks5://", "socks5h://"), f"http://{host}:{port}/"]
        )
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=8) as r:
        out["urllib"] = bait in r.read().decode()
except Exception:
    out["urllib"] = False
open("loop.json", "w").write(json.dumps(out))
"""


class Allow:
    async def decide(self, item: Any) -> Decision:
        return Decision(action=DecisionAction.ALLOW, decided_by=DecidedBy.USER, reason="Test")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = BAIT.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


class AllowingGatekeeper:
    """A supervising agent that allows everything it is shown, once (W4-04, Punkt 6)."""

    def __init__(self) -> None:
        self.shown: list[Any] = []

    async def screen(self, item: Any, context: Any) -> Decision:
        self.shown.append(item.request)
        return Decision(action=DecisionAction.ALLOW, decided_by=DecidedBy.GATEKEEPER, reason="ja")

    def failed(self, item: Any) -> Decision:
        return Decision(action=DecisionAction.DENY, decided_by=DecidedBy.FIXED_RULE, reason="nein")


def reached(kit: Any, lab: Any, port: int, screener: Any = None) -> dict[str, bool]:
    work = lab.agent.work_dir
    (work / "loop.py").write_text(PROBE)
    (work / "loop.json").unlink(missing_ok=True)
    script = [kit.bash(f"python3 loop.py {port} {BAIT}", "Loopback")]
    kit.run_file_tools(lab, script, Allow(), screener=screener)
    return json.loads((work / "loop.json").read_text())


def test_n3_no_agent_reaches_a_service_on_loopback_not_even_with_a_rule_star() -> None:
    import login_guard_kit as kit

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        with kit.open_lab("claude") as lab:
            # an allow rule with `*`, built past its check (as an old arr.yaml might hold it)
            star = AlwaysRule.model_construct(
                action="allow", hosts=["*"], exact=False, tools=[], paths=[], commands=[], reason=""
            )
            rules = PermissionRules.model_construct(always=[star], sensitive_paths=[])
            starred = replace(
                lab, profile=profile_for(lab.agent.file.config, lab.run_paths, rules, home=lab.home)
            )
            assert starred.profile.allowed_domains == ()
            for variant in (lab, starred):
                seen = reached(kit, variant, port)
                assert not any(seen.values()), seen
            # counter-checks: `*` (what the rule gave before W4-03.2) and loopback itself in
            # allowedDomains: the same probe reaches the service
            for domains in (("*",), ("127.0.0.1", "localhost")):
                opened = replace(lab, profile=replace(lab.profile, allowed_domains=domains))
                assert any(reached(kit, opened, port).values()), domains
    finally:
        server.shutdown()


def test_w4_04_a_rule_star_and_an_allowing_gatekeeper_open_no_loopback() -> None:
    """W4-04 Punkt 6: a hand-written rule `hosts: ["*"]` plus a supervising agent that allows
    everything: still nothing on this machine is reached, and no loopback request is shown to
    it. Counter-check: `*` in `allowedDomains` reaches the service through the same stage."""
    import login_guard_kit as kit

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        with kit.open_lab("claude") as lab:
            star = AlwaysRule.model_construct(
                action="allow", hosts=["*"], exact=False, tools=[], paths=[], commands=[], reason=""
            )
            rules = PermissionRules.model_construct(always=[star], sensitive_paths=[])
            starred = replace(
                lab, profile=profile_for(lab.agent.file.config, lab.run_paths, rules, home=lab.home)
            )
            gatekeeper = AllowingGatekeeper()
            seen = reached(kit, starred, port, gatekeeper)
            assert not any(seen.values()), seen
            loopback = [
                r for r in gatekeeper.shown if any("127" in h or "localhost" in h for h in r.hosts)
            ]
            assert loopback == []
            opened = replace(lab, profile=replace(lab.profile, allowed_domains=("*",)))
            assert any(reached(kit, opened, port, AllowingGatekeeper()).values())
    finally:
        server.shutdown()
