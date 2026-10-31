"""Live probe W3-05: `test_profile` with the login of this Mac, create/delete without a request.

Runs on a temporary `ARR_HOME`, so `~/.arr/profiles.yaml` stays as it is. Costs one Claude
request. The `codex_chatgpt` profile points at a login folder outside that `ARR_HOME`, so arr
must refuse it without a request and without touching the folder (W3-05.1, M7). The API-key
profile holds a fake value, lives in the keychain for a moment and sends no request.

Usage: uv run python spikes/w3_05/live_profiles.py
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import keyring
from pydantic import SecretStr

CODEX_PROFILE = "chatgpt-w2-02a"
FAKE_PROFILE = "arr-live-fake"


def report(label: str, info: object) -> None:
    fields = {"last_test", "secret_present", "usage_window", "keychain_label"}
    print(label, json.dumps(info.model_dump(mode="json", include=fields), ensure_ascii=False))  # type: ignore[attr-defined]


async def main(root: Path) -> None:
    from arr.models import AuthKind
    from arr.service.api_models import ApiException, CreateProfileRequest
    from arr.service.real_api import RealApi

    workfolder = root / "wf"
    (workfolder / "runs").mkdir(parents=True)
    (workfolder / "arr.yaml").write_text("version: 1\n")
    api = RealApi(workfolder, base_url="http://127.0.0.1:1", scan=False)
    claude_projects = Path.home() / ".claude" / "projects"
    sessions = len(list(claude_projects.iterdir()))
    report("max:", await api.test_profile("max"))
    print("Neue Sitzungen in ~/.claude/projects:", len(list(claude_projects.iterdir())) - sessions)

    folder = Path.home() / ".arr" / "auth" / CODEX_PROFILE / "codex_home"
    mode = folder.stat().st_mode
    report(f"{CODEX_PROFILE}:", await api.test_profile(CODEX_PROFILE))
    try:
        await api.start_login(CODEX_PROFILE)
    except ApiException as exc:
        print("Login abgelehnt:", exc.error.code.value)
    print("Modus des Ordners unverändert:", folder.stat().st_mode == mode)
    print("Liste:", [p.name for p in await api.list_profiles()])

    request = CreateProfileRequest(
        name=FAKE_PROFILE, kind=AuthKind.CLAUDE_API_KEY, api_key=SecretStr("sk-ant-fake-value")
    )
    report("angelegt:", await api.create_profile(request))
    print("Schlüsselbund:", keyring.get_password("arr", FAKE_PROFILE) is not None)
    print("Secret in profiles.yaml:", "sk-ant" in (root / "profiles.yaml").read_text())
    await api.delete_profile(FAKE_PROFILE)
    print("nach Löschen im Schlüsselbund:", keyring.get_password("arr", FAKE_PROFILE) is not None)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="arr-live-") as temp:
        home = Path(temp)
        os.environ["ARR_HOME"] = str(home)
        login_dir = Path.home() / ".arr" / "auth" / CODEX_PROFILE / "codex_home"
        (home / "profiles.yaml").write_text(
            "version: 1\nprofiles:\n  - {name: max, kind: claude_oauth}\n"
            f"  - {{name: {CODEX_PROFILE}, kind: codex_chatgpt, login_dir: {login_dir}}}\n"
        )
        sys.exit(asyncio.run(main(home)))
