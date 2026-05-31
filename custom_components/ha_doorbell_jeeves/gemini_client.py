"""Gemini Multimodal Live API client implementation."""

from __future__ import annotations

import asyncio
import base64

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from google import genai
from google.genai import types

from .client_base import BaseRealtimeClient
from .const import AUDIO_INPUT_SAMPLE_RATE

_LOGGER = logging.getLogger(__name__)

_SHARED_CLIENTS: dict[str, genai.Client] = {}
_SHARED_CLIENT_LOCKS: dict[str, asyncio.Lock] = {}
_WARMED_LIVE_MODELS: set[tuple[str, str]] = set()
_WARM_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}



class GeminiLiveClient(BaseRealtimeClient):
    """Async client for the Gemini Multimodal Live streaming API."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        system_prompt: str,
        tools: list[types.Tool],
        voice: str,
        reference_images: list[dict[str, str]],
        on_audio_output: Callable[[bytes], None],
        on_tool_call: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        on_session_end: Callable[[], None],
        on_transcript: Callable[[str, str], None],
        on_turn_complete: Callable[[int], None] | None = None,
        defer_tools_until_turn_one: bool = False,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._system_prompt = system_prompt
        self._deferred_tools = tools if defer_tools_until_turn_one else []
        self._tools = [] if defer_tools_until_turn_one else tools
        self._voice = voice
        self._reference_images = reference_images
        self._on_audio_output = on_audio_output
        self._on_tool_call = on_tool_call
        self._on_session_end = on_session_end
        self._on_transcript = on_transcript
        self._on_turn_complete = on_turn_complete

        self._client: genai.Client | None = None
        self._session: Any = None
        self._session_cm: Any = None
        self._receive_task: asyncio.Task[None] | None = None
        self._connected = False
        self._conversation_turns: list[dict[str, str]] = []
        self._recap_future: asyncio.Future[str] | None = None
        self._pending_tool_image: tuple[str, str, str] | None = None
        self._tool_call_pending = False
        self._vision_paused = False  # Pause vision loop during tool image injection
        # Track model generation state (informational — used by reconnect logic)
        self._model_generating = False
        # Monitor turn muting: when True, audio output is suppressed (proactive monitor)
        self._monitor_turn_active = False
        # Keepalive task: sends silent audio during tool calls to prevent server idle timeout
        self._keepalive_task: asyncio.Task[None] | None = None
        # Startup grace period: block audio/video input briefly after greeting to prevent 1008
        self._startup_grace_until: float = 0.0
        self._reference_images_injected = False
        self._turns_completed = 0
        self._suppress_session_end = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def model_generating(self) -> bool:
        return self._model_generating

    @property
    def conversation_summary(self) -> str:
        if not self._conversation_turns:
            return "No conversation yet."
        recent = self._conversation_turns[-5:]
        return " | ".join(f"[{t['role']}]: {t['text'][:100]}" for t in recent)

    @classmethod
    async def _get_shared_client(cls, api_key: str) -> genai.Client:
        lock = _SHARED_CLIENT_LOCKS.setdefault(api_key, asyncio.Lock())
        async with lock:
            client = _SHARED_CLIENTS.get(api_key)
            if client is not None:
                return client

            loop = asyncio.get_running_loop()
            client = await loop.run_in_executor(
                None, lambda: genai.Client(api_key=api_key)
            )
            _SHARED_CLIENTS[api_key] = client
            return client

    @classmethod
    async def prewarm_shared_client(cls, api_key: str, model: str) -> genai.Client:
        """Create and prewarm a shared Live API client once per API key/model.

        The first cold `genai.Client` + first live session is the only path that
        consistently showed 1008 policy violations in production testing. The
        official SDK examples keep a persistent `genai.Client`, and our own
        reconnect path (same client, new session) proved stable. We therefore
        prewarm the shared client at integration startup so real doorbell
        sessions never use the cold path.
        """
        client = await cls._get_shared_client(api_key)
        warm_key = (api_key, model)
        lock = _WARM_LOCKS.setdefault(warm_key, asyncio.Lock())
        async with lock:
            if warm_key in _WARMED_LIVE_MODELS:
                return client

            async def _wait_for_first_response(session: Any) -> bool:
                try:
                    async with asyncio.timeout(8.0):
                        async for _response in session.receive():
                            return True
                except TimeoutError:
                    return False
                return False

            for attempt in range(1, 4):
                warmup_cm = None
                warmup_session = None
                try:
                    warmup_cm = client.aio.live.connect(
                        model=model,
                        config=types.LiveConnectConfig(response_modalities=["AUDIO"]),
                    )
                    warmup_session = await warmup_cm.__aenter__()
                    await warmup_session.send_client_content(
                        turns=types.Content(
                            role="user",
                            parts=[types.Part(text="Warmup: say hello briefly.")],
                        ),
                        turn_complete=True,
                    )
                    if not await _wait_for_first_response(warmup_session):
                        raise RuntimeError("warmup session produced no response")
                    await asyncio.sleep(0.2)
                    _WARMED_LIVE_MODELS.add(warm_key)
                    _LOGGER.warning(
                        "Gemini shared client prewarmed with first generation (model=%s, attempt=%d)",
                        model,
                        attempt,
                    )
                    break
                except Exception:
                    if attempt >= 3:
                        _LOGGER.warning(
                            "Gemini shared client prewarm failed (model=%s)",
                            model,
                            exc_info=True,
                        )
                    else:
                        _LOGGER.warning(
                            "Gemini shared client warmup attempt %d failed; retrying",
                            attempt,
                            exc_info=True,
                        )
                        await asyncio.sleep(0.4 * attempt)
                finally:
                    if warmup_cm is not None:
                        try:
                            await warmup_cm.__aexit__(None, None, None)
                        except Exception:
                            _LOGGER.debug("Gemini warmup session close failed", exc_info=True)
        return client

    def _build_live_config(self) -> types.LiveConnectConfig:
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice)
                )
            ),
            thinking_config=types.ThinkingConfig(
                thinking_budget=0,
                include_thoughts=False,
            ),
            system_instruction=types.Content(parts=[types.Part(text=self._system_prompt)]),
            tools=self._tools if self._tools else None,
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                    silence_duration_ms=700,
                    prefix_padding_ms=300,
                ),
                activity_handling=types.ActivityHandling.NO_INTERRUPTION,
            ),
        )

    async def connect(self, greeting_text: str = "") -> None:
        """Connect to Gemini Live API.

        If greeting_text is provided, the greeting is sent as part of the initial
        connection burst (ref images → greeting → receive loop) with NO delay.
        """
        self._client = await self.prewarm_shared_client(self._api_key, self._model)
        self._live_config = self._build_live_config()

        self._session_cm = self._client.aio.live.connect(
            model=self._model, config=self._live_config,
        )
        self._session = await self._session_cm.__aenter__()
        self._connected = True
        self._connect_time = time.time()
        self._turns_completed = 0
        self._suppress_session_end = False

        # Keep the first turn isolated. Static reference images are injected
        # after the greeting completes instead of before the initial response.
        if not greeting_text:
            await self._inject_reference_images()

        # Start receiving BEFORE the first greeting so the initial turn is
        # drained immediately once generation begins.
        self._receive_task = asyncio.create_task(self._receive_loop())

        if greeting_text:
            self._model_generating = True
            await self._session.send_client_content(
                turns=[types.Content(role="user", parts=[types.Part(text=greeting_text)])],
                turn_complete=True,
            )
            _LOGGER.warning(
                "Gemini connected + greeting sent at T+%.1f",
                time.time() - self._connect_time,
            )

        # Set startup grace period
        self._startup_grace_until = time.time() + 5.0
        _LOGGER.warning("Gemini Live connected at T+%.1f (model=%s)", time.time() - self._connect_time, self._model)

    def start_receive(self) -> None:
        """Start the receive loop (no-op if already running)."""
        if self._receive_task and not self._receive_task.done():
            return
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def enable_deferred_tools_after_greeting(self, notify_instruction: str = "") -> bool:
        """Reconnect once with the full tool list after the first greeting turn.
        
        If notify_instruction is provided, it's included in the reconnect history
        to avoid a separate inject_context that would trigger a repeated greeting.
        """
        if not self._deferred_tools or self._tools:
            return True
        self._tools = self._deferred_tools
        self._deferred_tools = []
        self._live_config = self._build_live_config()
        if not self._connected or not self._session:
            return False
        _LOGGER.warning("Enabling Gemini tools after greeting turn via reconnect (notify=%s)", bool(notify_instruction))
        return await self._reconnect_session(
            turns_completed=max(self._turns_completed, 1),
            restart_receive_task=True,
            post_reconnect_instruction=notify_instruction,
        )

    async def _reconnect_session(
        self,
        turns_completed: int = -1,
        restart_receive_task: bool = False,
        post_reconnect_instruction: str = "",
    ) -> bool:
        """Tear down current session and open a new one (same config). Returns True on success."""
        if turns_completed < 0:
            turns_completed = self._turns_completed

        restart_receive = (
            restart_receive_task
            and self._receive_task is not None
            and not self._receive_task.done()
            and self._receive_task is not asyncio.current_task()
        )
        try:
            if restart_receive and self._receive_task is not None:
                self._suppress_session_end = True
                self._receive_task.cancel()
                try:
                    await self._receive_task
                except asyncio.CancelledError:
                    pass
                self._receive_task = None

            if self._session_cm:
                try:
                    await self._session_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            self._session = None
            self._session_cm = None

            await asyncio.sleep(0.5)  # Brief pause before retry

            self._session_cm = self._client.aio.live.connect(
                model=self._model, config=self._live_config,
            )
            self._session = await self._session_cm.__aenter__()
            self._connect_time = time.time()
            if turns_completed != 0:
                await self._inject_reference_images()

            if turns_completed == 0:
                # First turn (greeting) was interrupted by 1008.
                # Trigger a fresh greeting instead of "wait silently".
                # This makes the 1008 recovery seamless for the user.
                self._model_generating = True
                await self._session.send_client_content(
                    turns=[types.Content(role="user", parts=[types.Part(text=(
                        "[SYSTEM] A visitor is at the door. "
                        "Speak exactly one short sentence in French ending with a question. "
                        "End your turn immediately after that sentence. "
                        "Do NOT call any tools. Do NOT think aloud. Speak now."
                    ))])],
                    turn_complete=True,
                )
                self._startup_grace_until = time.time() + 5.0
                _LOGGER.warning("Gemini reconnected at turns=0 — re-triggering greeting")
            else:
                # Build proper conversation history with alternating user/model roles
                history_turns: list[types.Content] = []
                if self._conversation_turns:
                    recent = self._conversation_turns[-15:]
                    current_role = None
                    current_texts: list[str] = []
                    for turn in recent:
                        role = "model" if turn["role"] == "assistant" else "user"
                        if role != current_role:
                            if current_role and current_texts:
                                history_turns.append(types.Content(
                                    role=current_role,
                                    parts=[types.Part(text=" ".join(current_texts))]
                                ))
                            current_role = role
                            current_texts = [turn["text"]]
                        else:
                            current_texts.append(turn["text"])
                    if current_role and current_texts:
                        history_turns.append(types.Content(
                            role=current_role,
                            parts=[types.Part(text=" ".join(current_texts))]
                        ))

                # Build the final system instruction based on whether we need notify
                if post_reconnect_instruction:
                    system_msg = (
                        "[SYSTEM] Connection briefly interrupted. You already greeted this visitor — "
                        "your greeting is in the history above. "
                        "ABSOLUTE RULE: Do NOT repeat your greeting, do NOT re-introduce yourself, "
                        "do NOT say 'Bonjour' or any greeting words. "
                        f"{post_reconnect_instruction}"
                    )
                else:
                    system_msg = (
                        "[SYSTEM] Connection briefly interrupted. You already greeted this visitor. "
                        "Do NOT repeat your greeting or re-introduce yourself. "
                        "Wait silently for the visitor to speak next."
                    )
                history_turns.append(types.Content(role="user", parts=[types.Part(text=system_msg)]))

                # If we have an instruction (like notify), complete the turn to trigger action.
                # Otherwise leave turn open so model waits for visitor to speak.
                should_complete = bool(post_reconnect_instruction)
                await self._session.send_client_content(
                    turns=history_turns,
                    turn_complete=should_complete,
                )
                self._model_generating = should_complete
                self._startup_grace_until = time.time() + 2.0
                _LOGGER.warning("Gemini reconnected — history restored (%d turns, %d conversation entries, turn_complete=%s)",
                              len(history_turns), len(self._conversation_turns), should_complete)

            if restart_receive:
                self.start_receive()
            return True
        except Exception:
            _LOGGER.exception("Gemini reconnect failed")
            return False

    async def disconnect(self) -> None:
        self._connected = False
        self._tool_call_pending = False
        self._model_generating = False
        self._vision_paused = False
        self._monitor_turn_active = False
        self._turns_completed = 0
        self._suppress_session_end = False
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        self._stop_keepalive()
        if self._session_cm:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
            self._session_cm = None
        self._client = None
        self._conversation_turns.clear()
        _LOGGER.info("Gemini Live disconnected")

    async def send_audio(self, pcm_bytes: bytes) -> None:
        if not self._session or not self._connected:
            return
        # GATE: Block during tool processing or active model generation
        if self._tool_call_pending or self._model_generating:
            return
        # GATE: Block during startup grace period (prevents 1008 race with greeting)
        if time.time() < self._startup_grace_until:
            return
        try:
            await self._session.send_realtime_input(
                audio=types.Blob(
                    data=pcm_bytes,
                    mime_type=f"audio/pcm;rate={AUDIO_INPUT_SAMPLE_RATE}",
                )
            )
        except Exception:
            if self._connected:
                _LOGGER.debug("Failed to send audio")

    async def send_image(self, image_base64: str, mime_type: str = "image/jpeg") -> None:
        """Send an image frame to the live session via the realtime media channel.

        Uses send_realtime_input(media=...) as per the Gemini Live API docs.
        The image must be a properly-sized JPEG (ideally ~768x768 at 1 FPS).
        """
        if not self._session or not self._connected:
            return
        if self._tool_call_pending or self._model_generating:
            return
        # Block during startup grace period
        if time.time() < self._startup_grace_until:
            return
        try:
            image_bytes = base64.b64decode(image_base64)
            await self._session.send_realtime_input(
                media=types.Blob(data=image_bytes, mime_type=mime_type)
            )
        except Exception:
            if self._connected:
                _LOGGER.debug("Failed to send image")

    def _start_keepalive(self) -> None:
        """Start sending silent audio frames to prevent Gemini idle timeout during tool calls."""
        if self._keepalive_task and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _stop_keepalive(self) -> None:
        """Stop the keepalive task."""
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None

    async def _keepalive_loop(self) -> None:
        """Send silent audio every 5s while waiting for tool execution to complete.

        Keeps the WebSocket alive during tool execution AND during the gap
        between tool_response and model generation start.
        """
        # 640 bytes = 20ms of silence at 16kHz/16-bit mono
        silent_frame = b"\x00" * 640
        try:
            while self._session and self._connected:
                # Primary condition: keep alive while tool is pending
                if self._tool_call_pending:
                    await asyncio.sleep(5)
                    if not self._session or not self._connected:
                        break
                    if not self._tool_call_pending:
                        break  # Tool done, stop
                    try:
                        await self._session.send_realtime_input(
                            audio=types.Blob(
                                data=silent_frame,
                                mime_type=f"audio/pcm;rate={AUDIO_INPUT_SAMPLE_RATE}",
                            )
                        )
                    except Exception:
                        break
                else:
                    # Tool no longer pending — give brief window for model to start
                    await asyncio.sleep(3)
                    if not self._tool_call_pending:
                        break  # Model should be generating by now
        except asyncio.CancelledError:
            pass

    async def inject_context(
        self, text: str, image_base64: str | None = None,
        mime_type: str = "image/jpeg", turn_complete: bool = True,
        monitor: bool = False,
    ) -> None:
        """Inject a text message (and optional image) into the live session.

        Args:
            monitor: If True, this is a proactive monitor injection. Audio output
                     will be suppressed for this turn (model should call no_action_needed silently).
        """
        if not self._session or not self._connected:
            return
        # SAFETY GATE: Wait for model generation or tool processing to finish.
        # Rather than silently dropping, wait up to 15s for the model to finish.
        # Skip the gate during startup grace period (greeting must go through immediately)
        in_startup = time.time() < self._startup_grace_until
        if not in_startup and (self._model_generating or self._tool_call_pending):
            _LOGGER.info("inject_context waiting: model_generating=%s, tool_pending=%s",
                         self._model_generating, self._tool_call_pending)
            for _ in range(30):  # 30 x 0.5s = 15s max wait
                await asyncio.sleep(0.5)
                if not self._model_generating and not self._tool_call_pending:
                    break
                if not self._session or not self._connected:
                    return
            else:
                _LOGGER.warning("inject_context timed out waiting for model — dropping message")
                return
            _LOGGER.info("inject_context: gate cleared, proceeding with injection")
        # Set monitor mute BEFORE sending so the response audio gets suppressed
        if monitor:
            self._monitor_turn_active = True
        try:
            parts = [types.Part(text=text)]
            if image_base64:
                image_bytes = base64.b64decode(image_base64)
                parts.append(types.Part(inline_data=types.Blob(data=image_bytes, mime_type=mime_type)))

            # Pre-block vision/audio BEFORE sending turn_complete=True.
            # This closes the gap between send_client_content and the first model_turn
            # event. Without this, the vision loop can send frames during server-side
            # processing, triggering a 1008 policy violation.
            if turn_complete:
                self._model_generating = True
                if hasattr(self, '_connect_time'):
                    _LOGGER.warning("inject_context(turn_complete=True) at T+%.1f", time.time() - self._connect_time)

            # Retry once if WebSocket send fails (race with 1008 reconnection)
            for attempt in range(2):
                try:
                    if turn_complete and not image_base64:
                        await self._session.send_realtime_input(text=text)
                    else:
                        await self._session.send_client_content(
                            turns=[types.Content(role="user", parts=parts)],
                            turn_complete=turn_complete,
                        )
                    break  # Success
                except Exception as send_err:
                    if attempt == 0 and self._connected:
                        _LOGGER.warning("inject_context send failed, retrying after reconnect window: %s", str(send_err)[:100])
                        # Wait for reconnection to complete
                        await asyncio.sleep(2.0)
                        if not self._session or not self._connected:
                            raise
                    else:
                        raise
        except Exception:
            _LOGGER.exception("Failed to inject context")
            if monitor:
                self._monitor_turn_active = False
            # Reset pre-block on failure
            if turn_complete:
                self._model_generating = False

    async def request_recap(self, outcome: str, timeout: float = 8.0) -> dict[str, str] | None:
        """Ask the live model to generate a session recap.

        With output_audio_transcription enabled, even native audio models produce
        text output via the transcription stream. The recap prompt requests JSON
        which will appear in the output_transcription messages.
        """
        if not self._session or not self._connected:
            return None

        # Set up a future to capture the next text response
        self._recap_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

        prompt = (
            "[SYSTEM] The conversation has ended. Generate a brief JSON recap with these keys:\n"
            '- "visitor_name": name if known, else ""\n'
            '- "visitor_description": brief visual description of the visitor\n'
            '- "summary": 1-2 sentence summary of what happened\n'
            '- "outcome": the interaction result\n'
            f"Session end reason: {outcome}\n"
            "Respond ONLY with the JSON object, no other text or audio."
        )

        try:
            await self._session.send_client_content(
                turns=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                turn_complete=True,
            )
            # Wait for text response (captured by _process via _recap_future)
            text_response = await asyncio.wait_for(self._recap_future, timeout=timeout)
            # Try to parse JSON from response
            import json  # noqa: PLC0415
            # Strip any markdown code fences
            clean = text_response.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            recap = json.loads(clean)
            return {
                "visitor_description": recap.get("visitor_description", ""),
                "summary": recap.get("summary", ""),
                "outcome": recap.get("outcome", outcome),
                "visitor_name": recap.get("visitor_name", ""),
            }
        except asyncio.TimeoutError:
            _LOGGER.debug("Live recap request timed out after %.1fs", timeout)
        except Exception:
            _LOGGER.debug("Live recap parsing failed", exc_info=True)
        finally:
            self._recap_future = None  # type: ignore[assignment]
        return None

    async def _inject_reference_images(self) -> None:
        if not self._reference_images:
            return
        parts: list[types.Part] = [
            types.Part(text=(
                "The following are reference photos of known people/animals. "
                "Use these to identify visitors in the live camera feed."
            )),
        ]
        for ref in self._reference_images:
            image_bytes = base64.b64decode(ref["image_base64"])
            parts.append(types.Part(inline_data=types.Blob(data=image_bytes, mime_type="image/jpeg")))
            parts.append(types.Part(text=f"[Reference: {ref['caption']}]"))
        try:
            await self._session.send_client_content(
                turns=[types.Content(role="user", parts=parts)],
                turn_complete=False,  # Context only — don't trigger a response
            )
            self._reference_images_injected = True
            # Small delay after injecting reference images to let server process
            await asyncio.sleep(0.3)
        except Exception:
            _LOGGER.exception("Failed to inject reference images")

    async def inject_pending_reference_images(self) -> None:
        """Inject deferred static reference images once the first turn is done."""
        if self._reference_images_injected or not self._reference_images:
            return
        if not self._session or not self._connected:
            return
        await self._inject_reference_images()

    async def _receive_loop(self) -> None:
        reconnect_attempts = 0
        max_reconnects = 15  # INCREASED: more resilient to transient policy violations
        try:
            # NOTE: In the GenAI SDK, session.receive() can complete after a turn.
            # Re-enter receive() to keep the live session open across turns.
            consecutive_empty_iters = 0
            while self._connected and self._session:
                saw_message = False
                try:
                    async for response in self._session.receive():
                        saw_message = True
                        await self._process(response)
                except Exception as recv_err:
                    # Clear ALL state flags on error (stream is broken)
                    self._model_generating = False
                    self._tool_call_pending = False
                    self._vision_paused = False
                    err_str = str(recv_err)
                    t_err = time.time() - self._connect_time if hasattr(self, '_connect_time') else -1
                    _LOGGER.warning(
                        "Gemini receive() raised at T+%.1f: %s (turns=%d)",
                        t_err,
                        err_str[:200],
                        self._turns_completed,
                    )
                    if ("close" in err_str.lower() or "1008" in err_str
                            or "1011" in err_str or "deadline" in err_str.lower()
                            or "not implemented" in err_str.lower()):
                        # RECONNECT LOGIC: try to reconnect if within reconnection budget
                        if reconnect_attempts < max_reconnects:
                            reconnect_attempts += 1
                            # Increasing delay to avoid rapid reconnect loops
                            delay = 0.5 * min(reconnect_attempts, 4)
                            _LOGGER.warning(
                                "Transient Gemini disconnect (attempt %d/%d) — reconnecting in %.1fs...",
                                reconnect_attempts, max_reconnects, delay,
                            )
                            await asyncio.sleep(delay)
                            if await self._reconnect_session(
                                turns_completed=self._turns_completed
                            ):
                                consecutive_empty_iters = 0
                                continue
                        _LOGGER.warning("Gemini server closed connection permanently")
                        break
                    if not self._connected:
                        break
                    # Transient error — try re-entering
                    await asyncio.sleep(0.2)
                    continue

                # Full response stream consumed — safe to ungate vision/audio
                self._model_generating = False
                self._tool_call_pending = False  # Safety: ensure not stuck
                self._vision_paused = False  # Safety: ensure vision resumes

                if not self._connected:
                    break

                if saw_message:
                    self._turns_completed += 1
                    turns_completed = self._turns_completed
                    consecutive_empty_iters = 0
                    if self._on_turn_complete:
                        try:
                            self._on_turn_complete(turns_completed)
                        except Exception:
                            _LOGGER.debug("Gemini on_turn_complete callback failed", exc_info=True)
                    _LOGGER.warning("Gemini turn %d completed, re-entering receive() — waiting for speech", turns_completed)
                    continue

                consecutive_empty_iters += 1
                if consecutive_empty_iters >= 3:
                    # Early empty-stream disconnect — try to reconnect
                    if reconnect_attempts < max_reconnects:
                        reconnect_attempts += 1
                        _LOGGER.warning(
                            "Empty-stream Gemini disconnect (attempt %d/%d) — reconnecting...",
                            reconnect_attempts, max_reconnects,
                        )
                        if await self._reconnect_session(
                            turns_completed=self._turns_completed
                        ):
                            consecutive_empty_iters = 0
                            continue
                    _LOGGER.warning(
                        "Gemini receive stream ended repeatedly with no events (turns=%d)",
                        self._turns_completed,
                    )
                    break
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("Gemini receive loop error (turns=%d)", self._turns_completed)
        finally:
            _LOGGER.warning(
                "Gemini receive loop exiting (turns=%d, connected=%s)",
                self._turns_completed,
                self._connected,
            )
            if self._connected:
                if self._suppress_session_end:
                    self._suppress_session_end = False
                else:
                    self._connected = False
                    self._on_session_end()

    @staticmethod
    def _is_thinking_text(text: str) -> bool:
        """Detect model internal thinking output (not actual speech)."""
        stripped = text.strip()
        # Gemini thinking outputs are typically wrapped in ** markers
        return stripped.startswith("**") and stripped.endswith("**")

    async def _process(self, response: Any) -> None:
        server_content = getattr(response, "server_content", None)
        tool_call = getattr(response, "tool_call", None)

        if server_content and server_content.model_turn:
            # Track model generation state (informational)
            self._model_generating = True
            # Model is responding — tool call cycle is complete
            self._tool_call_pending = False
            # Stop keepalive now that model is generating (no idle timeout risk)
            self._stop_keepalive()
            # NOTE: Do NOT clear _vision_paused here! If a tool image was injected,
            # vision must stay paused until turn_complete so live frames don't flood
            # the model's context while it's still analyzing the tool snapshot.
            for part in server_content.model_turn.parts:
                if part.inline_data and part.inline_data.mime_type and "audio" in part.inline_data.mime_type:
                    # MUTE audio during monitor turns (proactive checks should be silent)
                    if not self._monitor_turn_active:
                        self._on_audio_output(part.inline_data.data)
                elif part.text:
                    if bool(getattr(part, "thought", False)):
                        _LOGGER.debug("Filtered Gemini thought part: %s", part.text[:80])
                        continue
                    # Filter out thinking/planning text (not actual speech)
                    if not self._is_thinking_text(part.text):
                        self._conversation_turns.append({"role": "assistant", "text": part.text})
                        # Only emit transcript if not a muted monitor turn
                        if not self._monitor_turn_active:
                            self._on_transcript("assistant", part.text)
                    else:
                        _LOGGER.debug("Filtered thinking text: %s", part.text[:80])
                    # Resolve recap future if waiting (thinking or not)
                    if hasattr(self, "_recap_future") and self._recap_future and not self._recap_future.done():
                        self._recap_future.set_result(part.text)

        if server_content and hasattr(server_content, "input_transcription"):
            t = getattr(server_content, "input_transcription", None)
            if t and hasattr(t, "text") and t.text:
                _LOGGER.info("Input transcription: %s", t.text[:200])
                self._conversation_turns.append({"role": "user", "text": t.text})
                self._on_transcript("user", t.text)

        if server_content and hasattr(server_content, "output_transcription"):
            t = getattr(server_content, "output_transcription", None)
            if t and hasattr(t, "text") and t.text:
                # Filter out thinking/planning text from output transcription
                if not self._is_thinking_text(t.text):
                    # Don't pollute conversation history with muted monitor chatter
                    if not self._monitor_turn_active:
                        self._conversation_turns.append({"role": "assistant", "text": t.text})
                        self._on_transcript("assistant", t.text)
                # Resolve recap future from transcription (thinking or not)
                if hasattr(self, "_recap_future") and self._recap_future and not self._recap_future.done():
                    self._recap_future.set_result(t.text)

        # Track model generation state via turn signals
        if server_content:
            interrupted = getattr(server_content, "interrupted", None)
            turn_complete = getattr(server_content, "turn_complete", None)
            if interrupted:
                _LOGGER.debug("Gemini: model output was INTERRUPTED (user started speaking)")
                self._monitor_turn_active = False
                # Resume vision on interrupt — the model won't finish analyzing anyway
                self._vision_paused = False
            if turn_complete:
                _LOGGER.debug("Gemini: turn_complete signal received")
                # Clear monitor mute at end of turn
                self._monitor_turn_active = False
                # Resume vision loop now that the model has finished its response
                # about the tool image (if any). Safe to send live frames again.
                self._vision_paused = False
                # Don't clear _model_generating here — there may be a tool_call
                # still pending in the same receive() stream. We clear it in
                # _receive_loop after the async for completes.

        if tool_call:
            await self._handle_tool_call(tool_call)

    async def _handle_tool_call(self, tool_call: Any) -> None:
        self._tool_call_pending = True
        # Start keepalive to prevent server idle timeout during tool execution
        self._start_keepalive()
        # Tool call received — model is no longer in monitor mode
        # (unless it's no_action_needed, which is silent anyway)
        has_no_action = any(
            fc.name == "no_action_needed" for fc in tool_call.function_calls
        )
        if not has_no_action:
            self._monitor_turn_active = False
        try:
            function_responses: list[types.FunctionResponse] = []
            pending_image_for_injection: tuple[str, str] | None = None

            for fc in tool_call.function_calls:
                try:
                    result = await self._on_tool_call(fc.name, fc.args or {})
                except Exception:
                    result = {"error": f"Tool '{fc.name}' execution failed"}

                # Extract pending tool image for injection AFTER tool response
                if self._pending_tool_image:
                    if len(self._pending_tool_image) == 2:
                        image_b64, pending_image_mime = self._pending_tool_image
                    else:
                        image_b64, pending_image_mime, _pending_image_ctx = self._pending_tool_image
                    self._pending_tool_image = None
                    pending_image_for_injection = (image_b64, pending_image_mime)
                elif fc.name == "view_camera":
                    _LOGGER.warning("view_camera completed but no _pending_tool_image was set!")

                function_response_kwargs: dict[str, Any] = {
                    "id": fc.id,
                    "name": fc.name,
                    "response": result,
                }

                function_responses.append(types.FunctionResponse(**function_response_kwargs))

            if self._session and self._connected:
                # Inject tool image BEFORE tool_response so it's in the model's
                # conversation context when generation starts. send_tool_response
                # triggers generation — any image sent after would arrive too late.
                if pending_image_for_injection and self._session and self._connected:
                    img_b64, img_mime = pending_image_for_injection
                    try:
                        img_bytes = base64.b64decode(img_b64)
                        image_part = types.Part(
                            inline_data=types.Blob(data=img_bytes, mime_type=img_mime)
                        )

                        # 1. Inject into conversation context via send_client_content
                        await self._session.send_client_content(
                            turns=[types.Content(
                                role="user",
                                parts=[
                                    types.Part(text=(
                                        "[CAMERA SNAPSHOT FOR ANALYSIS]\n"
                                        "The image attached to this message is a HIGH-RESOLUTION "
                                        "snapshot just captured from the requested camera.\n"
                                        "IMPORTANT: This is NOT from your live video feed. "
                                        "This is a dedicated snapshot you MUST analyze carefully.\n"
                                        "Look at this image and identify ALL objects: "
                                        "vehicles (count them, state their colors), people, "
                                        "furniture, plants, animals. Be thorough and accurate."
                                    )),
                                    image_part,
                                ],
                            )],
                            turn_complete=False,
                        )

                        # 2. Also inject into the realtime video buffer so the model's
                        # "current view" matches the snapshot (prevents it from seeing
                        # the primary camera feed instead of the target camera)
                        try:
                            await self._session.send_realtime_input(
                                media=types.Blob(data=img_bytes, mime_type=img_mime)
                            )
                        except Exception:
                            pass  # Non-critical: conversation context is the primary method

                        # 3. Small delay to ensure server processes the image before
                        # we send tool_response (which triggers generation)
                        await asyncio.sleep(0.3)

                        _LOGGER.warning(
                            "Tool image injected (context+realtime) BEFORE tool_response (%d bytes)",
                            len(img_bytes),
                        )
                    except Exception:
                        _LOGGER.exception("Failed to inject tool image via send_client_content")

                # Now send tool_response (this triggers model generation).
                # The image is already in context so the model will see it.
                try:
                    await self._session.send_tool_response(
                        function_responses=function_responses
                    )
                except Exception:
                    _LOGGER.exception("Failed to send tool response")
                    self._tool_call_pending = False
                    self._stop_keepalive()
                    return
            else:
                # Session gone — clear pending state
                self._tool_call_pending = False
                self._stop_keepalive()
        finally:
            # NOTE: Do NOT stop keepalive here — the gap between send_tool_response
            # and model generation starting is the most dangerous idle period.
            # The keepalive loop self-terminates when _model_generating becomes True.
            # NOTE: We intentionally do NOT clear _tool_call_pending here.
            # It stays True until the model starts generating its response
            # (detected in _process via model_turn). This prevents mic audio
            # from flowing in the gap between tool_response and model output,
            # which would cause a 1008 policy violation.
            pass
