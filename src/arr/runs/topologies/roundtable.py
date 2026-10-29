"""Topology `roundtable`: the agents discuss the task in a shared group chat.

One discussion phase `diskussion` via `ctx.discuss` with the pacing of the channel: in `rounds`
mode up to `rounds` rounds, in `event` mode from a kick-off until all are `done` or it is quiet.
It also ends when all members are `done` or on a stop. Then, optionally, the `summarizer`
gets one turn and submits a summary with `submit_result(kind="summary")`.
See docs/03-skills-und-research-groups.md §4.2 and docs/06-gruppenchats.md §5.

Parameters: `rounds` (3), `channel` (default: the only channel), `summarizer` (none).
"""

from arr.errors import ConfigIssue
from arr.models import ChannelMode, ResolvedRun
from arr.runs.context import RunContext
from arr.runs.topologies import agent_param, ensure_valid, int_param, require_task

STAGE = "diskussion"
SUMMARY_STAGE = "zusammenfassung"
SUMMARY_KIND = "summary"
ROUNDS = 3


def _channel_name(resolved: ResolvedRun) -> str | None:
    value = resolved.params.get("channel")
    if value is None and len(resolved.groupchats) == 1:
        return resolved.groupchats[0].name
    return value if isinstance(value, str) else None


def check(resolved: ResolvedRun) -> list[ConfigIssue]:
    """Problems of the `roundtable` parameters and its channel (see `check_topology`)."""
    issues: list[ConfigIssue] = []
    int_param(resolved.params, "rounds", ROUNDS, issues, minimum=1)
    names = [c.name for c in resolved.groupchats]
    name = _channel_name(resolved)
    if name is None:
        text = (
            "„roundtable“ braucht einen Gruppenchat. Lege unter „groupchats“ einen an."
            if not names
            else f"Nenne den Kanal der Diskussion im Parameter „channel“ ({', '.join(names)})."
        )
        issues.append(ConfigIssue("params.channel", text))
    elif name not in names:
        issues.append(
            ConfigIssue(
                "params.channel",
                f"Parameter „channel“ nennt „{name}“, den Kanal gibt es nicht. Vorhanden: "
                f"{', '.join(names) or 'keine'}.",
            )
        )
    else:
        config = resolved.channel(name)
        if config.mode is ChannelMode.MODERATED:
            issues.append(
                ConfigIssue(
                    "params.channel",
                    f"Der Kanal „{name}“ ist moderiert. Die Taktung „moderated“ ist noch nicht "
                    "verfügbar, nimm „rounds“ oder „event“.",
                )
            )
        if config.open_from not in (None, STAGE):
            issues.append(
                ConfigIssue(
                    "params.channel",
                    f"Der Kanal „{name}“ öffnet erst ab Stufe „{config.open_from}“. Bei "
                    f"„roundtable“ ist er ab Beginn offen: entferne „open_from“ oder setze "
                    f"„open_from: {STAGE}“.",
                )
            )
    summarizer = agent_param(resolved, "summarizer", issues)
    if summarizer and name in names and summarizer not in resolved.channel(name).members:
        issues.append(
            ConfigIssue(
                "params.summarizer",
                f"Der Summarizer „{summarizer}“ ist nicht Mitglied im Kanal „{name}“ und kann "
                "die Diskussion nicht lesen. Nimm ihn in „members“ auf.",
            )
        )
    return issues


async def run(ctx: RunContext) -> None:
    """Run `roundtable` (docs/03 §4.2)."""
    ensure_valid(ctx, check)
    params = ctx.params
    channel = _channel_name(ctx.config)
    assert channel is not None, "checked by ensure_valid"
    rounds = int(params.get("rounds") or ROUNDS)
    summarizer = params.get("summarizer")
    task = require_task(ctx)
    context = {"task": task, "channel": channel}
    prompt = (
        f"## Aufgabe\n\n{task}\n\nBearbeitet die Aufgabe gemeinsam in #{channel}. Lies die neuen "
        "Nachrichten, arbeite weiter und poste deinen Beitrag. Hast du gerade nichts "
        "beizutragen, poste `pass`. Bist du fertig, melde `done` mit set_status und einem "
        "kurzen Fazit."
    )
    await ctx.discuss(channel, prompt, stage=STAGE, rounds=rounds, context=context)
    if ctx.stop_requested or not summarizer:
        return
    await ctx.turn(
        summarizer,
        f"Die Diskussion in #{channel} ist beendet. Fasse sie zusammen: Ergebnisse, offene "
        f'Punkte, Dissens. Gib die Zusammenfassung mit `submit_result(kind="{SUMMARY_KIND}")` '
        "ab.",
        stage=SUMMARY_STAGE,
        context=context,
    )
