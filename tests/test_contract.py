"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime

import pytest

from custom_components.canada_post.const import ParcelStatus
from custom_components.canada_post.tracking.parcels import normalize_parcel, parse_iso

from .payloads import ACTIVE_CODE, delivered_detail, detail, event, pickup_detail

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]


def _with_eta(**extra) -> dict:
    return detail(**extra)


def _collected() -> dict:
    return delivered_detail(
        ACTIVE_CODE,
        events=[
            event("1466", "2026-04-29", "16:00:00", "Item delivered"),
            event(
                "1701",
                "2026-04-29",
                "10:00:00",
                "Item available for pickup",
                retail="Example Office",
            ),
        ],
    )


def _returning() -> dict:
    return pickup_detail(returnPinIndicator=True)


def _moment_only() -> dict:
    return detail(
        expectedDlvryWindow={"dlvryWindowStartTime": "13:00", "dlvryWindowEOD": True}
    )


SAMPLES = {
    "delivered": delivered_detail,
    "active": detail,
    "at_pickup_point": pickup_detail,
    "collected": _collected,
    "returning": _returning,
    "window": lambda: detail(
        expectedDlvryWindow={
            "dlvryWindowStartTime": "13:00",
            "dlvryWindowEndTime": "15:00",
        }
    ),
    "moment_only": _moment_only,
    "date_only": detail,
    "bare": lambda: {"pin": ACTIVE_CODE},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param](), pin=ACTIVE_CODE)


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if not parcel["delivered"]:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done_or_waiting(parcel):
    if parcel["status"] in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_a_bool_and_the_point_needs_it(parcel):
    assert isinstance(parcel["pickup"], bool)
    if not parcel["pickup"]:
        assert parcel["pickup_point"] is None


def test_a_waiting_parcel_has_its_eta_emptied():
    payload = pickup_detail()
    assert payload["expectedDlvryDateTime"]
    parcel = normalize_parcel(payload, pin=ACTIVE_CODE)
    assert parcel["planned_from"] is None and parcel["planned_to"] is None


def test_a_returning_parcel_has_its_eta_emptied():
    parcel = normalize_parcel(_returning(), pin=ACTIVE_CODE)
    assert parcel["status"] is ParcelStatus.RETURNING
    assert parcel["planned_from"] is None


def test_a_collected_pickup_parcel_stays_pickup():
    parcel = normalize_parcel(_collected(), pin=ACTIVE_CODE)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered"] is True
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Example Office"
    assert parcel["delivered_at"] is not None


def test_a_home_delivery_is_not_pickup():
    parcel = normalize_parcel(delivered_detail(), pin=ACTIVE_CODE)
    assert parcel["pickup"] is False and parcel["pickup_point"] is None


def test_a_moment_ends_at_the_end_of_that_day():
    parcel = normalize_parcel(_moment_only(), pin=ACTIVE_CODE)
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (start.hour, start.minute) == (13, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert end.date() == start.date()


def test_a_date_covers_the_whole_day():
    parcel = normalize_parcel(detail(), pin=ACTIVE_CODE)
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    assert (end.hour, end.minute, end.second, end.microsecond) == (23, 59, 59, 0)
    assert start.date() == end.date()
