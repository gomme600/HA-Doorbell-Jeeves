"""Dynamic tool generation and execution from admin-configured entities/actions.

Smart tools are auto-generated based on entity domains:
  - camera.* entities → view_camera tool (on-demand snapshots)
  - calendar.* entities → get_calendar_events tool
  - All entities → get_entity_history tool (recent state changes)
  - Global → search_events (cross-entity event search)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant import config_entries
from homeassistant.components.camera import async_get_image as ha_camera_get_image
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
    TOOL_PLAY_AUDIO,
    TOOL_PTZ_MOVE,
    TOOL_PTZ_RETURN,
    TOOL_SEARCH_EVENTS,
    TOOL_SWITCH_CAMERA,
    TOOL_VIEW_CAMERA,
)
from .models import ManagedEntity, NotificationTarget
from .store import DataStore

_LOGGER = logging.getLogger(__name__)


def _readable_entities(store: DataStore) -> list[ManagedEntity]:
    """Return managed entities that should be exposed for read/history tools."""
    return [entity for entity in store.managed_entities if entity.entity_id != GLOBAL_ACTIONS_ENTITY_ID]


def _llmvision_entries(hass: HomeAssistant) -> list[Any]:
    """Return loaded LLM Vision config entries."""
    return [
        entry
        for entry in hass.config_entries.async_entries("llmvision")
        if entry.state == config_entries.ConfigEntryState.LOADED
    ]


def _llmvision_get_events_available(hass: HomeAssistant) -> bool:
    """Return True if llmvision.get_events service is available."""
    return hass.services.has_service("llmvision", "get_events")


def _llmvision_detected(hass: HomeAssistant) -> bool:
    """Return True if LLM Vision appears to be installed and loaded."""
    return _llmvision_get_events_available(hass) or bool(_llmvision_entries(hass))


def _position_description(x: float, y: float) -> str:
    """Convert x/y (0-1) to a human-readable position relative to house center."""
    # House is at center (0.25-0.75 range), so describe relative to it
    if 0.3 <= x <= 0.7 and 0.3 <= y <= 0.7:
        return "at the house"
    parts = []
    if y < 0.3:
        parts.append("north")
    elif y > 0.7:
        parts.append("south")
    if x < 0.3:
        parts.append("west")
    elif x > 0.7:
        parts.append("east")
    return " ".join(parts) + " of house" if parts else "near the house"


def render_camera_map_image(store: DataStore) -> bytes | None:
    """Render a simple visual map of camera placements as a JPEG image.

    Returns JPEG bytes or None if no placements or PIL unavailable.
    This image is sent to the model at session start for spatial understanding.
    """
    if not store.camera_placements:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415
    except ImportError:
        return None

    W, H = 400, 400
    img = Image.new("RGB", (W, H), (240, 240, 240))
    draw = ImageDraw.Draw(img)

    # Draw house rectangle (center 25%-75%)
    house_rect = (W * 0.25, H * 0.25, W * 0.75, H * 0.75)
    draw.rectangle(house_rect, outline=(100, 100, 200), width=3)
    draw.text((W * 0.45, H * 0.48), "HOUSE", fill=(100, 100, 200))

    # Draw compass
    draw.text((W * 0.47, 5), "N", fill=(80, 80, 80))
    draw.text((W * 0.47, H - 18), "S", fill=(80, 80, 80))
    draw.text((5, H * 0.48), "W", fill=(80, 80, 80))
    draw.text((W - 15, H * 0.48), "E", fill=(80, 80, 80))

    import math  # noqa: PLC0415
    for cp in store.camera_placements:
        cx = int(cp.x * W)
        cy = int(cp.y * H)

        # Draw FOV triangle
        fov_len = 35
        fov_half = 30  # degrees
        rad = math.radians(cp.rotation)
        l_rad = math.radians(cp.rotation - fov_half)
        r_rad = math.radians(cp.rotation + fov_half)
        tip1 = (cx + math.sin(l_rad) * fov_len, cy - math.cos(l_rad) * fov_len)
        tip2 = (cx + math.sin(r_rad) * fov_len, cy - math.cos(r_rad) * fov_len)
        draw.polygon([(cx, cy), tip1, tip2], fill=(70, 130, 230, 60), outline=(70, 130, 230))

        # Draw camera dot
        color = (220, 50, 50) if cp.is_doorbell else (50, 150, 50)
        draw.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=color, outline=(30, 30, 30))

        # Label
        label = cp.name
        if cp.has_audio:
            label += " 🎙"
        draw.text((cx + 8, cy - 6), label, fill=(30, 30, 30))

    # Convert to JPEG
    import io  # noqa: PLC0415
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


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
        # Determine which is the primary camera from config
        primary_cam_id = ""
        if config:
            from .const import CONF_CAMERA_ENTITY  # noqa: PLC0415
            primary_cam_id = config.get(CONF_CAMERA_ENTITY, "")
        lines.append("\n--- CAMERAS ---")
        for cam in camera_entities:
            if cam.entity_id == primary_cam_id:
                marker = " ⭐ PRIMARY — YOU SEE THIS LIVE (do NOT call view_camera for it)"
            else:
                marker = ""
            lines.append(f"• {cam.name} [{cam.entity_id}]: {cam.description}{marker}")
        lines.append(
            "Your primary camera provides a LIVE video feed — you can already see it. "
            "Use 'view_camera' ONLY to check OTHER cameras (carport, garden, etc)."
        )
        lines.append("Use 'switch_camera' to change which camera provides the live video/audio feed.")

    # Camera placement map (spatial context)
    if store.camera_placements:
        lines.append("\n--- CAMERA PLACEMENT MAP (spatial layout of the property) ---")
        lines.append(
            "Property map uses a coordinate grid: x=0 (west) to x=1 (east), "
            "y=0 (north) to y=1 (south). The house is at center (~0.25-0.75)."
        )
        lines.append("Each camera's exact position and facing direction:")
        for cp in store.camera_placements:
            caps = []
            if cp.has_ptz:
                caps.append("PTZ")
            if cp.is_doorbell:
                caps.append("DOORBELL — YOUR PRIMARY CAMERA")
            if cp.has_audio:
                caps.append("2-WAY AUDIO")
            caps_str = f" [{', '.join(caps)}]" if caps else ""
            pos_desc = _position_description(cp.x, cp.y)
            lines.append(
                f"• {cp.name} [{cp.entity_id}]: "
                f"at ({cp.x:.2f}, {cp.y:.2f}) = {pos_desc}, "
                f"facing {cp.facing_direction} ({int(cp.rotation)}°){caps_str}"
            )
            if cp.area_description:
                lines.append(f"  Covers: {cp.area_description}")
        lines.append(
            "\nSpatial reasoning: If a person walks from a camera's view toward "
            "another camera's position, they will appear on that next camera. "
            "Use coordinates and facing directions to follow movement."
        )
        audio_cameras = [cp for cp in store.camera_placements if cp.has_audio]
        if audio_cameras:
            names = ", ".join(c.name for c in audio_cameras)
            lines.append(
                f"\n2-way audio cameras ({names}): You can switch_camera to talk/listen "
                "from these cameras in addition to the doorbell."
            )
        ptz_cameras = [cp for cp in store.camera_placements if cp.has_ptz]
        if ptz_cameras:
            lines.append(
                "PTZ cameras can be moved with 'ptz_move' tool (up/down/left/right) "
                "and returned to monitoring position with 'ptz_return_to_monitor'."
            )

    # Calendar capability
    calendar_entities = [e for e in readable_entities if e.entity_id.startswith("calendar.")]
    if calendar_entities:
        lines.append("\n--- CALENDARS (you can check schedules) ---")
        for cal in calendar_entities:
            lines.append(f"• {cal.name} [{cal.entity_id}]: {cal.description}")
        lines.append("Use the 'get_calendar_events' tool to check upcoming events.")

    # Audio files (playable sounds)
    if store.audio_files:
        lines.append("\n--- AUDIO FILES (you can play these over speakers/cameras) ---")
        lines.append("Use the 'play_audio' tool to play any of these sounds:")
        by_cat: dict[str, list[str]] = {}
        for af in store.audio_files:
            cat = af.category or "General"
            by_cat.setdefault(cat, [])
            desc = f" — {af.description}" if af.description else ""
            by_cat[cat].append(f"• {af.name} (id: {af.id}){desc}")
        for cat, items in by_cat.items():
            lines.append(f"  [{cat}]")
            lines.extend(f"    {item}" for item in items)

    # History/search capability (always available if entities exist)
    if readable_entities:
        lines.append("\n--- SMART CAPABILITIES ---")
        lines.append("• get_entity_history: View recent state changes for any managed entity")
        lines.append("• search_events: Search across all entity history for specific events/objects")
        lines.append("• recall_memories: Search past doorbell conversations for useful context")
        lines.append("These are useful for answering questions about what happened recently.")

    llmvision_enabled = bool((config or {}).get(CONF_LLMVISION_TIMELINE_ENABLED, False))
    if llmvision_enabled and _llmvision_detected(hass):
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

    task_instructions = store.task_instructions if hasattr(store, "task_instructions") else []
    if task_instructions:
        lines.append("\n--- TASK INSTRUCTIONS (follow these for specific situations) ---")
        for instr in task_instructions:
            lines.append(f"\n## {instr.title}")
            lines.append(instr.text)

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
        primary_cam = ""
        if config:
            from .const import CONF_CAMERA_ENTITY  # noqa: PLC0415
            primary_cam = config.get(CONF_CAMERA_ENTITY, "")
        # Only list NON-primary cameras in the enum — the primary is already
        # visible via the live video feed. view_camera is for OTHER cameras.
        other_camera_ids = [e.entity_id for e in camera_entities if e.entity_id != primary_cam]
        declarations.append(types.FunctionDeclaration(
            name=TOOL_VIEW_CAMERA,
            description=(
                "Capture a high-resolution snapshot from a DIFFERENT camera (not the doorbell). "
                "You already have a LIVE video feed from your primary doorbell camera — "
                "do NOT call this tool to see the doorbell view. "
                "Use this ONLY to check other areas: carport, garden, terrace, etc."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": "Which other camera to snapshot (NOT the doorbell — you already see that live).",
                        "enum": other_camera_ids,
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why you need this snapshot",
                    },
                },
                "required": ["camera_entity_id", "reason"],
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
            "End the doorbell conversation gracefully. IMPORTANT: Say your goodbye/farewell "
            "to the visitor BEFORE calling this tool, OR the system will give you a few seconds "
            "to say goodbye after you call it. Call when the visitor says goodbye, "
            "leaves, asks to hang up, or the conversation is naturally complete."
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
    declarations.append(types.FunctionDeclaration(
        name="no_action_needed",
        description=(
            "Call this during monitoring checks when nothing requires attention. "
            "This signals that you assessed the situation and no action is needed. "
            "Do NOT speak when calling this tool — remain completely silent."
        ),
        parameters={
            "type": "object",
            "properties": {},
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
            description=(
                (target.description or f"Send a doorbell notification to {target.name}") +
                " The notification includes a live camera snapshot and action buttons "
                "(Coming / Not Available). Keep the message SHORT and factual — "
                "state ONLY what you can CLEARLY see (e.g., 'Someone at the door', "
                "'A delivery driver with a package'). Do NOT guess identity unless you "
                "are certain from reference photos. The camera image is attached automatically."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": (
                            "Short factual description of the visitor. ONLY describe what you can "
                            "clearly see in the live camera feed. If unsure, say 'Someone at the door'."
                        ),
                    },
                    "title": {"type": "string", "description": "Notification title (optional)"},
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

    # ─── Save important event ─────────────────────────────────────────────────
    declarations.append(types.FunctionDeclaration(
        name="save_event",
        description=(
            "Save an important event for the homeowner to review later. Use this for "
            "information that needs to be recorded: visitor contact details, delivery "
            "instructions, suspicious activity reports, messages left by visitors, etc. "
            "This will also send a notification to all homeowners."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short title (e.g., 'Delivery attempt - package too large for mailbox')",
                },
                "description": {
                    "type": "string",
                    "description": "Detailed description including all relevant info (names, numbers, instructions)",
                },
                "severity": {
                    "type": "string",
                    "description": "Event importance level",
                    "enum": ["info", "warning", "urgent"],
                },
            },
            "required": ["title", "description"],
        },
    ))

    # ─── Attach photo to event ────────────────────────────────────────────────
    declarations.append(types.FunctionDeclaration(
        name="attach_event_photo",
        description=(
            "Attach a photo from a camera to the most recent saved event. Use this to "
            "document visual evidence: the visitor, a suspicious person, a package, etc. "
            "Can be called multiple times for multiple photos."
        ),
        parameters={
            "type": "object",
            "properties": {
                "camera_entity_id": {
                    "type": "string",
                    "description": "Camera to take snapshot from (use doorbell camera by default)",
                },
                "description": {
                    "type": "string",
                    "description": "What this photo shows (e.g., 'Visitor at the door', 'Package left at step')",
                },
            },
            "required": ["camera_entity_id"],
        },
    ))

    # ─── Check owner availability ─────────────────────────────────────────────
    declarations.append(types.FunctionDeclaration(
        name="check_owner_availability",
        description=(
            "Check if any homeowner has responded to the doorbell notification. "
            "Returns who pressed 'Coming' (on their way to the door), who pressed "
            "'Not Available', and who hasn't responded yet. Use this to inform "
            "visitors whether someone is coming to meet them."
        ),
        parameters={
            "type": "object",
            "properties": {},
        },
    ))

    # ─── Extend session timeout ───────────────────────────────────────────────
    declarations.append(types.FunctionDeclaration(
        name="extend_session",
        description=(
            "Extend the current session timeout. Use this when you need more time: "
            "monitoring a delivery driver, watching a suspicious person, waiting for "
            "someone to arrive, or any situation where the conversation or observation "
            "needs to continue beyond the normal timeout."
        ),
        parameters={
            "type": "object",
            "properties": {
                "extra_seconds": {
                    "type": "integer",
                    "description": "Additional seconds to add (30-300)",
                },
                "reason": {
                    "type": "string",
                    "description": "Why the session needs to be extended",
                },
            },
            "required": ["extra_seconds", "reason"],
        },
    ))

    # ─── Switch active camera (audio + video feed) ────────────────────────────
    if camera_entities:
        camera_ids = [e.entity_id for e in camera_entities]
        declarations.append(types.FunctionDeclaration(
            name=TOOL_SWITCH_CAMERA,
            description=(
                "Switch the live video and 2-way audio feed to a different camera. "
                "Use this to follow a person moving around the property, monitor a "
                "different area, or verify activity at another location. The previous "
                "camera feed will stop and the new one will start."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": "The camera to switch to",
                        "enum": camera_ids,
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why you are switching cameras (for audit log)",
                    },
                },
                "required": ["camera_entity_id", "reason"],
            },
        ))

    # ─── PTZ camera controls ──────────────────────────────────────────────────
    ptz_cameras = [cp for cp in store.camera_placements if cp.has_ptz]
    if ptz_cameras:
        ptz_camera_ids = [cp.entity_id for cp in ptz_cameras]
        declarations.append(types.FunctionDeclaration(
            name=TOOL_PTZ_MOVE,
            description=(
                "Move a PTZ camera in a direction. Use this to track a person, "
                "look at something specific, or scan an area. The camera will "
                "move in the specified direction briefly."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": "Which PTZ camera to move",
                        "enum": ptz_camera_ids,
                    },
                    "direction": {
                        "type": "string",
                        "description": "Direction to move the camera",
                        "enum": ["up", "down", "left", "right"],
                    },
                },
                "required": ["camera_entity_id", "direction"],
            },
        ))
        declarations.append(types.FunctionDeclaration(
            name=TOOL_PTZ_RETURN,
            description=(
                "Return a PTZ camera to its monitoring position (home). Use this "
                "after you've finished tracking or scanning, or when done observing "
                "a specific area. All moved cameras are automatically returned when "
                "the session ends."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": "Which PTZ camera to return to home position",
                        "enum": ptz_camera_ids,
                    },
                },
                "required": ["camera_entity_id"],
            },
        ))

    # ─── Play audio file ──────────────────────────────────────────────────────
    if store.audio_files:
        audio_ids = [af.id for af in store.audio_files]
        # Build target list: all media_player entities + cameras with audio
        target_entities = [
            e.entity_id for e in store.managed_entities
            if e.entity_id.startswith("media_player.")
        ]
        # Include cameras with 2-way audio
        for cp in store.camera_placements:
            if cp.has_audio and cp.entity_id not in target_entities:
                target_entities.append(cp.entity_id)
        target_entity_property: dict[str, Any] = {
            "type": "string",
            "description": (
                "Which speaker/camera to play on. "
                "Use a media_player entity or a camera with 2-way audio."
            ),
        }
        if target_entities:
            target_entity_property["enum"] = target_entities
        declarations.append(types.FunctionDeclaration(
            name=TOOL_PLAY_AUDIO,
            description=(
                "Play a pre-configured audio file/sound over a speaker or camera. "
                "Use this to play sound effects, alerts, music, or themed audio "
                "during interactions. The audio plays on the specified target device."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "audio_id": {
                        "type": "string",
                        "description": "Which audio file to play",
                        "enum": audio_ids,
                    },
                    "target_entity_id": target_entity_property,
                },
                "required": ["audio_id"],
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
        primary_cam_oai = ""
        if config:
            from .const import CONF_CAMERA_ENTITY  # noqa: PLC0415
            primary_cam_oai = config.get(CONF_CAMERA_ENTITY, "")
        primary_note_oai = f" Defaults to primary camera ({primary_cam_oai})." if primary_cam_oai else ""
        tools.append({
            "type": "function",
            "name": TOOL_VIEW_CAMERA,
            "description": (
                "Get a high-resolution snapshot from a camera for visual analysis."
                f"{primary_note_oai} "
                "Only specify a different camera_entity_id to check a DIFFERENT area."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": (
                            f"Camera to snapshot. OMIT to use primary camera ({primary_cam_oai}). "
                            "Only specify if checking a different camera."
                            if primary_cam_oai else "Which camera to view"
                        ),
                        "enum": camera_ids,
                    },
                    "reason": {
                        "type": "string",
                        "description": "Brief note on why you're checking this camera",
                    },
                },
                "required": [],
            },
        })

        # Switch camera
        tools.append({
            "type": "function",
            "name": TOOL_SWITCH_CAMERA,
            "description": (
                "Switch the live video and 2-way audio feed to a different camera. "
                "Use this to follow a person or monitor a different area."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": "The camera to switch to",
                        "enum": camera_ids,
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why switching cameras",
                    },
                },
                "required": ["camera_entity_id", "reason"],
            },
        })

    # PTZ controls
    ptz_cameras = [cp for cp in store.camera_placements if cp.has_ptz]
    if ptz_cameras:
        ptz_camera_ids = [cp.entity_id for cp in ptz_cameras]
        tools.append({
            "type": "function",
            "name": TOOL_PTZ_MOVE,
            "description": "Move a PTZ camera in a direction to track or scan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": "Which PTZ camera to move",
                        "enum": ptz_camera_ids,
                    },
                    "direction": {
                        "type": "string",
                        "description": "Direction to move",
                        "enum": ["up", "down", "left", "right"],
                    },
                },
                "required": ["camera_entity_id", "direction"],
            },
        })
        tools.append({
            "type": "function",
            "name": TOOL_PTZ_RETURN,
            "description": "Return a PTZ camera to its monitoring/home position.",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera_entity_id": {
                        "type": "string",
                        "description": "Which PTZ camera to return home",
                        "enum": ptz_camera_ids,
                    },
                },
                "required": ["camera_entity_id"],
            },
        })

    # Play audio file
    if store.audio_files:
        audio_ids = [af.id for af in store.audio_files]
        target_entities = [
            e.entity_id for e in store.managed_entities
            if e.entity_id.startswith("media_player.")
        ]
        for cp in store.camera_placements:
            if cp.has_audio and cp.entity_id not in target_entities:
                target_entities.append(cp.entity_id)
        target_entity_property: dict[str, Any] = {
            "type": "string",
            "description": "Speaker or camera to play on",
        }
        if target_entities:
            target_entity_property["enum"] = target_entities
        tools.append({
            "type": "function",
            "name": TOOL_PLAY_AUDIO,
            "description": (
                "Play a pre-configured audio file/sound over a speaker or camera."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "audio_id": {
                        "type": "string",
                        "description": "Which audio file to play",
                        "enum": audio_ids,
                    },
                    "target_entity_id": target_entity_property,
                },
                "required": ["audio_id"],
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
            "End the doorbell conversation gracefully. IMPORTANT: Say your goodbye/farewell "
            "to the visitor BEFORE calling this tool, OR the system will give you a few seconds "
            "to say goodbye after you call it. Call when the visitor says goodbye, "
            "leaves, asks to hang up, or the conversation is naturally complete."
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
            "description": (
                (target.description or f"Send a doorbell notification to {target.name}") +
                " Includes camera snapshot and Coming/Not Available buttons. "
                "Keep message SHORT and factual — only describe what you clearly see."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": (
                            "Short factual description of what you see. "
                            "If unsure, say 'Someone at the door'."
                        ),
                    },
                    "title": {"type": "string", "description": "Notification title (optional)"},
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
            return await _execute_view_camera(hass, store, arguments, config)
        if function_name == TOOL_SWITCH_CAMERA:
            return await _execute_switch_camera(hass, store, arguments)
        if function_name == TOOL_PTZ_MOVE:
            return await _execute_ptz_move(hass, store, arguments)
        if function_name == TOOL_PTZ_RETURN:
            return await _execute_ptz_return(hass, store, arguments)
        if function_name == TOOL_GET_CALENDAR:
            return await _execute_get_calendar(hass, store, arguments)
        if function_name == TOOL_GET_HISTORY:
            return await _execute_get_history(hass, store, arguments)
        if function_name == TOOL_SEARCH_EVENTS:
            return await _execute_search_events(hass, store, arguments)
        if function_name == TOOL_GET_LLMVISION_EVENTS:
            return await _execute_get_llmvision_events(hass, arguments, config)

        # Important events
        if function_name == "save_event":
            return await _execute_save_event(hass, store, arguments, config)
        if function_name == "attach_event_photo":
            return await _execute_attach_event_photo(hass, store, arguments, config)

        # Owner availability
        if function_name == "check_owner_availability":
            return _execute_check_availability(hass, config)

        # Extend session timeout
        if function_name == "extend_session":
            return _execute_extend_session(arguments, config)

        # Play audio file
        if function_name == TOOL_PLAY_AUDIO:
            return await _execute_play_audio(hass, store, arguments, config)

        # Recall memories
        if function_name == "recall_memories":
            return await _execute_recall_memories(hass, store, arguments, config)

        # Notification
        if function_name.startswith("notify_"):
            return await _execute_notification(hass, store, function_name, arguments, config)

        # Custom action
        return await _execute_action(hass, store, function_name, arguments)

    except Exception as err:
        _LOGGER.exception("Tool execution error: %s", function_name)
        return {
            "success": False,
            "error": f"{function_name} failed: {err}",
            "instruction": "Do not claim the tool succeeded.",
        }


# ─── Smart Tool Implementations ───────────────────────────────────────────────


async def _execute_view_camera(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture a snapshot from a camera and return it for AI analysis.

    The image is injected into the conversation as an inline image so the AI
    can visually analyze it (look for objects, people, etc.).
    """
    camera_id = arguments.get("camera_entity_id", "")
    reason = arguments.get("reason", "on-demand check")

    # Default to the active/primary session camera if none specified
    if not camera_id and config:
        from .const import CONF_CAMERA_ENTITY  # noqa: PLC0415
        camera_id = config.get(CONF_CAMERA_ENTITY, "")
        if camera_id:
            _LOGGER.info("view_camera: defaulting to primary camera %s", camera_id)

    # Validate entity is managed
    allowed = [e.entity_id for e in store.managed_entities if e.entity_id.startswith("camera.")]
    if camera_id not in allowed:
        return {"error": f"Camera '{camera_id}' not in managed entities."}

    try:
        raw_jpeg: bytes | None = None
        # Try HA camera snapshot API
        image = await asyncio.wait_for(
            ha_camera_get_image(hass, camera_id, timeout=8),
            timeout=10,
        )
        if image and image.content:
            raw_jpeg = image.content
            _LOGGER.info("Captured HA snapshot for %s: %d bytes", camera_id, len(raw_jpeg))
        else:
            # Fallback: try go2rtc snapshot
            import aiohttp  # noqa: PLC0415

            from .reolink_audio import _discover_go2rtc_url, _get_go2rtc_session  # noqa: PLC0415
            base_url = await _discover_go2rtc_url(hass)
            if base_url:
                url = f"{base_url}/api/frame.jpeg?src={camera_id}"
                session = _get_go2rtc_session(hass)
                own_session: aiohttp.ClientSession | None = None
                if session is None:
                    own_session = aiohttp.ClientSession()
                    session = own_session
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            raw_jpeg = await resp.read()
                            _LOGGER.info("Captured go2rtc fallback snapshot for %s: %d bytes", camera_id, len(raw_jpeg))
                except Exception:
                    _LOGGER.debug("go2rtc snapshot fallback failed for %s", camera_id, exc_info=True)
                finally:
                    if own_session is not None:
                        await own_session.close()

        if not raw_jpeg:
            return {"error": f"Could not get image from {camera_id} (all methods failed)"}

        # Process image (resize/optimize) — use higher quality for on-demand analysis
        # than the live feed (which is optimized for bandwidth at lower quality)
        from .frame_processor import process_frame  # noqa: PLC0415
        processed = await hass.async_add_executor_job(
            process_frame, raw_jpeg, 1280, 960, 85
        )

        # Return base64 image — the session manager will inject this into the model
        image_b64 = base64.b64encode(processed).decode("ascii")
        managed = store.get_entity(camera_id)
        cam_name = managed.name if managed else camera_id

        return {
            "success": True,
            "camera": cam_name,
            "camera_entity_id": camera_id,
            "reason": reason,
            "_image_base64": image_b64,  # Special key: session manager injects as image
            "_image_mime": "image/jpeg",
            "_image_context": (
                f"[VISUAL ANALYSIS REQUIRED] Camera: {cam_name} ({camera_id})\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "MANDATORY RULES FOR THIS IMAGE:\n"
                "1. Describe ONLY what you can ACTUALLY SEE in the pixel data.\n"
                "2. Do NOT use camera area descriptions, location metadata, or "
                "expectations to fabricate what should be visible.\n"
                "3. If NO PERSON is clearly visible in the image, you MUST say "
                "'No person is visible in this image' — do NOT invent one.\n"
                "4. If the image shows an empty scene (hallway, garden, room), "
                "describe it as empty. Do NOT assume someone 'must be there'.\n"
                "5. Do NOT copy descriptions from system prompts or camera configs "
                "(like 'near the letterbox' or 'at the front door') unless you can "
                "genuinely see those objects AND a person next to them.\n"
                "6. Before notifying anyone about a visitor, VERIFY you can see a "
                "human figure. If unsure, say 'I cannot clearly identify a person'.\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "Analyze the attached image NOW. What do you ACTUALLY see?"
            ),
            "message": (
                f"Snapshot captured from {cam_name}. "
                "IMPORTANT: Describe ONLY what is visible in the actual image pixels. "
                "If no person is visible, say so clearly. Do NOT fabricate descriptions "
                "based on expected context or camera metadata."
            ),
        }
    except asyncio.TimeoutError:
        return {
            "success": False,
            "error": (
                f"Timed out while capturing image from {camera_id}. "
                "No current image was provided to the model."
            ),
        }
    except Exception as err:
        return {
            "success": False,
            "error": f"Failed to capture from {camera_id}: {err}. No current image was provided to the model.",
        }


async def _execute_switch_camera(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Switch the active video/audio feed to a different camera.

    Returns a special key that the session manager intercepts to change
    the live camera feed source.
    """
    camera_id = arguments.get("camera_entity_id", "")
    reason = arguments.get("reason", "")

    allowed = [e.entity_id for e in store.managed_entities if e.entity_id.startswith("camera.")]
    if camera_id not in allowed:
        return {"error": f"Camera '{camera_id}' not in managed entities."}

    managed = store.get_entity(camera_id)
    cam_name = managed.name if managed else camera_id
    placement = next((cp for cp in store.camera_placements if cp.entity_id == camera_id), None)
    audio_note = (
        "Two-way audio will also be switched if the camera audio setup succeeds."
        if placement and placement.has_audio
        else "This camera is not marked as two-way-audio capable, so only video is expected to switch."
    )
    _LOGGER.info("Switching active camera to %s (%s): %s", cam_name, camera_id, reason)

    return {
        "success": True,
        "message": f"Switching live video feed to {cam_name}. {audio_note}",
        "_switch_camera": camera_id,  # Special key: session manager processes this
    }


async def _execute_ptz_move(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Move a PTZ camera in a direction by calling the configured service."""
    camera_id = arguments.get("camera_entity_id", "")
    direction = arguments.get("direction", "")

    if direction not in ("up", "down", "left", "right"):
        return {"error": f"Invalid direction: {direction}. Use up/down/left/right."}

    # Find camera placement with PTZ config
    placement = None
    for cp in store.camera_placements:
        if cp.entity_id == camera_id:
            placement = cp
            break

    if not placement or not placement.has_ptz:
        return {"error": f"Camera '{camera_id}' does not have PTZ controls configured."}

    # Get the service entity for this direction
    ptz_entity = getattr(placement, f"ptz_{direction}", "")
    if not ptz_entity:
        return {"error": f"PTZ {direction} not configured for {camera_id}."}

    try:
        # Call the PTZ service (button.press or similar)
        domain = ptz_entity.split(".")[0]
        if domain == "button":
            await hass.services.async_call("button", "press", {"entity_id": ptz_entity})
        elif domain == "script":
            await hass.services.async_call("script", "turn_on", {"entity_id": ptz_entity})
        else:
            # Generic: try homeassistant.turn_on
            await hass.services.async_call("homeassistant", "turn_on", {"entity_id": ptz_entity})

        _LOGGER.info("PTZ move %s on %s via %s", direction, camera_id, ptz_entity)
        managed = store.get_entity(camera_id)
        cam_name = managed.name if managed else camera_id
        return {
            "success": True,
            "message": f"Moving {cam_name} {direction}.",
            "_ptz_moved_camera": camera_id,  # Track that this camera was moved
        }
    except Exception as err:
        return {"error": f"PTZ move failed: {err}"}


async def _execute_ptz_return(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Return a PTZ camera to its monitoring/home position."""
    camera_id = arguments.get("camera_entity_id", "")

    placement = None
    for cp in store.camera_placements:
        if cp.entity_id == camera_id:
            placement = cp
            break

    if not placement:
        return {"error": f"Camera '{camera_id}' not found in camera placements."}

    if not placement.ptz_return_to_monitor:
        return {"error": f"No return-to-monitor configured for {camera_id}."}

    try:
        ptz_entity = placement.ptz_return_to_monitor
        domain = ptz_entity.split(".")[0]
        if domain == "button":
            await hass.services.async_call("button", "press", {"entity_id": ptz_entity})
        elif domain == "script":
            await hass.services.async_call("script", "turn_on", {"entity_id": ptz_entity})
        else:
            await hass.services.async_call("homeassistant", "turn_on", {"entity_id": ptz_entity})

        _LOGGER.info("PTZ return to monitor: %s via %s", camera_id, ptz_entity)
        managed = store.get_entity(camera_id)
        cam_name = managed.name if managed else camera_id
        return {
            "success": True,
            "message": f"{cam_name} returned to monitoring position.",
            "_ptz_returned_camera": camera_id,
        }
    except Exception as err:
        return {"error": f"PTZ return failed: {err}"}


async def _execute_play_audio(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Play a configured audio file over a speaker or camera."""
    audio_id = arguments.get("audio_id", "")
    target = arguments.get("target_entity_id", "")

    # Find the audio file
    audio_file = None
    for af in store.audio_files:
        if af.id == audio_id:
            audio_file = af
            break
    if not audio_file:
        return {"error": f"Audio file '{audio_id}' not found."}

    from .const import CONF_MEDIA_PLAYER_ENTITY  # noqa: PLC0415

    allowed_targets = {
        entity.entity_id
        for entity in store.managed_entities
        if entity.entity_id.startswith("media_player.")
    }
    allowed_targets.update(
        placement.entity_id for placement in store.camera_placements if placement.has_audio
    )
    default_target = (config or {}).get(CONF_MEDIA_PLAYER_ENTITY, "")
    if default_target:
        allowed_targets.add(default_target)

    # Determine target entity
    if not target:
        # Default to the configured media player if available
        target = default_target
    if not target:
        return {"error": "No target speaker specified and no default configured."}
    if target not in allowed_targets:
        return {"error": f"Target '{target}' is not an allowed audio output."}

    # Play the audio
    try:
        media_id = audio_file.media_id
        media_type = audio_file.media_type

        if target.startswith("media_player."):
            await hass.services.async_call(
                "media_player", "play_media",
                {
                    "entity_id": target,
                    "media_content_id": media_id,
                    "media_content_type": media_type,
                },
                blocking=True,
            )
        elif target.startswith("camera."):
            # For cameras, use media_player.play_media if available via proxy
            # or fall back to a generic approach
            await hass.services.async_call(
                "media_player", "play_media",
                {
                    "entity_id": target,
                    "media_content_id": media_id,
                    "media_content_type": media_type,
                },
                blocking=True,
            )
        else:
            await hass.services.async_call(
                "media_player", "play_media",
                {
                    "entity_id": target,
                    "media_content_id": media_id,
                    "media_content_type": media_type,
                },
                blocking=True,
            )

        _LOGGER.info("Playing audio '%s' on %s (media_id=%s)", audio_file.name, target, media_id)
        return {
            "success": True,
            "message": f"Playing '{audio_file.name}' on {target}.",
        }
    except Exception as err:
        _LOGGER.warning("Failed to play audio '%s' on %s: %s", audio_id, target, err)
        return {"error": f"Failed to play audio: {err}"}


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


async def _query_llmvision_events_with_timeline(
    hass: HomeAssistant,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Query LLM Vision timeline directly for installs without get_events service."""
    try:
        from custom_components.llmvision.const import CONF_PROVIDER as LLMVISION_CONF_PROVIDER  # noqa: PLC0415
        from custom_components.llmvision.timeline import Timeline  # noqa: PLC0415
    except Exception as err:
        raise RuntimeError("LLM Vision timeline backend is unavailable for compatibility mode.") from err

    settings_entry = None
    loaded_entries = _llmvision_entries(hass)
    for entry in loaded_entries:
        if str(entry.data.get(LLMVISION_CONF_PROVIDER, "")).strip().lower() == "settings":
            settings_entry = entry
            break
    if settings_entry is None and loaded_entries:
        settings_entry = loaded_entries[0]
    if settings_entry is None:
        raise RuntimeError("No loaded LLM Vision config entry found.")

    timeline = Timeline(hass, settings_entry)
    events = await timeline.get_events_json(
        start=payload.get("start"),
        end=payload.get("end"),
        cameras=[str(camera).lower() for camera in payload.get("cameras", []) if str(camera).strip()],
        categories=[
            str(category).lower()
            for category in payload.get("categories", [])
            if str(category).strip()
        ],
        labels=[str(label).lower() for label in payload.get("labels", []) if str(label).strip()],
        limit=payload.get("limit"),
        include_no_activity=payload.get("include_no_activity", True),
    )
    return {"events": events or []}


async def _execute_get_llmvision_events(
    hass: HomeAssistant,
    arguments: dict[str, Any],
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Query events from the LLM Vision timeline integration."""
    cfg = config or {}
    if not cfg.get(CONF_LLMVISION_TIMELINE_ENABLED, False):
        return {"error": "LLM Vision timeline integration is disabled in Jeeves options."}
    if not _llmvision_detected(hass):
        return {"error": "LLM Vision is not detected in Home Assistant."}

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
    cameras = [camera.lower() for camera in cameras]
    categories = _normalize_list(arguments.get("categories"))
    if not categories:
        categories = _normalize_list(cfg.get(CONF_LLMVISION_CATEGORIES, []))
    categories = [category.lower() for category in categories]

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

    used_source = "service" if _llmvision_get_events_available(hass) else "compatibility"
    try:
        if used_source == "service":
            response = await hass.services.async_call(
                "llmvision",
                "get_events",
                payload,
                blocking=True,
                return_response=True,
            )
        else:
            response = await _query_llmvision_events_with_timeline(hass, payload)
    except Exception as err:
        if used_source == "service":
            return {"error": f"Failed to query llmvision.get_events: {err}"}
        return {"error": f"Failed to query LLM Vision timeline compatibility backend: {err}"}

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
        "source": used_source,
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
    hass: HomeAssistant, store: DataStore, function_name: str, arguments: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a rich actionable notification with Coming/Not Available buttons."""
    from .const import CONF_CAMERA_ENTITY, DOMAIN  # noqa: PLC0415

    target = None
    for t in store.notification_targets:
        if f"notify_{_slugify(t.name)}" == function_name:
            target = t
            break

    if not target:
        return {"error": f"Unknown notification target: {function_name}"}

    message = arguments.get("message", "Someone is at the door")
    title = arguments.get("title", "🔔 Doorbell Jeeves")

    # Get the NotificationManager from the session manager
    entry_id = (config or {}).get("_entry_id", "")
    manager = None
    if entry_id and DOMAIN in hass.data:
        manager = hass.data[DOMAIN].get(entry_id)
    if not manager and DOMAIN in hass.data:
        for m in hass.data[DOMAIN].values():
            if hasattr(m, "_notification_manager"):
                manager = m
                break

    # Get camera snapshot for the notification image
    camera_entity = (config or {}).get(CONF_CAMERA_ENTITY, "")
    snapshot_url = ""
    if camera_entity:
        snapshot_url = f"/api/camera_proxy/{camera_entity}"
    _LOGGER.warning(
        "Notification image: camera_entity=%s, snapshot_url=%s",
        camera_entity, snapshot_url,
    )

    if manager and hasattr(manager, "_notification_manager"):
        notification_mgr = manager._notification_manager

        # Build on_coming callback to inform the AI
        async def _on_coming() -> None:
            if hasattr(manager, "_client") and manager._client and manager._client.connected:
                await manager._client.inject_context(
                    "[SYSTEM] The homeowner pressed 'Coming' — they are on their way to the door. "
                    "Inform the visitor that someone is coming to meet them shortly.",
                    turn_complete=True,
                )

        # Build on_not_available callback to inform the AI
        async def _on_not_available() -> None:
            if hasattr(manager, "_client") and manager._client and manager._client.connected:
                await manager._client.inject_context(
                    "[SYSTEM] The homeowner pressed 'Not Available' — nobody is coming to the door. "
                    "Handle the visitor yourself: take a message, offer to save event details, "
                    "or politely inform them nobody is available right now.",
                    turn_complete=True,
                )

        # Generate a session_id for tracking
        session_id = entry_id or "default"

        targets_list = [{"service": target.service, "name": target.name}]
        await notification_mgr.send_doorbell_notification(
            targets=targets_list,
            session_id=session_id,
            snapshot_b64="",
            on_coming=_on_coming,
            on_not_available=_on_not_available,
        )

        # Override the notification message with the AI's description
        # Re-send with correct message and image URL
        if "." in target.service:
            domain, svc = target.service.split(".", 1)
        else:
            domain, svc = "notify", target.service

        from .notifications import ACTION_COMING, ACTION_NOT_AVAILABLE  # noqa: PLC0415

        data: dict[str, Any] = {
            "message": message,
            "title": title,
            "data": {
                "actions": [
                    {"action": ACTION_COMING, "title": "🚶 I'm coming"},
                    {"action": ACTION_NOT_AVAILABLE, "title": "❌ Not available"},
                ],
                "push": {"interruption-level": "time-sensitive"},
                "tag": f"jeeves_doorbell_{session_id}",
            },
        }
        if snapshot_url:
            data["data"]["image"] = snapshot_url

        await hass.services.async_call(domain, svc, data, blocking=True)

        return {
            "success": True,
            "message": (
                f"Notification sent to {target.name} with action buttons. "
                f"Do NOT announce this to the visitor — do NOT say 'I notified the owner' or similar. "
                f"Simply continue the conversation naturally. The owner will respond via buttons if available."
            ),
        }

    # Fallback: no notification manager — send plain notification with buttons
    if "." in target.service:
        domain, svc = target.service.split(".", 1)
    else:
        domain, svc = "notify", target.service

    from .notifications import ACTION_COMING, ACTION_NOT_AVAILABLE  # noqa: PLC0415

    data = {
        "message": message,
        "title": title,
        "data": {
            "actions": [
                {"action": ACTION_COMING, "title": "🚶 I'm coming"},
                {"action": ACTION_NOT_AVAILABLE, "title": "❌ Not available"},
            ],
            "push": {"interruption-level": "time-sensitive"},
        },
    }
    if snapshot_url:
        data["data"]["image"] = snapshot_url

    await hass.services.async_call(domain, svc, data, blocking=True)
    return {"success": True, "message": f"Notification sent to {target.name} with action buttons."}


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


# ─── New Tool Implementations ────────────────────────────────────────────────


async def _execute_save_event(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save an important event and notify homeowners."""
    from .events import EventStore, ImportantEvent  # noqa: PLC0415
    from .const import DOMAIN  # noqa: PLC0415

    title = arguments.get("title", "Untitled Event")
    description = arguments.get("description", "")
    severity = arguments.get("severity", "info")

    # Get event store from hass.data
    entry_id = (config or {}).get("_entry_id", "")
    event_store: EventStore | None = None
    if entry_id and DOMAIN in hass.data:
        manager = hass.data[DOMAIN].get(entry_id)
        if manager and hasattr(manager, "_event_store"):
            event_store = manager._event_store
    if not event_store and DOMAIN in hass.data:
        for manager in hass.data[DOMAIN].values():
            if manager and hasattr(manager, "_event_store"):
                event_store = manager._event_store
                break

    if not event_store:
        return {"error": "Event store not available"}

    try:
        event = ImportantEvent(
            id="",
            timestamp=0.0,
            title=title,
            description=description,
            severity=severity,
        )
        event_id = await event_store.async_add_event(event)

        # Send notification to all configured notification targets
        for target in store.notification_targets:
            try:
                service_parts = target.service.split(".", 1)
                if len(service_parts) == 2:
                    await hass.services.async_call(
                        service_parts[0], service_parts[1],
                        {
                            "message": f"📋 {title}\n{description[:200]}",
                            "title": f"🔔 Jeeves: {severity.upper()} Event",
                            "data": {"push": {"interruption-level": "time-sensitive" if severity == "urgent" else "active"}},
                        },
                        blocking=False,
                    )
            except Exception:
                _LOGGER.debug("Failed to notify %s about event", target.service)

        return {
            "success": True,
            "event_id": event_id,
            "message": f"Event saved and homeowners notified: '{title}'",
        }
    except Exception as err:
        _LOGGER.exception("Failed to save important event")
        return {
            "success": False,
            "error": f"Failed to save event to database: {err}"
        }


async def _execute_attach_event_photo(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach a camera snapshot to the most recent event."""
    from .events import EventStore  # noqa: PLC0415
    from .const import DOMAIN  # noqa: PLC0415

    camera_id = arguments.get("camera_entity_id", "")

    # Validate camera access
    allowed = [e.entity_id for e in store.managed_entities if e.entity_id.startswith("camera.")]
    if camera_id not in allowed:
        return {"error": f"Camera '{camera_id}' not in managed entities."}

    # Get event store
    entry_id = (config or {}).get("_entry_id", "")
    event_store: EventStore | None = None
    if entry_id and DOMAIN in hass.data:
        manager = hass.data[DOMAIN].get(entry_id)
        if manager and hasattr(manager, "_event_store"):
            event_store = manager._event_store
    if not event_store and DOMAIN in hass.data:
        for manager in hass.data[DOMAIN].values():
            if manager and hasattr(manager, "_event_store"):
                event_store = manager._event_store
                break

    if not event_store or not event_store.events:
        return {"error": "No events to attach photo to"}

    # Get snapshot
    try:
        image = await ha_camera_get_image(hass, camera_id, timeout=10)
        if not image or not image.content:
            return {"error": f"Could not get image from {camera_id}"}

        from .frame_processor import process_frame  # noqa: PLC0415
        processed = await hass.async_add_executor_job(
            process_frame, image.content, 1024, 768, 80
        )
        photo_b64 = base64.b64encode(processed).decode("ascii")
    except Exception as err:
        return {"error": f"Failed to capture photo: {err}"}

    # Attach to most recent event
    latest_event = event_store.events[-1]
    success = await event_store.async_attach_photo(latest_event.id, photo_b64)

    if success:
        return {
            "success": True,
            "message": f"Photo attached to event '{latest_event.title}'",
            "event_id": latest_event.id,
        }
    return {"error": "Failed to attach photo"}


def _execute_check_availability(
    hass: HomeAssistant, config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check if owners responded to doorbell notification."""
    from .const import DOMAIN  # noqa: PLC0415

    entry_id = (config or {}).get("_entry_id", "")
    if entry_id and DOMAIN in hass.data:
        manager = hass.data[DOMAIN].get(entry_id)
        if manager and hasattr(manager, "_notification_manager"):
            status = manager._notification_manager.get_availability_status()
            return {"status": status}

    return {"status": "Notification system not configured or no notification sent yet."}


def _execute_extend_session(
    arguments: dict[str, Any], config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extend session timeout — returns instruction for session manager."""
    try:
        extra = int(arguments.get("extra_seconds", 60))
    except (TypeError, ValueError):
        extra = 60
    reason = arguments.get("reason", "")

    # Clamp to reasonable range
    extra = max(30, min(300, extra))

    return {
        "success": True,
        "_extend_session_seconds": extra,  # Special key: session manager processes
        "message": f"Session extended by {extra} seconds. Reason: {reason}",
    }


async def _execute_recall_memories(
    hass: HomeAssistant, store: DataStore, arguments: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Search past session memories."""
    from .const import DOMAIN  # noqa: PLC0415

    query = arguments.get("query", "")
    try:
        hours_back = int(arguments.get("hours_back", 72))
    except (TypeError, ValueError):
        hours_back = 72
    hours_back = max(1, min(hours_back, 720))

    entry_id = (config or {}).get("_entry_id", "")
    if entry_id and DOMAIN in hass.data:
        manager = hass.data[DOMAIN].get(entry_id)
        if manager and hasattr(manager, "_memory_store"):
            memory_store = manager._memory_store
            if query:
                results = memory_store.search_memories(query)
            else:
                results = memory_store.get_recent_memories(hours_back)

            if not results:
                return {"message": "No matching memories found."}

            summaries = []
            for mem in results[-5:]:
                import datetime  # noqa: PLC0415
                dt = datetime.datetime.fromtimestamp(mem.timestamp)
                summaries.append(
                    f"[{dt.strftime('%Y-%m-%d %H:%M')}] "
                    f"{mem.visitor_name or 'Unknown'}: {mem.summary[:200]}"
                )
            return {"memories": summaries, "count": len(results)}

    return {"message": "Memory system not available."}
