from __future__ import annotations

import asyncio
from typing import Any

from custom_components.ha_doorbell_jeeves.notifications import (
    ACTION_COMING,
    ACTION_NOT_AVAILABLE,
    NotificationManager,
)


def test_send_doorbell_notification_generates_unique_actions_per_target(
    hass: Any,
) -> None:
    async def _run() -> None:
        manager = NotificationManager(hass)
        session = await manager.send_doorbell_notification(
            targets=[
                {"service": "notify.mobile_app_owner_a", "name": "Owner A"},
                {"service": "notify.mobile_app_owner_b", "name": "Owner B"},
            ],
            session_id="session-123",
        )

        assert len(session.responses) == 2

        call_a = hass.services.calls[0]
        call_b = hass.services.calls[1]
        assert (call_a[0], call_a[1]) == ("notify", "mobile_app_owner_a")
        assert (call_b[0], call_b[1]) == ("notify", "mobile_app_owner_b")

        actions_a = call_a[2]["data"]["actions"]
        actions_b = call_b[2]["data"]["actions"]

        assert actions_a[0]["action"] == "JEEVES_COMING__session-123__0"
        assert actions_b[0]["action"] == "JEEVES_COMING__session-123__1"
        assert actions_a[1]["action"] == "JEEVES_NOT_AVAILABLE__session-123__0"
        assert actions_b[1]["action"] == "JEEVES_NOT_AVAILABLE__session-123__1"

    asyncio.run(_run())


def test_process_response_applies_to_matching_target_only(hass: Any) -> None:
    async def _run() -> None:
        manager = NotificationManager(hass)
        callback_count = 0

        async def _on_coming() -> None:
            nonlocal callback_count
            callback_count += 1

        session = await manager.send_doorbell_notification(
            targets=[
                {"service": "notify.mobile_app_owner_a", "name": "Owner A"},
                {"service": "notify.mobile_app_owner_b", "name": "Owner B"},
            ],
            session_id="session-abc",
            on_coming=_on_coming,
        )

        await manager._process_response(session.responses[1].coming_action)

        assert session.responses[0].response == ""
        assert session.responses[1].response == "coming"
        assert callback_count == 1

        # Duplicate response should be ignored.
        await manager._process_response(session.responses[1].coming_action)
        assert callback_count == 1

        await manager._process_response(session.responses[0].not_available_action)
        assert session.responses[0].response == "not_available"

    asyncio.run(_run())


def test_process_response_supports_legacy_static_action_ids(hass: Any) -> None:
    async def _run() -> None:
        manager = NotificationManager(hass)
        session = await manager.send_doorbell_notification(
            targets=[{"service": "notify.mobile_app_owner", "name": "Owner"}],
            session_id="legacy-session",
        )

        await manager._process_response(ACTION_NOT_AVAILABLE)
        assert session.responses[0].response == "not_available"

        # Existing legacy behavior still recognizes static "coming" action.
        await manager._process_response(ACTION_COMING)
        assert session.responses[0].response == "not_available"

    asyncio.run(_run())
