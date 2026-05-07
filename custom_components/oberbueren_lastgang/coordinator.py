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
    async_get_last_imported_hour,
    async_import_many,
    build_statistic_id,
)
from .tariffs import load_tariffs

# Cap auto catch-up so an HA host that's been off for months doesn't
# unexpectedly fire a year of API requests on next boot. Bigger gaps
# require an explicit ``backfill`` service call from the user.
_MAX_AUTO_CATCHUP_DAYS = 30

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

        Days are fetched and imported in chronological order so the
        cumulative sum stays monotonically increasing. Failures on
        individual days are logged and skipped — a single missing day
        shouldn't abort a multi-month backfill.
        """
        if start > end:
            raise ValueError(f"start ({start}) must not be after end ({end})")

        total_imported = 0
        for messlinie in ACTIVE_MESSLINIEN:
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
                    responses.append(response)
                except AuthError:
                    # Re-raise auth errors — they need user action.
                    raise
                except ApiError as err:
                    _LOGGER.warning(
                        "Skipping %s for %s: %s", current, messlinie.label, err
                    )
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
        """Fetch any days missing between the last imported day and yesterday.

        Idempotent: returns immediately if data is already up to date.
        Used by both the daily 06:00 trigger and the startup hook so a
        host that was offline at the scheduled time still picks up the
        missed days as soon as it comes back online.

        First-install (no prior data) is handled conservatively: we
        don't know how far back the user wants history, so we log a
        hint and return without fetching anything. Run the ``backfill``
        service for the initial population.

        Gaps larger than ``_MAX_AUTO_CATCHUP_DAYS`` (default 30) are
        also rejected; backfill is the right tool for those.
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
        if last_day >= yesterday:
            _LOGGER.debug(
                "Catch-up: already up to date (last imported day: %s)",
                last_day,
            )
            return 0

        gap_start = last_day + timedelta(days=1)
        gap_days = (yesterday - gap_start).days + 1
        if gap_days > _MAX_AUTO_CATCHUP_DAYS:
            _LOGGER.warning(
                "Last imported day was %s — gap of %d days exceeds the "
                "auto catch-up limit (%d). Use the backfill service to "
                "fill historical gaps manually.",
                last_day, gap_days, _MAX_AUTO_CATCHUP_DAYS,
            )
            return 0

        _LOGGER.info(
            "Catch-up: importing %s through %s (%d day(s))",
            gap_start, yesterday, gap_days,
        )
        return await self.async_import_range(gap_start, yesterday)

    async def _async_update_data(self) -> dict[str, Any]:
        # The base class wants an _async_update_data; we don't do periodic
        # state updates (no entities), but returning empty keeps the
        # coordinator usable for triggered refreshes.
        return {}
