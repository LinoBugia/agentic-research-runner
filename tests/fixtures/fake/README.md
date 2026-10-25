# Fixtures: FakeBackend

Skripte für das `FakeBackend` (`src/arr/backends/fake.py`), Format siehe Modul-Docstring und
Abschlussbericht W1-04 §3.

| Datei | Inhalt |
|---|---|
| `explorer.yaml` | Rolle Explorer: denkt, rechnet (Tool), postet eine Idee, gibt ein Ergebnis ab, meldet `done` |
| `critic.yaml` | Rolle Kritiker: liest das Postfach, widerspricht, stellt eine Frage; Turn 2 beantwortet eine Frage an ihn |
| `rater.yaml` | Rolle Rater: liest den Auftrag, bewertet das Ergebnis aus dem Kontextpaket, stimmt ab |
| `all_steps.yaml` | alle Schritt-Arten in einem Turn (Test A3) |
| `transcript_sample.jsonl` / `.md` | festes Transkript und seine gerenderte Fassung (Snapshot-Test) |
