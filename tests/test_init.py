"""Tests for Canada Post setup and unload."""
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.canada_post.account.client import (
    CanadaPostAccountApiError,
    CanadaPostAccountReauthRequired,
)
from custom_components.canada_post.const import (
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_ID_TOKEN,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    CONF_USERNAME,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from custom_components.canada_post.tracking.api import CanadaPostApiError

from .payloads import ACTIVE_CODE, delivered_detail
from .payloads import active_sample as _sample

OTHER_CODE = "222222222222"


async def test_setup_and_unload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.canada_post.tracking.api.CanadaPostTrackingClient.async_get_parcel",
        new=AsyncMock(return_value=_sample()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED

    # The active parcel produced a per-parcel sensor and the summary sensor.
    incoming = hass.states.get("sensor.canada_post_incoming_parcels")
    assert incoming is not None
    assert incoming.state == "1"

    # Services registered on setup...
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED

    # ...and removed on unload (single-instance integration).
    assert not hass.services.has_service(DOMAIN, "track_parcel")


async def test_setup_retries_when_first_refresh_fails(hass):
    """When the first data fetch fails, setup retries from the entry itself.

    The first refresh runs in __init__.py before platforms are forwarded, so a
    failure raises ConfigEntryNotReady from the entry setup (SETUP_RETRY) rather
    than — too late — from a forwarded platform.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.canada_post.tracking.api.CanadaPostTrackingClient.async_get_parcel",
        new=AsyncMock(side_effect=CanadaPostApiError("Canada Post unreachable")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_per_parcel_sensor_spawn_and_remove(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=_sample())
    with patch("custom_components.canada_post.tracking.api.CanadaPostTrackingClient.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
        )

        # Swapping the tracked code: the summary sensor spawns a new
        # per-parcel sensor and removes the stale one.
        mock.side_effect = lambda code: _sample(code)
        hass.config_entries.async_update_entry(
            entry, options={CONF_PARCELS: [{CONF_TRACKING_CODE: OTHER_CODE}]}
        )
        await hass.async_block_till_done()

        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{OTHER_CODE}"
        )
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{ACTIVE_CODE}"
            )
            is None
        )


async def test_options_update_applies_live_without_reload(hass):
    """Adding a parcel via options refreshes the coordinator immediately."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: ACTIVE_CODE}]},
    )
    entry.add_to_hass(hass)

    mock = AsyncMock(return_value=_sample())
    with patch("custom_components.canada_post.tracking.api.CanadaPostTrackingClient.async_get_parcel", new=mock):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        mock.side_effect = lambda code: _sample(code)
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_PARCELS: [
                    {CONF_TRACKING_CODE: ACTIVE_CODE},
                    {CONF_TRACKING_CODE: OTHER_CODE},
                ],
            },
        )
        await hass.async_block_till_done()

    incoming = hass.states.get("sensor.canada_post_incoming_parcels")
    assert incoming.state == "2"


ACCOUNT = "custom_components.canada_post.account.client.CanadaPostAccountClient"
DETAIL = "custom_components.canada_post.tracking.api.CanadaPostTrackingClient.async_get_parcel"


def _account_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="account:me@example.com",
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_USERNAME: "me@example.com",
            CONF_ACCESS_TOKEN: "a",
            CONF_REFRESH_TOKEN: "r",
            CONF_ID_TOKEN: "i",
            CONF_EXPIRES_AT: None,
        },
    )


def _item(track_id: str = ACTIVE_CODE) -> dict:
    return {"trackId": track_id, "source": "USER", "type": "DEFAULT", "userDescription": "Gift"}


async def test_account_setup_registers_no_services_and_sensors_work(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    with (
        patch(f"{ACCOUNT}.async_list_items", new=AsyncMock(return_value=[_item()])),
        patch(DETAIL, new=AsyncMock(return_value=_sample())),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        # The device is named after the login, so two accounts stay apart.
        assert hass.states.get("sensor.canada_post_me_example_com_incoming_parcels").state == "1"
        assert not hass.services.has_service(DOMAIN, "track_parcel")
        # The owner's own label names the per-parcel sensor.
        assert hass.states.get("sensor.canada_post_me_example_com_parcel_gift") is not None

        assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_account_unload_keeps_tracking_services(hass):
    tracking = MockConfigEntry(
        domain=DOMAIN, unique_id=SOURCE_TRACKING, data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: []},
    )
    tracking.add_to_hass(hass)
    account = _account_entry()
    account.add_to_hass(hass)
    with (
        patch(f"{ACCOUNT}.async_list_items", new=AsyncMock(return_value=[])),
        patch(DETAIL, new=AsyncMock(return_value=_sample())),
    ):
        assert await hass.config_entries.async_setup(tracking.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(account.entry_id) is True
    assert hass.services.has_service(DOMAIN, "track_parcel")


async def test_account_setup_rejected_refresh_starts_reauth(hass):
    """A bare client exception would retry setup forever without a prompt."""
    entry = _account_entry()
    entry.add_to_hass(hass)
    with patch(
        f"{ACCOUNT}.async_list_items",
        new=AsyncMock(side_effect=CanadaPostAccountReauthRequired("x")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_account_running_path_rejected_refresh_starts_reauth(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    mock = AsyncMock(return_value=[_item()])
    with patch(f"{ACCOUNT}.async_list_items", new=mock), patch(DETAIL, new=AsyncMock(return_value=_sample())):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        mock.side_effect = CanadaPostAccountReauthRequired("x")
        await entry.runtime_data.coordinator.async_refresh()
        await hass.async_block_till_done()
    assert any(flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress())


async def test_account_setup_retries_on_list_failure(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    with patch(
        f"{ACCOUNT}.async_list_items",
        new=AsyncMock(side_effect=CanadaPostAccountApiError("x")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_account_token_callback_persists_tokens(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    with (
        patch(f"{ACCOUNT}.async_list_items", new=AsyncMock(return_value=[])),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await entry.runtime_data.client._token_callback(
            {CONF_ACCESS_TOKEN: "a2", CONF_REFRESH_TOKEN: "r2", CONF_ID_TOKEN: "i2", CONF_EXPIRES_AT: 5.0}
        )
    assert entry.data[CONF_ID_TOKEN] == "i2"
    assert entry.data[CONF_USERNAME] == "me@example.com"
    assert "password" not in entry.data


async def test_account_delivered_pin_is_not_refetched(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    detail_mock = AsyncMock(return_value=delivered_detail(ACTIVE_CODE))
    with (
        patch(f"{ACCOUNT}.async_list_items", new=AsyncMock(return_value=[_item()])),
        patch(DETAIL, new=detail_mock),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await entry.runtime_data.coordinator.async_refresh()
    assert detail_mock.await_count == 1


async def test_rotated_tokens_do_not_trigger_another_poll(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    listing = AsyncMock(return_value=[_item()])
    with (
        patch(f"{ACCOUNT}.async_list_items", new=listing),
        patch(DETAIL, new=AsyncMock(return_value=_sample())),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        calls = listing.await_count

        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_ACCESS_TOKEN: "a2", CONF_ID_TOKEN: "i2"}
        )
        await hass.async_block_till_done()
        assert listing.await_count == calls

        hass.config_entries.async_update_entry(entry, options={CONF_INCLUDE_HISTORY: True})
        await hass.async_block_till_done()
        assert listing.await_count == calls + 1
