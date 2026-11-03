"""The login guard (W3-06): agents do not get to the logins, checked again with every update.

The sandbox tests run the probe (`login_guard_probe.py`) in the real sandboxes of both
backends, with arr's real profiles and a test `HOME` full of baits (`login_guard_kit.py`). No
model and no real login: Codex runs through `codex sandbox`, Claude through the real CLI
against `fake_messages_api.py`. Only `reached` flags and counts leave the probe.

The version guard runs everywhere, also in the CI: no update passes without a new probe.
"""

import importlib.metadata
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk._cli_version import __cli_version__

from arr.models import AuthKind
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
REAL_HOME_PROBE = "spikes/w3_06/real_home_probe.py"


def version_problems(recorded: dict[str, str], installed: dict[str, str]) -> list[str]:
    return [
        f"{name}: Sperr-Probe zuletzt grün mit {recorded.get(name)}, installiert ist "
        f"{installed.get(name)}. Lass die Probe auf dem Mac laufen (uv run pytest -m macos "
        f"tests/test_login_guard.py, darin auch {OUTSIDE_TEST}), dazu die Probe mit echtem HOME "
        f"({REAL_HOME_PROBE}), und aktualisiere dann tests/fixtures/login_guard.json."
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
    assert OUTSIDE_TEST in problems[0] and REAL_HOME_PROBE in problems[0]
    assert version_problems({"a": "1"}, {}) and version_problems({}, {"a": "1"})


def test_the_guard_names_what_exists() -> None:
    assert (Path(__file__).parents[1] / REAL_HOME_PROBE).is_file()
    assert callable(globals().get(OUTSIDE_TEST))


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

    with kit.old_home(), kit.open_lab("codex") as lab:
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

    with kit.old_way():
        result = _run_trapped(provider)
    assert any(line.startswith("git ") for line in result["outside"])
    assert result["shadow"] is True


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
