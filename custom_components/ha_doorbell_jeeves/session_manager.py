"""Session manager – orchestrates client, vision, security, audio, and dual-model routing."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

from homeassistant.components.camera import async_get_image as ha_camera_get_image
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
    CONF_MAX_SESSION_TIMEOUT,
    CONF_MICROPHONE_ENTITY,
    CONF_MODEL,
    CONF_PROVIDER,
    CONF_REOLINK_ENTRY_ID,
    CONF_REOLINK_MIC_METHOD,
    CONF_REOLINK_MIC_URL,
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
    CONF_TEXT_MODEL,
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
    DEFAULT_MAX_SESSION_TIMEOUT,
    DEFAULT_MEMORY_RETENTION_DAYS,
    DEFAULT_MODEL_GEMINI,
    DEFAULT_SESSION_TIMEOUT,
    DEFAULT_SILENCE_TIMEOUT,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TEXT_MODEL_GEMINI,
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
from .events import EventStore
from .frame_processor import process_frame
from .memory import MemoryStore, SessionMemory
from .notifications import NotificationManager
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
        self._config["_entry_id"] = entry.entry_id
        self._normalize_manual_audio_config()

        # Data store (entities, actions, identities)
        self.store = DataStore(hass, entry.entry_id)
        self._memory_store = MemoryStore(hass, entry.entry_id)
        self._event_store = EventStore(hass, entry.entry_id)
        self._notification_manager = NotificationManager(hass)
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
        self._session_timeout_deadline: float = 0.0
        self._ai_speaking_clear_task: asyncio.Task[None] | None = None
        self._session_end_reason: str = "session ended"
        self._session_memory_saved = False
        self._session_start_snapshot: str = ""
        self._transcript_history: list[dict[str, str]] = []

        # Echo gate: suppress mic forwarding while AI is speaking to prevent
        # the doorbell mic from picking up the AI's own voice output.
        # Gate stays closed for ECHO_GATE_HOLD seconds after the last AI output
        # chunk is received (accounts for speaker buffer + physical echo).
        self._ai_last_output_time: float = 0.0
        self._echo_gate_hold_sec: float = 1.2  # hold mic mute briefly after AI output without eating replies

        # PTZ camera tracking: cameras moved during this session (auto-return on end)
        self._ptz_moved_cameras: set[str] = set()

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

    @property
    def event_store(self) -> EventStore:
        """Access the event store for dashboard feeds."""
        return self._event_store

    async def async_initialize(self) -> None:
        """Load persistent data and register start triggers."""
        await self.store.async_load()
        await self._memory_store.async_load()
        await self._event_store.async_load()
        await self._notification_manager.async_setup()
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

    async def async_shutdown(self) -> None:
        """Release background listeners and helpers during unload."""
        await self._notification_manager.async_teardown()

    async def _lazy_setup_reolink(self) -> None:
        """Verify Reolink connectivity (deferred from boot for timing).

        No longer registers its own go2rtc stream — the audio input pipeline
        now discovers the existing go2rtc stream set up by HA's Reolink integration.
        """
        config = self._config
        reolink_entry_id = config.get(CONF_REOLINK_ENTRY_ID, "")
        camera_entity = config.get(CONF_CAMERA_ENTITY, "")
        if not reolink_entry_id and not camera_entity:
            return
        _LOGGER.info(
            "Reolink mode: entry=%s, camera=%s (audio input uses existing go2rtc stream)",
            reolink_entry_id[:8] if reolink_entry_id else "none",
            camera_entity or "none",
        )

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

            # Lazy Reolink go2rtc setup (deferred from entry load for timing)
            if getattr(self, "reolink_needs_setup", False):
                await self._lazy_setup_reolink()
                self.reolink_needs_setup = False

            # Capture visitor snapshot immediately after go2rtc is ready
            # (camera HTTP API + go2rtc fallback both available now)
            self._session_start_snapshot = await self._capture_memory_snapshot()
            if self._session_start_snapshot:
                _LOGGER.info("Captured session start snapshot (%d bytes)", len(self._session_start_snapshot))

            config = self._config
            provider = config.get(CONF_PROVIDER, PROVIDER_GEMINI)
            api_key: str = config[CONF_API_KEY]
            model: str = config.get(CONF_MODEL, DEFAULT_MODEL_GEMINI)
            dual_model = config.get(CONF_DUAL_MODEL_ENABLED, False)

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

            # Add visual camera map if placements exist
            from .tools import render_camera_map_image  # noqa: PLC0415
            map_jpeg = await self.hass.async_add_executor_job(
                render_camera_map_image, self.store
            )
            if map_jpeg:
                reference_images.append({
                    "image_base64": base64.b64encode(map_jpeg).decode(),
                    "caption": (
                        "PROPERTY CAMERA MAP: This image shows the spatial layout of all cameras. "
                        "The grey rectangle is the house. Camera dots show positions, triangles show "
                        "viewing direction (FOV). Red dot = doorbell. Green dot = other cameras. "
                        "Use this to understand which camera sees which area."
                    ),
                })
                _LOGGER.info("Injected camera map image (%d bytes) into session context", len(map_jpeg))
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
                voice_prompt = full_prompt + (
                    "\n\n--- TOOL EXECUTION PROTOCOL ---\n"
                    "When you execute a tool/action (turning on lights, sending notifications, "
                    "checking cameras, unlocking doors, etc.):\n"
                    "1. NEVER announce the technical details of what you are doing. Do NOT say things "
                    "like 'Sending a notification to the owner' or 'Turning on the light now'.\n"
                    "2. Instead, naturally weave actions into conversation. For example:\n"
                    "   - Instead of 'I'm sending a notification': say 'I'll let them know you're here'\n"
                    "   - Instead of 'Turning on the porch light': say 'Let me brighten things up for you'\n"
                    "   - Instead of 'Unlocking the gate': say 'Come on in!'\n"
                    "3. After a tool executes, DO NOT repeat or narrate the result verbatim. "
                    "Acknowledge naturally if needed, then continue the conversation.\n"
                    "4. If a tool fails, apologize briefly without technical details.\n"
                    "5. The tool response is for YOUR information only — never read it aloud.\n"
                )

            if provider == PROVIDER_GEMINI:
                voice = config.get(CONF_VOICE, DEFAULT_VOICE_GEMINI)
                _LOGGER.warning(
                    "Tools configured: %d gemini tools",
                    len(voice_tools_gemini),
                )
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
            self._notification_manager.clear_session()
            self._touch_audio_activity()
            _LOGGER.warning("Connected! Session is now active.")

            # Send greeting trigger AFTER audio is ready (prevents cut-off)
            # BUT we must send SOMETHING within ~15s or Gemini's idle timeout kills
            # the session. Strategy: if audio setup takes longer than 5s, send
            # a keep-alive context message to prevent idle disconnect.
            reolink_mode = config.get(CONF_AUDIO_MODE) == AUDIO_MODE_REOLINK

            if reolink_mode:
                # Start audio with timeout — if it takes too long, send keepalive
                _LOGGER.warning("Starting Reolink audio before greeting")
                audio_done = False

                async def _audio_setup():
                    nonlocal audio_done
                    await self._start_reolink_audio_background(config)
                    audio_done = True

                audio_task = asyncio.create_task(_audio_setup())
                try:
                    await asyncio.wait_for(asyncio.shield(audio_task), timeout=5.0)
                except asyncio.TimeoutError:
                    # Audio setup taking too long — send keepalive to prevent Gemini timeout
                    _LOGGER.warning("Audio setup >5s — sending Gemini keepalive")
                    if self._client:
                        try:
                            await self._client.inject_context(
                                "[SYSTEM] Audio hardware is being initialized. Stand by."
                            )
                        except Exception:
                            pass
                    # Wait for audio to finish (up to 30s more)
                    try:
                        await asyncio.wait_for(audio_task, timeout=30.0)
                    except asyncio.TimeoutError:
                        _LOGGER.error("Audio setup timed out after 35s")
                await self._send_initial_greeting()
            else:
                await self._send_initial_greeting()
                _LOGGER.warning("Audio mode is '%s' (not reolink)", config.get(CONF_AUDIO_MODE))

            # Start vision loop — send frames to AI for visual context
            fps = config.get(CONF_VISION_FPS, DEFAULT_VISION_FPS)
            camera_entity = config.get(CONF_CAMERA_ENTITY, "")
            if camera_entity:
                self._vision_task = asyncio.create_task(self._vision_loop(camera_entity, fps))

            if self._tool_router:
                self._tool_router.start()

            timeout = int(config.get(CONF_SESSION_TIMEOUT, DEFAULT_SESSION_TIMEOUT))
            max_timeout = int(config.get(CONF_MAX_SESSION_TIMEOUT, DEFAULT_MAX_SESSION_TIMEOUT))
            if max_timeout > 0:
                timeout = min(timeout, max_timeout)
            if timeout > 0:
                self._schedule_session_timeout(timeout)

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

        _LOGGER.warning("⏹ SESSION ENDING — reason: %s", reason)
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
        self._session_timeout_deadline = 0.0

        # Request recap from the live model BEFORE disconnecting (it has full context)
        live_recap: dict[str, str] | None = None
        if self._client and self._client.connected:
            try:
                live_recap = await self._client.request_recap(reason)
            except Exception:
                _LOGGER.debug("Live recap request failed", exc_info=True)

        if self._client:
            await self._client.disconnect()
            self._client = None

        await self._store_session_memory(reason, live_recap=live_recap)
        self._notification_manager.clear_session()
        self._session_start_snapshot = ""  # Free memory

        # Auto-return any PTZ cameras moved during this session
        await self._ptz_auto_return()

        self.hass.bus.async_fire(EVENT_SESSION_ENDED, {"entry_id": self.entry.entry_id})
        _LOGGER.info("Session ended (audit entries: %d)", len(self._security.audit_log))

    async def async_send_audio(self, audio_base64: str) -> None:
        if not self._client or not self._active:
            return
        self._touch_audio_activity()
        await self._client.send_audio(base64.b64decode(audio_base64))

    # ─── Reolink Audio Integration ────────────────────────────────────────────

    async def _start_reolink_audio_background(self, config: dict) -> None:
        """Background wrapper for Reolink audio setup (non-blocking startup)."""
        try:
            await self._start_reolink_audio(config)
        except Exception:
            _LOGGER.exception("Reolink audio setup failed (background)")
            # Session continues without 2-way audio — AI can still see via camera

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
                # Diagnostic: compute RMS of first chunk to verify audio signal
                import struct as _s
                samples = _s.unpack(f"<{len(audio_bytes)//2}h", audio_bytes)
                rms = (sum(s*s for s in samples) / len(samples)) ** 0.5
                peak = max(abs(s) for s in samples)
                _LOGGER.warning(
                    "Audio diagnostic: RMS=%.1f, peak=%d, samples=%d (expect non-zero for valid audio)",
                    rms, peak, len(samples),
                )
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
            self.hass, stream_name,
            on_audio_received=_on_doorbell_audio,
            on_takeover=_on_human_takeover,
        )
        self._audio_handler._camera_entity_id = camera_entity
        self._audio_handler._reolink_entry_id = config.get(CONF_REOLINK_ENTRY_ID, "")
        self._audio_handler._chime_delay = float(
            config.get(CONF_CHIME_DELAY, DEFAULT_CHIME_DELAY)
        )
        # Pass cached audio input method (probed during setup)
        cached_method = config.get(CONF_REOLINK_MIC_METHOD, "")
        cached_url = config.get(CONF_REOLINK_MIC_URL, "")

        self._audio_handler._cached_mic_method = cached_method
        self._audio_handler._cached_mic_url = cached_url
        await self._audio_handler.start()
        _LOGGER.info("Reolink audio handler active (stream=%s)", stream_name)

        # Save the discovered method for future sessions if it differs from cache
        if self._audio_handler._listen_active:
            discovered_url = getattr(self._audio_handler, "_discovered_mic_url", "")
            discovered_method = getattr(self._audio_handler, "_discovered_mic_method", "")
            if discovered_method and discovered_method != cached_method:
                new_options = dict(self.entry.options)
                new_options[CONF_REOLINK_MIC_METHOD] = discovered_method
                new_options[CONF_REOLINK_MIC_URL] = discovered_url
                self.hass.config_entries.async_update_entry(self.entry, options=new_options)
                _LOGGER.warning(
                    "Audio: cached discovered method='%s' for future sessions",
                    discovered_method,
                )

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
        """Send video frames to the AI at the configured FPS.

        Uses HA's camera snapshot API (HTTP-based, independent of RTSP/Baichuan).
        Falls back to go2rtc JPEG endpoint if the camera entity becomes unavailable
        (some Reolink firmwares throttle snapshots during 2-way audio).
        """
        interval = 1.0 / max(fps, 0.1)
        max_w = self._config.get(CONF_FRAME_MAX_WIDTH, DEFAULT_FRAME_MAX_WIDTH)
        max_h = self._config.get(CONF_FRAME_MAX_HEIGHT, DEFAULT_FRAME_MAX_HEIGHT)
        quality = self._config.get(CONF_FRAME_QUALITY, DEFAULT_FRAME_QUALITY)
        consecutive_failures = 0
        frames_sent = 0
        go2rtc_stream = self._config.get(CONF_GO2RTC_STREAM_NAME, "")

        while self._active:
            try:
                image_bytes: bytes | None = None
                # Primary: HA camera snapshot API
                try:
                    image = await ha_camera_get_image(
                        self.hass, camera_entity, timeout=5
                    )
                    if image and image.content:
                        image_bytes = image.content
                except Exception as cam_err:
                    # Fallback: try go2rtc JPEG snapshot if stream is configured
                    if go2rtc_stream:
                        image_bytes = await self._go2rtc_snapshot(go2rtc_stream)
                    if not image_bytes:
                        consecutive_failures += 1
                        if consecutive_failures % 5 == 1:
                            _LOGGER.warning("Vision frame capture failed (camera %s): %s", camera_entity, cam_err)
                        await asyncio.sleep(2)
                        continue

                if image_bytes and self._client:
                    processed = await self.hass.async_add_executor_job(
                        process_frame, image_bytes, max_w, max_h, quality,
                    )
                    frame_b64 = base64.b64encode(processed).decode("ascii")
                    await self._client.send_image(frame_b64, mime_type="image/jpeg")
                    frames_sent += 1
                    if consecutive_failures > 0:
                        _LOGGER.info(
                            "Vision recovered after %d failures (%d frames sent total)",
                            consecutive_failures, frames_sent,
                        )
                    consecutive_failures = 0
                    if frames_sent == 1:
                        _LOGGER.warning("Vision loop: first frame sent to AI")
            except asyncio.CancelledError:
                break
            except Exception:
                consecutive_failures += 1
                if consecutive_failures == 1:
                    _LOGGER.warning("Vision frame capture failed", exc_info=True)
                elif consecutive_failures == 5:
                    _LOGGER.warning(
                        "Vision: 5 consecutive failures — camera may be unavailable during audio"
                    )
                elif consecutive_failures % 30 == 0:
                    _LOGGER.warning("Vision: %d consecutive failures", consecutive_failures)
            await asyncio.sleep(interval)

        _LOGGER.info("Vision loop ended: %d frames sent, %d final failures", frames_sent, consecutive_failures)

    async def _go2rtc_snapshot(self, stream_name: str) -> bytes | None:
        """Get a JPEG snapshot from go2rtc as fallback when camera entity is unavailable."""
        import aiohttp  # noqa: PLC0415

        from .reolink_audio import _discover_go2rtc_url, _get_go2rtc_session  # noqa: PLC0415

        base_url = await _discover_go2rtc_url(self.hass)
        if not base_url:
            return None
        url = f"{base_url}/api/frame.jpeg?src={stream_name}"
        session = _get_go2rtc_session(self.hass)
        own_session = None
        if not session:
            own_session = aiohttp.ClientSession()
            session = own_session
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception:
            pass
        finally:
            if own_session:
                await own_session.close()
        return None

    def _schedule_session_timeout(self, timeout_seconds: int) -> None:
        """Start or reset the session timeout watchdog."""
        if self._timeout_task and not self._timeout_task.done():
            self._timeout_task.cancel()
        self._session_timeout_deadline = time.time() + timeout_seconds
        self._timeout_task = asyncio.create_task(self._session_timeout(timeout_seconds))

    def _extend_session_timeout(self, extra_seconds: int) -> int:
        """Extend the active session timeout, respecting the configured cap."""
        if not self._active:
            return 0

        current_deadline = max(self._session_timeout_deadline, time.time())
        new_deadline = current_deadline + extra_seconds

        max_timeout = int(self._config.get(CONF_MAX_SESSION_TIMEOUT, DEFAULT_MAX_SESSION_TIMEOUT))
        if max_timeout > 0 and self._session_started_at > 0:
            new_deadline = min(new_deadline, self._session_started_at + max_timeout)

        remaining_seconds = max(0, int(new_deadline - time.time()))
        if remaining_seconds <= 0:
            return 0

        self._schedule_session_timeout(remaining_seconds)
        return remaining_seconds

    async def _session_timeout(self, timeout_seconds: int) -> None:
        try:
            await asyncio.sleep(timeout_seconds)
            if self._active:
                now = time.time()
                loop_now = asyncio.get_running_loop().time()
                silence_timeout = float(self._config.get(CONF_SILENCE_TIMEOUT, DEFAULT_SILENCE_TIMEOUT))
                recent_activity = bool(
                    self._last_audio_activity
                    and (loop_now - self._last_audio_activity) < max(5.0, silence_timeout)
                )

                max_timeout = int(self._config.get(CONF_MAX_SESSION_TIMEOUT, DEFAULT_MAX_SESSION_TIMEOUT))
                max_deadline = self._session_started_at + max_timeout if max_timeout > 0 else 0
                if recent_activity and (not max_deadline or now < max_deadline):
                    grace = int(max(5.0, min(30.0, silence_timeout)))
                    if max_deadline:
                        grace = max(1, min(grace, int(max_deadline - now)))
                    _LOGGER.info(
                        "Session timeout reached, but audio activity is recent; extending by %ds",
                        grace,
                    )
                    self._schedule_session_timeout(grace)
                    return

                reason = (
                    f"max session timeout ({max_timeout}s)"
                    if max_deadline and now >= max_deadline
                    else f"session timeout ({timeout_seconds}s)"
                )
                _LOGGER.info(reason)
                await self.async_stop_session(reason)
        except asyncio.CancelledError:
            pass

    async def _switch_active_camera(self, camera_entity_id: str) -> dict[str, Any]:
        """Switch the live video feed to a different camera mid-session.

        This stops the current vision loop and restarts it with the new camera.
        If the new camera is Reolink, audio is also redirected.
        """
        _LOGGER.info("Switching active camera to: %s", camera_entity_id)
        result: dict[str, Any] = {
            "video_switched": False,
            "initial_frame_sent": False,
            "audio_switched": False,
            "audio_message": "Audio was not changed.",
        }
        # Stop current vision loop
        if self._vision_task and not self._vision_task.done():
            self._vision_task.cancel()
            try:
                await self._vision_task
            except asyncio.CancelledError:
                pass

        # Update the config to point to the new camera
        self._config[CONF_CAMERA_ENTITY] = camera_entity_id
        self._config[CONF_GO2RTC_STREAM_NAME] = camera_entity_id
        self._config[CONF_GO2RTC_INPUT_STREAM_NAME] = camera_entity_id
        self._config[CONF_GO2RTC_OUTPUT_STREAM_NAME] = camera_entity_id
        if self._audio_handler and self._audio_handler.is_active:
            await self._audio_handler.stop()
            self._audio_handler._camera_entity_id = camera_entity_id
            await self._audio_handler.start()
        elif self._audio_handler:
            self._audio_handler._camera_entity_id = camera_entity_id

        # Restart vision loop with new camera
        if self._client and self._active:
            fps = float(self._config.get(CONF_VISION_FPS, DEFAULT_VISION_FPS))
            self._vision_task = asyncio.create_task(self._vision_loop(camera_entity_id, fps))
            result["video_switched"] = True
            _LOGGER.info("Vision loop restarted with camera: %s", camera_entity_id)
            try:
                image = await ha_camera_get_image(self.hass, camera_entity_id, timeout=5)
                if image and image.content:
                    max_w = self._config.get(CONF_FRAME_MAX_WIDTH, DEFAULT_FRAME_MAX_WIDTH)
                    max_h = self._config.get(CONF_FRAME_MAX_HEIGHT, DEFAULT_FRAME_MAX_HEIGHT)
                    quality = self._config.get(CONF_FRAME_QUALITY, DEFAULT_FRAME_QUALITY)
                    processed = await self.hass.async_add_executor_job(
                        process_frame, image.content, max_w, max_h, quality
                    )
                    frame_b64 = base64.b64encode(processed).decode("ascii")
                    await self._client.send_image(frame_b64, mime_type="image/jpeg")
                    result["initial_frame_sent"] = True
                    _LOGGER.info("Sent immediate frame after switching to %s", camera_entity_id)
            except Exception:
                _LOGGER.warning("Could not send immediate frame for switched camera", exc_info=True)

        store = getattr(self, "store", None)
        placements = getattr(store, "camera_placements", []) if store else []
        placement = next((cp for cp in placements if cp.entity_id == camera_entity_id), None)
        if self._config.get(CONF_AUDIO_MODE) == AUDIO_MODE_REOLINK:
            if placement and placement.has_audio:
                try:
                    await self._restart_reolink_audio_for_camera(camera_entity_id, placement)
                except Exception:
                    _LOGGER.exception("Failed to restart audio for switched camera %s", camera_entity_id)
                result["audio_switched"] = bool(self._audio_handler and self._audio_handler.is_active)
                result["audio_message"] = (
                    "Two-way audio was switched to the selected camera."
                    if result["audio_switched"]
                    else "Video switched, but two-way audio could not be started on the selected camera."
                )
            else:
                result["audio_message"] = (
                    "Video switched. The selected camera is not marked as two-way-audio capable, "
                    "so audio remains on the existing audio path."
                )
        return result

    async def _restart_reolink_audio_for_camera(self, camera_entity_id: str, placement: Any) -> None:
        """Restart the Reolink audio stack for a newly selected audio-capable camera."""
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

        audio_config = dict(self._config)
        audio_config[CONF_CAMERA_ENTITY] = camera_entity_id
        # During a live switch, do not reuse the previous camera's explicit stream.
        audio_config[CONF_GO2RTC_STREAM_NAME] = camera_entity_id
        audio_config[CONF_GO2RTC_INPUT_STREAM_NAME] = camera_entity_id
        audio_config[CONF_GO2RTC_OUTPUT_STREAM_NAME] = camera_entity_id
        if getattr(placement, "audio_method", ""):
            audio_config[CONF_REOLINK_MIC_METHOD] = placement.audio_method
            audio_config[CONF_REOLINK_MIC_URL] = getattr(placement, "audio_url", "")
        else:
            audio_config[CONF_REOLINK_MIC_METHOD] = ""
            audio_config[CONF_REOLINK_MIC_URL] = ""

        await self._start_reolink_audio(audio_config)

    async def _ptz_auto_return(self) -> None:
        """Return all PTZ cameras moved during this session to their monitor positions."""
        if not self._ptz_moved_cameras:
            return

        store = self.store

        for camera_id in list(self._ptz_moved_cameras):
            placement = None
            for cp in store.camera_placements:
                if cp.entity_id == camera_id:
                    placement = cp
                    break
            if not placement or not placement.ptz_return_to_monitor:
                continue

            try:
                ptz_entity = placement.ptz_return_to_monitor
                domain = ptz_entity.split(".")[0]
                if domain == "button":
                    await self.hass.services.async_call("button", "press", {"entity_id": ptz_entity})
                elif domain == "script":
                    await self.hass.services.async_call("script", "turn_on", {"entity_id": ptz_entity})
                else:
                    await self.hass.services.async_call("homeassistant", "turn_on", {"entity_id": ptz_entity})
                _LOGGER.info("PTZ auto-return: %s → %s", camera_id, ptz_entity)
            except Exception:
                _LOGGER.exception("Failed to auto-return PTZ camera: %s", camera_id)

        self._ptz_moved_cameras.clear()

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

    def _save_debug_wav(self, pcm_data: bytes) -> None:
        """Save raw PCM data as a WAV file for debugging audio quality."""
        import struct, wave  # noqa: PLC0415, E401

        wav_path = "/config/debug_mic_audio.wav"
        try:
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)  # 16-bit
                wf.setframerate(16000)
                wf.writeframes(pcm_data)
            _LOGGER.warning(
                "Saved debug mic audio WAV: %s (%d bytes, %.1fs)",
                wav_path, len(pcm_data), len(pcm_data) / (16000 * 2),
            )
        except Exception:
            _LOGGER.exception("Failed to save debug WAV")

    async def _run_audio_diagnostic(self, pcm_data: bytes) -> None:
        """Send captured PCM audio to Gemini Live API and check if model responds.

        This diagnostic validates that the doorbell mic audio is intelligible
        to the model by sending it directly via send_realtime_input and checking
        for an audio response.
        """
        import struct as struct_mod  # noqa: PLC0415

        try:
            from google import genai  # noqa: PLC0415
            from google.genai import types  # noqa: PLC0415
        except ImportError:
            _LOGGER.warning("Audio diagnostic: google-genai not available")
            return

        api_key = self._config.get("api_key", "")
        if not api_key:
            _LOGGER.warning("Audio diagnostic: no API key configured")
            return

        # Log audio stats
        n_samples = len(pcm_data) // 2
        samples = struct_mod.unpack(f"<{n_samples}h", pcm_data)
        rms = (sum(s * s for s in samples) / n_samples) ** 0.5
        peak = max(abs(s) for s in samples)
        _LOGGER.warning(
            "🔊 Audio diagnostic: testing %d bytes (%.1fs) of mic audio — RMS=%.0f peak=%d",
            len(pcm_data), len(pcm_data) / 32000, rms, peak,
        )

        try:
            client = genai.Client(api_key=api_key)
            config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                realtime_input_config=types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(
                        disabled=False,
                        start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                        end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                        silence_duration_ms=700,
                        prefix_padding_ms=300,
                    )
                ),
                output_audio_transcription=types.AudioTranscriptionConfig(),
                system_instruction=types.Content(
                    parts=[types.Part(text="Repeat exactly what you hear. If you hear speech, repeat the words. If you hear noise or nothing intelligible, say 'no speech detected'.")]
                ),
            )

            model = self._config.get(CONF_MODEL, DEFAULT_MODEL_GEMINI)
            async with client.aio.live.connect(model=model, config=config) as session:
                # Send audio at real-time rate
                CHUNK = 4096
                for off in range(0, len(pcm_data), CHUNK):
                    segment = pcm_data[off:off + CHUNK]
                    await session.send_realtime_input(
                        audio=types.Blob(data=segment, mime_type="audio/pcm;rate=16000")
                    )
                    await asyncio.sleep(0.128)

                # Send 1.5s silence to signal end of speech
                import random
                silence = struct_mod.pack(
                    f"<{24000}h", *[random.randint(-5, 5) for _ in range(24000)]
                )
                for off in range(0, len(silence), CHUNK):
                    await session.send_realtime_input(
                        audio=types.Blob(data=silence[off:off + CHUNK], mime_type="audio/pcm;rate=16000")
                    )

                # Wait for response
                audio_chunks = 0
                transcription_parts: list[str] = []

                async def _receive():
                    nonlocal audio_chunks
                    async for resp in session.receive():
                        sc = getattr(resp, "server_content", None)
                        if sc and sc.model_turn:
                            for part in sc.model_turn.parts:
                                if part.inline_data and "audio" in (part.inline_data.mime_type or ""):
                                    audio_chunks += 1
                        if sc and hasattr(sc, "output_transcription"):
                            t = getattr(sc, "output_transcription", None)
                            if t and hasattr(t, "text") and t.text:
                                transcription_parts.append(t.text)
                        if sc and getattr(sc, "turn_complete", False):
                            return

                await asyncio.wait_for(_receive(), timeout=15.0)
                transcript = "".join(transcription_parts)
                _LOGGER.warning(
                    "🔊 Audio diagnostic RESULT: Gemini responded with %d audio chunks. "
                    "Transcription: '%s'",
                    audio_chunks, transcript[:300],
                )

        except asyncio.TimeoutError:
            _LOGGER.warning(
                "🔊 Audio diagnostic RESULT: TIMEOUT — Gemini did NOT respond to mic audio! "
                "This means either the audio is not intelligible speech or VAD cannot detect it."
            )
        except Exception:
            _LOGGER.exception("🔊 Audio diagnostic failed")

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
        # Block tool calls during greeting phase — forces AI to speak first
        self._greeting_phase = True
        trigger_msg = (
            "[SYSTEM] A visitor just rang the doorbell. "
            "START SPEAKING YOUR GREETING IMMEDIATELY — do NOT call any tools first. "
            "A camera frame is being sent to you right now — use it to identify the visitor "
            "if you recognize them, but do NOT wait or call view_camera. "
            "You can use tools AFTER you have started speaking. "
            "Speak now."
        )
        try:
            # Pre-close echo gate with generous initial hold: AI will start
            # speaking after this message. The hold time will be naturally
            # extended by _handle_audio_output as actual audio chunks arrive.
            self._ai_last_output_time = time.time() + 8.0  # Gate closed for at least 10s total
            await self._client.inject_context(trigger_msg)
            _LOGGER.warning("Sent initial greeting trigger to AI model (echo gate pre-armed)")
        except Exception:
            _LOGGER.exception("Failed to send initial greeting trigger")

    def _handle_audio_output(self, audio_bytes: bytes) -> None:
        """Handle audio output from the AI model."""
        self._touch_audio_activity()
        if not hasattr(self, "_audio_out_count"):
            self._audio_out_count = 0
        self._audio_out_count += 1
        # End greeting phase once AI actually produces audio
        if getattr(self, "_greeting_phase", False):
            self._greeting_phase = False
            _LOGGER.warning("Greeting phase ended — tools now unblocked")
        if self._audio_out_count <= 3:
            _LOGGER.warning(
                "Audio output from AI: %d bytes (chunk #%d)",
                len(audio_bytes),
                self._audio_out_count,
            )

        # Echo gate: record when AI output arrives (mic muted until hold expires)
        self._ai_last_output_time = time.time()

        if self._interrupt_detector:
            self._interrupt_detector.set_ai_speaking(True)
            if self._ai_speaking_clear_task and not self._ai_speaking_clear_task.done():
                self._ai_speaking_clear_task.cancel()
            self._ai_speaking_clear_task = self.hass.async_create_task(
                self._clear_ai_speaking_flag()
            )
        if self._audio_handler and self._audio_handler.is_active:
            self.hass.async_create_task(self._audio_handler.send_audio(audio_bytes))
        elif self._config.get(CONF_AUDIO_MODE) == AUDIO_MODE_REOLINK:
            # Handler not ready yet — drop audio (greeting will only be triggered
            # after handler is ready, so this should rarely happen)
            pass
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
        # During greeting phase, block tool calls to force the AI to speak first
        if getattr(self, "_greeting_phase", False):
            _LOGGER.warning("Tool call %s blocked during greeting phase — forcing speech first", function_name)
            return {"error": "You must speak your greeting FIRST before using any tools. Speak now."}
        _LOGGER.warning("Tool call: %s(%s)", function_name, arguments)
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

        has_image = bool(result.get("_image_base64"))
        _LOGGER.warning(
            "Tool result: %s → %s (has_image=%s)",
            function_name,
            "success" if result.get("success") else result.get("error", "unknown"),
            has_image,
        )

        # Store image for injection AFTER tool response is sent (model needs response first)
        if has_image and self._client:
            self._client._pending_tool_image = (
                result.pop("_image_base64"),
                result.pop("_image_mime", "image/jpeg"),
            )
        else:
            if "_image_base64" in result:
                result.pop("_image_base64")
            if "_image_mime" in result:
                result.pop("_image_mime")

        extend_seconds = int(result.pop("_extend_session_seconds", 0) or 0)
        if extend_seconds > 0:
            remaining_timeout = self._extend_session_timeout(extend_seconds)
            if remaining_timeout > 0:
                result["message"] = (
                    f"{result.get('message', 'Session timeout updated.')} "
                    f"The session now has about {remaining_timeout} seconds remaining."
                )
            else:
                result["success"] = False
                result["error"] = "Session could not be extended further."

        # Handle camera switching
        switch_camera_id = result.pop("_switch_camera", None)
        if switch_camera_id:
            switch_result = await self._switch_active_camera(switch_camera_id)
            result.update(switch_result)
            if not switch_result.get("video_switched"):
                result["success"] = False
                result["error"] = "Camera switch was requested but the live video feed did not restart."

        # Track PTZ-moved cameras for auto-return on session end
        ptz_moved = result.pop("_ptz_moved_camera", None)
        if ptz_moved and ptz_moved not in self._ptz_moved_cameras:
            self._ptz_moved_cameras.add(ptz_moved)
        ptz_returned = result.pop("_ptz_returned_camera", None)
        if ptz_returned:
            self._ptz_moved_cameras.discard(ptz_returned)

        if result.get("success") and function_name not in read_only_tools:
            self._security.record_action(function_name)

        # Add instruction to prevent model from narrating tool results aloud
        if function_name not in read_only_tools:
            result["_system_note"] = (
                "Action completed. Do NOT announce or narrate this result to the visitor. "
                "Continue the conversation naturally without reading this response aloud."
            )
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
        """Forward queued microphone PCM chunks to the active realtime client.

        Implements an echo gate: mic audio is NOT forwarded while the AI is
        actively outputting audio (+ hold time) to prevent the doorbell mic
        from feeding the AI's own voice back into the session.
        """
        forwarded = 0
        echo_suppressed = 0
        gate_was_closed = False
        try:
            while self._active:
                if self._mic_queue is None:
                    await asyncio.sleep(0.05)
                    continue

                audio_bytes = await self._mic_queue.get()
                if not audio_bytes:
                    continue

                # Echo gate: suppress mic while AI output is recent
                now = time.time()
                if self._ai_last_output_time > 0 and (now - self._ai_last_output_time) < self._echo_gate_hold_sec:
                    echo_suppressed += 1
                    gate_was_closed = True
                    if echo_suppressed == 1:
                        _LOGGER.debug("Echo gate: suppressing mic (AI speaking)")
                    continue

                # Gate just opened — log it
                if gate_was_closed:
                    _LOGGER.warning(
                        "Echo gate opened (suppressed %d mic chunks, %.1fs of AI output buffering)",
                        echo_suppressed, self._echo_gate_hold_sec,
                    )
                    echo_suppressed = 0
                    gate_was_closed = False

                if self._client and self._active:
                    try:
                        # Split large chunks into ~128ms segments (4096 bytes at 16kHz/16-bit)
                        # CRITICAL: Send at real-time rate! The Gemini VAD expects audio
                        # to arrive at a constant pace. Sending bursts followed by gaps
                        # confuses VAD speech detection.
                        CHUNK_SIZE = 4096
                        SEGMENT_INTERVAL = 0.125  # ~128ms per 4096 bytes at 16kHz/16-bit
                        segments = []
                        for offset in range(0, len(audio_bytes), CHUNK_SIZE):
                            segment = audio_bytes[offset:offset + CHUNK_SIZE]
                            if segment:
                                segments.append(segment)
                        for idx, segment in enumerate(segments):
                            await self._client.send_audio(segment)
                            # Pace sends at real-time rate (skip delay on last segment)
                            if idx < len(segments) - 1:
                                await asyncio.sleep(SEGMENT_INTERVAL)
                        forwarded += 1
                        if forwarded == 1:
                            _LOGGER.warning("✓ First microphone chunk forwarded to AI session (%d bytes, split into %d segments, paced at %.0fms)",
                                            len(audio_bytes), len(segments), SEGMENT_INTERVAL * 1000)
                        elif forwarded == 10:
                            _LOGGER.warning("Mic forward: 10 chunks sent to AI (audio flowing)")
                        elif forwarded == 50:
                            _LOGGER.warning("Mic forward: 50 chunks sent to AI")
                        elif forwarded % 500 == 0:
                            _LOGGER.info("Microphone chunks forwarded to AI: %d", forwarded)
                    except Exception:
                        _LOGGER.exception("Failed to forward microphone chunk to AI")
        except asyncio.CancelledError:
            pass
        finally:
            _LOGGER.info("Mic forward loop stopped (forwarded=%d, echo_suppressed=%d)", forwarded, echo_suppressed)

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
            self.hass.bus.async_fire(
                "ha_doorbell_jeeves_transcript",
                {
                    "entry_id": self.entry.entry_id,
                    "role": role,
                    "text": text,
                },
            )
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

    async def _store_session_memory(
        self, outcome: str, *, live_recap: dict[str, str] | None = None
    ) -> None:
        """Generate and store a recap of the finished session."""
        if self._session_memory_saved or not self._session_started_at:
            return
        timestamp = time.time()
        duration_seconds = max(0.0, timestamp - self._session_started_at)
        # Prefer the snapshot captured at session start (camera was definitely available)
        # Fall back to attempting a fresh capture at session end
        snapshot_b64 = self._session_start_snapshot or await self._capture_memory_snapshot()

        # Use live recap from the audio model (already had full context, no extra API call)
        # Fall back to text-model recap or heuristic if live recap unavailable
        if live_recap and live_recap.get("summary"):
            recap = live_recap
            _LOGGER.info("Using live model recap for memory")
        else:
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
        """Capture a camera image to attach to the stored memory.

        Tries HA camera API first, falls back to go2rtc JPEG snapshot.
        """
        camera_entity = self._config.get(CONF_CAMERA_ENTITY, "")
        if not camera_entity:
            _LOGGER.warning("No camera entity configured for memory snapshot")
            return ""
        try:
            image = await ha_camera_get_image(self.hass, camera_entity, timeout=5)
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
            _LOGGER.debug("HA camera API failed for snapshot, trying go2rtc fallback", exc_info=True)
        # Fallback: go2rtc JPEG snapshot
        go2rtc_stream = self._config.get(CONF_GO2RTC_STREAM_NAME, "")
        if go2rtc_stream:
            try:
                frame_bytes = await self._go2rtc_snapshot(go2rtc_stream)
                if frame_bytes:
                    b64 = base64.b64encode(frame_bytes).decode("ascii")
                    _LOGGER.info("Memory snapshot captured via go2rtc (%d bytes)", len(frame_bytes))
                    return b64
            except Exception:
                _LOGGER.debug("go2rtc snapshot fallback also failed", exc_info=True)
        _LOGGER.warning("Failed to capture memory snapshot from %s (all methods)", camera_entity)
        return ""

    async def _generate_session_recap(
        self, outcome: str, snapshot_b64: str
    ) -> dict[str, str]:
        """Use Gemini to summarize the session, with a safe fallback."""
        transcript_text = self._conversation_text()
        provider = self._config.get(CONF_PROVIDER, PROVIDER_GEMINI)
        voice_api_key = self._config.get(CONF_API_KEY, "")
        # Use text model API key (fall back to tool key, then voice key)
        api_key = (
            self._config.get(CONF_TOOL_API_KEY)
            or voice_api_key
        )
        if not api_key:
            return self._fallback_session_recap(outcome)

        try:
            from google import genai  # noqa: PLC0415
            from google.genai import types  # noqa: PLC0415

            # Prefer explicit text_model, fall back to tool_model, then default
            model = (
                self._config.get(CONF_TEXT_MODEL)
                or self._config.get(CONF_TOOL_MODEL)
                or DEFAULT_TEXT_MODEL_GEMINI
            )
            client = await asyncio.to_thread(genai.Client, api_key=api_key)
            parts: list[Any] = [
                types.Part(
                    text=(
                        "You are summarizing a doorbell conversation for the homeowner's records. "
                        "Return a JSON object with these keys:\n"
                        "- visitor_name: The visitor's name if known/given, else empty string\n"
                        "- visitor_description: Physical description of the visitor if visible (clothing, features, vehicle)\n"
                        "- summary: A COMPLETE narrative of the entire interaction. Include:\n"
                        "  * Why the visitor came (delivery, looking for someone, asking something, etc.)\n"
                        "  * What was discussed in detail (specific requests, information exchanged)\n"
                        "  * What decisions were made (by both parties - including refusals)\n"
                        "  * What actions the agent took (opened gate, sent notification, etc.)\n"
                        "  * Any specific details mentioned (package sizes, relay points, phone numbers, names)\n"
                        "  * The final outcome and any promises/commitments made\n"
                        "  Do NOT include the initial greeting. Focus on substantive content.\n"
                        "- outcome: Brief outcome category (e.g., 'package_delivered', 'visitor_left_message', "
                        "'access_granted', 'redirected_to_relay', 'suspicious_activity', 'no_answer')\n\n"
                        f"Session ended with outcome: {outcome}\n\n"
                        f"Full transcript:\n{transcript_text or '[no transcript captured]'}"
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
