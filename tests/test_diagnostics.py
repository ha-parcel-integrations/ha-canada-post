"""Tests for Canada Post diagnostics."""
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.canada_post.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_redacts_and_counts(hass):
    """Diagnostics get pasted into public issues — nothing identifying may survive."""
    entry = MagicMock()
    entry.data = {"source": "account", "username": "me@example.com", "id_token": "sekrit-token"}
    entry.options = {"parcels": [{"tracking_code": "123456789012"}]}
    entry.runtime_data.coordinator.current_tier_minutes = 15
    entry.runtime_data.coordinator.update_interval = timedelta(minutes=15)
    entry.runtime_data.coordinator.data = [
        {
            "barcode": "123456789012",
            "sender": "Example Shop",
            "receiver": None,
            "status": "out_for_delivery",
            "raw_status": "FullProgress",
            "pickup_point": "Test PO, Testville",
            "raw": {
                "pin": "123456789012",
                "status": "FullProgress",
                "custNm": "Example Shop",
                "recipientNm": "Jane Doe",
                "shipToAddr": {"addrLn1": "1 Example Rd", "city": "Testville"},
                "expectedDlvryDateTime": {"dlvryDate": "2026-04-29"},
                "addtnlOrigInfo": "ORIGINTOWN, ON",
                "addtnlDestInfo": "HOMETOWN, ON",
                "events": [
                    {"cd": "0174", "type": "Out", "descEn": "Out for delivery at 1 Example Rd",
                     "datetime": {"date": "2026-04-29"},
                     "retailNmEn": "Test PO", "retailNmFr": "Test PO", "retailLocationId": "0000",
                     "locationAddr": {"city": "Testville", "regionCd": "ON", "postCd": "A1A1A1"}}
                ],
                "account": {"userDescription": "Gift"},
            },
        }
    ]
    entry.runtime_data.coordinator.delivered = []
    entry.runtime_data.coordinator.delivered_codes = set()

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["counts"] == {
        "incoming_active": 1,
        "delivered": 0,
        "skipped_from_fetch": 0,
    }
    assert result["polling"] == {
        "tier_minutes": 15,
        "update_interval_seconds": 900.0,
        "suspended": False,
    }
    # tracking codes and payload PII are redacted, at every nesting level
    assert result["entry_options"]["parcels"][0]["tracking_code"] == "**REDACTED**"
    assert result["incoming"][0]["barcode"] == "**REDACTED**"
    assert set(result["incoming"][0]) == set(entry.runtime_data.coordinator.data[0])
    raw = result["incoming"][0]["raw"]
    assert raw["pin"] == raw["recipientNm"] == raw["custNm"] == "**REDACTED**"
    assert raw["shipToAddr"] == "**REDACTED**"
    assert raw["account"]["userDescription"] == "**REDACTED**"
    assert raw["addtnlDestInfo"] == raw["addtnlOrigInfo"] == "**REDACTED**"
    assert result["incoming"][0]["pickup_point"] == "**REDACTED**"
    assert "HOMETOWN" not in str(result) and "Test PO" not in str(result)
    event = raw["events"][0]
    assert event["descEn"] == event["locationAddr"]["postCd"] == "**REDACTED**"
    # tokens and the username never survive in the entry data
    assert result["entry_data"]["id_token"] == "**REDACTED**"
    assert result["entry_data"]["username"] == "**REDACTED**"
    assert "sekrit-token" not in str(result) and "me@example.com" not in str(result)
    # what a user report needs to confirm the status map and shape survives
    assert result["incoming"][0]["status"] == "out_for_delivery"
    assert raw["status"] == "FullProgress"
    assert event["cd"] == "0174" and event["type"] == "Out"
    assert event["locationAddr"]["city"] == "Testville"
    assert event["locationAddr"]["regionCd"] == "ON"
    assert raw["expectedDlvryDateTime"] == {"dlvryDate": "2026-04-29"}
    assert event["datetime"] == {"date": "2026-04-29"}


async def test_diagnostics_reports_suspended_polling(hass):
    """update_interval None (Section 2.1's full stop) must be visible, not just absent."""
    entry = MagicMock()
    entry.data = {"source": "tracking"}
    entry.options = {"parcels": []}
    entry.runtime_data.coordinator.current_tier_minutes = None
    entry.runtime_data.coordinator.update_interval = None
    entry.runtime_data.coordinator.data = []
    entry.runtime_data.coordinator.delivered = []

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["polling"] == {
        "tier_minutes": None,
        "update_interval_seconds": None,
        "suspended": True,
    }
