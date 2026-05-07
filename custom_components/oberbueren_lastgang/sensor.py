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

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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


# Periods that intentionally end at midnight today rather than ``now``:
# the upstream API only delivers data through *yesterday*, so anchoring
# the end to today's 00:00 means the sensor window contains exactly the
# hours we have data for (no partial-day artefacts).

@dataclass(frozen=True)
class Yesterday(PeriodSpec):
    def resolve(self, now_local: datetime) -> tuple[datetime, datetime]:
        today_start = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return today_start - timedelta(days=1), today_start


@dataclass(frozen=True)
class Last7Days(PeriodSpec):
    def resolve(self, now_local: datetime) -> tuple[datetime, datetime]:
        today_start = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return today_start - timedelta(days=7), today_start


@dataclass(frozen=True)
class Last30Days(PeriodSpec):
    def resolve(self, now_local: datetime) -> tuple[datetime, datetime]:
        today_start = now_local.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return today_start - timedelta(days=30), today_start


_PERIODS: tuple[PeriodSpec, ...] = (
    CurrentMonth(key="current_month", de_label="Aktueller Monat"),
    LastMonth(key="last_month", de_label="Letzter Monat"),
    CurrentYear(key="current_year", de_label="Aktuelles Jahr"),
    LastYear(key="last_year", de_label="Letztes Jahr"),
    Yesterday(key="yesterday", de_label="Gestern"),
    Last7Days(key="last_7_days", de_label="Letzte 7 Tage"),
    Last30Days(key="last_30_days", de_label="Letzte 30 Tage"),
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

    async def _async_update_data(self) -> dict[str, float | None]:
        now_local = dt_util.now().astimezone(_LOCAL_TZ)
        recorder = get_instance(self.hass)
        result: dict[str, float | None] = {}

        for period in _PERIODS:
            start_local, end_local = period.resolve(now_local)
            start_utc = start_local.astimezone(dt_util.UTC)
            end_utc = end_local.astimezone(dt_util.UTC)

            # One DB query per series per period. Seven series × seven
            # periods = 49 cheap range queries per hour, well below any
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

        cost_total_id = build_cost_statistic_id(self._objekt_id, COST_TOTAL_KEY)
        year_projection = await recorder.async_add_executor_job(
            _compute_seasonal_year_projection,
            self.hass,
            cost_total_id,
            now_local,
        )

        _add_derived(result, now_local, year_projection)
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


def _daily_change_map(
    hass: HomeAssistant,
    statistic_id: str,
    start_date: date,
    end_date_exclusive: date,
) -> dict[date, float]:
    """Sum hourly ``change`` rows into per-local-day buckets.

    A day is present in the output iff the recorder has at least one
    hourly row for it — that's how we distinguish "imported, zero
    consumption" from "no data". Returns an empty dict when nothing has
    been imported in the range.
    """
    if start_date >= end_date_exclusive:
        return {}
    start_local = datetime.combine(
        start_date, datetime.min.time()
    ).replace(tzinfo=_LOCAL_TZ)
    end_local = datetime.combine(
        end_date_exclusive, datetime.min.time()
    ).replace(tzinfo=_LOCAL_TZ)
    rows = statistics_during_period(
        hass,
        start_local.astimezone(dt_util.UTC),
        end_local.astimezone(dt_util.UTC),
        {statistic_id},
        "hour",
        None,
        {"change"},
    ).get(statistic_id, [])

    out: dict[date, float] = {}
    for r in rows:
        start = r.get("start")
        if start is None:
            continue
        if not isinstance(start, datetime):
            start = datetime.fromtimestamp(float(start), tz=dt_util.UTC)
        d = start.astimezone(_LOCAL_TZ).date()
        out[d] = out.get(d, 0.0) + float(r.get("change") or 0.0)
    return out


def _compute_seasonal_year_projection(
    hass: HomeAssistant,
    statistic_id: str,
    now_local: datetime,
) -> float | None:
    """Project full-year cost using actual + last-year + running average.

    For each day of the current calendar year we pick a value via this
    fallback chain:

      1. day ≤ yesterday and we have stats rows for it → real value
      2. matching day last year is imported                → last-year value
      3. otherwise                                         → running daily
         average of all current-year days that *do* have data

    Returns ``None`` if no current-year data has been imported yet —
    nothing meaningful to project from.
    """
    today = now_local.date()
    yesterday = today - timedelta(days=1)
    year_start = date(now_local.year, 1, 1)
    year_end_excl = date(now_local.year + 1, 1, 1)

    this_year = _daily_change_map(
        hass, statistic_id, year_start, yesterday + timedelta(days=1)
    )
    if not this_year:
        return None

    last_year = _daily_change_map(
        hass,
        statistic_id,
        date(now_local.year - 1, 1, 1),
        year_start,
    )

    daily_avg = sum(this_year.values()) / len(this_year)

    total = 0.0
    d = year_start
    one_day = timedelta(days=1)
    while d < year_end_excl:
        if d <= yesterday and d in this_year:
            total += this_year[d]
        else:
            try:
                ly = d.replace(year=d.year - 1)
            except ValueError:
                # Feb 29 in a leap current year with non-leap last year
                ly = None
            if ly is not None and ly in last_year:
                total += last_year[ly]
            else:
                total += daily_avg
        d += one_day
    return total


def _add_derived(
    result: dict[str, float | None],
    now_local: datetime,
    year_projection: float | None,
) -> None:
    """Compute projection / average sensors from the period values.

    Monthly projection is a simple linear extrapolation — within a
    single month, seasonality is small enough that this is fine. The
    yearly projection is computed separately by
    ``_compute_seasonal_year_projection`` and passed in here.
    """
    days_in_month = calendar.monthrange(now_local.year, now_local.month)[1]
    days_done_month = now_local.day - 1

    cm_kwh = result.get("kwh_current_month") or 0.0
    cm_cost = result.get("cost_total_current_month") or 0.0

    if days_done_month > 0:
        result["projected_month_cost"] = cm_cost / days_done_month * days_in_month
        result["avg_daily_kwh_current_month"] = cm_kwh / days_done_month
    else:
        # First day of the month before yesterday's data lands → no
        # meaningful projection.
        result["projected_month_cost"] = None
        result["avg_daily_kwh_current_month"] = None

    result["projected_year_cost"] = year_projection

    # Effective average price = total CHF / total kWh × 100 (Rp/kWh).
    # Falls apart if there's no kWh consumption (division by zero) or
    # the cost statistics never got recomputed against a real tariff
    # (cm_cost stays 0 → reported price 0 — visibly broken, which is
    # honest signaling).
    if cm_kwh > 0:
        result["avg_price_rp_per_kwh_current_month"] = cm_cost / cm_kwh * 100
    else:
        result["avg_price_rp_per_kwh_current_month"] = None


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
    for spec in _DERIVED_SPECS:
        entities.append(
            DerivedSensor(
                coordinator, friendly, objekt_id, meteringcode, spec
            )
        )

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Derived sensors (projections, averages)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DerivedSensorSpec:
    """Static description of one computed sensor backed by a coordinator key."""

    coordinator_key: str
    name: str                       # full sensor name (after device prefix)
    unique_id_suffix: str
    unit: str
    device_class: SensorDeviceClass | None
    icon: str | None = None
    decimals: int = 2


_DERIVED_SPECS: tuple[DerivedSensorSpec, ...] = (
    DerivedSensorSpec(
        coordinator_key="projected_month_cost",
        name="Prognose Monat",
        unique_id_suffix="prognose_monat",
        unit=CURRENCY,
        device_class=SensorDeviceClass.MONETARY,
    ),
    DerivedSensorSpec(
        coordinator_key="projected_year_cost",
        name="Prognose Jahr",
        unique_id_suffix="prognose_jahr",
        unit=CURRENCY,
        device_class=SensorDeviceClass.MONETARY,
    ),
    DerivedSensorSpec(
        coordinator_key="avg_daily_kwh_current_month",
        name="Ø Tagesverbrauch (Monat)",
        unique_id_suffix="avg_daily_kwh_month",
        unit="kWh",
        device_class=None,
        icon="mdi:counter",
        decimals=2,
    ),
    DerivedSensorSpec(
        coordinator_key="avg_price_rp_per_kwh_current_month",
        name="Ø Preis (Monat)",
        unique_id_suffix="avg_price_rp_kwh_month",
        unit="Rp/kWh",
        device_class=None,
        icon="mdi:cash-multiple",
        decimals=2,
    ),
)


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
        # No state_class: HA rejects MEASUREMENT for monetary/energy device
        # classes, and these aren't cumulative totals either — they're
        # summary values over fixed/rolling windows. The real long-term
        # data lives in our External Statistics.

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


class DerivedSensor(CoordinatorEntity[AggregateCoordinator], SensorEntity):
    """One of the projection / average sensors driven by ``DerivedSensorSpec``."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: AggregateCoordinator,
        friendly_name: str,
        objekt_id: str,
        meteringcode: str,
        spec: DerivedSensorSpec,
    ) -> None:
        super().__init__(coordinator)
        self._spec = spec
        self._attr_unique_id = (
            f"{DOMAIN}_{objekt_id}_{meteringcode}_{spec.unique_id_suffix}"
        )
        self._attr_name = spec.name
        self._attr_native_unit_of_measurement = spec.unit
        if spec.device_class is not None:
            self._attr_device_class = spec.device_class
        else:
            # Without a device class, MEASUREMENT is valid and accurate
            # for the Ø-style sensors (true point-in-time values).
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if spec.icon is not None:
            self._attr_icon = spec.icon

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
        value = self.coordinator.data.get(self._spec.coordinator_key)
        if value is None:
            return None
        return round(value, self._spec.decimals)
