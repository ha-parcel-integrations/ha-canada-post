"""Tests for update_contents, the track_parcel field and the store lifecycle."""

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.canada_post.account.coordinator import (
    CanadaPostAccountCoordinator,
)
from custom_components.canada_post.const import (
    CONF_ACCESS_TOKEN,
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_PARCELS,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    CONF_USERNAME,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from custom_components.canada_post.contents import CONTENTS_STORE_MAX_CODES
from custom_components.canada_post.services import async_setup_services
from custom_components.canada_post.tracking.coordinator import CanadaPostCoordinator

from .payloads import ACTIVE_CODE, DELIVERED_CODE, OTHER_CODE, delivered_detail, detail

HUB_CODE = OTHER_CODE
ACCOUNT_ACTIVE_CODE = ACTIVE_CODE
ACCOUNT_DELIVERED_CODE = DELIVERED_CODE
UNTRACKED = "888888888888"
FETCH = (
    "custom_components.canada_post.tracking.api.CanadaPostTrackingClient"
    ".async_get_parcel"
)
LIST = (
    "custom_components.canada_post.account.client.CanadaPostAccountClient"
    ".async_list_items"
)
STORE_KEY = f"{DOMAIN}.contents.%s"


def _payload(code):
    return delivered_detail(code) if code == DELIVERED_CODE else detail(code)


def _items():
    return [
        {"trackId": code, "source": "USER", "type": "DEFAULT", "userDescription": None}
        for code in (ACCOUNT_ACTIVE_CODE, ACCOUNT_DELIVERED_CODE)
    ]


@contextmanager
def _polls():
    with (
        patch(FETCH, new=AsyncMock(side_effect=_payload)),
        patch(LIST, new=AsyncMock(return_value=_items())),
    ):
        yield


async def _setup(hass, codes=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: [{CONF_TRACKING_CODE: code} for code in codes or []]},
    )
    entry.add_to_hass(hass)
    with _polls():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _setup_account(hass, login="someone@example.com"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"account:{login}",
        title=login,
        data={
            CONF_SOURCE: SOURCE_ACCOUNT,
            CONF_USERNAME: login,
            CONF_ACCESS_TOKEN: "access-1",
            CONF_REFRESH_TOKEN: "refresh-1",
        },
        options={
            CONF_DELIVERED_FILTER_TYPE: "parcels",
            CONF_DELIVERED_FILTER_AMOUNT: 5,
        },
    )
    entry.add_to_hass(hass)
    with _polls():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _call(hass, service, data):
    with _polls():
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
        await hass.async_block_till_done()
        # A refresh requested inside the debouncer's cooldown runs after it.
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
        await hass.async_block_till_done()


def _contents(entry, code):
    coordinator = entry.runtime_data.coordinator
    parcels = {p["barcode"]: p for p in [*coordinator.data, *coordinator.delivered]}
    return parcels[code]["contents"]


def _store(entry):
    return entry.runtime_data.coordinator.contents_store


@contextmanager
def _no_poll():
    """Fail the test when a contents change reaches the carrier or refreshes."""
    with (
        patch.object(DataUpdateCoordinator, "async_request_refresh", AsyncMock()) as r,
        patch(FETCH, new=AsyncMock(side_effect=AssertionError("fetched"))) as f,
        patch(LIST, new=AsyncMock(side_effect=AssertionError("listed"))) as li,
    ):
        yield
    assert r.await_count == 0
    assert f.await_count == 0
    assert li.await_count == 0


async def test_a_parcel_has_no_contents_without_a_stored_value(hass):
    entry = await _setup(hass, [HUB_CODE])
    assert _contents(entry, HUB_CODE) is None


async def test_update_contents_sets_replaces_and_clears(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    listener = []
    coordinator.async_add_listener(lambda: listener.append(1))

    with _no_poll():
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: HUB_CODE, "contents": " Running  shoes "},
        )
        assert _contents(entry, HUB_CODE) == "Running shoes"
        assert len(listener) == 1

        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: HUB_CODE, "contents": "Winter socks"},
        )
        assert _contents(entry, HUB_CODE) == "Winter socks"
        assert len(listener) == 2

        await _call(
            hass, "update_contents", {CONF_TRACKING_CODE: HUB_CODE, "contents": "  "}
        )
        assert _contents(entry, HUB_CODE) is None
        assert len(listener) == 3


async def test_update_contents_without_text_clears(hass):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )
    await _call(hass, "update_contents", {CONF_TRACKING_CODE: HUB_CODE})
    assert _contents(entry, HUB_CODE) is None


async def test_update_contents_normalizes_the_code(hass):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: f" {HUB_CODE.lower()} ", "contents": "Running shoes"},
    )
    assert _store(entry).get(HUB_CODE) == "Running shoes"


async def test_update_contents_works_for_a_code_that_is_not_tracked(hass):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )
    assert _store(entry).get(UNTRACKED) == "Running shoes"
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: HUB_CODE}]


async def test_a_contents_change_notifies_once_and_an_unchanged_one_never(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    data = {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"}
    with (
        _no_poll(),
        patch.object(
            CanadaPostCoordinator,
            "async_update_listeners",
            wraps=coordinator.async_update_listeners,
        ) as notify,
    ):
        await _call(hass, "update_contents", data)
        assert notify.call_count == 1
        await _call(hass, "update_contents", data)
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: UNTRACKED})
        assert notify.call_count == 1


async def test_an_unchanged_value_writes_nothing(hass):
    entry = await _setup(hass, [HUB_CODE])
    store = _store(entry)
    with patch.object(store._store, "async_delay_save") as save:
        assert store.async_set(HUB_CODE, "Running shoes") is True
        assert save.call_count == 1
        assert store.async_set(HUB_CODE, " Running shoes ") is False
        assert store.async_set(UNTRACKED, None) is False
        assert save.call_count == 1


async def test_the_store_drops_the_oldest_code_at_201(hass):
    entry = await _setup(hass, [HUB_CODE])
    store = _store(entry)
    for number in range(CONTENTS_STORE_MAX_CODES + 1):
        store.async_set(f"CODE{number}", f"Item {number}")
    assert store.get("CODE0") is None
    assert store.get("CODE1") == "Item 1"
    assert store.get(f"CODE{CONTENTS_STORE_MAX_CODES}") == (
        f"Item {CONTENTS_STORE_MAX_CODES}"
    )


async def test_update_contents_rejects_an_empty_code(hass):
    await _setup(hass)
    with pytest.raises(ServiceValidationError):
        await _call(hass, "update_contents", {CONF_TRACKING_CODE: " "})


async def test_update_contents_needs_a_loaded_entry(hass):
    async_setup_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "update_contents", {CONF_TRACKING_CODE: HUB_CODE}, blocking=True
        )


async def test_update_contents_ignores_an_entry_that_is_not_loaded(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
        )


async def test_track_parcel_stores_contents_for_a_new_code_and_still_refreshes(hass):
    entry = await _setup(hass)
    with (
        patch.object(
            CanadaPostCoordinator,
            "async_request_refresh",
            AsyncMock(),
        ) as refresh,
        _polls(),
    ):
        await hass.services.async_call(
            DOMAIN,
            "track_parcel",
            {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
            blocking=True,
        )
        await hass.async_block_till_done()
    assert refresh.await_count == 1
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: HUB_CODE}]
    assert _store(entry).get(HUB_CODE) == "Running shoes"

    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: HUB_CODE})
    await _call(
        hass,
        "track_parcel",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )
    assert _contents(entry, HUB_CODE) == "Running shoes"


async def test_track_parcel_without_contents_is_unchanged(hass):
    entry = await _setup(hass)
    await _call(hass, "track_parcel", {CONF_TRACKING_CODE: HUB_CODE})
    assert _store(entry).get(HUB_CODE) is None
    assert _contents(entry, HUB_CODE) is None


async def test_track_parcel_replaces_and_clears_contents_without_polling(hass):
    entry = await _setup(hass, [HUB_CODE])
    track = {CONF_TRACKING_CODE: HUB_CODE}
    with _no_poll():
        await _call(hass, "track_parcel", {**track, "contents": "Running shoes"})
        assert _contents(entry, HUB_CODE) == "Running shoes"
        await _call(hass, "track_parcel", {**track, "contents": "Winter socks"})
        assert _contents(entry, HUB_CODE) == "Winter socks"
        await _call(hass, "track_parcel", {**track, "contents": ""})
        assert _contents(entry, HUB_CODE) is None
    assert entry.options[CONF_PARCELS] == [{CONF_TRACKING_CODE: HUB_CODE}]


async def test_track_parcel_unchanged_contents_notifies_nobody(hass):
    entry = await _setup(hass, [HUB_CODE])
    data = {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"}
    coordinator = entry.runtime_data.coordinator
    with patch.object(
        CanadaPostCoordinator,
        "async_update_listeners",
        wraps=coordinator.async_update_listeners,
    ) as notify:
        await _call(hass, "track_parcel", data)
        await _call(hass, "track_parcel", data)
    assert notify.call_count == 1


async def test_track_parcel_with_contents_needs_a_loaded_hub(hass):
    entry = await _setup(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "track_parcel",
            {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
        )


async def test_untrack_parcel_drops_the_contents(hass):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )
    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: HUB_CODE})
    assert _store(entry).get(HUB_CODE) is None
    assert entry.options[CONF_PARCELS] == []


async def test_untrack_parcel_skips_a_hub_that_is_not_loaded(hass):
    entry = await _setup(hass, [HUB_CODE])
    entry.mock_state(hass, ConfigEntryState.SETUP_RETRY)
    await _call(hass, "untrack_parcel", {CONF_TRACKING_CODE: HUB_CODE})
    assert entry.options[CONF_PARCELS] == []


async def test_a_stored_value_survives_a_restart_of_the_entry(hass, hass_storage):
    entry = await _setup(hass, [HUB_CODE])
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )
    await _store(entry)._store.async_save(_store(entry)._data_to_save())
    assert await hass.config_entries.async_unload(entry.entry_id)
    with _polls():
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert _contents(entry, HUB_CODE) == "Running shoes"


async def test_an_entry_without_a_store_file_sets_up_and_loads_empty(
    hass, hass_storage
):
    assert not [key for key in hass_storage if key.startswith(f"{DOMAIN}.contents.")]
    entry = await _setup(hass, [HUB_CODE])
    assert entry.state is ConfigEntryState.LOADED
    assert _contents(entry, HUB_CODE) is None


@pytest.mark.parametrize("kind", ["hub", "account"])
async def test_removing_the_entry_removes_its_store(hass, hass_storage, kind):
    if kind == "hub":
        entry, code = await _setup(hass, [HUB_CODE]), HUB_CODE
    else:
        entry, code = await _setup_account(hass), ACCOUNT_ACTIVE_CODE
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: code, "contents": "Running shoes"},
    )
    store = _store(entry)
    await store._store.async_save(store._data_to_save())
    key = STORE_KEY % entry.entry_id
    assert key in hass_storage

    with patch(
        "custom_components.canada_post.async_revoke", new=AsyncMock(return_value=None)
    ):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    assert key not in hass_storage


async def test_a_contents_change_alone_fires_no_event(hass):
    await _setup(hass, [HUB_CODE])
    events = []
    for name in ("registered", "status_changed", "delivered", "delivery_time_changed"):
        hass.bus.async_listen(f"{DOMAIN}_parcel_{name}", events.append)
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )
    assert events == []


# ---------------------------------------------------------------------------
# overlay and reapply on the coordinators
# ---------------------------------------------------------------------------


async def test_the_poll_overlays_the_stored_value_before_events(hass):
    entry = await _setup(hass, [HUB_CODE])
    coordinator = entry.runtime_data.coordinator
    coordinator.contents_store.async_set(HUB_CODE, "Running shoes")
    with _polls():
        data = await coordinator._async_update_data()
    assert data[0]["contents"] == "Running shoes"


async def test_account_overlay_covers_active_and_delivered(hass):
    account = await _setup_account(hass)
    coordinator = account.runtime_data.coordinator
    assert isinstance(coordinator, CanadaPostAccountCoordinator)
    coordinator.contents_store.async_set(ACCOUNT_ACTIVE_CODE, "Running shoes")
    coordinator.contents_store.async_set(ACCOUNT_DELIVERED_CODE, "Winter socks")
    with _polls():
        active = await coordinator._async_update_data()
    assert active[0]["contents"] == "Running shoes"
    assert coordinator.delivered[0]["contents"] == "Winter socks"


async def test_reapply_resets_then_applies_on_active_and_delivered_parcels(hass):
    account = await _setup_account(hass)
    coordinator = account.runtime_data.coordinator
    coordinator.contents_store.async_set(ACCOUNT_ACTIVE_CODE, "Running shoes")
    coordinator.contents_store.async_set(ACCOUNT_DELIVERED_CODE, "Winter socks")
    coordinator.async_reapply_contents()
    assert _contents(account, ACCOUNT_ACTIVE_CODE) == "Running shoes"
    assert _contents(account, ACCOUNT_DELIVERED_CODE) == "Winter socks"

    coordinator.contents_store.async_set(ACCOUNT_ACTIVE_CODE, None)
    coordinator.async_reapply_contents()
    assert _contents(account, ACCOUNT_ACTIVE_CODE) is None


async def test_known_codes_list_every_code_the_entry_holds(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)
    assert hub.runtime_data.coordinator.known_codes == {HUB_CODE}
    assert account.runtime_data.coordinator.known_codes == {
        ACCOUNT_ACTIVE_CODE,
        ACCOUNT_DELIVERED_CODE,
    }


async def test_account_entries_take_contents_too_without_polling(hass):
    account = await _setup_account(hass)
    with _no_poll():
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: ACCOUNT_ACTIVE_CODE, "contents": "Running shoes"},
        )
    assert _contents(account, ACCOUNT_ACTIVE_CODE) == "Running shoes"


# ---------------------------------------------------------------------------
# several entries
# ---------------------------------------------------------------------------


async def test_update_contents_goes_to_the_entry_that_lists_the_code(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: HUB_CODE, "contents": "Running shoes"},
    )
    assert _store(hub).get(HUB_CODE) == "Running shoes"
    assert _store(account).get(HUB_CODE) is None


async def test_update_contents_goes_to_every_entry_that_lists_the_code(hass):
    home = await _setup_account(hass, "home@example.com")
    work = await _setup_account(hass, "work@example.com")
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: ACCOUNT_ACTIVE_CODE, "contents": "Running shoes"},
    )
    assert _store(home).get(ACCOUNT_ACTIVE_CODE) == "Running shoes"
    assert _store(work).get(ACCOUNT_ACTIVE_CODE) == "Running shoes"


async def test_update_contents_with_several_entries_and_no_match_needs_an_entry(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)
    with pytest.raises(ServiceValidationError) as error:
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
        )
    assert error.value.translation_key == "update_contents_entry_required"
    assert _store(hub).get(UNTRACKED) is None
    assert _store(account).get(UNTRACKED) is None


async def test_config_entry_id_restricts_and_stores_an_unknown_code(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)
    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: UNTRACKED,
            "contents": "Running shoes",
            "config_entry_id": account.entry_id,
        },
    )
    await _call(
        hass,
        "update_contents",
        {
            CONF_TRACKING_CODE: HUB_CODE,
            "contents": "Winter socks",
            "config_entry_id": hub.entry_id,
        },
    )
    assert _store(account).get(UNTRACKED) == "Running shoes"
    assert _store(hub).get(UNTRACKED) is None
    assert _store(hub).get(HUB_CODE) == "Winter socks"
    assert _store(account).get(HUB_CODE) is None


async def test_config_entry_id_must_name_a_loaded_entry(hass):
    await _setup(hass, [HUB_CODE])
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {CONF_TRACKING_CODE: HUB_CODE, "config_entry_id": "missing"},
        )


async def test_an_unloaded_entry_is_never_targeted(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)
    assert await hass.config_entries.async_unload(account.entry_id)
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            "update_contents",
            {
                CONF_TRACKING_CODE: HUB_CODE,
                "contents": "Running shoes",
                "config_entry_id": account.entry_id,
            },
        )
    # With one entry left loaded, it is targeted directly.
    await _call(
        hass,
        "update_contents",
        {CONF_TRACKING_CODE: UNTRACKED, "contents": "Running shoes"},
    )
    assert _store(hub).get(UNTRACKED) == "Running shoes"


# ---------------------------------------------------------------------------
# service registration
# ---------------------------------------------------------------------------


async def test_an_account_alone_registers_only_update_contents(hass):
    account = await _setup_account(hass)
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")
    assert not hass.services.has_service(DOMAIN, "untrack_parcel")
    assert await hass.config_entries.async_unload(account.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_unloading_one_entry_keeps_the_services_the_others_need(hass):
    hub = await _setup(hass, [HUB_CODE])
    account = await _setup_account(hass)
    assert hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(hub.entry_id)
    assert hass.services.has_service(DOMAIN, "update_contents")
    assert not hass.services.has_service(DOMAIN, "track_parcel")

    assert await hass.config_entries.async_unload(account.entry_id)
    assert not hass.services.has_service(DOMAIN, "update_contents")


async def test_a_hub_after_an_account_still_registers_the_hub_services(hass):
    await _setup_account(hass)
    await _setup(hass, [HUB_CODE])
    assert hass.services.has_service(DOMAIN, "track_parcel")
    assert hass.services.has_service(DOMAIN, "untrack_parcel")
