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
from datetime import datetime, timedelta, timezone
from typing import Iterable

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData

# StatisticMeanType is the modern way to declare "this stat has no
# arithmetic mean" — older HA used ``has_mean=False``. Both fields are
# accepted by current HA, but ``mean_type`` is required from 2026.11
# onward. Conditional import keeps us compatible with older versions.
try:
    from homeassistant.components.recorder.models import StatisticMeanType
    _MEAN_TYPE_NONE: object | None = StatisticMeanType.NONE
except ImportError:                                            # pragma: no cover
    _MEAN_TYPE_NONE = None
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
    as a cumulative-energy series (kWh). We declare no mean (cumulative
    energy doesn't have a meaningful arithmetic mean over a period).
    """
    return _build_meta(
        statistic_id=build_statistic_id(objekt_id, messlinie),
        name=f"{friendly_name} {messlinie.label}",
        unit="kWh",
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
    return _build_meta(
        statistic_id=build_cost_statistic_id(objekt_id, category_key),
        name=f"{friendly_name} {label}",
        unit=CURRENCY,
    )


def _build_meta(*, statistic_id: str, name: str, unit: str) -> StatisticMetaData:
    """Construct a StatisticMetaData using whichever ``has_mean`` /
    ``mean_type`` fields the running HA version expects.

    Both old and new HA still accept ``has_mean``, but new HA also
    requires (or warns when missing) ``mean_type``. We provide both
    when the new enum is available, so the metadata is acceptable on
    any version we support.
    """
    kwargs: dict = {
        "has_mean": False,
        "has_sum": True,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }
    if _MEAN_TYPE_NONE is not None:
        kwargs["mean_type"] = _MEAN_TYPE_NONE
    return StatisticMetaData(**kwargs)


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
    *,
    fresh_anchor: bool = False,
) -> None:
    """Compute and write all six cost statistics for one batch of hours.

    By default the cumulative sum continues from whatever was previously
    stored for each cost stat (the same anchoring behavior the kWh path
    uses). When ``fresh_anchor=True`` we instead start the running sum
    at zero — used by ``async_recompute_costs`` which rebuilds the
    entire chain from the first available hour, making the prior
    stored sums irrelevant.
    """
    per_category = compute_hourly_costs(hourly_kwh, tariffs)

    final_sums: dict[str, float] = {}
    for category_key in (*COST_CATEGORY_KEYS, COST_TOTAL_KEY):
        stat_id = build_cost_statistic_id(objekt_id, category_key)
        meta = build_cost_statistic_metadata(objekt_id, category_key, friendly_name)
        running = 0.0 if fresh_anchor else await async_get_last_sum(hass, stat_id)

        points: list[StatisticData] = []
        for hour_utc, increment in per_category[category_key]:
            running += increment
            points.append(
                StatisticData(start=hour_utc, sum=running, state=running)
            )

        async_add_external_statistics(hass, meta, points)
        final_sums[category_key] = running

    _LOGGER.info(
        "Imported cost statistics for objekt_%s across %d categories "
        "(%d hourly points each, fresh_anchor=%s). Final sums: %s",
        objekt_id,
        len(COST_CATEGORY_KEYS) + 1,
        len(hourly_kwh),
        fresh_anchor,
        ", ".join(f"{k}={v:.2f}" for k, v in final_sums.items()),
    )


async def async_recompute_costs(
    hass: HomeAssistant,
    objekt_id: int | str,
    messlinie: Messlinie,
    friendly_name: str,
    tariffs: TariffDatabase,
) -> int:
    """Rebuild cost statistics from existing kWh statistics — no API calls.

    Reads every available hourly kWh ``change`` from the recorder for
    the given Messlinie, applies the current tariff database, and
    overwrites the six cost statistics from scratch (anchor = 0). Use
    this after editing the tariff file or when the cost feature was
    enabled on top of pre-existing kWh data.

    Returns the number of hourly points recomputed; 0 if no kWh stats
    exist for the Messlinie or the Messlinie is non-consumption.
    """
    if messlinie.direction != "consumption":
        return 0

    kwh_id = build_statistic_id(objekt_id, messlinie)

    # Pull every kWh hour available — we don't expose a date filter
    # because partial recompute leaves the cumulative sums of *later*
    # untouched hours mis-anchored, which is much worse than the cost
    # of one extra DB scan.
    very_early = datetime(2020, 1, 1, tzinfo=timezone.utc)
    far_future = datetime.now(tz=timezone.utc) + timedelta(days=1)
    recorder = get_instance(hass)
    rows = await recorder.async_add_executor_job(
        statistics_during_period,
        hass, very_early, far_future,
        {kwh_id}, "hour", None, {"change"},
    )
    raw_rows = rows.get(kwh_id, [])
    if not raw_rows:
        _LOGGER.warning(
            "No kWh statistics found for %s — nothing to recompute. "
            "Check Developer Tools → Statistics whether this ID exists.",
            kwh_id,
        )
        return 0

    hourly_kwh: list[tuple[datetime, float]] = []
    for row in raw_rows:
        start = row.get("start")
        # Some recorder versions deliver epoch floats here.
        if not isinstance(start, datetime):
            start = datetime.fromtimestamp(float(start), tz=timezone.utc)
        change = row.get("change")
        hourly_kwh.append((start, float(change) if change is not None else 0.0))

    total_kwh = sum(k for _, k in hourly_kwh)
    _LOGGER.info(
        "Recompute kWh source for %s: %d hourly rows, %.3f kWh total, "
        "first=%s, last=%s",
        kwh_id, len(hourly_kwh), total_kwh,
        hourly_kwh[0][0].isoformat(),
        hourly_kwh[-1][0].isoformat(),
    )

    await _async_import_costs(
        hass, objekt_id, friendly_name, hourly_kwh, tariffs,
        fresh_anchor=True,
    )
    return len(hourly_kwh)
