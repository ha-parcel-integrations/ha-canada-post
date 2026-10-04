"""Tests for the account coordinator: list, then detail per PIN."""
import asyncio
from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.canada_post.account.client import (
    CanadaPostAccountApiError,
    CanadaPostAccountReauthRequired,
)
from custom_components.canada_post.account.coordinator import (
    CanadaPostAccountCoordinator,
)
from custom_components.canada_post.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_INCLUDE_HISTORY,
    CONF_SOURCE,
    DETAIL_FETCH_CONCURRENCY,
    DOMAIN,
    SOURCE_ACCOUNT,
    ParcelStatus,
)
from custom_components.canada_post.tracking.api import CanadaPostApiError

from ..payloads import active_sample, delivered_detail

PIN_A = "111111111111"
PIN_B = "222222222222"
DNC = "123456789012345"


def _item(track_id, **extra):
    return {"trackId": track_id, "source": "SET_AND_FORGET", "type": "DEFAULT", "userDescription": None, **extra}


def _make(hass, items, detail=None, **options):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:me",
        data={CONF_SOURCE: SOURCE_ACCOUNT},
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 100,
            **options,
        },
    )
    entry.add_to_hass(hass)
    client = AsyncMock()
    client.async_list_items.return_value = items
    tracking = AsyncMock()
    tracking.async_get_parcel.side_effect = detail or (lambda pin: active_sample(pin))
    coordinator = CanadaPostAccountCoordinator(hass, client, tracking, entry)
    return coordinator, client, tracking


async def test_lists_then_fetches_detail_per_pin_through_tracking_client(hass):
    coordinator, client, tracking = _make(
        hass, [_item(PIN_A, userDescription="Gift", source="FLEX"), _item(PIN_B)]
    )
    data = await coordinator._async_update_data()
    assert {p["barcode"] for p in data} == {PIN_A, PIN_B}
    assert tracking.async_get_parcel.await_count == 2
    parcel = next(p for p in data if p["barcode"] == PIN_A)
    assert parcel["raw"]["account"] == {"source": "FLEX", "type": "DEFAULT", "userDescription": "Gift"}
    assert parcel["pickup"] is False  # a FLEX source is a hint only
    assert coordinator.last_success_time is not None


async def test_account_extras_do_not_mutate_the_cached_detail(hass):
    coordinator, _, _ = _make(hass, [_item(PIN_A)])
    await coordinator._async_update_data()
    assert "account" not in coordinator._raw_cache[PIN_A]


async def test_delivered_pins_are_not_refetched(hass):
    coordinator, _, tracking = _make(
        hass, [_item(PIN_A), _item(PIN_B)],
        detail=lambda pin: delivered_detail(pin) if pin == PIN_A else active_sample(pin),
    )
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    fetched = [c.args[0] for c in tracking.async_get_parcel.await_args_list]
    assert fetched.count(PIN_A) == 1 and fetched.count(PIN_B) == 2
    assert coordinator.delivered_codes == {PIN_A}
    assert [p["barcode"] for p in coordinator.delivered] == [PIN_A]


async def test_one_bad_pin_keeps_the_others_and_its_previous_value(hass):
    coordinator, _, tracking = _make(hass, [_item(PIN_A), _item(PIN_B)])
    await coordinator._async_update_data()

    def flaky(pin):
        if pin == PIN_A:
            raise CanadaPostApiError("HTTP 500", status_code=500)
        return active_sample(pin, status="InTransit")

    tracking.async_get_parcel.side_effect = flaky
    data = await coordinator._async_update_data()
    by_pin = {p["barcode"]: p for p in data}
    assert by_pin[PIN_A]["status"] is ParcelStatus.OUT_FOR_DELIVERY  # previous value
    assert by_pin[PIN_B]["status"] is ParcelStatus.IN_TRANSIT


async def test_every_fetch_failing_with_nothing_cached_is_update_failed(hass):
    coordinator, _, _ = _make(hass, [_item(PIN_A)], detail=AsyncMock(side_effect=aiohttp.ClientError("x")))
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.last_success_time is None


async def test_every_fetch_failing_keeps_the_previous_values(hass):
    coordinator, _, tracking = _make(hass, [_item(PIN_A)])
    await coordinator._async_update_data()
    tracking.async_get_parcel.side_effect = aiohttp.ClientError("x")
    data = await coordinator._async_update_data()
    assert data[0]["status"] is ParcelStatus.OUT_FOR_DELIVERY


async def test_detail_fetches_are_capped_in_parallel(hass):
    running = peak = 0

    async def slow(pin):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        return None

    pins = [f"{n:016d}" for n in range(1, 21)]
    coordinator, _, _ = _make(hass, [_item(p) for p in pins], detail=AsyncMock(side_effect=slow))
    await coordinator._async_update_data()
    assert 1 <= peak <= DETAIL_FETCH_CONCURRENCY


async def test_not_found_detail_keeps_cache_or_placeholder(hass):
    coordinator, _, tracking = _make(hass, [_item(PIN_A)])
    await coordinator._async_update_data()
    tracking.async_get_parcel.side_effect = lambda pin: None
    data = await coordinator._async_update_data()
    assert data[0]["status"] is ParcelStatus.OUT_FOR_DELIVERY


async def test_unexpected_exception_is_not_swallowed(hass):
    coordinator, _, _ = _make(hass, [_item(PIN_A)], detail=AsyncMock(side_effect=ValueError("bug")))
    with pytest.raises(ValueError):
        await coordinator._async_update_data()


async def test_429_backs_off_with_retry_after(hass):
    coordinator, _, _ = _make(
        hass, [_item(PIN_A)],
        detail=AsyncMock(side_effect=CanadaPostApiError("HTTP 429", status_code=429, retry_after=90)),
    )
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after == 90


async def test_429_without_header_uses_exponential_backoff(hass):
    coordinator, _, _ = _make(
        hass, [_item(PIN_A)], detail=AsyncMock(side_effect=CanadaPostApiError("HTTP 429", status_code=429))
    )
    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after == 120


async def test_429_counter_resets_on_success(hass):
    coordinator, _, tracking = _make(hass, [_item(PIN_A)])
    coordinator._consecutive_429 = 3
    await coordinator._async_update_data()
    assert coordinator._consecutive_429 == 0


async def test_rejected_refresh_is_config_entry_auth_failed(hass):
    coordinator, client, _ = _make(hass, [])
    client.async_list_items.side_effect = CanadaPostAccountReauthRequired("x")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_list_failure_is_update_failed(hass):
    coordinator, client, _ = _make(hass, [])
    client.async_list_items.side_effect = CanadaPostAccountApiError("x")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_notice_card_is_resolved_once_and_cached(hass):
    coordinator, _, tracking = _make(hass, [_item(DNC)])
    tracking.async_resolve_dnc.return_value = PIN_A
    await coordinator._async_update_data()
    await coordinator._async_update_data()
    tracking.async_resolve_dnc.assert_awaited_once_with(DNC)
    assert tracking.async_get_parcel.await_args_list[0].args[0] == PIN_A


async def test_unresolved_notice_card_shows_a_placeholder_parcel(hass):
    coordinator, _, tracking = _make(hass, [_item(DNC)])
    tracking.async_resolve_dnc.return_value = None
    data = await coordinator._async_update_data()
    assert data[0]["barcode"] == DNC and data[0]["status"] is ParcelStatus.UNKNOWN
    tracking.async_get_parcel.assert_not_awaited()


async def test_notice_card_lookup_error_is_tolerated(hass):
    coordinator, _, tracking = _make(hass, [_item(DNC), _item(PIN_A)])
    tracking.async_resolve_dnc.side_effect = CanadaPostApiError("x")
    data = await coordinator._async_update_data()
    assert {p["barcode"] for p in data} == {DNC, PIN_A}


async def test_whitespace_and_case_in_track_id_are_normalised(hass):
    coordinator, _, tracking = _make(hass, [_item(" rn123456789ca ")])
    await coordinator._async_update_data()
    assert tracking.async_get_parcel.await_args.args[0] == "RN123456789CA"


async def test_every_account_parcel_fires_the_incoming_event_set_only(hass):
    coordinator, client, tracking = _make(hass, [_item(PIN_A)])
    fired = []
    for name in ("registered", "status_changed", "delivered", "delivery_time_changed"):
        hass.bus.async_listen(f"{DOMAIN}_parcel_{name}", lambda e: fired.append(e.event_type))
    outgoing = []
    hass.bus.async_listen(f"{DOMAIN}_outgoing_parcel_status_changed", lambda e: outgoing.append(e))
    await coordinator._async_update_data()  # first refresh: silent
    client.async_list_items.return_value = [_item(PIN_A), _item(PIN_B)]
    tracking.async_get_parcel.side_effect = lambda pin: (
        active_sample(pin, status="InTransit") if pin == PIN_A else active_sample(pin)
    )
    await coordinator._async_update_data()
    await hass.async_block_till_done()
    assert f"{DOMAIN}_parcel_registered" in fired
    assert f"{DOMAIN}_parcel_status_changed" in fired
    assert not outgoing


async def test_history_option_and_tiering(hass):
    coordinator, _, _ = _make(hass, [_item(PIN_A)], **{CONF_INCLUDE_HISTORY: True})
    data = await coordinator._async_update_data()
    assert data[0]["history"]
    assert coordinator.current_tier_minutes in (15, 45)
    assert coordinator.update_interval is not None


async def test_empty_account_still_polls(hass):
    coordinator, _, _ = _make(hass, [])
    assert await coordinator._async_update_data() == []
    assert coordinator.update_interval is not None
    assert coordinator.last_success_time is not None


async def test_device_id_is_cached(hass):
    from homeassistant.helpers import device_registry as dr

    coordinator, _, _ = _make(hass, [])
    assert coordinator._device_id() is None
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id, identifiers={(DOMAIN, "x")}
    )
    assert coordinator._device_id() == device.id
    assert coordinator._device_id() == device.id
