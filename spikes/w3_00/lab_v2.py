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
from arr.venvs import venv_root

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


def late_files(lab: Lab) -> dict[str, Path]:
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
    }
    for path in late.values():
        canary(path)
    lab.cleanup += [late["late_home"], late["late_venv"].parent]
    return late


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
        ("⚠️", "Run-venv des parallelen Runs", f"cat {c['parallel_venv']}"),
        ("⚠️", "andere Basis-venv", f"cat {c['other_base_venv']}"),
        ("⚠️", "Namen in <wurzel>/runs", f"ls {venv_root() / 'runs'} >/dev/null"),
        ("❌", "Schreiben in die Basis-venv", f"touch {base}/w3.txt"),
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
    }
    rows += [
        ("⚠️" if name == "late_venv" else "❌", labels[name], f"cat {path}")
        for name, path in late.items()
    ]
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
        bad = [r for r in rows if r["expect"] != "⚠️" and (r["expect"] == "✅") != r["ok"]]
        gaps = [r["case"] for r in rows if r["expect"] == "⚠️" and r["ok"]]
        tokens = {n: p.read_text().strip() for n, p in {**lab.canaries, **late}.items()}
        allowed = {"own_attachment", "parallel_venv", "other_base_venv", "late_venv"}
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
        print(f"Bekannte Lücke (⚠️ offen): {gaps}")
        print(f"→ {shown(str(OUT / 'lab_v2.json'))}")
        sys.exit(1 if bad or leaked else 0)
    finally:
        remove_cleanup(lab)


if __name__ == "__main__":
    main()
