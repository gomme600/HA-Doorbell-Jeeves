"""Actionable doorbell notifications with Coming/Not Available responses."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ACTION_COMING = "JEEVES_COMING"
ACTION_NOT_AVAILABLE = "JEEVES_NOT_AVAILABLE"

MOBILE_APP_NOTIFICATION_ACTION = "mobile_app_notification_action"


@dataclass
class OwnerResponse:
    """A response from an owner to the doorbell notification."""

    owner_name: str
    service: str
    response: str  # "coming", "not_available", or "" (no response yet)
    response_time: float = 0.0


@dataclass
class NotificationSession:
    """Tracks responses for a single doorbell ring notification."""

    session_id: str
    sent_at: float
    responses: list[OwnerResponse] = field(default_factory=list)
    _on_coming: Callable[[], Awaitable[None]] | None = None

    @property
    def someone_coming(self) -> bool:
        return any(r.response == "coming" for r in self.responses)

    @property
    def all_unavailable(self) -> bool:
        return all(r.response == "not_available" for r in self.responses) and len(self.responses) > 0

    @property
    def status_summary(self) -> str:
        """Human-readable availability status for the agent."""
        coming = [r.owner_name for r in self.responses if r.response == "coming"]
        unavailable = [r.owner_name for r in self.responses if r.response == "not_available"]
        pending = [r.owner_name for r in self.responses if r.response == ""]

        parts = []
        if coming:
            parts.append(f"Coming to the door: {', '.join(coming)}")
        if unavailable:
            parts.append(f"Not available: {', '.join(unavailable)}")
        if pending:
            parts.append(f"Haven't responded yet: {', '.join(pending)}")
        if not parts:
            return "No notification targets configured."
        return " | ".join(parts)


class NotificationManager:
    """Manages actionable doorbell notifications and tracks responses."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._active_session: NotificationSession | None = None
        self._unsub: Callable[[], None] | None = None

    async def async_setup(self) -> None:
        """Subscribe to mobile app notification action events."""

        @callback
        def _handle_action(event: Event) -> None:
            action = event.data.get("action", "")
            if action in (ACTION_COMING, ACTION_NOT_AVAILABLE):
                asyncio.create_task(self._process_response(action))

        self._unsub = self._hass.bus.async_listen(
            MOBILE_APP_NOTIFICATION_ACTION, _handle_action
        )

    async def async_teardown(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def send_doorbell_notification(
        self,
        targets: list[dict[str, str]],
        session_id: str,
        snapshot_b64: str = "",
        on_coming: Callable[[], Awaitable[None]] | None = None,
    ) -> NotificationSession:
        """Send actionable notification to all configured targets.

        Args:
            targets: List of {"service": "notify.xxx", "name": "Owner Name"}
            session_id: Current session ID for tracking
            snapshot_b64: Optional camera snapshot to include
            on_coming: Callback when someone presses "Coming"
        """
        self._active_session = NotificationSession(
            session_id=session_id,
            sent_at=time.time(),
            responses=[
                OwnerResponse(owner_name=t["name"], service=t["service"], response="")
                for t in targets
            ],
            _on_coming=on_coming,
        )

        for target in targets:
            service_parts = target["service"].split(".", 1)
            if len(service_parts) != 2:
                continue
            domain, service_name = service_parts

            data: dict[str, Any] = {
                "message": "Someone is at the door! Jeeves is handling it.",
                "title": "🔔 Doorbell",
                "data": {
                    "actions": [
                        {"action": ACTION_COMING, "title": "🚶 Coming"},
                        {"action": ACTION_NOT_AVAILABLE, "title": "❌ Not Available"},
                    ],
                    "push": {"interruption-level": "time-sensitive"},
                    "tag": f"jeeves_doorbell_{session_id}",
                },
            }

            if snapshot_b64:
                data["data"]["image"] = f"/api/camera_proxy/{DOMAIN}"

            try:
                await self._hass.services.async_call(
                    domain, service_name, data, blocking=False
                )
                _LOGGER.info(
                    "Sent doorbell notification to %s (%s)",
                    target["name"],
                    target["service"],
                )
            except Exception:
                _LOGGER.exception("Failed to send notification to %s", target["service"])

        return self._active_session

    async def _process_response(self, action: str) -> None:
        """Process a response from a mobile app notification."""
        if not self._active_session:
            return

        response_type = "coming" if action == ACTION_COMING else "not_available"

        for resp in self._active_session.responses:
            if resp.response == "":
                resp.response = response_type
                resp.response_time = time.time()
                _LOGGER.info(
                    "Owner '%s' responded: %s", resp.owner_name, response_type
                )
                break

        if response_type == "coming" and self._active_session._on_coming:
            try:
                await self._active_session._on_coming()
            except Exception:
                _LOGGER.exception("Failed to notify agent of 'coming' response")

    def get_availability_status(self) -> str:
        """Get current availability status for the agent tool."""
        if not self._active_session:
            return "No doorbell notification has been sent yet this session."
        return self._active_session.status_summary

    def clear_session(self) -> None:
        """Clear the active notification session."""
        self._active_session = None
