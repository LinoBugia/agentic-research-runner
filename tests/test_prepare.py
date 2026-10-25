import errno
import json
import os
import re
import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from arr import __version__
from arr.errors import ArrError, ConfigError
from arr.models import (
    AuthKind,
    AuthProfile,
    ProfilesFile,
    ProjectSettings,
    ResolvedRun,
    RunInput,
    RunStatus,
    Starter,
    WorkfolderSettings,
)
from arr.resolve import load_run_input, load_yaml_model, resolve_run, validate_model
from arr.runs import prepare as prepare_module
from arr.runs.prepare import (
    ALWAYS_EXCLUDED,
    ExcludeSet,
    PreparedRun,
    copy_tree,
    load_agent_file,
    load_run_meta,
    new_run_id,
    prepare_run,
)
from arr.workfolder import init_workfolder, load_project_settings

CONFIG = Path(__file__).parent / "fixtures" / "config"
FIXTURES = Path(__file__).parent / "fixtures" / "workfolder"
NOW = datetime(2026, 10, 25, 13, 5)
RUN_ID_RE = re.compile(r"^20261025-1305_collatz_exploration_[0-9a-f]{4}$")


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    root = init_workfolder(tmp_path / "wf")
    shutil.copytree(FIXTURES / "skills", root / "skills", dirs_exist_ok=True)
    shutil.copytree(FIXTURES / "projects", root / "projects", dirs_exist_ok=True)
    return root


@pytest.fixture
def settings(tmp_path: Path) -> WorkfolderSettings:
    """`arr.yaml` of the fixtures, with a toolchain that exists on this machine."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    base = load_yaml_model(CONFIG / "arr.yaml", WorkfolderSettings)
    return base.model_copy(
        update={
            "toolchain": base.toolchain.model_copy(
                update={"python": str(venv), "extra_path": [str(tools)]}
            )
        }
    )


def make_run(
    workfolder: Path,
    settings: WorkfolderSettings,
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[RunInput, ResolvedRun]:
    """The run.yaml fixture (3 Claude agents, 1 Codex agent), optionally changed, and resolved."""
    data = YAML(typ="safe").load((CONFIG / "run.yaml").read_text(encoding="utf-8"))
    if mutate is not None:
        mutate(data)
    run_input = validate_model(RunInput, data)
    project = load_project_settings(workfolder / "projects" / run_input.project)
    return run_input, resolve_run(run_input, None, settings, project_settings=project)


def prepare(
    workfolder: Path,
    settings: WorkfolderSettings,
    mutate: Callable[[dict[str, Any]], None] | None = None,
    **kwargs: Any,
) -> PreparedRun:
    run_input, resolved = make_run(workfolder, settings, mutate)
    kwargs.setdefault("task_base", FIXTURES)
    kwargs.setdefault("now", NOW)
    return prepare_run(workfolder, run_input, resolved, settings, **kwargs)


def tree(folder: Path) -> list[str]:
    return sorted(
        str(p.relative_to(folder)) + ("/" if p.is_dir() and not p.is_symlink() else "")
        for p in folder.rglob("*")
    )


@pytest.fixture
def run(workfolder: Path, settings: WorkfolderSettings) -> PreparedRun:
    return prepare(workfolder, settings)


# --------------------------------------------------------------------------------------------
# Run ID
# --------------------------------------------------------------------------------------------


def test_run_id_format() -> None:
    run_id = new_run_id("collatz", "exploration", now=NOW)
    assert RUN_ID_RE.fullmatch(run_id)
    assert new_run_id("collatz", None, now=NOW).startswith("20261025-1305_collatz_custom_")
    assert new_run_id("Mein Projekt_1", "a/b", now=NOW).startswith(
        "20261025-1305_Mein-Projekt-1_a-b_"
    )


# --------------------------------------------------------------------------------------------
# A4: structure (docs/02 §4)
# --------------------------------------------------------------------------------------------


def test_run_folder_structure(run: PreparedRun, workfolder: Path) -> None:
    assert RUN_ID_RE.fullmatch(run.run_id)
    assert run.run_dir == workfolder / "runs" / run.run_id
    top = sorted(p.name for p in run.run_dir.iterdir())
    assert top == [
        "agents",
        "base",
        "groupchats",
        "meta.json",
        "output",
        "run.input.yaml",
        "run.yaml",
        "skills",
        "task.md",
    ]
    assert sorted(p.name for p in (run.run_dir / "agents").iterdir()) == [
        "explorer-1",
        "explorer-2",
        "explorer-gpt",
        "ranker",
    ]
    for agent_id in ("explorer-1", "explorer-2", "explorer-gpt", "ranker"):
        files = sorted(p.name for p in (run.run_dir / "agents" / agent_id).iterdir())
        assert "agent.json" in files
        assert "role.md" in files
        assert "context.md" in files
        assert "work" in files
        assert "blobs" in files
    assert (run.run_dir / "output" / "results").is_dir()
    assert (run.run_dir / "groupchats" / "attachments").is_dir()
    assert not (run.run_dir / "groupchats" / "room.sqlite").exists(), "room DB is W2-03"
    assert not (run.run_dir / ".venv").exists(), "the run venv is created when needed"


def test_meta_json_is_preparing(run: PreparedRun) -> None:
    meta = load_run_meta(run.run_dir)
    assert meta == run.meta
    assert meta.status is RunStatus.PREPARING
    assert meta.starter is Starter.CLI
    assert meta.run_id == run.run_id
    assert meta.project == "collatz"
    assert meta.skill == "exploration"
    assert meta.arr_version == __version__
    assert meta.created == NOW.timestamp()
    assert meta.started is None
    raw = json.loads((run.run_dir / "meta.json").read_text(encoding="utf-8"))
    assert raw["status"] == "preparing"


def test_starter_is_recorded(workfolder: Path, settings: WorkfolderSettings) -> None:
    result = prepare(workfolder, settings, starter=Starter.USER_AGENT)
    assert load_run_meta(result.run_dir).starter is Starter.USER_AGENT


def test_run_yaml_and_input_roundtrip(
    run: PreparedRun, workfolder: Path, settings: WorkfolderSettings
) -> None:
    stored = load_yaml_model(run.run_dir / "run.yaml", ResolvedRun)
    assert stored == run.resolved
    assert [a.id for a in stored.agents] == ["explorer-1", "explorer-2", "explorer-gpt", "ranker"]
    assert stored.task_file == "task.md"
    assert stored.task is None

    given = load_run_input(CONFIG / "run.yaml")
    kept = load_run_input(run.run_dir / "run.input.yaml")
    assert kept == given, "run.input.yaml is the input as given"

    _, resolved = make_run(workfolder, settings)
    assert stored.agents == resolved.agents
    assert stored.limits == resolved.limits


def test_task_md_from_file_and_inline(workfolder: Path, settings: WorkfolderSettings) -> None:
    from_file = prepare(workfolder, settings)
    assert (from_file.run_dir / "task.md").read_text(encoding="utf-8") == (
        FIXTURES / "task.md"
    ).read_text(encoding="utf-8")

    def inline(data: dict[str, Any]) -> None:
        del data["task_file"]
        data["task"] = "Zeige, dass 3n+1 terminiert."

    result = prepare(workfolder, settings, inline)
    assert (result.run_dir / "task.md").read_text(
        encoding="utf-8"
    ) == "Zeige, dass 3n+1 terminiert."
    assert result.resolved.task_file == "task.md"


def test_role_md_is_the_role_file(run: PreparedRun, workfolder: Path) -> None:
    explorer = (workfolder / "skills" / "exploration" / "roles" / "explorer.md").read_text("utf-8")
    ranker = (workfolder / "skills" / "exploration" / "roles" / "ranker.md").read_text("utf-8")
    for agent_id in ("explorer-1", "explorer-2", "explorer-gpt"):
        assert (run.run_dir / "agents" / agent_id / "role.md").read_text("utf-8") == explorer
    assert (run.run_dir / "agents" / "ranker" / "role.md").read_text("utf-8") == ranker


def test_agent_without_role_gets_the_default_text(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    def no_role(data: dict[str, Any]) -> None:
        del data["agents"][2]["role"]

    result = prepare(workfolder, settings, no_role)
    text = (result.run_dir / "agents" / "ranker" / "role.md").read_text("utf-8")
    assert "submit_result" in text
    assert load_agent_file(result.run_dir, "ranker").role_file == "agents/ranker/role.md"


def test_role_from_the_project_when_there_is_no_skill(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    roles = workfolder / "projects" / "collatz" / "roles"
    roles.mkdir()
    (roles / "solo.md").write_text("# Solo\n", encoding="utf-8")

    def solo(data: dict[str, Any]) -> None:
        del data["skill"]
        data["agents"] = [{"id": "solo", "role": "roles/solo.md"}]
        data["groupchats"] = []
        data["topology"] = {"kind": "solo"}

    result = prepare(workfolder, settings, solo)
    assert result.run_id.startswith("20261025-1305_collatz_custom_")
    assert (result.run_dir / "agents" / "solo" / "role.md").read_text("utf-8") == "# Solo\n"
    assert not (result.run_dir / "work").exists()


# --------------------------------------------------------------------------------------------
# Copies: base/, work/, excludes, modes
# --------------------------------------------------------------------------------------------


def test_base_is_the_project_without_excluded_files(run: PreparedRun, workfolder: Path) -> None:
    project = workfolder / "projects" / "collatz"
    base = run.run_dir / "base"
    assert (base / "notes" / "ideen.md").read_text("utf-8") == (
        project / "notes" / "ideen.md"
    ).read_text("utf-8")
    assert (base / "tex" / "main.tex").is_file()
    assert (base / "CLAUDE.md").read_text("utf-8") == (project / "CLAUDE.md").read_text("utf-8")
    assert not (base / "data" / "big").exists(), "copy_exclude: data/big/**"
    assert not (base / "literatur.pdf").exists(), "copy_exclude: *.pdf"
    assert not (base / ".claude").exists(), "skill links of the user agent are not copied"


def test_work_copies_are_independent_of_base_and_project(
    run: PreparedRun, workfolder: Path
) -> None:
    project_file = workfolder / "projects" / "collatz" / "notes" / "ideen.md"
    original = project_file.read_text("utf-8")
    work = run.agents["explorer-1"].work_dir
    assert work == run.run_dir / "agents" / "explorer-1" / "work"
    assert (work / "notes" / "ideen.md").read_text("utf-8") == original

    (work / "notes" / "ideen.md").write_text("geändert", encoding="utf-8")
    (work / "neu.md").write_text("neu", encoding="utf-8")

    assert (run.run_dir / "base" / "notes" / "ideen.md").read_text("utf-8") == original
    assert project_file.read_text("utf-8") == original
    assert (run.agents["explorer-2"].work_dir / "notes" / "ideen.md").read_text("utf-8") == original
    assert not (workfolder / "projects" / "collatz" / "neu.md").exists()


def test_work_copy_differs_from_base_only_in_generated_files(run: PreparedRun) -> None:
    base = tree(run.run_dir / "base")
    for agent_id in ("explorer-1", "explorer-2"):
        work = tree(run.agents[agent_id].work_dir)
        assert [p for p in work if p not in base] == []
        assert [p for p in base if p not in work] == []
        changed = [
            p
            for p in work
            if not p.endswith("/")
            and (run.agents[agent_id].work_dir / p).read_bytes()
            != (run.run_dir / "base" / p).read_bytes()
        ]
        assert changed == ["AGENTS.md", "CLAUDE.md"]


def test_empty_workspace_has_only_the_generated_context(run: PreparedRun) -> None:
    ranker = run.agents["ranker"]
    assert tree(ranker.work_dir) == ["AGENTS.md"]
    assert ranker.file.copy_method is None
    assert ranker.file.generated_paths == ["AGENTS.md"]
    text = (ranker.work_dir / "AGENTS.md").read_text("utf-8")
    assert text.startswith("<!-- arr:research-agent -->")
    assert "Dieser Ordner ist **leer**" in text
    assert "Collatz-Varianten" not in text, "no project context in an empty folder"


def test_shared_workspace_is_one_copy_for_several_agents(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    def shared(data: dict[str, Any]) -> None:
        for agent in data["agents"][:2]:
            agent["workspace"] = {"mode": "shared", "name": "beweis"}
        data["agents"][1]["skills"] = ["sympy-check"]
        data["agents"][1]["provider"] = "codex"
        data["agents"][1]["model"] = "gpt-6.1-sol"
        data["agents"][1]["auth"] = "chatgpt-privat"
        data["agents"][2]["read_dirs"] = ["run:shared/beweis"]

    result = prepare(workfolder, settings, shared)
    folder = result.run_dir / "shared" / "beweis"
    explorer, gpt = result.agents["explorer-1"], result.agents["explorer-gpt"]
    assert explorer.work_dir == folder
    assert gpt.work_dir == folder
    assert explorer.file.work_dir == "shared/beweis"
    assert not (result.run_dir / "agents" / "explorer-1" / "work").exists()
    assert (folder / "notes" / "ideen.md").is_file()
    text = (folder / "AGENTS.md").read_text("utf-8")
    assert "geteilte Kopie" in text
    assert "explorer-1" not in text, "the shared file is neutral"
    assert (folder / "CLAUDE.md").read_text("utf-8") == text
    assert (folder / ".agents" / "skills" / "sympy-check" / "SKILL.md").is_file()
    assert gpt.file.generated_paths == ["CLAUDE.md", "AGENTS.md", ".agents/skills"]
    own = (result.run_dir / "agents" / "explorer-1" / "context.md").read_text("utf-8")
    assert "Du bist `explorer-1`" in own, "context.md is personal even in a shared folder"
    assert explorer.file.copy_method in ("clone", "copy")


def test_copy_exclude_patterns() -> None:
    excludes = ExcludeSet(
        ["data/big/**", "*.pdf", "build/", "/top.txt", "**/cache", "doc/*.tmp", "a?c.md"]
    )

    def hit(path: str, is_dir: bool = False) -> bool:
        return excludes.matches(path, is_dir=is_dir)

    assert hit("data/big", True)
    assert hit("data/big/x.bin")
    assert not hit("data/small.bin")
    assert hit("a.pdf") and hit("sub/dir/b.pdf")
    assert not hit("pdf")
    assert hit("build", True) and hit("x/build", True)
    assert not hit("build"), "a trailing slash matches folders only"
    assert hit("top.txt") and not hit("sub/top.txt")
    assert hit("cache", True) and hit("a/b/cache", True)
    assert hit("doc/x.tmp") and not hit("doc/sub/x.tmp")
    assert hit("abc.md") and not hit("abbc.md")


@pytest.mark.parametrize("pattern", ["", "!keep.md", "#kommentar", "/"])
def test_invalid_copy_exclude_patterns(pattern: str) -> None:
    with pytest.raises(ConfigError) as caught:
        ExcludeSet(["ok", pattern])
    assert caught.value.path == "copy_exclude[1]"


def test_skill_folders_of_the_project_are_never_copied() -> None:
    assert ".claude/skills" in ALWAYS_EXCLUDED
    assert ".agents/skills" in ALWAYS_EXCLUDED


def test_copy_tree_keeps_internal_symlinks_and_metadata(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "f.txt").write_text("inhalt", encoding="utf-8")
    (src / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (src / "run.sh").chmod(0o755)
    (src / "link").symlink_to("sub/f.txt")
    (src / "dirlink").symlink_to("sub")
    os.utime(src / "sub" / "f.txt", (1_700_000_000, 1_700_000_000))

    stats = copy_tree(src, tmp_path / "dst")

    dst = tmp_path / "dst"
    assert (dst / "sub" / "f.txt").read_text("utf-8") == "inhalt"
    assert os.access(dst / "run.sh", os.X_OK)
    assert (dst / "link").is_symlink() and os.readlink(dst / "link") == "sub/f.txt"
    assert os.readlink(dst / "dirlink") == "sub"
    assert int((dst / "sub" / "f.txt").stat().st_mtime) == 1_700_000_000
    assert stats.cloned + stats.copied == 2
    assert stats.symlinks == 2
    assert stats.external_links == []


def test_copy_tree_drops_links_that_leave_the_tree(tmp_path: Path) -> None:
    """M1: no link in a copy may lead to the original or to anything outside."""
    outside = tmp_path / "aussen"
    (outside / "ordner").mkdir(parents=True)
    (outside / "geheim.txt").write_text("geheim", encoding="utf-8")
    (outside / "ordner" / "x.txt").write_text("x", encoding="utf-8")
    src = tmp_path / "projekt"
    (src / "notes").mkdir(parents=True)
    (src / "notes" / "ideen.md").write_text("Original", encoding="utf-8")
    (src / "notes" / "abs-intern.md").symlink_to(src / "notes" / "ideen.md")  # absolute, inside
    (src / "notes" / "raus.txt").symlink_to("../../aussen/geheim.txt")  # relative with ..
    (src / "ordner-aussen").symlink_to(outside / "ordner")  # folder outside
    (src / "kaputt").symlink_to("gibts-nicht")  # broken
    (src / "zyklus-a").symlink_to("zyklus-b")  # loop
    (src / "zyklus-b").symlink_to("zyklus-a")

    stats = copy_tree(src, tmp_path / "kopie")

    dst = tmp_path / "kopie"
    assert os.readlink(dst / "notes" / "abs-intern.md") == "ideen.md", "absolute → relative"
    assert sorted(stats.external_links) == [
        "kaputt",
        "notes/raus.txt",
        "ordner-aussen",
        "zyklus-a",
        "zyklus-b",
    ]
    for name in ("kaputt", "notes/raus.txt", "ordner-aussen", "zyklus-a", "zyklus-b"):
        assert not os.path.lexists(dst / name), name

    # writing in the copy changes neither the project nor anything outside
    (dst / "notes" / "abs-intern.md").write_text("geändert", encoding="utf-8")
    assert (dst / "notes" / "ideen.md").read_text("utf-8") == "geändert"
    assert (src / "notes" / "ideen.md").read_text("utf-8") == "Original"
    assert (outside / "geheim.txt").read_text("utf-8") == "geheim"


def test_external_links_are_reported_and_cannot_reach_the_original(
    workfolder: Path, settings: WorkfolderSettings, tmp_path: Path
) -> None:
    outside = tmp_path / "aussen.txt"
    outside.write_text("aussen", encoding="utf-8")
    project = workfolder / "projects" / "collatz"
    (project / "notes" / "abs-link.md").symlink_to(project / "notes" / "ideen.md")
    (project / "extern").symlink_to(outside)

    result = prepare(workfolder, settings)

    work = result.agents["explorer-1"].work_dir
    assert os.readlink(work / "notes" / "abs-link.md") == "ideen.md"
    assert not os.path.lexists(work / "extern")
    (work / "notes" / "abs-link.md").write_text("vom Agenten", encoding="utf-8")
    assert "2-adische" in (project / "notes" / "ideen.md").read_text("utf-8")
    assert outside.read_text("utf-8") == "aussen"

    link_warnings = [w for w in result.warnings if "Symlink" in w]
    assert link_warnings == [
        "Der Symlink „extern“ im Projekt zeigt aus dem Projekt hinaus oder ins Leere und wurde "
        "nicht übernommen. Lege die Datei selbst dort ab, wenn die Agenten sie brauchen."
    ]
    assert str(outside) not in " ".join(result.warnings), "the target is never named"
    log = [
        json.loads(line)
        for line in (result.run_dir / "orchestrator.jsonl").read_text("utf-8").splitlines()
    ]
    entries = [e["data"] for e in log if e["data"]["code"] == "external_symlink"]
    assert entries == [{"code": "external_symlink", "message": link_warnings[0], "path": "extern"}]


# --------------------------------------------------------------------------------------------
# A4: fallback when cloning is impossible
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", [errno.ENOTSUP, errno.EXDEV, errno.EOPNOTSUPP])
def test_fallback_to_real_copy_with_warning(
    workfolder: Path,
    settings: WorkfolderSettings,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    calls: list[str] = []

    def no_clone(src: str, dst: str) -> None:
        calls.append(src)
        raise OSError(code, os.strerror(code), src)

    monkeypatch.setattr(prepare_module, "_clonefile", no_clone)
    result = prepare(workfolder, settings)

    project = workfolder / "projects" / "collatz"
    work = result.agents["explorer-1"].work_dir
    assert (work / "notes" / "ideen.md").read_text("utf-8") == (
        project / "notes" / "ideen.md"
    ).read_text("utf-8")
    assert (result.run_dir / "base" / "tex" / "main.tex").is_file()
    assert result.agents["explorer-1"].file.copy_method == "copy"
    assert result.agents["ranker"].file.copy_method is None

    assert result.warnings
    assert all("Kopie statt Klon" in w for w in result.warnings)
    assert any(w.startswith("Kopie statt Klon für base/") for w in result.warnings)
    log = [
        json.loads(line)
        for line in (result.run_dir / "orchestrator.jsonl").read_text("utf-8").splitlines()
    ]
    assert {entry["kind"] for entry in log} == {"warning"}
    assert {entry["data"]["code"] for entry in log} == {"clone_fallback"}
    assert any(entry["data"]["target"] == "base/" for entry in log)
    # one failed attempt per copied tree, then no more tries in that tree
    assert len(calls) == len(result.warnings)


def test_other_clone_errors_are_not_swallowed(
    workfolder: Path, settings: WorkfolderSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(src: str, dst: str) -> None:
        raise OSError(errno.EACCES, "Keine Berechtigung", src)

    monkeypatch.setattr(prepare_module, "_clonefile", broken)
    with pytest.raises(PermissionError):
        prepare(workfolder, settings)
    assert list((workfolder / "runs").iterdir()) == [], "a failed preparation leaves no folder"


def test_no_warning_when_cloning_works(
    workfolder: Path, settings: WorkfolderSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def clone(src: str, dst: str) -> None:
        shutil.copy2(src, dst)

    monkeypatch.setattr(prepare_module, "_clonefile", clone)
    result = prepare(workfolder, settings)
    assert result.warnings == []
    assert not (result.run_dir / "orchestrator.jsonl").exists()
    assert result.agents["explorer-1"].file.copy_method == "clone"


# --------------------------------------------------------------------------------------------
# A5: block swap, AGENTS.md, context.md
# --------------------------------------------------------------------------------------------


def test_block_swap_in_every_working_copy(run: PreparedRun, workfolder: Path) -> None:
    for agent_id in ("explorer-1", "explorer-2", "explorer-gpt"):
        agent = run.agents[agent_id]
        claude = (agent.work_dir / "CLAUDE.md").read_text("utf-8")
        assert "<!-- arr:user-agent -->" not in claude
        assert "start_run" not in claude, "no hints about tools the agent does not have"
        assert claude.count("<!-- arr:research-agent -->") == 1
        assert claude.count("<!-- /arr:research-agent -->") == 1
        assert claude.startswith("# Projekt: Collatz-Varianten")
        assert "Stoppzeit: σ(n)." in claude, "the project context stays"
        assert f"`{run.run_id}`" in claude
        assert f"Du bist `{agent_id}` (Rolle: `explorer`)" in claude
        assert "Kopie** des Projekts „collatz“" in claude
        assert "`submit_result`" in claude
        assert "- `main`: Zwischenstände teilen, Ansätze gegenseitig prüfen" in claude

    ranker = (run.agents["ranker"].work_dir / "AGENTS.md").read_text("utf-8")
    assert "Du bist `ranker` (Rolle: `ranker`)" in ranker
    assert "- `ranking`" in ranker
    assert "`main`" not in ranker, "only the agent's own channels"

    original = (workfolder / "projects" / "collatz" / "CLAUDE.md").read_text("utf-8")
    assert "<!-- arr:user-agent -->" in original, "the project itself is never changed"
    assert "<!-- arr:user-agent -->" in (run.run_dir / "base" / "CLAUDE.md").read_text("utf-8")


def test_agents_md_has_exactly_the_processed_content(run: PreparedRun) -> None:
    for agent_id in ("explorer-1", "explorer-2", "explorer-gpt"):
        agent = run.agents[agent_id]
        agents_md = (agent.work_dir / "AGENTS.md").read_text("utf-8")
        assert agents_md == (agent.work_dir / "CLAUDE.md").read_text("utf-8")
        assert agents_md == (agent.agent_dir / "context.md").read_text("utf-8")
        assert "Die Anweisungen für dieses Projekt stehen in" not in agents_md, (
            "the project's own AGENTS.md pointer is replaced"
        )
    assert (run.agents["ranker"].agent_dir / "context.md").read_text("utf-8") == (
        run.agents["ranker"].work_dir / "AGENTS.md"
    ).read_text("utf-8")


def test_context_is_personal_per_agent(run: PreparedRun) -> None:
    one = (run.agents["explorer-1"].agent_dir / "context.md").read_text("utf-8")
    two = (run.agents["explorer-2"].agent_dir / "context.md").read_text("utf-8")
    assert one != two
    assert "explorer-1" in one and "explorer-2" in two


def test_agents_md_symlink_in_the_project_is_replaced_not_followed(
    workfolder: Path, settings: WorkfolderSettings, tmp_path: Path
) -> None:
    outside = tmp_path / "ausserhalb.md"
    outside.write_text("Original\n", encoding="utf-8")
    project = workfolder / "projects" / "collatz"
    (project / "AGENTS.md").unlink()
    (project / "AGENTS.md").symlink_to(outside)
    (project / "CLAUDE.md").unlink()
    (project / "CLAUDE.md").symlink_to("AGENTS.md")
    outside.write_text(
        "# Projekt\n\n<!-- arr:user-agent -->\nalt\n<!-- /arr:user-agent -->\n", encoding="utf-8"
    )
    result = prepare(workfolder, settings)
    work = result.agents["explorer-1"].work_dir
    assert not (work / "AGENTS.md").is_symlink()
    assert not (work / "CLAUDE.md").is_symlink()
    assert "arr:research-agent" in (work / "AGENTS.md").read_text("utf-8")
    assert "alt" in outside.read_text("utf-8"), "the original behind the link is untouched"
    assert "research-agent" not in outside.read_text("utf-8")


def test_project_without_claude_md(workfolder: Path, settings: WorkfolderSettings) -> None:
    project = workfolder / "projects" / "collatz"
    (project / "CLAUDE.md").unlink()
    (project / "AGENTS.md").write_text("# Eigene Agenten-Hinweise\n", encoding="utf-8")
    result = prepare(workfolder, settings)
    work = result.agents["explorer-1"].work_dir
    assert not (work / "CLAUDE.md").exists(), "no CLAUDE.md is invented"
    text = (work / "AGENTS.md").read_text("utf-8")
    assert text.startswith("# Eigene Agenten-Hinweise")
    assert "<!-- arr:research-agent -->" in text
    assert result.agents["explorer-1"].file.generated_paths == ["AGENTS.md"]

    (project / "AGENTS.md").unlink()
    bare = prepare(workfolder, settings)
    assert (
        (bare.agents["explorer-1"].work_dir / "AGENTS.md")
        .read_text("utf-8")
        .startswith("<!-- arr:research-agent -->")
    )


def test_claude_md_without_a_user_agent_block_gets_the_block_appended(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    (workfolder / "projects" / "collatz" / "CLAUDE.md").write_text("# Nur Kontext\n", "utf-8")
    result = prepare(workfolder, settings)
    text = (result.agents["explorer-1"].work_dir / "CLAUDE.md").read_text("utf-8")
    assert text.startswith("# Nur Kontext\n\n<!-- arr:research-agent -->")


# --------------------------------------------------------------------------------------------
# A6: skills per backend
# --------------------------------------------------------------------------------------------


def test_snapshot_has_the_used_skills_only(run: PreparedRun) -> None:
    assert sorted(p.name for p in (run.run_dir / "skills").iterdir()) == [
        "exploration",
        "sympy-check",
    ]
    assert (run.run_dir / "skills" / "exploration" / "group.yaml").is_file()
    assert (run.run_dir / "skills" / "sympy-check" / "scripts" / "check.py").is_file()


def test_claude_agents_get_a_plugin_with_their_skills(run: PreparedRun) -> None:
    for agent_id in ("explorer-1", "explorer-2"):
        agent = run.agents[agent_id]
        assert agent.plugin_dir == run.run_dir / "agents" / agent_id / "plugin"
        manifest = json.loads(
            (agent.plugin_dir / ".claude-plugin" / "plugin.json").read_text("utf-8")
        )
        assert manifest["name"] == "arr"
        assert sorted(p.name for p in (agent.plugin_dir / "skills").iterdir()) == ["sympy-check"]
        assert (agent.plugin_dir / "skills" / "sympy-check" / "SKILL.md").is_file()
        assert (agent.plugin_dir / "skills" / "sympy-check" / "scripts" / "check.py").is_file()
        assert agent.file.skill_ids == ["arr:sympy-check"]
        assert agent.file.plugin_dir == f"agents/{agent_id}/plugin"
        assert not (agent.work_dir / ".agents").exists()
        assert not (agent.work_dir / ".claude").exists()
        assert ".agents/skills" not in agent.file.generated_paths


def test_agent_without_skills_has_no_plugin(run: PreparedRun) -> None:
    for agent_id in ("explorer-gpt", "ranker"):
        agent = run.agents[agent_id]
        assert agent.plugin_dir is None
        assert agent.file.skill_ids == []
        assert not (agent.work_dir / ".agents").exists()
        assert not (agent.agent_dir / "plugin").exists()


def test_codex_agents_get_agents_skills_in_the_working_copy(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    def with_skills(data: dict[str, Any]) -> None:
        data["agents"][1]["skills"] = ["sympy-check", "lean-notes"]

    result = prepare(workfolder, settings, with_skills)
    gpt = result.agents["explorer-gpt"]
    skills = gpt.work_dir / ".agents" / "skills"
    assert sorted(p.name for p in skills.iterdir()) == ["lean-notes", "sympy-check"]
    assert (skills / "sympy-check" / "scripts" / "check.py").is_file()
    assert gpt.plugin_dir is None
    assert gpt.file.skill_ids == ["sympy-check", "lean-notes"]
    assert gpt.file.generated_paths == ["CLAUDE.md", "AGENTS.md", ".agents/skills"]
    # other agents stay without
    assert not (result.agents["explorer-1"].work_dir / ".agents").exists()


def test_project_skill_folders_do_not_reach_research_agents(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    project = workfolder / "projects" / "collatz"
    (project / ".claude" / "skills").mkdir(parents=True)
    (project / ".claude" / "skills" / "arr").symlink_to("../../../../skills/arr")
    (project / ".agents" / "skills" / "eigen").mkdir(parents=True)
    (project / ".agents" / "skills" / "eigen" / "SKILL.md").write_text("x", encoding="utf-8")
    result = prepare(workfolder, settings)
    for agent_id in ("explorer-1", "explorer-gpt"):
        work = result.agents[agent_id].work_dir
        assert not (work / ".claude" / "skills").exists()
        assert not (work / ".agents" / "skills").exists()


def test_group_skills_and_arr_never_go_to_research_agents(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    def forbidden(data: dict[str, Any]) -> None:
        data["agents"][0]["skills"] = ["sympy-check", "exploration"]
        data["agents"][1]["skills"] = ["arr"]

    with pytest.raises(ConfigError) as caught:
        prepare(workfolder, settings, forbidden)
    assert [i.path for i in caught.value.issues] == [
        "agents[0].skills[1]",
        "agents[1].skills[1]",
        "agents[2].skills[0]",
    ]
    assert "E11" in str(caught.value)
    assert list((workfolder / "runs").iterdir()) == []


# --------------------------------------------------------------------------------------------
# Toolchain and agent.json
# --------------------------------------------------------------------------------------------


def test_agent_json_holds_runtime_data(run: PreparedRun, settings: WorkfolderSettings) -> None:
    venv = settings.toolchain.python
    tools = settings.toolchain.extra_path[0]
    for agent_id, agent in run.agents.items():
        file = load_agent_file(run.run_dir, agent_id)
        assert file == agent.file
        assert file.config.id == agent_id
        assert file.path_prepend == [f"{venv}/bin", tools]
        assert file.base_python == venv
        assert file.run_venv.value == "overlay"
        assert file.work_dir == f"agents/{agent_id}/work"
        assert file.context_file == f"agents/{agent_id}/context.md"
    raw = (run.run_dir / "agents" / "explorer-gpt" / "agent.json").read_text("utf-8")
    assert json.loads(raw)["config"]["provider"] == "codex"
    assert json.loads(raw)["config"]["auth"] == "chatgpt-privat", "only the profile name"


def test_missing_toolchain_paths_warn_but_do_not_fail(
    workfolder: Path, settings: WorkfolderSettings, tmp_path: Path
) -> None:
    missing = settings.model_copy(
        update={
            "toolchain": settings.toolchain.model_copy(
                update={"python": str(tmp_path / "nirgends"), "extra_path": [str(tmp_path / "weg")]}
            )
        }
    )
    result = prepare(workfolder, missing)
    toolchain = [w for w in result.warnings if "Kopie statt Klon" not in w]
    assert len(toolchain) == 2
    assert "Python-Umgebung" in toolchain[0]
    assert "extra_path" in toolchain[1]
    file = result.agents["ranker"].file
    assert file.base_python is None, "G7: a missing venv is not handed on"
    assert file.path_prepend == [str(tmp_path / "weg")]


def test_base_python_is_always_the_venv_folder(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    venv = Path(str(settings.toolchain.python))
    (venv / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    as_interpreter = settings.model_copy(
        update={
            "toolchain": settings.toolchain.model_copy(
                update={"python": str(venv / "bin" / "python"), "extra_path": []}
            )
        }
    )
    file = prepare(workfolder, as_interpreter).agents["explorer-1"].file
    assert file.base_python == str(venv)
    assert file.path_prepend == [str(venv / "bin")]


def test_no_toolchain_means_no_path_prefix(workfolder: Path, settings: WorkfolderSettings) -> None:
    bare = settings.model_copy(
        update={
            "toolchain": settings.toolchain.model_copy(update={"python": None, "extra_path": []})
        }
    )
    result = prepare(workfolder, bare)
    assert result.agents["explorer-1"].file.path_prepend == []
    assert result.agents["explorer-1"].file.base_python is None


def test_settings_default_to_the_workfolders_arr_yaml(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    run_input, resolved = make_run(workfolder, settings)
    result = prepare_run(workfolder, run_input, resolved, task_base=FIXTURES, now=NOW)
    assert result.run_dir.is_dir()
    assert result.agents["explorer-1"].file.path_prepend == []


# --------------------------------------------------------------------------------------------
# Validation before anything is created
# --------------------------------------------------------------------------------------------


def runs_are_empty(workfolder: Path) -> bool:
    return not (workfolder / "runs").exists() or list((workfolder / "runs").iterdir()) == []


def test_unknown_project(workfolder: Path, settings: WorkfolderSettings) -> None:
    def other(data: dict[str, Any]) -> None:
        data["project"] = "gibtsnicht"

    run_input, resolved = make_run(workfolder, settings, other)
    with pytest.raises(ConfigError) as caught:
        prepare_run(workfolder, run_input, resolved, settings, task_base=FIXTURES)
    assert caught.value.path == "project"
    assert "gibtsnicht" in str(caught.value)
    assert runs_are_empty(workfolder)


def test_problems_are_collected_with_paths(workfolder: Path, settings: WorkfolderSettings) -> None:
    def broken(data: dict[str, Any]) -> None:
        data["agents"][0]["role"] = "roles/fehlt.md"  # explorer-1 and explorer-2
        data["agents"][1]["role"] = "../../geheim.md"  # explorer-gpt
        data["agents"][2]["skills"] = ["sympy-chek"]  # ranker
        data["agents"][2]["read_dirs"] = ["run:shared/nix"]

    with pytest.raises(ConfigError) as caught:
        prepare(workfolder, settings, broken)
    issues = {i.path: i.message for i in caught.value.issues}
    assert set(issues) == {
        "agents[0].role",
        "agents[1].role",
        "agents[2].role",
        "agents[3].skills[0]",
        "agents[3].read_dirs[0]",
    }
    assert "roles/fehlt.md" in issues["agents[0].role"]
    assert "ohne „..“" in issues["agents[2].role"]
    assert "Meintest du „sympy-check“" in issues["agents[3].skills[0]"]
    assert "geteilte Kopie" in issues["agents[3].read_dirs[0]"]
    assert str(caught.value).startswith("Konfiguration ungültig (run.yaml)")
    assert runs_are_empty(workfolder)


def test_unknown_run_skill_is_reported_once(workfolder: Path, settings: WorkfolderSettings) -> None:
    def other(data: dict[str, Any]) -> None:
        data["skill"] = "exploratoin"

    with pytest.raises(ConfigError) as caught:
        prepare(workfolder, settings, other)
    assert [i.path for i in caught.value.issues] == ["skill"]
    assert "Meintest du „exploration“" in caught.value.issue_message
    assert runs_are_empty(workfolder)


def test_task_file_problems(workfolder: Path, settings: WorkfolderSettings) -> None:
    run_input, resolved = make_run(workfolder, settings)
    with pytest.raises(ConfigError) as caught:
        prepare_run(workfolder, run_input, resolved, settings, task_base=FIXTURES / "nirgends")
    assert caught.value.path == "task_file"
    with pytest.raises(ConfigError) as caught:
        prepare_run(workfolder, run_input, resolved, settings)
    assert caught.value.path == "task_file"
    assert runs_are_empty(workfolder)


def test_auth_profiles_are_checked_when_given(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    run_input, resolved = make_run(workfolder, settings)
    profiles = ProfilesFile(
        profiles=[
            AuthProfile(name="max-privat", kind=AuthKind.CLAUDE_OAUTH),
            AuthProfile(name="chatgpt-privat", kind=AuthKind.CLAUDE_API_KEY),
        ]
    )
    with pytest.raises(ConfigError) as caught:
        prepare_run(
            workfolder, run_input, resolved, settings, task_base=FIXTURES, profiles=profiles
        )
    assert [i.path for i in caught.value.issues] == ["agents[2].auth"]
    assert "passt nicht zum Anbieter" in caught.value.issues[0].message

    with pytest.raises(ConfigError, match="gibt es nicht") as caught:
        prepare_run(
            workfolder,
            run_input,
            resolved,
            settings,
            task_base=FIXTURES,
            profiles=ProfilesFile(profiles=[]),
        )
    assert len(caught.value.issues) == 4
    assert runs_are_empty(workfolder)

    good = ProfilesFile(
        profiles=[
            AuthProfile(name="max-privat", kind=AuthKind.CLAUDE_OAUTH),
            AuthProfile(name="chatgpt-privat", kind=AuthKind.CODEX_CHATGPT),
        ]
    )
    assert prepare_run(
        workfolder, run_input, resolved, settings, task_base=FIXTURES, profiles=good, now=NOW
    ).run_dir.is_dir()


def test_invalid_copy_exclude_in_project_yaml(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    (workfolder / "projects" / "collatz" / "project.yaml").write_text(
        "copy_exclude: ['!behalten.md']\n", encoding="utf-8"
    )
    run_input, resolved = make_run(workfolder, settings)
    with pytest.raises(ConfigError) as caught:
        prepare_run(workfolder, run_input, resolved, settings, task_base=FIXTURES)
    assert caught.value.path == "copy_exclude[0]"
    assert runs_are_empty(workfolder)


def test_fixed_run_id(workfolder: Path, settings: WorkfolderSettings) -> None:
    fixed = "20261025-1200_collatz_exploration_abcd"
    result = prepare(workfolder, settings, run_id=fixed)
    assert result.run_id == fixed
    assert result.run_dir == workfolder / "runs" / fixed
    assert load_run_meta(result.run_dir).run_id == fixed

    with pytest.raises(ArrError, match="existiert schon"):
        prepare(workfolder, settings, run_id=fixed)
    assert (result.run_dir / "meta.json").is_file(), "an existing run is never touched"
    with pytest.raises(ArrError, match="hat nicht die Form"):
        prepare(workfolder, settings, run_id="../evil")


def test_two_runs_in_the_same_minute_get_different_folders(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    first = prepare(workfolder, settings)
    second = prepare(workfolder, settings)
    assert first.run_id != second.run_id
    assert first.run_dir.is_dir() and second.run_dir.is_dir()


def test_project_settings_default_when_no_project_yaml(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    project = workfolder / "projects" / "collatz"
    (project / "project.yaml").unlink()
    assert load_project_settings(project) == ProjectSettings()
    result = prepare(workfolder, settings)
    assert (result.run_dir / "base" / "literatur.pdf").is_file(), "nothing excluded any more"
    assert (result.run_dir / "base" / "data" / "big" / "daten.bin").is_file()


# --------------------------------------------------------------------------------------------
# W1-02.1: task_file stays inside task_base (M2)
# --------------------------------------------------------------------------------------------


def with_task_file(task_file: str) -> Callable[[dict[str, Any]], None]:
    def mutate(data: dict[str, Any]) -> None:
        data["task_file"] = task_file

    return mutate


@pytest.fixture
def task_base(tmp_path: Path) -> Path:
    base = tmp_path / "konfig"
    base.mkdir()
    (base / "task.md").write_text("Aufgabe aus dem Konfig-Ordner", encoding="utf-8")
    (tmp_path / "geheim.txt").write_text("GEHEIM", encoding="utf-8")
    return base


def refused_task(
    workfolder: Path, settings: WorkfolderSettings, task_file: str, base: Path
) -> None:
    with pytest.raises(ConfigError) as caught:
        prepare(workfolder, settings, with_task_file(task_file), task_base=base)
    assert caught.value.path == "task_file"
    assert "außerhalb" in caught.value.issue_message
    assert "GEHEIM" not in str(caught.value)
    assert runs_are_empty(workfolder)


def test_task_file_absolute_outside_is_refused(
    workfolder: Path, settings: WorkfolderSettings, task_base: Path, tmp_path: Path
) -> None:
    refused_task(workfolder, settings, str(tmp_path / "geheim.txt"), task_base)


def test_task_file_with_tilde_is_refused(
    workfolder: Path,
    settings: WorkfolderSettings,
    task_base: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    refused_task(workfolder, settings, "~/geheim.txt", task_base)


def test_task_file_with_dotdot_is_refused(
    workfolder: Path, settings: WorkfolderSettings, task_base: Path
) -> None:
    refused_task(workfolder, settings, "../geheim.txt", task_base)


def test_task_file_symlink_out_of_task_base_is_refused(
    workfolder: Path, settings: WorkfolderSettings, task_base: Path, tmp_path: Path
) -> None:
    (task_base / "link.md").symlink_to(tmp_path / "geheim.txt")
    refused_task(workfolder, settings, "link.md", task_base)


def test_task_file_inside_task_base_is_accepted(
    workfolder: Path, settings: WorkfolderSettings, task_base: Path
) -> None:
    for task_file in ("task.md", str(task_base / "task.md"), "./sub/../task.md"):
        result = prepare(workfolder, settings, with_task_file(task_file), task_base=task_base)
        assert (result.run_dir / "task.md").read_text("utf-8") == "Aufgabe aus dem Konfig-Ordner"


def test_task_file_outside_only_with_explicit_switch(
    workfolder: Path, settings: WorkfolderSettings, task_base: Path, tmp_path: Path
) -> None:
    outside = str(tmp_path / "geheim.txt")
    with pytest.raises(ConfigError, match="außerhalb"):
        prepare(workfolder, settings, with_task_file(outside), task_base=None)
    result = prepare(
        workfolder,
        settings,
        with_task_file(outside),
        task_base=None,
        allow_external_task_file=True,
    )
    assert (result.run_dir / "task.md").read_text("utf-8") == "GEHEIM"


# --------------------------------------------------------------------------------------------
# W1-02.1: context source (G1, G2, G5) and files kept away from agents (G6)
# --------------------------------------------------------------------------------------------


def test_empty_claude_md_gives_the_same_text_everywhere(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    (workfolder / "projects" / "collatz" / "CLAUDE.md").write_text("", encoding="utf-8")
    result = prepare(workfolder, settings)
    for agent_id in ("explorer-1", "explorer-gpt"):
        agent = result.agents[agent_id]
        context = (agent.agent_dir / "context.md").read_text("utf-8")
        assert (agent.work_dir / "AGENTS.md").read_text("utf-8") == context
        assert (agent.work_dir / "CLAUDE.md").read_text("utf-8") == context
        assert context.startswith("<!-- arr:research-agent -->")
        assert "Hinweise für Agenten" not in context


def test_context_comes_from_the_project_even_if_excluded(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    (workfolder / "projects" / "collatz" / "project.yaml").write_text(
        'copy_exclude: ["*.md"]\n', encoding="utf-8"
    )
    result = prepare(workfolder, settings)
    assert not (result.run_dir / "base" / "CLAUDE.md").exists()
    agent = result.agents["explorer-1"]
    context = (agent.agent_dir / "context.md").read_text("utf-8")
    assert "Stoppzeit: σ(n)." in context
    assert (agent.work_dir / "AGENTS.md").read_text("utf-8") == context


def test_broken_mark_in_claude_md_is_reported_before_the_run(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    (workfolder / "projects" / "collatz" / "CLAUDE.md").write_text(
        "# X\n<!-- arr:user-agent -->\nohne Ende\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as caught:
        prepare(workfolder, settings)
    assert caught.value.path == "project"
    assert "CLAUDE.md" in caught.value.issue_message
    assert "<!-- /arr:user-agent -->" in caught.value.issue_message
    assert runs_are_empty(workfolder)


def test_claude_md_linked_out_of_the_project_is_not_read(
    workfolder: Path, settings: WorkfolderSettings, tmp_path: Path
) -> None:
    outside = tmp_path / "fremd.md"
    outside.write_text("# Fremder Kontext\n", encoding="utf-8")
    project = workfolder / "projects" / "collatz"
    (project / "CLAUDE.md").unlink()
    (project / "CLAUDE.md").symlink_to(outside)
    result = prepare(workfolder, settings)
    context = (result.agents["explorer-1"].agent_dir / "context.md").read_text("utf-8")
    assert "Fremder Kontext" not in context
    assert context.startswith("# Hinweise für Agenten"), "falls back to the project's AGENTS.md"


def test_override_and_mcp_json_never_reach_research_agents(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    project = workfolder / "projects" / "collatz"
    (project / "AGENTS.override.md").write_text("Überschreibt alles\n", encoding="utf-8")
    (project / ".mcp.json").write_text('{"mcpServers": {"arr": {}}}\n', encoding="utf-8")
    assert "/AGENTS.override.md" in ALWAYS_EXCLUDED
    assert "/.mcp.json" in ALWAYS_EXCLUDED
    result = prepare(workfolder, settings)
    for folder in (
        result.run_dir / "base",
        result.agents["explorer-1"].work_dir,
        result.agents["explorer-gpt"].work_dir,
    ):
        assert not (folder / "AGENTS.override.md").exists()
        assert not (folder / ".mcp.json").exists()
    assert (project / "AGENTS.override.md").is_file(), "the project keeps its files"


# --------------------------------------------------------------------------------------------
# W1-02.1: skills and roles do not follow links out of their folder (G3)
# --------------------------------------------------------------------------------------------


def test_skill_links_out_of_the_skill_are_left_out(
    workfolder: Path, settings: WorkfolderSettings, tmp_path: Path
) -> None:
    outside = tmp_path / "privat"
    outside.mkdir()
    (outside / "notizen.txt").write_text("privat", encoding="utf-8")
    skill = workfolder / "skills" / "sympy-check"
    (skill / "privat").symlink_to(outside)
    (skill / "scripts" / "hilfe.md").symlink_to("../SKILL.md")
    (skill / "zurueck").symlink_to("..")  # loop out of the skill

    def codex_skills(data: dict[str, Any]) -> None:
        data["agents"][1]["skills"] = ["sympy-check"]

    result = prepare(workfolder, settings, codex_skills)
    for copy in (
        result.run_dir / "skills" / "sympy-check",
        result.agents["explorer-1"].work_dir.parent / "plugin" / "skills" / "sympy-check",
        result.agents["explorer-gpt"].work_dir / ".agents" / "skills" / "sympy-check",
    ):
        assert not os.path.lexists(copy / "privat"), copy
        assert not os.path.lexists(copy / "zurueck"), copy
        assert os.readlink(copy / "scripts" / "hilfe.md") == "../SKILL.md"
        assert (copy / "scripts" / "check.py").is_file()
    assert "privat" not in "".join(
        p.read_text("utf-8", errors="ignore") for p in result.run_dir.rglob("*.txt")
    )
    skill_warnings = sorted(w for w in result.warnings if "im Skill" in w)
    assert len(skill_warnings) == 2
    assert "„sympy-check/privat“" in skill_warnings[0]
    assert "„sympy-check/zurueck“" in skill_warnings[1]


def test_role_file_linked_out_of_the_skill_is_refused(
    workfolder: Path, settings: WorkfolderSettings, tmp_path: Path
) -> None:
    outside = tmp_path / "hosts"
    outside.write_text("geheim", encoding="utf-8")
    (workfolder / "skills" / "exploration" / "roles" / "evil.md").symlink_to(outside)

    def evil(data: dict[str, Any]) -> None:
        data["agents"][2]["role"] = "roles/evil.md"

    with pytest.raises(ConfigError) as caught:
        prepare(workfolder, settings, evil)
    assert [i.path for i in caught.value.issues] == ["agents[3].role"]
    assert runs_are_empty(workfolder)


def test_role_file_linked_inside_the_skill_is_fine(
    workfolder: Path, settings: WorkfolderSettings
) -> None:
    (workfolder / "skills" / "exploration" / "roles" / "alias.md").symlink_to("ranker.md")

    def alias(data: dict[str, Any]) -> None:
        data["agents"][2]["role"] = "roles/alias.md"

    result = prepare(workfolder, settings, alias)
    assert "Ranker" in (result.agents["ranker"].agent_dir / "role.md").read_text("utf-8")
    assert not (result.agents["ranker"].agent_dir / "role.md").is_symlink()
