"""Offline check of Codex permission-profile syntax with `codex sandbox -P` (no model, no login).

Small fake layout under tmp/sbx/ with a venv inside `~`, one throwaway CODEX_HOME per variant.
Real sensitive paths are probed only by exit code.

    uv run python spikes/w2_02_codex/lab_sandbox.py [variant ...]
"""

import os
import secrets
import shutil
import subprocess
import sys
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


def main() -> None:
    variants = sys.argv[1:] or ["plain", "glob-home", "glob-children", "glob-children-read"]
    p = build()
    for variant in variants:
        run_variant(p, variant)


if __name__ == "__main__":
    main()
