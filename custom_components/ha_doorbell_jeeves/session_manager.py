"""Session manager – orchestrates client, vision, security, audio, and dual-model routing."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .client_base import BaseRealtimeClient
from .const import (
    AUDIO_MODE_REOLINK,
    CONF_API_BASE_URL,
    CONF_API_KEY,
    CONF_AUDIO_MODE,
    CONF_CAMERA_ENTITY,
    CONF_DUAL_MODEL_ENABLED,
    CONF_FRAME_MAX_HEIGHT,
    CONF_FRAME_MAX_WIDTH,
    CONF_FRAME_QUALITY,
    CONF_GO2RTC_STREAM_NAME,
    CONF_IDENTITY_MODE,
    CONF_FACE_SENSOR_ENTITY,
    CONF_MEDIA_PLAYER_ENTITY,
    CONF_MODEL,
    CONF_PROVIDER,
    CONF_REOLINK_ENTRY_ID,
    CONF_SESSION_TIMEOUT,
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
    DEFAULT_FRAME_MAX_HEIGHT,
    DEFAULT_FRAME_MAX_WIDTH,
    DEFAULT_FRAME_QUALITY,
    DEFAULT_MODEL_GEMINI,
    DEFAULT_SESSION_TIMEOUT,
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
)
from .frame_processor import process_frame
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
        self._active = False
        self._config = dict(entry.data) | dict(entry.options)

        # Data store (entities, actions, identities)
        self.store = DataStore(hass, entry.entry_id)
        self._security = SecurityManager(hass, self._config, self.store)

        # Audio handler (Reolink/go2rtc)
        self._audio_handler: Any = None  # ReolinkAudioHandler or None

        # Talk state monitor (detects human taking over via Reolink app)
        self._talk_monitor: Any = None  # ReolinkTalkMonitor or None

        # Audio interrupt detector (energy-based takeover detection)
        self._interrupt_detector: Any = None  # AudioInterruptDetector or None

        # Tool router (dual-model mode)
        self._tool_router: Any = None  # ToolRouter or None

        # Auto-stop & auto-start listener handles
        self._stop_unsubs: list[CALLBACK_TYPE] = []
        self._start_unsubs: list[CALLBACK_TYPE] = []

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def security(self) -> SecurityManager:
        return self._security

    async def async_initialize(self) -> None:
        """Load persistent data and register start triggers."""
        await self.store.async_load()
        self._register_start_triggers()

    async def _lazy_setup_reolink(self) -> None:
        """Set up go2rtc stream for Reolink (deferred from boot for timing)."""
        from .reolink_audio import auto_configure_reolink  # noqa: PLC0415

        config = self._config
        camera_entity = config.get(CONF_CAMERA_ENTITY, "")
        if not camera_entity:
            return
        result = await auto_configure_reolink(self.hass, camera_entity)
        if result:
            _LOGGER.info("Reolink go2rtc configured: stream=%s", result.get("stream_name"))
            new_options = dict(self.entry.options)
            new_options["go2rtc_stream_name"] = result["stream_name"]
            self.hass.config_entries.async_update_entry(self.entry, options=new_options)
        else:
            _LOGGER.warning("go2rtc not available — 2-way audio may not work")

    async def async_start_session(self) -> None:
        """Start the AI concierge session."""
        if self._active:
            _LOGGER.warning("Session already active — ignoring")
            return

        # Lazy Reolink go2rtc setup (deferred from entry load for timing)
        if getattr(self, "reolink_needs_setup", False):
            await self._lazy_setup_reolink()
            self.reolink_needs_setup = False

        config = self._config
        provider = config.get(CONF_PROVIDER, PROVIDER_GEMINI)
        api_key: str = config[CONF_API_KEY]
        model: str = config.get(CONF_MODEL, DEFAULT_MODEL_GEMINI)
        # Runtime fix: replace known-invalid model names
        _INVALID_MODELS = {"gemini-2.5-flash-native-audio-dialog"}
        if model in _INVALID_MODELS:
            model = DEFAULT_MODEL_GEMINI
            _LOGGER.warning("Replaced invalid model %s → %s", config.get(CONF_MODEL), model)
        dual_model = config.get(CONF_DUAL_MODEL_ENABLED, False)

        # Build full system prompt with entity context
        base_prompt: str = config.get(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT)
        entity_context = build_system_context(self.store, self.hass)
        identity_context = self._build_identity_context()
        full_prompt = base_prompt
        if entity_context:
            full_prompt += f"\n\n{entity_context}"
        if identity_context:
            full_prompt += f"\n\n{identity_context}"

        # Reference images
        reference_images = self._get_reference_images()

        # Build tools (runs in executor due to google.genai import blocking I/O)
        gemini_tools = await self.hass.async_add_executor_job(build_gemini_tools, self.store)
        openai_tools = build_openai_tools(self.store)

        # ─── Dual-model setup ─────────────────────────────────────────────
        if dual_model:
            await self._setup_tool_router(config, full_prompt, gemini_tools, openai_tools)
            # Voice client gets NO tools (native audio model can't use them)
            voice_tools_gemini: list = []
            voice_tools_openai: list = []
            # Add instruction to voice model about how to request actions
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

        # ─── Create voice client ─────────────────────────────────────────
        if provider == PROVIDER_GEMINI:
            voice = config.get(CONF_VOICE, DEFAULT_VOICE_GEMINI)
            self._client = await self._create_gemini_client(
                api_key, model, voice_prompt, voice, voice_tools_gemini, reference_images
            )
        else:
            voice = config.get(CONF_VOICE, DEFAULT_VOICE_OPENAI)
            self._client = await self._create_openai_client(
                api_key, model, voice_prompt, voice, voice_tools_openai, reference_images, config
            )

        # Security reset
        self._security.start_session()

        # Connect voice client
        await self._client.connect()
        self._active = True

        # ─── Start Reolink audio handler ──────────────────────────────────
        if config.get(CONF_AUDIO_MODE) == AUDIO_MODE_REOLINK:
            await self._start_reolink_audio(config)

        # ─── Start tool router ────────────────────────────────────────────
        if self._tool_router:
            self._tool_router.start()

        # Vision loop
        fps = config.get(CONF_VISION_FPS, DEFAULT_VISION_FPS)
        camera_entity = config.get(CONF_CAMERA_ENTITY, "")
        if camera_entity:
            self._vision_task = asyncio.create_task(self._vision_loop(camera_entity, fps))

        # Timeout
        timeout = config.get(CONF_SESSION_TIMEOUT, DEFAULT_SESSION_TIMEOUT)
        if timeout > 0:
            self._timeout_task = asyncio.create_task(self._session_timeout(timeout))

        # Stop triggers
        self._register_stop_triggers()

        self.hass.bus.async_fire(EVENT_SESSION_STARTED, {"entry_id": self.entry.entry_id})
        _LOGGER.info(
            "Session started (provider=%s, model=%s, fps=%.1f, dual_model=%s)",
            provider, model, fps, dual_model,
        )

    async def async_stop_session(self) -> None:
        """Tear down the active session."""
        if not self._active:
            return
        self._active = False

        for unsub in self._stop_unsubs:
            unsub()
        self._stop_unsubs.clear()

        # Stop tool router
        if self._tool_router:
            self._tool_router.stop()

        # Stop talk monitor
        if self._talk_monitor:
            await self._talk_monitor.stop()
            self._talk_monitor = None

        # Stop interrupt detector
        if self._interrupt_detector:
            self._interrupt_detector = None

        # Stop audio handler
        if self._audio_handler:
            await self._audio_handler.stop()
            self._audio_handler = None

        for task in (self._vision_task, self._timeout_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._vision_task = None
        self._timeout_task = None

        if self._client:
            await self._client.disconnect()
            self._client = None

        self.hass.bus.async_fire(EVENT_SESSION_ENDED, {"entry_id": self.entry.entry_id})
        _LOGGER.info("Session ended (audit entries: %d)", len(self._security.audit_log))

    async def async_send_audio(self, audio_base64: str) -> None:
        if not self._client or not self._active:
            return
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
            _LOGGER.warning("No go2rtc stream configured — Reolink audio disabled")
            return

        # Shared takeover callback
        async def _on_human_takeover() -> None:
            """Human started talking — stop AI session."""
            _LOGGER.info("Human takeover detected — stopping AI session")
            await self.async_stop_session()

        # Audio handler (always needed for Reolink mode)
        async def _on_doorbell_audio(audio_bytes: bytes) -> None:
            """Forward audio from doorbell mic to AI client."""
            if self._client and self._active:
                await self._client.send_audio(audio_bytes)

        self._audio_handler = ReolinkAudioHandler(
            self.hass, stream_name, on_audio_received=_on_doorbell_audio
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
        tool_provider = config.get(CONF_TOOL_PROVIDER, config.get(CONF_PROVIDER, PROVIDER_GEMINI))
        tool_api_key = config.get(CONF_TOOL_API_KEY, config.get(CONF_API_KEY, ""))
        tool_base_url = config.get(CONF_TOOL_BASE_URL, config.get(CONF_API_BASE_URL))

        if tool_provider == PROVIDER_GEMINI:
            tool_model = config.get(CONF_TOOL_MODEL, DEFAULT_TOOL_MODEL_GEMINI)
            tools = gemini_tools
        else:
            tool_model = config.get(CONF_TOOL_MODEL, DEFAULT_TOOL_MODEL_OPENAI)
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
                if new_state is None:
                    return
                if new_state.state != _to:
                    return
                if _from and old_state and old_state.state != _from:
                    return
                if not self._active:
                    _LOGGER.info("Start trigger: %s → %s", event.data.get("entity_id"), _to)
                    self.hass.async_create_task(self.async_start_session())

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
                        self.hass.async_create_task(self.async_stop_session())
                else:
                    _LOGGER.info("Auto-stop: %s changed", entity_id)
                    self.hass.async_create_task(self.async_stop_session())

            unsub = async_track_state_change_event(self.hass, stop_entities, _on_stop)
            self._stop_unsubs.append(unsub)

        for event_type in config.get(CONF_STOP_EVENTS, []):
            @callback
            def _on_event(event: Event, evt: str = event_type) -> None:
                _LOGGER.info("Auto-stop: event '%s'", evt)
                self.hass.async_create_task(self.async_stop_session())
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
                await self.async_stop_session()
        except asyncio.CancelledError:
            pass

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

    def _handle_audio_output(self, audio_bytes: bytes) -> None:
        """Handle audio output from the AI model."""
        # If Reolink audio handler is active, send directly to doorbell speaker
        if self._audio_handler and self._audio_handler.is_active:
            self.hass.async_create_task(self._audio_handler.send_audio(audio_bytes))
        else:
            # Fall back to event-based output (for media player or external handling)
            audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
            self.hass.bus.async_fire(EVENT_AUDIO_OUTPUT, {
                "entry_id": self.entry.entry_id,
                "audio_base64": audio_b64,
                "media_player": self._config.get(CONF_MEDIA_PLAYER_ENTITY, ""),
            })

    async def _handle_tool_call(self, function_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._security.log_event("tool_call_request", function_name, {"arguments": arguments})
        self.hass.bus.async_fire(EVENT_TOOL_CALL, {
            "entry_id": self.entry.entry_id,
            "function": function_name,
            "arguments": arguments,
        })

        # PIN verification
        if function_name == "verify_pin":
            pin = arguments.get("pin", "")
            verified = self._security.check_pin(pin)
            return {"success": verified, "message": "PIN verified." if verified else "Incorrect PIN."}

        # Security evaluation (skip for read-only smart tools)
        read_only_tools = ("view_camera", "get_calendar_events", "get_entity_history",
                           "search_events", "read_entity_state")
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
                    return {"error": f"Action blocked: {reason}", "instruction": "Inform the visitor this action cannot be completed."}

        # Execute
        result = await execute_tool_call(self.hass, self.store, function_name, arguments)

        # Special handling: if tool returned an image, inject it into the session
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
        _LOGGER.warning("AI session ended unexpectedly")
        self._active = False
        self.hass.bus.async_fire(EVENT_SESSION_ENDED, {"entry_id": self.entry.entry_id})

    def _handle_transcript(self, role: str, text: str) -> None:
        self._security.log_event("transcript", f"{role}_speech", {"text": text[:500]})
        # Forward to tool router for dual-model processing
        if self._tool_router and self._tool_router.is_active:
            self._tool_router.add_transcript(role, text)

    def _extract_claimed_identity(self, conversation_summary: str) -> str:
        summary_lower = conversation_summary.lower()
        for ident in self.store.known_identities:
            if ident.name.lower() in summary_lower:
                return ident.name
        return "unknown"

    def _get_reference_for_identity(self, name: str) -> str | None:
        ident = self.store.get_identity(name)
        return ident.image_base64 if ident and ident.image_base64 else None
