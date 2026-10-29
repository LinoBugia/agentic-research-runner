"""Secret scan over a run folder (W2-05 A4): planted values and patterns are found and named by
file, line and kind, never by value; a clean folder is clean; `.venv/` and caches are skipped."""

import json
from pathlib import Path

from arr.models import AuthKind, AuthProfile
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
    _write(run / ".venv/lib/site.py", f"KEY = '{ANTHROPIC}'\n")  # installed software: skipped
    _write(run / "tmp/cache/pip/x.txt", VALUE + "\n")  # cache: skipped
    _write(run / "tmp/work.txt", "harmlos\n")
    (run / "groupchats").mkdir()
    (run / "groupchats/room.sqlite").write_bytes(b"SQLite format 3\0" + VALUE.encode())  # binary

    report = scan_run_dir(run, {"Profil „max“": VALUE, "kurz": "ab"})
    assert report.secrets == 1, "a short secret is not searched for"
    found = {(f.file, f.line, f.kind) for f in report.findings}
    assert found == {
        ("agents/a/raw.jsonl", 2, "Wert von Profil „max“"),
        ("orchestrator.jsonl", 1, "anthropic_key"),
        ("orchestrator.jsonl", 1, "openai_key"),  # the generic `sk-` pattern matches as well
        ("base/notes.md", 3, "github_token"),
        ("base/notes.md", 3, "env_assignment"),
        ("agents/a/work/run.sh", 1, "url_credentials"),
    }
    assert not report.clean and report.skipped == []
    assert report.files == 7  # meta, transcript, raw, orchestrator, notes, run.sh, work.txt
    text = format_report(report) + json.dumps(report.to_data())
    for secret in (VALUE, ANTHROPIC, GITHUB, "hunter2", "user:pw"):
        assert secret not in text
    assert "agents/a/raw.jsonl:2 – Wert von Profil „max“" in text
    assert "6 Funde" in text


def test_clean_folder_is_clean_and_symlinks_are_not_followed(tmp_path: Path) -> None:
    run = _run_dir(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(ANTHROPIC + "\n", encoding="utf-8")
    (run / "agents/a/link.txt").symlink_to(outside)
    report = scan_run_dir(run)
    assert report.clean and report.files == 2 and report.secrets == 0
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
