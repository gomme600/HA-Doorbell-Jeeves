"""Doorbell Jeeves – AI-powered multimodal doorbell concierge for Home Assistant.

This integration connects a 2-way audio camera to Google Gemini or OpenAI's
real-time audio APIs, enabling an autonomous AI agent that can greet visitors,
identify known people, and execute configured home automation actions with
configurable security policies.
"""

from __future__ import annotations

import asyncio
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

    # ─── Data migration: update tool model defaults for better rate limits ───
    # Migrate stored tool_model from gemini-2.5-flash (20 RPD) to gemini-3.1-flash-lite (500 RPD)
    _TOOL_MODEL_MIGRATIONS = {"gemini-2.5-flash": "gemini-3.1-flash-lite"}
    for store in ("data", "options"):
        store_dict = getattr(entry, store, {}) or {}
        stored_tool_model = store_dict.get("tool_model", "")
        if stored_tool_model in _TOOL_MODEL_MIGRATIONS:
            new_store = dict(store_dict)
            new_store["tool_model"] = _TOOL_MODEL_MIGRATIONS[stored_tool_model]
            if store == "data":
                hass.config_entries.async_update_entry(entry, data=new_store)
            else:
                hass.config_entries.async_update_entry(entry, options=new_store)
            _LOGGER.info(
                "Migrated tool_model %s → %s", stored_tool_model,
                _TOOL_MODEL_MIGRATIONS[stored_tool_model],
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
        await manager.async_shutdown()
    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the integration."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _register_frontend_resources(hass: HomeAssistant) -> None:
    """Register frontend resources used by optional Lovelace cards.

    All card JS (memory timeline, events timeline, camera map) is served as a
    single bundled file from /api/ha_doorbell_jeeves/card_js to ensure reliable
    loading across all dashboard types.
    """
    import hashlib  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415

    from .memory_views import card_js_url  # noqa: PLC0415

    loop = asyncio.get_running_loop()

    # Compute combined hash of all JS files for cache-busting
    hasher = hashlib.md5()  # noqa: S324
    for filename in [
        "jeeves-memory-timeline-card.js",
        "jeeves-events-timeline-card.js",
        "jeeves-camera-map-panel.js",
    ]:
        js_file = Path(__file__).parent / "frontend" / filename
        try:
            content = await loop.run_in_executor(None, js_file.read_bytes)
            hasher.update(content)
        except OSError:
            pass
    file_hash = hasher.hexdigest()[:8]

    # Single bundled URL for all cards
    bundle_url = f"{card_js_url()}?v={file_hash}"

    # Register via add_extra_js_url (loads on every page)
    add_extra_js_url(hass, bundle_url)

    # Also ensure it's registered as a Lovelace resource for the card picker
    await _ensure_lovelace_resources(hass, bundle_url)

    # Register WebSocket commands for camera map
    _register_ws_commands(hass)

    _LOGGER.debug("Registered frontend bundle: %s", bundle_url)


async def _ensure_lovelace_resources(hass: HomeAssistant, bundle_url: str) -> None:
    """Ensure our bundled JS is registered as a Lovelace resource."""
    try:
        ll_resources = hass.data.get("lovelace_resources")
        if ll_resources is None:
            return

        base_url = bundle_url.split("?")[0]

        # Check existing resources - update or create
        for item in ll_resources.async_items():
            item_base = item.get("url", "").split("?")[0]
            if item_base == base_url:
                # Update version if needed
                if item.get("url") != bundle_url:
                    try:
                        await ll_resources.async_update_item(
                            item["id"], {"url": bundle_url, "res_type": "module"}
                        )
                    except Exception:  # noqa: BLE001
                        pass
                return

        # Not found — create it
        try:
            await ll_resources.async_create_item({"res_type": "module", "url": bundle_url})
            _LOGGER.info("Auto-registered Lovelace resource: %s", bundle_url)
        except Exception:  # noqa: BLE001
            pass
    except (AttributeError, KeyError, TypeError):
        pass


async def _setup_reolink(hass: HomeAssistant, entry: ConfigEntry, config: dict[str, Any]) -> None:
    """Verify Reolink configuration is present (no external setup needed).

    Audio input now discovers the existing go2rtc stream at session start.
    Audio output uses native Baichuan protocol directly.
    No RTSP URL construction or go2rtc stream registration is required.
    """
    reolink_entry_id = config.get(CONF_REOLINK_ENTRY_ID, "")
    camera_entity = config.get(CONF_CAMERA_ENTITY, "")
    if reolink_entry_id or camera_entity:
        _LOGGER.info(
            "Reolink mode configured: entry=%s, camera=%s "
            "(audio uses native Baichuan + existing go2rtc stream)",
            reolink_entry_id[:8] if reolink_entry_id else "none",
            camera_entity or "none",
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

    async def handle_inject_text(call: ServiceCall) -> None:
        """Inject text into the AI session (simulates user speech for testing)."""
        manager = await _get_manager(call)
        if manager and hasattr(manager, "_client") and manager._client and manager._client.connected:
            text = call.data.get("text", "")
            turn_complete = call.data.get("turn_complete", True)
            await manager._client.inject_context(
                f"[USER SPEECH] The visitor just said: \"{text}\"",
                turn_complete=turn_complete,
            )
            _LOGGER.info("Injected text into session: %s", text[:100])
        else:
            _LOGGER.warning("Cannot inject text — no active session")

    hass.services.async_register(
        DOMAIN, "inject_text", handle_inject_text,
        schema=vol.Schema({
            vol.Optional("entry_id"): cv.string,
            vol.Required("text"): cv.string,
            vol.Optional("turn_complete"): bool,
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


# ─── WebSocket API for Camera Map ─────────────────────────────────────────────


def _register_ws_commands(hass: HomeAssistant) -> None:
    """Register WebSocket commands for the interactive camera map panel."""
    from homeassistant.components import websocket_api  # noqa: PLC0415

    from .models import CameraPlacement  # noqa: PLC0415

    @websocket_api.websocket_command({
        vol.Required("type"): f"{DOMAIN}/camera_placements/list",
        vol.Optional("entry_id"): str,
    })
    @websocket_api.async_response
    async def ws_list_placements(
        hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        """List all camera placements."""
        manager = _get_manager_from_msg(hass, msg)
        if not manager:
            connection.send_error(msg["id"], "not_found", "No Jeeves entry found")
            return
        placements = [cp.to_dict() for cp in manager.store.camera_placements]
        connection.send_result(msg["id"], {"placements": placements})

    @websocket_api.websocket_command({
        vol.Required("type"): f"{DOMAIN}/camera_placements/save",
        vol.Optional("entry_id"): str,
        vol.Required("placements"): list,
    })
    @websocket_api.async_response
    async def ws_save_placements(
        hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        """Save all camera placements (replaces entire list)."""
        manager = _get_manager_from_msg(hass, msg)
        if not manager:
            connection.send_error(msg["id"], "not_found", "No Jeeves entry found")
            return
        manager.store.camera_placements = [
            CameraPlacement.from_dict(p) for p in msg["placements"]
        ]
        await manager.store.async_save_entities()
        connection.send_result(msg["id"], {"success": True})

    @websocket_api.websocket_command({
        vol.Required("type"): f"{DOMAIN}/camera_placements/cameras",
        vol.Optional("entry_id"): str,
    })
    @websocket_api.async_response
    async def ws_list_available_cameras(
        hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        """List camera entities available for placement (doorbell + managed cameras)."""
        manager = _get_manager_from_msg(hass, msg)
        if not manager:
            connection.send_error(msg["id"], "not_found", "No Jeeves entry found")
            return
        cameras = []
        seen: set[str] = set()

        # Include the primary doorbell camera
        doorbell_cam = manager._config.get(CONF_CAMERA_ENTITY, "")
        if doorbell_cam and doorbell_cam not in seen:
            state = hass.states.get(doorbell_cam)
            cameras.append({
                "entity_id": doorbell_cam,
                "name": state.attributes.get("friendly_name", doorbell_cam) if state else doorbell_cam,
                "description": "Primary doorbell camera",
            })
            seen.add(doorbell_cam)

        # Include all managed camera entities
        for entity in manager.store.managed_entities:
            if entity.entity_id.startswith("camera.") and entity.entity_id not in seen:
                cameras.append({
                    "entity_id": entity.entity_id,
                    "name": entity.name,
                    "description": entity.description,
                })
                seen.add(entity.entity_id)
        connection.send_result(msg["id"], {"cameras": cameras})

    websocket_api.async_register_command(hass, ws_list_placements)
    websocket_api.async_register_command(hass, ws_save_placements)
    websocket_api.async_register_command(hass, ws_list_available_cameras)

    @websocket_api.websocket_command({
        vol.Required("type"): f"{DOMAIN}/camera_placements/verify_audio",
        vol.Optional("entry_id"): str,
        vol.Required("entity_id"): str,
    })
    @websocket_api.async_response
    async def ws_verify_audio(
        hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        """Verify audio stream path for a camera (same discovery as doorbell)."""
        from .reolink_audio import (  # noqa: PLC0415
            ReolinkAudioHandler,
            _discover_go2rtc_url,
            find_reolink_entry_for_camera,
        )

        camera_entity = msg["entity_id"]
        manager = _get_manager_from_msg(hass, msg)

        # Discover the best audio input method
        method = ""
        url = ""
        try:
            # Create a temporary handler to discover the stream
            handler = ReolinkAudioHandler(hass, "verify_probe", lambda x: None)
            handler._camera_entity_id = camera_entity
            reolink_entry_id = find_reolink_entry_for_camera(hass, camera_entity)
            if reolink_entry_id:
                handler._reolink_entry_id = reolink_entry_id
            await handler._discover_reolink_details()

            # Check go2rtc first
            go2rtc_url = await _discover_go2rtc_url(hass)
            if go2rtc_url and handler._camera_unique_id:
                import aiohttp  # noqa: PLC0415
                stream_name = handler._camera_unique_id
                api_url = f"{go2rtc_url}/api/streams?src={stream_name}"
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status == 200:
                                method = "go2rtc"
                                url = f"{go2rtc_url}/api/ws?src={stream_name}"
                except Exception:
                    pass

            # Fallback to RTSP
            if not method and handler._reolink_rtsp_url:
                method = "rtsp"
                url = handler._reolink_rtsp_url

            # Fallback to FLV
            if not method and handler._reolink_flv_url:
                method = "flv"
                url = handler._reolink_flv_url

            if method:
                # Cache the result in the placement
                if manager:
                    for cp in manager.store.camera_placements:
                        if cp.entity_id == camera_entity:
                            cp.audio_method = method
                            cp.audio_url = url
                            break
                    await manager.store.async_save_entities()
                connection.send_result(msg["id"], {"success": True, "method": method, "url": url})
            else:
                connection.send_result(msg["id"], {"success": False, "error": "No audio stream found"})
        except Exception as exc:
            connection.send_result(msg["id"], {"success": False, "error": str(exc)})

    websocket_api.async_register_command(hass, ws_verify_audio)


def _get_manager_from_msg(hass: HomeAssistant, msg: dict) -> JeevesSessionManager | None:
    """Resolve a session manager from a WS message (uses entry_id or first available)."""
    managers = hass.data.get(DOMAIN, {})
    entry_id = msg.get("entry_id")
    if entry_id:
        manager = managers.get(entry_id)
        return manager if isinstance(manager, JeevesSessionManager) else None
    # Return first available manager
    for m in managers.values():
        if isinstance(m, JeevesSessionManager):
            return m
    return None
