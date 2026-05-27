"""Dynamic tool generation and execution from admin-configured entities/actions.

Smart tools are auto-generated based on entity domains:
  - camera.* entities → view_camera tool (on-demand snapshots)
  - calendar.* entities → get_calendar_events tool
  - All entities → get_entity_history tool (recent state changes)
  - Global → search_events (cross-entity event search)
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_LLMVISION_CAMERAS,
    CONF_LLMVISION_CATEGORIES,
    CONF_LLMVISION_HOURS_BACK,
    CONF_LLMVISION_INCLUDE_NO_ACTIVITY,
    CONF_LLMVISION_MAX_EVENTS,
    CONF_LLMVISION_TIMELINE_ENABLED,
    DEFAULT_CALENDAR_DAYS,
    DEFAULT_HISTORY_HOURS,
    DEFAULT_LLMVISION_HOURS_BACK,
    DEFAULT_LLMVISION_INCLUDE_NO_ACTIVITY,
    DEFAULT_LLMVISION_MAX_EVENTS,
    GLOBAL_ACTIONS_ENTITY_ID,
    GLOBAL_ACTIONS_ENTITY_NAME,
    MAX_CALENDAR_DAYS,
    MAX_HISTORY_HOURS,
    MAX_LLMVISION_EVENTS,
    MAX_LLMVISION_HOURS,
    TOOL_GET_CALENDAR,
    TOOL_GET_HISTORY,
    TOOL_GET_LLMVISION_EVENTS,
    TOOL_SEARCH_EVENTS,
    TOOL_VIEW_CAMERA,
)
from .models import ManagedEntity, NotificationTarget
from .store import DataStore

_LOGGER = logging.getLogger(__name__)


def _readable_entities(store: DataStore) -> list[ManagedEntity]:
    """Return managed entities that should be exposed for read/history tools."""
    return [entity for entity in store.managed_entities if entity.entity_id != GLOBAL_ACTIONS_ENTITY_ID]


def build_system_context(
    store: DataStore,
    hass: HomeAssistant,
    config: dict[str, Any] | None = None,
) -> str:
    """Build the entity/action context block appended to the system prompt.

    This tells the AI what entities it can see, what actions it can perform,
    and what smart capabilities are available (cameras, calendars, history).
    """
    lines: list[str] = []

    readable_entities = _readable_entities(store)

    # Readable entities (all managed entities except the automation-actions pseudo entity)
    if readable_entities:
        lines.append("\n--- AVAILABLE ENTITIES (you can read their state) ---")
        for entity in readable_entities:
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
            entity_name = GLOBAL_ACTIONS_ENTITY_NAME if entity.entity_id == GLOBAL_ACTIONS_ENTITY_ID else entity.name
            lines.append(f"• {action.name} (tool: {action.id}): {action.description} [on {entity_name}]")

    # Camera viewing capability
    camera_entities = [e for e in readable_entities if e.entity_id.startswith("camera.")]
    if camera_entities:
        lines.append("\n--- CAMERAS (you can view any of these on demand) ---")
        for cam in camera_entities:
            lines.append(f"• {cam.name} [{cam.entity_id}]: {cam.description}")
        lines.append("Use the 'view_camera' tool to get a live snapshot from any camera above.")

    # Calendar capability
    calendar_entities = [e for e in readable_entities if e.entity_id.startswith("calendar.")]
    if calendar_entities:
        lines.append("\n--- CALENDARS (you can check schedules) ---")
        for cal in calendar_entities:
            lines.append(f"• {cal.name} [{cal.entity_id}]: {cal.description}")
        lines.append("Use the 'get_calendar_events' tool to check upcoming events.")

    # History/search capability (always available if entities exist)
    if readable_entities:
        lines.append("\n--- SMART CAPABILITIES ---")
        lines.append("• get_entity_history: View recent state changes for any managed entity")
        lines.append("• search_events: Search across all entity history for specific events/objects")
        lines.append("• recall_memories: Search past doorbell conversations for useful context")
        lines.append("These are useful for answering questions about what happened recently.")

    llmvision_enabled = bool((config or {}).get(CONF_LLMVISION_TIMELINE_ENABLED, False))
    llmvision_service = hass.services.has_service("llmvision", "get_events")
    if llmvision_enabled and llmvision_service:
        lines.append("\n--- LLM VISION TIMELINE ---")
        lines.append(
            "• get_llmvision_events: Query recent LLM Vision detections "
            "(timeline events with titles, descriptions, labels, camera names, and key frames)."
        )

    # Notification targets
    if store.notification_targets:
        lines.append("\n--- NOTIFICATION TARGETS ---")
        for target in store.notification_targets:
            lines.append(f"• {target.name} (tool: notify_{_slugify(target.name)}): {target.description}")

    return "\n".join(lines)


def build_gemini_tools(
    store: DataStore,
    config: dict[str, Any] | None = None,
) -> list[Any]:
    """Build Gemini-format tool declarations from managed entities."""
    from google.genai import types  # noqa: PLC0415

    declarations: list[types.FunctionDeclaration] = []
    readable_entities = _readable_entities(store)

    # ─── Read entity state (always available if entities exist) ────────────────
    if readable_entities:
        entity_ids = [e.entity_id for e in readable_entities]
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

    # ─── View camera on demand ────────────────────────────────────────────────
    camera_entities = [e for e in readable_entities if e.entity_id.startswith("camera.")]
    if camera_entities:
        camera_ids = [e.entity_id for e in camera_entities]
        declarations.append(types.FunctionDeclaration(
            name=TOOL_VIEW_CAMERA,
            description=(
                "Get a live snapshot from a camera. Use this to look at different areas "
                "of the property, check for objects, people, or events. The image will be "
                "shown to you for analysis."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": "Which camera to view",
                        "enum": camera_ids,
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief note on why you're checking this camera (for audit)",
                    },
                },
                "required": ["camera_entity_id"],
            },
        ))

    # ─── Get calendar events ──────────────────────────────────────────────────
    calendar_entities = [e for e in readable_entities if e.entity_id.startswith("calendar.")]
    if calendar_entities:
        calendar_ids = [e.entity_id for e in calendar_entities]
        declarations.append(types.FunctionDeclaration(
            name=TOOL_GET_CALENDAR,
            description=(
                "Get upcoming calendar events to check schedules. Use this to answer "
                "questions about availability, appointments, or whether someone will be "
                "home at a certain time. Returns events for the next few days."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "calendar_entity_id": {
                        "type": "string",
                        "description": "Which calendar to check",
                        "enum": calendar_ids,
                    },
                    "days_ahead": {
                        "type": "integer",
                        "description": "How many days ahead to look (1-14, default 3)",
                    },
                },
                "required": ["calendar_entity_id"],
            },
        ))

    # ─── Get entity history ───────────────────────────────────────────────────
    if readable_entities:
        entity_ids = [e.entity_id for e in readable_entities]
        declarations.append(types.FunctionDeclaration(
            name=TOOL_GET_HISTORY,
            description=(
                "Get recent state change history for an entity. Use this to see what "
                "happened in the past hours — motion detections, door openings, sensor "
                "changes, detected objects, etc."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity to get history for",
                        "enum": entity_ids,
                    },
                    "hours_back": {
                        "type": "integer",
                        "description": "How many hours back to look (1-48, default 4)",
                    },
                },
                "required": ["entity_id"],
            },
        ))

    # ─── Search across all events ─────────────────────────────────────────────
    if readable_entities:
        declarations.append(types.FunctionDeclaration(
            name=TOOL_SEARCH_EVENTS,
            description=(
                "Search across ALL managed entity history for specific events, objects, "
                "or patterns. Use this to answer questions like 'was a ball detected?', "
                "'did anyone come to the door earlier?', 'were there any motion events?'. "
                "Searches state changes, attributes, and event descriptions."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for (e.g., 'ball', 'person', 'motion', 'package')",
                    },
                    "hours_back": {
                        "type": "integer",
                        "description": "How many hours back to search (1-48, default 4)",
                    },
                },
                "required": ["query"],
            },
        ))

    llmvision_enabled = bool((config or {}).get(CONF_LLMVISION_TIMELINE_ENABLED, False))
    if llmvision_enabled:
        declarations.append(types.FunctionDeclaration(
            name=TOOL_GET_LLMVISION_EVENTS,
            description=(
                "Query recent events from the LLM Vision timeline. Use this for "
                "questions about recently seen objects, motion, deliveries, or "
                "activities around specific cameras."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional keyword filter (e.g. 'football', 'package', 'person').",
                    },
                    "hours_back": {
                        "type": "integer",
                        "description": "How many hours back to search (1-168).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of events to return (1-200).",
                    },
                    "cameras": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional camera entity filters.",
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional category filters.",
                    },
                    "include_no_activity": {
                        "type": "boolean",
                        "description": "Whether to include 'no activity' timeline events.",
                    },
                },
            },
        ))

    # ─── Session memory and hangup tools ─────────────────────────────────────
    declarations.append(types.FunctionDeclaration(
        name="recall_memories",
        description=(
            "Search past doorbell interactions. Use when a visitor references a previous "
            "visit or you need context about past events."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term (visitor name, event description, etc.)",
                },
                "hours_back": {
                    "type": "integer",
                    "description": "How many hours back to search (default 72)",
                },
            },
        },
    ))
    declarations.append(types.FunctionDeclaration(
        name="end_conversation",
        description=(
            "End the doorbell conversation. Call this when the visitor says goodbye, "
            "leaves, or the conversation is complete."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief reason for ending (e.g., 'visitor said goodbye')",
                }
            },
        },
    ))

    # ─── Custom actions ───────────────────────────────────────────────────────
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

    # ─── Notification tools ───────────────────────────────────────────────────
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

    # ─── PIN verification ─────────────────────────────────────────────────────
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


def build_openai_tools(
    store: DataStore,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build OpenAI-format tool declarations from managed entities."""
    tools: list[dict[str, Any]] = []
    readable_entities = _readable_entities(store)

    # Read entity state
    if readable_entities:
        entity_ids = [e.entity_id for e in readable_entities]
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

    # View camera
    camera_entities = [e for e in readable_entities if e.entity_id.startswith("camera.")]
    if camera_entities:
        camera_ids = [e.entity_id for e in camera_entities]
        tools.append({
            "type": "function",
            "name": TOOL_VIEW_CAMERA,
            "description": (
                "Get a live snapshot from a camera. Use this to look at different areas "
                "of the property, check for objects, people, or events."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": "Which camera to view",
                        "enum": camera_ids,
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief note on why you're checking this camera",
                    },
                },
                "required": ["camera_entity_id"],
            },
        })

    # Calendar events
    calendar_entities = [e for e in readable_entities if e.entity_id.startswith("calendar.")]
    if calendar_entities:
        calendar_ids = [e.entity_id for e in calendar_entities]
        tools.append({
            "type": "function",
            "name": TOOL_GET_CALENDAR,
            "description": (
                "Get upcoming calendar events. Use to check availability and schedules."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "calendar_entity_id": {
                        "type": "string",
                        "description": "Which calendar to check",
                        "enum": calendar_ids,
                    },
                    "days_ahead": {
                        "type": "integer",
                        "description": "Days ahead to look (1-14, default 3)",
                    },
                },
                "required": ["calendar_entity_id"],
            },
        })

    # Entity history
    if readable_entities:
        entity_ids = [e.entity_id for e in readable_entities]
        tools.append({
            "type": "function",
            "name": TOOL_GET_HISTORY,
            "description": (
                "Get recent state change history for an entity. Shows what happened "
                "in the past hours — motion, door openings, detected objects, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "description": "The entity to get history for",
                        "enum": entity_ids,
                    },
                    "hours_back": {
                        "type": "integer",
                        "description": "Hours back to look (1-48, default 4)",
                    },
                },
                "required": ["entity_id"],
            },
        })

    # Search events
    if readable_entities:
        tools.append({
            "type": "function",
            "name": TOOL_SEARCH_EVENTS,
            "description": (
                "Search ALL entity history for specific events, objects, or patterns. "
                "E.g., 'was a ball detected?', 'any motion events?', 'did a package arrive?'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for",
                    },
                    "hours_back": {
                        "type": "integer",
                        "description": "Hours back to search (1-48, default 4)",
                    },
                },
                "required": ["query"],
            },
        })

    llmvision_enabled = bool((config or {}).get(CONF_LLMVISION_TIMELINE_ENABLED, False))
    if llmvision_enabled:
        tools.append({
            "type": "function",
            "name": TOOL_GET_LLMVISION_EVENTS,
            "description": (
                "Query recent events from the LLM Vision timeline. Use this for "
                "questions about recently seen objects, motion, deliveries, or "
                "activities around specific cameras."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional keyword filter (e.g. 'football', 'package', 'person').",
                    },
                    "hours_back": {
                        "type": "integer",
                        "description": "How many hours back to search (1-168).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of events to return (1-200).",
                    },
                    "cameras": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional camera entity filters.",
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional category filters.",
                    },
                    "include_no_activity": {
                        "type": "boolean",
                        "description": "Whether to include 'no activity' timeline events.",
                    },
                },
            },
        })

    # Session memory and hangup tools
    tools.append({
        "type": "function",
        "name": "recall_memories",
        "description": (
            "Search past doorbell interactions. Use when a visitor references a previous "
            "visit or you need context about past events."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term (visitor name, event description, etc.)",
                },
                "hours_back": {
                    "type": "integer",
                    "description": "How many hours back to search (default 72)",
                },
            },
        },
    })
    tools.append({
        "type": "function",
        "name": "end_conversation",
        "description": (
            "End the doorbell conversation. Call this when the visitor says goodbye, "
            "leaves, or the conversation is complete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief reason for ending (e.g., 'visitor said goodbye')",
                }
            },
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
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a tool call with strict entity validation."""
    try:
        # Read entity state
        if function_name == "read_entity_state":
            return await _execute_read_state(hass, store, arguments)

        # PIN verification (handled by session manager, but fallback here)
        if function_name == "verify_pin":
            return {"success": True, "message": "PIN received for verification"}

        # Smart tools
        if function_name == TOOL_VIEW_CAMERA:
            return await _execute_view_camera(hass, store, arguments)
        if function_name == TOOL_GET_CALENDAR:
            return await _execute_get_calendar(hass, store, arguments)
        if function_name == TOOL_GET_HISTORY:
            return await _execute_get_history(hass, store, arguments)
        if function_name == TOOL_SEARCH_EVENTS:
            return await _execute_search_events(hass, store, arguments)
        if function_name == TOOL_GET_LLMVISION_EVENTS:
            return await _execute_get_llmvision_events(hass, arguments, config)

        # Notification
        if function_name.startswith("notify_"):
            return await _execute_notification(hass, store, function_name, arguments)

        # Custom action
        return await _execute_action(hass, store, function_name, arguments)

    except Exception as err:
        _LOGGER.exception("Tool execution error: %s", function_name)
        return {"error": str(err)}


# ─── Smart Tool Implementations ───────────────────────────────────────────────


async def _execute_view_camera(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Capture a snapshot from a camera and return it for AI analysis.

    The image is injected into the conversation as an inline image so the AI
    can visually analyze it (look for objects, people, etc.).
    """
    camera_id = arguments.get("camera_entity_id", "")
    reason = arguments.get("reason", "on-demand check")

    # Validate entity is managed
    allowed = [e.entity_id for e in store.managed_entities if e.entity_id.startswith("camera.")]
    if camera_id not in allowed:
        return {"error": f"Camera '{camera_id}' not in managed entities."}

    try:
        image = await hass.components.camera.async_get_image(camera_id, timeout=10)
        if not image:
            return {"error": f"Could not get image from {camera_id}"}

        # Return base64 image — the session manager will inject this into the model
        image_b64 = base64.b64encode(image.content).decode("ascii")
        managed = store.get_entity(camera_id)
        cam_name = managed.name if managed else camera_id

        return {
            "success": True,
            "camera": cam_name,
            "reason": reason,
            "_image_base64": image_b64,  # Special key: session manager injects as image
            "_image_mime": "image/jpeg",
            "message": f"Here is the current view from {cam_name}. Analyze what you see.",
        }
    except Exception as err:
        return {"error": f"Failed to capture from {camera_id}: {err}"}


async def _execute_get_calendar(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Get upcoming events from a calendar entity.

    Uses HA's calendar.get_events service to retrieve scheduled events.
    """
    calendar_id = arguments.get("calendar_entity_id", "")
    try:
        requested_days = int(arguments.get("days_ahead", DEFAULT_CALENDAR_DAYS))
    except (TypeError, ValueError):
        requested_days = DEFAULT_CALENDAR_DAYS
    days_ahead = max(1, min(requested_days, MAX_CALENDAR_DAYS))

    # Validate entity is managed
    allowed = [e.entity_id for e in store.managed_entities if e.entity_id.startswith("calendar.")]
    if calendar_id not in allowed:
        return {"error": f"Calendar '{calendar_id}' not in managed entities."}

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)

    try:
        # Use the calendar.get_events service
        result = await hass.services.async_call(
            "calendar", "get_events",
            {
                "entity_id": calendar_id,
                "start_date_time": now.isoformat(),
                "end_date_time": end.isoformat(),
            },
            blocking=True,
            return_response=True,
        )

        # Parse the response
        events_data = result.get(calendar_id, {}).get("events", []) if result else []

        if not events_data:
            managed = store.get_entity(calendar_id)
            cal_name = managed.name if managed else calendar_id
            return {
                "success": True,
                "calendar": cal_name,
                "events": [],
                "message": f"No events found in {cal_name} for the next {days_ahead} day(s).",
            }

        # Format events for the AI
        formatted_events = []
        for event in events_data:
            formatted_events.append({
                "summary": event.get("summary", "Untitled"),
                "start": event.get("start", ""),
                "end": event.get("end", ""),
                "location": event.get("location", ""),
                "description": (event.get("description", "") or "")[:200],
            })

        managed = store.get_entity(calendar_id)
        cal_name = managed.name if managed else calendar_id
        return {
            "success": True,
            "calendar": cal_name,
            "days_ahead": days_ahead,
            "event_count": len(formatted_events),
            "events": formatted_events,
        }
    except Exception as err:
        return {"error": f"Failed to get calendar events: {err}"}


async def _execute_get_history(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Get recent state change history for an entity.

    Uses HA's history component to retrieve past states.
    """
    entity_id = arguments.get("entity_id", "")
    try:
        requested_hours = int(arguments.get("hours_back", DEFAULT_HISTORY_HOURS))
    except (TypeError, ValueError):
        requested_hours = DEFAULT_HISTORY_HOURS
    hours_back = max(1, min(requested_hours, MAX_HISTORY_HOURS))

    # Validate entity is managed
    allowed = [e.entity_id for e in _readable_entities(store)]
    if entity_id not in allowed:
        return {"error": f"Entity '{entity_id}' not in managed entities."}

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_back)

    try:
        # Use HA's history API
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.history import state_changes_during_period  # noqa: PLC0415

        history = await get_instance(hass).async_add_executor_job(
            state_changes_during_period,
            hass,
            start,
            now,
            entity_id,
        )

        states = history.get(entity_id, [])
        if not states:
            return {
                "success": True,
                "entity_id": entity_id,
                "hours_back": hours_back,
                "changes": [],
                "message": f"No state changes recorded for {entity_id} in the past {hours_back} hour(s).",
            }

        # Format state changes
        changes = []
        for state in states[-50:]:  # Last 50 changes max
            change_entry: dict[str, Any] = {
                "state": state.state,
                "time": state.last_changed.isoformat() if state.last_changed else "",
            }
            # Include useful attributes
            attrs = state.attributes or {}
            for key in ("friendly_name", "device_class", "detection_type",
                        "object_type", "label", "score", "count"):
                if key in attrs:
                    change_entry[key] = attrs[key]
            changes.append(change_entry)

        managed = store.get_entity(entity_id)
        entity_name = managed.name if managed else entity_id
        return {
            "success": True,
            "entity": entity_name,
            "entity_id": entity_id,
            "hours_back": hours_back,
            "total_changes": len(changes),
            "changes": changes,
        }
    except Exception as err:
        _LOGGER.debug("History query failed for %s: %s", entity_id, err)
        # Fallback: return current state only
        state = hass.states.get(entity_id)
        return {
            "success": True,
            "entity_id": entity_id,
            "note": "Full history unavailable, showing current state only",
            "current_state": state.state if state else "unknown",
            "current_attributes": dict(state.attributes) if state else {},
        }


async def _execute_search_events(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Search across all managed entity history for matching events.

    This is the "smart search" — looks for keywords in state values,
    attributes, and entity names across all managed entities.
    """
    query = arguments.get("query", "").lower().strip()
    try:
        requested_hours = int(arguments.get("hours_back", DEFAULT_HISTORY_HOURS))
    except (TypeError, ValueError):
        requested_hours = DEFAULT_HISTORY_HOURS
    hours_back = max(1, min(requested_hours, MAX_HISTORY_HOURS))

    if not query:
        return {"error": "Search query cannot be empty."}

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_back)
    entity_ids = [e.entity_id for e in _readable_entities(store)]

    matches: list[dict[str, Any]] = []

    try:
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.history import state_changes_during_period  # noqa: PLC0415

        # Search through all managed entities' history
        for eid in entity_ids:
            try:
                history = await get_instance(hass).async_add_executor_job(
                    state_changes_during_period,
                    hass,
                    start,
                    now,
                    eid,
                )
                states = history.get(eid, [])
                for state in states:
                    # Search in state value
                    searchable = state.state.lower()
                    # Search in attributes
                    attrs = state.attributes or {}
                    attr_text = " ".join(str(v).lower() for v in attrs.values())
                    searchable += " " + attr_text

                    if query in searchable:
                        managed = store.get_entity(eid)
                        matches.append({
                            "entity": managed.name if managed else eid,
                            "entity_id": eid,
                            "state": state.state,
                            "time": state.last_changed.isoformat() if state.last_changed else "",
                            "relevant_attributes": {
                                k: v for k, v in attrs.items()
                                if query in str(v).lower() or k in ("label", "object_type", "detection_type", "score")
                            },
                        })
            except Exception:
                continue

        if not matches:
            return {
                "success": True,
                "query": query,
                "hours_back": hours_back,
                "matches": [],
                "message": f"No events matching '{query}' found in the past {hours_back} hour(s) across {len(entity_ids)} entities.",
            }

        # Sort by time (most recent first) and limit
        matches.sort(key=lambda m: m.get("time", ""), reverse=True)
        matches = matches[:30]

        return {
            "success": True,
            "query": query,
            "hours_back": hours_back,
            "match_count": len(matches),
            "matches": matches,
        }
    except Exception as err:
        _LOGGER.debug("Event search failed: %s", err)
        # Fallback: search current states
        current_matches = []
        for eid in entity_ids:
            state = hass.states.get(eid)
            if state and query in (state.state.lower() + " " + str(state.attributes).lower()):
                managed = store.get_entity(eid)
                current_matches.append({
                    "entity": managed.name if managed else eid,
                    "entity_id": eid,
                    "state": state.state,
                })
        return {
            "success": True,
            "query": query,
            "note": "Full history search unavailable, showing current state matches",
            "matches": current_matches,
        }


async def _execute_get_llmvision_events(
    hass: HomeAssistant,
    arguments: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Query events from the LLM Vision timeline integration."""
    cfg = config or {}
    if not cfg.get(CONF_LLMVISION_TIMELINE_ENABLED, False):
        return {"error": "LLM Vision timeline integration is disabled in Jeeves options."}
    if not hass.services.has_service("llmvision", "get_events"):
        return {"error": "LLM Vision service 'llmvision.get_events' is not available."}

    def _normalize_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return []

    try:
        default_hours = int(cfg.get(CONF_LLMVISION_HOURS_BACK, DEFAULT_LLMVISION_HOURS_BACK))
    except (TypeError, ValueError):
        default_hours = DEFAULT_LLMVISION_HOURS_BACK
    try:
        default_limit = int(cfg.get(CONF_LLMVISION_MAX_EVENTS, DEFAULT_LLMVISION_MAX_EVENTS))
    except (TypeError, ValueError):
        default_limit = DEFAULT_LLMVISION_MAX_EVENTS

    try:
        requested_hours = int(arguments.get("hours_back", default_hours))
    except (TypeError, ValueError):
        requested_hours = default_hours
    try:
        requested_limit = int(arguments.get("limit", default_limit))
    except (TypeError, ValueError):
        requested_limit = default_limit

    hours_back = max(1, min(requested_hours, MAX_LLMVISION_HOURS))
    limit = max(1, min(requested_limit, MAX_LLMVISION_EVENTS))
    raw_include_no_activity = arguments.get(
        "include_no_activity",
        cfg.get(
            CONF_LLMVISION_INCLUDE_NO_ACTIVITY,
            DEFAULT_LLMVISION_INCLUDE_NO_ACTIVITY,
        ),
    )
    if isinstance(raw_include_no_activity, str):
        include_no_activity = raw_include_no_activity.strip().lower() in {"1", "true", "yes", "on"}
    else:
        include_no_activity = bool(raw_include_no_activity)

    cameras = _normalize_list(arguments.get("cameras"))
    if not cameras:
        cameras = _normalize_list(cfg.get(CONF_LLMVISION_CAMERAS, []))
    categories = _normalize_list(arguments.get("categories"))
    if not categories:
        categories = _normalize_list(cfg.get(CONF_LLMVISION_CATEGORIES, []))

    query = str(arguments.get("query", "") or "").strip().lower()

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_back)
    payload: dict[str, Any] = {
        "start": start.isoformat(),
        "end": now.isoformat(),
        "limit": limit,
        "include_no_activity": include_no_activity,
    }
    if cameras:
        payload["cameras"] = cameras
    if categories:
        payload["categories"] = categories

    try:
        response = await hass.services.async_call(
            "llmvision",
            "get_events",
            payload,
            blocking=True,
            return_response=True,
        )
    except Exception as err:
        return {"error": f"Failed to query llmvision.get_events: {err}"}

    events_raw = response.get("events", []) if isinstance(response, dict) else []
    if not isinstance(events_raw, list):
        events_raw = []

    filtered: list[dict[str, Any]] = []

    def _as_text(value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "isoformat"):
            try:
                return str(value.isoformat())
            except Exception:
                return str(value)
        return str(value)

    def _sort_key(item: dict[str, Any]) -> float:
        raw = item.get("start", "")
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            candidate = raw.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(candidate).timestamp()
            except ValueError:
                return 0.0
        return 0.0

    for event in events_raw:
        if not isinstance(event, dict):
            continue
        title = str(event.get("title", "") or "")
        description = str(event.get("description", "") or "")
        label = str(event.get("label", "") or "")
        camera_name = str(event.get("camera_name", "") or "")
        search_blob = f"{title} {description} {label} {camera_name}".lower()
        if query and query not in search_blob:
            continue
        filtered.append(
            {
                "id": event.get("uid", ""),
                "title": title,
                "description": description,
                "label": label,
                "camera": camera_name,
                "start": _as_text(event.get("start", "")),
                "end": _as_text(event.get("end", "")),
                "key_frame": _as_text(event.get("key_frame", "")),
            }
        )

    filtered.sort(key=_sort_key, reverse=True)
    filtered = filtered[:limit]

    return {
        "success": True,
        "query": query,
        "hours_back": hours_back,
        "limit": limit,
        "filters": {
            "cameras": cameras,
            "categories": categories,
            "include_no_activity": include_no_activity,
        },
        "event_count": len(filtered),
        "events": filtered,
        "message": (
            f"Found {len(filtered)} LLM Vision timeline event(s) in the last {hours_back} hour(s)."
            if filtered
            else f"No LLM Vision timeline events found in the last {hours_back} hour(s)."
        ),
    }


# ─── Standard Tool Implementations ───────────────────────────────────────────


async def _execute_read_state(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Read entity state — only allowed entities."""
    entity_id = arguments.get("entity_id", "")
    allowed = [e.entity_id for e in _readable_entities(store)]
    if entity_id not in allowed:
        _LOGGER.warning("BLOCKED read of non-managed entity: %s", entity_id)
        return {"error": f"Access denied: '{entity_id}' not in managed entities."}

    state = hass.states.get(entity_id)
    if state is None:
        return {"error": f"Entity '{entity_id}' not found or unavailable"}

    managed = store.get_entity(entity_id)
    return {
        "entity_id": entity_id,
        "name": managed.name if managed else entity_id,
        "state": state.state,
        "unit": state.attributes.get("unit_of_measurement", ""),
        "attributes": {
            k: v for k, v in state.attributes.items()
            if k in ("friendly_name", "temperature", "humidity", "battery",
                     "device_class", "last_triggered", "current_position",
                     "media_title", "source", "detection_type", "label")
        },
    }


async def _execute_notification(
    hass: HomeAssistant, store: DataStore, function_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Send a notification to a configured target."""
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

    step_results: list[dict[str, Any]] = []
    step_configs = action.steps or [action.service_data or {"action": action.service}]

    for index, step_config in enumerate(step_configs, start=1):
        service_name, service_data = _prepare_service_call(entity, action, step_config)
        if "." not in service_name:
            return {"error": f"Invalid service format: {service_name}"}

        domain, svc = service_name.split(".", 1)
        await hass.services.async_call(domain, svc, service_data, blocking=True)
        step_results.append({"step": index, "service": service_name, "service_data": service_data})

    result: dict[str, Any] = {"success": True, "action": action.name}
    if entity.entity_id != GLOBAL_ACTIONS_ENTITY_ID:
        result["entity_id"] = entity.entity_id
    if len(step_results) > 1:
        result["steps_executed"] = step_results
    elif step_results:
        result["service"] = step_results[0]["service"]
    return result


def _prepare_service_call(
    entity: ManagedEntity,
    action: Any,
    step_config: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """Normalize selector-style and legacy action configs into a service call."""
    config = dict(step_config or {})

    if any(key in config for key in ("action", "service", "target", "data")):
        service_name = config.get("action") or config.get("service") or action.service
        target = dict(config.get("target") or {})
        data = dict(config.get("data") or {})

        if (
            entity.entity_id != GLOBAL_ACTIONS_ENTITY_ID
            and "entity_id" not in target
            and "entity_id" not in data
        ):
            target["entity_id"] = entity.entity_id

        return service_name, {**target, **data}

    service_name = action.service
    service_data = dict(config)
    if entity.entity_id != GLOBAL_ACTIONS_ENTITY_ID and "entity_id" not in service_data:
        service_data["entity_id"] = entity.entity_id
    return service_name, service_data


def _slugify(text: str) -> str:
    """Convert text to a safe slug for tool names."""
    slug = text.lower().replace(" ", "_").replace("-", "_")
    slug = re.sub(r"[^a-z0-9_]+", "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        slug = "tool"
    if slug[0].isdigit():
        slug = f"tool_{slug}"
    return slug
