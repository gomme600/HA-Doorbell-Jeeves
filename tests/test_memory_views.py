from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web

from custom_components.ha_doorbell_jeeves.const import DOMAIN
from custom_components.ha_doorbell_jeeves.memory_views import JeevesEventImageView
from custom_components.ha_doorbell_jeeves.session_manager import JeevesSessionManager


def test_event_image_view_rejects_non_numeric_photo_index(hass: object) -> None:
    async def _run() -> None:
        manager = JeevesSessionManager.__new__(JeevesSessionManager)
        manager._event_store = SimpleNamespace(
            events=[SimpleNamespace(id="evt_1", photos=["aGVsbG8="])]
        )
        hass.data = {DOMAIN: {"entry-1": manager}}

        view = JeevesEventImageView(hass)
        with pytest.raises(web.HTTPNotFound) as err:
            await view.get(SimpleNamespace(), "entry-1", "evt_1", "not-a-number")
        assert "Invalid photo index" in err.value.text

    asyncio.run(_run())


def test_event_image_view_returns_decoded_image(hass: object) -> None:
    async def _run() -> None:
        manager = JeevesSessionManager.__new__(JeevesSessionManager)
        manager._event_store = SimpleNamespace(
            events=[SimpleNamespace(id="evt_2", photos=["aGVsbG8="])]
        )
        hass.data = {DOMAIN: {"entry-2": manager}}

        view = JeevesEventImageView(hass)
        response = await view.get(SimpleNamespace(), "entry-2", "evt_2", "0")

        assert response.content_type == "image/jpeg"
        assert response.body == b"hello"

    asyncio.run(_run())

