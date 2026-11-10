"""Shared parts of the W4-01 tests: a workfolder with a group skill and a project, a `RealApi`
with FakeBackend builders, and helpers to wait for a run. No model, no real login."""

import asyncio
import time
from pathlib import Path
from typing import Any

from run_kit import FAST, done, fake_builders, say, turns

from arr.models import RunStatus
from arr.service.real_api import RealApi

BASE = "http://abcdefghij0123456789.localhost:8765"
DEFAULTS = {"provider": "claude", "model": "fake-model", "auth": "fake-auth"}
SKILL_MD = "---\nname: {name}\ndescription: Eine Runde zum Testen.\n---\n# Runde\n"
GROUP_YAML = """kind: research-group
version: 1
topology: roundtable
params:
  rounds: { default: 1, min: 1, max: 4, doc: "Runden" }
limits:
  wall_time: 20m
  turns_per_agent: 6
  budget_usd: 2
agents:
  - id: m
    count: 2
    role: roles/m.md
  - id: critic
    role: roles/critic.md
groupchats:
  - name: main
    members: [m-*, critic]
    mode: rounds
"""
ARR_YAML = """version: 1
toolchain: {run_venv: off}
defaults:
  claude: {model: fake-model, auth: fake-auth}
limits:
  wall_time: 1h
  turns_per_agent: 10
  budget_usd: 5
"""


def group_skill(wf: Path, name: str = "runde", group_yaml: str = GROUP_YAML) -> Path:
    folder = wf / "skills" / name
    (folder / "roles").mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(SKILL_MD.format(name=name), "utf-8")
    (folder / "roles" / "m.md").write_text("# Rolle m\n", "utf-8")
    (folder / "roles" / "critic.md").write_text("# Rolle critic\n", "utf-8")
    (folder / "group.yaml").write_text(group_yaml, "utf-8")
    return folder


def prepare_workfolder(wf: Path) -> Path:
    """The workfolder of `service_kit.make_workfolder` with standards and the skill `runde`."""
    (wf / "arr.yaml").write_text(ARR_YAML, "utf-8")
    group_skill(wf)
    return wf


SCRIPTS: dict[str, Any] = {
    "m-1": turns([say("Ich prüfe."), done()]),
    "m-2": turns([say("Ich auch."), done()]),
    "critic": turns([say("Passt."), done()]),
}


def make_api(wf: Path, scripts: dict[str, Any] | None = None, **kwargs: Any) -> RealApi:
    return RealApi(
        wf,
        base_url=BASE,
        builders=fake_builders(scripts or SCRIPTS),
        manager_kwargs={"pacing": FAST},
        **kwargs,
    )


def spec(**changes: Any) -> dict[str, Any]:
    """A request of Claude Code in the project `demo` (as `user_api` builds it)."""
    return {
        "project": "demo",
        "task": "Prüfe Lemma 3.",
        "skill": "runde",
        "params": {},
        "overrides": {},
        **changes,
    }


async def until(check: Any, within: float = 20.0) -> Any:
    end = time.monotonic() + within
    while time.monotonic() < end:
        value = await check() if asyncio.iscoroutinefunction(check) else check()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def until_final(api: RealApi, run_id: str, within: float = 30.0) -> RunStatus:
    async def final() -> RunStatus | None:
        status = (await api.get_run(run_id)).run.status
        return status if status.is_final else None

    return await until(final, within)


# --------------------------------------------------------------------------------------------
# A service in the test process: Guard + `/mcp/` + RealApi on uvicorn, and the clients
# --------------------------------------------------------------------------------------------


class Service:
    """The service parts that matter for `/mcp/`, on a free port of `127.0.0.1`.

    The app is the real `Guard` around a Starlette app with the real mount (`mount_mcp`); a GUI
    route `/` stands in for NiceGUI, so the guard's GUI checks can be compared. `service.json`
    names this process, so `arr mcp` finds it like the real service.
    """

    def __init__(self, api: RealApi, *, write_info: bool = True) -> None:
        from service_kit import free_port

        from arr.service.guard import new_hostname, new_token

        self.api = api
        self.port = free_port()
        self.hostname = new_hostname()
        self.token = new_token()
        self.write_info = write_info
        api.state.base_url = self.origin  # `gui_url`s name this service, as in `arr serve`
        self._server: Any = None
        self._task: Any = None
        self.mounted: Any = None

    @property
    def host(self) -> str:
        return f"{self.hostname}:{self.port}"

    @property
    def origin(self) -> str:
        return f"http://{self.host}"

    def app(self) -> Any:
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        from arr.service.guard import Guard
        from arr.service.user_api import mount_mcp

        inner = Starlette(routes=[Route("/", lambda _r: PlainTextResponse("GUI-Daten"))])
        self.mounted = mount_mcp(inner, self.api, hostname=self.hostname, port=self.port)
        return Guard(inner, token=self.token, port=self.port, hostname=self.hostname)

    async def __aenter__(self) -> "Service":
        import os

        import uvicorn

        from arr.service.state import ServiceInfo, write_service_info

        app = self.app()
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        await until(lambda: self._server.started, within=10)
        await self.mounted.start()
        if self.write_info:
            write_service_info(
                ServiceInfo(
                    host=self.hostname,
                    port=self.port,
                    token=self.token,
                    pid=os.getpid(),
                    workfolder=str(self.api.state.workfolder),
                )
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.api.stop_runs()
        await self.mounted.stop()
        self._server.should_exit = True
        await self._task

    def http(self, **headers: str) -> Any:
        """An `httpx2.AsyncClient` to `/mcp/` with the given headers (nothing added)."""
        import httpx2

        return httpx2.AsyncClient(
            base_url=f"http://127.0.0.1:{self.port}", headers=headers, timeout=30.0
        )

    def headers(self, folder: Path, **extra: str) -> dict[str, str]:
        from urllib.parse import quote

        return {
            "Host": self.host,
            "Authorization": f"Bearer {self.token}",
            "x-arr-project-dir": quote(str(folder)),
            **extra,
        }

    def client(self, folder: Path) -> Any:
        """An MCP client over HTTP as `arr mcp` connects (without the stdio part)."""
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        http = httpx2.AsyncClient(
            headers=self.headers(folder), timeout=httpx2.Timeout(10, read=700)
        )
        url = f"http://127.0.0.1:{self.port}/mcp/"
        return _Owned(Client(streamable_http_client(url, http_client=http)), http)


class _Owned:
    """An MCP client that closes its HTTP client too."""

    def __init__(self, client: Any, http: Any) -> None:
        self.client, self.http = client, http

    async def __aenter__(self) -> Any:
        return await self.client.__aenter__()

    async def __aexit__(self, *exc: Any) -> None:
        await self.client.__aexit__(*exc)
        await self.http.aclose()


def result_data(result: Any) -> dict[str, Any]:
    """The answer of a tool call as data (`is_error` → `{"error": text}`)."""
    import json

    text = "".join(getattr(c, "text", "") for c in result.content)
    if result.is_error:
        return {"error": text}
    if result.structured_content is not None:
        data = result.structured_content
        return data.get("result", data) if set(data) == {"result"} else data
    return json.loads(text)
