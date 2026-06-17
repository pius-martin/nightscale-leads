# Find-Leads: Kriterien + Validierung + Editierbarkeit

## Problem
- Such-Formular hat nur Region/Max/Kategorien. Zusatzkriterien (Position, Größe,
  Umsatz, Standorte, Kette vs. Single, Refine-Vorfilter) lassen sich nicht vorab eingeben.
- Gefundene Lead-Kontakte sind nicht editierbar vor dem Import.
- Extrahierte Namen sind teils Unsinn (Regex greift zufällige Wortpaare) – keine Validierung.

## Plan
- [x] enrich.py: `plausible_name()` + `clean_name()` – nur echte Personennamen, sonst leeren.
      In FreeRegexEnricher und ClaudeEnricher anwenden.
- [x] app.py `_run_leadgen_job`: Namen als Safety-Net bereinigen; Kriterien im Job speichern.
- [x] app.py `leads_run`: Zusatzkriterien aus dem Formular parsen -> job["criteria"].
- [x] app.py `leads_import`: editierte Felder pro Zeile lesen (name/email/role/
      employees/revenue/locations/pos), serverseitig validieren; Fallback auf Snapshot.
- [x] leads.html Such-Formular: Kriterien-Felder (Rollen, Min. Mitarbeiter/Umsatz/Standorte,
      Betriebstyp Einzel/Kette, nur mit Email).
- [x] leads.html Ergebnis-Tabelle: editierbare Inputs (Name/Email/Rolle/Firmografik),
      Refine-Panel mit Vorbelegung aus Kriterien + Umsatz/Betriebstyp-Filter.
- [x] tests/smoke.py: plausible_name-Tests + editierter Import.

## Review
- Such-Formular hat jetzt einen ausklappbaren Kriterien-Block: Position/Rolle,
  Min. Mitarbeiter/Umsatz/Standorte, POS-System, Betriebstyp (Alle/Einzel/Kette),
  "nur mit Email". Diese filtern die Ergebnisse nach dem Enrichment und belegen die
  Refine-Filter vor (OSM kann nicht nach Firmografik suchen, daher Post-Filter).
- Ergebnis-Tabelle vollständig editierbar (Name/Email/Rolle/Mitarbeiter/Umsatz/
  Standorte/POS); Import liest die editierten Werte, validiert Email + Name serverseitig.
- Namensvalidierung (enrich.plausible_name/clean_name) verhindert Unsinns-Namen
  in Free- und Claude-Enricher sowie beim Import.
- Refine-JS um Umsatz- und Betriebstyp-Filter erweitert; liest editierte Zellwerte.
- tests/smoke.py: 83 Checks grün (inkl. neuer Namens- und Edited-Import-Tests).
- Gespeicherte Kontakte waren bereits voll editierbar — keine Änderung nötig.
