"""Tests for the Canada Post services (track_parcel / untrack_parcel)."""
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.canada_post.const import (
    CONF_ACCESS_TOKEN,
    CONF_PARCELS,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    CONF_USERNAME,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from custom_components.canada_post.services import _resolve_entry
from custom_components.canada_post.tracking.api import CanadaPostApiError

from .payloads import active_sample

DETAIL = "custom_components.canada_post.tracking.api.CanadaPostTrackingClient.async_get_parcel"
DNC = "custom_components.canada_post.tracking.api.CanadaPostTrackingClient.async_resolve_dnc"
PIN = "999999999999"


async def _setup(hass, parcels: list[dict] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: parcels or []},
    )
    entry.add_to_hass(hass)
    with patch(DETAIL, new=AsyncMock(return_value=active_sample())):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _call(hass, service: str, code: str):
    with patch(DETAIL, new=AsyncMock(return_value=active_sample())):
        await hass.services.async_call(
            DOMAIN, service, {CONF_TRACKING_CODE: code}, blocking=True
        )
        await hass.async_block_till_done()


async def test_track_parcel_adds_normalised_pin(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", "rn 123456789 ca")
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: "RN123456789CA"}]


async def test_track_parcel_rejects_malformed_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "track_parcel", "abc")


async def test_track_parcel_duplicate_is_noop(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", PIN)
    await _call(hass, "track_parcel", PIN)
    assert len(entry.options[CONF_PARCELS]) == 1


async def test_track_parcel_resolves_notice_card(hass):
    entry = await _setup(hass)
    with patch(DNC, new=AsyncMock(return_value=PIN)):
        await _call(hass, "track_parcel", "123456789012345")
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: PIN}]


async def test_track_parcel_unresolved_notice_card_raises(hass):
    entry = await _setup(hass)
    with patch(DNC, new=AsyncMock(return_value=None)), pytest.raises(ServiceValidationError):
        await _call(hass, "track_parcel", "123456789012345")
    assert entry.options[CONF_PARCELS] == []


@pytest.mark.parametrize("error", [CanadaPostApiError("x"), aiohttp.ClientError()])
async def test_track_parcel_lookup_failure_raises(hass, error):
    await _setup(hass)
    with patch(DNC, new=AsyncMock(side_effect=error)), pytest.raises(ServiceValidationError):
        await _call(hass, "track_parcel", "123456789012345")


async def test_untrack_parcel_removes_from_options(hass):
    entry = await _setup(hass, parcels=[{CONF_TRACKING_CODE: PIN}])
    await _call(hass, "untrack_parcel", PIN)
    assert entry.options[CONF_PARCELS] == []


async def test_untrack_unknown_code_is_noop(hass):
    entry = await _setup(hass, parcels=[{CONF_TRACKING_CODE: PIN}])
    await _call(hass, "untrack_parcel", "000000000000")
    assert len(entry.options[CONF_PARCELS]) == 1


async def test_services_only_see_the_tracking_hub(hass):
    account = MockConfigEntry(
        domain=DOMAIN, unique_id="account:me", data={CONF_SOURCE: SOURCE_ACCOUNT, CONF_USERNAME: "me", CONF_ACCESS_TOKEN: "a"},
    )
    account.add_to_hass(hass)
    with pytest.raises(ServiceValidationError):
        _resolve_entry(hass)
    entry = await _setup(hass)
    assert _resolve_entry(hass) is entry
