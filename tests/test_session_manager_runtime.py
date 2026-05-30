from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import custom_components.ha_doorbell_jeeves.gemini_client as gc_module
import custom_components.ha_doorbell_jeeves.session_manager as sm_module
import custom_components.ha_doorbell_jeeves.tools as tools_module
from custom_components.ha_doorbell_jeeves.const import (
    AUDIO_MODE_REOLINK,
    CONF_API_KEY,
    CONF_AUDIO_MODE,
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


def test_handle_model_turn_complete_starts_media_once() -> None:
    async def _run() -> None:
        manager = JeevesSessionManager.__new__(JeevesSessionManager)
        manager._startup_media_pending = True
        manager._startup_media_camera_entity = "camera.entree"
        manager._startup_media_config = {"audio_mode": "reolink"}
        manager._notify_after_greeting = True
        manager._greeting_phase = True

        scheduled: list[asyncio.Future[Any] | asyncio.Task[Any] | Any] = []
        calls: list[str] = []

        async def _fake_start(camera_entity: str, config: dict[str, Any]) -> None:
            calls.append(f"media:{camera_entity}:{config['audio_mode']}")

        async def _fake_notify() -> None:
            calls.append("notify")

        async def _fake_inject_refs() -> None:
            calls.append("refs")

        manager._start_session_media = _fake_start
        manager._post_greeting_notify = _fake_notify
        manager._client = SimpleNamespace(
            connected=True,
            inject_pending_reference_images=_fake_inject_refs,
        )
        manager.hass = SimpleNamespace(
            async_create_task=lambda coro: scheduled.append(coro) or coro
        )

        manager._handle_model_turn_complete(1)

        assert manager._startup_media_pending is False
        assert manager._startup_media_camera_entity == ""
        assert manager._startup_media_config is None
        assert manager._notify_after_greeting is False
        assert manager._greeting_phase is False
        assert len(scheduled) == 1

        await scheduled[0]

        assert calls == [
            "refs",
            "media:camera.entree:reolink",
            "notify",
        ]

    asyncio.run(_run())


def test_async_start_session_uses_configured_fps_for_startup_log(monkeypatch: Any) -> None:
    async def _run() -> None:
        manager = JeevesSessionManager.__new__(JeevesSessionManager)
        manager._active = False
        manager._starting = False
        manager._audio_handler = None
        manager._primary_camera_entity = ""
        manager._primary_go2rtc_stream = ""
        manager._primary_go2rtc_input = ""
        manager._primary_go2rtc_output = ""
        manager._primary_reolink_entry = ""
        manager.reolink_needs_setup = False
        manager._tool_router = None
        manager._vision_task = None
        manager._silence_task = None
        manager._notification_manager = SimpleNamespace(clear_session=lambda: None)
        manager._security = SimpleNamespace(start_session=lambda: None)
        manager._stop_unsubs = []
        manager._session_start_snapshot = None
        manager._transcript_history = []
        manager.entry = SimpleNamespace(entry_id="entry-1")
        manager.store = SimpleNamespace(camera_placements=[], managed_entities=[])

        async def _async_add_executor_job(func: Any, *args: Any) -> Any:
            return func(*args)

        manager.hass = SimpleNamespace(
            async_add_executor_job=_async_add_executor_job,
            bus=SimpleNamespace(async_fire=lambda *_args, **_kwargs: None),
        )
        manager._config = {
            CONF_API_KEY: "k",
            CONF_PROVIDER: PROVIDER_GEMINI,
            CONF_MODEL: "gemini-test-model",
            CONF_AUDIO_MODE: AUDIO_MODE_REOLINK,
            CONF_CAMERA_ENTITY: "camera.entree",
            CONF_VISION_FPS: 2.5,
        }

        async def _noop_async(*_args: Any, **_kwargs: Any) -> None:
            return None

        call_order: list[str] = []

        async def _fake_create_client(
            _api_key: str,
            _model: str,
            _voice_prompt: str,
            _voice: str,
            _tools: list[Any],
            _reference_images: list[Any],
        ) -> Any:
            async def _connect(*, greeting_text: str) -> None:
                call_order.append(f"connect:{greeting_text}")

            return SimpleNamespace(connect=_connect)

        async def _fake_start_reolink_audio(
            _config: dict[str, Any],
            skip_talk_monitor: bool = False,
            *,
            output_only: bool = False,
            input_only: bool = False,
        ) -> None:
            call_order.append(
                "audio:"
                f"output_only={output_only},input_only={input_only},skip_talk_monitor={skip_talk_monitor}"
            )

        monkeypatch.setattr(sm_module, "build_system_context", lambda *_args: "")
        monkeypatch.setattr(sm_module, "build_gemini_tools", lambda *_args: [])
        monkeypatch.setattr(sm_module, "build_openai_tools", lambda *_args: [])
        monkeypatch.setattr(tools_module, "render_camera_map_image", lambda *_args: None)

        manager._capture_memory_snapshot = _noop_async
        manager._get_reference_images = lambda: []
        manager._create_gemini_client = _fake_create_client
        manager._touch_audio_activity = lambda: None
        manager._schedule_session_timeout = lambda _timeout: None
        manager._register_stop_triggers = lambda: None
        manager._cleanup_after_end = _noop_async
        manager._resolve_camera_entity = lambda camera_entity: camera_entity
        manager._build_identity_context = lambda: ""
        manager._start_reolink_audio = _fake_start_reolink_audio

        await manager.async_start_session()

        assert manager._active is True
        assert manager._startup_media_pending is True
        assert manager._startup_media_camera_entity == "camera.entree"
        assert manager._starting is False
        assert call_order[0] == "audio:output_only=True,input_only=False,skip_talk_monitor=True"
        assert call_order[1].startswith("connect:[SYSTEM] A visitor just rang the doorbell.")

    asyncio.run(_run())


def test_start_session_media_enables_reolink_input_after_greeting() -> None:
    async def _run() -> None:
        manager = JeevesSessionManager.__new__(JeevesSessionManager)
        manager._active = True
        manager._client = SimpleNamespace(connected=True)
        manager._vision_task = None
        manager._monitor_task = None

        calls: list[tuple[bool, bool]] = []

        async def _fake_start_reolink_audio_background(
            _config: dict[str, Any],
            *,
            output_only: bool = False,
            input_only: bool = False,
        ) -> None:
            calls.append((output_only, input_only))

        async def _fake_vision_loop(_camera_entity: str, _fps: float) -> None:
            return None

        async def _fake_monitor_loop() -> None:
            return None

        manager._start_reolink_audio_background = _fake_start_reolink_audio_background
        manager._vision_loop = _fake_vision_loop
        manager._proactive_monitor_loop = _fake_monitor_loop

        await manager._start_session_media(
            "camera.entree",
            {
                CONF_AUDIO_MODE: AUDIO_MODE_REOLINK,
                CONF_CAMERA_ENTITY: "camera.entree",
                CONF_VISION_FPS: 2.5,
            },
        )

        assert calls == [(False, True)]
        assert manager._vision_task is not None
        assert manager._monitor_task is not None

        if manager._vision_task and not manager._vision_task.done():
            await manager._vision_task
        if manager._monitor_task and not manager._monitor_task.done():
            await manager._monitor_task

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
