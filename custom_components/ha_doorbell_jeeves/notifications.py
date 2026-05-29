"""Actionable doorbell notifications with Coming/Not Available responses."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

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
    _on_not_available: Callable[[], Awaitable[None]] | None = None

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
        on_not_available: Callable[[], Awaitable[None]] | None = None,
    ) -> NotificationSession:
        """Set up tracking session for doorbell notification responses.

        Creates a NotificationSession to track 'Coming'/'Not Available' button presses.
        The actual notification is sent separately by the tool execution layer.

        Args:
            targets: List of {"service": "notify.xxx", "name": "Owner Name"}
            session_id: Current session ID for tracking
            snapshot_b64: Unused (kept for API compat)
            on_coming: Callback when someone presses "Coming"
            on_not_available: Callback when all owners press "Not Available"
        """
        self._active_session = NotificationSession(
            session_id=session_id,
            sent_at=time.time(),
            responses=[
                OwnerResponse(owner_name=t["name"], service=t["service"], response="")
                for t in targets
            ],
            _on_coming=on_coming,
            _on_not_available=on_not_available,
        )
        _LOGGER.info(
            "Notification tracking session created for %d target(s)",
            len(targets),
        )
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

        if response_type == "not_available" and self._active_session.all_unavailable:
            if self._active_session._on_not_available:
                try:
                    await self._active_session._on_not_available()
                except Exception:
                    _LOGGER.exception("Failed to notify agent of 'not available' response")

    def get_availability_status(self) -> str:
        """Get current availability status for the agent tool."""
        if not self._active_session:
            return "No doorbell notification has been sent yet this session."
        return self._active_session.status_summary

    def clear_session(self) -> None:
        """Clear the active notification session."""
        self._active_session = None
