"""Calendar platform for the Canada Post parcel tracker integration."""
from __future__ import annotations

from datetime import datetime, time, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import CanadaPostConfigEntry
from .device import ATTRIBUTION, build_device_info
from .tracking.coordinator import CanadaPostCoordinator
from .tracking.parcels import display_name, parse_iso

PARALLEL_UPDATES = 0

_DEFAULT_EVENT_DURATION = timedelta(hours=1)



async def async_setup_entry(
    hass: HomeAssistant,
    entry: CanadaPostConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Canada Post deliveries calendar from a config entry."""
    async_add_entities([CanadaPostDeliveriesCalendar(entry.runtime_data.coordinator, entry)])


class CanadaPostDeliveriesCalendar(CoordinatorEntity[CanadaPostCoordinator], CalendarEntity):
    """A read-only calendar of expected Canada Post deliveries.

    Each active tracked parcel with a known delivery moment becomes an event.
    No extra API calls — a pure view over coordinator data — so it is enabled
    by default and can be turned off per entity if unwanted.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "deliveries"
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: CanadaPostCoordinator, entry: ConfigEntry) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_deliveries"
        self._attr_device_info = build_device_info(entry)

    def _events(self) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for parcel in self.coordinator.data or []:
            start = parse_iso(parcel.get("planned_from"))
            if start is None:
                continue
            end = parse_iso(parcel.get("planned_to"))
            local = dt_util.as_local(start)
            if end is None and local.time() == time.min:
                # A date-only ETA is an all-day event, not an hour after midnight.
                start, end = local.date(), local.date() + timedelta(days=1)
            elif end is None or end <= start:
                end = start + _DEFAULT_EVENT_DURATION

            barcode = parcel.get("barcode") or ""
            sender = parcel.get("sender")
            summary = display_name(parcel) or sender or (
                f"Parcel {barcode}" if barcode else "Canada Post parcel"
            )
            description_parts = [
                f"Barcode: {barcode}" if barcode else None,
                f"Status: {parcel.get('status')}" if parcel.get("status") else None,
                parcel.get("url"),
            ]
            description = "\n".join(p for p in description_parts if p)
            location = parcel.get("pickup_point") if parcel.get("pickup") else None

            events.append(
                CalendarEvent(
                    start=start,
                    end=end,
                    summary=summary,
                    description=description or None,
                    location=location,
                    uid=barcode or None,
                )
            )
        return events

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming calendar event."""
        now = dt_util.now()
        upcoming = [
            event for event in self._events() if event.end_datetime_local > now
        ]
        return (
            min(upcoming, key=lambda event: event.start_datetime_local)
            if upcoming
            else None
        )

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return calendar events within a datetime range."""
        return [
            event
            for event in self._events()
            if event.start_datetime_local < end_date
            and event.end_datetime_local > start_date
        ]
