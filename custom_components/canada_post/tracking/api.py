"""Canada Post public tracker client, the only detail transport.

Both sources fetch parcel detail through here: the account list carries
identifiers only. The contract the coordinators rely on:

* ``async_get_parcel`` returns the raw detail dict on success,
* returns ``None`` when Canada Post has no history for the PIN (a normal state
  — never an error),
* raises :class:`CanadaPostApiError` for anything else, with ``status_code``
  set on a non-2xx response and ``retry_after`` set when a 429 carried a
  seconds-valued ``Retry-After``,
* lets ``aiohttp.ClientError`` propagate untouched — ``DataUpdateCoordinator``
  already wraps those into ``UpdateFailed``.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from ..const import ALIAS_URL, DETAIL_HEADERS, DETAIL_URL
from ..status import NEW_ISSUE_URL

_LOGGER = logging.getLogger(__name__)

_envelope_warned: set[str] = set()


def _warn_envelope_first_sighting(reason: str, detail: str) -> None:
    """Log an unexpected detail envelope once per reason."""
    if reason in _envelope_warned:
        return
    _envelope_warned.add(reason)
    _LOGGER.warning(
        "Unexpected Canada Post tracking response — help us confirm the shape. "
        "Open an issue and paste this line: %s\n  %s: %s",
        NEW_ISSUE_URL,
        reason,
        detail,
    )


class CanadaPostApiError(Exception):
    """Raised when a Canada Post API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Canada Post API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class CanadaPostTrackingClient:
    """Client for the keyless public tracker.

    ``200`` and ``206`` both carry a JSON body; a miss is an ``error`` object
    (``cd`` ``004``) inside that body, not an HTTP status.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def _get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        async with self._session.get(
            url, headers=DETAIL_HEADERS, params=params
        ) as response:
            if response.status == 429:
                header = response.headers.get("Retry-After")
                try:
                    retry_after = float(header) if header else None
                except ValueError:
                    retry_after = None
                raise CanadaPostApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if response.status not in (200, 206):
                raise CanadaPostApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            try:
                return await response.json(content_type=None)
            except ValueError as err:
                raise CanadaPostApiError(f"unparseable body ({err})") from err

    async def async_get_parcel(self, pin: str) -> dict[str, Any] | None:
        """Fetch one parcel's detail; ``None`` when there is no history."""
        payload = await self._get_json(DETAIL_URL.format(pin=pin))
        if not isinstance(payload, dict):
            raise CanadaPostApiError("unexpected body (not a JSON object)")
        error = payload.get("error")
        if error is not None:
            code = error.get("cd") if isinstance(error, dict) else None
            if code != "004":
                _warn_envelope_first_sighting(
                    "error envelope", f"cd={code!r} keys={sorted(payload)}"
                )
            return None
        if "pin" not in payload:
            _warn_envelope_first_sighting(
                "no pin in body", f"keys={sorted(payload)}"
            )
            return None
        return payload

    async def async_resolve_dnc(self, dnc: str) -> str | None:
        """Resolve a delivery notice card number to its PIN.

        The alias route must answer with exactly one item; zero or several is
        a miss, since a several-match could point at someone else's parcel.
        """
        payload = await self._get_json(ALIAS_URL, params={"dncs": dnc})
        if not isinstance(payload, list):
            return None
        if len(payload) != 1 or not isinstance(payload[0], dict):
            return None
        pin = payload[0].get("pin")
        return pin if isinstance(pin, str) and pin else None
