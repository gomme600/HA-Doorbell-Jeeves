"""Data models for Doorbell Jeeves."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActionPolicy:
    """Security policy for a single action — fully configurable per action."""

    security_mode: str = "auto"
    require_visual_match: bool = False
    require_camera_feed: bool = False
    max_per_session: int = 0  # 0 = unlimited
    cooldown_seconds: float = 0.0
    validator_prompt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "security_mode": self.security_mode,
            "require_visual_match": self.require_visual_match,
            "require_camera_feed": self.require_camera_feed,
            "max_per_session": self.max_per_session,
            "cooldown_seconds": self.cooldown_seconds,
            "validator_prompt": self.validator_prompt,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionPolicy:
        return cls(
            security_mode=data.get("security_mode", "auto"),
            require_visual_match=data.get("require_visual_match", False),
            require_camera_feed=data.get("require_camera_feed", False),
            max_per_session=data.get("max_per_session", 0),
            cooldown_seconds=data.get("cooldown_seconds", 0.0),
            validator_prompt=data.get("validator_prompt", ""),
        )


@dataclass
class KnownFace:
    """A reference identity (person or animal)."""

    name: str
    relationship: str
    description: str
    image_base64: str | None = None
    access_level: str = "guest"
    allowed_actions: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relationship": self.relationship,
            "description": self.description,
            "image_base64": self.image_base64,
            "access_level": self.access_level,
            "allowed_actions": self.allowed_actions,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KnownFace:
        return cls(
            name=data["name"],
            relationship=data.get("relationship", "guest"),
            description=data.get("description", ""),
            image_base64=data.get("image_base64"),
            access_level=data.get("access_level", "guest"),
            allowed_actions=data.get("allowed_actions", []),
            notes=data.get("notes", ""),
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
