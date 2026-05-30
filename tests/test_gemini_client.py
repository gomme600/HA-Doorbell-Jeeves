from __future__ import annotations

import asyncio
from typing import Any

import custom_components.ha_doorbell_jeeves.gemini_client as gc_module
from custom_components.ha_doorbell_jeeves.gemini_client import GeminiLiveClient


class _FakeSession:
    def __init__(self) -> None:
        self.client_content_calls: list[tuple[Any, bool]] = []
        self.realtime_input_calls: list[dict[str, Any]] = []

    async def send_client_content(self, *, turns: Any, turn_complete: bool = True) -> None:
        self.client_content_calls.append((turns, turn_complete))

    async def send_realtime_input(self, **kwargs: Any) -> None:
        self.realtime_input_calls.append(kwargs)


class _FakeSessionCM:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.enter_calls = 0
        self.exit_calls = 0

    async def __aenter__(self) -> _FakeSession:
        self.enter_calls += 1
        return self.session

    async def __aexit__(self, *_args: Any) -> None:
        self.exit_calls += 1


class _FakeLive:
    def __init__(self) -> None:
        self.connect_calls: list[tuple[str, Any, _FakeSessionCM, _FakeSession]] = []

    def connect(self, *, model: str, config: Any) -> _FakeSessionCM:
        session = _FakeSession()
        cm = _FakeSessionCM(session)
        self.connect_calls.append((model, config, cm, session))
        return cm


class _FakeGenAIClient:
    instances: list["_FakeGenAIClient"] = []

    def __init__(self, *, api_key: str) -> None:
        self.api_key = api_key
        self.aio = type("_FakeAio", (), {"live": _FakeLive()})()
        _FakeGenAIClient.instances.append(self)


def _build_client() -> GeminiLiveClient:
    return GeminiLiveClient(
        api_key="k",
        model="gemini-test-model",
        system_prompt="prompt",
        tools=[],
        voice="Puck",
        reference_images=[],
        on_audio_output=lambda _audio: None,
        on_tool_call=lambda _name, _args: asyncio.sleep(0, result={"success": True}),
        on_session_end=lambda: None,
        on_transcript=lambda _role, _text: None,
    )


def _reset_shared_state() -> None:
    gc_module._SHARED_CLIENTS.clear()
    gc_module._SHARED_CLIENT_LOCKS.clear()
    gc_module._WARMED_LIVE_MODELS.clear()
    gc_module._WARM_LOCKS.clear()
    _FakeGenAIClient.instances.clear()


def test_gemini_prewarm_reuses_shared_client(monkeypatch: Any) -> None:
    async def _run() -> None:
        monkeypatch.setattr(gc_module.genai, "Client", _FakeGenAIClient)
        _reset_shared_state()

        first = await GeminiLiveClient.prewarm_shared_client("k", "gemini-test-model")
        second = await GeminiLiveClient.prewarm_shared_client("k", "gemini-test-model")

        assert first is second
        assert len(_FakeGenAIClient.instances) == 1
        assert len(first.aio.live.connect_calls) == 1

    asyncio.run(_run())


def test_gemini_connect_uses_prewarmed_shared_client(monkeypatch: Any) -> None:
    async def _run() -> None:
        monkeypatch.setattr(gc_module.genai, "Client", _FakeGenAIClient)
        _reset_shared_state()

        shared = await GeminiLiveClient.prewarm_shared_client("k", "gemini-test-model")

        client = _build_client()

        async def _noop_reference_images() -> None:
            return None

        async def _noop_receive_loop() -> None:
            return None

        client._inject_reference_images = _noop_reference_images  # type: ignore[method-assign]
        client._receive_loop = _noop_receive_loop  # type: ignore[method-assign]

        await client.connect(greeting_text="hello")

        assert client._client is shared
        assert len(_FakeGenAIClient.instances) == 1
        assert len(shared.aio.live.connect_calls) == 2
        actual_session = shared.aio.live.connect_calls[1][3]
        assert actual_session.client_content_calls
        assert actual_session.client_content_calls[0][1] is True

        await client.disconnect()

    asyncio.run(_run())
