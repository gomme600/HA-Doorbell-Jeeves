from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from custom_components.ha_doorbell_jeeves.const import CONF_CAMERA_ENTITY, CONF_VISION_FPS
from custom_components.ha_doorbell_jeeves.session_manager import JeevesSessionManager


def test_switch_active_camera_restarts_existing_vision_loop() -> None:
    async def _run() -> None:
        manager = JeevesSessionManager.__new__(JeevesSessionManager)
        manager._vision_task = asyncio.create_task(asyncio.sleep(3600))
        manager._config = {CONF_VISION_FPS: 2.5}
        async def _mock_start() -> None: pass
        async def _mock_stop() -> None: pass
        manager._audio_handler = SimpleNamespace(
            is_active=True, _camera_entity_id="camera.old", start=_mock_start, stop=_mock_stop
        )
        manager._client = object()
        manager._active = True

        calls: dict[str, Any] = {}

        async def _fake_vision_loop(camera_entity_id: str, fps: float) -> None:
            calls["camera"] = camera_entity_id
            calls["fps"] = fps

        manager._vision_loop = _fake_vision_loop

        await manager._switch_active_camera("camera.new")
        await asyncio.sleep(0)

        assert manager._config[CONF_CAMERA_ENTITY] == "camera.new"
        assert manager._audio_handler._camera_entity_id == "camera.new"
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

