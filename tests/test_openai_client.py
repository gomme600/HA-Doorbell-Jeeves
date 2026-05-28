from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from custom_components.ha_doorbell_jeeves.openai_client import OpenAIRealtimeClient


class _FakeWS:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.response_calls = 0
        self.conversation = SimpleNamespace(item=SimpleNamespace(create=self._create_item))
        self.response = SimpleNamespace(create=self._create_response)

    async def _create_item(self, *, item: dict[str, Any]) -> None:
        self.items.append(item)

    async def _create_response(self) -> None:
        self.response_calls += 1


class _Event:
    def __init__(self, *, call_id: str, name: str, arguments: str) -> None:
        self.call_id = call_id
        self.name = name
        self.arguments = arguments


def _build_client(on_tool_call: Any) -> OpenAIRealtimeClient:
    return OpenAIRealtimeClient(
        api_key="k",
        model="model",
        base_url=None,
        system_prompt="prompt",
        tools=[],
        voice="alloy",
        reference_images=[],
        on_audio_output=lambda _audio: None,
        on_tool_call=on_tool_call,
        on_session_end=lambda: None,
        on_transcript=lambda _role, _text: None,
    )


def test_openai_function_call_injects_pending_image_after_output() -> None:
    async def _run() -> None:
        async def _tool_call(_name: str, _args: dict[str, Any]) -> dict[str, Any]:
            return {"success": True}

        client = _build_client(_tool_call)
        ws = _FakeWS()
        client._ws = ws
        client._connected = True
        client._pending_tool_image = ("aGVsbG8=", "image/jpeg")

        await client._handle_function_call(
            _Event(call_id="call-1", name="test_tool", arguments='{"confirm": true}')
        )

        assert ws.items[0]["type"] == "function_call_output"
        assert ws.items[1]["type"] == "message"
        assert ws.items[1]["content"][0]["type"] == "input_image"
        assert ws.items[1]["content"][0]["image"] == "aGVsbG8="
        assert ws.response_calls == 1
        assert client._pending_tool_image is None

    asyncio.run(_run())


def test_openai_function_call_handles_malformed_json_arguments() -> None:
    async def _run() -> None:
        captured: list[dict[str, Any]] = []

        async def _tool_call(_name: str, args: dict[str, Any]) -> dict[str, Any]:
            captured.append(args)
            return {"success": True}

        client = _build_client(_tool_call)
        ws = _FakeWS()
        client._ws = ws
        client._connected = True

        await client._handle_function_call(
            _Event(call_id="call-2", name="test_tool", arguments='{"broken-json"')
        )

        assert captured == [{}]
        assert ws.items[0]["type"] == "function_call_output"
        assert ws.response_calls == 1

    asyncio.run(_run())

