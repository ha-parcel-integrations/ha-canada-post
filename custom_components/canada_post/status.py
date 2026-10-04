"""Canada Post's package status vocabulary, shared by both sources.

Both sources normalise the same detail payload, so the map lives here and
neither can drift from it.
"""
from __future__ import annotations

import logging
from typing import Any

from .const import RETURN_EVENT_ISSUE_URL, ParcelStatus

_LOGGER = logging.getLogger(__name__)

# The ``?template=`` parameter matters: without it the link opens a blank form,
# missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-canada-post/issues/new"
    "?template=unrecognised_status.yml"
)

STATUS_MAP: dict[str, ParcelStatus] = {
    "HalfAccepted": ParcelStatus.REGISTERED,
    "FullAccepted": ParcelStatus.IN_TRANSIT,
    "InTransit": ParcelStatus.IN_TRANSIT,
    "InTransitAlert": ParcelStatus.PROBLEM,
    "FullProgress": ParcelStatus.OUT_FOR_DELIVERY,
    "FullProgressAlert": ParcelStatus.PROBLEM,
    "HalfDelivered": ParcelStatus.PROBLEM,
    "ReadyPickup": ParcelStatus.AT_PICKUP_POINT,
    "Available_for_pickup_by": ParcelStatus.AT_PICKUP_POINT,
    "Delivered": ParcelStatus.DELIVERED,
}

# Each set holds values already warned about, so a value is logged once per
# HA session instead of on every poll.
_prefix_logged: set[str] = set()
_unmapped_logged: set[str] = set()


def map_status(value: Any, *, warn_unmapped: bool = True) -> ParcelStatus:
    """Map Canada Post's ``status`` string: exact, then prefix, then unknown.

    A prefix hit is a mitigation, not evidence that the variant means what its
    family means, so it logs its own one-shot warning. ``warn_unmapped=False``
    leaves the unknown warning to a caller that still has a fallback.
    """
    if not isinstance(value, str) or not value:
        return ParcelStatus.UNKNOWN
    mapped = STATUS_MAP.get(value)
    if mapped is not None:
        return mapped
    family = value.split("-", 1)[0]
    mapped = STATUS_MAP.get(family) if family != value else None
    if mapped is not None:
        if value not in _prefix_logged:
            _prefix_logged.add(value)
            _LOGGER.warning(
                "Canada Post status variant not in the known list — help us "
                "confirm it. Open an issue and paste this line: %s\n"
                "  status=%s → treated as '%s' (family match, not confirmed)",
                NEW_ISSUE_URL,
                value,
                family,
            )
        return mapped
    if warn_unmapped:
        _warn_unmapped_status(value)
    return ParcelStatus.UNKNOWN


def _warn_unmapped_status(value: str) -> None:
    if value in _unmapped_logged:
        return
    _unmapped_logged.add(value)
    _LOGGER.warning(
        "Unrecognised Canada Post status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        value,
    )


def resolve_status(
    detail: dict[str, Any], newest_events: list[dict[str, Any]] | None = None
) -> ParcelStatus:
    """Map a detail payload's status, applying the return-to-sender override.

    The ``status`` string is mapped first; when that fails, the newest event
    that maps decides, and only when neither works is the status reported as
    unknown with a warning. A parcel on its way back is ``returning``
    whichever status string it still carries, unless it is already delivered.
    """
    if detail.get("delivered") is True:
        return ParcelStatus.DELIVERED
    raw = detail.get("status")
    status = map_status(raw, warn_unmapped=False)
    if status is ParcelStatus.UNKNOWN:
        status = next(
            (
                mapped
                for event in newest_events or []
                if (mapped := map_event_status(event)) is not ParcelStatus.UNKNOWN
            ),
            ParcelStatus.UNKNOWN,
        )
        if status is ParcelStatus.UNKNOWN and isinstance(raw, str) and raw:
            _warn_unmapped_status(raw)
    returned = detail.get("returnedToSender") is True or (
        detail.get("returnPinIndicator") is True
    )
    if returned and status is not ParcelStatus.DELIVERED and detail.get("delivered") is not True:
        return ParcelStatus.RETURNING
    return status


# An event code outranks its type: one type covers several outcomes (an
# ``Attempted`` scan can send the parcel to a counter or back to the sender).
EVENT_CODE_MAP: dict[str, ParcelStatus] = {
    "3000": ParcelStatus.REGISTERED,
    "1466": ParcelStatus.DELIVERED,
    "0174": ParcelStatus.OUT_FOR_DELIVERY,
    "0500": ParcelStatus.OUT_FOR_DELIVERY,
    "1701": ParcelStatus.AT_PICKUP_POINT,
    "0156": ParcelStatus.AT_PICKUP_POINT,
    "2407": ParcelStatus.AT_PICKUP_POINT,
    "1419": ParcelStatus.RETURNING,
    "1481": ParcelStatus.RETURNING,
    "2600": ParcelStatus.RETURNING,
    "1303": ParcelStatus.RETURNING,
    "3001": ParcelStatus.RETURNING,
    "3002": ParcelStatus.RETURNING,
    "1100": ParcelStatus.PROBLEM,
}

# Upper-case, as Canada Post's own app groups them; the wire sends PascalCase.
EVENT_TYPE_MAP: dict[str, ParcelStatus] = {
    **dict.fromkeys(
        (
            "ARRIVALINCANADA", "CONTAINER", "INCOMING", "INDUCTION", "INFOTID",
            "REVIEWCOMPLETE", "VEHICLEINFO", "PRRECEIVED", "INFOCONT",
            "DISPATCH", "TOCUST", "FROMCUST", "INFO", "TORETAIL",
        ),
        ParcelStatus.IN_TRANSIT,
    ),
    "OUT": ParcelStatus.OUT_FOR_DELIVERY,
    # The app files ATTEMPTED under in-transit; a failed delivery attempt is a
    # problem for a household dashboard, matching ``HalfDelivered``.
    "ATTEMPTED": ParcelStatus.PROBLEM,
    "DETENTION": ParcelStatus.PROBLEM,
    "FORREVIEW": ParcelStatus.PROBLEM,
    "RTSLABELPROC": ParcelStatus.RETURNING,
    "DELIVERED": ParcelStatus.DELIVERED,
    "SIGNATURE": ParcelStatus.DELIVERED,
}

_unmapped_event_logged: set[str] = set()
# The app files these as a returned item; their wording has never been seen.
_UNCONFIRMED_RETURN_CODES = frozenset({"1303", "3001", "3002"})
_return_code_logged: set[str] = set()


def map_event_status(event: dict[str, Any]) -> ParcelStatus:
    """Map one tracking event to a canonical status: code, then type."""
    code = event.get("cd")
    if isinstance(code, str) and code in EVENT_CODE_MAP:
        if code in _UNCONFIRMED_RETURN_CODES and code not in _return_code_logged:
            _return_code_logged.add(code)
            _LOGGER.warning(
                "Canada Post return event seen — help us confirm what it means. "
                "Paste this line in %s\n  cd=%s type=%s → treated as 'returning'",
                RETURN_EVENT_ISSUE_URL,
                code,
                event.get("type"),
            )
        return EVENT_CODE_MAP[code]
    event_type = event.get("type")
    if isinstance(event_type, str) and event_type.upper() in EVENT_TYPE_MAP:
        return EVENT_TYPE_MAP[event_type.upper()]
    key = f"{event_type}/{code}"
    if key not in _unmapped_event_logged:
        _unmapped_event_logged.add(key)
        _LOGGER.warning(
            "Unrecognised Canada Post event type — help us map it. Open an "
            "issue and paste this line: %s\n  type=%s cd=%s → history entry "
            "reported as 'unknown'",
            NEW_ISSUE_URL,
            event_type,
            code,
        )
    return ParcelStatus.UNKNOWN
