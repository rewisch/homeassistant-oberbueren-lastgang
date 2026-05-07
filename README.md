# Strom Oberbüren Lastgang — Home Assistant Integration

A custom integration that pulls 15-minute electricity load-curve data from
[strom.oberbueren.ch](https://www.strom.oberbueren.ch) and writes it to
Home Assistant's long-term statistics so it survives the recorder purge
and shows up in the Energy Dashboard.

## Installation

### Via HACS (recommended)

1. In HACS: **Integrationen → ⋮ Menü → Benutzerdefinierte Repositories**.
2. Repository: `https://github.com/rewisch/homeassistant-oberbueren-lastgang`,
   Kategorie: **Integration**, hinzufügen.
3. Die Integration "Strom Oberbüren Lastgang" wird in HACS angezeigt →
   **Herunterladen**.
4. Home Assistant neu starten.
5. **Einstellungen → Geräte & Dienste → Integration hinzufügen → "Strom
   Oberbüren Lastgang"**.
6. Email + Passwort eingeben, dann `objektId` und `meteringcode` deines
   Zählers (beides findest du in der URL der Lastgangdaten-Seite im
   Browser).

### Manuelle Installation

1. `custom_components/oberbueren_lastgang/` aus diesem Repo nach
   `<config>/custom_components/` deiner HA-Installation kopieren.
2. Home Assistant neu starten.
3. Schritte 5–6 oben.

## Daily auto-import

Once configured, the integration imports yesterday's data every morning
at **06:00 local time**. No further action required.

## Initial backfill

The first time you set it up you'll likely want to import several months
or years of history. Use the `oberbueren_lastgang.backfill` service:

```yaml
service: oberbueren_lastgang.backfill
data:
  entry_id: 01HX9Z7E8K2QY7CDXR...    # see Settings → … → Show entry ID
  start_date: 2024-01-01
  end_date: 2024-12-31
```

Tip: backfill in chronological chunks (e.g. one year at a time) so the
cumulative kWh sums build up correctly. The integration fetches one HTTP
request per day, so a full year takes ~365 requests. Be a polite citizen
and don't hammer the API.

## Recomputing costs without re-fetching

If you edit `oberbueren_lastgang_tariffs.yaml` (price change, new tariff
period, fixed a typo), or you've imported kWh data on a version that
predates the cost feature, you can rebuild **all** cost statistics
from the existing kWh stats — no HTTP requests:

```yaml
service: oberbueren_lastgang.recompute_costs
data:
  entry_id: 01HX9Z7E8K2QY7CDXR...
```

The service reads every available hourly kWh increment from HA's
recorder, applies the current tariff file, and overwrites the six
cost statistics from scratch (anchor = 0, fresh cumulative chain).
It's safe to run repeatedly.

## What ends up in HA

### Long-term statistics (External Statistics)

For each configured meter the integration creates these statistics —
they survive the recorder purge and back the Energy Dashboard:

| Statistic ID | Meaning | Unit |
|--------------|---------|------|
| `oberbueren_lastgang:objekt_<id>_bezug` | cumulative consumption | kWh |
| `oberbueren_lastgang:objekt_<id>_cost_netznutzung_wirkstrom` | Netznutzung Wirkstrom (HT/NT) | CHF |
| `oberbueren_lastgang:objekt_<id>_cost_netznutzung_grundgebuehr` | Grundgebühr (Fix) | CHF |
| `oberbueren_lastgang:objekt_<id>_cost_energiebezug_wirkstrom` | Energiebezug Wirkstrom (HT/NT) | CHF |
| `oberbueren_lastgang:objekt_<id>_cost_energiebezug_zuschlaege` | SDL + Stromreserve + Solidarisierte + Netzzuschlag | CHF |
| `oberbueren_lastgang:objekt_<id>_cost_messtarif` | Messtarif (Fix) | CHF |
| `oberbueren_lastgang:objekt_<id>_cost_total` | Sum of the above | CHF |

For the Energy Dashboard: **Settings → Dashboards → Energy → Add
consumption**, pick `…_bezug` for kWh and `…_cost_total` for the price.

### Sensor entities (Lovelace-friendly)

In addition to the long-term statistics, 18 sensor entities per meter
are created so you can drop them on dashboards or use in automations.

**Period sensors** (kWh + CHF for each — 14 total):

| Period | Coverage |
|---|---|
| Aktueller Monat | seit 1. des Monats bis jetzt |
| Letzter Monat | kompletter Vormonat |
| Aktuelles Jahr | seit 1. Januar bis jetzt |
| Letztes Jahr | komplettes Vorjahr |
| Gestern | voller Vortag |
| Letzte 7 Tage | 7 komplette Tage bis und mit gestern |
| Letzte 30 Tage | 30 komplette Tage bis und mit gestern |

**Smart sensors** (4 derived):

| Sensor | Unit | Beschreibung |
|---|---|---|
| Prognose Monat | CHF | Linear hochgerechnet auf Monatsende |
| Prognose Jahr | CHF | Linear hochgerechnet auf Jahresende |
| Ø Tagesverbrauch (Monat) | kWh | Verbrauch ÷ Tage seit Monatsanfang |
| Ø Preis (Monat) | Rp/kWh | Effektiver Preis incl. MwSt |

The Kosten period sensors expose a per-category breakdown via the
entity attributes — open the entity in **Developer Tools → States**
to see "wovon kommt der Betrag".

Refresh runs hourly so values catch up within an hour after the daily
import lands at 06:00.

## Tariff configuration (cost calculation)

The integration ships with the **current Oberbüren tariff** built in.
On first setup it copies the bundled defaults to
`<HA-config>/oberbueren_lastgang_tariffs.yaml` — you don't need to do
anything for cost statistics to work out of the box.

**Updates never overwrite your file.** Once that path exists, the
integration leaves it alone forever, even across HACS upgrades. So you
can edit prices, add tariff periods, or apply per-position MwSt
overrides without worrying about losing your changes.

When the canonical Oberbüren tariff changes (typically on 1 January),
the bundled defaults in the repo are bumped — but your local file
stays as-is. To pick up new defaults, either compare against the
bundled `default_tariffs.yaml` and merge, or delete your file and
restart HA to re-seed from the new default.

The file format:

```yaml
- valid_from: 2026-01-01
  valid_until: ~                    # ~ = currently active
  mwst_default: 8.1

  netznutzung:
    wirkstrom_ht: 10.40             # Rp/kWh
    wirkstrom_nt: 10.00
    grundgebuehr: 6.00              # CHF/Monat

  energiebezug:
    wirkstrom_ht: 17.30
    wirkstrom_nt: 17.30

  abgaben:
    sdl_swissgrid: 0.27
    stromreserve: 0.41
    solidarisierte_kosten: 0.05
    netzzuschlag: 2.30
    # netzzuschlag_mwst: 0          # optional per-position MwSt override

  messtarif: 9.00                   # CHF/Monat
```

Add more periods for tariff history (Swiss tariffs typically change on
1 January). The integration looks up the right period for each imported
hour, so backfilling old years gets correct historical pricing as long
as the matching period is in the YAML.

**HT/NT logic** (hard-coded): Mon–Fri 07:00–19:00 = HT, otherwise NT.
Public holidays are *not* treated as NT — a holiday on a Wednesday at
10:00 still counts as HT.

The tariff file is re-read on every import, so edits take effect on
the next daily fetch (or backfill) without an HA restart.

## Adding Einspeisung (PV feed-in) later

The Messlinie abstraction in `const.py` already defines `1-1:2.5.0`
(Einspeisung). To activate it: change

```python
ACTIVE_MESSLINIEN = (MESSLINIE_BEZUG,)
```

to

```python
ACTIVE_MESSLINIEN = (MESSLINIE_BEZUG, MESSLINIE_EINSPEISUNG)
```

and re-run a backfill. The rest of the pipeline (API client, statistics
import) is already direction-agnostic.

## Limitations

* Re-running backfill over a window that's already imported re-anchors
  the running sum; for clean re-imports clear the existing statistics
  first via **Developer Tools → Statistics → Fix issues**.
* The daily import runs at 06:00 local. If your HA host is offline that
  morning, that day is missed — re-import via the backfill service.
* Only the `Bezug` Messlinie is wired up by default. See above.
