from __future__ import annotations

from custom_components.ha_doorbell_jeeves.models import (
    AudioFile,
    CameraPlacement,
    EntityAction,
    KnownIdentity,
    ManagedEntity,
    TaskInstruction,
)


def test_task_instruction_round_trip() -> None:
    instruction = TaskInstruction(title="Deliveries", text="Always ask for package ID.")
    restored = TaskInstruction.from_dict(instruction.to_dict())
    assert restored == instruction


def test_entity_action_round_trip_keeps_security_fields() -> None:
    action = EntityAction(
        id="unlock_gate",
        name="Unlock Gate",
        description="Unlock the front gate",
        service="lock.unlock",
        service_data={"code": "1234"},
        steps=[{"action": "lock.unlock", "target": {"entity_id": "lock.front_gate"}}],
        security_mode="pin_and_validated",
        require_visual_match=True,
        require_camera_feed=True,
        max_per_session=1,
        cooldown_seconds=10.0,
        validator_prompt="Require courier badge.",
    )

    restored = EntityAction.from_dict(action.to_dict())
    assert restored == action


def test_managed_entity_round_trip_restores_actions() -> None:
    entity = ManagedEntity(
        entity_id="light.porch",
        name="Porch Light",
        description="Main porch light",
        actions=[
            EntityAction(
                id="toggle_porch",
                name="Toggle Porch",
                description="Toggle porch light",
                service="light.toggle",
            )
        ],
        security_mode="auto",
    )
    restored = ManagedEntity.from_dict(entity.to_dict())
    assert restored.entity_id == "light.porch"
    assert restored.actions[0].id == "toggle_porch"
    assert restored.actions[0].service == "light.toggle"


def test_known_identity_defaults_from_dict() -> None:
    restored = KnownIdentity.from_dict({"name": "Alex"})
    assert restored.name == "Alex"
    assert restored.identity_type == "person"
    assert restored.relationship == "guest"
    assert restored.access_level == "guest"
    assert restored.allowed_action_ids == []


def test_camera_placement_migrates_legacy_side_offset() -> None:
    placement = CameraPlacement.from_dict(
        {
            "entity_id": "camera.backyard",
            "name": "Backyard Camera",
            "side": "west",
            "offset": 0.2,
        }
    )
    assert placement.x == 0.1
    assert placement.y == 0.2
    assert placement.rotation == 90.0


def test_camera_placement_facing_direction_and_ptz_flag() -> None:
    placement = CameraPlacement(
        entity_id="camera.driveway",
        name="Driveway",
        x=0.7,
        y=0.2,
        rotation=90,
        ptz_left="button.driveway_left",
    )
    assert placement.has_ptz is True
    assert placement.facing_direction == "east"


def test_audio_file_round_trip() -> None:
    audio = AudioFile(
        id="doorbell_chime",
        name="Doorbell Chime",
        description="Classic ding-dong",
        media_id="media-source://media_source/local/chime.mp3",
        media_type="music",
        category="General",
    )
    restored = AudioFile.from_dict(audio.to_dict())
    assert restored == audio

