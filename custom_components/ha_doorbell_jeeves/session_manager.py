"""Session manager – orchestrates client, vision, security, audio, and dual-model routing."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .client_base import BaseRealtimeClient
from .const import (
    AUDIO_MODE_MANUAL,
    AUDIO_MODE_REOLINK,
    CONF_API_BASE_URL,
    CONF_API_KEY,
    CONF_AUDIO_MANUAL_MODE,
    CONF_AUDIO_MODE,
    CONF_AUDIO_OUTPUT_MODE,
    CONF_CAMERA_ENTITY,
    CONF_CHIME_DELAY,
    CONF_DUAL_MODEL_ENABLED,
    CONF_FACE_SENSOR_ENTITY,
    CONF_FRAME_MAX_HEIGHT,
    CONF_FRAME_MAX_WIDTH,
    CONF_FRAME_QUALITY,
    CONF_GO2RTC_INPUT_STREAM_NAME,
    CONF_GO2RTC_OUTPUT_STREAM_NAME,
    CONF_GO2RTC_STREAM_NAME,
    CONF_IDENTITY_MODE,
    CONF_MEDIA_PLAYER_ENTITY,
    CONF_MEMORY_RETENTION_DAYS,
    CONF_MICROPHONE_ENTITY,
    CONF_MODEL,
    CONF_PROVIDER,
    CONF_REOLINK_ENTRY_ID,
    CONF_SESSION_TIMEOUT,
    CONF_SILENCE_TIMEOUT,
    CONF_STOP_ENTITIES,
    CONF_STOP_ENTITY_STATES,
    CONF_STOP_EVENTS,
    CONF_SYSTEM_PROMPT,
    CONF_TAKEOVER_AUDIO_ENERGY,
    CONF_TAKEOVER_ENERGY_THRESHOLD,
    CONF_TAKEOVER_POLL_INTERVAL,
    CONF_TAKEOVER_REOLINK_API,
    CONF_TOOL_API_KEY,
    CONF_TOOL_BASE_URL,
    CONF_TOOL_MODEL,
    CONF_TOOL_PROVIDER,
    CONF_VISION_FPS,
    CONF_VOICE,
    DEFAULT_CHIME_DELAY,
    DEFAULT_FRAME_MAX_HEIGHT,
    DEFAULT_FRAME_MAX_WIDTH,
    DEFAULT_FRAME_QUALITY,
    DEFAULT_MEMORY_RETENTION_DAYS,
    DEFAULT_MODEL_GEMINI,
    DEFAULT_SESSION_TIMEOUT,
    DEFAULT_SILENCE_TIMEOUT,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TOOL_MODEL_GEMINI,
    DEFAULT_TOOL_MODEL_OPENAI,
    DEFAULT_VISION_FPS,
    DEFAULT_VOICE_GEMINI,
    DEFAULT_VOICE_OPENAI,
    EVENT_AUDIO_OUTPUT,
    EVENT_SESSION_ENDED,
    EVENT_SESSION_STARTED,
    EVENT_TOOL_CALL,
    IDENTITY_MODE_BOTH,
    IDENTITY_MODE_REFERENCE_IMAGES,
    IDENTITY_MODE_SENSOR,
    PROVIDER_GEMINI,
    SECURITY_MODE_AUTO,
    TOOL_GET_LLMVISION_EVENTS,
)
from .frame_processor import process_frame
from .memory import MemoryStore, SessionMemory
from .security import SecurityManager
from .store import DataStore
from .tools import build_gemini_tools, build_openai_tools, build_system_context, execute_tool_call

_LOGGER = logging.getLogger(__name__)


class JeevesSessionManager:
    """Orchestrates the full concierge session lifecycle."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._client: BaseRealtimeClient | None = None
        self._vision_task: asyncio.Task[None] | None = None
        self._timeout_task: asyncio.Task[None] | None = None
        self._silence_task: asyncio.Task[None] | None = None
        self._active = False
        self._starting = False
        self._config = dict(entry.data) | dict(entry.options)
        self._normalize_manual_audio_config()

        # Data store (entities, actions, identities)
        self.store = DataStore(hass, entry.entry_id)
        self._memory_store = MemoryStore(hass, entry.entry_id)
        self._security = SecurityManager(hass, self._config, self.store)

        # Audio handler (Reolink/go2rtc)
        self._audio_handler: Any = None  # ReolinkAudioHandler or None

        # Talk state monitor (detects human taking over via Reolink app)
        self._talk_monitor: Any = None  # ReolinkTalkMonitor or None

        # Audio interrupt detector (energy-based takeover detection)
        self._interrupt_detector: Any = None  # AudioInterruptDetector or None

        # Mic forwarder queue/task (decouples ffmpeg reads from API send latency)
        self._mic_queue: asyncio.Queue[bytes] | None = None
        self._mic_forward_task: asyncio.Task[None] | None = None

        # Tool router (dual-model mode)
        self._tool_router: Any = None  # ToolRouter or None

        # Auto-stop & auto-start listener handles
        self._stop_unsubs: list[CALLBACK_TYPE] = []
        self._start_unsubs: list[CALLBACK_TYPE] = []

        self._last_audio_activity: float = 0.0
        self._session_started_at: float = 0.0
        self._ai_speaking_clear_task: asyncio.Task[None] | None = None
        self._session_end_reason: str = "session ended"
        self._session_memory_saved = False
        self._session_start_snapshot: str = ""
        self._transcript_history: list[dict[str, str]] = []

    def _normalize_manual_audio_config(self) -> None:
        """Backfill split go2rtc stream keys from the legacy single stream key."""
        legacy_stream = str(self._config.get(CONF_GO2RTC_STREAM_NAME, "") or "").strip()
        input_stream = str(self._config.get(CONF_GO2RTC_INPUT_STREAM_NAME, "") or "").strip()
        output_stream = str(self._config.get(CONF_GO2RTC_OUTPUT_STREAM_NAME, "") or "").strip()

        if legacy_stream:
            if not input_stream:
                input_stream = legacy_stream
                self._config[CONF_GO2RTC_INPUT_STREAM_NAME] = input_stream
            if not output_stream:
                output_stream = legacy_stream
                self._config[CONF_GO2RTC_OUTPUT_STREAM_NAME] = output_stream

        if output_stream:
            self._config[CONF_GO2RTC_STREAM_NAME] = output_stream

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def security(self) -> SecurityManager:
        return self._security

    def get_memories(self, limit: int | None = None) -> list[SessionMemory]:
        """Return stored memories, newest first."""
        memories = sorted(
            self._memory_store.memories,
            key=lambda memory: memory.timestamp,
            reverse=True,
        )
        if limit is not None:
            return memories[: max(0, limit)]
        return memories

    def get_latest_memory(self) -> SessionMemory | None:
        """Return the most recent stored memory, if available."""
        memories = self.get_memories(limit=1)
        return memories[0] if memories else None

    @property
    def memory_retention_days(self) -> int:
        """Configured memory retention period."""
        return self._memory_store.retention_days

    async def async_initialize(self) -> None:
        """Load persistent data and register start triggers."""
        await self.store.async_load()
        await self._memory_store.async_load()
        await self._memory_store.async_set_retention_days(
            int(self._config.get(CONF_MEMORY_RETENTION_DAYS, DEFAULT_MEMORY_RETENTION_DAYS))
        )

        # Sync start triggers from config entry data into the store.
        # Config flow stores triggers in two possible keys:
        #   - "start_triggers_config" (from triggers step)
        #   - "doorbell_trigger_entity" (from Reolink auto-detection, legacy)
        triggers_config = self._config.get("start_triggers_config", [])

        # Fallback: Reolink auto-detected doorbell entity
        if not triggers_config:
            doorbell_entity = self._config.get("doorbell_trigger_entity", "")
            if doorbell_entity:
                triggers_config = [{"entity_id": doorbell_entity, "to_state": "on"}]
                _LOGGER.warning(
                    "Using auto-detected doorbell trigger: %s", doorbell_entity
                )

        if triggers_config:
            from .models import StartTrigger  # noqa: PLC0415
            triggers = [StartTrigger.from_dict(t) for t in triggers_config]
            if triggers != self.store.start_triggers:
                await self.store.async_set_start_triggers(triggers)
                _LOGGER.warning("Synced %d start trigger(s) from config", len(triggers))
        elif not self.store.start_triggers:
            _LOGGER.warning(
                "No start triggers configured. "
                "Go to integration options → Triggers to set up doorbell auto-start."
            )

        self._register_start_triggers()
        _LOGGER.warning(
            "Registered %d start trigger(s): %s",
            len(self.store.start_triggers),
            [t.entity_id for t in self.store.start_triggers],
        )

    async def _lazy_setup_reolink(self) -> None:
        """Set up go2rtc stream for Reolink (deferred from boot for timing)."""
        from .reolink_audio import auto_configure_reolink  # noqa: PLC0415

        config = self._config
        reolink_entry_id = config.get(CONF_REOLINK_ENTRY_ID, "")
        camera_entity = config.get(CONF_CAMERA_ENTITY, "")
        if not reolink_entry_id and not camera_entity:
            return
        result = await auto_configure_reolink(
            self.hass,
            camera_entity_id=camera_entity,
            reolink_entry_id=reolink_entry_id or None,
        )
        if result:
            _LOGGER.info("Reolink go2rtc configured: stream=%s", result.get("stream_name"))
            new_options = dict(self.entry.options)
            new_options[CONF_GO2RTC_STREAM_NAME] = result["stream_name"]
            new_options[CONF_GO2RTC_INPUT_STREAM_NAME] = result["stream_name"]
            new_options[CONF_GO2RTC_OUTPUT_STREAM_NAME] = result["stream_name"]
            self.hass.config_entries.async_update_entry(self.entry, options=new_options)
            self._config[CONF_GO2RTC_STREAM_NAME] = result["stream_name"]
            self._config[CONF_GO2RTC_INPUT_STREAM_NAME] = result["stream_name"]
            self._config[CONF_GO2RTC_OUTPUT_STREAM_NAME] = result["stream_name"]
        else:
            _LOGGER.warning("go2rtc not available — 2-way audio may not work")

    async def _safe_start_session(self) -> None:
        """Wrapper for async_start_session that catches and logs all errors."""
        try:
            await self.async_start_session()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Failed to start session from trigger")

    async def async_start_session(self) -> None:
        """Start the AI concierge session."""
        if self._active or self._starting:
            _LOGGER.warning("Session already active or starting — ignoring")
            return

        self._starting = True
        try:
            _LOGGER.warning("async_start_session: beginning startup sequence")

            # Capture visitor snapshot immediately (camera is most likely available now)
            self._session_start_snapshot = await self._capture_memory_snapshot()
            if self._session_start_snapshot:
                _LOGGER.debug("Captured session start snapshot (%d bytes)", len(self._session_start_snapshot))

            # Lazy Reolink go2rtc setup (deferred from entry load for timing)
            if getattr(self, "reolink_needs_setup", False):
                await self._lazy_setup_reolink()
                self.reolink_needs_setup = False

            config = self._config
            provider = config.get(CONF_PROVIDER, PROVIDER_GEMINI)
            api_key: str = config[CONF_API_KEY]
            model: str = config.get(CONF_MODEL, DEFAULT_MODEL_GEMINI)
            _INVALID_MODELS = {"gemini-2.5-flash-native-audio-dialog"}
            if model in _INVALID_MODELS:
                model = DEFAULT_MODEL_GEMINI
                _LOGGER.warning("Replaced invalid model %s → %s", config.get(CONF_MODEL), model)
            dual_model = config.get(CONF_DUAL_MODEL_ENABLED, False)
            model_lacks_native_tools = (
                provider == PROVIDER_GEMINI
                and (
                    model.startswith("gemini-2.5-flash-native-audio")
                    or model == "gemini-2.5-flash-native-audio-dialog"
                )
            )
            if model_lacks_native_tools and not dual_model:
                _LOGGER.warning(
                    "Model %s does not support native tool calling; enabling dual-model routing",
                    model,
                )
                dual_model = True

            _LOGGER.warning(
                "Session config: provider=%s, model=%s, audio_mode=%s, camera=%s",
                provider,
                model,
                config.get(CONF_AUDIO_MODE),
                config.get(CONF_CAMERA_ENTITY),
            )

            base_prompt: str = config.get(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT)
            entity_context = build_system_context(self.store, self.hass, config)
            identity_context = self._build_identity_context()
            full_prompt = base_prompt
            if entity_context:
                full_prompt += f"\n\n{entity_context}"
            if identity_context:
                full_prompt += f"\n\n{identity_context}"

            reference_images = self._get_reference_images()
            gemini_tools = await self.hass.async_add_executor_job(
                build_gemini_tools, self.store, config
            )
            openai_tools = build_openai_tools(self.store, config)

            if dual_model:
                await self._setup_tool_router(config, full_prompt, gemini_tools, openai_tools)
                voice_tools_gemini: list = []
                voice_tools_openai: list = []
                voice_prompt = full_prompt + (
                    "\n\n--- IMPORTANT: ACTION PROTOCOL ---\n"
                    "When you want to perform an action (turn on a light, unlock a door, check "
                    "a camera, send a notification, etc.), simply state what you are going to do "
                    "clearly in your speech. For example: 'Let me turn on the porch light for you' "
                    "or 'I'll check the back camera now'. A separate system will detect your intent "
                    "and execute the action. You will receive a system message with the result.\n"
                    "Do NOT attempt to call functions directly — just speak naturally about what "
                    "you're doing and the system will handle execution.\n"
                )
            else:
                voice_tools_gemini = gemini_tools
                voice_tools_openai = openai_tools
                voice_prompt = full_prompt

            if provider == PROVIDER_GEMINI:
                voice = config.get(CONF_VOICE, DEFAULT_VOICE_GEMINI)
                self._client = await self._create_gemini_client(
                    api_key, model, voice_prompt, voice, voice_tools_gemini, reference_images
                )
            else:
                voice = config.get(CONF_VOICE, DEFAULT_VOICE_OPENAI)
                self._client = await self._create_openai_client(
                    api_key,
                    model,
                    voice_prompt,
                    voice,
                    voice_tools_openai,
                    reference_images,
                    config,
                )

            self._security.start_session()

            _LOGGER.warning("Connecting to %s API...", provider)
            await self._client.connect()
            self._active = True
            self._session_started_at = time.time()
            self._session_end_reason = "conversation complete"
            self._session_memory_saved = False
            self._transcript_history = []
            self._touch_audio_activity()
            _LOGGER.warning("Connected! Session is now active.")

            if config.get(CONF_AUDIO_MODE) == AUDIO_MODE_REOLINK:
                _LOGGER.warning("Starting Reolink 2-way audio handler")
                await self._start_reolink_audio(config)
            else:
                _LOGGER.warning("Audio mode is '%s' (not reolink)", config.get(CONF_AUDIO_MODE))

            if self._tool_router:
                self._tool_router.start()

            fps = config.get(CONF_VISION_FPS, DEFAULT_VISION_FPS)
            camera_entity = config.get(CONF_CAMERA_ENTITY, "")
            if camera_entity:
                self._vision_task = asyncio.create_task(self._vision_loop(camera_entity, fps))

            timeout = config.get(CONF_SESSION_TIMEOUT, DEFAULT_SESSION_TIMEOUT)
            if timeout > 0:
                self._timeout_task = asyncio.create_task(self._session_timeout(timeout))

            silence_timeout = float(config.get(CONF_SILENCE_TIMEOUT, DEFAULT_SILENCE_TIMEOUT))
            if silence_timeout > 0:
                self._silence_task = asyncio.create_task(self._silence_watchdog(silence_timeout))

            self._register_stop_triggers()

            self.hass.bus.async_fire(EVENT_SESSION_STARTED, {"entry_id": self.entry.entry_id})
            _LOGGER.warning(
                "Session started (provider=%s, model=%s, fps=%.1f, dual_model=%s)",
                provider,
                model,
                fps,
                dual_model,
            )

            await self._send_initial_greeting()
        except Exception:
            self._starting = False
            await self._cleanup_after_end()
            raise
        finally:
            self._starting = False

    async def async_stop_session(self, reason: str = "session ended") -> None:
        """Tear down the active session."""
        if not self._active and not self._starting:
            return

        self._active = False
        self._starting = False
        self._session_end_reason = reason

        for unsub in self._stop_unsubs:
            unsub()
        self._stop_unsubs.clear()

        if self._tool_router:
            self._tool_router.stop()

        if self._talk_monitor:
            await self._talk_monitor.stop()
            self._talk_monitor = None

        if self._interrupt_detector:
            self._interrupt_detector.stop()
            self._interrupt_detector = None

        if self._audio_handler:
            await self._audio_handler.stop()
            self._audio_handler = None

        await self._stop_mic_forwarder()

        for task in (
            self._vision_task,
            self._timeout_task,
            self._silence_task,
            self._ai_speaking_clear_task,
        ):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._vision_task = None
        self._timeout_task = None
        self._silence_task = None
        self._ai_speaking_clear_task = None

        if self._client:
            await self._client.disconnect()
            self._client = None

        await self._store_session_memory(reason)
        self._session_start_snapshot = ""  # Free memory
        self.hass.bus.async_fire(EVENT_SESSION_ENDED, {"entry_id": self.entry.entry_id})
        _LOGGER.info("Session ended (audit entries: %d)", len(self._security.audit_log))

    async def async_send_audio(self, audio_base64: str) -> None:
        if not self._client or not self._active:
            return
        self._touch_audio_activity()
        await self._client.send_audio(base64.b64decode(audio_base64))

    # ─── Reolink Audio Integration ────────────────────────────────────────────

    async def _start_reolink_audio(self, config: dict) -> None:
        """Start the Reolink 2-way audio handler via go2rtc."""
        from .reolink_audio import (  # noqa: PLC0415
            AudioInterruptDetector,
            ReolinkAudioHandler,
            ReolinkTalkMonitor,
            get_reolink_config,
        )
        from .const import (  # noqa: PLC0415
            DEFAULT_TAKEOVER_ENERGY_THRESHOLD,
            DEFAULT_TAKEOVER_POLL_INTERVAL,
        )

        stream_name = config.get(CONF_GO2RTC_STREAM_NAME, "")
        if not stream_name:
            reolink_entry_id = config.get(CONF_REOLINK_ENTRY_ID, "")
            if reolink_entry_id:
                stream_name = f"jeeves_reolink_{reolink_entry_id.replace('-', '_')[:12]}"
                _LOGGER.warning(
                    "No explicit go2rtc stream — using Reolink entry stream: %s",
                    stream_name,
                )

        if not stream_name:
            camera = config.get(CONF_CAMERA_ENTITY, "")
            if camera:
                stream_name = camera
                _LOGGER.warning("No explicit go2rtc stream — using camera entity: %s", stream_name)
            else:
                _LOGGER.warning("No go2rtc stream configured — Reolink audio disabled")
                return

        # Shared takeover callback
        async def _on_human_takeover() -> None:
            """Human started talking — stop AI session."""
            _LOGGER.info("Human takeover detected — stopping AI session")
            await self.async_stop_session("human takeover")

        # Audio handler (always needed for Reolink mode)
        self._mic_queue = asyncio.Queue(maxsize=256)
        self._mic_forward_task = self.hass.async_create_task(self._mic_forward_loop())
        mic_rx_count = 0
        dropped_count = 0

        async def _on_doorbell_audio(audio_bytes: bytes) -> None:
            """Forward audio from doorbell mic to AI client."""
            nonlocal mic_rx_count, dropped_count
            mic_rx_count += 1
            self._touch_audio_activity()
            if self._interrupt_detector:
                self._interrupt_detector.process_audio_frame(audio_bytes)
            if mic_rx_count == 1:
                _LOGGER.warning("✓ First microphone chunk received from doorbell (%d bytes)", len(audio_bytes))
            elif mic_rx_count % 500 == 0:
                _LOGGER.info("Microphone input received: %d chunks", mic_rx_count)

            if self._mic_queue is None:
                return

            try:
                self._mic_queue.put_nowait(audio_bytes)
            except asyncio.QueueFull:
                # Keep stream real-time by dropping oldest queued audio.
                try:
                    _ = self._mic_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._mic_queue.put_nowait(audio_bytes)
                dropped_count += 1
                if dropped_count <= 3 or dropped_count % 100 == 0:
                    _LOGGER.warning("Mic queue overflow: dropped %d chunk(s)", dropped_count)

        camera_entity = config.get(CONF_CAMERA_ENTITY, "")
        self._audio_handler = ReolinkAudioHandler(
            self.hass, stream_name, on_audio_received=_on_doorbell_audio
        )
        self._audio_handler._camera_entity_id = camera_entity
        self._audio_handler._reolink_entry_id = config.get(CONF_REOLINK_ENTRY_ID, "")
        self._audio_handler._chime_delay = float(
            config.get(CONF_CHIME_DELAY, DEFAULT_CHIME_DELAY)
        )
        await self._audio_handler.start()
        _LOGGER.info("Reolink audio handler active (stream=%s)", stream_name)

        # --- Human Takeover Detection (both methods can run simultaneously) ---

        # Method 1: Reolink API polling (GetTalkState)
        use_reolink_api = config.get(CONF_TAKEOVER_REOLINK_API, True)
        if use_reolink_api:
            reolink_entry_id = config.get(CONF_REOLINK_ENTRY_ID, "")
            if reolink_entry_id:
                reolink_config = get_reolink_config(self.hass, reolink_entry_id)
                if reolink_config and reolink_config.get("host"):
                    poll_interval = config.get(
                        CONF_TAKEOVER_POLL_INTERVAL, DEFAULT_TAKEOVER_POLL_INTERVAL
                    )
                    self._talk_monitor = ReolinkTalkMonitor(
                        hass=self.hass,
                        host=reolink_config["host"],
                        username=reolink_config["username"],
                        password=reolink_config["password"],
                        on_human_takeover=_on_human_takeover,
                        poll_interval=poll_interval,
                    )
                    await self._talk_monitor.start()
                    _LOGGER.info("Reolink talk monitor active (poll=%.1fs)", poll_interval)

        # Method 2: Audio energy detection
        use_audio_energy = config.get(CONF_TAKEOVER_AUDIO_ENERGY, False)
        if use_audio_energy:
            threshold = config.get(
                CONF_TAKEOVER_ENERGY_THRESHOLD, DEFAULT_TAKEOVER_ENERGY_THRESHOLD
            )
            self._interrupt_detector = AudioInterruptDetector(
                on_interrupt=_on_human_takeover,
                energy_threshold=threshold,
            )
            self._interrupt_detector.start()
            _LOGGER.info("Audio energy interrupt detector active (threshold=%d)", threshold)

    # ─── Dual-Model Tool Router Setup ─────────────────────────────────────────

    async def _setup_tool_router(
        self,
        config: dict,
        full_prompt: str,
        gemini_tools: list,
        openai_tools: list,
    ) -> None:
        """Initialize the tool router for dual-model mode."""
        from .tool_router import ToolRouter  # noqa: PLC0415

        # Tool model config (defaults to same provider/key as voice model)
        tool_provider = config.get(CONF_TOOL_PROVIDER) or config.get(CONF_PROVIDER, PROVIDER_GEMINI)
        voice_api_key = config.get(CONF_API_KEY, "")
        tool_api_key = config.get(CONF_TOOL_API_KEY) or voice_api_key
        tool_base_url = config.get(CONF_TOOL_BASE_URL) or config.get(CONF_API_BASE_URL)

        if tool_provider == PROVIDER_GEMINI:
            tool_model = config.get(CONF_TOOL_MODEL) or DEFAULT_TOOL_MODEL_GEMINI
            tools = gemini_tools
        else:
            tool_model = config.get(CONF_TOOL_MODEL) or DEFAULT_TOOL_MODEL_OPENAI
            tools = openai_tools

        async def _inject_context(text: str) -> None:
            """Inject tool result text back into the voice session."""
            if self._client and self._active:
                await self._client.inject_context(text)

        self._tool_router = ToolRouter(
            provider=tool_provider,
            api_key=tool_api_key,
            model=tool_model,
            base_url=tool_base_url,
            system_prompt=full_prompt,
            tools=tools,
            on_tool_call=self._handle_tool_call,
            on_inject_context=_inject_context,
        )

    # ─── Start Triggers ───────────────────────────────────────────────────────

    def _register_start_triggers(self) -> None:
        """Register entity listeners that auto-start the session."""
        for trigger in self.store.start_triggers:
            entity_id = trigger.entity_id
            to_state = trigger.to_state
            from_state = trigger.from_state

            @callback
            def _on_start_trigger(event: Event, _to: str = to_state, _from: str = from_state) -> None:
                new_state = event.data.get("new_state")
                old_state = event.data.get("old_state")
                _LOGGER.warning(
                    "Trigger event: entity=%s, new=%s, old=%s, expected_to=%s",
                    event.data.get("entity_id"),
                    new_state.state if new_state else None,
                    old_state.state if old_state else None,
                    _to,
                )
                if new_state is None:
                    return
                if new_state.state != _to:
                    return
                if _from and old_state and old_state.state != _from:
                    return
                if not self._active and not self._starting:
                    _LOGGER.warning("Start trigger FIRED → starting session")
                    self.hass.async_create_task(self._safe_start_session())
                else:
                    _LOGGER.warning("Start trigger matched but session already active/starting")

            unsub = async_track_state_change_event(self.hass, [entity_id], _on_start_trigger)
            self._start_unsubs.append(unsub)

    def unregister_start_triggers(self) -> None:
        """Remove start trigger listeners (called on unload)."""
        for unsub in self._start_unsubs:
            unsub()
        self._start_unsubs.clear()

    # ─── Stop Triggers ────────────────────────────────────────────────────────

    def _register_stop_triggers(self) -> None:
        config = self._config
        stop_entities = config.get(CONF_STOP_ENTITIES, [])
        stop_states = config.get(CONF_STOP_ENTITY_STATES, {})

        if stop_entities:
            @callback
            def _on_stop(event: Event) -> None:
                entity_id = event.data.get("entity_id", "")
                new_state = event.data.get("new_state")
                if new_state is None:
                    return
                target = stop_states.get(entity_id)
                if target:
                    if new_state.state == target:
                        _LOGGER.info("Auto-stop: %s → %s", entity_id, target)
                        self.hass.async_create_task(self.async_stop_session(f"stop trigger: {entity_id}"))
                else:
                    _LOGGER.info("Auto-stop: %s changed", entity_id)
                    self.hass.async_create_task(self.async_stop_session(f"stop trigger: {entity_id}"))

            unsub = async_track_state_change_event(self.hass, stop_entities, _on_stop)
            self._stop_unsubs.append(unsub)

        for event_type in config.get(CONF_STOP_EVENTS, []):
            @callback
            def _on_event(event: Event, evt: str = event_type) -> None:
                _LOGGER.info("Auto-stop: event '%s'", evt)
                self.hass.async_create_task(self.async_stop_session(f"stop event: {evt}"))
            unsub = self.hass.bus.async_listen(event_type, _on_event)
            self._stop_unsubs.append(unsub)

    # ─── Vision Loop ──────────────────────────────────────────────────────────

    async def _vision_loop(self, camera_entity: str, fps: float) -> None:
        interval = 1.0 / max(fps, 0.1)
        max_w = self._config.get(CONF_FRAME_MAX_WIDTH, DEFAULT_FRAME_MAX_WIDTH)
        max_h = self._config.get(CONF_FRAME_MAX_HEIGHT, DEFAULT_FRAME_MAX_HEIGHT)
        quality = self._config.get(CONF_FRAME_QUALITY, DEFAULT_FRAME_QUALITY)

        while self._active:
            try:
                image = await self.hass.components.camera.async_get_image(camera_entity, timeout=5)
                if image and self._client:
                    processed = await self.hass.async_add_executor_job(
                        process_frame, image.content, max_w, max_h, quality,
                    )
                    frame_b64 = base64.b64encode(processed).decode("ascii")
                    await self._client.send_image(frame_b64, mime_type="image/jpeg")
            except asyncio.CancelledError:
                break
            except Exception:
                _LOGGER.debug("Vision frame failed", exc_info=True)
            await asyncio.sleep(interval)

    async def _session_timeout(self, timeout_seconds: int) -> None:
        try:
            await asyncio.sleep(timeout_seconds)
            if self._active:
                _LOGGER.info("Session timeout (%ds)", timeout_seconds)
                await self.async_stop_session(f"session timeout ({timeout_seconds}s)")
        except asyncio.CancelledError:
            pass

    async def _silence_watchdog(self, timeout: float = DEFAULT_SILENCE_TIMEOUT) -> None:
        """End the session if no audio activity occurs for too long."""
        try:
            while self._active:
                await asyncio.sleep(5)
                if not self._active:
                    break
                now = asyncio.get_running_loop().time()
                if self._last_audio_activity and (now - self._last_audio_activity) > timeout:
                    _LOGGER.warning("Silence timeout (%.0fs) — ending session", timeout)
                    await self.async_stop_session(f"silence timeout ({timeout:.0f}s)")
                    break
        except asyncio.CancelledError:
            pass

    def _touch_audio_activity(self) -> None:
        """Mark the current time as the latest audio activity."""
        self._last_audio_activity = asyncio.get_running_loop().time()

    # ─── Identity Context ─────────────────────────────────────────────────────

    def _build_identity_context(self) -> str:
        mode = self._config.get(CONF_IDENTITY_MODE, "none")
        parts: list[str] = []

        if mode in (IDENTITY_MODE_SENSOR, IDENTITY_MODE_BOTH):
            sensor_id = self._config.get(CONF_FACE_SENSOR_ENTITY)
            if sensor_id:
                state = self.hass.states.get(sensor_id)
                if state and state.state not in ("unknown", "unavailable", ""):
                    parts.append(f"[IDENTITY SENSOR: Current visitor identified as '{state.state}']")

        if mode in (IDENTITY_MODE_REFERENCE_IMAGES, IDENTITY_MODE_BOTH):
            if self.store.known_identities:
                lines = ["[KNOWN IDENTITIES:]"]
                for ident in self.store.known_identities:
                    lines.append(
                        f"- {ident.name} ({ident.identity_type}): {ident.description} "
                        f"[{ident.relationship}, access: {ident.access_level}]"
                    )
                parts.append("\n".join(lines))

        return "\n\n".join(parts)

    def _get_reference_images(self) -> list[dict[str, str]]:
        mode = self._config.get(CONF_IDENTITY_MODE, "none")
        if mode not in (IDENTITY_MODE_REFERENCE_IMAGES, IDENTITY_MODE_BOTH):
            return []
        images = []
        for ident in self.store.known_identities:
            if ident.image_base64:
                images.append({
                    "image_base64": ident.image_base64,
                    "caption": f"{ident.name} ({ident.relationship}): {ident.description}",
                })
        return images

    # ─── Client Factories ─────────────────────────────────────────────────────

    async def _create_gemini_client(self, api_key: str, model: str, prompt: str,
                                     voice: str, tools: list, reference_images: list) -> BaseRealtimeClient:
        from .gemini_client import GeminiLiveClient  # noqa: PLC0415
        return GeminiLiveClient(
            api_key=api_key, model=model, system_prompt=prompt, tools=tools,
            voice=voice, reference_images=reference_images,
            on_audio_output=self._handle_audio_output,
            on_tool_call=self._handle_tool_call,
            on_session_end=self._handle_session_end,
            on_transcript=self._handle_transcript,
        )

    async def _create_openai_client(self, api_key: str, model: str, prompt: str,
                                     voice: str, tools: list, reference_images: list,
                                     config: dict) -> BaseRealtimeClient:
        from .openai_client import OpenAIRealtimeClient  # noqa: PLC0415
        return OpenAIRealtimeClient(
            api_key=api_key, model=model, base_url=config.get(CONF_API_BASE_URL) or None,
            system_prompt=prompt, tools=tools, voice=voice,
            reference_images=reference_images,
            on_audio_output=self._handle_audio_output,
            on_tool_call=self._handle_tool_call,
            on_session_end=self._handle_session_end,
            on_transcript=self._handle_transcript,
        )

    # ─── Callbacks ────────────────────────────────────────────────────────────

    async def _send_initial_greeting(self) -> None:
        """Send an initial trigger message so the AI starts speaking immediately."""
        if not self._client:
            return
        trigger_msg = (
            "[SYSTEM] A visitor just rang the doorbell. "
            "Greet them now according to your instructions. "
            "If you can see them in the camera feed, describe or acknowledge them naturally."
        )
        try:
            await self._client.inject_context(trigger_msg)
            _LOGGER.warning("Sent initial greeting trigger to AI model")
        except Exception:
            _LOGGER.exception("Failed to send initial greeting trigger")

    def _handle_audio_output(self, audio_bytes: bytes) -> None:
        """Handle audio output from the AI model."""
        self._touch_audio_activity()
        if not hasattr(self, "_audio_out_count"):
            self._audio_out_count = 0
        self._audio_out_count += 1
        if self._audio_out_count <= 3:
            _LOGGER.warning(
                "Audio output from AI: %d bytes (chunk #%d)",
                len(audio_bytes),
                self._audio_out_count,
            )
        if self._interrupt_detector:
            self._interrupt_detector.set_ai_speaking(True)
            if self._ai_speaking_clear_task and not self._ai_speaking_clear_task.done():
                self._ai_speaking_clear_task.cancel()
            self._ai_speaking_clear_task = self.hass.async_create_task(
                self._clear_ai_speaking_flag()
            )
        if self._audio_handler and self._audio_handler.is_active:
            self.hass.async_create_task(self._audio_handler.send_audio(audio_bytes))
        else:
            audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
            speaker_entity = self._config.get(CONF_MEDIA_PLAYER_ENTITY, "")
            go2rtc_output_stream = self._config.get(CONF_GO2RTC_OUTPUT_STREAM_NAME) or self._config.get(
                CONF_GO2RTC_STREAM_NAME, ""
            )
            go2rtc_input_stream = self._config.get(CONF_GO2RTC_INPUT_STREAM_NAME) or self._config.get(
                CONF_GO2RTC_STREAM_NAME, ""
            )
            self.hass.bus.async_fire(
                EVENT_AUDIO_OUTPUT,
                {
                    "entry_id": self.entry.entry_id,
                    "audio_base64": audio_b64,
                    "media_player": speaker_entity,
                    "speaker_entity": speaker_entity,
                    "microphone_entity": self._config.get(CONF_MICROPHONE_ENTITY, ""),
                    "audio_mode": self._config.get(CONF_AUDIO_MODE, AUDIO_MODE_MANUAL),
                    "manual_audio_mode": self._config.get(CONF_AUDIO_MANUAL_MODE, ""),
                    "audio_output_mode": self._config.get(CONF_AUDIO_OUTPUT_MODE, ""),
                    "go2rtc_stream_name": go2rtc_output_stream,
                    "go2rtc_input_stream_name": go2rtc_input_stream,
                    "go2rtc_output_stream_name": go2rtc_output_stream,
                },
            )

    async def _clear_ai_speaking_flag(self) -> None:
        """Clear the AI-speaking flag shortly after output stops."""
        try:
            await asyncio.sleep(1.0)
            if self._interrupt_detector:
                self._interrupt_detector.set_ai_speaking(False)
        except asyncio.CancelledError:
            pass

    async def _handle_tool_call(self, function_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._security.log_event("tool_call_request", function_name, {"arguments": arguments})
        self.hass.bus.async_fire(
            EVENT_TOOL_CALL,
            {
                "entry_id": self.entry.entry_id,
                "function": function_name,
                "arguments": arguments,
            },
        )

        if function_name == "end_conversation":
            reason = arguments.get("reason", "conversation complete") or "conversation complete"
            self.hass.async_create_task(self.async_stop_session(reason))
            return {"success": True, "message": f"Ending conversation: {reason}"}

        if function_name == "recall_memories":
            return await self._recall_memories(arguments)

        if function_name == "verify_pin":
            pin = arguments.get("pin", "")
            verified = self._security.check_pin(pin)
            return {
                "success": verified,
                "message": "PIN verified." if verified else "Incorrect PIN.",
            }

        read_only_tools = (
            "view_camera",
            "get_calendar_events",
            "get_entity_history",
            "search_events",
            TOOL_GET_LLMVISION_EVENTS,
            "read_entity_state",
            "recall_memories",
        )
        if function_name not in read_only_tools and not function_name.startswith("notify_"):
            mode, _, _ = self._security.get_action_security(function_name)
            if mode != SECURITY_MODE_AUTO:
                conversation_summary = self._client.conversation_summary if self._client else ""
                claimed_identity = self._extract_claimed_identity(conversation_summary)

                current_frame: str | None = None
                reference_image: str | None = None
                _entity, action = self.store.get_action(function_name)
                if action and action.require_camera_feed:
                    current_frame = await self._security._get_camera_frame()
                if action and action.require_visual_match:
                    reference_image = self._get_reference_for_identity(claimed_identity)

                approved, reason = await self._security.evaluate_action(
                    action_id=function_name,
                    arguments=arguments,
                    conversation_summary=conversation_summary,
                    claimed_identity=claimed_identity,
                    current_frame_b64=current_frame,
                    reference_image_b64=reference_image,
                )
                if not approved:
                    _LOGGER.warning("BLOCKED %s: %s", function_name, reason)
                    return {
                        "error": f"Action blocked: {reason}",
                        "instruction": "Inform the visitor this action cannot be completed.",
                    }

        result = await execute_tool_call(
            self.hass,
            self.store,
            function_name,
            arguments,
            self._config,
        )

        if result.get("_image_base64") and self._client:
            await self._client.send_image(
                result["_image_base64"],
                mime_type=result.get("_image_mime", "image/jpeg"),
            )
            result.pop("_image_base64", None)
            result.pop("_image_mime", None)

        if result.get("success") and function_name not in read_only_tools:
            self._security.record_action(function_name)
        return result

    def _handle_session_end(self) -> None:
        _LOGGER.warning("AI session ended — cleaning up")
        self._active = False
        self._starting = True
        self._session_end_reason = self._session_end_reason or "AI session ended unexpectedly"
        if self._session_end_reason in {"session ended", "conversation complete"}:
            self._session_end_reason = "AI session ended unexpectedly"
        self.hass.async_create_task(self._cleanup_after_end())
        self.hass.bus.async_fire(EVENT_SESSION_ENDED, {"entry_id": self.entry.entry_id})

    async def _cleanup_after_end(self) -> None:
        """Clean up resources after an unexpected session end."""
        try:
            for unsub in self._stop_unsubs:
                unsub()
            self._stop_unsubs.clear()

            if self._tool_router:
                self._tool_router.stop()
            if self._talk_monitor:
                await self._talk_monitor.stop()
                self._talk_monitor = None
            if self._interrupt_detector:
                self._interrupt_detector.stop()
                self._interrupt_detector = None
            if self._audio_handler:
                await self._audio_handler.stop()
                self._audio_handler = None
            await self._stop_mic_forwarder()
            for task in (
                self._vision_task,
                self._timeout_task,
                self._silence_task,
                self._ai_speaking_clear_task,
            ):
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            self._vision_task = None
            self._timeout_task = None
            self._silence_task = None
            self._ai_speaking_clear_task = None
            if self._client:
                try:
                    await self._client.disconnect()
                except Exception:
                    _LOGGER.debug("Client disconnect during cleanup failed", exc_info=True)
                self._client = None
            await self._store_session_memory(
                self._session_end_reason or "AI session ended unexpectedly"
            )
        finally:
            self._starting = False

    async def _mic_forward_loop(self) -> None:
        """Forward queued microphone PCM chunks to the active realtime client."""
        forwarded = 0
        try:
            while self._active:
                if self._mic_queue is None:
                    await asyncio.sleep(0.05)
                    continue

                audio_bytes = await self._mic_queue.get()
                if not audio_bytes:
                    continue

                if self._client and self._active:
                    try:
                        await self._client.send_audio(audio_bytes)
                        forwarded += 1
                        if forwarded == 1:
                            _LOGGER.warning("✓ First microphone chunk forwarded to AI session")
                        elif forwarded % 500 == 0:
                            _LOGGER.info("Microphone chunks forwarded to AI: %d", forwarded)
                    except Exception:
                        _LOGGER.exception("Failed to forward microphone chunk to AI")
        except asyncio.CancelledError:
            pass
        finally:
            _LOGGER.info("Mic forward loop stopped (forwarded=%d)", forwarded)

    async def _stop_mic_forwarder(self) -> None:
        """Stop mic queue forwarder task and clear pending chunks."""
        if self._mic_forward_task and not self._mic_forward_task.done():
            self._mic_forward_task.cancel()
            try:
                await self._mic_forward_task
            except asyncio.CancelledError:
                pass
        self._mic_forward_task = None
        self._mic_queue = None

    def _handle_transcript(self, role: str, text: str) -> None:
        self._security.log_event("transcript", f"{role}_speech", {"text": text[:500]})
        if text and (
            not self._transcript_history
            or self._transcript_history[-1]["role"] != role
            or self._transcript_history[-1]["text"] != text
        ):
            self._transcript_history.append({"role": role, "text": text})
        if self._tool_router and self._tool_router.is_active:
            self._tool_router.add_transcript(role, text)

    async def _recall_memories(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Search stored doorbell memories for prior context."""
        query = (arguments.get("query") or "").strip()
        hours_back = max(1, int(arguments.get("hours_back", 72) or 72))
        memories = self._memory_store.get_recent_memories(hours_back)
        if query:
            query_lower = query.lower()
            memories = [
                memory
                for memory in memories
                if query_lower in memory.summary.lower()
                or query_lower in memory.visitor_description.lower()
                or query_lower in memory.visitor_name.lower()
            ]
        memories = sorted(memories, key=lambda memory: memory.timestamp, reverse=True)[:5]
        return {
            "success": True,
            "count": len(memories),
            "memories": [memory.to_dict() for memory in memories],
            "message": self._summarize_memories(memories, query, hours_back),
        }

    def _summarize_memories(
        self, memories: list[SessionMemory], query: str, hours_back: int
    ) -> str:
        if not memories:
            if query:
                return f"No matching doorbell memories found for '{query}' in the last {hours_back} hours."
            return f"No doorbell memories found in the last {hours_back} hours."
        lines = []
        for memory in memories:
            visitor = memory.visitor_name or memory.visitor_description or "unknown visitor"
            lines.append(f"- {visitor}: {memory.summary} [{memory.outcome}]")
        return "Recent memories:\n" + "\n".join(lines)

    async def _store_session_memory(self, outcome: str) -> None:
        """Generate and store a recap of the finished session."""
        if self._session_memory_saved or not self._session_started_at:
            return
        timestamp = time.time()
        duration_seconds = max(0.0, timestamp - self._session_started_at)
        # Prefer the snapshot captured at session start (camera was definitely available)
        # Fall back to attempting a fresh capture at session end
        snapshot_b64 = self._session_start_snapshot or await self._capture_memory_snapshot()
        recap = await self._generate_session_recap(outcome, snapshot_b64)
        memory = SessionMemory(
            timestamp=timestamp,
            duration_seconds=duration_seconds,
            visitor_description=recap.get("visitor_description", "Unknown visitor"),
            summary=recap.get("summary", "Doorbell session ended."),
            outcome=recap.get("outcome", outcome),
            photo_base64=snapshot_b64 or "",
            visitor_name=recap.get("visitor_name", ""),
        )
        await self._memory_store.async_add_memory(memory)
        self._session_memory_saved = True

    async def _capture_memory_snapshot(self) -> str:
        """Capture a camera image to attach to the stored memory."""
        camera_entity = self._config.get(CONF_CAMERA_ENTITY, "")
        if not camera_entity:
            _LOGGER.warning("No camera entity configured for memory snapshot")
            return ""
        try:
            image = await self.hass.components.camera.async_get_image(camera_entity, timeout=5)
            if image and image.content:
                b64 = base64.b64encode(image.content).decode("ascii")
                _LOGGER.info(
                    "Memory snapshot captured from %s (%d bytes)",
                    camera_entity,
                    len(image.content),
                )
                return b64
            _LOGGER.warning("Camera %s returned empty image", camera_entity)
        except Exception:
            _LOGGER.warning("Failed to capture memory snapshot from %s", camera_entity, exc_info=True)
        return ""

    async def _generate_session_recap(
        self, outcome: str, snapshot_b64: str
    ) -> dict[str, str]:
        """Use Gemini to summarize the session, with a safe fallback."""
        transcript_text = self._conversation_text()
        provider = self._config.get(CONF_PROVIDER, PROVIDER_GEMINI)
        tool_provider = self._config.get(CONF_TOOL_PROVIDER) or provider
        voice_api_key = self._config.get(CONF_API_KEY, "")
        api_key = ""
        if tool_provider == PROVIDER_GEMINI:
            api_key = self._config.get(CONF_TOOL_API_KEY) or voice_api_key
        elif provider == PROVIDER_GEMINI:
            api_key = voice_api_key
        if not api_key:
            return self._fallback_session_recap(outcome)

        try:
            from google import genai  # noqa: PLC0415
            from google.genai import types  # noqa: PLC0415

            model = (
                self._config.get(CONF_TOOL_MODEL) or DEFAULT_TOOL_MODEL_GEMINI
                if tool_provider == PROVIDER_GEMINI
                else DEFAULT_TOOL_MODEL_GEMINI
            )
            client = await asyncio.to_thread(genai.Client, api_key=api_key)
            parts: list[Any] = [
                types.Part(
                    text=(
                        "Summarize this completed Home Assistant doorbell conversation as JSON. "
                        "Return keys: visitor_description, summary, outcome, visitor_name. "
                        f"The session ended with outcome: {outcome}.\n\n"
                        f"Transcript:\n{transcript_text or '[no transcript captured]'}"
                    )
                )
            ]
            if snapshot_b64:
                parts.append(
                    types.Part(
                        inline_data=types.Blob(
                            data=base64.b64decode(snapshot_b64), mime_type="image/jpeg"
                        )
                    )
                )
                parts.append(types.Part(text="[Visitor snapshot at end of session]"))
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model,
                contents=[types.Content(role="user", parts=parts)],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )
            recap = json.loads((response.text or "{}").strip())
            return {
                "visitor_description": recap.get("visitor_description", "Unknown visitor"),
                "summary": recap.get("summary", "Doorbell session ended."),
                "outcome": recap.get("outcome", outcome),
                "visitor_name": recap.get("visitor_name", ""),
            }
        except Exception:
            _LOGGER.exception("Failed to generate Gemini session recap")
            return self._fallback_session_recap(outcome)

    def _fallback_session_recap(self, outcome: str) -> dict[str, str]:
        """Fallback recap if Gemini summarization is unavailable."""
        visitor_name = self._extract_claimed_identity(self._conversation_text())
        if visitor_name == "unknown":
            visitor_name = ""
        if self._transcript_history:
            summary = " ".join(turn["text"] for turn in self._transcript_history[-4:])[:400]
        else:
            summary = "Brief doorbell interaction with no transcript captured."
        return {
            "visitor_description": visitor_name or "Unknown visitor",
            "summary": summary,
            "outcome": outcome,
            "visitor_name": visitor_name,
        }

    def _conversation_text(self) -> str:
        """Serialize the transcript history for recap generation."""
        return "\n".join(
            f"{turn['role']}: {turn['text']}" for turn in self._transcript_history
        )

    def _extract_claimed_identity(self, conversation_summary: str) -> str:
        summary_lower = conversation_summary.lower()
        for ident in self.store.known_identities:
            if ident.name.lower() in summary_lower:
                return ident.name
        return "unknown"

    def _get_reference_for_identity(self, name: str) -> str | None:
        ident = self.store.get_identity(name)
        return ident.image_base64 if ident and ident.image_base64 else None
