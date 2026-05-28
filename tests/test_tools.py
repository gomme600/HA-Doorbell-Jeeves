from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from custom_components.ha_doorbell_jeeves.const import DOMAIN, TOOL_PLAY_AUDIO
from custom_components.ha_doorbell_jeeves.events import EventStore, EVENT_IMPORTANT
from custom_components.ha_doorbell_jeeves.models import AudioFile, CameraPlacement, ManagedEntity
from custom_components.ha_doorbell_jeeves.tools import (
    _execute_extend_session,
    _execute_play_audio,
    _execute_recall_memories,
    _execute_save_event,
    build_gemini_tools,
    build_openai_tools,
)


class _DummyStore:
    def __init__(
        self,
        *,
        managed_entities: list[ManagedEntity] | None = None,
        camera_placements: list[CameraPlacement] | None = None,
        audio_files: list[AudioFile] | None = None,
    ) -> None:
        self.managed_entities = managed_entities or []
        self.camera_placements = camera_placements or []
        self.audio_files = audio_files or []
        self.notification_targets: list[Any] = []
        self.task_instructions: list[Any] = []

    def get_entity(self, entity_id: str) -> ManagedEntity | None:
        for entity in self.managed_entities:
            if entity.entity_id == entity_id:
                return entity
        return None


def _sample_audio_file() -> AudioFile:
    return AudioFile(
        id="doorbell_chime",
        name="Doorbell Chime",
        description="Simple chime",
        media_id="media-source://media_source/local/chime.mp3",
        media_type="music",
    )


def test_build_openai_tools_omits_empty_target_enum() -> None:
    store = _DummyStore(audio_files=[_sample_audio_file()])
    tools = build_openai_tools(store)
    play_audio = next(tool for tool in tools if tool["name"] == TOOL_PLAY_AUDIO)
    target = play_audio["parameters"]["properties"]["target_entity_id"]
    assert "enum" not in target


def test_build_openai_tools_includes_targets_when_available() -> None:
    store = _DummyStore(
        managed_entities=[ManagedEntity(entity_id="media_player.kitchen", name="Kitchen Speaker", description="")],
        audio_files=[_sample_audio_file()],
    )
    tools = build_openai_tools(store)
    play_audio = next(tool for tool in tools if tool["name"] == TOOL_PLAY_AUDIO)
    target = play_audio["parameters"]["properties"]["target_entity_id"]
    assert target["enum"] == ["media_player.kitchen"]


def test_build_gemini_tools_omits_empty_target_enum() -> None:
    store = _DummyStore(audio_files=[_sample_audio_file()])
    tools = build_gemini_tools(store)
    declarations = tools[0].function_declarations
    play_audio = next(declaration for declaration in declarations if declaration.name == TOOL_PLAY_AUDIO)
    params = play_audio.parameters
    if isinstance(params, dict):
        target = params["properties"]["target_entity_id"]
        assert "enum" not in target
        return

    target = params.properties["target_entity_id"]
    assert not getattr(target, "enum", None)


def test_execute_play_audio_rejects_unmanaged_target(hass: object) -> None:
    store = _DummyStore(
        managed_entities=[ManagedEntity(entity_id="media_player.kitchen", name="Kitchen Speaker", description="")],
        audio_files=[_sample_audio_file()],
    )
    result = asyncio.run(
        _execute_play_audio(
            hass,
            store,
            {"audio_id": "doorbell_chime", "target_entity_id": "light.porch"},
            {"media_player_entity": "media_player.kitchen"},
        )
    )
    assert "not an allowed audio output" in result["error"]


def test_execute_play_audio_uses_default_target(hass: object) -> None:
    store = _DummyStore(
        managed_entities=[ManagedEntity(entity_id="media_player.kitchen", name="Kitchen Speaker", description="")],
        audio_files=[_sample_audio_file()],
    )
    result = asyncio.run(
        _execute_play_audio(
            hass,
            store,
            {"audio_id": "doorbell_chime"},
            {"media_player_entity": "media_player.kitchen"},
        )
    )
    assert result["success"] is True
    domain, service, payload, blocking, _ = hass.services.calls[-1]
    assert (domain, service) == ("media_player", "play_media")
    assert payload["entity_id"] == "media_player.kitchen"
    assert blocking is True


def test_execute_extend_session_handles_invalid_input() -> None:
    result = _execute_extend_session({"extra_seconds": "not-a-number", "reason": "monitoring"})
    assert result["_extend_session_seconds"] == 60

    result_max = _execute_extend_session({"extra_seconds": 99999, "reason": "monitoring"})
    assert result_max["_extend_session_seconds"] == 300

    result_min = _execute_extend_session({"extra_seconds": 1, "reason": "monitoring"})
    assert result_min["_extend_session_seconds"] == 30


def test_execute_recall_memories_parses_and_clamps_hours(hass: object) -> None:
    class _MemoryStore:
        def __init__(self) -> None:
            self.hours_calls: list[int] = []
            self.search_calls: list[str] = []

        def search_memories(self, query: str) -> list[Any]:
            self.search_calls.append(query)
            return []

        def get_recent_memories(self, hours_back: int) -> list[Any]:
            self.hours_calls.append(hours_back)
            return []

    memory_store = _MemoryStore()
    manager = SimpleNamespace(_memory_store=memory_store)
    hass.data = {DOMAIN: {"entry-1": manager}}

    _ = asyncio.run(
        _execute_recall_memories(
            hass,
            _DummyStore(),
            {"hours_back": "invalid"},
            {"_entry_id": "entry-1"},
        )
    )
    _ = asyncio.run(
        _execute_recall_memories(
            hass,
            _DummyStore(),
            {"hours_back": 9999},
            {"_entry_id": "entry-1"},
        )
    )
    _ = asyncio.run(
        _execute_recall_memories(
            hass,
            _DummyStore(),
            {"query": "alex"},
            {"_entry_id": "entry-1"},
        )
    )

    assert memory_store.hours_calls[0] == 72
    assert memory_store.hours_calls[1] == 720
    assert memory_store.search_calls == ["alex"]


def test_execute_save_event_falls_back_to_available_manager_and_fires_entry_event(hass: object) -> None:
    event_store = EventStore(hass, "entry-1")
    manager = SimpleNamespace(_event_store=event_store)
    hass.data = {DOMAIN: {"entry-1": manager}}

    result = asyncio.run(
        _execute_save_event(
            hass,
            _DummyStore(),
            {
                "title": "Security risk",
                "description": "A suspicious visitor was seen near the side gate.",
                "severity": "urgent",
            },
            {},
        )
    )

    assert result["success"] is True
    assert event_store.events[0].title == "Security risk"
    assert hass.bus.events[-1][0] == EVENT_IMPORTANT
    assert hass.bus.events[-1][1]["entry_id"] == "entry-1"
