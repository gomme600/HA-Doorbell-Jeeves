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

    @property
    def connected(self) -> bool:
        return self._connected

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

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice)
                )
            ),
            system_instruction=types.Content(parts=[types.Part(text=self._system_prompt)]),
            tools=self._tools if self._tools else None,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

        self._session_cm = self._client.aio.live.connect(
            model=self._model, config=live_config,
        )
        self._session = await self._session_cm.__aenter__()
        self._connected = True

        await self._inject_reference_images()
        self._receive_task = asyncio.create_task(self._receive_loop())
        _LOGGER.info("Gemini Live connected (model=%s, voice=%s)", self._model, self._voice)

    async def disconnect(self) -> None:
        self._connected = False
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
        try:
            await self._session.send_realtime_input(
                audio=types.Blob(
                    data=pcm_bytes,
                    mime_type=f"audio/pcm;rate={AUDIO_INPUT_SAMPLE_RATE}",
                )
            )
        except Exception:
            _LOGGER.exception("Failed to send audio")

    async def send_image(self, image_base64: str, mime_type: str = "image/jpeg") -> None:
        if not self._session or not self._connected:
            return
        try:
            image_bytes = base64.b64decode(image_base64)
            await self._session.send_realtime_input(
                video=types.Blob(data=image_bytes, mime_type=mime_type)
            )
        except Exception:
            _LOGGER.exception("Failed to send image")

    async def inject_context(self, text: str) -> None:
        """Inject a text message into the live session (used by tool router for results)."""
        if not self._session or not self._connected:
            return
        try:
            await self._session.send(
                input=types.LiveClientContent(
                    turns=[types.Content(role="user", parts=[types.Part(text=text)])],
                    turn_complete=True,
                )
            )
        except Exception:
            _LOGGER.exception("Failed to inject context")

    async def request_recap(self, outcome: str, timeout: float = 8.0) -> dict[str, str] | None:
        """Ask the live model to generate a session recap as text before disconnecting.

        Returns parsed recap dict or None if the model doesn't support text output.
        Currently, native audio dialog models only support AUDIO modality, so this
        will return None immediately. Kept for future models that support mixed output.
        """
        # Native audio models can only output audio — skip to avoid timeout delay
        if "native-audio" in self._model:
            return None

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
            await self._session.send(
                input=types.LiveClientContent(
                    turns=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                    turn_complete=True,
                )
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
            await self._session.send(
                input=types.LiveClientContent(
                    turns=[types.Content(role="user", parts=parts)],
                    turn_complete=True,
                )
            )
        except Exception:
            _LOGGER.exception("Failed to inject reference images")

    async def _receive_loop(self) -> None:
        try:
            # NOTE: In the GenAI SDK, session.receive() can complete after a turn.
            # Re-enter receive() to keep the live session open across turns.
            consecutive_empty_iters = 0
            while self._connected and self._session:
                saw_message = False
                async for response in self._session.receive():
                    saw_message = True
                    await self._process(response)

                if not self._connected:
                    break

                if saw_message:
                    consecutive_empty_iters = 0
                    continue

                consecutive_empty_iters += 1
                if consecutive_empty_iters >= 3:
                    _LOGGER.warning("Gemini receive stream ended repeatedly with no events")
                    break
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("Gemini receive loop error")
        finally:
            if self._connected:
                self._connected = False
                self._on_session_end()

    async def _process(self, response: Any) -> None:
        server_content = getattr(response, "server_content", None)
        tool_call = getattr(response, "tool_call", None)

        if server_content and server_content.model_turn:
            for part in server_content.model_turn.parts:
                if part.inline_data and part.inline_data.mime_type and "audio" in part.inline_data.mime_type:
                    self._on_audio_output(part.inline_data.data)
                elif part.text:
                    self._conversation_turns.append({"role": "assistant", "text": part.text})
                    self._on_transcript("assistant", part.text)
                    # Resolve recap future if waiting
                    if hasattr(self, "_recap_future") and self._recap_future and not self._recap_future.done():
                        self._recap_future.set_result(part.text)

        if server_content and hasattr(server_content, "input_transcription"):
            t = getattr(server_content, "input_transcription", None)
            if t and hasattr(t, "text") and t.text:
                self._conversation_turns.append({"role": "user", "text": t.text})
                self._on_transcript("user", t.text)

        if server_content and hasattr(server_content, "output_transcription"):
            t = getattr(server_content, "output_transcription", None)
            if t and hasattr(t, "text") and t.text:
                self._conversation_turns.append({"role": "assistant", "text": t.text})
                self._on_transcript("assistant", t.text)

        if tool_call:
            await self._handle_tool_call(tool_call)

    async def _handle_tool_call(self, tool_call: Any) -> None:
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
                await self._session.send(
                    input=types.LiveClientToolResponse(function_responses=function_responses)
                )
            except Exception:
                _LOGGER.exception("Failed to send tool response")
