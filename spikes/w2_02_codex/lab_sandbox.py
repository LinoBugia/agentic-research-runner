"""Offline check of Codex permission-profile syntax with `codex sandbox -P` (no model, no login).

Small fake layout under tmp/sbx/ with a venv inside `~`, one throwaway CODEX_HOME per variant.
Real sensitive paths are probed only by exit code.

    uv run python spikes/w2_02_codex/lab_sandbox.py [variant ...]
    uv run python spikes/w2_02_codex/lab_sandbox.py v2     # feasibility of variant 2

Variant 2 (later hardening, docs/07 §3 ①): `~` denied as a whole, base venv and run venv
outside `~` (a temporary folder below `$TMPDIR`, removed afterwards), the working folder
stays below `~`.
"""

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from codex_cli_bin import bundled_codex_path

SBX = Path(__file__).resolve().parent / "tmp" / "sbx"
HOME = Path.home()


def build() -> dict[str, Path]:
    if SBX.exists():
        shutil.rmtree(SBX)
    p = {
        "wf": SBX / "wf",
        "work": SBX / "wf" / "runs" / "R" / "work",
        "tmp": SBX / "wf" / "runs" / "R" / "tmp",
        "other": SBX / "wf" / "other",
        "outside": SBX / "outside",
    }
    for path in p.values():
        path.mkdir(parents=True, exist_ok=True)
    for name, path in [("w.txt", p["work"]), ("c.txt", p["outside"]), ("c.txt", p["other"])]:
        (path / name).write_text(secrets.token_hex(8) + "\n")
    p["venv"] = SBX / "wf" / "runs" / "R" / ".venv"
    subprocess.run(["uv", "venv", "-q", str(p["venv"])], check=True)
    return p


def ancestors(path: Path, stop: Path) -> list[Path]:
    """Folders from `stop` (inclusive) down to the parent of `path`."""
    chain = []
    for parent in path.parents:
        chain.append(parent)
        if parent == stop:
            break
    return list(reversed(chain))


def _esc(ch: str) -> str:
    return f"[{ch}]" if ch in "*?[]{}!" else ch


def complement(name: str) -> list[str]:
    """Glob patterns matching every single path component except `name`."""
    pats = []
    for i, ch in enumerate(name):
        prefix = "".join(_esc(c) for c in name[:i])
        pats.append(f"{prefix}[!{_esc(ch)}]*")
        if i:
            pats.append(prefix)
    pats.append("".join(_esc(c) for c in name) + "?*")
    return pats


def entries(p: dict[str, Path], variant: str) -> list[tuple[str, str]]:
    base = [(":root", "read"), (str(p["work"]), "write"), (str(p["tmp"]), "write")]
    base.append((str(p["venv"]), "write"))
    if variant == "plain":
        return base + [(str(HOME), "deny"), (str(p["wf"]), "deny")]
    if variant == "glob-home":
        return base + [(f"{HOME}/**", "deny"), (f"{p['wf']}/**", "deny")]
    if variant == "glob-children":
        # ancestors stay readable, their children denied by glob, path entries punch through
        out = list(base)
        for folder in ancestors(p["work"], HOME):
            out.append((f"{folder}/*", "deny"))
        return out
    if variant == "glob-children-read":
        out = list(base)
        for folder in ancestors(p["work"], HOME):
            out.append((str(folder), "read"))
            out.append((f"{folder}/*", "deny"))
        return out
    if variant == "project-roots":
        return base + [(":project_roots", "write"), (str(HOME), "deny"), (str(p["wf"]), "deny")]
    if variant == "deny-siblings":
        out = list(base)
        keep = [p["work"], p["tmp"], p["venv"]]
        for folder in ancestors(p["work"], HOME):
            for child in folder.iterdir():
                if not any(k == child or child in k.parents for k in keep):
                    out.append((str(child), "deny"))
        return out
    if variant == "deny-siblings-glob":
        out = entries(p, "deny-siblings")
        keep = [p["work"], p["tmp"], p["venv"]]
        for folder in ancestors(p["work"], HOME):
            child = next(c for c in folder.iterdir() if any(k == c or c in k.parents for k in keep))
            for pattern in complement(child.name):
                out += [(f"{folder}/{pattern}", "deny"), (f"{folder}/{pattern}/**", "deny")]
        return out
    raise SystemExit(f"unknown variant {variant}")


def run_variant(p: dict[str, Path], variant: str) -> None:
    home = SBX / f"codex_home_{variant}"
    home.mkdir()
    lines = ['default_permissions = "arr"']
    if variant == "project-roots":
        lines += ["[permissions.arr]", f'workspace_roots = {{ "{p["work"]}" = true }}']
    lines += ["[permissions.arr.filesystem]"]
    lines += [f'"{k}" = "{v}"' for k, v in entries(p, variant)]
    (home / "config.toml").write_text("\n".join(lines) + "\n")
    env = {**os.environ, "CODEX_HOME": str(home), "TMPDIR": str(p["tmp"])}
    cases = [
        ("work read", "cat w.txt"),
        ("work write", "echo x > n.txt && echo ok"),
        ("venv python", f"{p['venv']}/bin/python -c 'print(\"py ok\")'"),
        ("outside", f"cat {p['outside']}/c.txt"),
        ("other", f"cat {p['other']}/c.txt"),
        ("ls home", f"ls {HOME} | head -3"),
        ("ls wf", f"ls {p['wf']}"),
        ("stat home", f"stat -f %N {HOME}"),
        ("ssh exit", f"cat {HOME}/.ssh/known_hosts >/dev/null 2>&1; echo exit=$?"),
        ("new file", f"echo x > {SBX}/new.txt; echo exit=$?"),
        ("firmlink", f"cat /System/Volumes/Data{p['outside']}/c.txt"),
        ("late sibling", f"cat {SBX}/late/c.txt"),
        ("late dotfile", f"cat {SBX}/.late"),
        ("late wf", f"cat {p['wf']}/late.txt"),
    ]
    late = SBX / "late"
    late.mkdir(exist_ok=True)
    (late / "c.txt").write_text("LATE-" + secrets.token_hex(4) + "\n")
    (SBX / ".late").write_text("LATEDOT-" + secrets.token_hex(4) + "\n")
    (p["wf"] / "late.txt").write_text("LATEWF-" + secrets.token_hex(4) + "\n")
    print(f"== {variant}")
    codex = str(bundled_codex_path())
    for label, cmd in cases:
        proc = subprocess.run(
            [codex, "sandbox", "-P", "arr", "-C", str(p["work"]), "--", "/bin/zsh", "-c", cmd],
            env=env,
            capture_output=True,
            text=True,
            cwd=p["work"],
        )
        text = (proc.stdout + proc.stderr).strip().replace(str(HOME), "~")
        last = " | ".join(text.splitlines()[-3:])
        print(f"  {proc.returncode:>3}  {label:<12} {last[:150]}")


def variant2() -> None:
    outside = Path(tempfile.mkdtemp(prefix="arr-w202-v2-"))
    try:
        work = SBX / "wf" / "runs" / "R" / "agents" / "a" / "work"
        tmp = SBX / "wf" / "runs" / "R" / "tmp"
        for folder in (work, tmp):
            folder.mkdir(parents=True, exist_ok=True)
        base, venv = outside / "base", outside / "run-venv"
        subprocess.run(["uv", "venv", "-q", "--seed", str(base)], check=True)
        py = str(base / "bin" / "python")
        subprocess.run(["uv", "pip", "install", "-q", "--python", py, "sympy"], check=True)
        subprocess.run(
            ["uv", "venv", "-q", "--seed", "--system-site-packages", "--python", py, str(venv)],
            check=True,
        )
        base_site = next(base.glob("lib/python*/site-packages"))
        next(venv.glob("lib/python*/site-packages")).joinpath("base.pth").write_text(
            f"{base_site}\n"
        )
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        (work / "t.tex").write_text(
            "\\documentclass{article}\\begin{document}$1+1=2$\\end{document}\n"
        )
        home = SBX / "codex_home_v2"
        home.mkdir()
        fs = [(":root", "read"), (str(HOME), "deny"), (str(work), "write"), (str(tmp), "write")]
        fs += [(str(venv), "write"), (str(base), "read")]
        lines = ['default_permissions = "arr"', "[permissions.arr.filesystem]"]
        lines += [f'"{k}" = "{v}"' for k, v in fs]
        (home / "config.toml").write_text("\n".join(lines) + "\n")
        path = f"{venv}/bin:{base}/bin:/opt/homebrew/bin:/Library/TeX/texbin:/usr/bin:/bin"
        env = {
            "PATH": path,
            "CODEX_HOME": str(home),
            "TMPDIR": str(tmp),
            "HOME": str(tmp / "home"),
            "UV_CACHE_DIR": str(tmp / "uv"),
            "XDG_CACHE_HOME": str(tmp / "cache"),
            "TEXMFVAR": str(tmp / "texmf-var"),
            "VIRTUAL_ENV": str(venv),
        }
        cases = [
            ("python script", "echo 'print(6*7)' > s.py && python3 s.py"),
            ("import sympy", "python3 -c 'import sympy; print(sympy.__version__)'"),
            ("pip list", "python3 -m pip list --disable-pip-version-check | grep -i sympy"),
            ("git status", "git status --short | head -3; echo exit=$?"),
            ("pwd -P", "pwd -P"),
            ("realpath .", "python3 -c 'import os; print(os.path.realpath(\".\"))'"),
            ("python getcwd", "python3 -c 'import os; print(os.getcwd())'"),
            ("uv", "uv --version"),
            ("uv pip list", "uv pip list >/dev/null && echo ok"),
            ("uv no-config", "UV_NO_CONFIG=1 uv pip list >/dev/null && echo ok"),
            ("pdflatex", "pdflatex -interaction=nonstopmode t.tex >/dev/null 2>&1; ls t.pdf"),
            ("latexmk", "latexmk -pdf -interaction=nonstopmode t.tex >/dev/null 2>&1; ls t.pdf"),
            ("~ canary", f"cat {SBX}/outside/c.txt"),
        ]
        print("== v2: ~ gesperrt, venvs außerhalb von ~")
        codex = str(bundled_codex_path())
        for label, cmd in cases:
            proc = subprocess.run(
                [codex, "sandbox", "-P", "arr", "-C", str(work), "--", "/bin/zsh", "-c", cmd],
                env=env,
                capture_output=True,
                text=True,
                cwd=work,
            )
            text = (proc.stdout + proc.stderr).strip().replace(str(HOME), "~")
            text = text.replace(str(outside), "<außen>")
            last = " | ".join(text.splitlines()[-2:])
            print(f"  {proc.returncode:>3}  {label:<14} {last[:150]}")
    finally:
        shutil.rmtree(outside, ignore_errors=True)


def main() -> None:
    if sys.argv[1:] == ["v2"]:
        build()
        variant2()
        return
    variants = sys.argv[1:] or ["plain", "glob-home", "glob-children", "glob-children-read"]
    p = build()
    for variant in variants:
        run_variant(p, variant)


if __name__ == "__main__":
    main()
