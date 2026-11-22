"""W4b-06a A8-A12: briefs, rules, prompts and texts for both kinds of run, and the own alias.

- A8: the brief of the private channel after Entscheidung 69, word for word and in behavior
  (probe `np_n2_and_brief.py`, parts 1 and 2, of the Nachprüfung W4b-03.1).
- A9: the brief of a group channel: in a `phases` run rule 4 after Entscheidung 69 and rule 8
  with `submit` (G8); in a run without phases character for character as on `main`; without
  `run.yaml` the old one.
- A10 (B2): the project context names the own alias per anonymous channel, as the room has it.
- A11: the prompts and the answers of the room server are right for both kinds of run.
- A12: `prepare.py` names `submit` in `phases` runs, `submit_result` in the others.
"""

import re
import shutil
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from phases_kit import (
    TurnView,
    answer,
    entries,
    errors,
    hand_in,
    manager,
    of_kind,
    private,
    run,
    run_with,
    say,
    solo,
)
from run_kit import eventually, make_workfolder, room
from templates_kit import new_manager

from arr.models import ChannelMode, GroupChatConfig, RunStatus, WorkspaceMode
from arr.room import server
from arr.room.store import (
    CONVERSATION_RULES,
    PHASES_CONVERSATION_RULES,
    PRIVATE_BRIEF,
    USER,
    RoomStore,
    channel_aliases,
)
from arr.room.types import MessageKind, MessageVia
from arr.room.waiting import WaitEnd, WaitOutcome
from arr.runs import pacing, prepare
from arr.runs.phases import wake

ASK = "@user Darf ich annehmen, dass n prim ist?"
AGENTS = {"agents": [{"id": "a"}]}

MAIN_JUDGES_BRIEF = """Steckbrief #judges
Zweck: Diskussion der Judges
Mitglieder:
- Mitglied A (judge)
- Mitglied B
Der Kanal ist anonym: Mitglieder erscheinen nur als „Mitglied A/B/…“.
Taktung: Runden: Alle Mitglieder kommen pro Runde dran, sonst `pass`.
Länge einer Nachricht: höchstens 3000 Zeichen.
Gesprächsregeln:
1. Eine Nachricht, ein Gedanke. Setze `kind` korrekt.
2. Behauptungen mit Begründung oder Beleg: Datei, Rechnung, Anhang.
3. Beantworte Fragen an dich zuerst.
4. Wer jemanden fragt, schreibt `@name` (oder `to`); `@alle` fragt alle, `@user` den User \
(nur er antwortet darauf). Auf eine Antwort wartest du mit `wait_for_messages`, auf den User \
nie mit `wait_until`: Antwortet er nicht, arbeite mit einer begründeten Annahme weiter und \
nenne sie in deinem Ergebnis.
5. Widerspruch als `objection` mit `reply_to`, sachlich und konkret.
6. Nicht wiederholen. Zustimmung nur, wenn sie etwas hinzufügt.
7. Lange Inhalte (Beweise, Code, Tabellen) als Anhang, im Text nur die Kernaussage.
8. Wer seinen Teil erledigt hat, gibt sein Ergebnis ab (`submit_result` oder Nachricht \
`result`) oder meldet `done`. Fertig heißt: Du wartest nicht mehr, bleibst aber ansprechbar; \
jede Nachricht an dich weckt dich. Wartest du auf das Ergebnis eines anderen, warte mit \
`wait_until(minutes, until_result_from="<name>")`.
Ende: nach der letzten Runde oder wenn alle `pass`/`done` melden."""
"""The brief of a run without phases as `main` (`cf8dd55`) writes it, character for character
(generated there with the same channel)."""

MAIN_USER_MAY_NOT_ANSWER = (
    "Deine Frage an den User ist noch offen, und er antwortet vielleicht lange nicht. Warte "
    "nicht länger auf ihn, auch nicht mit `wait_until`: Arbeite mit einer begründeten Annahme "
    "weiter und nenne sie in deinem Ergebnis. Antwortet er später, weckt dich das; prüfe dann "
    "deine Annahme."
)
MAIN_USER_WAITED_LONG = (
    "Beachte: Deine Frage an den User ist noch offen, und er antwortet vielleicht lange nicht. "
    "Warte danach nicht noch einmal auf ihn: Arbeite besser mit einer begründeten Annahme weiter "
    "und nenne sie in deinem Ergebnis. Eine späte Antwort weckt dich auch dann; prüfe dann deine "
    "Annahme."
)
JUDGES = GroupChatConfig(
    name="judges",
    purpose="Diskussion der Judges",
    members=["judge-1", "judge-2"],
    mode=ChannelMode.ROUNDS,
    anonymous=True,
)


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


@pytest.fixture
def hold(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    value = {"s": 600.0}
    monkeypatch.setattr(wake, "user_question_hold_s", lambda: value["s"])
    monkeypatch.setattr(pacing, "user_question_hold_s", lambda: value["s"])
    return value


def first(view: TurnView) -> bool:
    return f"# Auftrag – Phase „{view.stage}“" in view.prompt


def rules_of(brief: str) -> list[str]:
    return re.findall(r"^\d\. (.*)$", brief, flags=re.MULTILINE)


# --------------------------------------------------------------------------------------------
# A9: the brief of a group channel per kind of run
# --------------------------------------------------------------------------------------------


def test_a9_without_run_yaml_the_brief_is_that_of_main(tmp_path: Path) -> None:
    store = RoomStore.create(tmp_path / "run")
    brief = store.create_channel(JUDGES, roles={"judge-1": "judge"}).body
    assert brief == MAIN_JUDGES_BRIEF


def test_a9_the_rules_of_runs_without_phases_are_unchanged() -> None:
    assert rules_of(MAIN_JUDGES_BRIEF) == list(CONVERSATION_RULES)


def test_a9_the_rules_of_phases_runs() -> None:
    old, new = CONVERSATION_RULES, PHASES_CONVERSATION_RULES
    assert len(new) == len(old) == 8
    assert [i for i in range(8) if old[i] != new[i]] == [3, 7], "only rules 4 and 8 differ"
    rule4, rule8 = new[3], new[7]
    assert "Auf einen Agenten wartest du mit `wait_for_messages`." in rule4
    assert "Brauchst du die Antwort des Users, warte mit `wait_until` bis zu fünf Minuten" in rule4
    # N5 (W4b-06a.1): in a group channel only an answer ends the wait, in the private channel
    # also a note (`room/address.py`, `runs/phases/wake.py`)
    assert (
        "eine Antwort des Users (in deinem privaten Kanal auch eine Notiz) beendet das Warten."
        in rule4
    )
    assert "begründeten Annahme" in rule4 and "in deiner Abgabe" in rule4
    assert "Wer abgibt, ist fertig; seine offene Frage gilt damit als erledigt." in rule4
    assert "nie mit `wait_until`" not in rule4
    assert "(`submit`)" in rule8 and "submit_result" not in rule8 and "`result`" not in rule8
    assert "until_result_from" in rule8
    # in a phase only a sleeper is woken; a done agent with an action is not (W4b-03 §5.3)
    assert "weckt dich" not in rule8


async def test_a9_a_phases_run_writes_the_phases_rules(workfolder: Path) -> None:
    mgr, _log = new_manager(
        workfolder, "proof-review", {"author": answer(), "judge-1": answer(), "judge-2": answer()}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    brief = mgr.room.messages("judges")[0].body
    assert rules_of(brief) == list(PHASES_CONVERSATION_RULES)
    assert "submit_result" not in brief


async def test_a9_run_yaml_is_read_when_the_channel_is_made(
    workfolder: Path, tmp_path: Path
) -> None:
    """`_resolved()` does not hold „no run.yaml“ too early: a store made before `run.yaml`
    (as `RunManager._setup` never does, it comes after `prepare_run`) still writes the phases
    rules once the file is there."""
    mgr, _log = new_manager(
        workfolder, "proof-review", {"author": answer(), "judge-1": answer(), "judge-2": answer()}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    target = tmp_path / "later"
    store = RoomStore.create(target)
    shutil.copyfile(mgr.run_dir / "run.yaml", target / "run.yaml")
    brief = store.create_channel(JUDGES).body
    assert rules_of(brief) == list(PHASES_CONVERSATION_RULES)


# --------------------------------------------------------------------------------------------
# A8: the brief of the private channel
# --------------------------------------------------------------------------------------------


def test_a8_the_private_brief_after_decision_69() -> None:
    text = PRIVATE_BRIEF.format(name="user.a")
    assert text.startswith("Steckbrief #user.a\nDein privater Kanal zum User:")
    assert "Hier fragst du ihn mit einer `question` an `@user`, in jeder Phase" in text
    assert "Nur der User liest diesen Kanal" in text
    assert "Brauchst du die Antwort, warte mit `wait_until` bis zu fünf Minuten" in text
    assert "eine Antwort oder Notiz des Users beendet das Warten" in text
    assert "Gibst du ab, gilt deine Frage als erledigt." in text
    assert "beende den Turn ohne Abgabe" not in text and "Die Phase wartet" not in text


async def test_a8_part_1_a_turn_end_without_submission_does_not_hold(
    workfolder: Path, hold: Any
) -> None:
    """What the old sentence promised does not happen (W4b-03.1 NR1): `a` asks and ends its turn
    without handing in; turn 2 begins at once, without an answer. The brief no longer says
    otherwise."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        if first(view):
            return [say(private("a"), ASK, kind="question")]
        return [{"do": "text", "text": "weiter"}]

    async def user(mgr: Any) -> None:
        await eventually(lambda: len(of_kind(mgr, "turn", agent="a")) >= 2, within=30)
        hold["s"] = 0.0  # the hold after the last turn ends at once

    mgr, log = manager(workfolder, {**AGENTS, "phases": [solo("p", "a", turns=3)]}, {"a": a})
    assert await run_with(mgr, user) is RunStatus.DONE, errors(mgr)
    order = [(k, d.get("reason")) for k, d in entries(mgr, "turn", "waiting")]
    second = [i for i, x in enumerate(order) if x[0] == "turn"][1]
    assert not any(x == ("waiting", "user") for x in order[:second])
    assert "Turn 2 von 3" in log.of("a")[1].prompt
    brief = mgr.room.messages(private("a"))[0].body
    assert brief == PRIVATE_BRIEF.format(name=private("a"))


@pytest.mark.parametrize("how", ["note", "answer"])
async def test_a8_part_2_wait_until_ends_by_the_user(workfolder: Path, how: str) -> None:
    """What the brief says: `a` waits with `wait_until` (five minutes); a note (no `@a`) or an
    answer of the user ends the wait, and turn 2 shows it."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        if first(view):
            return [say(private("a"), ASK, kind="question"), room("wait_until", minutes=5)]
        return hand_in(view)

    async def user(mgr: Any) -> None:
        await eventually(lambda: bool(of_kind(mgr, "waiting", agent="a", reason="wait_until")), 30)
        if how == "note":
            mgr.room.post(private("a"), "Ja, n ist prim.", sender=USER, via=MessageVia.GUI)
        else:
            [q] = [m for m in mgr.room.messages(private("a")) if m.kind is MessageKind.QUESTION]
            mgr.room.post(
                private("a"),
                "Ja, n ist prim.",
                sender=USER,
                via=MessageVia.GUI,
                kind=MessageKind.ANSWER,
                to=["a"],
                reply_to=q.id,
            )

    mgr, log = manager(workfolder, {**AGENTS, "phases": [solo("p", "a")]}, {"a": a})
    assert await run_with(mgr, user) is RunStatus.DONE, errors(mgr)
    second = log.of("a")[1].prompt
    assert "Eine Nachricht an dich ist da" in second and "Ja, n ist prim." in second


async def test_a8_handing_in_closes_the_open_question(workfolder: Path, hold: Any) -> None:
    """Entscheidung 67: `a` asks and hands in in the same turn; the question is closed, the
    phase does not wait, no turn follows."""

    def a(view: TurnView) -> list[dict[str, Any]]:
        return [say(private("a"), ASK, kind="question"), *hand_in(view)]

    mgr, log = manager(workfolder, {**AGENTS, "phases": [solo("p", "a")]}, {"a": a})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    assert len(log.of("a")) == 1
    assert [d["state"] for d in of_kind(mgr, "user_question")][-1] == "closed"
    assert not [d for d in of_kind(mgr, "waiting") if d.get("reason") == "user"]


# --------------------------------------------------------------------------------------------
# A10 (B2): the own alias in the project context
# --------------------------------------------------------------------------------------------


def _members(run_dir: Path, channel: str) -> dict[str, str | None]:
    con = sqlite3.connect(run_dir / "groupchats" / "room.sqlite")
    try:
        rows = con.execute(
            "SELECT agent, alias FROM member WHERE channel=? ORDER BY rowid", (channel,)
        ).fetchall()
    finally:
        con.close()
    return dict(rows)


async def test_a10_every_judge_learns_its_own_alias(workfolder: Path) -> None:
    judges = [f"judge-{i}" for i in (1, 2, 3)]
    policies = {"author": answer(), **{j: answer() for j in judges}}
    mgr, _log = new_manager(workfolder, "proof-review", policies, params={"judges": 3})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    table = _members(mgr.run_dir, "judges")
    assert set(table) == set(judges) and len(set(table.values())) == 3
    config = mgr.prepared.resolved.channel("judges")
    assert channel_aliases(config) == table, "the helper names what the room stored"
    for judge in judges:
        context = (mgr.run_dir / "agents" / judge / "context.md").read_text("utf-8")
        work = mgr.run_dir / "agents" / judge / "work"
        texts = [context] + [
            (work / n).read_text("utf-8")
            for n in ("CLAUDE.md", "AGENTS.md")
            if (work / n).is_file()
        ]
        for text in texts:
            assert f"- In #judges heißt du „{table[judge]}“." in text
            assert text.count("heißt du") == 1
            for other in judges:
                if other != judge:  # no text ties another one's alias to its ID
                    assert other not in text
                    assert f"heißt du „{table[other]}“" not in text
    author = (mgr.run_dir / "agents" / "author" / "context.md").read_text("utf-8")
    assert "heißt du" not in author, "the author is not in #judges"
    brief = mgr.room.messages("judges")[0].body
    assert not any(j in brief for j in judges), "the brief shows aliases only"


async def test_a10_a_channel_that_is_not_anonymous_names_no_alias(workfolder: Path) -> None:
    def talking(view: TurnView) -> list[dict[str, Any]]:
        return hand_in(view)

    policies = {"diskutant-1": talking, "diskutant-2": talking, "summarizer": answer()}
    mgr, _log = new_manager(workfolder, "roundtable", policies, params={"rounds": 1})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    for agent in ("diskutant-1", "diskutant-2", "summarizer"):
        context = (mgr.run_dir / "agents" / agent / "context.md").read_text("utf-8")
        assert "heißt du" not in context and "Mitglied" not in context


async def test_a10_a_shared_folder_names_no_alias(workfolder: Path) -> None:
    mgr, _log = new_manager(
        workfolder, "proof-review", {"author": answer(), "judge-1": answer(), "judge-2": answer()}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    resolved = mgr.prepared.resolved
    block = prepare._research_block("r", resolved, None, WorkspaceMode.SHARED, "gemeinsam")
    assert "heißt du" not in block and "Mitglied" not in block


# --------------------------------------------------------------------------------------------
# A11: prompts and answers of the room server
# --------------------------------------------------------------------------------------------

BACKENDS = ("claude.md", "codex.md")


def prompt_text(name: str) -> str:
    return resources.files("arr").joinpath("templates", "prompts", name).read_text("utf-8")


def bullet(text: str, title: str) -> str:
    [line] = [line for line in text.splitlines() if line.startswith(f"- **{title}")]
    return line


@pytest.mark.parametrize("title", ["Runs mit Phasen", "Dein Kanal zum User"])
def test_a11_the_phases_bullets_are_the_same_in_both_backends(title: str) -> None:
    claude, codex = (bullet(prompt_text(n), title) for n in BACKENDS)
    assert claude == codex
    assert "In einem Run mit Phasen" in claude


def test_a11_the_private_channel_after_decisions_67_and_69() -> None:
    text = bullet(prompt_text("claude.md"), "Dein Kanal zum User")
    for part in (
        "privaten Kanal `#user.<deine-id>`",
        "Nur der User liest ihn.",
        "mit einer `question` an `@user`, in jeder Phase",
        "Brauchst du die Antwort, warte mit `wait_until` bis zu fünf Minuten",
        "eine Antwort oder Notiz des Users beendet das Warten",
        "Gibst du ab, ist deine Frage erledigt",
        "nenne die Annahme, mit der du gearbeitet hast, in der Abgabe",
    ):
        assert part in text, part
    assert "ohne Abgabe" not in text and "Die Phase wartet" not in text  # Punkt 8, Satz 1


def test_a11_on_call_and_what_counts() -> None:
    text = bullet(prompt_text("claude.md"), "Runs mit Phasen")
    assert "Abstimmungen und Moderator-Werkzeuge gibt es nicht" in text
    assert "was zählt, steht in den Abgaben" in text
    # Punkt 8, Satz 2: woken by a message to it and by the user in its private channel; NR2
    # is open, so the text says „Notiz“, not „jede Nachricht“
    assert "(`@name`, eine Frage an dich, eine Antwort auf deine Nachricht)" in text
    assert "oder eine Notiz des Users in deinem privaten Kanal" in text
    assert "jede Nachricht des Users" not in text


@pytest.mark.parametrize("name", BACKENDS)
def test_a11_done_for_both_kinds_of_run(name: str) -> None:
    text = bullet(prompt_text(name), "Ergebnisse und fertig")
    assert "`submit_result` in einem Run ohne Phasen, `submit` in einem Run mit Phasen" in text
    assert "In einem Run ohne Phasen gilt:" in text
    assert "In einem Run mit Phasen bist du mit deinem Teil fertig, sobald du abgegeben hast" in (
        text
    )
    assert "eine offene Frage an den User gilt mit der Abgabe als erledigt" in text
    assert (
        "In einer Diskussion `chat` bist du fertig, sobald du abgegeben hast und keinen anderen "
        "Diskutanten mehr fragst." in text
    )
    # every mention of an old way to be done is bound to runs without phases
    tail = text.split("In einem Run mit Phasen bist du")[1]
    assert "submit_result" not in tail and "set_status" not in tail


@pytest.mark.parametrize("name", BACKENDS)
def test_a11_v10_holds_in_runs_without_phases(name: str) -> None:
    text = bullet(prompt_text(name), "Ansprechen und warten")
    assert (
        "In einem Run ohne Phasen wartest du auf den User nur mit `wait_for_messages`, nie mit "
        "`wait_until`: Antwortet er nicht, arbeite mit einer begründeten Annahme weiter und "
        "nenne sie in deinem Ergebnis." in text
    )
    assert "In einem Run mit Phasen wartest du auf ihn wie unter „Dein Kanal zum User“." in text
    assert "Auf den User wartest du nur" not in text, "no sentence for every kind of run"


@pytest.mark.parametrize("name", BACKENDS)
def test_a11_no_sentence_names_submit_result_alone(name: str) -> None:
    """`submit_result` and `set_status` appear only where a run without phases is named."""
    for line in prompt_text(name).splitlines():
        for sentence in re.split(r"(?<=[.:])\s+(?=[A-ZÄÖÜ„])", line):
            if "submit_result" in sentence or "set_status" in sentence:
                context = line[: line.index(sentence) + len(sentence)]
                assert "ohne Phasen" in context, sentence


def _outcome() -> WaitOutcome:
    return WaitOutcome(ended_by=WaitEnd.NOBODY, waited_s=300, remaining_s=0, limit_s=45, asked_s=45)


def test_a11_the_room_answers_of_runs_without_phases_are_those_of_main() -> None:
    assert server.USER_MAY_NOT_ANSWER == MAIN_USER_MAY_NOT_ANSWER
    assert server.USER_WAITED_LONG == MAIN_USER_WAITED_LONG
    old = server.render_wait(_outcome(), asked_user=True)
    assert MAIN_USER_MAY_NOT_ANSWER in old
    assert old == server.render_wait(_outcome(), asked_user=True, phases=False)
    until = server.render_wait_until(4e9, None, asked_user=True)
    assert until.endswith(f" {MAIN_USER_WAITED_LONG}")


def test_a11_the_room_answers_of_phases_runs_allow_one_wait() -> None:
    text = server.render_wait(_outcome(), asked_user=True, phases=True)
    assert server.USER_MAY_NOT_ANSWER_PHASES in text
    assert "warte einmal mit `wait_until` bis zu fünf Minuten" in text
    assert "auch nicht mit `wait_until`" not in text
    until = server.render_wait_until(4e9, None, asked_user=True, phases=True)
    assert until.endswith(f" {server.USER_WAITED_LONG_PHASES}")
    assert "höchstens fünf Minuten" in until and "Gibst du ab, gilt deine Frage" in until
    # without an open question both kinds say the same
    assert server.render_wait(_outcome(), phases=True) == server.render_wait(_outcome())


# --------------------------------------------------------------------------------------------
# A12: `prepare.py`
# --------------------------------------------------------------------------------------------


async def test_a12_a_phases_run_names_submit(workfolder: Path) -> None:
    mgr, _log = new_manager(
        workfolder, "proof-review", {"author": answer(), "judge-1": answer(), "judge-2": answer()}
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    resolved = mgr.prepared.resolved
    for agent in resolved.agents:
        context = (mgr.run_dir / "agents" / agent.id / "context.md").read_text("utf-8")
        assert "- Ergebnisse gibst du mit `submit` ab." in context
        assert "submit_result" not in context
    assert prepare.default_role(resolved) == prepare.DEFAULT_ROLE_PHASES
    assert "`submit`" in prepare.DEFAULT_ROLE_PHASES
    assert "submit_result" not in prepare.DEFAULT_ROLE_PHASES


async def test_a12_a_run_without_role_gets_the_role_of_its_kind(workfolder: Path) -> None:
    mgr, _log = manager(workfolder, {**AGENTS, "phases": [solo("p", "a")]}, {"a": answer()})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    role = (mgr.run_dir / "agents" / "a" / "role.md").read_text("utf-8")
    assert role == prepare.DEFAULT_ROLE_PHASES


# --------------------------------------------------------------------------------------------
# A11: the answers of the room server through the real tools (Festlegung des Leads, 22.11.)
# --------------------------------------------------------------------------------------------


def _room(tmp_path: Path, *, phases: bool) -> RoomStore:
    """A room of two agents `a`, `b` and a channel `main`; with `phases` its `run.yaml` is that
    of a `phases` run, without it there is none (a run without phases)."""
    from phases_kit import FAKE_DEFAULTS
    from run_kit import make_settings

    from arr.models import RunInput
    from arr.resolve import dump_yaml_model, resolve_run, validate_model

    run_dir = tmp_path / "run"
    for agent in ("a", "b"):
        (run_dir / "agents" / agent / "work").mkdir(parents=True)
    channel = {"name": "main", "members": ["a", "b"]}
    if phases:
        data = {
            "project": "demo",
            "task": "x",
            "topology": {"kind": "phases"},
            "defaults": FAKE_DEFAULTS,
            "agents": [{"id": "a"}, {"id": "b"}],
            "channels": [channel],
            "phases": [solo("p", "a")],
        }
        resolved = resolve_run(validate_model(RunInput, data), None, make_settings())
        (run_dir / "run.yaml").write_text(dump_yaml_model(resolved), encoding="utf-8")
    store = RoomStore.create(run_dir)
    store.setup_channels([GroupChatConfig.model_validate(channel)])
    return store


@pytest.mark.parametrize("phases", [False, True], ids=["ohne-phasen", "phasen"])
async def test_a11_the_wait_tools_answer_per_kind_of_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phases: bool
) -> None:
    from mcp.client.client import Client

    from arr.room import waiting

    monkeypatch.setattr(waiting, "STREAK_MAX_S", 0.3)
    store = _room(tmp_path, phases=phases)
    a = store.for_agent("a")
    assert store.uses_phases() is phases
    a.post_message("main", "@user welches Band?", MessageKind.QUESTION)
    async with Client(server.build_server(a)) as client:
        nobody = (await client.call_tool("wait_for_messages", {"seconds": 5})).content[0].text
        until = (await client.call_tool("wait_until", {"minutes": 2})).content[0].text
    if phases:
        assert server.USER_MAY_NOT_ANSWER_PHASES in nobody
        assert server.USER_MAY_NOT_ANSWER not in nobody
        assert server.USER_WAITED_LONG_PHASES in until
        assert server.USER_WAITED_LONG not in until
    else:  # word for word as on `main`
        assert MAIN_USER_MAY_NOT_ANSWER in nobody
        assert server.USER_MAY_NOT_ANSWER_PHASES not in nobody
        assert MAIN_USER_WAITED_LONG in until
        assert server.USER_WAITED_LONG_PHASES not in until
