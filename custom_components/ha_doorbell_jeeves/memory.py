"""Session memory — stores recaps of doorbell interactions."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DEFAULT_MEMORY_RETENTION_DAYS, EVENT_MEMORY

_LOGGER = logging.getLogger(__name__)
STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = "ha_doorbell_jeeves.memories"


@dataclass
class SessionMemory:
    """A single doorbell interaction memory."""

    timestamp: float
    duration_seconds: float
    visitor_description: str
    summary: str
    outcome: str
    photo_base64: str = ""
    visitor_name: str = ""
    id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id or f"mem_{int(self.timestamp)}",
            "timestamp": self.timestamp,
            "duration_seconds": self.duration_seconds,
            "visitor_description": self.visitor_description,
            "summary": self.summary,
            "outcome": self.outcome,
            "photo_base64": self.photo_base64,
            "visitor_name": self.visitor_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionMemory":
        return cls(
            id=data.get("id", ""),
            timestamp=data.get("timestamp", 0),
            duration_seconds=data.get("duration_seconds", 0),
            visitor_description=data.get("visitor_description", ""),
            summary=data.get("summary", ""),
            outcome=data.get("outcome", ""),
            photo_base64=data.get("photo_base64", ""),
            visitor_name=data.get("visitor_name", ""),
        )


class MemoryStore:
    """Persistent storage for session memories."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}.{entry_id}")
        self._memories: list[SessionMemory] = []
        self._retention_days: int = DEFAULT_MEMORY_RETENTION_DAYS

    @property
    def memories(self) -> list[SessionMemory]:
        return self._memories

    @property
    def retention_days(self) -> int:
        return self._retention_days

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data:
            self._memories = [SessionMemory.from_dict(m) for m in data.get("memories", [])]
            self._retention_days = data.get("retention_days", DEFAULT_MEMORY_RETENTION_DAYS)
        await self._prune_old_memories()

    async def async_set_retention_days(self, retention_days: int) -> None:
        self._retention_days = max(0, int(retention_days))
        await self._prune_old_memories()
        await self.async_save()

    async def async_save(self) -> None:
        await self._store.async_save(
            {
                "memories": [m.to_dict() for m in self._memories],
                "retention_days": self._retention_days,
            }
        )

    async def async_add_memory(self, memory: SessionMemory) -> None:
        if not memory.id:
            memory.id = f"mem_{int(memory.timestamp)}_{len(self._memories)}"
        self._memories.append(memory)
        await self._prune_old_memories()
        await self.async_save()
        payload = memory.to_dict()
        payload["entry_id"] = self._entry_id
        self._hass.bus.async_fire(EVENT_MEMORY, payload)
        _LOGGER.info("Stored session memory %s", memory.id)

    async def _prune_old_memories(self) -> None:
        """Remove memories older than retention period."""
        if self._retention_days <= 0:
            return
        cutoff = time.time() - (self._retention_days * 86400)
        self._memories = [m for m in self._memories if m.timestamp > cutoff]

    def get_recent_memories(self, hours: int = 24) -> list[SessionMemory]:
        """Get memories from the last N hours."""
        cutoff = time.time() - (hours * 3600)
        return [m for m in self._memories if m.timestamp > cutoff]

    def search_memories(self, query: str) -> list[SessionMemory]:
        """Simple text search across memory summaries."""
        query_lower = query.lower()
        return [
            m
            for m in self._memories
            if query_lower in m.summary.lower()
            or query_lower in m.visitor_description.lower()
            or query_lower in m.visitor_name.lower()
        ]
