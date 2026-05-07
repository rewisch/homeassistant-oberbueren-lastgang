"""Strom Oberbüren Lastgang integration entry point.

For each config entry we:

  * Build an OberbuerenClient bound to that entry's credentials.
  * Wire up a ``LastgangCoordinator`` that knows how to fetch + import days.
  * Schedule a daily local-time trigger to import yesterday's data.

A single domain-wide ``backfill`` service is registered (only on the first
entry setup) that targets a config entry by its ID and imports a date range.
This is the user's escape hatch for the initial multi-month import.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_change

from .api import ApiError, AuthError, OberbuerenClient
from .const import (
    ACTIVE_MESSLINIEN,
    ATTR_END_DATE,
    ATTR_START_DATE,
    CONF_EMAIL,
    CONF_PASSWORD,
    DEFAULT_POLL_HOUR,
    DOMAIN,
    SERVICE_BACKFILL,
)
from .coordinator import LastgangCoordinator
from .statistics import async_recompute_costs
from .tariffs import install_default_tariffs_if_missing, load_tariffs

SERVICE_RECOMPUTE_COSTS = "recompute_costs"

PLATFORMS: list[str] = ["sensor"]

_LOGGER = logging.getLogger(__name__)


_BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
        vol.Required(ATTR_START_DATE): cv.date,
        vol.Optional(ATTR_END_DATE): cv.date,
    }
)

_RECOMPUTE_COSTS_SCHEMA = vol.Schema(
    {
        vol.Required("entry_id"): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = OberbuerenClient(
        hass=hass,
        email=entry.data[CONF_EMAIL],
        password=entry.data[CONF_PASSWORD],
    )

    # Verify credentials still work at startup. Wrong password → reauth flow;
    # transient network → retry on next HA boot via ConfigEntryNotReady.
    try:
        await client.async_login()
    except AuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except ApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = LastgangCoordinator(hass, entry, client)

    # Schedule the daily import. async_track_time_change uses HA's local
    # timezone, which on a Swiss installation will be Europe/Zurich —
    # matching the upstream API's day boundaries. Returns an unsub callable
    # which we register with the entry so it's cleaned up on unload.
    async def _daily_trigger(_now: datetime) -> None:
        try:
            await coordinator.async_import_yesterday()
        except AuthError as err:
            _LOGGER.error("Auth failed during daily import: %s", err)
        except ApiError as err:
            _LOGGER.warning("Daily import failed (will retry tomorrow): %s", err)

    unsub_daily = async_track_time_change(
        hass, _daily_trigger, hour=DEFAULT_POLL_HOUR, minute=0, second=0
    )
    entry.async_on_unload(unsub_daily)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Seed the user's tariffs file from the integration's bundled default
    # on first setup. Idempotent: never overwrites once the user has the
    # file, so HACS upgrades won't blow away their edits or annual
    # tariff additions.
    await hass.async_add_executor_job(
        install_default_tariffs_if_missing, hass.config.config_dir
    )

    # Bring up the sensor platform (8 aggregate entities per meter).
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register the backfill service exactly once, on the first entry setup.
    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL):
        async def _async_handle_backfill(call: ServiceCall) -> None:
            entry_id: str = call.data["entry_id"]
            start: date = call.data[ATTR_START_DATE]
            end: date = call.data.get(ATTR_END_DATE, start)

            target = hass.data.get(DOMAIN, {}).get(entry_id)
            if target is None:
                raise ValueError(f"Unknown config entry: {entry_id}")

            count = await target.async_import_range(start, end)
            _LOGGER.info(
                "Backfill complete: %d hourly points written for %s..%s",
                count,
                start,
                end,
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_BACKFILL,
            _async_handle_backfill,
            schema=_BACKFILL_SCHEMA,
        )

    # Register the cost-recompute service. Same one-time-per-domain
    # pattern as backfill.
    if not hass.services.has_service(DOMAIN, SERVICE_RECOMPUTE_COSTS):
        async def _async_handle_recompute_costs(call: ServiceCall) -> None:
            entry_id: str = call.data["entry_id"]
            target: LastgangCoordinator | None = hass.data.get(DOMAIN, {}).get(
                entry_id
            )
            if target is None:
                raise ValueError(f"Unknown config entry: {entry_id}")

            tariffs = await hass.async_add_executor_job(
                load_tariffs, hass.config.config_dir
            )
            if tariffs.is_empty:
                raise ValueError(
                    "No tariff data loaded — check "
                    "<HA-config>/oberbueren_lastgang_tariffs.yaml"
                )

            _LOGGER.info(
                "Recompute starting for %s (objekt_id=%r): %d tariff "
                "period(s) loaded",
                target.friendly_name, target.objekt_id, len(tariffs),
            )

            total = 0
            for messlinie in ACTIVE_MESSLINIEN:
                total += await async_recompute_costs(
                    hass,
                    target.objekt_id,
                    messlinie,
                    target.friendly_name,
                    tariffs,
                )
            _LOGGER.info(
                "Recompute complete: %d hourly cost points written across "
                "all categories for %s",
                total, target.friendly_name,
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_RECOMPUTE_COSTS,
            _async_handle_recompute_costs,
            schema=_RECOMPUTE_COSTS_SCHEMA,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    domain_data = hass.data.get(DOMAIN, {})
    domain_data.pop(entry.entry_id, None)

    # Remove the service when no entries remain so it doesn't linger after
    # the integration is fully removed.
    if not domain_data:
        for svc in (SERVICE_BACKFILL, SERVICE_RECOMPUTE_COSTS):
            if hass.services.has_service(DOMAIN, svc):
                hass.services.async_remove(DOMAIN, svc)

    return True
