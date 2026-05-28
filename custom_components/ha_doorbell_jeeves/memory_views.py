"""HTTP views for Doorbell Jeeves memory assets."""

from __future__ import annotations

import asyncio
import base64
import binascii
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .memory import SessionMemory
from .session_manager import JeevesSessionManager

_CARD_JS_PATH = Path(__file__).parent / "frontend" / "jeeves-memory-timeline-card.js"
_EVENTS_CARD_JS_PATH = Path(__file__).parent / "frontend" / "jeeves-events-timeline-card.js"
_CAMERA_MAP_JS_PATH = Path(__file__).parent / "frontend" / "jeeves-camera-map-panel.js"

# All frontend JS files bundled together via the single card_js endpoint
_ALL_CARD_JS_PATHS = [_CARD_JS_PATH, _EVENTS_CARD_JS_PATH, _CAMERA_MAP_JS_PATH]


def memory_image_url(entry_id: str, memory_id: str) -> str:
    """Build the authenticated URL for a stored memory snapshot."""
    return f"/api/{DOMAIN}/memory_image/{entry_id}/{memory_id}"


def card_js_url() -> str:
    """URL where the timeline card JavaScript is served."""
    return f"/api/{DOMAIN}/card_js"


class JeevesCardJSView(HomeAssistantView):
    """Serve ALL Jeeves card JS bundled together with correct MIME type."""

    url = f"/api/{DOMAIN}/card_js"
    name = f"api:{DOMAIN}:card_js"
    requires_auth = False  # Frontend resources must load without auth

    def __init__(self) -> None:
        self._content: bytes | None = None

    async def get(self, request: web.Request) -> web.Response:
        """Return all card JavaScript bundled together."""
        if self._content is None:
            loop = asyncio.get_running_loop()
            parts: list[bytes] = []
            for i, path in enumerate(_ALL_CARD_JS_PATHS):
                try:
                    data = await loop.run_in_executor(None, path.read_bytes)
                    # Wrap each card in an IIFE with error handling so one
                    # card's error doesn't prevent others from loading
                    wrapped = (
                        f'/* --- Jeeves card: {path.name} --- */\n'
                        f'(function() {{\n'
                        f'  console.info("[Jeeves] Loading {path.name}...");\n'
                        f'  try {{\n'
                    ).encode() + data + (
                        f'\n    console.info("[Jeeves] {path.name} loaded OK");\n'
                        f'  }} catch(_e) {{ console.error("[Jeeves] Error in {path.name}:", _e); }}\n'
                        f'}})();\n'
                    ).encode()
                    parts.append(wrapped)
                except OSError:
                    pass
            self._content = b"\n".join(parts)
        return web.Response(
            body=self._content,
            content_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )


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
    hass.http.register_view(JeevesEventImageView(hass))
    hass.http.register_view(JeevesCardJSView())
    hass.http.register_view(JeevesEventsCardJSView())
    hass.http.register_view(JeevesCameraMapJSView())


class JeevesEventsCardJSView(HomeAssistantView):
    """Serve the events timeline card JS."""

    url = f"/api/{DOMAIN}/events_card_js"
    name = f"api:{DOMAIN}:events_card_js"
    requires_auth = False

    def __init__(self) -> None:
        self._content: bytes | None = None

    async def get(self, request: web.Request) -> web.Response:
        if self._content is None:
            loop = asyncio.get_running_loop()
            self._content = await loop.run_in_executor(None, _EVENTS_CARD_JS_PATH.read_bytes)
        return web.Response(
            body=self._content,
            content_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )


class JeevesEventImageView(HomeAssistantView):
    """Serve stored event photos."""

    url = f"/api/{DOMAIN}/event_image/{{entry_id}}/{{event_id}}/{{photo_index}}"
    name = f"api:{DOMAIN}:event_image"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    async def get(
        self, request: web.Request, entry_id: str, event_id: str, photo_index: str
    ) -> web.StreamResponse:
        """Return a JPEG photo for the requested event."""
        managers = self._hass.data.get(DOMAIN, {})
        manager = managers.get(entry_id)
        if not isinstance(manager, JeevesSessionManager):
            raise web.HTTPNotFound(text="Entry not found")

        event_store = manager.event_store
        idx = int(photo_index)

        for evt in event_store.events:
            if evt.id == event_id:
                if idx < 0 or idx >= len(evt.photos):
                    raise web.HTTPNotFound(text="Photo index out of range")
                try:
                    image_bytes = base64.b64decode(evt.photos[idx], validate=True)
                except (binascii.Error, ValueError):
                    raise web.HTTPNotFound(text="Invalid event image")
                return web.Response(
                    body=image_bytes,
                    content_type="image/jpeg",
                    headers={"Cache-Control": "no-store"},
                )

        raise web.HTTPNotFound(text="Event not found")


class JeevesCameraMapJSView(HomeAssistantView):
    """Serve the interactive camera map panel JS."""

    url = f"/api/{DOMAIN}/camera_map_js"
    name = f"api:{DOMAIN}:camera_map_js"
    requires_auth = False

    def __init__(self) -> None:
        self._content: bytes | None = None

    async def get(self, request: web.Request) -> web.Response:
        if self._content is None:
            loop = asyncio.get_running_loop()
            self._content = await loop.run_in_executor(None, _CAMERA_MAP_JS_PATH.read_bytes)
        return web.Response(
            body=self._content,
            content_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )
