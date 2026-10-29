"""Translation SandboxProfile → Codex launch settings (no SDK, no model)."""

import re
import tomllib
from pathlib import Path

import pytest

from arr.backends.codex_config import (
    PERMISSION_PROFILE,
    ROOM_SERVER,
    SECRET_ENV,
    build_launch,
    filesystem_entries,
    glob_escape,
    isolation_overrides,
    toml_value,
)
from arr.models import AgentConfig, Provider, RunVenvMode, Toolchain
from arr.permissions.policy import PermissionRules, RunPaths, profile_for
from arr.runs.prepare import AgentFile, PreparedAgent


def agent_config(**changes: object) -> AgentConfig:
    data: dict[str, object] = {
        "id": "gpt-1",
        "base_id": "gpt-1",
        "provider": Provider.CODEX,
        "model": "gpt-6-luna",
        "auth": "chatgpt-test",
        "channels": ["main"],
    }
    data.update(changes)
    return AgentConfig.model_validate(data)


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    wf = home / "wf"
    run = wf / "runs" / "R"
    work = run / "agents" / "gpt-1" / "work"
    att = run / "groupchats" / "attachments"
    for folder in (work, run / "tmp", home / "readdir", home / ".ssh", home / "Documents"):
        folder.mkdir(parents=True)
    for folder in (att / "main", att / "secret", run / "agents" / "critic-1" / "work"):
        folder.mkdir(parents=True)
    for folder in (run / "skills" / "demo", run / "agents" / "gpt-1" / "blobs"):
        folder.mkdir(parents=True)
    for folder in (wf / "projects" / "other", wf / ".arr", run / "output", run / "base"):
        folder.mkdir(parents=True)
    files = {
        "notes.txt": "x",
        ".zsh_history": "x",
        "readdir/paper.md": "x",
        "readdir/vault.kdbx": "x",
        ".ssh/known_hosts": "x",
        "wf/arr.yaml": "x",
        "wf/projects/other/canary.txt": "x",
        "wf/runs/R/meta.json": "x",
        "wf/runs/R/task.md": "x",
        "wf/runs/R/groupchats/room.sqlite": "x",
        "wf/runs/R/groupchats/attachments/main/a.txt": "x",
        "wf/runs/R/groupchats/attachments/secret/b.txt": "x",
        "wf/runs/R/agents/gpt-1/agent.json": "x",
        "wf/runs/R/agents/critic-1/transcript.jsonl": "x",
        "wf/runs/R/skills/demo/SKILL.md": "x",
        "wf/runs/R/agents/gpt-1/work/w.txt": "x",
    }
    for name, text in files.items():
        (home / name).write_text(text)
    (run / "agents" / "gpt-1" / "role.md").write_text("Du bist gpt-1.\n")
    return {"home": home, "wf": wf, "run": run, "work": work}


def setup(layout: dict[str, Path], **changes: object):
    changes.setdefault("read_dirs", [str(layout["home"] / "readdir")])
    config = agent_config(**changes)
    paths = RunPaths(layout["wf"], layout["run"], Toolchain())
    profile = profile_for(
        config, paths, PermissionRules(), home=layout["home"], case_insensitive=False
    )
    agent = PreparedAgent(
        AgentFile(
            config=config,
            work_dir="agents/gpt-1/work",
            role_file="agents/gpt-1/role.md",
            context_file="agents/gpt-1/context.md",
            path_prepend=["/opt/base/bin"],
        ),
        layout["run"],
    )
    return agent, profile, paths


# -- a small model of how Codex applies the entries (checked offline with `codex sandbox`,
#    spikes/w2_02_codex/lab_variant1.py): a glob `deny` beats everything, else the most
#    specific path entry wins, `*` and `?` do not cross `/`, `[x]` is a character class.


def _glob_regex(glob: str) -> re.Pattern[str]:
    out, i = "", 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif c == "*":
            out, i = out + "[^/]*", i + 1
        elif c == "?":
            out, i = out + "[^/]", i + 1
        elif c == "[":
            end = glob.index("]", i + 2)
            out, i = out + "[" + re.escape(glob[i + 1 : end]).replace("!", "^", 1) + "]", end + 1
        else:
            out, i = out + re.escape(c), i + 1
    return re.compile(out + "$")


def codex_access(entries: dict[str, str], path: Path) -> str:
    text = str(path)
    for key, access in entries.items():
        if any(ch in key for ch in "*?[") and access == "deny" and _glob_regex(key).match(text):
            return "deny"
    best, access = "", entries[":root"]
    for key, value in entries.items():
        if any(ch in key for ch in "*?[") or key == ":root":
            continue
        if (text == key or text.startswith(key.rstrip("/") + "/")) and len(key) > len(best):
            best, access = key, value
    return access


def tree(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*"))


def test_every_existing_entry_follows_profile(layout: dict[str, Path]) -> None:
    """The sibling list covers everything that exists at start (variant 1)."""
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    run, home = layout["run"], layout["home"]
    allowed = [layout["work"], run / "tmp", run / "output", home / "readdir"]
    allowed.append(run / "groupchats" / "attachments" / "main")
    passage = {p for a in allowed for p in a.parents if p == home or home in p.parents}
    hidden = [run / "skills", run / "groupchats" / "attachments" / "secret"]
    for path in [home, *tree(home)]:
        got = codex_access(entries, path)
        if path in passage:
            assert got == "read", path  # open for traversal (Python's realpath)
        elif path.name.endswith(".kdbx") or any(h == path or h in path.parents for h in hidden):
            assert got == "deny", path
        elif any(a == path or a in path.parents for a in allowed):
            assert got in ("read", "write"), path
        else:
            assert got == "deny", path


def test_write_roots_and_system(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    run = layout["run"]
    assert entries[":root"] == "read"
    assert codex_access(entries, layout["work"] / "new.py") == "write"
    assert codex_access(entries, run / "tmp" / "x") == "write"
    assert codex_access(entries, run / ".venv" / "bin" / "python") == "write"
    assert codex_access(entries, layout["home"] / "readdir" / "x.txt") == "read"
    assert codex_access(entries, Path("/usr/lib/libz.dylib")) == "read"
    assert codex_access(entries, Path("/Volumes/USB/x")) == "deny"


LATE = [
    "orchestrator.jsonl",
    "approvals.jsonl",
    "usage.json",
    "agents/gpt-1/transcript.jsonl",
    "agents/gpt-1/raw.jsonl",
    "agents/gpt-1/session.json",
    "agents/gpt-1/stderr.log",
    "agents/gpt-1/transcript.md",
    "agents/gpt-1/changes.diff",
    "agents/gpt-1/blobs/0001.txt",
    "agents/gpt-1/plugin/x.json",
    "agents/spawned-1/transcript.jsonl",
    "groupchats/room.sqlite-wal",
    "groupchats/room.sqlite-shm",
    "groupchats/main.md",
    "groupchats/main.jsonl",
]


@pytest.mark.parametrize("name", LATE)
def test_globs_cover_files_arr_creates_later(layout: dict[str, Path], name: str) -> None:
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    assert codex_access(entries, layout["run"] / name) == "deny"


def test_globs_keep_own_folders_open(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    run = layout["run"]
    for path in (
        layout["work"] / "notes.md",
        layout["work"] / "log.jsonl",
        layout["work"] / "blobs" / "x",
        run / "groupchats" / "attachments" / "main" / "1" / "a.md",
        run / "output" / "summary.md",
    ):
        assert codex_access(entries, path) in ("read", "write"), path


def test_known_gap_new_unknown_entry(layout: dict[str, Path]) -> None:
    """Documented gap (docs/07 §3 ①): a new entry with an unknown name stays readable until
    the list is rebuilt at the next app-server start."""
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    assert codex_access(entries, layout["home"] / "new-folder" / "x.txt") == "read"
    (layout["home"] / "new-folder").mkdir()
    assert (
        codex_access(filesystem_entries(profile, paths, ["main"]), layout["home"] / "new-folder")
        == "deny"
    )


def test_skills_snapshot_and_plugin_denied(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths)
    run = layout["run"]
    assert profile.can_read(run / "skills")  # the SandboxProfile allows it …
    assert entries[str(run / "skills")] == "deny"  # … Codex agents do not need it (L14)
    assert entries[str(run / "agents" / "gpt-1" / "plugin")] == "deny"


def test_only_own_channel_attachments(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main", "review"])
    att = layout["run"] / "groupchats" / "attachments"
    assert codex_access(entries, att) == "read"  # on the way to the own channels
    assert codex_access(entries, att / "main" / "a.txt") == "read"
    assert codex_access(entries, att / "review" / "c.txt") == "read"
    assert codex_access(entries, att / "secret" / "b.txt") == "deny"


def test_without_own_channel_attachments_stay_closed(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, [])
    att = layout["run"] / "groupchats" / "attachments"
    assert entries[str(att)] == "deny"
    assert codex_access(entries, att / "main" / "a.txt") == "deny"


def test_broad_read_dir_does_not_reopen_skills(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout, read_dirs=[str(layout["run"])])
    entries = filesystem_entries(profile, paths, ["main"])
    run = layout["run"]
    assert entries[str(run)] == "read"
    assert entries[str(run / "skills")] == "deny"
    assert codex_access(entries, run / "groupchats" / "attachments" / "secret" / "b.txt") == "deny"
    # a readable run folder stays readable: no late globs there
    assert codex_access(entries, run / "orchestrator.jsonl") == "read"


def test_sensitive_wins_over_read_dir(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout, read_dirs=[str(layout["home"])])
    entries = filesystem_entries(profile, paths)
    home = layout["home"]
    assert codex_access(entries, home / "notes.txt") == "deny"  # tie: deny wins (SandboxProfile)
    assert codex_access(entries, home / ".ssh" / "known_hosts") == "deny"
    assert codex_access(entries, home / ".zsh_history") == "deny"
    assert codex_access(entries, home / ".bash_history") == "deny"  # pattern, also when new


def test_sensitive_read_dir_is_dropped(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout, read_dirs=[str(layout["home"] / ".ssh")])
    entries = filesystem_entries(profile, paths)
    assert codex_access(entries, layout["home"] / ".ssh" / "known_hosts") == "deny"


def test_glob_characters_are_escaped(layout: dict[str, Path]) -> None:
    odd = layout["home"] / "a[1]*.txt"
    odd.write_text("x")
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths)
    assert entries[glob_escape(str(odd))] == "deny"
    assert glob_escape("a[1]*?.md") == "a[[]1[]][*][?].md"
    assert codex_access(entries, odd) == "deny"


def test_allowed_path_with_glob_character_is_refused(layout: dict[str, Path]) -> None:
    odd = layout["home"] / "pa[pers]"
    odd.mkdir()
    _, profile, paths = setup(layout, read_dirs=[str(odd)])
    with pytest.raises(ValueError, match="Muster"):
        filesystem_entries(profile, paths)


def test_overrides_parse_as_toml(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    overrides = isolation_overrides(profile, paths, model="gpt-6-luna", channels=["main"])
    parsed: dict[str, object] = {}
    for item in overrides:
        key, _, value = item.partition("=")
        parsed[key] = tomllib.loads(f"v = {value}")["v"]
    assert parsed["default_permissions"] == PERMISSION_PROFILE
    fs = parsed[f"permissions.{PERMISSION_PROFILE}.filesystem"]
    assert fs == filesystem_entries(profile, paths, ["main"])
    assert parsed["notify"] == []
    assert parsed["project_root_markers"] == []
    assert parsed["skills.bundled.enabled"] is False
    assert parsed["web_search"] == "disabled"
    assert parsed["features.plugins"] is False
    assert "features.code_mode_host" not in parsed  # would remove the shell
    assert not any(k.startswith("mcp_servers") for k in parsed)
    assert not any("auto_review" in str(v) for v in parsed.values())


def test_web_search_follows_tools_web(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout, tools={"web": True})
    overrides = isolation_overrides(profile, paths, model="m")
    assert 'web_search="live"' in overrides


def test_toml_value_escapes() -> None:
    value = {'a "b"\\': ["x", True, 3]}
    assert tomllib.loads(f"v = {toml_value(value)}")["v"] == value


def test_launch_env_and_thread_config(layout: dict[str, Path], tmp_path: Path) -> None:
    agent, profile, paths = setup(layout)
    environ = {"PATH": "/usr/bin:/opt/base/bin", "OPENAI_API_KEY": "sk-not-real-000000000000"}
    launch = build_launch(agent, profile, paths, codex_home=tmp_path / "ch", environ=environ)
    run = layout["run"]
    env = launch.env
    assert env["CODEX_HOME"] == str(tmp_path / "ch")
    assert env["TMPDIR"] == str(run / "tmp")
    assert env["HOME"] == str(run / "tmp" / "home" / "gpt-1")
    assert env["PATH"].split(":") == [str(run / ".venv/bin"), "/opt/base/bin", "/usr/bin"]
    assert env["OPENAI_API_KEY"] == ""
    assert all(env[name] == "" for name in SECRET_ENV if name in environ)
    assert env["MPLCONFIGDIR"].startswith(str(run / "tmp"))
    room = launch.thread_config["mcp_servers"][ROOM_SERVER]
    assert room["args"] == ["-m", "arr.room.server"]
    assert room["env"] == {"AGENT_ID": "gpt-1", "RUN_DIR": str(run)}
    assert room["default_tools_approval_mode"] == "approve"
    assert launch.cwd == str(layout["work"])
    assert launch.developer_instructions.startswith("Du bist gpt-1.")
    assert "Arbeitsumgebung (arr)" in launch.developer_instructions
    assert not launch.network_access


def test_launch_without_run_venv(layout: dict[str, Path], tmp_path: Path) -> None:
    agent, profile, paths = setup(layout)
    file = agent.file.model_copy(update={"run_venv": RunVenvMode.OFF})
    agent = PreparedAgent(file, agent.run_dir)
    launch = build_launch(agent, profile, paths, codex_home=tmp_path, environ={"PATH": ""})
    assert launch.env["PATH"] == "/opt/base/bin"


def test_project_mcp_servers_are_switched_off(layout: dict[str, Path], tmp_path: Path) -> None:
    """Codex starts `mcp_servers` of `<cwd>/.codex/config.toml` even in an untrusted folder."""
    codex_dir = layout["work"] / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.decoy]\ncommand = "/usr/bin/true"\n[mcp_servers.arr-room]\ncommand = "x"\n'
    )
    agent, profile, paths = setup(layout)
    launch = build_launch(agent, profile, paths, codex_home=tmp_path, environ={"PATH": ""})
    servers = launch.thread_config["mcp_servers"]
    assert servers["decoy"] == {"command": "/usr/bin/false", "enabled": False}
    assert servers[ROOM_SERVER]["args"] == ["-m", "arr.room.server"]  # ours, not the project's


def test_broken_project_config_is_ignored(layout: dict[str, Path], tmp_path: Path) -> None:
    (layout["work"] / ".codex").mkdir()
    (layout["work"] / ".codex" / "config.toml").write_text("[mcp_servers\n")
    agent, profile, paths = setup(layout)
    launch = build_launch(agent, profile, paths, codex_home=tmp_path, environ={"PATH": ""})
    assert list(launch.thread_config["mcp_servers"]) == [ROOM_SERVER]


def test_launch_env_isolates_python(layout: dict[str, Path], tmp_path: Path) -> None:
    agent, profile, paths = setup(layout)
    environ = {"PATH": "/usr/bin", "VIRTUAL_ENV": "/arr/.venv", "PYTHONPATH": "/arr/src"}
    env = build_launch(agent, profile, paths, codex_home=tmp_path, environ=environ).env
    assert env["VIRTUAL_ENV"] == str(layout["run"] / ".venv")  # not arr's own venv
    assert env["PYTHONPATH"] == "" and env["PYTHONHOME"] == ""
    assert env["UV_NO_CONFIG"] == "1"  # parents on the way may hold a denied pyproject.toml
