# BACKLOG — VRS41

> Lebendes Dokument. Überschreiben, nicht anhäufen. Priorität: P0 = jetzt, P3 = später.
> Stand: 2026-08-04. SIM-Diagnose- und Fix-Session eingearbeitet.
>
> **Diese Datei ist die Aufgabenliste.** Beobachtung, Ausschlussbegründungen,
> Hypothesentabelle, Messansatz und Testprotokoll zum P0-Thema stehen in
> `CONTEXT.md` — dort nachlesen, bevor an A/B/C gearbeitet wird.

## P0 — Wellenlängen-Offset ~0.07 nm zwischen Scan-Gruppen

Defect A und B sind code-seitig behoben und **in SIM verifiziert**
(`sim_defect_probe.py`) — kein Rig-Test bisher. Defect C ist mit einem
Re-Anchor entschärft, aber strukturell kein vollständiger Fix (Begründung in
`CONTEXT.md`). Ein vierter, vorher unbekannter Bug (Stop/GoTo-Race) wurde beim
Verifizieren von B gefunden und ebenfalls behoben. **Nächster Schritt ist die
Rig-Session**, nicht mehr die Ursachendiagnose.

Kurzfassung des Ausgangsbefunds: Scans derselben Linie sind innerhalb einer
Gruppe deckungsgleich, zwischen Gruppen starr um ~0.07 nm verschoben.
Avg-Fenster, Thermodrift, Backlash-Streuung, RC-Zeitkonstanten und
mechanische Relaxation sind als Ursache ausgeschlossen (Begründungen in
`CONTEXT.md`).

- [x] **Defect A — Stepped Scan re-ankert nicht nach Abort. Behoben (SIM).**
      `current_nm`/`entry_current` blieben auf dem letzten geplotteten Punkt,
      während der Antrieb über die DEC-Rampe weiterlief. `free_run_worker` hatte
      die Rückführung, `scan_worker` nicht. Jetzt ergänzt: Re-Anchor an der
      `break`-Stelle (nur bestätigter Slack ausgeschlossen, s. Code-Kommentar)
      sowie am normalen Scan-Ende (`while`/`else`, voller Slack ausgeschlossen).
      Dieselbe Lücke gab es identisch in `do_resume()` — dort ebenfalls
      gefixt. SIM: `label - true_nm nach Stop = +0.00000`.
      Rig-Verifikation offen (TESTPLAN.md 3c / T3 aus CONTEXT.md).

- [x] **Defect B — Go To fährt nach Stop weiter, mit stalem Ursprung. Behoben (SIM).**
      `goto_worker` rechnete das Delta erneut vom nie aktualisierten
      `entry_current` → zweite Bewegung, Overshoot ≈ bereits gefahrene Strecke.
      Fix: Anker-Paar (Schritte, nm) am Worker-Start erfasst, im
      Interrupted-Zweig daraus die wahre aktuelle Wellenlänge berechnet (nicht
      aus dem stalen Feld), Label sofort aktualisiert. SIM (8 nm Move, Stop
      bei ~33 %): Label korrekt `523.000 → 525.720 nm`, Retry erreicht sauber
      das Ziel.
      **Weiterhin offen:** `LR` ist laut Handbuch relativ zum letzten
      *kommandierten Sollwert*, nicht zur Istposition — `sim_hardware`
      bildet diese Feinheit nicht nach. Nur ein Rig-Test (T4) kann zeigen, ob
      ein aus `POS` berechnetes `LR` nach einem harten Stop wirklich korrekt
      ist, oder ob dafür ein Absolut-Move (`LA`) nötig wäre.

- [x] **Vierter Fund — Stop/GoTo-Race. Behoben (SIM), beim Verifizieren von B entdeckt.**
      `stop_action()` wartete vor dem Re-Arm nur auf `state['is_scanning']`
      (setzt nur Scan/Free-Run, nie GoTo) → ein Stop während eines GoTo
      löschte `stop_flag` fast sofort, oft bevor `goto_worker`s eigene
      `wait_until_position()`-Poll ihn je sah. Folge: die Konsole zeigte
      sofort "Recovered after STOP; ready.", während der GoTo bis zu
      `MAX_MOVE_TIMEOUT` (900 s) im Hintergrund weiterlief. Per
      Live-Thread-Stack-Dump in SIM verifiziert. Fix: neues
      `state['is_moving']`-Flag (`app_context.py`), `stop_action()` wartet
      jetzt auf `is_scanning` ODER `is_moving`. Zusätzlich behoben: `fsm`
      zeigte während `goto_worker`s Auto-Recover-Retry fälschlich "IDLE"
      (weil `recover_after_stop()` es unbedingt zurücksetzt) — jetzt wird
      `fsm` nach dem Retry-Start wieder auf "MOVING" gesetzt.
      Rig-Verifikation offen — genau wie bei B ist unklar, ob echtes
      Motor-/Serial-Timing dieselbe Race-Charakteristik zeigt wie SIM.

- [ ] **Defect C — Kompensation im Ziel, nicht im Label. Re-Anchor ergänzt,
      KEIN vollständiger Fix möglich.** `reversal_compensation_steps()` geht
      in `target_abs`, aber nicht in `pos_nm` (Plot-/CSV-Label) — das ist *by
      design* korrekt, solange Kompensation reines Spiel-Aufnehmen ohne
      optische Bewegung ist. In SIM sauber durchgerechnet: der Label-Fehler
      skaliert mit `(konfigurierter slip_nm − echter Backlash)`, nicht mit
      dem Backlash selbst — die erste "saubere" Messung dieser Session
      (`+0.082 nm`, exakt `SIM_BACKLASH_NM`) war durch den P3-Bug "Scan
      überfährt End um einen Step" verunreinigt (s. P3 unten). Ergänzt:
      Re-Anchor gegen die kompensations-bereinigte Encoder-Position nach
      jedem GoTo und am Scan-Ende (Muster wie `free_run_worker`), **nicht**
      blinde Addition von `comp` ins Label (das wäre bei korrekter
      Kalibrierung falsch und würde den historischen Free-Run-Bug
      `587.000 → 586.918 nm` wiederherstellen). Für exakt erreichte Moves ist
      dieser Re-Anchor mathematisch ein No-Op — er kann eine
      `slip_nm`-Fehlkalibrierung strukturell **nicht** beseitigen, dafür
      fehlt eine unabhängige optische Rückmeldung auf echter Hardware.
      **Der eigentliche nächste Schritt ist kein Code-Fix mehr:** `slip_nm`
      am Rig gegen den echten Backlash verifizieren/kalibrieren
      (`backlash_cal.py`).

### Nachtrag 2026-08-04 (Selbst-Review der obigen Fixes)

Beim Durchgehen der eigenen Änderungen sind vier Folgefehler aufgefallen,
zwei davon gravierender als die ursprünglich geflaggten Punkte. Alle vier
behoben und in SIM verifiziert (`verify_abcd.py`-Szenarien, s. HISTORY.md):

- [x] **A — `read_position()` liefert bei Lesefehler still `0`.** Die fünf
      neuen Re-Anchor-Stellen schrieben diesen erfundenen Wert ungeprüft in
      `state['current_nm']`/`entry_current` — also in den Ursprung, aus dem
      jeder Folge-Move sein Delta rechnet und den `app_config` als
      `last_current_nm` persistiert. Die umgebenden `try/except` halfen nicht,
      weil `read_position()` **nie wirft**. Wirkung im SIM-Test: das alte
      Verhalten hätte 4.0 nm danebengelegen (wächst mit der Sessiondauer, da
      `POS` frei läuft). Fix: neuer Helper `_encoder_nm_or_none()` nutzt
      `read_position_or_none()`; bei `None` behält jede Stelle ihre bisherige
      Schätzung und loggt eine Warnung. Zusätzlich `do_resume` gehärtet: der
      Anker `curr_abs` kam ebenfalls aus `read_position()` — schlägt der Read
      fehl, wird der Schritt jetzt abgebrochen statt aus einer erfundenen
      Position heraus ein Relativ-Kommando zu schicken.
- [x] **B — Stop während eines Go To stoppte nicht.** Der Auto-Recover-Retry
      konnte „Nutzer-Stop" nicht von „Timeout/Fault" unterscheiden und fuhr
      nach dem Abbruch trotzdem ans Originalziel. Im SIM-Log der Vorsession
      stand das wörtlich (`[DONE] Reached 531.000 nm (after auto-recover)`
      nach einem Stop bei 1/3 der Strecke) — beim ersten Lesen als Erfolg
      fehlinterpretiert. Der `is_moving`-Fix hatte es sogar verschärft, weil
      `stop_action()` nun bis zu 10 s wartet und dem Retry mehr Zeit gibt.
      Fix: `stop_flag` wird **vor** `recover_after_stop()` ausgelesen; bei
      Nutzer-Stop wird nur noch re-verankert, nicht erneut gefahren, und
      `recover_after_stop()` bleibt `stop_action()` überlassen.
- [x] **C — Referenzfahrt setzte gar kein Busy-Flag.** Damit lief ein Stop
      während einer Referenzfahrt weiterhin in die ursprüngliche Race
      (`stop_action()` sah weder `is_scanning` noch `is_moving`, übersprang
      die Wartephase und re-armte den Antrieb). Fix: `reference_run_action`
      setzt/löscht `is_moving` wie `goto_worker`.
- [x] **D — Go To war nicht gegen Mehrfachstart geschützt.** Nur
      `is_scanning` wurde geprüft; ein Doppelklick auf Go To oder Jog
      (ein Jog IST ein Go To) startete zwei `goto_worker` und machte
      `is_moving` unzuverlässig, weil der zuerst fertige Worker das Flag
      löschte. Fix: Reentrancy-Guard auf `is_moving`, und das Flag wird jetzt
      auf dem GUI-Thread **vor** dem Thread-Start gesetzt — das schließt
      zusätzlich das Fenster von einem Thread-Start, in dem ein Stop das Flag
      noch nicht sehen konnte.

**Noch offen aus demselben Review (nicht gefixt, bewusst):** die Log-Schwelle
`1e-4` nm in den Erfolgs-Re-Anchors liegt unter der legitimen
Ankunftstoleranz (`POS_TOL_STEPS` = 90 Schritte ≈ 2.5e-4 nm) → die
„Re-anchoring"-Zeile feuert bei praktisch jedem Move statt nur bei echter
Slip-Fehlkalibrierung. Ebenfalls offen: der Teilweg-Re-Anchor schätzt den
Slack-Anteil mit 0, obwohl „Spiel wird zuerst aufgenommen" eine strikt
bessere Schranke (`min(comp, gefahrene Strecke)`) erlauben würde.

### Arbeitsschritte

- [x] `stage1_diag.patch` angewendet (fertig, verifiziert: 3 Hunks, additiv,
      `try/except: pass`, `read_position()`-Calls 10→10, `patch --dry-run`
      bestätigt sauberen, vollständig applizierten Stand)
- [x] SIM-Session mit `sim_defect_probe.py`: alle drei Defekte plus der
      Stop/GoTo-Race reproduziert, A/B/Race gefixt, C entschärft (s. oben)
- [ ] **Rig-Session** (jetzt der kritische Pfad, ersetzt die vorherige
      "Session mit reproduziertem Offset mitschneiden"):
      - [ ] T3/T4 aus `CONTEXT.md` fahren — sollten nach dem A/B/Race-Fix
            keinen bleibenden Offset mehr zeigen bzw. sofort (nicht erst nach
            vollem Timeout) abbrechen. Falls doch: SIM-Fix am Rig unvollständig,
            zurück zu `CONTEXT.md`.
      - [ ] `slip_nm` gegen echten Backlash verifizieren (`backlash_cal.py`) —
            das ist der Schritt, der Defect C tatsächlich schließt.
      - [ ] TESTPLAN.md Section 3c (10 gequeute Scans) — Ergebnis muss "kein
            monotoner Trend" zeigen.
      - [ ] SIM-Smoke-Test vor der Rig-Session zur Kontrolle erneut laufen
            lassen (`sim_defect_probe.py`).
- [ ] T1/T2 aus `CONTEXT.md` fahren, um die verbleibende C-Restgröße
      (Kalibrierabweichung) zu quantifizieren.

**T5 aktuell nicht durchführbar:** das Konsolen-Log vom 29.07. (15:00–15:30)
wurde nicht mitgeschnitten.

### Artefakte

| Artefakt | Status |
|---|---|
| `stage1_diag.patch` | **angewendet** in `scan_engine.py` (3 Hunks, additiv, `try/except: pass`, `read_position()`-Calls 10→10, `patch --dry-run` bestätigt sauberen Stand) |
| `stage1_patch_guide.pdf` | Erklärung des Patches (EN) |
| `offset_report.pdf` | Diagnose + Hypothesen + T1–T5 (EN) |
| `offset_probe.py` | automatisierte Erhebung; Selftest grün (80 pm injiziert → 80.2 pm in `nm_app`, 0.2 pm in `nm_enc`); Hardware-Hälfte unverifiziert |
| `sim_defect_probe.py` | **neu (2026-08-04).** Fährt `gui_main`/`scan_engine` direkt, headless (`QT_QPA_PLATFORM=offscreen`, kein Xvfb nötig). Hat A/B + den Stop/GoTo-Race reproduziert und die Fixes bestätigt (`label - true_nm nach Stop = +0.00000`); Ground Truth aus `state['ser']._pos`/`._optical_pos` (nur in SIM). Watchdog via `faulthandler` pro Sektion. Details im Kopf-Docstring der Datei. |

`offset_probe.py` fährt `gui_main`/`scan_engine` **nicht** (Qt-Widgets
verschweißt; Stubs kämen sonst in den Messpfad). Es beweist Physik und
Arithmetik, nicht das Verhalten von `gui_main`. `sim_defect_probe.py` schließt
genau diese Lücke — fährt `gui_main` direkt, beweist aber wiederum nur
Codepfade/SIM-Physik, nicht reales Motor-Timing oder die `LR`-Sollwert-
Feinheit nach einem harten Stop. Beide zusammen brauchen weiterhin T3/T4 + Log
am Rig zur Bestätigung.

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
      Buchhaltungs-Ungenauigkeit. **Beobachtung 2026-08-04:** dieser Overrun
      hat eine P0-SIM-Messung (Defect-C-Repro in `sim_defect_probe.py`)
      verunreinigt — die Messung lief einen Schritt hinter dem beabsichtigten
      Scan-Ende, s. `CONTEXT.md` P0. Ändert nichts an "bewusst erhalten, nur
      mit Rücksprache" — nur ein Präzedenzfall dafür, dass sich der Bug auch
      außerhalb reiner Buchhaltung bemerkbar machen kann.
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
