BEGIN TRANSACTION;
CREATE TABLE agent_report(agent TEXT PRIMARY KEY, status TEXT, ts REAL);
INSERT INTO "agent_report" VALUES('judge-1','done',1.79544162874090496018e+09);
INSERT INTO "agent_report" VALUES('judge-2','done',1.79544162874261399309e+09);
CREATE TABLE agent_state(agent TEXT PRIMARY KEY, status TEXT, wakeups INTEGER,
    note TEXT, ts REAL);
INSERT INTO "agent_state" VALUES('author','done',0,'result',1.7954416286755510157e+09);
INSERT INTO "agent_state" VALUES('judge-1','done',0,'reported',1.79544162874470902835e+09);
INSERT INTO "agent_state" VALUES('judge-2','done',0,'reported',1.79544162874769774873e+09);
CREATE TABLE agent_wait(agent TEXT, kind TEXT, since REAL, until REAL,
    file TEXT, result_from TEXT, PRIMARY KEY (agent, kind));
CREATE TABLE assignment(agent TEXT, turn INTEGER, stage TEXT, prompt TEXT,
    context TEXT, ts REAL);
INSERT INTO "assignment" VALUES('author',1,'artefakt','## Aufgabe

Zeige, dass Lemma A für alle n ≡ 1 mod 4 gilt.

Schreibe das Artefakt zu dieser Aufgabe, z. B. einen Beweis, als Datei in deinem Arbeitsordner, und gib es mit `submit_result(kind="artifact", path="<Datei>")` ab (oder den Text in `body`). Danach prüfen es unabhängige Judges.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "version": 1}',1.79544162860170786188e+09);
INSERT INTO "assignment" VALUES('judge-1',1,'pruefung','Prüfrunde 1, Fassung 1: Prüfe das Artefakt unabhängig von den anderen Judges. Aufgabe, Fassung und Einwände stehen in `get_assignment()`. Gib dein Gutachten mit `submit_result(kind="review")` ab.

## So prüfst du

- **Unabhängig:** In der Prüfung siehst du die Gutachten der anderen Judges nicht, und #judges ist zu. Erst in der Diskussion öffnet der Kanal; dort stehen die Gutachten im Auftrag.
- **Auftrag:** `get_assignment()` nennt Aufgabe, Fassung (`artifact.path`, absolut), bei einer Überarbeitung den Diff (`diff.path`), die Einwände mit Status und die Notizen der Vorrunde.
- **Gutachten:** Gib es mit `submit_result(kind="review")` ab, als Text in `body` oder als Datei in `path`. Halte diese Form ein:
  - eine Zeile `Urteil: accept`, `Urteil: minor`, `Urteil: major` oder `Urteil: reject`;
  - einen Abschnitt `## Einwände` mit einem Punkt je Einwand (`- …`). Zu einem Einwand aus dem Auftrag schreibst du `- [E3] behoben: …`, `- [E3] offen: …` oder `- [E3] strittig: …`;
  - einen Abschnitt `## Für die nächste Runde`: kurz, was du dir merken willst.
- **Urteil ändern:** Überzeugt dich die Diskussion, gib dort ein neues Gutachten in derselben Form ab (`submit_result(kind="review")`). Es zählt dein letztes Gutachten der Runde.
','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "round": 1, "version": 1, "artifact": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v1/artefakt.md", "excerpt": "Beweis v1\nZeile 2\n", "complete": true}, "verdicts": ["accept", "minor", "major", "reject"], "objections": [], "previous": []}',1.79544162861366380923e+09);
INSERT INTO "assignment" VALUES('judge-2',1,'pruefung','Prüfrunde 1, Fassung 1: Prüfe das Artefakt unabhängig von den anderen Judges. Aufgabe, Fassung und Einwände stehen in `get_assignment()`. Gib dein Gutachten mit `submit_result(kind="review")` ab.

## So prüfst du

- **Unabhängig:** In der Prüfung siehst du die Gutachten der anderen Judges nicht, und #judges ist zu. Erst in der Diskussion öffnet der Kanal; dort stehen die Gutachten im Auftrag.
- **Auftrag:** `get_assignment()` nennt Aufgabe, Fassung (`artifact.path`, absolut), bei einer Überarbeitung den Diff (`diff.path`), die Einwände mit Status und die Notizen der Vorrunde.
- **Gutachten:** Gib es mit `submit_result(kind="review")` ab, als Text in `body` oder als Datei in `path`. Halte diese Form ein:
  - eine Zeile `Urteil: accept`, `Urteil: minor`, `Urteil: major` oder `Urteil: reject`;
  - einen Abschnitt `## Einwände` mit einem Punkt je Einwand (`- …`). Zu einem Einwand aus dem Auftrag schreibst du `- [E3] behoben: …`, `- [E3] offen: …` oder `- [E3] strittig: …`;
  - einen Abschnitt `## Für die nächste Runde`: kurz, was du dir merken willst.
- **Urteil ändern:** Überzeugt dich die Diskussion, gib dort ein neues Gutachten in derselben Form ab (`submit_result(kind="review")`). Es zählt dein letztes Gutachten der Runde.
','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "round": 1, "version": 1, "artifact": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v1/artefakt.md", "excerpt": "Beweis v1\nZeile 2\n", "complete": true}, "verdicts": ["accept", "minor", "major", "reject"], "objections": [], "previous": []}',1.79544162861845091327e+09);
INSERT INTO "assignment" VALUES('judge-1',2,'diskussion','Runde 1 von 1 in #judges.

Diskussion zu Prüfrunde 1 in #judges: Die Gutachten aller Judges stehen jetzt in `get_assignment()` unter `reviews`, die Einwände unter `objections`. Besprecht strittige Einwände und das Urteil. Ändert sich dein Urteil, gib ein neues Gutachten mit `submit_result(kind="review")` und der Zeile `Urteil: …` ab. Hast du nichts mehr beizutragen, melde `done`.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "round": 1, "version": 1, "artifact": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v1/artefakt.md", "excerpt": "Beweis v1\nZeile 2\n", "complete": true}, "verdicts": ["accept", "minor", "major", "reject"], "objections": [{"id": "E1", "text": "L\u00fccke in Lemma 2", "status": "offen", "by": "Mitglied A", "round": 1}, {"id": "E2", "text": "Tippfehler in Zeile 2", "status": "offen", "by": "Mitglied B", "round": 1}], "previous": [{"by": "Mitglied A", "round": 0, "next_round": "Lemma 2 pr\u00fcfen"}, {"by": "Mitglied B", "round": 0, "next_round": "nichts"}], "reviews": [{"by": "Mitglied A", "verdict": "major", "path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/review/runde-1/gutachten-mitglied-a.md", "excerpt": "Urteil: major\n\n## Einw\u00e4nde\n- L\u00fccke in Lemma 2\n\n## F\u00fcr die n\u00e4chste Runde\nLemma 2 pr\u00fcfen"}, {"by": "Mitglied B", "verdict": "minor", "path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/review/runde-1/gutachten-mitglied-b.md", "excerpt": "Urteil: minor\n\n## Einw\u00e4nde\n- Tippfehler in Zeile 2\n\n## F\u00fcr die n\u00e4chste Runde\nnichts"}]}',1.79544162864295504533e+09);
INSERT INTO "assignment" VALUES('judge-2',2,'diskussion','Runde 1 von 1 in #judges.

Diskussion zu Prüfrunde 1 in #judges: Die Gutachten aller Judges stehen jetzt in `get_assignment()` unter `reviews`, die Einwände unter `objections`. Besprecht strittige Einwände und das Urteil. Ändert sich dein Urteil, gib ein neues Gutachten mit `submit_result(kind="review")` und der Zeile `Urteil: …` ab. Hast du nichts mehr beizutragen, melde `done`.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "round": 1, "version": 1, "artifact": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v1/artefakt.md", "excerpt": "Beweis v1\nZeile 2\n", "complete": true}, "verdicts": ["accept", "minor", "major", "reject"], "objections": [{"id": "E1", "text": "L\u00fccke in Lemma 2", "status": "offen", "by": "Mitglied A", "round": 1}, {"id": "E2", "text": "Tippfehler in Zeile 2", "status": "offen", "by": "Mitglied B", "round": 1}], "previous": [{"by": "Mitglied A", "round": 0, "next_round": "Lemma 2 pr\u00fcfen"}, {"by": "Mitglied B", "round": 0, "next_round": "nichts"}], "reviews": [{"by": "Mitglied A", "verdict": "major", "path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/review/runde-1/gutachten-mitglied-a.md", "excerpt": "Urteil: major\n\n## Einw\u00e4nde\n- L\u00fccke in Lemma 2\n\n## F\u00fcr die n\u00e4chste Runde\nLemma 2 pr\u00fcfen"}, {"by": "Mitglied B", "verdict": "minor", "path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/review/runde-1/gutachten-mitglied-b.md", "excerpt": "Urteil: minor\n\n## Einw\u00e4nde\n- Tippfehler in Zeile 2\n\n## F\u00fcr die n\u00e4chste Runde\nnichts"}]}',1795441628.64722);
INSERT INTO "assignment" VALUES('author',2,'revision','Revision nach Prüfrunde 1: Das Urteil ist „major“. Gutachten, Einwände mit Status und Fassung 1 stehen in `get_assignment()`. Überarbeite das Artefakt und gib Fassung 2 mit `submit_result(kind="artifact", path="<Datei>")` ab. Hältst du einen Einwand für falsch, begründe das im Ergebnis.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "round": 1, "version": 1, "artifact": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v1/artefakt.md", "excerpt": "Beweis v1\nZeile 2\n", "complete": true}, "verdicts": ["accept", "minor", "major", "reject"], "objections": [{"id": "E1", "text": "L\u00fccke in Lemma 2", "status": "offen", "by": "Mitglied A", "round": 1}, {"id": "E2", "text": "Tippfehler in Zeile 2", "status": "offen", "by": "Mitglied B", "round": 1}], "previous": [{"by": "Mitglied A", "round": 0, "next_round": "Lemma 2 pr\u00fcfen"}, {"by": "Mitglied B", "round": 0, "next_round": "nichts"}], "verdict": "major", "reviews": [{"by": "Mitglied A", "verdict": "major", "path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/review/runde-1/gutachten-mitglied-a.md", "excerpt": "Urteil: major\n\n## Einw\u00e4nde\n- L\u00fccke in Lemma 2\n\n## F\u00fcr die n\u00e4chste Runde\nLemma 2 pr\u00fcfen"}, {"by": "Mitglied B", "verdict": "minor", "path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/review/runde-1/gutachten-mitglied-b.md", "excerpt": "Urteil: minor\n\n## Einw\u00e4nde\n- Tippfehler in Zeile 2\n\n## F\u00fcr die n\u00e4chste Runde\nnichts"}]}',1.79544162866914502885e+09);
INSERT INTO "assignment" VALUES('judge-1',3,'pruefung','Prüfrunde 2, Fassung 2: Prüfe das Artefakt unabhängig von den anderen Judges. Aufgabe, Fassung, Diff zur Vorfassung und Einwände stehen in `get_assignment()`. Gib dein Gutachten mit `submit_result(kind="review")` ab.

## So prüfst du

- **Unabhängig:** In der Prüfung siehst du die Gutachten der anderen Judges nicht, und #judges ist zu. Erst in der Diskussion öffnet der Kanal; dort stehen die Gutachten im Auftrag.
- **Auftrag:** `get_assignment()` nennt Aufgabe, Fassung (`artifact.path`, absolut), bei einer Überarbeitung den Diff (`diff.path`), die Einwände mit Status und die Notizen der Vorrunde.
- **Gutachten:** Gib es mit `submit_result(kind="review")` ab, als Text in `body` oder als Datei in `path`. Halte diese Form ein:
  - eine Zeile `Urteil: accept`, `Urteil: minor`, `Urteil: major` oder `Urteil: reject`;
  - einen Abschnitt `## Einwände` mit einem Punkt je Einwand (`- …`). Zu einem Einwand aus dem Auftrag schreibst du `- [E3] behoben: …`, `- [E3] offen: …` oder `- [E3] strittig: …`;
  - einen Abschnitt `## Für die nächste Runde`: kurz, was du dir merken willst.
- **Urteil ändern:** Überzeugt dich die Diskussion, gib dort ein neues Gutachten in derselben Form ab (`submit_result(kind="review")`). Es zählt dein letztes Gutachten der Runde.
','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "round": 2, "version": 2, "artifact": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v2/artefakt.md", "excerpt": "Beweis v2\nZeile 2\n", "complete": true}, "verdicts": ["accept", "minor", "major", "reject"], "objections": [{"id": "E1", "text": "L\u00fccke in Lemma 2", "status": "offen", "by": "Mitglied A", "round": 1}, {"id": "E2", "text": "Tippfehler in Zeile 2", "status": "offen", "by": "Mitglied B", "round": 1}], "previous": [{"by": "Mitglied A", "round": 1, "next_round": "Lemma 2 pr\u00fcfen"}, {"by": "Mitglied B", "round": 1, "next_round": "nichts"}], "diff": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v2/diff.patch"}}',1.79544162870157304467e+09);
INSERT INTO "assignment" VALUES('judge-2',3,'pruefung','Prüfrunde 2, Fassung 2: Prüfe das Artefakt unabhängig von den anderen Judges. Aufgabe, Fassung, Diff zur Vorfassung und Einwände stehen in `get_assignment()`. Gib dein Gutachten mit `submit_result(kind="review")` ab.

## So prüfst du

- **Unabhängig:** In der Prüfung siehst du die Gutachten der anderen Judges nicht, und #judges ist zu. Erst in der Diskussion öffnet der Kanal; dort stehen die Gutachten im Auftrag.
- **Auftrag:** `get_assignment()` nennt Aufgabe, Fassung (`artifact.path`, absolut), bei einer Überarbeitung den Diff (`diff.path`), die Einwände mit Status und die Notizen der Vorrunde.
- **Gutachten:** Gib es mit `submit_result(kind="review")` ab, als Text in `body` oder als Datei in `path`. Halte diese Form ein:
  - eine Zeile `Urteil: accept`, `Urteil: minor`, `Urteil: major` oder `Urteil: reject`;
  - einen Abschnitt `## Einwände` mit einem Punkt je Einwand (`- …`). Zu einem Einwand aus dem Auftrag schreibst du `- [E3] behoben: …`, `- [E3] offen: …` oder `- [E3] strittig: …`;
  - einen Abschnitt `## Für die nächste Runde`: kurz, was du dir merken willst.
- **Urteil ändern:** Überzeugt dich die Diskussion, gib dort ein neues Gutachten in derselben Form ab (`submit_result(kind="review")`). Es zählt dein letztes Gutachten der Runde.
','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "round": 2, "version": 2, "artifact": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v2/artefakt.md", "excerpt": "Beweis v2\nZeile 2\n", "complete": true}, "verdicts": ["accept", "minor", "major", "reject"], "objections": [{"id": "E1", "text": "L\u00fccke in Lemma 2", "status": "offen", "by": "Mitglied A", "round": 1}, {"id": "E2", "text": "Tippfehler in Zeile 2", "status": "offen", "by": "Mitglied B", "round": 1}], "previous": [{"by": "Mitglied A", "round": 1, "next_round": "Lemma 2 pr\u00fcfen"}, {"by": "Mitglied B", "round": 1, "next_round": "nichts"}], "diff": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v2/diff.patch"}}',1.79544162870790691144e+09);
INSERT INTO "assignment" VALUES('judge-1',4,'diskussion','Runde 1 von 1 in #judges.

Diskussion zu Prüfrunde 2 in #judges: Die Gutachten aller Judges stehen jetzt in `get_assignment()` unter `reviews`, die Einwände unter `objections`. Besprecht strittige Einwände und das Urteil. Ändert sich dein Urteil, gib ein neues Gutachten mit `submit_result(kind="review")` und der Zeile `Urteil: …` ab. Hast du nichts mehr beizutragen, melde `done`.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "round": 2, "version": 2, "artifact": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v2/artefakt.md", "excerpt": "Beweis v2\nZeile 2\n", "complete": true}, "verdicts": ["accept", "minor", "major", "reject"], "objections": [{"id": "E1", "text": "L\u00fccke in Lemma 2", "status": "behoben", "by": "Mitglied A", "round": 1}, {"id": "E2", "text": "Tippfehler in Zeile 2", "status": "behoben", "by": "Mitglied B", "round": 1}], "previous": [{"by": "Mitglied A", "round": 1, "next_round": "nichts"}, {"by": "Mitglied B", "round": 1, "next_round": "nichts"}], "diff": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v2/diff.patch"}, "reviews": [{"by": "Mitglied A", "verdict": "accept", "path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/review/runde-2/gutachten-mitglied-a.md", "excerpt": "Urteil: accept\n\n## Einw\u00e4nde\n- [E1] behoben: passt jetzt\n\n## F\u00fcr die n\u00e4chste Runde\nnichts"}, {"by": "Mitglied B", "verdict": "accept", "path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/review/runde-2/gutachten-mitglied-b.md", "excerpt": "Urteil: accept\n\n## Einw\u00e4nde\n- [E2] behoben\n- [E1] behoben\n\n## F\u00fcr die n\u00e4chste Runde\nnichts"}]}',1.79544162873282608927e+09);
INSERT INTO "assignment" VALUES('judge-2',4,'diskussion','Runde 1 von 1 in #judges.

Diskussion zu Prüfrunde 2 in #judges: Die Gutachten aller Judges stehen jetzt in `get_assignment()` unter `reviews`, die Einwände unter `objections`. Besprecht strittige Einwände und das Urteil. Ändert sich dein Urteil, gib ein neues Gutachten mit `submit_result(kind="review")` und der Zeile `Urteil: …` ab. Hast du nichts mehr beizutragen, melde `done`.','{"task": "Zeige, dass Lemma A f\u00fcr alle n \u2261 1 mod 4 gilt.", "round": 2, "version": 2, "artifact": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v2/artefakt.md", "excerpt": "Beweis v2\nZeile 2\n", "complete": true}, "verdicts": ["accept", "minor", "major", "reject"], "objections": [{"id": "E1", "text": "L\u00fccke in Lemma 2", "status": "behoben", "by": "Mitglied A", "round": 1}, {"id": "E2", "text": "Tippfehler in Zeile 2", "status": "behoben", "by": "Mitglied B", "round": 1}], "previous": [{"by": "Mitglied A", "round": 1, "next_round": "nichts"}, {"by": "Mitglied B", "round": 1, "next_round": "nichts"}], "diff": {"path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/artifacts/v2/diff.patch"}, "reviews": [{"by": "Mitglied A", "verdict": "accept", "path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/review/runde-2/gutachten-mitglied-a.md", "excerpt": "Urteil: accept\n\n## Einw\u00e4nde\n- [E1] behoben: passt jetzt\n\n## F\u00fcr die n\u00e4chste Runde\nnichts"}, {"by": "Mitglied B", "verdict": "accept", "path": "@@WORKFOLDER@@/runs/20261001-1000_demo_review_a001/output/review/runde-2/gutachten-mitglied-b.md", "excerpt": "Urteil: accept\n\n## Einw\u00e4nde\n- [E2] behoben\n- [E1] behoben\n\n## F\u00fcr die n\u00e4chste Runde\nnichts"}]}',1.79544162873710488881e+09);
CREATE TABLE channel(name TEXT PRIMARY KEY, created REAL, config TEXT,
    state TEXT NOT NULL DEFAULT 'open');
INSERT INTO "channel" VALUES('judges',1.79544162857737488181e+09,'{"name":"judges","purpose":null,"members":["judge-1","judge-2"],"mode":"rounds","order":"parallel","moderator":null,"open_from":"diskussion","anonymous":true,"wake_on":["mention","question"],"debounce_s":20.0,"max_wakeups_per_agent":12,"quiet_after_s":300.0,"idle_after_s":30.0,"max_message_chars":3000,"summary_every":15}','open');
CREATE TABLE cursor(agent TEXT, channel TEXT, last_id INTEGER,
    PRIMARY KEY (agent, channel));
CREATE TABLE delegation(id INTEGER PRIMARY KEY, ts REAL, stage TEXT,
    attempt INTEGER, number INTEGER, lead TEXT, agent TEXT, format TEXT, task TEXT,
    status TEXT NOT NULL DEFAULT 'queued', named INTEGER NOT NULL DEFAULT 0,
    submission_id INTEGER, started REAL, ended REAL);
CREATE TABLE delegation_closed(stage TEXT, attempt INTEGER, ts REAL,
    PRIMARY KEY (stage, attempt));
CREATE TABLE floor(id INTEGER PRIMARY KEY, ts REAL, channel TEXT,
    moderator TEXT, agent TEXT, prompt TEXT, msg_id INTEGER, handled INTEGER DEFAULT 0);
CREATE TABLE member(channel TEXT, agent TEXT, alias TEXT,
    PRIMARY KEY (channel, agent));
INSERT INTO "member" VALUES('judges','judge-1','Mitglied A');
INSERT INTO "member" VALUES('judges','judge-2','Mitglied B');
CREATE TABLE msg(id INTEGER PRIMARY KEY, ts REAL, sender TEXT, channel TEXT,
    kind TEXT, body TEXT, to_agents TEXT, reply_to INTEGER, refs TEXT, pinned INTEGER,
    open INTEGER, via TEXT, delivery TEXT);
INSERT INTO "msg" VALUES(1,1.79544162857741511629e+09,'orchestrator','judges','note','Steckbrief #judges
Mitglieder:
- Mitglied A
- Mitglied B
Der Kanal ist anonym: Mitglieder erscheinen nur als „Mitglied A/B/…“.
Taktung: Runden: Alle Mitglieder kommen pro Runde dran, sonst `pass`.
Länge einer Nachricht: höchstens 3000 Zeichen.
Gesprächsregeln:
1. Eine Nachricht, ein Gedanke. Setze `kind` korrekt.
2. Behauptungen mit Begründung oder Beleg: Datei, Rechnung, Anhang.
3. Beantworte Fragen an dich zuerst.
4. Wer jemanden fragt, schreibt `@name` (oder `to`); `@alle` fragt alle, `@user` den User (nur er antwortet darauf). Auf eine Antwort wartest du mit `wait_for_messages`, auf den User nie mit `wait_until`: Antwortet er nicht, arbeite mit einer begründeten Annahme weiter und nenne sie in deinem Ergebnis.
5. Widerspruch als `objection` mit `reply_to`, sachlich und konkret.
6. Nicht wiederholen. Zustimmung nur, wenn sie etwas hinzufügt.
7. Lange Inhalte (Beweise, Code, Tabellen) als Anhang, im Text nur die Kernaussage.
8. Wer seinen Teil erledigt hat, gibt sein Ergebnis ab (`submit_result` oder Nachricht `result`) oder meldet `done`. Fertig heißt: Du wartest nicht mehr, bleibst aber ansprechbar; jede Nachricht an dich weckt dich. Wartest du auf das Ergebnis eines anderen, warte mit `wait_until(minutes, until_result_from="<name>")`.
Ende: nach der letzten Runde oder wenn alle `pass`/`done` melden.','[]',NULL,'[]',1,0,NULL,NULL);
INSERT INTO "msg" VALUES(2,1.79544162865038492427e+09,'judge-1','judges','note','Gelesen.','[]',NULL,'[]',0,0,NULL,NULL);
INSERT INTO "msg" VALUES(3,1.79544162865254293137e+09,'judge-2','judges','note','Gelesen.','[]',NULL,'[]',0,0,NULL,NULL);
INSERT INTO "msg" VALUES(4,1.79544162874010186925e+09,'judge-1','judges','note','Gelesen.','[]',NULL,'[]',0,0,NULL,NULL);
INSERT INTO "msg" VALUES(5,1.7954416287418160536e+09,'judge-2','judges','note','Gelesen.','[]',NULL,'[]',0,0,NULL,NULL);
CREATE TABLE note_turn(agent TEXT PRIMARY KEY, since REAL);
CREATE TABLE poll(proposal_id INTEGER PRIMARY KEY, channel TEXT, rule TEXT,
    called_by TEXT, ts REAL);
CREATE TABLE rating(id INTEGER PRIMARY KEY, ts REAL, rater TEXT,
    result_id INTEGER, score REAL, criteria TEXT, comment TEXT);
CREATE TABLE result(id INTEGER PRIMARY KEY, ts REAL, agent TEXT, stage TEXT,
    kind TEXT, title TEXT, body TEXT, path TEXT, attempt INTEGER);
INSERT INTO "result" VALUES(1,1.79544162860601712594e+09,'author','artefakt','artifact','Beweis','Beweis v1
Zeile 2
',NULL,1);
INSERT INTO "result" VALUES(2,1.79544162862193683594e+09,'judge-1','pruefung','review','Gutachten','Urteil: major

## Einwände
- Lücke in Lemma 2

## Für die nächste Runde
Lemma 2 prüfen',NULL,1);
INSERT INTO "result" VALUES(3,1.7954416286229870181e+09,'judge-2','pruefung','review','Gutachten','Urteil: minor

## Einwände
- Tippfehler in Zeile 2

## Für die nächste Runde
nichts',NULL,1);
INSERT INTO "result" VALUES(4,1.79544162867272820705e+09,'author','revision','artifact','Beweis neu','Beweis v2
Zeile 2
',NULL,1);
INSERT INTO "result" VALUES(5,1.79544162871176382623e+09,'judge-1','pruefung','review','Gutachten','Urteil: accept

## Einwände
- [E1] behoben: passt jetzt

## Für die nächste Runde
nichts',NULL,1);
INSERT INTO "result" VALUES(6,1.79544162871276373749e+09,'judge-2','pruefung','review','Gutachten','Urteil: accept

## Einwände
- [E2] behoben
- [E1] behoben

## Für die nächste Runde
nichts',NULL,1);
CREATE TABLE sealed_result(result_id INTEGER PRIMARY KEY, stage TEXT);
CREATE TABLE sealed_stage(stage TEXT PRIMARY KEY, channel TEXT, ts REAL);
CREATE TABLE sealed_submission(submission_id INTEGER PRIMARY KEY, stage TEXT);
CREATE TABLE submission(id INTEGER PRIMARY KEY, ts REAL, agent TEXT,
    shown_as TEXT, stage TEXT, attempt INTEGER, action TEXT, format TEXT, name TEXT,
    version INTEGER, status TEXT, summary TEXT, data TEXT, path TEXT, final_path TEXT);
CREATE TABLE vote(proposal_id INTEGER, agent TEXT, choice TEXT, reason TEXT,
    ts REAL, PRIMARY KEY (proposal_id, agent));
CREATE TABLE wait_guard(agent TEXT PRIMARY KEY, waited REAL NOT NULL DEFAULT 0,
    seen INTEGER, last_end TEXT, yield_to TEXT, end_turn TEXT,
    until_streak INTEGER NOT NULL DEFAULT 0);
INSERT INTO "wait_guard" VALUES('author',0.0,NULL,NULL,NULL,NULL,0);
INSERT INTO "wait_guard" VALUES('judge-1',0.0,NULL,NULL,NULL,NULL,0);
INSERT INTO "wait_guard" VALUES('judge-2',0.0,NULL,NULL,NULL,NULL,0);
CREATE INDEX msg_channel ON msg(channel, id);
CREATE INDEX msg_reply ON msg(reply_to);
CREATE UNIQUE INDEX rating_once ON rating(rater, result_id);
CREATE UNIQUE INDEX submission_version ON submission(name, version);
CREATE UNIQUE INDEX delegation_number ON delegation(stage, attempt, number);
COMMIT;
