"""Doorbell Jeeves – Production-ready multimodal AI concierge."""

from __future__ import annotations

import logging
from typing import Any, TypeAlias

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    SERVICE_SEND_AUDIO,
    SERVICE_START_SESSION,
    SERVICE_STOP_SESSION,
)
from .session_manager import JeevesSessionManager

_LOGGER = logging.getLogger(__name__)

JeevesConfigEntry: TypeAlias = ConfigEntry


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up integration (UI-only, no YAML)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: JeevesConfigEntry) -> bool:
    """Set up Doorbell Jeeves from a config entry."""
    manager = JeevesSessionManager(hass, entry)
    await manager.async_initialize()
    hass.data[DOMAIN][entry.entry_id] = manager

    _register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info("Doorbell Jeeves ready (entry=%s)", entry.entry_id)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: JeevesConfigEntry) -> bool:
    """Unload and stop any active session."""
    manager: JeevesSessionManager = hass.data[DOMAIN].pop(entry.entry_id)
    await manager.async_stop_session()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    """Register domain services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_START_SESSION):
        return

    async def handle_start(call: ServiceCall) -> None:
        entry_id = call.data["entry_id"]
        manager: JeevesSessionManager | None = hass.data[DOMAIN].get(entry_id)
        if not manager:
            _LOGGER.error("No instance for entry_id=%s", entry_id)
            return
        await manager.async_start_session()

    async def handle_stop(call: ServiceCall) -> None:
        entry_id = call.data["entry_id"]
        manager: JeevesSessionManager | None = hass.data[DOMAIN].get(entry_id)
        if not manager:
            return
        await manager.async_stop_session()

    async def handle_audio(call: ServiceCall) -> None:
        entry_id = call.data["entry_id"]
        manager: JeevesSessionManager | None = hass.data[DOMAIN].get(entry_id)
        if not manager:
            return
        await manager.async_send_audio(call.data["audio_base64"])

    schema_start = vol.Schema({vol.Required("entry_id"): cv.string})
    schema_stop = vol.Schema({vol.Required("entry_id"): cv.string})
    schema_audio = vol.Schema({vol.Required("entry_id"): cv.string, vol.Required("audio_base64"): cv.string})

    hass.services.async_register(DOMAIN, SERVICE_START_SESSION, handle_start, schema=schema_start)
    hass.services.async_register(DOMAIN, SERVICE_STOP_SESSION, handle_stop, schema=schema_stop)
    hass.services.async_register(DOMAIN, SERVICE_SEND_AUDIO, handle_audio, schema=schema_audio)
