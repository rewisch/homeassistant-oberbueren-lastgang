"""HTTP client for strom.oberbueren.ch.

Login flow (Symfony-style form auth):
    1. GET /login   → receive initial PHPSESSID cookie + parse _csrf_token
    2. POST /login  → with _username, _password, _csrf_token
                      success = 302 redirect to /
                      failure = 302 redirect back to /login
    3. Subsequent requests carry the (regenerated) PHPSESSID cookie.

Session expiry is detected by the data endpoint either returning HTML
(redirect to login) instead of JSON, or returning 401/403. On expiry the
client re-authenticates transparently and retries the request once.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from yarl import URL

from .const import BASE_URL

_LOGGER = logging.getLogger(__name__)

# Symfony emits the CSRF token as a hidden <input>. The attribute order in
# rendered HTML is not guaranteed (templates, minifiers, server versions can
# all reshuffle it), so we first locate the whole <input> element by its
# name attribute, then pick out the value attribute from inside it. This
# also tolerates single-quoted attributes.
_CSRF_INPUT_RE = re.compile(
    r"""<input\b[^>]*\bname=["']_csrf_token["'][^>]*>""",
    re.IGNORECASE,
)
_VALUE_RE = re.compile(r"""value=["']([^"']+)["']""", re.IGNORECASE)


def _extract_csrf_token(html: str) -> str | None:
    """Pull the _csrf_token value out of the rendered login HTML."""
    input_match = _CSRF_INPUT_RE.search(html)
    if not input_match:
        return None
    value_match = _VALUE_RE.search(input_match.group(0))
    return value_match.group(1) if value_match else None


class OberbuerenError(Exception):
    """Base error for the Oberbüren API client."""


class AuthError(OberbuerenError):
    """Authentication failed or session is invalid."""


class ApiError(OberbuerenError):
    """Generic transport / parsing error talking to the API."""


@dataclass(slots=True)
class MessdatenResponse:
    """Parsed subset of /lastgangdaten/getMessdaten we care about.

    The wire format is a struct-of-arrays: ``intervals`` and ``values`` are
    parallel sequences of equal length. We deliberately ignore the chart
    metadata (labels, parameters, …) — those are presentation concerns.
    """

    objekt_description: str
    unit: str
    intervals: list[dict[str, str]]
    values: list[str]


class OberbuerenClient:
    """Async client for the Oberbüren Lastgangdaten endpoint.

    Each instance owns its own aiohttp session with a *private* cookie
    jar. We deliberately don't reuse HA's shared client session
    (``async_get_clientsession``) because its cookie jar is global to
    the whole HA instance — a stale PHPSESSID left over from a previous
    config-flow validation made GET /login serve the *profile* page
    (which contains no login form, hence no CSRF token), causing setup
    to retry-fail forever. With a private jar each client starts clean.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        email: str,
        password: str,
        base_url: str = BASE_URL,
        debug_logging: bool = False,
    ) -> None:
        # async_create_clientsession spins up a session backed by HA's
        # connector pool but with its own CookieJar. ``auto_cleanup=True``
        # (the default) registers it for shutdown closure so we don't
        # need explicit lifecycle management here.
        self._session = async_create_clientsession(hass)
        self._email = email
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._debug_logging = debug_logging
        self._logged_in = False

    async def async_login(self) -> None:
        """Perform login. Raises AuthError on bad credentials."""
        # 1. GET the login form to obtain PHPSESSID + CSRF token.
        try:
            async with self._session.get(f"{self._base_url}/login") as resp:
                if self._debug_logging:
                    _LOGGER.info(
                        "Diagnostic login GET: status=%s content_type=%r url=%s",
                        resp.status,
                        resp.headers.get("Content-Type", ""),
                        resp.url,
                    )
                resp.raise_for_status()
                html = await resp.text()
        except aiohttp.ClientError as err:
            raise ApiError(f"Could not reach login page: {err}") from err

        csrf_token = _extract_csrf_token(html)
        if self._debug_logging:
            _LOGGER.info(
                "Diagnostic login form: csrf_token_found=%s html_chars=%d",
                csrf_token is not None,
                len(html),
            )
        if csrf_token is None:
            # Log a focused snippet so we can diagnose without the full
            # 17 KB page in the error log. We try to land near a likely
            # location of the field; otherwise just show the start.
            idx = html.lower().find("_csrf_token")
            if idx == -1:
                snippet = html[:600]
                marker = "no _csrf_token substring; showing first 600 chars"
            else:
                snippet = html[max(0, idx - 200) : idx + 400]
                marker = f"context around _csrf_token (at offset {idx})"
            _LOGGER.error(
                "CSRF token regex did not match. %s:\n%s", marker, snippet
            )
            raise ApiError("CSRF token not found in login form HTML")

        # 2. POST credentials. Symfony returns 302; we don't want aiohttp to
        # follow the redirect because we need to inspect the Location header
        # to distinguish success (→ "/") from failure (→ "/login").
        form = {
            "_username": self._email,
            "_password": self._password,
            "_csrf_token": csrf_token,
        }
        try:
            async with self._session.post(
                f"{self._base_url}/login",
                data=form,
                allow_redirects=False,
            ) as resp:
                if self._debug_logging:
                    _LOGGER.info(
                        "Diagnostic login POST: status=%s location=%r",
                        resp.status,
                        resp.headers.get("Location", ""),
                    )
                if resp.status != 302:
                    raise AuthError(
                        f"Unexpected login response status {resp.status}"
                    )
                location = resp.headers.get("Location", "")
        except aiohttp.ClientError as err:
            raise ApiError(f"Login POST failed: {err}") from err

        # Symfony redirects back to /login with a flash message on bad creds.
        if location.rstrip("/").endswith("/login"):
            raise AuthError("Invalid email or password")

        self._logged_in = True
        _LOGGER.debug("Logged in to %s as %s", self._base_url, self._email)

    async def async_fetch_messdaten(
        self,
        objekt_id: int | str,
        meteringcode: str,
        messlinie_obis: str,
        datum: date,
    ) -> MessdatenResponse:
        """Fetch one day of 15-minute load data for one Messlinie."""
        if not self._logged_in:
            await self.async_login()

        try:
            return await self._do_fetch(objekt_id, meteringcode, messlinie_obis, datum)
        except AuthError:
            # Session likely expired mid-session; re-login once and retry.
            _LOGGER.debug("Session expired, re-authenticating")
            self._logged_in = False
            await self.async_login()
            return await self._do_fetch(objekt_id, meteringcode, messlinie_obis, datum)

    async def _do_fetch(
        self,
        objekt_id: int | str,
        meteringcode: str,
        messlinie_obis: str,
        datum: date,
    ) -> MessdatenResponse:
        # The site expects messlinienIds as a JSON-encoded mapping.
        # Use compact separators to mirror the browser's request shape.
        messlinien_ids = json.dumps(
            {meteringcode: [messlinie_obis]}, separators=(",", ":")
        )
        params = {
            "objektId": str(objekt_id),
            "meteringcode": meteringcode,
            "messlinieId": messlinie_obis,
            "messlinienIds": messlinien_ids,
            "messlinievorjahr": "false",
            "datum": datum.isoformat(),
            "zeitraum": "day",
            "type": "detailpage",
            "vorjahr": "false",
        }
        # Mirror the browser's AJAX request context. The endpoint is a
        # frontend controller and can behave differently when it is not called
        # as an XMLHttpRequest from the detail page.
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": (
                f"{self._base_url}/lastgangdaten/detail"
                f"?objektId={objekt_id}&meteringcode={meteringcode}"
            ),
            "X-Requested-With": "XMLHttpRequest",
        }

        url = f"{self._base_url}/lastgangdaten/getMessdaten"
        if self._debug_logging:
            _LOGGER.info(
                "Diagnostic fetch Messdaten: url=%s params=%r headers=%r",
                url, params, headers,
            )
        try:
            async with self._session.get(
                url, params=params, headers=headers, allow_redirects=False
            ) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if self._debug_logging:
                    _LOGGER.info(
                        "Diagnostic Messdaten response: status=%s "
                        "content_type=%r url=%s",
                        resp.status, content_type, resp.url,
                    )
                # If the session is gone, the app redirects HTML→/login.
                if resp.status in (301, 302, 303):
                    raise AuthError(
                        f"Redirected to {resp.headers.get('Location')!r} — session expired"
                    )
                if resp.status in (401, 403):
                    raise AuthError(f"HTTP {resp.status} on data endpoint")
                if resp.status >= 400:
                    if self._debug_logging:
                        body = await resp.text()
                        _LOGGER.info(
                            "Diagnostic Messdaten error body (%d chars): %s",
                            len(body), body[:1200],
                        )
                    resp.raise_for_status()

                # Some installations may serve text/html; check explicitly.
                if "json" not in content_type:
                    raise AuthError(
                        f"Expected JSON, got {content_type!r} — likely auth redirect"
                    )

                data = await resp.json()
        except aiohttp.ClientError as err:
            raise ApiError(f"Fetch failed: {err}") from err

        response = _parse_response(data)
        if self._debug_logging:
            _LOGGER.info(
                "Diagnostic parsed Messdaten: object=%r unit=%r "
                "intervals=%d values=%d",
                response.objekt_description,
                response.unit,
                len(response.intervals),
                len(response.values),
            )
        return response

    @property
    def logged_in(self) -> bool:
        return self._logged_in


def _parse_response(payload: dict[str, Any]) -> MessdatenResponse:
    """Validate and unpack the response into our internal shape."""
    try:
        chart = payload["chartData"]
        intervals = chart["intervals"]
        datasets = chart["datasets"]
        if not datasets:
            raise ApiError("Response contains no datasets")
        # We requested a single Messlinie, so a single dataset is expected.
        values = datasets[0]["data"]
        unit = chart.get("unit", "")
        objekt_description = chart.get("objectDescription", "")
    except (KeyError, IndexError, TypeError) as err:
        raise ApiError(f"Unexpected response shape: {err}") from err

    if len(intervals) != len(values):
        raise ApiError(
            f"intervals/values length mismatch: {len(intervals)} vs {len(values)}"
        )

    return MessdatenResponse(
        objekt_description=objekt_description,
        unit=unit,
        intervals=intervals,
        values=values,
    )


def build_test_url(base_url: str = BASE_URL) -> URL:
    """Return the login URL — exposed for unit tests."""
    return URL(f"{base_url.rstrip('/')}/login")
