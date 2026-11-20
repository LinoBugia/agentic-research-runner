"""W4b-02b.1 Teil 2, points of the re-check of Teil 1: the names of agents with an alias (N-M1),
the order of cutting down to the core and the objections (N-G1, N-G2), reading files without
following a link anywhere in the path (N-G3), a file name `content: full` cannot read (N-G5)
and a sealed submission of another agent (N-G6).

The two package cases and the file cases follow the probes K1 and F1 of the reviewer
(`np_package_and_files.py`); the run follows `np_review_names.py`.
"""

import copy
import os
import random
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from phases_kit import TurnView, errors, fixture_manager, hand_in, manager, parallel, run, solo
from run_kit import make_workfolder

from arr.models import InputContent, RunStatus
from arr.room.types import Submission
from arr.runs.phases import inputs as inp
from arr.runs.phases.text import FEEDBACK_CORE, MAX_PACKAGE_CHARS, fit_package, package_size

JUDGES = ("judge-1", "judge-2")


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    return make_workfolder(tmp_path)


# --------------------------------------------------------------------------------------------
# N-M1: an agent with an alias is shown to every other agent only under that alias
# --------------------------------------------------------------------------------------------


def judge(verdict: str) -> Any:
    def policy(view: TurnView) -> list[dict[str, Any]]:
        if view.attempt == 1:
            return hand_in(
                view, summary="Gutachten", verdict=verdict, objections=[{"text": "Lücke"}]
            )
        return hand_in(view, summary="Gutachten", verdict="accept")

    return policy


def author(view: TurnView) -> list[dict[str, Any]]:
    return hand_in(view, summary="Fassung")


def leaks(text: str) -> list[str]:
    return [j for j in JUDGES if j in text]


@pytest.mark.parametrize("memory", ["keep", "reset"])
async def test_nm1_the_author_in_review_never_sees_a_judges_id(
    workfolder: Path, memory: str
) -> None:
    mgr, log = fixture_manager(
        workfolder,
        "review",
        {"author": author, "judge-1": judge("minor"), "judge-2": judge("major")},
        params={"discussion_rounds": 0, "judge_memory": memory},
    )
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [revision] = [v for v in log.of("author") if v.stage == "revision"]
    reviews = [i for i in revision.context["inputs"] if i["source"] == "phase:pruefung-allein"]
    aliases = sorted(i["from"] for i in reviews)
    assert len(aliases) == 2 and not leaks(" ".join(aliases)), aliases
    # the alias is the name in the folder of the submission, as today's `review._alias`
    for i in reviews:
        assert Path(i["path"]).name.endswith(i["from"].lower().replace(" ", "-")), i
    for view in log.of("author"):
        assert not leaks(view.prompt), view.prompt
        assert not leaks(str(view.context)), view.context
    # counter-check: a judge sees its own submission and the author under their names
    second = [v for v in log.of("judge-1") if v.attempt == 2][0]
    beweis = [i for i in second.context["inputs"] if i["source"] == "name:beweis"]
    assert beweis and beweis[0]["from"] == "author"


async def test_nm1_a_summarizer_outside_the_channel_sees_only_aliases(workfolder: Path) -> None:
    """A custom group: two judges in an anonymous channel review (isolated), a summarizer
    outside the channel reads their reviews in full, its peers and the author's feedback
    never carry a judge's ID."""
    group = {
        "agents": [{"id": a} for a in ("author", *JUDGES, "sum")],
        "channels": [{"name": "judges", "members": ["judge-*"], "anonymous": True}],
        "phases": [
            solo("fassung", "author", "artifact", name="beweis"),
            {
                **parallel("pruefung", ["judge-*"], "review"),
                "isolated": True,
                "inputs": [{"name": "beweis", "content": "full"}],
            },
            {
                **solo("zusammenfassung", "sum"),
                "inputs": [{"from": "pruefung", "content": "full"}],
            },
            {**solo("revision", "author"), "inputs": [{"from": "pruefung"}]},
        ],
    }
    policies = {"author": author, "sum": author, **{j: judge("minor") for j in JUDGES}}
    mgr, log = manager(workfolder, group, policies)
    assert await run(mgr) is RunStatus.DONE, errors(mgr)
    [summary] = log.of("sum")
    reviews = summary.context["inputs"]
    assert len(reviews) == 2 and all("text" in i and i["verdict"] == "minor" for i in reviews)
    for view in [*log.of("sum"), *log.of("author")]:
        assert not leaks(view.prompt) and not leaks(str(view.context)), view.context
    # counter-check: arr's own records keep the real IDs
    subs = mgr.room.submissions(stage="pruefung")
    assert sorted(s.agent for s in subs) == list(JUDGES)
    assert all(s.shown_as != s.agent for s in subs)


class _Names:
    """`shown_name` as the orchestrator answers it: `alias` only for members of the channel
    and for the agent itself."""

    run_folder = Path("/run")

    def __init__(self, alias: dict[str, str], members: set[str]) -> None:
        self.alias, self.members = alias, members

    def shown_name(self, viewer: str, agent: str) -> str:
        if agent in self.alias and (viewer in self.members or viewer == agent):
            return self.alias[agent]
        return agent


def test_nm1_shown_for_every_viewer() -> None:
    ctx: Any = _Names({"judge-1": "Mitglied A"}, {"judge-1", "judge-2"})
    assert inp.shown(ctx, "author", "judge-1") == "Mitglied A", "outside the channel"
    assert inp.shown(ctx, "judge-2", "judge-1") == "Mitglied A", "inside the channel"
    assert inp.shown(ctx, "judge-1", "judge-1") == "Mitglied A", "itself, as today"
    assert inp.shown(ctx, "judge-1", "author") == "author", "counter-check: no alias"
    assert inp.shown(ctx, "author", "x", "Mitglied X") == "Mitglied X", "the submission's alias"


# --------------------------------------------------------------------------------------------
# N-G6: a sealed submission of another agent never gets a path or text
# --------------------------------------------------------------------------------------------


def _sub(agent: str, *, sealed: bool, files: list[str] | None = None, path: str = "") -> Submission:
    return Submission(
        id=1,
        ts=0.0,
        agent=agent,
        shown_as=agent,
        stage="x",
        format="artifact",
        status="done",
        summary="Kurzfassung",
        files=files or [],
        path=path or ("groupchats/sealed/submission-001" if sealed else "output/s/001"),
        sealed=sealed,
    )


def test_ng6_a_sealed_submission_of_another_has_no_path_and_no_text(tmp_path: Path) -> None:
    files = tmp_path / "groupchats" / "sealed" / "submission-001" / "files"
    files.mkdir(parents=True)
    (files / "beweis.md").write_text("GEHEIM", "utf-8")
    ctx: Any = SimpleNamespace(run_folder=tmp_path, shown_name=lambda viewer, agent: agent)
    sealed = _sub("a", sealed=True, files=["beweis.md"])
    other = inp.item(ctx, "b", sealed, source="phase:x", content=InputContent.FULL)
    assert other["path"] is None and "text" not in other and "note" not in other
    own = inp.item(ctx, "a", sealed, source="phase:x", content=InputContent.FULL)
    assert own["path"] is None and "versiegelt" in own["note"], "counter-check: own"
    assert "GEHEIM" in own["text"], "the author may read its own"
    released = inp.item(ctx, "b", _sub("a", sealed=False), source="phase:x")
    assert released["path"] == str(tmp_path / "output/s/001"), "counter-check: released"


# --------------------------------------------------------------------------------------------
# N-G1, N-G2: cut down to the core of the feedback, then the objections; the result stays
# --------------------------------------------------------------------------------------------


def _objections(n: int) -> list[dict[str, Any]]:
    return [{"id": f"E{k + 1}", "status": "offen", "text": "o" * 300} for k in range(n)]


def test_ng2_the_core_goes_before_the_objections_and_the_result_stays() -> None:
    """The fixed part is large: after inputs and peers the feedback goes to its core, and
    only then objections fall from the end. Cutting objections first would fit as well, so
    the order shows (mutation N9), and the result of the gate stays (N17)."""
    rest = {
        "submission": {"format": "text"},
        "objections": _objections(10),
        "feedback": {"attempt": 1, "gate": "judge", "result": "fail", "submissions": []},
    }
    task = "T" * (MAX_PACKAGE_CHARS + 50 - package_size({**rest, "task": ""}))
    package = {
        **copy.deepcopy(rest),
        "task": task,
        "inputs": [{"id": i, "summary": "i" * 500} for i in range(3)],
        "peers": [{"from": "b", "summary": "p" * 500}],
    }
    out = fit_package(package)
    assert package_size(out) <= MAX_PACKAGE_CHARS
    fb = out["feedback"]
    assert fb["result"] == "fail" and fb["gate"] == "judge", "the result of the gate stays"
    assert set(fb) <= {*FEEDBACK_CORE, "note"} and out["feedback_cut"], "down to the core"
    assert out["objections_cut"] and out["objections"] == _objections(len(out["objections"]))
    assert 0 < len(out["objections"]) < 10, "from the end, not all"
    assert out["inputs_cut"] and out["peers_cut"] and not out["inputs"] and not out["peers"]


def test_ng2_without_need_nothing_of_the_core_or_the_objections_goes() -> None:
    """Counter-check: the same package with a small task keeps feedback and objections."""
    package = {
        "submission": {"format": "text"},
        "task": "T",
        "objections": _objections(10),
        "feedback": {"attempt": 1, "gate": "judge", "result": "fail", "submissions": []},
    }
    out = fit_package(copy.deepcopy(package))
    assert out == package


def _random_package(rng: random.Random) -> dict[str, Any]:
    big = rng.random() < 0.4  # a large fixed part reaches the late steps
    p: dict[str, Any] = {
        "task": "T" * (60_000 if big else rng.choice([100, 20_000])),
        "research_question": "Q" * (rng.choice([5_000, 30_000]) if big else 200),
        "submission": {"format": "text"},
    }
    if rng.random() < 0.8:
        p["inputs"] = [
            {
                "id": i,
                "summary": "s" * rng.randint(0, 800),
                **({"text": "F" * rng.randint(1, 20_000)} if rng.random() < 0.3 else {}),
            }
            for i in range(rng.randint(0, 120))
        ]
    if rng.random() < 0.6:
        p["peers"] = [{"from": "b", "summary": "p" * 800} for _ in range(rng.randint(0, 50))]
    if rng.random() < 0.6:
        p["objections"] = _objections(rng.randint(0, 100))
    if rng.random() < 0.7:
        p["feedback"] = {
            "attempt": 1,
            "gate": "judge",
            "result": "fail",
            "score": 3.0,
            "submissions": [{"id": k, "summary": "u" * 800} for k in range(rng.randint(0, 50))],
            "ratings": [
                {"rater": "r", "ref": k, "score": 5, "comment": "c" * 1000}
                for k in range(rng.randint(0, 150))
            ],
            "reviews": [
                {"from": "j", "verdict": "minor", "objections": [{"text": "r" * 300}] * 20}
                for _ in range(rng.randint(0, 10))
            ],
            **({"user_hint": "h" * 2000} if rng.random() < 0.3 else {}),
        }
    return p


def test_ng2_random_packages_keep_the_order(  # the probe K1 of the reviewer
) -> None:
    # W4b-03: 16 packages of a seed that reaches both branches several times (core 4,
    # objections 5) instead of 400 packages (22 s CPU)
    rng = random.Random(30)
    reached = {"core": 0, "objections": 0}
    for n in range(16):
        before = _random_package(rng)
        out = fit_package(copy.deepcopy(before))
        fixed = package_size({k: out[k] for k in ("task", "research_question", "submission")})
        if fixed < MAX_PACKAGE_CHARS * 0.7:
            assert package_size(out) <= MAX_PACKAGE_CHARS, n
        fb = out.get("feedback") or {}
        extra = set(fb) - {*FEEDBACK_CORE, "note"}
        inputs_cut = len(out.get("inputs") or []) < len(before.get("inputs") or [])
        objections_cut = len(out.get("objections") or []) < len(before.get("objections") or [])
        core = "note" in fb
        reached["core"] += core
        reached["objections"] += objections_cut
        if before.get("feedback"):
            assert fb.get("result") == "fail", f"#{n}: the result of the gate is gone"
        if inputs_cut:
            assert not fb.get("ratings") and not fb.get("submissions"), n
        if core:
            assert not out.get("peers") and not out.get("inputs"), n
        if objections_cut:
            assert not extra and not out.get("inputs") and not out.get("peers"), n
        for key in ("inputs", "peers", "objections"):
            cut = len(out.get(key) or []) < len(before.get(key) or [])
            assert bool(out.get(f"{key}_cut")) == cut, (n, key)
    assert reached["core"] >= 2 and reached["objections"] >= 2, reached


# --------------------------------------------------------------------------------------------
# N-G3, N-G5: files below the run folder, no link anywhere in the path
# --------------------------------------------------------------------------------------------


@pytest.fixture
def run_folder(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    files = run / "output" / "submissions" / "001-x-a" / "files"
    files.mkdir(parents=True)
    outside = tmp_path / "aussen"
    outside.mkdir()
    (outside / "geheim.md").write_text("GEHEIM-AUSSERHALB", "utf-8")
    (files / "gut.md").write_text("Lemma 1 gilt.", "utf-8")
    (files / "link.md").symlink_to(outside / "geheim.md")
    os.mkfifo(files / "fifo.md")
    (files / "ordner.md").mkdir()
    # a link in the middle of the path: the folder `files` of a second submission
    (run / "output" / "submissions" / "002-x-a").mkdir()
    (run / "output" / "submissions" / "002-x-a" / "files").symlink_to(outside)
    (run / "output" / "link-ordner").symlink_to(run / "output" / "submissions")
    return run


FILES = "output/submissions/001-x-a/files"


def head(run: Path, rel: str) -> tuple[str | None, bool]:
    return inp._file_head(SimpleNamespace(run_folder=run), rel, 80_000)  # type: ignore[arg-type]


def test_ng3_a_regular_file_is_read(run_folder: Path) -> None:
    assert head(run_folder, f"{FILES}/gut.md") == ("Lemma 1 gilt.", False)


@pytest.mark.parametrize(
    "rel",
    [
        f"{FILES}/link.md",  # the last part is a link
        "output/submissions/002-x-a/files/geheim.md",  # a folder of the path is a link
        "output/link-ordner/001-x-a/files/gut.md",  # a link to a folder inside the run
        f"{FILES}/ordner.md",  # a folder
        "output/../../aussen/geheim.md",
        "/etc/hosts",
        "",
    ],
    ids=["link last", "link middle", "link inside", "folder", "dotdot", "absolute", "empty"],
)
def test_ng3_no_link_anywhere_and_nothing_outside(run_folder: Path, rel: str) -> None:
    assert head(run_folder, rel) == (None, False)


def test_ng3_a_fifo_gives_no_text_and_does_not_hang(run_folder: Path) -> None:
    box: dict[str, Any] = {}
    reader = threading.Thread(target=lambda: box.update(r=head(run_folder, f"{FILES}/fifo.md")))
    reader.start()
    reader.join(30)  # a bound against a hang only; the read returns at once
    assert not reader.is_alive(), "reading a FIFO hangs"
    assert box["r"] == (None, False)


def test_ng3_the_run_folder_itself_may_lie_below_a_link(run_folder: Path, tmp_path: Path) -> None:
    """`/tmp` is a link on macOS: arr's own run folder may lie below one."""
    (tmp_path / "zum-run").symlink_to(run_folder)
    assert head(tmp_path / "zum-run", f"{FILES}/gut.md") == ("Lemma 1 gilt.", False)


def test_ng5_a_file_full_cannot_read_gets_a_line(run_folder: Path) -> None:
    ctx: Any = SimpleNamespace(run_folder=run_folder)
    s = _sub(
        "a",
        sealed=False,
        files=["gut.md", "a/b.md", "..", "link.md", "fifo.md"],
        path="output/submissions/001-x-a",
    )
    text = inp._full_text(ctx, s)
    assert "[Datei „gut.md“]\nLemma 1 gilt." in text
    for name in ("a/b.md", "..", "link.md", "fifo.md"):
        assert f"[Datei „{name}“: kein lesbarer Text]" in text, name
    assert "GEHEIM" not in text
