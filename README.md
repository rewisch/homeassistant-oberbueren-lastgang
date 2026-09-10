# Strom Oberbüren Lastgang — Home-Assistant-Integration

Eine benutzerdefinierte Integration, die 15-Minuten-Stromlastgangdaten von
[strom.oberbueren.ch](https://www.strom.oberbueren.ch?utm_source=chatgpt.com) in Home Assistant importiert
— für das Energie-Dashboard, für die Kostenverfolgung und für Lovelace-Dashboards.

## Funktionen

* **Langzeitstatistiken** — täglicher Import der 15-Minuten-Werte des Vortags,
  zu stündlichen kWh zusammengefasst und als External Statistics gespeichert, sodass sie die Recorder-Bereinigung überstehen und im **Energie-Dashboard** erscheinen (kWh + CHF).
* **Kostenberechnung** — jede importierte Stunde wird anhand einer vom Benutzer bearbeitbaren Schweizer Tarifdatei berechnet (HT/NT, Netznutzung, Energiebezug, Abgaben, Messtarif), inklusive Aufschlüsselung nach Kategorien und `cost_total` für das Preisfeld des Energie-Dashboards.
* **18 Dashboard-Sensoren pro Zähler** — Verbrauch & Kosten für Aktueller Monat / Letzter Monat / Aktuelles Jahr / Letztes Jahr / Gestern / Letzte 7 Tage / Letzte 30 Tage sowie Monatsprognose, Jahresprognose, Ø Tagesverbrauch, Ø Preis.
* **Automatisches Nachholen fehlender Daten** — wenn HA beim täglichen Abruf offline war (oder mehrere Tage), werden fehlende Tage automatisch beim nächsten Start oder beim nächsten Poll-Trigger importiert.
* **Sichere erneute Ausführung** — erneutes Ausführen von `backfill` über bereits in HA vorhandene Daten erkennt Überschneidungen, führt die Daten zusammen und erstellt die kumulative Summenkette sauber neu — kein „Fix issues in Statistics“-Workaround mehr nötig.

## Installation

### Über HACS (empfohlen)

1. In HACS: **Integrationen → ⋮ Menü → Benutzerdefinierte Repositories**.
2. Repository: `https://github.com/rewisch/homeassistant-oberbueren-lastgang`,
   Kategorie: **Integration**, hinzufügen.
3. Die Integration „Strom Oberbüren Lastgang“ erscheint in HACS →
   **Herunterladen**.
4. Home Assistant neu starten.
5. **Einstellungen → Geräte & Dienste → Integration hinzufügen → „Strom Oberbüren Lastgang“**.
6. URL (Default `https://www.strom.oberbueren.ch`), E-Mail und Passwort eingeben, dann `objektId` und `meteringcode` deines Zählers angeben
   (beides findest du in der URL der Lastgangdaten-Seite im Browser).

### Manuelle Installation

1. `custom_components/oberbueren_lastgang/` aus diesem Repository nach
   `<config>/custom_components/` deiner HA-Installation kopieren.
2. Home Assistant neu starten.
3. Schritte 5–6 oben ausführen.

## Täglicher Auto-Import

Nach der Einrichtung importiert die Integration die Daten des Vortags automatisch — standardmäßig zu je einem Versuch um **06, 07, 08 und 09 Uhr Ortszeit**. Jeder Slot prüft die letzten drei Tage rollend auf Vollständigkeit und ergänzt fehlende Stunden, falls der Upstream sie inzwischen veröffentlicht hat; ein bereits vollständiger Tag wird übersprungen, gespeicherte Daten werden bei einem Upstream-Fehler oder leeren Response nie überschrieben.

Falls dein HA-Host während aller Slots offline war (oder mehrere Tage), erkennt ein Catch-up beim Start die Lücke und lädt die fehlenden Tage automatisch nach — begrenzt auf 30 Tage. Längere Ausfälle benötigen einen manuellen `backfill`-Aufruf.

Ein erneutes Ausführen von `backfill` über einen Zeitraum, der bereits in HA vorhanden ist, ist **sicher**:
Die Integration erkennt die Überschneidung, führt die neuen Daten mit den bereits gespeicherten zusammen und erstellt die kumulative Summenkette vollständig neu.
Kein manueller „Fix issues in Statistics“-Workaround mehr nötig.

## Einstellungen nachträglich ändern

Auf der Integrations-Kachel (**Einstellungen → Geräte & Dienste → Strom Oberbüren Lastgang**) gibt es zwei Buttons:

* **Konfigurieren** — im Options-Dialog:
  * *Poll-Zeiten* auswählen (Multi-Select 01–23, Default `6, 7, 8, 9`).
  * *Ausführliches Debug-Logging* — schreibt Request-Parameter, HTTP-Status, Fehlerbody-Ausschnitte und Antwortgrössen ins Log. Passwort wird nicht geloggt.
  * *Tarifdatei automatisch verwalten* (Default an) — hält `oberbueren_lastgang_tariffs.yaml` mit den mitgelieferten Standardtarifen synchron. Ausschalten, um die Datei selbst zu pflegen.
* **Neu konfigurieren** — URL, E-Mail, Passwort, Anzeigename und Zähler-IDs ändern. Die Zugangsdaten werden beim Speichern erneut gegen die angegebene URL geprüft.

Nach dem Speichern wird die Integration automatisch neu geladen — kein HA-Neustart nötig.

## Initiales Backfill

Beim ersten Einrichten möchtest du wahrscheinlich mehrere Monate oder Jahre an Verlaufsdaten importieren. Verwende dafür den Dienst `oberbueren_lastgang.backfill`:

```yaml
service: oberbueren_lastgang.backfill
data:
  entry_id: 01HX9Z7E8K2QY7CDXR...    # siehe Einstellungen → … → Show entry ID
  start_date: 2024-01-01
  end_date: 2024-12-31
```

Tipp: Führe das Backfill in chronologischen Blöcken aus (z. B. jeweils ein Jahr), damit sich die kumulativen kWh-Summen korrekt aufbauen. Die Integration sendet eine HTTP-Anfrage pro Tag, daher benötigt ein ganzes Jahr etwa 365 Anfragen. Sei höflich und überlaste die API nicht.

Ein Tag wird übersprungen, wenn die gespeicherten Daten bereits mindestens so viele Stunden **und** so viel Energie enthalten wie der frische Abruf. Falls ein Tag früher importiert wurde, während der Upstream noch Null-Platzhalter lieferte (voller Stundensatz, aber fast nur Nullwerte), heilt ein erneuter `backfill` diesen Tag automatisch, sobald echte Werte veröffentlicht sind. Mit `force: true` werden alle Tage im Bereich bedingungslos neu importiert:

```yaml
service: oberbueren_lastgang.backfill
data:
  entry_id: 01HX9Z7E8K2QY7CDXR...
  start_date: 2024-01-01
  end_date: 2024-12-31
  force: true
```

## Catch-up manuell auslösen

Zum Testen oder zum manuellen Wiederholen nach einem temporären API-Fehler kannst du denselben Catch-up auslösen, der sonst beim HA-Start und zu den konfigurierten Poll-Zeiten läuft:

```yaml
service: oberbueren_lastgang.catch_up
data:
  entry_id: 01HX9Z7E8K2QY7CDXR...
```

Dieser Dienst aktualisiert das rollende Fenster der letzten Tage und füllt erkannte Lücken bis gestern. Er ist deshalb näher am automatischen Betrieb als ein frei gewähltes Backfill-Datum.

## Kosten neu berechnen ohne erneuten Download

Wenn du `oberbueren_lastgang_tariffs.yaml` bearbeitest
(Preisänderung, neue Tarifperiode, Tippfehler korrigiert) oder kWh-Daten mit einer Version importiert hast, die die Kostenfunktion noch nicht unterstützte, kannst du **alle** Kostenstatistiken aus den vorhandenen kWh-Statistiken neu erstellen — ganz ohne HTTP-Anfragen:

```yaml
service: oberbueren_lastgang.recompute_costs
data:
  entry_id: 01HX9Z7E8K2QY7CDXR...
```

Der Dienst liest jeden verfügbaren stündlichen kWh-Zuwachs aus dem HA-Recorder, wendet die aktuelle Tarifdatei an und überschreibt die sechs Kostenstatistiken vollständig neu (Anker = 0, frische kumulative Kette).
Die Ausführung kann beliebig oft wiederholt werden.

## Was in HA angelegt wird

### Langzeitstatistiken (External Statistics)

Für jeden konfigurierten Zähler erstellt die Integration folgende Statistiken — sie überstehen die Recorder-Bereinigung und bilden die Grundlage des Energie-Dashboards:

| Statistik-ID                                                    | Bedeutung                                          | Einheit |
| --------------------------------------------------------------- | -------------------------------------------------- | ------- |
| `oberbueren_lastgang:objekt_<id>_bezug`                         | kumulativer Verbrauch                              | kWh     |
| `oberbueren_lastgang:objekt_<id>_leistung`                      | Leistung pro Stunde (Mittel + Min/Max)             | kW      |
| `oberbueren_lastgang:objekt_<id>_cost_netznutzung_wirkstrom`    | Netznutzung Wirkstrom (HT/NT bzw. Einheitstarif)   | CHF     |
| `oberbueren_lastgang:objekt_<id>_cost_netznutzung_grundgebuehr` | Grundgebühr (Fix)                                  | CHF     |
| `oberbueren_lastgang:objekt_<id>_cost_netznutzung_leistung`     | Leistungspreis: Monatsspitze × CHF/kW (ab 2027)    | CHF     |
| `oberbueren_lastgang:objekt_<id>_cost_energiebezug_wirkstrom`   | Energiebezug Wirkstrom (HT/NT bzw. Sommer/Winter)  | CHF     |
| `oberbueren_lastgang:objekt_<id>_cost_energiebezug_zuschlaege`  | SDL + Stromreserve + Solidarisierte + Netzzuschlag | CHF     |
| `oberbueren_lastgang:objekt_<id>_cost_messtarif`                | Messtarif (Fix)                                    | CHF     |
| `oberbueren_lastgang:objekt_<id>_cost_total`                    | Summe aller Kostenpositionen                       | CHF     |

Für das Energie-Dashboard:
**Einstellungen → Dashboards → Energie → Verbrauch hinzufügen**, dort `…_bezug` für kWh und `…_cost_total` als Preis auswählen.

Der Leistungspreis (`…_cost_netznutzung_leistung`) wird der Stunde zugeschlagen, in der die **höchste 15-Minuten-Mittelleistung des Kalendermonats** auftrat (Monatsspitze × Leistungspreis × MwSt), und fliesst in `…_cost_total` ein. Vor 2027 ist er 0.

Die `…_leistung`-Statistik (kW, Stundenmittel mit Min/Max-Band) ist keine Energie-Dashboard-Grösse, sondern für einen Leistungsgraphen gedacht — siehe [Leistungsgraph](#leistungsgraph-kw).

### Sensor-Entitäten (Lovelace-freundlich)

Zusätzlich zu den Langzeitstatistiken werden pro Zähler 21 Sensor-Entitäten erstellt, die direkt in Dashboards oder Automationen verwendet werden können.

**Periodensensoren** (kWh + CHF für jede Periode — insgesamt 14):

| Periode         | Abdeckung                                       |
| --------------- | ----------------------------------------------- |
| Aktueller Monat | seit dem 1. des Monats bis jetzt                |
| Letzter Monat   | kompletter Vormonat                             |
| Aktuelles Jahr  | seit dem 1. Januar bis jetzt                    |
| Letztes Jahr    | komplettes Vorjahr                              |
| Gestern         | vollständiger Vortag                            |
| Letzte 7 Tage   | 7 vollständige Tage bis einschließlich gestern  |
| Letzte 30 Tage  | 30 vollständige Tage bis einschließlich gestern |

**Intelligente Sensoren** (7 abgeleitete Werte):

| Sensor                        | Einheit | Beschreibung                        |
| ----------------------------- | ------- | ----------------------------------- |
| Prognose Monat                | CHF     | Linear bis Monatsende hochgerechnet |
| Prognose Jahr                 | CHF     | Pro Tag: echter Wert → Vorjahr (falls importiert) → laufender Tagesschnitt |
| Ø Tagesverbrauch (Monat)      | kWh     | Verbrauch ÷ Tage seit Monatsanfang  |
| Ø Preis (Monat)               | Rp/kWh  | Effektiver Preis inkl. MwSt         |
| Leistung Spitze (Monat)       | kW      | Höchste 15-Min-Mittelleistung seit Monatsanfang (Basis des Leistungspreises) |
| Leistung Spitze (Letzter Monat) | kW    | Dieselbe Spitze für den kompletten Vormonat |
| Leistung Kosten (Monat)       | CHF     | Leistungspreis-Anteil des laufenden Monats |

Die Kosten-Periodensensoren stellen eine Aufschlüsselung nach Kategorien über die Entity-Attribute bereit — öffne die Entität unter **Entwicklerwerkzeuge → Zustände**, um zu sehen, „woher der Betrag kommt“.

Die Aktualisierung erfolgt stündlich, sodass die Werte innerhalb einer Stunde nach dem täglichen Import aktuell sind.

## Tarifkonfiguration (Kostenberechnung)

Die Integration enthält den **aktuellen Oberbüren-Tarif** bereits integriert — du musst nichts tun, damit die Kostenstatistiken sofort funktionieren.

**Standardmässig ist `<HA-config>/oberbueren_lastgang_tariffs.yaml` eine verwaltete Datei.** Da diese Integration nur für Oberbüren ist, hält sie deine Kopie automatisch mit den mitgelieferten Standardtarifen synchron: bringt ein Update eine neue Version (z. B. die Preise fürs neue Jahr), wird deine Datei ersetzt und die Kosten werden **aus den bereits in HA vorhandenen Daten neu berechnet — ohne API-Aufrufe** (`recompute_costs` läuft automatisch).

Willst du die Datei selbst pflegen: schalte in den Integrations-Optionen (**Einstellungen → Geräte & Dienste → Strom Oberbüren Lastgang → Konfigurieren**) **„Tarifdatei automatisch verwalten"** aus. Danach legt die Integration die Datei nur noch an, wenn sie fehlt, und fasst sie sonst nie wieder an.

Eine **von Hand geänderte** Datei wird selbst bei aktiver Verwaltung nie überschrieben — stattdessen erscheint ein Reparatur-Hinweis mit den zwei Auswegen (Option ausschalten, oder Datei löschen und Integration neu laden).

Die Kosten lassen sich jederzeit ohne API-Aufruf neu berechnen — der Dienst `oberbueren_lastgang.recompute_costs` wendet die aktuelle Tarifdatei auf die gespeicherten kWh-Daten an. Der Leistungspreis braucht dafür allerdings die 15-Minuten-Leistungsdaten (`…_leistung`); für Zeiträume ohne diese Daten bleibt er 0, bis ein echtes `backfill` sie nachlädt.

Das Dateiformat — jede Periode trägt nur die Positionen, die für sie gelten. Fehlende Positionen werden still übersprungen, sodass das **HT/NT-Modell (bis 2026)** und das **Einheitstarif- + Saison- + Leistungs-Modell (ab 2027)** nach Datum nebeneinander bestehen:

```yaml
# Bis 2026: Hoch-/Niedertarif
- valid_from: 2026-01-01
  valid_until: 2026-12-31
  mwst_default: 8.1
  netznutzung:
    wirkstrom_ht: 10.40            # Rp/kWh
    wirkstrom_nt: 10.00
    grundgebuehr: 6.00            # CHF/Monat
  energiebezug:
    wirkstrom_ht: 17.30
    wirkstrom_nt: 17.30
  abgaben:
    sdl_swissgrid: 0.27
    stromreserve: 0.41
    solidarisierte_kosten: 0.05
    netzzuschlag: 2.30
    # netzzuschlag_mwst: 0         # optionaler positionsspezifischer MwSt-Override
  messtarif: 9.00                 # CHF/Monat

# Ab 2027: Einheitstarif + Saison-Energie + Leistungspreis
- valid_from: 2027-01-01
  valid_until: ~                  # ~ = aktuell aktiv
  mwst_default: 8.1
  netznutzung:
    wirkstrom: 8.80              # Rp/kWh, Einheitstarif (kein HT/NT)
    grundgebuehr: 6.00
    leistung: 1.50              # CHF/kW/Monat, auf die Monatsspitze
    netznutzung_abgaben: 0.55    # Rp/kWh, kombinierte Abgaben-Zeile
  energiebezug:
    wirkstrom_sommer: 15.30      # Rp/kWh, 1. April – 30. September
    wirkstrom_winter: 18.30      # Rp/kWh, 1. Oktober – 31. März
  abgaben:
    netzzuschlag: 2.30           # „Abgaben Einheitspreis“ (unverändert)
  messtarif: 9.00
```

Die Integration verwendet für jede importierte Stunde automatisch die nach Datum passende Periode, sodass Backfills älterer Jahre korrekte historische Preise verwenden, sofern die passende Periode im YAML vorhanden ist.

**Zeit-/Saison-Logik** (fest eingebaut):
* HT/NT: Mo–Fr 07:00–19:00 = HT, sonst NT (nur wenn `wirkstrom_ht`/`_nt` gesetzt sind).
* Sommer/Winter: April–September = Sommer, Oktober–März = Winter (nur wenn `wirkstrom_sommer`/`_winter` gesetzt sind).
* Feiertage werden *nicht* berücksichtigt — ein Feiertag an einem Mittwoch um 10:00 zählt als HT.
* `leistung`: die höchste 15-Minuten-Mittelleistung (kW) je Kalendermonat × Leistungspreis × MwSt, komplett der Spitzenstunde zugeschlagen.

Beim Wechsel auf das 2027-Modell: `valid_until: 2026-12-31` beim alten Block setzen, den neuen Block anfügen, dann einmal `oberbueren_lastgang.recompute_costs` ausführen — die kWh- und Leistungsdaten sind schon da, nur die Kosten werden neu gerechnet.

Die Tarifdatei wird bei jedem Import neu eingelesen, sodass Änderungen ohne Neustart von HA beim nächsten täglichen Import (oder Backfill) wirksam werden.

## Spätere Aktivierung von Einspeisung (PV-Rückspeisung)

Die Messlinien-Abstraktion in `const.py` definiert bereits `1-1:2.5.0` (Einspeisung). Um sie zu aktivieren, ändere:

```python
ACTIVE_MESSLINIEN = (MESSLINIE_BEZUG,)
```

zu:

```python
ACTIVE_MESSLINIEN = (MESSLINIE_BEZUG, MESSLINIE_EINSPEISUNG)
```

und führe anschließend ein Backfill erneut aus.
Der Rest der Pipeline (API-Client, Statistikimport) arbeitet bereits richtungsunabhängig.

## Einschränkungen

* **Die initiale Befüllung** ist ein manueller Schritt — führe den Dienst `backfill` mit dem gewünschten Datumsbereich aus. Das automatische Catch-up schließt nur Lücken relativ zu bereits vorhandenen Daten, nicht den allerersten Import.
* **Das automatische Catch-up ist auf 30 Tage begrenzt.** Wenn dein HA-Host länger offline war, führe `backfill` für den älteren Zeitraum manuell aus.
  (Diese Begrenzung existiert, damit ein lange offline gewesener Host beim ersten Start nicht versehentlich mehrere hundert API-Anfragen hintereinander auslöst.)
* **Nur die Messlinie `Bezug` ist standardmäßig aktiv.** Das Hinzufügen von Einspeisung erfordert eine Zeile in `const.py` sowie einen separaten Einspeisetarif (der derzeit noch nicht modelliert ist). Siehe `MESSLINIE_EINSPEISUNG` im Quellcode.
