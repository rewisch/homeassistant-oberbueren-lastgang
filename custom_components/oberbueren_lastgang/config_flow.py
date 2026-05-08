"""Config flow for Strom Oberbüren Lastgang.

Initial setup is a two-step flow:

1. ``user``: collect base URL, email + password and verify them by
   performing an actual login. We don't proceed to step 2 until we know
   the credentials work — failing late (after the user has filled in
   meter IDs too) would be annoying.
2. ``meter``: collect the meter identification (objekt_id, meteringcode,
   friendly name). Not validated against the API: the user already has
   these values from the URL bar.

After setup, two flows let the user edit settings without removing the
integration:

* The **reconfigure** flow exposes everything that was entered during
  setup — URL, credentials, meter IDs, display name — in a single form.
  Used when something about the connection identity changes.
* The **options** flow (``OberbuerenOptionsFlow``) covers behavioural
  knobs that aren't part of the connection identity, currently just the
  daily poll hours.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import ApiError, AuthError, OberbuerenClient
from .const import (
    BASE_URL,
    CONF_BASE_URL,
    CONF_EMAIL,
    CONF_METERINGCODE,
    CONF_NAME,
    CONF_OBJEKT_ID,
    CONF_PASSWORD,
    CONF_POLL_HOURS,
    DEFAULT_POLL_HOURS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL, default=BASE_URL): str,
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


async def _async_validate_credentials(
    hass, base_url: str, email: str, password: str
) -> str | None:
    """Try to log in. Return an error key for the form, or None on success."""
    client = OberbuerenClient(
        hass=hass, email=email, password=password, base_url=base_url
    )
    try:
        await client.async_login()
    except AuthError:
        return "invalid_auth"
    except ApiError as err:
        _LOGGER.warning("Login check failed: %s", err)
        return "cannot_connect"
    return None


class OberbuerenConfigFlow(ConfigFlow, domain=DOMAIN):
    """UI-driven config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._base_url: str | None = None
        self._email: str | None = None
        self._password: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OberbuerenOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].strip().rstrip("/")
            err_key = await _async_validate_credentials(
                self.hass, base_url, user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
            )
            if err_key:
                errors["base"] = err_key
            else:
                self._base_url = base_url
                self._email = user_input[CONF_EMAIL]
                self._password = user_input[CONF_PASSWORD]
                return await self.async_step_meter()

        return self.async_show_form(
            step_id="user", data_schema=_USER_SCHEMA, errors=errors
        )

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert (
            self._base_url is not None
            and self._email is not None
            and self._password is not None
        )

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
                    CONF_BASE_URL: self._base_url,
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit URL/credentials/meter IDs/name on an existing entry.

        Single-step form (unlike initial setup's two-step flow): the user
        already has a working entry, so the credential-validation gate of
        the initial flow doesn't earn its keep here — it would just mean
        an extra screen for the common case of changing only the name.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = user_input[CONF_BASE_URL].strip().rstrip("/")
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            objekt_id = user_input[CONF_OBJEKT_ID].strip()
            meteringcode = user_input[CONF_METERINGCODE].strip()
            name = user_input[CONF_NAME].strip()

            err_key = await _async_validate_credentials(
                self.hass, base_url, email, password
            )
            if err_key:
                errors["base"] = err_key
            else:
                new_unique_id = f"{objekt_id}:{meteringcode}"
                if new_unique_id != entry.unique_id:
                    # Meter swap: make sure we're not stepping on another entry.
                    for existing in self._async_current_entries(include_ignore=False):
                        if (
                            existing.entry_id != entry.entry_id
                            and existing.unique_id == new_unique_id
                        ):
                            return self.async_abort(reason="already_configured")

                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=new_unique_id,
                    title=name,
                    data_updates={
                        CONF_BASE_URL: base_url,
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                        CONF_OBJEKT_ID: objekt_id,
                        CONF_METERINGCODE: meteringcode,
                        CONF_NAME: name,
                    },
                )

        # Pre-fill from current values; if validation just failed, prefer the
        # values the user just typed so they don't have to re-enter them.
        defaults = {**entry.data, **(user_input or {})}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_BASE_URL,
                    default=defaults.get(CONF_BASE_URL, BASE_URL),
                ): str,
                vol.Required(CONF_EMAIL, default=defaults[CONF_EMAIL]): str,
                vol.Required(CONF_PASSWORD, default=defaults[CONF_PASSWORD]): str,
                vol.Required(CONF_NAME, default=defaults[CONF_NAME]): str,
                vol.Required(
                    CONF_OBJEKT_ID, default=str(defaults[CONF_OBJEKT_ID])
                ): str,
                vol.Required(
                    CONF_METERINGCODE, default=defaults[CONF_METERINGCODE]
                ): str,
            }
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )


class OberbuerenOptionsFlow(OptionsFlow):
    """Lets the user adjust runtime settings (poll hours) without re-adding the integration.

    HA injects ``self.config_entry`` automatically before the first step is
    called, so we don't need to accept it in __init__.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # SelectSelector returns a list of strings; normalise to a sorted
            # de-duplicated list of ints so downstream code never has to.
            hours = sorted({int(h) for h in user_input[CONF_POLL_HOURS]})
            return self.async_create_entry(
                title="", data={CONF_POLL_HOURS: hours}
            )

        current = self.config_entry.options.get(
            CONF_POLL_HOURS, list(DEFAULT_POLL_HOURS)
        )
        # Hour 0 is excluded: at 00:00 local "yesterday" just rolled over and
        # upstream definitely doesn't have its data ready yet.
        hour_options = [
            {"label": f"{h:02d}:00", "value": str(h)} for h in range(1, 24)
        ]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_HOURS,
                    default=[str(h) for h in current],
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=hour_options,
                        multiple=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
