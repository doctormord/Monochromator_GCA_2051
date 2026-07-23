# BACKLOG — VRS41

> Lebendes Dokument. Überschreiben, nicht anhäufen. Priorität: P0 = jetzt, P3 = später.

## DONE — Fundament + Modul-Split + Qt6-Lift (alle end-to-end verifiziert)

- Monolith komplett in 13 Module aufgeteilt (siehe HANDOVER.md für die
  vollständige Liste). GUI von Tkinter auf **Qt6** (PyQt6 + pyqtgraph)
  gehoben, `scan_engine`/`motion`/`daq`/`protocol_faulhaber` dabei
  unverändert gelassen (Adapter-Brücke + GUI-Thread-Marshalling).
- **SIM-Modus** (`sim_hardware.py`): Port `"SIM"` → kompletter Ablauf
  (Connect/Goto/Scan/Queue/Backlash/CSV) ohne Motor/DAQ testbar.
- **Backlash-Kalibrierung** (`backlash_cal.py` + `CalibrationDialog`):
  Referenzlinie auf-/abwärts, Peak-Versatz = Backlash, "Apply & Save".
- **Settings-Persistenz** (`app_config.py`): fast jedes Eingabefeld
  überlebt einen Neustart (Scan-Range/Step/Wait, Jog-Step, Averaging,
  Export, DAQ, Port/Baud, Kalibrier-Dialog-Felder, `last_current_nm`).
  Kalibrier-Dialog-Defaults liegen in `device_constants.py`
  (`CAL_*_DEFAULT`), Overrides in `app_config.DEFAULT_CONFIG` — kein
  hartcodierter Wert mehr direkt in `gui_main.py`.
- **Wait/dwell** zeigt ms statt s (Default 0); Konvertierung sitzt nur in
  `gui_main._EntryAdapterMsToSec`, Backend bekommt weiter Sekunden.
- **"⇅ Swap Start / End"-Button** in SCAN.
- **Scan-Button-Bug** behoben: `scan_worker()` rief nach normalem Ende nie
  `ui_hook('set_buttons_scanning', False)` — jetzt analog zu `do_resume()`.
- **Motion-Ramp** (`AC`/`DEC`/`SP`, FAULHABER-Handbuch 7000.00029 bestätigt):
  `protocol_faulhaber.configure_ramp()` + `device_constants.USE_MOTION_
  RAMP`/`RAMP_AC`/`RAMP_DEC`/`RAMP_SP`. Am Rig mit `tune_ramp.py` gemessen
  (`DEC` stand auf Maximum 30000 → abrupter Stopp), auf `AC=50, DEC=100,
  SP=10000` korrigiert und vom Nutzer bestätigt. Aktiv (`USE_MOTION_RAMP
  =True`).
- **`REFERENCE_DIRECTION`** von `-1` auf `1` korrigiert (Rig-Verifikation).
- **Alle Code-Kommentare/Docstrings + GUI-Strings auf Englisch** übersetzt
  (Code bytegenau unverändert, nur Text). Nachkontrolle in dieser Session:
  drei restliche deutsche Stellen gefunden (Docstrings "Referenzfahrt" in
  `motion.py`/`scan_engine.py`, zwei Sektions-Kommentare in `gui_main.py`)
  + `gui_main_tk.py` (unbenutzte Tk-Referenz) war komplett auf Deutsch
  stehengeblieben (Button-Label, Richtungs-Label, Kommentare) -- alle jetzt
  Englisch, per Grep über alle `.py`-Dateien gegenverifiziert (0 Treffer).
  HANDOVER.md/BACKLOG.md bleiben Deutsch (eigene, im Dokument selbst
  festgelegte Konvention: "Prosa DE, Code/Bezeichner EN").
- Diverse C-Kleinkram-Fixes: `SCAN_EPSILON_NM` vereinheitlicht,
  DAQ-Stillfehler jetzt geloggt (rate-limited) statt still `0.0`.

### DONE (jüngste Session -- SIM/offscreen-getestet, Rig-Test optional)

- **`_EntryAdapter`-Shadow-Bug** (Scan startet nicht, "start/end/step must be
  numbers", betraf gefühlt immer Step): Shadow wurde nur bei künftigen Edits
  gefüllt, ein vorbelegtes ungeändertes Feld hatte Shadow "" -> Validierung
  scheiterte bis zum Neutippen. `_EntryAdapter.__init__` übernimmt jetzt den
  aktuellen Widget-Text als Shadow, wenn kein `initial` übergeben wird.
  Unit-getestet.
- **Crosshair verschwand nach "Clear Plot"** (bis App-Neustart): `_pi.clear()`
  in `PlotManager.clear_all()` entfernte die Crosshair-Linien mit; sie werden
  jetzt nach dem Clear wieder angehängt. Smoke-getestet (Linien nach Clear
  wieder im PlotItem).
- **Connect/Disconnect -> ein Toggle-Button** (Label+Farbe nach Zustand,
  teal Primary/rot Danger). Alte Adapter-Namen als Aliase erhalten.
- **SCAN-Layout**: Swap-Button (Glyph) zwischen Labels und Start/End-Feldern,
  über beide Zeilen gespannt; Reihenfolge Start Scan -> Free Run Scan ->
  Free-Run-Speed -> Pause/Resume -> Stop; langer Free-Run-Hinweis -> Tooltip.
- **"Clear Plot"** aus EXPORT in eine Toolbar über dem Scope verschoben.
- **CALIBRATION** im Panel nach BACKLASH (vor DAQ) einsortiert.
- **Wait/dwell = ms** bereits erledigt (siehe oben) -- diese Session nur
  bestätigt, keine Änderung nötig.
- **Dezimaltrennzeichen-Inkonsistenz**: der Post-Connect-Dialog nutzte
  `QInputDialog.getDouble`, das LOCALE-gebunden ist -> auf einem deutschen
  System war dort ein KOMMA Pflicht, während alle Felder der App mit
  `float()` geparst werden und einen PUNKT brauchen. Jetzt `getText` +
  `_parse_float_loose()` (akzeptiert beides, fragt bei Unsinn erneut statt
  still nichts zu tun); dieselbe Funktion parst auch das
  CALIBRATION-Feld "Known λ here".

## P1 — Rig-Verifikation (weiterhin offen)
- [x] **Scan-Drift: URSACHE GEFUNDEN, es war NIE das Spiel.**
      Entscheidend war der Vergleich zweier Serien mit unterschiedlicher
      Scanbreite (Rig 2026-07-23):
        0.2 nm /  20 Schritte/Scan:  +2.7 pm pro Zyklus
        2.0 nm / 200 Schritte/Scan: +22.5 pm pro Zyklus  (Queue, 10x)
      Beide mit exakt ZWEI Umkehrungen pro Zyklus, beide streng monoton.
      Die Drift skaliert also mit der SCHRITTZAHL (~0.11-0.17 pm je Schritt),
      NICHT mit der Zahl der Umkehrungen. Das schliesst Backlash als Ursache
      aus -- Backlash waere pro Umkehrung konstant, unabhaengig von der
      Scanbreite. Passt auch dazu, dass eine zweite Referenzfahrt nichts half.
      URSACHE: `scan_worker` berechnete jedes Ziel RELATIV zur gerade
      gemessenen Istposition (`target = read_position() + fester_Schritt`).
      Ein Fehlbetrag wurde damit zum Startpunkt des naechsten Schritts und
      dauerhaft mitgeschleppt -- eine reine relative Kette ohne Korrektur.
      Verstaerkt durch `POS_TOL_STEPS` (0.01 nm = 3617 Schritte), das bei
      0.01 nm Schrittweite exakt EINE ganze Schrittweite breit ist: ein Move
      darf weit zu frueh als 'angekommen' gelten. Gemessen ~40 Schritte
      Verlust je 3618-Schritt-Move (~1.1 %).
      GEFIXT: absolutes Schrittraster. `grid_anchor_steps` wird einmal beim
      Scanstart gelesen, jedes Ziel ist
      `anchor + rel_step_steps*index + grid_slack_steps`. Ein Fehlbetrag wird
      damit vom naechsten Move korrigiert statt akkumuliert. Die
      Backlash-Kompensation laeuft separat in `grid_slack_steps` (sie taucht
      in POS auf, ist aber Spielaufnahme, keine optische Fahrt).
      SIM-verifiziert: 5 identische Scans, Restdrift -0.8 pm (vorher waere es
      bei dieser Schrittzahl deutlich mehr gewesen).
      AM RIG GEGENZUPRUEFEN: dieselbe 10er-Queue-Serie wiederholen; die Drift
      sollte von +22.5 pm/Zyklus auf ~0 fallen.

- [ ] **`POS_TOL_STEPS` ist fuer feine Schritte zu grob.** Fest 0.01 nm
      (3617 Schritte), unabhaengig von der Schrittweite:
        0.10 nm Schritt -> Toleranz 0.1x der Schrittweite   (unkritisch)
        0.01 nm Schritt -> Toleranz 1.0x                    (grenzwertig)
        0.001 nm Schritt -> Toleranz 10x                    (kaputt)
        0.0001 nm Schritt -> Toleranz 100x                  (kaputt)
      Bei `super_slow_scan.txt` (0.001 nm Schritt) gilt ein Move als
      angekommen, BEVOR er nennenswert gefahren ist -- die einzelnen
      Messpunkte sitzen dann nicht dort, wo das Label sagt. Das absolute
      Raster verhindert zwar die AKKUMULATION, korrigiert aber nicht die
      Position des einzelnen Punktes.
      DIE HARDWARE IST NICHT DAS LIMIT: STEPS_PER_NM = 361.765, also
      1 Schritt = 2.76 fm und 120.6 Motorumdrehungen pro nm. Der Antrieb
      regelt laut eigenem Konfig-Dump auf GCORRIDOR = 20 Schritte, das ist
      181x feiner als die App-Toleranz. 0.001 nm = 362 Schritte = 18x der
      Antriebs-Korridor, servotechnisch also unproblematisch.
      MESSPROGRAMM GESCHRIEBEN: `step_resolution_test.py`. Faehrt pro
      Schrittweite N Einzelschritte, misst die Einschwingkurve und den
      ENDGUELTIGEN Ruhefehler und wertet Kandidaten-Toleranzen (20..3617
      Schritte) nach derselben Regel aus wie `wait_until_position`
      (POS_STABLE_COUNT aufeinanderfolgende Treffer, KEINE Gutschrift fuer
      transientes Durchfahren). Gibt die engste Toleranz aus, die bei JEDEM
      Move gehalten hat.
      NICHT spekulativ geaendert -- erst `step_resolution_test.py` am Rig
      laufen lassen, dann den Wert mit ~2x Reserve in `POS_TOL_STEPS`
      eintragen und einen echten 0.001-nm-Scan auf `[TIMEOUT]`-Zeilen pruefen.

- [x] **Zahlenformatierung zerstoerte feine Schrittweiten.** Die Scan-Queue
      formatierte mit festem `%.3f`: ein Schritt von 0.0001 nm wurde als
      "Delta 0.000 nm" angezeigt UND -- gravierender --
      `run_scan_queue_worker()` schreibt diese Strings vor jedem Lauf ZURUECK
      in die Eingabefelder, der gequeuete Scan waere also mit Schrittweite
      null gelaufen. Ersetzt durch `_fmt_nm()`: so viele Nachkommastellen wie
      noetig (bis 6), mindestens 3. Getestet inkl. Queue-Roundtrip mit
      0.0001 nm.

- [x] **Free Run zaehlte die Backlash-Kompensation als optische Fahrt.**
      Rig-Log 2026-07-23: drei identische Sweeps, Ziel 587.000 nm, jedes Mal
      re-verankert auf 586.918 nm -- Abweichung exakt 0.082 nm, also exakt die
      geloggte Slip-Kompensation. `rel_sweep` enthielt die Kompensationsschritte
      (richtig, sie muessen gefahren werden), aber die nm-Achse wurde als
      `pos - start_steps_abs` gerechnet, hat sie also als Wellenlaengenaenderung
      mitgezaehlt. Der gesteppte Scan hat das Problem nicht, weil er `pos_nm`
      arithmetisch fuehrt -- DESHALB lagen Step-Scan und Free Run derselben
      Linie um genau eine Kompensation gegeneinander versetzt (Screenshot
      "3 freerun vs 1 stepscan").
      GEFIXT: `nm_anchor_steps = start_steps_abs + comp_steps` als Bezugspunkt
      fuer die nm-Achse (live UND fuer die Re-Verankerung nach dem Sweep).
      Waehrend die ersten `comp_steps` gefahren werden, steht nm korrekt bei
      s_nm -- das Spiel wird aufgenommen, das Gitter bewegt sich noch nicht.
      SIM-verifiziert: Free Run landet jetzt auf 587.000 statt 586.918.

- [ ] **Wiederholgenauigkeit: MONOTONE DRIFT, kein Streuen -- Ursache offen.**
      10 identische Step-Scans ueber dieselbe Linie, Schwerpunkt bei 50 % der
      Peakhoehe ausgewertet:
        1 Ref-Fahrt : 587.9315 -> 587.9557 nm, +24.2 pm gesamt, +2.7 pm/Zyklus
        2 Ref-Fahrten: 587.9099 -> 587.9398 nm, +29.9 pm gesamt, +3.3 pm/Zyklus
      BEIDE streng monoton steigend (jeder einzelne Scan hoeher als der
      vorherige). Damit ist es KEIN Rauschen und keine Streuung -- die
      Standardabweichung (7.6 bzw. 9.9 pm) ist hier die falsche Kennzahl.
      WICHTIG: eine ZWEITE Referenzfahrt hat NICHT geholfen, die Drift war
      sogar minimal groesser. Das entkraeftet "Spiel nicht richtig gesetzt"
      als Erklaerung -- wiederholtes Setzen aendert nichts.
      Groessenordnung: +2.7 pm/Zyklus = ~973 Schritte = ~3.3 % der
      Kompensation (29665 Schritte). Pro Scan-Zyklus finden ZWEI Umkehrungen
      statt (runter scannen, hoch zum Startpunkt), jede mit Kompensation.
      Ein systematischer Restfehler von wenigen Prozent auf der Kompensation
      wuerde sich genau so aufsummieren.
      NAECHSTER SCHRITT (Messung, nicht raten): dieselbe 10er-Serie mit
      Slip = 0 fahren und die Drift/Zyklus vergleichen.
        * Drift verschwindet oder kehrt Vorzeichen um -> der Kompensationswert
          ist zu gross/zu klein, und aus den zwei Messpunkten laesst sich der
          richtige Wert direkt interpolieren.
        * Drift bleibt gleich -> die Ursache liegt nicht in der Kompensation,
          sondern in echtem mechanischem Schlupf pro Umkehrung.
      Zusatzbeobachtung: die Peakamplitude faellt in beiden Serien monoton
      (0.1868 -> 0.1842 bzw. 0.1959 -> 0.1881). Konsistent damit, dass der
      Peak relativ zum 0.01-nm-Abtastraster wandert; alternativ Lampendrift.
      Unterscheidbar, indem eine Serie mit feinerem Step wiederholt wird.

- [x] **"Stop tut manchmal nichts, man muss mehrmals druecken" -- Race
      gefunden und behoben.** `recover_after_stop()` setzt `stop_flag`
      wieder auf False (es re-armiert den Antrieb). `stop_action()` rief das
      auf dem GUI-Thread auf, WAEHREND der Scan-Worker noch lief. `stop_flag`
      war also nur fuer die Dauer von `safe_stop()` dazwischen True -- schaute
      der Worker in genau diesem Fenster nicht hin, sah er die Anfrage NIE und
      scannte weiter. Mehrfaches Druecken traf das Fenster irgendwann zufaellig.
      VERSCHAERFT DURCH DEN EIGENEN FIX: das Fenster war frueher ~2.4s (ST und
      V0 warteten je einen vollen Serial-Timeout auf eine Quittung, die es in
      ANSW0 nie gibt). Seit diese Kommandos fire-and-forget sind (korrekt, und
      anderswo ein grosser Speedup) sind es ~20ms -- KUERZER als die 50ms
      (`PAUSE_POLL_S`), in denen die Sleep-Schleifen des Workers das Flag
      pruefen. Derselbe Bug, nur von "beisst selten" zu "beisst meistens".
      GEFIXT: `stop_action()` setzt das Flag und loescht es NIE selbst; das
      Re-Armieren laeuft in einem Hintergrund-Thread, der erst wartet, bis der
      Worker seine Schleife wirklich verlassen hat (`is_scanning` -> False,
      gedeckelt durch `STOP_JOIN_TIMEOUT_S` = 10s). Die serielle Arbeit liegt
      damit ausserdem nicht mehr auf dem GUI-Thread -- `safe_stop()` /
      `recover_after_stop()` nehmen den gemeinsamen Serial-Lock, den der
      Worker streckenweise haelt; darauf vom GUI-Thread zu blockieren fror
      die ganze UI ein, die andere Haelfte von "Stop tut nichts".
      ZUSAETZLICH: der Stepped-Scan-Loop pruefte `stop_flag` nur am
      Schleifenanfang. `acquire_measurement()` bricht bei Stop zwar korrekt
      ab, der Loop fiel danach aber direkt in `LR`/`M` -- nach jedem Stop
      waehrend eines Dwells wurde also noch EIN Schritt losgeschickt und
      sofort wieder abgebrochen. Jetzt Stop-Checkpoint vor dem Fahrbefehl.
      Getestet: 8/8 Einzeldruecke werden von einem Worker mit 50ms-
      Pruefgranularitaet gesehen (vorher der Race-Fall); echter Scan bricht
      bei EINEM Druck ab; Antrieb danach sofort wieder nutzbar.

- [x] **Rig-Test bestaetigt: keine Truncated/Failed Reads mehr ueber den
      GESAMTEN Drehzahlbereich (500-10000 rpm, voller Sweep, dann zurueck),
      mit dem Codestand VOR dem LF-Fix.** Das bedeutet: die drei Fixes
      (Gap-tolerantes Lesen, CR-Vollstaendigkeitspruefung, keine
      RESYNC-Verstaerkung) haben das Problem bereits vollstaendig geloest --
      unabhaengig von der urspruenglich vermuteten Bewegungskorrelation.
      Die REVIDIERTE Einordnung: die vorher scheinbar drehzahlabhaengige
      Fehlerrate war ein Artefakt der ALTEN Bugs (jede Pause = Fragment,
      jede Truncation = ein zusaetzliches Kommando, das den naechsten Read
      seinerseits gefaehrdete -- eine Kaskade, die bei laengeren/langsameren
      Sweeps mehr Gelegenheiten hatte). Nach dem Fix zeigt selbst SP=500 auf
      diesem Rig 0 Fehler. Die vorherige Empfehlung "naechste Schritte sind
      elektrisch" ist damit VERALTET -- siehe die durchgestrichenen Punkte
      unten, die als Kandidaten stehen bleiben, aber nicht mehr prioritaer
      sind. Der Idle-Test (0 Fehler in 2 min) ergaenzt das nur noch, er war
      nicht der alleinige Beleg.

- [x] **NEUER, ECHTER BUG gefunden beim Auswerten desselben Rig-Logs: Free-Run-
      Watchdog-Timeout skaliert NICHT mit dem SP-Override.** Der EINZIGE
      Sweep im Log, der sein Ziel NICHT erreichte (SP=500, 385->390 nm, 1.
      Sweep der Sitzung), brach nach 857 Punkten (~44s bei ~19.5 Hz) bei
      388.798 nm ab -- 3.8 von 5.0 nm, 76% der Strecke. `timeout_for_nm()`
      budgetiert rein nach Distanz ueber die feste Konstante `SEC_PER_NM`
      (device_constants.py) OHNE jede Kenntnis des Free-Run-SP-Overrides:
      fuer 5 nm ergibt das 55 s, unabhaengig davon ob der Sweep mit
      RAMP_SP=10000 oder mit SP=500 (1/20 der Geschwindigkeit) faehrt.
      44s von 55s Budget bei 76% der Strecke passt zu eng zusammen, um Zufall
      zu sein -- der Sweep ist ziemlich sicher in sein Zeitlimit gelaufen,
      lange bevor der Antrieb physisch ankam. Alle ANDEREN Sweeps derselben
      Sitzung (1000-10000 rpm) erreichten ihr Ziel normal, weil sie bei
      diesen Drehzahlen tatsaechlich innerhalb der 55s fertig werden -- nur
      der langsamste Override deckte das fehlende Scaling auf.
      GEFIXT in `scan_engine.free_run_worker`: wenn `sp_rpm < RAMP_SP`, wird
      das Zeitbudget um genau den Geschwindigkeitsfaktor gestreckt
      (`RAMP_SP / sp_rpm`), gedeckelt durch `MAX_MOVE_TIMEOUT` (900s). Bei
      SP=RAMP_SP (kein Override) bleibt das Verhalten exakt wie vorher.
      Unit-getestet (Formel: SP=5000->110s, SP=1000->550s, SP=500->900s
      gedeckelt) und SIM-Regression: ein Sweep mit SP=500 erreicht jetzt sein
      Ziel; ein Sweep mit RAMP_SP verhaelt sich unveraendert.
      HINWEIS: im gepasteten Log fehlt die erwartete
      "[TIMEOUT] Free run exceeded Xs."-Zeile fuer diesen Sweep -- `log()`
      selbst kann eine Zeile nicht stillschweigend verschlucken (faellt bei
      defektem Sink auf print() zurueck), am wahrscheinlichsten wurde sie
      beim Kopieren aus der Konsole schlicht nicht mitgenommen. Die
      quantitative Uebereinstimmung (44s/55s, 76% Strecke) traegt die
      Diagnose unabhaengig davon.

- [x] **ENTSCHEIDENDER TEST DURCHGEFUEHRT: 0 Fehler im Stillstand.**
      `idle_poll_test.py`, 120 s, ~20 Hz, 2401 Polls, 0 truncated/failed.
      Ergebnis eindeutig: die Ursache ist BEWEGUNGSKORRELIERT (EMI, Masse,
      Kabelfuehrung), NICHT der serielle Link/Treiber selbst. Naechste
      Schritte sind elektrisch, nicht Code -- siehe unten.

- [x] **Nebenfund aus demselben Test, echter Bug: `late_reply_drains` war bei
      JEDEM einzelnen Poll aktiv (2401/2401), obwohl 0 Fehler auftraten.**
      Ursache: Handbuch DE_7000_00029, 4.2 -- jede Antwort ist CR GEFOLGT VON
      LF (`98956[CR][LF]`). `_read_reply_until_cr()` stoppt beim CR und liest
      das LF nie mit; es blieb bei JEDEM Kommando im Puffer stehen, bis das
      naechste Kommando es als vermeintlich "spaete Antwort" fehlinterpretierte
      und via `_drain_late_reply()` (WRITE_SETTLE_S extra) wegraeumte. Harmlos
      fuer die Daten (das LF wurde nie geparst), aber eine systematische
      Zusatzverzoegerung auf JEDEM Kommando der ganzen App, nicht nur POS-Polls.
      GEFIXT: `_consume_trailing_lf()` liest das erwartete LF direkt im
      Anschluss an ein vollstaendiges CR-Ende, als Teil DIESES Reads -- kein
      Drain, kein Zaehler, keine Verzoegerung. Ein unerwartetes Byte statt LF
      wird geloggt und der Stream als dirty markiert (Sicherheitsnetz).
      `sim_hardware.py` sendet jetzt ebenfalls CR+LF (Handbuch-treu) statt nur
      CR, damit SIM diese Fehlerklasse kuenftig selbst faengt.
      Unit-getestet: 300 Polls gegen einen CR+LF-Fake-Port -> 0
      `late_reply_drains`, 0 `truncated_replies`. Volle SIM-Regression danach
      gruen (18 Drains statt "bei jedem Kommando" über eine komplette Sitzung
      mit Reference Run/Kalibrierung/Goto/Scan/Free Run -- Rest plausibel).


- [ ] **UEBERHOLT (siehe Eintrag oben "Rig-Test bestaetigt") -- als Kandidatenliste stehen gelassen, falls das Problem trotz der Software-Fixes je wiederkehrt, aber NICHT mehr die naechste Prioritaet.** Urspruenglich: "naechster Schritt (elektrisch, nicht Code)", basierend auf einer inzwischen widerlegten Drehzahlkorrelation. Kandidaten in
      absteigender Wahrscheinlichkeit: (1) Signalkabel (RS232/USB-Seriell)
      liegt parallel zu oder buendelt mit der Motorleitung -> induktive/
      kapazitive Einkopplung, staerker bei niedriger Drehzahl mangels PWM-
      Filterung durch die Traegheit; (2) fehlende/schlechte Masseverbindung
      zwischen Controller-Gehaeuse und PC/USB-Adapter -> Erdschleife oder
      schwimmende Masse; (3) USB-Seriell-Adapter selbst empfindlich auf die
      vom Antrieb erzeugten Stoerungen (naeher am Antrieb positioniert als am
      PC). Naechste Schritte: Kabel raeumlich vom Motorkabel trennen, Ferrit-
      kern auf das Seriellkabel, Massepunkt pruefen -- jeweils mit einem
      identischen langsamen Sweep (SP=500) vorher/nachher vergleichen
      (`truncated_replies`-Zaehler im Log).

- [x] **Abgebrochene POS-Antworten: Ursache im Lesecode gefunden.**
      Rig-Log 2026-07-23 (vier Sweeps, Drehzahl haendisch variiert):
        10000 rpm: 148 Punkte,   0 Fehler ( 0.0%)
         5000 rpm: 155 Punkte,   0 Fehler ( 0.0%)
         1000 rpm: 453 Punkte,  39 Fehler ( 7.9%)
          500 rpm: 402 Punkte, 127 Fehler (24.0%)
      Die Fehlerrate steigt streng monoton mit SINKENDER Drehzahl -- die
      frueher vermutete Korrelation war genau falsch herum.
      Die abgeschnittenen Antworten sind die FUEHRENDEN Ziffern der laufenden
      Position ('768','752','734',... fallend im einen Sweep, '55x','56x',
      '57x',... steigend im Gegenlauf), 2-5 Zeichen lang. Bei 9600 Baud
      dauert ein Zeichen ~1 ms -- das war nie ein Bandbreitenproblem: die
      Antwort STOCKT mitten in der Uebertragung.
      CODE-URSACHE (verifiziert an der installierten pyserial-3.5-Quelle):
      `read_until()` bricht beim ERSTEN leeren `read(1)` ab
      (`else: break`), UND sein Timeout ist ein Gesamt-Deadline fuer den
      ganzen Aufruf. Eine einzige Pause reichte also, um alles bereits
      Empfangene wegzuwerfen.
      GEFIXT: `_read_reply_until_cr()` liest bis CR und toleriert dabei
      Luecken bis `REPLY_GAP_TOLERANCE_S` (0.08 s), begrenzt durch den
      Gesamt-Deadline. Eine nur LANGSAME Antwort kommt jetzt vollstaendig an,
      statt verworfen zu werden.
      AUSSERDEM GEFIXT -- RESYNC-Verstaerkung: der Recovery-Pfad schrieb nach
      JEDEM Abbruch ein zusaetzliches `POS` und warf die Antwort weg. Im
      langsamen Sweep waren das 127 Extra-Kommandos auf einen Link, der
      ohnehin keine vollstaendigen Antworten mehr lieferte. RESYNC drained
      jetzt nur noch, ohne neu abzufragen.
      Unit-getestet: Antwort mit 50-ms-Pause mitten drin kommt vollstaendig
      an; toter Link bleibt durch den Deadline begrenzt; ein fehlgeschlagenes
      POS erzeugt genau 1 Write statt 2.

- [x] **Der CR-Terminator-Check hat sich bewaehrt.** In beiden langsamen
      Sweeps wurde KEIN falscher Wert geplottet und beide endeten auf der
      korrekten Position (380.000 bzw. re-verankert 369.226). Vor dem Check
      waeren aus '768' & Co. echte Positionen geworden -- genau der alte
      ~587nm-Sprung. Der Schutz bleibt, unabhaengig von der Ursache.


- [x] **~587nm-"Zickzack" / POS-Ausreisser: URSACHE GEKLÄRT — es sind
      FRAGMENTE, keine veralteten Antworten.** Der Rohstring-Log vom
      2026-07-23 hat genau die Frage beantwortet, für die er gebaut wurde.
      21 verworfene POS-Antworten in einem Free Run, Rohstrings ausschliesslich
      `'1'` (20x) und `'103862'` (1x) — während die echte Position 7-8 Stellen
      hatte. Das sind abgeschnittene Zahlen (Kopf weggeflusht bzw. Rest noch
      nicht angekommen), KEINE plausiblen alten Positionen. Damit ist die
      Hypothese "stale-aber-valide alte Antwort" widerlegt und die
      Stream-Desynchronisation bestätigt.
      Mechanismus: `send_cmd` flushte den Eingangspuffer bedingungslos direkt
      vor dem nächsten Schreiben. Traf das eine noch EINLAUFENDE Antwort der
      vorherigen Abfrage, wurden nur die bereits angekommenen Bytes verworfen —
      der Rest landete danach im Puffer und wurde als Antwort auf die NEUE
      Abfrage gelesen. Bei 9600 Baud braucht eine 8-stellige POS-Antwort ~9 ms,
      das Fenster ist real.
      GEFIXT (drei Massnahmen):
      1. `_drain_late_reply()`: flusht nur noch, wenn tatsächlich Bytes
         warten, und wartet vorher eine Settle-Periode — eine komplette alte
         Antwort wegzuwerfen ist harmlos, eine halbe ist Gift.
      2. `REQUIRE_CR_TERMINATED_REPLY`: eine Antwort ohne abschliessendes CR
         gilt als fehlgeschlagener Read (pyserial liefert bei Timeout den
         Teil-Puffer OHNE Terminator zurück — daraus wurde bisher eine kürzere,
         perfekt parsebare und völlig falsche Position).
      3. `POLL_TIMEOUT` 0.10 -> 0.25 s: die Antworten kamen zu SPÄT, nicht gar
         nicht. Länger warten ist besser als verwaisen lassen.
      4. `serial_dirty`-Flag: nach einem Read, der leer oder abgeschnitten
         zurueckkam, schuldet der Antrieb noch eine Antwort, die evtl. NOCH
         NICHT angekommen ist (`in_waiting` beweist dann nichts). Das naechste
         Kommando wartet deshalb `SERIAL_RECOVERY_SETTLE_S` und drained, bevor
         es schreibt. Das ist der deterministische Teil des Fixes.
      Unit-getestet gegen eine Nachstellung des echten Fehlerbilds
      (Timeout -> verspaetete Antwort -> naechster Write).

      KEINE Drehzahl-Korrelation: der Nutzer hatte Lauf 2 bewusst auf 10000
      gestellt, und frueher liefen umgekehrt LANGSAME Laeufe schlecht und
      schnelle gut (der alte ~587er-Vorfall war bei SP=1000). Der Mechanismus
      ist drehzahlunabhaengig -- er haengt an der Antwortlatenz gegenueber dem
      Poll-Takt, nicht an der Motordrehzahl.

      VEREINHEITLICHT DEN ALTEN ~587nm-VORFALL: eine KONSTANTE Muell-Antwort
      erzeugt eine KONSTANTE falsche Wellenlaenge, weil
      `nm = start_nm + f(garbage - start_steps_abs)` bei konstantem `garbage`
      konstant ist. Genau deshalb sprang der Wert "immer auf denselben ~587er"
      -- das war nie das Merkmal einer alten Antwort, sondern die Signatur
      eines konstanten Fragments (bzw. der frueher fabrizierten 0 aus
      `read_position()`). Plausibilitaetsrechnung: POS = 0 bzw. 1 bei einem
      Sweep-Start von 300 nm ergibt 587 nm, wenn `start_steps_abs` ~103.8M
      war -- dieselbe Groessenordnung wie die real geloggten Absolutpositionen
      (9.9M-12.5M in einer anderen Sitzung). Kein Beweis (der damalige
      `start_steps_abs` wurde nicht geloggt), aber ein enger Fit.
      OFFEN am Rig: denselben Free Run zweimal bei GLEICHER Drehzahl fahren und
      Ausreisserzahl vergleichen. Bleibt sie unverändert, ist Antwortlatenz
      NICHT die Ursache und `POLL_TIMEOUT` gehört zurückgedreht statt weiter
      hochgesetzt.

- [ ] **`read_position()` hat KEINEN Ausreisser-Schutz — verbleibende
      Exposition.** Der Free Run filtert unmögliche Sprünge, der normale
      Goto/Scan-Pfad nicht: `read_position()` macht `int(reply)` und liefert
      bei Exception `0`. Ein Fragment wie `'1'` wird dort also zu Position 1.
      In der Toleranzschleife ist das harmlos (sieht aus wie "noch nicht da"
      und heilt sich selbst), aber `goto_worker` liest `read_position()` EINMAL
      zu Beginn und rechnet daraus das Ziel — dort wäre ein Fragment nicht
      harmlos. Die drei Protokoll-Fixes oben sollten Fragmente verhindern;
      ein zusätzlicher Plausibilitätsfilter in `read_position()` wurde BEWUSST
      NICHT eingebaut (würde Bewegungsverhalten auf ungetesteter Hardware
      ändern). Erst nach dem Rig-Vergleichslauf entscheiden.

- [x] **Free-Run-Drehzahl wird jetzt IMMER geloggt** (vorher nur bei einem
      Override). Im Rig-Log vom 2026-07-23 fehlte die Zeile bei Lauf 2, weil
      die eingestellten 10000 rpm exakt RAMP_SP entsprachen -- der Lauf war
      VOM NUTZER BEWUSST schneller eingestellt (1000 -> 10000), nicht durch
      einen Bug. Ohne die Zeile liessen sich die beiden Laeufe im Log aber
      nicht unterscheiden, was zu einer falschen Ursachenzuschreibung fuehrte.
      Ausserdem gefixt (unabhaengiger Designfehler, NICHT die Ursache dieses
      Vorfalls): `_on_free_run_sp_edited` fiel bei leerem/ungueltigem Feld auf
      RAMP_SP zurueck, also auf die MAXIMALdrehzahl, und `editingFinished`
      feuert auch bei Fokusverlust. Faellt jetzt auf den letzten GUELTIGEN Wert
      zurueck (`_last_valid_free_run_sp`).


- [ ] **`HP0` war nie ein Bewegungsbefehl — Endschalter-Polarität wurde bei
      jedem Connect/Stop überschrieben.** Die App sendet seit den Monolith-
      Zeiten `HP0` zusammen mit `EN`/`V0`, offenbar in der Annahme, das sei
      ein „halt/hold position". Im Handbuch (3.5.3.1 / 7.1.5, jedes Vorkommen
      geprüft) hat `HP` genau EINE Bedeutung: **Hard Polarity — Bitmaske,
      welche Flanke/welcher Pegel jedes ENDSCHALTER-Eingangs gültig ist.**
      `HP0` schreibt also die Endschalter-Polarität aller fünf Eingänge auf 0.
      RAM-only (kein SAVE), aber bis zum Power-Cycle wirksam.
      Umgesetzt: `device_constants.SEND_LEGACY_HP0` (Default **False** — der
      Schreibzugriff unterbleibt; `init_motor`/`safe_stop`/`recover_after_stop`
      angepasst). Es geht nichts Bewegungsrelevantes verloren, `HP` ist kein
      Bewegungsbefehl. Auf True setzen stellt das alte Verhalten wortgleich her.
      **ENTSCHEIDENDES EXPERIMENT (bitte am Rig):** Controller stromlos machen,
      dann `read_faulhaber_config.py` starten **BEVOR** die App läuft. Die
      IOC/HP-Zeile zeigt dann den EEPROM-Wert:
        * `HP = 00000` -> das `HP0` war immer ein No-Op, Thema erledigt.
        * `HP != 00000` -> die App hat die Endschalter-Polarität jede Session
          umgestellt, d.h. Hard-Blocking war auf dem FALSCHEN Pegel scharf.
          Das wäre ein plausibler (nicht bewiesener) Beitrag zu einem
          Durchfahren eines Endes.

- [ ] **`HD = 00101`: beide Endschalter sperren dieselbe Richtung (Rechtslauf).**
      Bei einem Endlagen-Paar heißt das, dass nur EIN Ende geschützt ist — in
      Gegenrichtung lässt sich durch den anderen Schalter durchfahren.
      BEWUSST NICHT GEÄNDERT: das braucht einen Schreibzugriff + `SAVE`, und
      vorher muss am Rig geklärt sein, welcher Eingang physisch an welchem
      Ende sitzt (Eingang 1 = AnIn, Eingang 3 = 3. Eingang; mit
      `read_faulhaber_config.py --watch` und Drücken beider Schalter
      eindeutig zuzuordnen). Erst zuordnen, dann entscheiden.

- [ ] **OST-Endschalter-Logik ist ACTIVE LOW — die schlafende Guard-Logik ist
      invertiert.** Am Rig gemessen (drei Läufe, beide Schalter einzeln
      gedrückt): `0x0500` = beide frei, `0x0400` = Eingang 1 gedrückt,
      `0x0100` = Eingang 3 gedrückt. Ein gedrückter Endschalter LÖSCHT sein
      Bit; deckt sich mit `HP = 0` („fallende Flanke/LOW gültig", Handbuch).
      Der ruhende Guard in `motion._pos_wait_impl_fallback` prüft aber auf
      0 -> 1 („became active"). Würde man ihn nur durch Initialisieren der
      drei Variablen aktivieren, ignorierte er JEDEN echten Endschalter-Treffer
      und feuerte stattdessen beim LOSLASSEN. Kommentar im Code entsprechend
      korrigiert, Guard weiterhin bewusst inaktiv. Ebenso korrigiert: der
      Beispielwert `OST_HARD_LIMIT_MASK = 256` in `device_constants` war
      invertiert (256 = Eingang 1 frei) — eingetragen hätte er nach JEDEM
      normalen Move einen Limit-Fehler gemeldet (der Rig-Ruhewert ist 0x0500).
      Ein korrekter Limit-Check lässt sich nicht als OR-Maske ausdrücken.
      OFFEN: sauberen active-low-Guard bauen + am Rig verifizieren (Schalter
      während einer langsamen Fahrt drücken).

- [ ] **`LR` ist relativ zur zuletzt GESTARTETEN Sollposition, nicht zu `POS`**
      (Handbuch 3.3.1.3). Nach hartem `ST`, DEVIATION-Fault oder Endschalter-
      Block ist die LR-Basis des Antriebs also nicht die Istposition. Die App
      re-verankert nach einem Free Run zwar ihre eigene `current_nm` aus `POS`,
      der ANTRIEB rechnet den nächsten `LR` aber weiter von der alten
      Sollposition. Verdächtig für die beobachtete Entkopplung. Zu prüfen, ob
      Goto/Scan besser auf `LA` (absolut) umgestellt werden sollten — NICHT
      spekulativ umgebaut, braucht einen Rig-Test mit absichtlichem Stopp
      mitten in der Fahrt und anschließendem Goto.

- [ ] **Wiederholter ~587nm-Spike beim Free Run ("Zickzack") -- drei
      Härtungen ergänzt, Ursache noch offen:** Nutzer-Beschreibung des
      Vorfalls (300->350 nm, SP=1000, Sprung immer auf denselben ~587er-Wert,
      nicht zufällig unterschiedlich, dann zurück) spricht GEGEN "zwei
      Antworten kleben zusammen" (würde je nach echter Position
      unterschiedlich aussehen) und FÜR eine feste alte Antwort, die
      wiederholt auftaucht. Motion Manager als Ursache AUSGESCHLOSSEN (nie
      installiert/benutzt -- Nutzerauskunft). Concurrency-Bug in der eigenen
      App AUSGESCHLOSSEN (grep über den ganzen Code: jeder serielle Zugriff
      läuft durch denselben `state['lock']`, keine Umgehung gefunden). Drei
      Maßnahmen ergänzt (kein Rate-Fix, alle sinnvoll unabhängig von der
      genauen Ursache):
      1. **Outlier-Filter** im Free-Run-Loop verwirft physikalisch
         unmögliche Sprünge (>~0.41 nm/50ms = 50x gemessene Maxrate).
         Unit-getestet.
      2. **`drain_stale_serial()`**: beim Connect, VOR dem ersten Befehl,
         wird der Eingangspuffer passiv geleert UND geloggt, was drin war.
      3. **`ANSW0`** wird jetzt explizit bei `init_motor()` gesendet (stiller
         Modus) -- das Protokoll der App geht ohnehin überall davon aus,
         dass der Antrieb nie unaufgefordert antwortet; Handbuch dokumentiert
         den Werksstandard aber als NICHT still.
      NEU: `read_position_raw_or_none()` gibt zusätzlich den ROHEN String
      zurück, den der Controller geschickt hat -- der Outlier-Filter loggt
      bei JEDEM Ausreißer (nicht gedrosselt) die exakte rohe Antwort, nicht
      nur "verworfen". Damit ist der nächste Vorfall forensisch auswertbar
      (garbled Frame vs. stale-aber-valide alte Antwort lassen sich am
      Rohstring unterscheiden). SIM-getestet (alle drei Härtungen, `ANSW0`
      bricht SIM-Connect nicht). Rig-Test aussteht -- siehe auch
      `read_faulhaber_config.py`-Punkt direkt darunter, dessen CST-Decode
      den tatsächlich aktiven ANSW-Modus zeigen wird.

- [x] **`read_faulhaber_config.py` am Rig gelaufen — Ergebnisse in HANDOVER
      ("Controller-Fakten"). Beantwortet: ANSW0 aktiv (bestätigt die
      Delay-Ursache), Endschalter = Eingang 1 + 3, active LOW, HD sperrt
      beidesmal Rechtslauf, SWS latcht hier nichts (HOC=0), TEM =
      Gehäusetemperatur einer integrierten Einheit. Folgepunkte daraus stehen
      oben. Ursprünglicher Auftrag war:** Diagnose-Skript liest den kompletten aktiven Zustand des
      Controllers aus PDF-Handbuch DE_7000_00029 -- beantwortet mit echten
      Werten statt Vermutung: (1) ist POS echter Encoder/Hall-Feedback oder
      nicht (CST/GMOD/GENCRES), (2) welche Eingänge sind als Hard-Blocking-
      Endschalter konfiguriert + wurden sie je ausgelöst (IOC/HOC/SWS --
      SWS ist LATCHED, das nutzt die App bisher NICHT, nur OST-Live-Pegel),
      (3) welcher ANSW-Modus tatsächlich aktiv ist/war (siehe Punkt oben).
      `--watch`/`--watch-only`: live Endschalter-Test, Druck auf einen
      Endstop zeigt sofort, welcher Eingang reagiert (Live-Pegel via OST +
      gelatchter Trip via SWS). Beide Decoder gegen Handbuch-Beispiele
      unit-getestet. Noch nicht am Rig gelaufen.

- [ ] **Free-Run-Runaway + Positions-Entkopplung gefixt, Rig-Test offen:**
      Am Rig lief ein Free Run 305->350 nm bis 586 nm weg (867 Punkte statt
      ~350er-Ende), Motor stand mechanisch bei ~318, App zeigte 586 -- völlig
      entkoppelt. DREI Ursachen gefunden+gefixt (alle in scan_engine.py außer
      1):
      1. **`motion.steps_to_delta_nm()` gab None zurück** -- Funktionskörper
         (`return dnm`) fehlte komplett, nur der Docstring war da. Free Run
         crashte darauf (`float + NoneType`) bzw. rechnete Müll. Körper
         wiederhergestellt.
      2. **Kein Overshoot-Guard im Free-Run-Loop:** einzige "fertig"-Bedingung
         war Position-steht-still-UND-am-Ziel; lief der Motor am Ziel VORBEI,
         pollte+plottete die Schleife endlos weiter. Neuer harter Guard: stoppt,
         sobald die reale POS das Ziel in Bewegungsrichtung erreicht/überschreitet
         (Vergleich im Step-Raum via `direction_steps`, das INVERT_DIRECTION
         berücksichtigt). Unit-getestet: feuert exakt bei Ziel, nicht davor.
      3. **Keine Positions-Rückführung nach Stop/Fault:** `current_nm` blieb beim
         letzten geplotteten Wert stehen, während der Motor woanders stand. Jetzt
         wird nach jedem Free Run (egal wie beendet) die echte POS zurückgelesen
         und `current_nm`/`entry_current` darauf re-verankert.
      SIM-getestet (Guard greift bei 350, current_nm korrekt). OFFEN: am echten
      Rig mit graduellem Move gegenprüfen, dass der volle Punkt-Trace bis ~350
      entsteht und dort sauber stoppt.
- [ ] **DEVIATION-Fault bei hartem Stop im schnellen Free Run** (aus Vor-
      Session): `ST` mitten in schneller Bewegung -> DEVIATION (OST 0x0520).
      Physik des Sofortstopps, kein Bug. OFFEN: reicht `EN` in
      recover_after_stop zum Quittieren, oder braucht es explizites Fault-Reset?
      Am Rig prüfen: nach dem Fault einen normalen Goto -> läuft er sauber?

- [ ] **Offset-Kalibrierung ("CALIBRATION"-Sektion) am Rig prüfen +
      klären ob Offset oder Skalierung:** Nutzer hat gesehen, dass die
      Referenzlinie nicht genau sitzt. Da das System rein relativ ist (kein
      Absolut-Positionssensor, `current_nm` nur delta-fortgeschrieben), ist
      das zunächst ein ANKER-Problem. Neue `motion.set_current_wavelength()`
      + GUI-Control "Set current λ to this value": bekannte Linie zentrieren,
      wahre Wellenlänge eintragen, anwenden -> `current_nm`/`entry_current`
      werden re-verankert, OHNE Motorbewegung, STEPS_PER_NM unangetastet.
      SIM-getestet (setzt + persistiert korrekt). OFFEN: am Rig eine zweite
      bekannte Linie gegenprüfen -- stimmt sie danach auch, war's ein
      konstanter Offset (erledigt); driftet sie noch, ist es ein
      Skalierungsfehler und braucht die Zwei-Punkt-Kalibrierung (s. P3).

- [ ] **`free_run_scan_action()` ("Free Run Scan") — Rig-Test aussteht:**
      Umgesetzt (`scan_engine.py` + Button in `gui_main.py` SCAN-Sektion +
      `motion.steps_to_delta_nm` für POS-Readback->nm). Basis:
      `probe_move_timing.py`-Messung (~19-20 Hz POS+DAQ-Pollrate, siehe
      History). SIM-Funktionstest durchgelaufen (Connect -> Free Run 500->
      505 nm -> Plot-Punkte vorhanden -> anschließender Goto zielt korrekt,
      `entry_current` bleibt konsistent) -- ABER SIM bewegt Moves instant
      (kein Rampen-Timing simuliert, siehe `sim_hardware.py`), das
      eigentliche kontinuierliche Poll-während-Move-Verhalten (Punktdichte,
      Timing-Jitter, POS-Readback-Genauigkeit während echter Bewegung) ist
      NUR am echten Rig zu prüfen. Kein Pause/Resume in diesem Modus
      (bewusst, ein einzelner durchgehender Move).

- [ ] **`entry_current`-Desync-Bug gefixt, braucht Rig-Bestätigung:**
      `backlash_cal._move_rel_nm()` aktualisierte bisher nur
      `state["current_nm"]`, nie das GUI-Feld `entry_current`. Und
      `motion.reference_run_action()` aktualisierte GAR NICHTS (weder State
      noch Feld), obwohl der Reference Run den Motor real um
      `REFERENCE_MOVE_NM` bewegt. Problem: `scan_engine` benutzt für die
      nächste Goto/Scan-Delta-Berechnung das GUI-FELD als Quelle
      (`refs['entry_current'].get() or state["current_nm"]`), nicht
      `state["current_nm"]` allein -- nach Kalibrierung oder Reference Run
      war das Feld stehengeblieben, der nächste Goto/Scan zielte also
      lautlos auf die falsche absolute Wellenlänge. Fix: beide Stellen
      ziehen `entry_current` jetzt nach jedem erfolgreichen Move nach,
      exakt im selben delete/insert-Muster wie `scan_engine.py`. Xvfb-
      Start (offscreen) fehlerfrei geprüft; SIM/Rig-Funktionstest (Connect
      -> Reference Run -> Calibrate -> Goto, Feld live beobachten) steht
      noch aus.

- [ ] **Post-Connect-Workflow "Referenzfahrt? -> aktuelle λ bestätigen"
      -- Rig-Test aussteht:** Nach echtem Connect (nicht SIM) fragt die App
      jetzt per Dialog, ob eine Referenzfahrt laufen soll; danach (ob ja
      oder nein) wird die vom mechanischen Zeiger abgelesene Wellenlänge
      abgefragt und via `motion.set_current_wavelength()` neu verankert
      (dieselbe Offset-Logik wie die CALIBRATION-Sektion). `reference_run_
      action(on_done=...)` triggert den λ-Dialog erst NACH Abschluss der
      Referenzfahrt (die in einem Hintergrund-Thread läuft), marshaled über
      `ui_hook('ui_after', ...)`. Bewusst NICHT im SIM-Modus (kein
      mechanischer Zeiger zum Gegenchecken, hätte alle automatisierten
      SIM-Tests blockiert). SIM-getestet: `on_done`-Callback feuert korrekt
      mit Erfolgsstatus, λ-Dialog setzt bei Bestätigung/lässt bei Cancel
      unverändert. Am echten Rig noch nicht durchgeklickt.

- [ ] **`[RESYNC]`-Spam + ~14s Delay pro Move — UMGESETZT, Rig-Test offen.**
      Der Rig-Mitschnitt, auf den dieser Punkt gewartet hat, kam: nach dem
      `ANSW0` aus der Vorsession lief JEDES SET-Kommando (EN/HP0/V0/ANSW0/
      AC/DEC/SP/LR/M/ST) in den Timeout, POS/OST antworteten normal. Damit
      war klar, welche Kommandos im stillen Modus nie quittieren. Umgesetzt:
      `device_constants.NO_REPLY_COMMANDS` + `_is_no_reply_cmd()` in
      `send_cmd` -> fire-and-forget statt 1.2s Warten pro Kommando; Abfragen
      unverändert inkl. RESYNC. `MOTOR_ANSW_MODE` macht den ANSW-Wert
      konfigurierbar (!= 0 -> altes Verhalten für alle Kommandos).
      Unit+SIM-getestet, `init_motor()` 8.4s -> 0.07s gemessen.
      OFFEN am Rig: (1) Delay wirklich weg, (2) Moves fahren korrekt —
      LR/M werden jetzt sofort quittiert-los abgeschickt, das Warten macht
      allein `wait_until_position`; früher lagen zufällig ~1.2s Pause
      zwischen LR und M. Beim ersten Goto/Scan darauf achten, ob der Antrieb
      das ohne Zusatzpause sauber annimmt.
- [ ] Prüfen, ob `POLL_TIMEOUT` (0.10s) nach dem Ramp-Fix überhaupt noch
      angehoben werden muss.
- [ ] [SLIP]-Meldung nur bei tatsächlichem Reversal beobachten (sollte
      korrekt sein, aber am Rig nochmal gegenprüfen).

## P2 — bewusst erhaltene Alt-Bugs (Sicherheitsrelevanz, nur mit Rücksprache)

- [ ] `scan_engine._maybe_prompt_save_all_scans_after_queue`: `graph_data`
      undefiniert → Queue-Ende-Speicherprompt faktisch inaktiv. Entweder auf
      `plot_mgr`-basierte Prüfung umstellen oder Prompt bewusst entfernen.
- [ ] `motion._pos_wait_impl_fallback`: `last_ost`/`ost_baseline`/
      `ost_debounce` uninitialisiert → OST-Guard läuft faktisch nie. NUR mit
      Rig-Test aktivieren, da bisher nie aktiv war → Aktivierung ändert
      Laufzeitverhalten.

## P3 — Später / optional

- [ ] **Zwei-Punkt-Kalibrierung (Skalierung, `STEPS_PER_NM`):** NUR bauen,
      falls sich am Rig zeigt, dass der Referenzlinien-Fehler mit Abstand
      wächst (Skalierung), nicht konstant ist (Offset -> schon durch die
      CALIBRATION-Sektion abgedeckt). Design dann: zwei bekannte Linien
      messen, aus dem gemessenen nm-Abstand vs. Soll-Abstand ein korrigiertes
      STEPS_PER_NM (bzw. einen Skalierungsfaktor) ableiten und persistieren.
      Bewusst NICHT spekulativ vorgebaut -- ohne zwei echte Messpunkte wäre
      jede Steigungsänderung geraten und würde einen sichtbaren Fehler zu
      einem systematischen machen.
- [x] Plot-Fadenkreuz (Crosshair) im PS-2600A-Stil: teal V/H-Linien folgen
      der Maus über dem Scope, Header zeigt CURSOR λ (nm) + CURSOR V (aus dem
      aktiven Trace, nächster x). Reine Mess-/Inspektionshilfe, ändert keinen
      State, liest keine Hardware. In `gui_main.py` (`_on_plot_mouse_moved`
      via `pg.SignalProxy`).
- [x] Notstop-Button oben rechts im Header (neben STATE): großer, immer
      sichtbarer "■ EMERGENCY STOP" (Danger-Style), aktiv sobald verbunden,
      auf dieselbe `stop_action()` verdrahtet wie der SCAN-Stop -- nicht in
      einer Panel-Sektion vergraben, sofort erreichbar bei Fehlverhalten.
- [x] "Read while move"-Idee -> umgesetzt als `free_run_scan_action()`
      ("Free Run Scan"), siehe P1 oben für den offenen Rig-Test.
      `probe_move_timing.py` bleibt als eigenständiges Timing-Diagnose-Tool
      im Projekt (unabhängig nutzbar für künftige Timing-Fragen).
- [ ] Native Qt-Signals in `scan_engine` statt Adapter-Brücke (`refs`/
      `ui_hook`) — würde die Tk-kompatible Adapterschicht in `gui_main.py`
      obsolet machen. Größerer, sauberer Schnitt, kein akuter Bedarf.
- [ ] Kalibrier-Dwell auf ms umstellen (aktuell Sekunden, inkonsistent zum
      Haupt-Scan-Feld) — nur falls gewünscht.
- [ ] STATUS_FAULT_BIT/STATUS_TR_BIT-Vierfach-Dopplung weiter konsolidieren
      (nur Konstanten sind schon zusammengeführt, die 3 Audit-Funktionen
      bleiben bewusst getrennt) — nur mit Rücksprache.
- [ ] GUI-Feinschliff: weitere Readouts (Last V, Rate), Theme-Toggle,
      xcb-Plugin ist nur ein Sandbox-Thema (auf echter Zielmaschine kein
      Problem).
- [ ] `backlash_cal`: `seat`-Over-Travel und Peak-Schwelle im Dialog
      konfigurierbar machen, falls am Rig nötig.

## Referenz

Retired: `monochromator_python_code_vrs41.py` (ex-Monolith) ist NICHT Teil
des ausgelieferten Pakets, bleibt nur im Projekt als historische Referenz.
