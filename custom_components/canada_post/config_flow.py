"""Config flow for the Canada Post parcel tracker integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.client import (
    CanadaPostAccountApiError,
    CanadaPostAccountClient,
    CanadaPostAccountInvalidCredentials,
    CanadaPostAccountReauthRequired,
    CanadaPostAccountTwoStepRequired,
)
from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_PASSWORD,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    CONF_USERNAME,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DEFAULT_INCLUDE_HISTORY,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .tracking.api import CanadaPostApiError, CanadaPostTrackingClient

_LOGGER = logging.getLogger(__name__)

_PIN_RE = re.compile(r"^(\d{11}|\d{12}|\d{16})$")
_S10_RE = re.compile(r"^[A-Z]{2}\d{9}[A-Z]{2}$")
_DNC_RE = re.compile(r"^(\d{9}|\d{15})$")


def normalize_tracking_code(value: str) -> str:
    """Return the tracking code upper-cased with whitespace stripped."""
    return re.sub(r"\s+", "", (value or "").upper())


def is_notice_card(code: str) -> bool:
    """Whether ``code`` is a delivery notice card number, not a PIN."""
    return bool(_DNC_RE.match(code))


def valid_tracking_code(value: str) -> bool:
    """Whether ``value`` is a PIN, an international S10 code or a notice card."""
    return bool(
        _PIN_RE.match(value) or _S10_RE.match(value) or _DNC_RE.match(value)
    )


async def async_resolve_tracking_code(
    client: CanadaPostTrackingClient, code: str
) -> str | None:
    """Return the PIN to store for ``code``; ``None`` when a notice card misses.

    A notice card number is resolved once here so the PIN is what gets stored
    and polled from then on.
    """
    if not is_notice_card(code):
        return code
    return await client.async_resolve_dnc(code)


def _current_parcels(entry: ConfigEntry) -> list[dict[str, str]]:
    """Return a mutable copy of the tracked parcels list."""
    return [dict(item) for item in entry.options.get(CONF_PARCELS, [])]


def _clean_tracking_codes(values: list[str] | None) -> list[str]:
    """Normalise, drop blanks, and de-duplicate tracking codes."""
    codes: list[str] = []
    for value in values or []:
        code = normalize_tracking_code(value)
        if code and code not in codes:
            codes.append(code)
    return codes


class CanadaPostConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI-driven configuration flow for the Canada Post integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> CanadaPostOptionsFlowHandler:
        """Return the options flow handler."""
        return CanadaPostOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the parcel source: the account list or typed-in tracking codes."""
        return self.async_show_menu(
            step_id="user", menu_options=[SOURCE_ACCOUNT, SOURCE_TRACKING]
        )

    async def async_step_tracking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the tracking hub — nothing to ask, and only one can exist."""
        await self.async_set_unique_id(SOURCE_TRACKING)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Canada Post",
            data={CONF_SOURCE: SOURCE_TRACKING},
            options={
                CONF_PARCELS: [],
                CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
            },
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in and list once; persist only the tokens, never the password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            username = str(user_input[CONF_USERNAME]).strip().lower()
            password = str(user_input[CONF_PASSWORD])
            if not username or not password:
                errors["base"] = "invalid_auth"
            else:
                client = CanadaPostAccountClient(async_get_clientsession(self.hass))
                try:
                    tokens = await client.async_login(username, password)
                    await client.async_list_items()
                except CanadaPostAccountTwoStepRequired:
                    return self.async_abort(reason="two_step_not_supported")
                except (
                    CanadaPostAccountInvalidCredentials,
                    CanadaPostAccountReauthRequired,
                ):
                    errors["base"] = "invalid_auth"
                except CanadaPostAccountApiError:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(f"account:{username}")
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"Canada Post ({username})",
                        data={
                            CONF_SOURCE: SOURCE_ACCOUNT,
                            CONF_USERNAME: username,
                            **tokens,
                        },
                        options={
                            CONF_DELIVERED_FILTER_TYPE: DEFAULT_DELIVERED_FILTER_TYPE,
                            CONF_DELIVERED_FILTER_AMOUNT: DEFAULT_DELIVERED_FILTER_AMOUNT,
                            CONF_INCLUDE_HISTORY: DEFAULT_INCLUDE_HISTORY,
                        },
                    )
        return self.async_show_form(
            step_id=SOURCE_ACCOUNT,
            data_schema=vol.Schema(
                {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Reauthenticate the fixed account; never silently change accounts."""
        self._reauth_entry = self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace only the tokens after a successful password sign-in."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        if user_input is not None:
            client = CanadaPostAccountClient(async_get_clientsession(self.hass))
            try:
                tokens = await client.async_login(
                    entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except CanadaPostAccountTwoStepRequired:
                return self.async_abort(reason="two_step_not_supported")
            except CanadaPostAccountInvalidCredentials:
                errors["base"] = "invalid_auth"
            except CanadaPostAccountApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(entry.unique_id)
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, data_updates=tokens)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )


class CanadaPostOptionsFlowHandler(OptionsFlow):
    """Manage tracked parcels separately from integration settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer parcel management separately from integration settings."""
        menu_options = ["settings"]
        if self.config_entry.data[CONF_SOURCE] == SOURCE_TRACKING:
            menu_options.insert(0, "parcels")
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_parcels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the complete tracked-code list."""
        errors: dict[str, str] = {}
        if user_input is not None:
            codes = _clean_tracking_codes(user_input.get("tracking_codes"))
            if any(not valid_tracking_code(code) for code in codes):
                errors["base"] = "invalid_tracking_code"
            else:
                client = CanadaPostTrackingClient(async_get_clientsession(self.hass))
                pins: list[str] = []
                try:
                    for code in codes:
                        pin = await async_resolve_tracking_code(client, code)
                        if pin is None:
                            errors["base"] = "not_found"
                            break
                        if pin not in pins:
                            pins.append(pin)
                except (CanadaPostApiError, aiohttp.ClientError):
                    errors["base"] = "cannot_connect"
                if not errors:
                    return self.async_create_entry(
                        title="",
                        data={
                            **self.config_entry.options,
                            CONF_PARCELS: [{CONF_TRACKING_CODE: pin} for pin in pins],
                        },
                    )
        current_codes = [
            p[CONF_TRACKING_CODE] for p in _current_parcels(self.config_entry)
        ]
        schema = vol.Schema(
            {
                vol.Optional("tracking_codes"): selector.TextSelector(
                    selector.TextSelectorConfig(multiple=True)
                )
            }
        )
        return self.async_show_form(
            step_id="parcels",
            data_schema=self.add_suggested_values_to_schema(
                schema, {"tracking_codes": current_codes}
            ),
            errors=errors,
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and handle the non-parcel integration settings."""
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    **self.config_entry.options,
                    CONF_DELIVERED_FILTER_TYPE: user_input[CONF_DELIVERED_FILTER_TYPE],
                    CONF_DELIVERED_FILTER_AMOUNT: int(
                        user_input[CONF_DELIVERED_FILTER_AMOUNT]
                    ),
                    CONF_INCLUDE_HISTORY: bool(user_input[CONF_INCLUDE_HISTORY]),
                },
            )
        current = self.config_entry.options
        schema: dict[Any, Any] = {
            vol.Required(
                CONF_DELIVERED_FILTER_TYPE,
                default=current.get(
                    CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
                ),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["days", "parcels"],
                    translation_key=CONF_DELIVERED_FILTER_TYPE,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(
                CONF_DELIVERED_FILTER_AMOUNT,
                default=current.get(
                    CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=365, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_INCLUDE_HISTORY,
                default=current.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY),
            ): selector.BooleanSelector(),
        }
        return self.async_show_form(step_id="settings", data_schema=vol.Schema(schema))
