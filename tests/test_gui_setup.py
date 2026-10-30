"""Workfolder, projects and settings in the GUI on `RealApi` (W3-04), with the clicks of the
tasks E1 and E3 of the UX acceptance test 3 (docs/08 §9.3): a click is a mouse click or a key
that triggers an action, typing does not count (§2.1)."""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from nicegui.testing.user import User
from run_kit import write_profiles
from service_kit import make_run_folder, make_workfolder
from test_gui import gui_user
from test_gui_real import page_html, see
from test_real_api_setup import fake_base_venv

from arr.models import RunStatus
from arr.service.mock_api import MockApi
from arr.service.real_api import RealApi
from arr.service.real_api import live as live_module
from arr.service.state import remember_workfolder
from arr.workfolder import init_workfolder, load_workfolder_settings

CLICK_LIMIT_E1 = 3
CLICK_LIMIT_E3 = 3


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return make_workfolder(tmp_path)


@asynccontextmanager
async def real_user(workfolder: Path) -> AsyncIterator[tuple[User, RealApi]]:
    api = RealApi(workfolder, base_url="http://test")
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await api.start()
        try:
            yield user, api
        finally:
            await api.close()


class Clicks:
    """Counts the clicks of a task."""

    def __init__(self, user: User) -> None:
        self.user = user
        self.count = 0

    def on(self, marker: str) -> None:
        self.count += 1
        self.user.find(marker=marker).click()


async def test_e1_new_workfolder_from_the_start_page(wf: Path, tmp_path: Path) -> None:
    target = tmp_path / "Test-UX3"
    async with real_user(wf) as (user, api):
        await user.open("/")
        await see(user, "Workfolder wechseln")
        clicks = Clicks(user)
        clicks.on("switch-workfolder")
        await see(user, "Neuen Workfolder anlegen")
        clicks.on("new-workfolder")
        await see(user, "Ein neuer oder leerer Ordner")
        user.find(marker="field-workfolder-path").trigger("input", str(target))
        clicks.on("form-confirm")
        for _ in range(100):
            if api.state.workfolder == target:
                break
            await asyncio.sleep(0.02)
        assert api.state.workfolder == target
        assert clicks.count <= CLICK_LIMIT_E1
        for name in ("arr.yaml", "projects", "skills", "runs"):
            assert (target / name).exists()
        await user.open("/")
        html = await see(user, "Test-UX3")
        assert str(target) in html and "0 Projekte" in html


async def test_foreign_folder_shows_the_message_at_the_field(wf: Path, tmp_path: Path) -> None:
    target = tmp_path / "fremd"
    target.mkdir()
    (target / "datei.txt").write_text("Meins")
    async with real_user(wf) as (user, api):
        await user.open("/?workfolder=neu")
        await see(user, "Ein neuer oder leerer Ordner")
        user.find(marker="field-workfolder-path").trigger("input", str(target))
        user.find(marker="form-confirm").click()
        html = await see(user, "liegen schon andere Dateien")
        assert "Dieser Ordner geht nicht." in html
        assert sorted(p.name for p in target.iterdir()) == ["datei.txt"]
        assert api.state.workfolder == wf


async def test_exactly_one_base_venv_is_offered_for_a_new_workfolder(
    wf: Path, tmp_path: Path
) -> None:
    venv = fake_base_venv("mathe")
    target = tmp_path / "mit-python"
    async with real_user(wf) as (user, _api):
        await user.open("/?workfolder=neu")
        await see(user, "Python-Umgebung „mathe“ für die Agenten nutzen")
        user.find(marker="field-workfolder-path").trigger("input", str(target))
        user.find(marker="form-confirm").click()
        for _ in range(100):
            if (target / "arr.yaml").exists():
                break
            await asyncio.sleep(0.02)
    assert load_workfolder_settings(target).toolchain.python == str(venv)


async def test_no_offer_with_two_base_venvs(wf: Path) -> None:
    fake_base_venv("mathe")
    fake_base_venv("zahlen")
    async with real_user(wf) as (user, _api):
        await user.open("/?workfolder=neu")
        html = await see(user, "Ein neuer oder leerer Ordner")
        assert "für die Agenten nutzen" not in html


async def test_switch_to_a_known_workfolder_reaches_the_open_pages(
    wf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = init_workfolder(tmp_path / "zweiter")
    (other / "projects" / "demo").mkdir()
    (other / "arr.yaml").write_text("version: 1\ntoolchain: {run_venv: off}\n")
    make_run_folder(other, status=RunStatus.DONE, name="Im zweiten Workfolder")
    moved: list[bool] = []
    monkeypatch.setattr(live_module, "show_new_workfolder", lambda: moved.append(True))
    remember_workfolder(other)
    async with real_user(wf) as (user, api):
        await user.open("/")
        user.find(marker="switch-workfolder").click()
        await see(user, str(other))
        user.find(marker="workfolder-zweiter").trigger("change")
        user.find(marker="dialog-confirm").click()
        for _ in range(100):
            if moved:
                break
            await asyncio.sleep(0.02)
        assert moved and api.state.workfolder == other
        await user.open("/runs")
        await see(user, "Im zweiten Workfolder")


async def test_e3_project_with_all_checks_and_repair(wf: Path, tmp_path: Path) -> None:
    target = init_workfolder(tmp_path / "Test-UX3")
    async with real_user(target) as (user, _api):
        await user.open("/projekte")
        html = await see(user, "Noch keine Projekte")
        await user.open("/projekte?neu=1")
        clicks = Clicks(user)
        await see(user, "arr legt an:")
        user.find(marker="field-new-name").trigger("input", "ux3-collatz")
        clicks.on("form-confirm")
        html = await see(user, "CLAUDE.md mit arr-Block vorhanden")
        assert ".mcp.json vorhanden" in html and "Skill-Links" in html
        assert "eingerichtet" in html and "unvollständig" not in html
        folder = target / "projects" / "ux3-collatz"
        assert (folder / ".mcp.json").is_file()
        assert clicks.count + 1 <= CLICK_LIMIT_E3  # + „＋ Neues Projekt“

        (folder / ".mcp.json").unlink()
        await user.open("/projekte/ux3-collatz")
        html = await see(user, ".mcp.json fehlt")
        assert "unvollständig" in html and "Reparieren" in html
        user.find(marker="repair").click()
        await see(user, "„ux3-collatz“ repariert.")
        assert (folder / ".mcp.json").is_file()
        await see(user, ".mcp.json vorhanden")


async def test_existing_folder_is_set_up_in_the_dialog(wf: Path) -> None:
    folder = wf / "projects" / "Meins"
    folder.mkdir()
    async with real_user(wf) as (user, _api):
        await user.open("/projekte?neu=ordner")
        await see(user, "arr ergänzt nur, was fehlt")
        user.find(marker="field-existing-path").trigger("input", str(folder))
        user.find(marker="form-confirm").click()
        await see(user, "„Meins“ repariert.")
    assert (folder / "CLAUDE.md").is_file()


async def test_settings_page_saves_and_removes_a_rule(wf: Path) -> None:
    file = wf / "arr.yaml"
    file.write_text(
        "# meine Zeile\nversion: 1\ntoolchain: {run_venv: off}\n"
        "permissions: {always: [{action: allow, hosts: [pypi.org]}]}\n"
    )
    async with real_user(wf) as (user, api):
        await user.open("/einstellungen")
        html = await see(user, "pypi.org")
        assert "Freigaben und Vollmacht" in html and "kommt mit Auftrag W4-04" in html
        assert "Standards und Limits" in html and "Aufräumregeln" in html
        user.find(marker="save-approvals").click()
        await see(user, "Gespeichert: Freigaben")
        assert file.read_text().startswith("# meine Zeile\n")
        user.find(marker="remove-rule").click()
        await see(user, "Regel entfernt: pypi.org")
        assert "pypi.org" not in file.read_text()
        assert not [r for r in await api.list_fixed_rules() if r.removable]


async def test_notice_stands_on_start_and_settings(
    wf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (wf / "arr.yaml").write_text("version: 1\n")
    home = tmp_path / "home"
    (home / "uv").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "base_prefix", str(home / "uv"))
    async with real_user(wf) as (user, _api):
        await user.open("/")
        html = await see(user, "Die Codex-Sandbox sperrt diesen Ordner")
        assert "/einstellungen#python" in html and "Python-Umgebung wählen" in html
        await user.open("/einstellungen")
        await see(user, "Die Codex-Sandbox sperrt diesen Ordner")


async def test_mock_keeps_its_prototype_flow() -> None:
    async with gui_user(MockApi()) as user:
        await user.open("/")
        user.find(marker="switch-workfolder").click()
        user.find(marker="new-workfolder").click()
        await see(user, "Ein neuer oder leerer Ordner")
        user.find(marker="field-workfolder-path").trigger("input", "~/Test-UX3")
        user.find(marker="form-confirm").click()
        html = await see(user, "Workfolder „Test-UX3“ geöffnet.")
        assert "Im Prototyp" in html and page_html(user)
