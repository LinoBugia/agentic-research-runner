"""W3-07 in the GUI on `RealApi` with FakeBackends: waits with reason and time left (A7) and
the result of the secret scan in the run detail and the runs list (A10, U3)."""

import json
from pathlib import Path
from typing import Any

from run_kit import done, post, room, say, turns, wait
from test_gui_real import page_html, see
from test_gui_runs_real import EVENT_FORM, make_api, real_user, until, until_final, wf

from arr.models import RunStatus
from arr.service.api_models import AgentDisplayState, SecretScanInfo, WaitReason
from arr.service.gui.components.agent_card import card_html, remaining
from arr.service.gui.components.stage_bar import stage_bar_html
from arr.service.gui.pages.runs import run_row

__all__ = ["wf"]  # the fixture

FAKE_KEY = "sk-ant-api03-W307TESTVALUE0000"
"""Looks like a key to the scan; it must never appear in the GUI."""


async def final_status(api: Any, run_id: str) -> RunStatus:
    await until_final(api, run_id)
    return (await api.get_run(run_id)).run.status


async def scanned(api: Any, run_id: str) -> SecretScanInfo:
    """The scan runs after the final status (`run_launch`); wait for its entry."""

    async def present() -> bool:
        return (await api.get_run(run_id)).run.secret_scan is not None

    await until(present, what="secret scan")
    scan = (await api.get_run(run_id)).run.secret_scan
    assert scan is not None
    return scan


def form(**channel: Any) -> dict[str, Any]:
    data = json.loads(json.dumps(EVENT_FORM))
    data["groupchats"][0].update(channel)
    return data


async def test_a7_waits_show_reason_and_time_left(wf: Path) -> None:
    flag = "out/fertig.flag"
    scripts = {
        "a": turns(
            [
                post("main", "@b gilt das für n = 9?", kind="question"),
                room("wait_for_messages", seconds=40),
                room("wait_until", minutes=10, until_file=flag),
            ],
            [done()],
        ),
        # b answers after 6 s: a window in which a slow page still shows the wait
        "b": turns([wait(6), post("main", "Ja.", kind="answer", reply_to=2), done()]),
    }
    api = make_api(wf, scripts)
    async with real_user(api) as user:
        run_id = (await api.start_run(form())).run_id
        await user.open(f"/runs/{run_id}")
        html = await see(user, "wartet auf Antwort von b", retries=400)
        assert "noch" in html and 'data-until="' in html, "time left, counted down"
        agents = {a.id: a for a in await api.list_agents(run_id)}
        assert agents["a"].waiting is not None
        assert agents["a"].waiting.reason is WaitReason.MESSAGES
        await see(user, f"wartet auf „{flag}“", retries=400)
        detail = await api.get_run(run_id)
        assert detail.discussion is not None
        assert detail.discussion.text == "wartet auf wartende Agenten"
        await see(user, "#main: wartet auf wartende Agenten", retries=200)
        work = wf / "runs" / run_id / "agents" / "a" / "work"
        (work / "out").mkdir(exist_ok=True)
        (work / "out" / "fertig.flag").write_text("")
        assert await final_status(api, run_id) is RunStatus.DONE


async def test_a7_idle_agent_shows_the_next_round(wf: Path) -> None:
    scripts = {
        "a": turns([post("main", "Mein Ergebnis", kind="result")]),
        "b": turns([say("überlege")], [done()]),
    }
    api = make_api(wf, scripts)
    async with real_user(api) as user:
        run_id = (await api.start_run(form(idle_after_s=10))).run_id  # window for a slow page
        await user.open(f"/runs/{run_id}")
        await see(user, "untätig · Runde 1 von", retries=400)
        await see(user, "Ruhe, Runde 1", retries=200)

        async def b_idle_with_until() -> bool:
            b = {x.id: x for x in await api.list_agents(run_id)}["b"]
            return b.waiting is not None and b.waiting.until is not None

        await until(b_idle_with_until, what="b waits for the round")
        assert await final_status(api, run_id) is RunStatus.DONE


async def test_a10_scan_clean_in_the_head(wf: Path) -> None:
    scripts = {"a": turns([done()]), "b": turns([done()])}
    api = make_api(wf, scripts)
    async with real_user(api) as user:
        run_id = (await api.start_run(form())).run_id
        assert await final_status(api, run_id) is RunStatus.DONE
        assert (await scanned(api, run_id)).clean
        await user.open(f"/runs/{run_id}")
        await see(user, "Secret-Scan sauber", retries=200)
        await user.open("/runs")
        assert "Treffer" not in page_html(user)


async def test_a10_scan_hits_with_file_and_line_never_the_value(wf: Path) -> None:
    scripts = {
        "a": turns([post("main", f"Hier ein Schlüssel: {FAKE_KEY}"), done()]),
        "b": turns([done()]),
    }
    api = make_api(wf, scripts)
    async with real_user(api) as user:
        run_id = (await api.start_run(form())).run_id
        assert await final_status(api, run_id) is RunStatus.DONE
        scan = await scanned(api, run_id)
        assert not scan.clean and scan.findings
        assert FAKE_KEY not in scan.model_dump_json(), "the API never carries the value"
        await user.open(f"/runs/{run_id}?tab=ablauf")
        html = await see(user, f"Secret-Scan: {len(scan.findings)} Treffer.", retries=200)
        banner = html[html.index("Secret-Scan:") :].split("\n")[0]
        first = scan.findings[0]
        assert f"{first.file} Zeile {first.line}" in banner
        assert FAKE_KEY not in banner and "W307TESTVALUE" not in banner
        await user.open("/runs")
        listing = await see(user, f"Secret-Scan: {len(scan.findings)} Treffer", retries=200)
        row = listing[listing.index("Secret-Scan:") :].split("\n")[0]
        assert "W307TESTVALUE" not in row


# --------------------------------------------------------------------------------------------
# The HTML pieces without a page
# --------------------------------------------------------------------------------------------


def test_card_stage_bar_and_row_render_waits_and_the_scan(wf: Path) -> None:
    from arr.service.api_models import (
        AgentWait,
        DiscussionState,
        ScanFinding,
        StageInfo,
        StageStatus,
    )
    from arr.service.mock_api import MockApi

    assert remaining(32) == "0:32" and remaining(12 * 60) == "12 min"
    mock = MockApi()
    run = next(r for r in mock.state.runs.values() if r.agents)
    agent = next(iter(run.agents.values()))
    waiting = AgentWait(reason=WaitReason.MESSAGES, text="wartet auf Antwort von b", until=1032)
    card = card_html(
        agent.model_copy(update={"waiting": waiting, "state": AgentDisplayState.WORKING}), now=1000
    )
    assert "wartet auf Antwort von b" in card and 'data-until="1032"' in card and "0:32" in card
    stage = StageInfo(key="diskussion", label="diskussion", index=1, status=StageStatus.RUNNING)
    discussion = DiscussionState(channel="main", reason="idle", text="Ruhe, Ende", until=1250)
    bar = stage_bar_html([stage], run.topology, discussion, now=1000)
    assert "#main: Ruhe, Ende · noch" in bar and "4:10" in bar
    summary = mock._summary(run).model_copy(
        update={
            "secret_scan": SecretScanInfo(
                clean=False, findings=[ScanFinding(file="x.md", line=3, kind="anthropic_key")]
            )
        }
    )
    cells = run_row(summary, 1000, None).cells
    assert "Secret-Scan: 1 Treffer" in cells[0]
