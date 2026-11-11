import json
import os
import shutil
from importlib import resources
from pathlib import Path

import pytest
from run_kit import empty_skills
from typer.testing import CliRunner

from arr.cli import app
from arr.errors import ArrError, ConfigError
from arr.models import BUILTIN_LIMITS, ProjectSettings, WorkfolderSettings
from arr.workfolder import (
    block_span,
    create_project,
    init_workfolder,
    is_workfolder,
    load_project_settings,
    load_workfolder_settings,
    project_dir,
    read_template,
    replace_block,
    setup_project,
)

FIXTURES = Path(__file__).parent / "fixtures" / "workfolder"
REPO_ROOT = Path(__file__).parent.parent
runner = CliRunner()


@pytest.fixture
def workfolder(tmp_path: Path) -> Path:
    """An initialised workfolder with the fixture skills instead of the start skills."""
    root = empty_skills(init_workfolder(tmp_path / "wf"))
    shutil.copytree(FIXTURES / "skills", root / "skills", dirs_exist_ok=True)
    return root


# --------------------------------------------------------------------------------------------
# A1: arr init
# --------------------------------------------------------------------------------------------


def test_init_creates_the_layout(tmp_path: Path) -> None:
    root = init_workfolder(tmp_path / "neu" / "wf")
    assert root == (tmp_path / "neu" / "wf").absolute()
    for name in ("projects", "skills", "runs", ".arr", ".arr/logs"):
        assert (root / name).is_dir(), name
    assert (root / "arr.yaml").is_file()
    assert "arr-Workfolder" in (root / "README.md").read_text(encoding="utf-8")
    assert is_workfolder(root)


def test_init_is_idempotent_and_keeps_existing_files(tmp_path: Path) -> None:
    root = init_workfolder(tmp_path)
    (root / "arr.yaml").write_text("version: 1\nlimits: {wall_time: 3h}\n", encoding="utf-8")
    (root / "README.md").write_text("Mein README\n", encoding="utf-8")
    (root / "projects" / "p").mkdir()
    (root / "projects" / "p" / "notiz.md").write_text("x", encoding="utf-8")
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))

    assert init_workfolder(tmp_path) == root

    assert (root / "arr.yaml").read_text(encoding="utf-8").startswith("version: 1\nlimits:")
    assert (root / "README.md").read_text(encoding="utf-8") == "Mein README\n"
    assert before == sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_init_rejects_a_file(tmp_path: Path) -> None:
    file = tmp_path / "datei"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(ArrError, match="kein Ordner"):
        init_workfolder(file)


def test_default_arr_yaml_is_valid_and_matches_the_builtin_standards(tmp_path: Path) -> None:
    root = init_workfolder(tmp_path)
    settings = load_workfolder_settings(root)
    assert settings == WorkfolderSettings(limits=BUILTIN_LIMITS)
    text = (root / "arr.yaml").read_text(encoding="utf-8")
    assert "# " in text, "the standards come with comments"


def test_load_settings_of_a_plain_folder_explains_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(ArrError, match="arr init"):
        load_workfolder_settings(tmp_path)


def test_load_settings_reports_errors_with_path(tmp_path: Path) -> None:
    root = init_workfolder(tmp_path)
    (root / "arr.yaml").write_text("version: 1\nlimitz: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="limits"):
        load_workfolder_settings(root)


def test_cli_init_creates_and_repeats(tmp_path: Path) -> None:
    target = tmp_path / "cli-wf"
    first = runner.invoke(app, ["init", str(target)])
    assert first.exit_code == 0, first.output
    assert "Workfolder bereit" in first.output
    assert str(target) in first.output.replace("\n", "")
    assert (target / "arr.yaml").is_file()

    (target / "README.md").write_text("bleibt\n", encoding="utf-8")
    second = runner.invoke(app, ["init", str(target)])
    assert second.exit_code == 0, second.output
    assert (target / "README.md").read_text(encoding="utf-8") == "bleibt\n"


def test_cli_init_on_a_file_fails_without_traceback(tmp_path: Path) -> None:
    file = tmp_path / "datei"
    file.write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["init", str(file)])
    assert result.exit_code == 1
    assert "kein Ordner" in result.output
    assert "Traceback" not in result.output


# --------------------------------------------------------------------------------------------
# A3: templates are package data
# --------------------------------------------------------------------------------------------

TEMPLATE_FILES = [
    ("workfolder", "arr.yaml"),
    ("workfolder", "README.md"),
    ("project", "CLAUDE.md"),
    ("project", ".mcp.json"),
    ("project", "project.yaml"),
    ("project", "AGENTS.md"),
    ("run", "research-agent-block.md"),
]


def test_templates_come_from_the_package_not_from_the_repo() -> None:
    assert not (REPO_ROOT / "templates").exists(), "templates/ moved into the package"
    root = resources.files("arr").joinpath("templates")
    for parts in TEMPLATE_FILES:
        node = root.joinpath(*parts)
        assert node.is_file(), parts
        assert node.read_text(encoding="utf-8").strip(), f"{parts} has content"
        assert read_template(*parts) == node.read_text(encoding="utf-8")


def test_init_and_projects_work_from_another_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    root = init_workfolder(Path("wf"))
    project = create_project(root, "demo")
    assert (project / "CLAUDE.md").is_file()
    assert not (elsewhere / "templates").exists()


def test_project_template_text_is_valid() -> None:
    claude = read_template("project", "CLAUDE.md")
    span = block_span(claude, "user-agent")
    assert span is not None
    assert "start_run" in claude[span[0] : span[1]]
    assert json.loads(read_template("project", ".mcp.json")) == {
        "mcpServers": {"arr": {"command": "arr", "args": ["mcp"]}}
    }


# --------------------------------------------------------------------------------------------
# A2: projects
# --------------------------------------------------------------------------------------------


def test_create_project_from_template(workfolder: Path) -> None:
    project = create_project(workfolder, "collatz")
    assert project == workfolder / "projects" / "collatz"

    claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude.startswith("# Projekt: collatz")
    assert "<!-- arr:user-agent -->" in claude
    assert "<!-- /arr:user-agent -->" in claude

    assert json.loads((project / ".mcp.json").read_text(encoding="utf-8")) == {
        "mcpServers": {"arr": {"command": "arr", "args": ["mcp"]}}
    }
    assert load_project_settings(project) == ProjectSettings()
    assert (project / "project.yaml").is_file()

    agents = (project / "AGENTS.md").read_text(encoding="utf-8")
    assert "CLAUDE.md" in agents
    assert not (project / "AGENTS.md").is_symlink()


def test_create_project_links_the_user_skills(workfolder: Path) -> None:
    project = create_project(workfolder, "collatz")
    links = project / ".claude" / "skills"
    assert sorted(p.name for p in links.iterdir()) == ["arr", "exploration", "sympy-check"]
    for link in links.iterdir():
        assert link.is_symlink()
        assert not os.path.isabs(os.readlink(link)), "links are relative"
        assert link.resolve() == (workfolder / "skills" / link.name).resolve()
        assert (link / "SKILL.md").is_file()
    assert not (links / "lean-notes").exists(), "research-only skills are not for the user agent"


def test_create_project_rejects_bad_or_existing_names(workfolder: Path) -> None:
    for bad in ("", "Große", "a/b", "..", ".versteckt", "mit leerzeichen"):
        with pytest.raises(ArrError, match="Ungültiger Projektname"):
            create_project(workfolder, bad)
    create_project(workfolder, "p")
    (workfolder / "projects" / "p" / "eigene.md").write_text("meins", encoding="utf-8")
    with pytest.raises(ArrError, match="gibt es schon"):
        create_project(workfolder, "p")
    assert (workfolder / "projects" / "p" / "eigene.md").read_text(encoding="utf-8") == "meins"


def test_create_project_needs_a_workfolder(tmp_path: Path) -> None:
    with pytest.raises(ArrError, match="arr init"):
        create_project(tmp_path, "p")


def test_setup_project_keeps_existing_content(workfolder: Path) -> None:
    project = workfolder / "projects" / "alt"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# Mein Projekt\n\nViel Kontext.\n", encoding="utf-8")
    (project / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"andere": {"command": "x"}}, "extra": 1}), encoding="utf-8"
    )
    (project / "AGENTS.md").write_text("Eigene Hinweise\n", encoding="utf-8")
    (project / "project.yaml").write_text("skills: [sympy-check]\n", encoding="utf-8")

    result = setup_project(project)

    assert result.created == []
    assert result.updated == ["CLAUDE.md", ".mcp.json"]
    assert sorted(result.skipped) == ["AGENTS.md", "project.yaml"]
    claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
    assert claude.startswith("# Mein Projekt\n\nViel Kontext.\n")
    assert block_span(claude, "user-agent") is not None
    mcp = json.loads((project / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["andere"] == {"command": "x"}
    assert mcp["mcpServers"]["arr"]["args"] == ["mcp"]
    assert mcp["extra"] == 1
    assert (project / "AGENTS.md").read_text(encoding="utf-8") == "Eigene Hinweise\n"
    assert result.skill_links == ["sympy-check"], "project.yaml restricts the skills"


def test_setup_project_is_idempotent(workfolder: Path) -> None:
    project = create_project(workfolder, "collatz")
    snapshot = {p: p.read_text(encoding="utf-8") for p in project.rglob("*") if p.is_file()}
    result = setup_project(project)
    assert result.created == result.updated == result.skill_links == []
    assert {p: p.read_text(encoding="utf-8") for p in project.rglob("*") if p.is_file()} == snapshot


def test_setup_project_leaves_a_present_arr_block_alone(workfolder: Path) -> None:
    project = workfolder / "projects" / "mitblock"
    project.mkdir()
    own = "# X\n\n<!-- arr:user-agent -->\nMein eigener Text\n<!-- /arr:user-agent -->\n"
    (project / "CLAUDE.md").write_text(own, encoding="utf-8")
    result = setup_project(project)
    assert (project / "CLAUDE.md").read_text(encoding="utf-8") == own
    assert "CLAUDE.md" in result.skipped


def test_setup_project_refuses_a_broken_block_and_json(workfolder: Path) -> None:
    project = workfolder / "projects" / "kaputt"
    project.mkdir()
    (project / "CLAUDE.md").write_text("# X\n<!-- arr:user-agent -->\nohne Ende\n", "utf-8")
    with pytest.raises(ArrError, match="fehlt „<!-- /arr:user-agent -->“"):
        setup_project(project)
    assert not (project / ".mcp.json").exists(), "nothing is changed before the checks pass"

    (project / "CLAUDE.md").unlink()
    (project / ".mcp.json").write_text("{kein json", encoding="utf-8")
    with pytest.raises(ArrError, match="kein gültiges JSON"):
        setup_project(project)


def test_setup_project_never_writes_through_an_existing_link(
    workfolder: Path, tmp_path: Path
) -> None:
    project = workfolder / "projects" / "mit-link"
    project.mkdir()
    outside = tmp_path / "ausserhalb.md"
    outside.write_text("Original\n", encoding="utf-8")
    (project / "AGENTS.md").symlink_to(outside)
    result = setup_project(project)
    assert "AGENTS.md" in result.skipped
    assert outside.read_text(encoding="utf-8") == "Original\n"


def test_skill_links_follow_the_workfolder(workfolder: Path) -> None:
    project = create_project(workfolder, "p")
    link = project / ".claude" / "skills" / "sympy-check"
    # a real folder in the project is never replaced
    (project / ".claude" / "skills" / "arr").unlink()
    (project / ".claude" / "skills" / "arr").mkdir()
    (project / ".claude" / "skills" / "arr" / "eigen.md").write_text("x", encoding="utf-8")
    # a removed workfolder skill takes its link away, a new one gets a link
    shutil.rmtree(workfolder / "skills" / "sympy-check")
    shutil.copytree(workfolder / "skills" / "exploration", workfolder / "skills" / "neu-skill")
    (workfolder / "skills" / "neu-skill" / "group.yaml").unlink()
    (workfolder / "skills" / "neu-skill" / "SKILL.md").write_text(
        "---\nname: neu-skill\ndescription: Neu.\n---\n", encoding="utf-8"
    )

    result = setup_project(project)

    assert not link.is_symlink()
    assert (project / ".claude" / "skills" / "neu-skill").is_symlink()
    assert (project / ".claude" / "skills" / "arr" / "eigen.md").is_file()
    assert any("arr ist bereits belegt" in w for w in result.warnings)
    assert sorted(result.skill_links) == ["neu-skill", "sympy-check"]


def test_setup_project_needs_the_workfolder_around_it(tmp_path: Path, workfolder: Path) -> None:
    loose = tmp_path / "lose"
    loose.mkdir()
    with pytest.raises(ArrError, match="liegt in keinem arr-Workfolder"):
        setup_project(loose)
    result = setup_project(loose, workfolder=workfolder)
    assert (loose / "CLAUDE.md").is_file()
    assert (loose / ".claude" / "skills" / "arr").is_symlink()
    assert result.path == loose


def test_setup_project_reports_unknown_skills_in_project_yaml(workfolder: Path) -> None:
    project = workfolder / "projects" / "p"
    project.mkdir()
    (project / "project.yaml").write_text("skills: [gibts-nicht]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="gibts-nicht") as caught:
        setup_project(project)
    assert caught.value.path == "skills[0]"
    assert not (project / "CLAUDE.md").exists()


def test_project_dir_lookup(workfolder: Path) -> None:
    create_project(workfolder, "p")
    assert project_dir(workfolder, "p") == workfolder / "projects" / "p"
    with pytest.raises(ArrError, match="gibt es nicht"):
        project_dir(workfolder, "q")
    for bad in ("../x", "a/b", ".x", "", " p"):
        with pytest.raises(ArrError, match="Ungültiger Projektname"):
            project_dir(workfolder, bad)


# --------------------------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------------------------


def test_replace_block_swaps_appends_and_dedupes() -> None:
    new = "<!-- arr:research-agent -->\nNeu\n<!-- /arr:research-agent -->"
    text = "A\n\n<!-- arr:user-agent -->\nalt\n<!-- /arr:user-agent -->\n\nB\n"
    assert replace_block(text, "user-agent", new) == f"A\n\n{new}\n\nB\n"
    assert replace_block("A\n", "user-agent", new) == f"A\n\n{new}\n"
    assert replace_block("", "user-agent", new) == new + "\n"
    twice = text + "<!-- arr:user-agent -->\nzweiter\n<!-- /arr:user-agent -->\nC\n"
    result = replace_block(twice, "user-agent", new)
    assert result.count("<!-- arr:research-agent -->") == 1
    assert "zweiter" not in result
    assert result.endswith("C\n")


def test_block_span_rejects_a_lone_mark() -> None:
    with pytest.raises(ArrError, match="ohne öffnendes"):
        block_span("x <!-- /arr:user-agent -->", "user-agent")


# --------------------------------------------------------------------------------------------
# W1-02.1
# --------------------------------------------------------------------------------------------


def test_setup_project_linked_from_outside_the_workfolder(workfolder: Path, tmp_path: Path) -> None:
    real = tmp_path / "woanders" / "collatz"
    real.mkdir(parents=True)
    linked = workfolder / "projects" / "collatz"
    linked.symlink_to(real)

    setup_project(linked)

    for name in ("arr", "exploration", "sympy-check"):
        assert (linked / ".claude" / "skills" / name / "SKILL.md").is_file(), name
    # a broken link from the old computation is repaired by the next setup
    broken = real / ".claude" / "skills" / "arr"
    broken.unlink()
    broken.symlink_to("../../../../skills/arr")
    assert not broken.exists()
    result = setup_project(linked)
    assert result.skill_links == ["arr"]
    assert (broken / "SKILL.md").is_file()


def test_init_without_write_permission_is_a_clear_error(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root may write anywhere")
    locked = tmp_path / "gesperrt"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(ArrError, match="Kein Schreibrecht"):
            init_workfolder(locked / "wf")
        result = runner.invoke(app, ["init", str(locked / "wf")])
        assert result.exit_code == 1
        assert "Kein Schreibrecht" in result.output
        assert "Traceback" not in result.output
    finally:
        locked.chmod(0o700)
