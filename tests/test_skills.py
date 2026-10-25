import shutil
from pathlib import Path

import pytest

from arr.errors import ConfigError
from arr.models import GroupSpec, ProjectSettings
from arr.resolve import load_group_spec
from arr.skills import (
    SkillAudience,
    check_research_skills,
    discover_skills,
    load_skill,
    skills_for_research_agent,
    skills_for_user_agent,
)

FIXTURE_SKILLS = Path(__file__).parent / "fixtures" / "workfolder" / "skills"


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    target = tmp_path / "skills"
    shutil.copytree(FIXTURE_SKILLS, target)
    return target


def write_skill(folder: Path, frontmatter: str, body: str = "# Skill\n") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return folder


def test_discovers_fixture_skills(skills_dir: Path) -> None:
    catalog = discover_skills(skills_dir)
    assert sorted(catalog.skills) == ["arr", "exploration", "lean-notes", "sympy-check"]
    assert catalog.problems == {}
    assert (
        catalog.skills["sympy-check"].description
        == "Rechnungen und Identitäten mit SymPy nachprüfen."
    )


def test_group_skill_is_recognised_by_group_yaml(skills_dir: Path) -> None:
    catalog = discover_skills(skills_dir)
    group = catalog.skills["exploration"]
    assert group.is_group
    assert group.group_file == skills_dir / "exploration" / "group.yaml"
    assert not catalog.skills["sympy-check"].is_group
    spec = load_group_spec(group.group_file)
    assert isinstance(spec, GroupSpec)


def test_default_audience(skills_dir: Path) -> None:
    skills = discover_skills(skills_dir).skills
    assert skills["exploration"].audience is SkillAudience.USER  # group skill
    assert skills["arr"].audience is SkillAudience.USER  # name arr
    assert skills["sympy-check"].audience is SkillAudience.BOTH  # explicit
    assert skills["lean-notes"].audience is SkillAudience.RESEARCH
    assert skills["exploration"].explicit_audience is None
    assert skills["sympy-check"].explicit_audience is SkillAudience.BOTH


def test_plain_skill_without_metadata_is_for_both(tmp_path: Path) -> None:
    folder = write_skill(tmp_path / "plain", "name: plain\ndescription: Nur ein Test.")
    skill = load_skill(folder)
    assert skill.audience is SkillAudience.BOTH
    assert skill.for_user_agent
    assert skill.for_research_agent


def test_other_frontmatter_keys_are_ignored(tmp_path: Path) -> None:
    folder = write_skill(
        tmp_path / "extra",
        "name: extra\ndescription: Mit Zusatzfeldern.\nlicense: MIT\nallowed-tools: Read",
    )
    assert load_skill(folder).name == "extra"


@pytest.mark.parametrize(
    ("frontmatter", "path", "text"),
    [
        ("description: Ohne Namen.", "name", "name"),
        ("name: x", "description", "description"),
        ("name: x\ndescription: ''", "description", "description"),
        ("name: Falsch_Groß\ndescription: d", "name", "Kleinbuchstaben"),
        ("name: anders\ndescription: d", "name", "passt nicht zum Ordner"),
        (
            "name: x\ndescription: d\nmetadata:\n  arr-audience: alle",
            "metadata.arr-audience",
            "alle",
        ),
    ],
)
def test_invalid_frontmatter_names_the_field(
    tmp_path: Path, frontmatter: str, path: str, text: str
) -> None:
    folder = write_skill(tmp_path / "x", frontmatter)
    with pytest.raises(ConfigError) as caught:
        load_skill(folder)
    assert caught.value.path == path
    assert text in str(caught.value)
    assert str(folder / "SKILL.md") in str(caught.value)


def test_missing_or_broken_skill_file(tmp_path: Path) -> None:
    empty = tmp_path / "leer"
    empty.mkdir()
    with pytest.raises(ConfigError, match="fehlt die Datei SKILL.md"):
        load_skill(empty)

    no_front = tmp_path / "nofront"
    no_front.mkdir()
    (no_front / "SKILL.md").write_text("# Ohne Kopf\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Kopfbereich"):
        load_skill(no_front)

    bad_yaml = write_skill(tmp_path / "badyaml", "name: badyaml\ndescription: a: b: c")
    with pytest.raises(ConfigError, match="kein gültiges YAML") as caught:
        load_skill(bad_yaml)
    assert caught.value.issues[0].line == 3


def test_group_skill_cannot_be_offered_to_research_agents(tmp_path: Path) -> None:
    folder = write_skill(
        tmp_path / "grp", "name: grp\ndescription: d\nmetadata:\n  arr-audience: both"
    )
    (folder / "group.yaml").write_text("kind: research-group\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="nur an den User-Agent"):
        load_skill(folder)


def test_broken_skill_does_not_block_the_rest(skills_dir: Path) -> None:
    (skills_dir / "kaputt").mkdir()
    (skills_dir / ".versteckt").mkdir()
    (skills_dir / "_archiv").mkdir()
    (skills_dir / "notiz.txt").write_text("keine Skill", encoding="utf-8")
    catalog = discover_skills(skills_dir)
    assert "sympy-check" in catalog
    assert list(catalog.problems) == ["kaputt"]
    with pytest.raises(ConfigError, match="fehlt die Datei SKILL.md"):
        catalog.get("kaputt")


def test_unknown_skill_gets_a_suggestion(skills_dir: Path) -> None:
    catalog = discover_skills(skills_dir)
    with pytest.raises(ConfigError, match="Meintest du „sympy-check“") as caught:
        catalog.get("sympy-chek", path="agents[0].skills[0]")
    assert caught.value.path == "agents[0].skills[0]"


def test_missing_skills_dir_gives_empty_catalog(tmp_path: Path) -> None:
    catalog = discover_skills(tmp_path / "nichts")
    assert catalog.skills == {}


def test_user_agent_gets_all_but_research_only(skills_dir: Path) -> None:
    catalog = discover_skills(skills_dir)
    names = [s.name for s in skills_for_user_agent(catalog, ProjectSettings())]
    assert names == ["arr", "exploration", "sympy-check"]


def test_project_yaml_restricts_user_skills(skills_dir: Path) -> None:
    catalog = discover_skills(skills_dir)
    chosen = skills_for_user_agent(catalog, ProjectSettings(skills=["sympy-check", "arr"]))
    assert [s.name for s in chosen] == ["arr", "sympy-check"]
    with pytest.raises(ConfigError) as caught:
        skills_for_user_agent(catalog, ProjectSettings(skills=["arr", "nope", "lean-notes"]))
    assert [i.path for i in caught.value.issues] == ["skills[1]", "skills[2]"]
    assert "nur für Research-Agenten" in caught.value.issues[1].message


def test_research_agent_gets_only_listed_skills(skills_dir: Path) -> None:
    catalog = discover_skills(skills_dir)
    skills = skills_for_research_agent(catalog, ["lean-notes", "sympy-check", "lean-notes"])
    assert [s.name for s in skills] == ["lean-notes", "sympy-check"]
    assert skills_for_research_agent(catalog, []) == []


def test_research_agent_never_gets_group_skills_or_arr(skills_dir: Path) -> None:
    catalog = discover_skills(skills_dir)
    found, issues = check_research_skills(
        catalog, ["sympy-check", "exploration", "arr", "fehlt"], "agents[3].skills"
    )
    assert [s.name for s in found] == ["sympy-check"]
    assert [i.path for i in issues] == [
        "agents[3].skills[1]",
        "agents[3].skills[2]",
        "agents[3].skills[3]",
    ]
    assert "Group-Skill" in issues[0].message
    assert "E11" in issues[0].message
    assert "User-Agent" in issues[1].message
    with pytest.raises(ConfigError):
        skills_for_research_agent(catalog, ["exploration"])
