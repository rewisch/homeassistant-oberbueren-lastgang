"""Config flow for Strom Oberbüren Lastgang.

Two-step flow:

1. ``user``: collect email + password and verify them by performing an
   actual login against the upstream site. We don't proceed to step 2
   until we know the credentials work — failing late (after the user has
   filled in meter IDs too) would be annoying.
2. ``meter``: collect the meter identification (objekt_id, meteringcode,
   friendly name). We don't validate these against the API in V1 —
   verifying them would require a real fetch and the user already has
   them from the URL bar (per their request to keep V1 simple).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiError, AuthError, OberbuerenClient
from .const import (
    CONF_EMAIL,
    CONF_METERINGCODE,
    CONF_NAME,
    CONF_OBJEKT_ID,
    CONF_PASSWORD,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

_METER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_OBJEKT_ID): str,
        vol.Required(CONF_METERINGCODE): str,
    }
)


class OberbuerenConfigFlow(ConfigFlow, domain=DOMAIN):
    """UI-driven config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            client = OberbuerenClient(
                session=session,
                email=user_input[CONF_EMAIL],
                password=user_input[CONF_PASSWORD],
            )
            try:
                await client.async_login()
            except AuthError:
                errors["base"] = "invalid_auth"
            except ApiError as err:
                _LOGGER.warning("Login check failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                self._email = user_input[CONF_EMAIL]
                self._password = user_input[CONF_PASSWORD]
                return await self.async_step_meter()

        return self.async_show_form(
            step_id="user", data_schema=_USER_SCHEMA, errors=errors
        )

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._email is not None and self._password is not None

        errors: dict[str, str] = {}

        if user_input is not None:
            objekt_id = user_input[CONF_OBJEKT_ID].strip()
            meteringcode = user_input[CONF_METERINGCODE].strip()
            name = user_input[CONF_NAME].strip()

            # Use the (objekt_id, meteringcode) tuple as a unique-ID so the
            # same meter can't be added twice.
            unique_id = f"{objekt_id}:{meteringcode}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=name,
                data={
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_OBJEKT_ID: objekt_id,
                    CONF_METERINGCODE: meteringcode,
                    CONF_NAME: name,
                },
            )

        return self.async_show_form(
            step_id="meter", data_schema=_METER_SCHEMA, errors=errors
        )
