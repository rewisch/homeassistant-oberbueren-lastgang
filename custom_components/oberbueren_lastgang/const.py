"""Constants for the Strom Oberbüren Lastgang integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

DOMAIN: Final = "oberbueren_lastgang"

BASE_URL: Final = "https://www.strom.oberbueren.ch"

CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"
CONF_OBJEKT_ID: Final = "objekt_id"
CONF_METERINGCODE: Final = "meteringcode"
CONF_NAME: Final = "name"
CONF_BASE_URL: Final = "base_url"
CONF_POLL_HOURS: Final = "poll_hours"
CONF_DEBUG_LOGGING: Final = "debug_logging"

# Daily poll: previous day at these local hours (must all be > 0 because data is
# for yesterday). We fire at multiple hours because the upstream API occasionally
# returns 5xx or simply doesn't have yesterday's data ready that early. Since
# ``async_catch_up`` is idempotent (no-op once up to date), the later slots act
# as automatic retries — no extra bookkeeping needed.
DEFAULT_POLL_HOURS: Final = (6, 7, 8, 9)

# Service names
SERVICE_BACKFILL: Final = "backfill"
SERVICE_CATCH_UP: Final = "catch_up"
ATTR_START_DATE: Final = "start_date"
ATTR_END_DATE: Final = "end_date"


@dataclass(frozen=True)
class Messlinie:
    """A measurement line (OBIS code) with its semantic properties.

    The integration is structured around this abstraction so that adding
    additional Messlinien (e.g. Einspeisung) only requires registering a new
    entry — the API client, coordinator, and statistics importer are all
    Messlinien-agnostic.
    """

    obis: str
    label: str
    statistic_suffix: str
    direction: Literal["consumption", "production"]


# OBIS 1-1:1.5.0 = Wirkleistung Bezug (active power consumed, average over interval).
MESSLINIE_BEZUG: Final = Messlinie(
    obis="1-1:1.5.0",
    label="Bezug",
    statistic_suffix="bezug",
    direction="consumption",
)

# OBIS 1-1:2.5.0 = Wirkleistung Lieferung (active power produced/fed-in).
# Not yet wired into the config flow but the rest of the pipeline supports it.
MESSLINIE_EINSPEISUNG: Final = Messlinie(
    obis="1-1:2.5.0",
    label="Einspeisung",
    statistic_suffix="einspeisung",
    direction="production",
)

# Messlinien activated for V1. Extending this tuple is all that is needed to
# also collect Einspeisung once the user has a PV system.
ACTIVE_MESSLINIEN: Final = (MESSLINIE_BEZUG,)


# ---------------------------------------------------------------------------
# Cost-statistics IDs.
#
# These mirror the structure in tariffs.py: each ``StatCategory`` from there
# corresponds to one external statistic here. ``cost_total`` is the sum and
# is what users typically link in the Energy Dashboard.
# ---------------------------------------------------------------------------

COST_CATEGORY_KEYS: Final = (
    "netznutzung_wirkstrom",
    "netznutzung_grundgebuehr",
    "energiebezug_wirkstrom",
    "energiebezug_zuschlaege",
    "messtarif",
)

COST_TOTAL_KEY: Final = "total"

COST_CATEGORY_LABELS: Final = {
    "netznutzung_wirkstrom": "Netznutzung Wirkstrom",
    "netznutzung_grundgebuehr": "Netznutzung Grundgebühr",
    "energiebezug_wirkstrom": "Energiebezug Wirkstrom",
    "energiebezug_zuschlaege": "Abgaben & Zuschläge",
    "messtarif": "Messtarif",
    "total": "Gesamtkosten",
}

CURRENCY: Final = "CHF"
