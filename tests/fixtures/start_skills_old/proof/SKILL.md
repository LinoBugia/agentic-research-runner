---
name: proof
description: Entwickelt einen Beweis in drei Stufen, Strategie, Skizze, ausgearbeiteter Beweis; jede Stufe wird von Ratern (Claude und ChatGPT) bewertet und bei zu niedrigem Score nachgearbeitet. Nutzen, wenn eine Aussage feststeht, aber noch kein Beweis da ist.
---

# Proof (Research-Group)

Ein Stratege legt den Beweisweg fest, ein Beweiser arbeitet erst eine Skizze, dann den Beweis aus. Nach jeder Stufe bewerten Rater das Ergebnis (Kriterien `korrektheit`, `strenge`, `klarheit`, Skala 1–10). Liegt das Mittel unter `pass_score`, arbeitet derselbe Agent nach.

## Wann sinnvoll

- Die Aussage ist klar, ein Beweis fehlt, und du willst ihn schrittweise und geprüft aufbauen.
- Der Weg ist nicht offensichtlich: Die Strategie-Stufe zwingt dazu, ihn vor den Details festzulegen.

Nicht sinnvoll, wenn schon ein Beweis vorliegt (nimm `proof-review`) oder wenn noch unklar ist, ob die Aussage stimmt (nimm erst `exploration`).

## task.md

- die Aussage mit Voraussetzungen und Notation
- bekannte Hilfsresultate und Dateien im Projekt, die benutzt werden dürfen
- was als Ergebnis zählt, z. B. vollständiger Beweis in LaTeX, oder auch ein Gegenbeispiel

## Parameter

| Name | Standard | Bereich | Bedeutung |
|---|---|---|---|
| pass_score | 7 | 1–10 | Mindest-Score einer Stufe (Mittel der Rater) |
| max_rework | 1 | 0–1 | Nacharbeiten je Stufe |
| on_fail | continue | | `continue`: bester Versuch gilt; `stop`: Run endet; `escalate`: der User entscheidet in der GUI |
| n_raters | 2 | 1–4 | Anzahl der Rater, abwechselnd Claude und ChatGPT |

Die Höchstwerte sind so gewählt, dass 12 Turns je Agent auch im ungünstigsten Fall reichen (ein Rater bewertet höchstens 3 Stufen × 2 Versuche, mit Nachfrage). Mehr Nacharbeit (`on_fail: escalate`, „Noch ein Versuch“) braucht mehr Turns: `overrides={"limits": {"turns_per_agent": 18}}`.

Agenten: `strategist` (Claude Sonnet), `prover` (Claude Opus), `rater-1 …` (Claude Sonnet und ChatGPT im Wechsel; Modell und Profil für ChatGPT kommen aus `arr.yaml`). Limits: 1 h, 12 Turns je Agent, 5 $.

## Ergebnis

`read_run_summary` nennt je Stufe Score und Entscheidung. Die Ergebnisse der Stufen stehen in `read_results(stage="strategie")`, `"skizze"`, `"beweis"`, mit Ratings. Übernimm einen Beweis ins Projekt nur nach Rücksprache mit dem User.
