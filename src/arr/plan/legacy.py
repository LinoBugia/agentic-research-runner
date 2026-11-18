"""Message for groups of the five old topologies (Entscheidung 19 und 24 of wave 4b; docs/10 §5).

After W4b-06 the old topologies `review`, `exploration`, `roundtable`, `waterfall` and `solo`
are gone; a `group.yaml` that still names one gets a clear message with the template that
replaces it. W4b-02a builds the message; W4b-06 switches it on without changing `arr.models`
(the old fields stay there for exactly this).
"""

from arr.models import TopologyKind

__all__ = ["TEMPLATES", "old_topology_message"]

TEMPLATES: dict[TopologyKind, tuple[str, str]] = {
    TopologyKind.REVIEW: (
        "proof-review",
        "fassung → pruefung (isoliert) → diskussion (Gate signal auf verdict) → revision",
    ),
    TopologyKind.EXPLORATION: (
        "exploration",
        "ideen → exploration (parallel über die Liste ideen, Gate judge mit rank) → "
        "zusammenfassung",
    ),
    TopologyKind.ROUNDTABLE: ("roundtable", "diskussion (chat) → zusammenfassung"),
    TopologyKind.WATERFALL: (
        "proof",
        "je Stufe eine Phase solo mit Gate judge (mean, pass_score), retry bis max_visits",
    ),
    TopologyKind.SOLO: ("", "eine Phase solo mit Gate done"),
}
"""Old topology → start skill that is its template (none for `solo`) and its phases."""


def old_topology_message(kind: TopologyKind | str) -> str | None:
    """The German message for a group of an old topology, None for `phases` or an unknown
    name. Not called yet: W4b-06 switches it on (Entscheidung 19)."""
    try:
        kind = TopologyKind(kind)
    except ValueError:
        return None
    if kind not in TEMPLATES:
        return None
    skill, phases = TEMPLATES[kind]
    head = (
        f"Die Topologie „{kind.value}“ gibt es nicht mehr. Eine Group beschreibt ihren Ablauf "
        "jetzt mit „topology: phases“ und einer Liste „phases“."
    )
    if skill:
        where = (
            f" Vorlage ist der Start-Skill „{skill}“ ({phases}): Lege ihn unter „Skills“ neu "
            "an oder übernimm dessen group.yaml und trage deine Parameter ein."
        )
    else:
        where = (
            f" Für „solo“ genügt {phases}, z. B. „phases: [{{ name: arbeit, topology: solo, "
            "actions: { <agent>: { task: tasks/arbeit.md, submission: { format: text } } } }]“."
        )
    return head + where + " Eine automatische Umstellung gibt es nicht."
