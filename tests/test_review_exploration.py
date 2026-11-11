"""Topologies `review` and `exploration` and their building blocks (W4-03 A1-A7, A9).

FakeBackend agents with the real room, run through the real RunManager; no model, no network.
The hang cases (A8) are in `test_topology_hangs.py`, the sandbox probe of F4 (A3) in
`test_review_seal_probe.py`.
"""

import os
from pathlib import Path
from typing import Any

import pytest
from run_kit import agent, channel, events, log_entries, make_settings, post, room, say, wait
from test_topologies import (
    TASK,
    Recorder,
    checkpoints,
    resolved_run,
    run,
    setup,
    stage,
    staged,
    submit,
)

from arr.backends.fake import FakeBackend
from arr.errors import ArrError, ConfigError
from arr.models import RunStatus
from arr.permissions.policy import RUN_HIDDEN_PATHS, PermissionRules, RunPaths, profile_for
from arr.room import store as room_store
from arr.runs.context import RunContext
from arr.runs.finalize import finalize_run
from arr.runs.orchestrator import Orchestrator
from arr.runs.pacing import _result_text
from arr.runs.topologies import check_topology, exploration, review, topology_for
from arr.runs.topologies.review import aggregate, parse_objections, parse_verdict
from arr.service.real_api.runs import estimate_run

J1, J2 = "judge-1", "judge-2"


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    from run_kit import make_workfolder

    return make_workfolder(tmp_path)


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def a_review(verdict: str | None, *objections: str, note: str = "", **extra: Any) -> dict:
    """A review in the agreed form; `verdict=None` leaves the line out."""
    lines = [f"Urteil: {verdict}", ""] if verdict else ["Ohne Urteil.", ""]
    lines += ["## Einwände", *[f"- {o}" for o in objections], "", "## Für die nächste Runde"]
    lines.append(note or "nichts")
    return submit(extra.pop("title", "Gutachten"), "review", "\n".join(lines))


def judge(*rounds: list[dict], discussion: list[dict] | None = None) -> dict[str, Any]:
    return staged(
        pruefung=stage(*rounds),
        diskussion=stage(
            discussion or [post("judges", "Gelesen."), room("set_status", status="done")],
            repeat_last=True,
        ),
    )


def author(*versions: str) -> dict[str, Any]:
    first, *rest = versions
    by_stage = {"artefakt": stage([submit("Beweis", "artifact", first)])}
    if rest:
        by_stage["revision"] = stage(*[[submit("Beweis neu", "artifact", v)] for v in rest])
    return {"turns": [], "by_stage": by_stage}


def run_review(
    workfolder: Path,
    scripts: dict[str, Any],
    *,
    params: dict[str, Any] | None = None,
    recorder: Recorder | None = None,
    judges: list[str] = (J1, J2),  # type: ignore[assignment]
    chat: dict[str, Any] | None = None,
    agents: list[dict[str, Any]] | None = None,
    **extra: Any,
):
    agents = agents or [agent("author"), *[agent(j) for j in judges]]
    chat_fields = {"mode": "rounds", "anonymous": True, "open_from": "diskussion", **(chat or {})}
    return setup(
        workfolder,
        "review",
        scripts,
        params={"max_revisions": 1, **(params or {})},
        recorder=recorder,
        agents=agents,
        groupchats=[channel("judges", list(judges), **chat_fields)],
        **extra,
    )


def room_calls(run_dir: Path, agent_id: str, tool: str) -> list[tuple[str | None, bool, str]]:
    """`(stage, is_error, content)` of every call of a room tool by the agent."""
    stage_of: dict[int, str | None] = {}
    calls: dict[str, str] = {}
    found = []
    for event in events(run_dir, agent_id):
        data = event["data"]
        if event["kind"] == "turn_start":
            stage_of[event["turn"]] = data.get("stage")
        elif event["kind"] == "tool_call":
            calls[data["id"]] = data["name"]
        elif event["kind"] == "tool_result" and calls.get(data["id"]) == f"mcp__arr-room__{tool}":
            found.append((stage_of.get(event["turn"]), bool(data["is_error"]), data["content"]))
    return found


async def summary_of(manager: Any) -> str:
    """`summary.md` of a finished run (the test managers have no finalizer)."""
    await finalize_run(manager.prepared)
    return (manager.run_dir / "output/summary.md").read_text()


def text_of(path: str) -> str:
    return Path(path).read_text()


def verdicts(manager: Any) -> list[str | None]:
    return [c["verdict"] for c in checkpoints(manager, "urteil")]


# --------------------------------------------------------------------------------------------
# Register, F2: phases in the plan, open_from checked
# --------------------------------------------------------------------------------------------


def test_review_and_exploration_are_available() -> None:
    assert topology_for("review") is review.run
    assert topology_for("exploration") is exploration.run


def test_a2_open_from_must_name_a_phase_with_a_suggestion() -> None:
    resolved = resolved_run(
        "review",
        [agent("author"), agent(J1), agent(J2)],
        groupchats=[channel("judges", [J1, J2], open_from="discussion")],
    )
    with pytest.raises(ConfigError) as caught:
        check_topology(resolved)
    text = str(caught.value)
    assert "„discussion“, das ist keine Phase von „review“" in text
    assert "Meintest du „diskussion“?" in text
    good = resolved_run(
        "exploration",
        [agent("explorer-1"), agent("explorer-2")],
        params={"ideas": ["A", "B"]},
        groupchats=[channel("lab", ["explorer-1", "explorer-2"], open_from="bewertun")],
    )
    with pytest.raises(ConfigError, match="Meintest du „bewertung“"):
        check_topology(good)


def test_a5_meta_judge_gives_a_clear_message() -> None:
    resolved = resolved_run(
        "review",
        [agent("author"), agent(J1)],
        params={"verdict": "meta_judge"},
        groupchats=[channel("judges", [J1])],
    )
    with pytest.raises(ConfigError, match="„verdict: meta_judge“ ist noch nicht wählbar"):
        check_topology(resolved)


def test_review_check_names_roles_and_independence() -> None:
    resolved = resolved_run(
        "review",
        [
            agent(J1),
            agent(J2, workspace={"mode": "shared", "name": "x"}),
            agent("judge-3", workspace={"mode": "shared", "name": "x"}),
        ],
        params={"judge_memory": "fresch"},
        groupchats=[
            channel("judges", [J1, J2, "judge-3"]),
            channel("flur", [J1, J2]),
        ],
    )
    with pytest.raises(ConfigError) as caught:
        check_topology(resolved)
    text = str(caught.value)
    assert "braucht einen Autor" in text
    assert "Meintest du „fresh“?" in text
    assert "teilen sich den Kanal „flur“" in text
    assert "teilen den Arbeitsordner „x“" in text


async def test_a2_phases_are_the_plan(workfolder: Path) -> None:
    scripts = {
        "author": author("v1"),
        J1: judge([a_review("accept")]),
        J2: judge([a_review("accept")]),
    }
    manager = run_review(workfolder, scripts)
    assert await run(manager) is RunStatus.DONE
    [plan] = checkpoints(manager, "plan")
    assert plan["stages"] == list(review.PHASES)


# --------------------------------------------------------------------------------------------
# A7: a whole review with one revision; F6, F7
# --------------------------------------------------------------------------------------------


async def test_a7_review_with_one_revision(workfolder: Path) -> None:
    recorder = Recorder()
    scripts = {
        "author": author("Beweis v1\nZeile 2\n", "Beweis v2\nZeile 2\n"),
        J1: judge(
            [a_review("major", "Lücke in Lemma 2", note="Lemma 2 prüfen")],
            [a_review("accept", "[E1] behoben: passt jetzt")],
        ),
        J2: judge(
            [a_review("minor", "Tippfehler in Zeile 2")],
            [a_review("accept", "[E2] behoben", "[E1] behoben")],
        ),
    }
    manager = run_review(workfolder, scripts, recorder=recorder)
    assert await run(manager) is RunStatus.DONE
    run_dir = manager.run_dir
    assert verdicts(manager) == ["major", "accept"], "1 : 1 → the stricter (F6)"
    [revision] = checkpoints(manager, "revision")
    assert revision["version"] == 2
    diff = (run_dir / revision["diff"]).read_text()
    assert "-Beweis v1" in diff and "+Beweis v2" in diff
    assert (run_dir / "output/artifacts/v1/artefakt.md").read_text() == "Beweis v1\nZeile 2\n"
    [end] = checkpoints(manager, "zusammenfassung")
    assert (end["verdict"], end["reason"], end["rounds"]) == ("accept", "accepted", 2)
    assert end["open_objections"] == []
    summary = await summary_of(manager)
    assert "## Übersicht" in summary and "**Urteil:** accept" in summary
    assert "judge-1 (Mitglied A): major" in summary
    # F7: the package of round 2
    [second] = [c for c in recorder.of(J1, "pruefung") if c.context["round"] == 2]
    package = second.context
    assert Path(package["artifact"]["path"]).is_absolute()
    assert package["artifact"]["excerpt"] == "Beweis v2\nZeile 2\n"
    assert text_of(package["diff"]["path"]) == diff
    assert [(o["id"], o["status"], o["by"]) for o in package["objections"]] == [
        ("E1", "offen", "Mitglied A"),
        ("E2", "offen", "Mitglied B"),
    ]
    assert {"by": "Mitglied A", "round": 1, "next_round": "Lemma 2 prüfen"} in package["previous"]
    # the author got verdict, objections and the reviews
    [rework] = recorder.of("author", "revision")
    assert rework.context["verdict"] == "major"
    assert {r["by"] for r in rework.context["reviews"]} == {"Mitglied A", "Mitglied B"}


async def test_a1_a_judge_finished_in_the_review_gets_turns_in_the_discussion(
    workfolder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: the judges are `done` after their reviews; the discussion still gives each a turn.
    Counter-check: without the reset per phase it ends at once with `all_done`."""
    for naive in (False, True):
        if naive:
            monkeypatch.setattr(Orchestrator, "_enter_phase", lambda *a, **k: None)
        recorder = Recorder()
        scripts = {
            "author": author("v1"),
            J1: judge([a_review("accept")]),
            J2: judge([a_review("accept")]),
        }
        manager = run_review(workfolder, scripts, recorder=recorder)
        assert await run(manager) is RunStatus.DONE
        [end] = log_entries(manager.run_dir, "discussion_end")
        talked = [c.agent for c in recorder.calls if c.stage == "diskussion"]
        if naive:
            assert (end["data"]["ended_by"], end["data"]["turns"], talked) == ("all_done", 0, [])
        else:
            assert sorted(talked) == [J1, J2] and end["data"]["turns"] == 2


async def test_a2_judges_channel_closed_in_review_open_in_discussion_closed_again(
    workfolder: Path,
) -> None:
    read = room("read_messages", channel="judges")
    scripts = {
        "author": author("v1", "v2"),
        J1: judge(
            [read, a_review("major", "Fehler")],
            [read, a_review("accept")],
            discussion=[read, post("judges", "ok"), room("set_status", status="done")],
        ),
        J2: judge([a_review("major")], [a_review("accept")]),
    }
    manager = run_review(workfolder, scripts)
    assert await run(manager) is RunStatus.DONE
    calls = room_calls(manager.run_dir, J1, "read_messages")
    assert [(s, err) for s, err, _ in calls] == [
        ("pruefung", True),
        ("diskussion", False),
        ("pruefung", True),
        ("diskussion", False),
    ]
    assert "#judges ist noch geschlossen. Er öffnet ab Phase „diskussion“" in calls[0][2]
    kinds = [
        (e["kind"], e["data"].get("by"))
        for e in log_entries(manager.run_dir)
        if e["kind"] in ("channel_open", "channel_close") and e["data"]["channel"] == "judges"
    ]
    # with `open_from` it starts closed, so the first close changes nothing and is not logged
    assert kinds == [
        ("channel_open", "topology"),
        ("channel_close", None),
        ("channel_open", "topology"),
    ]


# --------------------------------------------------------------------------------------------
# A3: independent reviews through the room (the sandbox in test_review_seal_probe.py)
# --------------------------------------------------------------------------------------------


async def test_a3_no_judge_sees_another_review_before_the_discussion(workfolder: Path) -> None:
    """judge-1 hands in its review as a file at once; judge-2, still working, finds it neither
    in the room nor in `output/`. Files and packages name judges only by alias."""
    seen: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        release = ctx.release_results

        def snapshot(stage_name: str) -> Any:
            run_dir = ctx.run_folder
            seen["output"] = sorted(os.listdir(run_dir / "output" / "results"))
            seen["sealed"] = sorted(os.listdir(run_dir / room_store.SEALED_DIR))
            return release(stage_name)

        ctx.release_results = snapshot  # type: ignore[method-assign]
        await review.run(ctx)

    secret = submit("Urteil: minor GEHEIM-1", "review", "")
    secret["args"] = {"kind": "review", "title": "Urteil: minor GEHEIM-1", "path": "notes.md"}
    scripts = {
        "author": author("v1"),
        J1: judge([secret]),
        J2: judge(
            [
                wait(0.3),
                room("read_messages", channel="judges"),
                room("get_assignment"),
                room("inbox"),
                room("wait_until", minutes=1, until_result_from="judge-1"),
                a_review("accept"),
            ]
        ),
    }
    manager = run_review(workfolder, scripts)
    manager.topology = topology
    assert await run(manager) is RunStatus.DONE
    assert not [n for n in seen["output"] if "review" in n], seen
    assert [n for n in seen["sealed"] if n.endswith("-mitglied-a-review.md")], seen
    assert not any("judge" in n for n in seen["sealed"] + seen["output"])
    contents = [
        c
        for t in ("read_messages", "get_assignment", "inbox", "wait_until")
        for s, _, c in room_calls(manager.run_dir, J2, t)
        if s == "pruefung"
    ]
    assert len(contents) == 4
    assert not any("GEHEIM-1" in c or "notes" in c for c in contents)
    released = [r for r in manager.room.results(agent=J1)]
    assert released[0].path.startswith("output/results/") and "mitglied-a" in released[0].path


def test_a3_the_wake_text_hides_a_sealed_result(tmp_path: Path) -> None:
    from run_kit import make_run_input, make_workfolder

    from arr.resolve import resolve_run
    from arr.runs.prepare import prepare_run

    wf = make_workfolder(tmp_path)
    run_input = make_run_input(
        [agent(J1), agent(J2)], [channel("judges", [J1, J2], anonymous=True)]
    )
    resolved = resolve_run(run_input, None, make_settings())
    prepared = prepare_run(wf, run_input, resolved, make_settings())
    store = room_store.RoomStore.create(prepared.run_dir)
    store.setup_channels(resolved.groupchats)
    store.seal("pruefung", "judges")
    store.add_assignment(J1, 1, "x", stage="pruefung")
    result = store.for_agent(J1).submit_result("review", "Urteil: minor", body="GEHEIM")
    view = store.for_agent(J2)
    assert _result_text(result, "Mitglied A", view) is None
    store.release("pruefung")
    assert "GEHEIM" in str(_result_text(result, "Mitglied A", view))


def test_a3_policy_hides_sealed_results_and_the_room_database(tmp_path: Path) -> None:
    """F4 as a fixed rule: the sealed folder and the database are sensitive (never readable,
    not even with an approval); `output/results` stays readable. The names match the store."""
    store_paths = {room_store.SEALED_DIR, room_store.STAGING_DIR}
    store_paths |= {f"{room_store.ROOM_DIR}/{n}" for n in room_store.ROOM_DB_FILES}
    assert set(RUN_HIDDEN_PATHS) == store_paths
    from run_kit import make_run_input

    from arr.resolve import resolve_run

    resolved = resolve_run(make_run_input([agent(J1)]), None, make_settings())
    run_dir = tmp_path / "wf" / "runs" / "r1"
    profile = profile_for(
        resolved.agents[0], RunPaths(tmp_path / "wf", run_dir), PermissionRules(), home=tmp_path
    )
    sealed = run_dir / room_store.SEALED_DIR / "007-mitglied-b-review.md"
    for path in (sealed, run_dir / "groupchats/room.sqlite", run_dir / "groupchats/staging/x"):
        assert profile.is_sensitive(path) and not profile.can_read(path), path
    assert profile.can_read(run_dir / "output/results/007-mitglied-b-review.md")


# --------------------------------------------------------------------------------------------
# A4: fresh sessions per round (F5)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("memory", ["fresh", "continue"])
async def test_a4_judge_memory(workfolder: Path, memory: str) -> None:
    scripts = {
        "author": author("v1", "v2"),
        J1: judge([a_review("major", "Fehler")], [a_review("accept")]),
        J2: judge([a_review("major")], [a_review("accept")]),
    }
    manager = run_review(workfolder, scripts, params={"judge_memory": memory})
    backends: dict[str, FakeBackend] = {}
    build = manager.backend_factory

    def keep(prepared: Any) -> Any:
        backends[prepared.id] = build(prepared)
        return backends[prepared.id]

    manager.backend_factory = keep
    assert await run(manager) is RunStatus.DONE
    resets = [e["data"]["agent"] for e in log_entries(manager.run_dir, "session_reset")]
    remembers = any("Prüfrunde 1" in p for p in backends[J1].history)
    sessions = {
        e["data"]["session"] for e in events(manager.run_dir, J1) if e["kind"] == "turn_end"
    }
    if memory == "fresh":
        assert sorted(resets) == [J1, J2]
        assert not remembers, "the fresh judge does not know its turn of round 1"
        assert sessions == {"fake-judge-1", "fake-judge-1-2"}
        assert "Prüfrunde 2" in backends[J1].history[0]
    else:
        assert resets == [] and remembers and sessions == {"fake-judge-1"}


# --------------------------------------------------------------------------------------------
# A5: verdicts (F6); parsing what judges write
# --------------------------------------------------------------------------------------------


def test_a5_aggregate() -> None:
    assert aggregate(["minor", "minor", "reject"], "majority") == "minor"
    assert aggregate(["accept", "major"], "majority") == "major"
    assert aggregate(["accept", "minor", "major"], "majority") == "major"
    assert aggregate(["accept", "accept", None], "majority") == "accept"
    assert aggregate(["accept", "accept"], "unanimous") == "accept"
    assert aggregate(["accept", "minor", "accept"], "unanimous") == "minor"
    assert aggregate(["accept", "reject", "minor"], "unanimous") == "reject"
    assert aggregate([None, None], "majority") is None


def test_parse_verdict_and_objections() -> None:
    assert parse_verdict("Gutachten", "**Urteil:** Major\n…") == "major"
    assert parse_verdict("Urteil: minor", "") == "minor"
    assert parse_verdict("minor: Lücke", "") == "minor"
    assert parse_verdict("accept oder reject?", "kein Urteil") is None
    text = (
        "Urteil: major\n\n## Einwände\n- [E1] behoben: ok\n- E2 strittig\n* Neuer Punkt\n"
        "1. Noch einer\n\nFließtext\n\n## Für die nächste Runde\nLemma 3"
    )
    assert parse_objections(text) == [
        ("E1", "behoben", "ok"),
        ("E2", "strittig", ""),
        (None, None, "Neuer Punkt"),
        (None, None, "Noch einer"),
    ]
    assert review.parse_next_round(text) == "Lemma 3"


async def test_a5_disputed_objection_and_unanimous(workfolder: Path) -> None:
    scripts = {
        "author": author("v1", "v2"),
        J1: judge([a_review("major", "Fehler A")], [a_review("accept", "[E1] behoben")]),
        J2: judge([a_review("accept")], [a_review("minor", "[E1] offen: nein")]),
    }
    manager = run_review(workfolder, scripts, params={"verdict": "unanimous"})
    assert await run(manager) is RunStatus.DONE
    assert verdicts(manager) == ["major", "minor"]
    [end] = checkpoints(manager, "zusammenfassung")
    assert end["reason"] == "revisions_used" and end["open_objections"] == ["E1"]
    assert "E1 (strittig, judge-1 (Mitglied A)): Fehler A" in end["overview"]


# --------------------------------------------------------------------------------------------
# A6: context package with large and hostile artifacts (F7)
# --------------------------------------------------------------------------------------------


async def test_a6_large_artifact_only_as_path_with_a_short_excerpt(workfolder: Path) -> None:
    recorder = Recorder()
    big = "Zeile\n" * 3000  # 18 KB
    scripts = {"author": author(big), J1: judge([a_review("accept")])}
    manager = run_review(workfolder, scripts, recorder=recorder, judges=[J1])
    assert await run(manager) is RunStatus.DONE
    [call] = recorder.of(J1, "pruefung")
    ref = call.context["artifact"]
    assert len(ref["excerpt"].encode()) <= review.EXCERPT_BYTES and ref["complete"] is False
    assert text_of(ref["path"]) == big


async def test_a6_artifact_from_the_project_and_a_link_in_its_place(workfolder: Path) -> None:
    project = workfolder / "projects" / "demo"
    (project / "beweis.md").write_text("Satz: 1 + 1 = 2.\n")
    (project / "link.md").symlink_to(project / "beweis.md")
    recorder = Recorder()
    scripts = {J1: judge([a_review("accept")])}
    params = {"artifact": "beweis.md", "max_revisions": 0}
    manager = run_review(
        workfolder, scripts, recorder=recorder, judges=[J1], agents=[agent(J1)], params=params
    )
    assert await run(manager) is RunStatus.DONE
    [first] = checkpoints(manager, "artefakt")
    assert (first["source"], first["path"]) == ("project", "output/artifacts/v1/beweis.md")
    assert recorder.of(J1, "pruefung")[0].context["artifact"]["excerpt"] == "Satz: 1 + 1 = 2.\n"
    linked = run_review(
        workfolder,
        scripts,
        judges=[J1],
        agents=[agent(J1)],
        params={"artifact": "link.md", "max_revisions": 0},
    )
    assert await run(linked) is RunStatus.DONE
    [bad] = checkpoints(linked, "artefakt")
    assert bad["reason"] == "artifact_unreadable" and "Symlink" in bad["error"]
    assert checkpoints(linked, "zusammenfassung")[0]["reason"] == "no_artifact"


async def test_a6_save_output_refuses_links_fifos_hardlinks_and_huge_files(
    workfolder: Path,
) -> None:
    outcome: dict[str, Any] = {}

    async def topology(ctx: RunContext) -> None:
        assert isinstance(ctx, Orchestrator)
        work = ctx.prepared.agents["a"].work_dir
        (work / "ok.md").write_text("gut\n")
        (work / "link.md").symlink_to(work / "ok.md")
        os.mkfifo(work / "pipe")
        os.link(work / "ok.md", work / "hard.md")
        (work / "big.bin").write_bytes(b"")
        os.truncate(work / "big.bin", room_store.MAX_ATTACHMENT_BYTES + 1)
        (work / "dir").mkdir()
        (work / "dir" / "x.md").write_text("x")
        (work / "via").symlink_to(work / "dir")
        for name in ("link.md", "pipe", "hard.md", "big.bin", "via/x.md", "../../../meta.json"):
            try:
                ctx.save_output(f"t/{name.replace('/', '_')}", agent="a", source=name)
                outcome[name] = "kopiert"
            except ArrError as exc:
                outcome[name] = str(exc)
        outcome["ok"] = ctx.save_output("t/ok.md", agent="a", source="dir/x.md")
        (ctx.run_folder / "output" / "t" / "lnk").symlink_to(ctx.run_folder / "meta.json")
        outcome["read_link"] = ctx.read_output("output/t/lnk", 10_000)
        outcome["read_ok"] = ctx.read_output("output/t/ok.md", 10_000)
        outcome["read_up"] = ctx.read_output("../x", 10_000)

    from run_kit import make_manager, turns

    manager = make_manager(workfolder, topology, {"a": turns([say("x")])})
    assert await run(manager) is RunStatus.DONE
    refused = ("link.md", "pipe", "hard.md", "big.bin", "via/x.md", "../../../meta.json")
    assert all(outcome[n] != "kopiert" for n in refused), outcome
    assert "Symlink" in outcome["link.md"] and "zu groß" in outcome["big.bin"]
    assert outcome["ok"] == "output/t/ok.md"
    assert (outcome["read_link"], outcome["read_ok"], outcome["read_up"]) == (None, "x", None)


# --------------------------------------------------------------------------------------------
# A7: exploration
# --------------------------------------------------------------------------------------------

E1, E2, R = "explorer-1", "explorer-2", "ranker"


def explorer(title: str = "Stand") -> dict[str, Any]:
    step = [submit(title, "exploration", f"{title}: Belege und offene Punkte.")]
    return staged(exploration=stage(step, repeat_last=True))


def ranker(*scores: float) -> dict[str, Any]:
    steps: list[dict[str, Any]] = [room("get_assignment", save_as="job")]
    for index, score in enumerate(scores):
        steps.append(
            room(
                "submit_rating",
                result_id=f"${{job.context.result_ids.{index}}}",
                score=score,
                comment="gesehen",
            )
        )
    return staged(bewertung=stage(steps))


def run_exploration(workfolder: Path, scripts: dict, *, params: dict, recorder=None, chats=None):
    agents = [agent(a) for a in scripts]
    return setup(
        workfolder,
        "exploration",
        scripts,
        params=params,
        recorder=recorder,
        agents=agents,
        groupchats=chats or [],
    )


@pytest.mark.parametrize("exchange", ["none", "digest", "channel"])
async def test_a7_exploration_with_exchange_and_ranking(workfolder: Path, exchange: str) -> None:
    recorder = Recorder()
    scripts = {E1: explorer("Stand-1"), E2: explorer("Stand-2"), R: ranker(5, 9, 7)}
    chats = [channel("lab", [E1, E2], mode="event")] if exchange == "channel" else None
    params = {
        "ideas": ["Induktion", "Gegenbeispiel", {"title": "Computer", "text": "Suche"}],
        "exchange": exchange,
    }
    manager = run_exploration(workfolder, scripts, params=params, recorder=recorder, chats=chats)
    assert await run(manager) is RunStatus.DONE
    [plan] = checkpoints(manager, "plan")
    assert plan["stages"] == list(exploration.PHASES)
    # round robin: explorer-1 gets I1 and I3, one after the other
    ideas = [(c.agent, c.context["idea"]["id"]) for c in recorder.calls if c.stage == "exploration"]
    assert sorted(ideas) == [(E1, "I1"), (E1, "I3"), (E2, "I2")]
    [end] = checkpoints(manager, "zusammenfassung")
    assert [(r["rank"], r["idea"], r["score"]) for r in end["ranking"]] == [
        (1, "I2", 9.0),
        (2, "I3", 7.0),
        (3, "I1", 5.0),
    ]
    assert "| 1 | Gegenbeispiel | explorer-2 |" in end["overview"]
    assert "## Übersicht" in await summary_of(manager)
    [third] = [
        c for c in recorder.calls if c.stage == "exploration" and c.context["idea"]["id"] == "I3"
    ]
    sealed = [e["data"]["stage"] for e in log_entries(manager.run_dir, "results_sealed")]
    if exchange == "digest":
        digest = third.context["digest"]
        assert {d["idea"] for d in digest} == {"Gegenbeispiel"}
        assert digest[0]["start"].startswith("Stand-2: Belege")
        assert sealed == ["exploration"]
    elif exchange == "channel":
        assert third.context["channel"] == "lab" and "#lab" in third.prompt
        assert sealed == [] and "digest" not in third.context
    else:
        assert sealed == ["exploration"] and "digest" not in third.context
    assert all(r.path is None or r.path.startswith("output/") for r in manager.room.results())


async def test_a7_ideator_is_asked_once_more_and_no_rankers(workfolder: Path) -> None:
    def idea(title: str) -> dict[str, Any]:
        return submit(title, "idea", f"{title}: so geht es")

    scripts = {
        "ideator": staged(ideen=stage([idea("Erste")], [idea("Zweite"), idea("Dritte")])),
        E1: explorer(),
    }
    manager = run_exploration(
        workfolder, scripts, params={"ideator": "ideator", "n_ideas": 2, "rankers": []}
    )
    assert await run(manager) is RunStatus.DONE
    [ideen] = checkpoints(manager, "ideen")
    assert (ideen["ideas"], ideen["missing"]) == (2, 0)
    [end] = checkpoints(manager, "zusammenfassung")
    assert [(r["title"], r["score"]) for r in end["ranking"]] == [("Erste", None), ("Zweite", None)]
    assert "Ohne Ranker" in end["overview"]


def test_exploration_check() -> None:
    resolved = resolved_run(
        "exploration",
        [agent("explorer-1"), agent("ranker")],
        params={"exchange": "digets", "rankers": "rankr", "criteria": ["x"]},
        rubric={"criteria": ["korrektheit"]},
    )
    with pytest.raises(ConfigError) as caught:
        check_topology(resolved)
    text = str(caught.value)
    assert "Meintest du „digest“?" in text
    assert "„rankr“, darauf passt kein Agent" in text and "Meintest du „ranker“?" in text
    assert "braucht „exploration“ einen „ideator“" in text
    assert "Die Kriterien x stehen nicht in der Rubrik" in text


# --------------------------------------------------------------------------------------------
# A9: the estimate knows both topologies
# --------------------------------------------------------------------------------------------


def test_a9_estimate_review_and_exploration() -> None:
    reviewed = resolved_run(
        "review",
        [agent("author"), agent(J1), agent(J2)],
        params={"max_revisions": 1, "discussion_rounds": 1},
        groupchats=[channel("judges", [J1, J2], mode="rounds")],
    )
    estimate = estimate_run(reviewed)
    # least: author 1 + 2 reviews + 2 discussion turns; most: 2 + 2 × (4 + 2) + 2
    assert (estimate.turns.min, estimate.turns.max) == (5, 16)
    assert not estimate.turns_open
    explored = resolved_run(
        "exploration",
        [agent(E1), agent(E2), agent(R)],
        params={"ideas": ["a", "b"], "turns_per_idea": 2},
    )
    estimate = estimate_run(explored)
    # least: 2 ideas × 2 turns + 1 ranker; most: 2 × 3 + 2
    assert (estimate.turns.min, estimate.turns.max) == (5, 8)
    assert TASK
