"""Canonical parcel shape and list helpers, shared by both sources.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. The account source gets only a list
of PINs from Canada Post, so it normalises through here too.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    DELIVERY_WINDOW_ISSUE_URL,
    HISTORY_MAX_EVENTS,
    PICKUP_STATUS_ISSUE_URL,
    TRACKING_URL,
    ParcelStatus,
)
from ..status import NEW_ISSUE_URL, map_event_status, resolve_status

_LOGGER = logging.getLogger(__name__)

DELIVERED_EVENT_CODE = "1466"

_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    """Log a first-sighting WARNING once per key and session."""
    if key in _warned:
        return
    _warned.add(key)
    _LOGGER.warning(message, *args)


def _warn_eta_window_first_sighting(window: Any) -> None:
    """Log the raw ETA window object once; its shape is unconfirmed."""
    _warn_once(
        "eta_window",
        "Canada Post delivery window seen — help us confirm its shape. Paste "
        "this line in %s\n  window=%s",
        DELIVERY_WINDOW_ISSUE_URL,
        window,
    )


def _warn_pickup_status_first_sighting(raw_status: Any, has_office: bool) -> None:
    """Log once when a parcel is waiting at a pickup point right now."""
    _warn_once(
        "pickup_status",
        "Canada Post parcel waiting at a pickup point — help us confirm it. "
        "Paste this line in %s\n  status=%s pickup_point_found=%s",
        PICKUP_STATUS_ISSUE_URL,
        raw_status,
        has_office,
    )


def _warn_event_time_first_sighting(time_text: Any, offset_text: Any) -> None:
    """Log an event time or zone offset we could not parse, once."""
    _warn_once(
        "event_time",
        "Canada Post event time not understood — help us confirm the format. "
        "Open an issue and paste this line: %s\n  time=%r zoneOffset=%r",
        NEW_ISSUE_URL,
        time_text,
        offset_text,
    )


def _warn_pin_mismatch() -> None:
    """Log once when the body names a different PIN than the one stored."""
    _warn_once(
        "pin_mismatch",
        "Canada Post answered with a different PIN than the one requested. "
        "Open an issue and paste this line: %s",
        NEW_ISSUE_URL,
    )


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def resolve_lang(language: str | None) -> str:
    """Return ``fr`` for a French Home Assistant language, else ``en``."""
    return "fr" if (language or "").lower().startswith("fr") else "en"


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10] if "-" in value else value.strip())
    except ValueError:
        return None


_TIME_RE = re.compile(r"^(\d{2}):?(\d{2})(?::?(\d{2}))?")


def _parse_time(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    match = _TIME_RE.match(value.strip())
    if not match:
        return None
    hour, minute, second = (int(part or 0) for part in match.groups())
    try:
        return time(hour, minute, second)
    except ValueError:
        return None


_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})?$")


def _parse_offset(value: Any) -> timezone | None:
    if not isinstance(value, str):
        return None
    if value.strip() == "Z":
        return timezone.utc
    match = _OFFSET_RE.match(value.strip())
    if not match:
        return None
    delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3) or 0))
    return timezone(-delta if match.group(1) == "-" else delta)


def _local_datetime(day: date, moment: time | None = None) -> datetime:
    """Return ``day`` at ``moment`` (default midnight) in HA's local zone."""
    return datetime.combine(day, moment or time(0, 0)).replace(
        tzinfo=dt_util.get_default_time_zone()
    )


def _event_datetime(event: dict[str, Any]) -> datetime | None:
    """Return an event's timestamp as an aware datetime, or ``None``."""
    stamp = event.get("datetime")
    if not isinstance(stamp, dict):
        return None
    day = _parse_date(stamp.get("date"))
    if day is None:
        return None
    raw_time = stamp.get("time")
    raw_offset = stamp.get("zoneOffset")
    moment = _parse_time(raw_time)
    zone = _parse_offset(raw_offset)
    if (raw_time and moment is None) or (raw_offset and zone is None):
        _warn_event_time_first_sighting(raw_time, raw_offset)
    naive = datetime.combine(day, moment or time(0, 0))
    if zone is None:
        return naive.replace(tzinfo=dt_util.get_default_time_zone())
    return naive.replace(tzinfo=zone)


def _dated_events(detail: dict[str, Any]) -> list[tuple[datetime | None, dict]]:
    events = detail.get("events")
    return [
        (_event_datetime(event), event)
        for event in (events if isinstance(events, list) else [])
        if isinstance(event, dict)
    ]


def _newest_first(
    pairs: list[tuple[datetime | None, dict]],
) -> list[tuple[datetime | None, dict]]:
    """Order by parsed time, newest first; undated events go last."""
    dated = sorted(
        (pair for pair in pairs if pair[0] is not None),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return dated + [pair for pair in pairs if pair[0] is None]


def build_history(
    pairs: list[tuple[datetime | None, dict]],
    lang: str,
    *,
    max_events: int = HISTORY_MAX_EVENTS,
) -> list[dict]:
    """Build the canonical ``history`` list, oldest → newest, capped.

    ``status`` is derived from the event's code and type; ``raw_status`` holds
    the localised text.
    """
    entries = [
        {
            "timestamp": moment.isoformat(),
            "status": map_event_status(event),
            "raw_status": event.get(f"desc{lang.capitalize()}")
            or event.get("descEn")
            or event.get("cd"),
        }
        for moment, event in reversed(_newest_first(pairs))
        if moment is not None
    ]
    return entries[-max_events:]


def _eta(detail: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` from the expected-delivery data."""
    expected = detail.get("expectedDlvryDateTime")
    if not isinstance(expected, dict):
        return None, None
    day = _parse_date(expected.get("revisedDate")) or _parse_date(
        expected.get("dlvryDate")
    )
    if day is None:
        return None, None
    window = detail.get("expectedDlvryWindow")
    if not window or not isinstance(window, dict):
        return _local_datetime(day).isoformat(), None
    _warn_eta_window_first_sighting(window)
    start = _parse_time(window.get("dlvryWindowStartTime"))
    end = _parse_time(window.get("dlvryWindowEndTime"))
    if end is None and window.get("dlvryWindowEOD") is True:
        end = time(23, 59)
    return (
        _local_datetime(day, start).isoformat(),
        _local_datetime(day, end).isoformat() if end else None,
    )


def _delivered_at(
    detail: dict[str, Any], newest_first: list[tuple[datetime | None, dict]]
) -> str | None:
    for moment, event in newest_first:
        if moment is not None and str(event.get("cd")) == DELIVERED_EVENT_CODE:
            return moment.isoformat()
    day = _parse_date(detail.get("actualDlvryDate"))
    return _local_datetime(day).isoformat() if day else None


def _pickup_point(newest_first: list[tuple[datetime | None, dict]]) -> str | None:
    for _, event in newest_first:
        name = event.get("retailNmEn")
        if not name:
            continue
        address = event.get("locationAddr")
        city = address.get("city") if isinstance(address, dict) else None
        return f"{name}, {city}" if city else str(name)
    return None


def display_name(parcel: dict | None) -> str | None:
    """Return the account owner's own label for a parcel, if it has one."""
    raw = (parcel or {}).get("raw")
    account = raw.get("account") if isinstance(raw, dict) else None
    label = account.get("userDescription") if isinstance(account, dict) else None
    return label.strip() or None if isinstance(label, str) else None


def tracking_url(pin: str | None, lang: str = "en") -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not pin:
        return None
    return TRACKING_URL.format(pin=pin, lang=lang)


def normalize_parcel(
    detail: dict,
    *,
    pin: str,
    lang: str = "en",
    include_history: bool = False,
) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    ``pin`` is the stored PIN (typed by the user, resolved from a notice card
    or taken from the account list). The keys of the returned dict are the
    suite contract; a key the carrier does not expose is ``None``, never
    omitted.
    """
    if detail.get("pin") and detail["pin"] != pin:
        _warn_pin_mismatch()
    pairs = _dated_events(detail)
    newest_first = _newest_first(pairs)
    status = resolve_status(detail, [event for _, event in newest_first])
    delivered = detail.get("delivered") is True or status is ParcelStatus.DELIVERED
    pickup_point = (
        _pickup_point(newest_first) if status is ParcelStatus.AT_PICKUP_POINT else None
    )
    if status is ParcelStatus.AT_PICKUP_POINT:
        _warn_pickup_status_first_sighting(detail.get("status"), pickup_point is not None)
    planned_from, planned_to = (None, None) if delivered else _eta(detail)

    return {
        "carrier": "Canada Post",
        "barcode": pin,
        "sender": detail.get("custNm") or None,
        "receiver": None,
        "status": status,
        "raw_status": detail.get("status"),
        "delivered": delivered,
        "delivered_at": _delivered_at(detail, newest_first) if delivered else None,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": status is ParcelStatus.AT_PICKUP_POINT,
        "pickup_point": pickup_point,
        "url": tracking_url(pin, lang),
        "weight": None,
        "dimensions": None,
        "history": build_history(pairs, lang) if include_history else None,
        "raw": detail,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
