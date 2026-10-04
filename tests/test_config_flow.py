"""Tests for the Canada Post config and options flow."""
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.canada_post.account.client import (
    CanadaPostAccountApiError,
    CanadaPostAccountInvalidCredentials,
    CanadaPostAccountTwoStepRequired,
)
from custom_components.canada_post.config_flow import (
    is_notice_card,
    normalize_tracking_code,
    valid_tracking_code,
)
from custom_components.canada_post.const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    CONF_ID_TOKEN,
    CONF_INCLUDE_HISTORY,
    CONF_PARCELS,
    CONF_PASSWORD,
    CONF_SOURCE,
    CONF_TRACKING_CODE,
    CONF_USERNAME,
    DOMAIN,
    SOURCE_ACCOUNT,
    SOURCE_TRACKING,
)
from custom_components.canada_post.tracking.api import CanadaPostApiError

ACCOUNT = "custom_components.canada_post.account.client.CanadaPostAccountClient"
DNC = "custom_components.canada_post.tracking.api.CanadaPostTrackingClient.async_resolve_dnc"
TOKENS = {"access_token": "a", "refresh_token": "r", "id_token": "i", "expires_at": 1.0}
PIN = "1234567890123456"


@pytest.mark.parametrize(
    "code",
    ["12345678901", "123456789012", "1234567890123456", "RN123456789CA",
     "123456789", "123456789012345"],
)
def test_valid_codes(code):
    assert valid_tracking_code(code)


@pytest.mark.parametrize(
    "code", ["", "ABC", "1234567890", "12345678901234", "R1123456789CA", "RN12345678CA", "1234567890123456A"]
)
def test_invalid_codes(code):
    assert not valid_tracking_code(code)


def test_normalize_strips_spaces_and_uppercases():
    assert normalize_tracking_code(" rn 123456789 ca ") == "RN123456789CA"
    assert normalize_tracking_code(None) == ""


def test_notice_card_shapes():
    assert is_notice_card("123456789") and is_notice_card("123456789012345")
    assert not is_notice_card(PIN)


async def test_user_flow_shows_source_menu(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "menu"
    assert result["menu_options"] == ["account", "tracking"]


async def test_tracking_hub_created_without_input(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tracking"}
    )
    assert result["type"] == "create_entry"
    assert result["title"] == "Canada Post"
    assert result["data"] == {CONF_SOURCE: SOURCE_TRACKING}
    assert result["options"][CONF_PARCELS] == []


async def test_only_one_tracking_hub(hass):
    MockConfigEntry(
        domain=DOMAIN, unique_id=SOURCE_TRACKING, data={CONF_SOURCE: SOURCE_TRACKING}
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tracking"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def _account_form(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "account"}
    )


async def test_account_flow_stores_tokens_never_the_password(hass):
    form = await _account_form(hass)
    with (
        patch(f"{ACCOUNT}.async_login", new=AsyncMock(return_value=TOKENS)) as login,
        patch(f"{ACCOUNT}.async_list_items", new=AsyncMock(return_value=[])) as listing,
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_USERNAME: " Me@Example.com ", CONF_PASSWORD: "pw"}
        )
    login.assert_awaited_once_with("me@example.com", "pw")
    listing.assert_awaited()
    assert result["type"] == "create_entry"
    assert result["title"] == "Canada Post (me@example.com)"
    assert result["result"].unique_id == "account:me@example.com"
    assert result["data"][CONF_ID_TOKEN] == "i"
    assert result["data"][CONF_SOURCE] == SOURCE_ACCOUNT
    assert "pw" not in str(result["data"])
    assert CONF_PASSWORD not in result["data"]


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (CanadaPostAccountInvalidCredentials("x"), "invalid_auth"),
        (CanadaPostAccountApiError("x"), "cannot_connect"),
    ],
)
async def test_account_flow_errors(hass, error, key):
    form = await _account_form(hass)
    with patch(f"{ACCOUNT}.async_login", new=AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_USERNAME: "me", CONF_PASSWORD: "pw"}
        )
    assert result["type"] == "form"
    assert result["errors"] == {"base": key}


async def test_account_flow_list_rejection_is_invalid_auth(hass):
    from custom_components.canada_post.account.client import (
        CanadaPostAccountReauthRequired,
    )

    form = await _account_form(hass)
    with (
        patch(f"{ACCOUNT}.async_login", new=AsyncMock(return_value=TOKENS)),
        patch(f"{ACCOUNT}.async_list_items", new=AsyncMock(side_effect=CanadaPostAccountReauthRequired("x"))),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_USERNAME: "me", CONF_PASSWORD: "pw"}
        )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_account_flow_blank_credentials(hass):
    form = await _account_form(hass)
    result = await hass.config_entries.flow.async_configure(
        form["flow_id"], {CONF_USERNAME: "  ", CONF_PASSWORD: ""}
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_account_flow_two_step_aborts(hass):
    form = await _account_form(hass)
    with patch(f"{ACCOUNT}.async_login", new=AsyncMock(side_effect=CanadaPostAccountTwoStepRequired("x"))):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_USERNAME: "me", CONF_PASSWORD: "pw"}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "two_step_not_supported"


async def test_duplicate_account_aborts_case_insensitively(hass):
    MockConfigEntry(
        domain=DOMAIN, unique_id="account:me@example.com",
        data={CONF_SOURCE: SOURCE_ACCOUNT, CONF_USERNAME: "me@example.com"},
    ).add_to_hass(hass)
    form = await _account_form(hass)
    with (
        patch(f"{ACCOUNT}.async_login", new=AsyncMock(return_value=TOKENS)),
        patch(f"{ACCOUNT}.async_list_items", new=AsyncMock(return_value=[])),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_USERNAME: "ME@EXAMPLE.COM", CONF_PASSWORD: "pw"}
        )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


def _account_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, unique_id="account:me@example.com",
        data={CONF_SOURCE: SOURCE_ACCOUNT, CONF_USERNAME: "me@example.com", **TOKENS},
    )


async def test_reauth_keeps_unique_id_and_replaces_tokens(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["username"] == "me@example.com"
    new = {**TOKENS, "id_token": "new"}
    with (
        patch(f"{ACCOUNT}.async_login", new=AsyncMock(return_value=new)) as login,
        patch.object(hass.config_entries, "async_schedule_reload"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "pw2"}
        )
    login.assert_awaited_once_with("me@example.com", "pw2")
    assert result["type"] == "abort" and result["reason"] == "reauth_successful"
    assert entry.data[CONF_ID_TOKEN] == "new"
    assert entry.unique_id == "account:me@example.com"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CanadaPostAccountInvalidCredentials("x"), "invalid_auth"),
        (CanadaPostAccountApiError("x"), "cannot_connect"),
    ],
)
async def test_reauth_errors(hass, error, expected):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(f"{ACCOUNT}.async_login", new=AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "x"}
        )
    assert result["errors"] == {"base": expected}


async def test_reauth_two_step_aborts(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(f"{ACCOUNT}.async_login", new=AsyncMock(side_effect=CanadaPostAccountTwoStepRequired("x"))):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "x"}
        )
    assert result["reason"] == "two_step_not_supported"


def _hub(parcels: list[dict]) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=SOURCE_TRACKING,
        data={CONF_SOURCE: SOURCE_TRACKING},
        options={CONF_PARCELS: parcels},
    )


def _settings_input(*, history=False, filter_type="days", amount=7) -> dict:
    return {
        CONF_DELIVERED_FILTER_TYPE: filter_type,
        CONF_DELIVERED_FILTER_AMOUNT: amount,
        CONF_INCLUDE_HISTORY: history,
    }


async def _open_options_step(hass, entry, step_id: str):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    assert result["menu_options"] == ["parcels", "settings"]
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step_id}
    )


async def test_account_options_menu_is_settings_only(hass):
    entry = _account_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["settings"]


async def _submit_codes(hass, entry, codes):
    result = await _open_options_step(hass, entry, "parcels")
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"tracking_codes": codes}
    )


async def test_options_add_pin_keeps_other_options(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _submit_codes(hass, entry, [" rn123456789ca"])
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: "RN123456789CA"}]


async def test_options_notice_card_is_resolved_to_pin(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    with patch(DNC, new=AsyncMock(return_value=PIN)) as resolve:
        result = await _submit_codes(hass, entry, ["123456789012345", PIN])
    resolve.assert_awaited_once_with("123456789012345")
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: PIN}]


async def test_options_unresolved_notice_card_is_not_found(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    with patch(DNC, new=AsyncMock(return_value=None)):
        result = await _submit_codes(hass, entry, ["123456789"])
    assert result["type"] == "form" and result["errors"] == {"base": "not_found"}


@pytest.mark.parametrize("error", [CanadaPostApiError("x"), aiohttp.ClientError()])
async def test_options_lookup_failure_is_cannot_connect(hass, error):
    entry = _hub([])
    entry.add_to_hass(hass)
    with patch(DNC, new=AsyncMock(side_effect=error)):
        result = await _submit_codes(hass, entry, ["123456789"])
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_rejects_malformed_code(hass):
    entry = _hub([])
    entry.add_to_hass(hass)
    result = await _submit_codes(hass, entry, ["abc"])
    assert result["errors"] == {"base": "invalid_tracking_code"}


async def test_options_de_duplicates_and_removes(hass):
    entry = _hub([{CONF_TRACKING_CODE: PIN}, {CONF_TRACKING_CODE: "RN123456789CA"}])
    entry.add_to_hass(hass)
    result = await _submit_codes(hass, entry, [PIN, PIN.lower()])
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: PIN}]


async def test_options_can_clear_the_list(hass):
    entry = _hub([{CONF_TRACKING_CODE: PIN}])
    entry.add_to_hass(hass)
    result = await _submit_codes(hass, entry, [])
    assert result["data"][CONF_PARCELS] == []


async def test_options_settings_keep_parcels(hass):
    entry = _hub([{CONF_TRACKING_CODE: PIN}])
    entry.add_to_hass(hass)
    result = await _open_options_step(hass, entry, "settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _settings_input(history=True, filter_type="parcels", amount=5)
    )
    assert result["data"][CONF_INCLUDE_HISTORY] is True
    assert result["data"][CONF_DELIVERED_FILTER_TYPE] == "parcels"
    assert result["data"][CONF_DELIVERED_FILTER_AMOUNT] == 5
    assert result["data"][CONF_PARCELS] == [{CONF_TRACKING_CODE: PIN}]
