"""Daily polling coordinator.

Each morning we fetch the *previous* day's load curve and import it into
HA's long-term statistics. We don't use a tight 15-minute polling loop
because the upstream API publishes data with a delay of several hours —
yesterday's data is only fully available the next morning.

The actual time-of-day trigger is owned by ``__init__.py`` (a fixed
``async_track_time_change`` at ~06:00 local). The coordinator's role is
just to encapsulate "fetch and import N days" as a reusable operation
that the time trigger and the backfill service both call.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import ApiError, AuthError, OberbuerenClient
from .const import ACTIVE_MESSLINIEN, CONF_METERINGCODE, CONF_NAME, CONF_OBJEKT_ID
from .statistics import (
    aggregate_to_hourly_kwh,
    async_count_stored_hours_per_day,
    async_get_last_imported_hour,
    async_import_many,
    build_statistic_id,
)
from .tariffs import load_tariffs

# Cap auto catch-up so an HA host that's been off for months doesn't
# unexpectedly fire a year of API requests on next boot. Bigger gaps
# require an explicit ``backfill`` service call from the user.
_MAX_AUTO_CATCHUP_DAYS = 30

# Each catch-up re-checks at least the last N days regardless of what's
# already stored. This is the antidote to "an early-morning slot
# imported only a partial day because upstream hadn't published the
# rest yet" — the next slot (or the next day's run) will see that the
# stored count is below the now-published count and re-import. Without
# this lookback the partial day would be locked in forever, because
# "last imported day == yesterday" makes the simple anchor-based check
# think we're done.
_DAILY_LOOKBACK_DAYS = 3

_LOCAL_TZ = ZoneInfo("Europe/Zurich")

_LOGGER = logging.getLogger(__name__)


class LastgangCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Owns the API client and the import lifecycle for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: OberbuerenClient,
    ) -> None:
        # update_interval is None: we drive imports via an explicit time
        # trigger (see __init__.py) and via the backfill service. We still
        # use DataUpdateCoordinator because it gives us a clean shutdown
        # hook and a place to hang shared state.
        super().__init__(
            hass,
            _LOGGER,
            name=f"oberbueren_lastgang[{entry.data[CONF_NAME]}]",
            update_interval=None,
        )
        self.entry = entry
        self.client = client
        self.objekt_id: int | str = entry.data[CONF_OBJEKT_ID]
        self.meteringcode: str = entry.data[CONF_METERINGCODE]
        self.friendly_name: str = entry.data[CONF_NAME]

    async def async_import_range(self, start: date, end: date) -> int:
        """Import all days in ``[start, end]`` (inclusive) for active Messlinien.

        For each (day, Messlinie) we fetch upstream and then decide
        whether to actually write:

          * Upstream raised or returned no usable points → leave
            stored data alone (never destroy on transient failure).
          * Stored hourly-point count for the day is already ≥ the
            count produced by the new response → skip the (expensive)
            import path. This makes daily re-checks of recent days a
            cheap no-op once the day is complete.
          * Otherwise → enqueue the response. ``async_import_many``
            takes the merge path which correctly splices new hours
            into an existing partial day.

        Days are processed in chronological order so the cumulative
        sum stays monotonically increasing. Failures on individual
        days are logged and skipped — a single missing day shouldn't
        abort a multi-month backfill.
        """
        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end})")

        total_imported = 0
        for messlinie in ACTIVE_MESSLINIEN:
            stat_id = build_statistic_id(self.objekt_id, messlinie)
            stored_per_day = await async_count_stored_hours_per_day(
                self.hass, stat_id, start, end, _LOCAL_TZ,
            )

            responses = []
            current = start
            while current <= end:
                try:
                    response = await self.client.async_fetch_messdaten(
                        self.objekt_id,
                        self.meteringcode,
                        messlinie.obis,
                        current,
                    )
                except AuthError:
                    # Re-raise auth errors — they need user action.
                    raise
                except ApiError as err:
                    _LOGGER.warning(
                        "Skipping %s for %s: %s", current, messlinie.label, err
                    )
                    current += timedelta(days=1)
                    continue

                new_hourly = aggregate_to_hourly_kwh(response)
                if not new_hourly:
                    _LOGGER.info(
                        "No %s data published yet for %s — leaving "
                        "stored data untouched.",
                        messlinie.label, current,
                    )
                    current += timedelta(days=1)
                    continue

                stored = stored_per_day.get(current, 0)
                if len(new_hourly) <= stored:
                    _LOGGER.debug(
                        "Day %s already complete for %s (%d stored vs "
                        "%d new) — skipping import.",
                        current, messlinie.label, stored, len(new_hourly),
                    )
                    current += timedelta(days=1)
                    continue

                _LOGGER.info(
                    "Importing %s for %s: %d stored → %d new hourly point(s)",
                    current, messlinie.label, stored, len(new_hourly),
                )
                responses.append(response)
                current += timedelta(days=1)

            if responses:
                # Re-load tariffs each import so the user can edit
                # ``oberbueren_lastgang_tariffs.yaml`` between runs without
                # restarting HA. Loading is a small file read; cheap.
                tariffs = await self.hass.async_add_executor_job(
                    load_tariffs, self.hass.config.config_dir
                )
                count = await async_import_many(
                    self.hass,
                    self.objekt_id,
                    messlinie,
                    self.friendly_name,
                    responses,
                    tariffs=tariffs,
                )
                total_imported += count

        return total_imported

    async def async_catch_up(self) -> int:
        """Re-fetch the recent days and fill any gap up to yesterday.

        Instead of trusting "we have *some* hour for yesterday → done",
        we always re-check a rolling window of ``_DAILY_LOOKBACK_DAYS``
        days. ``async_import_range`` is responsible for deciding per
        day whether new data is actually better than what's stored, so
        days that are already complete are cheap no-ops.

        If the integration has been offline long enough that the gap
        between the last stored day and yesterday exceeds the window,
        the window is widened backwards to cover the gap — up to
        ``_MAX_AUTO_CATCHUP_DAYS``. Larger gaps trigger a warning and
        a partial catch-up; the user is expected to run ``backfill``
        for the rest.

        First-install (no stored data) still returns without fetching
        — we don't know how far back the user wants history.
        """
        kwh_id = build_statistic_id(self.objekt_id, ACTIVE_MESSLINIEN[0])
        last_hour = await async_get_last_imported_hour(self.hass, kwh_id)

        today = datetime.now(tz=_LOCAL_TZ).date()
        yesterday = today - timedelta(days=1)

        if last_hour is None:
            _LOGGER.info(
                "No prior kWh data for %s — skipping catch-up. Use the "
                "backfill service to populate an initial date range.",
                kwh_id,
            )
            return 0

        last_day = last_hour.astimezone(_LOCAL_TZ).date()

        lookback_start = yesterday - timedelta(days=_DAILY_LOOKBACK_DAYS - 1)
        # If there's a real gap (last imported day before the lookback
        # window starts), widen the window backwards to cover it.
        gap_start = last_day + timedelta(days=1) if last_day < yesterday else yesterday
        cap_start = yesterday - timedelta(days=_MAX_AUTO_CATCHUP_DAYS - 1)
        window_start = max(min(lookback_start, gap_start), cap_start)

        if last_day < cap_start - timedelta(days=1):
            _LOGGER.warning(
                "Last imported day was %s — gap exceeds the auto "
                "catch-up limit (%d days). Will refresh the most recent "
                "%d days; use the backfill service to fill the rest.",
                last_day, _MAX_AUTO_CATCHUP_DAYS,
                (yesterday - window_start).days + 1,
            )

        _LOGGER.debug(
            "Catch-up window: %s..%s (last imported day: %s)",
            window_start, yesterday, last_day,
        )
        return await self.async_import_range(window_start, yesterday)

    async def _async_update_data(self) -> dict[str, Any]:
        # The base class wants an _async_update_data; we don't do periodic
        # state updates (no entities), but returning empty keeps the
        # coordinator usable for triggered refreshes.
        return {}
