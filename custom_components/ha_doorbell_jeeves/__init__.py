"""Doorbell Jeeves – AI-powered multimodal doorbell concierge for Home Assistant.

This integration connects a 2-way audio camera to Google Gemini or OpenAI's
real-time audio APIs, enabling an autonomous AI agent that can greet visitors,
identify known people, and execute configured home automation actions with
configurable security policies.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    AUDIO_MODE_REOLINK,
    AUDIO_OUTPUT_GO2RTC,
    CONF_API_KEY,
    CONF_AUDIO_MODE,
    CONF_AUDIO_OUTPUT_MODE,
    CONF_CAMERA_ENTITY,
    CONF_GO2RTC_INPUT_STREAM_NAME,
    CONF_GO2RTC_OUTPUT_STREAM_NAME,
    CONF_GO2RTC_STREAM_NAME,
    CONF_MODEL,
    CONF_REOLINK_ENTRY_ID,
    DEFAULT_MODEL_GEMINI,
    DOMAIN,
    GLOBAL_ACTIONS_ENTITY_ID,
    GLOBAL_ACTIONS_ENTITY_NAME,
    SERVICE_ADD_ACTION,
    SERVICE_ADD_ENTITY,
    SERVICE_ADD_IDENTITY,
    SERVICE_REMOVE_ACTION,
    SERVICE_REMOVE_ENTITY,
    SERVICE_REMOVE_IDENTITY,
    SERVICE_SEND_AUDIO,
    SERVICE_START_SESSION,
    SERVICE_STOP_SESSION,
)
from .memory_views import register_memory_views
from .models import EntityAction, KnownIdentity, ManagedEntity
from .session_manager import JeevesSessionManager

_LOGGER = logging.getLogger(__name__)
PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.CAMERA]
_DOMAIN_INTERNAL = f"{DOMAIN}_internal"
_INTERNAL_MEMORY_VIEWS = "memory_views_registered"
_INTERNAL_FRONTEND_RESOURCES = "frontend_resources_registered"
_MEMORY_TIMELINE_CARD_URL = "/ha_doorbell_jeeves/jeeves-memory-timeline-card.js"
_MEMORY_TIMELINE_CARD_FILE = "jeeves-memory-timeline-card.js"

# Type alias for runtime data stored on the config entry
JeevesData = JeevesSessionManager


def _normalize_action_id(action_id: str) -> str:
    """Normalize action IDs to a safe tool/function name."""
    slug = action_id.lower().replace(" ", "_").replace("-", "_")
    slug = re.sub(r"[^a-z0-9_]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        slug = "action"
    if slug[0].isdigit():
        slug = f"action_{slug}"
    return slug


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Doorbell Jeeves from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    internal_data = hass.data.setdefault(_DOMAIN_INTERNAL, {})
    if not internal_data.get(_INTERNAL_MEMORY_VIEWS):
        register_memory_views(hass)
        internal_data[_INTERNAL_MEMORY_VIEWS] = True
    if not internal_data.get(_INTERNAL_FRONTEND_RESOURCES):
        await _register_frontend_resources(hass)
        internal_data[_INTERNAL_FRONTEND_RESOURCES] = True

    # ─── Data migration: fix invalid model names from older configs ───
    _INVALID_MODELS = {"gemini-2.5-flash-native-audio-dialog"}
    stored_model = entry.data.get(CONF_MODEL) or entry.options.get(CONF_MODEL)
    if stored_model in _INVALID_MODELS:
        new_data = dict(entry.data)
        new_data[CONF_MODEL] = DEFAULT_MODEL_GEMINI
        hass.config_entries.async_update_entry(entry, data=new_data)
        _LOGGER.info(
            "Migrated model %s → %s for entry %s",
            stored_model, DEFAULT_MODEL_GEMINI, entry.entry_id[:8],
        )

    # Create session manager
    manager = JeevesSessionManager(hass, entry)
    await manager.async_initialize()
    hass.data[DOMAIN][entry.entry_id] = manager

    # Reolink auto-configuration (deferred to first session start for timing)
    # go2rtc may not be ready at entry load time, so we flag it for lazy init
    config = dict(entry.data) | dict(entry.options)
    if config.get(CONF_AUDIO_MODE) == AUDIO_MODE_REOLINK:
        manager.reolink_needs_setup = True
    else:
        manager.reolink_needs_setup = False

    # Register services (only once globally)
    if not hass.services.has_service(DOMAIN, SERVICE_START_SESSION):
        _register_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Listen for options updates
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    _LOGGER.info(
        "Doorbell Jeeves loaded: entry=%s, camera=%s",
        entry.entry_id[:8],
        config.get(CONF_CAMERA_ENTITY, "none"),
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Doorbell Jeeves config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    manager: JeevesSessionManager = hass.data[DOMAIN].pop(entry.entry_id, None)
    if manager:
        await manager.async_stop_session()
        manager.unregister_start_triggers()
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _register_frontend_resources(hass: HomeAssistant) -> None:
    """Register static frontend resources used by optional Lovelace cards."""
    if not getattr(hass, "http", None):
        _LOGGER.warning("HTTP component is not available; skipping frontend resource registration")
        return
    card_path = Path(__file__).parent / "frontend" / _MEMORY_TIMELINE_CARD_FILE
    if not card_path.exists():
        _LOGGER.warning("Memory timeline card file missing: %s", card_path)
        return

    # Serve the card JS via a custom view with the correct MIME type
    from aiohttp import web  # noqa: PLC0415

    from homeassistant.components.http import HomeAssistantView  # noqa: PLC0415

    class _CardJSView(HomeAssistantView):
        """Serve the memory timeline card JavaScript with correct MIME."""

        url = _MEMORY_TIMELINE_CARD_URL
        name = f"api:{DOMAIN}:card_js"
        requires_auth = False  # Frontend resources must load without auth

        async def get(self, request: web.Request) -> web.FileResponse:
            return web.FileResponse(
                card_path,
                headers={"Content-Type": "application/javascript; charset=utf-8"},
            )

    hass.http.register_view(_CardJSView())

    # Tell the HA frontend to load the card JS on every page
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415

    add_extra_js_url(hass, _MEMORY_TIMELINE_CARD_URL)
    _LOGGER.debug("Registered memory timeline card resource: %s", _MEMORY_TIMELINE_CARD_URL)


async def _setup_reolink(hass: HomeAssistant, entry: ConfigEntry, config: dict[str, Any]) -> None:
    """Configure go2rtc for Reolink doorbell 2-way audio."""
    from .reolink_audio import auto_configure_reolink  # noqa: PLC0415

    reolink_entry_id = config.get(CONF_REOLINK_ENTRY_ID, "")
    camera_entity = config.get(CONF_CAMERA_ENTITY, "")
    if not reolink_entry_id and not camera_entity:
        return

    result = await auto_configure_reolink(
        hass,
        camera_entity_id=camera_entity,
        reolink_entry_id=reolink_entry_id or None,
    )
    if result:
        _LOGGER.info(
            "Reolink go2rtc configured: stream=%s, host=%s",
            result.get("stream_name"),
            result.get("host"),
        )
        # Store the stream name in options for session use
        new_options = dict(entry.options)
        new_options[CONF_GO2RTC_STREAM_NAME] = result["stream_name"]
        new_options[CONF_GO2RTC_INPUT_STREAM_NAME] = result["stream_name"]
        new_options[CONF_GO2RTC_OUTPUT_STREAM_NAME] = result["stream_name"]
        hass.config_entries.async_update_entry(entry, options=new_options)
    else:
        _LOGGER.warning(
            "Could not auto-configure Reolink go2rtc. 2-way audio may not work. "
            "Ensure the Reolink integration is loaded and the camera has valid credentials."
        )


# ─── Service Registration ─────────────────────────────────────────────────────


def _register_services(hass: HomeAssistant) -> None:
    """Register all Doorbell Jeeves services."""

    async def _get_manager(call: ServiceCall) -> JeevesSessionManager | None:
        """Resolve the session manager from entry_id in service data."""
        entry_id = call.data.get("entry_id", "")
        # If only one entry, use it
        managers = hass.data.get(DOMAIN, {})
        if not entry_id and len(managers) == 1:
            return next(iter(managers.values()))
        return managers.get(entry_id)

    # ─── Session Services ─────────────────────────────────────────────────────

    async def handle_start_session(call: ServiceCall) -> None:
        """Start the AI concierge session."""
        manager = await _get_manager(call)
        if manager:
            await manager.async_start_session()
        else:
            _LOGGER.error("No Doorbell Jeeves instance found for entry_id: %s", call.data.get("entry_id"))

    async def handle_stop_session(call: ServiceCall) -> None:
        """Stop the active AI concierge session."""
        manager = await _get_manager(call)
        if manager:
            await manager.async_stop_session()

    async def handle_send_audio(call: ServiceCall) -> None:
        """Send audio data to the AI session."""
        manager = await _get_manager(call)
        if manager:
            audio_b64 = call.data.get("audio_base64", "")
            await manager.async_send_audio(audio_b64)

    hass.services.async_register(
        DOMAIN, SERVICE_START_SESSION, handle_start_session,
        schema=vol.Schema({vol.Optional("entry_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STOP_SESSION, handle_stop_session,
        schema=vol.Schema({vol.Optional("entry_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_AUDIO, handle_send_audio,
        schema=vol.Schema({
            vol.Optional("entry_id"): cv.string,
            vol.Required("audio_base64"): cv.string,
        }),
    )

    # ─── Entity Management Services ──────────────────────────────────────────

    async def handle_add_entity(call: ServiceCall) -> None:
        """Add or update a managed entity."""
        manager = await _get_manager(call)
        if not manager:
            return
        entity = ManagedEntity(
            entity_id=call.data["entity_id"],
            name=call.data["name"],
            description=call.data.get("description", ""),
            security_mode=call.data.get("security_mode", "auto"),
            require_visual_match=call.data.get("require_visual_match", False),
            require_camera_feed=call.data.get("require_camera_feed", False),
        )
        await manager.store.async_add_entity(entity)

    async def handle_remove_entity(call: ServiceCall) -> None:
        """Remove a managed entity."""
        manager = await _get_manager(call)
        if manager:
            await manager.store.async_remove_entity(call.data["entity_id"])

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_ENTITY, handle_add_entity,
        schema=vol.Schema({
            vol.Optional("entry_id"): cv.string,
            vol.Required("entity_id"): cv.entity_id,
            vol.Required("name"): cv.string,
            vol.Optional("description", default=""): cv.string,
            vol.Optional("security_mode", default="auto"): cv.string,
            vol.Optional("require_visual_match", default=False): cv.boolean,
            vol.Optional("require_camera_feed", default=False): cv.boolean,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_ENTITY, handle_remove_entity,
        schema=vol.Schema({
            vol.Optional("entry_id"): cv.string,
            vol.Required("entity_id"): cv.entity_id,
        }),
    )

    # ─── Action Management Services ──────────────────────────────────────────

    async def handle_add_action(call: ServiceCall) -> None:
        """Add an action to a managed entity."""
        manager = await _get_manager(call)
        if not manager:
            return
        entity_id = call.data["entity_id"]
        entity = manager.store.get_entity(entity_id)
        if not entity and entity_id == GLOBAL_ACTIONS_ENTITY_ID:
            entity = ManagedEntity(
                entity_id=GLOBAL_ACTIONS_ENTITY_ID,
                name=GLOBAL_ACTIONS_ENTITY_NAME,
                description="Automation triggers available to the AI.",
            )
        if not entity:
            _LOGGER.error("Entity %s not managed — add it first", entity_id)
            return
        steps = list(call.data.get("steps", []))
        service_data = dict(call.data.get("service_data", {}))
        service = call.data.get("service") or ""
        if not service and steps:
            service = steps[0].get("action") or steps[0].get("service") or ""
        if not service and service_data:
            service = service_data.get("action") or service_data.get("service") or ""
        if not service:
            _LOGGER.error("Action '%s' has no service/step definition", call.data.get("action_id"))
            return
        normalized_action_id = _normalize_action_id(call.data["action_id"])
        if normalized_action_id != call.data["action_id"]:
            _LOGGER.info(
                "Normalized action id '%s' -> '%s'",
                call.data["action_id"],
                normalized_action_id,
            )
        entity.actions = [action for action in entity.actions if action.id != normalized_action_id]
        action = EntityAction(
            id=normalized_action_id,
            name=call.data["action_name"],
            description=call.data.get("description", ""),
            service=service,
            service_data=service_data,
            steps=steps,
            security_mode=call.data.get("security_mode", "auto"),
            require_visual_match=call.data.get("require_visual_match", False),
            require_camera_feed=call.data.get("require_camera_feed", False),
            max_per_session=call.data.get("max_per_session", 0),
            cooldown_seconds=call.data.get("cooldown_seconds", 0.0),
            validator_prompt=call.data.get("validator_prompt", ""),
        )
        entity.actions.append(action)
        await manager.store.async_add_entity(entity)

    async def handle_remove_action(call: ServiceCall) -> None:
        """Remove an action by ID."""
        manager = await _get_manager(call)
        if not manager:
            return
        action_id = call.data["action_id"]
        normalized_action_id = _normalize_action_id(action_id)
        for entity in manager.store.managed_entities:
            entity.actions = [
                action
                for action in entity.actions
                if action.id not in {action_id, normalized_action_id}
            ]
        await manager.store.async_save_entities()

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_ACTION, handle_add_action,
        schema=vol.Schema({
            vol.Optional("entry_id"): cv.string,
            vol.Required("entity_id"): cv.string,
            vol.Required("action_id"): cv.string,
            vol.Required("action_name"): cv.string,
            vol.Optional("description", default=""): cv.string,
            vol.Optional("service", default=""): cv.string,
            vol.Optional("service_data", default={}): dict,
            vol.Optional("steps", default=[]): list,
            vol.Optional("security_mode", default="auto"): cv.string,
            vol.Optional("require_visual_match", default=False): cv.boolean,
            vol.Optional("require_camera_feed", default=False): cv.boolean,
            vol.Optional("max_per_session", default=0): int,
            vol.Optional("cooldown_seconds", default=0.0): vol.Coerce(float),
            vol.Optional("validator_prompt", default=""): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_ACTION, handle_remove_action,
        schema=vol.Schema({
            vol.Optional("entry_id"): cv.string,
            vol.Required("action_id"): cv.string,
        }),
    )

    # ─── Identity Management Services ─────────────────────────────────────────

    async def handle_add_identity(call: ServiceCall) -> None:
        """Add a known identity (person/animal/object)."""
        manager = await _get_manager(call)
        if not manager:
            return
        identity = KnownIdentity(
            name=call.data["name"],
            identity_type=call.data.get("identity_type", "person"),
            relationship=call.data.get("relationship", "guest"),
            description=call.data.get("description", ""),
            access_level=call.data.get("access_level", "guest"),
            image_base64=call.data.get("image_base64"),
            notes=call.data.get("notes", ""),
        )
        await manager.store.async_add_identity(identity)

    async def handle_remove_identity(call: ServiceCall) -> None:
        """Remove a known identity by name."""
        manager = await _get_manager(call)
        if manager:
            await manager.store.async_remove_identity(call.data["name"])

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_IDENTITY, handle_add_identity,
        schema=vol.Schema({
            vol.Optional("entry_id"): cv.string,
            vol.Required("name"): cv.string,
            vol.Optional("identity_type", default="person"): cv.string,
            vol.Optional("relationship", default="guest"): cv.string,
            vol.Optional("description", default=""): cv.string,
            vol.Optional("access_level", default="guest"): cv.string,
            vol.Optional("image_base64"): cv.string,
            vol.Optional("notes", default=""): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_IDENTITY, handle_remove_identity,
        schema=vol.Schema({
            vol.Optional("entry_id"): cv.string,
            vol.Required("name"): cv.string,
        }),
    )
