# W5-00 Spike: eigener macOS-Benutzer für Agenten

**Frage:** Schließt ein eigener macOS-Benutzer `arr-agent` den offenen Weg 2 aus `SECURITY.md` (Umgebung fremder Prozesse über `KERN_PROCARGS2`)? Stufe A misst dazu auch Dateien im Home, einen Test-Schlüsselbund und die Gegenrichtung. Stufe B (was die Lösung kostet) folgt nur, wenn Stufe A den Weg schließt.

## Programme

- `bait.py` – Köder-Prozess: zufälliger Marker in einer sonst leeren Umgebung, läuft im Hintergrund, schreibt eine Zustandsdatei mit PID, Startzeit und SHA-256 des Markers (nie den Marker).
- `probe.py` – Sonde: `env` (Umgebung über `KERN_PROCARGS2`, nur für die PID aus einer Köder-Zustandsdatei mit passender Startzeit), `file` (Köder-Datei), `keychain` (Köder-Eintrag in einem Test-Schlüsselbund als Datei, nie der Anmelde-Schlüsselbund), `access` (nur `access(2)`, liest keine Einträge). Ausgabe: eine JSON-Zeile und ein Exit-Code, 0 Marker gefunden, 1 gelesen ohne Marker, 2 verweigert, 3 Wächter/Fehler, 4 Zeitüberschreitung.
- `stage_a.py` – Stufe A: `prepare`, `counter`, `agent`, `reverse`, `cleanup`.
- `stage_b.py` – Stufe B, Runde 1: Test-Run-Ordner zwischen arr und `arr-agent` (ACL, Gruppe, Klone, Ablegen, Entziehen), Testordner für ein geschlossenes Home, `access`-Proben, Deskriptoren über `sudo`, Sitzung, Seatbelt, `kill(-1)`. `agent-dry` spielt die Agenten-Seite als normaler Benutzer durch, ohne Kill.
- `stage_b2.py` – Stufe B, Runde 2: launchd-Dienst als `arr-agent` (Plist aus `prepare`), Sitzung, Schlüsselbund-Abfrage ohne Anmeldesitzung, Secret als Deskriptor über einen Unix-Socket mit Peer-Prüfung.

Alle drei laufen mit dem System-Python der Command Line Tools (3.9, nur Standardbibliothek), weil `arr-agent` weder das Repo noch die venvs des Users lesen kann. `prepare` kopiert sie dafür nach `/Users/Shared/arr-w5-00/bin/`.

## Ausführung

```bash
PY=/Library/Developer/CommandLineTools/usr/bin/python3
$PY -I -B spikes/w5_00_user/stage_a.py prepare     # normaler Benutzer: Köder, Gegenproben
cd /Users/Shared/arr-w5-00 && sudo -H -u arr-agent $PY -I -B bin/stage_a.py agent   # der User
$PY -I -B spikes/w5_00_user/stage_a.py reverse     # normaler Benutzer: Gegenrichtung
$PY -I -B spikes/w5_00_user/stage_b.py prepare     # Stufe B, Runde 1
$PY -I -B /Users/Shared/arr-w5-00/bin/stage_b.py arr-side &     # Seite von arr wartet
cd /Users/Shared/arr-w5-00 && sudo -H -u arr-agent $PY -I -B bin/stage_b.py agent 3<b/fd-bait <b/stdin-bait   # der User
$PY -I -B spikes/w5_00_user/stage_b2.py prepare    # Runde 2: Plist, K2 entsperren
# der User: Plist nach /Library/LaunchDaemons, launchctl bootstrap system …
$PY -I -B /Users/Shared/arr-w5-00/bin/stage_b2.py client        # Secret über den Socket
$PY -I -B spikes/w5_00_user/stage_a.py cleanup     # Köder, Test-Schlüsselbünde, Ordner entfernen
```

Den Benutzer `arr-agent` legt der User von Hand an und entfernt ihn wieder; die Befehle stehen im Ergebnis des Spikes. Kein Modell, kein Login, kein `sudo` durch den Agenten.
