from __future__ import annotations

import asyncio
from types import SimpleNamespace

from google import genai

from custom_components.ha_doorbell_jeeves.const import (
    CONF_API_KEY,
    CONF_TOOL_API_KEY,
    CONF_VALIDATOR_MODEL,
)
from custom_components.ha_doorbell_jeeves.security import SecurityManager


def test_validator_prefers_tool_api_key_and_rejects_invalid_image(
    hass: object, monkeypatch: object
) -> None:
    captured: dict[str, str] = {}

    class _DummyClient:
        def __init__(self, api_key: str) -> None:
            captured["api_key"] = api_key
            self.models = SimpleNamespace(generate_content=lambda **_kwargs: None)

    monkeypatch.setattr(genai, "Client", lambda api_key: _DummyClient(api_key))

    manager = SecurityManager(
        hass,
        {
            CONF_API_KEY: "voice-key",
            CONF_TOOL_API_KEY: "tool-key",
            CONF_VALIDATOR_MODEL: "gemini-test-model",
        },
        store=SimpleNamespace(get_action=lambda _action_id: (None, None), managed_entities=[]),
    )

    decision = asyncio.run(
        manager._run_validator(
            action_id="unlock_gate",
            action_name="Unlock Gate",
            arguments={"confirm": True},
            conversation_summary="Visitor asks to unlock.",
            claimed_identity="Alex",
            camera_frame_b64="not-valid-base64%%%$$$",
            reference_image_b64=None,
            custom_prompt="",
            require_visual_match=False,
        )
    )

    assert captured["api_key"] == "tool-key"
    assert decision.approved is False
    assert "invalid" in decision.reasoning.lower()
    assert "invalid_image_input" in decision.threat_indicators


def test_get_camera_frame_returns_none_when_no_camera_configured(hass: object) -> None:
    manager = SecurityManager(
        hass,
        config={},
        store=SimpleNamespace(get_action=lambda _action_id: (None, None), managed_entities=[]),
    )
    assert asyncio.run(manager._get_camera_frame()) is None

