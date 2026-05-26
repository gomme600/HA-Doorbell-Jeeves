"""Abstract base for real-time AI streaming clients."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any


class BaseRealtimeClient(ABC):
    """Protocol for real-time audio/vision streaming clients."""

    @property
    @abstractmethod
    def connected(self) -> bool:
        """Return True if the session is active."""

    @property
    @abstractmethod
    def conversation_summary(self) -> str:
        """Return a summary of the conversation so far."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish the real-time session."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close the session."""

    @abstractmethod
    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send raw PCM audio into the session."""

    @abstractmethod
    async def send_image(self, image_base64: str, mime_type: str = "image/jpeg") -> None:
        """Inject an image frame into the session."""

    @abstractmethod
    async def inject_context(self, text: str) -> None:
        """Inject a text context message into the session (for tool results in dual-model mode)."""
