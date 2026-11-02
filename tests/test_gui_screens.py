"""Pages of W1-08 with the NiceGUI user simulation: Start, Neuer Run (with „Anpassen“),
Projekte, Skills, Auth-Profile, Usage, Speicher, Einstellungen, Einrichtung; forced states,
simulated notifications. Mock additions (acceptance data set, forced states, usage, resolve)
are checked directly. Browser behaviour (deep links, layout) is in test_gui_screens_browser.py.
See docs/08-ux-konzept.md §5, §8.4–8.6, §9.2.
"""

import asyncio
import re
from collections.abc import Iterator

import pytest
from nicegui.element import Element
from nicegui.testing.user import User
from test_gui import gui_user  # same simulated user as the W1-06 GUI tests

from arr.permissions.types import ApprovalStatus, DecisionAction
from arr.service.api_models import ApiException, DecisionRequest, RunAction, UsageGroupBy
from arr.service.gui import format as fmt
from arr.service.gui.pages.neuer_run import (
    NeuerRunPage,
    parse_duration,
    parse_money,
    task_has_content,
)
from arr.service.mock_api import (
    R_EXPLORE,
    R_LEMMA,
    R_REVIEW,
    Dataset,
    MockApi,
    PageState,
)

ABN_EXPLORE = "20261024-1412_goldbach-ternaer_exploration_a3f9"


def _walk(element: Element) -> Iterator[Element]:
    yield element
    for slot in element.slots.values():
        for child in slot.children:
            yield from _walk(child)


def page_html(user: User) -> str:
    """All HTML and text of the current page (content of `ui.html` and text elements)."""
    with user.client:
        parts = []
        for node in _walk(user.client.layout):
            for value in (getattr(node, "content", None), getattr(node, "text", None)):
                if isinstance(value, str) and value:
                    parts.append(value)
        return "\n".join(parts)


async def see(user: User, text: str, retries: int = 40) -> str:
    """Wait until `text` is in the page and return the page HTML."""
    html = ""
    for _ in range(retries):
        html = page_html(user)
        if text in html:
            return html
        await asyncio.sleep(0.05)
    raise AssertionError(f"„{text}“ not on the page:\n{html[:3000]}")


@pytest.fixture
def api() -> MockApi:
    return MockApi()


# --- Start (§5.1) -----------------------------------------------------------------------------


async def test_start_shows_waiting_active_warnings_and_recent(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/")
        html = await see(user, "Wartet auf dich")
        for text in [
            "explorer-1 möchte eine Datei von oeis.org laden",
            "Stufe „Skizze“ nach 3 Versuchen nicht bestanden",
            "proof-review: Lemma 3 – unabhängige Prüfung",
            "⚠ 3 Abweichungen",
            "Collatz – Ideen-Exploration",
            "2 arbeiten · 1 wartet auf dich · 2 warten",
            "#main 3 neu",
            "Speicher knapp",
            "Runs belegen 46,2 GB von 50 GB. 8 fertige Runs lassen sich verschlanken (14,9 GB).",
            "Zuletzt beendet",
            "Lemma 3 (Stoppzeit 4) – Beweis ausarbeiten",
            "/neuer-run?skill=proof-review",
        ]:
            assert text in html, text
        # same count everywhere (§8.3): badge, chip, section
        assert html.count(' ansehen">Ansehen</a>') == 3
        assert (
            '<span class="badge st-wait"><span class="ico ico-sm" aria-hidden="true">'
            "front_hand</span>3</span>" in html
        )
        # „Aktive Runs“: the running run with its approval chip comes first (wireframe)
        active = html[html.index('id="h-active"') :]
        assert active.index("Collatz – Ideen-Exploration") < active.index("R(4,5) ≤ 25")
        assert active.index("R(4,5) ≤ 25 – Beweisweg ausarbeiten") < active.index(
            "Lemma 3 – unabhängige Prüfung</b>"
        )


async def test_start_states_empty_loading_error() -> None:
    async with gui_user(MockApi()) as user:
        await user.open("/?zustand=leer")
        html = await see(user, "Alles ruhig")
        assert "Kein Run läuft, nichts wartet auf dich, keine Warnungen." in html
        await user.open("/?zustand=ohne-profil")
        await see(user, "Noch kein Zugang eingerichtet")
        await user.open("/?zustand=fehler")
        html = await see(user, "Index neu aufbauen")
        assert "Run-Ordner nicht lesbar" in html
        assert "explorer-1 möchte eine Datei" in html  # the other areas stay (§5.1)
        await user.open("/?zustand=laedt")
        html = await see(user, 'data-state="laedt"')
        assert "Die Freigabe-Inbox wird gelesen …" in html
        await user.open("/?zustand=workfolder-fehlt")
        await see(user, "Workfolder ~/Mathe nicht gefunden.")


async def test_start_live_new_approval_appears(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/")
        await see(user, "Wartet auf dich")
        api.simulate_new_approval(R_EXPLORE)
        html = await see(user, "explorer-2 möchte eine Datei von arxiv.org laden", retries=60)
        # the simulated macOS notification appears with its deep link (§4.2, §9.2)
        assert "arr · explorer-2 möchte eine Datei von arxiv.org laden" in html


# --- Neuer Run (§5.4) -------------------------------------------------------------------------


async def test_new_run_checks_budget_and_starts(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/neuer-run?skill=exploration&projekt=collatz")
        await see(user, "1 · Projekt und Skill")
        html = await see(user, "Die Aufgabe fehlt.")
        assert "Rollen und Skills gefunden" in html
        user.find(marker="field-task_md").trigger("input", "# Aufgabe\n\nZwei Ideen prüfen.")
        user.find(marker="field-limits.budget_usd").trigger("input", "10 $")
        await asyncio.sleep(0.7)
        html = await see(user, "Zu knapp: Die Schätzung liegt bei ≈ 9–16 $.")
        assert "Noch offen (1)" in html  # the budget field was not left yet
        user.find(marker="field-limits.budget_usd").trigger("blur")
        html = await see(user, "1 Problem")
        assert "nimm weniger Turns pro Idee" in html
        assert invalid_fields(user) == ["f-limits.budget_usd"]
        assert (await api.list_runs()).total == 31
        user.find(marker="field-params.turns_per_idea").trigger("input", "3")
        await asyncio.sleep(0.7)
        await see(user, "Profile getestet")
        html = page_html(user)
        assert "Zu knapp" not in html
        user.find(marker="start-run").click()
        for _ in range(40):
            if (await api.list_runs()).total == 32:
                break
            await asyncio.sleep(0.05)
        page = await api.list_runs()
        assert page.total == 32
        new = next(r for r in page.items if r.starter.value == "gui" and r.created_at > 1.7e9)
        assert new.project == "collatz" and new.skill == "exploration"
        assert new.limits.budget_usd == 10


def invalid_fields(user: User) -> list[str]:
    """IDs of the fields shown as invalid (red frame, error text)."""
    with user.client:
        return [
            str(node.props.get("id"))
            for node in _walk(user.client.layout)
            if "field" in node.classes and "invalid" in node.classes
        ]


async def test_fresh_new_run_form_has_no_red_field(api: MockApi) -> None:
    """M2: fresh form neutral („Noch offen“), the error shows once you left the field; a focus
    set by the page counts only after you typed (W1-08.2 N2)."""
    async with gui_user(api) as user:
        await user.open("/neuer-run?skill=exploration&projekt=collatz")
        html = await see(user, "Noch offen (1)")
        assert "Die Aufgabe fehlt." in html and "1 Problem" not in html
        assert invalid_fields(user) == []
        # W1-08.2 N2: the deep link put the focus there, you moved on without typing
        user.find(marker="field-task_md").trigger("blur", True)
        await asyncio.sleep(0.3)
        assert invalid_fields(user) == [] and "Noch offen (1)" in page_html(user)
        user.find(marker="field-task_md").trigger("input", "# Aufgabe\n\n")
        await asyncio.sleep(0.7)
        assert invalid_fields(user) == []  # typing alone does not make it red
        user.find(marker="field-task_md").trigger("blur", True)  # typed, then left: touched
        await see(user, "1 Problem")
        assert invalid_fields(user) == ["f-task_md"]
        user.find(marker="field-task_md").trigger("input", "# Aufgabe\n\nZwei Ideen prüfen.")
        await asyncio.sleep(0.7)
        await see(user, "Profile getestet")
        assert invalid_fields(user) == []


async def test_new_run_parameters_have_german_labels(api: MockApi) -> None:
    """M3/G8: label from the skill declaration, the YAML key small beside it, help once."""
    async with gui_user(api) as user:
        await user.open("/neuer-run?skill=exploration&projekt=collatz")
        html = await see(user, "Turns pro Idee")
        assert '<span class="opt mono">turns_per_idea</span>' in html
        assert "Austausch zwischen den Explorern" in html
        assert html.count("Vorgegebene Ideen") == 1
        assert "Leer: Die Agenten erzeugen selbst 3 Ideen." in html
        await user.open("/skills/proof-review")
        html = await see(user, "Gedächtnis der Judges")
        assert "jede Runde neu, Sitzung fortsetzen" in html


async def test_new_run_preselects_the_last_group_skill_never_solo(api: MockApi) -> None:
    """G3: without `?skill=` the group skill used last, never „Ohne Skill“; G2: the preview's
    <summary> is the first child of <details>."""
    used = [r.skill for r in (await api.list_runs(limit=20)).items if r.skill]
    async with gui_user(api) as user:
        await user.open("/neuer-run")
        await see(user, "Profile getestet")
        with user.client:
            checked = [
                n.props.get("value")
                for n in _walk(user.client.layout)
                if n.tag == "input" and n.props.get("name") == "skill" and "checked" in n.props
            ]
            details = [n for n in _walk(user.client.layout) if n.tag == "details"]
            first = details[0].default_slot.children[0]
        assert checked == [used[0]] and used[0] != ""
        assert first.props.get("tag") == "summary"  # rendered as <summary>, not wrapped


async def test_new_run_without_a_used_skill_asks_for_one(
    api: MockApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_runs(_self: NeuerRunPage) -> list[object]:
        return []

    monkeypatch.setattr(NeuerRunPage, "_recent_runs", no_runs)
    async with gui_user(api) as user:
        await user.open("/neuer-run")
        html = await see(user, "Wähle einen Skill oder „Ohne Skill“.")
        assert "noch nicht gewählt" in html and "Noch offen (1)" in html
        user.find(marker="radio-skill-roundtable").trigger("change")
        await see(user, "Profile getestet")


async def test_new_run_without_projects_and_skills() -> None:
    async with gui_user(MockApi()) as user:
        await user.open("/neuer-run?zustand=leer")
        html = await see(user, "Lege zuerst ein Projekt an")
        assert "/projekte?neu=1" in html


async def test_new_run_error_state_shows_check_error_with_jump_link() -> None:
    async with gui_user(MockApi()) as user:
        await user.open("/neuer-run?skill=exploration&zustand=fehler")
        html = await see(user, "Rolle für critic nicht gefunden")
        assert 'data-jump="s1"' in html


async def test_adjust_mode_marks_changes_and_approves_with_same_run_id(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/neuer-run?von=apr_0041")
        html = await see(user, "Run-Start von Claude Code anpassen")
        assert R_REVIEW in html and "bleibt gleich" in html
        html = await see(user, "Abweichungen von deinen")
        assert "Freigeben und starten" in html
        for judge in ("judge-1", "judge-2", "judge-3"):
            user.find(marker=f"web-{judge}").trigger("change", False)
        await asyncio.sleep(0.3)
        html = await see(user, "Deine Änderungen (3)")
        assert "Web judge-1: an → aus" in html
        assert "(von dir geändert)" in html
        user.find(marker="start-run").click()
        for _ in range(40):
            item = await api.get_approval("apr_0041")
            if item.status is ApprovalStatus.DECIDED:
                break
            await asyncio.sleep(0.05)
        assert item.decision is not None and item.decision.action is DecisionAction.ADJUST
        assert {c.label for c in item.decision.changes} == {
            "Web judge-1: an → aus",
            "Web judge-2: an → aus",
            "Web judge-3: an → aus",
        }
        run = (await api.get_run(R_REVIEW)).run
        assert run.adjusted_by_user and run.id == R_REVIEW


async def test_adjust_mode_of_a_decided_run_start(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/neuer-run?von=apr_0035")
        await see(user, "Dieser Run-Start ist schon entschieden")


def test_parsers_of_the_new_run_form() -> None:
    assert parse_duration("3 h") == 3 * 3600
    assert parse_duration("1,5 h") == 5400
    assert parse_duration("90 min") == 5400
    assert parse_duration("drei") is None
    assert parse_money("20 $") == 20 and parse_money("12,50") == 12.5
    assert parse_money("viel") is None
    assert not task_has_content("# Aufgabe\n\n## Kontext\n")
    assert task_has_content("# Aufgabe\nText")


# --- Projekte, Skills (§5.6, §5.7) ------------------------------------------------------------


async def test_projects_list_detail_repair_and_new(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/projekte/primzahl-luecken")
        html = await see(user, ".mcp.json fehlt")
        assert "unvollständig" in html and "Mit Claude Code arbeiten" in html
        user.find(marker="repair").click()
        await see(user, "„primzahl-luecken“ repariert.")
        assert (await api.get_project("primzahl-luecken")).setup_ok
        user.find(marker="new-project").click()
        await see(user, "arr legt an:")
        user.find(marker="field-new-name").trigger("input", "Goldbach Schwach")
        user.find(marker="form-confirm").click()
        await see(user, "Nimm Kleinbuchstaben, Ziffern, Binde- und Unterstriche")
        user.find(marker="field-new-name").trigger("input", "goldbach-schwach")
        user.find(marker="form-confirm").click()
        await see(user, "Projekt „goldbach-schwach“ angelegt.")
        assert (await api.get_project("goldbach-schwach")).setup_ok


async def test_projects_states() -> None:
    async with gui_user(MockApi()) as user:
        await user.open("/projekte?zustand=leer")
        html = await see(user, "Noch keine Projekte")
        assert "Beispielprojekt collatz" in html
        await user.open("/projekte?zustand=fehler")
        await see(user, "collatz fehlt unter ~/Mathe/projects. 22 Runs bleiben lesbar.")
        # G4: the runs are readable, nothing in the missing folder counts as checked
        html = await see(user, "nicht prüfbar, solange der Ordner fehlt")
        assert "ließen sich" not in html and "Letzte Runs" in html and "/runs/" in html
        assert "Nicht lesbar, solange der Ordner fehlt" in html
        assert 'class="st ok"' not in html
        await user.open("/projekte?zustand=laedt")
        await see(user, "Die Projekte werden gelesen …")


async def test_skills_read_only_with_tabs_and_errors() -> None:
    async with gui_user(MockApi()) as user:
        await user.open("/skills/proof-review")
        html = await see(user, "Ablauf (Topologie Review)")
        for text in ["judges", "max_revisions", "roles/judge.md", "#judges", "Benutzt von"]:
            assert text in html, text
        assert "/neuer-run?skill=proof-review" in html
        await user.open("/skills/proof-review?tab=yaml")
        await see(user, "kind: research-group")
        await user.open("/skills/latex-writeup?tab=pruefung")
        await see(user, "Beschreibung fehlt im Kopf.")
        await user.open("/skills/exploration?zustand=fehler")
        html = await see(user, "group.yaml, Zeile 31: Rolle roles/critic.md nicht gefunden.")
        assert 'disabled aria-disabled="true"' in html  # „Run mit diesem Skill“ locked
        await user.open("/skills?zustand=leer")
        await see(user, "Die Start-Skills lassen sich mit einem Klick anlegen.")


# --- Auth-Profile (§5.8) ----------------------------------------------------------------------


async def test_profiles_cards_errors_and_no_secret(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/profile")
        html = await see(user, "claude-api-uni")
        assert "Der API-Key von „claude-api-uni“ wurde abgelehnt (401)." in html
        assert "Key ersetzen" in html and "im Schlüsselbund ✓" in html
        assert "persönliche</b> Nutzung" in html
        assert "3 von 4</b> Plätzen belegt" in html
        await user.open("/profile?zustand=fehler")
        html = await see(user, "Die ChatGPT-Anmeldung ist abgelaufen.")
        assert "Die Claude-Anmeldung wurde abgebrochen." in html
        await user.open("/profile?zustand=leer")
        await see(user, "Ohne Auth-Profil können keine Agenten starten.")


async def test_concurrency_hint_is_the_help_text_of_the_field(api: MockApi) -> None:
    """G9: the hint stands in the field's help (12 px like all help texts), per kind."""
    async with gui_user(api) as user:
        await user.open("/profile?neu=1")
        await see(user, "Bei Max-Abos sind 3–4 ein guter Wert.")
        with user.client:
            helps = [
                n.content
                for n in _walk(user.client.layout)
                if "help" in n.classes and "Max-Abos" in str(getattr(n, "content", ""))
            ]
        assert helps == [
            "Höchstens so viele Agenten nutzen das Profil gleichzeitig. Bei Max-Abos sind 3–4 "
            "ein guter Wert."
        ]


async def test_new_subscription_profile_logs_in_without_token_field(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/profile?neu=1")
        html = await see(user, "Neues Auth-Profil")
        assert "Mit Claude anmelden" in html
        assert 'type="password"' not in html and "Token einfügen" not in html  # E18
        user.find(marker="field-new-profile-name").trigger("input", "max-uni")
        user.find(marker="form-confirm").click()
        await see(user, "Warte auf die Anmeldung im Browser")
        await see(user, "arr führt dafür claude setup-token aus.")
        api.advance(4)
        await see(user, "✓ max-uni angemeldet und getestet.", retries=80)
        profile = next(p for p in await api.list_profiles() if p.name == "max-uni")
        assert profile.secret_present and profile.last_test and profile.last_test.ok


async def test_profile_test_and_delete_dialog(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/profile/claude-api-uni")
        await see(user, "claude-api-uni")
        user.find(marker="test-claude-api-uni").click()
        await see(user, "Testen …")
        await see(user, "wurde abgelehnt (401)", retries=60)


# --- Usage, Speicher (§5.9, §5.10) ------------------------------------------------------------


async def test_usage_keeps_billed_and_estimated_apart(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/usage")
        html = await see(user, "Abgerechnet (API-Keys)")
        assert "Rechnerisch (Abos)" in html and "rechnerisch" in html
        assert "Codex liefert keinen Preis" in html
        assert "Rate-Limit-Ereignisse" in html and "gedrosselt" in html
        await user.open("/usage?nach=modell")
        await see(user, "Claude Sonnet 5.5")
        await user.open("/usage?zustand=leer")
        await see(user, "Noch kein Verbrauch im Oktober 2026")
        await user.open("/usage?zustand=fehler")
        await see(user, "Usage-Index nicht lesbar.")


async def test_storage_suggestion_slims_after_dialog_and_warning_goes(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/speicher")
        html = await see(user, "Vorschlag: 14,9 GB freimachen")
        assert "Runs belegen 46,2 GB" in html and "92 % deiner Grenze von 50 GB" in html
        assert "Ausgenommen: 1 angehefteter Run und 3 aktive Runs" in html
        assert "Aktive Runs lassen sich nicht aufräumen." in html
        user.find(marker="slim-suggestion").click()
        html = await see(user, "8 Runs verschlanken?")
        assert "14,9 GB werden frei." in html and "nicht rückgängig" in html
        before = (await api.get_storage()).runs_actual
        assert before > 0
        user.find(marker="dialog-confirm").click()
        for _ in range(80):
            if not [w for w in await api.list_warnings() if w.kind.value == "storage_low"]:
                break
            await asyncio.sleep(0.05)
        await see(user, "14,9 GB frei\n", retries=80)
        report = await api.get_storage()
        assert fmt.size(before - report.runs_actual) == "14,9 GB"
        assert not [w for w in await api.list_warnings() if w.kind.value == "storage_low"]
        await user.open("/speicher?zustand=leer")
        await see(user, "Runs belegen 0 B")
        await user.open("/speicher?zustand=fehler")
        # G5: a note above the runs, the rest of the page stays usable
        html = await see(user, "konnte nicht gemessen werden: Ordner nicht lesbar.")
        assert "Vorschlag:" in html and "Runs nach Größe" in html and "Im Finder zeigen" in html
        assert html.index("konnte nicht gemessen") > html.index("Runs nach Größe")


async def test_run_detail_error_offers_finder_and_the_real_path() -> None:
    """G6: „Im Finder zeigen“ and „Pfad kopieren“ with the run folder of this workfolder."""
    api = MockApi(Dataset.ACCEPTANCE)
    async with gui_user(api) as user:
        await user.open(f"/runs/{ABN_EXPLORE}?zustand=fehler")
        html = await see(user, "Pfad kopieren")
        folder = f"{(await api.get_workfolder()).path}/runs/{ABN_EXPLORE}"
        assert f'data-copy="{folder}"' in html and "~/Mathe" not in html
        user.find(marker="show-in-finder").click()
        await see(user, f"Im Finder gezeigt: {folder}")


async def test_no_wave_codes_in_the_interface(api: MockApi) -> None:
    """G7: no project jargon like „W4-02“ in texts of the interface."""
    wave = re.compile(r"\bW\d-\d\d\b")
    async with gui_user(api) as user:
        for path in ("/skills", "/usage", "/einstellungen", "/einrichtung?schritt=1"):
            await user.open(path)
            await asyncio.sleep(0.3)
            assert not wave.findall(page_html(user)), path
    with pytest.raises(ApiException, match="späteren Version"):
        await api.get_usage(group_by=UsageGroupBy.AGENT)


async def test_dataset_switch_restarts_the_current_data_set(api: MockApi) -> None:
    """G11: „↺ … neu starten“ loads the chosen data set afresh (before each test pass)."""
    api.switch_dataset(Dataset.ACCEPTANCE)
    before = (await api.get_counts()).waiting
    item = (await api.list_approvals(status=[ApprovalStatus.OPEN]))[0]
    await api.decide_approval(item.id, DecisionRequest(action=DecisionAction.DENY))
    assert (await api.get_counts()).waiting == before - 1
    async with gui_user(api) as user:
        await user.open("/usage")
        await see(user, "↺ Abnahme (UX-Abnahme 2) neu starten")
        user.find(marker="dataset").trigger("change", "neu-starten")
        for _ in range(40):
            if (await api.get_counts()).waiting == before:
                break
            await asyncio.sleep(0.05)
    assert api.dataset is Dataset.ACCEPTANCE
    assert (await api.get_counts()).waiting == before


# --- Einstellungen, Einrichtung (§5.11, §5.12) --------------------------------------------------


async def test_settings_sections_and_mandate_confirmation(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/einstellungen")
        html = await see(user, "Deine Vollmacht")
        assert "Runs, die Claude Code startet, brauchen immer deine Freigabe" in html
        assert "Netzwerk zu www.lmfdb.org" in html and "Entfernen" in html
        assert "Python-Umgebung der Agenten" in html and "Browser-Tab (Standard)" in html
        user.find(marker="field-approvals.timeout").trigger("input", "bald")
        user.find(marker="save-approvals").click()
        await see(user, "Frist: Gib eine Dauer an, z. B. 20 min.")
        user.find(marker="field-mandate").trigger("input", "Erlaubt:\n- Netzwerk zu oeis.org\n")
        user.find(marker="save-mandate").click()
        html = await see(user, "Vollmacht ändern?")
        assert "gilt sofort für alle neuen Anfragen, auch in 1 laufenden Run" in html
        user.find(marker="dialog-confirm").click()
        for _ in range(40):
            if "oeis.org" in (await api.get_mandate()).text:
                break
            await asyncio.sleep(0.05)
        assert "oeis.org" in (await api.get_mandate()).text
        await user.open("/einstellungen?zustand=fehler")
        html = await see(user, "arr.yaml, Zeile 14")
        assert "Mitteilungen auf dem Mac" in html  # app sections stay usable


async def test_setup_assistant_steps(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/einrichtung")
        html = await see(user, "Wo sollen Projekte, Skills und Runs liegen?")
        assert "Später einrichten" in html and "1 · Workfolder" in html
        user.find(marker="setup-create").click()
        await see(user, "Workfolder ~/Mathe ist bereit.")
        await see(user, "Mit welchen KI-Zugängen sollen die Agenten arbeiten?", retries=80)
        await user.open("/einrichtung?zustand=fehler")
        html = await see(user, "Die Claude-Anmeldung wurde abgebrochen.")
        assert "Erneut versuchen" in html
        await user.open("/einrichtung?zustand=laedt")
        await see(user, "Warte auf die Anmeldung im Browser …")
        await user.open("/einrichtung?schritt=5")
        html = await see(user, "Ersten Run starten")
        assert "/neuer-run?skill=exploration&amp;projekt=collatz&amp;beispiel=1" in html


async def test_first_run_from_setup_is_small_enough(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/neuer-run?skill=exploration&projekt=collatz&beispiel=1")
        await see(user, "Profile getestet")
        html = page_html(user)
        assert "Zu knapp" not in html and "Die Aufgabe fehlt" not in html


# --- forced states on the pages of W1-06/07 and all pages ----------------------------------------


@pytest.mark.parametrize(
    ("path", "state", "text"),
    [
        ("/freigaben", "leer", "Nichts wartet auf dich"),
        ("/freigaben", "laedt", "Die Freigabe-Inbox wird gelesen …"),
        ("/freigaben", "fehler", "Die Freigabe-Inbox ist nicht lesbar."),
        ("/runs", "leer", "Noch keine Runs in „Mathe privat“"),
        ("/runs", "laedt", "Die Run-Ordner werden gelesen"),
        ("/runs", "fehler", "Run-Ordner nicht lesbar"),
        (f"/runs/{R_LEMMA}", "laedt", "Die letzten 200 Ereignisse werden geladen …"),
        (f"/runs/{R_LEMMA}", "fehler", "Run-Ordner nicht lesbar: meta.json fehlt."),
        ("/skills", "laedt", "Die Skills werden geprüft …"),
        ("/profile", "laedt", "Die Profile werden gelesen …"),
        ("/usage", "laedt", "Der Usage-Index wird gelesen …"),
        ("/speicher", "laedt", "Größen werden gemessen …"),
        ("/einstellungen", "laedt", "arr.yaml wird gelesen …"),
        ("/neuer-run", "laedt", "Projekte, Skills und Standards werden gelesen …"),
    ],
)
async def test_every_page_has_its_forced_states(path: str, state: str, text: str) -> None:
    async with gui_user(MockApi()) as user:
        await user.open(f"{path}?zustand={state}")
        await see(user, text)


# --- mock additions -----------------------------------------------------------------------------


FORBIDDEN = re.compile(
    r"collatz|Collatz|explorer|critic|Kritiker|oeis|OEIS|Stoppzeit|Terras|Lemma 3|lemma3|"
    r"ramsey-schranken|R\(4,5\)|primzahl-luecken|Mathe privat|~/Mathe\b|Mathe-Literatur|"
    r"max-privat|chatgpt-privat|claude-api-uni|#main\b|Lagarias"
)


async def acceptance_texts(api: MockApi) -> list[str]:
    """All texts the acceptance data set shows: lists, details, live stream, skill files."""
    texts = [
        (await api.get_workfolder()).model_dump_json(),
        *(w.model_dump_json() for w in await api.list_warnings()),
        *(p.model_dump_json() for p in await api.list_profiles()),
        *(p.model_dump_json() for p in await api.list_projects()),
        *(s.model_dump_json() for s in await api.list_skills()),
        *(a.model_dump_json() for a in await api.list_approvals()),
        (await api.get_mandate()).model_dump_json(),
        (await api.get_workfolder_settings()).model_dump_json(),
    ]
    for group in (UsageGroupBy.PROFILE, UsageGroupBy.RUN):
        texts.append((await api.get_usage(group_by=group)).model_dump_json())
    page = await api.list_runs(limit=1000)
    texts.append(page.model_dump_json())
    for run in page.items:
        texts.append((await api.get_run(run.id)).model_dump_json())
        texts.append((await api.get_results(run.id)).model_dump_json())
        for channel in await api.list_channels(run.id):
            texts.append(
                (await api.get_messages(run.id, channel.name, limit=100)).model_dump_json()
            )
        for agent in await api.list_agents(run.id):
            texts.append((await api.get_transcript(run.id, agent.id)).model_dump_json())
    resolved = await api.resolve_run(
        {"project": "goldbach-ternaer", "skill": "exploration", "task_md": "x"}
    )
    texts.append(resolved.model_dump_json())
    api.stream_rate = 20
    for _ in range(30):
        api.advance(1)
    texts.append(api.simulate_new_approval().model_dump_json())
    for agent in await api.list_agents(ABN_EXPLORE):
        texts.append((await api.get_transcript(ABN_EXPLORE, agent.id)).model_dump_json())
    for skill in await api.list_skills():
        texts.append((await api.get_skill(skill.name)).model_dump_json())
        for path in skill.files:
            texts.append((await api.get_skill_file(skill.name, path)).model_dump_json())
    return texts


async def test_acceptance_data_set_has_its_own_names_and_contents() -> None:
    """docs/08 §9.2 Nr. 3: same structure as the scenario, other names and contents."""
    api = MockApi(Dataset.ACCEPTANCE)
    texts = await acceptance_texts(api)
    leftovers = {m.group(0) for text in texts for m in FORBIDDEN.finditer(text)}
    assert not leftovers
    # the contents of docs/08 §9.2
    all_text = "\n".join(texts)
    for text in [
        "Forschung",
        "Ternäre Goldbach-Darstellungen – Exploration",
        "wget https://primes.utm.edu/lists/small/millions/primes1.zip",
        "#labor" if "#labor" in all_text else "labor",
        "rangliste",
        "R(3,3,3)-Färbung – Beweis",
        "Stufe „Konstruktion“ nach 3 Versuchen nicht bestanden",
        "Kongruenz p(5n+4) ≡ 0 mod 5 – Beweis",
        "~/Documents/Partitionen-Daten",
        "claude-api-lehrstuhl",
    ]:
        assert text in all_text, text
    start = next(a for a in await api.list_approvals() if a.id == "apr_0041")
    assert [d.label for d in start.deviations] == ["Zusätzlicher Lese-Ordner", "Laufzeit 6 h"]
    assert not any(a.tools.web for a in start.agents)
    results = await api.get_results("20261024-0930_partitionen_proof_7c21")
    assert len(results.items) == 4
    assert sum(1 for r in results.items if r.status.value == "rework") == 1
    storage = await api.get_storage()
    assert fmt.size(storage.runs_actual) == "47,8 GB"
    assert storage.suggestion and fmt.size(storage.suggestion.freed_bytes) == "11,2 GB"
    agents = {a.id for a in await api.list_agents(ABN_EXPLORE)}
    assert agents == {"scout-1", "scout-2", "scout-gpt", "skeptic", "ranker"}


async def test_acceptance_plurals_of_scout_are_german() -> None:
    """N1: „Explorern“ becomes „Scouts“, not „Scoutn“; plural „die Scouts“, not „die Scout“."""
    texts = "\n".join(await acceptance_texts(MockApi(Dataset.ACCEPTANCE)))
    assert not re.findall(r"\w*scoutn\b", texts, flags=re.I)
    assert not re.findall(r"\b(?:die|mehrere|alle) Scout(?![\w-])", texts, flags=re.I)
    assert "Austausch zwischen den Scouts" in texts


async def test_forced_states_of_the_mock() -> None:
    api = MockApi()
    empty = api.forced(PageState.EMPTY)
    assert (await empty.list_runs()).total == 0 and await empty.list_approvals() == []
    assert (await empty.get_counts()).waiting == 0
    failing = api.forced("fehler")
    with pytest.raises(ApiException) as exc:
        await failing.list_runs()
    assert exc.value.error.action and exc.value.error.action.label == "Index neu aufbauen"
    assert (await failing.get_counts()).waiting == 3  # the frame stays usable
    skill = await failing.get_skill("exploration")
    assert not skill.valid and skill.issues[0].line == 31


async def test_resolve_estimate_follows_ideas_and_turns() -> None:
    api = MockApi()
    base = {"project": "collatz", "skill": "exploration", "task_md": "x"}
    default = await api.resolve_run(base)
    assert (default.estimate.cost_usd.min, default.estimate.cost_usd.max) == (9, 16)
    two = await api.resolve_run(
        {**base, "params": {"ideas": ["a", "b"]}, "limits": {"budget_usd": 10}}
    )
    assert two.errors and "mindestens 11 $" in two.errors[0].message
    fixed = await api.resolve_run(
        {**base, "params": {"ideas": ["a", "b"], "turns_per_idea": 5}, "limits": {"budget_usd": 10}}
    )
    assert not fixed.errors
    web = await api.resolve_run(
        {
            "project": "collatz",
            "skill": "proof-review",
            "task_md": "x",
            "agents": {"judge-1": {"web": True}},
            "limits": {"wall_time_s": 6 * 3600},
        }
    )
    assert [d.label for d in web.deviations] == ["Web-Zugriff an", "Laufzeit 6 h"]


async def test_run_action_slim_frees_disk_and_refreshes_warning() -> None:
    api = MockApi()
    report = await api.get_storage()
    assert report.suggestion is not None
    free = (await api.get_workfolder()).disk_free_bytes
    for run_id in report.suggestion.run_ids:
        await api.run_action(run_id, RunAction.SLIM)
    assert (await api.get_workfolder()).disk_free_bytes == free + report.suggestion.freed_bytes
    assert all(w.kind.value != "storage_low" for w in await api.list_warnings())


# --- A4: no terminal step (E18, G4) -------------------------------------------------------------

TERMINAL = re.compile(r"Terminal|Kommandozeile|uv run|arr gui|arr serve|brew |Token einfügen", re.I)


@pytest.mark.parametrize("zustand", ["", "fehler"])
async def test_no_terminal_step_in_setup_profiles_and_errors(zustand: str) -> None:
    """Setup, Auth-Profile and every error text name GUI actions, never a command to type;
    when arr runs a command itself, the GUI says so („arr führt dafür … aus“)."""
    async with gui_user(MockApi()) as user:
        for path in [
            "/einrichtung?schritt=1",
            "/einrichtung?schritt=2",
            "/einrichtung?schritt=3",
            "/einrichtung?schritt=4",
            "/einrichtung?schritt=5",
            "/profile",
            "/profile?neu=1",
            "/",
            "/projekte",
            "/skills/exploration",
            "/usage",
            "/speicher",
            "/einstellungen",
            "/neuer-run?skill=exploration",
            "/runs",
            "/freigaben",
        ]:
            await user.open(
                path + (("&" if "?" in path else "?") + f"zustand={zustand}" if zustand else "")
            )
            await asyncio.sleep(0.3)
            html = page_html(user)
            assert not TERMINAL.findall(html), (path, TERMINAL.findall(html))
            for line in re.findall(r"[^<>\n]*setup-token[^<>\n]*", html):
                assert "arr führt dafür" in line or "selbst aus" in html, line


# --- Not-Aus and record per profile (W3-06) ---------------------------------------------------


async def test_sperren_asks_first_and_shows_the_block(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/profile/claude-api-uni")
        await see(user, "claude-api-uni")
        user.find(marker="disable-claude-api-uni").click()
        html = await see(user, "Profil „claude-api-uni“ sperren?")
        assert "offene Anmeldungen enden" in html and "Der Login bleibt erhalten" in html
        assert "Runs in anderen Workfoldern stoppen von selbst" in html
        assert not next(p for p in await api.list_profiles() if p.name == "claude-api-uni").disabled
        user.find(marker="dialog-confirm").click()
        html = await see(user, "gesperrt")
        assert next(p for p in await api.list_profiles() if p.name == "claude-api-uni").disabled
        await user.should_see(marker="enable-claude-api-uni")


async def test_login_entfernen_is_a_second_step_and_names_the_provider(api: MockApi) -> None:
    await api.disable_profile("claude-api-uni")
    async with gui_user(api) as user:
        await user.open("/profile/claude-api-uni")
        await see(user, "claude-api-uni")
        user.find(marker="remove-login-claude-api-uni").click()
        html = await see(user, "Login von „claude-api-uni“ entfernen?")
        assert "Anthropic Console" in html and "Das Profil bleibt gesperrt" in html
        user.find(marker="dialog-confirm").click()
        await see(user, "ist aus arr entfernt")


async def test_nachweis_opens_with_sums_runs_and_its_limit(api: MockApi) -> None:
    async with gui_user(api) as user:
        await user.open("/profile/claude-api-uni")
        await see(user, "claude-api-uni")
        user.find(marker="usage-claude-api-uni").click()
        html = await see(user, "Erfasst sind nur Läufe über arr")
        for text in ("Heute", "7 Tage", "Gesamt", "Cache lesen", "Reasoning", "abgebrochen"):
            assert text in html
        assert 'href="/runs/20261030-1402_demo"' in html
        assert "direkte Nutzung von Claude Code oder Codex" in html
