from __future__ import annotations

import asyncio
import time

from custom_components.ha_doorbell_jeeves.const import EVENT_MEMORY
from custom_components.ha_doorbell_jeeves.events import EVENT_IMPORTANT, EventStore, ImportantEvent
from custom_components.ha_doorbell_jeeves.memory import MemoryStore, SessionMemory


def test_memory_store_adds_memory_and_fires_event(hass: object) -> None:
    store = MemoryStore(hass, "entry-1")
    memory = SessionMemory(
        timestamp=time.time(),
        duration_seconds=14.2,
        visitor_description="Delivery driver",
        summary="Visitor left package at the door.",
        outcome="package_delivered",
        visitor_name="Alex",
    )

    asyncio.run(store.async_add_memory(memory))

    assert len(store.memories) == 1
    assert store.memories[0].summary.startswith("Visitor left package")
    assert hass.bus.events[-1][0] == EVENT_MEMORY
    assert hass.bus.events[-1][1]["entry_id"] == "entry-1"


def test_memory_store_prunes_old_memories() -> None:
    from homeassistant.core import HomeAssistant

    hass = HomeAssistant()
    store = MemoryStore(hass, "entry-2")
    now = time.time()
    store._retention_days = 1
    store._memories = [
        SessionMemory(
            timestamp=now - (3 * 86400),
            duration_seconds=10,
            visitor_description="Old",
            summary="Old memory",
            outcome="ended",
        ),
        SessionMemory(
            timestamp=now,
            duration_seconds=11,
            visitor_description="New",
            summary="Recent memory",
            outcome="ended",
        ),
    ]

    asyncio.run(store._prune_old_memories())

    assert len(store.memories) == 1
    assert store.memories[0].summary == "Recent memory"


def test_event_store_add_attach_acknowledge(hass: object) -> None:
    store = EventStore(hass, "entry-3")
    event = ImportantEvent(
        id="",
        timestamp=0,
        title="Suspicious movement",
        description="Person loitering near front gate.",
        severity="warning",
    )

    event_id = asyncio.run(store.async_add_event(event))
    assert event_id.startswith("evt_")
    assert store.events[0].id == event_id
    assert hass.bus.events[-1][0] == EVENT_IMPORTANT

    added_photo = asyncio.run(store.async_attach_photo(event_id, "aGVsbG8="))
    acknowledged = asyncio.run(store.async_acknowledge(event_id))
    assert added_photo is True
    assert acknowledged is True
    assert store.events[0].photos == ["aGVsbG8="]
    assert store.events[0].acknowledged is True

