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
from .const import DOMAIN, Messlinie

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


async def async_import_messdaten(
    hass: HomeAssistant,
    objekt_id: int | str,
    messlinie: Messlinie,
    friendly_name: str,
    response: MessdatenResponse,
) -> int:
    """Convert and import one day's response into HA statistics.

    Returns the number of hourly statistic points written.
    """
    statistic_id = build_statistic_id(objekt_id, messlinie)
    metadata = build_statistic_metadata(objekt_id, messlinie, friendly_name)

    hourly = aggregate_to_hourly_kwh(response)
    if not hourly:
        _LOGGER.warning(
            "No usable samples in response for %s %s",
            statistic_id,
            response.intervals[0]["from"] if response.intervals else "<empty>",
        )
        return 0

    # Anchor the running sum to whatever already exists for this stat_id
    # *before* the first hour we're about to write. For backfill scenarios
    # (importing an older window) this could over-count, so callers doing
    # backfill must import in chronological order from the earliest day.
    running_sum = await async_get_last_sum(hass, statistic_id)

    points: list[StatisticData] = []
    for hour_utc, kwh in hourly:
        running_sum += kwh
        points.append(
            StatisticData(start=hour_utc, sum=running_sum, state=running_sum)
        )

    async_add_external_statistics(hass, metadata, points)
    _LOGGER.info(
        "Imported %d hourly points for %s (last hour: %s, total: %.3f kWh)",
        len(points),
        statistic_id,
        points[-1]["start"].isoformat(),
        running_sum,
    )
    return len(points)


async def async_import_many(
    hass: HomeAssistant,
    objekt_id: int | str,
    messlinie: Messlinie,
    friendly_name: str,
    responses: Iterable[MessdatenResponse],
) -> int:
    """Import multiple days in chronological order, sharing one running sum.

    More efficient than calling async_import_messdaten in a loop because we
    only look up the last sum once.
    """
    statistic_id = build_statistic_id(objekt_id, messlinie)
    metadata = build_statistic_metadata(objekt_id, messlinie, friendly_name)
    running_sum = await async_get_last_sum(hass, statistic_id)

    points: list[StatisticData] = []
    for response in responses:
        for hour_utc, kwh in aggregate_to_hourly_kwh(response):
            running_sum += kwh
            points.append(
                StatisticData(start=hour_utc, sum=running_sum, state=running_sum)
            )

    if not points:
        return 0

    async_add_external_statistics(hass, metadata, points)
    _LOGGER.info(
        "Imported %d hourly points for %s (final sum: %.3f kWh)",
        len(points),
        statistic_id,
        running_sum,
    )
    return len(points)
