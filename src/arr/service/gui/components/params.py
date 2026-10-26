"""German label and help text of a skill parameter (docs/08 §5.4, §8.1).

The contract has no label field (`ParamInfo` of W1-06, `ParamSpec` of W1-01), so the label
travels in `doc`: its first line is the label, the following lines are the help text. A
one-line `doc` is help only, and the label falls back to the YAML key. Forms show the key
small in monospace next to the label. W3-01: a field `ParamInfo.label` replaces this.
"""

from arr.service.api_models import ParamInfo

CHOICE_LABELS = {
    "none": "keiner",
    "channel": "gemeinsamer Kanal",
    "summaries": "Zusammenfassungen",
    "majority": "Mehrheit",
    "unanimous": "einstimmig",
    "meta_judge": "Meta-Judge",
    "fresh": "jede Runde neu",
    "continue": "Sitzung fortsetzen",
}
"""German words of the choices of the start skills (`exchange`, `verdict`, `judge_memory`)."""


def param_text(param: ParamInfo) -> tuple[str, str]:
    """(label, help) of a parameter, e.g. („Turns pro Idee“, „Wie lange jeder Explorer …“)."""
    lines = [line.strip() for line in (param.doc or "").splitlines() if line.strip()]
    if len(lines) >= 2:
        return lines[0], " ".join(lines[1:])
    return param.name, lines[0] if lines else ""


def param_label(param: ParamInfo) -> str:
    """Only the label („Turns pro Idee“)."""
    return param_text(param)[0]
