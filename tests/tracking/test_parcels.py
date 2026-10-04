"""Tests for the shared normaliser and list helpers (no Home Assistant needed)."""
import logging
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.canada_post.const import (
    CAPABILITIES,
    CAPABILITIES_BY_VARIANT,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DOMAIN,
    KNOWN_CAPABILITIES,
    PENDING_CAPABILITIES,
    ParcelStatus,
)
from custom_components.canada_post.tracking.parcels import (
    _dated_events,
    _parse_date,
    _parse_offset,
    _parse_time,
    apply_delivered_filter,
    build_history,
    display_name,
    normalize_parcel,
    parse_iso,
    resolve_lang,
    sort_parcels_by_ts,
    tracking_url,
)

from ..payloads import (
    ACTIVE_CODE,
    DELIVERED_CODE,
    delivered_detail,
    detail,
    event,
    pickup_detail,
)

CANONICAL_KEYS = [
    "carrier", "barcode", "sender", "receiver", "status", "raw_status",
    "delivered", "delivered_at", "planned_from", "planned_to", "pickup",
    "pickup_point", "url", "weight", "dimensions", "history", "raw",
]


def test_capabilities_are_consistent():
    assert CAPABILITIES <= KNOWN_CAPABILITIES
    assert CAPABILITIES == CAPABILITIES_BY_VARIANT["Tracking"]
    assert "pickup_point" in CAPABILITIES
    assert not PENDING_CAPABILITIES
    for variant in CAPABILITIES_BY_VARIANT.values():
        assert variant <= KNOWN_CAPABILITIES


def test_normalize_publishes_exactly_the_canonical_keys():
    parcel = normalize_parcel(detail(), pin=ACTIVE_CODE)
    assert list(parcel) == CANONICAL_KEYS


def test_raw_is_the_untouched_payload():
    payload = detail(custNm="Someone")
    assert normalize_parcel(payload, pin=ACTIVE_CODE)["raw"] is payload


def test_in_flight_parcel_fields():
    parcel = normalize_parcel(detail(), pin=ACTIVE_CODE)
    assert parcel["carrier"] == "Canada Post"
    assert parcel["barcode"] == ACTIVE_CODE
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "FullProgress"
    assert parcel["delivered"] is False
    assert parcel["delivered_at"] is None
    assert parcel["sender"] is None
    assert parcel["receiver"] is None
    assert parcel["weight"] is None and parcel["dimensions"] is None
    assert parcel["history"] is None
    assert parcel["pickup"] is False and parcel["pickup_point"] is None


def test_eta_is_date_only_and_revised_date_wins():
    parcel = normalize_parcel(detail(), pin=ACTIVE_CODE)
    start = datetime.fromisoformat(parcel["planned_from"])
    assert start.date().isoformat() == "2026-04-30"
    assert (start.hour, start.minute) == (0, 0)
    assert start.tzinfo is not None
    assert parcel["planned_to"] is None


def test_eta_falls_back_to_dlvry_date():
    payload = detail(expectedDlvryDateTime={"dlvryDate": "2026-05-02"})
    start = datetime.fromisoformat(normalize_parcel(payload, pin=ACTIVE_CODE)["planned_from"])
    assert start.date().isoformat() == "2026-05-02"


@pytest.mark.parametrize("eta", [None, {}, {"revisedDate": "garbage"}, "x"])
def test_missing_or_bad_eta_is_none(eta):
    payload = detail()
    payload["expectedDlvryDateTime"] = eta
    parcel = normalize_parcel(payload, pin=ACTIVE_CODE)
    assert parcel["planned_from"] is None and parcel["planned_to"] is None


def test_eta_window_combines_date_with_window_times_and_warns_once(caplog):
    window = {"dlvryWindowStartTime": "13:00", "dlvryWindowEndTime": "15:30:00"}
    payload = detail(expectedDlvryWindow=window)
    with caplog.at_level(logging.WARNING):
        parcel = normalize_parcel(payload, pin=ACTIVE_CODE)
        normalize_parcel(payload, pin=ACTIVE_CODE)
    start = datetime.fromisoformat(parcel["planned_from"])
    end = datetime.fromisoformat(parcel["planned_to"])
    assert (start.hour, start.minute) == (13, 0)
    assert (end.hour, end.minute) == (15, 30)
    assert caplog.text.count("delivery window seen") == 1
    assert "ha-canada-post/issues/1" in caplog.text


def test_eta_window_eod_without_end_time_ends_at_2359():
    payload = detail(expectedDlvryWindow={"dlvryWindowStartTime": "0900", "dlvryWindowEOD": True})
    parcel = normalize_parcel(payload, pin=ACTIVE_CODE)
    assert datetime.fromisoformat(parcel["planned_to"]).strftime("%H:%M") == "23:59"
    assert datetime.fromisoformat(parcel["planned_from"]).strftime("%H:%M") == "09:00"


def test_eta_window_without_times_keeps_date_midnight_and_no_end():
    payload = detail(expectedDlvryWindow={"dlvryWindowStartTime": "bad"})
    parcel = normalize_parcel(payload, pin=ACTIVE_CODE)
    assert datetime.fromisoformat(parcel["planned_from"]).hour == 0
    assert parcel["planned_to"] is None


def test_delivered_from_flag_clears_eta_and_uses_1466_event():
    parcel = normalize_parcel(delivered_detail(), pin=DELIVERED_CODE)
    assert parcel["delivered"] is True
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["planned_from"] is None and parcel["planned_to"] is None
    assert parcel["delivered_at"] == "2026-04-29T13:12:42-04:00"
    assert parcel["sender"] == "Example Shop"


def test_delivered_at_falls_back_to_actual_date_at_local_midnight():
    payload = delivered_detail(events=[])
    parcel = normalize_parcel(payload, pin=DELIVERED_CODE)
    moment = datetime.fromisoformat(parcel["delivered_at"])
    assert moment.date().isoformat() == "2026-04-29" and moment.hour == 0


def test_delivered_at_none_without_event_or_date():
    payload = delivered_detail(events=[])
    del payload["actualDlvryDate"]
    assert normalize_parcel(payload, pin=DELIVERED_CODE)["delivered_at"] is None


def test_delivered_comes_from_flag_or_status_never_event_text():
    payload = detail(events=[event("0174", "2026-04-29", "08:00:00", "Item delivered")])
    assert normalize_parcel(payload, pin=ACTIVE_CODE)["delivered"] is False
    assert normalize_parcel(detail(status="Delivered"), pin=ACTIVE_CODE)["delivered"] is True
    assert normalize_parcel(detail(delivered=True), pin=ACTIVE_CODE)["delivered"] is True


def test_returned_parcel_is_returning_but_customs_refusal_is_problem():
    returned = detail(status="FullProgressAlert", returnedToSender=True, returnPinIndicator=True)
    customs = detail(status="FullProgressAlert", returnPinIndicator=False)
    assert normalize_parcel(returned, pin=ACTIVE_CODE)["status"] is ParcelStatus.RETURNING
    assert normalize_parcel(customs, pin=ACTIVE_CODE)["status"] is ParcelStatus.PROBLEM


def test_pickup_point_from_newest_retail_event():
    parcel = normalize_parcel(pickup_detail(), pin=ACTIVE_CODE)
    assert parcel["status"] is ParcelStatus.AT_PICKUP_POINT
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] == "Example Office, Testville"


def test_pickup_point_without_city_or_retail_event():
    payload = pickup_detail()
    payload["events"][0]["locationAddr"] = None
    assert normalize_parcel(payload, pin=ACTIVE_CODE)["pickup_point"] == "Example Office"
    payload["events"] = [event("0174", "2026-04-29", "08:00:00", "x")]
    assert normalize_parcel(payload, pin=ACTIVE_CODE)["pickup_point"] is None


def test_retail_event_alone_does_not_populate_pickup_point():
    payload = detail(events=pickup_detail()["events"])
    parcel = normalize_parcel(payload, pin=ACTIVE_CODE)
    assert parcel["pickup"] is False and parcel["pickup_point"] is None


def test_url_follows_language():
    assert tracking_url(ACTIVE_CODE).endswith(f"/track-reperage/en#/details/{ACTIVE_CODE}")
    assert "/track-reperage/fr#" in tracking_url(ACTIVE_CODE, "fr")
    assert tracking_url("") is None
    assert resolve_lang("fr-CA") == "fr" and resolve_lang("nl") == "en" and resolve_lang(None) == "en"


def test_pin_mismatch_warns_once(caplog):
    with caplog.at_level(logging.WARNING):
        normalize_parcel(detail(pin="111111111111"), pin=ACTIVE_CODE)
        normalize_parcel(detail(pin="111111111111"), pin=ACTIVE_CODE)
    assert caplog.text.count("different PIN") == 1


def test_not_found_placeholder_is_unknown():
    parcel = normalize_parcel({"pin": ACTIVE_CODE}, pin=ACTIVE_CODE)
    assert parcel["status"] is ParcelStatus.UNKNOWN


def test_history_sorted_oldest_first_even_when_wire_order_is_wrong():
    payload = detail(
        events=[
            event("4100", "2026-04-27", "10:00:00", "older listed first"),
            event("0174", "2026-04-29", "08:46:00", "newest"),
            event("4202", "2026-04-28", "10:00:00", "middle"),
        ]
    )
    history = normalize_parcel(payload, pin=ACTIVE_CODE, include_history=True)["history"]
    assert [h["raw_status"] for h in history] == ["older listed first", "middle", "newest"]
    assert [h["status"] for h in history] == [
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.IN_TRANSIT,
        ParcelStatus.OUT_FOR_DELIVERY,
    ]
    assert all(datetime.fromisoformat(h["timestamp"]).tzinfo for h in history)


def test_history_language_and_cap():
    events = [event("0190", f"2026-04-{d:02d}", "10:00:00", f"e{d}") for d in range(1, 26)]
    payload = detail(events=list(reversed(events)))
    history = normalize_parcel(payload, pin=ACTIVE_CODE, lang="fr", include_history=True)["history"]
    assert len(history) == 20
    assert history[-1]["raw_status"] == "e25 (fr)"
    assert history[0]["raw_status"] == "e6 (fr)"


def test_history_falls_back_to_english_then_code():
    entry = event("0190", "2026-04-01", "10:00:00", "plain")
    del entry["descFr"]
    assert build_history(_dated_events({"events": [entry]}), "fr")[0]["raw_status"] == "plain"
    del entry["descEn"]
    assert build_history(_dated_events({"events": [entry]}), "en")[0]["raw_status"] == "0190"


def test_events_without_usable_datetime_are_skipped_from_history():
    odd = {"cd": "1", "datetime": "nope"}
    nodate = {"cd": "2", "datetime": {"date": "x"}}
    assert build_history(_dated_events({"events": [odd, nodate, "junk"]}), "en") == []
    assert _dated_events({"events": "bad"}) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [("-0400", -4), ("-04:00", -4), ("+05", 5), ("Z", 0)],
)
def test_offset_formats(value, expected):
    assert _parse_offset(value).utcoffset(None) == timedelta(hours=expected)


def test_unparseable_parts_return_none():
    assert _parse_offset("EST") is None and _parse_offset(5) is None
    assert _parse_time("xx") is None and _parse_time(5) is None and _parse_time("25:00") is None
    assert _parse_time("143015").second == 15
    assert _parse_date(5) is None and _parse_date("nope") is None
    assert _parse_date("20260429").isoformat() == "2026-04-29"


def test_unparseable_event_time_warns_once_and_uses_local_zone(caplog):
    payload = detail(events=[event("0190", "2026-04-29", "late", "x", offset="EST")])
    with caplog.at_level(logging.WARNING):
        normalize_parcel(payload, pin=ACTIVE_CODE, include_history=True)
        history = normalize_parcel(payload, pin=ACTIVE_CODE, include_history=True)["history"]
    assert caplog.text.count("event time not understood") == 1
    assert history[0]["timestamp"].startswith("2026-04-29T00:00:00")


def test_naive_event_without_offset_uses_local_zone():
    entry = event("0190", "2026-04-29", "10:00:00", "x")
    del entry["datetime"]["zoneOffset"]
    history = build_history(_dated_events({"events": [entry]}), "en")
    assert datetime.fromisoformat(history[0]["timestamp"]).tzinfo is not None


def test_display_name_reads_account_label():
    assert display_name(None) is None
    assert display_name({"raw": {}}) is None
    assert display_name({"raw": {"account": {"userDescription": "  "}}}) is None
    assert display_name({"raw": {"account": {"userDescription": " Gift "}}}) == "Gift"
    assert display_name({"raw": {"account": {"userDescription": 5}}}) is None


def test_parse_iso_handles_z_naive_and_garbage():
    assert parse_iso("2026-04-29T13:12:42Z").tzinfo is not None
    assert parse_iso("2026-04-29T13:12:42").tzinfo == timezone.utc
    assert parse_iso("not-a-date") is None
    assert parse_iso(None) is None


def _entry(options=None):
    return MockConfigEntry(domain=DOMAIN, data={}, options=options or {})


def test_sort_puts_missing_timestamps_last():
    parcels = [
        {"barcode": "late", "planned_from": "2026-05-02T00:00:00+00:00"},
        {"barcode": "none", "planned_from": None},
        {"barcode": "early", "planned_from": "2026-05-01T00:00:00+00:00"},
    ]
    ordered = sort_parcels_by_ts(parcels, "planned_from")
    assert [p["barcode"] for p in ordered] == ["early", "late", "none"]
    desc = sort_parcels_by_ts(parcels, "planned_from", descending=True)
    assert [p["barcode"] for p in desc] == ["late", "early", "none"]


def test_delivered_filter_days_and_parcels():
    now = datetime.now(timezone.utc)
    parcels = [
        {"delivered_at": now.isoformat()},
        {"delivered_at": (now - timedelta(days=30)).isoformat()},
        {"delivered_at": None},
    ]
    days = apply_delivered_filter(
        parcels, _entry({CONF_DELIVERED_FILTER_TYPE: "days", CONF_DELIVERED_FILTER_AMOUNT: 7})
    )
    assert len(days) == 2
    count = apply_delivered_filter(
        parcels, _entry({CONF_DELIVERED_FILTER_TYPE: "parcels", CONF_DELIVERED_FILTER_AMOUNT: 1})
    )
    assert count == parcels[:1]


def test_unmapped_status_is_derived_from_the_newest_event_on_the_wire_order():
    payload = detail(
        status="BrandNewStatus",
        events=[
            event("1701", "2026-04-28", "10:00:00", "available for pickup", retail="Test PO"),
            event("0174", "2026-04-29", "08:46:00", "newest: out"),
        ],
    )
    parcel = normalize_parcel(payload, pin=ACTIVE_CODE)
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY
    assert parcel["raw_status"] == "BrandNewStatus"


def test_waiting_at_pickup_point_warns_once_with_keys_only(caplog):
    payload = detail(
        status="ReadyPickup",
        events=[event("1701", "2026-04-28", "10:00:00", "available", retail="Secret PO")],
    )
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            parcel = normalize_parcel(payload, pin=ACTIVE_CODE)
    assert parcel["pickup_point"] == "Secret PO, Testville"
    assert caplog.text.count("waiting at a pickup point") == 1
    assert "ha-canada-post/issues/2" in caplog.text
    assert "Secret PO" not in caplog.text and "Testville" not in caplog.text
