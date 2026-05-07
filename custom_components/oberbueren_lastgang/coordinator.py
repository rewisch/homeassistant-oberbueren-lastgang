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
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import ApiError, AuthError, OberbuerenClient
from .const import ACTIVE_MESSLINIEN, CONF_METERINGCODE, CONF_NAME, CONF_OBJEKT_ID
from .statistics import async_import_many

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
                count = await async_import_many(
                    self.hass,
                    self.objekt_id,
                    messlinie,
                    self.friendly_name,
                    responses,
                )
                total_imported += count

        return total_imported

    async def async_import_yesterday(self) -> int:
        """Fetch and import the data for the previous local day."""
        # We use the local "today" relative to the HA host; the API operates
        # in Europe/Zurich anyway and serves whole days.
        from datetime import datetime
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        _LOGGER.debug("Daily fetch: importing %s", yesterday)
        return await self.async_import_range(yesterday, yesterday)

    async def _async_update_data(self) -> dict[str, Any]:
        # The base class wants an _async_update_data; we don't do periodic
        # state updates (no entities), but returning empty keeps the
        # coordinator usable for triggered refreshes.
        return {}
