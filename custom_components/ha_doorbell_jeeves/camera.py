"""Camera entity exposing the latest stored Doorbell Jeeves memory snapshot."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_MEMORY
from .memory import SessionMemory
from .session_manager import JeevesSessionManager


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return shared device metadata for memory entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title or "Doorbell Jeeves",
        manufacturer="Doorbell Jeeves",
        model="AI Doorbell Concierge",
    )


def _memory_timestamp(memory: SessionMemory | None) -> str:
    """Format memory timestamp as ISO string."""
    if memory is None:
        return ""
    return datetime.fromtimestamp(memory.timestamp, tz=timezone.utc).isoformat()


class JeevesLatestMemoryImageCamera(Camera):
    """Camera-like entity that serves the latest saved memory image."""

    _attr_has_entity_name = True
    _attr_name = "Latest Memory Image"
    _attr_icon = "mdi:image-multiple"
    _attr_should_poll = False
    _attr_content_type = "image/jpeg"

    def __init__(self, manager: JeevesSessionManager, entry: ConfigEntry) -> None:
        super().__init__()
        self._manager = manager
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_latest_memory_image"
        self._attr_device_info = _device_info(entry)
        self._latest_memory: SessionMemory | None = None
        self._latest_image_bytes: bytes | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to memory updates."""
        self._refresh_from_store()
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_MEMORY, self._handle_memory_event)
        )

    @callback
    def _handle_memory_event(self, event: Event) -> None:
        """Refresh when a memory record is stored."""
        event_entry_id = event.data.get("entry_id")
        if event_entry_id and event_entry_id != self._entry.entry_id:
            return
        self._refresh_from_store()
        self.async_write_ha_state()

    def _refresh_from_store(self) -> None:
        """Load latest memory and decode snapshot."""
        self._latest_memory = self._manager.get_latest_memory()
        self._latest_image_bytes = None
        if self._latest_memory and self._latest_memory.photo_base64:
            try:
                self._latest_image_bytes = base64.b64decode(
                    self._latest_memory.photo_base64
                )
            except (TypeError, ValueError):
                self._latest_image_bytes = None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return latest stored snapshot bytes."""
        return self._latest_image_bytes

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose metadata for the currently served snapshot."""
        memory = self._latest_memory
        if memory is None:
            return {"has_memory": False, "has_image": False}
        return {
            "has_memory": True,
            "has_image": bool(self._latest_image_bytes),
            "id": memory.id,
            "timestamp": _memory_timestamp(memory),
            "visitor_name": memory.visitor_name,
            "visitor_description": memory.visitor_description,
            "summary": memory.summary,
            "outcome": memory.outcome,
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up memory camera for a Doorbell Jeeves config entry."""
    manager: JeevesSessionManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([JeevesLatestMemoryImageCamera(manager, entry)])
