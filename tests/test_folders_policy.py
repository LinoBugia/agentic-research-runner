"""W4b-05a: read rules for `retired/`, `scratch/` and the agent's own `agents/<id>/` (A4–A7).

The engine is the same for both backends (the Claude hooks and file tools, the Codex
approvals); the sandbox lists are checked as data here and in the lab in
`test_folders_probe.py`.
"""

import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
from run_kit import make_run_input, make_settings, make_workfolder

from arr.backends.claude_options import shell_env_file
from arr.backends.codex_config import build_launch, filesystem_entries
from arr.models import AgentConfig, Toolchain
from arr.permissions import policy
from arr.permissions.policy import (
    AlwaysRule,
    PermissionRules,
    PolicyEngine,
    RunPaths,
    SandboxProfile,
    claude_options,
    profile_for,
)
from arr.permissions.types import DecidedBy, DecisionAction, PermissionRequest
from arr.resolve import resolve_run
from arr.runs.prepare import PreparedRun, prepare_run

ALLOW, DENY, ASK = DecisionAction.ALLOW, DecisionAction.DENY, DecisionAction.ASK

OWN_LOG = [
    "transcript.jsonl",
    "raw.jsonl",
    "blobs/bild.png",
    "session.json",
    "transcript.md",
    "changes.diff",
    "retired-001-p.diff",
    "agent.json",
    "role.md",
    "context.md",
    "shell-env.sh",
    "home/.gitconfig",
    "spaeter-angelegt.txt",  # a file that appears only later
    "",  # the folder itself
]


class Setup:
    def __init__(self, root: Path) -> None:
        self.home = Path(os.path.realpath(root)) / "home"
        self.workfolder = self.home / "wf"
        self.run = self.workfolder / "runs" / "r1"
        for agent_id in ("a1", "a2"):
            (self.run / "agents" / agent_id / "work").mkdir(parents=True)
            (self.run / "agents" / agent_id / "plugin").mkdir()
            (self.run / "scratch" / agent_id).mkdir(parents=True)
        (self.run / "retired" / "a1" / "001-p").mkdir(parents=True)
        self.run_paths = RunPaths(self.workfolder, self.run, Toolchain())

    def agent(self, agent_id: str = "a1", provider: str = "claude") -> AgentConfig:
        model = "claude-sonnet-5-5" if provider == "claude" else "gpt-6.1-sol"
        return AgentConfig.model_validate(
            {
                "id": agent_id,
                "base_id": agent_id,
                "provider": provider,
                "model": model,
                "auth": "p",
                "channels": ["main"],
            }
        )

    def profile(self, agent_id: str = "a1", rules: PermissionRules | None = None) -> SandboxProfile:
        return profile_for(
            self.agent(agent_id), self.run_paths, rules, home=self.home, case_insensitive=True
        )

    def engine(self, agent_id: str = "a1", rules: PermissionRules | None = None) -> PolicyEngine:
        return PolicyEngine([self.profile(agent_id, rules)])


@pytest.fixture
def setup(tmp_path: Path, arr_home: Path) -> Setup:
    return Setup(tmp_path)


def req(tool: str, agent_id: str = "a1", **fields: Any) -> PermissionRequest:
    return PermissionRequest(run_id="r1", agent_id=agent_id, tool=tool, **fields)


def every_way(path: Path, agent_id: str = "a1") -> list[PermissionRequest]:
    """Read, Grep, Glob, the shell of both backends, Write, Edit and a Codex file change."""
    paths = [str(path)]
    return [
        req("Read", agent_id, paths=paths),
        req("Grep", agent_id, paths=paths),
        req("Glob", agent_id, paths=paths),
        req("Bash", agent_id, command=f"cat {path}"),
        req("commandExecution", agent_id, command=f"head -c 10 {path}", provider="codex"),
        req("Write", agent_id, paths=paths),
        req("Edit", agent_id, paths=paths),
        req("fileChange", agent_id, paths=paths, provider="codex"),
    ]


def rules_for(*paths: Path) -> PermissionRules:
    return PermissionRules(always=[AlwaysRule(action="allow", paths=[str(p) for p in paths])])


# --------------------------------------------------------------------------------------------
# A6: the agent's own folder, except work/ and plugin/: a ban without a question
# --------------------------------------------------------------------------------------------


def test_a6_own_transcript_events_and_files_of_arr_are_banned_without_a_question(
    setup: Setup,
) -> None:
    own = setup.run / "agents" / "a1"
    for rules in (None, rules_for(setup.run), rules_for(own / "work"), rules_for(own)):
        engine = setup.engine(rules=rules)
        for rel in OWN_LOG:
            for request in every_way(own / rel if rel else own):
                decision = engine.evaluate(request)
                assert decision.action is DENY, (rel, request.tool, rules)
                assert decision.decided_by is DecidedBy.FIXED_RULE, (rel, request.tool)
    reason = setup.engine().evaluate(req("Read", paths=[str(own / "transcript.jsonl")])).reason
    assert "eigenen Agenten" in reason and "anderen Agenten" not in reason
    # also case-insensitively, as the other rules
    upper = str(own / "TRANSCRIPT.JSONL").replace("/agents/", "/AGENTS/")
    assert setup.engine().evaluate(req("Read", paths=[upper])).action is DENY


def test_a6_counter_check_work_is_never_asked_and_the_plugin_stays_readable(
    setup: Setup,
) -> None:
    own = setup.run / "agents" / "a1"
    engine = setup.engine()
    for request in every_way(own / "work" / "notizen.md")[:4] + [
        req("Write", paths=[str(own / "work" / "neu.md")]),
        req("Edit", paths=[str(own / "work" / "neu.md")]),
    ]:
        if request.provider == "codex":
            continue  # a Codex shell request outside the sandbox is always asked (H2)
        assert engine.evaluate(request).action is ALLOW, request.tool
    skill = own / "plugin" / "skills" / "x" / "SKILL.md"
    assert engine.evaluate(req("Read", paths=[str(skill)])).action is ALLOW
    assert engine.evaluate(req("Grep", paths=[str(own / "plugin")])).action is ALLOW
    assert engine.evaluate(req("Write", paths=[str(skill)])).action is DENY
    # counter-check: the rule is what bans it; the profile of before (own = agents/<id>/)
    # gave the old question
    profile = setup.profile()
    old = replace(profile, own=(*profile.own, own))
    decision = PolicyEngine([old]).evaluate(req("Read", paths=[str(own / "transcript.jsonl")]))
    assert decision.action is ASK


def test_a6_another_agent_reads_none_of_it_either(setup: Setup) -> None:
    other = setup.run / "agents" / "a1"
    engine = setup.engine("a2", rules_for(setup.run))
    for rel in [*OWN_LOG, "work/notizen.md", "plugin/skills/x/SKILL.md"]:
        for request in every_way(other / rel, "a2"):
            assert engine.evaluate(request).action is DENY, (rel, request.tool)


# --------------------------------------------------------------------------------------------
# A4: retired/ is closed for every agent, also the former owner
# --------------------------------------------------------------------------------------------


def test_a4_retired_is_closed_for_every_agent_also_under_a_rule_for_the_run(setup: Setup) -> None:
    bait = setup.run / "retired" / "a1" / "001-p" / "notizen.md"
    for agent_id in ("a1", "a2"):
        for rules in (None, rules_for(setup.run), rules_for(setup.run / "retired")):
            engine = setup.engine(agent_id, rules)
            for target in (bait, bait.parent, setup.run / "retired"):
                for request in every_way(target, agent_id):
                    decision = engine.evaluate(request)
                    assert decision.action is DENY, (agent_id, target, request.tool)
                    assert decision.decided_by is DecidedBy.FIXED_RULE
    reason = setup.engine().evaluate(req("Read", paths=[str(bait)])).reason
    assert "abgelegter Arbeitsordner" in reason
    # a search over the run would walk into it
    assert setup.engine().evaluate(req("Grep", paths=[str(setup.run)])).action is DENY


# --------------------------------------------------------------------------------------------
# A5: scratch folder
# --------------------------------------------------------------------------------------------


def test_a5_own_scratch_is_read_and_write_another_agents_is_closed(setup: Setup) -> None:
    own = setup.run / "scratch" / "a1" / "daten.bin"
    other = setup.run / "scratch" / "a2" / "daten.bin"
    engine = setup.engine(rules=rules_for(setup.run))
    for tool in ("Read", "Grep", "Write", "Edit"):
        assert engine.evaluate(req(tool, paths=[str(own)])).action is ALLOW, tool
    assert engine.evaluate(req("Bash", command=f"python3 x.py > {own}")).action is ALLOW
    for request in every_way(other):
        assert engine.evaluate(request).action is DENY, request.tool
    reason = engine.evaluate(req("Read", paths=[str(other)])).reason
    assert "anderen Agenten" in reason
    profile = setup.profile()
    assert profile.scratch_dir == setup.run / "scratch" / "a1"
    assert profile.scratch_dir in profile.write_roots


def prepared_run(root: Path, arr_home: Path) -> PreparedRun:
    from run_kit import write_profiles

    write_profiles(arr_home, {"fake-auth": "claude_oauth", "fake-codex": "codex_chatgpt"})
    workfolder = make_workfolder(root)
    run_input = make_run_input(
        [
            {"id": "c1"},
            {"id": "x1", "provider": "codex", "model": "gpt-6.1-sol", "auth": "fake-codex"},
        ]
    )
    settings = make_settings()
    return prepare_run(workfolder, run_input, resolve_run(run_input, None, settings), settings)


def test_a5_arr_scratch_is_set_in_the_shell_of_both_backends(
    tmp_path: Path, arr_home: Path
) -> None:
    run = prepared_run(tmp_path, arr_home)
    run_paths = RunPaths(run.run_dir.parent.parent, run.run_dir, make_settings().toolchain)
    for agent_id in ("c1", "x1"):
        agent = run.agents[agent_id]
        scratch = run.run_dir / "scratch" / agent_id
        assert scratch.is_dir() and (scratch.stat().st_mode & 0o777) == 0o700
        profile = profile_for(agent.file.config, run_paths, PermissionRules())
        if agent_id == "c1":
            text = shell_env_file(agent, profile, "/usr/bin:/bin").read_text()
            assert f"export ARR_SCRATCH={scratch}\n" in text
        else:
            launch = build_launch(
                agent,
                profile,
                run_paths,
                codex_home=tmp_path / "codex-home",
                environ={"PATH": "/usr/bin:/bin"},
            )
            assert launch.shell_env["ARR_SCRATCH"] == str(scratch)
            assert "ARR_SCRATCH" not in launch.env, "only the commands need it"


# --------------------------------------------------------------------------------------------
# A7: sizes of the Codex profile; options unchanged but for the new folders
# --------------------------------------------------------------------------------------------


def _old_profile(profile: SandboxProfile, run: Path) -> SandboxProfile:
    """The profile as before W4b-05a: no scratch folder, `own` = `agents/<id>/`."""
    new = {run / "scratch", run / "retired"}
    scratch = profile.scratch_dir
    return replace(
        profile,
        write_roots=tuple(p for p in profile.write_roots if p != scratch),
        read_allow=tuple(p for p in profile.read_allow if p != scratch),
        read_deny=tuple(p for p in profile.read_deny if p not in new),
        private_roots=tuple(p for p in profile.private_roots if p not in new),
        own=(run / "agents" / profile.agent_id, profile.work_dir, profile.tmp_dir),
        scratch_dir=None,
        agent_dir=None,
        retired_dir=None,
    )


def _without_new(value: Any, run: Path) -> Any:
    marks = (str(run / "scratch"), str(run / "retired"))
    if isinstance(value, dict):
        return {k: _without_new(v, run) for k, v in value.items() if not str(k).startswith(marks)}
    if isinstance(value, list):
        return [_without_new(v, run) for v in value if not str(v).startswith(marks)]
    if isinstance(value, str) and value.startswith(marks):
        return None
    return value


def test_a7_options_are_unchanged_but_for_the_new_folders(setup: Setup) -> None:
    for rules in (None, rules_for(setup.run)):
        profile = setup.profile(rules=rules)
        old = _old_profile(profile, setup.run)
        new_claude = json.loads(json.dumps(asdict(claude_options(profile)), default=str))
        old_claude = json.loads(json.dumps(asdict(claude_options(old)), default=str))
        assert _without_new(new_claude, setup.run) == old_claude
        added = set(json.dumps(new_claude).split('"')) - set(json.dumps(old_claude).split('"'))
        assert added and all("/scratch" in a or "/retired" in a for a in added), added
        new_codex = filesystem_entries(profile, setup.run_paths, ["main"])
        old_codex = filesystem_entries(old, setup.run_paths, ["main"])
        assert _without_new(new_codex, setup.run) == old_codex
        assert {k: v for k, v in new_codex.items() if k not in old_codex} == {
            str(setup.run / "scratch"): "deny",
            str(setup.run / "retired"): "deny",
            str(setup.run / "scratch" / "a1"): "write",
            **{str(setup.run / "scratch" / "a1" / n): "read" for n in policy.CODEX_READ_ONLY_NAMES},
        }


def test_a7_the_codex_profile_stays_within_its_limits_with_many_agents_and_channels(
    arr_home: Path,
) -> None:
    """The new entries are a fixed number per agent: no entry per other agent, so the profile
    does not grow with the agents (review NP2B-G3). A run with 2 and one with 150 agent
    folders on disk (long IDs) give the same number of entries; with 60 channels of the run,
    each in the deny list, it stays within the bounds, and the new folders add 9 entries,
    each as long as its path (review G4). The run lies below a short fixed folder, so the
    result does not depend on where pytest keeps its temp folders (review NP2-G2)."""
    import shutil
    import tempfile

    from arr.backends import codex_config

    long_id = "explorer-" + "x" * 50
    channels = [f"kanal-{n:03d}-{'y' * 30}" for n in range(60)]
    root = Path(tempfile.mkdtemp(prefix="a7-", dir="/tmp"))
    try:
        counts: dict[int, int] = {}
        checks: list[tuple[int, int, int, int]] = []  # entries, size, new entries, new bytes
        for many in (2, 150):
            setup = Setup(root / f"n{many:03d}")
            ids = [f"{long_id}-{n}" for n in range(many)]
            for agent_id in ids:
                (setup.run / "agents" / agent_id / "work").mkdir(parents=True)
                (setup.run / "scratch" / agent_id).mkdir(parents=True)
            agent = setup.agent(ids[0], "codex")
            profile = profile_for(agent, setup.run_paths, rules_for(setup.run), home=setup.home)
            entries = filesystem_entries(profile, setup.run_paths, ["main"], run_channels=channels)
            old = filesystem_entries(
                _old_profile(profile, setup.run), setup.run_paths, ["main"], run_channels=channels
            )
            size = len(codex_config.toml_value(entries).encode())
            counts[many] = len(entries)
            extra = size - len(codex_config.toml_value(old).encode())
            checks.append((len(entries), size, len(entries) - len(old), extra))
        assert counts[2] == counts[150], counts  # no entry per other agent
        per_entry = len(str(setup.run / "scratch" / ids[0])) + len(".gitattributes") + 30
        for entries_n, size, new_entries, extra in checks:
            assert entries_n < codex_config.ENTRIES_WARN
            assert size < codex_config.PROFILE_MAX_BYTES
            assert new_entries == 2 + 1 + len(policy.CODEX_READ_ONLY_NAMES)
            # each new entry carries the full path (review G4, accepted, Entscheidung 56)
            assert extra < 9 * per_entry, (extra, per_entry)
    finally:
        shutil.rmtree(root)


# --------------------------------------------------------------------------------------------
# W4b-05a.1, Entscheidung 55: way 9 closed in the Claude sandbox lists
# --------------------------------------------------------------------------------------------


def test_e55_a_rule_by_hand_puts_no_ban_into_allow_read(setup: Setup) -> None:
    """A read rule written by hand for a folder that is a ban for the agent never reaches
    `allowRead` of the Claude sandbox; one for a harmless folder outside still does."""
    run = setup.run
    for other in ("a2", "judge-x"):
        (run / "tmp" / other).mkdir(parents=True, exist_ok=True)
    banned = {
        "own_agent_dir": run / "agents" / "a1",
        "own_transcript": run / "agents" / "a1" / "transcript.jsonl",
        "retired_id": run / "retired" / "a1",
        "retired_folder": run / "retired" / "a1" / "001-p",
        "other_scratch": run / "scratch" / "a2",
        "other_agent": run / "agents" / "a2",
        "other_work": run / "agents" / "a2" / "work",
        "other_tmp": run / "tmp" / "a2",
    }
    harmless = setup.home / "Literatur"
    profile = setup.profile(rules=rules_for(*banned.values(), harmless))
    allow = claude_options(profile).sandbox["filesystem"]["allowRead"]
    own = {str(profile.work_dir), str(run / "agents" / "a1" / "plugin")}  # from the profile
    for name, path in banned.items():
        opened = [p for p in allow if p == str(path) or p.startswith(f"{path}/")]
        assert not set(opened) - own, name
    assert str(harmless) in allow
    assert str(profile.work_dir) in allow and str(profile.scratch_dir) in allow
