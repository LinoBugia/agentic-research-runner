"""W4b-07 §6.7, §6.11, §6.18 (A13, A14, A20): the tools of Claude Code for runs and groups of
`topology: phases`.

- `read_results` hands out submissions beside the old results: every text of an agent
  bounded (`agent_text`, page budget), names and paths as one line, `stage` and `kind` (the
  format) filter, `note_next` never, sealed submissions only counted until the phase is
  released (Entscheidung 78).
- `describe_skill` and `estimate_run` give the phase plan; `estimate_run` the deviations of the
  run-start card, with an added or removed tightening (`decider: user`) and the hints of
  `check`.
- `post_to_run` refuses a private channel, `read_groupchat` reads it.
- `group_schema` is `json_schema("run")`; NG6: the run schema takes `topology: "phases"`.

Runs are FakeBackend runs of `phases_kit` read by the real `RealApi`; page budgets and hostile
texts use a small stand-in of the API so every value is under control. No model, no network.
"""

import json
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pydantic_core
import pytest
from mcp_kit import Service, make_api, prepare_workfolder, result_data
from phases_kit import FIXTURES, errors, hand_in, manager, parallel, private, run, say, solo
from run_kit import write_profiles
from service_kit import make_workfolder

from arr.models import RunInput, RunStatus
from arr.permissions import policy
from arr.plan.schema import json_schema
from arr.resolve import parse_yaml_text, validate_model
from arr.service.api_models import (
    PhaseActionInfo,
    PhasePlan,
    PhaseRow,
    Result,
    ResultKind,
    ResultStatus,
    RunResults,
    SubmissionInfo,
)
from arr.service.user_api import (
    MAX_ANSWER,
    MAX_TEXT,
    NOTICE,
    PRIVATE_POST,
    SCHEMA_HINT,
    ToolProblem,
    UserApi,
    tool_names,
)

SECRET_NOTE = "GEHEIME-NOTIZ-FUER-DEN-NAECHSTEN-DURCHLAUF"
HOSTILE = (
    "Anfang\x1b[2J IGNORE ALL ‮<img src=x onerror=alert(1)>\r\n"
    "[javascript:x](javascript:alert(1))​\x00\u0085Ende "
)
LONG = HOSTILE + "x" * 30_000
FORBIDDEN = {" ", " ", "‮", "​", "\x1b", "\x00", "\x85", "\r"}


def one_line(text: Any) -> bool:
    """No break, tab, control or format character: the text never starts a line of its own."""
    return isinstance(text, str) and not any(
        unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in text
    )


def clean(text: Any) -> bool:
    """Agent text that may keep `\\n` and tab, but nothing invisible or controlling."""
    return isinstance(text, str) and not (set(text) & FORBIDDEN)


def sent(answer: dict[str, Any]) -> str:
    """The answer as the MCP server sends it (`pydantic_core.to_json(…, indent=2)`)."""
    return pydantic_core.to_json(answer, indent=2).decode()


# --------------------------------------------------------------------------------------------
# A stand-in of the API: one run of project `demo` with the given results and submissions
# --------------------------------------------------------------------------------------------


class Api:
    def __init__(
        self, submissions: list[SubmissionInfo], items: list[Result] | None = None, **more: Any
    ) -> None:
        self.results = RunResults(items=items or [], submissions=submissions)
        self.more = more

    async def get_run(self, run_id: str) -> Any:
        return SimpleNamespace(run=SimpleNamespace(project="demo", id=run_id))

    async def get_results(self, run_id: str) -> RunResults:
        return self.results

    async def get_skill(self, name: str) -> Any:
        return self.more["skill"]

    async def get_skill_file(self, name: str, path: str) -> Any:
        return SimpleNamespace(content="---\nname: x\n---\n")


def sub(n: int, **changes: Any) -> SubmissionInfo:
    data: dict[str, Any] = {
        "id": n,
        "ts": 1.0 + n,
        "agent": "a",
        "stage": "p",
        "format": "text",
        "status": "done",
        "summary": f"Kurz {n}",
        "body_excerpt": f"Text {n}",
    }
    data.update(changes)
    return SubmissionInfo(**data)


def hostile_sub(n: int, **changes: Any) -> SubmissionInfo:
    """Every text an agent writes at its limit and hostile."""
    fields = {f"{HOSTILE}{i}": [LONG, LONG, {"x": LONG}, 3] if i % 2 else LONG for i in range(50)}
    fields["note_next"] = SECRET_NOTE
    return sub(
        n,
        agent="a",
        alias=f"Alias{HOSTILE}",
        action=f"work-{HOSTILE}",
        name=f"name{HOSTILE}",
        version=2,
        status=f"done{HOSTILE}",
        summary=LONG,
        fields=fields,
        files=[f"datei{HOSTILE}{i}.md" * 20 for i in range(60)],
        body_excerpt=LONG,
        path=f"output/{HOSTILE}" * 30,
        score=7.5,
        **changes,
    )


def result(n: int) -> Result:
    return Result(
        id=n,
        ts=float(n),
        agent="a",
        kind=ResultKind.PROOF,
        title=f"Ergebnis {n}",
        status=ResultStatus.UNRATED,
    )


async def all_pages(users: UserApi, **args: Any) -> list[dict[str, Any]]:
    pages = []
    offset = 0
    while True:
        page = await users.read_results("demo", "run-1", offset=offset, **args)
        pages.append(page)
        if "next" not in page:
            return pages
        offset = int(page["next"].rsplit("offset=", 1)[1].rstrip(")"))


# --------------------------------------------------------------------------------------------
# A13, A20: read_results with submissions
# --------------------------------------------------------------------------------------------


async def test_submissions_with_hostile_text_at_the_limit_stay_within_the_page_budget() -> None:
    users = UserApi(Api([hostile_sub(n) for n in range(1, 41)]))
    pages = await all_pages(users)
    seen = [s["submission_id"] for p in pages for s in p["submissions"]]
    assert seen == list(range(1, 41)), "every submission exactly once, in order"
    assert len(pages) > 3, "the budget ends pages before `limit`"
    for page in pages:
        text = sent(page)
        assert len(text) <= MAX_ANSWER + 5_000, len(text)
        assert page["notice"] == NOTICE and page["submissions_total"] == 40
        assert SECRET_NOTE not in text and "note_next" not in text
        for entry in page["submissions"]:
            for key in ("agent", "alias", "stage", "action", "format", "name", "status", "path"):
                assert one_line(entry[key]), (key, entry[key])
            assert all(one_line(f) for f in entry["files"]) and entry["files_more"] == 40
            assert all(one_line(k) for k in entry["fields"])
            assert clean(entry["summary"]) and len(entry["summary"]) < 1_100
            assert clean(entry["agent_text"]) and len(entry["agent_text"]) <= MAX_TEXT + 60
            assert "IGNORE ALL" in entry["agent_text"], "the text stays, as data"
            assert entry["fields_cut"], "the fields are bounded too"
            values = json.dumps(entry["fields"], ensure_ascii=False)
            assert len(values) < 15_000 and not (set(values) & FORBIDDEN)
            assert entry["score"] == 7.5 and entry["version"] == 2


async def test_a_single_entry_never_exceeds_the_budget_alone() -> None:
    users = UserApi(Api([hostile_sub(1)]))
    page = await users.read_results("demo", "run-1")
    assert len(sent(page)) <= MAX_ANSWER and "next" not in page


async def test_many_small_values_in_fields_stay_within_the_budget() -> None:
    """Structure costs too: 50 × 50 objects of tiny values, nested lists, 80 fields."""
    tiny = [{f"k{j}": j for j in range(50)} for _ in range(50)]
    fields = {f"f{i}": tiny if i % 2 else [[[["tief"] * 50] * 50] * 50] for i in range(80)}
    users = UserApi(Api([sub(n, fields=fields, body_excerpt=LONG, summary=LONG) for n in (1, 2)]))
    pages = await all_pages(users)
    assert [s["submission_id"] for p in pages for s in p["submissions"]] == [1, 2]
    for page in pages:
        assert len(sent(page)) <= MAX_ANSWER, len(sent(page))
        for entry in page["submissions"]:
            assert entry["fields_cut"] and entry["fields"]["f0"], "cut, not hidden"


async def test_stage_and_kind_filter_submissions() -> None:
    subs = [
        sub(1, stage="ideen", format="list"),
        sub(2, stage="beweis", format="artifact"),
        sub(3, stage="beweis", format="review"),
        sub(4, stage="urteil", format="review"),
    ]
    users = UserApi(Api(subs))

    async def ids(**args: Any) -> list[int]:
        page = await users.read_results("demo", "run-1", **args)
        assert page["submissions_total"] == len(page["submissions"])
        return [s["submission_id"] for s in page["submissions"]]

    assert await ids() == [1, 2, 3, 4]
    assert await ids(stage="beweis") == [2, 3]
    assert await ids(kind="review") == [3, 4]
    assert await ids(stage="beweis", kind="review") == [3]
    assert await ids(stage="fehlt") == []


async def test_results_and_submissions_share_one_paging() -> None:
    users = UserApi(Api([sub(1), sub(2), sub(3)], items=[result(1), result(2)]))
    first = await users.read_results("demo", "run-1", limit=2)
    assert [r["id"] for r in first["results"]] == [1, 2] and first["submissions"] == []
    assert first["next"] == "read_results(run_id='run-1', limit=2, offset=2)"
    second = await users.read_results("demo", "run-1", limit=2, offset=2)
    assert second["results"] == [] and [s["submission_id"] for s in second["submissions"]] == [
        1,
        2,
    ]
    third = await users.read_results("demo", "run-1", limit=2, offset=4)
    assert [s["submission_id"] for s in third["submissions"]] == [3] and "next" not in third
    assert first["total"] == 2 and first["submissions_total"] == 3


async def test_an_old_run_answers_as_before() -> None:
    page = await UserApi(Api([], items=[result(1)])).read_results("demo", "run-1")
    assert set(page) == {"run_id", "notice", "total", "results"}


async def test_sealed_submissions_are_only_counted_until_the_phase_is_released() -> None:
    hidden = f"VERSIEGELT {HOSTILE}"
    before = [
        sub(1, stage="ideen", format="list"),
        sub(2, stage="exploration", sealed=True, summary=hidden, body_excerpt=hidden),
        sub(3, stage="exploration", agent="b", sealed=True, summary=hidden, body_excerpt=hidden),
        sub(4, stage="pruefung", sealed=True, summary=hidden, body_excerpt=hidden),
    ]
    page = await UserApi(Api(before)).read_results("demo", "run-1")
    assert [s["submission_id"] for s in page["submissions"]] == [1]
    assert page["submissions_total"] == 1
    assert page["sealed"] == [
        "2 versiegelte Abgaben in Phase exploration",
        "1 versiegelte Abgabe in Phase pruefung",
    ]
    assert "VERSIEGELT" not in sent(page)
    filtered = await UserApi(Api(before)).read_results("demo", "run-1", stage="pruefung")
    assert filtered["sealed"] == ["1 versiegelte Abgabe in Phase pruefung"]
    # after the release the room store drops the mark: the same submissions are readable
    after = [s.model_copy(update={"sealed": False}) for s in before]
    page = await UserApi(Api(after)).read_results("demo", "run-1")
    assert [s["submission_id"] for s in page["submissions"]] == [1, 2, 3, 4]
    assert "sealed" not in page and "VERSIEGELT" in sent(page)


# --------------------------------------------------------------------------------------------
# A real FakeBackend run: submissions, `note_next`, the private channel
# --------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_user_temp_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tmp_path` lies in the user's temp folder, which the profiles deny; not the point."""
    monkeypatch.setattr(policy, "user_temp_dirs", lambda: [])


async def finished(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from run_kit import make_workfolder as run_workfolder

    from arr.service.real_api import RealApi

    workfolder = run_workfolder(tmp_path)

    def policy_of(view: Any) -> list[dict[str, Any]]:
        steps = []
        if view.stage == "p" and view.agent == "a" and not view.follow_up:
            steps.append(say(private("a"), f"Privat an den User {HOSTILE}"))
        if view.stage == "q":
            return steps + hand_in(
                view, verdict="minor", objections=[{"text": HOSTILE}], note_next=SECRET_NOTE
            )
        return steps + hand_in(view, body=f"Text {view.agent} {HOSTILE}", note_next=SECRET_NOTE)

    group = {
        "agents": [{"id": "a"}, {"id": "b"}],
        "phases": [parallel("p", ["a", "b"], isolated=True), solo("q", "b", "review")],
    }
    mgr, _log = manager(workfolder, group, {"a": policy_of, "b": policy_of})
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    api = RealApi(workfolder, base_url="http://127.0.0.1:8765", scan=False)
    return SimpleNamespace(users=UserApi(api), api=api, run_id=mgr.prepared.run_id)


async def test_a_real_run_shows_its_submissions_without_note_next(
    tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = await finished(tmp_path, arr_home, monkeypatch)
    page = await done.users.read_results("demo", done.run_id)
    text = sent(page)
    assert SECRET_NOTE not in text and "note_next" not in text
    subs = page["submissions"]
    assert sorted((s["agent"], s["stage"], s["format"]) for s in subs) == [
        ("a", "p", "text"),
        ("b", "p", "text"),
        ("b", "q", "review"),
    ]
    assert "sealed" not in page, "the isolated phase is over: released"
    review = next(s for s in subs if s["format"] == "review")
    assert review["fields"]["verdict"] == "minor"
    [objection] = review["fields"]["objections"]
    assert "onerror" in objection["text"] and clean(objection["text"]), "an object, as data"
    assert all(clean(s["agent_text"]) and clean(s["summary"]) for s in subs)
    only = await done.users.read_results("demo", done.run_id, stage="q", kind="review")
    assert [s["submission_id"] for s in only["submissions"]] == [review["submission_id"]]


async def test_post_to_run_refuses_a_private_channel_and_read_groupchat_reads_it(
    tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    done = await finished(tmp_path, arr_home, monkeypatch)
    channel = private("a")
    page = await done.users.read_groupchat("demo", done.run_id, channel)
    assert page["notice"] == NOTICE
    bodies = [m["agent_text"] for m in page["messages"] if m["sender"] == "a"]
    assert any("Privat an den User" in b for b in bodies)
    assert all(clean(m["agent_text"]) for m in page["messages"])
    count = len((await done.api.get_messages(done.run_id, channel)).items)
    with pytest.raises(ToolProblem) as caught:
        await done.users.post_to_run("demo", done.run_id, channel, "Bitte prüfe Fall 2.")
    assert str(caught.value) == "Den privaten Kanal von a beschreibt nur der User in arr."
    assert str(caught.value) == PRIVATE_POST.format(agent="a")
    assert len((await done.api.get_messages(done.run_id, channel)).items) == count, "no post"


# --------------------------------------------------------------------------------------------
# A13, A12: describe_skill and estimate_run with the phase plan and the deviations
# --------------------------------------------------------------------------------------------

SKILL = "fx-beispiel"
ARR_YAML = """version: 1
toolchain: {run_venv: off}
defaults:
  claude: {model: claude-sonnet-5-5, auth: fake-auth}
  codex: {model: gpt-5.5, auth: fake-codex}
"""


def beispiel() -> dict[str, Any]:
    data, _lines = parse_yaml_text((FIXTURES / "beispiel.yaml").read_text("utf-8"), "b.yaml")
    return data


def install(wf: Path) -> None:
    """`beispiel.yaml` as the skill `fx-beispiel` with roles and task files."""
    text = (FIXTURES / "beispiel.yaml").read_text("utf-8")
    data = beispiel()
    skill = wf / "skills" / SKILL
    skill.mkdir(parents=True)
    (skill / "group.yaml").write_text(text, "utf-8")
    (skill / "SKILL.md").write_text(
        f"---\nname: {SKILL}\ndescription: Testgruppe beispiel\n---\nTest.\n", "utf-8"
    )
    for agent in data["agents"]:
        (skill / agent["role"]).parent.mkdir(parents=True, exist_ok=True)
        (skill / agent["role"]).write_text(f"Rolle {agent['id']}.\n", "utf-8")
    for phase in data["phases"]:
        tasks = [a["task"] for a in phase["actions"].values()]
        gate = phase.get("gate") or {}
        if isinstance(gate.get("action"), dict):
            tasks.append(gate["action"]["task"])
        for rel in tasks:
            (skill / rel).parent.mkdir(parents=True, exist_ok=True)
            (skill / rel).write_text(f"Auftrag aus {rel}.\n", "utf-8")


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    write_profiles(arr_home, {"fake-auth": "claude_oauth", "fake-codex": "codex_chatgpt"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    workfolder = make_workfolder(tmp_path)
    (workfolder / "arr.yaml").write_text(ARR_YAML, "utf-8")
    install(workfolder)
    return workfolder


PHASES = ["ideen", "exploration", "beweis", "pruefung", "urteil", "revision", "zusammenfassung"]


def check_plan(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = {r["name"]: r for r in plan["rows"]}
    assert list(rows) == PHASES
    assert rows["beweis"]["decider_user"] is True and rows["ideen"]["decider_user"] is False
    assert rows["exploration"]["topology"] == "parallel"
    assert "isoliert" in rows["exploration"]["topology_text"]
    assert rows["urteil"]["actions"][0]["agents"] == ["judge-1", "judge-2"]
    assert rows["ideen"]["actions"][0]["format"] == "list"
    texts = json.dumps(plan, ensure_ascii=False)
    assert not (set(texts) & FORBIDDEN)
    return rows


async def test_describe_skill_gives_the_phase_plan(wf: Path) -> None:
    users = UserApi(make_api(wf))
    answer = await users.describe_skill("demo", SKILL)
    assert answer["topology"] == "phases"
    rows = check_plan(answer["phase_plan"])
    assert rows["beweis"]["field"] == "phases[2]" and rows["beweis"]["line"]


async def test_describe_skill_of_an_old_group_has_no_plan(tmp_path: Path, arr_home: Path) -> None:
    from mcp_kit import group_skill

    workfolder = make_workfolder(tmp_path)
    group_skill(workfolder)
    answer = await UserApi(make_api(workfolder)).describe_skill("demo", "runde")
    assert "phase_plan" not in answer


async def test_the_plan_texts_reach_claude_code_as_one_line_each() -> None:
    """A20: text from the YAML (task paths, format names, routes) is one bounded line."""
    row = PhaseRow(
        index=0,
        name="p",
        topology="solo",
        topology_text=f"solo {HOSTILE}",
        actions=[
            PhaseActionInfo(
                key="a", agents=["a"], task=f"tasks/{LONG}", format=HOSTILE, name=HOSTILE
            )
        ],
        gate_text=f"signal: {HOSTILE} → Stopp",
        decider_text=HOSTILE,
        estimate_text=LONG,
    )
    skill = SimpleNamespace(
        name="x",
        kind=SimpleNamespace(value="group"),
        description="d",
        topology=SimpleNamespace(value="phases"),
        params=[],
        agents_spec=[],
        channels_spec=[],
        valid=True,
        issues=[],
        phase_plan=PhasePlan(rows=[row], dropped=[HOSTILE], notes=[LONG]),
    )
    answer = await UserApi(Api([], skill=skill)).describe_skill("demo", "x")
    plan = answer["phase_plan"]
    stack: list[Any] = [plan]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack += list(item.values())
        elif isinstance(item, list):
            stack += item
        elif isinstance(item, str):
            assert one_line(item) and len(item) < 500, item[:80]
    assert "onerror" in plan["rows"][0]["actions"][0]["format"], "text, not removed"


def deviation(answer: dict[str, Any], path: str) -> dict[str, Any]:
    found = [d for d in answer["deviations"] if d["path"] == path]
    assert len(found) == 1, (path, answer["deviations"])
    return found[0]


async def test_estimate_run_gives_the_plan_the_tightening_and_the_warnings(wf: Path) -> None:
    users = UserApi(make_api(wf))
    plain = await users.estimate_run("demo", skill=SKILL)
    assert plain["topology"] == "phases"
    check_plan(plain["phase_plan"])
    assert isinstance(plain["warnings"], list)
    assert not [d for d in plain["deviations"] if d["path"].startswith("tightening.")]
    # own phases: `decider: user` moves from „beweis“ to „ideen“
    config = beispiel()
    config.pop("kind")
    config.pop("rubric")
    phases = {p["name"]: p for p in config["phases"]}
    phases["beweis"].pop("decider")
    phases["ideen"]["decider"] = "user"
    moved = await users.estimate_run("demo", skill=SKILL, config=config)
    removed = deviation(moved, "tightening.removed")
    added = deviation(moved, "tightening.added")
    assert removed["sensitive"] is True and "Phase beweis" in removed["value"]
    assert added["sensitive"] is False and "Phase ideen" in added["value"]
    rows = {r["name"]: r for r in moved["phase_plan"]["rows"]}
    assert rows["ideen"]["decider_user"] and not rows["beweis"]["decider_user"]


async def test_estimate_run_of_an_old_group_has_no_plan(tmp_path: Path, arr_home: Path) -> None:
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    workfolder = prepare_workfolder(make_workfolder(tmp_path))
    answer = await UserApi(make_api(workfolder)).estimate_run("demo", skill="runde")
    assert "phase_plan" not in answer and "warnings" not in answer


# --------------------------------------------------------------------------------------------
# A14: group_schema and NG6
# --------------------------------------------------------------------------------------------


async def test_group_schema_is_the_run_schema_over_mcp(wf: Path) -> None:
    assert "group_schema" in tool_names()
    async with Service(make_api(wf)) as service, service.client(wf / "projects" / "demo") as c:
        result = await c.call_tool("group_schema", {})
        tools = {t.name: t for t in (await c.list_tools()).tools}
    size = sum(len(getattr(part, "text", "")) for part in result.content)
    assert size <= MAX_ANSWER, f"{size} characters"
    answer = result_data(result)
    assert json.loads(answer["schema_json"]) == json_schema("run")
    assert answer["hint"] == SCHEMA_HINT and answer["project"] == "demo"
    for name in ("estimate_run", "start_run"):
        assert "group_schema" in (tools[name].description or ""), name


def test_ng6_the_run_schema_takes_topology_phases_as_text() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json_schema("run")
    validator = jsonschema.Draft202012Validator(schema)
    config = {k: v for k, v in beispiel().items() if k != "kind"}
    data = {"project": "demo", "task": "Aufgabe", **config}
    assert data["topology"] == "phases"
    assert validator.is_valid(data), list(validator.iter_errors(data))[:3]
    assert validate_model(RunInput, data).topology.kind.value == "phases", "the model, too"
    assert validator.is_valid({**data, "topology": {"kind": "phases", "params": {"n": 2}}})
    assert validator.is_valid({k: v for k, v in data.items() if k != "topology"})
    assert not validator.is_valid({**data, "topology": "review"})
    assert not validator.is_valid({**data, "topology": {"kind": "phases", "x": 1}})
    assert '„isolated“: "true"' in schema["description"]
