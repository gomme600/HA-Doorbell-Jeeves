"""OpenAI Realtime API client – supports cloud and local OpenAI-compatible models."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import openai

from .client_base import BaseRealtimeClient

_LOGGER = logging.getLogger(__name__)


class OpenAIRealtimeClient(BaseRealtimeClient):
    """Client for the OpenAI Realtime API (and compatible endpoints)."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None,
        system_prompt: str,
        tools: list[dict[str, Any]],
        voice: str,
        reference_images: list[dict[str, str]],
        on_audio_output: Callable[[bytes], None],
        on_tool_call: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        on_session_end: Callable[[], None],
        on_transcript: Callable[[str, str], None],
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._system_prompt = system_prompt
        self._tools = tools
        self._voice = voice
        self._reference_images = reference_images
        self._on_audio_output = on_audio_output
        self._on_tool_call = on_tool_call
        self._on_session_end = on_session_end
        self._on_transcript = on_transcript

        self._connection: Any = None
        self._ws: Any = None
        self._receive_task: asyncio.Task[None] | None = None
        self._connected = False
        self._conversation_turns: list[dict[str, str]] = []
        self._pending_tool_image: tuple[str, str] | None = None

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
        client_kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            client_kwargs["base_url"] = self._base_url

        client = openai.AsyncOpenAI(**client_kwargs)
        self._connection = await client.beta.realtime.connect(model=self._model)
        self._ws = await self._connection.__aenter__()
        self._connected = True

        await self._configure_session()
        await self._inject_reference_images()
        self._receive_task = asyncio.create_task(self._receive_loop())
        _LOGGER.info("OpenAI Realtime connected (model=%s, voice=%s)", self._model, self._voice)

    async def disconnect(self) -> None:
        self._connected = False
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._connection:
            try:
                await self._connection.__aexit__(None, None, None)
            except Exception:
                pass
            self._connection = None
        self._conversation_turns.clear()
        _LOGGER.info("OpenAI Realtime disconnected")

    async def send_audio(self, pcm_bytes: bytes) -> None:
        if not self._ws or not self._connected:
            return
        try:
            audio_b64 = base64.b64encode(pcm_bytes).decode("ascii")
            await self._ws.input_audio_buffer.append(audio=audio_b64)
        except Exception:
            _LOGGER.exception("Failed to send audio to OpenAI")

    async def send_image(self, image_base64: str, mime_type: str = "image/jpeg") -> None:
        if not self._ws or not self._connected:
            return
        try:
            await self._ws.conversation.item.create(
                item={
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image": image_base64}],
                }
            )
        except Exception:
            _LOGGER.exception("Failed to send image to OpenAI")

    async def inject_context(self, text: str) -> None:
        """Inject a text message into the realtime session (used by tool router for results)."""
        if not self._ws or not self._connected:
            return
        try:
            await self._ws.conversation.item.create(
                item={
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                }
            )
        except Exception:
            _LOGGER.exception("Failed to inject context to OpenAI")

    async def _configure_session(self) -> None:
        session_config: dict[str, Any] = {
            "modalities": ["text", "audio"],
            "instructions": self._system_prompt,
            "voice": self._voice,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
            },
        }
        if self._tools:
            session_config["tools"] = self._tools
            session_config["tool_choice"] = "auto"
        try:
            await self._ws.session.update(session=session_config)
        except Exception:
            _LOGGER.exception("Failed to configure OpenAI session")

    async def _inject_reference_images(self) -> None:
        if not self._reference_images:
            return
        for ref in self._reference_images:
            try:
                await self._ws.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": f"Reference photo: {ref['caption']}"},
                            {"type": "input_image", "image": ref["image_base64"]},
                        ],
                    }
                )
            except Exception:
                _LOGGER.debug("Failed to inject reference image for %s", ref.get("caption"))

    async def _receive_loop(self) -> None:
        try:
            async for event in self._ws:
                await self._process_event(event)
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("OpenAI receive loop error")
        finally:
            if self._connected:
                self._connected = False
                self._on_session_end()

    async def _process_event(self, event: Any) -> None:
        event_type = getattr(event, "type", "")

        if event_type == "response.audio.delta":
            audio_bytes = base64.b64decode(event.delta)
            self._on_audio_output(audio_bytes)
        elif event_type == "conversation.item.input_audio_transcription.completed":
            text = getattr(event, "transcript", "")
            if text:
                self._conversation_turns.append({"role": "user", "text": text})
                self._on_transcript("user", text)
        elif event_type == "response.audio_transcript.delta":
            text = getattr(event, "delta", "")
            if text:
                self._conversation_turns.append({"role": "assistant", "text": text})
                self._on_transcript("assistant", text)
        elif event_type == "response.function_call_arguments.done":
            await self._handle_function_call(event)

    async def _handle_function_call(self, event: Any) -> None:
        call_id = getattr(event, "call_id", "")
        name = getattr(event, "name", "")
        arguments_str = getattr(event, "arguments", "{}")

        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError:
            arguments = {}

        try:
            result = await self._on_tool_call(name, arguments)
        except Exception:
            result = {"error": f"Tool '{name}' execution failed"}

        if self._ws and self._connected:
            try:
                await self._ws.conversation.item.create(
                    item={
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result),
                    }
                )
                pending_tool_image = self._pending_tool_image
                self._pending_tool_image = None
                if pending_tool_image:
                    image_b64, mime_type = pending_tool_image
                    await self.send_image(image_b64, mime_type=mime_type)
                await self._ws.response.create()
            except Exception:
                _LOGGER.exception("Failed to send function output")
