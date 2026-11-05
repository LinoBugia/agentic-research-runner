"""Secret scan over a run folder (W2-05 A4): planted values and patterns are found and named by
file, line and kind, never by value; a clean folder is clean. W2-05.1 M2: profile values are
found in every regular file (`tmp/home/`, `tmp/cache/`, `.venv/`, binary and UTF-16 files),
patterns only in text files outside `.venv/`. W2-05.2 N1: only regular files are opened, a
FIFO in an agent's work folder does not block the scan."""

import json
import os
import sys
import time
from pathlib import Path

import pytest

from arr.models import AuthKind, AuthProfile
from arr.runs import secret_scan as scan_module
from arr.runs.orchestrator import OrchestratorLog
from arr.runs.secret_scan import (
    SCAN_KIND,
    Finding,
    ScanReport,
    format_report,
    profile_secrets,
    record_scan,
    scan_run_dir,
)

VALUE = "PLANTED-VALUE-9f8e7d6c5b4a"
ANTHROPIC = "sk-ant-api03-PLANTEDPLANTEDPLANTED"
GITHUB = "ghp_" + "A" * 36


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    _write(run / "meta.json", '{"status": "done"}\n')
    _write(run / "agents/a/transcript.jsonl", '{"kind": "text", "data": {"text": "ok"}}\n')
    return run


def test_finds_planted_values_and_patterns_without_naming_them(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    _write(run / "agents/a/raw.jsonl", f'{{"a": 1}}\n{{"token": "{VALUE}"}}\n')
    _write(run / "orchestrator.jsonl", f'{{"kind": "x", "data": {{"m": "{ANTHROPIC}"}}}}\n')
    _write(run / "base/notes.md", f"# Notizen\n\nexport GITHUB_TOKEN={GITHUB}\n")
    _write(run / "agents/a/work/run.sh", "curl -u bob:hunter2 https://user:pw@example.org/\n")
    _write(run / ".venv/lib/site.py", f"KEY = '{ANTHROPIC}'\n")  # installed software: no patterns
    _write(run / "tmp/cache/pip/x.txt", VALUE + "\n")  # cache: values are searched (M2)
    _write(run / "tmp/work.txt", "harmlos\n")
    (run / "groupchats").mkdir()
    (run / "groupchats/room.sqlite").write_bytes(b"SQLite format 3\0" + VALUE.encode())  # binary

    report = scan_run_dir(run, {"Profil „max“": VALUE, "kurz": "ab"})
    assert report.secrets == 1, "a short secret is not searched for"
    found = {(f.file, f.line, f.kind) for f in report.findings}
    assert found == {
        ("agents/a/raw.jsonl", 2, "Wert von Profil „max“"),
        ("tmp/cache/pip/x.txt", 1, "Wert von Profil „max“"),
        ("groupchats/room.sqlite", 1, "Wert von Profil „max“"),
        ("orchestrator.jsonl", 1, "anthropic_key"),
        ("orchestrator.jsonl", 1, "openai_key"),  # the generic `sk-` pattern matches as well
        ("base/notes.md", 3, "github_token"),
        ("base/notes.md", 3, "env_assignment"),
        ("agents/a/work/run.sh", 1, "url_credentials"),
    }
    assert not report.clean and report.skipped == []
    assert report.files == 10, "every regular file is read"
    text = format_report(report) + json.dumps(report.to_data())
    for secret in (VALUE, ANTHROPIC, GITHUB, "hunter2", "user:pw"):
        assert secret not in text
    assert "agents/a/raw.jsonl:2 – Wert von Profil „max“" in text
    assert "8 Funde" in text


def test_w2_05_1_values_are_found_in_every_file_and_patterns_only_in_text_outside_the_venv(
    tmp_path: Path,
) -> None:
    run = _run_dir(tmp_path)
    _write(run / "tmp/home/prover/.bash_history", f"echo {VALUE} > ~/x\n")  # Codex HOME
    _write(run / "tmp/cache/pip/log.txt", f"downloaded {VALUE}\n")
    _write(run / ".venv/lib/python3.12/site-packages/leak.txt", f"x\n{VALUE}\n")  # overlay
    (run / "groupchats").mkdir()
    (run / "groupchats/room.sqlite").write_bytes(b"SQLite format 3\0" * 5 + b"\n" + VALUE.encode())
    (run / "agents/a/work").mkdir()
    (run / "agents/a/work/notes-utf16.txt").write_text(f"Hinweis\n{VALUE}\n", encoding="utf-16")
    chunk = scan_module._CHUNK
    (run / "agents/a/work/big.bin").write_bytes(b"\0" * (chunk - 5) + VALUE.encode() + b"\n")
    _write(run / ".venv/lib/site.py", f"KEY = '{ANTHROPIC}'\n")  # a pattern in the venv: no
    _write(run / "tmp/home/prover/.zshrc", f"export GITHUB_TOKEN={GITHUB}\n")  # outside: yes

    report = scan_run_dir(run, {"Profil „max“": VALUE})
    found = {(f.file, f.line, f.kind) for f in report.findings}
    value = "Wert von Profil „max“"
    assert found == {
        ("tmp/home/prover/.bash_history", 1, value),
        ("tmp/cache/pip/log.txt", 1, value),
        (".venv/lib/python3.12/site-packages/leak.txt", 2, value),
        ("groupchats/room.sqlite", 2, value),
        ("agents/a/work/notes-utf16.txt", 2, value),
        ("agents/a/work/big.bin", 1, value),  # across the chunk boundary
        ("tmp/home/prover/.zshrc", 1, "github_token"),
        ("tmp/home/prover/.zshrc", 1, "env_assignment"),
    }
    assert report.files == 10 and report.skipped == []
    assert VALUE not in format_report(report) + json.dumps(report.to_data())


def test_clean_folder_is_clean_and_symlinks_are_not_followed(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(ANTHROPIC + "\n", encoding="utf-8")
    (run / "agents/a/link.txt").symlink_to(outside)
    (run / "agents/a/dir-link").symlink_to(tmp_path, target_is_directory=True)
    report = scan_run_dir(run, {"Profil „max“": ANTHROPIC})
    assert report.clean and report.files == 2 and report.secrets == 1
    assert format_report(report).startswith("Secret-Scan: sauber (2 Dateien geprüft")


def test_record_scan_writes_the_entry_without_values(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    report = ScanReport(files=3, secrets=1, findings=[Finding("agents/a/raw.jsonl", 2, "x")])
    record_scan(run, report)
    [entry] = [e for e in OrchestratorLog(run).entries() if e["kind"] == SCAN_KIND]
    assert entry["data"] == {
        "clean": False,
        "files": 3,
        "secrets": 1,
        "findings": [{"file": "agents/a/raw.jsonl", "line": 2, "kind": "x"}],
        "skipped": [],
    }


def test_profile_secrets_come_from_keychain_and_codex_login_in_memory(tmp_path: Path) -> None:
    login = tmp_path / "login"
    login.mkdir()
    (login / "auth.json").write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "tokens": {
                    "id_token": "id." + "x" * 40,
                    "access_token": "acc." + "y" * 40,
                    "refresh_token": "ref." + "z" * 40,
                    "account_id": "acct-short",
                },
                "last_refresh": "2026-10-29T08:11:00.000000Z",
            }
        )
    )
    profiles = [
        AuthProfile(name="max", kind=AuthKind.CLAUDE_OAUTH),
        AuthProfile(name="api", kind=AuthKind.CLAUDE_API_KEY),
        AuthProfile(name="gpt", kind=AuthKind.CODEX_CHATGPT, login_dir=str(login)),
    ]
    reader = lambda service, account: {"max": "oauth-token-123456"}.get(account)  # noqa: E731
    secrets = profile_secrets(profiles, reader)
    assert secrets == {
        "Profil „max“": "oauth-token-123456",
        "Profil „gpt“, Login-Token 1": "id." + "x" * 40,
        "Profil „gpt“, Login-Token 2": "acc." + "y" * 40,
        "Profil „gpt“, Login-Token 3": "ref." + "z" * 40,
    }, "no value for a profile without secret; dates and short ids are not tokens"


def test_keychain_errors_do_not_stop_the_scan() -> None:
    def broken(service: str, account: str) -> str | None:
        raise RuntimeError("no keyring")

    assert profile_secrets([AuthProfile(name="max", kind=AuthKind.CLAUDE_OAUTH)], broken) == {}


@pytest.mark.skipif(sys.platform == "win32", reason="os.mkfifo")
def test_w2_05_2_a_fifo_in_the_work_folder_does_not_block_the_scan(tmp_path: Path) -> None:
    """`open()` on a FIFO without a writer blocks forever; an agent can create one with
    `mkfifo`. The scan skips everything that is not a regular file and still finds the value in
    the file next to it."""
    run = _run_dir(tmp_path)
    work = run / "agents/a/work"
    work.mkdir()
    os.mkfifo(work / "pipe")
    _write(work / "notes.txt", f"x\n{VALUE}\n")
    started = time.monotonic()
    report = scan_run_dir(run, {"Profil „max“": VALUE})
    assert time.monotonic() - started < 2
    assert {(f.file, f.line, f.kind) for f in report.findings} == {
        ("agents/a/work/notes.txt", 2, "Wert von Profil „max“")
    }
    assert report.files == 3 and report.skipped == []  # the FIFO is neither read nor "unreadable"


# -- W3-00: the run venv lives outside the run folder -------------------------------------


def _meta(run: Path, run_venv: Path | None) -> None:
    data = {
        "run_id": run.name,
        "status": "done",
        "starter": "cli",
        "project": "demo",
        "created": 1.0,
        "arr_version": "0.1.0",
        "run_venv": str(run_venv) if run_venv else None,
    }
    _write(run / "meta.json", json.dumps(data) + "\n")


def test_w3_00_value_in_the_run_venv_at_its_new_place_is_found(
    tmp_path: Path, venv_root: Path
) -> None:
    run = _run_dir(tmp_path)
    venv = venv_root / "runs" / run.name
    _meta(run, venv)
    _write(venv / "lib/python3.12/site-packages/leak.txt", f"x\n{VALUE}\n")
    _write(venv / "lib/python3.12/site-packages/pip/x.py", f"KEY = '{ANTHROPIC}'\n")  # no
    _write(venv / "bin/activate", "harmlos\n")

    report = scan_run_dir(run, {"Profil „max“": VALUE})
    found = {(f.file, f.line, f.kind) for f in report.findings}
    assert found == {("run-venv/lib/python3.12/site-packages/leak.txt", 2, "Wert von Profil „max“")}
    assert report.files == 2 + 3  # meta.json, transcript.jsonl and the three venv files
    text = format_report(report)
    assert "run-venv/lib/python3.12/site-packages/leak.txt:2" in text
    assert str(venv_root) not in text and VALUE not in text


def test_w3_00_a_linked_run_venv_is_not_followed(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    _write(elsewhere / "leak.txt", VALUE + "\n")
    link = tmp_path / "venv-link"
    link.symlink_to(elsewhere)
    _meta(run, link)
    assert scan_run_dir(run, {"Profil „max“": VALUE}).clean


def test_w3_00_missing_or_absent_run_venv_scans_the_run_folder_only(
    tmp_path: Path, venv_root: Path
) -> None:
    run = _run_dir(tmp_path)
    _meta(run, venv_root / "runs" / run.name)  # recorded, but never created
    assert scan_run_dir(run, {"Profil „max“": VALUE}).files == 2
    _meta(run, None)
    assert scan_run_dir(run, {"Profil „max“": VALUE}).files == 2


CODE_LINES = (
    "ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)",
    "    key=lambda n: steps[n],",
    "best = max(pairs, key=itemgetter(1))",
    "top = sorted(counts, key=counts.get)",
    "order = sorted(words, key=str.lower)[:10]",
    "token = make(token=os.environ['X'])",
    '{"input": {"command": "python -c \\"print(sorted(x, key=lambda t: -t))\\""}}',
    "allow: python3 collatz.py # key=«entfernt»",
    '{"command": "sort key=\\u00abentfernt\\u00bb"}',
)
"""Python code from the UX acceptance 3 runs (U2) and redacted values: no secret."""

SECRET_LINES = (
    "export API_KEY=abc123def456ghi",
    "API_KEY=sk_live_planted",
    'token="planted-value-9f8e7d6c"',
    "PASSWORD='hunter2hunter2'",
    "curl -H x MY_SECRET=plain",
    "GITHUB_TOKEN=Ab12Cd34Ef56Gh78Ij90)",
)
"""Real assignments of secrets: still hits."""


def test_w3_08_u2_python_code_is_no_env_assignment_but_real_secrets_are(tmp_path: Path) -> None:
    from arr.permissions.policy import _SECRET_ASSIGN_RE

    for line in CODE_LINES[:6]:
        assert _SECRET_ASSIGN_RE.search(line), f"counter-check: the old rule flags {line!r}"
    run = _run_dir(tmp_path)
    _write(run / "agents/a/work/code.py", "\n".join(CODE_LINES) + "\n")
    _write(run / "approvals.jsonl", json.dumps({"command": "sort key=«entfernt»"}) + "\n")
    assert scan_run_dir(run).clean

    _write(run / "agents/a/work/env.sh", "\n".join(SECRET_LINES) + "\n")
    report = scan_run_dir(run)
    found = {(f.file, f.line) for f in report.findings if f.kind == "env_assignment"}
    assert found == {("agents/a/work/env.sh", n) for n in range(1, len(SECRET_LINES) + 1)}
    text = format_report(report) + json.dumps(report.to_data())
    for secret in ("abc123def456ghi", "planted-value", "hunter2", "Ab12Cd34"):
        assert secret not in text


def test_w3_08_u2_a_secret_after_code_on_the_same_line_is_found(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    _write(run / "x.sh", "sorted(a, key=len); export API_TOKEN=planted123planted\n")
    assert [(f.file, f.kind) for f in scan_run_dir(run).findings] == [("x.sh", "env_assignment")]
