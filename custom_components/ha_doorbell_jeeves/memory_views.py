"""HTTP views for Doorbell Jeeves memory assets."""

from __future__ import annotations

import base64
import binascii

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .memory import SessionMemory
from .session_manager import JeevesSessionManager


def memory_image_url(entry_id: str, memory_id: str) -> str:
    """Build the authenticated URL for a stored memory snapshot."""
    return f"/api/{DOMAIN}/memory_image/{entry_id}/{memory_id}"


class JeevesMemoryImageView(HomeAssistantView):
    """Serve stored memory snapshot images."""

    url = f"/api/{DOMAIN}/memory_image/{{entry_id}}/{{memory_id}}"
    name = f"api:{DOMAIN}:memory_image"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(self, request: web.Request, entry_id: str, memory_id: str) -> web.StreamResponse:
        """Return a JPEG snapshot for the requested stored memory."""
        manager = self._resolve_manager(entry_id)
        if manager is None:
            raise web.HTTPNotFound(text="Doorbell Jeeves entry not found")

        memory = self._resolve_memory(manager, memory_id)
        if memory is None or not memory.photo_base64:
            raise web.HTTPNotFound(text="Memory image not found")

        try:
            image_bytes = base64.b64decode(memory.photo_base64, validate=True)
        except (binascii.Error, ValueError):
            raise web.HTTPNotFound(text="Invalid memory image")

        return web.Response(
            body=image_bytes,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    def _resolve_manager(self, entry_id: str) -> JeevesSessionManager | None:
        managers = self._hass.data.get(DOMAIN, {})
        manager = managers.get(entry_id)
        if isinstance(manager, JeevesSessionManager):
            return manager
        return None

    @staticmethod
    def _resolve_memory(manager: JeevesSessionManager, memory_id: str) -> SessionMemory | None:
        for memory in manager.get_memories():
            if memory.id == memory_id:
                return memory
        return None


def register_memory_views(hass: HomeAssistant) -> None:
    """Register integration HTTP views once per Home Assistant instance."""
    hass.http.register_view(JeevesMemoryImageView(hass))
