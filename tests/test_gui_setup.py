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
from run_kit import FAST, done, fake_builders, say, turns, write_profiles
from service_kit import hold_lock, make_run_folder, make_workfolder
from test_gui import gui_user
from test_gui_real import page_html, see
from test_real_api_setup import fake_base_venv

from arr.models import RunStatus
from arr.service.api_models import SLUG as API_SLUG
from arr.service.gui.pages.projekte import SLUG
from arr.service.mock_api import MockApi
from arr.service.real_api import RealApi
from arr.service.real_api import live as live_module
from arr.service.real_api.runs import SOLO_AGENT
from arr.service.state import read_app_settings, remember_workfolder, write_app_settings
from arr.skills import START_SKILLS
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
        assert "Freigaben und Vollmacht" in html and "Deine Vollmacht" in html  # W4-04
        assert "Die Vollmacht ist noch nicht angelegt." in html
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
        html = await see(user, "<b>Das Python von arr startet nicht für Codex-Agenten.</b>")
        assert "/einstellungen#python" in html and "Python-Umgebung wählen" in html
        await user.open("/einstellungen")
        await see(user, "<b>Das Python von arr startet nicht für Codex-Agenten.</b>")


async def test_mock_keeps_its_prototype_flow() -> None:
    async with gui_user(MockApi()) as user:
        await user.open("/")
        user.find(marker="switch-workfolder").click()
        await see(user, "Neuen Workfolder anlegen")
        user.find(marker="new-workfolder").click()
        await see(user, "Ein neuer oder leerer Ordner")
        user.find(marker="field-workfolder-path").trigger("input", "~/Test-UX3")
        user.find(marker="form-confirm").click()
        html = await see(user, "Workfolder „Test-UX3“ geöffnet.")
        assert "Im Prototyp" in html and page_html(user)


# --- W3-04.1: findings of the review ----------------------------------------------------------


async def test_a_file_that_is_not_utf8_leaves_the_pages_alive(wf: Path) -> None:
    (wf / "projects" / "kaputt").mkdir()
    (wf / "projects" / "kaputt" / "CLAUDE.md").write_bytes(b"\xff\xfe")
    async with real_user(wf) as (user, _api):
        await user.open("/projekte/kaputt")
        html = await see(user, "CLAUDE.md lässt sich nicht lesen")
        assert "UTF-8" in html and "✕" in html and "demo" in html
        await user.open("/neuer-run")
        await see(user, "demo")
        await user.open("/usage")
        await see(user, "Usage")


async def test_repair_shows_why_it_failed(wf: Path) -> None:
    folder = wf / "projects" / "demo"
    async with real_user(wf) as (user, _api):
        await user.open("/projekte/demo")
        await see(user, "Reparieren")
        folder.chmod(0o555)
        try:
            user.find(marker="repair").click()
            await see(user, "„demo“ ließ sich nicht einrichten (keine Berechtigung)")
        finally:
            folder.chmod(0o755)


async def test_switch_dialog_names_only_the_real_number(wf: Path, tmp_path: Path) -> None:
    other = init_workfolder(tmp_path / "zweiter")
    remember_workfolder(other)
    locks = [
        hold_lock(make_run_folder(wf, status=RunStatus.RUNNING)),
        hold_lock(make_run_folder(wf, status=RunStatus.RUNNING)),
    ]
    try:
        async with real_user(wf) as (user, _api):
            await user.open("/")
            await see(user, "Workfolder wechseln")
            user.find(marker="switch-workfolder").click()
            html = await see(user, str(other))
            assert "2 Runs aktiv" in html and "keine Runs aktiv" not in html
    finally:
        for lock in locks:
            lock.release()


async def test_switch_dialog_counts_at_the_moment_it_opens(wf: Path) -> None:
    async with real_user(wf) as (user, api):
        await user.open("/")
        await see(user, "Workfolder wechseln")
        user.find(marker="switch-workfolder").click()
        await see(user, "keine Runs aktiv")
        lock = hold_lock(make_run_folder(wf, status=RunStatus.RUNNING))
        try:
            api.state.store.scan()
            await user.open("/")  # the page keeps no number, so only the dialog is fresh
            await see(user, "Workfolder wechseln")
            page = page_html(user)
            make_run_folder(wf, status=RunStatus.PAUSED)
            api.state.store.scan()
            user.find(marker="switch-workfolder").click()
            await see(user, "2 Runs aktiv")
            assert "2 Runs aktiv" not in page
        finally:
            lock.release()


async def test_an_old_message_gives_way_to_the_new_one(wf: Path, tmp_path: Path) -> None:
    foreign = tmp_path / "doktorarbeit"
    foreign.mkdir()
    (foreign / "kapitel.tex").write_text("x")
    async with real_user(wf) as (user, _api):
        await user.open("/?workfolder=neu")
        await see(user, "Ein neuer oder leerer Ordner")
        user.find(marker="field-workfolder-path").trigger("input", str(foreign))
        user.find(marker="form-confirm").click()
        await see(user, "liegen schon andere Dateien")
        user.find(marker="field-workfolder-path").trigger("input", f"/{'a' * 300}")
        user.find(marker="form-confirm").click()
        html = await see(user, "der Name ist zu lang")
        assert "liegen schon andere Dateien" not in html


async def test_project_names_follow_the_api(wf: Path) -> None:
    assert SLUG.pattern == API_SLUG
    (wf / "projects" / "mein_ordner").mkdir()
    async with real_user(wf) as (user, api):
        await user.open("/projekte?neu=1")
        await see(user, "arr legt an:")
        user.find(marker="field-new-name").trigger("input", "neues_projekt")
        user.find(marker="form-confirm").click()
        await see(user, "Projekt „neues_projekt“ angelegt.")
        assert (await api.get_project("neues_projekt")).setup_ok
        assert (await api.setup_project("mein_ordner")).project.setup_ok


async def test_existing_folder_with_underscore_is_set_up_in_the_dialog(wf: Path) -> None:
    (wf / "projects" / "mein_ordner").mkdir()
    async with real_user(wf) as (user, api):
        await user.open("/projekte?neu=ordner")
        await see(user, "arr ergänzt nur, was fehlt")
        user.find(marker="field-existing-path").trigger("input", str(wf / "projects/mein_ordner"))
        user.find(marker="form-confirm").click()
        await see(user, "„mein_ordner“ repariert.")
        assert (await api.get_project("mein_ordner")).setup_ok


async def test_rule_table_heads_stand_over_their_columns(wf: Path) -> None:
    async with real_user(wf) as (user, _api):
        await user.open("/einstellungen")
        html = await see(user, "<th>Herkunft</th>")
        assert html.index("<th>Regel</th>") < html.index("<th>Aktion</th>")


# --- W3-04.2: after the merge of W3-02 and W3-03 --------------------------------------------

CLICK_LIMIT_E4 = 6


async def test_e4_in_a_workfolder_made_in_the_gui_needs_no_file(wf: Path, tmp_path: Path) -> None:
    new = tmp_path / "Test-UX3"
    api = RealApi(
        wf,
        base_url="http://test",
        builders=fake_builders({SOLO_AGENT: turns([say("fertig"), done()])}),
        manager_kwargs={"pacing": FAST},
    )
    async with gui_user(api) as user:  # type: ignore[arg-type]
        await api.start()
        try:
            await user.open("/?workfolder=neu")
            await see(user, "Ein neuer oder leerer Ordner")
            user.find(marker="field-workfolder-path").trigger("input", str(new))
            user.find(marker="form-confirm").click()
            for _ in range(200):
                if api.state.workfolder == new:
                    break
                await asyncio.sleep(0.02)
            assert api.state.workfolder == new
            await user.open("/projekte?neu=1")
            await see(user, "arr legt an:")
            user.find(marker="field-new-name").trigger("input", "ux3-collatz")
            user.find(marker="form-confirm").click()
            await see(user, "Projekt „ux3-collatz“ angelegt.")
            assert "defaults:" not in (new / "arr.yaml").read_text().replace("# defaults:", "")

            clicks = Clicks(user)
            clicks.count += 1  # navigation „Neuer Run“
            await user.open("/neuer-run")
            await see(user, "1 · Projekt und Skill")
            user.find(marker="radio-skill-").trigger("change")
            clicks.count += 1
            await see(user, "Ein Agent, Topologie solo")
            user.find(marker="field-task_md").trigger("input", "# Aufgabe\n\nFrage")
            html = await see(user, "In diesem Workfolder fehlen Standards", retries=120)
            assert "fake-auth" in html and "Claude Sonnet 5.5" in html and "in arr.yaml" not in html
            assert "Für claude fehlt" not in html
            await see(user, "Rollen und Skills gefunden", retries=120)
            clicks.on("start-run")
            await see(user, 'data-rd="', retries=400)
            assert clicks.count <= CLICK_LIMIT_E4
            [run] = list((new / "runs").iterdir())
            stored = (run / "run.yaml").read_text()
            assert "model: claude-sonnet-5-5" in stored and "auth: fake-auth" in stored
        finally:
            await api.stop_runs()
            await api.close()


async def test_new_run_shows_the_python_notice_for_codex_agents(
    wf: Path, tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (wf / "arr.yaml").write_text("version: 1\n")
    write_profiles(arr_home, {"chatgpt": "codex_chatgpt"})
    home = tmp_path / "home"
    (home / "uv").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "base_prefix", str(home / "uv"))
    async with real_user(wf) as (user, _api):
        await user.open("/neuer-run?projekt=demo")
        await see(user, "1 · Projekt und Skill")
        user.find(marker="radio-skill-").trigger("change")
        await see(user, "Das Python von arr startet nicht für Codex-Agenten.", retries=120)


# --- W3-04.3 ------------------------------------------------------------------------------------


def _empty_skills(wf: Path) -> None:
    """The workfolder without the start skills `arr init` created (W4-05)."""
    import shutil

    for entry in (wf / "skills").iterdir():
        shutil.rmtree(entry)


async def test_start_skills_are_created_on_the_real_service(wf: Path) -> None:
    """W4-05: the button creates the start skills in the workfolder."""
    _empty_skills(wf)
    async with real_user(wf) as (user, _api):
        await user.open("/skills")
        html = await see(user, "Lege die Start-Skills mit einem Klick an")
        user.find(marker="start-skills").click()
        html = await see(user, "Start-Skills angelegt: arr, exploration, latex-writeup", 80)
        assert "proof-review" in html and "sympy-check" in html
        assert "Start-Skills anlegen" not in html, "nothing missing: no button"
    assert sorted(p.name for p in (wf / "skills").iterdir()) == sorted(START_SKILLS)


async def test_start_skills_never_overwrite_and_the_page_names_them(wf: Path) -> None:
    """W4-05: a skill with the same name stays as it is; the toast names it."""
    _empty_skills(wf)
    own = wf / "skills" / "proof"
    own.mkdir()
    (own / "SKILL.md").write_text("---\nname: proof\ndescription: Mein Beweis.\n---\n", "utf-8")
    async with real_user(wf) as (user, _api):
        await user.open("/skills")
        html = await see(user, "Mein Beweis.")
        assert "Start-Skills anlegen" in html, "the button shows while start skills are missing"
        user.find(marker="start-skills").click()
        await see(user, "Schon vorhanden und nicht verändert: proof.", 80)
    assert (own / "SKILL.md").read_text() == "---\nname: proof\ndescription: Mein Beweis.\n---\n"
    assert sorted(p.name for p in own.iterdir()) == ["SKILL.md"]


async def test_start_skills_in_the_prototype() -> None:
    async with gui_user(MockApi()) as user:
        await user.open("/skills?zustand=leer")
        await see(user, "Start-Skills anlegen")
        user.find(marker="start-skills").click()
        # the forced empty state only hides the data set, which has the start skills already
        await see(user, "Alle Start-Skills sind schon da")


# --- W3-04.4 ------------------------------------------------------------------------------------


async def test_switches_without_an_event_are_shown_as_later_and_keep_their_value(wf: Path) -> None:
    write_app_settings({"notifications": {"storage_low": False, "run_finished": False}})
    async with real_user(wf) as (user, _api):
        await user.open("/einstellungen")
        html = await see(user, "Mitteilungen dazu kommen später.")
        assert html.count("Mitteilungen dazu kommen später.") == 3
        for label in ("Limit erreicht", "Speicher knapp", "Neue Nachricht"):
            line = next(part for part in html.splitlines() if label in part)
            assert "Mitteilungen dazu kommen später." in line
        with user.client:
            boxes = [
                e
                for e in user.client.elements.values()
                if e.tag == "input" and e._props.get("type") == "checkbox"
            ]
        off = [b for b in boxes if "disabled" in b._props]
        assert len(off) >= 4  # the three, and „Neue Freigabe“ is always on
        user.find(marker="save-notifications").click()
        await see(user, "Gespeichert")
    saved = read_app_settings()["notifications"]
    assert saved["storage_low"] is False and saved["run_finished"] is False
    assert saved["limit_reached"] is True and saved["approval"] is True
