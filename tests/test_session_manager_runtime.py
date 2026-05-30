from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import custom_components.ha_doorbell_jeeves.gemini_client as gc_module
from custom_components.ha_doorbell_jeeves.const import (
    CONF_API_KEY,
    CONF_CAMERA_ENTITY,
    CONF_MEMORY_RETENTION_DAYS,
    CONF_MODEL,
    CONF_PROVIDER,
    CONF_VISION_FPS,
    PROVIDER_GEMINI,
)
from custom_components.ha_doorbell_jeeves.session_manager import JeevesSessionManager


def test_switch_active_camera_restarts_existing_vision_loop(monkeypatch: Any) -> None:
    async def _run() -> None:
        import custom_components.ha_doorbell_jeeves.reolink_audio as reolink_audio

        manager = JeevesSessionManager.__new__(JeevesSessionManager)
        manager._vision_task = asyncio.create_task(asyncio.sleep(3600))
        manager._config = {CONF_VISION_FPS: 2.5}
        manager.hass = SimpleNamespace()
        async def _mock_start() -> None: pass
        async def _mock_stop() -> None: pass
        manager._audio_handler = SimpleNamespace(
            is_active=True,
            has_pending_audio=False,
            _camera_entity_id="camera.old",
            start=_mock_start,
            stop=_mock_stop,
        )
        manager._talk_monitor = None
        manager._interrupt_detector = None
        manager._client = SimpleNamespace(_model_generating=False)
        manager._active = True
        manager._camera_exists = lambda _entity_id: True

        async def _noop_stop_mic_forwarder() -> None:
            return None

        manager._stop_mic_forwarder = _noop_stop_mic_forwarder
        monkeypatch.setattr(reolink_audio, "find_reolink_entry_for_camera", lambda *_args: "")

        calls: dict[str, Any] = {}

        async def _fake_vision_loop(camera_entity_id: str, fps: float) -> None:
            calls["camera"] = camera_entity_id
            calls["fps"] = fps

        manager._vision_loop = _fake_vision_loop

        await manager._switch_active_camera("camera.new")
        await asyncio.sleep(0)

        assert manager._config[CONF_CAMERA_ENTITY] == "camera.new"
        assert calls == {"camera": "camera.new", "fps": 2.5}

        if manager._vision_task and not manager._vision_task.done():
            manager._vision_task.cancel()
            try:
                await manager._vision_task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


def test_ptz_auto_return_uses_store_and_clears_tracking() -> None:
    async def _run() -> None:
        class _Services:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, dict[str, Any]]] = []

            async def async_call(self, domain: str, service: str, data: dict[str, Any]) -> None:
                self.calls.append((domain, service, data))

        manager = JeevesSessionManager.__new__(JeevesSessionManager)
        services = _Services()
        manager.hass = SimpleNamespace(services=services)
        manager.store = SimpleNamespace(
            camera_placements=[
                SimpleNamespace(entity_id="camera.front_ptz", ptz_return_to_monitor="button.front_return"),
                SimpleNamespace(entity_id="camera.side_ptz", ptz_return_to_monitor="script.side_return"),
            ]
        )
        manager._ptz_moved_cameras = {"camera.front_ptz", "camera.side_ptz"}

        await manager._ptz_auto_return()

        assert ("button", "press", {"entity_id": "button.front_return"}) in services.calls
        assert ("script", "turn_on", {"entity_id": "script.side_return"}) in services.calls
        assert manager._ptz_moved_cameras == set()

    asyncio.run(_run())


def test_restart_session_from_trigger_waits_for_stop_before_start() -> None:
    async def _run() -> None:
        manager = JeevesSessionManager.__new__(JeevesSessionManager)
        manager._active = True
        manager._starting = False
        calls: list[str] = []

        async def _stop(reason: str) -> None:
            calls.append(f"stop:{reason}")
            manager._active = False

        async def _start() -> None:
            calls.append("start")

        manager.async_stop_session = _stop
        manager._safe_start_session = _start

        await manager._restart_session_from_trigger()

        assert calls == ["stop:restart requested by trigger", "start"]

    asyncio.run(_run())


def test_async_initialize_prewarms_shared_gemini_client(monkeypatch: Any) -> None:
    async def _run() -> None:
        manager = JeevesSessionManager.__new__(JeevesSessionManager)
        manager._config = {
            CONF_PROVIDER: PROVIDER_GEMINI,
            CONF_API_KEY: "k",
            CONF_MODEL: "gemini-test-model",
            CONF_MEMORY_RETENTION_DAYS: 30,
        }

        async def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        manager.store = SimpleNamespace(
            async_load=_noop,
            start_triggers=[],
            async_set_start_triggers=_noop,
        )
        manager._memory_store = SimpleNamespace(
            async_load=_noop,
            async_set_retention_days=_noop,
        )
        manager._event_store = SimpleNamespace(async_load=_noop)
        manager._notification_manager = SimpleNamespace(async_setup=_noop)
        manager._register_start_triggers = lambda: None

        called: list[tuple[str, str]] = []

        async def _fake_prewarm(_cls: Any, api_key: str, model: str) -> None:
            called.append((api_key, model))

        monkeypatch.setattr(
            gc_module.GeminiLiveClient,
            "prewarm_shared_client",
            classmethod(_fake_prewarm),
        )

        await manager.async_initialize()

        assert called == [("k", "gemini-test-model")]

    asyncio.run(_run())


def test_resolve_camera_entity_falls_back_to_managed_camera() -> None:
    manager = JeevesSessionManager.__new__(JeevesSessionManager)
    manager._config = {}
    manager._primary_camera_entity = ""
    manager.hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(state="idle")
            if entity_id == "camera.entree"
            else None
        )
    )
    manager.store = SimpleNamespace(
        camera_placements=[
            SimpleNamespace(entity_id="camera.entree", is_doorbell=True),
        ],
        managed_entities=[
            SimpleNamespace(entity_id="camera.entree"),
        ],
    )

    assert manager._resolve_camera_entity("camera.missing") == "camera.entree"
    assert manager._config[CONF_CAMERA_ENTITY] == "camera.entree"


def test_resolve_camera_entity_prefers_managed_camera_over_unmanaged_state() -> None:
    manager = JeevesSessionManager.__new__(JeevesSessionManager)
    manager._config = {}
    manager._primary_camera_entity = ""
    manager.hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: SimpleNamespace(state="idle")
            if entity_id in {"camera.raw_reolink", "camera.entree"}
            else None
        )
    )
    manager.store = SimpleNamespace(
        camera_placements=[
            SimpleNamespace(entity_id="camera.entree", is_doorbell=True),
        ],
        managed_entities=[
            SimpleNamespace(entity_id="camera.entree"),
        ],
    )

    assert manager._resolve_camera_entity("camera.raw_reolink") == "camera.entree"
    assert manager._config[CONF_CAMERA_ENTITY] == "camera.entree"
