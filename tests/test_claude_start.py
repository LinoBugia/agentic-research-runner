"""The start program hands the login to the CLI through a pipe (W3-06 §12, `claude_start`).

The real program runs here in a process of its own, against a stand-in for the CLI that says
what it got. The real CLI and the real sandboxes are in `test_login_guard.py`.
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from arr.auth import (
    CLAUDE_SWITCHES,
    SECRET_VARIABLES,
    START_PROFILE_VARIABLE,
    AuthError,
    claude_env,
)
from arr.backends import claude_start
from arr.backends.claude_start import (
    HANDOVER_FAILED,
    child_environment,
    program_parts,
    refuse_writable,
    shim_text,
    start_program,
)
from arr.errors import ArrError
from arr.models import AuthProfile

SECRET = "sk-ant-MARKER-only-a-test"
OAUTH = "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"
API_KEY = "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR"

STAND_IN = f"""#!{sys.executable} -I
import json, os, sys
answer = {{"argv": sys.argv[1:], "env": dict(os.environ)}}
for name in ({OAUTH!r}, {API_KEY!r}):
    if name in os.environ:
        answer["variable"] = name
        descriptor = int(os.environ[name])
        answer["secret"] = os.read(descriptor, 4096).decode()
        answer["at_end"] = os.read(descriptor, 4096) == b""
print(json.dumps(answer))
"""


@pytest.fixture
def stand_in(tmp_path: Path) -> Path:
    program = tmp_path / "claude"
    program.write_text(STAND_IN)
    program.chmod(0o755)
    return program


def profiles(arr_home: Path, *lines: str) -> None:
    arr_home.mkdir(parents=True, exist_ok=True)
    body = "".join(f"  - {line}\n" for line in lines)
    (arr_home / "profiles.yaml").write_text(f"version: 1\nprofiles:\n{body}")


def run_start(
    stand_in: Path,
    arr_home: Path,
    *arguments: str,
    profile: str | None = "lab",
    secret: str | None = SECRET,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """`main` as the shell file runs it: its own process, the keychain replaced by a value."""
    entry = (
        "from pathlib import Path\n"
        "from arr.backends import claude_start\n"
        f"claude_start.bundled_cli = lambda: Path({str(stand_in)!r})\n"
        f"claude_start.main(reader=lambda service, account: {secret!r})\n"
    )
    env = {"PATH": "/usr/bin:/bin", "ARR_HOME": str(arr_home), **(extra_env or {})}
    if profile:
        env[START_PROFILE_VARIABLE] = profile
    return subprocess.run(
        [sys.executable, "-I", "-c", entry, *arguments],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_claude_env_never_holds_a_secret() -> None:
    reader = lambda _service, _account: SECRET  # noqa: E731
    for kind in ("claude_oauth", "claude_api_key"):
        env = claude_env(AuthProfile(name="lab", kind=kind), reader)
        assert env == {**CLAUDE_SWITCHES, START_PROFILE_VARIABLE: "lab"}
        assert SECRET not in json.dumps(env)


def test_the_mac_login_needs_no_start_program() -> None:
    env = claude_env(AuthProfile(name="max", kind="claude_oauth"), lambda _s, _a: None)
    assert env == CLAUDE_SWITCHES


@pytest.mark.parametrize(
    ("kind", "variable"), [("claude_oauth", OAUTH), ("claude_api_key", API_KEY)]
)
def test_the_secret_arrives_through_a_pipe_and_nowhere_else(
    kind: str, variable: str, stand_in: Path, arr_home: Path
) -> None:
    profiles(arr_home, f"{{name: lab, kind: {kind}}}")
    done = run_start(stand_in, arr_home, "--print", "hallo")
    assert done.returncode == 0, done.stderr
    answer = json.loads(done.stdout)
    assert answer["secret"] == SECRET and answer["at_end"], "the pipe can be read only once"
    assert answer["variable"] == variable and answer["argv"] == ["--print", "hallo"]
    assert SECRET not in json.dumps({"argv": answer["argv"], "env": answer["env"]})
    assert START_PROFILE_VARIABLE not in answer["env"]


def test_a_secret_that_came_along_never_reaches_the_cli(stand_in: Path, arr_home: Path) -> None:
    profiles(arr_home, "{name: lab, kind: claude_oauth}")
    inherited = dict.fromkeys(SECRET_VARIABLES, "from-the-parent")
    done = run_start(stand_in, arr_home, extra_env=inherited)
    answer = json.loads(done.stdout)
    assert not answer["env"].keys() & set(SECRET_VARIABLES)
    assert "from-the-parent" not in done.stdout


def test_the_version_probe_of_the_sdk_needs_no_profile(stand_in: Path, arr_home: Path) -> None:
    done = run_start(stand_in, arr_home, "-v", profile=None)
    assert done.returncode == 0 and json.loads(done.stdout)["argv"] == ["-v"]


@pytest.mark.parametrize(
    ("lines", "profile", "secret", "text"),
    [
        (["{name: lab, kind: claude_oauth}"], None, SECRET, "kein Profil genannt"),
        (["{name: other, kind: claude_oauth}"], "lab", SECRET, "gibt es nicht"),
        (["{name: lab, kind: claude_oauth}"], "lab", None, "kein Geheimnis"),
        (["{name: lab, kind: claude_oauth, disabled: true}"], "lab", SECRET, "gesperrt"),
        (["{name: lab, kind: codex_chatgpt}"], "lab", SECRET, "passt nicht"),
    ],
)
def test_without_a_handover_the_cli_does_not_start(
    lines: list[str],
    profile: str | None,
    secret: str | None,
    text: str,
    stand_in: Path,
    arr_home: Path,
) -> None:
    profiles(arr_home, *lines)
    done = run_start(stand_in, arr_home, "--print", profile=profile, secret=secret)
    assert done.returncode == HANDOVER_FAILED
    assert done.stdout == "", "the CLI must not start"
    assert text in done.stderr and "Der Agent startet nicht" in done.stderr
    assert SECRET not in done.stderr


def test_an_unreadable_profile_file_stops_the_start(stand_in: Path, arr_home: Path) -> None:
    arr_home.mkdir(parents=True)
    (arr_home / "profiles.yaml").write_text("{ kaputt")
    done = run_start(stand_in, arr_home, "--print")
    assert done.returncode == HANDOVER_FAILED and done.stdout == ""


def test_the_shell_file_shuts_out_the_surroundings(arr_home: Path) -> None:
    text = shim_text()
    assert text.startswith("#!/bin/sh\nexec ")
    interpreter = text.split("\n")[1].split()[1]
    assert Path(interpreter).is_absolute(), "`python3` would come from the run venv"
    assert " -I " in text and text.rstrip().endswith('"$@"')
    assert "env python" not in text


def test_a_path_with_blanks_stays_one_word() -> None:
    assert "'/a b/python'" in shim_text(python="/a b/python")


def test_start_program_is_private_and_stays(arr_home: Path) -> None:
    shim = start_program()
    assert stat.S_IMODE(shim.stat().st_mode) == 0o700
    assert stat.S_IMODE(shim.parent.stat().st_mode) == 0o700
    assert shim.parent.parent == arr_home
    before = shim.stat().st_ino
    assert start_program() == shim and shim.stat().st_ino == before
    assert [p.name for p in shim.parent.iterdir()] == [shim.name], "no temp file stays"


def test_a_changed_start_program_is_replaced(arr_home: Path) -> None:
    shim = start_program()
    text = shim.read_text()
    shim.write_text("#!/bin/sh\necho fremd\n")
    assert start_program().read_text() == text
    shim.unlink()
    shim.symlink_to("/usr/bin/true")
    assert not start_program().is_symlink()


@pytest.mark.parametrize("index", range(5))
def test_no_part_may_lie_in_a_write_root(index: int, arr_home: Path) -> None:
    shim = arr_home / "bin" / "claude-start-x"
    part = program_parts(shim)[index]
    with pytest.raises(ArrError, match="startet nicht"):
        refuse_writable(shim, [part.parent])
    with pytest.raises(ArrError, match="startet nicht"):
        refuse_writable(shim, [part])


def test_a_write_root_inside_the_installation_is_refused(arr_home: Path) -> None:
    with pytest.raises(ArrError, match="startet nicht"):
        start_program([Path(sys.prefix) / "lib" / "agent-work"])


def test_case_does_not_hide_a_write_root(arr_home: Path) -> None:
    arr_home.mkdir(parents=True)
    with pytest.raises(ArrError, match="startet nicht"):
        start_program([Path(str(arr_home).upper())])


def test_a_workfolder_elsewhere_is_fine(arr_home: Path, tmp_path: Path) -> None:
    assert start_program([tmp_path / "wf" / "runs" / "1" / "work"]).is_file()


def test_a_folder_that_cannot_be_written_stops_the_start(arr_home: Path) -> None:
    arr_home.parent.mkdir(parents=True, exist_ok=True)
    arr_home.write_text("no folder")
    with pytest.raises(ArrError, match="ließ sich nicht anlegen"):
        start_program()


def test_child_environment_keeps_the_rest() -> None:
    assert child_environment({"PATH": "/bin", START_PROFILE_VARIABLE: "x"}) == {"PATH": "/bin"}


def test_a_missing_sdk_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_start.importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(ArrError, match="claude-agent-sdk"):
        claude_start.bundled_cli()


def test_the_key_error_of_a_wrong_kind_stays(arr_home: Any) -> None:
    with pytest.raises(AuthError):
        claude_env(AuthProfile(name="gpt", kind="codex_chatgpt"), lambda _s, _a: SECRET)


def test_descriptor_stays_open_across_exec(stand_in: Path, arr_home: Path) -> None:
    profiles(arr_home, "{name: lab, kind: claude_oauth}")
    answer = json.loads(run_start(stand_in, arr_home).stdout)
    assert int(answer["env"][OAUTH]) > 2, "stdin, stdout and stderr stay what they are"
    assert os.path.exists(stand_in)
