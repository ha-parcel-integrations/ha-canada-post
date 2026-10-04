"""Tests for the account client: sign-in, refresh and the saved-list read."""
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.canada_post.account.client import (
    CanadaPostAccountApiError,
    CanadaPostAccountClient,
    CanadaPostAccountInvalidCredentials,
    CanadaPostAccountReauthRequired,
    CanadaPostAccountTwoStepRequired,
)
from custom_components.canada_post.const import (
    ACCOUNT_CLIENT_ID,
    ACCOUNT_GRAPHQL_URL,
    ACCOUNT_TOKEN_URL,
)

LOGIN_OK = {"access_token": "a1", "refresh_token": "r1", "id_token": "i1", "expires_in": 3600}


class FakeSession:
    """Queue-driven stand-in for ``aiohttp.ClientSession.post``."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        status, body = reply
        response = AsyncMock()
        response.status = status
        if isinstance(body, str):
            response.json = AsyncMock(side_effect=ValueError("bad json"))
        else:
            response.json = AsyncMock(return_value=body)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx


def _page(items, token=None):
    return (200, {"data": {"listTrackSyncItems": {"items": items, "nextToken": token}}})


def _item(track_id="123456789012", **extra):
    return {"id": "x", "trackId": track_id, "deleted": False, "isFlaggedAsNotMine": False, **extra}


def _client(session, **kwargs):
    return CanadaPostAccountClient(
        session,
        access_token="a0", refresh_token="r0", id_token="i0",
        **kwargs,
    )


async def test_login_posts_form_and_returns_tokens_not_password():
    session = FakeSession((200, LOGIN_OK))
    client = CanadaPostAccountClient(session)
    tokens = await client.async_login("me", "pw")
    call = session.calls[0]
    assert call["url"] == ACCOUNT_TOKEN_URL
    assert call["data"]["grant_type"] == "password"
    assert call["data"]["client_id"] == ACCOUNT_CLIENT_ID
    assert tokens["id_token"] == "i1" and tokens["refresh_token"] == "r1"
    assert tokens["expires_at"] > time.time()
    assert "pw" not in str(tokens) and "password" not in tokens


async def test_login_without_expiry_leaves_expires_at_none():
    session = FakeSession((200, {k: v for k, v in LOGIN_OK.items() if k != "expires_in"}))
    tokens = await CanadaPostAccountClient(session).async_login("me", "pw")
    assert tokens["expires_at"] is None


async def test_login_bad_credentials_mapping_error():
    body = {"error": "mapping_error", "error_description": "Invalid username or password. LFE Authentication failed."}
    with pytest.raises(CanadaPostAccountInvalidCredentials):
        await CanadaPostAccountClient(FakeSession((400, body))).async_login("me", "pw")


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"error": "x", "error_description": "Two-step verification required"}),
        (200, {"verification_method": "SMS"}),
        (401, {"error": "mfa_required"}),
    ],
)
async def test_login_second_factor_is_distinct(status, body):
    with pytest.raises(CanadaPostAccountTwoStepRequired):
        await CanadaPostAccountClient(FakeSession((status, body))).async_login("me", "pw")


@pytest.mark.parametrize(
    "reply", [(500, {}), (200, "junk"), (200, {"access_token": "a"}), (404, None)]
)
async def test_login_other_failures_are_api_errors(reply):
    with pytest.raises(CanadaPostAccountApiError) as err:
        await CanadaPostAccountClient(FakeSession(reply)).async_login("me", "pw")
    assert not isinstance(err.value, CanadaPostAccountInvalidCredentials)


async def test_network_errors_are_wrapped_without_the_host():
    session = FakeSession(aiohttp.ClientError("cannot reach sso-osu.example"))
    with pytest.raises(CanadaPostAccountApiError) as err:
        await CanadaPostAccountClient(session).async_login("me", "pw")
    assert "sso-osu" not in str(err.value) and err.value.__cause__ is None
    session = FakeSession(TimeoutError())
    with pytest.raises(CanadaPostAccountApiError):
        await _client(session).async_list_items()


async def test_confirmed_token_responses_log_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        client = CanadaPostAccountClient(FakeSession((200, LOGIN_OK), (200, LOGIN_OK)))
        await client.async_login("me", "pw")
        client._refresh_token = "r0"
        await client.async_refresh()
    assert caplog.text == ""


async def test_refresh_rotates_and_notifies_callback():
    callback = AsyncMock()
    session = FakeSession((200, {**LOGIN_OK, "id_token": "i2", "refresh_token": "r2"}))
    client = _client(session, token_callback=callback)
    await client.async_refresh()
    assert session.calls[0]["data"]["grant_type"] == "refresh_token"
    assert session.calls[0]["data"]["refresh_token"] == "r0"
    stored = callback.await_args.args[0]
    assert stored["id_token"] == "i2" and stored["refresh_token"] == "r2"


async def test_refresh_keeps_old_refresh_token_when_not_rotated():
    body = {"access_token": "a2", "id_token": "i2"}
    client = _client(FakeSession((200, body)))
    tokens = await client.async_refresh()
    assert tokens["refresh_token"] == "r0"


async def test_refresh_without_callback_is_fine():
    await _client(FakeSession((200, LOGIN_OK))).async_refresh()


@pytest.mark.parametrize(
    "reply",
    [(400, {"error": "invalid_grant"}), (401, {"error": "x"}), (200, {"access_token": "a"}), (200, [])],
)
async def test_rejected_refresh_requires_reauth(reply):
    with pytest.raises(CanadaPostAccountReauthRequired):
        await _client(FakeSession(reply)).async_refresh()


@pytest.mark.parametrize("reply", [(503, {}), (502, "junk"), (400, "junk"), (429, {"error": "rate"})])
async def test_refresh_outage_is_not_reauth(reply):
    with pytest.raises(CanadaPostAccountApiError) as err:
        await _client(FakeSession(reply)).async_refresh()
    assert not isinstance(err.value, CanadaPostAccountReauthRequired)


async def test_refresh_without_token_requires_reauth():
    with pytest.raises(CanadaPostAccountReauthRequired):
        await CanadaPostAccountClient(FakeSession()).async_refresh()


async def test_list_sends_raw_id_token_and_skips_deleted_and_not_mine():
    items = [
        _item("111111111111"),
        _item("222222222222", deleted=True),
        _item("333333333333", isFlaggedAsNotMine=True),
    ]
    session = FakeSession(_page(items))
    result = await _client(session).async_list_items()
    assert [i["trackId"] for i in result] == ["111111111111"]
    call = session.calls[0]
    assert call["url"] == ACCOUNT_GRAPHQL_URL
    assert call["headers"] == {"Authorization": "i0"}
    assert "createTrackSyncItem" not in str(call) and "mutation" not in str(call)
    assert call["json"]["variables"] == {"n": None}


async def test_list_follows_next_token_until_null():
    session = FakeSession(_page([_item("111111111111")], "t1"), _page([_item("222222222222")], "t2"), _page([_item("333333333333")]))
    result = await _client(session).async_list_items()
    assert [i["trackId"] for i in result] == ["111111111111", "222222222222", "333333333333"]
    assert [c["json"]["variables"]["n"] for c in session.calls] == [None, "t1", "t2"]


async def test_list_pagination_loop_is_an_error():
    session = FakeSession(_page([], "t1"), _page([], "t1"))
    with pytest.raises(CanadaPostAccountApiError):
        await _client(session).async_list_items()


async def test_list_odd_items_are_skipped_with_one_warning(caplog):
    session = FakeSession(_page(["junk", {"id": "x"}, {"trackId": 5}, _item()]))
    with caplog.at_level(logging.WARNING):
        result = await _client(session).async_list_items()
    assert len(result) == 1
    assert caplog.text.count("unexpected shape") == 1
    assert "keys=" in caplog.text


async def test_list_items_not_a_list_is_empty():
    session = FakeSession((200, {"data": {"listTrackSyncItems": {"items": None, "nextToken": None}}}))
    assert await _client(session).async_list_items() == []


@pytest.mark.parametrize(
    "first",
    [
        (401, {"errors": [{"errorType": "UnauthorizedException"}]}),
        (200, {"errors": [{"errorType": "Unauthorized"}], "data": None}),
        (401, "junk"),
    ],
)
async def test_unauthorized_refreshes_once_and_retries(first):
    callback = AsyncMock()
    session = FakeSession(first, (200, {**LOGIN_OK, "id_token": "i2"}), _page([_item()]))
    client = _client(session, token_callback=callback)
    assert len(await client.async_list_items()) == 1
    assert session.calls[2]["headers"] == {"Authorization": "i2"}
    callback.assert_awaited_once()


async def test_second_unauthorized_requires_reauth():
    denied = (401, {"errors": [{"errorType": "UnauthorizedException"}]})
    session = FakeSession(denied, (200, LOGIN_OK), denied)
    with pytest.raises(CanadaPostAccountReauthRequired):
        await _client(session).async_list_items()


async def test_rejected_refresh_during_list_requires_reauth():
    denied = (401, {"errors": [{"errorType": "UnauthorizedException"}]})
    session = FakeSession(denied, (400, {"error": "invalid_grant"}))
    with pytest.raises(CanadaPostAccountReauthRequired):
        await _client(session).async_list_items()


async def test_expired_token_is_refreshed_proactively():
    session = FakeSession((200, {**LOGIN_OK, "id_token": "i2"}), _page([]))
    client = _client(session, expires_at=time.time() + 10)
    await client.async_list_items()
    assert session.calls[0]["url"] == ACCOUNT_TOKEN_URL
    assert session.calls[1]["headers"] == {"Authorization": "i2"}


async def test_fresh_token_is_not_refreshed():
    session = FakeSession(_page([]))
    await _client(session, expires_at=time.time() + 3600).async_list_items()
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "reply",
    [(500, {"data": None}), (200, {"data": {"listTrackSyncItems": None}, "errors": [{"errorType": "Other"}]}), (200, "junk")],
)
async def test_other_list_failures_are_api_errors(reply):
    with pytest.raises(CanadaPostAccountApiError) as err:
        await _client(FakeSession(reply)).async_list_items()
    assert not isinstance(err.value, CanadaPostAccountReauthRequired)
