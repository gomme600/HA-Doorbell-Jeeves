"""Tool system – dynamic declarations for both Gemini and OpenAI formats."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_ALLOWED_ENTITIES, CONF_NOTIFY_SERVICE

_LOGGER = logging.getLogger(__name__)

# ─── Tool Registry ────────────────────────────────────────────────────────────
# Defines all possible tools. Actual availability depends on admin allowlist.

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "turn_on_light",
        "description": "Turn on a light.",
        "domain": "light",
        "service": "turn_on",
        "parameters": {
            "entity_id": {"type": "string", "description": "Light entity ID", "required": True},
            "brightness_pct": {"type": "integer", "description": "Brightness 1-100 (default: 100)"},
        },
        "service_data_map": lambda args: {
            "entity_id": args["entity_id"],
            **({"brightness_pct": args["brightness_pct"]} if "brightness_pct" in args else {}),
        },
    },
    {
        "name": "turn_off_light",
        "description": "Turn off a light.",
        "domain": "light",
        "service": "turn_off",
        "parameters": {"entity_id": {"type": "string", "description": "Light entity ID", "required": True}},
        "service_data_map": lambda args: {"entity_id": args["entity_id"]},
    },
    {
        "name": "unlock_door",
        "description": "Unlock a door or gate.",
        "domain": "lock",
        "service": "unlock",
        "parameters": {"entity_id": {"type": "string", "description": "Lock entity ID", "required": True}},
        "service_data_map": lambda args: {"entity_id": args["entity_id"]},
    },
    {
        "name": "lock_door",
        "description": "Lock a door or gate.",
        "domain": "lock",
        "service": "lock",
        "parameters": {"entity_id": {"type": "string", "description": "Lock entity ID", "required": True}},
        "service_data_map": lambda args: {"entity_id": args["entity_id"]},
    },
    {
        "name": "turn_on_switch",
        "description": "Turn on a switch (gate buzzer, irrigation, etc.).",
        "domain": "switch",
        "service": "turn_on",
        "parameters": {"entity_id": {"type": "string", "description": "Switch entity ID", "required": True}},
        "service_data_map": lambda args: {"entity_id": args["entity_id"]},
    },
    {
        "name": "turn_off_switch",
        "description": "Turn off a switch.",
        "domain": "switch",
        "service": "turn_off",
        "parameters": {"entity_id": {"type": "string", "description": "Switch entity ID", "required": True}},
        "service_data_map": lambda args: {"entity_id": args["entity_id"]},
    },
    {
        "name": "open_cover",
        "description": "Open a cover (garage door, gate, blinds).",
        "domain": "cover",
        "service": "open_cover",
        "parameters": {"entity_id": {"type": "string", "description": "Cover entity ID", "required": True}},
        "service_data_map": lambda args: {"entity_id": args["entity_id"]},
    },
    {
        "name": "close_cover",
        "description": "Close a cover.",
        "domain": "cover",
        "service": "close_cover",
        "parameters": {"entity_id": {"type": "string", "description": "Cover entity ID", "required": True}},
        "service_data_map": lambda args: {"entity_id": args["entity_id"]},
    },
    {
        "name": "get_sensor_state",
        "description": "Read the current value of a sensor.",
        "domain": "sensor",
        "service": None,
        "parameters": {"entity_id": {"type": "string", "description": "Sensor entity ID", "required": True}},
        "service_data_map": None,
    },
    {
        "name": "send_notification",
        "description": "Send a notification to the homeowner.",
        "domain": "_notify",
        "service": None,
        "parameters": {
            "message": {"type": "string", "description": "Notification message", "required": True},
            "title": {"type": "string", "description": "Notification title (optional)"},
        },
        "service_data_map": None,
    },
    {
        "name": "verify_pin",
        "description": "Verify a PIN code spoken by the visitor for protected actions.",
        "domain": "_system",
        "service": None,
        "parameters": {"pin": {"type": "string", "description": "The PIN code", "required": True}},
        "service_data_map": None,
    },
]


# ─── Gemini Format ────────────────────────────────────────────────────────────


def build_tool_declarations(config: dict[str, Any]) -> list[Any]:
    """Build Gemini-format tool declarations scoped to allowed entities."""
    from google.genai import types  # noqa: PLC0415

    allowed: dict[str, list[str]] = config.get(CONF_ALLOWED_ENTITIES, {})
    notify_service = config.get(CONF_NOTIFY_SERVICE, "")
    declarations: list[types.FunctionDeclaration] = []

    for tool_def in TOOL_DEFINITIONS:
        domain = tool_def["domain"]

        if domain == "_system":
            declarations.append(_gemini_declaration(tool_def, None))
            continue
        if domain == "_notify":
            if notify_service:
                declarations.append(_gemini_declaration(tool_def, None))
            continue

        domain_entities = allowed.get(domain, [])
        if not domain_entities:
            continue
        declarations.append(_gemini_declaration(tool_def, domain_entities))

    if not declarations:
        return []
    return [types.Tool(function_declarations=declarations)]


def _gemini_declaration(tool_def: dict[str, Any], entity_enum: list[str] | None) -> Any:
    """Build a single Gemini FunctionDeclaration."""
    from google.genai import types  # noqa: PLC0415

    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param_info in tool_def["parameters"].items():
        prop: dict[str, Any] = {"type": param_info["type"], "description": param_info["description"]}
        if param_name == "entity_id" and entity_enum:
            prop["enum"] = entity_enum
        properties[param_name] = prop
        if param_info.get("required"):
            required.append(param_name)

    return types.FunctionDeclaration(
        name=tool_def["name"],
        description=tool_def["description"],
        parameters={"type": "object", "properties": properties, "required": required},
    )


# ─── OpenAI Format ────────────────────────────────────────────────────────────


def build_openai_tool_declarations(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Build OpenAI-format tool declarations scoped to allowed entities."""
    allowed: dict[str, list[str]] = config.get(CONF_ALLOWED_ENTITIES, {})
    notify_service = config.get(CONF_NOTIFY_SERVICE, "")
    tools: list[dict[str, Any]] = []

    for tool_def in TOOL_DEFINITIONS:
        domain = tool_def["domain"]

        if domain == "_system":
            tools.append(_openai_declaration(tool_def, None))
            continue
        if domain == "_notify":
            if notify_service:
                tools.append(_openai_declaration(tool_def, None))
            continue

        domain_entities = allowed.get(domain, [])
        if not domain_entities:
            continue
        tools.append(_openai_declaration(tool_def, domain_entities))

    return tools


def _openai_declaration(tool_def: dict[str, Any], entity_enum: list[str] | None) -> dict[str, Any]:
    """Build a single OpenAI function tool dict."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param_info in tool_def["parameters"].items():
        prop: dict[str, Any] = {"type": param_info["type"], "description": param_info["description"]}
        if param_name == "entity_id" and entity_enum:
            prop["enum"] = entity_enum
        properties[param_name] = prop
        if param_info.get("required"):
            required.append(param_name)

    return {
        "type": "function",
        "name": tool_def["name"],
        "description": tool_def["description"],
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


# ─── Execution ────────────────────────────────────────────────────────────────


async def execute_tool_call(
    hass: HomeAssistant,
    config: dict[str, Any],
    function_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Execute a tool with strict entity allowlist enforcement."""
    allowed: dict[str, list[str]] = config.get(CONF_ALLOWED_ENTITIES, {})

    tool_def = _get_tool_def(function_name)
    if tool_def is None:
        return {"error": f"Unknown function: {function_name}"}

    domain = tool_def["domain"]

    try:
        if domain == "_system":
            if function_name == "verify_pin":
                return {"success": True, "message": "PIN received"}
            return {"error": "Unknown system tool"}

        if domain == "_notify":
            notify_svc = config.get(CONF_NOTIFY_SERVICE, "")
            if not notify_svc:
                return {"error": "Notification service not configured"}
            message = arguments.get("message", "")
            title = arguments.get("title", "🔔 Doorbell Jeeves")
            svc_domain, svc_name = notify_svc.split(".", 1) if "." in notify_svc else ("notify", notify_svc)
            await hass.services.async_call(svc_domain, svc_name, {"message": message, "title": title}, blocking=True)
            return {"success": True, "message": "Notification sent"}

        if function_name == "get_sensor_state":
            entity_id = arguments.get("entity_id", "")
            if entity_id not in allowed.get("sensor", []):
                return _blocked(entity_id)
            state = hass.states.get(entity_id)
            if state is None:
                return {"error": f"Entity '{entity_id}' not found"}
            return {
                "entity_id": entity_id,
                "state": state.state,
                "unit": state.attributes.get("unit_of_measurement", ""),
                "friendly_name": state.attributes.get("friendly_name", entity_id),
            }

        # Entity-bound service calls
        entity_id = arguments.get("entity_id", "")
        if entity_id not in allowed.get(domain, []):
            return _blocked(entity_id)

        service = tool_def["service"]
        data_mapper = tool_def["service_data_map"]
        if not service or not data_mapper:
            return {"error": f"No service mapping for '{function_name}'"}

        await hass.services.async_call(domain, service, data_mapper(arguments), blocking=True)
        return {"success": True, "action": function_name, "entity_id": entity_id}

    except Exception as err:
        _LOGGER.exception("Tool execution error: %s", function_name)
        return {"error": str(err)}


def _get_tool_def(name: str) -> dict[str, Any] | None:
    for td in TOOL_DEFINITIONS:
        if td["name"] == name:
            return td
    return None


def _blocked(entity_id: str) -> dict[str, Any]:
    _LOGGER.warning("BLOCKED access to non-allowed entity: %s", entity_id)
    return {"error": f"Access denied: '{entity_id}' not in allowed list."}


def get_all_action_names() -> list[str]:
    """All action names for the config UI."""
    return [td["name"] for td in TOOL_DEFINITIONS if td["domain"] != "_system"]
