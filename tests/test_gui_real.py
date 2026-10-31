"""All GUI pages with `RealApi` on a test workfolder (W3-01 A8): every page loads without an
exception; pages of the areas that later orders fill in show the message of the
`ApiException` instead of crashing (docs/08 §8.4)."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from nicegui.element import Element
from nicegui.testing.user import User
from run_kit import FAST, done, fake_builders, post, room, say, turns, write_profiles
from service_kit import hold_lock, make_run_folder, make_workfolder
from test_gui import gui_user

from arr.models import RunStatus
from arr.runs.launch import plan_run_input
from arr.runs.launch import run_launch as launch_run
from arr.service.real_api import RealApi

BASE = "http://test"


def _walk(element: Element) -> Iterator[Element]:
    yield element
    for slot in element.slots.values():
        for child in slot.children:
            yield from _walk(child)


def page_html(user: User) -> str:
    with user.client:
        parts = []
        for node in _walk(user.client.layout):
            for value in (getattr(node, "content", None), getattr(node, "text", None)):
                if isinstance(value, str) and value:
                    parts.append(value)
            for key, value in node._props.items():
                if key.startswith("data-"):
                    parts.append(f'{key}="{value}"')
        return "\n".join(parts)


async def see(user: User, text: str, retries: int = 60) -> str:
    html = ""
    for _ in range(retries):
        html = page_html(user)
        if text in html:
            return html
        await asyncio.sleep(0.05)
    raise AssertionError(f"„{text}“ not on the page:\n{html[:3000]}")


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    workfolder = make_workfolder(tmp_path)
    group = workfolder / "skills" / "runde"
    group.mkdir(parents=True)
    (group / "SKILL.md").write_text("---\nname: runde\ndescription: Eine Runde.\n---\n# Runde\n")
    (group / "group.yaml").write_text(
        "kind: research-group\nversion: 1\ntopology: roundtable\n"
        "params:\n  rounds: {default: 2, min: 1, max: 5, doc: Runden}\n"
        "agents:\n  - {id: m-1}\n  - {id: m-2}\n"
        "groupchats:\n  - {name: main, members: [m-1, m-2]}\n"
    )
    return workfolder


@pytest.fixture
def finished(wf: Path) -> str:
    """A finished FakeBackend run with a message, a result and a rating."""
    from arr.models import RunInput
    from arr.resolve import validate_model

    scripts = {
        "a": turns(
            [
                say("Hallo"),
                post("main", "Idee 1", kind="idea"),
                room("submit_result", kind="idea", title="Idee 1", body="n zerlegen"),
                done(),
            ]
        ),
        "b": turns([room("submit_rating", result_id=1, score=7, comment="ok"), done()]),
    }
    run_input = validate_model(
        RunInput,
        {
            "project": "demo",
            "name": "Fertiger Lauf",
            "task": "Teste arr.",
            "topology": {"kind": "roundtable", "params": {"rounds": 1, "channel": "main"}},
            "defaults": {"provider": "claude", "model": "fake-model", "auth": "fake-auth"},
            "agents": [{"id": "a"}, {"id": "b"}],
            "groupchats": [{"name": "main", "members": ["a", "b"], "mode": "rounds"}],
        },
    )
    launch = plan_run_input(run_input, workfolder=wf)
    result = asyncio.run(
        launch_run(
            launch,
            decider=None,
            report=lambda line: None,
            builders=fake_builders(scripts),
            signals=False,
            pacing=FAST,
        )
    )
    assert result.status is RunStatus.DONE
    return result.run_id


@asynccontextmanager
async def real_user(wf: Path) -> AsyncIterator[tuple[User, RealApi]]:
    api = RealApi(wf, base_url=BASE)
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await api.start()
        try:
            yield user, api
        finally:
            await api.close()


async def test_every_page_loads_with_real_data(wf: Path, finished: str) -> None:
    running_dir = make_run_folder(wf, agents=["x", "y"], status=RunStatus.RUNNING, name="Läuft")
    running = running_dir.name
    lock = hold_lock(running_dir)
    async with real_user(wf) as (user, _api):
        pages = [
            "/",
            "/runs",
            "/runs?status=aktiv",
            "/runs?status=fertig&projekt=demo",
            f"/runs/{finished}",
            f"/runs/{finished}?ansicht=ergebnisse",
            f"/runs/{finished}?agent=a",
            f"/runs/{finished}?kanal=main",
            f"/runs/{running}",
            f"/runs/{running}?agent=x",
            "/runs/gibt-es-nicht",
            "/projekte",
            "/projekte/demo",
            "/skills",
            "/skills/runde",
            "/neuer-run",
            "/neuer-run?skill=runde&projekt=demo",
            "/freigaben",
            "/freigaben?ansicht=verlauf",
            "/profile",
            "/usage",
            "/speicher",
            "/einstellungen",
            "/einrichtung",
        ]
        for path in pages:
            await user.open(path)
            html = page_html(user)
            assert "Traceback" not in html and "Internal Server Error" not in html, path
            if path != "/einrichtung":
                await user.should_see(marker="nav")
        await user.open("/runs")
        html = await see(user, "Fertiger Lauf")
        assert "Läuft" in html and 'data-chip="active"' in html
        await user.open(f"/runs/{finished}")
        html = await see(user, "Fertiger Lauf")
        assert "Idee 1" in html
        await user.open("/")
        html = await see(user, "Läuft")
        assert "1 aktiv" in html
    lock.release()


async def test_pages_of_later_orders_show_the_message_instead_of_crashing(wf: Path) -> None:
    async with real_user(wf) as (user, _api):
        for path, order in [
            ("/usage", "W5-01"),
            ("/speicher", "W5-01"),
            ("/einstellungen", "W3-04"),
        ]:
            await user.open(path)
            html = await see(user, "kommt mit Auftrag")
            assert order in html, path
            assert "Traceback" not in html
        # profiles are listed (catalog, read only); testing and logins come with W3-05
        await user.open("/profile")
        html = await see(user, "fake-auth")
        assert "Traceback" not in html
