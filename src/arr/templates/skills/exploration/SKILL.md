---
name: exploration
description: Erkundet mehrere Ansätze zu einer offenen Frage parallel (Claude und ChatGPT) und bewertet sie in einer Rangliste. Nutzen, wenn unklar ist, welcher Weg trägt, ob eine Vermutung stimmt oder wie man ein Problem überhaupt angeht.
---

# Exploration (Research-Group)

Ein Ideator schlägt verschiedene Ansätze vor, oder du gibst sie vor. Explorer erkunden je einen Ansatz in wenigen Arbeitsrunden, Ranker bewerten die Ergebnisse (Kriterien `tragfaehigkeit`, `belege`, `aufwand`, Skala 1–10), und ein Zusammenfasser schreibt, was gesichert ist und was als Nächstes lohnt.

## Ablauf in Phasen

| Phase | Wer | Abgabe | Weiter |
|---|---|---|---|
| ideen | ideator | `list` „ideen“ mit `n_ideas` Einträgen | entfällt, wenn du `ideas` vorgibst |
| exploration | explorer-1 … reihum über die Liste, je Ansatz `turns_per_idea` Arbeitsrunden | `text` je Arbeitsrunde | Ranker bewerten (`rating`) und ordnen → zusammenfassung |
| zusammenfassung | summarizer | `text` | Ende; entfällt mit `summarizer: none` |

`exchange` wählt die Variante der Phase `exploration`: getrennt (`none`), mit den Kurzfassungen der anderen aus der vorigen Arbeitsrunde (`digest`, Phase `exploration-digest`) oder im Gespräch in `#werkstatt` (`channel`, Phase `exploration-kanal`). Gibt der Ideator mehr Ansätze ab als `n_ideas`, werden alle erkundet.

## Wann sinnvoll

- Die Frage ist offen: Man weiß noch nicht, welcher Ansatz trägt oder ob die Aussage stimmt.
- Du willst mehrere Wege vergleichen, statt dich früh auf einen festzulegen.

Nicht sinnvoll, wenn der Weg schon klar ist (nimm `proof`) oder ein Beweis nur geprüft werden soll (nimm `proof-review`).

## task.md

- die Frage, genau formuliert, mit Notation und bekanntem Stand
- was ein gutes Ergebnis wäre (Beweisweg, Gegenbeispiel, Datenlage, Literatur)
- Ansätze, die schon versucht wurden oder ausgeschlossen sind
- eigene Ansätze gibst du als Parameter `ideas` mit

## Parameter

| Name | Standard | Bereich | Bedeutung |
|---|---|---|---|
| ideas | leer | | vorgegebene Ansätze (Liste von Texten); leer: der Ideator schlägt welche vor |
| n_ideas | 3 | 1–4 | Anzahl vorgeschlagener Ansätze |
| turns_per_idea | 2 | 1–2 | Arbeitsrunden je Ansatz |
| exchange | none | | `none`: getrennt; `digest`: Kurzfassungen der anderen; `channel`: Austausch in `#werkstatt` |
| n_explorers | 2 | 1–6 | Anzahl der Explorer, abwechselnd Claude und ChatGPT; mehr Ansätze als Explorer werden reihum verteilt |
| n_rankers | 1 | 1–3 | Anzahl der Ranker, mindestens einer |
| summarizer | summarizer | | `summarizer`: Zusammenfassung am Ende; `none`: ohne |

Die Höchstwerte sind so gewählt, dass 16 Turns je Agent auch im ungünstigsten Fall reichen (ein Explorer mit 4 Ansätzen × 2 Arbeitsrunden × 2 Turns, mit Nachfrage je Arbeitsrunde). Gibst du mehr als 4 Ansätze je Explorer vor oder gibt der Ideator mehr ab, erhöhe die Turns: `overrides={"limits": {"turns_per_agent": 24}}`.

Agenten: `ideator` und `ranker-1 …` (Claude Sonnet), `explorer-1 …` (Claude Sonnet und ChatGPT im Wechsel; Modell und Profil für ChatGPT kommen aus `arr.yaml`), `summarizer` (Claude Haiku). Limits: 1 h, 16 Turns je Agent, 5 $.

## Ergebnis

`read_run_summary(run_id)` nennt die Abgaben mit ihren Kurzfassungen; die Abgaben einer Phase liest du mit `read_results(run_id, stage="ideen")` (ebenso die Phase der Erkundung und `zusammenfassung`). Im Run-Ordner liegt die Liste der Ansätze unter `output/ideen/v1/`, die Stände der Explorer, die Bewertungen und die Zusammenfassung unter `output/submissions/` (je Abgabe ein Ordner `NNN-<phase>-<agent>/`). Was davon ins Projekt geht, entscheidet der User.
