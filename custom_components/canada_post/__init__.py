"""Canada Post parcel tracker custom component for Home Assistant."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .account.client import CanadaPostAccountClient
from .account.coordinator import CanadaPostAccountCoordinator
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    PLATFORMS,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from .services import async_setup_services, async_unload_services
from .tracking.api import CanadaPostTrackingClient
from .tracking.coordinator import CanadaPostCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass
class CanadaPostData:
    """Runtime data attached to the Canada Post config entry."""

    client: CanadaPostTrackingClient | CanadaPostAccountClient
    coordinator: CanadaPostCoordinator | CanadaPostAccountCoordinator
    applied_options: dict = field(default_factory=dict)


type CanadaPostConfigEntry = ConfigEntry[CanadaPostData]


async def async_setup_entry(hass: HomeAssistant, entry: CanadaPostConfigEntry) -> bool:
    """Set up Canada Post from a config entry."""
    is_account = entry.data[CONF_SOURCE] == SOURCE_ACCOUNT
    tracking_client = CanadaPostTrackingClient(async_get_clientsession(hass))
    if is_account:

        async def async_store_tokens(tokens: dict) -> None:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, **tokens}
            )

        client = CanadaPostAccountClient(
            async_get_clientsession(hass),
            access_token=entry.data.get(CONF_ACCESS_TOKEN),
            refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
            id_token=entry.data.get(CONF_ID_TOKEN),
            expires_at=entry.data.get(CONF_EXPIRES_AT),
            token_callback=async_store_tokens,
        )
        coordinator = CanadaPostAccountCoordinator(
            hass, client, tracking_client, entry
        )
    else:
        client = tracking_client
        coordinator = CanadaPostCoordinator(hass, client, entry)

    # Fetch initial data here, before forwarding to platforms. Raising
    # ConfigEntryNotReady from a forwarded platform is too late for HA to catch
    # cleanly (it logs a warning and half-sets-up the entry); doing the first
    # refresh here lets a transient failure fail the whole entry so HA retries
    # it with backoff.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = CanadaPostData(
        client=client, coordinator=coordinator, applied_options=dict(entry.options)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Apply option changes (added/removed parcels, history) live via a
    # coordinator refresh — no reload — so per-parcel sensors appear and
    # disappear immediately. This is also the resume path after polling fully
    # suspended: adding a parcel back triggers a refresh that re-arms
    # scheduling.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    if not is_account:
        async_setup_services(hass)

    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: CanadaPostConfigEntry
) -> None:
    """Apply changed options by refreshing the coordinator.

    Rotated account tokens are written to the entry too; they change nothing
    to apply, and refreshing on them would poll again mid-poll.
    """
    if dict(entry.options) == entry.runtime_data.applied_options:
        return
    entry.runtime_data.applied_options = dict(entry.options)
    await entry.runtime_data.coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: CanadaPostConfigEntry) -> bool:
    """Unload a Canada Post config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    if entry.data[CONF_SOURCE] == SOURCE_TRACKING:
        # Only one tracking hub can exist, so its unload removes the services.
        async_unload_services(hass)
    return True
