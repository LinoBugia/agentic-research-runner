"""Q1: exact signatures from the installed SDK (inspect + help()), no model calls."""

import importlib.metadata
import inspect
import pydoc

import openai_codex
from common import dump
from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    AsyncThread,
    AsyncTurnHandle,
    CodexConfig,
    Sandbox,
    TurnResult,
)
from openai_codex.generated.v2_all import ThreadTokenUsage, TokenUsageBreakdown

TARGETS = {
    "AsyncCodex.__init__": AsyncCodex.__init__,
    "AsyncCodex.thread_start": AsyncCodex.thread_start,
    "AsyncCodex.thread_resume": AsyncCodex.thread_resume,
    "AsyncCodex.thread_fork": AsyncCodex.thread_fork,
    "AsyncCodex.models": AsyncCodex.models,
    "AsyncCodex.account": AsyncCodex.account,
    "AsyncCodex.login_chatgpt_device_code": AsyncCodex.login_chatgpt_device_code,
    "AsyncThread.turn": AsyncThread.turn,
    "AsyncThread.run": AsyncThread.run,
    "AsyncThread.read": AsyncThread.read,
    "AsyncTurnHandle.steer": AsyncTurnHandle.steer,
    "AsyncTurnHandle.interrupt": AsyncTurnHandle.interrupt,
    "AsyncTurnHandle.stream": AsyncTurnHandle.stream,
    "AsyncTurnHandle.run": AsyncTurnHandle.run,
    "CodexConfig": CodexConfig,
}


def main() -> None:
    lines = [
        f"openai-codex {importlib.metadata.version('openai-codex')}",
        f"openai-codex-cli-bin {importlib.metadata.version('openai-codex-cli-bin')}",
        "",
    ]
    for name, obj in TARGETS.items():
        lines.append(f"{name}{inspect.signature(obj)}")
    lines.append("")
    lines.append(f"ApprovalMode: {[m.value for m in ApprovalMode]}")
    lines.append(f"Sandbox: {[m.value for m in Sandbox]}")
    lines.append(f"TurnResult fields: {list(TurnResult.__dataclass_fields__)}")
    lines.append(f"ThreadTokenUsage fields: {list(ThreadTokenUsage.model_fields)}")
    lines.append(f"TokenUsageBreakdown fields: {list(TokenUsageBreakdown.model_fields)}")
    text = "\n".join(lines)
    print(text)
    dump("q01_signatures.txt", text)

    helps = []
    for obj in (openai_codex, AsyncCodex, AsyncThread, AsyncTurnHandle, CodexConfig):
        helps.append(pydoc.render_doc(obj, renderer=pydoc.plaintext))
    path = dump("q01_help.txt", "\n\n".join(helps))
    print(f"\nhelp() -> {path}")


if __name__ == "__main__":
    main()
