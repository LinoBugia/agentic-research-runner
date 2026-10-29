"""Post a question as `user` into a channel of a running run (throwaway helper for W2-05).

    uv run python spikes/w2_05/ask.py <run-ordner> <kanal> <agent> "<frage>"

The message has `kind: question` and `to: [<agent>]` (docs/06 §10, sender `user`, via `gui`):
a working agent gets it as a hint mid-turn, a waiting agent in the inbox of its next turn.
Never imported by arr.
"""

from __future__ import annotations

import sys
from pathlib import Path

from arr.room.store import RoomStore
from arr.room.types import MessageKind, MessageVia


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    run_dir, channel, agent, body = argv
    store = RoomStore(Path(run_dir).expanduser())
    msg = store.post(
        channel,
        body,
        sender="user",
        kind=MessageKind.QUESTION,
        to=[agent],
        via=MessageVia.GUI,
    )
    print(f"Frage #{msg.id} an {agent} in #{channel} gestellt.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
