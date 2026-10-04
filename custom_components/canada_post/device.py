"""The device every entity of this integration belongs to.

One place, because sensors, the button and the calendar must all land on the
*same* device entry.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_SOURCE, CONF_USERNAME, DOMAIN, SOURCE_ACCOUNT

CONFIGURATION_URL = "https://www.canadapost-postescanada.ca"

ATTRIBUTION = "Data provided by Canada Post"


def build_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the DeviceInfo shared by every entity of this hub."""
    username = entry.data.get(CONF_USERNAME)
    is_account = entry.data.get(CONF_SOURCE) == SOURCE_ACCOUNT
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Canada Post ({username})" if is_account and username else "Canada Post",
        manufacturer="Canada Post",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=CONFIGURATION_URL,
    )
