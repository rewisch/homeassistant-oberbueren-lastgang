"""Convert API responses into HA Long-Term Statistics.

The API returns 15-minute average power samples in kW. Home Assistant's
external statistics API requires hourly aggregation and a monotonically
increasing cumulative ``sum`` (in kWh) for the Energy Dashboard to display
the data as energy consumption.

Key transformations performed here:

  * Each 15-minute interval is converted from kW (average power) to kWh
    (energy consumed in that interval): ``kWh = kW × 0.25h``.
  * Intervals are bucketed by their UTC hour-start. The API reports times
    in ``+01:00`` / ``+02:00`` (Europe/Zurich) — converting to UTC handles
    DST changeovers automatically.
  * The cumulative ``sum`` continues from whatever was previously imported
    for the same statistic_id. On first import the running sum starts at 0.

Re-importing an already-imported day is supported: ``async_add_external_
statistics`` overwrites at the given timestamps. Note however that running
sums *before* the re-imported window stay correct, while the absolute sums
for hours *at and after* the new data will be recalculated on the fly from
the previous-hour anchor — so contiguous imports are recommended.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.core import HomeAssistant

from .api import MessdatenResponse
from .const import (
    COST_CATEGORY_KEYS,
    COST_CATEGORY_LABELS,
    COST_TOTAL_KEY,
    CURRENCY,
    DOMAIN,
    Messlinie,
)
from .cost import compute_hourly_costs
from .tariffs import TariffDatabase

_LOGGER = logging.getLogger(__name__)

# All API-side power values are in kW; one 15-min sample = 0.25h of energy.
_INTERVAL_HOURS = 0.25


def build_statistic_id(objekt_id: int | str, messlinie: Messlinie) -> str:
    """Construct the external-statistics ID for one (objekt, messlinie) pair.

    Format: ``oberbueren_lastgang:objekt_<id>_<suffix>``  e.g.
    ``oberbueren_lastgang:objekt_305_bezug``. The ``<domain>:<name>`` shape
    is what HA requires for external statistics (no real entity backs it).
    """
    return f"{DOMAIN}:objekt_{objekt_id}_{messlinie.statistic_suffix}"


def build_statistic_metadata(
    objekt_id: int | str,
    messlinie: Messlinie,
    friendly_name: str,
) -> StatisticMetaData:
    """Build the metadata block sent to async_add_external_statistics.

    ``has_sum=True`` is what wires this statistic into the Energy Dashboard
    as a cumulative-energy series (kWh). ``has_mean=False`` because we don't
    push hourly mean power separately — the Energy Dashboard only needs sum.
    """
    return StatisticMetaData(
        has_mean=False,
        has_sum=True,
        name=f"{friendly_name} {messlinie.label}",
        source=DOMAIN,
        statistic_id=build_statistic_id(objekt_id, messlinie),
        unit_of_measurement="kWh",
    )


def aggregate_to_hourly_kwh(
    response: MessdatenResponse,
) -> list[tuple[datetime, float]]:
    """Bucket 15-min kW samples into hourly kWh totals (UTC hour-aligned).

    Returns a chronologically sorted list of ``(hour_start_utc, kwh)`` pairs.

    Note: the API's ``intervals[i].from`` is timezone-aware (Europe/Zurich).
    Converting to UTC and flooring to the hour is what HA expects; an
    interval that crosses a DST boundary will simply land in whichever UTC
    hour its ``from`` timestamp falls into, which is the correct behavior.
    """
    buckets: dict[datetime, float] = {}
    for interval, value_str in zip(response.intervals, response.values):
        # Values can be empty strings or "null" if the meter had a gap;
        # treat those as zero rather than failing the whole import.
        try:
            kw = float(value_str)
        except (TypeError, ValueError):
            _LOGGER.debug(
                "Skipping non-numeric sample %r at %s", value_str, interval.get("from")
            )
            continue

        from_local = datetime.fromisoformat(interval["from"])
        hour_utc = (
            from_local.astimezone(timezone.utc).replace(
                minute=0, second=0, microsecond=0
            )
        )
        buckets[hour_utc] = buckets.get(hour_utc, 0.0) + kw * _INTERVAL_HOURS

    return sorted(buckets.items())


async def async_get_last_sum(hass: HomeAssistant, statistic_id: str) -> float:
    """Look up the cumulative sum from the most recently stored hour.

    Returns 0.0 if no statistics exist yet (first-time import).
    """
    recorder = get_instance(hass)
    last_stats = await recorder.async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        statistic_id,
        True,  # convert_units
        {"sum"},
    )
    rows = last_stats.get(statistic_id)
    if not rows:
        return 0.0
    last_sum = rows[0].get("sum")
    return float(last_sum) if last_sum is not None else 0.0


def build_cost_statistic_id(objekt_id: int | str, category_key: str) -> str:
    """Statistic ID for one cost category (or ``total``)."""
    return f"{DOMAIN}:objekt_{objekt_id}_cost_{category_key}"


def build_cost_statistic_metadata(
    objekt_id: int | str,
    category_key: str,
    friendly_name: str,
) -> StatisticMetaData:
    label = COST_CATEGORY_LABELS.get(category_key, category_key)
    return StatisticMetaData(
        has_mean=False,
        has_sum=True,
        name=f"{friendly_name} {label}",
        source=DOMAIN,
        statistic_id=build_cost_statistic_id(objekt_id, category_key),
        unit_of_measurement=CURRENCY,
    )


async def async_import_many(
    hass: HomeAssistant,
    objekt_id: int | str,
    messlinie: Messlinie,
    friendly_name: str,
    responses: Iterable[MessdatenResponse],
    tariffs: TariffDatabase | None = None,
) -> int:
    """Import multiple days, plus matching cost statistics if tariffs given.

    Days are processed in input order (caller's responsibility to feed
    them chronologically). One previous-sum lookup per statistic_id, then
    a single ``async_add_external_statistics`` call per series.

    Returns the number of hourly kWh points written. Cost statistics are
    written for the same hours; their count equals the kWh count.
    """
    # ---- kWh series ------------------------------------------------------
    kwh_id = build_statistic_id(objekt_id, messlinie)
    kwh_meta = build_statistic_metadata(objekt_id, messlinie, friendly_name)
    kwh_running = await async_get_last_sum(hass, kwh_id)

    # Flatten all responses to a single chronological hourly series. We
    # also keep the kWh points around for cost computation below — there
    # is no point re-doing the 15-min→hour aggregation a second time.
    hourly_kwh: list[tuple[datetime, float]] = []
    for response in responses:
        hourly_kwh.extend(aggregate_to_hourly_kwh(response))

    if not hourly_kwh:
        return 0

    kwh_points: list[StatisticData] = []
    for hour_utc, kwh in hourly_kwh:
        kwh_running += kwh
        kwh_points.append(
            StatisticData(start=hour_utc, sum=kwh_running, state=kwh_running)
        )
    async_add_external_statistics(hass, kwh_meta, kwh_points)
    _LOGGER.info(
        "Imported %d hourly points for %s (final sum: %.3f kWh)",
        len(kwh_points), kwh_id, kwh_running,
    )

    # ---- Cost series -----------------------------------------------------
    # Only consumption Messlinien have cost semantics in V1. Production
    # would need a separate tariff structure (selling rate) which we
    # don't model yet.
    if tariffs is not None and messlinie.direction == "consumption":
        await _async_import_costs(
            hass, objekt_id, friendly_name, hourly_kwh, tariffs
        )

    return len(kwh_points)


async def _async_import_costs(
    hass: HomeAssistant,
    objekt_id: int | str,
    friendly_name: str,
    hourly_kwh: list[tuple[datetime, float]],
    tariffs: TariffDatabase,
) -> None:
    """Compute and write all six cost statistics for one batch of hours."""
    per_category = compute_hourly_costs(hourly_kwh, tariffs)

    for category_key in (*COST_CATEGORY_KEYS, COST_TOTAL_KEY):
        stat_id = build_cost_statistic_id(objekt_id, category_key)
        meta = build_cost_statistic_metadata(objekt_id, category_key, friendly_name)
        running = await async_get_last_sum(hass, stat_id)

        points: list[StatisticData] = []
        for hour_utc, increment in per_category[category_key]:
            running += increment
            points.append(
                StatisticData(start=hour_utc, sum=running, state=running)
            )

        async_add_external_statistics(hass, meta, points)

    _LOGGER.info(
        "Imported cost statistics for objekt_%s across %d categories "
        "(%d hourly points each)",
        objekt_id,
        len(COST_CATEGORY_KEYS) + 1,
        len(hourly_kwh),
    )
