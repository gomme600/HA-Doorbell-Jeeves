from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from typing import Any

import custom_components.ha_doorbell_jeeves.gemini_client as gc_module
from custom_components.ha_doorbell_jeeves.gemini_client import GeminiLiveClient


class _FakeSession:
    def __init__(self) -> None:
        self.client_content_calls: list[tuple[Any, bool]] = []
        self.realtime_input_calls: list[dict[str, Any]] = []
        self.tool_response_calls: list[Any] = []

    async def send_client_content(self, *, turns: Any, turn_complete: bool = True) -> None:
        self.client_content_calls.append((turns, turn_complete))

    async def send_realtime_input(self, **kwargs: Any) -> None:
        self.realtime_input_calls.append(kwargs)

    async def send_tool_response(self, *, function_responses: Any) -> None:
        self.tool_response_calls.append(function_responses)

    async def receive(self) -> Any:
        yield SimpleNamespace(server_content=SimpleNamespace(model_turn=object()))


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


def _build_client(*, tools: list[Any] | None = None, defer_tools_until_turn_one: bool = False) -> GeminiLiveClient:
    return GeminiLiveClient(
        api_key="k",
        model="gemini-test-model",
        system_prompt="prompt",
        tools=tools or [],
        voice="Puck",
        reference_images=[],
        on_audio_output=lambda _audio: None,
        on_tool_call=lambda _name, _args: asyncio.sleep(0, result={"success": True}),
        on_session_end=lambda: None,
        on_transcript=lambda _role, _text: None,
        defer_tools_until_turn_one=defer_tools_until_turn_one,
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
        warmup_session = first.aio.live.connect_calls[0][3]
        assert warmup_session.client_content_calls
        assert warmup_session.client_content_calls[0][1] is True

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


def test_live_config_disables_thinking_budget() -> None:
    client = _build_client()
    config = client._build_live_config()

    assert config.thinking_config is not None
    assert config.thinking_config.thinking_budget == 0
    assert config.thinking_config.include_thoughts is False


def test_tool_response_omits_parts_kwarg_when_no_image(monkeypatch: Any) -> None:
    async def _run() -> None:
        captured_kwargs: list[dict[str, Any]] = []

        def _fake_function_response(**kwargs: Any) -> dict[str, Any]:
            captured_kwargs.append(kwargs)
            return kwargs

        client = _build_client()
        session = _FakeSession()
        client._session = session
        client._connected = True

        monkeypatch.setattr(gc_module.types, "FunctionResponse", _fake_function_response)

        tool_call = SimpleNamespace(
            function_calls=[SimpleNamespace(id="tool-1", name="notify_sebastians_phone", args={})]
        )

        await client._handle_tool_call(tool_call)

        assert len(captured_kwargs) == 1
        assert "parts" not in captured_kwargs[0]
        assert session.tool_response_calls == [[captured_kwargs[0]]]

    asyncio.run(_run())


def test_view_camera_injects_image_via_client_content(monkeypatch: Any) -> None:
    """Test that view_camera tool images are injected via send_client_content BEFORE tool response."""
    async def _run() -> None:
        captured_kwargs: list[dict[str, Any]] = []
        client_content_calls: list[dict[str, Any]] = []
        call_order: list[str] = []

        def _fake_function_response(**kwargs: Any) -> dict[str, Any]:
            captured_kwargs.append(kwargs)
            return kwargs

        client = _build_client()
        session = _FakeSession()
        # Track send_client_content calls with ordering
        async def _track_client_content(**kwargs: Any) -> None:
            client_content_calls.append(kwargs)
            call_order.append("client_content")
        session.send_client_content = _track_client_content

        original_send_tool = session.send_tool_response
        async def _track_tool_response(**kwargs: Any) -> None:
            call_order.append("tool_response")
            await original_send_tool(**kwargs)
        session.send_tool_response = _track_tool_response

        client._session = session
        client._connected = True
        client._pending_tool_image = (
            base64.b64encode(b"jpeg-bytes").decode(),
            "image/jpeg",
        )

        monkeypatch.setattr(gc_module.types, "FunctionResponse", _fake_function_response)

        tool_call = SimpleNamespace(
            function_calls=[SimpleNamespace(id="tool-2", name="view_camera", args={})]
        )

        await client._handle_tool_call(tool_call)

        # Tool response should NOT contain parts (no image in response)
        assert len(captured_kwargs) == 1
        assert "parts" not in captured_kwargs[0]
        # Image should have been injected via send_client_content BEFORE tool_response
        # Two calls: first the image alone, then the analysis instructions text
        assert len(client_content_calls) == 2
        assert "turns" in client_content_calls[0]
        assert "turns" in client_content_calls[1]
        # Verify ordering: image injections come before tool response
        assert call_order == ["client_content", "client_content", "tool_response"]

    asyncio.run(_run())


def test_process_filters_gemini_thought_parts() -> None:
    async def _run() -> None:
        transcripts: list[tuple[str, str]] = []
        client = GeminiLiveClient(
            api_key="k",
            model="gemini-test-model",
            system_prompt="prompt",
            tools=[],
            voice="Puck",
            reference_images=[],
            on_audio_output=lambda _audio: None,
            on_tool_call=lambda _name, _args: asyncio.sleep(0, result={"success": True}),
            on_session_end=lambda: None,
            on_transcript=lambda role, text: transcripts.append((role, text)),
        )

        response = SimpleNamespace(
            server_content=SimpleNamespace(
                model_turn=SimpleNamespace(
                    parts=[
                        SimpleNamespace(text="Internal plan", inline_data=None, thought=True),
                        SimpleNamespace(text="Bonjour, puis-je vous aider ?", inline_data=None, thought=False),
                    ]
                ),
                input_transcription=None,
                output_transcription=None,
                interrupted=False,
                turn_complete=False,
            ),
            tool_call=None,
        )

        await client._process(response)

        assert client._conversation_turns == [
            {"role": "assistant", "text": "Bonjour, puis-je vous aider ?"}
        ]
        assert transcripts == [("assistant", "Bonjour, puis-je vous aider ?")]

    asyncio.run(_run())


def test_enable_deferred_tools_after_greeting_reconnects_once() -> None:
    async def _run() -> None:
        reconnect_calls: list[tuple[int, bool]] = []
        fake_tool = gc_module.types.Tool(
            function_declarations=[
                gc_module.types.FunctionDeclaration(
                    name="noop_tool",
                    description="test tool",
                )
            ]
        )
        client = _build_client(
            tools=[fake_tool],
            defer_tools_until_turn_one=True,
        )
        client._connected = True
        client._session = object()
        client._turns_completed = 1

        async def _fake_reconnect(
            *,
            turns_completed: int = -1,
            restart_receive_task: bool = False,
            post_reconnect_instruction: str = "",
        ) -> bool:
            reconnect_calls.append((turns_completed, restart_receive_task))
            return True

        client._reconnect_session = _fake_reconnect  # type: ignore[method-assign]

        assert client._tools == []

        assert await client.enable_deferred_tools_after_greeting("test instruction") is True

        assert client._tools == [fake_tool]
        assert reconnect_calls == [(1, True)]

    asyncio.run(_run())


def test_controlled_reconnect_restarts_receive_loop_without_session_end() -> None:
    async def _run() -> None:
        session_end_calls: list[str] = []

        class _BlockingSession(_FakeSession):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()

            async def receive(self) -> Any:
                self.started.set()
                await asyncio.Future()
                if False:  # pragma: no cover
                    yield None

        class _BlockingLive:
            def __init__(self) -> None:
                self.connect_calls: list[tuple[str, Any, _FakeSessionCM, _BlockingSession]] = []

            def connect(self, *, model: str, config: Any) -> _FakeSessionCM:
                session = _BlockingSession()
                cm = _FakeSessionCM(session)
                self.connect_calls.append((model, config, cm, session))
                return cm

        blocking_live = _BlockingLive()
        client = GeminiLiveClient(
            api_key="k",
            model="gemini-test-model",
            system_prompt="prompt",
            tools=[],
            voice="Puck",
            reference_images=[],
            on_audio_output=lambda _audio: None,
            on_tool_call=lambda _name, _args: asyncio.sleep(0, result={"success": True}),
            on_session_end=lambda: session_end_calls.append("ended"),
            on_transcript=lambda _role, _text: None,
        )

        current_session = _BlockingSession()
        client._client = SimpleNamespace(aio=SimpleNamespace(live=blocking_live))
        client._live_config = gc_module.types.LiveConnectConfig(response_modalities=["AUDIO"])
        client._session = current_session
        client._session_cm = _FakeSessionCM(current_session)
        client._connected = True
        client._turns_completed = 1
        client._conversation_turns = [
            {"role": "assistant", "text": "Bonjour"},
            {"role": "user", "text": "Pouvez-vous regarder dehors ?"},
        ]

        old_receive_task = asyncio.create_task(client._receive_loop())
        client._receive_task = old_receive_task
        await asyncio.wait_for(current_session.started.wait(), timeout=1.0)

        assert await client._reconnect_session(turns_completed=1, restart_receive_task=True) is True

        assert session_end_calls == []
        assert old_receive_task.done() is True
        assert client._receive_task is not old_receive_task
        assert client._receive_task is not None
        assert client._receive_task.done() is False
        assert len(blocking_live.connect_calls) == 1
        reconnect_session = blocking_live.connect_calls[0][3]
        assert reconnect_session.client_content_calls
        assert reconnect_session.client_content_calls[0][1] is False

        await client.disconnect()

    asyncio.run(_run())
