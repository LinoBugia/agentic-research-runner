"""W4-04.1: the review's findings on the supervising agent, each with a counter-check.

M1 withdrawing works at once, M2/G2 a clean environment and outside hygiene for the call,
M4 the Not-Aus knows the supervising agent's profile, G1 package names by a positive list,
G3 no channel between agents through the supervising agent, G4 the protections the review's
mutations found untested. Fake models and a stand-in CLI only: no keychain, no real API.
"""

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from test_gatekeeper import ALLOW_JSON, Env, FakeModel, bash, host

from arr.backends.base import CostKind, Usage
from arr.models import AuthKind, AuthProfile
from arr.permissions import gatekeeper as gatekeeper_module
from arr.permissions.gatekeeper import (
    DISALLOWED_TOOLS,
    SYSTEM_PROMPT,
    CallFailed,
    ClaudeGatekeeperModel,
    ScreenContext,
)
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.mandate import check_request as mandate_check
from arr.permissions.mandate import parse_mandate, standard_text
from arr.permissions.types import (
    ApprovalItem,
    ApprovalStatus,
    DecidedBy,
    Decision,
    DecisionAction,
)
from arr.settings import app_dir


@pytest.fixture
def env(tmp_path: Path, arr_home: Path) -> Env:
    return Env(tmp_path)


async def until(check: Any, within: float = 5.0) -> None:
    for _ in range(int(within / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Bedingung nicht rechtzeitig erfüllt")


def lines(env: Env) -> list[dict[str, Any]]:
    path = env.run_dir / "gatekeeper.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


# --------------------------------------------------------------------------------------------
# M1: withdrawing holds at once, also for requests being screened or waiting for the lock
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("revoke", [True, False], ids=["zurückgezogen", "Gegenprobe"])
async def test_m1_withdrawing_reaches_requests_in_the_call_and_behind_the_lock(
    env: Env, revoke: bool
) -> None:
    model = FakeModel(*[ALLOW_JSON] * 7, delays=[0, 0.6])
    inbox = env.inbox(model, timeout=0.3)
    await inbox.handle(host("arxiv.org"))
    first = inbox.items()[-1]
    waiting = [asyncio.create_task(inbox.handle(host("arxiv.org"))) for _ in range(5)]
    await until(
        lambda: len(model.prompts) >= 2
    )  # one equal request is with the model now, four wait
    if revoke:
        inbox.revoke(first.id)
    decisions = await asyncio.gather(*waiting)
    allowed = [d for d in decisions if d.action is DecisionAction.ALLOW]
    if not revoke:  # counter-check: without withdrawing the gatekeeper allows all five
        assert len(allowed) == 5
        return
    assert allowed == []  # (before W4-04.1: the one in the call and 4 of 4 waiting allowed)
    passed = [i for i in inbox.items() if i.escalated_by is not None]
    assert len(passed) == 5 and all("zurückgezogen" in i.escalated_by.note for i in passed)  # type: ignore[union-attr]
    assert len(model.prompts) == 2  # the waiting ones never reached the model


async def test_m1_withdrawn_during_the_call_with_gatekeeper_only_is_a_deny(env: Env) -> None:
    model = FakeModel(ALLOW_JSON, ALLOW_JSON, delays=[0, 0.4])
    inbox = env.inbox(model, to_user=False)
    await inbox.handle(host("arxiv.org"))
    task = asyncio.create_task(inbox.handle(host("arxiv.org")))
    await until(lambda: len(model.prompts) >= 2)
    inbox.revoke(inbox.items()[0].id)
    decision = await task
    assert decision.action is DecisionAction.DENY
    assert decision.decided_by is DecidedBy.FIXED_RULE and "zurückgezogen" in decision.reason


# --------------------------------------------------------------------------------------------
# M2, G2: the call starts clean, like every process outside the sandbox
# --------------------------------------------------------------------------------------------

INHERITED = {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/fremd",
    "ANTHROPIC_API_KEY": "sk-ant-api03-" + "E" * 40,
    "ANTHROPIC_AUTH_TOKEN": "fremd-auth",
    "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "E" * 40,
    "CLAUDE_CONFIG_DIR": "/tmp/fremd-claude",
    "CLAUDE_CODE_SESSION_ID": "eltern-sitzung",
    "CLAUDE_CODE_USER_EMAIL": "eltern@example.org",
    "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/eltern.sock",
    "CLAUDECODE": "1",
    "CLAUDE_AGENT_SDK_FREMD": "1",
    "CLAUDE_PREVIEW_CLASSIFIER_FLOOR": "1",  # a session variable outside CLAUDE_CODE_*
}


def stand_in_cli(folder: Path, dump: Path) -> Path:
    """A CLI that answers the SDK's version question and writes its environment, then fails."""
    cli = folder / "claude-stand-in"
    cli.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-v" ]; then echo "2.1.200 (Claude Code)"; exit 0; fi\n'
        f"/usr/bin/env > {dump}\nexit 3\n"
    )
    cli.chmod(0o700)
    return cli


def call_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, clean: bool = True
) -> dict[str, str]:
    work = tmp_path / "run" / "agents" / "a" / "work"  # a write root of the run
    work.mkdir(parents=True)
    for name, value in INHERITED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", f"{work}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    if not clean:
        monkeypatch.setattr(gatekeeper_module, "clean_environment", lambda: [])
    dump = tmp_path / "env.txt"
    model = ClaudeGatekeeperModel(
        model="claude-haiku-5-5",
        profile=AuthProfile(name="mac", kind=AuthKind.CLAUDE_OAUTH),  # no secret: no start program
        write_roots=[work],
        cli_path=str(stand_in_cli(tmp_path, dump)),
    )
    with pytest.raises(Exception):  # noqa: B017 - the stand-in ends the call
        asyncio.run(asyncio.wait_for(model.ask(SYSTEM_PROMPT, "Anfrage"), 30))
    pairs = (line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
    return dict(pairs)


def test_m2_no_inherited_login_address_or_session_reaches_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arr_home: Path
) -> None:
    seen = call_env(tmp_path, monkeypatch)
    assert not set(INHERITED) & set(seen), sorted(set(INHERITED) & set(seen))
    work = str(tmp_path / "run" / "agents" / "a" / "work")
    path = seen["PATH"].split(os.pathsep)
    assert path[0] == str(app_dir() / "bin" / "no-git")  # the `git` stub comes first
    assert work not in path  # no write root on the CLI's PATH
    assert (Path(path[0]) / "git").is_file()
    assert seen["NODE_DISABLE_COMPILE_CACHE"] == "1"
    assert seen["TMPDIR"] == str(app_dir() / "gatekeeper-tmp")
    assert stat.S_IMODE(os.stat(seen["TMPDIR"]).st_mode) == 0o700


def test_m2_counter_check_without_cleaning_the_inherited_login_reaches_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arr_home: Path
) -> None:
    seen = call_env(tmp_path, monkeypatch, clean=False)
    assert seen["ANTHROPIC_BASE_URL"] == INHERITED["ANTHROPIC_BASE_URL"]
    assert seen["CLAUDE_CODE_OAUTH_TOKEN"] == INHERITED["CLAUDE_CODE_OAUTH_TOKEN"]


# --------------------------------------------------------------------------------------------
# M4: the Not-Aus of the supervising agent's profile ends a running call at once
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("blocks", [True, False], ids=["gesperrt", "Gegenprobe"])
async def test_m4_a_block_during_the_call_ends_it_and_its_answer_does_not_count(
    env: Env, monkeypatch: pytest.MonkeyPatch, blocks: bool
) -> None:
    monkeypatch.setattr(gatekeeper_module, "BLOCK_POLL_S", 0.02)
    blocked = [False]
    model = FakeModel(ALLOW_JSON, delay=2 if blocks else 0.1)
    inbox = env.inbox(model, is_blocked=lambda _name: blocked[0], timeout=0.3)
    task = asyncio.create_task(inbox.handle(host("arxiv.org")))
    await until(lambda: bool(model.prompts))
    started = asyncio.get_running_loop().time()
    if blocks:
        blocked[0] = True
    decision = await task
    if not blocks:
        assert decision.action is DecisionAction.ALLOW
        return
    assert asyncio.get_running_loop().time() - started < 1.0  # not the 2 s of the call
    assert decision.action is DecisionAction.DENY and model.finished == 0
    item = inbox.items()[-1]
    assert item.escalated_by is not None and "gesperrt (Not-Aus)" in item.escalated_by.note
    assert [x["outcome"] for x in lines(env)] == ["blocked"]


async def test_m4_a_block_right_at_the_answer_still_voids_it(env: Env) -> None:
    calls = [0]

    def is_blocked(_name: str) -> bool:
        calls[0] += 1
        return calls[0] > 1  # free before the call, blocked when its answer comes

    inbox = env.inbox(FakeModel(ALLOW_JSON), is_blocked=is_blocked, timeout=0.2)
    decision = await inbox.handle(host("arxiv.org"))
    assert decision.action is DecisionAction.DENY
    assert [x["outcome"] for x in lines(env)] == ["blocked"]


# --------------------------------------------------------------------------------------------
# G1: a package is a PyPI name, never a file
# --------------------------------------------------------------------------------------------

PIP_ENDINGS = [
    ".whl",
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz",
    ".tar.xz",
    ".txz",
    ".tlz",
    ".tar.lz",
    ".tar.lzma",
]


@pytest.mark.parametrize(
    "package",
    [
        *(f"paket{ending}" for ending in PIP_ENDINGS),
        *(f"Paket{ending.upper()}" for ending in PIP_ENDINGS),
        "paket.tbz[extra]",
        "paket.tar.lzma==1.0",
        "./paket",
        "../paket",
        "paket/",
        "work/paket.tbz",
        ".paket",
        "C:\\\\paket",
    ],
)
def test_g1_files_and_paths_are_no_package_names(env: Env, package: str) -> None:
    for command in (f"pip install '{package}'", f"python3 -m pip install '{package}'"):
        assessment = env.engine.assess(bash(command))
        found = mandate_check(assessment, parse_mandate(standard_text()).checks)
        assert not found.inside, command
        assert package.lower() not in found.packages


@pytest.mark.parametrize(
    "package", ["sympy", "python-flint", "mpmath==1.3.0", "scipy[all]", "zope.interface", "gmpy2"]
)
def test_g1_counter_check_pypi_names_pass(env: Env, package: str) -> None:
    found = mandate_check(
        env.engine.assess(bash(f"pip install '{package}'")), parse_mandate(standard_text()).checks
    )
    assert found.inside, found.note


# --------------------------------------------------------------------------------------------
# G3: no channel between agents through the supervising agent
# --------------------------------------------------------------------------------------------


async def test_g3_a_command_of_one_agent_never_reaches_the_prompt_for_another(env: Env) -> None:
    model = FakeModel(ALLOW_JSON, ALLOW_JSON, ALLOW_JSON)
    inbox = env.inbox(model, timeout=0.2)
    secret_bit = "https://arxiv.org/abs/1?nachricht=FUER-AGENT-B"
    await inbox.handle(bash(f"curl -s '{secret_bit}'"))  # explorer-1
    codex_host = host("pypi.org").model_copy(
        update={"agent_id": "codex-1", "provider": "codex", "tool": "commandExecution"}
    )
    await inbox.handle(codex_host)
    assert "FUER-AGENT-B" not in model.prompts[1]
    assert "explorer-1" not in model.prompts[1]
    await inbox.handle(host("arxiv.org"))  # counter-check: explorer-1 sees its own decision
    assert "FUER-AGENT-B" in model.prompts[2]


# --------------------------------------------------------------------------------------------
# G4: protections the review's mutations found untested
# --------------------------------------------------------------------------------------------


async def test_g17_the_budget_holds_with_parallel_requests(env: Env) -> None:
    spent = [0.0]
    model = FakeModel(*[ALLOW_JSON] * 10, delay=0.05, cost=0.4)
    inbox = env.inbox(
        model,
        budget_left=lambda: spent[0] < 0.8,
        charge=lambda usage: spent.__setitem__(0, spent[0] + (usage.cost_usd or 0)),
        timeout=0.3,
    )
    requests = [host("arxiv.org").model_copy(update={"tool_call_id": f"t{i}"}) for i in range(10)]
    await asyncio.gather(*(inbox.handle(r) for r in requests))
    assert len(model.prompts) == 2  # budget for two calls, not ten


async def test_g18_a_cancelled_call_is_counted_and_booked(env: Env) -> None:
    booked: list[Usage] = []
    model = FakeModel(ALLOW_JSON, delay=5)
    inbox = env.inbox(model, charge=booked.append, timeout=5)
    task = asyncio.create_task(inbox.handle(host("arxiv.org")))
    await until(lambda: bool(model.prompts))
    inbox.cancel_all()
    await asyncio.wait_for(task, 2)
    assert [x["outcome"] for x in lines(env)] == ["cancelled"] and len(booked) == 1


async def test_g19_a_failed_call_is_counted_and_booked(env: Env) -> None:
    class Failing(FakeModel):
        async def ask(self, system: str, prompt: str) -> Any:
            usage = Usage(input=7, output=1, cost_usd=0.01, cost_kind=CostKind.ESTIMATED)
            raise CallFailed("kaputt", usage)

    booked: list[Usage] = []
    inbox = env.inbox(Failing(), charge=booked.append, timeout=0.2)
    await inbox.handle(host("arxiv.org"))
    [line] = lines(env)
    assert line["outcome"] == "error" and line["usage"]["input"] == 7
    assert booked[0].cost_usd == 0.01


async def test_g20_an_answer_after_a_stop_decides_nothing(env: Env) -> None:
    holder: list[ApprovalInbox] = []

    class StoppedMeanwhile:
        async def screen(self, item: ApprovalItem, context: ScreenContext) -> Decision:
            holder[0].cancel(item.id, "Der Run wurde gestoppt.")  # the stop in the same step
            return Decision(action=DecisionAction.ALLOW, decided_by=DecidedBy.GATEKEEPER)

        def failed(self, item: ApprovalItem) -> Decision:
            raise AssertionError

    inbox = ApprovalInbox(env.engine, screener=StoppedMeanwhile(), timeout=1)
    holder.append(inbox)
    decision = await inbox.handle(host("arxiv.org"))
    assert decision.action is DecisionAction.DENY
    assert inbox.items()[-1].status is ApprovalStatus.OBSOLETE
    assert not [e for e in env.log() if e.get("event") == "decided"]


async def test_g22_withdrawing_an_installation_ends_its_pass(env: Env) -> None:
    model = FakeModel(ALLOW_JSON, ALLOW_JSON)
    inbox = env.inbox(model, timeout=0.2)
    await inbox.handle(bash("pip install sympy"))
    inbox.revoke(inbox.items()[-1].id)
    await inbox.handle(host("files.pythonhosted.org"))
    assert len(model.prompts) == 2  # asked again, not let through by the old pass


async def test_g23_the_record_per_profile_counts_calls_of_a_running_run(
    tmp_path: Path, arr_home: Path
) -> None:
    from run_kit import write_profiles
    from service_kit import make_run_folder, make_workfolder

    from arr.models import RunStatus
    from arr.service.real_api import RealApi

    write_profiles(arr_home, {"fake-auth": "claude_oauth", "aufsicht": "claude_oauth"})
    wf = make_workfolder(tmp_path / "wf")
    run_dir = make_run_folder(wf, status=RunStatus.INTERRUPTED)  # no usage.json
    usage = {"input": 300, "output": 20, "cache_read": None, "cache_write": None}
    line = {"ts": 1.0, "item_id": "apr_x", "profile": "aufsicht", "model": "m", "usage": usage}
    (run_dir / "gatekeeper.jsonl").write_text(json.dumps(line | {"outcome": "allow"}) + "\n")
    api = RealApi(wf, base_url="http://127.0.0.1:8765", scan=False)
    try:
        record = await asyncio.wait_for(api.get_profile_usage("aufsicht"), 10)
    finally:
        await api.close()
    [row] = record.runs
    assert row.input == 300 and row.agents == ["aufsichts-agent"]


def test_g26_to_g30_the_options_of_the_call(
    arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    model = ClaudeGatekeeperModel(
        model="claude-haiku-5-5", profile=AuthProfile(name="mac", kind=AuthKind.CLAUDE_OAUTH)
    )
    options = model.options(SYSTEM_PROMPT)
    assert options.tools == [] and options.allowed_tools == []
    assert set(DISALLOWED_TOOLS) <= set(options.disallowed_tools) and "Bash" in DISALLOWED_TOOLS
    assert options.setting_sources == []  # G27: no CLAUDE.md, hooks or settings
    assert options.strict_mcp_config is True and options.mcp_servers == {}  # G28
    assert options.plugins == [] and options.max_turns == 1
    assert options.system_prompt == SYSTEM_PROMPT  # no preset with tool texts
    assert options.permission_mode == "default" and options.can_use_tool is not None
    assert str(options.cwd) == str(app_dir() / "gatekeeper") != os.getcwd()  # G30
    assert not any((app_dir() / "gatekeeper").iterdir())


class FakeClient:
    """A stand-in for `ClaudeSDKClient` that plays fixed messages (G29)."""

    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages

    async def connect(self) -> None:
        pass

    async def query(self, prompt: str) -> None:
        pass

    async def receive_response(self) -> Any:
        for message in self.messages:
            yield message

    async def disconnect(self) -> None:
        pass


def _messages(init: dict[str, Any]) -> list[Any]:
    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

    return [
        SystemMessage(subtype="init", data=init),
        AssistantMessage(content=[TextBlock(text=ALLOW_JSON)], model="m"),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.001,
            usage={"input_tokens": 3, "output_tokens": 2},
        ),
    ]


@pytest.mark.parametrize(
    "init",
    [{"tools": ["Bash"], "mcp_servers": []}, {"tools": [], "mcp_servers": [{"name": "x"}]}],
)
async def test_g29_a_cli_that_reports_tools_or_mcp_at_its_start_is_stopped(
    arr_home: Path, monkeypatch: pytest.MonkeyPatch, init: dict[str, Any]
) -> None:
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    model = ClaudeGatekeeperModel(
        model="m",
        profile=AuthProfile(name="mac", kind=AuthKind.CLAUDE_OAUTH),
        client_factory=lambda _options: FakeClient(_messages(init)),
    )
    with pytest.raises(CallFailed, match="Werkzeuge"):
        await model.ask(SYSTEM_PROMPT, "Anfrage")
    clean = ClaudeGatekeeperModel(  # counter-check: without tools the same answer counts
        model="m",
        profile=AuthProfile(name="mac", kind=AuthKind.CLAUDE_OAUTH),
        client_factory=lambda _options: FakeClient(_messages({"tools": [], "mcp_servers": []})),
    )
    reply = await clean.ask(SYSTEM_PROMPT, "Anfrage")
    assert reply.text == ALLOW_JSON and reply.usage.cost_usd == 0.001
