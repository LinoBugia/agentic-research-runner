"""Offline check of variant 1 with the adapter's own entries (no model, no login).

Builds the lab (`common.build_lab`), takes the permission profile from `build_launch` and runs
probes with `codex sandbox -P arr` in the agent's working folder: what exists at start, files
with known names that arr creates later (`LATE_GLOBS`), an unknown new entry (documented
gap) and the toolchain. Real sensitive paths are probed only by exit code.

    uv run python spikes/w2_02_codex/lab_variant1.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from codex_cli_bin import bundled_codex_path
from common import OUT, build_lab, dump, remove_cleanup

from arr.backends.codex_config import PERMISSION_PROFILE, build_launch, toml_value

HOME = Path.home()
SBX = Path(__file__).resolve().parent / "tmp" / "sbx_v1"
TEX = "\\documentclass{article}\\begin{document}x\\end{document}"
PDFLATEX = "TEXMFVAR=$TMPDIR/texmf pdflatex -interaction=nonstopmode"


def main() -> None:
    lab = build_lab()
    try:
        run = lab.run.run_dir
        launch = build_launch(lab.agent, lab.profile, lab.run_paths, codex_home=SBX)
        if SBX.exists():
            shutil.rmtree(SBX)
        SBX.mkdir(parents=True)
        lines = [f'default_permissions = "{PERMISSION_PROFILE}"']
        lines += [f"[permissions.{PERMISSION_PROFILE}.filesystem]"]
        lines += [f"{toml_value(k)} = {toml_value(v)}" for k, v in launch.filesystem.items()]
        (SBX / "config.toml").write_text("\n".join(lines) + "\n")
        size = len(next(o for o in launch.config_overrides if o.startswith("permissions.")))
        print(f"{len(launch.filesystem)} Einträge, Override {size} Zeichen")
        # created after the list was built
        late_known = [
            run / "orchestrator.jsonl",
            run / "approvals.jsonl",
            run / "usage.json",
            run / "groupchats" / "room.sqlite-wal",
            run / "groupchats" / "main.md",
            run / "agents" / "gpt-1" / "transcript.jsonl",
            run / "agents" / "gpt-1" / "session.json",
            run / "agents" / "gpt-1" / "blobs" / "0001.txt",
            run / "agents" / "spawned-1" / "raw.jsonl",
        ]
        late_unknown = [lab.root / "late-unknown.txt", run / "late-unknown.txt"]
        for path in late_known + late_unknown:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("LATE\n")
        subprocess.run(["git", "init", "-q", launch.cwd], check=True)
        vault = lab.root / "readdir" / "vault.kdbx"
        vault.write_text("LATE\n")
        c = lab.canaries
        cases = [
            ("✅", "work write", "echo x > w.txt && cat w.txt"),
            ("✅", "readdir", f"cat {lab.root / 'readdir' / 'paper.md'}"),
            ("✅", "own attachment", f"cat {c['own_attachment']}"),
            ("✅", "venv sympy", "python3 -c 'import sympy; print(sympy.__version__)'"),
            ("✅", "pwd -P", "pwd -P"),
            ("✅", "realpath .", "python3 -c 'import os; print(os.path.realpath(\".\"))'"),
            ("✅", "$TMPDIR write", 'echo x > "$TMPDIR/t" && echo ok'),
            ("✅", "pip list", "python3 -m pip list --disable-pip-version-check | grep -i sympy"),
            ("✅", "git status", "git status --short | head -2; echo exit=${pipestatus[1]}"),
            ("✅", "uv pip list", "uv pip list >/dev/null && echo ok"),
            ("⚠️", "uv ohne UV_NO_CONFIG", "env -u UV_NO_CONFIG uv pip list >/dev/null && echo ok"),
            (
                "✅",
                "pdflatex",
                f"printf '%s' '{TEX}' > t.tex && {PDFLATEX} t.tex >/dev/null; ls t.pdf",
            ),
            ("❌", "outside", f"cat {c['outside']}"),
            ("❌", "other project", f"cat {c['other_project']}"),
            ("❌", "run skills", f"cat {c['run_skills']}"),
            ("❌", "foreign attachment", f"cat {c['foreign_attachment']}"),
            ("❌", "sensitive in readdir", f"cat {c['sensitive_in_readdir']}"),
            ("❌", "~/.arr", f"cat {c['arr_home']}"),
            ("❌", "kdbx (name glob)", f"cat {vault}"),
            ("❌", "~/.ssh (exit)", "cat ~/.ssh/known_hosts >/dev/null 2>&1; echo exit=$?"),
            ("❌", "history (exit)", "cat ~/.zsh_history >/dev/null 2>&1; echo exit=$?"),
            ("❌", "run meta.json", f"cat {run / 'meta.json'}"),
        ]
        cases += [("❌", f"late {p.relative_to(run)}", f"cat {p}") for p in late_known]
        cases += [("⚠️", f"late unknown in {p.parent.name}", f"cat {p}") for p in late_unknown]
        cases += [("⚠️", "ls ~ (names)", "ls ~ >/dev/null; echo exit=$?")]
        env = {**os.environ, **launch.env, "CODEX_HOME": str(SBX)}
        rows = []
        for expect, label, cmd in cases:
            proc = subprocess.run(
                [str(bundled_codex_path()), "sandbox", "-P", PERMISSION_PROFILE, "-C", launch.cwd]
                + ["--", "/bin/zsh", "-c", cmd],
                env=env,
                capture_output=True,
                text=True,
                cwd=launch.cwd,
            )
            text = (proc.stdout + proc.stderr).strip().replace(str(HOME), "~")
            ok = proc.returncode == 0 and "exit=1" not in text
            rows.append({"expect": expect, "case": label, "ok": ok, "out": text[-400:]})
            print(f"  {expect} {'läuft ' if ok else 'gesperrt'}  {label:<34} {text[-70:]!r}")
        dump("lab_variant1.json", rows)
        bad = [r for r in rows if (r["expect"] == "✅") != r["ok"] and r["expect"] != "⚠️"]
        print(f"Abweichungen: {len(bad)}  → {OUT / 'lab_variant1.json'}")
        sys.exit(1 if bad else 0)
    finally:
        remove_cleanup(lab)


if __name__ == "__main__":
    main()
