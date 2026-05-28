"""Persistent storage manager for managed entities, actions, and identities."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_TASK_INSTRUCTIONS,
    STORAGE_KEY_ENTITIES,
    STORAGE_KEY_IDENTITIES,
    STORAGE_VERSION,
)
from .models import (
    CameraPlacement,
    KnownIdentity,
    ManagedEntity,
    NotificationTarget,
    StartTrigger,
    TaskInstruction,
)

_LOGGER = logging.getLogger(__name__)


class DataStore:
    """Persistent data store for complex config that doesn't fit in config entries.

    Manages:
      - Managed entities (with their custom actions)
      - Notification targets
      - Start triggers
      - Task instructions
      - Known identities (with reference images)
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._entity_store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_ENTITIES}_{entry_id}")
        self._identity_store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_IDENTITIES}_{entry_id}")

        self.managed_entities: list[ManagedEntity] = []
        self.notification_targets: list[NotificationTarget] = []
        self.start_triggers: list[StartTrigger] = []
        self.task_instructions: list[TaskInstruction] = []
        self.camera_placements: list[CameraPlacement] = []
        self.known_identities: list[KnownIdentity] = []

    async def async_load(self) -> None:
        """Load all data from persistent storage."""
        # Entities + actions + notifications + triggers
        entity_data = await self._entity_store.async_load()
        if entity_data and isinstance(entity_data, dict):
            self.managed_entities = [
                ManagedEntity.from_dict(e) for e in entity_data.get("entities", [])
            ]
            self.notification_targets = [
                NotificationTarget.from_dict(n) for n in entity_data.get("notifications", [])
            ]
            self.start_triggers = [
                StartTrigger.from_dict(t) for t in entity_data.get("start_triggers", [])
            ]
            self.task_instructions = [
                TaskInstruction.from_dict(t)
                for t in entity_data.get(CONF_TASK_INSTRUCTIONS, [])
            ]
            self.camera_placements = [
                CameraPlacement.from_dict(c)
                for c in entity_data.get("camera_placements", [])
            ]

        # Identities
        id_data = await self._identity_store.async_load()
        if id_data and isinstance(id_data, dict):
            self.known_identities = [
                KnownIdentity.from_dict(i) for i in id_data.get("identities", [])
            ]

        _LOGGER.debug(
            "Loaded: %d entities, %d notifications, %d triggers, %d task instructions, %d cameras, %d identities",
            len(self.managed_entities),
            len(self.notification_targets),
            len(self.start_triggers),
            len(self.task_instructions),
            len(self.camera_placements),
            len(self.known_identities),
        )

    async def async_save_entities(self) -> None:
        """Persist entities, notifications, and triggers."""
        data = {
            "entities": [e.to_dict() for e in self.managed_entities],
            "notifications": [n.to_dict() for n in self.notification_targets],
            "start_triggers": [t.to_dict() for t in self.start_triggers],
            CONF_TASK_INSTRUCTIONS: [t.to_dict() for t in self.task_instructions],
            "camera_placements": [c.to_dict() for c in self.camera_placements],
        }
        await self._entity_store.async_save(data)

    async def async_save_identities(self) -> None:
        """Persist known identities."""
        data = {"identities": [i.to_dict() for i in self.known_identities]}
        await self._identity_store.async_save(data)

    # ─── Entity Management ────────────────────────────────────────────────────

    def get_entity(self, entity_id: str) -> ManagedEntity | None:
        for e in self.managed_entities:
            if e.entity_id == entity_id:
                return e
        return None

    async def async_add_entity(self, entity: ManagedEntity) -> None:
        """Add or update a managed entity."""
        for i, existing in enumerate(self.managed_entities):
            if existing.entity_id == entity.entity_id:
                self.managed_entities[i] = entity
                await self.async_save_entities()
                return
        self.managed_entities.append(entity)
        await self.async_save_entities()

    async def async_remove_entity(self, entity_id: str) -> bool:
        for i, e in enumerate(self.managed_entities):
            if e.entity_id == entity_id:
                self.managed_entities.pop(i)
                await self.async_save_entities()
                return True
        return False

    # ─── Action Management ────────────────────────────────────────────────────

    def get_action(self, action_id: str) -> tuple[ManagedEntity | None, Any | None]:
        """Find an action by ID across all entities."""
        from .models import EntityAction  # noqa: PLC0415
        for entity in self.managed_entities:
            for action in entity.actions:
                if action.id == action_id:
                    return entity, action
        return None, None

    # ─── Notification Management ──────────────────────────────────────────────

    async def async_add_notification(self, target: NotificationTarget) -> None:
        for i, existing in enumerate(self.notification_targets):
            if existing.service == target.service:
                self.notification_targets[i] = target
                await self.async_save_entities()
                return
        self.notification_targets.append(target)
        await self.async_save_entities()

    async def async_remove_notification(self, service: str) -> bool:
        for i, n in enumerate(self.notification_targets):
            if n.service == service:
                self.notification_targets.pop(i)
                await self.async_save_entities()
                return True
        return False

    # ─── Trigger Management ───────────────────────────────────────────────────

    async def async_set_start_triggers(self, triggers: list[StartTrigger]) -> None:
        self.start_triggers = triggers
        await self.async_save_entities()

    # ─── Identity Management ──────────────────────────────────────────────────

    def get_identity(self, name: str) -> KnownIdentity | None:
        for i in self.known_identities:
            if i.name.lower() == name.lower():
                return i
        return None

    async def async_add_identity(self, identity: KnownIdentity) -> None:
        for i, existing in enumerate(self.known_identities):
            if existing.name.lower() == identity.name.lower():
                self.known_identities[i] = identity
                await self.async_save_identities()
                return
        self.known_identities.append(identity)
        await self.async_save_identities()

    async def async_remove_identity(self, name: str) -> bool:
        for i, ident in enumerate(self.known_identities):
            if ident.name.lower() == name.lower():
                self.known_identities.pop(i)
                await self.async_save_identities()
                return True
        return False
