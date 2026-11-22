"""Decision 68 (W4b-07 §6.12, A15): only "\\n" ends a line of `approvals.jsonl`.

`json.dumps(ensure_ascii=False)` writes U+2028, U+2029 and NEL (U+0085) raw inside a JSON
string; `str.splitlines()` split such a line, `json.loads` failed, and with it
`ApprovalLog.entries()` (read by `ApprovalInbox._next_id`) and `RunStartsArea.recover`. An agent
could so stop the grey zone of a whole run with one character in its reason (fail closed;
review W4b-09, finding 5, probe `np03_log_split.py`).

Cases A-D of `np03` per separator, with `decider: user` and a user who allows everything (any
allow would show): no exception, every grey request reaches the user (no deny without a card),
and every outcome equals the outcome with a plain character instead of the separator.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from mcp_kit import make_api, prepare_workfolder, spec
from run_kit import write_profiles
from service_kit import make_workfolder
from test_gatekeeper import Env, bash, host

from arr.models import RunStatus
from arr.permissions.inbox import ApprovalInbox
from arr.permissions.policy import ApprovalLog
from arr.permissions.types import (
    ApprovalItem,
    ApprovalStatus,
    DecidedBy,
    Decision,
    DecisionAction,
)
from arr.service.real_api.run_starts import RUN_START_KEY, is_run_start

SEPARATORS = {"U+2028": " ", "U+2029": " ", "NEL": "\x85"}
PLAIN = "x"
"""The same cases with a plain character: what the inbox does without a separator."""


class AllowAll:
    """The user's decider: allows every item it sees and records it."""

    def __init__(self) -> None:
        self.items: list[ApprovalItem] = []

    async def decide(self, item: ApprovalItem) -> Decision:
        self.items.append(item)
        return Decision(action=DecisionAction.ALLOW, decided_by=DecidedBy.USER, reason="ja")


def sequence(case: str, ch: str) -> list[Any]:
    """The requests of a case; `ch` stands in the reason of one of them."""
    if case == "A":  # the very first grey request carries the separator
        return [host("oeis.org", reason=f"a{ch}b"), host("arxiv.org")]
    if case == "B":  # an engine-allowed request with it comes first, then a clean grey one
        return [bash("ls", reason=f"a{ch}b"), host("oeis.org")]
    if case == "C":  # the counter is set first, then the separator, then a clean one
        return [host("oeis.org"), host("arxiv.org", reason=f"a{ch}b"), host("example.org")]
    # D: like A, then five clean grey requests
    return [host("oeis.org", reason=f"a{ch}b")] + [host(f"h{i}.example.org") for i in range(5)]


async def outcomes(root: Path, case: str, ch: str) -> tuple[list[tuple[str, str]], int, Env]:
    """Each request's (action, decided_by) through a fresh inbox, the items the user saw."""
    env = Env(root)
    user = AllowAll()
    inbox = ApprovalInbox(env.engine, decider=user, timeout=5)
    out = []
    for request in sequence(case, ch):
        decision = await inbox.handle(request)  # an exception fails the test
        out.append((decision.action.value, decision.decided_by.value))
    return out, len(user.items), env


@pytest.mark.parametrize("case", ["A", "B", "C", "D"])
@pytest.mark.parametrize("sep", list(SEPARATORS))
async def test_a_separator_in_agent_text_changes_no_outcome(
    tmp_path: Path, arr_home: Path, sep: str, case: str
) -> None:
    ch = SEPARATORS[sep]
    got, seen, env = await outcomes(tmp_path / "sep", case, ch)
    want, seen_plain, _ = await outcomes(tmp_path / "plain", case, PLAIN)
    assert got == want, "nothing is allowed or denied other than without the separator"
    grey = [o for o in want if o[1] == DecidedBy.USER.value]
    assert seen == seen_plain == len(grey), "every grey request reached the user"
    denied = [o for o in got if o[0] == DecisionAction.DENY.value]
    assert denied == [], "no deny without a card"
    # the log keeps the raw character, and reads back whole
    raw = (env.run_dir / "approvals.jsonl").read_text("utf-8")
    assert ch in raw
    reasons = [e["request"]["reason"] for e in env.engine.log.entries() if "request" in e]
    assert f"a{ch}b" in reasons


@pytest.mark.parametrize("sep", list(SEPARATORS))
def test_entries_reads_a_line_with_a_separator_whole(tmp_path: Path, sep: str) -> None:
    path = tmp_path / "approvals.jsonl"
    text = f"vorher{SEPARATORS[sep]}nachher"
    lines = [{"event": "open", "item_id": "a", "note": text}, {"event": "x", "item_id": "b"}]
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines), "utf-8")
    assert ApprovalLog(path).entries() == lines


# --------------------------------------------------------------------------------------------
# RunStartsArea.recover: a waiting start left by a crash ends stopped, its item obsolete
# --------------------------------------------------------------------------------------------


@pytest.fixture
def wf(tmp_path: Path, arr_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    arr_home.mkdir(parents=True, exist_ok=True)
    write_profiles(arr_home, {"fake-auth": "claude_oauth"})
    monkeypatch.setattr("arr.auth._keyring_get", lambda service, account: None)
    return prepare_workfolder(make_workfolder(tmp_path))


def log_lines(run_dir: Path) -> list[dict[str, Any]]:
    text = (run_dir / "approvals.jsonl").read_text("utf-8")
    return [json.loads(x) for x in text.split("\n") if x.strip()]


@pytest.mark.parametrize("sep", list(SEPARATORS))
async def test_recover_reads_a_log_with_a_separator(wf: Path, sep: str) -> None:
    """The separator stands in the task of the run start (its own line `open`) and in the
    reason of an agent's request in the same file."""
    ch = SEPARATORS[sep]
    api = make_api(wf)
    started = await api.request_run_start(spec(task=f"Prüfe{ch}Lemma 3."))
    pending = api.run_starts.pending[started.run_id]
    pending.lock.release()  # as if the process had died
    api.run_starts.pending.clear()
    run_dir = wf / "runs" / started.run_id
    with (run_dir / "approvals.jsonl").open("a", encoding="utf-8") as handle:
        line = {"event": "open", "item_id": "apr_r_0001", "request": {"reason": f"a{ch}b"}}
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    raw = (run_dir / "approvals.jsonl").read_text("utf-8")
    [opened] = [e for e in log_lines(run_dir) if e.get("event") == "open" and is_run_start(e)]
    assert raw.count(ch) == 2 and ch in opened[RUN_START_KEY]["task_md"]

    fresh = make_api(wf)
    assert fresh.run_starts.recover() == [started.run_id]
    obsolete = [
        e
        for e in log_lines(run_dir)
        if e.get("event") == "obsolete" and e.get("item_id") == opened["item_id"]
    ]
    assert len(obsolete) == 1 and is_run_start(obsolete[0]), "the open start is marked obsolete"
    assert (await fresh.get_run(started.run_id)).run.status is RunStatus.STOPPED
    [item] = [
        i for i in await fresh.list_approvals(run_id=started.run_id) if i.id == opened["item_id"]
    ]
    assert item.status is ApprovalStatus.OBSOLETE
