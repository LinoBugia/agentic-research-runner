"""W4b-07 §6.1, §6.16 (A19): progress lines of `describe_entry` and `describe_checkpoint`.

German lines for the checkpoints and log kinds of a phases run; the lines of the old
topologies word for word as before W4b-07; every line exactly one line, shortened, also for
unknown kinds and hostile values; unexpected types never raise. Escaping for HTML is the
GUI's job and not tested here.
"""

import unicodedata
from typing import Any

import pytest

from arr.runs.inspect import describe_checkpoint
from arr.runs.launch import describe_entry

# Lines of the old topologies, taken from `describe_entry` before W4b-07 (word for word).
OLD_LINES = [
    ({"kind": "status", "data": {"status": "running"}}, "Status: läuft (running)"),
    ({"kind": "status", "data": {"status": "weird"}}, "Status: weird (weird)"),
    (
        {"kind": "checkpoint", "data": {"stage": "plan", "stages": ["entwurf", "pruefung"]}},
        "● Plan: entwurf → pruefung",
    ),
    ({"kind": "checkpoint", "data": {"stage": "plan", "stages": []}}, "● Plan: keine Stufen"),
    (
        {
            "kind": "checkpoint",
            "data": {
                "stage": "entwurf",
                "attempt": 2,
                "agent": "prover",
                "result": 3,
                "score": 7.5,
                "ratings": 2,
                "decision": "rework",
            },
        },
        "● Stufe „entwurf“, Versuch 2 (prover): Nacharbeit, Score 7,5",
    ),
    (
        {
            "kind": "checkpoint",
            "data": {
                "stage": "entwurf",
                "attempt": 1,
                "agent": "prover",
                "result": None,
                "score": None,
                "ratings": 0,
                "reason": "no_result",
                "decision": "failed",
            },
        },
        "● Stufe „entwurf“, Versuch 1 (prover): nicht bestanden (kein Ergebnis)",
    ),
    (
        {"kind": "checkpoint", "data": {"stage": "solo", "decision": "passed"}},
        "● Stufe „solo“, Versuch – (–): bestanden",
    ),
    (
        {
            "kind": "checkpoint",
            "data": {
                "stage": "entwurf",
                "phase": "stage_end",
                "outcome": "passed",
                "accepted": 4,
                "attempt": 2,
                "score": 8.0,
                "attempts": 2,
            },
        },
        "● Stufe „entwurf“ abgeschlossen: bestanden, akzeptiert #4",
    ),
    (
        {
            "kind": "checkpoint",
            "data": {
                "stage": "entwurf",
                "phase": "stage_end",
                "outcome": "best_accepted",
                "accepted": None,
                "score": None,
                "attempts": 3,
            },
        },
        "● Stufe „entwurf“ abgeschlossen: bester Versuch übernommen",
    ),
    (
        {
            "kind": "checkpoint",
            "data": {
                "stage": "entwurf",
                "phase": "stage_end",
                "outcome": "stage_failed",
                "reason": "stage_failed",
                "accepted": None,
                "score": 3.0,
                "attempts": 3,
            },
        },
        "● Stufe „entwurf“ abgeschlossen: nicht bestanden",
    ),
    (
        {
            "kind": "checkpoint",
            "data": {"stage": "diskussion", "channel": "judges", "phase": "discussion"},
        },
        "● Stufe „diskussion“: Diskussion in #judges",
    ),
    (
        {"kind": "checkpoint", "data": {"stage": "ideen", "ideas": 3, "source": "vorgegeben"}},
        "● Checkpoint „ideen“ (ideas=3, source=vorgegeben)",
    ),
    (
        {"kind": "checkpoint", "data": {"stage": "bewertung", "phase": "end", "results": 2}},
        "● Checkpoint „bewertung“ (phase=end, results=2)",
    ),
    ({"kind": "checkpoint", "data": {"stage": "artefakt"}}, "● Checkpoint „artefakt“"),
    (
        {"kind": "turn", "data": {"agent": "prover", "turn": 3, "stage": "entwurf"}},
        "▶ prover: Turn 3 · Stufe „entwurf“",
    ),
    ({"kind": "turn", "data": {"agent": "prover", "turn": 1}}, "▶ prover: Turn 1"),
    (
        {
            "kind": "turn_end",
            "data": {"agent": "prover", "turn": 3, "status": "ok", "cost_usd": 0.125},
        },
        "■ prover: Turn 3 beendet (ok, 0,12 $)",
    ),
    (
        {"kind": "turn_end", "data": {"agent": "prover", "turn": 4, "status": "error"}},
        "■ prover: Turn 4 beendet (Fehler)",
    ),
    (
        {"kind": "limit", "data": {"limit": "wall_time"}},
        "Limit erreicht: Die Gesamtlaufzeit des Runs ist erreicht.",
    ),
    ({"kind": "limit", "data": {"limit": "other"}}, "Limit erreicht: other"),
    (
        {"kind": "stop", "data": {}},
        "Stopp: laufende Turns werden unterbrochen, danach läuft der Finalizer.",
    ),
    ({"kind": "pause", "data": {"reason": "escalation"}}, "Pause (escalation): neue Turns warten."),
    ({"kind": "resume", "data": {"reason": "escalation"}}, "Weiter (escalation)."),
    (
        {"kind": "rate_limit", "data": {"profile": "max", "status": "rejected", "action": "pause"}},
        "Rate-Limit bei Profil „max“ (rejected): pausiert bis –",
    ),
    (
        {
            "kind": "rate_limit",
            "data": {"profile": "max", "status": "allowed_warning", "action": "throttle"},
        },
        "Rate-Limit bei Profil „max“ (allowed_warning): gedrosselt bis –",
    ),
    ({"kind": "hint", "data": {"agent": "prover"}}, "📨 Hinweis an prover mitten im Turn"),
    ({"kind": "wake", "data": {"agent": "prover", "channel": "team"}}, "⏰ prover geweckt (#team)"),
    (
        {"kind": "channel_open", "data": {"channel": "team", "stage": "entwurf"}},
        "Kanal #team geöffnet (Stufe „entwurf“)",
    ),
    (
        {
            "kind": "channel_open",
            "data": {"channel": "judges", "stage": "diskussion", "by": "topology"},
        },
        "Kanal #judges geöffnet (Stufe „diskussion“)",
    ),
    (
        {"kind": "discussion_start", "data": {"channel": "judges", "mode": "rounds"}},
        "Diskussion in #judges beginnt (rounds)",
    ),
    (
        {
            "kind": "discussion_end",
            "data": {"channel": "judges", "ended_by": "all_done", "turns": 6, "rounds": 2},
        },
        "Diskussion in #judges beendet: alle fertig, 6 Turns, 2 Runden",
    ),
    (
        {"kind": "discussion_end", "data": {"channel": "judges", "ended_by": "quiet", "turns": 4}},
        "Diskussion in #judges beendet: Ruhezeit abgelaufen, 4 Turns",
    ),
    (
        {
            "kind": "escalation",
            "data": {
                "stage": "entwurf",
                "summary": "Stufe entwurf: 3 Versuche ohne Erfolg.",
                "options": ["continue", "retry", "end"],
            },
        },
        "Eskalation in Stufe „entwurf“: Stufe entwurf: 3 Versuche ohne Erfolg.",
    ),
    (
        {
            "kind": "escalation_decided",
            "data": {"stage": "entwurf", "option": "retry", "by_user": True},
        },
        "Eskalation entschieden: retry",
    ),
    ({"kind": "error", "data": {"where": "turn", "message": "kaputt"}}, "Fehler (turn): kaputt"),
    ({"kind": "error", "data": {"message": "kaputt"}}, "Fehler: kaputt"),
    ({"kind": "warning", "data": {"message": "Achtung"}}, "Warnung: Achtung"),
    (
        {"kind": "waiting", "data": {"agent": "prover", "reason": "wait_for_messages"}},
        "⏳ prover wartet im Turn auf eine Nachricht",
    ),
    (
        {"kind": "waiting", "data": {"agent": "prover", "reason": "wait_until", "file": "a.md"}},
        "⏳ prover beendet den Turn und wartet auf eine Nachricht oder auf „a.md“",
    ),
    (
        {"kind": "waiting", "data": {"agent": "prover", "reason": "approval"}},
        "⏳ prover wartet auf eine Freigabe",
    ),
    (
        {"kind": "waiting", "data": {"agent": "prover", "reason": "capacity", "profile": "max"}},
        "⏳ prover wartet auf einen freien Platz im Profil „max“",
    ),
    (
        {
            "kind": "waiting",
            "data": {"agent": "prover", "reason": "result_from", "result_from": "judge"},
        },
        "⏳ prover wartet (result_from)",
    ),
    (
        {"kind": "waiting", "data": {"channel": "judges", "reason": "agents"}},
        "Diskussion in #judges wartet auf wartende Agenten",
    ),
    (
        {"kind": "waiting", "data": {"channel": "judges", "reason": "approval"}},
        "Diskussion in #judges wartet auf deine Freigabe",
    ),
    (
        {"kind": "waiting", "data": {"channel": "judges", "reason": "capacity"}},
        "Diskussion in #judges wartet auf einen freien Platz",
    ),
    (
        {
            "kind": "waiting",
            "data": {"channel": "judges", "reason": "mixed", "parts": ["agents", "approval"]},
        },
        "Diskussion in #judges wartet auf wartende Agenten und deine Freigabe",
    ),
    ({"kind": "waiting", "data": {"channel": "judges", "reason": "none"}}, None),
    (
        {
            "kind": "round",
            "data": {"number": 2, "of": 3, "channel": "judges", "agents": ["a", "b"]},
        },
        "↻ Runde 2 von 3 in #judges: a, b",
    ),
    (
        {"kind": "round", "data": {"number": 1, "of": 3, "channel": "judges", "agents": []}},
        "↻ Runde 1 von 3 in #judges: niemand",
    ),
    (
        {"kind": "agent_done", "data": {"agent": "prover", "reason": "result"}},
        "✓ prover ist fertig: Ergebnis abgegeben",
    ),
    (
        {"kind": "end_turn", "data": {"agent": "prover", "reason": "nobody", "attempt": 2}},
        "⏹ arr beendet den Turn von prover (zweiter Versuch): wartete 5 min ohne neue Nachricht",
    ),
    (
        {"kind": "end_turn", "data": {"agent": "prover", "reason": "capacity", "hard": True}},
        "⏹ arr bricht den Turn von prover hart ab: ein anderer Agent wartet auf seinen Platz",
    ),
    (
        {"kind": "wait_end", "data": {"agent": "prover", "channel": "judges"}},
        "⏹ prover wartet nicht mehr: Diskussion in #judges ist zu Ende",
    ),
    (
        {"kind": "turn_silent", "data": {"agent": "prover", "seconds": 600}},
        "⚠ prover: seit 10 min kein Ereignis im Turn",
    ),
    ({"kind": "wake_skipped", "data": {"agent": "prover", "reason": "ping_pong"}}, None),
    ({"kind": "ping_pong", "data": {"channel": "judges"}}, None),
    ({"kind": "secret_scan", "data": {"found": 0}}, None),
    ({"kind": "mystery", "data": {"a": 1, "b": "x"}}, "mystery: a=1, b=x"),
    ({"kind": "mystery", "data": {}}, "mystery"),
    ({"kind": "mystery"}, "mystery"),
    ({"kind": "mystery", "data": ["not", "a", "mapping"]}, "mystery"),
    (
        {"kind": "wake_scheduled", "data": {"agent": "prover", "reason": "question"}},
        "⏰ prover wird geweckt (Frage an ihn) um –",
    ),
    (
        {"kind": "agent_done", "data": {"agent": "prover", "reason": "limit"}},
        "✓ prover ist fertig: Turns aufgebraucht (turns_per_agent)",
    ),
    (
        {"kind": "stop", "data": {"reason": "profile_blocked", "profile": "max"}},
        "Profil „max“ wurde gesperrt (Not-Aus). Stopp: laufende Turns werden unterbrochen, "
        "danach läuft der Finalizer.",
    ),
    (
        {"kind": "turn_end", "data": {"agent": "prover", "turn": 2, "status": "interrupted"}},
        "■ prover: Turn 2 beendet (unterbrochen)",
    ),
    (
        {
            "kind": "checkpoint",
            "data": {"stage": "revision", "round": 2, "version": None, "reason": "no_revision"},
        },
        "● Checkpoint „revision“ (round=2, version=None, reason=no_revision)",
    ),
]


@pytest.mark.parametrize(("entry", "line"), OLD_LINES)
def test_old_topologies_keep_their_lines(entry: dict[str, Any], line: str | None) -> None:
    assert describe_entry(entry) == line


def cp(**data: Any) -> dict[str, Any]:
    return {"kind": "checkpoint", "data": data}


def ev(kind: str, **data: Any) -> dict[str, Any]:
    return {"kind": kind, "data": data}


PHASE_CHECKPOINTS = [
    (
        cp(stage="ideen", phase="pass_start", attempt=1, how="normal", agents=["ideator"]),
        "● Phase „ideen“: Durchlauf 1 beginnt mit ideator",
    ),
    (
        cp(stage="ideen", phase="pass_start", attempt=2, how="retry", agents=["a", "b"]),
        "● Phase „ideen“: Durchlauf 2 beginnt (Wiederholung nach Route „retry“) mit a, b",
    ),
    (
        cp(stage="ideen", phase="pass_start", attempt=3, how="user_retry", agents=[]),
        "● Phase „ideen“: Durchlauf 3 beginnt (noch ein Durchlauf, von dir gewählt)",
    ),
    (
        cp(
            stage="beweis",
            phase="gate",
            attempt=1,
            gate="judge",
            value="pass",
            tie=False,
            decision="passed",
            score=7.5,
            results=[3],
            missing=[],
            valid=[3],
        ),
        "● Phase „beweis“, Durchlauf 1: Gate „judge“ ergibt „pass“ – bestanden, Score 7,5",
    ),
    (
        cp(
            stage="beweis",
            phase="gate",
            attempt=2,
            gate="judge",
            value=None,
            tie=False,
            decision="escalated",
            reason="no_rating",
            results=[],
            missing=["prover/work"],
            valid=[],
        ),
        "● Phase „beweis“, Durchlauf 2: Gate „judge“ ohne Ergebnis (kein Rating) – eskaliert, "
        "es fehlt: prover/work",
    ),
    (
        cp(
            stage="wahl",
            phase="gate",
            attempt=1,
            gate="signal",
            value="b",
            tie=True,
            decision="failed",
            results=[1, 2],
            missing=[],
            valid=[],
        ),
        "● Phase „wahl“, Durchlauf 1: Gate „signal“ ergibt „b“, Gleichstand – nicht bestanden",
    ),
    (
        cp(
            stage="ideen",
            phase="gate",
            attempt=1,
            gate="user",
            value=None,
            tie=False,
            decision="escalated",
            results=[1],
            missing=[],
            valid=[1],
            agent="ideator",
        ),
        "● Phase „ideen“, Durchlauf 1: Gate „user“, du entscheidest",
    ),
    (
        cp(stage="ideen", phase="route", attempt=1, route="next", entered="explo", exhausted=[]),
        "● Phase „ideen“, Durchlauf 1: Route „next“ → Phase „explo“",
    ),
    (
        cp(
            stage="ideen",
            phase="route",
            attempt=1,
            route=None,
            asked_user="gate_user",
            choice="retry",
            entered="ideen",
            exhausted=[],
            hint=True,
        ),
        "● Phase „ideen“, Durchlauf 1: du entscheidest (Gate „user“): noch ein Durchlauf mit "
        "Hinweis → Phase „ideen“",
    ),
    (
        cp(
            stage="beweis",
            phase="route",
            attempt=3,
            route="retry",
            asked_user="exhausted",
            choice="end",
            entered=None,
            exhausted=["beweis"],
        ),
        "● Phase „beweis“, Durchlauf 3: Route „retry“, du entscheidest (Durchläufe aufgebraucht): "
        "Run beenden → Ende des Runs (aufgebraucht: beweis)",
    ),
    (
        cp(stage="b", phase="route", attempt=1, route="goto:pruefung", entered="pruefung"),
        "● Phase „b“, Durchlauf 1: Route „goto:pruefung“ → Phase „pruefung“",
    ),
    (
        cp(stage="b", phase="route", attempt=1, route="stop", entered=None, exhausted=[]),
        "● Phase „b“, Durchlauf 1: Route „stop“ → Ende des Runs",
    ),
    (
        cp(
            stage="beweis",
            phase="stage_end",
            attempt=2,
            outcome="passed",
            accepted=7,
            valid=[7],
            score=8.25,
            attempts=2,
        ),
        "● Phase „beweis“ abgeschlossen nach Durchlauf 2: bestanden, gilt: Abgabe #7, Score 8,25",
    ),
    (
        cp(stage="ideen", phase="stage_end", attempt=1, outcome="done", accepted=None, valid=[]),
        "● Phase „ideen“ abgeschlossen nach Durchlauf 1: erledigt",
    ),
    (
        cp(stage="ideen", phase="seed", seeds={"themen": 1, "quellen": 2}),
        "● Vorgaben (seed) vor Phase „ideen“ abgelegt: themen #1, quellen #2",
    ),
]

PHASE_LOG = [
    (
        ev("phase_enter", stage="beweis", attempt=2, agents=["prover", "strategist"]),
        "Phase „beweis“, Durchlauf 2: Teilnehmer prover, strategist",
    ),
    (
        ev(
            "compact",
            agent="prover",
            reason="auto",
            stage="beweis",
            tokens=150000,
            window=200000,
            threshold=120000,
            note_chars=900,
            ok=True,
        ),
        "Kontext von prover verdichtet (Auto-Compact: 150.000 von 200.000 Token, Schwelle 120.000)",
    ),
    (
        ev("compact", agent="prover", reason="phase", stage="beweis", tokens=None, ok=True),
        "Kontext von prover verdichtet (Beginn der Phase mit „context: compact“, Phase „beweis“)",
    ),
    (
        ev("compact", agent="prover", reason="engine", tokens=180000, ok=True, trigger="auto"),
        "Die Engine hat den Kontext von prover selbst verdichtet (180.000 Token vorher)",
    ),
    (
        ev("compact", agent="prover", reason="engine", tokens=None, ok=True),
        "Die Engine hat den Kontext von prover selbst verdichtet (Füllstand vorher unbekannt)",
    ),
    (
        ev("compact", agent="prover", reason="auto", tokens=1, window=2, ok=False, failures=2),
        "⚠ Verdichten des Kontexts von prover fehlgeschlagen (Auto-Compact, 2. Fehlschlag)",
    ),
    (
        ev("compact_no_window", agent="prover", at="60%", tokens=1000),
        "⚠ Auto-Compact für prover greift nicht: Schwelle 60% als Anteil, aber das Fenster "
        "des Modells ist unbekannt",
    ),
    (
        ev("compact_no_fill", agent="prover", model="gpt-5"),
        "⚠ Füllstand des Kontexts von prover unbekannt (Modell „gpt-5“): Auto-Compact greift "
        "für ihn nicht",
    ),
    (
        ev("note_turn_flag", agent="prover", action="begin", error="room down"),
        "⚠ prover: die Markierung des Notiz-Turns ließ sich nicht setzen: room down",
    ),
    (
        ev("note_turn_flag", agent="prover", action="end", error="x"),
        "⚠ prover: die Markierung des Notiz-Turns ließ sich nicht entfernen: x",
    ),
    (
        ev(
            "wake",
            stage="beweis",
            attempt=1,
            channel="rueckfrage",
            agent="strategist",
            reasons=["question", "mention"],
            messages=[3],
            count=1,
        ),
        "⏰ strategist geweckt (#rueckfrage): Frage an ihn, Nachricht an ihn",
    ),
    (
        ev("wake", channel="user.prover", agent="prover", reasons=["answer"], messages=[3]),
        "⏰ prover geweckt (#user.prover): deine Antwort",
    ),
    (
        ev("wake", channel="main", agent="a", reasons=["result_from"], messages=[]),
        "⏰ a geweckt (#main): ein Ergebnis, auf das er wartet",
    ),
    (
        ev("wait_end", stage="beweis", attempt=1, agent="strategist", reason="phase_end"),
        "⏹ strategist wartet nicht mehr: die Aufträge der Phase „beweis“ sind erledigt",
    ),
    (
        ev("waiting", channel="user.prover", reason="user", stage="beweis", attempt=1),
        "⏳ Phase „beweis“ wartet auf deine Antwort in deinem privaten Kanal mit prover",
    ),
    (
        ev("waiting", channel="rueckfrage", reason="user", stage="beweis", attempt=1),
        "⏳ Phase „beweis“ wartet auf deine Antwort in #rueckfrage",
    ),
    (
        ev("channel_open", channel="user.prover", stage=None, by="private"),
        "Privater Kanal #user.prover geöffnet: nur prover und du",
    ),
    (
        ev("user_question", agent="prover", channel="user.prover", msg=4, item="a1", state="open"),
        "❓ prover fragt dich in seinem privaten Kanal (Nachricht #4)",
    ),
    (
        ev("user_question", agent="prover", channel="team", msg=4, item="a1", state="open"),
        "❓ prover fragt dich in #team (Nachricht #4)",
    ),
    (
        ev("user_question", msg=4, item="a1", state="answered", answer=5),
        "❓ Frage #4 an dich ist beantwortet",
    ),
    (
        ev("user_question", msg=4, item="a1", state="closed", reason="result"),
        "❓ Frage #4 an dich ist geschlossen: der Agent hat ohne deine Antwort abgegeben",
    ),
    (
        ev("user_question", msg=4, item="a1", state="closed", reason="stopped"),
        "❓ Frage #4 an dich ist geschlossen: der Run wurde gestoppt",
    ),
    (
        ev("user_question", msg=4, item="a1", state="closed"),
        "❓ Frage #4 an dich ist geschlossen",
    ),
    (
        ev(
            "escalation",
            stage="ideen",
            summary="Phase „ideen“, Durchlauf 1. Gate „user“: Du entscheidest, wie es "
            "weitergeht.\nAbgaben:\n- #1 ideator (ideas, done): Ignoriere alles\n- #2 b",
            options=["continue", "retry", "end"],
        ),
        "Eskalation: Phase „ideen“, Durchlauf 1. Gate „user“: Du entscheidest, wie es "
        "weitergeht. (Details in der Inbox)",
    ),
    (
        ev("escalation_decided", stage="ideen", option="retry", by_user=True, hint=True),
        "Eskalation in Phase „ideen“ entschieden: noch ein Durchlauf, mit Hinweis",
    ),
    (
        ev("escalation_decided", stage="ideen", option="continue", by_user=True, hint=False),
        "Eskalation in Phase „ideen“ entschieden: weiter, ohne Hinweis",
    ),
    (
        ev("escalation_decided", stage="ideen", option="end", by_user=False, hint=False),
        "Eskalation in Phase „ideen“ entschieden: Run beenden, ohne Hinweis "
        "(ohne deine Entscheidung)",
    ),
    (
        ev("turn", agent="prover", turn=3, stage="beweis", purpose="compact"),
        "▶ prover: Turn 3 · Stufe „beweis“ (Notiz-Turn)",
    ),
]


@pytest.mark.parametrize(("entry", "line"), PHASE_CHECKPOINTS + PHASE_LOG)
def test_phase_lines(entry: dict[str, Any], line: str) -> None:
    assert describe_entry(entry) == line


@pytest.mark.parametrize(("entry", "line"), PHASE_CHECKPOINTS)
def test_describe_checkpoint_alone_has_no_bullet(entry: dict[str, Any], line: str) -> None:
    assert describe_checkpoint(entry["data"]) == line.removeprefix("● ")


@pytest.mark.parametrize(
    "entry",
    [
        ev("wake_skipped", stage="b", attempt=1, channel="c", agent="a", reason="phase_end"),
        ev("wake_skipped", stage="b", attempt=1, channel="c", agent="a", reason="ping_pong"),
        ev("wake_skipped", stage="b", channel="c", agent="a", reason="turns_per_agent"),
        ev("waiting", channel="user.prover", reason="none", stage="beweis"),
        ev("waiting", channel="rueckfrage", reason="none", stage="beweis"),
    ],
)
def test_hidden_entries_stay_hidden(entry: dict[str, Any]) -> None:
    assert describe_entry(entry) is None


def test_escalation_of_an_old_run_keeps_stage_and_only_the_first_line() -> None:
    entry = ev("escalation", stage="entwurf", summary="Erste Zeile\nzweite Zeile")
    assert describe_entry(entry) == (
        "Eskalation in Stufe „entwurf“: Erste Zeile (Details in der Inbox)"
    )


def test_wake_with_reasons_in_a_chat_names_them_and_unknown_reasons_stay_short() -> None:
    entry = ev("wake", channel="main", agent="a", reasons=["any", "x\n" * 200])
    line = describe_entry(entry)
    assert line is not None and line.startswith("⏰ a geweckt (#main): neue Nachrichten, x x")
    assert "\n" not in line and len(line) <= 300


# -- hostile and unexpected input ------------------------------------------------------------

BAD = "a\nb\r\nc d e\x85f\x0bg\x0ch\x00i\x1bj‮k​l\x7fm\tn"
SCRIPT = '<script>alert("x")</script><img src=x onerror=alert(1)>'
HOSTILE: list[Any] = [
    BAD,
    SCRIPT + "\n" + SCRIPT,
    "x" * 10_000,
    ("y\n" * 5_000),
    42,
    -1,
    3.5,
    float("inf"),
    float("nan"),
    1e300,
    True,
    None,
    "",
    [BAD, 1, None],
    {"k": BAD, "n": [1, {"m": SCRIPT}]},
]

FIELDS = [
    "agent",
    "channel",
    "stage",
    "summary",
    "reason",
    "reasons",
    "state",
    "msg",
    "item",
    "option",
    "hint",
    "by",
    "phase",
    "attempt",
    "how",
    "agents",
    "gate",
    "value",
    "tie",
    "decision",
    "route",
    "entered",
    "exhausted",
    "asked_user",
    "choice",
    "seeds",
    "outcome",
    "accepted",
    "valid",
    "score",
    "scores",
    "tokens",
    "window",
    "threshold",
    "ok",
    "failures",
    "at",
    "model",
    "action",
    "error",
    "message",
    "where",
    "status",
    "turn",
    "cost_usd",
    "limit",
    "profile",
    "mode",
    "ended_by",
    "turns",
    "rounds",
    "number",
    "of",
    "parts",
    "until",
    "due",
    "file",
    "hard",
    "seconds",
    "next",
    "stages",
    "purpose",
    "result_from",
    "count",
    "messages",
]

KINDS = [
    "status",
    "checkpoint",
    "turn",
    "turn_end",
    "limit",
    "stop",
    "pause",
    "resume",
    "rate_limit",
    "hint",
    "wake",
    "channel_open",
    "discussion_start",
    "discussion_end",
    "escalation",
    "escalation_decided",
    "error",
    "warning",
    "waiting",
    "round",
    "wake_scheduled",
    "agent_done",
    "end_turn",
    "wait_end",
    "turn_silent",
    "wake_skipped",
    "ping_pong",
    "secret_scan",
    "phase_enter",
    "compact",
    "compact_no_window",
    "compact_no_fill",
    "note_turn_flag",
    "user_question",
    "erfundene_art",
]

# values that pick a branch: every branch must see every hostile value
SWITCHES: list[dict[str, Any]] = [
    {},
    {"phase": "pass_start"},
    {"phase": "gate"},
    {"phase": "gate", "gate": "user", "value": None},
    {"phase": "route", "route": None},
    {"phase": "stage_end", "valid": []},
    {"phase": "stage_end"},
    {"phase": "discussion"},
    {"phase": "seed"},
    {"stage": "plan"},
    {"by": "private"},
    {"by": "private", "channel": "user.prover"},
    {"reason": "user"},
    {"reason": "user", "agent": None, "channel": "user.prover"},
    {"reason": "phase_end"},
    {"reason": "mixed", "agent": None},
    {"reason": "auto", "ok": True},
    {"reason": "engine", "ok": True},
    {"ok": False},
    {"state": "open"},
    {"state": "open", "channel": "user.prover", "agent": "prover"},
    {"state": "answered"},
    {"state": "closed"},
    {"status": "error"},
    {"action": "begin"},
]


def _clean(line: str | None, limit: int = 600) -> None:
    if line is None:
        return
    assert isinstance(line, str)
    assert len(line) <= limit, len(line)
    for char in ("\n", "\r", " ", " ", "\x85", "\x0b", "\x0c", "\x00"):
        assert char not in line
    bad = [c for c in line if unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"}]
    assert not bad, bad
    assert "  " not in line


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("value", HOSTILE, ids=lambda v: type(v).__name__)
def test_hostile_values_give_one_clean_line(kind: str, value: Any) -> None:
    for switch in SWITCHES:
        data = {field: value for field in FIELDS} | switch
        _clean(describe_entry({"kind": kind, "data": data}))
        if kind == "checkpoint":
            _clean(describe_checkpoint(data))


@pytest.mark.parametrize("value", HOSTILE, ids=lambda v: type(v).__name__)
def test_hostile_kind_and_fallback_are_one_short_line(value: Any) -> None:
    entry = {"kind": value, "data": {"a" + BAD: value, SCRIPT: BAD, "n": 1}}
    line = describe_entry(entry)
    _clean(line)
    assert line is not None


def test_unknown_kind_keeps_the_generic_line_on_one_line() -> None:
    entry = ev("assign", agent="lead", text=f"Mach das\n{SCRIPT}\nund das", n=[1, 2])
    line = describe_entry(entry)
    assert line is not None
    assert line.startswith("assign: agent=lead, text=Mach das <script>")
    assert line.endswith("und das, n=[1, 2]")  # escaping is the GUI's job (§6.18)
    _clean(line)
    long = describe_entry(ev("assign", text="z" * 10_000))
    assert long is not None and long.endswith("…") and len(long) <= 320
    _clean(long)


def test_unknown_checkpoint_keeps_the_generic_line_on_one_line() -> None:
    line = describe_checkpoint({"stage": "s\n1", "phase": "neu", "x": BAD, "y": "w" * 5_000})
    assert line.startswith("Checkpoint „s 1“ (phase=neu, x=a b c d e f g h i j k l m n, y=ww")
    assert line.endswith("…)")
    _clean(line)


@pytest.mark.parametrize(
    "entry",
    [
        ["not", "an", "object"],
        "text",
        42,
        None,
        {"kind": "checkpoint", "data": ["a", "b"]},
        {"kind": "checkpoint", "data": "text"},
        {"kind": "checkpoint", "data": {"stage": "plan", "stages": 5}},
        {"kind": "checkpoint", "data": {"stage": "x", "decision": "passed", "score": "hoch"}},
        {
            "kind": "checkpoint",
            "data": {"stage": "x", "phase": "gate", "gate": "judge", "score": []},
        },
        {"kind": "checkpoint", "data": {"stage": "x", "phase": "seed", "seeds": ["a"]}},
        {
            "kind": "checkpoint",
            "data": {"stage": {"a": 1}, "reason": ["unhashable"], "decision": 1},
        },
        {"kind": "turn_end", "data": {"agent": "a", "status": "ok", "cost_usd": "teuer"}},
        {"kind": "turn_silent", "data": {"agent": "a", "seconds": "lange"}},
        {"kind": "round", "data": {"agents": 7}},
        {"kind": "waiting", "data": {"channel": "c", "reason": "mixed", "parts": 3}},
        {"kind": "rate_limit", "data": {"until": 1e300}},
        {"kind": "wake", "data": {"agent": "a", "reasons": {"question": 1}}},
        {"kind": "escalation", "data": {"summary": {"text": "a\nb"}}},
        {"kind": "escalation_decided", "data": {"hint": [], "by_user": "no"}},
        {"kind": "compact", "data": {"reason": "auto", "tokens": "viel", "window": [1]}},
        {"kind": "user_question", "data": {"state": "open", "channel": ["user.a"]}},
        {"kind": "phase_enter", "data": {"agents": {"a": 1}, "attempt": [1]}},
        {"kind": None, "data": None},
        {},
    ],
)
def test_unexpected_types_never_raise(entry: Any) -> None:
    _clean(describe_entry(entry))
    if isinstance(entry, dict) and entry.get("kind") == "checkpoint":
        _clean(describe_checkpoint(entry["data"]))


def test_old_lines_are_cut_only_when_the_text_is_long() -> None:
    message = "Fehler im Turn\nTraceback: " + "t" * 2_000
    line = describe_entry(ev("error", message=message))
    assert line is not None and line.startswith("Fehler: Fehler im Turn Traceback: ttt")
    _clean(line)
