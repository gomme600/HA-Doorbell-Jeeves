"""Session manager – orchestrates client, vision, security, identity, and auto-stop."""

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
    CONF_ALLOWED_ENTITIES,
    CONF_API_BASE_URL,
    CONF_API_KEY,
    CONF_CAMERA_ENTITY,
    CONF_FRAME_MAX_HEIGHT,
    CONF_FRAME_MAX_WIDTH,
    CONF_FRAME_QUALITY,
    CONF_MEDIA_PLAYER_ENTITY,
    CONF_MODEL,
    CONF_PROVIDER,
    CONF_SESSION_TIMEOUT,
    CONF_STOP_ENTITIES,
    CONF_STOP_ENTITY_STATES,
    CONF_STOP_EVENTS,
    CONF_SYSTEM_PROMPT,
    CONF_VISION_FPS,
    CONF_VOICE,
    DEFAULT_FRAME_MAX_HEIGHT,
    DEFAULT_FRAME_MAX_WIDTH,
    DEFAULT_FRAME_QUALITY,
    DEFAULT_MODEL_GEMINI,
    DEFAULT_SESSION_TIMEOUT,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_VISION_FPS,
    DEFAULT_VOICE_GEMINI,
    DEFAULT_VOICE_OPENAI,
    EVENT_AUDIO_OUTPUT,
    EVENT_SESSION_ENDED,
    EVENT_SESSION_STARTED,
    EVENT_TOOL_CALL,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    SECURITY_MODE_AUTO,
)
from .frame_processor import process_frame
from .identity import IdentityManager
from .security import SecurityManager
from .tools import build_openai_tool_declarations, build_tool_declarations, execute_tool_call

_LOGGER = logging.getLogger(__name__)


class JeevesSessionManager:
    """Orchestrates the full concierge session lifecycle.

    Manages:
      - Provider-agnostic AI client (Gemini or OpenAI-compatible)
      - Vision loop with configurable FPS (0.1–60) and frame downscaling
      - Per-action security evaluation
      - Identity context injection
      - Auto-stop triggers (entity state changes, HA events)
      - Session timeout
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._client: BaseRealtimeClient | None = None
        self._vision_task: asyncio.Task[None] | None = None
        self._timeout_task: asyncio.Task[None] | None = None
        self._active = False

        # Merge data + options
        self._config = dict(entry.data) | dict(entry.options)

        # Sub-managers
        self._identity = IdentityManager(hass, entry.entry_id, self._config)
        self._security = SecurityManager(hass, self._config)

        # Auto-stop listener handles
        self._stop_unsubs: list[CALLBACK_TYPE] = []

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def security(self) -> SecurityManager:
        return self._security

    @property
    def identity_manager(self) -> IdentityManager:
        return self._identity

    async def async_initialize(self) -> None:
        """Load persistent data on integration setup."""
        await self._identity.async_load()

    async def async_start_session(self) -> None:
        """Start the AI concierge session."""
        if self._active:
            _LOGGER.warning("Session already active — ignoring")
            return

        config = self._config
        provider = config.get(CONF_PROVIDER, PROVIDER_GEMINI)
        api_key: str = config[CONF_API_KEY]
        model: str = config.get(CONF_MODEL, DEFAULT_MODEL_GEMINI)
        system_prompt: str = config.get(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT)

        # Build identity context
        identity_context = await self._identity.build_identity_context()
        full_prompt = f"{system_prompt}\n\n{identity_context}" if identity_context else system_prompt

        # Reference images
        reference_images = self._identity.get_reference_images_for_session()

        # Create provider-specific client
        if provider == PROVIDER_OPENAI:
            voice = config.get(CONF_VOICE, DEFAULT_VOICE_OPENAI)
            self._client = await self._create_openai_client(
                api_key, model, full_prompt, voice, reference_images, config
            )
        else:
            voice = config.get(CONF_VOICE, DEFAULT_VOICE_GEMINI)
            self._client = await self._create_gemini_client(
                api_key, model, full_prompt, voice, reference_images, config
            )

        # Security reset
        self._security.start_session()

        # Connect
        await self._client.connect()
        self._active = True

        # Start vision loop
        fps = config.get(CONF_VISION_FPS, DEFAULT_VISION_FPS)
        camera_entity = config.get(CONF_CAMERA_ENTITY, "")
        if camera_entity:
            self._vision_task = asyncio.create_task(self._vision_loop(camera_entity, fps))

        # Session timeout
        timeout = config.get(CONF_SESSION_TIMEOUT, DEFAULT_SESSION_TIMEOUT)
        if timeout > 0:
            self._timeout_task = asyncio.create_task(self._session_timeout(timeout))

        # Register auto-stop triggers
        self._register_stop_triggers()

        self.hass.bus.async_fire(EVENT_SESSION_STARTED, {"entry_id": self.entry.entry_id})
        _LOGGER.info(
            "Session started (provider=%s, model=%s, fps=%.1f, timeout=%ds)",
            provider, model, fps, timeout,
        )

    async def async_stop_session(self) -> None:
        """Tear down the active session."""
        if not self._active:
            return

        self._active = False

        # Unregister stop triggers
        for unsub in self._stop_unsubs:
            unsub()
        self._stop_unsubs.clear()

        # Cancel tasks
        for task in (self._vision_task, self._timeout_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._vision_task = None
        self._timeout_task = None

        # Disconnect client
        if self._client:
            await self._client.disconnect()
            self._client = None

        self.hass.bus.async_fire(EVENT_SESSION_ENDED, {"entry_id": self.entry.entry_id})
        _LOGGER.info("Session ended (audit entries: %d)", len(self._security.audit_log))

    async def async_send_audio(self, audio_base64: str) -> None:
        """Forward PCM audio into the active session."""
        if not self._client or not self._active:
            return
        await self._client.send_audio(base64.b64decode(audio_base64))

    # ─── Auto-Stop Triggers ───────────────────────────────────────────────────

    def _register_stop_triggers(self) -> None:
        """Register HA state listeners and event listeners that stop the session.

        This handles:
          - Homeowner taking over via Reolink app (entity becomes 'on')
          - Front door opening (lock entity state change)
          - Any custom HA event
        """
        config = self._config

        # Entity state-based triggers
        stop_entities = config.get(CONF_STOP_ENTITIES, [])
        stop_states = config.get(CONF_STOP_ENTITY_STATES, {})

        if stop_entities:
            @callback
            def _on_entity_change(event: Event) -> None:
                """Stop session when a monitored entity reaches its target state."""
                entity_id = event.data.get("entity_id", "")
                new_state = event.data.get("new_state")
                if new_state is None:
                    return

                # Check if this entity has a specific target state configured
                target = stop_states.get(entity_id)
                if target:
                    if new_state.state == target:
                        _LOGGER.info("Auto-stop: %s reached state '%s'", entity_id, target)
                        self.hass.async_create_task(self.async_stop_session())
                else:
                    # Default: any state change stops session
                    _LOGGER.info("Auto-stop: %s state changed", entity_id)
                    self.hass.async_create_task(self.async_stop_session())

            unsub = async_track_state_change_event(self.hass, stop_entities, _on_entity_change)
            self._stop_unsubs.append(unsub)

        # Event-based triggers
        stop_events = config.get(CONF_STOP_EVENTS, [])
        for event_type in stop_events:
            @callback
            def _on_event(event: Event, evt_type: str = event_type) -> None:
                _LOGGER.info("Auto-stop: event '%s' fired", evt_type)
                self.hass.async_create_task(self.async_stop_session())

            unsub = self.hass.bus.async_listen(event_type, _on_event)
            self._stop_unsubs.append(unsub)

    # ─── Vision Loop ──────────────────────────────────────────────────────────

    async def _vision_loop(self, camera_entity: str, fps: float) -> None:
        """Capture, downscale, and inject camera frames at configured FPS."""
        interval = 1.0 / max(fps, 0.1)
        max_w = self._config.get(CONF_FRAME_MAX_WIDTH, DEFAULT_FRAME_MAX_WIDTH)
        max_h = self._config.get(CONF_FRAME_MAX_HEIGHT, DEFAULT_FRAME_MAX_HEIGHT)
        quality = self._config.get(CONF_FRAME_QUALITY, DEFAULT_FRAME_QUALITY)

        _LOGGER.debug("Vision loop: %.1f FPS, max %dx%d, quality=%d", fps, max_w, max_h, quality)

        while self._active:
            try:
                image = await self.hass.components.camera.async_get_image(
                    camera_entity, timeout=5
                )
                if image and self._client:
                    # Downscale in executor to avoid blocking event loop
                    processed = await self.hass.async_add_executor_job(
                        process_frame, image.content,
                        max_w, max_h, quality,
                    )
                    frame_b64 = base64.b64encode(processed).decode("ascii")
                    await self._client.send_image(frame_b64, mime_type="image/jpeg")
            except asyncio.CancelledError:
                break
            except Exception:
                _LOGGER.debug("Vision frame failed", exc_info=True)

            await asyncio.sleep(interval)

    # ─── Session Timeout ──────────────────────────────────────────────────────

    async def _session_timeout(self, timeout_seconds: int) -> None:
        try:
            await asyncio.sleep(timeout_seconds)
            if self._active:
                _LOGGER.info("Session timeout (%ds)", timeout_seconds)
                await self.async_stop_session()
        except asyncio.CancelledError:
            pass

    # ─── Client Factories ─────────────────────────────────────────────────────

    async def _create_gemini_client(
        self, api_key: str, model: str, prompt: str, voice: str,
        reference_images: list[dict[str, str]], config: dict[str, Any],
    ) -> BaseRealtimeClient:
        from .gemini_client import GeminiLiveClient  # noqa: PLC0415
        from google.genai import types  # noqa: PLC0415

        tools = build_tool_declarations(config)

        return GeminiLiveClient(
            api_key=api_key,
            model=model,
            system_prompt=prompt,
            tools=tools,
            voice=voice,
            reference_images=reference_images,
            on_audio_output=self._handle_audio_output,
            on_tool_call=self._handle_tool_call,
            on_session_end=self._handle_session_end,
            on_transcript=self._handle_transcript,
        )

    async def _create_openai_client(
        self, api_key: str, model: str, prompt: str, voice: str,
        reference_images: list[dict[str, str]], config: dict[str, Any],
    ) -> BaseRealtimeClient:
        from .openai_client import OpenAIRealtimeClient  # noqa: PLC0415

        base_url = config.get(CONF_API_BASE_URL) or None
        tools = build_openai_tool_declarations(config)

        return OpenAIRealtimeClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
            system_prompt=prompt,
            tools=tools,
            voice=voice,
            reference_images=reference_images,
            on_audio_output=self._handle_audio_output,
            on_tool_call=self._handle_tool_call,
            on_session_end=self._handle_session_end,
            on_transcript=self._handle_transcript,
        )

    # ─── Callbacks ────────────────────────────────────────────────────────────

    def _handle_audio_output(self, audio_bytes: bytes) -> None:
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        self.hass.bus.async_fire(EVENT_AUDIO_OUTPUT, {
            "entry_id": self.entry.entry_id,
            "audio_base64": audio_b64,
            "media_player": self._config.get(CONF_MEDIA_PLAYER_ENTITY, ""),
        })

    async def _handle_tool_call(
        self, function_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Evaluate security policy and execute tool."""
        self._security.log_event("tool_call_request", function_name, {"arguments": arguments})
        self.hass.bus.async_fire(EVENT_TOOL_CALL, {
            "entry_id": self.entry.entry_id,
            "function": function_name,
            "arguments": arguments,
        })

        # PIN verification bypass
        if function_name == "verify_pin":
            pin = arguments.get("pin", "")
            verified = self._security.check_pin(pin)
            self._security.log_event("pin_attempt", function_name, {"verified": verified})
            if verified:
                return {"success": True, "message": "PIN verified."}
            return {"success": False, "message": "Incorrect PIN."}

        # Security evaluation
        policy = self._security.get_policy(function_name)
        if policy.security_mode != SECURITY_MODE_AUTO:
            conversation_summary = self._client.conversation_summary if self._client else ""
            claimed_identity = self._extract_claimed_identity(conversation_summary)

            current_frame: str | None = None
            if policy.require_camera_feed:
                current_frame = await self._security.get_camera_frame()

            reference_image: str | None = None
            if policy.require_visual_match:
                reference_image = self._get_reference_for_identity(claimed_identity)

            approved, reason = await self._security.evaluate_action(
                action=function_name,
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

        # Execute
        result = await execute_tool_call(self.hass, self._config, function_name, arguments)
        if result.get("success"):
            self._security.record_action(function_name)
        self._security.log_event("tool_call_executed", function_name, {"result": result}, approved=True)
        return result

    def _handle_session_end(self) -> None:
        _LOGGER.warning("AI session ended unexpectedly")
        self._active = False
        self.hass.bus.async_fire(EVENT_SESSION_ENDED, {"entry_id": self.entry.entry_id})

    def _handle_transcript(self, role: str, text: str) -> None:
        self._security.log_event("transcript", f"{role}_speech", {"text": text[:500]})

    def _extract_claimed_identity(self, conversation_summary: str) -> str:
        summary_lower = conversation_summary.lower()
        for face in self._identity.known_faces:
            if face.name.lower() in summary_lower:
                return face.name
        return "unknown"

    def _get_reference_for_identity(self, name: str) -> str | None:
        face = self._identity.get_face_by_name(name)
        return face.image_base64 if face and face.image_base64 else None
