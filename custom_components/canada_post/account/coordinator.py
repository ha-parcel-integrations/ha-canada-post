"""Coordinator for the account source.

Canada Post's account list carries identifiers only, so each listed PIN goes
through the tracking client and the shared normaliser. Every account parcel is
incoming: the list has no direction field.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_INCLUDE_HISTORY,
    DEFAULT_INCLUDE_HISTORY,
    DETAIL_FETCH_CONCURRENCY,
    DOMAIN,
    MID_INTERVAL_MINUTES,
    ParcelStatus,
)
from ..events import (
    fire_incoming_change_events,
    snapshot_delivery_times,
    snapshot_states,
)
from ..tracking.api import CanadaPostApiError, CanadaPostTrackingClient
from ..tracking.coordinator import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    _hottest_tier_minutes,
    _next_update_interval,
)
from ..tracking.parcels import (
    apply_delivered_filter,
    normalize_parcel,
    resolve_lang,
    sort_parcels_by_ts,
)
from .client import (
    CanadaPostAccountApiError,
    CanadaPostAccountClient,
    CanadaPostAccountReauthRequired,
)

_LOGGER = logging.getLogger(__name__)

_DNC_RE = re.compile(r"^(\d{9}|\d{15})$")


class CanadaPostAccountCoordinator(DataUpdateCoordinator[list[dict]]):
    """Refresh the account's tracked parcels; polling never fully suspends."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: CanadaPostAccountClient,
        tracking_client: CanadaPostTrackingClient,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} account",
            update_interval=timedelta(minutes=MID_INTERVAL_MINUTES),
        )
        self._client = client
        self._tracking = tracking_client
        self.delivered: list[dict] = []
        self._raw_cache: dict[str, dict] = {}
        self._delivered_pins: set[str] = set()
        self._resolved: dict[str, str] = {}
        self._consecutive_429 = 0
        # A long saved list must not hit the tracker with hundreds of calls at once.
        self._fetch_slots = asyncio.Semaphore(DETAIL_FETCH_CONCURRENCY)
        self._current_tier_minutes: int | None = MID_INTERVAL_MINUTES
        self._known_state: dict[str, ParcelStatus] | None = None
        self._known_delivery_times: (
            dict[str, tuple[str | None, str | None]] | None
        ) = None
        self._cached_device_id: str | None = None
        self.last_success_time: datetime | None = None

    @property
    def current_tier_minutes(self) -> int | None:
        """Tier minutes computed on the last refresh (diagnostics only)."""
        return self._current_tier_minutes

    @property
    def delivered_codes(self) -> set[str]:
        """PINs currently skipped from the fetch (diagnostics only)."""
        return self._delivered_pins

    def _device_id(self) -> str | None:
        """Resolve (and cache) this entry's device id for event payloads."""
        if self._cached_device_id is not None:
            return self._cached_device_id
        registry = dr.async_get(self.hass)
        device = next(
            iter(
                dr.async_entries_for_config_entry(registry, self.config_entry.entry_id)
            ),
            None,
        )
        if device is not None:
            self._cached_device_id = device.id
        return self._cached_device_id

    async def _pin_for(self, track_id: str) -> str | None:
        """Return the PIN for a list entry, resolving a notice card number."""
        code = re.sub(r"\s+", "", track_id).upper()
        if not _DNC_RE.match(code):
            return code
        if code not in self._resolved:
            pin = await self._tracking.async_resolve_dnc(code)
            if pin is None:
                return None
            self._resolved[code] = pin
        return self._resolved[code]

    async def _fetch(self, pin: str) -> dict | None:
        async with self._fetch_slots:
            return await self._tracking.async_get_parcel(pin)

    async def _async_update_data(self) -> list[dict]:
        try:
            items = await self._client.async_list_items()
        except CanadaPostAccountReauthRequired as err:
            raise ConfigEntryAuthFailed(
                "Canada Post account needs reauthentication"
            ) from err
        except CanadaPostAccountApiError as err:
            raise UpdateFailed("Unable to update the Canada Post account list") from err

        by_pin: dict[str, dict[str, Any]] = {}
        placeholders: dict[str, dict[str, Any]] = {}
        for item in items:
            try:
                pin = await self._pin_for(item["trackId"])
            except (CanadaPostApiError, aiohttp.ClientError) as err:
                _LOGGER.warning("Canada Post notice-card lookup failed: %s", err)
                pin = None
            if pin is None:
                placeholders[item["trackId"]] = item
            else:
                by_pin.setdefault(pin, item)

        self._raw_cache = {p: r for p, r in self._raw_cache.items() if p in by_pin}
        self._delivered_pins &= set(by_pin)
        to_fetch = [pin for pin in by_pin if pin not in self._delivered_pins]
        results = await asyncio.gather(
            *(self._fetch(pin) for pin in to_fetch), return_exceptions=True
        )

        errors = 0
        retry_afters: list[float] = []
        saw_429 = False
        for pin, result in zip(to_fetch, results):
            if isinstance(result, BaseException):
                if not isinstance(result, (CanadaPostApiError, aiohttp.ClientError)):
                    raise result
                errors += 1
                if isinstance(result, CanadaPostApiError) and result.status_code == 429:
                    saw_429 = True
                    if result.retry_after is not None:
                        retry_afters.append(result.retry_after)
                _LOGGER.warning("Canada Post fetch failed for a listed parcel: %s", result)
            elif result is None:
                self._raw_cache.setdefault(pin, {"pin": pin})
            else:
                self._raw_cache[pin] = result

        if saw_429:
            self._consecutive_429 += 1
            raise UpdateFailed(
                "Canada Post rate-limited (429)",
                retry_after=(
                    max(retry_afters)
                    if retry_afters
                    else min(
                        BACKOFF_BASE_SECONDS * 2**self._consecutive_429,
                        BACKOFF_CAP_SECONDS,
                    )
                ),
            )
        self._consecutive_429 = 0

        if to_fetch and errors == len(to_fetch) and not any(
            pin in self._raw_cache for pin in to_fetch
        ):
            raise UpdateFailed("Canada Post unreachable for all listed parcels")

        include_history = bool(
            self.config_entry.options.get(CONF_INCLUDE_HISTORY, DEFAULT_INCLUDE_HISTORY)
        )
        lang = resolve_lang(self.hass.config.language)
        parcels: list[dict] = []
        for pin, item in by_pin.items():
            raw = self._raw_cache.get(pin) or {"pin": pin}
            parcels.append(self._normalise(raw, pin, item, lang, include_history))
        for track_id, item in placeholders.items():
            parcels.append(
                self._normalise({"pin": track_id}, track_id, item, lang, include_history)
            )

        active = [p for p in parcels if not p["delivered"]]
        delivered = [p for p in parcels if p["delivered"]]
        self._delivered_pins = {p["barcode"] for p in delivered if p["barcode"] in by_pin}
        self.delivered = apply_delivered_filter(
            sort_parcels_by_ts(delivered, "delivered_at", descending=True),
            self.config_entry,
        )
        active = sort_parcels_by_ts(active, "planned_from")

        incoming = active + self.delivered
        fire_incoming_change_events(
            self.hass,
            incoming,
            self._known_state,
            self._known_delivery_times,
            self._device_id(),
        )
        self._known_state = snapshot_states(incoming)
        self._known_delivery_times = snapshot_delivery_times(incoming)

        if not to_fetch or errors < len(to_fetch):
            self.last_success_time = datetime.now(timezone.utc)

        now = dt_util.now()
        self._current_tier_minutes = (
            _hottest_tier_minutes(active, now) or MID_INTERVAL_MINUTES
        )
        self.update_interval = _next_update_interval(
            now, self._current_tier_minutes, self.config_entry.entry_id
        )
        return active

    @staticmethod
    def _normalise(
        raw: dict, pin: str, item: dict[str, Any], lang: str, include_history: bool
    ) -> dict:
        parcel = normalize_parcel(
            raw, pin=pin, lang=lang, include_history=include_history
        )
        parcel["raw"] = {
            **parcel["raw"],
            "account": {
                "source": item.get("source"),
                "type": item.get("type"),
                "userDescription": item.get("userDescription"),
            },
        }
        return parcel
