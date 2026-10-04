"""Synthetic Canada Post payloads shared by the test modules.

Shapes follow the public tracker's detail response. PINs, names, offices and
places are fictitious. Keeping them in one module means one place to fix when
a real parcel shows the shape differs.
"""
from __future__ import annotations

ACTIVE_CODE = "1234567890123456"
DELIVERED_CODE = "123456789012"
OTHER_CODE = "999999999999"


def event(
    cd: str,
    date: str,
    time: str,
    desc: str,
    *,
    offset: str = "-0400",
    retail: str | None = None,
    city: str = "Testville",
) -> dict:
    """One entry of the detail ``events`` list."""
    result = {
        "cd": cd,
        "type": "Info",
        "datetime": {"date": date, "time": time, "zoneOffset": offset},
        "descEn": desc,
        "descFr": f"{desc} (fr)",
        "locationAddr": {
            "city": city,
            "regionCd": "ON",
            "countryCd": "CA",
            "postCd": "A1A1A1",
        },
        "webCd": "1",
    }
    if retail:
        result["retailLocationId"] = "0000"
        result["retailNmEn"] = retail
        result["retailNmFr"] = retail
    return result


def detail(pin: str = ACTIVE_CODE, status: str = "FullProgress", **extra) -> dict:
    """A representative in-flight detail response."""
    payload = {
        "pin": pin,
        "status": status,
        "acceptedDate": "2026-04-27",
        "expectedDlvryDateTime": {"dlvryDate": "2026-04-29", "revisedDate": "2026-04-30"},
        "returnPinIndicator": False,
        "deliveryOptions": [],
        "shipToAddr": {"addrLn1": "1 Example Rd", "city": "Testville", "postCd": "A1A1A1"},
        "events": [
            event("0174", "2026-04-29", "08:46:00", "Item out for delivery"),
            event("0190", "2026-04-28", "15:52:17", "Item processed"),
            event("1301", "2026-04-27", "23:03:58", "Item inducted"),
        ],
    }
    payload.update(extra)
    return payload


def delivered_detail(pin: str = DELIVERED_CODE, **extra) -> dict:
    """A delivered parcel."""
    fields = {
        "delivered": True,
        "actualDlvryDate": "2026-04-29",
        "custNm": "Example Shop",
        "events": [
            event("1466", "2026-04-29", "13:12:42", "Item delivered"),
            event("0174", "2026-04-29", "08:46:00", "Item out for delivery"),
        ],
        **extra,
    }
    return detail(pin, "Delivered", **fields)


def pickup_detail(pin: str = ACTIVE_CODE, **extra) -> dict:
    """A parcel waiting at a post office."""
    fields = {
        "events": [
            event("1701", "2026-04-29", "10:00:00", "Item available for pickup",
                  retail="Example Office", city="Testville"),
            event("0174", "2026-04-29", "08:46:00", "Item out for delivery"),
        ],
        **extra,
    }
    return detail(pin, "ReadyPickup", **fields)


def not_found(pin: str = ACTIVE_CODE) -> dict:
    """The no-history envelope."""
    return {"pin": pin, "error": {"cd": "004", "descEn": "No PIN History"}}


active_sample = detail
delivered_sample = delivered_detail
pickup_sample = pickup_detail
