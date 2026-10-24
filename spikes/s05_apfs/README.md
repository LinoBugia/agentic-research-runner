# S5 – APFS-Klone und Speicher

**Frage:** Wie schnell ist das Kopieren eines Projekts mit ca. 500 MB, und wie viel Platz kostet es tatsächlich? Wie misst man „logisch“ und „tatsächlich“? Was passiert, wenn das Ziel nicht auf APFS liegt? (Roadmap S5, Auftrag W0-04)

Ergebnis: [docs/spikes/S5_apfs.md](../../docs/spikes/S5_apfs.md)

## Dateien

| Datei | Zweck |
|---|---|
| `main.py` | Gesamte Messung: Testdaten anlegen, `cp -c -R` / `clonefile` / `shutil.copytree` / `cp -R` vergleichen, Änderungen im Klon, Messmethode gegen freigegebenen Platz prüfen, Ziele auf HFS+/ExFAT/zweitem APFS-Volume (Disk-Images), Aufräumen |
| `sizes.py` | Vorgeschlagene Messmethode: logische, belegte (`du`), private und tatsächliche Größe eines Ordners über `getattrlist(2)` |
| `results/` | `s05_results.json` (alle Werte inkl. `df` vorher/nachher), `s05_run.log` (Konsolenausgabe) |

## Ausführung

Voraussetzung: macOS mit APFS, ca. 1,6 GB frei. Kein Login, kein sudo.

```bash
uv run python spikes/s05_apfs/main.py                # ca. 500 MB in 4480 Dateien, 3 Wiederholungen, ca. 1 min
uv run python spikes/s05_apfs/sizes.py <ordner>      # Größen eines beliebigen Ordners
```

Die Testdaten liegen unter `spikes/s05_apfs/tmp/` und werden am Ende gelöscht (`--keep` behält sie). Die Disk-Images werden nach dem Test wieder ausgehängt und gelöscht.
