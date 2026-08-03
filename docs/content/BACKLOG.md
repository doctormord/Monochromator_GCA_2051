# BACKLOG — VRS41

> Lebendes Dokument. Überschreiben, nicht anhäufen. Priorität: P0 = jetzt, P3 = später.
> Stand: 2026-08-04.
>
> **Diese Datei ist die Aufgabenliste.** Beobachtung, Ausschlussbegründungen,
> Hypothesentabelle, Messansatz und Testprotokoll zum P0-Thema stehen in
> `CONTEXT.md` — dort nachlesen, bevor an A/B/C gearbeitet wird.

## P0 — Wellenlängen-Offset ~0.07 nm zwischen Scan-Gruppen

Einziger aktiver Arbeitsstrang. **Kein Fix committet.**

Kurzfassung: Scans derselben Linie sind innerhalb einer Gruppe deckungsgleich,
zwischen Gruppen starr um ~0.07 nm verschoben. Avg-Fenster, Thermodrift,
Backlash-Streuung, RC-Zeitkonstanten und mechanische Relaxation sind als Ursache
ausgeschlossen (Begründungen in `CONTEXT.md`). Drei Defekte in `scan_engine.py`
sind gegen den Volltext verifiziert:

- [ ] **Defect A — Stepped Scan re-ankert nicht nach Abort.**
      `current_nm`/`entry_current` bleiben auf dem letzten geplotteten Punkt,
      während der Antrieb über die DEC-Rampe weiterläuft. `free_run_worker` hat
      die Rückführung, `scan_worker` nicht — der Fix wurde nur an einem von zwei
      Pfaden gemacht.
      ⚠️ **Nicht vor der Datenerhebung fixen** (Reihenfolge s. unten).

- [ ] **Defect B — Go To fährt nach Stop weiter, mit stalem Ursprung.**
      `goto_worker` rechnet das Delta erneut vom nie aktualisierten
      `entry_current` → zweite Bewegung, Overshoot ≈ bereits gefahrene Strecke.
      Das ist der gemeldete Dial-vs-Software-Bug, skaliert mit Move-Länge.
      **Beim Fix beachten:** `LR` ist laut Handbuch relativ zum letzten
      *kommandierten Sollwert*, nicht zur Istposition — ein aus `POS`
      gerechnetes `LR` ist nach einem harten Stop nicht automatisch korrekt.
      Braucht Rig-Verifikation.

- [ ] **Defect C — Kompensation im Ziel, nicht im Label.**
      Wahrscheinlichste Offset-Ursache. `reversal_compensation_steps()` geht in
      `target_abs`, aber nicht in `pos_nm` (Plot-/CSV-Label) → bei falscher
      Annahme oder falschem Slip-Wert verschiebt sich der ganze Scan starr um
      bis zu 1 Slip (0.080 nm). `last_move_direction` wird von **jedem** Move
      mutiert, ein Jog zwischen zwei Scans kippt es.

### Arbeitsschritte

- [x] `stage1_diag.patch` angewendet (fertig, verifiziert: 3 Hunks, additiv,
      `try/except: pass`, `read_position()`-Calls 10→10, `patch --dry-run`
      bestätigt sauberen, vollständig applizierten Stand)
- [ ] Session mit reproduziertem Offset mitschneiden
- [ ] `grep "\[DIAG\]"` gegen die Entscheidungstabelle in `CONTEXT.md` auswerten
- [ ] T1–T4 nach Testprotokoll (`CONTEXT.md`) fahren — Vorbedingungen beachten
- [ ] Fix-Commits für den Defekt schreiben, den die Daten tatsächlich belasten

**Reihenfolge — wichtig:** Defect A nicht vor der Datenerhebung fixen. Ein
Rückanker nach Abbruch ändert genau den Ursprung, dessen Vergiftung Hypothese H2
nachweisen soll. Erst messen, dann fixen.

**T5 aktuell nicht durchführbar:** das Konsolen-Log vom 29.07. (15:00–15:30)
wurde nicht mitgeschnitten.

### Artefakte

| Artefakt | Status |
|---|---|
| `stage1_diag.patch` | **angewendet** in `scan_engine.py` (3 Hunks, additiv, `try/except: pass`, `read_position()`-Calls 10→10, `patch --dry-run` bestätigt sauberen Stand) — wartet auf Session-Mitschnitt |
| `stage1_patch_guide.pdf` | Erklärung des Patches (EN) |
| `offset_report.pdf` | Diagnose + Hypothesen + T1–T5 (EN) |
| `offset_probe.py` | automatisierte Erhebung; Selftest grün (80 pm injiziert → 80.2 pm in `nm_app`, 0.2 pm in `nm_enc`); Hardware-Hälfte unverifiziert |

`offset_probe.py` fährt `gui_main`/`scan_engine` **nicht** (Qt-Widgets
verschweißt; Stubs kämen sonst in den Messpfad). Es beweist Physik und
Arithmetik, nicht das Verhalten von `gui_main`. Defect A/B brauchen T3/T4 + Log.

---

## P1 — Rig-Verifikation (offen)

- [ ] **POS-Spikes/Zickzack (~587 nm Sprünge).** Outlier-Filter fängt die Folge
      ab, die Ursache ist offen. Motion Manager ausgeschlossen (nie
      installiert), Concurrency in der eigenen App ausgeschlossen (ein
      gemeinsamer Lock, per Grep verifiziert). `drain_stale_serial()` +
      explizites `ANSW0` beim Connect als Härtung ergänzt.
      `read_position_raw_or_none()` liefert den Rohstring mit, der Filter loggt
      ihn bei jedem Vorfall → **nächster Vorfall ist forensisch auswertbar**.
      Anmerkung: der Serial-Delay-Fix aus 23b hat die ANSW-Semantik geändert;
      ob das die Spike-Rate beeinflusst, ist unbeobachtet.
- [ ] **`EN` in `recover_after_stop()`** — quittiert das einen DEVIATION-Fault
      sauber, oder braucht der Controller ein explizites Fault-Reset? Nach
      einem Fault einen normalen Goto/Scan schicken und beobachten.
- [ ] **Serial-Fix am Rig gegenprüfen** (aus 23b, noch offen): Delay wirklich
      weg? Fahren die Moves korrekt? `LR` und `M` gehen jetzt unmittelbar
      hintereinander raus, vorher lagen zufällig ~1.2 s dazwischen. Falls der
      Antrieb das nicht sauber annimmt, ist die Stellschraube `WRITE_SETTLE_S`
      (aktuell 0.01 s) — **nicht** das Zurückdrehen des Fixes.
- [ ] **`[SLIP]`-Meldung nur bei echtem Reversal** — sollte stimmen,
      Rig-Gegenprüfung offen. Fällt bei P0/T1 ohnehin ab.
- [ ] **„Position nach Referenzfahrt manchmal nicht geupdated"** — der
      Update-Code läuft nur im `if reached`-Zweig; bei abgebrochener Fahrt
      bewusst kein Update. Ob es bei ERFOLGREICHER Fahrt auftritt, war ohne Log
      nicht reproduzierbar. NICHT spekulativ gefixt. Beim nächsten Auftreten:
      steht im Log `[REF] Reference run complete` oder `interrupted`?
- [ ] **Free Run + Offset-Kalibrierung + Crosshair** komplett am Rig
      durchspielen.
- [ ] **Zweite bekannte Linie nach Offset-Kalibrierung gegenprüfen** →
      entscheidet, ob eine Zwei-Punkt-Skalierungskalibrierung nötig ist
      (aktuell bewusst nicht gebaut).
- [ ] **Absoluter Kalibrieroffset.** Peaks im Screenshot bei 587.75–587.87 nm,
      He-D3 = 587.5618 nm (Luft) → ~0.19–0.31 nm zu hoch. Eigenes Thema,
      aber relevant, sobald Cluster-Lagen als „richtig/falsch" bewertet werden.
      Referenzlinien in `CONTEXT.md`.

## P2 — bewusst erhaltene Alt-Bugs (nur mit Rücksprache)

- [ ] `scan_engine._maybe_prompt_save_all_scans_after_queue`: `graph_data`
      undefiniert → Queue-Ende-Speicherprompt faktisch inaktiv. Entweder auf
      `plot_mgr`-basierte Prüfung umstellen oder Prompt bewusst entfernen.
- [ ] `motion._pos_wait_impl_fallback`: `last_ost`/`ost_baseline`/
      `ost_debounce` uninitialisiert → OST-Guard läuft faktisch nie (NameError
      vom `except` verschluckt). POS-Toleranz-Pfad ist nicht betroffen. NUR mit
      Rig-Test aktivieren — Aktivierung ändert Laufzeitverhalten.
- [ ] `device_constants.POS_TOL_STEPS_RESUME` ist definiert, wird aber nirgends
      gelesen. Als tot markiert, nicht gelöscht.
- [ ] **Endschalter-Guard der App ist schlafend** und seine Logik ist gegenüber
      der gemessenen Active-Low-Polarität invertiert. Bewusst deaktiviert statt
      ungetestet aktiviert. Die Endschalter werden vom Antrieb selbst
      erzwungen; die App merkt eine Blockade nur indirekt über einen
      Deviation-Fault.
- [ ] **`HD = 00101`: beide Endschalter blockieren dieselbe Fahrtrichtung.**
      Für ein Paar Endlagenschalter heißt das, dass nur ein Ende wirklich
      geschützt ist. Ändern hieße in den Antrieb schreiben — nicht gemacht.

## P3 — später / optional

- [ ] Native Qt-Signals in `scan_engine` statt Adapter-Brücke (`refs`/
      `ui_hook`) — würde die Tk-kompatible Adapterschicht obsolet machen.
      Größerer, sauberer Schnitt, kein akuter Bedarf.
- [ ] Kalibrier-Dwell auf ms umstellen (aktuell Sekunden, inkonsistent zum
      Haupt-Scan-Feld).
- [ ] STATUS_FAULT_BIT/STATUS_TR_BIT-Vierfach-Dopplung weiter konsolidieren
      (Konstanten sind zusammengeführt, die drei Audit-Funktionen bleiben
      bewusst getrennt) — nur mit Rücksprache.
- [ ] `backlash_cal`: `seat`-Over-Travel und Peak-Schwelle im Dialog
      konfigurierbar machen, falls am Rig nötig.
- [ ] GUI-Feinschliff: weitere Readouts (Last V, Rate), Theme-Toggle.
- [ ] **Scan überfährt End um einen Step.** Der letzte Punkt wird bei `e_nm`
      gemessen, danach wird noch ein Schritt gefahren, erst dann bricht die
      Schleife ab → `entry_current` steht nach jedem Scan auf `End + Step`.
      Konsistent, erzeugt also keinen Gruppenversatz, aber eine
      Buchhaltungs-Ungenauigkeit.
- [ ] **CSV-Export enthält nur `index, wavelength_nm, voltage_V`** — kein
      `pos_steps`, kein `last_move_direction`. Das Encoder-Residuum ist aus der
      GUI-CSV nicht rekonstruierbar. Nachrüsten hieße `PlotManager`-
      Speicherstruktur + `export_scan_csv_by_id` + Format ändern; im Rahmen der
      Offset-Diagnose bewusst nicht gemacht (Stufe 1 = Logging reicht).
- [ ] **`POS_TOL_STEPS` skaliert nicht mit der Schrittweite.** Bei 0.0001 nm
      (36 Schritte) wäre 90 das 2.5-fache des Schritts. Vor Scans feiner als
      0.001 nm `step_resolution_test.py` laufen lassen.
- [ ] **Free-Run-Punktabstand ist ungleichmäßig** und hängt von Geschwindigkeit
      und Link-Performance ab. Bekannt und akzeptiert.
- [ ] **`daq.acquire_measurement`: dokumentierter Semantikwechsel** der
      Fraction-Rechnung (`window = (dwell − presettle) × frac` statt alter
      Deckelung) → Scans vor/nach dem Wechsel im `time`-Modus nicht direkt
      vergleichbar. Timebase/Pre-settle immer aus dem CSV-Header nehmen, nie aus
      der aktuellen Panel-Anzeige.

## Doku-Korrekturen (gefunden, noch nicht eingepflegt)

- [ ] **`MANUAL.md`, Known limits, ist an einer Stelle veraltet:** dort steht,
      `POS_TOL_STEPS` sei „a fixed 0.01 nm, which equals a whole step at
      0.01 nm". Tatsächlich ist der Wert **90 Schritte ≈ 0.25 pm**, laut
      Kommentar in `device_constants.py` 40× feiner als ein 0.01-nm-Schritt.
      Der Manual-Absatz beschreibt einen früheren Wert. Beim nächsten
      MANUAL-Update korrigieren (README und PDF werden daraus erzeugt).
- [ ] Ältere HANDOVER-Stände führen `gui_main_tk.py` als Modul 12; die Datei
      liegt nicht mehr im Repo. In `HANDOVER.md` bereits korrigiert.

## Erledigt

### Offset-Diagnose (Vorarbeit)
- Avg-Fenster, Thermodrift, Backlash-Streuung, RC-Zeitkonstanten und
  mechanische Relaxation als Ursache ausgeschlossen (Statiktest war der
  entscheidende Diskriminator).
- Drei Defekte A/B/C am Volltext lokalisiert und belegt.
- `POS` als frei laufender Absolutzähler nachgewiesen (kein `HO` im
  Protokoll-Layer) → scan-übergreifend vergleichbarer zweiter Frame.
- Diagnose-Patch + zwei PDFs + Probe-Tool erstellt.

### Serial / Timing
- `[RESYNC]`-Spam bei ST/HP0/V0/EN gelöst (`NO_REPLY_COMMANDS`,
  `_is_no_reply_cmd()`, `MOTOR_ANSW_MODE`). `init_motor()` 8.4 s → 0.07 s.
  Im aktuellen Code verifiziert vorhanden. Rig-Gegenprüfung steht noch aus
  (P1).
- `POLL_TIMEOUT` 0.10 → 0.25 s.

### Fundament
- Monolith in Module aufgeteilt, GUI von Tkinter auf Qt6 gehoben
  (`scan_engine`/`motion`/`daq`/`protocol_faulhaber` unverändert, Adapter-
  Brücke + GUI-Thread-Marshalling).
- SIM-Modus (`sim_hardware.py`) inkl. modelliertem Backlash.
- Backlash-Kalibrierung (`backlash_cal.py` + `CalibrationDialog`).
- Settings-Persistenz (`app_config.py`); Kalibrier-Dialog-Defaults aus
  `gui_main.py` nach `device_constants.py`/`app_config.py` gezogen.
- Motion-Ramp `AC=50/DEC=100/SP=10000` (vorher `DEC` auf Max 30000).
- `REFERENCE_DIRECTION` −1 → 1.
- Free Run: Overshoot-Guard, Positions-Rückführung, POS-Outlier-Filter mit
  Rohstring-Logging.
- `motion.steps_to_delta_nm()` Funktionskörper wiederhergestellt.
- `entry_current`-Desync (backlash_cal / reference_run).
- Scan-Button-Lock nach normalem Scan-Ende.
- Step-Feld-Bug (`_EntryAdapter`-Shadow) und Crosshair-nach-Clear-Bug.
- Dezimaltrennzeichen Komma/Punkt (`_parse_float_loose`).
- Offset-Kalibrierung, Plot-Crosshair, Notstop-Button, Connect/Disconnect-
  Toggle, SCAN-Layout, „Clear Plot" in die Scope-Toolbar, CALIBRATION-Sektion
  einsortiert.
- Post-Connect-Workflow (Referenzfahrt-Abfrage → Anker-Abfrage).
- Diagnose-Tools: `read_faulhaber_config.py`, `probe_move_timing.py`,
  `tune_ramp.py`, `idle_poll_test.py`, `step_resolution_test.py`.
- `MANUAL.md` als Single Source (README + PDF werden daraus erzeugt).
- Alles Code-seitig auf Englisch, per Grep gegengeprüft.
- `SCAN_EPSILON_NM` vereinheitlicht; DAQ-Stillfehler werden geloggt
  (rate-limited) statt still `0.0` zurückzugeben.

## Referenz

`monochromator_python_code_vrs41.py` (ex-Monolith) ist NICHT Teil des
ausgelieferten Pakets, bleibt nur als historische Referenz im Projekt.
Der ältere Monolith-Track (N_SLIP-Widerspruch 1.1618 vs. 1.618, neun
duplizierte Funktionsdefinitionen, `except Exception: pass`-Stellen) ist durch
den Modul-Split abgelöst und wird **nicht weiterverfolgt**.
