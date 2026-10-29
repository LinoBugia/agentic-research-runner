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
from arr.errors import ArrError
from arr.models import AgentConfig, Provider, RunVenvMode, Toolchain
from arr.permissions import policy
from arr.permissions.policy import PermissionRules, RunPaths, profile_for
from arr.runs.prepare import AgentFile, PreparedAgent


@pytest.fixture(autouse=True)
def no_user_temp_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tmp_path` lies in the user's temp folder, which `profile_for` denies (W2-01.1). Its
    other entries would all become siblings; the tests must not depend on the machine."""
    monkeypatch.setattr(policy, "user_temp_dirs", lambda: [])


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
    assert not profile.can_read(run / "skills")  # W2-01: not in the profile any more …
    assert entries[str(run / "skills")] == "deny"  # … and denied explicitly (L14)
    assert profile.can_read(run / "agents" / "gpt-1" / "plugin")  # the profile allows it …
    assert entries[str(run / "agents" / "gpt-1" / "plugin")] == "deny"  # … Codex needs none


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


# -- W2-02.1 ------------------------------------------------------------------------------


def parsed_overrides(overrides: tuple[str, ...]) -> dict[str, object]:
    out: dict[str, object] = {}
    for item in overrides:
        key, _, value = item.partition("=")
        out[key] = tomllib.loads(f"v = {value}")["v"]
    return out


def test_h1_shell_inherits_only_core_and_the_agents_variables(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    agent, profile, paths = setup(layout)
    environ = {"PATH": "/usr/bin", "GITHUB_TOKEN": "x", "CLAUDE_CODE_USER_EMAIL": "x"}
    launch = build_launch(agent, profile, paths, codex_home=tmp_path / "ch", environ=environ)
    parsed = parsed_overrides(launch.config_overrides)
    assert parsed["shell_environment_policy.inherit"] == "core"
    shell = parsed["shell_environment_policy.set"]
    assert shell == launch.shell_env
    assert {"HOME", "PATH", "TMPDIR", "VIRTUAL_ENV", "UV_NO_CONFIG", "MPLCONFIGDIR"} <= set(shell)
    assert "CODEX_HOME" not in shell and "CODEX_HOME" in launch.env  # only the app-server
    assert not [n for n in shell if any(w in n for w in ("TOKEN", "SECRET", "KEY", "CLAUDE"))]


def test_h1_no_virtual_env_without_run_venv(layout: dict[str, Path], tmp_path: Path) -> None:
    agent, profile, paths = setup(layout)
    file = agent.file.model_copy(update={"run_venv": RunVenvMode.OFF})
    launch = build_launch(PreparedAgent(file, agent.run_dir), profile, paths, codex_home=tmp_path)
    assert "VIRTUAL_ENV" not in launch.shell_env


def test_m1_limits(layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    from arr.backends import codex_config

    assert (codex_config.ENTRIES_WARN, codex_config.ENTRIES_MAX) == (250, 500)  # W2-05
    _, profile, paths = setup(layout)
    count = len(filesystem_entries(profile, paths, ["main"]))
    monkeypatch.setattr(codex_config, "ENTRIES_WARN", count)
    caplog.set_level("WARNING")
    filesystem_entries(profile, paths, ["main"])
    assert f"{count} Einträgen" in caplog.text
    monkeypatch.setattr(codex_config, "ENTRIES_MAX", count)
    with pytest.raises(ArrError, match="Archiviere alte Runs"):
        filesystem_entries(profile, paths, ["main"])


def test_w2_05_profile_size_limit_gives_the_german_message(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nachprüfung W2-02.1 N1: a cautious byte bound with a German message (W2-05.1 G3: the
    message no longer claims a Seatbelt limit of 65535 bytes, the review measured 205 KB)."""
    from arr.backends import codex_config
    from arr.backends.codex_config import toml_value

    assert codex_config.PROFILE_MAX_BYTES == 40_000
    assert not hasattr(codex_config, "SEATBELT_MAX_BYTES")
    _, profile, paths = setup(layout)
    size = len(toml_value(filesystem_entries(profile, paths, ["main"])).encode("utf-8"))
    monkeypatch.setattr(codex_config, "PROFILE_MAX_BYTES", size)
    with pytest.raises(ArrError, match=rf"{size} Bytes groß \(Grenze {size} Bytes\)") as info:
        filesystem_entries(profile, paths, ["main"])
    assert "65535" not in str(info.value) and "Seatbelt" not in str(info.value)


def test_m1_many_old_runs_stop_the_turn(layout: dict[str, Path]) -> None:
    runs = layout["run"].parent
    for n in range(1000):
        (runs / f"20261029-0000_demo_custom_{n:04x}").mkdir()
    _, profile, paths = setup(layout)
    with pytest.raises(ArrError, match=r"Lese-Sperrliste für Codex hat 1\d{3} Einträge"):
        filesystem_entries(profile, paths, ["main"])


@pytest.mark.parametrize(
    "name", ["Rechnung 🧾.pdf", 'zitat "x".txt', "back\\slash", "ctl\x01\x7f\ttab", "äöü ß"]
)
def test_m2_odd_names_give_valid_toml(layout: dict[str, Path], name: str) -> None:
    (layout["home"] / name).write_text("x")
    _, profile, paths = setup(layout)
    overrides = isolation_overrides(profile, paths, model="m", channels=["main"])
    fs = parsed_overrides(overrides)[f"permissions.{PERMISSION_PROFILE}.filesystem"]
    assert fs == filesystem_entries(profile, paths, ["main"])
    assert fs[str(layout["home"] / name)] == "deny"


def test_m2_undecodable_name_is_refused() -> None:
    from arr.backends.codex_config import toml_string

    with pytest.raises(ArrError, match="kein gültiges UTF-8"):
        toml_string("/home/a\udcff.txt")  # what os.listdir gives for the byte 0xff
    assert tomllib.loads(f"v = {toml_string('🧾')}")["v"] == "🧾"


def test_m3_foreign_channels_denied_before_they_exist(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    att = layout["run"] / "groupchats" / "attachments"
    entries = filesystem_entries(profile, paths, ["main"], run_channels=["main", "later", "secret"])
    assert not (att / "later").exists()
    assert entries[str(att / "later")] == "deny"
    assert codex_access(entries, att / "later" / "17" / "x.txt") == "deny"
    assert codex_access(entries, att / "main" / "a.txt") == "read"


def test_m3_staging_copies_are_denied(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    att = layout["run"] / "groupchats" / "attachments"
    assert codex_access(entries, att / ".staging-abc123" / "x.txt") == "deny"
    assert codex_access(entries, att / ".staging-abc123") == "deny"


def test_m5_login_folder_outside_home_is_denied(layout: dict[str, Path], tmp_path: Path) -> None:
    login = tmp_path / "shared" / "codex_home"  # outside the layout's ~
    (login).mkdir(parents=True)
    (login / "auth.json").write_text("{}")
    agent, profile, paths = setup(layout)
    launch = build_launch(agent, profile, paths, codex_home=login, environ={"PATH": ""})
    assert launch.filesystem[str(login)] == "deny"
    assert codex_access(launch.filesystem, login / "auth.json") == "deny"


def test_m5_login_folder_is_never_opened(layout: dict[str, Path]) -> None:
    """Even a read folder inside the login folder does not make it readable."""
    login = layout["home"] / "logins" / "codex_home"
    (login / "notes").mkdir(parents=True)
    _, profile, paths = setup(layout, read_dirs=[str(login / "notes")])
    entries = filesystem_entries(profile, paths, hard_deny=[login])
    assert codex_access(entries, login / "auth.json") == "deny"
    assert codex_access(entries, login / "notes" / "x.md") == "deny"
