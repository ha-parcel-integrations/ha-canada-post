"""Tests for the shared status map."""
import logging

import pytest

from custom_components.canada_post.const import ParcelStatus
from custom_components.canada_post.status import (
    EVENT_CODE_MAP,
    EVENT_TYPE_MAP,
    STATUS_MAP,
    map_event_status,
    map_status,
    resolve_status,
)


@pytest.mark.parametrize(("key", "expected"), sorted(STATUS_MAP.items()))
def test_every_map_key(key, expected):
    assert map_status(key) is expected


def test_the_map_is_the_documented_one():
    assert STATUS_MAP["HalfDelivered"] is ParcelStatus.PROBLEM
    assert STATUS_MAP["ReadyPickup"] is ParcelStatus.AT_PICKUP_POINT
    assert len(STATUS_MAP) == 10


@pytest.mark.parametrize("value", [None, "", 5])
def test_missing_status_is_silently_unknown(value, caplog):
    with caplog.at_level(logging.WARNING):
        assert map_status(value) is ParcelStatus.UNKNOWN
    assert not caplog.records


def test_prefix_variant_hits_family_with_own_one_shot_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert map_status("InTransit-Item-Delay") is ParcelStatus.IN_TRANSIT
        assert map_status("InTransit-Item-Delay") is ParcelStatus.IN_TRANSIT
        assert map_status("HalfAccepted-Tomorrow") is ParcelStatus.REGISTERED
    assert len(caplog.records) == 2
    assert "family match" in caplog.records[0].getMessage()
    assert "issues/new?template=unrecognised_status.yml" in caplog.text


def test_unseen_value_is_unknown_with_exactly_one_warning(caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert map_status("Mystery") is ParcelStatus.UNKNOWN
        assert map_status("Mystery-Sub") is ParcelStatus.UNKNOWN
    assert len(caplog.records) == 2
    assert caplog.records[0].getMessage().startswith("Unrecognised Canada Post status")


def test_return_override_both_flags():
    assert resolve_status({"status": "FullProgressAlert", "returnedToSender": True}) is ParcelStatus.RETURNING
    assert resolve_status({"status": "InTransit", "returnPinIndicator": True}) is ParcelStatus.RETURNING


def test_customs_refused_without_return_flags_stays_problem():
    detail = {"status": "FullProgressAlert", "returnPinIndicator": False}
    assert resolve_status(detail) is ParcelStatus.PROBLEM


def test_return_override_never_beats_delivered():
    assert resolve_status({"status": "Delivered", "returnPinIndicator": True}) is ParcelStatus.DELIVERED
    assert resolve_status({"status": "InTransit", "returnedToSender": True, "delivered": True}) is ParcelStatus.DELIVERED


@pytest.mark.parametrize(("code", "expected"), sorted(EVENT_CODE_MAP.items()))
def test_event_code_outranks_type(code, expected):
    assert map_event_status({"cd": code, "type": "Info"}) is expected


@pytest.mark.parametrize(("event_type", "expected"), sorted(EVENT_TYPE_MAP.items()))
def test_event_type_when_code_is_not_special(event_type, expected):
    assert map_event_status({"cd": "0170", "type": event_type}) is expected


def test_attempted_scan_that_sends_the_parcel_back_is_returning():
    assert map_event_status({"cd": "1419", "type": "Attempted"}) is ParcelStatus.RETURNING
    assert map_event_status({"cd": "1479", "type": "Attempted"}) is ParcelStatus.PROBLEM


def test_unseen_event_type_is_unknown_with_one_warning(caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            assert map_event_status({"cd": "9999", "type": "Mystery"}) is ParcelStatus.UNKNOWN
    assert caplog.text.count("Unrecognised Canada Post event type") == 1


def test_unmapped_status_falls_back_to_newest_mappable_event_without_warning(caplog):
    events = [
        {"cd": "9999", "type": "Mystery"},
        {"cd": "0174", "type": "Out"},
        {"cd": "1301", "type": "Induction"},
    ]
    with caplog.at_level(logging.WARNING):
        status = resolve_status({"status": "BrandNewStatus"}, events)
    assert status is ParcelStatus.OUT_FOR_DELIVERY
    assert "Unrecognised Canada Post status" not in caplog.text


def test_unmapped_status_without_usable_history_warns_once(caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            assert resolve_status({"status": "BrandNewStatus"}, []) is ParcelStatus.UNKNOWN
            assert (
                resolve_status({"status": "BrandNewStatus"}, [{"cd": "9999", "type": "Mystery"}])
                is ParcelStatus.UNKNOWN
            )
    assert caplog.text.count("Unrecognised Canada Post status") == 1


def test_mapped_status_wins_over_history():
    events = [{"cd": "1466", "type": "Delivered"}]
    assert resolve_status({"status": "InTransit"}, events) is ParcelStatus.IN_TRANSIT


def test_history_fallback_still_gets_the_return_override():
    events = [{"cd": "0170", "type": "Info"}]
    status = resolve_status({"status": "BrandNewStatus", "returnedToSender": True}, events)
    assert status is ParcelStatus.RETURNING


@pytest.mark.parametrize(
    ("event_type", "code", "expected"),
    [
        ("FromCust", "0910", ParcelStatus.IN_TRANSIT),
        ("VehicleInfo", "0410", ParcelStatus.IN_TRANSIT),
        ("VehicleInfo", "0405", ParcelStatus.IN_TRANSIT),
        ("Signature", "20", ParcelStatus.DELIVERED),
    ],
)
def test_event_types_first_seen_on_a_real_account(event_type, code, expected, caplog):
    with caplog.at_level(logging.WARNING):
        assert map_event_status({"cd": code, "type": event_type}) is expected
    assert "Unrecognised" not in caplog.text


def test_delivered_flag_forces_delivered_whatever_the_status_says():
    assert resolve_status({"status": "FullProgress", "delivered": True}) is ParcelStatus.DELIVERED
    assert resolve_status({"status": "BrandNewStatus", "delivered": True}, []) is ParcelStatus.DELIVERED


def test_unconfirmed_return_code_warns_once_with_its_issue(caplog):
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            assert map_event_status({"cd": "3001", "type": "Info"}) is ParcelStatus.RETURNING
    assert caplog.text.count("Canada Post return event seen") == 1
    assert "ha-canada-post/issues/3" in caplog.text
