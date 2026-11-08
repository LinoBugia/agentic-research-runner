"""W3-10 in the GUI on `RealApi` with FakeBackends: the view „Eingaben“ (V9), „Wiederholen“
from the ⋯ menu and the runs list into a filled „Neuer Run“ (V8), costs in `$` as text (U9).
"""

import asyncio
import shutil
from pathlib import Path
from typing import Any

import yaml
from run_kit import write_profiles
from service_kit import hold_lock, make_run_folder
from test_gui_real import page_html, see
from test_gui_runs_real import FORM, make_api, real_user, until_final, wf

from arr.models import Decider, RunStatus
from arr.service.gui.components.base import markdown_html

__all__ = ["wf"]  # the fixture

FAKE_KEY = "sk-ant-api03-W310GUIVALUE00000"


def form(**extra: Any) -> dict[str, Any]:
    data = {
        **FORM,
        "name": "Erster Versuch",
        "agents": {"m-2": {"web": True, "read_dirs": ["~/Documents/Literatur"]}},
        "limits": {"wall_time_s": 20 * 60, "budget_usd": 2.0},
        "decider": Decider.USER.value,
    }
    data.update(extra)
    return data


async def finished(api: Any, data: dict[str, Any]) -> str:
    run_id = (await api.start_run(data)).run_id
    await until_final(api, run_id)
    return run_id


def value(user: Any, marker: str) -> Any:
    with user.client:
        return next(iter(user.find(marker=marker).elements))._props.get("value")


def checked(user: Any, marker: str) -> bool:
    with user.client:
        return "checked" in next(iter(user.find(marker=marker).elements))._props


# --- V9: Eingaben ------------------------------------------------------------------------------


async def test_inputs_of_a_finished_run_without_the_secret(wf: Path) -> None:
    api = make_api(wf)
    task = f"# Aufgabe\n\nZwei Ideen prüfen, Schlüssel ANTHROPIC_API_KEY={FAKE_KEY}."
    async with real_user(api) as user:
        run_id = await finished(api, form(task_md=task))
        await user.open(f"/runs/{run_id}?ansicht=eingaben")
        html = await see(user, "Aufgelöste run.yaml", retries=200)
        for text in (
            "Zwei Ideen prüfen",  # the task, rendered
            'aria-label="Aufgabe kopieren"',
            "data-project>demo<",
            'data-param="rounds"',
            "geändert",  # rounds 1, default 2
            'data-agent="m-1"',
            'data-agent="m-2"',
            "fake-auth",
            "~/Documents/Literatur",
            "#main",
            "ereignisgesteuert",
            "20 min",
            "Du entscheidest jede Anfrage",
            f'href="/neuer-run?wie={run_id}"',
        ):
            assert text in html, text
        assert FAKE_KEY not in html and "W310GUIVALUE" not in html
        # „md“ is a visibility class of Quasar (hidden from 1440 px): the task box is „prose“
        assert 'class="prose in-task"' in html
        assert "aria-current=page" in html.split('data-sel="ansicht:eingaben"')[1][:40]


async def test_inputs_of_a_stopped_a_slimmed_and_a_running_run(wf: Path) -> None:
    stopped = make_run_folder(wf, status=RunStatus.STOPPED, name="Gestoppt")
    slimmed = make_run_folder(wf, status=RunStatus.DONE, name="Verschlankt")
    (slimmed / ".slimmed").write_text("")
    shutil.rmtree(slimmed / "agents" / "a" / "work", ignore_errors=True)
    running = make_run_folder(wf, status=RunStatus.RUNNING, name="Läuft")
    lock = hold_lock(running)
    try:
        async with real_user(make_api(wf)) as user:
            for folder in (stopped, slimmed, running):
                await user.open(f"/runs/{folder.name}?ansicht=eingaben")
                html = await see(user, "Aufgelöste run.yaml", retries=200)
                assert 'data-agent="a"' in html and "Limits und Freigaben" in html
                assert f"/neuer-run?wie={folder.name}" in html
    finally:
        lock.release()


# --- V8: Wiederholen ---------------------------------------------------------------------------


async def test_repeat_from_menu_and_list_fills_new_run_and_starts_the_same_run(wf: Path) -> None:
    api = make_api(wf)
    async with real_user(api) as user:
        first = await finished(api, form())
        await user.open(f"/runs/{first}")
        html = await see(user, "Wiederholen …", retries=200)
        assert f'href="/neuer-run?wie={first}"' in html, "⋯ → Wiederholen"
        await user.open("/runs")
        html = await see(user, f'data-mark="repeat-{first}"', retries=200)
        await user.open(f"/neuer-run?wie={first}")
        await see(user, "Rollen und Skills gefunden", retries=200)
        html = page_html(user)
        assert "Wiederholung von „Erster Versuch“" in html
        assert value(user, "field-name") == "Erster Versuch (Wiederholung)"
        assert value(user, "field-project") == "demo"
        assert "Zwei Ideen prüfen" in value(user, "field-task_md")
        assert value(user, "field-limits.wall_time_s") == "20 min"
        assert value(user, "field-limits.budget_usd") == "2 $"
        assert checked(user, "web-m-2") and not checked(user, "web-m-1")
        assert "~/Documents/Literatur" in html
        assert (wf / "runs").exists() and len(list((wf / "runs").iterdir())) == 1, "not started"
        user.find(marker="start-run").click()
        await see(user, 'data-rd="', retries=200)
        second = next(p.name for p in (wf / "runs").iterdir() if p.name != first)
        await until_final(api, second)
    old = yaml.safe_load((wf / "runs" / first / "run.yaml").read_text("utf-8"))
    new = yaml.safe_load((wf / "runs" / second / "run.yaml").read_text("utf-8"))
    assert (old.pop("name"), new.pop("name")) == ("Erster Versuch", "Erster Versuch (Wiederholung)")
    assert old == new


async def test_repeat_shows_a_project_and_a_skill_that_are_gone_at_the_field(wf: Path) -> None:
    api = make_api(wf)
    async with real_user(api) as user:
        first = await finished(api, form())
        (wf / "projects" / "zweites").mkdir(parents=True)  # not the only project
        (wf / "projects" / "zweites" / "CLAUDE.md").write_text("# Zweites\n", "utf-8")
        shutil.rmtree(wf / "projects" / "demo")
        await user.open(f"/neuer-run?wie={first}")
        html = await see(user, "demo – gibt es nicht mehr", retries=200)
        await asyncio.sleep(0.3)
        html = await see(user, "1 Problem", retries=200)
        assert value(user, "field-project") == "demo"
        repeat = (await api.get_run_inputs(first)).repeat
        problem = next(e for e in (await api.resolve_run(repeat)).errors if e.path == "project")
        # at the field (its error slot) and as a jump link in the check list
        assert html.count(problem.message) >= 2, problem.message
        (wf / "projects" / "demo").mkdir(parents=True)
        (wf / "projects" / "demo" / "CLAUDE.md").write_text("# Demo\n", "utf-8")
        shutil.rmtree(wf / "skills" / "runde")
        await user.open(f"/neuer-run?wie={first}")
        html = await see(user, "Diesen Skill gibt es im Workfolder nicht mehr.", retries=200)
        html = await see(user, "runde", retries=200)
        await see(user, "Problem", retries=200)
        assert len(list((wf / "runs").iterdir())) == 1, "nothing started"


# --- U9 ----------------------------------------------------------------------------------------


def test_costs_in_dollars_are_no_formula_real_formulas_stay() -> None:
    usage = (
        "## Usage\n\n- Gesamt: 2 Turns, ≈ 0,28 $ rechnerisch\n"
        "- `diskutant-1`: 1 Turn, ≈ 0,14 $ rechnerisch\n"
        "- `diskutant-2`: 1 Turn, ≈ 0,14 $ rechnerisch\n"
    )
    out = markdown_html(usage)
    assert 'class="math' not in out and out.count("$ rechnerisch") == 3
    out = markdown_html(r"Für $σ(n) = 4 \iff n ≡ 3 \pmod{16}$ gilt $$T^4(n) < n$$.")
    assert out.count('class="math"') == 1 and out.count('class="math math-display"') == 1
    assert markdown_html("$x$ und $y^2$").count('class="math"') == 2
    for text in ("Preis $5 und $6", "$ x $", "Kosten 5$ und 6$", r"\$5 bis \$7"):
        assert 'class="math' not in markdown_html(text), text
    assert "$5 bis $7" in markdown_html(r"\$5 bis \$7")
    assert 'class="math' not in markdown_html("`echo $HOME $PATH`")
    assert 'class="math' not in markdown_html("```\nprint('$x$')\n```")
    assert 'class="math' not in markdown_html("$a\n\nb$"), "no formula across paragraphs"


async def test_repeat_shows_a_profile_that_is_gone_at_the_agents(wf: Path, arr_home: Path) -> None:
    api = make_api(wf)
    async with real_user(api) as user:
        first = await finished(api, form())
        write_profiles(arr_home, {"anderes-profil": "claude_oauth"})
        api.state._profiles = None  # the profile page does this when a profile goes
        await user.open(f"/neuer-run?wie={first}")
        html = await see(user, "Probleme", retries=200)
        repeat = (await api.get_run_inputs(first)).repeat
        errors = (await api.resolve_run(repeat)).errors
        assert {e.path for e in errors} >= {"agents.m-1.profile", "agents.m-2.profile"}, errors
        message = next(e.message for e in errors if e.path == "agents.m-1.profile")
        assert "fake-auth" in message
        # the rows of the old run stand in the table, each with its error (check list + rows)
        html = await see(user, 'data-mark="agent-error-m-2"', retries=200)
        assert html.count(message) >= 3, html.count(message)
        assert len(list((wf / "runs").iterdir())) == 1, "nothing started"
