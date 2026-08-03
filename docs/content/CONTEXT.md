# CONTEXT.md — VRS41: technischer Tiefgang

> Verdichtung aus mehreren Chat-Exports + HANDOVER/BACKLOG/HANDOFF-Ständen.
> Stand: 2026-07-31 (Offset-Diagnose-Session eingearbeitet).
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

### Die drei Defekte im Detail

*(Checkliste und Fix-Reihenfolge in `BACKLOG.md` P0 — hier die Begründung.)*

**Defect A — Stepped Scan re-ankert nicht nach Abort.** Bei `if not
wait_until_position(...): safe_stop(); break` bleiben `current_nm`/`entry_current`
auf dem letzten *geplotteten* Punkt, während der Antrieb über die DEC-Rampe
weiterläuft. `free_run_worker` hat genau diese Rückführung
(`real_nm = s_nm + steps_to_delta_nm(final_pos - nm_anchor_steps)`) — der Fix
wurde nur an einem von zwei Pfaden gemacht.

**Defect B — Go To fährt nach Stop weiter, mit stalem Ursprung.** Nach
`[STOP] Motion interrupted` liest `goto_worker` erneut `entry_current.get()`
(nie aktualisiert), rechnet das Delta vom *ursprünglichen* Startpunkt und löst
eine zweite Bewegung aus → Overshoot ≈ bereits gefahrene Strecke, danach wird
das Ziel als Istwert geschrieben. Das ist der gemeldete Dial-vs-Software-Bug,
skaliert mit Move-Länge.

**Defect C — Kompensation im Ziel, nicht im Label** (wahrscheinlichste
Offset-Ursache). `comp = reversal_compensation_steps(direction)` geht in
`grid_slack_steps` → `target_abs`, aber **nicht** in `pos_nm` (Plot-/CSV-Label).
Korrekt *unter der Annahme*, dass Kompensation reines Spiel-Aufnehmen ohne
optische Bewegung ist. Ist die Annahme oder der Slip-Wert falsch, verschiebt
sich der ganze Scan starr um bis zu 1 Slip (0.080 nm). `last_move_direction`
wird von **jedem** Move mutiert → ein Jog zwischen zwei Scans kippt es.

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

### Hypothesen (rangiert)

| # | These | Signatur | Falsifikation |
|---|---|---|---|
| H1 | Defect C: Komp. im Ziel, nicht im Label | Offset = exakt 1 Slip; weg bei Slip=0 | T2 |
| H2 | Defect A: Abbruch vergiftet Ursprung | Offset nach Abbruch, **bleibt**; Betrag = Auslaufstrecke, nicht Slip | T3 |
| H3 | Jog kippt `last_move_direction` | Offset nach Jog, bleibt | T1 |
| H4 | echte optische Bewegung | Offset auch im **Encoder**-Frame | Diag-Patch |
| H5 | etwas anderes im manuellen Workflow | nichts reproduziert | alle Tests negativ |

H1/H2/H3 erzeugen alle „innerhalb kongruent, zwischen Gruppen starr versetzt".
Unterschieden werden sie über den **Auslöser** und darüber, **ob der Betrag
gleich dem Slip ist**.

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

### Testprotokoll

| Test | Zweck |
|---|---|
| T1 | Offset ohne Avg-Änderung provozieren: 2 Baseline-Scans → **GUI-Jog** +/− → 2 Scans → 1 Scan. Prüft H3 |
| T2 | Slip-Feld = 0, T1 wiederholen. Offset weg → H1; Offset bleibt → H1 tot |
| T3 | 2 Baseline → langer Go To ≥5 nm mit Stop mittendrin → 2 Scans ohne Korrektur. Prüft H2 |
| T4 | Go To ≥20 nm, Stop bei ~50 %, Dial vs. Software protokollieren. Defect B |
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
