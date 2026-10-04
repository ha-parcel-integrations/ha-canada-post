"""Services for the Canada Post parcel tracker integration.

`canada_post.track_parcel` / `canada_post.untrack_parcel` let you add or remove a
tracked parcel without opening the integration options — so a Lovelace button
can start tracking a parcel straight from a dashboard. Only the tracking hub
has a parcel list, so an account entry is never a target. A notice card number
is resolved to its PIN before it is stored.
"""
from __future__ import annotations

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .config_flow import (
    async_resolve_tracking_code,
    normalize_tracking_code,
    valid_tracking_code,
)
from .const import (
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    DOMAIN,
    SOURCE_TRACKING,
)
from .tracking.api import CanadaPostApiError, CanadaPostTrackingClient

SERVICE_TRACK_PARCEL = "track_parcel"
SERVICE_UNTRACK_PARCEL = "untrack_parcel"

_TRACK_SCHEMA = vol.Schema({vol.Required(CONF_TRACKING_CODE): cv.string})
_UNTRACK_SCHEMA = vol.Schema({vol.Required(CONF_TRACKING_CODE): cv.string})


def _resolve_entry(hass: HomeAssistant) -> ConfigEntry:
    """Return the Canada Post tracking hub, or raise when it is not set up."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data[CONF_SOURCE] == SOURCE_TRACKING:
            return entry
    raise ServiceValidationError("Canada Post tracking is not set up")


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the Canada Post services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_TRACK_PARCEL):
        return

    async def _track(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        if not valid_tracking_code(tracking_code):
            raise ServiceValidationError(
                f"'{tracking_code}' is not a valid Canada Post tracking code"
            )
        entry = _resolve_entry(hass)
        try:
            pin = await async_resolve_tracking_code(
                CanadaPostTrackingClient(async_get_clientsession(hass)), tracking_code
            )
        except (CanadaPostApiError, aiohttp.ClientError) as err:
            raise ServiceValidationError(
                "Could not reach Canada Post to look up the notice card"
            ) from err
        if pin is None:
            raise ServiceValidationError(
                f"No parcel found for notice card '{tracking_code}'"
            )

        parcels = [dict(p) for p in entry.options.get(CONF_PARCELS, [])]
        if any(p[CONF_TRACKING_CODE] == pin for p in parcels):
            return  # already tracked — no-op
        parcels.append({CONF_TRACKING_CODE: pin})
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_PARCELS: parcels}
        )

    async def _untrack(call: ServiceCall) -> None:
        tracking_code = normalize_tracking_code(call.data[CONF_TRACKING_CODE])
        entry = _resolve_entry(hass)
        current = entry.options.get(CONF_PARCELS, [])
        kept = [p for p in current if p[CONF_TRACKING_CODE] != tracking_code]
        if len(kept) != len(current):
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_PARCELS: kept}
            )

    hass.services.async_register(
        DOMAIN, SERVICE_TRACK_PARCEL, _track, schema=_TRACK_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNTRACK_PARCEL, _untrack, schema=_UNTRACK_SCHEMA
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the Canada Post services."""
    for service in (SERVICE_TRACK_PARCEL, SERVICE_UNTRACK_PARCEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
