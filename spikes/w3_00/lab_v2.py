"""Offline check of variant 2 with the adapter's own profile (no model, no login).

Builds the lab (`common.build_lab`: real run folder, `create_run_venv`, `RoomStore`), takes the
permission profile from `build_launch` and runs each probe with `codex sandbox -P arr` in the
agent's working folder, with the shell environment the agent gets (`launch.shell_env`).
Files that appear after the profile was written ("during the turn") are created between
writing the profile and running the probes; Codex compiles the profile for every command.
Real sensitive paths and the login folder are probed by exit code only.

Then the time per command: `/usr/bin/true` with the variant-2 profile, compared with a
variant-1-sized profile (the same entries plus 250 denied siblings of 115 characters, as in
`spikes/w2_02_codex/lab_scale.py`), median of 7 runs each.

    uv run python spikes/w3_00/lab_v2.py
"""

import os
import secrets
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

from codex_cli_bin import bundled_codex_path
from common import (
    HOME,
    OUT,
    Lab,
    build_lab,
    canary,
    codex_auth,
    dump,
    remove_cleanup,
    shown,
)

from arr.backends.codex_config import PERMISSION_PROFILE, build_launch, codex_home_for, toml_value
from arr.permissions.policy import PermissionRules, PolicyEngine, claude_options, profile_for
from arr.permissions.types import DecisionAction, PermissionRequest
from arr.venvs import base_venv_dir, venv_root

SBX = Path(__file__).resolve().parent / "tmp" / "sbx_v2"
TEX = "\\documentclass{article}\\begin{document}$1+1=2$\\end{document}"
PDFLATEX = "TEXMFVAR=$TMPDIR/texmf pdflatex -interaction=nonstopmode"
RUNS = 7


def write_config(folder: Path, entries: dict[str, str]) -> None:
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    lines = [f'default_permissions = "{PERMISSION_PROFILE}"']
    lines += [f"[permissions.{PERMISSION_PROFILE}.filesystem]"]
    lines += [f"{toml_value(k)} = {toml_value(v)}" for k, v in entries.items()]
    (folder / "config.toml").write_text("\n".join(lines) + "\n")


def sandbox(cwd: str, env: dict[str, str], command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(bundled_codex_path()), "sandbox", "-P", PERMISSION_PROFILE, "-C", cwd, "--"] + command,
        env=env,
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def late_files(lab: Lab, *, create: bool = True) -> dict[str, Path]:
    """Files that appear after the profile was built (new names in `~`, `runs/` …)."""
    tag = secrets.token_hex(3)
    runs = lab.workfolder / "runs"
    late = {
        "late_home": HOME / f"arr-w3-00-late-{tag}.txt",
        "late_outer": lab.root / f"late-{tag}" / "x.txt",
        "late_run": runs / f"20261029-2359_new_custom_{tag[:4]}" / "meta.json",
        "late_in_run": lab.run.run_dir / f"late-{tag}.txt",
        "late_agent": lab.run.run_dir / "agents" / "spawned-1" / "transcript.jsonl",
        "late_venv": venv_root() / "runs" / f"20261029-2359_new_custom_{tag[:4]}" / "c.txt",
        "late_venv_longer": venv_root() / "runs" / (lab.run.run_id + "x") / "c.txt",
        "late_venv_shorter": venv_root() / "runs" / lab.run.run_id[:-1] / "c.txt",
        "late_base_venv": base_venv_dir(f"w3-00-late-{tag}") / "c.txt",
    }
    if create:
        create_late(late)
    lab.cleanup += [late["late_home"]]
    lab.cleanup += [late[n].parent for n in late if "venv" in n]
    return late


def create_late(late: dict[str, Path]) -> None:
    for path in late.values():
        canary(path)


def cases(lab: Lab, late: dict[str, Path], login: Path) -> list[tuple[str, str, str]]:
    """(expected, label, command): ✅ must work, ❌ must fail, ⚠️ known gap (reported)."""
    c = lab.canaries
    run = lab.run.run_dir
    readdir = lab.root / "readdir"
    base = lab.run_paths.toolchain.python
    ssh = HOME / ".ssh"
    rows = [
        ("✅", "Skript im Arbeitsordner", "echo 'print(6*7)' > s.py && python3 s.py"),
        ("✅", "import sympy (Basis-venv)", "python3 -c 'import sympy; print(sympy.__version__)'"),
        ("✅", "pip list", "python3 -m pip list --disable-pip-version-check | grep sympy"),
        ("✅", "git status (vorhandenes Repo)", "git status --short | head -3"),
        ("✅", "pwd -P", "pwd -P"),
        ("✅", "realpath .", "python3 -c 'import os; print(os.path.realpath(\".\"))'"),
        ("✅", "os.getcwd()", "python3 -c 'import os; print(os.getcwd())'"),
        ("✅", "uv --version", "uv --version"),
        ("✅", "uv pip list (UV_NO_CONFIG)", "uv pip list >/dev/null && echo ok"),
        ("✅", "pdflatex", f"printf '%s' '{TEX}' > t.tex && {PDFLATEX} t.tex >/dev/null; ls t.pdf"),
        (
            "✅",
            "Schreiben in die Run-venv",
            "python3 -c \"import sys; open(sys.prefix + '/w3.txt', 'w').write('ok')\" "
            '&& cat "$VIRTUAL_ENV/w3.txt"',
        ),
        ("✅", "/usr/bin/python3 import ssl", "/usr/bin/python3 -c 'import ssl; print(1)'"),
        ("✅", "$TMPDIR schreiben", 'echo x > "$TMPDIR/t" && echo ok'),
        ("✅", "Lese-Ordner", f"cat {readdir / 'paper.md'}"),
        ("✅", "output/", f"cat {run / 'output' / 'ex.md'}"),
        ("✅", "eigener Anhang", f"cat {c['own_attachment']}"),
        ("✅", "eigener Skill", "head -3 .agents/skills/demo-skill/SKILL.md"),
        ("❌", "Kanarie unter ~", f"cat {c['outside']}"),
        ("❌", "Kanarie in ~/.arr (Python)", f"python3 -c \"open('{c['arr_home']}').read()\""),
        ("❌", "ls ~", f"ls {HOME} >/dev/null"),
        ("❌", "ls ~/Documents", f"ls {HOME / 'Documents'} >/dev/null"),
        ("❌", "Namen im Workfolder", f"ls {lab.workfolder} >/dev/null"),
        ("❌", "Namen in runs/", f"ls {lab.workfolder / 'runs'} >/dev/null"),
        ("❌", "Namen im Run-Ordner", f"ls {run} >/dev/null"),
        ("❌", "meta.json des Runs", f"cat {run / 'meta.json'}"),
        ("❌", "anderes Projekt", f"cat {c['other_project']}"),
        ("❌", "paralleler Run", f"cat {c['parallel_run']}"),
        ("❌", "Run-venv des parallelen Runs", f"cat {c['parallel_venv']}"),
        ("❌", "Run-venv des parallelen Runs (ls)", f"ls {c['parallel_venv'].parent} >/dev/null"),
        ("❌", "fremde Basis-venv", f"cat {c['other_base_venv']}"),
        ("⚠️", "Namen in <wurzel>/runs (bekannter Rest)", f"ls {venv_root() / 'runs'} >/dev/null"),
        ("❌", "Schreiben in die Basis-venv", f"touch {base}/w3.txt"),
        (
            "❌",
            "Schreiben in die Basis-venv (Python)",
            f"python3 -c \"open('{base}/lib/w3.txt', 'w').write('x')\"",
        ),
        ("❌", "Hardlink (ln)", f"ln {c['outside']} hl.txt && cat hl.txt"),
        (
            "❌",
            "Hardlink (os.link)",
            f"python3 -c \"import os; os.link('{c['outside']}', 'hl2.txt')\" && cat hl2.txt",
        ),
        ("❌", "Symlink nach außen", f"ln -sf {c['outside']} sl.txt; cat sl.txt"),
        ("❌", "Firmlink /System/Volumes/Data", f"cat /System/Volumes/Data{c['outside']}"),
        ("❌", "~/.ssh (Exit-Code)", f"ls {ssh} >/dev/null 2>&1"),
        ("❌", "~/.zsh_history (Exit-Code)", f"cat {HOME / '.zsh_history'} >/dev/null 2>&1"),
        ("❌", "Login-Ordner (Exit-Code)", f"ls {login} >/dev/null 2>&1"),
        ("❌", "sensibler Pfad im Lese-Ordner", f"cat {c['sensitive_in_readdir']}"),
        ("❌", "kdbx im Lese-Ordner", f"cat {c['kdbx']}"),
        ("❌", "skills/ des Runs", f"cat {c['run_skills']}"),
        ("❌", "fremder Anhang", f"cat {c['foreign_attachment']}"),
        ("❌", "Schreiben im Lese-Ordner", f"echo x > {readdir / 'w.txt'}"),
        ("❌", "/tmp schreiben", f"echo x > /tmp/arr-w3-00-{secrets.token_hex(3)}"),
    ]
    labels = {
        "late_home": "neu während des Turns: Datei direkt in ~",
        "late_outer": "neu während des Turns: Ordner im Elternordner",
        "late_run": "neu während des Turns: Run in runs/",
        "late_in_run": "neu während des Turns: Datei im Run-Ordner",
        "late_agent": "neu während des Turns: neuer Agent",
        "late_venv": "neu während des Turns: Run-venv in <wurzel>/runs",
        "late_venv_longer": "neu während des Turns: Run-venv, längere ID",
        "late_venv_shorter": "neu während des Turns: Run-venv, kürzere ID",
        "late_base_venv": "neu während des Turns: Basis-venv in <wurzel>/venvs",
    }
    rows += [("❌", labels[name], f"cat {path}") for name, path in late.items()]
    return rows


def _within(path: str, root: str) -> bool:
    child, parent = path.casefold(), root.casefold().rstrip("/")
    return child == parent or child.startswith(parent + "/")


def claude_offline(lab: Lab, late: dict[str, Path]) -> list[dict]:
    """Claude without a model: profile, sandbox lists and file-tool rules for the same paths.

    Sandbox semantics as in tests/test_policy.py (S1 §3.4): the narrower entry wins, deny on
    a tie. File tools: the PolicyEngine's answer for Read/Write.
    """
    config = lab.agent.file.config.model_copy(update={"provider": "claude", "id": "claude-1"})
    rules = PermissionRules(sensitive_paths=[str(lab.root / "readdir" / ".secret")])
    profile = profile_for(config, lab.run_paths, rules)
    fs = claude_options(profile).sandbox["filesystem"]
    engine = PolicyEngine([profile])

    def reads(path: Path) -> bool:
        text = str(path)
        allow = max((len(r) for r in fs["allowRead"] if _within(text, r)), default=-1)
        deny = max((len(r) for r in fs["denyRead"] if _within(text, r)), default=-1)
        return deny < 0 or allow > deny

    def writes(path: Path) -> bool:
        return any(_within(str(path), r) for r in fs["allowWrite"])

    def tool(name: str, path: Path) -> bool:
        request = PermissionRequest(
            run_id="lab", agent_id="claude-1", tool=name, provider="claude", paths=[str(path)]
        )
        return engine.evaluate(request).action is DecisionAction.ALLOW

    c = lab.canaries
    venv = lab.run_paths.venv_dir
    base = Path(str(lab.run_paths.toolchain.python))
    cases = [
        ("✅", "lesen", "Run-venv", venv / "bin" / "python"),
        ("✅", "schreiben", "Run-venv", venv / "lib" / "w3.txt"),
        ("✅", "lesen", "Basis-venv (sympy)", next(base.glob("lib/python*/site-packages/sympy"))),
        ("❌", "schreiben", "Basis-venv", base / "lib" / "w3.txt"),
        ("❌", "lesen", "Run-venv des parallelen Runs", c["parallel_venv"]),
        ("❌", "lesen", "fremde Basis-venv", c["other_base_venv"]),
        ("❌", "lesen", "<wurzel>/runs (Namen)", venv_root() / "runs"),
    ]
    cases += [("❌", "lesen", f"neu: {n}", path) for n, path in late.items() if "venv" in n]
    rows = []
    for expect, mode, label, path in cases:
        if mode == "lesen":
            sandbox, tools = reads(path), tool("Read", path)
        else:
            sandbox, tools = writes(path), tool("Write", path)
        ok = (expect == "✅") == sandbox == tools
        rows.append({"expect": expect, "mode": mode, "case": label, "sandbox": sandbox})
        rows[-1] |= {"file_tool": tools, "as_expected": ok}
        print(f"  Claude {expect} {mode:<9} {label:<38} Sandbox={sandbox} Werkzeug={tools}")
    return rows + claude_wide_read_dirs(lab, config, rules)


def claude_wide_read_dirs(lab: Lab, config, rules) -> list[dict]:
    """W3-00.1 M2: a read folder that contains a block (`<wurzel>`, the workfolder) gets no
    `Read(//…/**)` rule, so Read/Grep/Glob go to the PolicyEngine, which decides. W3-00.2:
    Grep/Glob/LS with the read folder itself as search root are refused."""
    c = lab.canaries
    wide = [
        (
            venv_root().parent,
            "Lese-Ordner = Elternordner von <wurzel>",
            [
                ("❌", "Run-venv des parallelen Runs", c["parallel_venv"]),
                ("❌", "fremde Basis-venv", c["other_base_venv"]),
                ("✅", "Lese-Ordner selbst", venv_root().parent / "w3-00-paper.md"),
                ("❌", "Suche mit dem Lese-Ordner als Wurzel", venv_root().parent),
            ],
        ),
        (
            lab.workfolder.parent,
            "Lese-Ordner = Elternordner des Workfolders",
            [
                ("❌", "paralleler Run", c["parallel_run"]),
                ("❌", "anderes Projekt", c["other_project"]),
                ("✅", "Lese-Ordner selbst", lab.workfolder.parent / "AGENTS.md"),
                ("❌", "Suche mit dem Lese-Ordner als Wurzel", lab.workfolder.parent),
            ],
        ),
    ]
    rows = []
    for folder, title, checks in wide:
        profile = profile_for(
            config.model_copy(update={"read_dirs": [str(folder)]}), lab.run_paths, rules
        )
        allowed = claude_options(profile).allowed_tools
        engine = PolicyEngine([profile])
        for expect, label, path in checks:
            rule = any(
                r.startswith("Read(//") and _within(str(path), r[6:-1].removesuffix("/**"))
                for r in allowed
            )
            answers = []
            search_root = "Suche" in label  # W3-00.2: the read folder itself as search root
            tools = ("Grep", "Glob", "LS") if search_root else ("Read", "Grep", "Glob")
            for name in tools:
                request = PermissionRequest(
                    run_id="lab",
                    agent_id="claude-1",
                    tool=name,
                    provider="claude",
                    paths=[str(path)],
                )
                answers.append(engine.evaluate(request).action is DecisionAction.ALLOW)
            ok = (not rule) and all(a == (expect == "✅") for a in answers)
            label = f"{title}: {label}"
            rows.append(
                {
                    "expect": expect,
                    "mode": "/".join(tools),
                    "case": label,
                    "read_rule": rule,
                    "policy_allow": answers,
                    "as_expected": ok,
                }
            )
            print(f"  Claude {expect} Read-Regel={rule} Policy={answers} {label}")
    return rows


def timing(cwd: str, env: dict[str, str]) -> float:
    times = []
    for _ in range(RUNS):
        start = time.perf_counter()
        proc = sandbox(cwd, env, ["/usr/bin/true"])
        times.append(time.perf_counter() - start)
        if proc.returncode != 0:
            sys.exit(f"/usr/bin/true scheitert: {shown(proc.stderr.strip()[-200:])}")
    return statistics.median(times)


def variant1_sized(entries: dict[str, str]) -> dict[str, str]:
    """The variant-2 entries plus 250 denied siblings of ~115 characters (W2-02.1 B2)."""
    runs = "/Users/someone/Documents/wf/runs"
    fill = "x" * (115 - len(runs) - 25)
    extra = {f"{runs}/20261029-{i:04d}_demo_{fill}_{i:04x}": "deny" for i in range(250)}
    return {**entries, **extra}


def main() -> None:
    lab = build_lab()
    try:
        login = codex_home_for(codex_auth())
        launch = build_launch(
            lab.agent,
            lab.profile,
            lab.run_paths,
            codex_home=login,  # its `deny` is part of the profile
            run_channels=["main", "secret-channel"],
        )
        write_config(SBX, launch.filesystem)
        entries = len(launch.filesystem)
        size = len(toml_value(launch.filesystem).encode())
        print(f"Profil: {entries} Einträge, {size} Bytes")
        dump("lab_v2_filesystem.json", launch.filesystem)
        late = late_files(lab)  # after the profile was written
        user = os.environ.get("USER", "")
        env = {**launch.shell_env, "CODEX_HOME": str(SBX), "USER": user, "LOGNAME": user}
        env["SHELL"] = "/bin/zsh"
        rows = []
        for expect, label, cmd in cases(lab, late, login):
            proc = sandbox(launch.cwd, env, ["/bin/zsh", "-c", cmd])
            text = shown((proc.stdout + proc.stderr).strip())
            ok = proc.returncode == 0
            rows.append({"expect": expect, "case": label, "ok": ok, "exit": proc.returncode})
            rows[-1]["out"] = text[-300:]
            last = " | ".join(text.splitlines()[-2:])[:90]
            mark = "läuft  " if ok else "gesperrt"
            print(f"  {expect} {mark} exit={proc.returncode:<3} {label:<46} {last!r}")
        claude_rows = claude_offline(lab, late)
        claude_bad = [r["case"] for r in claude_rows if not r["as_expected"]]
        bad = [r for r in rows if r["expect"] != "⚠️" and (r["expect"] == "✅") != r["ok"]]
        gaps = [r["case"] for r in rows if r["expect"] == "⚠️" and r["ok"]]
        tokens = {n: p.read_text().strip() for n, p in {**lab.canaries, **late}.items()}
        allowed = {"own_attachment"}
        texts = [r["out"] for r in rows]
        leaked = [n for n, t in tokens.items() if n not in allowed and any(t in x for x in texts)]
        v2 = timing(launch.cwd, env)
        write_config(SBX, variant1_sized(launch.filesystem))
        v1 = timing(launch.cwd, env)
        write_config(SBX, {":root": "read", launch.cwd: "write"})
        bare = timing(launch.cwd, env)
        result = {
            "entries": entries,
            "bytes": size,
            "rows": rows,
            "deviations": [r["case"] for r in bad],
            "known_gaps_open": gaps,
            "claude_offline": claude_rows,
            "claude_deviations": claude_bad,
            "leaked_canaries": leaked,
            "seconds_per_command": {
                "variant2": round(v2, 3),
                "variant1_250": round(v1, 3),
                "bare_2_entries": round(bare, 3),
                "variant1_250_entries": entries + 250,
            },
        }
        dump("lab_v2.json", result)
        print(
            f"Zeit je Befehl (Median aus {RUNS}): Variante 2 ({entries} Einträge) {v2:.2f} s, "
            f"Variante-1-Größe ({entries + 250}) {v1:.2f} s, nur 2 Einträge {bare:.2f} s"
        )
        print(f"Abweichungen: {len(bad)} {[r['case'] for r in bad]}  Kanarien im Text: {leaked}")
        print(f"Bekannter Rest (⚠️ offen): {gaps}  Claude offline Abweichungen: {claude_bad}")
        print(f"→ {shown(str(OUT / 'lab_v2.json'))}")
        sys.exit(1 if bad or leaked or claude_bad else 0)
    finally:
        remove_cleanup(lab)


if __name__ == "__main__":
    main()
