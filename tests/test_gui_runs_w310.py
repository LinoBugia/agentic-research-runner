"""W3-10 in the GUI on `RealApi` with FakeBackends: the view „Eingaben“ (V9), „Wiederholen“
from the ⋯ menu and the runs list into a filled „Neuer Run“ (V8), costs in `$` as text (U9).
"""

import asyncio
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from run_kit import turns, wait, write_profiles
from service_kit import append, hold_lock, make_run_folder, transcript_line
from test_gui_live_v4 import pages
from test_gui_real import page_html, see
from test_gui_runs_real import FORM, make_api, real_user, until, until_final, until_status, wf

from arr.models import ChannelMode, Decider, RunStatus
from arr.room.store import RoomStore
from arr.room.types import MessageKind
from arr.service.api_models import ChannelInfo, Message
from arr.service.gui import format as fmt
from arr.service.gui.components.base import esc, markdown_html
from arr.service.gui.components.transcript_event import rate_limit_text
from arr.service.gui.pages.run_detail_channel import ChannelView

__all__ = ["pages", "wf"]  # the fixtures

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


# --- W3-10.1 point 1: placeholders, finding 6: linear -------------------------------------------

PLACEHOLDER_WORDS = "ARRMATH0END ARRCODE0END ARRDOLLAR ARRMATH7END ARRCODE3END"


def test_placeholder_words_stay_text() -> None:
    for text in (PLACEHOLDER_WORDS, f"x {PLACEHOLDER_WORDS} $a$ `c` \\$5"):
        out = markdown_html(text)
        assert PLACEHOLDER_WORDS in out, out
    out = markdown_html(f"$a$ `b`\n\n```\nc\n```\n\n{PLACEHOLDER_WORDS}")
    assert 'data-tex="a"' in out and "<code>b</code>" in out and "<pre><code>c\n" in out
    assert "\x02" not in out and "\x03" not in out, "no placeholder is left"


def test_code_inside_a_formula_keeps_its_source() -> None:
    out = markdown_html("$a `b` c$ und ``x`y``")
    assert 'data-tex="a `b` c"' in out and "<code>x`y</code>" in out


async def test_placeholder_words_in_summary_and_task_never_break_the_page(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.DONE, name="Platzhalter")
    (folder / "output").mkdir(exist_ok=True)
    (folder / "output" / "summary.md").write_text(
        f"# Zusammenfassung\n\nDer Agent schrieb {PLACEHOLDER_WORDS} und $a$.\n", "utf-8"
    )
    (folder / "task.md").write_text(f"# Aufgabe\n\nPrüfe {PLACEHOLDER_WORDS}.\n", "utf-8")
    async with real_user(make_api(wf)) as user:
        await user.open(f"/runs/{folder.name}?ansicht=ergebnisse")
        await see(user, f"Der Agent schrieb {PLACEHOLDER_WORDS} und", retries=200)
        await user.open(f"/runs/{folder.name}?ansicht=eingaben")
        await see(user, f"Prüfe {PLACEHOLDER_WORDS}.", retries=200)


def test_markdown_stays_linear_with_many_code_pieces() -> None:
    """Finding 6: 400 KB of formulas and code fences took 9 s (markdown2 replaces its table of
    code pieces over the whole text once per piece); now code is rendered here."""
    text = "und $σ(n)$ ```\ncode\n``` t\n" * 16_000
    start = time.perf_counter()
    out = markdown_html(text)
    assert time.perf_counter() - start < 3.0, "quadratic again (was 9 s, now ≈ 0.2 s)"
    assert out.count("<code>code</code>") == 16_000  # ``` inside a line is a code span
    fenced = "und $σ(n)$\n```\ncode\n```\n" * 16_000
    start = time.perf_counter()
    out = markdown_html(fenced)
    assert time.perf_counter() - start < 3.0
    assert out.count("<pre><code>code\n</code></pre>") == 16_000


# --- W3-10.1 point 2: a secret in a parameter or a channel purpose ------------------------------


async def test_a_secret_in_a_parameter_or_purpose_is_not_on_the_inputs_page(wf: Path) -> None:
    folder = make_run_folder(wf, status=RunStatus.DONE, name="Geheim im Parameter")
    data = yaml.safe_load((folder / "run.yaml").read_text("utf-8"))
    data["topology"]["params"] = {"token": FAKE_KEY}
    data["groupchats"][0]["purpose"] = f"Zweck {FAKE_KEY}"
    (folder / "run.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), "utf-8")
    async with real_user(make_api(wf)) as user:
        await user.open(f"/runs/{folder.name}?ansicht=eingaben")
        html = await see(user, "Aufgelöste run.yaml", retries=200)
        assert 'data-param="token"' in html and "Zweck «entfernt»" in html
        assert FAKE_KEY not in html and "W310GUIVALUE" not in html


# --- W3-10.1 point 5: rate_limit (V5, W3-09 B10) ----------------------------------------------


def test_rate_limit_text_says_reached_only_for_rejected() -> None:
    at = 1_794_150_000.0  # a fixed time; the clock text comes from fmt.clock
    clock = fmt.clock(at)
    assert rate_limit_text({"status": "rejected", "resets_at": at}) == (
        f"Nutzungslimit erreicht · weiter um {clock}"
    )
    warning = rate_limit_text({"status": "allowed_warning", "resets_at": at})
    assert warning == (
        f"Hinweis zum Nutzungslimit: Fenster bis {clock}, Profil auf einen Turn gedrosselt"
    )
    for data in ({"status": "allowed", "resets_at": at}, {"status": "allowed_warning"}):
        assert "erreicht" not in rate_limit_text(data), data
    assert "erreicht" not in rate_limit_text({"status": "seltsam"})


async def test_rate_limit_lines_in_the_transcript(wf: Path) -> None:
    """Until W3-09.1 delivers `allowed_warning` again, the line comes from a fake transcript."""
    folder = make_run_folder(wf, status=RunStatus.DONE, agents=["a"], name="Limits")
    path = folder / "agents" / "a" / "transcript.jsonl"
    resets = time.time() + 3600
    lines = [
        transcript_line(1, "a", folder.name, "turn_start", {"turn": 1, "prompt": "Los."}),
        transcript_line(
            2, "a", folder.name, "rate_limit", {"status": "allowed_warning", "resets_at": resets}
        ),
        transcript_line(
            3, "a", folder.name, "rate_limit", {"status": "rejected", "resets_at": resets}
        ),
    ]
    append(path, "".join(f"{line}\n" for line in lines))
    async with real_user(make_api(wf)) as user:
        await user.open(f"/runs/{folder.name}?agent=a")
        html = await see(user, "Profil auf einen Turn gedrosselt", retries=200)
        clock = fmt.clock(resets)
        assert f"Hinweis zum Nutzungslimit: Fenster bis {clock}" in html
        assert f"Nutzungslimit erreicht · weiter um {clock}" in html
        assert html.count("Nutzungslimit erreicht") == 1, "only the rejected one"


# --- W3-10.1 point 6: „Antwort an X“ ------------------------------------------------------------


def ask_the_user(run_dir: Path, sender: str, body: str) -> int:
    """An agent's open question to `user`. Until W3-09 is merged an agent cannot address the
    user through the room server, so the test writes the row the way the room would."""
    store = RoomStore(run_dir)
    with store._write() as con:
        return store._insert_msg(con, "main", sender, MessageKind.QUESTION, body, to=["user"])


def message(run_dir: Path, msg_id: int) -> Any:
    return next(m for m in RoomStore(run_dir).messages_after(msg_id - 1) if m.id == msg_id)


def hint(user: Any) -> str:
    with user.client:
        box = next(iter(user.find(marker="answer-hint").elements))
        return " ".join(str(getattr(child, "content", "")) for child in box.default_slot.children)


async def running_with_composer(api: Any, user: Any) -> tuple[str, Path]:
    run_id = (await api.start_run(FORM)).run_id
    await until_status(api, run_id, RunStatus.RUNNING)
    await user.open(f"/runs/{run_id}?kanal=main")
    await see(user, "Notiz weckt niemanden", retries=200)
    return run_id, api.state.workfolder / "runs" / run_id


WAITING = {"m-1": turns([wait(30)]), "m-2": turns([wait(30)])}


async def test_an_open_question_offers_the_answer_and_closes_with_it(
    wf: Path, pages: list[Any]
) -> None:
    api = make_api(wf, WAITING)
    async with real_user(api) as user:
        _run_id, run_dir = await running_with_composer(api, user)
        assert hint(user).strip() == "", "no question, no line"
        asked = ask_the_user(run_dir, "m-1", "Welches Höhenband zuerst, 10–35 oder 35–60?")
        await see(user, "Antwort an m-1 senden", retries=200)
        assert "Offene Frage von <b>m-1</b>: „Welches Höhenband zuerst" in hint(user)
        user.find(marker="answer-accept").click()
        await see(user, "Geht als Antwort an <b>m-1</b>", retries=100)
        user.find(marker="send").trigger(
            "click", {"to": "", "kind": "note", "body": "Zuerst 10–35."}
        )
        answer = asked + 1

        async def sent() -> bool:
            return any(m.id == answer for m in RoomStore(run_dir).messages_after(asked))

        await until(sent, what="answer in the room")
        posted = message(run_dir, answer)
        assert (posted.sender, posted.kind, posted.to, posted.reply_to) == (
            "user",
            MessageKind.ANSWER,
            ["m-1"],
            asked,
        )
        assert not message(run_dir, asked).open, "the room closed the question"
        for _ in range(100):
            if "Offene Frage" not in hint(user) and "Geht als Antwort" not in hint(user):
                break
            await asyncio.sleep(0.05)
        assert hint(user).strip() == "", "after the answer the line goes"
        view = next(p for p in pages if p.run_id == _run_id).view
        assert not view.messages[asked].open, "the question row is drawn as answered"


async def test_declined_the_message_stays_a_note_to_everyone(wf: Path) -> None:
    api = make_api(wf, WAITING)
    async with real_user(api) as user:
        _run_id, run_dir = await running_with_composer(api, user)
        asked = ask_the_user(run_dir, "m-2", "Soll ich mpmath nehmen?")
        await see(user, "Antwort an m-2 senden", retries=200)
        user.find(marker="answer-accept").click()
        await see(user, "Doch als Notiz an alle", retries=100)
        user.find(marker="answer-decline").click()
        await see(user, "Antwort an m-2 senden", retries=100)
        user.find(marker="send").trigger(
            "click", {"to": "", "kind": "note", "body": "Kurze Notiz."}
        )

        async def sent() -> bool:
            return any(m.id == asked + 1 for m in RoomStore(run_dir).messages_after(asked))

        await until(sent, what="note in the room")
        posted = message(run_dir, asked + 1)
        assert (posted.kind, posted.to, posted.reply_to) == (MessageKind.NOTE, [], None)
        assert message(run_dir, asked).open, "a note to everyone answers no question"
        assert "Offene Frage von <b>m-2</b>" in hint(user), "the line stays"
        assert "Antwort an m-2 senden" in page_html(user)


def test_questions_of_the_inbox_count_too() -> None:
    """After W3-09 an inbox item `question` carries `channel`, `msg_id` and the agent; the line
    takes it, also when the channel message is not among the scanned ones."""
    item = SimpleNamespace(
        type="question",
        channel="main",
        msg_id=7,
        agent=SimpleNamespace(id="diskutant-1"),
        question_text="Welches Band?",
        title="diskutant-1 fragt dich",
    )
    other = SimpleNamespace(type="question", channel="nebenbei", msg_id=9, agent=item.agent)
    permission = SimpleNamespace(type="permission", title="pip install")
    page = SimpleNamespace(
        open_items={"q": item, "o": other, "p": permission},
        questions={
            ("main", 4): Message(
                id=4,
                ts=0,
                channel="main",
                sender="diskutant-2",
                kind=MessageKind.QUESTION,
                body="Und du?",
                to=["user"],
                open=True,
            ),
            ("main", 5): Message(
                id=5,
                ts=0,
                channel="main",
                sender="diskutant-2",
                kind=MessageKind.QUESTION,
                body="An m-1",
                to=["diskutant-1"],
                open=True,
            ),
        },
    )
    view = ChannelView(page, ChannelInfo(name="main", mode=ChannelMode.EVENT))  # type: ignore[arg-type]
    assert view.user_questions() == [
        ("diskutant-1", 7, "Welches Band?"),
        ("diskutant-2", 4, "Und du?"),
    ]


# --- W3-10.1 point 7: „Aufgabe“ once ------------------------------------------------------------


def test_the_task_heading_aufgabe_stands_once() -> None:
    from arr.service.api_models import RunInputs
    from arr.service.gui.pages.run_detail_inputs import inputs_html

    task = "# Aufgabe\n\n## Forschungsfrage\nIst 7 prim?\n"
    html = inputs_html(RunInputs(run_id="r", name="n", available=False, project="p", task_md=task))
    assert html.count(">Aufgabe<") == 1, "the section title only"
    assert "<h2>Forschungsfrage</h2>" in html and "Ist 7 prim?" in html
    assert f'data-copy="{esc(task)}"' in html, "„Kopieren“ keeps the heading"
    other = inputs_html(
        RunInputs(run_id="r", name="n", available=False, project="p", task_md="# Ziel\n\nx")
    )
    assert "<h1>Ziel</h1>" in other, "another first heading stays"


# --- W3-10.1 point 8: limits of the skill, effort of the preview, `$x$5` ------------------------

STRICT = (
    "kind: research-group\nversion: 1\ntopology: roundtable\n"
    "params:\n  rounds: {default: 2, min: 1, max: 5, doc: Runden}\n"
    "defaults: {effort: high}\n"
    "limits: {turns_per_agent: 4}\n"
    "agents:\n  - {id: m, count: 2}\n"
    "groupchats:\n  - {name: main, members: [m-*]}\n"
)


def strict_skill(wf: Path) -> None:
    group = wf / "skills" / "streng"
    group.mkdir(parents=True)
    (group / "SKILL.md").write_text("---\nname: streng\ndescription: Streng.\n---\n# Streng\n")
    (group / "group.yaml").write_text(STRICT)


async def test_repeat_keeps_a_limit_equal_to_the_standard_over_the_skill(wf: Path) -> None:
    """The old run had 6 turns (= the standard of arr.yaml), its skill says 4: the repeat must
    send the 6, or the skill's value would win and the run.yaml would differ."""
    strict_skill(wf)
    api = make_api(wf)
    async with real_user(api) as user:
        first = await finished(api, {**FORM, "skill": "streng", "limits": {"turns_per_agent": 6}})
        old = yaml.safe_load((wf / "runs" / first / "run.yaml").read_text("utf-8"))
        assert old["limits"]["turns_per_agent"] == 6
        await user.open(f"/neuer-run?wie={first}")
        await see(user, "Rollen und Skills gefunden", retries=200)
        assert value(user, "field-limits.turns_per_agent") == "6"
        user.find(marker="start-run").click()
        await see(user, 'data-rd="', retries=200)
        second = next(p.name for p in (wf / "runs").iterdir() if p.name != first)
        await until_final(api, second)
    new = yaml.safe_load((wf / "runs" / second / "run.yaml").read_text("utf-8"))
    assert new["limits"]["turns_per_agent"] == 6
    old.pop("name"), new.pop("name")
    assert old == new


async def test_the_agents_table_shows_the_effort_of_the_preview(wf: Path) -> None:
    """arr.yaml says effort low, the skill high: the table follows the resolved run."""
    strict_skill(wf)
    async with real_user(make_api(wf)) as user:
        await user.open("/neuer-run?skill=streng&projekt=demo")
        html = await see(user, "Rollen und Skills gefunden", retries=200)
        html = await see(user, "fake-auth · Effort high", retries=100)
        assert "Effort low" not in html


def test_a_closing_dollar_before_a_digit_is_no_formula() -> None:
    assert 'class="math' not in markdown_html("Preis $x$5 und $y$7")
    assert markdown_html("Preis $x$ 5").count('class="math"') == 1
