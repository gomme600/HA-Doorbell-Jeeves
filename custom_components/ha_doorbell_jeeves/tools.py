"""Dynamic tool generation and execution from admin-configured entities/actions."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .models import ManagedEntity, NotificationTarget
from .store import DataStore

_LOGGER = logging.getLogger(__name__)


def build_system_context(store: DataStore, hass: HomeAssistant) -> str:
    """Build the entity/action context block appended to the system prompt.

    This tells the AI what entities it can see and what actions it can perform.
    """
    lines: list[str] = []

    # Readable entities (all managed entities)
    if store.managed_entities:
        lines.append("\n--- AVAILABLE ENTITIES (you can read their state) ---")
        for entity in store.managed_entities:
            state = hass.states.get(entity.entity_id)
            current = state.state if state else "unavailable"
            lines.append(f"• {entity.name} [{entity.entity_id}]: {entity.description} (current: {current})")

    # Available actions
    all_actions = []
    for entity in store.managed_entities:
        for action in entity.actions:
            all_actions.append((entity, action))

    if all_actions:
        lines.append("\n--- AVAILABLE ACTIONS (you can call these as tools) ---")
        for entity, action in all_actions:
            lines.append(f"• {action.name} (tool: {action.id}): {action.description} [on {entity.name}]")

    # Notification targets
    if store.notification_targets:
        lines.append("\n--- NOTIFICATION TARGETS ---")
        for target in store.notification_targets:
            lines.append(f"• {target.name} (tool: notify_{_slugify(target.name)}): {target.description}")

    return "\n".join(lines)


def build_gemini_tools(store: DataStore) -> list[Any]:
    """Build Gemini-format tool declarations from managed entities."""
    from google.genai import types  # noqa: PLC0415

    declarations: list[types.FunctionDeclaration] = []

    # Read entity state tool (if there are readable entities)
    if store.managed_entities:
        entity_ids = [e.entity_id for e in store.managed_entities]
        declarations.append(types.FunctionDeclaration(
            name="read_entity_state",
            description="Read the current state and attributes of an entity.",
            parameters={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity to read",
                        "enum": entity_ids,
                    }
                },
                "required": ["entity_id"],
            },
        ))

    # Custom actions
    for entity in store.managed_entities:
        for action in entity.actions:
            declarations.append(types.FunctionDeclaration(
                name=action.id,
                description=action.description or action.name,
                parameters={
                    "type": "object",
                    "properties": {
                        "confirm": {
                            "type": "boolean",
                            "description": "Set to true to confirm this action.",
                        }
                    },
                    "required": ["confirm"],
                },
            ))

    # Notification tools
    for target in store.notification_targets:
        tool_name = f"notify_{_slugify(target.name)}"
        declarations.append(types.FunctionDeclaration(
            name=tool_name,
            description=target.description or f"Send a notification via {target.name}",
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The notification message"},
                    "title": {"type": "string", "description": "Notification title (optional)"},
                    "urgency": {
                        "type": "string",
                        "description": "Urgency level",
                        "enum": ["low", "normal", "high", "critical"],
                    },
                },
                "required": ["message"],
            },
        ))

    # PIN verification (always available if PIN is configured)
    declarations.append(types.FunctionDeclaration(
        name="verify_pin",
        description="Verify a PIN code spoken by the visitor for protected actions.",
        parameters={
            "type": "object",
            "properties": {
                "pin": {"type": "string", "description": "The PIN code to verify"},
            },
            "required": ["pin"],
        },
    ))

    if not declarations:
        return []
    return [types.Tool(function_declarations=declarations)]


def build_openai_tools(store: DataStore) -> list[dict[str, Any]]:
    """Build OpenAI-format tool declarations from managed entities."""
    tools: list[dict[str, Any]] = []

    # Read entity state
    if store.managed_entities:
        entity_ids = [e.entity_id for e in store.managed_entities]
        tools.append({
            "type": "function",
            "name": "read_entity_state",
            "description": "Read the current state and attributes of an entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity to read",
                        "enum": entity_ids,
                    }
                },
                "required": ["entity_id"],
            },
        })

    # Custom actions
    for entity in store.managed_entities:
        for action in entity.actions:
            tools.append({
                "type": "function",
                "name": action.id,
                "description": action.description or action.name,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "confirm": {
                            "type": "boolean",
                            "description": "Set to true to confirm this action.",
                        }
                    },
                    "required": ["confirm"],
                },
            })

    # Notification tools
    for target in store.notification_targets:
        tool_name = f"notify_{_slugify(target.name)}"
        tools.append({
            "type": "function",
            "name": tool_name,
            "description": target.description or f"Send notification via {target.name}",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The notification message"},
                    "title": {"type": "string", "description": "Notification title (optional)"},
                    "urgency": {
                        "type": "string",
                        "description": "Urgency level",
                        "enum": ["low", "normal", "high", "critical"],
                    },
                },
                "required": ["message"],
            },
        })

    # PIN verification
    tools.append({
        "type": "function",
        "name": "verify_pin",
        "description": "Verify a PIN code spoken by the visitor for protected actions.",
        "parameters": {
            "type": "object",
            "properties": {
                "pin": {"type": "string", "description": "The PIN code to verify"},
            },
            "required": ["pin"],
        },
    })

    return tools


async def execute_tool_call(
    hass: HomeAssistant,
    store: DataStore,
    function_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Execute a tool call with strict entity validation."""
    try:
        # Read entity state
        if function_name == "read_entity_state":
            return await _execute_read_state(hass, store, arguments)

        # PIN verification (handled by session manager, but fallback here)
        if function_name == "verify_pin":
            return {"success": True, "message": "PIN received for verification"}

        # Notification
        if function_name.startswith("notify_"):
            return await _execute_notification(hass, store, function_name, arguments)

        # Custom action
        return await _execute_action(hass, store, function_name, arguments)

    except Exception as err:
        _LOGGER.exception("Tool execution error: %s", function_name)
        return {"error": str(err)}


async def _execute_read_state(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Read entity state — only allowed entities."""
    entity_id = arguments.get("entity_id", "")
    allowed = [e.entity_id for e in store.managed_entities]
    if entity_id not in allowed:
        _LOGGER.warning("BLOCKED read of non-managed entity: %s", entity_id)
        return {"error": f"Access denied: '{entity_id}' not in managed entities."}

    state = hass.states.get(entity_id)
    if state is None:
        return {"error": f"Entity '{entity_id}' not found or unavailable"}

    # Find the friendly description
    managed = store.get_entity(entity_id)
    return {
        "entity_id": entity_id,
        "name": managed.name if managed else entity_id,
        "state": state.state,
        "unit": state.attributes.get("unit_of_measurement", ""),
        "attributes": {
            k: v for k, v in state.attributes.items()
            if k in ("friendly_name", "temperature", "humidity", "battery", "device_class")
        },
    }


async def _execute_notification(
    hass: HomeAssistant, store: DataStore, function_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Send a notification to a configured target."""
    # Match tool name to target
    target = None
    for t in store.notification_targets:
        if f"notify_{_slugify(t.name)}" == function_name:
            target = t
            break

    if not target:
        return {"error": f"Unknown notification target: {function_name}"}

    message = arguments.get("message", "")
    title = arguments.get("title", "🔔 Doorbell Jeeves")
    urgency = arguments.get("urgency", "normal")

    if "." in target.service:
        domain, svc = target.service.split(".", 1)
    else:
        domain, svc = "notify", target.service

    await hass.services.async_call(
        domain, svc,
        {"message": f"[{urgency.upper()}] {message}", "title": title},
        blocking=True,
    )
    return {"success": True, "message": f"Notification sent via {target.name}"}


async def _execute_action(
    hass: HomeAssistant, store: DataStore, function_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Execute a custom action by ID."""
    entity, action = store.get_action(function_name)
    if not entity or not action:
        return {"error": f"Unknown action: {function_name}"}

    if not arguments.get("confirm", False):
        return {"error": "Action not confirmed. Set confirm=true to proceed."}

    # Parse service
    if "." in action.service:
        domain, svc = action.service.split(".", 1)
    else:
        return {"error": f"Invalid service format: {action.service}"}

    # Build service data — substitute entity_id if present
    service_data = dict(action.service_data)
    if "entity_id" not in service_data:
        service_data["entity_id"] = entity.entity_id

    await hass.services.async_call(domain, svc, service_data, blocking=True)
    return {"success": True, "action": action.name, "entity_id": entity.entity_id}


def _slugify(text: str) -> str:
    """Convert text to a safe slug for tool names."""
    return text.lower().replace(" ", "_").replace("-", "_")
