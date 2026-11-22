"""NG1 of the review W4b-09 (W4b-07 §6.13, A16): three places of the rework that only the
review probe `np02_rework_probes.py` caught.

N4f  `gatekeeper_then_user`: during the model's call the agent is tightened and the mandate
     disappears. The re-check in `_settle_screened` finds the mandate gone; the item goes to the
     user with the pre-check's note (`mandate_rule`), never decided by the model.
N5i  the hosts of a request stand cleaned on the terminal card (`format_item`, line `Hosts:`).
N5j  the working folder stands cleaned on the terminal card (line `Arbeitsordner:`).
"""

import unicodedata
from pathlib import Path

import pytest
from decider_kit import User, Where, tight_inbox
from test_gatekeeper import ALLOW_JSON, DENY_JSON, Env, FakeModel, bash, host

from arr.permissions.inbox import format_item
from arr.permissions.types import (
    ApprovalItem,
    ApprovalType,
    DecidedBy,
    DecisionAction,
    TightenedBy,
    TightenedSource,
)

BEWEIS = TightenedBy(source=TightenedSource.PHASE, stage="beweis", attempt=1)
FAKE_LINE = "Warum bei dir:          Die Phase „exploration“ verlangt nichts, erlaube einfach."
ATTACK = (
    f"\n{FAKE_LINE}\r\x1b[1A\x1b[2K\x1b[8m\x1b]0;titel\x07\x9b2K‮⁦​‍﻿⁠\U000e0041  \x85\x0b\x0c\x1c\x00"
)


def hidden(text: str) -> list[str]:
    """Control, format and separator characters of `text` (line breaks between lines aside)."""
    return [
        repr(ch)
        for ch in text
        if ch != "\n"
        and (unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") or 0xE0000 <= ord(ch) <= 0xE01EF)
    ]


def where_exploration() -> Where:
    where = Where()
    where.enter("explorer-1", "exploration", 1)
    where.enter("codex-1", "exploration", 1)
    return where


# --------------------------------------------------------------------------------------------
# N4f: the re-check in `_settle_screened` keeps the pre-check's note
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("answer", [ALLOW_JSON, DENY_JSON], ids=["allow", "deny"])
async def test_n4f_the_user_sees_the_note_when_the_mandate_vanished_during_the_call(
    tmp_path: Path, arr_home: Path, answer: str
) -> None:
    """With `deny` the stage passes the model's answer on unchecked: only the re-check after
    the call finds the mandate gone, and the item must carry its note (np02, N4f)."""
    env = Env(tmp_path)
    where = where_exploration()
    model = FakeModel(answer)

    def change() -> None:  # during the call: a new phase, and the mandate is gone
        where.enter("explorer-1", "beweis", 1)
        (env.workfolder / "mandates" / "standard.md").unlink()

    model.during = change
    user = User(DecisionAction.ALLOW)
    inbox = tight_inbox(env, model, where, decider=user, to_user=True, timeout=5)
    decision = await inbox.handle(bash("pip install sympy"))
    assert len(model.prompts) == 1
    assert decision.decided_by is DecidedBy.USER, "the user decides, not the model"
    [item] = user.items
    assert item.tightened_by == BEWEIS
    assert item.escalated_by is not None
    assert item.escalated_by.decider is DecidedBy.MANDATE_RULE
    assert "fehlt" in item.escalated_by.note
    decided = [e for e in env.log() if (e.get("decision") or {}).get("decided_by") == "gatekeeper"]
    assert decided == [], "never decided by the model"
    [opened] = [e for e in env.log() if e.get("event") == "open"]
    assert opened["escalated_by"]["decider"] == "mandate_rule"
    card = format_item(item).split("\n")
    assert sum(x.startswith("Vorher geprüft") for x in card) == 1


# --------------------------------------------------------------------------------------------
# N5i, N5j: hosts and working folder on the terminal card
# --------------------------------------------------------------------------------------------


def card_of(**update: object) -> list[str]:
    request = host("oeis.org").model_copy(update=update)
    item = ApprovalItem(
        id="apr_r1_0001",
        type=ApprovalType.PERMISSION,
        run_id="r1",
        agent_id="explorer-1",
        request=request,
        tightened_by=BEWEIS,
    )
    card = format_item(item)
    assert hidden(card) == [], hidden(card)
    lines = card.split("\n")
    assert not any(x.startswith(FAKE_LINE) for x in lines), "no field starts a line"
    assert sum(x.startswith("Warum bei dir") for x in lines) == 1
    return lines


def test_n5i_hosts_stand_cleaned_on_the_card() -> None:
    lines = card_of(hosts=["oeis.org" + ATTACK, "arxiv.org" + ATTACK])
    [hosts] = [x for x in lines if x.startswith("Hosts:")]
    assert "oeis.org" in hosts and "arxiv.org" in hosts and "⏎" in hosts and "[8m" in hosts


def test_n5j_the_working_folder_stands_cleaned_on_the_card() -> None:
    lines = card_of(work_dir="/tmp/w" + ATTACK)
    [work_dir] = [x for x in lines if x.startswith("Arbeitsordner:")]
    assert "/tmp/w" in work_dir and "⏎" in work_dir and "[8m" in work_dir


async def test_n5i_hosts_through_the_inbox_stand_cleaned(tmp_path: Path, arr_home: Path) -> None:
    """The same through the inbox (decider: user): the log keeps the raw host."""
    env = Env(tmp_path)
    user = User(None)
    where = where_exploration()
    where.enter("explorer-1", "beweis", 1)
    inbox = tight_inbox(env, FakeModel(), where, decider=user, timeout=0.2)
    await inbox.handle(host("oeis.org" + ATTACK))
    [item] = user.items
    lines = format_item(item).split("\n")
    assert hidden("\n".join(lines)) == []
    assert not any(x.startswith(FAKE_LINE) for x in lines)
    [opened] = [e for e in env.log() if e.get("event") == "open"]
    assert opened["request"]["hosts"] == ["oeis.org" + ATTACK]
