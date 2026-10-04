"""Client for the Canada Post account's saved tracking list.

Read-only: it signs in, refreshes its own tokens and lists the account's
tracked PINs. Parcel detail is fetched elsewhere, through the tracking client.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from ..const import (
    ACCOUNT_CLIENT_ID,
    ACCOUNT_GRAPHQL_URL,
    ACCOUNT_SCOPE,
    ACCOUNT_TOKEN_REFRESH_MARGIN_SECONDS,
    ACCOUNT_TOKEN_URL,
    CONF_ACCESS_TOKEN,
    CONF_EXPIRES_AT,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
)
from ..status import NEW_ISSUE_URL

_LOGGER = logging.getLogger(__name__)

TokenCallback = Callable[[dict[str, Any]], Awaitable[None]]

LIST_QUERY = (
    "query($n:String){listTrackSyncItems(limit:300,nextToken:$n){items{id trackId "
    "userDescription type source deleted isFlaggedAsNotMine createdAt updatedAt}"
    "nextToken}}"
)

_UNAUTHORIZED = {"UnauthorizedException", "Unauthorized"}
_SECOND_FACTOR = re.compile(
    r"(?<![a-z0-9])(verification|two[-_ ]?step|2sv|mfa|otp|security[-_ ]?question)(?![a-z0-9])"
)

_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _warned:
        return
    _warned.add(key)
    _LOGGER.warning(message, *args)


class CanadaPostAccountApiError(Exception):
    """An unexpected account response, without any response data attached."""

    def __init__(self, detail: str, *, status_code: int | None = None) -> None:
        """Store safe failure metadata."""
        super().__init__(detail)
        self.status_code = status_code


class CanadaPostAccountInvalidCredentials(CanadaPostAccountApiError):
    """The supplied username/password was rejected."""


class CanadaPostAccountTwoStepRequired(CanadaPostAccountApiError):
    """The account answered with a second-factor challenge."""


class CanadaPostAccountReauthRequired(CanadaPostAccountApiError):
    """Stored tokens cannot be refreshed."""


def _looks_like_second_factor(payload: Any) -> bool:
    text = json.dumps(payload, default=str).lower() if payload is not None else ""
    return _SECOND_FACTOR.search(text) is not None


class CanadaPostAccountClient:
    """Account client with an id-token authorised list call."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        access_token: str | None = None,
        refresh_token: str | None = None,
        id_token: str | None = None,
        expires_at: float | None = None,
        token_callback: TokenCallback | None = None,
    ) -> None:
        """Initialise the client with entry-owned tokens and a persistence hook."""
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._id_token = id_token
        self._expires_at = expires_at
        self._token_callback = token_callback

    def _tokens(self) -> dict[str, Any]:
        return {
            CONF_ACCESS_TOKEN: self._access_token,
            CONF_REFRESH_TOKEN: self._refresh_token,
            CONF_ID_TOKEN: self._id_token,
            CONF_EXPIRES_AT: self._expires_at,
        }

    async def _token_request(self, form: dict[str, str]) -> tuple[int, Any]:
        try:
            async with self._session.post(
                ACCOUNT_TOKEN_URL,
                data={**form, "client_id": ACCOUNT_CLIENT_ID, "scope": ACCOUNT_SCOPE},
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except ValueError:
                    body = None
                return response.status, body
        except (aiohttp.ClientError, TimeoutError):
            raise CanadaPostAccountApiError("sign-in service unreachable") from None

    def _adopt(self, payload: dict[str, Any]) -> None:
        """Store a token response; absent optional fields keep their old value."""
        self._access_token = payload["access_token"]
        self._id_token = payload["id_token"]
        self._refresh_token = payload.get("refresh_token") or self._refresh_token
        try:
            self._expires_at = time.time() + float(payload.get("expires_in"))
        except (TypeError, ValueError):
            self._expires_at = None

    async def async_login(self, username: str, password: str) -> dict[str, Any]:
        """Authenticate once; callers persist only the returned tokens."""
        status, body = await self._token_request(
            {"grant_type": "password", "username": username, "password": password}
        )
        if isinstance(body, dict) and body.get("access_token") and body.get("id_token"):
            self._adopt(body)
            return self._tokens()
        if _looks_like_second_factor(body):
            raise CanadaPostAccountTwoStepRequired("second factor required")
        if status in (400, 401, 403) and isinstance(body, dict) and body.get("error"):
            raise CanadaPostAccountInvalidCredentials(
                "sign-in rejected", status_code=status
            )
        raise CanadaPostAccountApiError("sign-in request failed", status_code=status)

    async def async_refresh(self) -> dict[str, Any]:
        """Rotate the tokens once and notify the entry owner."""
        if not self._refresh_token:
            raise CanadaPostAccountReauthRequired("no refresh token")
        status, body = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
        )
        if status >= 500 or status == 429 or (status != 200 and not isinstance(body, dict)):
            raise CanadaPostAccountApiError("refresh request failed", status_code=status)
        if status != 200 or not isinstance(body, dict):
            raise CanadaPostAccountReauthRequired("refresh rejected", status_code=status)
        if not body.get("access_token") or not body.get("id_token"):
            raise CanadaPostAccountReauthRequired("refresh returned no tokens")
        self._adopt(body)
        if self._token_callback:
            await self._token_callback(self._tokens())
        return self._tokens()

    async def _graphql(self, variables: dict[str, Any]) -> Any:
        try:
            async with self._session.post(
                ACCOUNT_GRAPHQL_URL,
                json={"query": LIST_QUERY, "variables": variables},
                headers={"Authorization": self._id_token or ""},
            ) as response:
                try:
                    body = await response.json(content_type=None)
                except ValueError:
                    body = None
                return response.status, body
        except (aiohttp.ClientError, TimeoutError):
            raise CanadaPostAccountApiError("list service unreachable") from None

    @staticmethod
    def _unauthorized(status: int, body: Any) -> bool:
        if status == 401:
            return True
        errors = body.get("errors") if isinstance(body, dict) else None
        return isinstance(errors, list) and any(
            isinstance(error, dict) and error.get("errorType") in _UNAUTHORIZED
            for error in errors
        )

    async def _page(self, next_token: str | None) -> dict[str, Any]:
        if self._expires_at is not None and (
            time.time() >= self._expires_at - ACCOUNT_TOKEN_REFRESH_MARGIN_SECONDS
        ):
            await self.async_refresh()
        variables = {"n": next_token}
        status, body = await self._graphql(variables)
        if self._unauthorized(status, body):
            await self.async_refresh()
            status, body = await self._graphql(variables)
            if self._unauthorized(status, body):
                raise CanadaPostAccountReauthRequired(
                    "token rejected after refresh", status_code=status
                )
        data = body.get("data") if isinstance(body, dict) else None
        page = data.get("listTrackSyncItems") if isinstance(data, dict) else None
        if status != 200 or not isinstance(page, dict):
            raise CanadaPostAccountApiError("list request failed", status_code=status)
        return page

    async def async_list_items(self) -> list[dict[str, Any]]:
        """Return the account's live, own tracked items (all pages)."""
        items: list[dict[str, Any]] = []
        next_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            page = await self._page(next_token)
            raw_items = page.get("items")
            for item in raw_items if isinstance(raw_items, list) else []:
                if not isinstance(item, dict) or not isinstance(
                    item.get("trackId"), str
                ):
                    _warn_once(
                        "item_shape",
                        "Canada Post list item has an unexpected shape — help us "
                        "confirm it. Open an issue and paste this line: %s\n  "
                        "keys=%s",
                        NEW_ISSUE_URL,
                        sorted(item) if isinstance(item, dict) else type(item).__name__,
                    )
                    continue
                if item.get("deleted") is True or item.get("isFlaggedAsNotMine") is True:
                    continue
                items.append(item)
            next_token = page.get("nextToken")
            if not next_token:
                return items
            if next_token in seen_tokens:
                raise CanadaPostAccountApiError("list pagination did not advance")
            seen_tokens.add(next_token)
