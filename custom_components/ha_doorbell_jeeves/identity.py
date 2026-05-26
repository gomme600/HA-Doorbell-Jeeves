"""Identity management – known faces, animals, and reference images."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_FACE_SENSOR_ENTITY,
    CONF_IDENTITY_MODE,
    DOMAIN,
    IDENTITY_MODE_BOTH,
    IDENTITY_MODE_NONE,
    IDENTITY_MODE_REFERENCE_IMAGES,
    IDENTITY_MODE_SENSOR,
)
from .models import KnownFace

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}_known_faces"
STORAGE_VERSION = 1


class IdentityManager:
    """Manages known faces/animals and builds identity context for the AI.

    Supports two modes:
      1. Sensor-based: Reads face names from a sensor entity (Frigate, CompreFace)
      2. Reference images: Sends pre-uploaded photos to the AI with descriptions
      3. Both: Combines sensor data with reference image context

    Reference images are stored persistently in HA's .storage directory.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, config: dict[str, Any]) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._config = config
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry_id}")
        self._known_faces: list[KnownFace] = []
        self._loaded = False

    async def async_load(self) -> None:
        """Load known faces from persistent storage."""
        data = await self._store.async_load()
        if data and isinstance(data, dict):
            faces_raw = data.get("faces", [])
            self._known_faces = [KnownFace.from_dict(f) for f in faces_raw]
        else:
            self._known_faces = []
        self._loaded = True
        _LOGGER.debug("Loaded %d known faces from storage", len(self._known_faces))

    async def async_save(self) -> None:
        """Persist known faces to storage."""
        data = {"faces": [f.to_dict() for f in self._known_faces]}
        await self._store.async_save(data)

    @property
    def known_faces(self) -> list[KnownFace]:
        """Return the list of known faces."""
        return list(self._known_faces)

    async def async_add_face(self, face: KnownFace) -> None:
        """Add or update a known face."""
        # Update existing by name
        for i, existing in enumerate(self._known_faces):
            if existing.name.lower() == face.name.lower():
                self._known_faces[i] = face
                await self.async_save()
                return
        self._known_faces.append(face)
        await self.async_save()

    async def async_remove_face(self, name: str) -> bool:
        """Remove a known face by name. Returns True if found and removed."""
        for i, face in enumerate(self._known_faces):
            if face.name.lower() == name.lower():
                self._known_faces.pop(i)
                await self.async_save()
                return True
        return False

    def get_face_by_name(self, name: str) -> KnownFace | None:
        """Look up a face by name (case-insensitive)."""
        for face in self._known_faces:
            if face.name.lower() == name.lower():
                return face
        return None

    async def build_identity_context(self) -> str:
        """Build the identity context string to inject into the system prompt.

        This is called at session start and provides the AI with knowledge
        about who it might encounter.
        """
        mode = self._config.get(CONF_IDENTITY_MODE, IDENTITY_MODE_NONE)
        parts: list[str] = []

        if mode in (IDENTITY_MODE_SENSOR, IDENTITY_MODE_BOTH):
            sensor_context = await self._build_sensor_context()
            if sensor_context:
                parts.append(sensor_context)

        if mode in (IDENTITY_MODE_REFERENCE_IMAGES, IDENTITY_MODE_BOTH):
            reference_context = self._build_reference_context()
            if reference_context:
                parts.append(reference_context)

        if not parts:
            return ""

        return "\n\n".join(parts)

    async def _build_sensor_context(self) -> str:
        """Read identity from a face recognition sensor."""
        sensor_id = self._config.get(CONF_FACE_SENSOR_ENTITY)
        if not sensor_id:
            return ""

        state = self._hass.states.get(sensor_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return "[IDENTITY SENSOR: No visitor currently recognized by face detection system.]"

        visitor_name = state.state
        attrs = state.attributes
        confidence = attrs.get("confidence", "N/A")
        extra = attrs.get("description", "")

        context = (
            f"[IDENTITY SENSOR: Face detection system has identified the current "
            f"visitor as '{visitor_name}' (confidence: {confidence})."
        )
        if extra:
            context += f" Additional info: {extra}"
        context += "]"

        # Cross-reference with known faces for access level
        known = self.get_face_by_name(visitor_name)
        if known:
            context += (
                f"\n[ACCESS PROFILE: {visitor_name} is registered as "
                f"'{known.relationship}' with access level '{known.access_level}'. "
                f"Allowed actions: {', '.join(known.allowed_actions) or 'none specified'}.]"
            )

        return context

    def _build_reference_context(self) -> str:
        """Build context from uploaded reference images."""
        if not self._known_faces:
            return ""

        lines = ["[KNOWN PERSONS/ANIMALS REGISTRY:]"]
        for face in self._known_faces:
            line = (
                f"- Name: {face.name} | Relationship: {face.relationship} | "
                f"Access: {face.access_level} | Description: {face.description}"
            )
            if face.allowed_actions:
                line += f" | Allowed actions: {', '.join(face.allowed_actions)}"
            lines.append(line)

        lines.append(
            "\n[INSTRUCTION: If you visually recognize any of these individuals in "
            "the camera feed, greet them by name. Only grant access-controlled actions "
            "to individuals whose visual appearance matches their registered profile AND "
            "who explicitly request the action.]"
        )

        return "\n".join(lines)

    def get_reference_images_for_session(self) -> list[dict[str, str]]:
        """Return reference images to inject at session start.

        These are sent as initial image content so the model has visual
        references to compare against the live camera feed.
        """
        mode = self._config.get(CONF_IDENTITY_MODE, IDENTITY_MODE_NONE)
        if mode not in (IDENTITY_MODE_REFERENCE_IMAGES, IDENTITY_MODE_BOTH):
            return []

        images = []
        for face in self._known_faces:
            if face.image_base64:
                images.append({
                    "name": face.name,
                    "image_base64": face.image_base64,
                    "caption": (
                        f"Reference photo of {face.name} ({face.relationship}). "
                        f"Description: {face.description}"
                    ),
                })
        return images
