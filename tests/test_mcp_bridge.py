"""W4-01 A1-A4: `arr mcp` over stdio, `/mcp/` behind the guard, the tools of docs/04 §2.

A real service part in the test process (`mcp_kit.Service`: the real `Guard`, the real mount,
`RealApi` with FakeBackend) and real MCP clients of the `mcp` package: over HTTP like the
bridge, and over stdio through the real `arr mcp`. No model, no real login.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp_kit import Service, make_api, prepare_workfolder, result_data, until
from run_kit import write_profiles
from service_kit import make_workfolder

from arr.permissions.types import ApprovalStatus, DecisionAction
from arr.service.api_models import DecisionRequest
from arr.service.mcp_bridge import NOT_RUNNING
from arr.service.user_api import tool_names


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return prepare_workfolder(make_workfolder(tmp_path))


def project(wf: Path) -> Path:
    return wf / "projects" / "demo"


async def call(service: Service, folder: Path, tool: str, **arguments: Any) -> dict[str, Any]:
    async with service.client(folder) as client:
        return result_data(await client.call_tool(tool, arguments))


# --------------------------------------------------------------------------------------------
# A1: the tools, the project, the bridge over stdio
# --------------------------------------------------------------------------------------------


async def test_the_service_offers_all_tools_of_04(wf: Path) -> None:
    async with Service(make_api(wf)) as service, service.client(project(wf)) as client:
        names = [t.name for t in (await client.list_tools()).tools]
    assert names == tool_names()
    assert set(names) == {
        "list_skills",
        "describe_skill",
        "estimate_run",
        "start_run",
        "list_runs",
        "run_status",
        "wait_run",
        "read_run_summary",
        "read_results",
        "read_groupchat",
        "read_transcript",
        "post_to_run",
        "control_run",
        "list_approvals",
    }


async def test_reading_tools_work_in_the_project(wf: Path) -> None:
    async with Service(make_api(wf)) as service:
        skills = await call(service, project(wf), "list_skills")
        assert [s["name"] for s in skills["skills"]] == ["runde"]
        skill = await call(service, project(wf), "describe_skill", name="runde")
        assert skill["check"]["valid"] is True
        assert skill["skill_md"].startswith("---")
        assert [p["name"] for p in skill["params"]] == ["rounds"]
        estimate = await call(
            service,
            project(wf) / "tex",
            "estimate_run",
            skill="runde",
            overrides={"agents": {"critic": {"tools": {"web": True}}}},
        )
        assert [a["id"] for a in estimate["agents"]] == ["m-1", "m-2", "critic"]
        assert estimate["estimate"]["cost_basis"] == "budget"
        assert estimate["deviations"][0]["label"] == "Web-Zugriff an"
        assert estimate["capacity"][0]["profile"] == "fake-auth"
        assert not list((wf / "runs").glob("*/meta.json")), "an estimate reserves nothing"


async def test_a_folder_outside_the_projects_gets_a_clear_message(wf: Path, tmp_path: Path) -> None:
    async with Service(make_api(wf)) as service:
        for folder in (tmp_path, wf, wf / "skills"):
            answer = await call(service, folder, "list_skills")
            assert "kein Projekt des Workfolders" in answer["error"]
            assert str(tmp_path) not in answer["error"], "no absolute path"


async def test_a_run_of_another_project_is_not_found(wf: Path) -> None:
    other = wf / "projects" / "anderes"
    other.mkdir()
    async with Service(make_api(wf)) as service:
        started = await call(service, project(wf), "start_run", task="Prüfe.", skill="runde")
        for tool, args in (
            ("run_status", {}),
            ("wait_run", {"timeout_s": 1}),
            ("read_groupchat", {"channel": "main"}),
            ("control_run", {"action": "stop"}),
        ):
            answer = await call(service, other, tool, run_id=started["run_id"], **args)
            assert "gibt es in diesem Projekt nicht" in answer["error"], tool
        assert (await call(service, other, "list_approvals"))["open"] == []


def bridge_params(folder: Path, arr_home: Path) -> Any:
    from mcp import StdioServerParameters

    arr = Path(sys.executable).with_name("arr")
    env = {
        "ARR_HOME": str(arr_home),
        "HOME": str(arr_home.parent),
        "PATH": os.environ.get("PATH", ""),
        "LANG": "de_DE.UTF-8",
    }
    return StdioServerParameters(command=str(arr), args=["mcp"], env=env, cwd=str(folder))


async def test_arr_mcp_over_stdio_forwards_the_tools(wf: Path, arr_home: Path) -> None:
    from mcp import Client

    async with Service(make_api(wf)) as service:
        params = bridge_params(project(wf), arr_home)
        assert service.token not in json.dumps(params.model_dump()), "token never in argv or env"
        async with Client(params) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            assert names == tool_names()
            skills = result_data(await client.call_tool("list_skills", {}))
            started = result_data(
                await client.call_tool("start_run", {"task": "Prüfe Lemma 3.", "skill": "runde"})
            )
        assert [s["name"] for s in skills["skills"]] == ["runde"]
        assert started["status"] == "awaiting_approval"
        assert started["gui_url"].startswith(f"http://{service.host}/freigaben/apr_")
        assert service.token not in json.dumps(started)


async def test_arr_mcp_says_when_arr_is_not_running(wf: Path, arr_home: Path) -> None:
    from mcp import Client

    params = bridge_params(project(wf), arr_home)
    assert not (arr_home / "service.json").exists()
    async with Client(params) as client:
        names = [t.name for t in (await client.list_tools()).tools]
        answer = result_data(await client.call_tool("list_runs", {}))
    assert names == tool_names(), "the tools are there while arr is not running"
    assert answer == {"error": NOT_RUNNING}
    # a stale service.json (process gone) and a port nobody answers say the same
    from arr.service.state import ServiceInfo, write_service_info

    stale = ServiceInfo(host="x.localhost", port=9, token="t", pid=999_999, workfolder="/")
    write_service_info(stale)
    async with Client(params) as client:
        assert result_data(await client.call_tool("list_runs", {})) == {"error": NOT_RUNNING}


async def test_arr_mcp_outside_a_project_says_so(wf: Path, arr_home: Path, tmp_path: Path) -> None:
    from mcp import Client

    elsewhere = tmp_path / "irgendwo"
    elsewhere.mkdir()
    async with Service(make_api(wf)), Client(bridge_params(elsewhere, arr_home)) as client:
        answer = result_data(await client.call_tool("list_runs", {}))
    assert "kein Projekt des Workfolders" in answer["error"]


def _seen_by_ps(folder: Path, arr_home: Path) -> str:
    """Start `arr mcp` and read its arguments and environment like another process can."""
    import time

    arr = Path(sys.executable).with_name("arr")
    env = {"ARR_HOME": str(arr_home), "PATH": os.environ.get("PATH", "")}
    process = subprocess.Popen(
        [str(arr), "mcp"],
        cwd=folder,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.0)
        return subprocess.run(
            ["/bin/ps", "-E", "-ww", "-o", "command=", "-p", str(process.pid)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    finally:
        process.kill()  # by its PID (Arbeitsweise §4 Regel 10)
        process.wait(timeout=10)


@pytest.mark.macos
async def test_the_token_is_not_in_the_bridge_process(wf: Path, arr_home: Path) -> None:
    """`KERN_PROCARGS2` (SECURITY.md 5.3): what `ps` sees of `arr mcp` holds no token."""
    async with Service(make_api(wf)) as service:
        seen = await asyncio.to_thread(_seen_by_ps, project(wf), arr_home)
    assert "mcp" in seen and str(arr_home) in seen, "ps shows argv and environment"
    assert service.token not in seen


def test_the_project_template_has_no_token() -> None:
    from arr.workfolder import mcp_entry

    entry = mcp_entry()
    assert entry["args"] == ["mcp"]
    assert "env" not in entry and "token" not in json.dumps(entry).lower()
    assert Path(entry["command"]).name == "arr"


# --------------------------------------------------------------------------------------------
# A2: `/mcp/` only with the token; the GUI check stays as strict
# --------------------------------------------------------------------------------------------

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


async def post_init(service: Service, headers: dict[str, str]) -> Any:
    async with service.http(**ACCEPT, **headers) as http:
        return await http.post("/mcp/", json=INIT)


async def test_mcp_only_with_the_token(wf: Path) -> None:
    from arr.service.guard import COOKIE

    async with Service(make_api(wf), write_info=False) as service:
        good = {"Host": service.host, "Authorization": f"Bearer {service.token}"}
        answer = await post_init(service, good)
        assert answer.status_code == 200, "Gegenprobe: with the token the server answers"
        assert "serverInfo" in answer.text
        refused = {
            "falscher Host": {**good, "Host": f"127.0.0.1:{service.port}"},
            "localhost": {**good, "Host": f"localhost:{service.port}"},
            "anderer Name": {**good, "Host": f"attacker.localhost:{service.port}"},
            "fremder Origin": {**good, "Origin": "http://evil.example"},
            "Origin null": {**good, "Origin": "null"},
            "kein Token": {"Host": service.host},
            "falsches Token": {**good, "Authorization": "Bearer falsch"},
            "Token ohne Bearer": {**good, "Authorization": service.token},
        }
        for case, headers in refused.items():
            answer = await post_init(service, headers)
            assert answer.status_code == 403, case
            assert "serverInfo" not in answer.text and len(answer.content) < 40, case
        cookie = {"Host": service.host, "Cookie": f"{COOKIE}={service.token}"}
        cookie_only = await post_init(service, cookie)
        assert cookie_only.status_code == 403, "a cookie is not enough"
        own_origin = await post_init(service, {**good, "Origin": service.origin})
        assert own_origin.status_code == 200


async def test_the_gui_check_is_not_weaker(wf: Path) -> None:
    from arr.service.guard import COOKIE

    async with Service(make_api(wf), write_info=False) as service:
        bearer = {"Host": service.host, "Authorization": f"Bearer {service.token}"}
        async with service.http(**bearer) as http:
            assert (await http.get("/")).status_code == 401, "the bearer token opens no GUI page"
            assert (await http.post("/")).status_code == 401
        cookie = f"{COOKIE}={service.token}"
        async with service.http(Host=service.host, Cookie=cookie) as http:
            answer = await http.get("/")
            assert answer.status_code == 200 and answer.text == "GUI-Daten"
            foreign = await http.get("/", headers={"Origin": "http://evil.example"})
            assert foreign.status_code == 403
        async with service.http(Host=f"127.0.0.1:{service.port}", Cookie=cookie) as http:
            assert (await http.get("/")).status_code == 403


async def test_a_websocket_to_mcp_is_refused(wf: Path) -> None:
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    async with Service(make_api(wf), write_info=False) as service:
        headers = {"Authorization": f"Bearer {service.token}"}
        with pytest.raises(InvalidStatus) as caught:
            async with connect(
                f"ws://127.0.0.1:{service.port}/mcp/",
                additional_headers={**headers, "Host": service.host},
            ):
                pass
        assert caught.value.response.status_code == 403


# --------------------------------------------------------------------------------------------
# A3: start_run → awaiting_approval; wait_run returns on approval, adjustment and refusal
# --------------------------------------------------------------------------------------------


async def _start_and_wait(
    service: Service, folder: Path, decide: Any, **start: Any
) -> tuple[dict, dict]:
    started = await call(
        service, folder, "start_run", task="Prüfe Lemma 3.", skill="runde", **start
    )

    async def waiting() -> dict[str, Any]:
        return await call(service, folder, "wait_run", run_id=started["run_id"], timeout_s=30)

    task = asyncio.create_task(waiting())
    await asyncio.sleep(0.5)
    assert not task.done(), "wait_run waits for the decision"
    items = await service.api.list_approvals(status=[ApprovalStatus.OPEN])
    await decide(items[0])
    return started, await asyncio.wait_for(task, 20)


async def test_start_run_creates_an_item_and_a_notification(wf: Path) -> None:
    sent: list[str] = []
    api = make_api(wf, notify=lambda text, title: sent.append(text))
    await api.start()
    try:
        async with Service(api) as service:
            started = await call(service, project(wf), "start_run", task="Prüfe.", skill="runde")
            assert started["status"] == "awaiting_approval"
            status = await call(service, project(wf), "run_status", run_id=started["run_id"])
            assert status["gui_url"] == started["gui_url"]
            approvals = await call(service, project(wf), "list_approvals")
            assert [a["gui_url"] for a in approvals["open"]] == [started["gui_url"]]
            await until(lambda: sent, within=10)
    finally:
        await api.close()
    assert sent == ["Run wartet auf Freigabe – runde in demo (von Claude Code)"]


async def test_wait_run_returns_on_approval(wf: Path) -> None:
    async with Service(make_api(wf)) as service:

        async def allow(item: Any) -> None:
            await service.api.decide_approval(item.id, DecisionRequest(action=DecisionAction.ALLOW))

        started, waited = await _start_and_wait(service, project(wf), allow)
        assert waited["changed"] and waited["status"] in ("queued", "preparing", "running", "done")
        assert waited["adjusted_by_user"] is False
        assert waited["run_id"] == started["run_id"]


async def test_wait_run_returns_on_adjustment_with_the_changes(wf: Path) -> None:
    async with Service(make_api(wf)) as service:

        async def adjust(item: Any) -> None:
            request = DecisionRequest(
                action=DecisionAction.ADJUST, config={"agents.critic.tools.web": False}
            )
            await service.api.decide_approval(item.id, request)

        started, waited = await _start_and_wait(
            service,
            project(wf),
            adjust,
            overrides={"agents": {"critic": {"tools": {"web": True}}}},
        )
        assert waited["run_id"] == started["run_id"], "the run ID stays"
        assert waited["adjusted_by_user"] is True
        assert waited["changes"] == ["Web critic: an → aus"]


async def test_wait_run_returns_on_refusal_with_the_message(wf: Path) -> None:
    async with Service(make_api(wf)) as service:

        async def deny(item: Any) -> None:
            request = DecisionRequest(action=DecisionAction.DENY, comment="Erst Lemma 2.")
            await service.api.decide_approval(item.id, request)

        _started, waited = await _start_and_wait(service, project(wf), deny)
        assert waited["status"] == "stopped" and waited["status_text"] == "Gestoppt"
        assert waited["message"] == "Der User hat den Run-Start abgelehnt: Erst Lemma 2."


async def test_wait_run_ends_at_its_timeout(wf: Path) -> None:
    async with Service(make_api(wf)) as service:
        started = await call(service, project(wf), "start_run", task="Prüfe.", skill="runde")
        waited = await call(service, project(wf), "wait_run", run_id=started["run_id"], timeout_s=1)
    assert waited["changed"] is False and waited["status"] == "awaiting_approval"
    assert 0.9 <= waited["waited_s"] <= 3 and "next" in waited


async def test_an_invalid_start_says_why_and_reserves_nothing(wf: Path) -> None:
    async with Service(make_api(wf)) as service:
        answers = [
            await call(service, project(wf), "start_run", task="x", skill="gibtsnicht"),
            await call(service, project(wf), "start_run", task="", skill="runde"),
            await call(
                service,
                project(wf),
                "start_run",
                task="x",
                skill="runde",
                overrides={"agents": {"niemand": {"model": "x"}}},
            ),
            await call(
                service,
                project(wf),
                "start_run",
                task="x",
                skill="runde",
                overrides={"topology": {"kind": "solo"}},
            ),
        ]
    assert all("error" in a for a in answers)
    assert "niemand" in answers[2]["error"] and "topology" in answers[3]["error"]
    assert not list((wf / "runs").glob("*/meta.json"))
