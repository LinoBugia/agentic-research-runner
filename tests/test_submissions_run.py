"""`submit` in a run: tools per topology, MCP, FakeBackend, finalizer, start (W4b-01 A10, A11).

Runs with `topology: phases` get `submit` and not `submit_result`/`submit_rating`, the five old
topologies the reverse; a `phases` run is refused before its start until W4b-02b. The
FakeBackend plays `submit` without a model, and the finalizer writes `submissions.json` and an
overview of the summaries.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from run_kit import agent, channel, make_manager, make_workfolder, room, turns, write_profiles

from arr.errors import ArrError
from arr.models import (
    AgentConfig,
    ApprovalSettings,
    GroupChatConfig,
    Limits,
    ResolvedRun,
    RunStatus,
    TopologyKind,
)
from arr.resolve import dump_yaml_model
from arr.room.server import build_server
from arr.room.store import RoomStore
from arr.runs.context import RunContext
from arr.runs.finalize import finalize_run
from arr.runs.launch import plan_launch
from arr.runs.topologies import AVAILABLE, check_topology, planned_stages

OLD_KINDS = ["solo", "roundtable", "exploration", "waterfall", "review"]


def write_run_yaml(run_dir: Path, kind: str) -> None:
    run = ResolvedRun(
        project="demo",
        topology={"kind": kind},
        limits=Limits(),
        approvals=ApprovalSettings(),
        agents=[
            AgentConfig(id=a, base_id=a, provider="claude", model="m", auth="p")
            for a in ("prover", "judge")
        ],
    )
    (run_dir / "run.yaml").write_text(dump_yaml_model(run), encoding="utf-8")


def make_store(tmp_path: Path, kind: str | None) -> RoomStore:
    run_dir = tmp_path / kind if kind else tmp_path / "none"
    for a in ("prover", "judge"):
        (run_dir / "agents" / a / "work").mkdir(parents=True)
    if kind:
        write_run_yaml(run_dir, kind)
    store = RoomStore.create(run_dir)
    store.setup_channels([GroupChatConfig(name="main", members=["prover", "judge"])])
    return store


async def tool_list(store: RoomStore, agent_id: str = "prover") -> dict[str, Any]:
    async with Client(build_server(store.for_agent(agent_id))) as client:
        return {t.name: t for t in (await client.list_tools()).tools}


# --------------------------------------------------------------------------------------------
# A10: which tools a run offers, and no start for `phases`
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [*OLD_KINDS, None])
async def test_old_topologies_keep_the_old_tools(tmp_path: Path, kind: str | None) -> None:
    tools = await tool_list(make_store(tmp_path, kind))
    assert {"submit_result", "submit_rating"} <= set(tools)
    assert "submit" not in tools


async def test_phases_offers_submit_instead(tmp_path: Path) -> None:
    tools = await tool_list(make_store(tmp_path, "phases"))
    assert "submit" in tools
    assert not {"submit_result", "submit_rating"} & set(tools)
    schema = tools["submit"].input_schema
    assert schema["required"] == ["status", "summary"]
    assert {"body", "files", "note_next", "fields"} <= set(schema["properties"])
    fields = schema["properties"]["fields"]["anyOf"][0]
    assert fields["type"] == "object" and fields.get("additionalProperties", True) is True
    old = await tool_list(make_store(tmp_path, "solo"))
    assert set(tools) - {"submit"} == set(old) - {"submit_result", "submit_rating"}


def test_phases_is_a_topology_kind_but_not_available() -> None:
    assert TopologyKind("phases") is TopologyKind.PHASES
    assert TopologyKind.PHASES not in AVAILABLE


PHASES_RUN = """\
version: 1
project: demo
task: Teste arr.
topology: {kind: phases}
defaults: {provider: claude, model: fake-model, auth: fake-auth}
agents: [{id: a}]
"""


def test_a_phases_run_is_refused_before_the_start(
    tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workfolder = make_workfolder(tmp_path)
    (workfolder / "arr.yaml").write_text("version: 1\ntoolchain: {run_venv: off}\n", "utf-8")
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    run_file = workfolder / "run.yaml"
    run_file.write_text(PHASES_RUN, encoding="utf-8")
    with pytest.raises(ArrError) as info:
        plan_launch(run_file, cwd=workfolder)
    assert str(info.value) == (
        "Die Topologie „phases“ ist noch nicht verfügbar. Verfügbar sind: solo, roundtable, "
        "exploration, waterfall, review."
    )
    assert list((workfolder / "runs").iterdir()) == [], "nothing was prepared"
    # counter-check: the same run with `solo` passes the planning
    run_file.write_text(PHASES_RUN.replace("phases", "solo"), encoding="utf-8")
    assert plan_launch(run_file, cwd=workfolder).resolved.topology.kind is TopologyKind.SOLO


# --------------------------------------------------------------------------------------------
# A2/A9 over MCP in this process: nested fields, German errors
# --------------------------------------------------------------------------------------------


async def test_submit_over_mcp_with_nested_fields(tmp_path: Path) -> None:
    store = make_store(tmp_path, "phases")
    store.add_assignment(
        "judge",
        1,
        "Prüfe.",
        stage="pruefung",
        context={"submission": {"format": "review", "known_objections": ["E1"]}},
    )
    fields = {
        "verdict": "major",
        "objections": [
            {"text": "Basisfall fehlt."},
            {"ref": "E1", "status": "behoben", "text": "Erledigt."},
        ],
    }
    async with Client(build_server(store.for_agent("judge"))) as client:
        bad = await client.call_tool(
            "submit", {"status": "done", "summary": "x", "fields": {"verdict": "gut"}}
        )
        assert bad.is_error
        text = bad.content[0].text
        assert "„fields.verdict“: Erwartet: einer von accept, minor, major, reject." in text
        assert "„fields.objections“: fehlt." in text and "ungelesen:" in text
        wrong_type = await client.call_tool(
            "submit", {"status": "done", "summary": "x", "fields": "verdict=major"}
        )
        assert wrong_type.is_error
        assert "„fields“ muss ein Objekt sein" in wrong_type.content[0].text
        good = await client.call_tool(
            "submit", {"status": "done", "summary": "Zwei Einwände.", "fields": fields}
        )
        assert not good.is_error, good.content[0].text
        answer = good.content[0].text
        assert answer.startswith("Abgabe #1 angenommen (review, done, Phase pruefung")
        assert "Ordner: output/submissions/001-pruefung-judge" in answer
    [sub] = store.submissions()
    assert sub.fields == fields


# --------------------------------------------------------------------------------------------
# FakeBackend and finalizer (A11)
# --------------------------------------------------------------------------------------------


def submit(**args: Any) -> dict[str, Any]:
    return room("submit", **args)


def phases_manager(workfolder: Path) -> Any:
    holder: list[Any] = []

    async def topology(ctx: RunContext) -> None:
        work = holder[0].prepared.agents["prover"].work_dir
        (work / "beweis.md").write_text("Beweis.\n", encoding="utf-8")
        artifact = {"submission": {"format": "artifact", "name": "beweis"}}
        await ctx.turn("prover", "Schreib den Beweis.", stage="beweis", context=artifact)
        await ctx.turn("prover", "Überarbeite.", stage="beweis", context={**artifact, "attempt": 2})
        review = {"submission": {"format": "review"}}
        await ctx.turn("judge", "Prüfe.", stage="pruefung", context=review)

    scripts = {
        "prover": turns(
            [
                submit(status="done", summary="Erste Fassung.", files=["beweis.md"]),
            ],
            [
                submit(
                    status="done",
                    summary="Zweite Fassung | mit *Markdown*.",
                    body="Neu.",
                    files=["beweis.md"],
                ),
                submit(status="falsch", summary="x"),  # a mistake the agent sees as tool error
            ],
        ),
        "judge": turns(
            [
                submit(
                    status="done",
                    summary="Fast fertig.",
                    fields={"verdict": "minor", "objections": [{"text": "Lemma 2 knapp."}]},
                )
            ]
        ),
    }
    manager = make_manager(
        workfolder,
        topology,
        scripts,
        agents=[agent("prover"), agent("judge")],
        groupchats=[channel("main", ["prover", "judge"])],
        run_extra={"topology": {"kind": "phases"}},
        finalizer=finalize_run,
    )
    holder.append(manager)
    return manager


async def test_fake_backend_plays_submit_and_finalizer_lists_submissions(tmp_path: Path) -> None:
    workfolder = make_workfolder(tmp_path)
    manager = phases_manager(workfolder)
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    run_dir = manager.run_dir
    data = json.loads((run_dir / "output" / "submissions.json").read_text("utf-8"))
    assert data["version"] == 1 and data["run"] == manager.prepared.run_id
    entries = data["submissions"]
    assert [(e["id"], e["agent"], e["format"], e["name"], e["version"]) for e in entries] == [
        (1, "prover", "artifact", "beweis", 1),
        (2, "prover", "artifact", "beweis", 2),
        (3, "judge", "review", None, None),
    ]
    assert entries[0]["files"] == ["output/beweis/v1/files/beweis.md"]
    assert entries[1]["body_file"] == "output/beweis/v2/body.md" and entries[1]["attempt"] == 2
    assert entries[2]["fields"]["objections"] == [{"text": "Lemma 2 knapp."}]
    assert all((run_dir / e["path"] / "submission.json").is_file() for e in entries)
    summary = (run_dir / "output" / "summary.md").read_text("utf-8")
    section = summary[summary.index("## Abgaben") :]
    assert "- **#1 beweis v1** · `prover` · artifact · done · Phase beweis, Durchlauf 1" in section
    assert "  Erste Fassung." in section
    assert "  Zweite Fassung \\| mit \\*Markdown\\*." in section, "agent text is escaped"
    assert "[Ordner](submissions/003-pruefung-judge/)" in section
    # the mistake of the second turn reached the agent as a German tool error
    transcript = (run_dir / "agents" / "prover" / "transcript.jsonl").read_text("utf-8")
    assert "Erwartet: done oder not_solved" in transcript


async def test_finalizer_output_of_old_topologies_is_unchanged(tmp_path: Path) -> None:
    """A11 counter-check: a run of an old topology gets neither `submissions.json` nor a
    section `Abgaben` (the rest of its output is covered by `test_finalize.py`)."""
    workfolder = make_workfolder(tmp_path)

    async def topology(ctx: RunContext) -> None:
        await ctx.turn("a", "Los.", stage="s")

    manager = make_manager(
        workfolder,
        topology,
        {"a": turns([room("submit_result", kind="proof", title="Beweis", body="…")])},
        finalizer=finalize_run,
    )
    assert await asyncio.wait_for(manager.run(), 30) is RunStatus.DONE
    out = manager.run_dir / "output"
    assert sorted(p.name for p in out.iterdir()) == [
        "ratings.json",
        "results",
        "results.json",
        "summary.md",
    ]
    summary = (out / "summary.md").read_text("utf-8")
    assert "## Ergebnisse" in summary and "## Abgaben" not in summary


def test_planned_stages_of_phases_is_empty_until_the_runner(tmp_path: Path) -> None:
    run = ResolvedRun(
        project="demo",
        topology={"kind": "phases"},
        limits=Limits(),
        approvals=ApprovalSettings(),
        agents=[AgentConfig(id="a", base_id="a", provider="claude", model="m", auth="p")],
    )
    assert planned_stages(run) == []
    with pytest.raises(ArrError, match="noch nicht verfügbar"):
        check_topology(run)
