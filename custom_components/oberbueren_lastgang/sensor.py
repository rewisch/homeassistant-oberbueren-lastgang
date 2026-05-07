"""Aggregate sensor entities (Verbrauch & Kosten over fixed periods).

Per configured meter we expose 8 sensors:

  * Verbrauch (kWh):    aktueller_monat, letzter_monat, aktuelles_jahr, letztes_jahr
  * Kosten   (CHF):     same four periods, reading the ``cost_total`` statistic.

Values are computed from HA's long-term statistics (the ones we import in
statistics.py) by summing per-hour ``change`` increments over the desired
window. Refresh runs hourly — values can lag the daily import by at most
that long, which is fine for dashboard purposes.

The Kosten sensors expose per-category breakdown via
``extra_state_attributes`` so a click on the entity reveals "wovon kommt
der Betrag" without needing four extra entities per period.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import homeassistant.util.dt as dt_util
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import (
    ACTIVE_MESSLINIEN,
    CONF_METERINGCODE,
    CONF_NAME,
    CONF_OBJEKT_ID,
    COST_CATEGORY_KEYS,
    COST_CATEGORY_LABELS,
    COST_TOTAL_KEY,
    CURRENCY,
    DOMAIN,
)
from .statistics import build_cost_statistic_id, build_statistic_id

_LOGGER = logging.getLogger(__name__)
_LOCAL_TZ = ZoneInfo("Europe/Zurich")

# How often to recompute the aggregates. Daily import lands at 06:00, so
# users see fresh numbers on their dashboard within an hour after that.
_REFRESH_INTERVAL = timedelta(hours=1)


# ---------------------------------------------------------------------------
# Period definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PeriodSpec:
    """One named period — e.g. "current_month" — with a date-range resolver."""

    key: str          # e.g. "current_month"
    de_label: str     # human-readable suffix in the entity name

    def resolve(self, now_local: datetime) -> tuple[datetime, datetime]:
        """Return (start_local, end_local) for ``now_local``. Implemented per spec."""
        raise NotImplementedError


@dataclass(frozen=True)
class CurrentMonth(PeriodSpec):
    def resolve(self, now_local: datetime) -> tuple[datetime, datetime]:
        start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, now_local


@dataclass(frozen=True)
class LastMonth(PeriodSpec):
    def resolve(self, now_local: datetime) -> tuple[datetime, datetime]:
        first_this = now_local.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        last_month_end = first_this  # exclusive end
        # Step into previous month: subtract one day, snap to its 1st.
        prev = first_this - timedelta(days=1)
        last_month_start = prev.replace(day=1)
        return last_month_start, last_month_end


@dataclass(frozen=True)
class CurrentYear(PeriodSpec):
    def resolve(self, now_local: datetime) -> tuple[datetime, datetime]:
        start = now_local.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        return start, now_local


@dataclass(frozen=True)
class LastYear(PeriodSpec):
    def resolve(self, now_local: datetime) -> tuple[datetime, datetime]:
        start = now_local.replace(
            year=now_local.year - 1, month=1, day=1,
            hour=0, minute=0, second=0, microsecond=0,
        )
        end = now_local.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0,
        )
        return start, end


_PERIODS: tuple[PeriodSpec, ...] = (
    CurrentMonth(key="current_month", de_label="Aktueller Monat"),
    LastMonth(key="last_month", de_label="Letzter Monat"),
    CurrentYear(key="current_year", de_label="Aktuelles Jahr"),
    LastYear(key="last_year", de_label="Letztes Jahr"),
)


# ---------------------------------------------------------------------------
# Coordinator that batches all 8 statistic queries into one refresh
# ---------------------------------------------------------------------------

class AggregateCoordinator(DataUpdateCoordinator[dict[str, float]]):
    """Computes period totals for both kWh and per-category cost stats.

    The data returned by ``_async_update_data`` is a flat dict keyed like
    ``"kwh_current_month"`` and ``"cost_total_current_month"`` so sensors
    can pluck their value with a single key. Cost-category breakdowns
    (used in the Kosten sensors' attributes) live under keys like
    ``"cost_netznutzung_wirkstrom_current_month"``.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}.aggregates[{entry.data[CONF_NAME]}]",
            update_interval=_REFRESH_INTERVAL,
        )
        self.entry = entry
        self._objekt_id: str = str(entry.data[CONF_OBJEKT_ID])
        # We aggregate the Bezug Messlinie only — for now that's the
        # single ACTIVE_MESSLINIEN entry. If Einspeisung is added later,
        # additional sensors would be wired similarly.
        self._messlinie = ACTIVE_MESSLINIEN[0]

        # All statistic IDs we'll query. Keep them as a list of
        # ``(prefix, statistic_id)`` so the result dict's keys are
        # composable with period names below.
        self._series: list[tuple[str, str]] = [
            ("kwh", build_statistic_id(self._objekt_id, self._messlinie)),
            ("cost_total", build_cost_statistic_id(self._objekt_id, COST_TOTAL_KEY)),
        ]
        for cat in COST_CATEGORY_KEYS:
            self._series.append(
                (f"cost_{cat}", build_cost_statistic_id(self._objekt_id, cat))
            )

    async def _async_update_data(self) -> dict[str, float]:
        now_local = dt_util.now().astimezone(_LOCAL_TZ)
        recorder = get_instance(self.hass)
        result: dict[str, float] = {}

        for period in _PERIODS:
            start_local, end_local = period.resolve(now_local)
            start_utc = start_local.astimezone(dt_util.UTC)
            end_utc = end_local.astimezone(dt_util.UTC)

            # One DB query per series per period. Eight series × four
            # periods = 32 cheap range queries per hour, well below any
            # recorder overhead concerns.
            for prefix, stat_id in self._series:
                value = await recorder.async_add_executor_job(
                    _sum_change_for,
                    self.hass,
                    stat_id,
                    start_utc,
                    end_utc,
                )
                result[f"{prefix}_{period.key}"] = value

        return result


def _sum_change_for(
    hass: HomeAssistant,
    statistic_id: str,
    start_utc: datetime,
    end_utc: datetime,
) -> float:
    """Sum hourly ``change`` increments between two UTC times."""
    rows = statistics_during_period(
        hass,
        start_utc,
        end_utc,
        {statistic_id},
        "hour",
        None,
        {"change"},
    ).get(statistic_id, [])
    return float(sum(r.get("change") or 0.0 for r in rows))


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = AggregateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    friendly = entry.data[CONF_NAME]
    objekt_id = str(entry.data[CONF_OBJEKT_ID])
    meteringcode = entry.data[CONF_METERINGCODE]

    entities: list[SensorEntity] = []
    for period in _PERIODS:
        entities.append(
            ConsumptionPeriodSensor(
                coordinator, friendly, objekt_id, meteringcode, period
            )
        )
        entities.append(
            CostPeriodSensor(
                coordinator, friendly, objekt_id, meteringcode, period
            )
        )

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Entity classes
# ---------------------------------------------------------------------------

class _BasePeriodSensor(CoordinatorEntity[AggregateCoordinator], SensorEntity):
    """Common base for the kWh and CHF period sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: AggregateCoordinator,
        friendly_name: str,
        objekt_id: str,
        meteringcode: str,
        period: PeriodSpec,
        kind: str,                 # "verbrauch" | "kosten"
        device_class: SensorDeviceClass,
        unit: str,
        coordinator_key_prefix: str,
    ) -> None:
        super().__init__(coordinator)
        self._period = period
        self._coordinator_key = f"{coordinator_key_prefix}_{period.key}"

        # unique_id ties to the meter (objekt_id+meteringcode) so re-adding
        # the same meter doesn't fragment history.
        self._attr_unique_id = (
            f"{DOMAIN}_{objekt_id}_{meteringcode}_{kind}_{period.key}"
        )
        self._attr_name = f"{kind.capitalize()} {period.de_label}"
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        # Period-aggregate sensors are point-in-time *summaries* of fixed
        # time windows that reset on calendar boundaries — not classical
        # cumulative totals. ``measurement`` keeps HA from trying to
        # interpret them as long-term-stats counters; the real long-term
        # data lives in our External Statistics.
        self._attr_state_class = SensorStateClass.MEASUREMENT

        # Group all sensors for one meter under a single device card in
        # the UI — looks cleaner than 8 floating entities.
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"{objekt_id}:{meteringcode}")},
            "name": friendly_name,
            "manufacturer": "Strom Oberbüren",
            "model": "Lastgang-Importer",
        }

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        value = self.coordinator.data.get(self._coordinator_key)
        if value is None:
            return None
        # Round to 3 decimals (Wh / Rappen precision) so the dashboard
        # doesn't show 47.18399999999998 kWh.
        return round(value, 3)


class ConsumptionPeriodSensor(_BasePeriodSensor):
    """kWh consumed within the period."""

    def __init__(
        self,
        coordinator: AggregateCoordinator,
        friendly_name: str,
        objekt_id: str,
        meteringcode: str,
        period: PeriodSpec,
    ) -> None:
        super().__init__(
            coordinator,
            friendly_name,
            objekt_id,
            meteringcode,
            period,
            kind="verbrauch",
            device_class=SensorDeviceClass.ENERGY,
            unit="kWh",
            coordinator_key_prefix="kwh",
        )


class CostPeriodSensor(_BasePeriodSensor):
    """CHF cost (incl. VAT) within the period, with category breakdown."""

    def __init__(
        self,
        coordinator: AggregateCoordinator,
        friendly_name: str,
        objekt_id: str,
        meteringcode: str,
        period: PeriodSpec,
    ) -> None:
        super().__init__(
            coordinator,
            friendly_name,
            objekt_id,
            meteringcode,
            period,
            kind="kosten",
            device_class=SensorDeviceClass.MONETARY,
            unit=CURRENCY,
            coordinator_key_prefix="cost_total",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Per-category breakdown — keys are German-labeled for friendliness."""
        if self.coordinator.data is None:
            return {}
        out: dict[str, Any] = {}
        for cat in COST_CATEGORY_KEYS:
            value = self.coordinator.data.get(f"cost_{cat}_{self._period.key}")
            if value is None:
                continue
            out[COST_CATEGORY_LABELS[cat]] = round(value, 2)
        return out
