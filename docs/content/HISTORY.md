# HISTORY.md — VRS41: chronologisches Log

> **Append-only.** Bestehende Einträge werden nie verändert oder gelöscht, nur
> neue angehängt (Regeln siehe CLAUDE.md, Abschnitt „Dokumentationsstruktur").
> Am Ende jeder Arbeitssession einen neuen `## YYYY-MM-DD`-Eintrag anhängen.
>
> **Rekonstruktion 2026-08-04:** Alle Einträge bis einschließlich 2026-07-24
> sind nachträglich aus exportierten Claude.ai-Chat-Transkripten (sechs Chats,
> `claude-exports-Monochromator.zip`) rekonstruiert, per Nachrichten-Zeitstempel
> in einzelne Arbeitssessions unterteilt. Zeiten sind lokal (CEST, UTC+2) wie
> in den Chat-Exports selbst angegeben. Der Eintrag zur Offset-Diagnose-Session
> 2026-07-29/31 hat keine Chat-Export-Quelle und ist stattdessen aus dem
> damaligen Stand von CONTEXT.md/BACKLOG.md rekonstruiert. Ab 2026-08-04
> stammen die Einträge direkt aus der laufenden Claude-Code-Session.

## 2026-07-20

AVG-Regression im Monolith gefunden und gefixt (Chat "Prüfe mal alle drei
Dateien al...", erste Session, 15:26–15:40).

Nutzer lädt drei Versionen eines ~2000–3400 Zeilen langen Tkinter-Monolithen
hoch (`alt_vrs41_fixed.py`, `vrs41_fixed_AVG_PATCH.py`,
`vrs41_final_gui_..._DIAG_v2.py`), meldet einen persistenten ~1.6 nm Schlupf
mit dem aktuellen Getriebe und bittet um Review + Roadmap vor jeder Änderung.
Ohne vorhandene Doku (keine HANDOVER/BACKLOG-Dateien) wurden die drei
Dateiversionen gegeneinander diffet, um Aktualität zu bestimmen — `DIAG_v2`
als neueste/umfangreichste identifiziert (Superset aus AVG-Helpern,
Preload/Edge-Guard, Diag/Resume-Pfad).

Gefundener Bug: `scan_worker()` (frische Scans) rief den rohen,
unaveraged `read_pmt_voltage()` auf und ignorierte die AVG-Einstellungen
komplett; nur `do_resume()` averagete korrekt. Averaging funktionierte damit
nur nach Pause+Resume, nie auf einem frischen Scan — eine Regression, die
beim Zusammenführen von `AVG_PATCH` in `DIAG_v2` entstanden war.

Vier-Phasen-Roadmap vorgeschlagen und akzeptiert (Phase 0: Doku als Source of
Truth; Phase 1: AVG-Fix; Phase 2: Schlupf-Konstanten-Widerspruch klären;
Phase 3: PS-2600A-artiger Umbau). Nutzer bestätigt `DIAG_v2` als Baseline,
liefert PS-2600A-Referenzrepo (`github.com/doctormord/Pasco-PS-2600A-Spectrometer`)
als Architektur-Vorbild.

Fix: neue gemeinsame Funktion `acquire_measurement(dwell)` (Presettle →
Averaging-Fenster → Rest-Dwell), von `scan_worker` UND `do_resume` genutzt;
redundantes `time.sleep(dwell)` entfernt (hätte Dwell doppelt gezählt).
Magic-Numbers-Konfigblock ergänzt. Bewusst NICHT angefasst (mit
WARN-Kommentaren markiert): Schlupf-Konstanten-Widerspruch, neun duplizierte
Funktionsdefinitionen, `except: pass`-Stellen, toter Statusword-Code.

Offen am Ende: AVG-Verifikationslog vom Nutzer, Bestätigung des echten
DAQ-Kanals (ai0 vs. ai1).

## 2026-07-21/22

Neue Baseline, Backlash-Redesign, kompletter Modul-Split
(Chat "Prüfe mal alle drei Dateien al...", zweite Session, 21.7. 22:09 –
22.7. 00:06, zieht sich über Mitternacht).

Nutzer lädt `monochromator_python_code_vrs41.py` als neue Baseline hoch
(faktisch der alte `alt_vrs41_fixed.py`-Stand + DAQ-Kanal `ai1` — beantwortet
rückwirkend die offene ai0/ai1-Frage aus der Vorsession) und meldet: Schlupf
mechanisch bereits von ~1.6 nm auf ~0.3 nm reduziert, aber weiterhin
vorhanden. Wunsch: Referenzfahrt-Workflow zur Bestimmung der
Kompensationsrichtung, saubere Trennung Code/GUI im PS-2600A-Stil, später
Qt6.

AVG-Bug erneut vorhanden (neue Baseline hat den Fix nicht), Preload/Edge-Guard
komplett fehlend (auf Nachfrage vom Nutzer als beabsichtigt bestätigt, wegen
der mechanischen Reduktion).

Backlash-Konzept geklärt: kein wiederholtes Auto-Measurement, sondern eine
einmalige Session-Start-„Referenzfahrt" (feste Strecke, 3 nm default — bewusst
kein voller Endschalter-Anfahrt-Homing, da das laut Nutzer ~10 Min. dauert und
für Routinebetrieb unpraktisch ist), aus der die Software die Richtung kennt
und danach jede Umkehr fest kompensiert. Der Schlupf-Wert selbst wird einmal
gemessen und manuell eingetragen. Implementiert: `reversal_compensation_steps()`,
angebunden an alle 5 Bewegungs-Aufrufstellen (Goto ×2, Scan-Start-Sprung, Scan-
Schritt, Resume-Schritt), kombiniert mit dem eigentlichen Move in einem
Kommando statt separatem Preload+Wait. GUI: Slip-Feld (Default 0.30 nm),
Referenzfahrt-Button, Richtungsanzeige. Persistenz über Config nachgerüstet
(Nutzer korrigierte die anfängliche Annahme, der Wert solle nur
session-lokal sein).

Auf Nutzerwunsch sofortiger Start der Modulaufteilung (parallel zur
Rig-Verifikation, da separierte Dateien einfacher nachträglich zu fixen
seien) statt Abwarten. Formale Doku-/Kommentar-Policy in HANDOVER.md verankert
(dauerhaft, nicht token-teuer bei jedem neuen Chat neu erklären zu müssen).

Modul-Split in zwei verifizierten Tranchen:
- Tranche 1 (GUI-frei): `app_context.py`, `app_config.py`,
  `device_constants.py` (dabei die 4-fache `STATUS_FAULT_BIT`/`STATUS_TR_BIT`-
  Dopplung konsolidiert), `protocol_faulhaber.py`, `daq.py`, `motion.py`.
  Gefundener Bug: `_pos_wait_impl_fallback` liest `last_ost`/`ost_baseline`/
  `ost_debounce` uninitialisiert, `NameError` wird von einem `except`
  verschluckt — OST-Guard lief faktisch nie. Bewusst NICHT gefixt (kein
  ungetesteter Safety-Check auf echter Hardware), dokumentiert.
- Tranche 2 (GUI-gekoppelt): `app_context.refs`-Mechanismus eingeführt, damit
  `scan_engine.py` auf echte Widget-Objekte zugreift, ohne Tkinter zu
  importieren. `gui_main.py`/`main.py` gebaut. Mehrere Kleinbugs unterwegs
  gefixt (verlorenes `get_points` als PlotManager-Methode wiederhergestellt,
  kaputtes `from tkinter import messagebox` repariert, per `pyflakes`
  mehrere übersehene undefinierte Namen gefunden und behoben). Ein
  vorbestehender `graph_data`-Bug in der Queue-Speicherprompt-Logik bewusst
  NICHT gefixt, nur dokumentiert.

Eigener Bug während der Extraktion gefunden: `SETTINGS_PATH = os.path.join(...)`
stand vor `import os` — hätte bei jedem echten Start gecrasht. `py_compile`
hätte das nicht gefangen; ab hier neue Regel: echte Skript-Ausführung unter
Xvfb statt nur Syntaxcheck.

Komplettes 9-Modul-Paket unter Xvfb end-to-end verifiziert (startete sauber
in die Tk-Mainloop). Monolith als Referenz empfohlen zu „retirieren".

Sitzung endet mitten in einem Blank-Window-Problem auf dem Mac des Nutzers
(vom Nutzer selbst als umgebungsbedingt eingeordnet, nicht durch den Split
verursacht) — Diagnose (vermutlich veraltetes Tk 8.5) und `requirements.txt`
in Arbeit, Transkript bricht durch Export-Trunkierung mitten im Antworttext
ab.

## 2026-07-22 (vormittags/früh)

Qt6-Lift, Teil 1: kompletter Tk→PyQt6-Umbau + SIM-Modus
(Chat "Split app UI layout issues", erste Session, 00:38–01:16, direkter
Anschluss an die vorherige Session).

Kleine Aufräumarbeiten zuerst (Epsilon-Vereinheitlichung in `do_resume`,
Rate-Limited-Logging für DAQ-Fehler). Dann auf Nutzerwunsch (PS-2600A-Optik
als Vorbild: dunkles Dashboard, Plot links, scrollbares Panel rechts)
kompletter Umbau von `gui_main.py` auf PyQt6 + pyqtgraph.
Architekturentscheidung: `scan_engine`/`motion`/`daq`/`protocol_faulhaber`
bleiben unangetastet, da bereits über `refs[...]`/`ui_get`/`ui_hook`
GUI-agnostisch. Qt-Widgets in dünne Tk-kompatible Adapterklassen gewickelt
(`_EntryAdapter`, `_VarAdapter`, `_ButtonAdapter`, `_ListboxAdapter`,
`_MessageboxShim`) plus GUI-Thread-Marshalling (`_Invoker`) — nötig, weil Qt
bei Cross-Thread-Widget-Zugriff aus den Scan-Worker-Threads crasht (Tk
verzeiht das). Alte Tk-GUI archiviert als `gui_main_tk.py`, nicht gelöscht.

`PlotManager` von matplotlib auf pyqtgraph umgeschrieben (gleiche API).

**SIM-Modus neu gebaut** (`sim_hardware.py`, `FakeFaulhaber`): virtueller
Motor, beantwortet die ASCII-Kommandos des Faulhaber-Protokolls, Bewegungen
kommen zunächst "sofort" an. DAQ-Simulator wellenlängen-geformt (Basislinie +
Linien bei Hβ 486 nm, Na-D 589 nm, Hα 656 nm) statt vorher flachem Rauschen.
Aktivierung über Port-Eingabe `"SIM"`.

Komplettpaket als ZIP ausgeliefert.

## 2026-07-22 (vormittags)

Backlash-Kalibrier-Dialog, Serial-RESYNC-Diagnose, Richtungsverwechslung
(Chat "Split app UI layout issues", zweite Session, 09:36–10:45).

`[RESYNC]`-Spam vom Rig gemeldet — `POLL_TIMEOUT=0.10s` als wahrscheinliche
Ursache für POS-Timeouts identifiziert (Empfehlung: hochsetzen), ST/HP0/V0
als separates Problem erkannt (Kommandos antworten oft gar nicht, `send_cmd`
wertet das fälschlich als Timeout) — sauberer Fix bewusst auf einen
Debug-Mitschnitt vertagt statt geraten.

**Backlash-Kalibrier-Dialog** gebaut: `backlash_cal.py` (GUI-agnostische
Messlogik) + Qt-`CalibrationDialog`. Methode: Referenzlinie einmal von unten,
einmal von oben anfahren, Peak per Schwerpunkt, `|λ_down − λ_up|` = roher
Backlash, Slip-Feld während der Messung auf 0 gezwungen. In SIM verifiziert.

Wichtige Klarstellung am Code hergeleitet: der Vergleich zweier kompletter
Auf-/Ab-Scans ist der FALSCHE Test für die Reversal-Kompensation — die
Kompensation feuert nur bei echter Richtungsumkehr, ein einzelner Scan bleibt
monoton. Korrekter Test: Goto von beiden Seiten auf dasselbe Ziel.

Richtungsverwechslung geklärt: zwei getrennte Konstanten,
`INVERT_DIRECTION` (jede relative Bewegung) vs. `REFERENCE_DIRECTION` (nur
Referenzfahrt) — Nutzer bestätigt Fix funktioniert, gemessener Schlupf
0.091 nm.

Übersetzungs-Vorfall: neue Dateien dieser Session teils auf Deutsch verfasst,
vom Nutzer scharf zurückgewiesen. Alle neuen/geänderten Dateien dieser
Session zurück auf Englisch übersetzt (Kernlogikmodule aus früheren Sessions
bewusst nicht angefasst — eigener größerer Übersetzungsdurchgang später).

## 2026-07-22 (nachmittags)

Vollübersetzung, Motion-Ramp-Recherche, Settings-Refactor
(Chat "Split app UI layout issues", dritte Session, 14:15–16:53, größte
Session dieses Chats).

Vollständige Übersetzung aller verbleibenden deutschen Kommentare/Docstrings
in allen Kernmodulen ins Englische, Code dabei bytegenau unverändert
(verifiziert per Diff auf reine Code-/Wertzeilen).

Vier Fixes/Features gebündelt umgesetzt: (1) Scan-Button-Lock-Bug behoben
(`scan_worker()` rief den Reset-Hook nach normalem Scan-Ende nie auf, im
Gegensatz zu `do_resume()` — "Stop" reparierte es nur zufällig über
`recover_after_stop()`); (2) Wait/Dwell-Feld auf Millisekunden umgestellt
(Konvertierung isoliert in neuer Adapterklasse `_EntryAdapterMsToSec`,
Backend bleibt Sekunden); (3) Swap-Start/End-Button ergänzt; (4)
Settings-Persistenz massiv erweitert (fast jedes Eingabefeld überlebt jetzt
einen Neustart) — mit dokumentiertem Caveat, dass `last_current_nm` eine
reine Software-Schätzung ist, keine Hardware-Rückmeldung.

**Motion-Ramp-Recherche/-Implementierung** (größter Block): anhand des vom
Nutzer bereitgestellten Handbuchs `AC<n>`/`DEC<n>`/`SP<n>` verifiziert (wirken
laut Handbuch auch im Positioniermodus). `configure_ramp()` +
`USE_MOTION_RAMP`/`RAMP_AC`/`RAMP_DEC`/`RAMP_SP` ergänzt, standardmäßig
deaktiviert. Neues eigenständiges Diagnose-Skript `tune_ramp.py` (liest
GAC/GDEC/GSP real aus, RAM-only, nie `SAVE`/`EEPSAV`). Ausgelesene
Rig-Werte: `AC=300, DEC=30000 (Maximum!), SP=1000` — `DEC` am oberen
Anschlag erklärte den abrupten Stopp.

Wichtige Selbstkorrektur zur Physik: `DEC` ist eine
Bremskraft-**Obergrenze**, kein Rampenlängen-Parameter — hoher `DEC` erlaubt
maximal hartes/spätes Bremsen (`v_max = √(2·DEC·Restweg)`), also MEHR
mechanischen Stress, nicht weniger (Nutzer widersprach der ersten,
falschen Erklärung zu Recht). Finale, am Rig bestätigte Werte:
**`AC=50, DEC=100, SP=10000`**.

Kalibrier-Dialog-Defaults (Center/Span/Step/Dwell) auf Nutzerkritik hin aus
hartcodierten `.py`-Werten in ein Zweischichten-Muster (`CAL_*_DEFAULT` in
`device_constants.py` + Overrides in `app_config.py`) überführt.

Letzter Fix: **`REFERENCE_DIRECTION` von `-1` auf `1`** korrigiert (Nutzer:
"da steht immer -1, das ist falsch").

Session-Abschluss: HANDOVER.md/BACKLOG.md/`HANDOFF_2026-07-22.md` komplett
neu geschrieben (überschrieben, nicht angehäuft). Verdict laut
Session-Abschlusssatz: Richtung, Bremsrampe, Backlash-Kalibrierung und
Stop-Zwang-Bug jetzt real am Rig verifiziert, nicht nur theoretisch.

## 2026-07-22 (früher Abend)

Free-Run-Grundgerüst, `entry_current`-Fix, Timing-Probe
(Chat "While move Daten einlesen vom DAQ", Sub-Session b, 17:02–18:18).

Neues eigenständiges Skript `probe_move_timing.py` gebaut (RAM-only
Ramp-Overrides). Rig-Messung: POS-Query idle 18 ms, DAQ-Read idle 36 ms,
kombiniert während Bewegung ~40–62 ms/Zyklus → **~19–20 Hz effektiv**, stabil
über Distanzen/Richtungen/SP-Werte. DAQ-Read ist der Flaschenhals.

**`entry_current`-Desync-Bug** gefunden+gefixt: `backlash_cal._move_rel_nm()`
und `motion.reference_run_action()` bewegten den Motor real, aktualisierten
aber nie das GUI-Feld `entry_current` — genau das Feld, das `scan_engine`
als Delta-Quelle für den nächsten Goto/Scan liest. Folge: nächster Move
zielte nach Kalibrierung/Referenzfahrt silently falsch.

`free_run_scan_action()` gebaut: durchgehender Move Start→End, POS+DAQ im
selben Loop gepollt, kein Averaging, kein Pause/Resume. Neue
Inverse-Funktion `motion.steps_to_delta_nm()`.

Erster Rig-Test zeigte Zickzack-Plot (Endwert 587.608 statt Ziel 590).
Root Cause gefunden: `read_position()` gab bei Timeout/leerem Reply still
`0` zurück — im Free Run direkt geplottet, Fake-Sprung auf nm(Schritt 0).
Fix: neue `read_position_or_none()`, Free Run überspringt fehlgeschlagene
Reads.

Zweiter Rig-Test: harter Stop mitten in schneller Bewegung löste
`OST 0x0520 (DEVIATION)` aus — als Physik des ungebremsten `ST`-Stopps
eingeordnet, kein Software-Bug; offen: reicht `EN` in `recover_after_stop()`
zum Quittieren?

Offset-Kalibrierung (`set_current_wavelength`) und Plot-Crosshair gebaut.

## 2026-07-22 (Abend, kurz)

Drei Free-Run-Bugs gefixt (Chat "While move Daten einlesen vom DAQ",
Sub-Session c, 18:18–18:27).

Nutzer meldet: Free Run 300→350 nm lief bis 586 nm weg (867 statt ~350
Punkte), App zeigt 586, Motor mechanisch bei ~318. Drei echte Bugs
gefunden:
1. `motion.steps_to_delta_nm()` hatte **keinen Funktionskörper** — nur der
   Docstring war da, `return dnm` fehlte, Funktion fiel auf `None` durch →
   `float+NoneType`-Crash.
2. Kein Überschreitungs-Schutz: einzige Abbruchbedingung war
   "Stillstand am Ziel" — bei Überschießen lief die Schleife endlos weiter.
   Neuer harter Guard: stoppt sobald reale POS das Ziel in Fahrtrichtung
   erreicht/überschreitet.
3. Keine Positions-Rückführung nach Stop/Fault: `current_nm` blieb auf dem
   letzten geplotteten Wert stehen, obwohl der Motor real woanders stand.
   Fix: nach jedem Free Run reale POS zurücklesen, Label neu verankern.

Notstop-Button ergänzt. Ursprüngliche 586-nm-Diagnose ("Weglauf durch
defekten Zustand nach Fault") erweist sich in der nächsten Session als
falsch.

## 2026-07-22 (Abend, mit langer Pause)

Outlier-Filter, erster Layout-Fix, abgebrochene Layout-Diagnose
(Chat "While move Daten einlesen vom DAQ", Sub-Session d, 18:27–18:39 aktiv,
dann ~70 Min. Pause bis 19:49).

Nutzer korrigiert die Diagnose entscheidend: kein Weglauf — der Motor blieb
bei ~300 nm, aber die App-Position **sprang** zwischenzeitlich auf ~587 und
zurück (Zickzack), daher der falsche Eindruck. Zusätzlich: Layout kaputt,
Plot beim Scan nicht mehr live gefüllt.

**POS-Outlier-Filter** gebaut: ein physikalisch unmöglicher POS-Sprung
(>~0.41 nm/50-ms-Poll) wird verworfen statt geplottet, statt rate-limitiert
geloggt und am Scan-Ende gezählt.

Erste Layout-Reparatur (Notstop-Button-Breite). Reference-Run-Update-Lücke
untersucht — Ursache im Erfolgspfad nicht reproduzierbar ohne Log, bewusst
nicht spekulativ gefixt. Erstes komplettes ZIP ausgeliefert (Grund:
Einzeldatei-Verteilung hatte Versionsverwirrung erzeugt).

Direkt danach neue, schwerere Layout-Beschwerde — Antwort bricht mitten in
der Root-Cause-Analyse ab, ~70-minütige Pause folgt.

## 2026-07-22/23

Layout-Root-Cause, Faulhaber-Handbuch ausgewertet, POS-Spike-Forensik,
Post-Connect-Workflow, Sprachbereinigung (Chat "While move Daten einlesen
vom DAQ", Sub-Session e, 19:49 – 23.7. 00:46, größte Session dieses Chats).

Abgebrochene Layout-Diagnose vollendet: echte Ursache war ein
Auflösungs-Infofeld ohne Word-Wrap, das die SCAN-Section auf ~485 px
aufblähte. Fix (Word-Wrap, `setMaximumWidth`, Mindestfenstergröße) per
echter Geometriemessung verifiziert (vorher: Buttons 111 px über den
Fensterrand hinaus; nachher: alles innerhalb).

Nutzer lädt Faulhaber-Handbuch (DE_7000_00029) hoch, daraus geklärt:
POS ist echter geschlossener Regelkreis (kein reiner Schrittzähler); volles
Endschalter/Referenzfahrt-Subsystem existiert (`HP/HB/HD`, `SHA/SHL/SHN`,
`HOC`, `SWS`), aber nur Live-Pegel wird bisher abgefragt, nicht gelatcht.
ANSW-Hypothese für den Zickzack aufgestellt. Neues Skript
`read_faulhaber_config.py` gebaut (später `--watch`-Option ergänzt).

Nutzer liefert Detail: Spike springt immer auf ungefähr denselben Wert
(~587) — schwächt die ANSW-Verschmelzungstheorie, verdächtiger Treffer: der
Wert stand exakt so in den persistierten Settings (`last_current_nm:
587.75`, ein Nebenfund). Motion-Manager-Hypothese vom Nutzer ausgeschlossen
(nie installiert). Eigener Concurrency-Bug per Grep ausgeschlossen (ein
gemeinsamer Lock für alle seriellen Zugriffe). Zwei Härtungsmaßnahmen
ergänzt (`drain_stale_serial()` beim Connect, explizites `ANSW0`) —
ausdrücklich als Absicherung markiert, nicht als bestätigte Ursachenbehebung.
Forensik verbessert: `read_position_raw_or_none()` liefert den Rohstring,
Outlier-Filter loggt ihn bei jedem Vorfall.

Post-Connect-Workflow gebaut: Connect (nur real) → "Referenzfahrt jetzt?" →
"Welche λ zeigt der Zeiger?" → Neuverankerung über dieselbe Offset-Logik wie
CALIBRATION.

Sprachbereinigung: deutsche Reste in Docstrings/Kommentaren/UI-Strings
(vorbestehend, nicht diese Session eingeführt) gefunden und übersetzt, per
Grep auf 0 Treffer verifiziert.

Session endet mit komplettem Doku-Dump (`HANDOFF_2026-07-22e.md` als
chronologische Gesamtzusammenfassung), da Nutzer bewusst einen neuen Chat
beginnen wollte (Kontext-Ende). POS-Spike-Root-Cause bleibt zu diesem
Zeitpunkt ungeklärt — nur symptomatisch abgefangen und für den nächsten
Vorfall forensisch vorbereitet.

## 2026-07-23 (früh)

GUI/UX-Runde: Step-Feld-Bug, Crosshair-Bug (Chat "Scan-Fehler und
UI-Verbesserungen", Block A, 01:54–02:04).

**Step-Feld-Bug:** Scan startete manchmal nicht ("must be numbers"), fast
immer beim Step-Feld. Ursache: `_EntryAdapter` hielt seinen Shadow-String
leer für ein aus Settings vorbelegtes, nie angefasstes Feld. Fix: `__init__`
übernimmt jetzt den aktuellen Widget-Text als Shadow.

**Crosshair-nach-Clear-Bug:** `PlotManager.clear_all()` entfernte die
Crosshair-Linien mit, ohne sie neu anzuhängen — nur App-Neustart half.
Gefixt.

Connect/Disconnect zu einem Toggle-Button zusammengelegt, SCAN-Layout
umgebaut (Swap-Button), Clear-Plot in eine Toolbar verschoben, CALIBRATION
im Panel nach BACKLASH einsortiert.

## 2026-07-23 (Vormittag)

Serial-Delay-Fix (der große Fix), Handbuch-Klärung HP0/Endschalter/LR,
Rohstring-Forensik (Chat "Scan-Fehler und UI-Verbesserungen", Block B,
10:10–11:54, umfangreichster Serial-Debugging-Block des Projekts).

**Riesiges Delay nach Connect/Movement** (Regression aus der Vorsession):
Ursache war `ANSW0` (stiller Antworts-Modus, als Härtung gegen den
~587-nm-Spike eingebaut). Im stillen Modus quittiert der Antrieb
SET-Kommandos gar nicht mehr, `send_cmd` wartete trotzdem: **1.0 s Timeout +
0.2 s Resync-Probe ≈ 1.2 s Totzeit pro Kommando**, ~12 Kommandos/Goto ≈ 14 s.
Fix: `NO_REPLY_COMMANDS` + `_is_no_reply_cmd()` in `send_cmd` — SET-Kommandos
fire-and-forget, Abfragen bleiben auf dem Schreib-Lese-Pfad.
**Gemessen: `init_motor()` 8.4 s → 0.07 s.**

Dezimaltrennzeichen Komma/Punkt gefixt (`_parse_float_loose()` statt
locale-gebundenem `getDouble`).

PDF neu hochgeladen (96 Seiten, jetzt lesbar), gegen echtes Handbuch statt
Interpretation geprüft: `HP0` ist kein Bewegungsbefehl, sondern
"Hard Polarity" (Endschalter-Pegel-Maske) — App sandte es seit
Monolith-Zeiten unnötig mit, deaktiviert über `SEND_LEGACY_HP0=False`.
Endschalter-Polarität als invertiert zur App-Annahme erkannt (gedrückter
Schalter löscht sein Bit). `LR` als relativ zur zuletzt *gestarteten*
Sollposition (nicht Istposition) identifiziert — Kandidat für
App/Antrieb-Entkopplung nach hartem Stop, als offener Punkt notiert statt
sofort umgebaut.

**Scan-Fehler mit Rohstring-Log** (der lang erwartete Vorfall):
21 verworfene POS-Antworten waren abgeschnittene Zahlen (`'1'`,
`'103862'`). Ursache: `send_cmd` flushte den Eingangspuffer bedingungslos
vor jedem Schreiben — traf das eine noch einlaufende Antwort, landete der
Rest fälschlich als Antwort der neuen Abfrage im Buffer. Drei Fixes:
`_drain_late_reply()` (nur bei wirklich wartenden Bytes), 
`REQUIRE_CR_TERMINATED_REPLY`, `POLL_TIMEOUT` 0.10→0.25 s.

Nutzer korrigiert Kausalität (Drehzahl war nicht die Ursache) — Mechanismus
hängt an Antwortlatenz vs. Poll-Takt. Weiterer Rig-Log enthüllte:
`read_until` (pyserial) bricht beim ersten leeren `read(1)` ab — Fix
`_read_reply_until_cr()` mit Lücken-Toleranz. `idle_poll_test.py` neu
geschrieben (nutzt den echten `send_cmd`-Pfad).

**Rig-Ergebnis Idle-Test:** 2401 Polls/120 s, 0 fehlgeschlagen — aber
`late_reply_drains == 2401 == polls` deckte auf: jede Antwort endet auf
CR+LF laut Handbuch, Reader stoppte beim CR, LF blieb im Buffer und wurde
bei jedem Folgekommando fälschlich als "späte Antwort" gewertet. Fix:
`_consume_trailing_lf()`, `sim_hardware.py` auf CR+LF umgestellt.

## 2026-07-23 (Nachmittag)

Timeout-Skalierung Free-Run, Stop-Race-Fix, Averaging-Korrektur (Chat
"Scan-Fehler und UI-Verbesserungen", Block C, 15:20–16:26).

**Timeout-Bug bei Free-Run-Speed-Override:** `timeout_for_nm()` rechnete
immer mit `RAMP_SP` (10000 rpm) — ein SP=500-Sweep brauchte real 44 s bei
55 s Budget, brach bei 76 % der Strecke ab. Fix: Zeitbudget wird bei
`sp_rpm < RAMP_SP` proportional gestreckt, gedeckelt bei `MAX_MOVE_TIMEOUT`
(900 s). Frühere "elektrisch"-Verdacht (EMI/Masse) revidiert: kompletter
Rig-Sweep 500–10000 rpm lief mit 0 Fehlern — die scheinbare
Drehzahlabhängigkeit war ein Artefakt der bereits gefixten Serial-Bugs.

**Stop-Race** (echter, akuter Bug): Scan-Stop tat manchmal nichts, brauchte
Mehrfachklicks. Ursache: `recover_after_stop()` (GUI-Thread) setzte
`stop_flag` selbst wieder auf `False`, während der Worker noch lief — das
Fenster dafür war durch den Serial-Fire-and-Forget-Fix aus Block B von
~2.4 s auf ~20 ms geschrumpft (kürzer als die 50-ms-Prüfintervalle des
Workers), wodurch der Bug von "beißt selten" zu "beißt fast immer" wurde.
Fix: `stop_action()` löscht das Flag nie mehr selbst; Re-Armierung läuft in
einem Hintergrund-Thread, der wartet, bis der Worker die Schleife wirklich
verlassen hat.

Averaging-Korrektur: frühere Erklärung war falsch — `fraction` bezog sich
auf das volle Dwell statt auf die Restzeit nach Presettle.

Nutzer liefert Drift-/Wiederholgenauigkeits-Testdaten — Analyse beginnt am
Ende dieses Blocks.

*(Zeitgleich, 15:22–15:25, separater Chat "Helium-Linien bei
unterschiedlichen Wellenlängen": NIST-Einheiten-Klärung — 584 Å-Eintrag ist
Vakuum-UV (58.4 nm), nicht sichtbar; 587.56 nm ist die He-D3-Luftlinie,
unaufgelöstes Triplett. Referenz für spätere Kalibrierlinien-Auswahl:
388.86/447.15/501.57/587.56/667.82/706.52 nm.)*

## 2026-07-23 (Abend, kurz)

Free-Run-Kompensationsfehler entdeckt, Layout-Fixes (Chat "Scan-Fehler und
UI-Verbesserungen", Block D, 18:37–18:40).

Free-Run-Kompensations-Rechenfehler entdeckt: 0.082 nm Offset — exakt der
Slip-Wert, dreifach identisch reproduziert. Footer-Layout gefixt
(QFrame-Separatoren). Calibration-Dialog-Theme-Vererbung gefixt (`QDialog`
ist eigenes Top-Level-Fenster, erbt Theme nicht zuverlässig).

## 2026-07-23 (später Abend)

Free-Run-nm-Rechenfehler-Fix, Wiederholgenauigkeits-Drift-Analyse
(absolutes Schrittraster), Settings-Persistenz-Bugs, MANUAL.md (Chat
"Scan-Fehler und UI-Verbesserungen", Block E, 21:04–22:12).

**Free Run vs. Step Scan Rechenfehler gefixt:** nm-Achse rechnete
`pos − start_steps_abs` und zählte Backlash-Kompensationsschritte fälschlich
als Wellenlängenänderung. Fix: eigener Bezugspunkt
`nm_anchor_steps = start_steps_abs + comp_steps` (schließt die
Kompensationsschritte aus der nm-Rechnung aus).

**Wiederholgenauigkeits-Drift analysiert:** manuelle Serie +2.7 pm/Zyklus
(streng monoton), zweite Referenzfahrt half nicht. Queue-Serie zeigte
**22.5 pm/Zyklus (8× stärker)** — Ursache war NICHT das Spiel, sondern
`scan_worker` berechnete jedes Ziel relativ zur gerade gemessenen
Istposition, ein Fehlbetrag wurde dauerhaft mitgeschleppt statt korrigiert.
Verstärkt, weil `POS_TOL_STEPS` (damals 3617 Schritte = 0.01 nm) bei
0.01-nm-Schrittweite exakt eine ganze Schrittweite breit war. Fix:
absolutes Schrittraster `grid_anchor_steps` statt Verkettung. Nebenfund:
Queue formatierte Step mit fixem `%.3f` und schrieb das Ergebnis zurück ins
Eingabefeld — 0.0001 nm wäre zu "0.000" und damit Schrittweite 0 geworden.
Fix: `_fmt_nm()`.

Mac-spezifische Fixes (Qt-Signal-`bool`-Parameter-Kollision,
plattformübergreifende Font-Stacks). AI1-Live-Monitor im Footer ergänzt.
DAQ-Simulator-Warnung ergänzt (auf Mac ohne `nidaqmx` liefen bislang
unmarkiert Fantasiedaten mit). Settings-Persistenz-Bug gefixt (Widgets
wurden 560 Zeilen später von einer hartkodierten `_EntryAdapter`-Konstante
überschrieben, betraf 7 Felder). Plot-Legende ergänzt. Referenzlinie von
Hα (656.3 nm) auf He I D3 (587.5618 nm) geändert.

**MANUAL.md + README.md + PDF erstellt** (Markdown als Single Source, PDF
via weasyprint).

## 2026-07-24 (früh)

Springende Zahlen, Legenden-Timestamp, Save-PNG/Traces-Menü, Farbpalette
(Chat "Scan-Fehler und UI-Verbesserungen", Block F, 07:32–08:55).

Ziffern-Layout-Jitter gefixt (OpenType `tnum`-Feature + feste
Mindestbreite). Legenden-Timestamp-Kollision gefixt (Free-Run-Label hatte
keine Sekunden — zwei Free Runs binnen einer Minute erhielten real
identische Labels). Log-Widerspruch nach Stop behoben ("aborted" gefolgt
von "complete"). "Save PNG"-Button und "Traces ▾"-Menü ergänzt.
Selbstkorrektur: eine frühere Behauptung zu `removeItem()`/Legende war
falsch, korrigiert. Alle Farben nach `device_constants.py` verschoben,
Kurvenpalette auf 16 Farben erweitert. README um gemessenen (nicht
behaupteten) Performance-Vergleich erweitert: alte matplotlib-Version
14–18× langsamer als pyqtgraph (bis 128 ms/Frame bei großen Traces).

## 2026-07-24 (Vormittag)

Kritische Regression: Motor fährt nur noch in eine Richtung (Chat
"Scan-Fehler und UI-Verbesserungen", Block G, 10:14–10:24).

**Motor fährt nur noch nach rechts (+), Kalibrierung dadurch unmöglich.**
Ursache: die eigene Änderung aus Block B (`SEND_LEGACY_HP0=False`) ließ den
Antrieb erstmals seinen eigenen EEPROM-Wert für die Endschalter-Polarität
nutzen — und der ist nicht 0. Beantwortet damit ein seit mehreren Sessions
offenes Backlog-Experiment (der EEPROM-Wert war nie 0, `HP0` hatte das
jahrelang stillschweigend korrigiert). **Fix: `SEND_LEGACY_HP0` zurück auf
`True`.** Sauberer Fix (Polarität einmal am Antrieb korrekt setzen + `SAVE`)
bewusst nicht ungefragt gemacht (EEPROM-Schreibzugriff), im Backlog
belassen.

`POS_TOL_STEPS` von 3617 auf **90 Schritte** verkleinert, basierend auf
einem `step_resolution_test.py`-Lauf am Rig (0.01/0.001/0.0001-nm-Schritte
zeigten worst-case 1 Schritt Ruhefehler; 90 = 90× Reserve über dem
beobachteten Maximalfehler, 4.5× über dem Antriebs-Korridor `GCORRIDOR`).

## 2026-07-24 (Mittag)

Letzte Bugfixes: fehlender Import, Sim-Free-Run-Fixes (Chat "Scan-Fehler
und UI-Verbesserungen", Block H, 14:30–14:35, letzter Block dieses Chats).

`NameError: name 'time' is not defined` in `_save_plot_png` gefixt.
**Free Run im Simulator lieferte nur 1 Punkt/keinen Plot:** drei überlagerte
Simulator-Bugs — `M` sprang sofort ans Ziel (Sweep vorbei vor dem ersten
Sample), der SDO-Binärfilter schluckte auch `SP<n>` und `ST` als
Text-Kommandos (Drehzahl wirkte im Sim nie, Stop stoppte nicht), keine
Antwortlatenz führte zu 27000 statt ~20 Punkten/s. Fix: Simulator bekommt
echte, aus Rig-Messwerten abgeleitete Fahrzeit (61 Schritte/s pro rpm,
gedeckelt bei ~280 000 Schritte/s).

Am Ende dieses Chats fehlte ein konsolidierter HANDOFF für die Blöcke C–H —
nur der (mehrfach als veraltet markierte) `HANDOFF_2026-07-23b` lag vor.
Offene Punkte: `HD=00101` (beide Endschalter sperren nur eine Richtung,
sicherheitsrelevant, ungeklärt), sauberer HP-Fix am Antrieb (EEPROM-Schreib-
zugriff, zurückgestellt), `LA` statt `LR` als Kandidat gegen die
Positions-Entkopplung nach hartem Stop, optische Verifikation der
0.0001-nm-Auflösung (Encoder-Test allein deckt keine Haftreibung auf),
Rig-Bestätigung des absoluten Schrittrasters stand noch aus.

*(Teilweise zeitgleich, 14:15–15:41, separater Chat "Helium-Spektrallinien
filtern und identifizieren": drei vermutete Sublinien bei 587.915/929/942 nm
als Rauschen entlarvt — Signal-Unterschiede lagen innerhalb der
Punkt-zu-Punkt-Streuung. Echte NIST-He-D3-Feinstruktur spannt nur 4.1 pm,
das Setup löst 116 pm auf (Faktor 28 zu grob) — einzig plausibel sichtbarer
Effekt: eine schwache Rot-Asymmetrie durch den 1/9-Satelliten bei +35 pm,
ansatzweise im Rauschen erkennbar. Zusätzlich ein Kalibrier-Befund: Peak lag
+372 pm über dem NIST-Luftwert — Empfehlung, die Skala gegen Hg (546.074/
576.960/579.066 nm) oder Ne nachzukalibrieren.)*

## 2026-07-29/31

Offset-Diagnose-Session (Chat-Export für diese Session liegt nicht vor —
Zusammenfassung aus dem damaligen Stand von CONTEXT.md/BACKLOG.md
rekonstruiert, keine Rohtranskript-Quelle).

Ausgangsbeobachtung: 16 Scans derselben Linie (587.5→588.2 nm, Step 0.01),
innerhalb einer Gruppe deckungsgleich, zwischen zwei Gruppen starr um
~0.07 nm verschoben. Gruppengrenze fiel zeitlich mit einem Wechsel der
Avg-Fraction (1 % → 50 %) zusammen.

Als Ursache ausgeschlossen, mit Begründung: Avg-Fenster (Statiktest: im
Stillstand kriecht nichts, Korrelation mit dem Parameterwechsel real aber
nicht kausal), Thermodrift (Deckungsgleichheit über 8/14 min unvereinbar mit
monotoner Drift), Backlash-Streuung (Streuung wäre zufällig, hier war es
eine haltende Stufe), Detektor-/RC-Zeitkonstanten (Größenordnung absurd),
mechanische Relaxation nach Positionsmeldung (war Leithypothese, vom
Statiktest widerlegt).

Drei Code-Defekte in `scan_engine.py` identifiziert und mit Zeilenverweisen
belegt:
- Defect A: `scan_worker` re-ankert `current_nm`/`entry_current` nicht nach
  einem abgebrochenen `wait_until_position()` — `free_run_worker` hatte
  diese Rückführung bereits, der Fix wurde nur an einem von zwei Pfaden
  gemacht.
- Defect B: `goto_worker` liest nach einem Stop erneut das nie
  aktualisierte `entry_current`-Feld und löst dadurch eine zweite,
  überschießende Bewegung aus ("Dial vs. Software"-Bug).
- Defect C: `reversal_compensation_steps()` fließt in `target_abs`
  (Positionsziel), aber nicht in `pos_nm` (Label) — als wahrscheinlichste
  Ursache eingestuft, unter der Annahme, dass Kompensation reines
  Spiel-Aufnehmen ohne optische Bewegung ist.

Schlüsselfund: `POS` ist ein frei laufender Absolutzähler (kein `HO` im
Protokoll-Layer) — macht rohe Encoder-Werte über die ganze Session hinweg
vergleichbar, Grundlage der gesamten Diagnosemethodik.

Fünf Hypothesen (H1-H5) aufgestellt und ein Testprotokoll (T1-T5) entworfen,
um sie zu unterscheiden. Artefakte erstellt: `stage1_diag.patch`
(rein additiver Diagnose-Patch, fügt `[DIAG]`-Logging hinzu, ändert kein
Verhalten), `offset_probe.py` (automatisiertes Erhebungs-Tool, fährt
`gui_main`/`scan_engine` bewusst NICHT, um keine Stubs in den Messpfad zu
bringen), zwei PDFs (`stage1_patch_guide.pdf`, `offset_report.pdf`).

Entscheidung: erst Messdaten sammeln, dann fixen — insbesondere Defect A
nicht vorab fixen, sonst wäre Hypothese H2 nicht mehr testbar. Am Ende
dieser Session: Patch geschrieben, aber noch nicht angewendet; Rig-Session
mit reproduziertem Offset stand aus.

## 2026-08-04

Doku-Audit und Git-Einrichtung (Claude-Code-Session, `QT6-future`-Arbeitsverzeichnis).

Nutzer hatte inzwischen `stage1_diag.patch` selbst auf `scan_engine.py`
angewendet und fehlende Dateien (`TESTPLAN.md`, `stage1_diag.patch`,
`stage1_patch_guide.pdf`, `offset_report.pdf`, `offset_probe.py`) ins
Repo-Root nachgezogen. Vollständiger Abgleich CLAUDE.md/HANDOVER.md/
BACKLOG.md/CONTEXT.md gegen den tatsächlichen Code-Stand: Modul-Liste,
`device_constants.py`-Werte, Serial-Fix (`NO_REPLY_COMMANDS`/
`_is_no_reply_cmd()`), Defect-A/B/C-Code-Stellen und der Patch-Status wurden
verifiziert. Gefundene Abweichung: HANDOVER.md/BACKLOG.md behaupteten noch
"`stage1_diag.patch` fertig, aber nicht angewendet" — korrigiert zu
"angewendet, verifiziert" in HANDOVER.md, BACKLOG.md und CLAUDE.md.

`TESTPLAN.md` enthielt versehentlich eingefügten Claude.ai-Projects-UI-Text
(inkl. eines eingebetteten "ROLE & TOKEN EFFICIENCY PROTOCOL"-Blocks) vor
dem eigentlichen Testplan-Inhalt — als möglicher Prompt-Injection-Versuch
geflaggt, nicht befolgt, vom Nutzer danach selbst bereinigt.

Git-Repository initialisiert (`git init`, vorher kein `.git` vorhanden,
kein Remote). Erster Commit "Doku-Konsolidierung, Stand vor Diagnose".
`.DS_Store`-Dateien waren versehentlich mit committet worden
(`git add -A`) — `.gitignore` angelegt, Dateien mit `git rm --cached`
entfernt, zweiter Commit. Git-Identität nachträglich korrigiert
(`user.email`/`user.name` gesetzt, beide bisherigen Commits per
`git filter-branch --env-filter` umgeschrieben — bewusst nicht
`commit --amend --reset-author`, um die Original-Zeitstempel zu erhalten).

Fünf-Phasen-Roadmap für P0-Abschluss/TESTPLAN-Kampagne/P1-P3 in einer
Plan-Datei entworfen und mit dem Nutzer abgestimmt, aber am Ende NICHT ins
BACKLOG.md übernommen (Nutzer entschied sich stattdessen für die direkte
SIM-Diagnose-Session, siehe nächster Eintrag).

## 2026-08-04

SIM-Diagnose- und Fix-Session.

Nutzer fragte, ob der SIM-Modus sich nutzen lässt, um den Code selbst
(automatisiert, ohne Rig) zu testen. Neues Diagnose-Skript
`sim_defect_probe.py` gebaut: importiert `gui_main` headless unter
`QT_QPA_PLATFORM=offscreen` (kein Xvfb auf macOS verfügbar/nötig) und ruft
`scan_engine`-Funktionen direkt auf den echten, laufenden Objekten auf —
anders als das bestehende `offset_probe.py`, das `gui_main` bewusst nicht
fährt. Ground Truth kommt aus `state['ser']._pos`/`._optical_pos`
(nur in SIM verfügbar). Watchdog per `faulthandler.dump_traceback_later()`
pro Sektion eingebaut, nachdem ein erster Testlauf durch eine falsche
Zeitannahme (SIM-Bewegungsdauer skaliert mit realer Wall-Clock-Zeit) über
Minuten hing.

Alle drei Defekte in SIM reproduziert. Beim genaueren Nachrechnen von
Defect C (vollständig instrumentierter Trace der Backlash-Mechanik in
`sim_hardware.py`) stellte sich die anfängliche, scheinbar sehr saubere
Messung (`label - optical_truth = +0.082 nm`, exakt `SIM_BACKLASH_NM`) als
durch den separaten, bekannten P3-Bug "Scan überfährt End um einen Step"
verunreinigt heraus — wurde korrigiert und im Kopf-Docstring des Skripts
dokumentiert.

Fixes in `scan_engine.py`/`app_context.py`:
- Defect A (kein Re-Anchor nach Scan-Abbruch): behoben in `scan_worker`
  (Abbruch-Pfad und normaler Scan-Ende-Pfad) sowie in der identischen Lücke
  in `do_resume()`.
- Defect B (Desync nach Stop bei Go To): behoben — `goto_worker` berechnet
  die Wellenlänge nach einem Stop jetzt aus einem am Worker-Start erfassten
  Anker-Paar (Schritte, nm) statt aus dem stalen `entry_current`-Feld.
- Beim Verifizieren von B ein vierter, vorher unbekannter Bug gefunden:
  `stop_action()` wartete vor dem Re-Arm nur auf `state['is_scanning']`,
  das ein Go To nie setzt — ein Stop während eines Go To konnte
  `stop_flag` löschen, bevor `goto_worker`s eigene Warteschleife ihn je sah,
  wodurch der Stop erst nach dem vollen Move-Timeout (bis zu 900 s) wirkte,
  während die Konsole sofort "Recovered" meldete. Per Live-Thread-Stack-Dump
  bestätigt. Fix: neues `state['is_moving']`-Flag; ein zugehöriger Bug
  (fsm zeigte während des Auto-Recover-Retries fälschlich "IDLE") ebenfalls
  behoben.
- Defect C: Re-Anchor gegen die kompensations-bereinigte Encoder-Position
  nach jedem Go To und am Scan-Ende ergänzt (Muster wie `free_run_worker`s
  bestehende Rückführung), bewusst NICHT `comp` blind ins Label addiert.
  Analytisch und in SIM bestätigt: für exakt erreichte Moves ist das
  mathematisch ein No-Op — kann eine `slip_nm`-Fehlkalibrierung strukturell
  nicht beseitigen. Als KEIN vollständiger Fix dokumentiert; der
  eigentliche nächste Schritt ist eine Rig-Kalibrierprüfung
  (`backlash_cal.py`), kein weiterer Code.

Alle Fixes ausschließlich in SIM verifiziert, keine Rig-Session.
`sim_defect_probe.py` ins Repo übernommen. HANDOVER.md/BACKLOG.md/
CONTEXT.md vollständig überarbeitet, um diesen Stand widerzuspiegeln —
inklusive einer expliziten Korrektur der eigenen anfänglich überzogenen
Aussage zu Defect C.

## 2026-08-04

Branch `testing`: Commit, Push, Rebase auf reale `QT6-future`-Historie.

Neuer Branch `testing`, ein Commit mit allen P0-Fixes + Doku-Updates.
Vor dem Commit: German-Only-Sweep der Code-Diffs (eine deutsche Zitat-Zeile
in einem `scan_engine.py`-Kommentar gefunden und übersetzt), `.gitignore`
um `__pycache__/`, `*.pyc`, `.claude/` ergänzt, versehentlich durch
SIM-Testläufe verändertes `vrs41_settings.json` zurückgesetzt.

Push nach GitHub (`doctormord/Monochromator_GCA_2051`) scheiterte zunächst
an fehlenden Credentials (kein Cred-Helper, kein SSH-Key in der Sandbox).
Neuer SSH-Key für die Session generiert, vom Nutzer bei GitHub hinterlegt,
Host-Key-Fingerprint gegen die öffentlich bekannten GitHub-Fingerprints
plausibilisiert. Push erfolgreich.

Festgestellt: das lokale Repo war erst diese Woche frisch `git init`'t
worden und hatte keine gemeinsame Historie mit dem echten `QT6-future` auf
GitHub — ein PR hätte den kompletten Codebestand als Diff gezeigt statt nur
der Session-Änderungen. Auf Wunsch des Nutzers behoben: `origin/QT6-future`
gefetcht, `testing` darauf rebased. Zwei Konflikt-Arten dabei: fünf
Diagnose-Skripte als "add/add"-Konflikte geflaggt, aber laut SHA-256-Hash
byte-identisch (trivial aufgelöst); ein echter Konflikt in `scan_engine.py`,
weil `origin/QT6-future` älter ist als `stage1_diag.patch` — aufgelöst durch
Beibehalten der gepatchten Seite, gegen den Patch-Inhalt gegengeprüft.
Ein Zwischenfall: der Rebase brach zunächst an untracked `.DS_Store`-Dateien
im Arbeitsverzeichnis ab (kein Datenverlust, sauber mit `git rebase --abort`
zurückgesetzt, Dateien gelöscht, erneut versucht). Ergebnis: `testing` liegt
jetzt sauber 3 Commits vor `origin/QT6-future`, 0 dahinter. Mit
`--force-with-lease` erneut gepusht (sicher, da noch nichts anderes auf dem
zuvor gepushten Stand aufsetzte).

## 2026-08-04

CLAUDE.md-Doku-Struktur-Policy + HISTORY.md-Rekonstruktion.

Nutzer erweiterte CLAUDE.md um einen neuen Abschnitt "Dokumentationsstruktur":
vier Doku-Dateien mit klar getrennten Rollen (CONTEXT.md/HANDOVER.md werden
überschrieben, BACKLOG.md wird gepflegt, HISTORY.md — diese Datei — ist
append-only) plus die Regel, HISTORY.md nicht automatisch bei jedem Prompt
zu lesen, nur bei Bedarf.

HISTORY.md initial aus der kompletten bisherigen Projekthistorie erstellt.
Quelle: ein vom Nutzer bereitgestelltes ZIP mit sechs exportierten
Claude.ai-Chat-Transkripten (`claude-exports-Monochromator.zip`,
20.–24.7.2026) plus die in dieser Datei bereits dokumentierten späteren
Sessions. Die vier großen Chats (insgesamt ~9600 Zeilen Rohtranskript) über
vier parallele Recherche-Subagenten ausgewertet — pro Chat: Sessiongrenzen
aus Nachrichten-Zeitstempeln rekonstruiert (Lücken ≥1h als Sessionwechsel
gewertet, auch innerhalb eines einzigen fortlaufenden Chat-Threads), was
mehr real getrennte Arbeitssessions ergab als die im Repo vorhandenen
`HANDOFF_*.md`-Artefakte allein nahelegten (z.B. 8 statt 2 Blöcke im Chat
"Scan-Fehler und UI-Verbesserungen"). Zwei kleine Helium-Referenz-Chats
selbst gelesen (keine Delegation nötig). Rohtext von den Subagenten nicht
unverändert übernommen, sondern selbst zu den finalen Einträgen verdichtet
und in den bestehenden Dokumentationsstil überführt.
