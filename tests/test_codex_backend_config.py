"""Translation SandboxProfile → Codex launch settings (no SDK, no model).

W3-00 (variant 2): `~` is denied as a whole, the venvs live outside `~` (`arr.venvs`), and the
entries depend on the profile only. The tests of variant 1 (open parents, sibling list,
`LATE_GLOBS`, known gap) were rewritten for variant 2 or removed.
"""

import os
import re
import tomllib
import unicodedata
from pathlib import Path

import pytest

from arr.backends.codex_config import (
    ENTRIES_WARN,
    PERMISSION_PROFILE,
    ROOM_SERVER,
    SECRET_ENV,
    build_launch,
    filesystem_entries,
    glob_escape,
    isolation_overrides,
    toml_value,
)
from arr.backends.process_path import NO_GIT_FOLDER
from arr.errors import ArrError
from arr.models import AgentConfig, Provider, RunVenvMode, Toolchain
from arr.permissions import policy
from arr.permissions.policy import (
    CODEX_READ_ONLY_NAMES,
    PROTECTED_NAMES,
    PermissionRules,
    RunPaths,
    profile_for,
)
from arr.runs.prepare import AgentFile, PreparedAgent
from arr.settings import app_dir


def no_git() -> str:
    """The folder of the `git` stub, first on the PATH outside the sandbox (W3-06.4)."""
    return str(app_dir().joinpath(*NO_GIT_FOLDER))


@pytest.fixture(autouse=True)
def no_user_temp_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tmp_path` lies in the user's temp folder, which `profile_for` denies (W2-01.1); the
    tests must not depend on the machine."""
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


def setup(layout: dict[str, Path], toolchain: Toolchain | None = None, **changes: object):
    changes.setdefault("read_dirs", [str(layout["home"] / "readdir")])
    config = agent_config(**changes)
    paths = RunPaths(layout["wf"], layout["run"], toolchain or Toolchain())
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
        elif glob.startswith("{,/**}", i):  # the only alternation arr writes (W3-00)
            out, i = out + "(/.*)?", i + 6
        else:
            out, i = out + re.escape(c), i + 1
    return re.compile(out + "$")


def codex_access(entries: dict[str, str], path: Path) -> str:
    text = str(path)
    for key, access in entries.items():
        if any(ch in key for ch in "*?[{") and access == "deny" and _glob_regex(key).match(text):
            return "deny"
    best, access = "", entries[":root"]
    for key, value in entries.items():
        if any(ch in key for ch in "*?[{") or key == ":root":
            continue
        if (text == key or text.startswith(key.rstrip("/") + "/")) and len(key) > len(best):
            best, access = key, value
    return access


def tree(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*"))


def run_venv(paths: RunPaths) -> Path:
    return Path(os.path.realpath(paths.venv_dir))


def test_every_existing_entry_follows_profile(layout: dict[str, Path]) -> None:
    """`~` is denied as a whole; only the allowed roots are open (variant 2, W3-00)."""
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    run, home = layout["run"], layout["home"]
    allowed = [layout["work"], run / "tmp", run / "output", home / "readdir"]
    allowed.append(run / "groupchats" / "attachments" / "main")
    assert entries[str(home)] == "deny"
    for path in [home, *tree(home)]:
        got = codex_access(entries, path)
        if path.name.endswith(".kdbx"):
            assert got == "deny", path
        elif any(a == path or a in path.parents for a in allowed):
            assert got in ("read", "write"), path
        else:
            assert got == "deny", path  # also the parents of allowed roots: no names visible


def test_entries_come_from_the_profile_only(layout: dict[str, Path], venv_root: Path) -> None:
    """Every path entry is a root, a block or a sensitive path of the profile, or one of the
    explicitly denied run folders; nothing is found by listing a folder."""
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    run = layout["run"]
    known = {*profile.write_roots, *profile.read_allow, *profile.read_deny, *profile.sensitive}
    known |= {run / "skills", run / "groupchats" / "attachments", Path(os.path.realpath(venv_root))}
    known |= {run / "agents" / "gpt-1" / "plugin"}
    known |= {root / name for root in profile.write_roots for name in CODEX_READ_ONLY_NAMES}
    plain = {Path(k) for k in entries if k != ":root" and "*" not in k}
    assert plain <= known, plain - known
    assert not any(Path(k).parent == run.parent for k in entries)  # no other runs


def test_write_roots_and_system(layout: dict[str, Path]) -> None:
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    run = layout["run"]
    assert entries[":root"] == "read"
    assert codex_access(entries, layout["work"] / "new.py") == "write"
    assert codex_access(entries, run / "tmp" / "x") == "write"
    assert codex_access(entries, run_venv(paths) / "bin" / "python") == "write"
    assert codex_access(entries, run / ".venv" / "bin" / "python") == "deny"  # no <run>/.venv
    assert codex_access(entries, layout["home"] / "readdir" / "x.txt") == "read"
    assert codex_access(entries, Path("/usr/lib/libz.dylib")) == "read"
    assert codex_access(entries, Path("/Volumes/USB/x")) == "deny"


RUN_ID = "20261029-1912_demo_custom_b32e"


@pytest.fixture
def venv_layout(layout: dict[str, Path], venv_root: Path) -> dict[str, Path]:
    """`layout` with a realistic run ID and a base venv below `<venv root>/venvs/`."""
    run = layout["run"].parent / RUN_ID
    layout["run"].rename(run)
    root = Path(os.path.realpath(venv_root))
    base = root / "venvs" / "mathe"
    (base / "bin").mkdir(parents=True)
    (base / "pyvenv.cfg").write_text("home = /opt/pybase/bin\n")
    return {**layout, "run": run, "work": run / "agents" / "gpt-1" / "work", "root": root}


def venv_setup(layout: dict[str, Path], **toolchain: object):
    toolchain.setdefault("python", str(layout["root"] / "venvs" / "mathe"))
    return setup(layout, Toolchain.model_validate(toolchain))


def test_no_ancestor_of_the_run_venv_is_denied(venv_layout: dict[str, Path]) -> None:
    """A denied ancestor of the run venv stops Python (`realpath` of `bin/`, W3-00 lab)."""
    _, profile, paths = venv_setup(venv_layout)
    entries = filesystem_entries(profile, paths, ["main"])
    venv = run_venv(paths)
    for ancestor in venv.parents:
        assert codex_access(entries, ancestor) == "read", ancestor
    assert str(venv_layout["root"]) not in entries  # the profile denies it; Codex uses globs
    assert codex_access(entries, venv / "lib" / "x.py") == "write"


def test_w3_00_other_venvs_are_denied_by_glob(venv_layout: dict[str, Path]) -> None:
    root = venv_layout["root"]
    _, profile, paths = venv_setup(venv_layout)
    entries = filesystem_entries(profile, paths, ["main"])
    base = root / "venvs" / "mathe"
    assert codex_access(entries, base / "lib" / "sympy.py") == "read"  # never write
    assert codex_access(entries, Path("/opt/pybase/lib/libpython.dylib")) == "read"
    for name in (
        "20261029-1912_demo_custom_b33f",  # parallel run
        "20261029-2359_new_custom_aaaa",  # appears during the turn
        RUN_ID + "x",  # longer ID
        RUN_ID[:-1],  # shorter ID
        "2",
        "X",
    ):
        for path in (root / "runs" / name, root / "runs" / name / "lib" / "c.txt"):
            assert codex_access(entries, path) == "deny", path
    for name in ("other", "math", "mathex", "Mathe2"):
        assert codex_access(entries, root / "venvs" / name / "c.txt") == "deny", name
    assert codex_access(entries, root / "runs") == "read"  # names stay visible (report §7)
    globs = [k for k in entries if k.startswith(str(root)) and k.endswith("{,/**}")]
    assert len(globs) == 2 * len(RUN_ID) + 2 * len("mathe")


def test_w3_00_without_run_venv_or_base_venv_everything_is_denied(
    venv_layout: dict[str, Path], tmp_path: Path
) -> None:
    root = venv_layout["root"]
    elsewhere = tmp_path / "base-elsewhere"
    elsewhere.mkdir()
    _, profile, paths = venv_setup(venv_layout, python=str(elsewhere), run_venv="off")
    entries = filesystem_entries(profile, paths, ["main"])
    assert entries[glob_escape(str(root / "runs")) + "/*{,/**}"] == "deny"
    assert entries[glob_escape(str(root / "venvs")) + "/*{,/**}"] == "deny"
    assert codex_access(entries, root / "runs" / RUN_ID / "bin" / "python") == "deny"
    assert codex_access(entries, root / "venvs" / "mathe" / "lib") == "deny"


def test_w3_00_venv_name_that_globs_cannot_express_is_refused(
    venv_layout: dict[str, Path],
) -> None:
    odd = venv_layout["root"] / "venvs" / "ma[th]e"
    (odd / "bin").mkdir(parents=True)
    with pytest.raises((ArrError, ValueError)):
        _, profile, paths = venv_setup(venv_layout, python=str(odd))
        filesystem_entries(profile, paths, ["main"])


@pytest.mark.parametrize(
    "name", ["mäthe", "w3rev-é1", unicodedata.normalize("NFD", "w3rev-é1"), "mathe✓"]
)
def test_w3_00_1_m1_non_ascii_base_venv_name_is_refused(
    venv_layout: dict[str, Path], name: str
) -> None:
    """Seatbelt compares in NFD, `[!é]` acts like `[!e]`: such a name would open `e1`."""
    base = venv_layout["root"] / "venvs" / name
    (base / "bin").mkdir(parents=True)
    _, profile, paths = venv_setup(venv_layout, python=str(base))
    with pytest.raises(ArrError, match="Nicht-ASCII") as info:
        filesystem_entries(profile, paths, ["main"])
    assert "Benenne die venv um" in info.value.message


def test_w3_00_1_m1_same_message_for_glob_characters(venv_layout: dict[str, Path]) -> None:
    from arr.backends.codex_config import _others

    _, profile, _ = venv_setup(venv_layout)
    folder = venv_layout["root"] / "venvs"
    messages = []
    for name in ("mä", "m!a"):
        with pytest.raises(ArrError) as info:
            _others(profile, folder, folder / name)
        messages.append(info.value.message.split("“", 1)[1])
    assert messages[0].replace("mä", "m!a") == messages[1]


FILES_ARR_WRITES = [
    "orchestrator.jsonl",
    "approvals.jsonl",
    "usage.json",
    "meta.json",
    "late-unknown.txt",
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


@pytest.mark.parametrize("name", FILES_ARR_WRITES)
def test_files_of_the_run_are_denied_without_globs(layout: dict[str, Path], name: str) -> None:
    """Variant 1 needed `LATE_GLOBS` for these; now `~` and the workfolder deny them."""
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    assert codex_access(entries, layout["run"] / name) == "deny"
    assert not [k for k in entries if "*" in k and k.startswith(str(layout["run"]))]


def test_own_folders_stay_open(layout: dict[str, Path]) -> None:
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


def test_new_entries_during_a_turn_are_denied(layout: dict[str, Path]) -> None:
    """The gap of variant 1 is closed: a new entry below `~` or in `runs/` is denied by the
    list built before it existed, and the list does not change."""
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    new = [
        layout["home"] / "new-folder" / "x.txt",
        layout["run"].parent / "20261029-1200_other_custom_beef" / "meta.json",
        layout["run"] / "late-unknown.txt",
        layout["wf"] / "projects" / "new" / "canary.txt",
    ]
    for path in new:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
        assert codex_access(entries, path) == "deny", path
    assert filesystem_entries(profile, paths, ["main"]) == entries


@pytest.mark.parametrize("where", ["home", "runs", "venv-runs", "venv-venvs"])
def test_entries_do_not_depend_on_what_lies_in_home_or_runs(
    venv_layout: dict[str, Path], where: str
) -> None:
    """A3: the same entries with 5 or 2000 entries in `~`, `runs/` or below the venv root."""
    folder = {
        "home": venv_layout["home"],
        "runs": venv_layout["run"].parent,
        "venv-runs": venv_layout["root"] / "runs",
        "venv-venvs": venv_layout["root"] / "venvs",
    }[where]
    folder.mkdir(parents=True, exist_ok=True)
    _, profile, paths = venv_setup(venv_layout)
    for n in range(5):
        (folder / f"20261029-0000_demo_custom_{n:04x}").mkdir()
    few = filesystem_entries(profile, paths, ["main"])
    for n in range(5, 2000):
        (folder / f"20261029-0000_demo_custom_{n:04x}").mkdir()
    many = filesystem_entries(profile, paths, ["main"])
    assert many == few
    assert len(few) < ENTRIES_WARN


def test_variant_1_is_gone() -> None:
    from arr.backends import codex_config

    for name in ("LATE_GLOBS", "_open_parents", "_late_globs", "_children", "_governing"):
        assert not hasattr(codex_config, name), name


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
    assert codex_access(entries, att) == "deny"  # variant 2: no folder opened on the way
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
    odd = layout["home"] / "readdir" / "a[1]*.txt"  # a denied path inside an allowed root
    odd.write_text("x")
    _, _, paths = setup(layout)
    profile = profile_for(
        agent_config(read_dirs=[str(layout["home"] / "readdir")]),
        paths,
        PermissionRules(sensitive_paths=[str(odd)]),
        home=layout["home"],
        case_insensitive=False,
    )
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
    # the app-server's git reads $HOME/.gitconfig: no write root (W3-06.3), the commands
    # get a writable HOME of their own
    assert env["HOME"] == str(run / "agents" / "gpt-1" / "home")
    assert launch.shell_env["HOME"] == str(run / "tmp" / "home" / "gpt-1")
    assert not profile.can_write(Path(env["HOME"]) / ".gitconfig")
    venv = str(paths.venv_dir / "bin")  # outside the run folder (W3-00)
    # the app-server runs outside the sandbox: the run venv is for the commands only (B1)
    assert env["PATH"].split(":") == [no_git(), "/opt/base/bin", "/usr/bin"]
    assert launch.shell_env["PATH"].split(":") == [venv, "/opt/base/bin", "/usr/bin"]
    assert env["OPENAI_API_KEY"] == ""
    assert all(env[name] == "" for name in SECRET_ENV if name in environ)
    assert env["MPLCONFIGDIR"].startswith(str(run / "tmp"))
    room = launch.thread_config["mcp_servers"][ROOM_SERVER]
    assert room["args"] == ["-I", "-m", "arr.room.server"]  # no `arr/` of the cwd (B2)
    assert room["env"] == {
        "AGENT_ID": "gpt-1",
        "RUN_DIR": str(run),
        "PATH": env["PATH"],
        "ARR_WAIT_MAX_S": "25",  # under the 30 s hand-back of `exec` (spike W3-07, F3)
        "ARR_PROVIDER": "codex",  # tool descriptions per backend (W3-07.1, B4)
    }
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
    assert launch.env["PATH"] == f"{no_git()}:/opt/base/bin"


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
    assert servers[ROOM_SERVER]["args"] == ["-I", "-m", "arr.room.server"]  # ours


def test_broken_project_config_is_ignored(layout: dict[str, Path], tmp_path: Path) -> None:
    (layout["work"] / ".codex").mkdir()
    (layout["work"] / ".codex" / "config.toml").write_text("[mcp_servers\n")
    agent, profile, paths = setup(layout)
    launch = build_launch(agent, profile, paths, codex_home=tmp_path, environ={"PATH": ""})
    assert list(launch.thread_config["mcp_servers"]) == [ROOM_SERVER]


def test_launch_env_isolates_python(layout: dict[str, Path], tmp_path: Path) -> None:
    agent, profile, paths = setup(layout)
    environ = {"PATH": "/usr/bin", "VIRTUAL_ENV": "/arr/.venv", "PYTHONPATH": "/arr/src"}
    launch = build_launch(agent, profile, paths, codex_home=tmp_path, environ=environ)
    env = launch.env
    assert env["VIRTUAL_ENV"] == ""  # neither arr's own venv nor the run venv
    assert launch.shell_env["VIRTUAL_ENV"] == str(paths.venv_dir)
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
    with pytest.raises(ArrError, match="Lese-Ordner"):
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


def test_m1_many_read_dirs_stop_the_turn(layout: dict[str, Path]) -> None:
    """The bound stays as a guard: it now grows with read folders, not with `runs/`."""
    dirs = [layout["home"] / "readdir" / f"d{n:03d}" for n in range(600)]
    _, profile, paths = setup(layout, read_dirs=[str(d) for d in dirs])
    with pytest.raises(ArrError, match=r"Lese-Sperrliste für Codex hat \d{3} Einträge"):
        filesystem_entries(profile, paths, ["main"])


@pytest.mark.parametrize(
    "name", ["Rechnung 🧾", 'zitat "x"', "back\\slash", "ctl\x01\x7f\ttab", "äöü ß"]
)
def test_m2_odd_names_give_valid_toml(layout: dict[str, Path], name: str) -> None:
    folder = layout["home"] / name
    folder.mkdir()
    _, profile, paths = setup(layout, read_dirs=[str(folder)])
    overrides = isolation_overrides(profile, paths, model="m", channels=["main"])
    fs = parsed_overrides(overrides)[f"permissions.{PERMISSION_PROFILE}.filesystem"]
    assert fs == filesystem_entries(profile, paths, ["main"])
    assert fs[str(folder)] == "read"


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


def test_no_source_puts_a_write_root_on_the_app_servers_path(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    """W3-06.3, B1: run venv, `path_prepend` and the inherited PATH, each with a write root."""
    agent, profile, paths = setup(layout)
    work_bin, tmp_bin = str(layout["work"] / "bin"), str(layout["run"] / "tmp" / "bin")
    file = agent.file.model_copy(update={"path_prepend": [work_bin, "/opt/base/bin"]})
    agent = PreparedAgent(file, agent.run_dir)
    environ = {"PATH": f"{tmp_bin}:bin:/usr/bin"}
    launch = build_launch(agent, profile, paths, codex_home=tmp_path, environ=environ)
    assert launch.env["PATH"].split(":") == [no_git(), "/opt/base/bin", "/usr/bin"]
    venv = str(paths.venv_dir / "bin")
    assert launch.shell_env["PATH"].split(":") == [
        venv,
        work_bin,
        "/opt/base/bin",
        tmp_bin,
        "bin",
        "/usr/bin",
    ]


def test_the_app_servers_home_never_lies_in_a_write_root(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    import dataclasses

    from arr.backends.codex_config import server_home

    agent, profile, paths = setup(layout)
    inside = dataclasses.replace(profile, write_roots=(*profile.write_roots, agent.agent_dir))
    with pytest.raises(ArrError, match="Home-Ordner"):
        server_home(agent, inside)


def test_protected_names_are_read_only_at_the_top_of_every_write_root(
    layout: dict[str, Path],
) -> None:
    """W3-06.4: `apply_patch` honours the profile but not the sandbox's own `.git` block."""
    assert set(CODEX_READ_ONLY_NAMES) <= set(PROTECTED_NAMES)
    _, profile, paths = setup(layout)
    entries = filesystem_entries(profile, paths, ["main"])
    for root in profile.write_roots:
        assert entries[str(root)] == "write"
        for name in CODEX_READ_ONLY_NAMES:
            assert entries[str(root / name)] == "read", (root, name)
        assert codex_access(entries, root / ".git" / "config") == "read"
        assert codex_access(entries, root / "sub" / "x.py") == "write"
