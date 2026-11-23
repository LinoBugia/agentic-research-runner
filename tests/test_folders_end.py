"""W4b-05a(.1): the end of a run through `run_launch`, as `arr run` and the service do it.

A13: a run without `fresh_folder` leaves the whole tree of its run folder as before (no
`scratch/`, no `retired/`, every working folder = `base/` plus the generated files).
Entscheidung 54: the secret scan sees what an agent left in its scratch folder; only then
arr deletes it.
"""

import re
import secrets
from pathlib import Path
from typing import Any

import pytest
from folders_kit import tree
from run_kit import fake_builders, log_entries, make_workfolder, say, turns, write_profiles

from arr.models import Provider, RunStatus
from arr.runs.launch import plan_launch, run_launch

SOLO = """\
version: 1
project: demo
task: Teste arr.
topology: {kind: phases}
limits: {wall_time: 2m}
defaults: {provider: claude, model: fake-model, auth: fake-auth}
agents: [{id: a}]
groupchats: [{name: main, members: [a]}]
phases:
  - name: arbeit
    topology: solo
    actions: {a: {task: tasks/ohne-skill.md, submission: {format: text}}}
"""

OLD_LAYOUT = [
    r"(run\.lock|meta\.json|run\.input\.yaml|run\.yaml|task\.md|orchestrator\.jsonl|approvals\.jsonl)",
    r"(usage\.json|gatekeeper\.jsonl)",
    r"(base|skills|groupchats|output|tmp)(/.*)?",
    r"agents(/a)?",
    r"agents/a/(agent\.json|role\.md|context\.md|transcript\.jsonl|raw\.jsonl|session\.json)",
    r"agents/a/(transcript\.md|changes\.diff|shell-env\.sh)",
    r"agents/a/(blobs|work|plugin)(/.*)?",
    r"tasks(/.*)?",
]
"""Every path a run folder of a solo run had before W4b-05a (`main` 8185f8d); since W4b-06b
the run is a phases run, which files its assignments under `tasks/` (W4b-02b)."""


def _there(path: Path) -> bool:
    return path.is_symlink() or path.exists()


@pytest.fixture
def workfolder(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    wf = make_workfolder(tmp_path)
    (wf / "arr.yaml").write_text("version: 1\ntoolchain: {run_venv: off}\n", encoding="utf-8")
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return wf


async def launch_run(workfolder: Path, value: str | None, *, scan: bool = True) -> Any:
    """A solo run; the agent leaves `value` in its scratch folder (written when its backend
    is built, so it lies there the whole run)."""
    (workfolder / "run.yaml").write_text(SOLO, encoding="utf-8")
    builders = fake_builders({"a": turns([say("ok")])})
    inner = builders[Provider.CLAUDE]

    def build(prepared: Any, **kwargs: Any) -> Any:
        if value is not None:
            (prepared.run_dir / "scratch" / prepared.id / "notiz.txt").write_text(f"t={value}\n")
        return inner(prepared, **kwargs)

    builders[Provider.CLAUDE] = build
    return await run_launch(
        plan_launch(workfolder / "run.yaml", cwd=workfolder),
        decider=None,
        report=lambda line: None,
        builders=builders,
        signals=False,
        interval_s=0.02,
        scan=scan,
        secret_reader=lambda service, account: value,
    )


async def test_a13_a_run_without_fresh_folder_keeps_the_whole_tree_of_before(
    workfolder: Path,
) -> None:
    result = await launch_run(workfolder, None)
    assert result.status is RunStatus.DONE
    run_dir = result.run_dir
    found = [p.relative_to(run_dir).as_posix() for p in run_dir.rglob("*")]
    strange = [p for p in found if not any(re.fullmatch(r, p) for r in OLD_LAYOUT)]
    assert strange == [], strange[:10]
    work = tree(run_dir / "agents" / "a" / "work")
    base = tree(run_dir / "base")
    assert {k: v for k, v in work.items() if k != "AGENTS.md"} == base
    kinds = {e["kind"] for e in log_entries(run_dir)}
    assert not kinds & {"folder_fresh", "folder_fresh_failed"}
    parked, purged = log_entries(run_dir, "cleanup")
    assert parked["data"]["removed"] == ["scratch"] and purged["data"]["where"] == "after_scan"


@pytest.mark.parametrize("value", [True, False], ids=["wert-im-scratch", "gegenprobe"])
async def test_e54_the_secret_scan_sees_the_scratch_folder_then_it_is_gone(
    workfolder: Path, value: bool
) -> None:
    secret = "WERT" + secrets.token_hex(16) if value else None
    result = await launch_run(workfolder, secret)
    run_dir = result.run_dir
    hits = [f for f in result.scan.findings if "Wert von" in f.kind]
    if value:
        assert len(hits) == 1 and "/scratch/a/notiz.txt" in hits[0].file, hits
    else:
        assert hits == [] and result.scan.clean
    assert not _there(run_dir / "scratch")
    assert not _there(run_dir / "retired"), "nothing parked stays after the scan"


async def test_e54_without_a_scan_arr_run_cleans_up_as_well(workfolder: Path) -> None:
    result = await launch_run(workfolder, "WERT" + secrets.token_hex(8), scan=False)
    assert result.scan is None
    assert not _there(result.run_dir / "scratch")
    assert not _there(result.run_dir / "retired")
