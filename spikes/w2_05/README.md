# W2-05: Live-E2E für M1 (Wegwerf-Code)

**Frage:** Erfüllt `arr run` die M1-Kriterien 1–7 aus [05 Roadmap](../../docs/05-roadmap.md#phase-1--harness-kern-cli) live mit kleinen Modellen (2 × `claude-haiku-5-5`, 1 × `gpt-6-luna`)?

**Dateien**

- `m1/` – das Szenario: `run.yaml`, `task.md`, `project/` (Projekt `collatz-mini` mit den Rollen `roles/*.md`). Ohne persönliche Pfade; die Auth-Profile kommen aus der `arr.yaml` des Test-Workfolders. Dazu `wecken.yaml`: eine kleine `roundtable`-Diskussion im Modus `event` (zwei Haiku-Agenten), weil `waterfall` keine Diskussionsphase hat und darum niemanden weckt (Kriterium 6, Teil „Wecken“).
- `setup_workfolder.py <ziel>` – legt den Test-Workfolder außerhalb des Repos an (`arr init`, `arr.yaml` mit `toolchain.python`, Projekt, Szenario).
- `check_m1.py <run-ordner>` – prüft maschinell, was sich am Run-Ordner prüfen lässt (Kriterien 1, 2, 5, 6, 7), Tabelle ✅/❌ plus Kennzahlen (Anfragen, Kosten, Dauer).
- `ask.py <run-ordner> <kanal> <agent> "<frage>"` – stellt während eines Laufs eine Frage als `user` mit `to`, für den Hinweis mitten im Turn bzw. den Posteingang.

**Ausführung** (Voraussetzungen: Claude-Login des Macs, ein angemeldetes Codex-Profil, Basis-venv `~/.venvs/mathe` mit `sympy`, `~/.arr/profiles.yaml` mit den Profilen `max` und `chatgpt-w2-02a`):

```bash
python3.12 -m venv ~/.venvs/mathe && ~/.venvs/mathe/bin/pip install sympy   # einmalig
uv run python spikes/w2_05/setup_workfolder.py ~/arr-m1
cd ~/arr-m1 && uv run --project <repo> arr run m1/run.yaml                  # ein M1-Lauf
uv run python spikes/w2_05/check_m1.py ~/arr-m1/runs/<run-id>
```

Ergebnisse und Auszüge stehen im [Abschlussbericht](../../umsetzung/welle-2/W2-05_CLI_LiveE2E_Abschlussbericht.md). Temporäre Ausgaben nach `spikes/w2_05/tmp/` (von Git ignoriert).
