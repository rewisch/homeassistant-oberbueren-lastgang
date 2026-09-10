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
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Iterable, NamedTuple
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData

# StatisticMeanType is the modern way to declare a statistic's mean
# behaviour — older HA used ``has_mean``. Both fields are accepted by
# current HA, but ``mean_type`` is required from 2026.11 onward.
# Conditional import keeps us compatible with older versions.
try:
    from homeassistant.components.recorder.models import StatisticMeanType
    _MEAN_TYPE_NONE: object | None = StatisticMeanType.NONE
    _MEAN_TYPE_ARITHMETIC: object | None = StatisticMeanType.ARITHMETIC
except ImportError:                                            # pragma: no cover
    _MEAN_TYPE_NONE = None
    _MEAN_TYPE_ARITHMETIC = None
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
_LOCAL_TZ = ZoneInfo("Europe/Zurich")

# All API-side power values are in kW; one 15-min sample = 0.25h of energy.
_INTERVAL_HOURS = 0.25

# Suffix for the hourly power (kW) statistic — mean/min/max, no sum.
_LEISTUNG_SUFFIX = "leistung"


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


async def async_get_last_imported_hour(
    hass: HomeAssistant, statistic_id: str
) -> datetime | None:
    """Return the timestamp of the most recently stored hour, or None."""
    recorder = get_instance(hass)
    last_stats = await recorder.async_add_executor_job(
        get_last_statistics,
        hass,
        1,
        statistic_id,
        True,
        {"sum"},
    )
    rows = last_stats.get(statistic_id)
    if not rows:
        return None
    start = rows[0].get("start")
    if isinstance(start, datetime):
        return start
    if start is None:
        return None
    return datetime.fromtimestamp(float(start), tz=timezone.utc)


class StoredDayCoverage(NamedTuple):
    """What is already stored for one local day: hour count and total kWh."""

    hours: int
    kwh: float


async def async_stored_coverage_per_day(
    hass: HomeAssistant,
    statistic_id: str,
    start: date,
    end: date,
    local_tz: tzinfo,
) -> dict[date, StoredDayCoverage]:
    """Return ``{local_date: StoredDayCoverage(hours, kwh)}`` over ``[start, end]``.

    Used by the daily catch-up / backfill to decide whether a freshly
    fetched day is actually *better* than what's already stored before
    triggering a (potentially expensive) re-import. We compare both the
    hour count and the summed energy: a day that upstream first served
    as mostly ``0.000`` placeholders lands with a full 24 rows but a tiny
    kWh total, so an hour-count check alone would wrongly treat it as
    complete forever. A day not present in the result has zero coverage.
    """
    range_start_local = datetime.combine(
        start, datetime.min.time(), tzinfo=local_tz
    )
    range_end_local = datetime.combine(
        end + timedelta(days=1), datetime.min.time(), tzinfo=local_tz
    )
    # One-hour padding either side absorbs DST edge cases when bucketing
    # back from UTC into the local date.
    range_start_utc = range_start_local.astimezone(timezone.utc) - timedelta(hours=1)
    range_end_utc = range_end_local.astimezone(timezone.utc) + timedelta(hours=1)

    recorder = get_instance(hass)
    rows = await recorder.async_add_executor_job(
        statistics_during_period,
        hass, range_start_utc, range_end_utc,
        {statistic_id}, "hour", None, {"change"},
    )
    hours: dict[date, int] = {}
    kwh: dict[date, float] = {}
    for row in rows.get(statistic_id, []):
        ts = row.get("start")
        if not isinstance(ts, datetime):
            ts = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        local_date = ts.astimezone(local_tz).date()
        if start <= local_date <= end:
            hours[local_date] = hours.get(local_date, 0) + 1
            kwh[local_date] = kwh.get(local_date, 0.0) + float(
                row.get("change") or 0.0
            )
    return {
        d: StoredDayCoverage(hours=h, kwh=kwh.get(d, 0.0))
        for d, h in hours.items()
    }


async def _async_read_existing_hourly_kwh(
    hass: HomeAssistant,
    statistic_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[datetime, float]:
    """Pull stored hourly kWh ``change`` for one statistic.

    Used by the overlap-rebuild path so we can splice new days into a
    chronologically-correct cumulative chain. ``since`` / ``until`` (UTC)
    bound the query for the month-scoped cost rebuild; ``until`` is
    exclusive. Returns an empty dict if nothing exists in range.
    """
    start = since or datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = until or (datetime.now(tz=timezone.utc) + timedelta(days=1))
    recorder = get_instance(hass)
    rows = await recorder.async_add_executor_job(
        statistics_during_period,
        hass, start, end,
        {statistic_id}, "hour", None, {"change"},
    )
    out: dict[datetime, float] = {}
    for row in rows.get(statistic_id, []):
        row_start = row.get("start")
        if not isinstance(row_start, datetime):
            row_start = datetime.fromtimestamp(float(row_start), tz=timezone.utc)
        out[row_start] = float(row.get("change") or 0.0)
    return out


async def _async_read_existing_hourly_power(
    hass: HomeAssistant,
    statistic_id: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[datetime, HourlyPower]:
    """Pull stored hourly power (mean/min/max kW) for the Leistung series.

    ``until`` is exclusive. Empty dict if nothing exists in range (e.g.
    data imported before the power series was introduced).
    """
    start = since or datetime(2020, 1, 1, tzinfo=timezone.utc)
    end = until or (datetime.now(tz=timezone.utc) + timedelta(days=1))
    recorder = get_instance(hass)
    rows = await recorder.async_add_executor_job(
        statistics_during_period,
        hass, start, end,
        {statistic_id}, "hour", None, {"mean", "min", "max"},
    )
    out: dict[datetime, HourlyPower] = {}
    for row in rows.get(statistic_id, []):
        row_start = row.get("start")
        if not isinstance(row_start, datetime):
            row_start = datetime.fromtimestamp(float(row_start), tz=timezone.utc)
        out[row_start] = HourlyPower(
            mean_kw=float(row.get("mean") or 0.0),
            min_kw=float(row.get("min") or 0.0),
            max_kw=float(row.get("max") or 0.0),
        )
    return out


async def _async_sum_before(
    hass: HomeAssistant, statistic_id: str, ts_utc: datetime
) -> float:
    """Cumulative ``sum`` at the last stored hour strictly before ``ts_utc``.

    Anchors a month-scoped cost rebuild onto the already-stored chain.
    Returns 0.0 when nothing precedes ``ts_utc`` (or there's a gap wider
    than the lookback window — in practice the series is contiguous
    across month boundaries).
    """
    recorder = get_instance(hass)
    rows = await recorder.async_add_executor_job(
        statistics_during_period,
        hass, ts_utc - timedelta(days=3), ts_utc,
        {statistic_id}, "hour", None, {"sum"},
    )
    series = rows.get(statistic_id, [])
    if not series:
        return 0.0
    last_sum = series[-1].get("sum")
    return float(last_sum) if last_sum is not None else 0.0


def _month_start_utc(dt_utc: datetime) -> datetime:
    """UTC instant of 00:00 on the 1st of ``dt_utc``'s local month."""
    local = dt_utc.astimezone(_LOCAL_TZ)
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first.astimezone(timezone.utc)


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


def _build_meta(
    *,
    statistic_id: str,
    name: str,
    unit: str,
    has_sum: bool = True,
    has_mean: bool = False,
) -> StatisticMetaData:
    """Construct a StatisticMetaData using whichever ``has_mean`` /
    ``mean_type`` fields the running HA version expects.

    Both old and new HA still accept ``has_mean``, but new HA also
    requires (or warns when missing) ``mean_type``. We provide both
    when the new enum is available, so the metadata is acceptable on
    any version we support.

    Cumulative series (kWh, CHF) use ``has_sum=True``; the power series
    uses ``has_mean=True`` instead so HA stores/plots hourly mean with a
    min/max band.
    """
    kwargs: dict = {
        "has_mean": has_mean,
        "has_sum": has_sum,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }
    if _MEAN_TYPE_NONE is not None:
        kwargs["mean_type"] = _MEAN_TYPE_ARITHMETIC if has_mean else _MEAN_TYPE_NONE
    return StatisticMetaData(**kwargs)


def build_power_statistic_id(objekt_id: int | str) -> str:
    """External-statistics ID for the hourly power (kW) series."""
    return f"{DOMAIN}:objekt_{objekt_id}_{_LEISTUNG_SUFFIX}"


def build_power_statistic_metadata(
    objekt_id: int | str, friendly_name: str
) -> StatisticMetaData:
    """Metadata for the hourly power series (kW, mean + min/max, no sum)."""
    return _build_meta(
        statistic_id=build_power_statistic_id(objekt_id),
        name=f"{friendly_name} Leistung",
        unit="kW",
        has_sum=False,
        has_mean=True,
    )


class HourlyPower(NamedTuple):
    """Power (kW) summary for one clock hour, from its ≤4 fifteen-min samples."""

    mean_kw: float
    min_kw: float
    max_kw: float


def aggregate_to_hourly_power_kw(
    response: MessdatenResponse,
) -> dict[datetime, HourlyPower]:
    """Bucket the 15-min kW samples into per-UTC-hour mean/min/max.

    The monthly demand charge is billed on the highest 15-minute average
    power, so ``max_kw`` here is the max of that hour's 15-min samples —
    the monthly peak is then the max of those hourly maxima.
    """
    samples: dict[datetime, list[float]] = {}
    for interval, value_str in zip(response.intervals, response.values):
        try:
            kw = float(value_str)
        except (TypeError, ValueError):
            continue
        from_local = datetime.fromisoformat(interval["from"])
        hour_utc = from_local.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        samples.setdefault(hour_utc, []).append(kw)
    return {
        hour: HourlyPower(
            mean_kw=sum(vals) / len(vals),
            min_kw=min(vals),
            max_kw=max(vals),
        )
        for hour, vals in samples.items()
    }


async def async_import_many(
    hass: HomeAssistant,
    objekt_id: int | str,
    messlinie: Messlinie,
    friendly_name: str,
    responses: Iterable[MessdatenResponse],
    tariffs: TariffDatabase | None = None,
) -> int:
    """Import multiple days, plus matching cost statistics if tariffs given.

    Picks one of two strategies based on whether the new data overlaps
    pre-existing statistics:

      * **Append** (fast): new hours are strictly after every stored
        hour. Anchor on ``last_sum``, build cumulative chain forward.
        This is the daily-import path and stays cheap.

      * **Rebuild** (safe): new hours overlap or precede stored hours.
        Read every existing hourly kWh ``change`` from the recorder,
        merge with the new data (new wins on collision), sort
        chronologically, and re-emit the entire cumulative chain from
        zero. This makes re-running ``backfill`` over an old window
        idempotent — no more "Fix issues in Statistics" workaround.

    Also writes the hourly power (kW) series — mean/min/max per hour —
    which backs the Leistung graph and the monthly demand-charge peak.

    Returns the number of *new* hourly points the caller's responses
    produced. (Rebuild may write many more rows than that to maintain
    chain integrity, but the return value still reflects the size of
    the input.)
    """
    kwh_id = build_statistic_id(objekt_id, messlinie)
    kwh_meta = build_statistic_metadata(objekt_id, messlinie, friendly_name)
    power_id = build_power_statistic_id(objekt_id)
    power_meta = build_power_statistic_metadata(objekt_id, friendly_name)

    # Flatten responses to one chronological hourly series (kWh + power).
    new_hourly: list[tuple[datetime, float]] = []
    new_power: dict[datetime, HourlyPower] = {}
    for response in responses:
        new_hourly.extend(aggregate_to_hourly_kwh(response))
        new_power.update(aggregate_to_hourly_power_kw(response))

    if not new_hourly:
        return 0
    new_hourly.sort()  # cheap and ensures the rebuild path's anchor logic

    last_existing = await async_get_last_imported_hour(hass, kwh_id)
    overlap = last_existing is not None and new_hourly[0][0] <= last_existing

    # --- kWh cumulative series ------------------------------------------
    merged: dict[datetime, float] | None = None
    merged_pairs: list[tuple[datetime, float]] | None = None
    if overlap:
        merged = await _async_read_existing_hourly_kwh(hass, kwh_id)
        for hour, kwh in new_hourly:
            merged[hour] = kwh
        merged_pairs = sorted(merged.items())

        running = 0.0
        kwh_points: list[StatisticData] = []
        for hour_utc, kwh in merged_pairs:
            running += kwh
            kwh_points.append(
                StatisticData(start=hour_utc, sum=running, state=running)
            )
        async_add_external_statistics(hass, kwh_meta, kwh_points)
        _LOGGER.info(
            "Imported %d hourly points for %s via REBUILD "
            "(merged %d new + %d existing, final sum %.3f kWh)",
            len(new_hourly), kwh_id, len(new_hourly),
            len(merged_pairs) - len(new_hourly), running,
        )
    else:
        running = await async_get_last_sum(hass, kwh_id)
        kwh_points = []
        for hour_utc, kwh in new_hourly:
            running += kwh
            kwh_points.append(
                StatisticData(start=hour_utc, sum=running, state=running)
            )
        async_add_external_statistics(hass, kwh_meta, kwh_points)
        _LOGGER.info(
            "Imported %d hourly points for %s via APPEND "
            "(final sum %.3f kWh)",
            len(new_hourly), kwh_id, running,
        )

    # --- hourly power (kW) series: mean/min/max, no cumulative sum ------
    merged_power: dict[datetime, HourlyPower] | None = None
    if overlap:
        merged_power = await _async_read_existing_hourly_power(hass, power_id)
        merged_power.update(new_power)
        power_source: dict[datetime, HourlyPower] = merged_power
    else:
        power_source = new_power
    power_points = [
        StatisticData(start=h, mean=p.mean_kw, min=p.min_kw, max=p.max_kw)
        for h, p in sorted(power_source.items())
    ]
    if power_points:
        async_add_external_statistics(hass, power_meta, power_points)

    # --- cost series --------------------------------------------------
    # Only on consumption Messlinien (Einspeisung would need a separate
    # selling-rate tariff that we don't model yet).
    if tariffs is not None and messlinie.direction == "consumption":
        if tariffs.has_power_position:
            # A monthly demand charge needs the whole month in view →
            # month-scoped rebuild (see the function's docstring).
            await _async_import_costs_month_scoped(
                hass, objekt_id, friendly_name, tariffs,
                kwh_id=kwh_id,
                power_id=power_id,
                new_kwh=new_hourly,
                new_power=new_power,
                full_rebuild=overlap,
                merged_kwh=merged if overlap else None,
                merged_power=merged_power if overlap else None,
            )
        else:
            await _async_import_costs(
                hass, objekt_id, friendly_name,
                merged_pairs if overlap else new_hourly, tariffs,
                fresh_anchor=overlap,
            )

    return len(new_hourly)


async def _async_import_costs(
    hass: HomeAssistant,
    objekt_id: int | str,
    friendly_name: str,
    hourly_kwh: list[tuple[datetime, float]],
    tariffs: TariffDatabase,
    *,
    fresh_anchor: bool = False,
    hourly_peak_kw: dict[datetime, float] | None = None,
) -> None:
    """Compute and write all cost statistics for one batch of hours.

    By default the cumulative sum continues from whatever was previously
    stored for each cost stat (the same anchoring behavior the kWh path
    uses). When ``fresh_anchor=True`` we instead start the running sum
    at zero — used by ``async_recompute_costs`` which rebuilds the
    entire chain from the first available hour, making the prior
    stored sums irrelevant.

    ``hourly_peak_kw`` (hour → max 15-min kW) enables the monthly demand
    charge; pass it only for a rebuild that spans whole months.
    """
    per_category = compute_hourly_costs(
        hourly_kwh, tariffs, hourly_peak_kw=hourly_peak_kw
    )

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


async def _async_import_costs_month_scoped(
    hass: HomeAssistant,
    objekt_id: int | str,
    friendly_name: str,
    tariffs: TariffDatabase,
    *,
    kwh_id: str,
    power_id: str,
    new_kwh: list[tuple[datetime, float]],
    new_power: dict[datetime, HourlyPower],
    full_rebuild: bool,
    merged_kwh: dict[datetime, float] | None,
    merged_power: dict[datetime, HourlyPower] | None,
) -> None:
    """Write cost statistics for a tariff regime with a monthly demand charge.

    The Leistungspreis lands entirely on the hour that set each calendar
    month's 15-minute peak, so a correct figure needs the whole month in
    view — a plain per-batch append can't produce it. Two cases:

      * ``full_rebuild`` (the kWh REBUILD path just ran): ``merged_*``
        already hold the complete history in memory → recompute every
        cost series from zero.
      * otherwise (APPEND path): extend the in-memory new days backwards
        to the start of the earliest affected local month using stored
        rows, then recompute the cost series from that month start,
        anchored on the cumulative sum just before it. Stored rows read
        here were written on earlier runs, so they're safely flushed;
        the just-written new rows are taken from memory to dodge the
        recorder's async write lag.
    """
    categories = (*COST_CATEGORY_KEYS, COST_TOTAL_KEY)

    if full_rebuild and merged_kwh is not None:
        combined_kwh: dict[datetime, float] = dict(merged_kwh)
        combined_power: dict[datetime, HourlyPower] = dict(merged_power or {})
        combined_power.update(new_power)
        since = min(combined_kwh)
        anchors = {cat: 0.0 for cat in categories}
    else:
        earliest_new = new_kwh[0][0]
        since = _month_start_utc(earliest_new)
        stored_kwh = await _async_read_existing_hourly_kwh(
            hass, kwh_id, since=since, until=earliest_new
        )
        stored_power = await _async_read_existing_hourly_power(
            hass, power_id, since=since, until=earliest_new
        )
        combined_kwh = {**stored_kwh, **dict(new_kwh)}
        combined_power = {**stored_power, **new_power}
        anchors = {
            cat: await _async_sum_before(
                hass, build_cost_statistic_id(objekt_id, cat), since
            )
            for cat in categories
        }

    hourly_kwh = sorted(combined_kwh.items())
    peak_map = {h: p.max_kw for h, p in combined_power.items()}
    per_category = compute_hourly_costs(
        hourly_kwh, tariffs, hourly_peak_kw=peak_map
    )

    final_sums: dict[str, float] = {}
    for category_key in categories:
        meta = build_cost_statistic_metadata(
            objekt_id, category_key, friendly_name
        )
        running = anchors[category_key]
        points: list[StatisticData] = []
        for hour_utc, increment in per_category[category_key]:
            running += increment
            points.append(
                StatisticData(start=hour_utc, sum=running, state=running)
            )
        async_add_external_statistics(hass, meta, points)
        final_sums[category_key] = running

    _LOGGER.info(
        "Imported cost statistics for objekt_%s (Leistungs-Tarif) from %s "
        "over %d hourly points, full_rebuild=%s. Final sums: %s",
        objekt_id, since.isoformat(), len(hourly_kwh), full_rebuild,
        ", ".join(f"{k}={v:.2f}" for k, v in final_sums.items()),
    )


async def async_recompute_costs(
    hass: HomeAssistant,
    objekt_id: int | str,
    messlinie: Messlinie,
    friendly_name: str,
    tariffs: TariffDatabase,
) -> int:
    """Rebuild cost statistics from data already in HA — no API calls.

    Reads every available hourly kWh ``change`` (and the real hourly power
    series, for the demand charge) from the recorder for the given
    Messlinie, applies the current tariff database, and overwrites every
    cost statistic from scratch (anchor = 0). Use this after editing the
    tariff file (e.g. switching to the 2027 regime) or when the cost
    feature was enabled on top of pre-existing kWh data.

    The monthly demand charge needs the 15-minute-derived power series
    (written by imports). For hours where it is absent — data imported
    before that series existed — ``cost_netznutzung_leistung`` stays 0
    for that month until a real ``backfill`` re-fetches the 15-minute
    data. We deliberately do not approximate the peak from hourly kWh:
    the demand charge must be billed on an accurate 15-minute figure.

    Returns the number of hourly points recomputed; 0 if no kWh stats
    exist for the Messlinie or the Messlinie is non-consumption.
    """
    if messlinie.direction != "consumption":
        return 0

    kwh_id = build_statistic_id(objekt_id, messlinie)

    # Partial recompute is unsafe (it'd leave the cumulative sums of
    # *later* untouched hours mis-anchored), so we always rebuild over
    # all available data.
    existing = await _async_read_existing_hourly_kwh(hass, kwh_id)
    if not existing:
        _LOGGER.warning(
            "No kWh statistics found for %s — nothing to recompute. "
            "Check Developer Tools → Statistics whether this ID exists.",
            kwh_id,
        )
        return 0

    hourly_kwh = sorted(existing.items())
    total_kwh = sum(k for _, k in hourly_kwh)

    # Real (15-min-derived) power series feeds the monthly demand charge.
    # Absent for hours imported before the power series existed → those
    # months get no Leistung cost until a real backfill fills them.
    power = await _async_read_existing_hourly_power(
        hass, build_power_statistic_id(objekt_id)
    )
    peak_map = {h: p.max_kw for h, p in power.items()}

    _LOGGER.info(
        "Recompute source for %s: %d hourly kWh rows, %.3f kWh total, "
        "%d real hourly power rows, first=%s, last=%s",
        kwh_id, len(hourly_kwh), total_kwh, len(peak_map),
        hourly_kwh[0][0].isoformat(),
        hourly_kwh[-1][0].isoformat(),
    )

    await _async_import_costs(
        hass, objekt_id, friendly_name, hourly_kwh, tariffs,
        fresh_anchor=True,
        hourly_peak_kw=peak_map or None,
    )
    return len(hourly_kwh)
