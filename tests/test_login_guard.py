"""The login guard (W3-06): agents do not get to the logins, checked again with every update.

The sandbox tests run the probe (`login_guard_probe.py`) in the real sandboxes of both
backends, with arr's real profiles and a test `HOME` full of baits (`login_guard_kit.py`). No
model and no real login: Codex runs through `codex sandbox`, Claude through the real CLI
against `fake_messages_api.py`. Only `reached` flags and counts leave the probe.

The version guard runs everywhere, also in the CI: no update passes without a new probe.
"""

import importlib.metadata
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk._cli_version import __cli_version__

from arr.models import AuthKind
from arr.permissions.policy import CODEX_READ_ONLY_NAMES, SandboxProfile
from arr.permissions.types import DecidedBy, Decision, DecisionAction, RequestCategory

FIXTURE = Path(__file__).parent / "fixtures" / "login_guard.json"

on_mac = [
    pytest.mark.macos,
    pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt exists on macOS only"),
]


def mac(test: Any) -> Any:
    for mark in on_mac:
        test = mark(test)
    return test


KINDS = [AuthKind.CLAUDE_OAUTH, AuthKind.CLAUDE_API_KEY]
OUTSIDE = "Kontrolle"


# --------------------------------------------------------------------------------------------
# Version guard, also in the CI
# --------------------------------------------------------------------------------------------


def installed_versions() -> dict[str, str]:
    return {
        "claude_agent_sdk": importlib.metadata.version("claude-agent-sdk"),
        "claude_cli": __cli_version__,
        "openai_codex": importlib.metadata.version("openai-codex"),
    }


OUTSIDE_TEST = "test_nothing_of_a_write_root_runs_outside_the_sandbox"
GUARD_TESTS = (
    OUTSIDE_TEST,
    "test_file_tools_write_nothing_that_bash_cannot",
    "test_file_tools_of_the_cli_never_write_a_protected_name",
    "test_the_write_tool_lays_no_git_for_a_filter",
    "test_apply_patch_changes_no_protected_name",
    "test_a_filter_of_apply_patch_never_runs_outside",
    "test_no_git_runs_outside_the_sandbox",
    "test_a_changed_codex_folder_stops_the_agent",
    "test_no_compile_cache_of_node_in_run_tmp",
    "test_apply_patch_move_is_refused",
    "test_apply_patch_delete_is_refused_outside_and_on_the_block_list",
    "test_a_file_request_with_a_grant_root_is_refused",
    "test_a_file_request_arr_cannot_read_is_refused",
)
"""The fixed tests of W3-06.3 and W3-06.4: what runs outside the sandbox (CLI, app-server,
their file tools) never executes or reads what an agent wrote. Each has a countertest."""
REAL_HOME_PROBE = "spikes/w3_06/real_home_probe.py"


def version_problems(recorded: dict[str, str], installed: dict[str, str]) -> list[str]:
    return [
        f"{name}: Sperr-Probe zuletzt grün mit {recorded.get(name)}, installiert ist "
        f"{installed.get(name)}. Lass die Probe auf dem Mac laufen (uv run pytest -m macos "
        f"tests/test_login_guard.py, darin auch {', '.join(GUARD_TESTS)}), dazu die Probe mit "
        f"echtem HOME ({REAL_HOME_PROBE}), und aktualisiere dann tests/fixtures/login_guard.json."
        for name in sorted({*recorded, *installed})
        if recorded.get(name) != installed.get(name)
    ]


def test_versions_are_the_ones_of_the_last_green_probe() -> None:
    recorded = json.loads(FIXTURE.read_text())
    assert version_problems(recorded, installed_versions()) == []


def test_another_version_is_reported_in_german() -> None:
    problems = version_problems({"claude_cli": "2.1.310"}, {"claude_cli": "9.9.9"})
    assert len(problems) == 1
    assert "Sperr-Probe zuletzt grün mit 2.1.310" in problems[0] and "9.9.9" in problems[0]
    assert "tests/fixtures/login_guard.json" in problems[0]
    assert all(test in problems[0] for test in GUARD_TESTS) and REAL_HOME_PROBE in problems[0]
    assert version_problems({"a": "1"}, {}) and version_problems({}, {"a": "1"})


def test_the_guard_names_what_exists() -> None:
    assert (Path(__file__).parents[1] / REAL_HOME_PROBE).is_file()
    assert all(callable(globals().get(test)) for test in GUARD_TESTS)


# --------------------------------------------------------------------------------------------
# The whole probe per backend
# --------------------------------------------------------------------------------------------


def _carries_the_probe_text(path: str) -> bool:
    try:
        return "written from the sandbox" in Path(path).read_text()
    except OSError:
        return False


def _collect(lab: Any, result: dict[str, Any]) -> dict[str, Any]:
    """What must be judged while the lab still exists."""
    plan = result["plan"]
    result["written"] = sorted(
        name for name, path in plan["write_targets"].items() if _carries_the_probe_text(path)
    )
    result["escaped"] = Path(plan["escaped"]).exists()
    result["baits_kept"] = all(lab.bait in path.read_text() for path in lab.baits.values())
    return result


@pytest.fixture(scope="module")
def codex_probe() -> dict[str, Any]:
    import login_guard_kit as kit

    with kit.open_lab("codex") as lab:
        return _collect(lab, kit.run_codex(lab))


@pytest.fixture(scope="module")
def claude_probe() -> dict[str, Any]:
    import login_guard_kit as kit

    with kit.open_lab("claude") as lab:
        return _collect(lab, kit.run_claude(lab))


@pytest.fixture(params=["codex", "claude"])
def probe(request: pytest.FixtureRequest) -> dict[str, Any]:
    return request.getfixturevalue(f"{request.param}_probe")


def reached(rows: list[dict[str, Any]]) -> list[str]:
    return [f"{row['way']} → {row['target']}" for row in rows if row["reached"]]


@mac
def test_no_way_of_the_probe_reaches_a_login(probe: dict[str, Any]) -> None:
    rows = [row for row in probe["rows"] if row["target"] != OUTSIDE]
    assert len(rows) > 50, "the probe did not run"
    assert reached(rows) == []
    assert probe["escaped"] is False
    assert probe["baits_kept"]


@mac
def test_every_way_can_read_the_control_file(probe: dict[str, Any]) -> None:
    """A way that cannot even read a file of the working folder proves nothing."""
    ways = [r for r in probe["rows"] if r["target"] == OUTSIDE and r["way"] != "Ordner auflisten"]
    assert len(ways) == 10
    assert [row["way"] for row in ways if not row["reached"]] == []


@mac
def test_a_process_that_stays_behind_reaches_nothing(probe: dict[str, Any]) -> None:
    background = probe["background"]
    assert background["ran"], "the process did not outlive the turn"
    rows = [row for row in background["rows"] if row["target"] != OUTSIDE]
    assert len(rows) > 50 and reached(rows) == []


@mac
def test_nothing_is_written_outside_the_write_roots(probe: dict[str, Any]) -> None:
    assert probe["written"] == []


@mac
def test_the_start_program_cannot_be_changed_or_run(probe: dict[str, Any]) -> None:
    rows = [row for row in probe["rows"] if row["way"].startswith("Startprogramm")]
    assert len(rows) == 5 and reached(rows) == []
    assert all(row["note"].endswith("Error") or "exit" in row["note"] for row in rows)
    assert not [row for row in rows if "FileNotFoundError" in row["note"]], "nothing to block"


@mac
def test_the_keychain_gives_nothing(probe: dict[str, Any]) -> None:
    """Not a proof on its own (review W3-06, B6): the bait is a text file, no keychain, so this
    stays green with or without the block. The block on the files is proven by the mutations
    below, the real keychain by `spikes/w3_06/real_home_probe.py`."""
    rows = [row for row in probe["rows"] if row["target"] == "Schlüsselbund"]
    assert len(rows) == 3 and reached(rows) == []


@mac
def test_the_codex_sandbox_has_no_way_to_the_keychain_service() -> None:
    """The Claude sandbox reaches the service, there the read block on the files holds: measured
    with the real `HOME` in the live probe, because a test `HOME` has no real keychain."""
    import login_guard_kit as kit

    with kit.open_lab("codex") as lab:
        rows = kit.in_codex(lab, kit.plan_for(lab, 9), "services")
    assert len(rows) == 2 and reached(rows) == []


@mac
def test_the_real_cli_got_the_marker_through_the_pipe(claude_probe: dict[str, Any]) -> None:
    assert claude_probe["credential_seen"] > 0
    assert claude_probe["errors"] == [False, False, False]


# --------------------------------------------------------------------------------------------
# A2: the probe fails when a block is missing
# --------------------------------------------------------------------------------------------


@mac
@pytest.mark.parametrize(
    "place", ["codex_auth", "claude_folder", "codex_folder", "ssh", "keychains"]
)
def test_a_missing_block_in_codex_is_found(place: str) -> None:
    import login_guard_kit as kit

    with kit.open_lab("codex") as lab:
        entries = kit.codex_entries(lab)[0]
        if place == "keychains":  # a second block: every `*.keychain-db` by name, in any folder
            entries = {key: mode for key, mode in entries.items() if "/**/" not in key}
        entries[str(lab.baits[place].parent)] = "read"
        rows = kit.in_codex(lab, kit.plan_for(lab, 9), "read", entries)
    assert [row for row in rows if row["target"] == place and row["reached"]]


@mac
@pytest.mark.parametrize(
    "place", ["codex_auth", "claude_folder", "codex_folder", "ssh", "keychains"]
)
def test_a_missing_block_in_claude_is_found(place: str) -> None:
    import login_guard_kit as kit

    with kit.open_lab("claude") as lab:
        folder = {"path": str(lab.baits[place].parent)}
        rows, _ = kit.in_claude(lab, "read", allow_in=folder)
    assert [row for row in rows if row["target"] == place and row["reached"]]


# --------------------------------------------------------------------------------------------
# A8: no process carries the secret, read through `KERN_PROCARGS2`
# --------------------------------------------------------------------------------------------


def sysctl_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    row = next(row for row in rows if row["way"].startswith("KERN_PROCARGS2"))
    assert int(row["note"].split()[0]) > 20, "the probe read no processes"
    return row


@mac
@pytest.mark.parametrize("kind", KINDS)
def test_no_process_carries_the_secret(kind: AuthKind) -> None:
    import login_guard_kit as kit

    with kit.open_lab("claude", kind) as lab:
        rows, api = kit.in_claude(lab, "environment")
    assert api.credential_seen > 0, "the CLI did not take the secret from the pipe"
    assert sysctl_row(rows)["reached"] is False
    assert reached(rows) == []


@mac
@pytest.mark.parametrize("kind", KINDS)
def test_the_secret_back_in_the_environment_is_found(kind: AuthKind) -> None:
    import login_guard_kit as kit

    with kit.open_lab("claude", kind) as lab:
        rows, _ = kit.in_claude(lab, "environment", leak=True)
    row = sysctl_row(rows)
    assert row["reached"] is True and "claude" in row["note"]


def _from_codex(kind: AuthKind, *, leak: bool) -> dict[str, Any]:
    """A Codex agent looks at a Claude agent of the same Mac, as in a mixed run."""
    import login_guard_kit as kit

    with kit.open_lab("claude", kind) as claude, kit.open_lab("codex") as codex:
        plan = kit.plan_for(codex, 9) | {"markers": [claude.token]}
        with kit.HeldTurn(claude, leak=leak) as held:
            rows = kit.in_codex(codex, plan, "environment")
        assert held.failure is None
    return sysctl_row(rows)


@mac
@pytest.mark.parametrize("kind", KINDS)
def test_a_codex_agent_finds_no_secret_in_the_real_cli(kind: AuthKind) -> None:
    assert _from_codex(kind, leak=False)["reached"] is False


@mac
@pytest.mark.parametrize("kind", KINDS)
def test_a_codex_agent_would_find_the_secret_in_the_environment(kind: AuthKind) -> None:
    row = _from_codex(kind, leak=True)
    assert row["reached"] is True and "claude" in row["note"]


@mac
def test_the_probe_gives_out_names_and_numbers_only(tmp_path: Path) -> None:
    """It reads real processes, so nothing of an environment may leave it (W3-06 §12)."""
    import login_guard_kit as kit

    marker = "ARR-GUARD-MARKER-only-a-test"
    other = "ARR-GUARD-OTHER-VALUE-only-a-test"
    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={"GUARD_MARKER": marker, "GUARD_OTHER": other, "PATH": "/usr/bin:/bin"},
    )
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"scratch": str(tmp_path), "markers": [marker], "token_names": []}),
    )
    try:
        time.sleep(0.2)
        done = subprocess.run(
            [sys.executable, str(kit.PROBE), str(plan), "environment"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    finally:
        holder.kill()
        holder.wait()
    assert marker not in done.stdout + done.stderr
    assert other not in done.stdout + done.stderr
    assert "GUARD_" not in done.stdout + done.stderr
    row = sysctl_row(json.loads(done.stdout))
    assert row["reached"] is True
    assert re.fullmatch(r"\d+ lesbar, Träger: \[('[\w.\- ]+'(, )?)+\]", row["note"])


@mac
def test_the_cli_still_knows_the_pipe_variables() -> None:
    from arr.auth import DESCRIPTOR_VARIABLES
    from arr.backends.claude_start import bundled_cli

    program = bundled_cli().read_bytes()
    assert all(name.encode() in program for name in DESCRIPTOR_VARIABLES.values())


# --------------------------------------------------------------------------------------------
# W3-06.3: what runs outside the sandbox never comes from a write root (B1, B2)
# --------------------------------------------------------------------------------------------


def _run_trapped(provider: str) -> dict[str, Any]:
    """The real CLI or app-server with stubs in every write root on a `PATH` and `arr/` in the
    working folder: two CLI starts with two turns each, or three app-servers (one per turn)."""
    import login_guard_kit as kit

    with kit.open_lab(provider) as lab:
        traps = kit.set_traps(lab)
        commands = kit.trap_commands(traps)
        if provider == "claude":
            for _ in range(2):
                kit.run_file_tools(lab, [kit.bash(command) for command in commands], turns=2)
        else:
            kit.run_codex_backend(lab, commands, turns=3)
        return {
            "outside": traps.outside_lines(),
            "inside": traps.inside_lines(),
            "shadow": traps.shadow.exists(),
            "prefix": f"prefix {lab.run_paths.venv_dir}",
            "probes": set(kit.PATH_PROBES.values()),
        }


@mac
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_nothing_of_a_write_root_runs_outside_the_sandbox(provider: str) -> None:
    result = _run_trapped(provider)
    assert result["outside"] == [], "a program of a write root ran without the sandbox"
    assert result["shadow"] is False, "the room server imported arr from the working folder"
    # the agent's commands keep their PATH: each source, and Python of the run venv
    assert result["probes"] <= set(result["inside"])
    assert result["prefix"] in result["inside"]


@mac
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_git_filter_of_the_agent_never_runs_outside(provider: str) -> None:
    """Found in W3-06.3: `git` of the app-server read `$HOME/.gitconfig` in Run-tmp."""
    import login_guard_kit as kit

    with kit.open_lab(provider) as lab:
        assert kit.run_git_filter(lab, provider) == []


@mac
def test_the_old_home_of_the_app_server_is_found() -> None:
    import login_guard_kit as kit

    # the later walls of W3-06.4 (read-only `.gitattributes`, no `git` outside) off as well
    with kit.old_home(), kit.no_protection(), kit.with_git(), kit.open_lab("codex") as lab:
        assert "filter" in kit.run_git_filter(lab, "codex")


class Allow:
    async def decide(self, item: Any) -> Decision:
        return Decision(action=DecisionAction.ALLOW, decided_by=DecidedBy.USER, reason="Test")


def _wheel(folder: Path, name: str) -> Path:
    """A tiny wheel of one package, so `pip install` needs no network."""
    import zipfile

    info = f"{name}-0.1.dist-info"
    files = {
        f"{name}/__init__.py": "",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n"
        "Tag: py3-none-any\n",
    }
    files[f"{info}/RECORD"] = "".join(f"{path},,\n" for path in [*files, f"{info}/RECORD"])
    wheel = folder / f"{name}-0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return wheel


@mac
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_pip_and_python_of_the_agent_use_the_run_venv(provider: str) -> None:
    import login_guard_kit as kit

    with kit.open_lab(provider, with_pip=True) as lab:
        wheel = _wheel(lab.agent.work_dir, "arrguardpkg")
        report = lab.profile.tmp_dir / "pip.log"
        python = "import arrguardpkg, sys; print(sys.prefix); print(arrguardpkg.__file__)"
        # `;`, not `&&`: under Codex pip installs, then fails to remove its temp folder (rmdir
        # is denied in the write roots, an older finding outside W3-06)
        command = (
            f"pip install --no-index --no-deps ./{wheel.name}; python3 -c {python!r} > {report}"
        )
        if provider == "claude":
            kit.run_file_tools(lab, [kit.bash(command)], Allow())
        else:
            kit.run_codex_backend(lab, [command])
        lines = report.read_text().splitlines() if report.exists() else []
        venv = str(lab.run_paths.venv_dir)
    assert lines and lines[0] == venv
    assert lines[1].startswith(venv + "/lib/")


@mac
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_the_old_way_is_found(provider: str) -> None:
    """The mutation: the agent's `PATH` for CLI and app-server, the room server without `-I`."""
    import login_guard_kit as kit

    with kit.old_way(), kit.with_git():  # and without the `git` stub of W3-06.4
        result = _run_trapped(provider)
    assert any(line.startswith("git ") for line in result["outside"])
    assert result["shadow"] is True


# --------------------------------------------------------------------------------------------
# W3-06.4: file tools never write more than Bash in the sandbox (N1, docs/07 §1)
# --------------------------------------------------------------------------------------------

IS_PROTECTED = SandboxProfile.is_protected  # the real check, also under `no_protection`


CODEX_TOP = {name.casefold() for name in CODEX_READ_ONLY_NAMES}


def _top_name(path: str, roots: list[str]) -> str | None:
    """The first name of `path` below its write root, case folded."""
    for root in roots:
        if path.startswith(root + "/"):
            return path[len(root) + 1 :].split("/")[0].casefold()
    return None


def _against_bash(provider: str) -> dict[str, Any]:
    """Bash tries `WRITE_CANDIDATES` in every write root, with the lab's repo and without one;
    each path Bash could not write goes to the `PolicyEngine` as a file tool and, under Codex,
    through the real `apply_patch` with a decider that allows everything."""
    import shutil

    import login_guard_kit as kit

    from arr.permissions.policy import PolicyEngine
    from arr.permissions.types import PermissionRequest

    tool = "Write" if provider == "claude" else "fileChange"
    with kit.open_lab(provider) as lab:
        first = [p for p in kit.WRITE_CANDIDATES if p not in (".git", ".GIT")]
        results = kit.bash_writes(lab, provider, first)
        git_status = results.pop("git status")
        patched = []
        if provider == "codex":
            blocked = [p for p, r in results.items() if r == "PermissionError"]
            patched += kit.apply_patches(lab, blocked, Allow())
        shutil.rmtree(lab.agent.work_dir / ".git")
        second = kit.bash_writes(lab, provider, [".git", ".GIT"])
        second.pop("git status")
        if provider == "codex":
            blocked = [p for p, r in second.items() if r == "PermissionError"]
            patched += kit.apply_patches(lab, blocked, Allow())
        results |= second
        engine = PolicyEngine([lab.profile])
        roots = [str(root) for root in lab.profile.write_roots]

        def refused(path: str) -> bool:
            request = PermissionRequest(
                run_id=lab.run.run_id, agent_id=lab.agent.id, tool=tool, paths=[path]
            )
            return engine.evaluate(request).action is DecisionAction.DENY

        blocked = [p for p, r in results.items() if r == "PermissionError"]
        return {
            "blocked": blocked,
            "written_by_tools": [p for p in blocked if not refused(p)],
            "protected_but_written": [
                p for p, r in results.items() if r == "ok" and IS_PROTECTED(lab.profile, Path(p))
            ],
            "top_protected_but_written": [
                p for p, r in results.items() if r == "ok" and _top_name(p, roots) in CODEX_TOP
            ],
            "patched": patched,
            "git_status": git_status,
        }


@mac
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_file_tools_write_nothing_that_bash_cannot(provider: str) -> None:
    """The fixed lists of the CLI and of Codex are compared with arr's one list, measured."""
    result = _against_bash(provider)
    assert result["blocked"], "the sandbox blocks nothing at all, the measurement is broken"
    assert result["written_by_tools"] == []
    assert result["patched"] == [], "apply_patch wrote what Bash cannot"
    if provider == "claude":  # arr's list reaches Bash as `denyWrite`, at every depth
        assert result["protected_but_written"] == []
    else:  # and the Codex profile as read-only entries at the top of each write root
        assert result["top_protected_but_written"] == []
    assert result["git_status"] == "0", "git in the sandbox still reads the repo"


@mac
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_a_missing_protection_is_found(provider: str) -> None:
    import login_guard_kit as kit

    with kit.no_protection():
        result = _against_bash(provider)
    assert result["written_by_tools"], "without the list a file tool writes what Bash cannot"
    if provider == "claude":
        assert result["protected_but_written"]
    else:  # the finding of W3-06.4: apply_patch changes `.git/config`
        assert any(p.endswith("/.git/config") for p in result["patched"])


@mac
def test_file_tools_of_the_cli_never_write_a_protected_name() -> None:
    """N3: Write and Edit of the real CLI, outside the sandbox, even when the user says yes."""
    import login_guard_kit as kit
    from fake_messages_api import ToolCall

    with kit.open_lab("claude") as lab:
        work = lab.agent.work_dir
        config = work / ".git" / "config"
        before = config.read_text()
        (work / "to-git").symlink_to(work / ".git")
        targets = [".gitattributes", ".gitmodules", ".GITMODULES", ".Git/hooks/x", "to-git/x"]
        script = [
            ToolCall("Read", {"file_path": str(config)}),
            ToolCall("Write", {"file_path": str(config), "content": before + "[x]\n"}),
            ToolCall(
                "Edit", {"file_path": str(config), "old_string": "[core]", "new_string": "[x]"}
            ),
            *[ToolCall("Write", {"file_path": str(work / t), "content": "x\n"}) for t in targets],
        ]
        api = kit.run_file_tools(lab, script, Allow())
        created = [t for t in targets if os.path.lexists(work / t)]
        changed = config.read_text() != before
    assert api.errors == [False] + [True] * (len(script) - 1)
    assert "nie geschrieben" in api.results[1]
    assert created == [] and not changed


@mac
def test_the_cli_writes_a_protected_name_without_the_list() -> None:
    import login_guard_kit as kit
    from fake_messages_api import ToolCall

    with kit.no_protection(), kit.open_lab("claude") as lab:
        target = lab.agent.work_dir / ".gitattributes"
        api = kit.run_file_tools(
            lab, [ToolCall("Write", {"file_path": str(target), "content": "x"})]
        )
        assert api.errors == [False] and target.exists()


@mac
def test_the_write_tool_lays_no_git_for_a_filter() -> None:
    """N1, the whole chain of `gitfile_e2e_claude.py`: the bait stays where it is."""
    import login_guard_kit as kit

    with kit.open_lab("claude") as lab:
        result = kit.claude_git_chain(lab)
        assert not result["git_laid"]
        assert not kit.stolen_bait(lab)


@mac
def test_the_chain_of_the_review_reaches_the_bait_without_the_walls() -> None:
    import login_guard_kit as kit

    with kit.no_protection(), kit.with_git(), kit.open_lab("claude") as lab:
        assert kit.claude_git_chain(lab)["git_laid"]
        assert kit.stolen_bait(lab)


@mac
def test_apply_patch_changes_no_protected_name() -> None:
    """3a: Codex's own file tool, even when the user says yes."""
    import login_guard_kit as kit

    with kit.open_lab("codex") as lab:
        work = lab.agent.work_dir
        paths = [str(work / p) for p in (".git/config", ".codex/config.toml", ".gitattributes")]
        paths += [str(work / ".GITATTRIBUTES"), str(lab.profile.tmp_dir / ".git" / "config")]
        allow = Allow()
        assert kit.apply_patches(lab, paths, allow) == []


@mac
def test_apply_patch_changes_git_config_without_the_walls() -> None:
    import login_guard_kit as kit

    with kit.no_protection(), kit.open_lab("codex") as lab:
        config = str(lab.agent.work_dir / ".git" / "config")
        assert kit.apply_patches(lab, [config], Allow()) == [config]


@mac
def test_a_filter_of_apply_patch_never_runs_outside() -> None:
    """3a/3b, the whole chain under Codex: filter in `.git/config`, `.gitattributes`, next turn."""
    import login_guard_kit as kit

    with kit.open_lab("codex") as lab:
        result = kit.codex_git_chain(lab)
        assert not result["config_changed"]
        assert not kit.stolen_bait(lab)


@mac
def test_the_codex_chain_reaches_the_bait_without_the_walls() -> None:
    import login_guard_kit as kit

    with kit.no_protection(), kit.with_git(), kit.open_lab("codex") as lab:
        assert kit.codex_git_chain(lab)["config_changed"]
        assert kit.stolen_bait(lab)


@mac
def test_a_changed_codex_folder_stops_the_agent(caplog: pytest.LogCaptureFixture) -> None:
    """3c: a project MCP server laid during the run never starts, even when arr's list of
    names misses it (fail closed): the agent starts no further thread."""
    import login_guard_kit as kit

    with kit.no_protection(), kit.no_name_list(), kit.open_lab("codex") as lab:
        result = kit.codex_mcp_chain(lab)
    assert result["laid"] and not result["ran"]
    assert caplog.text.count("seit dem Start verändert") == 2, "turns 2 and 3 refused"


@mac
def test_the_mcp_server_of_the_agent_runs_without_the_check() -> None:
    import login_guard_kit as kit

    with kit.no_protection(), kit.no_project_check(), kit.open_lab("codex") as lab:
        result = kit.codex_mcp_chain(lab)
    assert result["laid"] and result["ran"]


# --------------------------------------------------------------------------------------------
# W3-06.5: a file change runs outside the sandbox, so the request names every path (N5)
# --------------------------------------------------------------------------------------------


@mac
def test_apply_patch_move_is_refused() -> None:
    """`*** Move to:` whose target would leave the sandbox is refused: outside every write
    root and on the block list. The source stays, nothing is written at the target (N5).

    A move whose target is a plain path inside a write root is not refused here: Codex applies
    it without asking arr (`test_an_in_workspace_move_is_not_surfaced_to_arr`). It stays inside
    the write roots, so it is no escape; the block-list targets below N7 are the known limit."""
    import login_guard_kit as kit

    with kit.open_lab("codex") as lab:
        work = lab.agent.work_dir
        (work / "m1.txt").write_text("eins\n")
        (work / "m3.txt").write_text("eins\n")
        outside = lab.base / "outside.txt"  # outside every write root
        config = work / ".git" / "config"  # on the block list, already there
        before = config.read_text()
        kit.apply_raw_patches(
            lab, [kit.move_patch("m1.txt", str(outside)), kit.move_patch("m3.txt", str(config))]
        )
        assert not outside.exists(), "moved outside every write root"
        assert config.read_text() == before, "move overwrote .git/config"
        assert (work / "m1.txt").read_text() == "eins\n", "source m1 changed"
        assert (work / "m3.txt").read_text() == "eins\n", "source m3 changed"


@mac
def test_an_in_workspace_move_is_not_surfaced_to_arr() -> None:
    """A move to a plain path inside a write root triggers no approval, so arr cannot deny it.
    Documented, not a guard test: it never leaves the write roots and is no escape (Punkt 4
    limit, reported to the Lead)."""
    import login_guard_kit as kit

    from arr.backends import codex_approvals as ca

    calls: list[str] = []
    original = ca.ApprovalBridge.__call__
    ca.ApprovalBridge.__call__ = lambda self, m, p: (calls.append(m), original(self, m, p))[1]
    try:
        with kit.open_lab("codex") as lab:
            work = lab.agent.work_dir
            (work / "m.txt").write_text("eins\n")
            target = work / "sub" / "moved.txt"
            kit.apply_raw_patches(lab, [kit.move_patch("m.txt", str(target))])
            assert target.exists() and calls == [], "in-workspace move asked arr after all"
    finally:
        ca.ApprovalBridge.__call__ = original


@mac
def test_a_move_outside_succeeds_without_the_fix() -> None:
    import login_guard_kit as kit

    with kit.old_file_request(), kit.open_lab("codex") as lab:
        work = lab.agent.work_dir
        (work / "m.txt").write_text("eins\n")
        target = lab.base / "outside.txt"
        kit.apply_raw_patches(lab, [kit.move_patch("m.txt", str(target))])
        assert target.exists(), "the countertest should let the move through"


@mac
def test_apply_patch_delete_is_refused_outside_and_on_the_block_list() -> None:
    """`*** Delete File:` is refused outside every write root and on the block list."""
    import login_guard_kit as kit

    with kit.open_lab("codex") as lab:
        work = lab.agent.work_dir
        outside = lab.base / "keep.txt"
        outside.write_text("eins\n")
        config = work / ".git" / "config"
        before = config.read_text()
        kit.apply_raw_patches(lab, [kit.delete_patch(str(outside)), kit.delete_patch(str(config))])
        assert outside.exists(), "deleted a file outside every write root"
        assert config.read_text() == before, "deleted .git/config"


@mac
def test_delete_on_the_block_list_succeeds_without_the_protection() -> None:
    import login_guard_kit as kit

    with kit.no_protection(), kit.open_lab("codex") as lab:
        config = lab.agent.work_dir / ".git" / "config"
        kit.apply_raw_patches(lab, [kit.delete_patch(str(config))])
        assert not config.exists(), "the countertest should delete .git/config"


def _bridge(lab: Any, item_changes: list[dict[str, Any]], **params: Any) -> Any:
    """An `ApprovalBridge` with one observed file item, to call `_file_request` directly.

    grantRoot and an unknown change arr cannot be produced by a fake model, so they are
    checked on the bridge, where they are decided (W3-06.5, Punkt 2 and 3)."""
    from arr.backends.codex_approvals import ApprovalBridge

    bridge = ApprovalBridge(
        inbox=None,
        run_id=lab.run.run_id,
        agent_id=lab.agent.id,
        role=None,
        work_dir=str(lab.agent.work_dir),
        loop=None,  # _file_request never touches the loop or the inbox
        on_denied=lambda _reason: None,
    )
    bridge._items["i1"] = {"type": "fileChange", "id": "i1", "changes": item_changes}
    return bridge._file_request({"itemId": "i1", **params})


@mac
def test_a_file_request_with_a_grant_root_is_refused() -> None:
    """Every `grantRoot` is refused, even one that points into the work folder (Punkt 3)."""
    import login_guard_kit as kit

    from arr.permissions.types import Decision, DecisionAction

    with kit.open_lab("codex") as lab:
        work = str(lab.agent.work_dir)
        change = {"path": f"{work}/a.txt", "kind": {"type": "update"}, "diff": "x"}
        result = _bridge(lab, [change], grantRoot=work)
        assert isinstance(result, Decision) and result.action is DecisionAction.DENY
        assert "grantRoot" in result.reason
        # without the fix a grantRoot into the work folder is allowed
        with kit.old_file_request():
            request = _bridge(lab, [change], grantRoot=work)
        assert not isinstance(request, Decision)


@mac
def test_a_file_request_arr_cannot_read_is_refused() -> None:
    """An unknown change kind, an unknown field and an unreadable change are refused (Punkt 2)."""
    import login_guard_kit as kit

    from arr.permissions.types import Decision, DecisionAction

    with kit.open_lab("codex") as lab:
        work = str(lab.agent.work_dir)

        def denied(changes: list[dict[str, Any]], **params: Any) -> bool:
            result = _bridge(lab, changes, **params)
            return isinstance(result, Decision) and result.action is DecisionAction.DENY

        good = {"path": f"{work}/a.txt", "kind": {"type": "add"}, "diff": "x"}
        assert not isinstance(_bridge(lab, [good]), Decision)  # a plain add is allowed to ask
        assert denied([{"path": f"{work}/a.txt", "kind": {"type": "rename"}, "diff": "x"}])
        assert denied([{"path": f"{work}/a.txt", "kind": {"type": "update", "secret": "x"}}])
        assert denied([{"path": f"{work}/a.txt", "kind": {"type": "add"}, "diff": "x", "extra": 1}])
        assert denied([{"kind": {"type": "add"}, "diff": "x"}])  # no path
        assert denied([good], unknownField="/etc/x")  # a new param that could carry a path


def _node_cache_after_two_turns() -> bool:
    import login_guard_kit as kit

    with kit.open_lab("claude") as lab:
        kit.run_file_tools(lab, [kit.bash("echo eins"), kit.bash("echo zwei")], turns=2)
        return (lab.profile.tmp_dir / "node-compile-cache").exists()


@mac
def test_no_compile_cache_of_node_in_run_tmp() -> None:
    """Punkt 5: `npm` of the CLI runs outside the sandbox; Run-tmp is its TMPDIR."""
    assert not _node_cache_after_two_turns()


@mac
def test_node_keeps_its_compile_cache_in_run_tmp_without_the_switch() -> None:
    import login_guard_kit as kit

    with kit.with_node_cache():
        assert _node_cache_after_two_turns()


# --------------------------------------------------------------------------------------------
# W3-06.4, 3b: no `git` outside the sandbox
# --------------------------------------------------------------------------------------------


@mac
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_no_git_runs_outside_the_sandbox(provider: str) -> None:
    """A repo with filters that a project brings along: CLI and app-server run `git` only
    through the stub, the agent's shell keeps the real one."""
    import login_guard_kit as kit

    with kit.open_lab(provider) as lab:
        result = kit.run_with_filter_repo(lab, provider)
    assert result["outside"] == 0, "a git outside the sandbox ran a filter"
    assert result["inside"] and set(result["inside"]) == {"0"}, "git in the shell works"


@mac
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_git_outside_runs_the_filter_without_the_stub(provider: str) -> None:
    import login_guard_kit as kit

    with kit.with_git(), kit.open_lab(provider) as lab:
        assert kit.run_with_filter_repo(lab, provider)["outside"] > 0


# --------------------------------------------------------------------------------------------
# File tools of the CLI: they run outside the sandbox
# --------------------------------------------------------------------------------------------


@mac
def test_file_tools_do_not_follow_a_link_to_a_login() -> None:
    import login_guard_kit as kit
    from fake_messages_api import ToolCall

    with kit.open_lab("claude") as lab:
        work = lab.agent.work_dir
        outside = lab.home / ".ssh" / "authorized_keys"
        start = lab.start_folder / "claude-start-guard"
        kit.guard_start(lab)
        text = start.read_text()
        (work / "to-bait").symlink_to(lab.baits["ssh"])
        (work / "to-new").symlink_to(outside)
        (work / "to-start").symlink_to(start)
        script = [
            ToolCall("Read", {"file_path": str(work / "to-bait")}),
            ToolCall("Grep", {"pattern": "ARR", "path": str(work / "to-bait")}),
            ToolCall("Write", {"file_path": str(work / "to-new"), "content": "x\n"}),
            ToolCall("Write", {"file_path": str(work / "to-start"), "content": "x\n"}),
            ToolCall(
                "Edit",
                {"file_path": str(work / "to-start"), "old_string": "exec", "new_string": "x"},
            ),
        ]
        api = kit.run_file_tools(lab, script)
        assert len(api.results) == len(script) and all(api.errors)
        assert not any(lab.bait in text_ for text_ in api.results)
        assert not outside.exists()
        assert start.read_text() == text


class Refuse:
    """A decider that keeps what it was asked and says no."""

    def __init__(self) -> None:
        self.items: list[Any] = []

    async def decide(self, item: Any) -> Decision:
        self.items.append(item)
        return Decision(action=DecisionAction.DENY, decided_by=DecidedBy.USER, reason="Test")


@mac
def test_pip_install_of_the_real_cli_asks_as_an_installation() -> None:
    """What the live smoke test checks, without a model: asked before it runs, as Bash."""
    import login_guard_kit as kit

    refuse = Refuse()
    with kit.open_lab("claude") as lab:
        api = kit.run_file_tools(lab, [kit.bash("pip install six")], refuse)
    assert api.errors == [True], "denied, so it never ran"
    [item] = refuse.items
    assert item.request.tool == "Bash" and item.request.command == "pip install six"
    assert RequestCategory.INSTALL in item.request.categories


@mac
@pytest.mark.parametrize("hold", [(0.0005, 0.0005), (0.003, 0.0005), (0.015, 0.003)])
def test_file_tools_lose_the_race_against_a_changing_link(hold: tuple[float, float]) -> None:
    """The link stays harmless and turns for a moment, between the check and the access."""
    import login_guard_kit as kit
    from fake_messages_api import ToolCall

    rounds = 60
    with kit.open_lab("claude") as lab:
        work = lab.agent.work_dir
        safe = work / "safe.txt"
        safe.write_text("harmlos\n" * 200)
        outside = lab.home / ".ssh" / "authorized_keys"
        (work / "flip.py").write_text(kit.FLIP_PROGRAM)
        plans = {
            "r": [str(work / "flip-r"), str(safe), str(lab.baits["ssh"]), *hold, 30],
            "w": [str(work / "flip-w"), str(work / "new.txt"), str(outside), *hold, 30],
        }
        for name, plan in plans.items():
            (work / f"{name}.json").write_text(json.dumps(plan))
        read = [
            ToolCall("Read", {"file_path": plans["r"][0], "limit": 1 + number})
            for number in range(rounds)
        ]
        grep = [
            ToolCall("Grep", {"pattern": "ARR|harmlos", "path": plans["r"][0]})
            for _ in range(rounds // 2)
        ]
        write = [
            ToolCall("Write", {"file_path": plans["w"][0], "content": f"{number}\n"})
            for number in range(rounds)
        ]
        script = [kit.bash("python3 flip.py r.json"), *read, *grep]
        script += [kit.bash("python3 flip.py w.json"), *write]
        api = kit.run_file_tools(lab, script)
        assert len(api.results) == len(script)
        assert not any(lab.bait in text for text in api.results)
        assert not outside.exists()
        assert lab.bait in lab.baits["ssh"].read_text()
        reads = api.results[1 : 1 + rounds]
        assert any("harmlos" in text for text in reads), "the harmless side was never read"
        assert any(error for error in api.errors[1 : 1 + rounds]), "the link never turned"
