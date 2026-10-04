"""Tests for the Canada Post detail transport."""
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.canada_post.const import ALIAS_URL, DETAIL_HEADERS, DETAIL_URL
from custom_components.canada_post.tracking import api as api_module
from custom_components.canada_post.tracking.api import (
    CanadaPostApiError,
    CanadaPostTrackingClient,
)

from ..payloads import ACTIVE_CODE, detail, not_found


def _session_returning(status: int, body: object = None, headers=None) -> MagicMock:
    response = AsyncMock()
    response.status = status
    response.headers = headers or {}
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


@pytest.fixture(autouse=True)
def _reset():
    api_module._envelope_warned.clear()


@pytest.mark.parametrize("status", [200, 206])
async def test_get_parcel_success_on_200_and_206(status):
    session = _session_returning(status, detail())
    parcel = await CanadaPostTrackingClient(session).async_get_parcel(ACTIVE_CODE)
    assert parcel["pin"] == ACTIVE_CODE
    url = session.get.call_args[0][0]
    assert url == DETAIL_URL.format(pin=ACTIVE_CODE)


async def test_sends_exactly_the_three_load_bearing_headers():
    session = _session_returning(200, detail())
    await CanadaPostTrackingClient(session).async_get_parcel(ACTIVE_CODE)
    headers = session.get.call_args.kwargs["headers"]
    assert headers == DETAIL_HEADERS
    assert headers["Authorization"] == "Basic Og=="
    assert headers["Referer"].startswith("https://www.canadapost-postescanada.ca/")
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert headers["Accept"] == "application/vnd.cpc.trackweb-v1+json"


async def test_not_found_004_is_none_without_warning(caplog):
    client = CanadaPostTrackingClient(_session_returning(206, not_found()))
    with caplog.at_level(logging.WARNING):
        assert await client.async_get_parcel(ACTIVE_CODE) is None
    assert not caplog.records


async def test_other_error_code_is_none_with_one_shot_warning(caplog):
    body = {"pin": ACTIVE_CODE, "error": {"cd": "999"}}
    client = CanadaPostTrackingClient(_session_returning(200, body))
    with caplog.at_level(logging.WARNING):
        assert await client.async_get_parcel(ACTIVE_CODE) is None
        assert await client.async_get_parcel(ACTIVE_CODE) is None
    assert len(caplog.records) == 1
    assert "issues/new?template=unrecognised_status.yml" in caplog.text


async def test_non_dict_error_is_none_with_warning(caplog):
    client = CanadaPostTrackingClient(_session_returning(200, {"error": "x"}))
    with caplog.at_level(logging.WARNING):
        assert await client.async_get_parcel(ACTIVE_CODE) is None
    assert len(caplog.records) == 1


async def test_body_without_pin_is_none_with_warning(caplog):
    client = CanadaPostTrackingClient(_session_returning(200, {"status": "x"}))
    with caplog.at_level(logging.WARNING):
        assert await client.async_get_parcel(ACTIVE_CODE) is None
    assert "no pin in body" in caplog.text


async def test_403_html_is_an_error_not_not_found():
    client = CanadaPostTrackingClient(_session_returning(403, "<html>"))
    with pytest.raises(CanadaPostApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.status_code == 403


async def test_unparseable_body_raises():
    client = CanadaPostTrackingClient(_session_returning(200, "not json"))
    with pytest.raises(CanadaPostApiError):
        await client.async_get_parcel(ACTIVE_CODE)


async def test_non_object_body_raises():
    client = CanadaPostTrackingClient(_session_returning(200, ["a"]))
    with pytest.raises(CanadaPostApiError):
        await client.async_get_parcel(ACTIVE_CODE)


@pytest.mark.parametrize(
    ("header", "expected"), [("120", 120.0), ("Wed, 21 Oct", None), (None, None)]
)
async def test_429_carries_retry_after(header, expected):
    headers = {"Retry-After": header} if header else {}
    client = CanadaPostTrackingClient(_session_returning(429, {}, headers))
    with pytest.raises(CanadaPostApiError) as err:
        await client.async_get_parcel(ACTIVE_CODE)
    assert err.value.status_code == 429
    assert err.value.retry_after == expected


async def test_network_error_propagates():
    session = MagicMock()
    session.get = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await CanadaPostTrackingClient(session).async_get_parcel(ACTIVE_CODE)


async def test_resolve_dnc_single_item_returns_pin():
    session = _session_returning(200, [{"pin": ACTIVE_CODE}])
    pin = await CanadaPostTrackingClient(session).async_resolve_dnc("123456789012345")
    assert pin == ACTIVE_CODE
    assert session.get.call_args.kwargs["params"] == {"dncs": "123456789012345"}
    assert session.get.call_args[0][0] == ALIAS_URL


@pytest.mark.parametrize(
    "body", [[], [{"pin": "1"}, {"pin": "2"}], {"error": {}}, ["x"], [{"pin": ""}]]
)
async def test_resolve_dnc_zero_several_or_odd_is_none(body):
    client = CanadaPostTrackingClient(_session_returning(200, body))
    assert await client.async_resolve_dnc("123456789012345") is None
