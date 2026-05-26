"""Session manager – orchestrates client, vision, security, and auto-stop."""

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
    CONF_API_BASE_URL,
    CONF_API_KEY,
    CONF_CAMERA_ENTITY,
    CONF_FRAME_MAX_HEIGHT,
    CONF_FRAME_MAX_WIDTH,
    CONF_FRAME_QUALITY,
    CONF_IDENTITY_MODE,
    CONF_FACE_SENSOR_ENTITY,
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

    async def async_start_session(self) -> None:
        """Start the AI concierge session."""
        if self._active:
            _LOGGER.warning("Session already active — ignoring")
            return

        config = self._config
        provider = config.get(CONF_PROVIDER, PROVIDER_GEMINI)
        api_key: str = config[CONF_API_KEY]
        model: str = config.get(CONF_MODEL, DEFAULT_MODEL_GEMINI)

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

        # Create client
        if provider == PROVIDER_GEMINI:
            voice = config.get(CONF_VOICE, DEFAULT_VOICE_GEMINI)
            tools = build_gemini_tools(self.store)
            self._client = await self._create_gemini_client(api_key, model, full_prompt, voice, tools, reference_images)
        else:
            voice = config.get(CONF_VOICE, DEFAULT_VOICE_OPENAI)
            tools = build_openai_tools(self.store)
            self._client = await self._create_openai_client(api_key, model, full_prompt, voice, tools, reference_images, config)

        # Security reset
        self._security.start_session()

        # Connect
        await self._client.connect()
        self._active = True

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
        _LOGGER.info("Session started (provider=%s, model=%s, fps=%.1f)", provider, model, fps)

    async def async_stop_session(self) -> None:
        """Tear down the active session."""
        if not self._active:
            return
        self._active = False

        for unsub in self._stop_unsubs:
            unsub()
        self._stop_unsubs.clear()

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
            # Remove the internal keys from the result sent back to the model
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

    def _extract_claimed_identity(self, conversation_summary: str) -> str:
        summary_lower = conversation_summary.lower()
        for ident in self.store.known_identities:
            if ident.name.lower() in summary_lower:
                return ident.name
        return "unknown"

    def _get_reference_for_identity(self, name: str) -> str | None:
        ident = self.store.get_identity(name)
        return ident.image_base64 if ident and ident.image_base64 else None
