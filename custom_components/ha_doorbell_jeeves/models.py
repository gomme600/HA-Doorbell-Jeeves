"""Data models for Doorbell Jeeves v2 – entity-centric architecture."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskInstruction:
    """A titled instruction block appended to the system prompt."""

    title: str
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "text": self.text}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskInstruction":
        return cls(title=data.get("title", ""), text=data.get("text", ""))


@dataclass
class EntityAction:
    """A custom action that the AI can perform on a managed entity.

    Each action is independently configurable with its own security policy.
    """

    id: str  # Unique slug (e.g., "unlock_front_door")
    name: str  # Human-readable (e.g., "Unlock the front door")
    description: str  # Sent to the AI as the tool description
    service: str  # HA service to call (e.g., "lock.unlock")
    service_data: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)

    # Per-action security policy
    security_mode: str = "auto"
    require_visual_match: bool = False
    require_camera_feed: bool = False
    max_per_session: int = 0  # 0 = unlimited
    cooldown_seconds: float = 0.0
    validator_prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "service": self.service,
            "service_data": self.service_data,
            "steps": self.steps,
            "security_mode": self.security_mode,
            "require_visual_match": self.require_visual_match,
            "require_camera_feed": self.require_camera_feed,
            "max_per_session": self.max_per_session,
            "cooldown_seconds": self.cooldown_seconds,
            "validator_prompt": self.validator_prompt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EntityAction:
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            service=data["service"],
            service_data=data.get("service_data", {}),
            steps=data.get("steps", []),
            security_mode=data.get("security_mode", "auto"),
            require_visual_match=data.get("require_visual_match", False),
            require_camera_feed=data.get("require_camera_feed", False),
            max_per_session=data.get("max_per_session", 0),
            cooldown_seconds=data.get("cooldown_seconds", 0.0),
            validator_prompt=data.get("validator_prompt", ""),
        )


@dataclass
class ManagedEntity:
    """An entity exposed to the AI agent with optional custom actions.

    Entities without actions are read-only (AI can see their state).
    Entities with actions can be controlled by the AI.
    """

    entity_id: str
    name: str  # Display name for the AI (e.g., "Porch Light")
    description: str  # Tells the AI what this entity is/does
    actions: list[EntityAction] = field(default_factory=list)

    # Default security for this entity (applies if entity has no per-action override)
    security_mode: str = "auto"
    require_visual_match: bool = False
    require_camera_feed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "description": self.description,
            "actions": [a.to_dict() for a in self.actions],
            "security_mode": self.security_mode,
            "require_visual_match": self.require_visual_match,
            "require_camera_feed": self.require_camera_feed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManagedEntity:
        return cls(
            entity_id=data["entity_id"],
            name=data["name"],
            description=data.get("description", ""),
            actions=[EntityAction.from_dict(a) for a in data.get("actions", [])],
            security_mode=data.get("security_mode", "auto"),
            require_visual_match=data.get("require_visual_match", False),
            require_camera_feed=data.get("require_camera_feed", False),
        )


@dataclass
class NotificationTarget:
    """A notification service the AI can use to alert the homeowner."""

    service: str  # e.g., "notify.mobile_app_oneplus13"
    name: str  # Display name (e.g., "Admin Phone")
    description: str  # e.g., "Send push notification to the homeowner"

    def to_dict(self) -> dict[str, Any]:
        return {"service": self.service, "name": self.name, "description": self.description}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotificationTarget:
        return cls(
            service=data["service"],
            name=data["name"],
            description=data.get("description", ""),
        )


@dataclass
class StartTrigger:
    """Condition that automatically starts the AI concierge session."""

    entity_id: str
    to_state: str = "on"  # Start session when entity reaches this state
    from_state: str = ""  # Optional: only trigger if coming from this state
    restart_session: bool = False  # If True, restart even if session is active

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "to_state": self.to_state,
            "from_state": self.from_state,
            "restart_session": self.restart_session,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StartTrigger:
        return cls(
            entity_id=data["entity_id"],
            to_state=data.get("to_state", "on"),
            from_state=data.get("from_state", ""),
            restart_session=data.get("restart_session", False),
        )


@dataclass
class KnownIdentity:
    """A reference identity (person, animal, or object) the AI should recognize."""

    name: str
    identity_type: str  # "person", "animal", "object"
    relationship: str  # e.g., "owner", "family", "friend", "pet", "delivery"
    description: str  # Physical description for visual matching
    access_level: str = "guest"  # "full", "limited", "guest", "none"
    allowed_action_ids: list[str] = field(default_factory=list)
    image_base64: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "identity_type": self.identity_type,
            "relationship": self.relationship,
            "description": self.description,
            "access_level": self.access_level,
            "allowed_action_ids": self.allowed_action_ids,
            "image_base64": self.image_base64,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnownIdentity:
        return cls(
            name=data["name"],
            identity_type=data.get("identity_type", "person"),
            relationship=data.get("relationship", "guest"),
            description=data.get("description", ""),
            access_level=data.get("access_level", "guest"),
            allowed_action_ids=data.get("allowed_action_ids", []),
            image_base64=data.get("image_base64"),
            notes=data.get("notes", ""),
        )


@dataclass
class CameraPlacement:
    """Camera placement on the property map for spatial awareness.

    Position uses free placement:
    - x: 0.0 to 1.0 (horizontal position on map, 0=left, 1=right)
    - y: 0.0 to 1.0 (vertical position on map, 0=top, 1=bottom)
    - rotation: 0 to 360 degrees (direction camera faces, 0=up/north)
    """

    entity_id: str
    name: str
    x: float = 0.5  # horizontal position (0-1)
    y: float = 0.5  # vertical position (0-1)
    rotation: float = 0.0  # degrees, 0=north, 90=east, 180=south, 270=west
    side: str = ""  # legacy, kept for backward compat
    offset: float = 0.5  # legacy
    facing: str = ""  # legacy
    area_description: str = ""  # e.g., "Front garden, driveway, mailbox"
    is_doorbell: bool = False
    has_audio: bool = False  # 2-way audio capable (mic + speaker)
    audio_method: str = ""  # Cached best audio input method (go2rtc_rtsp, rtsp, etc.)
    audio_url: str = ""  # Cached audio stream URL
    # PTZ capabilities (entity IDs for PTZ controls)
    ptz_up: str = ""
    ptz_down: str = ""
    ptz_left: str = ""
    ptz_right: str = ""
    ptz_return_to_monitor: str = ""  # Service/script to return to home position

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "rotation": self.rotation,
            "side": self.side,
            "offset": self.offset,
            "facing": self.facing,
            "area_description": self.area_description,
            "is_doorbell": self.is_doorbell,
            "has_audio": self.has_audio,
            "audio_method": self.audio_method,
            "audio_url": self.audio_url,
            "ptz_up": self.ptz_up,
            "ptz_down": self.ptz_down,
            "ptz_left": self.ptz_left,
            "ptz_right": self.ptz_right,
            "ptz_return_to_monitor": self.ptz_return_to_monitor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CameraPlacement":
        # Migrate legacy side/offset to x/y if needed
        x = data.get("x")
        y = data.get("y")
        rotation = data.get("rotation", 0.0)
        if x is None or y is None:
            # Convert old side/offset format to x/y
            side = data.get("side", "north")
            offset = data.get("offset", 0.5)
            if side == "north":
                x, y = offset, 0.1
                rotation = 180.0
            elif side == "south":
                x, y = offset, 0.9
                rotation = 0.0
            elif side == "west":
                x, y = 0.1, offset
                rotation = 90.0
            else:  # east
                x, y = 0.9, offset
                rotation = 270.0
        return cls(
            entity_id=data["entity_id"],
            name=data.get("name", ""),
            x=float(x),
            y=float(y),
            rotation=float(rotation),
            side=data.get("side", ""),
            offset=data.get("offset", 0.5),
            facing=data.get("facing", ""),
            area_description=data.get("area_description", ""),
            is_doorbell=data.get("is_doorbell", False),
            has_audio=data.get("has_audio", False),
            audio_method=data.get("audio_method", ""),
            audio_url=data.get("audio_url", ""),
            ptz_up=data.get("ptz_up", ""),
            ptz_down=data.get("ptz_down", ""),
            ptz_left=data.get("ptz_left", ""),
            ptz_right=data.get("ptz_right", ""),
            ptz_return_to_monitor=data.get("ptz_return_to_monitor", ""),
        )

    @property
    def has_ptz(self) -> bool:
        """Check if this camera has any PTZ controls configured."""
        return bool(self.ptz_up or self.ptz_down or self.ptz_left or self.ptz_right)

    @property
    def facing_direction(self) -> str:
        """Get human-readable facing direction from rotation angle."""
        dirs = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
        idx = round(self.rotation / 45) % 8
        return dirs[idx]


@dataclass
class AudioFile:
    """An audio file the agent can play over speakers/cameras.

    media_id: HA media content ID (e.g. media-source://media_source/local/halloween/scream.mp3)
              or a URL (http://...) or a local path (/media/sounds/doorbell.mp3)
    """

    id: str  # unique slug (e.g. "halloween-scream")
    name: str  # friendly name shown to agent (e.g. "Scary Scream")
    description: str = ""  # context for agent (e.g. "A loud scream, good for scaring visitors")
    media_id: str = ""  # HA media content ID or URL
    media_type: str = "music"  # HA media content type (music, sound, etc.)
    category: str = ""  # optional grouping (e.g. "Halloween", "Alerts")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "media_id": self.media_id,
            "media_type": self.media_type,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AudioFile":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            media_id=data.get("media_id", ""),
            media_type=data.get("media_type", "music"),
            category=data.get("category", ""),
        )


@dataclass
class AuditEntry:
    """Immutable audit log entry."""

    timestamp: str
    event_type: str
    action: str
    details: dict[str, Any] = field(default_factory=dict)
    approved: bool | None = None


@dataclass
class ValidatorDecision:
    """Result from the security validator agent."""

    action: str
    approved: bool
    confidence: float
    reasoning: str
    visual_match: bool | None = None
    threat_indicators: list[str] = field(default_factory=list)
