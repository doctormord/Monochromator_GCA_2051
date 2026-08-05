# CLAUDE.md — VRS41 Monochromator/Scan-Controller

Python-Anwendung zur Steuerung eines VRS41-Monochromators mit Faulhaber-Antrieb
(Serial) und NI-DAQ-Messwerterfassung. GUI ist Qt6 (PyQt6 + pyqtgraph).
Einstiegspunkt `main.py`.

Repo: `doctormord/Monochromator_GCA_2051`, Branch `QT6-future`, public.
Fork von `sk-HTW/monochromator_python_code`.

## Vor jeder Änderung lesen

| Datei | Inhalt | Wann lesen |
|---|---|---|
| `docs/content/CONTEXT.md` | Technischer Tiefgang: Skalen, Positions-Wahrheiten, laufende Offset-Diagnose | Immer bei Sessionstart |
| `docs/content/BACKLOG.md` | Offene Punkte nach Priorität (P0 = aktiver Arbeitsstrang) | Immer bei Sessionstart |
| `docs/content/HANDOVER.md` | Session-Übergabe: aktueller Stand, Architektur/Modulübersicht, rig-bestätigte Fakten | Nur bei Bedarf (explizite Nachfrage oder wenn CONTEXT.md nicht ausreicht) |
| `docs/content/HISTORY.md` | Chronologisches Log, append-only | Nur bei gezielter Nachfrage nach früheren Entscheidungen/Verlauf — nicht automatisch |
| `MANUAL.md` | Vollständiges Handbuch, Single Source (README + PDF werden daraus erzeugt) | Bei Aufgaben, die die Doku selbst betreffen |
| `TESTPLAN.md` | Testplan | Bei Test-bezogenen Aufgaben |

## Dokumentationsstruktur

Vier Doku-Dateien unter `docs/content/`, mit unterschiedlichem Zweck und
Lebenszyklus:

- `docs/content/CONTEXT.md` — aktueller technischer Stand (wird überschrieben)
- `docs/content/BACKLOG.md` — offene Aufgaben nach Priorität (wird gepflegt)
- `docs/content/HANDOVER.md` — Session-Übergabe/Architektur (wird überschrieben)
- `docs/content/HISTORY.md` — chronologisches Log (append-only)

**Session-Ende:**
- `docs/content/CONTEXT.md` und `docs/content/HANDOVER.md` aus dem aktuellen
  Stand neu schreiben (überschreiben, nicht anhängen).
- `docs/content/HISTORY.md` einen neuen Eintrag anhängen (`## YYYY-MM-DD`):
  was wurde gemacht, welche Entscheidungen wurden getroffen und warum, was
  ist offen. **NIE** bestehende HISTORY.md-Einträge verändern oder löschen —
  nur anhängen.
- `docs/content/HISTORY.md` darf bei Bedarf (neue Session, unklarer Kontext)
  gelesen werden, um frühere Entscheidungen nachzuvollziehen — aber nicht
  automatisch bei jedem Prompt, nur wenn CONTEXT.md/HANDOVER.md nicht
  ausreichen.

## ⚠️ VERBINDLICHE DOKU-/KOMMENTAR-POLICY (gilt für jede Session, jede Datei)

Diese Regel steht bewusst ganz oben — jede künftige Session (auch eine andere
Claude-Instanz) sieht sie zuerst, bevor irgendein Code angefasst wird.

1. **Docstrings/Kommentare sind Teil der Lieferung, kein Beiwerk.** Jede
   Funktion bekommt einen Docstring: WAS sie tut, WOMIT sie aufgerufen wird,
   WANN/unter welcher Bedingung sie läuft, WARUM (falls nicht selbsterklärend).
2. **Beim Editieren bestehender Dateien: Kommentare/Docstrings werden NICHT
   gekürzt, entfernt oder "wegoptimiert"**, auch nicht implizit durch
   Refactoring. Wird eine Aussage durch eine Änderung falsch, wird sie
   korrigiert, nicht gelöscht.
3. **Vor jedem Edit an einer bestehenden Datei: die Datei vollständig lesen.**
4. **Gefundene Altlasten (toter Code, Duplikate, inkonsistente Werte) werden
   nicht stillschweigend gelöscht.** Markieren statt löschen. Löschen nur
   nach expliziter Bestätigung durch den Nutzer.
5. **Diese Sektion selbst wird nicht gekürzt.**
6. **Verifikation heißt echter Skript-Start, nicht nur `py_compile`.**
   `py_compile` prüft nur Syntax. Vor jeder Auslieferung: Datei(en) unter
   Xvfb tatsächlich starten (`xvfb-run -a python3 <script>`) und auf
   fehlerfreien Start bis in die Event-Loop prüfen. Bei GUI-Änderungen
   zusätzlich ein SIM-Smoke-Test (Connect → Scan → Buttons re-enabled).
7. **Code-Kommentare/Docstrings und alle GUI-Strings sind Englisch.**
   Prosa in `docs/content/HANDOVER.md`, `docs/content/BACKLOG.md`,
   `docs/content/CONTEXT.md` und `docs/content/HISTORY.md` bleibt Deutsch
   (Nutzer-Präferenz).
8. **Hardware-Werte (Timing, Richtung, Rampen) werden nie geraten.** Erst
   auslesen/verifizieren (z.B. `tune_ramp.py`, ein Jog-Test), dann ändern.
   Bei Unsicherheit: Debug-Mitschnitt oder Rig-Test anfordern statt zu raten.

## Arbeiten ohne Hardware

Port `"SIM"` injiziert über `sim_hardware.py` einen virtuellen Faulhaber-Antrieb
inkl. modelliertem Backlash. DAQ hat einen wellenlängen-geformten Simulator.
Damit ist der komplette Scan-Pfad ohne Rig testbar.

Was der SIM-Modus **nicht** beweist: reales Timing, echte Backlash-Beträge,
Endschalter-Verhalten, optische Bewegung des Gitters. Alles, was in
`docs/content/BACKLOG.md` unter P1 steht, braucht das Rig.

## Nicht anfassen ohne Rücksprache

- **Kein `SAVE`/`EEPSAV`** an den Controller — EEPROM-Schreibzyklen sind begrenzt.
  Diagnose-Tools schreiben grundsätzlich RAM-only.
- **Kein `HO`** im Protokoll-Layer. `POS` ist bewusst ein frei laufender
  Absolutzähler über die ganze Session (Grundlage der Offset-Diagnose).
- `monochromator_python_code_vrs41.py` (ex-Monolith) ist historische Referenz,
  nicht Teil des ausgelieferten Pakets.
- P2-Punkte in `docs/content/BACKLOG.md` sind **bewusst** erhaltene Alt-Bugs,
  keine Aufräumkandidaten.

## Aktueller Arbeitsstrang

P0: Wellenlängen-Offset ~0.07 nm zwischen Scan-Gruppen. Diagnose läuft,
**kein Fix committet**. `stage1_diag.patch` ist angewendet (in `scan_engine.py`
verifiziert); ausstehend ist die Session mit reproduziertem Offset und deren
Auswertung.

**Reihenfolge beachten:** Defect A nicht vor der Datenerhebung fixen, sonst ist
Hypothese H2 nicht mehr testbar. Details in `docs/content/CONTEXT.md`.

## Git

Lokal arbeiten. Kein `git push`, kein Remote-Zugriff ohne
ausdrückliche Aufforderung.
