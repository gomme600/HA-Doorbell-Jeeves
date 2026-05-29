"""Gemini Multimodal Live API client implementation."""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from google import genai
from google.genai import types

from .client_base import BaseRealtimeClient
from .const import AUDIO_INPUT_SAMPLE_RATE

_LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._system_prompt = system_prompt
        self._tools = tools
        self._voice = voice
        self._reference_images = reference_images
        self._on_audio_output = on_audio_output
        self._on_tool_call = on_tool_call
        self._on_session_end = on_session_end
        self._on_transcript = on_transcript

        self._client: genai.Client | None = None
        self._session: Any = None
        self._session_cm: Any = None
        self._receive_task: asyncio.Task[None] | None = None
        self._connected = False
        self._conversation_turns: list[dict[str, str]] = []
        self._recap_future: asyncio.Future[str] | None = None
        self._pending_tool_image: tuple[str, str, str] | None = None
        self._tool_call_pending = False
        # Track model generation state (informational — used by reconnect logic)
        self._model_generating = False

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

    async def connect(self) -> None:
        # genai.Client() does blocking I/O (SSL cert loading) — run in executor
        loop = asyncio.get_event_loop()
        self._client = await loop.run_in_executor(
            None, lambda: genai.Client(api_key=self._api_key)
        )

        self._live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice)
                )
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

        self._session_cm = self._client.aio.live.connect(
            model=self._model, config=self._live_config,
        )
        self._session = await self._session_cm.__aenter__()
        self._connected = True

        await self._inject_reference_images()
        self._receive_task = asyncio.create_task(self._receive_loop())
        _LOGGER.info("Gemini Live connected (model=%s, voice=%s)", self._model, self._voice)

    async def _reconnect_session(self) -> bool:
        """Tear down current session and open a new one (same config). Returns True on success."""
        try:
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
            await self._inject_reference_images()

            # Build proper conversation history with alternating user/model roles
            # This restores the model's memory of what IT said, preventing greeting repeat
            history_turns: list[types.Content] = []
            if self._conversation_turns:
                recent = self._conversation_turns[-15:]
                # Group consecutive same-role entries and build proper Content turns
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
                # Flush last group
                if current_role and current_texts:
                    history_turns.append(types.Content(
                        role=current_role,
                        parts=[types.Part(text=" ".join(current_texts))]
                    ))

            # Always end with a user turn to set context
            history_turns.append(types.Content(role="user", parts=[types.Part(text=(
                "[SYSTEM] Connection briefly interrupted. You already greeted this visitor. "
                "Do NOT repeat your greeting or re-introduce yourself. "
                "Wait silently for the visitor to speak next."
            ))]))

            # turn_complete=False: the user "hasn't finished" — model should not respond
            await self._session.send_client_content(
                turns=history_turns,
                turn_complete=False,
            )
            self._model_generating = False
            _LOGGER.warning("Gemini reconnected — history restored (%d turns, %d conversation entries)",
                          len(history_turns), len(self._conversation_turns))
            return True
        except Exception:
            _LOGGER.exception("Gemini reconnect failed")
            return False

    async def disconnect(self) -> None:
        self._connected = False
        self._tool_call_pending = False
        self._model_generating = False
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
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
        try:
            image_bytes = base64.b64decode(image_base64)
            await self._session.send_realtime_input(
                media=types.Blob(data=image_bytes, mime_type=mime_type)
            )
        except Exception:
            if self._connected:
                _LOGGER.debug("Failed to send image")

    async def inject_context(
        self, text: str, image_base64: str | None = None,
        mime_type: str = "image/jpeg", turn_complete: bool = True,
    ) -> None:
        """Inject a text message (and optional image) into the live session."""
        if not self._session or not self._connected:
            return
        try:
            parts = [types.Part(text=text)]
            if image_base64:
                image_bytes = base64.b64decode(image_base64)
                parts.append(types.Part(inline_data=types.Blob(data=image_bytes, mime_type=mime_type)))

            await self._session.send_client_content(
                turns=[types.Content(role="user", parts=parts)],
                turn_complete=turn_complete,
            )
        except Exception:
            _LOGGER.exception("Failed to inject context")

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
        except Exception:
            _LOGGER.exception("Failed to inject reference images")

    async def _receive_loop(self) -> None:
        turns_completed = 0
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
                    # Clear generation flag on any error (stream is done)
                    self._model_generating = False
                    err_str = str(recv_err)
                    _LOGGER.warning("Gemini receive() raised: %s (turns=%d)", err_str[:200], turns_completed)
                    if "close" in err_str.lower() or "1008" in err_str or "not implemented" in err_str.lower():
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
                            if await self._reconnect_session():
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

                if not self._connected:
                    break

                if saw_message:
                    turns_completed += 1
                    consecutive_empty_iters = 0
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
                        if await self._reconnect_session():
                            consecutive_empty_iters = 0
                            continue
                    _LOGGER.warning("Gemini receive stream ended repeatedly with no events (turns=%d)", turns_completed)
                    break
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("Gemini receive loop error (turns=%d)", turns_completed)
        finally:
            _LOGGER.warning("Gemini receive loop exiting (turns=%d, connected=%s)", turns_completed, self._connected)
            if self._connected:
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
            for part in server_content.model_turn.parts:
                if part.inline_data and part.inline_data.mime_type and "audio" in part.inline_data.mime_type:
                    self._on_audio_output(part.inline_data.data)
                elif part.text:
                    # Filter out thinking/planning text (not actual speech)
                    if not self._is_thinking_text(part.text):
                        self._conversation_turns.append({"role": "assistant", "text": part.text})
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
                # Don't clear _model_generating here — wait for receive() to end
            if turn_complete:
                _LOGGER.debug("Gemini: turn_complete signal received")
                # Don't clear _model_generating here — there may be a tool_call
                # still pending in the same receive() stream. We clear it in
                # _receive_loop after the async for completes.

        if tool_call:
            await self._handle_tool_call(tool_call)

    async def _handle_tool_call(self, tool_call: Any) -> None:
        self._tool_call_pending = True
        try:
            function_responses: list[types.FunctionResponse] = []
            for fc in tool_call.function_calls:
                try:
                    result = await self._on_tool_call(fc.name, fc.args or {})
                except Exception:
                    result = {"error": f"Tool '{fc.name}' execution failed"}
                function_responses.append(
                    types.FunctionResponse(id=fc.id, name=fc.name, response=result)
                )
            if self._session and self._connected:
                try:
                    await self._session.send_tool_response(
                        function_responses=function_responses
                    )
                except Exception:
                    _LOGGER.exception("Failed to send tool response")

            # Inject pending tool image IMMEDIATELY after the tool response
            # (before releasing the gate) so the model processes the image
            # without interference from the live video stream.
            if self._session and self._connected and self._pending_tool_image:
                if len(self._pending_tool_image) == 2:
                    image_b64, mime_type = self._pending_tool_image
                    image_context = (
                        "[SYSTEM] IMAGE CAPTURED. The requested visual snapshot has been injected "
                        "below. Analyze THIS IMAGE carefully for your response."
                    )
                else:
                    image_b64, mime_type, image_context = self._pending_tool_image
                self._pending_tool_image = None
                await self.inject_context(
                    image_context,
                    image_base64=image_b64,
                    mime_type=mime_type
                )
                # Keep gate closed briefly so live frames don't immediately drown
                # out the high-quality tool snapshot the model needs to analyze
                await asyncio.sleep(2.0)
        finally:
            self._tool_call_pending = False
