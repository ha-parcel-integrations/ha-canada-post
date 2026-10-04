"""Diagnostics support for the Canada Post parcel tracker integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import CanadaPostConfigEntry

# Diagnostics are pasted into public issues, so redact anything that
# identifies a person, an address or a specific parcel. Over-redacting is
# cheap; under-redacting leaks a user's home address into a GitHub thread.
#
#
# Not redacted on purpose: ``status``, event ``cd``/``type``/``datetime``,
# ``city``, ``regionCd``, ``expectedDlvryDateTime`` and the ETA window. A user
# report needs them to confirm or correct the status map and the payload shape.
# ``raw_status`` is over-redacted: the canonical top-level field and each
# history entry's own localised event text share that key name, and redaction
# is by key, not by position.
TO_REDACT = {
    # sign-in and tokens
    "username",
    "password",
    "access_token",
    "refresh_token",
    "id_token",
    "owner",
    # identifiers
    "pin",
    "trackId",
    "dnc",
    "cuPin",
    "refNbr1",
    "refNbr2",
    # people and addresses
    "recipientNm",
    "custNm",
    "signatureNm",
    "shipToAddr",
    "shipFromAddr",
    "addrLn1",
    "addrLn2",
    "postCd",
    "shipperPostalCode",
    "correctedPostalCode",
    "photoConfURL",
    "userDescription",
    # the destination town and the pickup office narrow down where the user lives
    "addtnlOrigInfo",
    "addtnlDestInfo",
    "retailNmEn",
    "retailNmFr",
    "retailLocationId",
    # event text can carry addresses
    "descEn",
    "descFr",
    # canonical fields we publish ourselves
    "tracking_code",
    "barcode",
    "sender",
    "receiver",
    "url",
    "raw_status",
    "pickup_point",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CanadaPostConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Canada Post config entry."""
    coordinator = entry.runtime_data.coordinator

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        "counts": {
            "incoming_active": len(coordinator.data or []),
            "delivered": len(coordinator.delivered or []),
            "skipped_from_fetch": len(coordinator.delivered_codes),
        },
        "polling": {
            "tier_minutes": coordinator.current_tier_minutes,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "suspended": coordinator.update_interval is None,
        },
        "incoming": async_redact_data(coordinator.data or [], TO_REDACT),
        "delivered": async_redact_data(coordinator.delivered or [], TO_REDACT),
    }
