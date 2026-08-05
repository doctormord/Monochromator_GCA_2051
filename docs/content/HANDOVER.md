# HANDOVER — VRS41 Monochromator/Scan-Controller

> Lebendes Dokument. Überschreiben, nicht anhäufen. Prosa DE, Code/Bezeichner EN.
> Stand: 2026-08-05. Konsolidiert aus allen HANDOVER/BACKLOG/HANDOFF-Ständen bis
> HANDOFF_2026-07-23b + Offset-Diagnose-Session 2026-07-29/31 + Diagnose-Patch
> angewendet 2026-08-04 + SIM-Diagnose-/Fix-Session 2026-08-04 (Defect A/B
> behoben, Defect C entschärft, Stop/GoTo-Race, vier Folgefehler aus dem
> Selbst-Review, Slack-first-Modell, Begriffsvereinheitlichung
> Slip → Backlash) + Doku-/Kommentar-Audit 2026-08-05 (reine Sprachqualität,
> keine Verhaltensänderung — s. unten und `HISTORY.md`). **Fachlich alles
> weiterhin NUR SIM-verifiziert, keine Rig-Session** — s. „AKTUELL" unten.
> Chronologie aller Sessions: `HISTORY.md`.

## Rollenverteilung der Dokumente

| Datei | Inhalt |
|---|---|
| `CLAUDE.md` | **Doku-/Kommentar-Policy** (verbindlich, jede Session) + Kurzeinstieg |
| `HANDOVER.md` (hier) | Architektur, rig-bestätigter Stand, Geräte-Eigenheiten (wird überschrieben) |
| `BACKLOG.md` | Offene Punkte nach Priorität, Defect-Checkliste (wird gepflegt) |
| `CONTEXT.md` | Technischer Tiefgang: Skalen, Positions-Wahrheiten, Offset-Diagnose (wird überschrieben) |
| `HISTORY.md` | Chronologisches Log, append-only — nicht bei jedem Prompt lesen, nur bei Bedarf (s. CLAUDE.md) |

**Die verbindliche Doku-/Kommentar-Policy steht jetzt in `CLAUDE.md`** (Punkte
1–8, wortgleich übernommen). Sie wurde dorthin verschoben, weil Claude Code
`CLAUDE.md` automatisch bei jedem Sessionstart lädt — damit greift die Regel
garantiert, auch wenn dieses Dokument nicht gelesen wird. Nicht gekürzt,
nur verlagert.

## Repo

`doctormord/Monochromator_GCA_2051`, Branch `QT6-future`, public,
Fork von `sk-HTW/monochromator_python_code`.

## Architektur — aktueller Stand

Modul-Split abgeschlossen. GUI ist **Qt6** (PyQt6 + pyqtgraph). Einstiegspunkt
`main.py`. Der ex-Monolith `monochromator_python_code_vrs41.py` liegt weiterhin
im Projekt, ist aber NICHT Teil des ausgelieferten Pakets (historische Referenz).

**Module (Abhängigkeitsrichtung von unten nach oben):**

1. **`device_constants.py`** — Kalibrierung (`STEPS_PER_NM=361765`,
   `INVERT_DIRECTION=True`), Timing/Polling (`POLL_TIMEOUT=0.25`,
   `WRITE_SETTLE_S=0.01`), Backlash-Tunables (`REFERENCE_MOVE_NM`,
   `REFERENCE_DIRECTION=1`), Averaging-Defaults, Kalibrier-Dialog-Defaults
   (`CAL_*_DEFAULT`), Statusword/OST-Bits, Motion-Ramp (`USE_MOTION_RAMP=True`,
   `RAMP_AC=50`, `RAMP_DEC=100`, `RAMP_SP=10000`), Serial-Semantik
   (`MOTOR_ANSW_MODE=0`, `NO_REPLY_COMMANDS`), Positionstoleranz
   (`POS_TOL_STEPS=90`, `POS_STABLE_COUNT=3`).
2. **`app_context.py`** — Nabel: `state`-Dict, `log`+Sink, `set_fsm`,
   ui-Registry (`register_var`/`register_hook`/`ui_get`/`ui_hook`), `refs`.
   Kein Logik-Modul importiert die GUI-Bibliothek.
3. **`app_config.py`** — `DEFAULT_CONFIG` + JSON-Singleton
   (`vrs41_settings.json`). Persistiert fast jedes Eingabefeld. Fehlende/
   korrupte Datei → Defaults, nie ein Crash.
4. **`protocol_faulhaber.py`** — Serial-Layer (ASCII + binärer SDO-Client).
   `configure_ramp(ac,dec,sp)`; `init_motor()` wendet die RAMP_*-Konstanten an.
   `_is_no_reply_cmd()` + Fire-and-Forget-Zweig in `send_cmd` (s.u.).
   Kein SAVE/EEPSAV automatisch (EEPROM-Schreibzyklen begrenzt).
5. **`daq.py`** — `read_pmt_voltage` (NI-DAQ oder wellenlängen-geformter
   Simulator), `read_pmt_voltage_avg`, `acquire_measurement` — **single source
   of truth** für die Punkt-Erfassung (Scan, Resume, Free Run).
6. **`motion.py`** — nm↔Schritte (`steps_for_delta_nm`, `steps_to_delta_nm`),
   `wait_until_position`, `timeout_for_target_steps`, Backlash-Kompensation
   (`reversal_compensation_steps`), `reference_run_action`,
   `set_current_wavelength`.
7. **`scan_engine.py`** — Goto/Scan/Pause/Resume/Jog/Queue/CSV, Free-Run-Scan.
   Bedient GUI-Objekte duck-typed über `refs[...]`.
8. **`gui_main.py`** — Qt6-GUI im PS-2600A-Dark-Look. Links Scope/Console-Tabs
   (pyqtgraph) mit Crosshair + Toolbar, rechts scrollbares Panel mit
   Collapsible-Sektionen (Connection/Move/Scan/Averaging/Queue/Backlash/
   Calibration/DAQ/Export). Qt-Widgets in Tk-kompatible Adapter
   (`_EntryAdapter`, `_EntryAdapterMsToSec`, `_VarAdapter`, `_ButtonAdapter`,
   `_ListboxAdapter`, `_MessageboxShim`) + GUI-Thread-Marshalling (`_Invoker`).
   `CalibrationDialog`, `PlotManager`, `_parse_float_loose` (Komma/Punkt).
9. **`main.py`** — Entry Point (`gui_main.run()` → `app.exec()`).
10. **`sim_hardware.py`** — `FakeFaulhaber`: Port `"SIM"` injiziert einen
    virtuellen Antrieb inkl. modelliertem Backlash.
11. **`backlash_cal.py`** — Auto-Slip-Kalibrierung, vom `CalibrationDialog`
    über `run_calibration()` gestartet.

**Eigenständige Diagnose-Tools** (kein App-Import, schreiben nie SAVE/EEPSAV):
`read_faulhaber_config.py` (Controller-Zustand read-only, `--watch` für
Live-Endschalter), `idle_poll_test.py` (Serial-Link vs. bewegungskorreliert),
`step_resolution_test.py` (feinste nutzbare Schrittweite + Ankunftstoleranz),
`probe_move_timing.py` (reale Move-/Pollrate), `tune_ramp.py` (AC/DEC/SP),
`offset_probe.py` (P0-Offset-Erhebung, reimplementiert die Move-/Scan-Logik
transparent aus denselben Primitiven statt `gui_main` zu fahren), `sim_defect_probe.py`
(neu 2026-08-04; fährt `gui_main`/`scan_engine` DIREKT und headless gegen
`sim_hardware`, kein App-Import-Verzicht wie bei `offset_probe.py` — Details
in `CONTEXT.md` P0).

**Dokumente:** `MANUAL.md` (vollständiges Handbuch, Single Source; README und
PDF werden daraus erzeugt), `README.md`, `CLAUDE.md`, `HANDOVER.md`,
`BACKLOG.md`, `CONTEXT.md`, `TESTPLAN.md`.

**Entkopplungs-Mechanismus:** Logik-Module lesen GUI-Werte über `ui_get`, lösen
GUI-Aktionen über `ui_hook` aus, `scan_engine` bedient konkrete Widgets über
`refs[...]`. Kein Logik-Modul kennt Qt/Tkinter direkt.

**Korrektur (2026-08-04):** ein früherer HANDOVER-Stand behauptete,
`gui_main_tk.py` (alte Tkinter-GUI) liege nicht mehr im Repo. Das ist
**falsch** — die Datei liegt weiterhin im Projekt (~25 kB, Stand 2026-07-24).
Sie wird von nichts importiert und ist durch `gui_main.py` (Qt6) vollständig
abgelöst, hat aber denselben Status wie der ex-Monolith: historische Referenz,
nicht Teil des ausgelieferten Pakets. Nicht gelöscht (Doku-Policy: Altlasten
markieren statt stillschweigend entfernen).

## Rig-bestätigter Stand (nicht mehr offen)

- **`REFERENCE_DIRECTION = 1`** (war `-1`).
- **Motion-Ramp aktiv:** `AC=50, DEC=100, SP=10000`. Vorher war `DEC` nie
  gesetzt und lief auf dem Geräte-Maximum (30000) → praktisch keine Bremsrampe.
  Mit `tune_ramp.py` gemessen, live getestet, bestätigt.
- **Backlash/Slip:** ca. 0.09–0.20 nm.
- **Scan-Button-Lock** nach normalem Scan-Ende behoben (`scan_worker` rief den
  Reset-Hook nie auf).
- **`entry_current`-Desync** behoben (`backlash_cal`/`reference_run_action`
  aktualisierten das GUI-Feld nicht, das `scan_engine` als Delta-Quelle nutzt).
- **`motion.steps_to_delta_nm()`** hatte keinen Funktionskörper → `None` →
  `float+NoneType`-Crash. Gefixt.
- **Free Run:** Overshoot-Guard + Positions-Rückführung nach Stop/Fault +
  POS-Outlier-Filter mit Rohstring-Logging.
- **Serial-Delay-Regression behoben** (der große Fix aus 23b): `ANSW0` schaltet
  den stillen Modus, in dem SET-Kommandos gar nicht quittieren — `send_cmd`
  wartete trotzdem, ~1.2 s Totzeit pro Kommando. Jetzt `NO_REPLY_COMMANDS` +
  `_is_no_reply_cmd()`: diese Kommandos werden fire-and-forget geschrieben.
  Abfragen (POS/OST/G*) bleiben auf dem Schreib-Lese-Pfad. `init_motor()`
  8.4 s → 0.07 s. **Im aktuellen Code verifiziert vorhanden.**
  (Rig-Gegenprüfung steht noch aus → `BACKLOG.md` P1.)
- **`POLL_TIMEOUT`** von 0.10 auf 0.25 s angehoben.
- **Dezimaltrennzeichen:** Post-Connect-Dialog und CALIBRATION-Feld akzeptieren
  Komma und Punkt (`_parse_float_loose` statt locale-gebundenem `getDouble`).
- **Step-Feld-Bug** (`_EntryAdapter`-Shadow leer bei vorbelegten Feldern) und
  **Crosshair-nach-Clear-Bug** behoben.
- **DEVIATION-Fault nach hartem `ST`** bei schneller/langer Bewegung ist Physik
  (keine Bremsrampe bei `ST`), kein Bug.
- **Endschalter-Konfiguration am Rig ausgemessen:** OST 0x0500 = beide frei,
  0x0400 = Input 1 gedrückt. `HD = 00101` → beide Schalter blockieren dieselbe
  Fahrtrichtung.
- Layout-Bugs (Panel-Breite, Notstop-Button) behoben.
- Alles Code-seitig auf Englisch (Docs bleiben Deutsch). **Korrektur
  2026-08-05:** der frühere Grep-Sweep (23.07.) war unvollständig — mehrere
  einzelne deutsche Fachbegriffe ohne Anführungszeichen/Übersetzung waren
  ihm entgangen, weil sie in keiner damaligen Wortliste standen (u.a.
  `Kommandoset`, `Verdrahtung` als Abschnitts-Header, `Endschalter`
  unquotiert, ein echtes Kommentar-Paar in `gui_main.py`). Mit breiterer
  Wortliste gefunden und übersetzt; wörtliche Zitate aus dem deutschen
  Handbuch (`Gehäusetemperatur`, `Zustand Eingang`, `Antwort`) bleiben mit
  Übersetzung daneben stehen, zur Rückverfolgbarkeit. Details: `HISTORY.md`
  2026-08-05.

## Zwei Eigenschaften, die man kennen muss

**Kein absoluter Positionssensor.** Jede angezeigte Wellenlänge rechnet sich aus
einem Anker, den der Nutzer setzt. Neustart, Handbewegung oder verlorener
Schritt entwertet ihn. Nach solchen Ereignissen mit *Known λ here* neu verankern.

**`POS` ist ein frei laufender Absolutzähler.** `init_motor()` sendet
`EN`/`V0`/`ANSW`/Ramp, `recover_after_stop()` nur `ST`/`EN`/`V0`. **Kein `HO`
irgendwo im Protokoll-Layer.** `POS` läuft damit über die ganze Session bis zum
Power-Cycle durch — Rohwerte sind ohne Anker-Arithmetik scan-übergreifend
vergleichbar. Grundlage der laufenden Offset-Diagnose.

**Caveat dazu, aus dem `POS_TOL_STEPS`-Kommentar:** `POS` ist der **Encoder**.
Er beweist, dass der Servo positioniert — nicht, dass sich das Gitter bewegt
hat. Bei sehr feinen Schritten kann Stiction die Optik stehen lassen, während
der Encoder das Ziel erreicht. Diese Frage braucht einen **optischen** Test.

**`LR` ist relativ zum letzten kommandierten Sollwert, nicht zur Istposition**
(laut Antriebshandbuch). Nach einem harten Stop können der interne Sollwert des
Antriebs und `POS` auseinanderlaufen. Relevant für Defect B — dessen
Buchhaltungsseite ist behoben (s. „AKTUELL" unten), aber ob ein aus `POS`
berechnetes `LR` nach einem harten Stop auf ECHTER Hardware wirklich richtig
ist, bildet `sim_hardware` nicht nach. Bleibt eine offene Rig-Frage.

## SIM-bestätigter Stand, Rig-Verifikation offen (2026-08-04)

Zur Abgrenzung von „Rig-bestätigter Stand" oben: alles hier ist **nur** unter
`QT_QPA_PLATFORM=offscreen` gegen `sim_hardware.FakeFaulhaber` verifiziert
(`sim_defect_probe.py`), nicht am Gerät. SIM beweist Codepfade und
Bookkeeping-Arithmetik, nicht reales Motor-/Serial-Timing oder die
`LR`-Sollwert-Feinheit nach einem harten Stop (s. oben).

- **Defect A behoben:** `scan_worker` re-ankert `current_nm`/`entry_current`
  jetzt sowohl beim Abbruch (`break` nach fehlgeschlagenem
  `wait_until_position()`) als auch am normalen Scan-Ende gegen die reale
  Encoder-Position — analog zu `free_run_worker`s bestehender Rückführung.
  Dieselbe Lücke in `do_resume()` ebenfalls geschlossen.
- **Defect B behoben:** `goto_worker` berechnet die Wellenlänge nach einem
  Stop jetzt aus der frisch gelesenen Encoderposition (über ein am
  Worker-Start erfasstes Anker-Paar), nicht mehr aus dem nie aktualisierten
  `entry_current`-Feld.
- **Vierter Fund, behoben:** Stop während eines GoTo re-armte die Drive
  (`recover_after_stop()`, löscht `stop_flag`) oft BEVOR `goto_worker`s
  eigene Warteschleife den Stop überhaupt bemerkte — weil `stop_action()`
  bisher nur auf `state['is_scanning']` wartete, was ein GoTo nie setzt.
  Der GoTo lief dann im Hintergrund bis zu seinem vollen Move-Timeout weiter
  (bis zu 900 s), während die Konsole bereits „Recovered" meldete. Neues
  `state['is_moving']`-Flag schließt die Lücke; ein zugehöriger `fsm`-Bug
  (zeigte „IDLE" während eines laufenden Auto-Recover-Retries) ebenfalls
  behoben.
- **Nachtrag aus dem Selbst-Review derselben Session (vier Folgefehler,
  alle behoben, SIM-verifiziert):** (A) `read_position()` liefert bei
  Lesefehler still `0` — die neuen Re-Anchor-Stellen schrieben das ungeprüft
  in den Positions-Ursprung; jetzt über `_encoder_nm_or_none()` /
  `read_position_or_none()` abgesichert. (B) Ein Stop während eines Go To
  wurde vom Auto-Recover-Retry ignoriert, der trotzdem ans Originalziel fuhr
  — jetzt wird `stop_flag` vor dem Re-Arm ausgewertet und bei Nutzer-Stop
  nicht mehr nachgefahren. (C) Die Referenzfahrt setzte kein Busy-Flag und
  lief damit weiter in die alte Stop-Race — setzt jetzt `is_moving`. (D) Go To
  hatte keinen Reentrancy-Schutz (Doppelklick/Jog-Spam startete zwei Worker)
  — Guard ergänzt, Flag wird auf dem GUI-Thread vor dem Thread-Start gesetzt.
  Details in `BACKLOG.md` P0, Abschnitt „Nachtrag".
- **Defect C nur teilweise entschärft, kein Fix.** Re-Anchor gegen die
  kompensations-bereinigte Encoder-Position nach GoTo/Scan-Ende ergänzt
  (Muster wie `free_run_worker`), bewusst NICHT `comp` blind ins Label
  addiert. Für exakt erreichte Moves ist das mathematisch ein No-Op — kann
  eine `slip_nm`-Fehlkalibrierung strukturell nicht beseitigen. Der
  eigentliche nächste Schritt ist eine Rig-Kalibrierprüfung
  (`backlash_cal.py`), kein weiterer Code. Details/Herleitung in `CONTEXT.md`.

## AKTUELL: Wellenlängen-Offset ~0.07 nm zwischen Scan-Gruppen

Diagnose abgeschlossen, drei Defekte plus ein vierter Fund SIM-verifiziert
gefixt (s. Abschnitt oben) — **Rig-Session steht noch aus.** Details,
Hypothesen und Testprotokoll in `CONTEXT.md`; Checkliste in `BACKLOG.md` (P0).

Kurzfassung des Ausgangsbefunds: Scans derselben Linie sind innerhalb einer
Gruppe deckungsgleich, zwischen Gruppen starr um ~0.07 nm verschoben (Slip ≈
0.09 nm laut Settings). Avg-Fenster, Thermodrift, Backlash-Streuung,
RC-Zeitkonstanten und mechanische Relaxation sind ausgeschlossen.

Diagnose-Patch `stage1_diag.patch` ist weiterhin **angewendet** (in
`scan_engine.py` verifiziert: 3 Hunks, additiv, `try/except: pass`,
`read_position()`-Calls 10→10 — die Position wird nur früher gelesen und
wiederverwendet, nicht doppelt abgefragt — `patch --dry-run` bestätigt
sauberen, vollständig applizierten Stand) und bleibt für die Rig-Session
nützlich.

## Nächster sinnvoller Schritt

Rig-Session: T3/T4 aus `CONTEXT.md` fahren (sollten nach dem A/B/Race-Fix
keinen bleibenden Offset mehr zeigen bzw. sofort statt erst nach vollem
Timeout abbrechen), `slip_nm` gegen den echten Backlash verifizieren
(`backlash_cal.py` — das ist der Schritt, der Defect C tatsächlich schließt,
kein weiterer Code), dann TESTPLAN.md Section 3c (10 gequeute Scans) als
Abschlussbestätigung. Falls T3/T4 am Rig doch noch einen Offset zeigen: der
SIM-Fix war unvollständig, zurück zu `CONTEXT.md`. Alt (weiterhin gültig als
Referenz für die Auswertung, falls die Rig-Session neue `[DIAG]`-Daten
braucht): `[DIAG]`-Zeilen gegen die Entscheidungstabelle in `CONTEXT.md`
auswerten. Danach ggf. weitere Fixes schreiben — für den Defekt,
den die Daten tatsächlich belasten.
