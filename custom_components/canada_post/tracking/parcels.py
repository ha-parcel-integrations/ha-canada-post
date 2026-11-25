"""Canonical parcel shape and list helpers, shared by both sources.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. The account source gets only a list
of PINs from Canada Post, so it normalises through here too.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
import logging
import re
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
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
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
        return UTC
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
        ((moment, event) for moment, event in pairs if moment is not None),
        key=lambda pair: pair[0],
        reverse=True,
    )
    return [*dated, *(pair for pair in pairs if pair[0] is None)]


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


def _moment(value: Any, zone: tzinfo) -> tuple[datetime, bool, str] | None:
    """Return ``(aware datetime, date_only, text)`` for a carrier time, or ``None``.

    A naive time or a bare date gets ``zone``; an aware time keeps its offset
    and, when it came in as a string, its own spelling.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.min, tzinfo=zone)
        return moment, True, moment.isoformat()
    elif isinstance(value, str) and value:
        try:
            if len(value) == 10:
                moment = datetime.combine(
                    date.fromisoformat(value), time.min, tzinfo=zone
                )
                return moment, True, moment.isoformat()
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo:
            return parsed, False, value
    else:
        return None
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)
    return moment, False, moment.isoformat()


def _end_of_day(moment: datetime, zone: tzinfo) -> datetime:
    local = moment.astimezone(zone)
    return datetime.combine(local.date(), time(23, 59, 59), tzinfo=zone)


def planned_window(
    start: Any, end: Any, *, zone: tzinfo
) -> tuple[str | None, str | None]:
    """Return ``(planned_from, planned_to)`` in the canonical window shape.

    ``start`` and ``end`` are ISO strings, dates or datetimes, or ``None``; an
    epoch number must be converted first (see ``to_iso_timestamp``). A window
    keeps both ends. One moment with a time ends at ``23:59:59`` the same local
    day; a date covers ``00:00:00`` to ``23:59:59``; two dates run from the
    first day's start to the last day's end. Equal values are one moment,
    reversed values are swapped, and ``planned_to`` is never set without
    ``planned_from``. ``zone`` is the carrier's own zone: it dates naive times
    and bare dates and decides which day a moment ends on, so a ``Z`` moment
    still ends on the carrier's local day. When nothing is left of that day
    after the moment, there is no ``planned_to``.
    """
    first = _moment(start, zone)
    last = _moment(end, zone)
    if first is None:
        first, last = last, None
    if first is None:
        return None, None

    def closing(moment: tuple[datetime, bool, str]) -> datetime:
        return _end_of_day(moment[0], zone) if moment[1] else moment[0]

    if last is not None and closing(last) < first[0]:
        first, last = last, first
    if last is not None and closing(last) == first[0]:
        last = None

    if last is None:
        end_of_day = _end_of_day(first[0], zone)
        return first[2], end_of_day.isoformat() if end_of_day > first[0] else None
    return first[2], closing(last).isoformat() if last[1] else last[2]


_NO_WINDOW_STATUSES = frozenset(
    {
        ParcelStatus.REGISTERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
        ParcelStatus.DELIVERED,
    }
)


def planned_for_status(
    status: ParcelStatus, planned_from: str | None, planned_to: str | None
) -> tuple[str | None, str | None]:
    """Empty the delivery window once the parcel is at its destination or done."""
    if status in _NO_WINDOW_STATUSES:
        return None, None
    return planned_from, planned_to


def is_pickup(
    route_flag: bool | None,
    status: ParcelStatus,
    event_statuses: Iterable[ParcelStatus | None] = (),
) -> bool:
    """Whether the delivery route ends at a pickup point.

    The carrier's own route flag wins; otherwise the parcel having reached
    ``AT_PICKUP_POINT`` at any point counts, so the value survives collection.
    """
    if route_flag:
        return True
    return status is ParcelStatus.AT_PICKUP_POINT or any(
        event is ParcelStatus.AT_PICKUP_POINT for event in event_statuses
    )


def _zone() -> tzinfo:
    return dt_util.get_default_time_zone()


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
        return planned_window(day, None, zone=_zone())
    _warn_eta_window_first_sighting(window)
    start = _parse_time(window.get("dlvryWindowStartTime"))
    end = _parse_time(window.get("dlvryWindowEndTime"))
    return planned_window(
        datetime.combine(day, start) if start else day,
        datetime.combine(day, end) if end else None,
        zone=_zone(),
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
        if name and map_event_status(event) is ParcelStatus.AT_PICKUP_POINT:
            return str(name)
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
    delivered = status is ParcelStatus.DELIVERED
    pickup = is_pickup(
        None, status, (map_event_status(event) for _, event in newest_first)
    )
    pickup_point = _pickup_point(newest_first) if pickup else None
    if status is ParcelStatus.AT_PICKUP_POINT:
        _warn_pickup_status_first_sighting(
            detail.get("status"), pickup_point is not None
        )
    planned_from, planned_to = planned_for_status(status, *_eta(detail))

    return {
        "carrier": "Canada Post",
        "barcode": pin,
        "sender": detail.get("custNm") or None,
        "receiver": None,
        "contents": None,
        "status": status,
        "raw_status": detail.get("status"),
        "delivered": delivered,
        "delivered_at": _delivered_at(detail, newest_first) if delivered else None,
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
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
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
