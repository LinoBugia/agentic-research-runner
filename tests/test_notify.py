"""macOS notifications (W3-03 A8): the text goes as an argument, never into the script, no full
command, errors hold nothing up. No test here shows a real notification: `send` is replaced,
and the one test with a real `osascript` runs a script without `display notification`."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from arr.models import Provider, RunStatus, Starter, TopologyKind
from arr.permissions.types import ApprovalType, RequestCategory
from arr.service.api_client import EventHub
from arr.service.api_models import (
    ApprovalAgentRef,
    ApprovalEvent,
    ApprovalItem,
    ApprovalRunRef,
    EscalationOptionInfo,
    NotificationEvent,
    RiskInfo,
    RiskLevel,
    RunStatusEvent,
    RunSummary,
    SizeInfo,
    Vendor,
)
from arr.service.notify import (
    SCRIPT,
    Notifier,
    message_for,
    osascript_args,
    send_notification,
    switched_on,
)
from arr.service.state import write_app_settings

SECRET = "sk-ant-api03-SECRETSECRETSECRET"
ATTACK = '" & (do shell script "touch PWNED") & "'


def permission(**fields: Any) -> ApprovalItem:
    data: dict[str, Any] = {
        "id": "apr_1",
        "type": ApprovalType.PERMISSION,
        "created_at": 1.0,
        "gui_url": "http://x/freigaben/apr_1",
        "title": "explorer-1 möchte etwas installieren",
        "run": ApprovalRunRef(id="r1", name="Collatz", project="demo", gui_url="http://x/runs/r1"),
        "agent": ApprovalAgentRef(
            id="explorer-1",
            role_label="Explorer",
            model="m",
            model_label="M",
            provider=Provider.CLAUDE,
            vendor=Vendor.ANTHROPIC,
        ),
        "command": f"pip install x --token {SECRET}",
        "risk": RiskInfo(level=RiskLevel.MEDIUM, text="Installation."),
        "categories": [RequestCategory.INSTALL],
        "deadline": 1200.0,
        **fields,
    }
    return ApprovalItem(**data)


def created(item: ApprovalItem, kind: str = "approval.created") -> ApprovalEvent:
    return ApprovalEvent(seq=1, ts=1.0, run_id="r1", type=kind, item=item)  # type: ignore[arg-type]


def test_a8_text_names_agent_run_and_kind_but_not_the_command() -> None:
    text = message_for(created(permission()), 0.0)
    assert text is not None
    assert "explorer-1" in text and "Collatz" in text and "Installation" in text
    assert "läuft in 20 min ab" in text
    assert "pip install" not in text and SECRET not in text and "token" not in text


def test_w3_08_u8_text_names_the_risk_with_its_explanation() -> None:
    text = message_for(created(permission()), 0.0)
    assert text is not None
    assert "fragt nach: Installation · Risiko mittel: Installation. · Collatz" in text


def test_a8_secrets_and_control_characters_leave_every_text() -> None:
    item = permission(
        run=ApprovalRunRef(
            id="r1", name=f"Run\n{SECRET}\x07", project="demo", gui_url="http://x/runs/r1"
        )
    )
    text = message_for(created(item), 0.0)
    assert text is not None and SECRET not in text and "\n" not in text and "\x07" not in text


def test_a8_escalation_and_other_events() -> None:
    item = ApprovalItem(
        id="apr_2",
        type=ApprovalType.ESCALATION,
        created_at=1.0,
        gui_url="http://x",
        title="Stufe",
        run=ApprovalRunRef(id="r1", name="Ramsey", project="demo", gui_url="http://x"),
        stage="skizze",
        topology=TopologyKind.WATERFALL,
        options=[EscalationOptionInfo(key="end", label="Run beenden", help="…")],
    )
    text = message_for(created(item), 0.0)
    assert text is not None and "Ramsey" in text and "skizze" in text
    assert message_for(created(permission(), "approval.updated"), 0.0) is None
    assert message_for(created(permission(), "approval.decided"), 0.0) is None


def run_event(status: RunStatus, previous: RunStatus | None) -> RunStatusEvent:
    summary = RunSummary(
        id="r1",
        name="Collatz",
        project="demo",
        topology=TopologyKind.SOLO,
        status=status,
        active=status.is_active,
        starter=Starter.GUI,
        created_at=1.0,
        size=SizeInfo(actual_bytes=0, logical_bytes=0),
        gui_url="http://x",
    )
    return RunStatusEvent(seq=1, ts=1.0, run_id="r1", run=summary, previous=previous)


def test_a8_run_end_gets_a_notification_only_when_it_ends() -> None:
    text = message_for(run_event(RunStatus.DONE, RunStatus.RUNNING), 0.0)
    assert text == "Run fertig: Collatz"
    assert message_for(run_event(RunStatus.STOPPED, RunStatus.FINALIZING), 0.0)
    assert message_for(run_event(RunStatus.RUNNING, RunStatus.PREPARING), 0.0) is None
    assert message_for(run_event(RunStatus.DONE, None), 0.0) is None


def test_a8_text_is_an_argument_behind_the_end_of_options() -> None:
    args = osascript_args(ATTACK)
    script = [args[i + 1] for i, arg in enumerate(args) if arg == "-e"]
    assert script == list(SCRIPT) and script[0] == "on run argv"
    assert not any(ATTACK in line for line in script)
    assert args[args.index("--") + 1 :] == [ATTACK, "arr"]


@pytest.mark.macos
@pytest.mark.skipif(sys.platform != "darwin", reason="osascript exists only on macOS")
def test_a8_applescript_in_the_text_is_not_executed(tmp_path: Path) -> None:
    # the script of the notifier, with `return` instead of the notification itself
    script = tuple(
        "return (item 1 of argv) & (item 2 of argv)" if line.startswith("display") else line
        for line in SCRIPT
    )
    for text in (ATTACK, '-e "do shell script \\"touch PWNED\\""', "-s"):
        args = osascript_args(text, "titel", script=script)
        done = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, check=True)
        assert done.stdout.strip() == text + "titel"
    assert not (tmp_path / "PWNED").exists()


def test_a8_nothing_happens_without_osascript_or_when_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    monkeypatch.setattr("arr.service.notify.subprocess.run", lambda *a, **k: calls.append(a))
    monkeypatch.setenv("ARR_NOTIFY", "off")
    send_notification("text")
    monkeypatch.delenv("ARR_NOTIFY")
    monkeypatch.setattr("arr.service.notify.shutil.which", lambda _name: None)
    send_notification("text")
    assert not calls
    monkeypatch.setattr("arr.service.notify.shutil.which", lambda _name: "/usr/bin/osascript")
    send_notification("text")
    assert len(calls) == 1 and calls[0][0][0] == "osascript"


async def test_a8_a_failing_sender_holds_nothing_up() -> None:
    hub = EventHub()
    sent: list[str] = []

    def send(text: str, _title: str) -> None:
        sent.append(text)
        raise OSError("kein osascript")

    notifier = Notifier(hub, send)
    notifier.start()
    subscriber = hub.add(None, None)
    hub.publish(created(permission()))
    hub.publish(created(permission(id="apr_2", gui_url="http://x/2")))
    for _ in range(500):
        if len(sent) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(sent) == 2
    assert len(subscriber.drain()) == 2
    await notifier.stop()
    hub.publish(created(permission()))
    await asyncio.sleep(0.01)
    assert len(sent) == 2


def test_tests_never_show_real_notifications() -> None:
    assert os.environ.get("ARR_NOTIFY") == "off"


async def pumped(events: list[Any]) -> list[str]:
    """What a `Notifier` sends for `events`; a marker behind them shows it has seen all."""
    hub = EventHub()
    sent: list[str] = []
    notifier = Notifier(hub, lambda text, _title: sent.append(text))
    notifier.start()
    for event in [*events, created(permission(id="apr_ende", gui_url="http://x/e"))]:
        hub.publish(event)
    for _ in range(500):
        if any("fragt nach" in t for t in sent):
            break
        await asyncio.sleep(0.01)
    await notifier.stop()
    return sent


FINISHED = run_event(RunStatus.DONE, RunStatus.RUNNING)


async def test_the_switch_for_finished_runs_is_read_at_every_event(arr_home: Path) -> None:
    write_app_settings({"notifications": {"run_finished": False}})
    assert not any(t.startswith("Run fertig") for t in await pumped([FINISHED]))
    write_app_settings({"notifications": {"run_finished": True}})
    assert any(t.startswith("Run fertig") for t in await pumped([FINISHED]))


async def test_a_missing_switch_means_on(arr_home: Path) -> None:
    write_app_settings({"notifications": {"storage_low": False}})
    assert any(t.startswith("Run fertig") for t in await pumped([FINISHED]))
    write_app_settings({"open_in": "tab"})
    assert any(t.startswith("Run fertig") for t in await pumped([FINISHED]))


async def test_a_new_approval_comes_whatever_the_switches_say(arr_home: Path) -> None:
    write_app_settings({"notifications": {"approval": False, "run_finished": False}})
    assert switched_on(created(permission()))
    assert any("fragt nach" in t for t in await pumped([]))


def test_only_finished_runs_have_a_switch_of_their_own(arr_home: Path) -> None:
    """The others of §5.11 raise nothing here yet; see the report of W3-04.3."""
    write_app_settings({"notifications": {e.value: False for e in NotificationEvent}})
    assert not switched_on(FINISHED)
    assert switched_on(created(permission()))
