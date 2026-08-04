# CONTEXT.md — VRS41: technischer Tiefgang

> Verdichtung aus mehreren Chat-Exports + HANDOVER/BACKLOG/HANDOFF-Ständen.
> Stand: 2026-08-04 (SIM-Diagnose- und Fix-Session eingearbeitet — Defect
> A/B code-seitig behoben und in SIM verifiziert, Defect C mit Re-Anchor
> entschärft, ein vierter Fund (Stop/GoTo-Race) dabei entdeckt und ebenfalls
> behoben. Alles NUR in SIM verifiziert — Rig-Session steht noch aus).
>
> **Abgrenzung:** Architektur und rig-bestätigter Stand stehen in `HANDOVER.md`,
> die Aufgabenliste in `BACKLOG.md`. Hier steht nur, was zum *Verstehen* der
> laufenden Offset-Diagnose nötig ist — Zahlen, Strukturschwächen, Hypothesen,
> Messverfahren.

## Skalen (verifiziert gegen `device_constants.py`)

| Größe | Schritte |
|---|---|
| 1 nm | 361 765 |
| Scan-Schritt 0.01 nm | 3 618 |
| 1 Slip (0.080 nm) | 28 941 |
| beobachteter Offset (~0.07 nm) | ~25 300 |
| `POS_TOL_STEPS` (Ankunftstoleranz) | 90 (≈ 0.25 pm) |

`INVERT_DIRECTION = True`, `SAFE_AFTER_MOVE = True`, `USE_STATUSWORD = False`.

**Korrektur an einer früheren Annahme:** `POS_TOL_STEPS = 90` (≈0.25 pm),
**nicht** ~3617 Schritte. Der historische Kommentar in `scan_worker` beschreibt
den Zustand *vor* dem Abs-Grid-Fix vom 23.07.

## Drei Positions-Wahrheiten (Architektur-Schwachstelle)

| Quelle | Was | Verwendung |
|---|---|---|
| `read_position()` | Encoder, einzige Wahrheit | nur für `target_abs` |
| `state['current_nm']` | Buchhaltung = **befohlenes** Ziel | Anzeige, Plot-Label, CSV |
| `entry_current.get()` | GUI-Textfeld, auf 3 Dezimalen gerundet | **Ursprung jeder Delta-Rechnung** |

Muster in `goto_worker` und im Sprung-zum-Start von `scan_worker`: Encoder wird
gelesen, das Delta kommt aber aus dem gerundeten GUI-String. Rundung selbst ist
vernachlässigbar (1 pm); strukturell folgt: **jeder Fehler, der je in dieses Feld
gerät, ist permanent und wird von jedem Folge-Move geerbt.**

## Jog ist ein Go To

`jog_plus_action()`/`jog_minus_action()` schreiben ins Goto-Feld und rufen
`goto_wavelength_action()`. Ein Jog durchläuft also denselben Pfad inkl.
`entry_current`-Ursprung und `reversal_compensation_steps()` — und mutiert
`last_move_direction`. Für Tests: **GUI-Jog-Buttons benutzen**, kein Relativ-Move.

---

## Offset-Diagnose ~0.07 nm zwischen Scan-Gruppen

### Beobachtung

16 Scans, 587.5→588.2 nm, Step 0.01, immer gleiche Fahrtrichtung, Rückfahrt
zwischen den Scans. Innerhalb einer Gruppe deckungsgleich, zwischen den Gruppen
**starr verschoben** um ~0.07 nm — gleiche FWHM, gleiche Flanken, nur
translatiert. Gruppengrenze fällt mit dem Wechsel Avg-Fraction 1 % → 50 %
zusammen (15:12:01 → 15:12:42). Drei Traces bei 15:10:5x sind Abbrüche
(4–5 s Abstand, ein 71-Punkte-Scan kann das nicht).

### Ausgeschlossen (mit Begründung, nicht per Bauchgefühl)

- **Avg-Fenster.** Statiktest: im Stillstand kriecht nichts. Messdauer kann die
  Wellenlänge nur beeinflussen, wenn sich während der Messung etwas bewegt.
  Gilt unabhängig von Timebase/Pre-settle → ersetzt das frühere Rechenargument.
  Korrelation mit dem Parameterwechsel ist real, aber nicht kausal.
- **Thermodrift.** Deckungsgleichheit über 8 bzw. 14 min ist mit monotoner Drift
  unvereinbar.
- **Backlash-Streuung.** Streuung ist zufällig; hier ist es eine Stufe, die hält.
- **Detektor-/RC-Zeitkonstanten.** Punktzeit ~0.9 s (bewegungsdominiert); ein
  Fensterunterschied von ~29 ms ist <3 %. Für 0.07 nm Differenz bräuchte es
  ~2.3 nm Grundlag. Größenordnung absurd.
- **Mechanische Relaxation nach Positionsmeldung.** War Leithypothese, vom
  Statiktest widerlegt.

### Die drei Defekte im Detail — Stand nach der SIM-Fix-Session 2026-08-04

*(Checkliste und Status in `BACKLOG.md` P0 — hier die Begründung. Alles unten
NUR in SIM verifiziert, mit `sim_defect_probe.py`, Ergebnisse dort im Kopf-
Docstring zusammengefasst. Rig-Session steht noch aus — SIM beweist die
Codepfade, nicht reale Motor-Timing/`LR`-Sollwert-Feinheiten, s. HANDOVER.md.)*

**Defect A — Stepped Scan re-ankert nicht nach Abort. BEHOBEN.** Bei `if not
wait_until_position(...): safe_stop(); break` blieben `current_nm`/
`entry_current` auf dem letzten *geplotteten* Punkt, während der Antrieb über
die DEC-Rampe weiterlief. `free_run_worker` hatte genau diese Rückführung
(`real_nm = s_nm + steps_to_delta_nm(final_pos - nm_anchor_steps)`) —
`scan_worker` jetzt auch, an der `break`-Stelle selbst (mit dem *nur
bestätigten* Slack-Anteil vor diesem Schritt ausgeschlossen, da bei einem
Abbruch nicht bekannt ist, wie viel des tatsächlichen Teilwegs Spiel-Aufnahme
war). Zusätzlich am `while`-Schleifenende (`else:`-Zweig, läuft nur bei
normalem Abschluss, nie nach `break`) eine Verifikation gegen die reale
Encoder-Position eingebaut — dieselbe Lücke gab es identisch in `do_resume()`,
dort ebenfalls behoben. SIM-Ergebnis: `label - true_nm nach Stop = +0.00000`.

**Defect B — Go To fährt nach Stop weiter, mit stalem Ursprung. BEHOBEN.**
Nach `[STOP] Motion interrupted` las `goto_worker` erneut `entry_current.get()`
(nie aktualisiert), rechnete das Delta vom *ursprünglichen* Startpunkt und löste
eine zweite Bewegung aus → Overshoot ≈ bereits gefahrene Strecke, danach wurde
das Ziel als Istwert geschrieben. Fix: ein Anker-Paar (Schritte, nm) wird am
Anfang von `goto_worker` erfasst; im Interrupted-Zweig wird die wahre aktuelle
Wellenlänge daraus (nicht aus dem stalen Textfeld) berechnet, das Label sofort
aktualisiert, und erst dann das Retry-Delta gebildet. SIM-Ergebnis (8-nm-Move,
Stop bei ~33 %): Label sprang vorher fälschlich nicht mit; jetzt korrekt
`523.000 → 525.720 nm`, Retry erreicht danach sauber das echte Ziel.
**Offen:** `LR` ist laut Handbuch relativ zum letzten *kommandierten*
Sollwert, nicht zur Istposition (s. HANDOVER.md) — SIMs `FakeFaulhaber`
bildet diese Feinheit nicht nach, das kann nur ein Rig-Test zeigen (T4).

**Vierter Fund (beim Verifizieren von B entdeckt) — Stop/GoTo-Race. BEHOBEN.**
`stop_action()` wartete vor dem Re-Arm (`recover_after_stop()`, löscht
`stop_flag`) nur auf `state['is_scanning']` — das setzt aber nur Scan/Free-Run,
nie GoTo. Ein Stop während eines GoTo löschte `stop_flag` fast sofort, oft
BEVOR `goto_worker`s eigene `wait_until_position()`-Poll-Schleife ihn je True
sah — das Log zeigte sofort „Recovered after STOP; ready.", während der GoTo
im Hintergrund einfach weiterlief, bis sein volles Move-Timeout ablief (bis zu
`MAX_MOVE_TIMEOUT` = 900 s). Per Live-Thread-Stack-Dump in SIM verifiziert
(`goto_worker` stand 18+ s nach dem Stop noch in `wait_until_position()`).
Fix: neues `state['is_moving']`-Flag (analog `is_scanning`), von `goto_worker`
gesetzt/gelöscht, `stop_action()` wartet jetzt auf beide. Ein direkt damit
zusammenhängender zweiter Bug: `recover_after_stop()` setzt `fsm` unbedingt
auf `IDLE` — wenn `goto_worker` es beim Auto-Recover-Retry selbst aufruft,
zeigte `fsm` fälschlich „IDLE", während der Retry-Move noch lief. Auch behoben
(`set_fsm("MOVING")` direkt nach dem `recover_after_stop()`-Aufruf im
Interrupted-Zweig).

**Quantitative Bestätigung der C-Herleitung (2026-08-04).** Nach dem
Slack-first-Fix (s.u.) misst `sim_defect_probe.py` für einen abgebrochenen
Reversal-GoTo `label − optische Wahrheit = −0.00874 nm`. Das ist exakt
`slip_konfiguriert (0.09074 nm) − backlash_real (SIM_BACKLASH_NM = 0.082 nm)`.
Der Fehler skaliert also tatsächlich mit der **Kalibrierabweichung**, nicht
mit dem Backlash — die vorher nur algebraisch hergeleitete Aussage ist damit
gemessen. Praktische Folge: der verbleibende Offset lässt sich ausschließlich
über eine `slip_nm`-Kalibrierung am Rig (`backlash_cal.py`) verkleinern, nicht
über weiteren Code.

**Zwei Modell-/Schwellenfehler aus dem Review, ebenfalls behoben:** (1) die
Log-Schwelle der Erfolgs-Re-Anchors lag mit `1e-4` nm unter der
Ankunftstoleranz `POS_TOL_STEPS` (90 Schritte ≈ 2.49e-4 nm) und ist jetzt
als `REANCHOR_LOG_TOL_NM` daraus abgeleitet; (2) der Teilweg-Re-Anchor
behandelte den gesamten Encoder-Weg als optische Bewegung, obwohl das Spiel
laut Modell **zuerst** aufgenommen wird — jetzt über `_slack_consumed_steps()`
korrekt aufgeteilt. Ein Stop innerhalb des Spiels lässt das Label damit
korrekt stehen (SIM: Encoder +0.028 nm, Optik 0.000 nm, Label 0.000 nm),
statt es um bis zu 1 Slip springen zu lassen.

**Nachtrag — vier Folgefehler aus dem Selbst-Review (behoben, SIM-verifiziert).**
(A) `read_position()` gibt bei Timeout/Garbage still `0` zurück und wirft nie
(`protocol_faulhaber.py`) — die neuen Re-Anchor-Stellen schrieben diesen Wert
in `state['current_nm']`, also in den Ursprung jeder Folge-Delta-Rechnung; im
SIM-Gegentest hätte das alte Verhalten 4.0 nm danebengelegen, mit der
Sessiondauer wachsend (POS läuft frei). Jetzt zentral über
`_encoder_nm_or_none()` abgesichert. (B) Der Auto-Recover-Retry in
`goto_worker` unterschied Nutzer-Stop nicht von Timeout/Fault und fuhr nach
einem Stop trotzdem ans Originalziel — belegt im SIM-Log der Vorsession
(`[DONE] Reached 531.000 nm (after auto-recover)`), damals als Erfolg
fehlgelesen. (C) `reference_run_action` setzte kein Busy-Flag → Stop während
einer Referenzfahrt lief weiter in die alte Race. (D) Go To hatte keinen
Reentrancy-Guard → zwei parallele Worker möglich, `is_moving` dadurch
unzuverlässig. Alle vier behoben; Details in `BACKLOG.md` P0.

**Defect C — Kompensation im Ziel, nicht im Label. TEILWEISE ENTSCHÄRFT, KEIN
vollständiger Fix.** `comp = reversal_compensation_steps(direction)` geht in
`grid_slack_steps` → `target_abs`, aber **nicht** in `pos_nm` (Plot-/CSV-Label).
Das ist *by design* korrekt, solange die Annahme stimmt, dass Kompensation
reines Spiel-Aufnehmen ohne optische Bewegung ist — und genau das war die
offene Frage.

Sauber in SIM durchgerechnet (nicht nur behauptet, s. `sim_defect_probe.py`
Kopf-Docstring): Bei jedem Reversal-Move, der die modellierte Backlash-Breite
voll überschreitet, konvergiert `sim_hardware`s optische Position **immer**
gegen `(pos_vorher + kommandiertes_delta)` — **unabhängig vom verwendeten
Kompensationswert**. Der Label-vs-Optik-Fehler skaliert also mit
`(konfigurierter slip_nm − echter Backlash)`, **nicht** mit dem Backlash
selbst. Die erste, scheinbar sehr saubere Messung dieser Session
(`label - optical_truth = +0.082 nm`, exakt `SIM_BACKLASH_NM`) war dadurch
verunreinigt, dass genau in diesem Testlauf der bereits katalogisierte,
bewusst nicht angefasste P3-Bug „Scan überfährt End um einen Step"
(`BACKLOG.md` P3) mit hineinspielte — die Messung lief nicht am wahren
Scan-Ende, sondern einen Schritt zu weit.

**Was tatsächlich gefixt wurde:** Re-Anchor (nicht Blind-Addition) — nach
jedem erfolgreichen GoTo und am Ende jedes Scans wird das Label gegen die
reale (kompensations-bereinigte) Encoder-Position verifiziert, nach demselben
Muster wie `free_run_worker`s bestehende Rückführung (`nm_anchor_steps =
start_steps_abs + comp_steps`, um Slack-Schritte aus der nm-Rechnung
auszuschließen — genau das, was den historischen Free-Run-Bug
`587.000 → 586.918 nm` = 1 Slip behoben hatte). Explizit **nicht** gewählt:
`comp` blind ins Label addieren — das wäre bei korrekt kalibriertem
`slip_nm` falsch und würde den historischen Free-Run-Bug wieder aufmachen.

**Warum das KEIN vollständiger Fix ist:** Für einen Move, der sein
kommandiertes Ziel exakt erreicht, ist die reine Grid-Arithmetik (`pos_nm =
s_nm + step×index`) algebraisch bereits identisch mit der
kompensations-bereinigten Encoder-Umrechnung — der Re-Anchor ist dann ein
No-Op (bestätigt in SIM). Er fängt echte `POS_TOL_STEPS`-Anlauf-Toleranz ab
und dient als Verifikations-/Log-Sicherheitsnetz, kann aber eine
`slip_nm`-Fehlkalibrierung strukturell **nicht** beseitigen — dafür gibt es
auf echter Hardware keine unabhängige optische Rückmeldung (kein
Absolutsensor, s. „Zwei Eigenschaften" in HANDOVER.md). Der real am Rig
beobachtete ~0.07 nm-Offset kann also NUR über eine Rig-Kalibrierprüfung von
`slip_nm` (`backlash_cal.py`, gegen den echten Backlash) weiter reduziert
werden — das ist der eigentliche nächste Schritt für C, kein Code-Fix mehr.

Korroboration aus dem Code selbst: der Free-Run-Fix-Kommentar nennt
`587.000 → 586.918 nm` = **0.082 nm** = 1 Slip. Dieselbe Fehlerklasse gab es in
dieser Codebase schon einmal, in einem anderen Pfad.

### Schlüsselfund: POS ist frei laufend

`init_motor()` sendet `EN`/`V0`/`ANSW`/Ramp, `recover_after_stop()` nur
`ST`/`EN`/`V0`. **Kein `HO` irgendwo im Protokoll-Layer.** → `POS` ist
Absolutzähler über die ganze Session bis Power-Cycle. Rohwerte sind ohne
Anker-Arithmetik scan-übergreifend vergleichbar. Das ist die Grundlage der
gesamten Diagnose.

**Caveat:** `POS` ist der Encoder. Er beweist Servo-Positionierung, nicht
Gitterbewegung. Ein Nullbefund im Encoder-Frame schließt Stiction nicht aus —
das bräuchte einen optischen Test.

### Hypothesen (rangiert) — Stand vor der Rig-Session, jetzt Fix-Verifikation statt Diagnose

| # | These | Signatur | Falsifikation | Code-Status |
|---|---|---|---|---|
| H1 | Defect C: Komp. im Ziel, nicht im Label | Offset = exakt 1 Slip; weg bei Slip=0 | T2 | Re-Anchor ergänzt, Rest ist Kalibriersache (s. oben) |
| H2 | Defect A: Abbruch vergiftet Ursprung | Offset nach Abbruch, **bleibt**; Betrag = Auslaufstrecke, nicht Slip | T3 | Behoben (SIM) |
| H3 | Jog kippt `last_move_direction` | Offset nach Jog, bleibt | T1 | Unverändert — Jog ist ein Go To, profitiert vom B-Fix |
| H4 | echte optische Bewegung | Offset auch im **Encoder**-Frame | Diag-Patch | Kann `sim_hardware` naturgemäß nicht ausschließen — Rig-Frage |
| H5 | etwas anderes im manuellen Workflow | nichts reproduziert | alle Tests negativ | — |

H1/H2/H3 erzeugen alle „innerhalb kongruent, zwischen Gruppen starr versetzt".
Unterschieden werden sie über den **Auslöser** und darüber, **ob der Betrag
gleich dem Slip ist**. Da A und B jetzt code-seitig behoben sind, dient die
ausstehende Rig-Session nicht mehr primär der Unterscheidung, sondern der
**Verifikation**, dass der reale ~0.07 nm-Offset dadurch tatsächlich
verschwindet bzw. sich auf die `slip_nm`-Kalibrierabweichung reduziert (H1-Rest).

### Entscheidender Messansatz

Peaklage allein trennt Label-Fehler nicht von echter Bewegung. Nötig sind zwei
Frames, einer davon unabhängig von der Buchhaltung:

- `nm_app` = Label, `start + step × index` (wie `scan_engine` rechnet)
- `nm_enc` = aus `POS`, Anker **einmal** pro Session

Peak per **Schwerpunkt der oberen Hälfte**, nicht argmax — bei 1 DAQ-Read/Punkt
springt argmax zwischen Nachbarpunkten (10 pm Eigenrauschen auf 70 pm Effekt).

### Entscheidungstabelle für die `[DIAG]`-Auswertung

| Befund | Schluss |
|---|---|
| `pos` am Peak konstant, `label_nm` springt | Software-Buchhaltung (H1/H2/H3) |
| `pos` am Peak springt mit | Mechanik/Antrieb (H4) |
| nichts springt | H5 |
| `lbl_minus_enc_nm` wächst innerhalb eines Scans | Kompensation greift mitten im Scan (unerwartet) |
| `scan-start` zeigt unterschiedliches `last_move_direction` je Gruppe | H3 direkt bestätigt |
| `reads=1` in beiden Gruppen | Avg-Wechsel war wirkungslos, aus Daten belegt |

**Erwartete Signatur falls H1 stimmt:** `slack` bleibt scanweit 0 (Richtung dreht
innerhalb eines Scans nie) · `[SLIP]`-Zeile erscheint vor der einen Gruppe, vor
der anderen nicht (kommt vom Sprung-zum-Start, nicht aus der Scan-Schleife) ·
`pos` am Peak differiert um ~28 941 Schritte · `label_nm` am Peak **identisch**.

### Testprotokoll — jetzt Fix-Verifikation (T3/T4 sollten nach dem A/B-Fix negativ ausfallen)

| Test | Zweck |
|---|---|
| T1 | Offset ohne Avg-Änderung provozieren: 2 Baseline-Scans → **GUI-Jog** +/− → 2 Scans → 1 Scan. Prüft H3, verbleibende C-Restgröße |
| T2 | Slip-Feld = 0, T1 wiederholen. Offset weg → H1 vollständig entkräftet; Offset bleibt → Rest ist mechanisch oder H3 |
| T3 | 2 Baseline → langer Go To ≥5 nm mit Stop mittendrin → 2 Scans ohne Korrektur. **Sollte nach dem A-Fix keinen bleibenden Offset mehr zeigen** — wenn doch, ist der SIM-Fix am Rig unvollständig |
| T4 | Go To ≥20 nm, Stop bei ~50 %, Dial vs. Software protokollieren. **Sollte nach dem B-Fix + Stop/GoTo-Race-Fix sofort abbrechen** (nicht erst nach dem vollen Move-Timeout) und korrekt re-ankern — genau die `LR`-Sollwert-Feinheit prüfen, die SIM nicht abbildet (s. HANDOVER.md) |
| T5 | Konsolen-Log nach `[SLIP] Reversal` grep'en — entschiede H1 ohne Rig-Zeit, **aber das Log vom 29.07. wurde nicht mitgeschnitten** → aktuell nicht durchführbar |

Für T1 die **GUI-Jog-Buttons** benutzen (s. „Jog ist ein Go To" oben) — ein
reiner Relativ-Move testet etwas anderes.

**Vorbedingungen:** Lampe ≥30 min warm · nur ein Programm am COM-Port ·
Referenzfahrt nach Connect (sonst ist `last_move_direction` undefiniert) ·
Konsolen-Log in Datei mitschneiden · DAQ = NI-DAQ, nicht SIMULATED ·
**Timebase ≥200 ms, Pre-settle ≥50 ms** (bei 10 ms überschreitet ein einzelner
DAQ-Read von ~29 ms das Budget um 3×, die Avg-Settings sind dann wirkungslos).

### Grenze von `offset_probe.py`

Fährt `gui_main`/`scan_engine` **nicht** (Qt-Widgets verschweißt; Stubs kämen
sonst in den Messpfad). Beweist Physik + Arithmetik, **nicht** was `gui_main`
tut. Defect A/B brauchen T3/T4 + Log.

### `sim_defect_probe.py` (neu, 2026-08-04) — fährt `gui_main` doch

Anders als `offset_probe.py`: importiert `gui_main` headless unter
`QT_QPA_PLATFORM=offscreen` (kein Xvfb nötig, macOS hat das nicht — Qts
eigenes Offscreen-Backend reicht) und ruft `scan_engine.goto_wavelength_action()`
/`scan_action()`/`stop_action()` **direkt** auf den echten, laufenden
Objekten auf — kein Stub im Messpfad, weil gar nichts gestubbt wird. Ground
Truth kommt direkt aus `state['ser']._pos`/`._optical_pos` (nur in SIM
verfügbar), umgerechnet über die echte `motion.steps_to_delta_nm()`.
Watchdog per `faulthandler.dump_traceback_later()` pro Sektion — ein
hängender Thread dumpt seinen Stack und die Sektion bricht hart ab, statt
still zu hängen (damit wurden sowohl der Stop/GoTo-Race-Bug als auch mehrere
Timing-Fallstricke im Test selbst gefunden, s. Kopf-Docstring der Datei).
**Grenze:** beweist Codepfade und Bookkeeping, nicht reale Motor-Timing oder
die `LR`-Sollwert-Feinheit nach einem harten Stop (s. HANDOVER.md) — dafür
bleibt die Rig-Session nötig.

---

## Helium-Spektrallinien (Referenz)

- 587.56 nm (Luft) = He-D3 (2p³P→3d³D), **unaufgelöstes Triplett**, Komponenten
  nur ~0.03–0.35 nm auseinander → am VRS41 (>100 pm) nicht trennbar. Die drei
  vermuteten Sublinien bei 587.915/929/942 waren Rauschen.
- Vakuum/Luft-Umschaltpunkt der NIST-Tabelle: 200 nm. 587.56 nm ist Luftwert
  (587.73 nm Vakuum) — 0.17 nm Unterschied, kalibrierrelevant.
- Saubere isolierte Kalibrierlinien: 388.86 / 447.15 / 501.57 / 587.56 /
  667.82 / 706.52 nm.
- 584 nm (NIST) war 584.334 **Å** = 58.4 nm (Vakuum-UV) — Einheiten-Verwechslung
  Å vs. nm.

## Quellen (Session-Exports, chronologisch grob)

- `Split_app_UI_layout_issues_*` (HANDOVER/BACKLOG/HANDOFF 2026-07-22) — Qt6-Lift
- `While_move_Daten_einlesen_vom_DAQ_HANDOFF_2026-07-22b/c/d/e` —
  Free-Run-Entwicklung, POS-Spike-Debugging, `e` ist gültig (ersetzt b/c/d)
- `Prüfe_mal_alle_drei_Dateien_al..._*` — Monolith-Analyse vor Modul-Split;
  durch den Modul-Split abgelöst, wird nicht weiterverfolgt (s. `BACKLOG.md`,
  Abschnitt Referenz)
- `Helium-*.md` — Kalibrierlinien-Referenz
- Offset-Diagnose-Session 2026-07-29/31 — Defect A/B/C, `stage1_diag.patch`,
  `offset_probe.py`, beide PDFs
- SIM-Diagnose- und Fix-Session 2026-08-04 — Defect A/B code-seitig behoben,
  Defect C mit Re-Anchor entschärft (kein vollständiger Fix, s. oben),
  Stop/GoTo-Race + fsm-während-Retry-Bug gefunden und behoben,
  `sim_defect_probe.py` erstellt. Alles NUR SIM-verifiziert.
