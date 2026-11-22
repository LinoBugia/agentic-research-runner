"""W4b-07.1 (B1, B11): the deviations of a run start through `RunStartsArea.card` as Claude Code
gets them from `estimate_run` (the same card the inbox shows): a phase the defaults drop is no
alarm, a phase the request's parameters drop is one, and `defaults: {decider: null}` lifts the
setting of every agent."""

from pathlib import Path
from typing import Any

import pytest
from mcp_kit import make_api
from phases_ui_kit import install_skill
from run_kit import write_profiles
from service_kit import make_workfolder

from arr.service.user_api import UserApi

ARR_YAML = """version: 1
toolchain: {run_venv: off}
defaults:
  claude: {model: claude-sonnet-5-5, auth: fake-auth}
"""

OPTIONAL = """kind: research-group
version: 1
topology: phases
params:
  extra: {{ default: {default}, choices: [true, false] }}
agents: [{{ id: worker, role: roles/worker.md }}]
phases:
  - name: arbeit
    topology: solo
    actions:
      worker: {{ task: tasks/arbeit.md, submission: {{ format: text }} }}
  - name: extra
    topology: solo
    when: {{ param: extra, equals: true }}
    decider: user
    actions:
      worker: {{ task: tasks/extra.md, submission: {{ format: text }} }}
"""

AGENTS_USER = """kind: research-group
version: 1
topology: phases
defaults: { decider: user }
agents: [{ id: worker, role: roles/worker.md }]
phases:
  - name: arbeit
    topology: solo
    actions:
      worker: { task: tasks/arbeit.md, submission: { format: text } }
"""


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    workfolder = make_workfolder(tmp_path)
    (workfolder / "arr.yaml").write_text(ARR_YAML, "utf-8")
    install_skill(workfolder, "aus", OPTIONAL.format(default="false"))
    install_skill(workfolder, "an", OPTIONAL.format(default="true"))
    install_skill(workfolder, "agenten", AGENTS_USER)
    for skill in ("fx-aus", "fx-an"):
        (workfolder / "skills" / skill / "tasks" / "extra.md").write_text("Extra.", "utf-8")
    return workfolder


def tightening(answer: dict[str, Any]) -> list[dict[str, Any]]:
    return [d for d in answer["deviations"] if str(d.get("path", "")).startswith("tightening.")]


async def test_b1_a_phase_the_defaults_drop_is_no_alarm(wf: Path) -> None:
    users = UserApi(make_api(wf))
    assert tightening(await users.estimate_run("demo", skill="fx-aus")) == []
    same = await users.estimate_run("demo", skill="fx-aus", params={"extra": False})
    assert tightening(same) == [], "the request equals the standard"


async def test_b1_a_phase_the_requests_parameters_drop_is_reported(wf: Path) -> None:
    users = UserApi(make_api(wf))
    assert tightening(await users.estimate_run("demo", skill="fx-an")) == []
    dropped = await users.estimate_run("demo", skill="fx-an", params={"extra": False})
    [dev] = tightening(dropped)
    assert dev["path"] == "tightening.removed" and dev["sensitive"] is True
    assert "Phase extra" in dev["value"]


async def test_b11_defaults_decider_null_lifts_the_agents_setting(wf: Path) -> None:
    users = UserApi(make_api(wf))
    assert tightening(await users.estimate_run("demo", skill="fx-agenten")) == []
    lifted = await users.estimate_run(
        "demo", skill="fx-agenten", config={"defaults": {"decider": None}}
    )
    [dev] = tightening(lifted)
    assert dev["path"] == "tightening.removed" and dev["sensitive"] is True
    assert "Agent worker" in dev["value"]


async def test_b11_the_inbox_card_shows_the_same_deviation(wf: Path) -> None:
    """`start_run` puts the card into the `open` line; the inbox reads the same deviation."""
    api = make_api(wf)
    users = UserApi(api)
    started = await users.start_run(
        "demo", skill="fx-agenten", task="Probe", config={"defaults": {"decider": None}}
    )
    item = next(i for i in await api.list_approvals() if i.run.id == started["run_id"])
    [dev] = [d for d in item.deviations if d.path.startswith("tightening.")]
    assert (dev.path, dev.sensitive, dev.value_text) == (
        "tightening.removed",
        True,
        "für Agent worker",
    )
    assert item.phase_plan is not None and [r.name for r in item.phase_plan.rows] == ["arbeit"]
