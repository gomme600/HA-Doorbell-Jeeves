"""Sensor entities for Doorbell Jeeves memory visibility in dashboards."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_MEMORY
from .memory import SessionMemory
from .memory_views import memory_image_url
from .session_manager import JeevesSessionManager

_MAX_STATE_LEN = 255


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


def _truncate(text: str, max_len: int = _MAX_STATE_LEN) -> str:
    """Clamp text to Home Assistant state length limits."""
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3]}..."


def _build_memory_feed_markdown(entry_id: str, memories: list[SessionMemory]) -> str:
    """Create markdown content for a single scrollable memory feed card."""
    if not memories:
        return "No memories saved yet."

    lines: list[str] = ["<div style='max-height: 70vh; overflow-y: auto; padding-right: 4px;'>"]
    for memory in memories:
        visitor = memory.visitor_name or memory.visitor_description or "Unknown visitor"
        timestamp = _memory_timestamp(memory)
        lines.append(f"### {visitor}")
        lines.append(
            f"_{timestamp} • {round(memory.duration_seconds, 1)}s • {memory.outcome}_"
        )
        if memory.photo_base64:
            lines.append(f"![{visitor}]({memory_image_url(entry_id, memory.id)})")
        summary = memory.summary or "No summary available."
        lines.append(summary)
        lines.append("---")
    lines.append("</div>")
    return "\n\n".join(lines)


class JeevesMemoryEntity(SensorEntity):
    """Base class for event-driven memory sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: JeevesSessionManager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._entry = entry
        self._attr_device_info = _device_info(entry)

    async def async_added_to_hass(self) -> None:
        """Subscribe to memory updates."""
        self._refresh_state()
        self.async_on_remove(
            self.hass.bus.async_listen(EVENT_MEMORY, self._handle_memory_event)
        )

    @callback
    def _handle_memory_event(self, event: Event) -> None:
        """Refresh when a new memory is stored."""
        event_entry_id = event.data.get("entry_id")
        if event_entry_id and event_entry_id != self._entry.entry_id:
            return
        self._refresh_state()
        self.async_write_ha_state()

    def _refresh_state(self) -> None:
        """Refresh internal state from manager data."""


class JeevesMemoryCountSensor(JeevesMemoryEntity):
    """Number of stored session memories."""

    _attr_name = "Memories"
    _attr_icon = "mdi:counter"

    def __init__(self, manager: JeevesSessionManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_memory_count"
        self._attr_native_value = 0
        self._recent_memories: list[SessionMemory] = []

    def _refresh_state(self) -> None:
        memories = self._manager.get_memories()
        self._attr_native_value = len(memories)
        self._recent_memories = memories[:5]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose recent memory metadata for dashboard drill-down."""
        return {
            "retention_days": self._manager.memory_retention_days,
            "latest_memory_at": _memory_timestamp(self._recent_memories[0] if self._recent_memories else None),
            "recent_memories": [
                {
                    "id": memory.id,
                    "timestamp": _memory_timestamp(memory),
                    "visitor_name": memory.visitor_name,
                    "visitor_description": memory.visitor_description,
                    "summary": memory.summary,
                    "outcome": memory.outcome,
                    "has_image": bool(memory.photo_base64),
                }
                for memory in self._recent_memories
            ],
        }


class JeevesLatestMemorySummarySensor(JeevesMemoryEntity):
    """Latest memory summary as a dashboard-friendly sensor."""

    _attr_name = "Latest Memory Summary"
    _attr_icon = "mdi:text-box-search-outline"

    def __init__(self, manager: JeevesSessionManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_latest_memory_summary"
        self._latest_memory: SessionMemory | None = None
        self._attr_native_value = "No memories yet"

    def _refresh_state(self) -> None:
        self._latest_memory = self._manager.get_latest_memory()
        if self._latest_memory:
            summary = self._latest_memory.summary or "No summary available"
            self._attr_native_value = _truncate(summary)
        else:
            self._attr_native_value = "No memories yet"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose full latest memory details."""
        memory = self._latest_memory
        if memory is None:
            return {"has_memory": False}
        return {
            "has_memory": True,
            "id": memory.id,
            "timestamp": _memory_timestamp(memory),
            "duration_seconds": round(memory.duration_seconds, 1),
            "visitor_name": memory.visitor_name,
            "visitor_description": memory.visitor_description,
            "summary": memory.summary,
            "outcome": memory.outcome,
            "has_image": bool(memory.photo_base64),
        }


class JeevesMemoryFeedSensor(JeevesMemoryEntity):
    """Single-entity feed for dashboard memory timeline cards."""

    _attr_name = "Memory Feed"
    _attr_icon = "mdi:view-stream"

    def __init__(self, manager: JeevesSessionManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_unique_id = f"{entry.entry_id}_memory_feed"
        self._attr_native_value = 0
        self._memories: list[SessionMemory] = []

    def _refresh_state(self) -> None:
        self._memories = self._manager.get_memories()
        self._attr_native_value = len(self._memories)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose all memories (with image URLs) for one-card dashboard feeds."""
        return {
            "entry_id": self._entry.entry_id,
            "retention_days": self._manager.memory_retention_days,
            "latest_memory_at": _memory_timestamp(self._memories[0] if self._memories else None),
            "memories": [
                {
                    "id": memory.id,
                    "timestamp": _memory_timestamp(memory),
                    "duration_seconds": round(memory.duration_seconds, 1),
                    "visitor_name": memory.visitor_name,
                    "visitor_description": memory.visitor_description,
                    "summary": memory.summary,
                    "outcome": memory.outcome,
                    "has_image": bool(memory.photo_base64),
                    "image_url": (
                        memory_image_url(self._entry.entry_id, memory.id)
                        if memory.photo_base64
                        else ""
                    ),
                }
                for memory in self._memories
            ],
            "dashboard_markdown": _build_memory_feed_markdown(
                self._entry.entry_id, self._memories
            ),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up memory sensors for a Doorbell Jeeves config entry."""
    manager: JeevesSessionManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            JeevesMemoryCountSensor(manager, entry),
            JeevesLatestMemorySummarySensor(manager, entry),
            JeevesMemoryFeedSensor(manager, entry),
        ]
    )
