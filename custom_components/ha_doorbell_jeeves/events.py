"""Important events published by the AI agent during sessions."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = "ha_doorbell_jeeves.events"

EVENT_IMPORTANT = f"{DOMAIN}_important_event"


@dataclass
class ImportantEvent:
    """An important event saved by the AI agent."""

    id: str
    timestamp: float
    title: str
    description: str
    severity: str = "info"  # info, warning, urgent
    photos: list[str] = field(default_factory=list)  # base64 JPEG images
    session_id: str = ""
    acknowledged: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "title": self.title,
            "description": self.description,
            "severity": self.severity,
            "photos": self.photos,
            "session_id": self.session_id,
            "acknowledged": self.acknowledged,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImportantEvent":
        return cls(
            id=data.get("id", ""),
            timestamp=data.get("timestamp", 0),
            title=data.get("title", ""),
            description=data.get("description", ""),
            severity=data.get("severity", "info"),
            photos=data.get("photos", []),
            session_id=data.get("session_id", ""),
            acknowledged=data.get("acknowledged", False),
        )


class EventStore:
    """Persistent storage for important events."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry_id}")
        self._events: list[ImportantEvent] = []
        self._retention_days: int = 90

    @property
    def events(self) -> list[ImportantEvent]:
        return self._events

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data and isinstance(data, dict):
            self._events = [ImportantEvent.from_dict(e) for e in data.get("events", [])]
            self._retention_days = data.get("retention_days", 90)
        await self._prune_old_events()

    async def async_save(self) -> None:
        await self._store.async_save({
            "events": [e.to_dict() for e in self._events],
            "retention_days": self._retention_days,
        })

    async def async_add_event(self, event: ImportantEvent) -> str:
        """Add an event and return its ID."""
        if not event.id:
            event.id = f"evt_{uuid.uuid4().hex[:12]}"
        if not event.timestamp:
            event.timestamp = time.time()
        self._events.append(event)
        await self.async_save()
        payload = event.to_dict()
        payload["entry_id"] = self._entry_id
        self._hass.bus.async_fire(EVENT_IMPORTANT, payload)
        _LOGGER.info("Stored important event: %s (%s)", event.title, event.id)

        # Auto-notify configured devices
        await self._auto_notify_event(event)

        return event.id

    async def _auto_notify_event(self, event: ImportantEvent) -> None:
        """Send notification to configured event_notify_targets."""
        from .const import CONF_EVENT_NOTIFY_TARGETS  # noqa: PLC0415

        # Get config from the integration entry
        entry_data = {}
        if hasattr(self._hass, "config_entries"):
            entry = self._hass.config_entries.async_get_entry(self._entry_id)
            if entry:
                entry_data = dict(entry.options) if entry.options else dict(entry.data)

        targets = entry_data.get(CONF_EVENT_NOTIFY_TARGETS, [])
        if not targets:
            return

        severity_emoji = {"urgent": "🚨", "warning": "⚠️", "info": "📋"}.get(event.severity, "📋")
        message = f"{severity_emoji} {event.title}\n{event.description[:300]}"
        title = f"🔔 Jeeves: {event.severity.upper()} Event"

        for target in targets:
            try:
                # target can be "notify.mobile_app_xxx" or just "mobile_app_xxx"
                service_str = target if isinstance(target, str) else str(target)
                if "." in service_str:
                    domain, svc = service_str.split(".", 1)
                else:
                    domain, svc = "notify", service_str
                await self._hass.services.async_call(
                    domain, svc,
                    {
                        "message": message,
                        "title": title,
                        "data": {
                            "push": {
                                "interruption-level": (
                                    "critical" if event.severity == "urgent"
                                    else "time-sensitive" if event.severity == "warning"
                                    else "active"
                                ),
                            },
                        },
                    },
                    blocking=False,
                )
            except Exception:
                _LOGGER.debug("Auto-notify failed for target %s", target)

    async def async_attach_photo(self, event_id: str, photo_b64: str) -> bool:
        """Attach a photo to an existing event."""
        for evt in self._events:
            if evt.id == event_id:
                evt.photos.append(photo_b64)
                await self.async_save()
                return True
        return False

    async def async_acknowledge(self, event_id: str) -> bool:
        """Mark an event as acknowledged."""
        for evt in self._events:
            if evt.id == event_id:
                evt.acknowledged = True
                await self.async_save()
                return True
        return False

    def get_unacknowledged(self) -> list[ImportantEvent]:
        return [e for e in self._events if not e.acknowledged]

    def get_recent(self, hours: int = 24) -> list[ImportantEvent]:
        cutoff = time.time() - (hours * 3600)
        return [e for e in self._events if e.timestamp > cutoff]

    async def _prune_old_events(self) -> None:
        if self._retention_days <= 0:
            return
        cutoff = time.time() - (self._retention_days * 86400)
        self._events = [e for e in self._events if e.timestamp > cutoff]
