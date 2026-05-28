"""Config flow for Doorbell Jeeves integration."""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    ActionSelector,
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    AUDIO_MANUAL_EXTERNAL_GO2RTC,
    AUDIO_MANUAL_HA_ENTITIES,
    AUDIO_MODE_MANUAL,
    AUDIO_MODE_REOLINK,
    AUDIO_OUTPUT_EVENT,
    AUDIO_OUTPUT_GO2RTC,
    AUDIO_OUTPUT_MEDIA_PLAYER,
    CAMERA_FACINGS,  # noqa: F401 - kept for backward compat
    CAMERA_SIDES,  # noqa: F401 - kept for backward compat
    CONF_API_BASE_URL,
    CONF_API_KEY,
    CONF_AUDIO_MANUAL_MODE,
    CONF_AUDIO_MODE,
    CONF_AUDIO_OUTPUT_MODE,
    CONF_CAMERA_ENTITY,
    CONF_CAMERA_PLACEMENTS,
    CONF_DEFAULT_SECURITY_MODE,
    CONF_DUAL_MODEL_ENABLED,
    CONF_FACE_SENSOR_ENTITY,
    CONF_FRAME_MAX_HEIGHT,
    CONF_FRAME_MAX_WIDTH,
    CONF_FRAME_QUALITY,
    CONF_GO2RTC_INPUT_STREAM_NAME,
    CONF_GO2RTC_OUTPUT_STREAM_NAME,
    CONF_GO2RTC_STREAM_NAME,
    CONF_IDENTITY_MODE,
    CONF_LLMVISION_CAMERAS,
    CONF_LLMVISION_CATEGORIES,
    CONF_LLMVISION_HOURS_BACK,
    CONF_LLMVISION_INCLUDE_NO_ACTIVITY,
    CONF_LLMVISION_MAX_EVENTS,
    CONF_LLMVISION_TIMELINE_ENABLED,
    CONF_MEDIA_PLAYER_ENTITY,
    CONF_MEMORY_RETENTION_DAYS,
    CONF_MICROPHONE_ENTITY,
    CONF_MODEL,
    CONF_PIN_CODE,
    CONF_PROVIDER,
    CONF_REOLINK_ENTRY_ID,
    CONF_REOLINK_MIC_METHOD,
    CONF_REOLINK_MIC_URL,
    CONF_SESSION_TIMEOUT,
    CONF_STOP_ENTITIES,
    CONF_SYSTEM_PROMPT,
    CONF_TASK_INSTRUCTIONS,
    CONF_TEXT_MODEL,
    CONF_TOOL_API_KEY,
    CONF_TOOL_BASE_URL,
    CONF_TOOL_MODEL,
    CONF_TOOL_PROVIDER,
    CONF_VALIDATOR_MODEL,
    CONF_VISION_FPS,
    CONF_VOICE,
    DEFAULT_AUDIO_MANUAL_MODE,
    DEFAULT_FRAME_MAX_HEIGHT,
    DEFAULT_FRAME_MAX_WIDTH,
    DEFAULT_FRAME_QUALITY,
    DEFAULT_LLMVISION_HOURS_BACK,
    DEFAULT_LLMVISION_INCLUDE_NO_ACTIVITY,
    DEFAULT_LLMVISION_MAX_EVENTS,
    DEFAULT_MEMORY_RETENTION_DAYS,
    DEFAULT_MODEL_GEMINI,
    DEFAULT_MODEL_OPENAI,
    DEFAULT_SESSION_TIMEOUT,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TEXT_MODEL_GEMINI,
    DEFAULT_TOOL_MODEL_GEMINI,
    DEFAULT_TOOL_MODEL_OPENAI,
    DEFAULT_VALIDATOR_MODEL,
    DEFAULT_VISION_FPS,
    DEFAULT_VOICE_GEMINI,
    DEFAULT_VOICE_OPENAI,
    DOMAIN,
    GLOBAL_ACTIONS_ENTITY_ID,
    GLOBAL_ACTIONS_ENTITY_NAME,
    IDENTITY_MODE_BOTH,
    IDENTITY_MODE_NONE,
    IDENTITY_MODE_REFERENCE_IMAGES,
    IDENTITY_MODE_SENSOR,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    SECURITY_MODE_AUTO,
    SECURITY_MODES,
)
from .models import CameraPlacement, EntityAction, ManagedEntity, NotificationTarget, TaskInstruction
from .store import DataStore

_LOGGER = logging.getLogger(__name__)
_ACTION_STEP_FIELDS = ["action_config", "step_2_config", "step_3_config", "step_4_config", "step_5_config"]
_SPEAKER_ENTITY_DOMAINS = ("media_player",)
_MICROPHONE_ENTITY_DOMAINS = ("assist_satellite", "media_player")


def _loaded_reolink_entries(hass: Any) -> list[ConfigEntry]:
    """Return loaded Reolink config entries."""
    return [
        entry
        for entry in hass.config_entries.async_entries("reolink")
        if entry.state == config_entries.ConfigEntryState.LOADED
    ]


def _reolink_entry_selector_options(hass: Any) -> list[dict[str, str]]:
    """Build selector options for available Reolink integrations."""
    registry = er.async_get(hass)
    options: list[dict[str, str]] = []
    for entry in _loaded_reolink_entries(hass):
        camera_count = sum(
            1
            for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
            if entity.domain == "camera" and not entity.disabled
        )
        options.append(
            {
                "value": entry.entry_id,
                "label": f"{entry.title} ({camera_count} camera{'s' if camera_count != 1 else ''})",
            }
        )
    return options


def _auto_detect_reolink_trigger_entity(hass: Any, reolink_entry_id: str) -> str | None:
    """Pick the best trigger entity for a Reolink config entry."""
    registry = er.async_get(hass)
    best_match: tuple[int, str] | None = None
    for entity in er.async_entries_for_config_entry(registry, reolink_entry_id):
        entity_id = entity.entity_id or ""
        if not entity_id or entity.disabled:
            continue
        score = 0
        lower = entity_id.lower()
        if "visitor" in lower:
            score += 4
        if "doorbell" in lower:
            score += 3
        if entity.domain == "button":
            score += 2
        if "button" in lower:
            score += 1
        if score == 0:
            continue
        if best_match is None or score > best_match[0]:
            best_match = (score, entity_id)
    return best_match[1] if best_match else None


def _clean_text(value: Any) -> str:
    """Return a stripped string or empty string."""
    return value.strip() if isinstance(value, str) else ""


def _add_optional_entity_selector(
    schema: dict[Any, Any],
    key: str,
    default_value: Any,
    selector_config: EntitySelectorConfig,
) -> None:
    """Add an optional entity selector, avoiding invalid empty defaults."""
    default_text = _clean_text(default_value)
    if default_text:
        schema[vol.Optional(key, default=default_text)] = EntitySelector(selector_config)
    else:
        schema[vol.Optional(key)] = EntitySelector(selector_config)


def _entity_exists(hass: Any, entity_id: str) -> bool:
    """Return True when entity exists in states or registry."""
    if hass.states.get(entity_id) is not None:
        return True
    return er.async_get(hass).async_get(entity_id) is not None


def _is_valid_entity_for_domains(hass: Any, entity_id: Any, allowed_domains: tuple[str, ...]) -> bool:
    """Validate entity id exists and belongs to one of the allowed domains."""
    entity = _clean_text(entity_id)
    if not entity or "." not in entity:
        return False
    domain = entity.split(".", 1)[0]
    if domain not in allowed_domains:
        return False
    return _entity_exists(hass, entity)


def _manual_stream_default(data: dict[str, Any], key: str) -> str:
    """Get manual stream default with legacy single-stream fallback."""
    explicit = _clean_text(data.get(key, ""))
    if explicit:
        return explicit
    return _clean_text(data.get(CONF_GO2RTC_STREAM_NAME, ""))


def _sync_manual_stream_fields(data: dict[str, Any]) -> None:
    """Backfill split go2rtc stream fields from legacy single-stream value."""
    legacy_stream = _clean_text(data.get(CONF_GO2RTC_STREAM_NAME, ""))
    input_stream = _clean_text(data.get(CONF_GO2RTC_INPUT_STREAM_NAME, ""))
    output_stream = _clean_text(data.get(CONF_GO2RTC_OUTPUT_STREAM_NAME, ""))

    if legacy_stream:
        if not input_stream:
            data[CONF_GO2RTC_INPUT_STREAM_NAME] = legacy_stream
        if not output_stream:
            data[CONF_GO2RTC_OUTPUT_STREAM_NAME] = legacy_stream

    output_for_legacy = _clean_text(data.get(CONF_GO2RTC_OUTPUT_STREAM_NAME, ""))
    if output_for_legacy:
        data[CONF_GO2RTC_STREAM_NAME] = output_for_legacy


async def _check_model_capabilities(
    hass: Any, provider: str, api_key: str, model_name: str
) -> dict[str, bool]:
    """Query the model's capabilities to determine what it supports.

    Returns dict with keys: supports_text_output, supports_tools, supports_audio_output.
    """
    caps: dict[str, bool] = {
        "supports_text_output": True,
        "supports_tools": True,
        "supports_audio_output": False,
    }
    if provider != PROVIDER_GEMINI or not api_key or not model_name:
        return caps

    try:
        from google import genai  # noqa: PLC0415

        def _get_model_info() -> dict[str, bool]:
            client = genai.Client(api_key=api_key)
            try:
                model_info = client.models.get(model=f"models/{model_name}")
            except Exception:
                try:
                    model_info = client.models.get(model=model_name)
                except Exception:
                    return caps  # Unknown model, assume full caps

            result = {
                "supports_text_output": True,
                "supports_tools": True,
                "supports_audio_output": False,
            }

            # For native audio dialog models, they output audio only (not text)
            if "native-audio" in model_name or "audio-dialog" in model_name:
                result["supports_audio_output"] = True
                result["supports_text_output"] = False

            return result

        return await hass.async_add_executor_job(_get_model_info)
    except Exception as err:
        _LOGGER.debug("Model capability check failed: %s", err)
    return caps


class DoorbellJeevesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle initial setup of Doorbell Jeeves."""

    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DoorbellJeevesOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._data[CONF_AUDIO_MODE] = user_input[CONF_AUDIO_MODE]
            if user_input[CONF_AUDIO_MODE] == AUDIO_MODE_REOLINK:
                return await self.async_step_reolink()
            return await self.async_step_manual_audio()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AUDIO_MODE, default=AUDIO_MODE_REOLINK): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": AUDIO_MODE_REOLINK, "label": "Reolink Doorbell (recommended)"},
                                {"value": AUDIO_MODE_MANUAL, "label": "Manual Setup"},
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def async_step_reolink(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        reolink_options = _reolink_entry_selector_options(self.hass)
        if not reolink_options:
            errors["base"] = "no_reolink_found"
            return self.async_show_form(step_id="reolink", data_schema=vol.Schema({}), errors=errors)

        if user_input is not None:
            reolink_entry_id = user_input.get(CONF_REOLINK_ENTRY_ID)
            if not reolink_entry_id:
                errors[CONF_REOLINK_ENTRY_ID] = "required"
            else:
                self._data[CONF_REOLINK_ENTRY_ID] = str(reolink_entry_id)
                self._data[CONF_AUDIO_OUTPUT_MODE] = AUDIO_OUTPUT_GO2RTC
                self._data.pop(CONF_AUDIO_MANUAL_MODE, None)
                self._data.pop(CONF_GO2RTC_STREAM_NAME, None)
                self._data.pop(CONF_GO2RTC_INPUT_STREAM_NAME, None)
                self._data.pop(CONF_GO2RTC_OUTPUT_STREAM_NAME, None)
                self._data.pop(CONF_MEDIA_PLAYER_ENTITY, None)
                self._data.pop(CONF_MICROPHONE_ENTITY, None)

                # Probe audio input methods in background and cache the result
                await self._probe_reolink_audio(str(reolink_entry_id))

                trigger_entity = _auto_detect_reolink_trigger_entity(self.hass, str(reolink_entry_id))
                if trigger_entity:
                    self._data["doorbell_trigger_entity"] = trigger_entity
                return await self.async_step_provider()

        return self.async_show_form(
            step_id="reolink",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_REOLINK_ENTRY_ID,
                        default=self._data.get(CONF_REOLINK_ENTRY_ID, ""),
                    ): SelectSelector(
                        SelectSelectorConfig(options=reolink_options, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
            errors=errors,
        )

    async def _probe_reolink_audio(self, reolink_entry_id: str) -> None:
        """Probe audio input methods for a Reolink camera and cache the best one."""
        from .reolink_audio import get_reolink_config, probe_audio_input_method  # noqa: PLC0415

        reolink_config = get_reolink_config(self.hass, reolink_entry_id)
        if not reolink_config or not reolink_config.get("host"):
            return

        try:
            method, url = await probe_audio_input_method(
                host=reolink_config["host"],
                username=reolink_config.get("username", ""),
                password=reolink_config.get("password", ""),
                rtsp_port=reolink_config.get("rtsp_port", 554),
            )
            if method != "none":
                self._data[CONF_REOLINK_MIC_METHOD] = method
                self._data[CONF_REOLINK_MIC_URL] = url
                _LOGGER.info("Audio probe result: method=%s", method)
            else:
                self._data.pop(CONF_REOLINK_MIC_METHOD, None)
                self._data.pop(CONF_REOLINK_MIC_URL, None)
        except Exception:
            _LOGGER.warning("Audio probe failed (non-critical)", exc_info=True)

    async def async_step_manual_audio(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        manual_mode = self._data.get(CONF_AUDIO_MANUAL_MODE, DEFAULT_AUDIO_MANUAL_MODE)
        _sync_manual_stream_fields(self._data)

        if user_input is not None:
            manual_mode = user_input.get(CONF_AUDIO_MANUAL_MODE, DEFAULT_AUDIO_MANUAL_MODE)
            if manual_mode == AUDIO_MANUAL_EXTERNAL_GO2RTC:
                if not _clean_text(user_input.get(CONF_GO2RTC_INPUT_STREAM_NAME, "")):
                    errors[CONF_GO2RTC_INPUT_STREAM_NAME] = "required"
                if not _clean_text(user_input.get(CONF_GO2RTC_OUTPUT_STREAM_NAME, "")):
                    errors[CONF_GO2RTC_OUTPUT_STREAM_NAME] = "required"
            elif manual_mode == AUDIO_MANUAL_HA_ENTITIES:
                speaker_entity = _clean_text(user_input.get(CONF_MEDIA_PLAYER_ENTITY, ""))
                microphone_entity = _clean_text(user_input.get(CONF_MICROPHONE_ENTITY, ""))
                if not speaker_entity:
                    errors[CONF_MEDIA_PLAYER_ENTITY] = "required"
                elif not _is_valid_entity_for_domains(
                    self.hass,
                    speaker_entity,
                    _SPEAKER_ENTITY_DOMAINS,
                ):
                    errors[CONF_MEDIA_PLAYER_ENTITY] = "invalid_speaker_entity"
                if not microphone_entity:
                    errors[CONF_MICROPHONE_ENTITY] = "required"
                elif not _is_valid_entity_for_domains(
                    self.hass,
                    microphone_entity,
                    _MICROPHONE_ENTITY_DOMAINS,
                ):
                    errors[CONF_MICROPHONE_ENTITY] = "invalid_microphone_entity"

            if not errors:
                self._data[CONF_AUDIO_MODE] = AUDIO_MODE_MANUAL
                self._data[CONF_AUDIO_MANUAL_MODE] = str(manual_mode)
                self._data.pop(CONF_REOLINK_ENTRY_ID, None)
                self._data.pop("doorbell_trigger_entity", None)
                if manual_mode == AUDIO_MANUAL_EXTERNAL_GO2RTC:
                    input_stream = _clean_text(user_input.get(CONF_GO2RTC_INPUT_STREAM_NAME, ""))
                    output_stream = _clean_text(user_input.get(CONF_GO2RTC_OUTPUT_STREAM_NAME, ""))
                    self._data[CONF_GO2RTC_INPUT_STREAM_NAME] = input_stream
                    self._data[CONF_GO2RTC_OUTPUT_STREAM_NAME] = output_stream
                    self._data[CONF_GO2RTC_STREAM_NAME] = output_stream
                    self._data[CONF_AUDIO_OUTPUT_MODE] = AUDIO_OUTPUT_EVENT
                    self._data.pop(CONF_MEDIA_PLAYER_ENTITY, None)
                    self._data.pop(CONF_MICROPHONE_ENTITY, None)
                else:
                    self._data[CONF_MEDIA_PLAYER_ENTITY] = _clean_text(
                        user_input.get(CONF_MEDIA_PLAYER_ENTITY, "")
                    )
                    self._data[CONF_MICROPHONE_ENTITY] = _clean_text(
                        user_input.get(CONF_MICROPHONE_ENTITY, "")
                    )
                    self._data[CONF_AUDIO_OUTPUT_MODE] = AUDIO_OUTPUT_MEDIA_PLAYER
                    self._data.pop(CONF_GO2RTC_STREAM_NAME, None)
                    self._data.pop(CONF_GO2RTC_INPUT_STREAM_NAME, None)
                    self._data.pop(CONF_GO2RTC_OUTPUT_STREAM_NAME, None)
                return await self.async_step_provider()

        schema: dict[Any, Any] = {
            vol.Required(
                CONF_AUDIO_MANUAL_MODE,
                default=manual_mode,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {
                            "value": AUDIO_MANUAL_EXTERNAL_GO2RTC,
                            "label": "External go2rtc stream",
                        },
                        {
                            "value": AUDIO_MANUAL_HA_ENTITIES,
                            "label": "Home Assistant entities (speaker + microphone)",
                        },
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_GO2RTC_INPUT_STREAM_NAME,
                default=_manual_stream_default(self._data, CONF_GO2RTC_INPUT_STREAM_NAME),
            ): TextSelector(),
            vol.Optional(
                CONF_GO2RTC_OUTPUT_STREAM_NAME,
                default=_manual_stream_default(self._data, CONF_GO2RTC_OUTPUT_STREAM_NAME),
            ): TextSelector(),
        }
        _add_optional_entity_selector(
            schema,
            CONF_MEDIA_PLAYER_ENTITY,
            self._data.get(CONF_MEDIA_PLAYER_ENTITY),
            EntitySelectorConfig(domain=list(_SPEAKER_ENTITY_DOMAINS)),
        )
        _add_optional_entity_selector(
            schema,
            CONF_MICROPHONE_ENTITY,
            self._data.get(CONF_MICROPHONE_ENTITY),
            EntitySelectorConfig(domain=list(_MICROPHONE_ENTITY_DOMAINS)),
        )
        return self.async_show_form(
            step_id="manual_audio",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def async_step_provider(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {
            "model_hint": "e.g. gemini-2.5-flash-native-audio-latest",
        }
        provider = self._data.get(CONF_PROVIDER, PROVIDER_GEMINI)
        default_model = DEFAULT_MODEL_GEMINI if provider == PROVIDER_GEMINI else DEFAULT_MODEL_OPENAI
        default_voice = DEFAULT_VOICE_GEMINI if provider == PROVIDER_GEMINI else DEFAULT_VOICE_OPENAI

        if user_input is not None:
            valid = await self._validate_api_key(
                user_input[CONF_PROVIDER],
                user_input[CONF_API_KEY],
                user_input.get(CONF_API_BASE_URL),
            )
            if valid:
                # Check model capabilities to inform user
                caps = await _check_model_capabilities(
                    self.hass,
                    user_input[CONF_PROVIDER],
                    user_input[CONF_API_KEY],
                    user_input[CONF_MODEL],
                )
                self._data.update(user_input)
                self._data["_model_caps"] = caps
                if not caps["supports_text_output"]:
                    # Auto-set text model if voice model can't output text
                    if not self._data.get(CONF_TEXT_MODEL):
                        self._data[CONF_TEXT_MODEL] = DEFAULT_TEXT_MODEL_GEMINI
                return await self.async_step_camera()
            errors["base"] = "invalid_api_key"

        return self.async_show_form(
            step_id="provider",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PROVIDER, default=provider): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": PROVIDER_GEMINI, "label": "Google Gemini"},
                                {"value": PROVIDER_OPENAI, "label": "OpenAI / Compatible"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type="password")),
                    vol.Optional(CONF_API_BASE_URL, default=self._data.get(CONF_API_BASE_URL, "")): TextSelector(
                        TextSelectorConfig(type="url")
                    ),
                    vol.Required(CONF_MODEL, default=self._data.get(CONF_MODEL, default_model)): TextSelector(),
                    vol.Required(CONF_VOICE, default=self._data.get(CONF_VOICE, default_voice)): TextSelector(),
                }
            ),
            errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_camera(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_prompt()

        schema: dict[Any, Any] = {
            vol.Required(
                CONF_CAMERA_ENTITY,
                default=self._data.get(CONF_CAMERA_ENTITY, ""),
            ): EntitySelector(EntitySelectorConfig(domain="camera"))
        }

        schema[vol.Required(CONF_VISION_FPS, default=self._data.get(CONF_VISION_FPS, DEFAULT_VISION_FPS))] = NumberSelector(
            NumberSelectorConfig(min=0.1, max=10.0, step=0.1, mode=NumberSelectorMode.SLIDER)
        )
        schema[
            vol.Required(CONF_FRAME_MAX_WIDTH, default=self._data.get(CONF_FRAME_MAX_WIDTH, DEFAULT_FRAME_MAX_WIDTH))
        ] = NumberSelector(NumberSelectorConfig(min=160, max=1920, step=80, mode=NumberSelectorMode.SLIDER))
        schema[
            vol.Required(CONF_FRAME_MAX_HEIGHT, default=self._data.get(CONF_FRAME_MAX_HEIGHT, DEFAULT_FRAME_MAX_HEIGHT))
        ] = NumberSelector(NumberSelectorConfig(min=120, max=1080, step=60, mode=NumberSelectorMode.SLIDER))
        schema[
            vol.Required(CONF_FRAME_QUALITY, default=self._data.get(CONF_FRAME_QUALITY, DEFAULT_FRAME_QUALITY))
        ] = NumberSelector(NumberSelectorConfig(min=10, max=100, step=5, mode=NumberSelectorMode.SLIDER))
        return self.async_show_form(step_id="camera", data_schema=vol.Schema(schema))

    async def async_step_prompt(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._data[CONF_SYSTEM_PROMPT] = user_input[CONF_SYSTEM_PROMPT]
            self._data[CONF_SESSION_TIMEOUT] = user_input.get(CONF_SESSION_TIMEOUT, DEFAULT_SESSION_TIMEOUT)
            self._data[CONF_MEMORY_RETENTION_DAYS] = user_input.get(
                CONF_MEMORY_RETENTION_DAYS, DEFAULT_MEMORY_RETENTION_DAYS
            )
            return await self.async_step_triggers_setup()

        return self.async_show_form(
            step_id="prompt",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SYSTEM_PROMPT,
                        default=self._data.get(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT),
                    ): TextSelector(TextSelectorConfig(multiline=True, type="text")),
                    vol.Required(
                        CONF_SESSION_TIMEOUT,
                        default=self._data.get(CONF_SESSION_TIMEOUT, DEFAULT_SESSION_TIMEOUT),
                    ): NumberSelector(
                        NumberSelectorConfig(min=10, max=600, step=10, mode=NumberSelectorMode.SLIDER)
                    ),
                    vol.Required(
                        CONF_MEMORY_RETENTION_DAYS,
                        default=self._data.get(
                            CONF_MEMORY_RETENTION_DAYS, DEFAULT_MEMORY_RETENTION_DAYS
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(min=1, max=365, step=1, mode=NumberSelectorMode.SLIDER)
                    ),
                }
            ),
        )

    async def async_step_triggers_setup(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._data["start_triggers_config"] = [
                {"entity_id": entity_id, "to_state": "on"} for entity_id in user_input.get("start_entities", [])
            ]
            self._data[CONF_STOP_ENTITIES] = user_input.get(CONF_STOP_ENTITIES, [])
            return self._create_entry()

        start_entities = [
            trigger["entity_id"]
            for trigger in self._data.get("start_triggers_config", [])
            if trigger.get("entity_id")
        ]
        if not start_entities and self._data.get("doorbell_trigger_entity"):
            start_entities = [self._data["doorbell_trigger_entity"]]

        return self.async_show_form(
            step_id="triggers_setup",
            data_schema=vol.Schema(
                {
                    vol.Optional("start_entities", default=start_entities): EntitySelector(
                        EntitySelectorConfig(
                            domain=["binary_sensor", "input_boolean", "button"], multiple=True
                        )
                    ),
                    vol.Optional(CONF_STOP_ENTITIES, default=self._data.get(CONF_STOP_ENTITIES, [])): EntitySelector(
                        EntitySelectorConfig(multiple=True)
                    ),
                }
            ),
        )

    def _create_entry(self) -> FlowResult:
        if not self._data.get("start_triggers_config") and self._data.get("doorbell_trigger_entity"):
            self._data["start_triggers_config"] = [
                {"entity_id": self._data["doorbell_trigger_entity"], "to_state": "on"}
            ]
        self._data.pop("doorbell_trigger_entity", None)
        camera = self._data.get(CONF_CAMERA_ENTITY, "")
        title = f"Jeeves ({camera})" if camera else "Doorbell Jeeves"
        return self.async_create_entry(title=title, data=self._data)

    async def _validate_api_key(self, provider: str, api_key: str, base_url: str | None = None) -> bool:
        if not api_key:
            return False
        try:
            if provider == PROVIDER_GEMINI:

                def _validate_gemini() -> bool:
                    from google import genai  # noqa: PLC0415

                    return len(list(genai.Client(api_key=api_key).models.list())) > 0

                return await self.hass.async_add_executor_job(_validate_gemini)

            import openai  # noqa: PLC0415

            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            await openai.AsyncOpenAI(**kwargs).models.list()
            return True
        except Exception as err:
            _LOGGER.debug("API key validation failed: %s", err)
            return False


class DoorbellJeevesOptionsFlow(OptionsFlow):
    """Handle options for Doorbell Jeeves."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._data: dict[str, Any] = dict(config_entry.data) | dict(config_entry.options)
        _sync_manual_stream_fields(self._data)
        self._entity_edit: dict[str, Any] = {}
        self._action_edit: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "general",
                "dual_model",
                "vision",
                "audio",
                "timeline",
                "entities",
                "camera_map",
                "security",
                "triggers",
                "task_instructions",
                "identities",
                "prompt",
            ],
        )

    async def async_step_general(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            # Check capabilities of the selected voice model
            caps = await _check_model_capabilities(
                self.hass,
                user_input.get(CONF_PROVIDER, self._data.get(CONF_PROVIDER, PROVIDER_GEMINI)),
                user_input.get(CONF_API_KEY, self._data.get(CONF_API_KEY, "")),
                user_input.get(CONF_MODEL, ""),
            )
            if not caps["supports_text_output"] and not user_input.get(CONF_TEXT_MODEL):
                user_input[CONF_TEXT_MODEL] = DEFAULT_TEXT_MODEL_GEMINI
            self._data.update(user_input)
            return self._save_options()
        provider = self._data.get(CONF_PROVIDER, PROVIDER_GEMINI)
        default_model = DEFAULT_MODEL_GEMINI if provider == PROVIDER_GEMINI else DEFAULT_MODEL_OPENAI
        default_voice = DEFAULT_VOICE_GEMINI if provider == PROVIDER_GEMINI else DEFAULT_VOICE_OPENAI
        default_text_model = self._data.get(CONF_TEXT_MODEL) or DEFAULT_TEXT_MODEL_GEMINI
        return self.async_show_form(
            step_id="general",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PROVIDER, default=provider): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": PROVIDER_GEMINI, "label": "Google Gemini"},
                                {"value": PROVIDER_OPENAI, "label": "OpenAI / Compatible"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(CONF_API_KEY, default=self._data.get(CONF_API_KEY, "")): TextSelector(
                        TextSelectorConfig(type="password")
                    ),
                    vol.Optional(CONF_API_BASE_URL, default=self._data.get(CONF_API_BASE_URL, "")): TextSelector(
                        TextSelectorConfig(type="url")
                    ),
                    vol.Required(CONF_MODEL, default=self._data.get(CONF_MODEL, default_model)): TextSelector(),
                    vol.Required(CONF_VOICE, default=self._data.get(CONF_VOICE, default_voice)): TextSelector(),
                    vol.Optional(CONF_TEXT_MODEL, default=default_text_model): TextSelector(),
                    vol.Required(
                        CONF_SESSION_TIMEOUT,
                        default=self._data.get(CONF_SESSION_TIMEOUT, DEFAULT_SESSION_TIMEOUT),
                    ): NumberSelector(NumberSelectorConfig(min=10, max=600, step=10, mode=NumberSelectorMode.SLIDER)),
                    vol.Required(
                        CONF_MEMORY_RETENTION_DAYS,
                        default=self._data.get(
                            CONF_MEMORY_RETENTION_DAYS, DEFAULT_MEMORY_RETENTION_DAYS
                        ),
                    ): NumberSelector(NumberSelectorConfig(min=1, max=365, step=1, mode=NumberSelectorMode.SLIDER)),
                    vol.Optional(CONF_VALIDATOR_MODEL, default=self._data.get(CONF_VALIDATOR_MODEL, DEFAULT_VALIDATOR_MODEL)): TextSelector(),
                }
            ),
        )

    async def async_step_dual_model(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self._save_options()
        tool_provider = self._data.get(CONF_TOOL_PROVIDER, self._data.get(CONF_PROVIDER, PROVIDER_GEMINI))
        default_tool_model = DEFAULT_TOOL_MODEL_GEMINI if tool_provider == PROVIDER_GEMINI else DEFAULT_TOOL_MODEL_OPENAI
        return self.async_show_form(
            step_id="dual_model",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DUAL_MODEL_ENABLED, default=self._data.get(CONF_DUAL_MODEL_ENABLED, False)): BooleanSelector(),
                    vol.Optional(CONF_TOOL_PROVIDER, default=tool_provider): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": PROVIDER_GEMINI, "label": "Google Gemini"},
                                {"value": PROVIDER_OPENAI, "label": "OpenAI / Compatible"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional(CONF_TOOL_MODEL, default=self._data.get(CONF_TOOL_MODEL, default_tool_model)): TextSelector(),
                    vol.Optional(CONF_TOOL_API_KEY, default=self._data.get(CONF_TOOL_API_KEY, "")): TextSelector(
                        TextSelectorConfig(type="password")
                    ),
                    vol.Optional(CONF_TOOL_BASE_URL, default=self._data.get(CONF_TOOL_BASE_URL, "")): TextSelector(
                        TextSelectorConfig(type="url")
                    ),
                }
            ),
        )

    async def async_step_vision(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self._save_options()
        return self.async_show_form(
            step_id="vision",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CAMERA_ENTITY, default=self._data.get(CONF_CAMERA_ENTITY, "")): EntitySelector(
                        EntitySelectorConfig(domain="camera")
                    ),
                    vol.Required(CONF_VISION_FPS, default=self._data.get(CONF_VISION_FPS, DEFAULT_VISION_FPS)): NumberSelector(
                        NumberSelectorConfig(min=0.1, max=10.0, step=0.1, mode=NumberSelectorMode.SLIDER)
                    ),
                    vol.Required(
                        CONF_FRAME_MAX_WIDTH,
                        default=self._data.get(CONF_FRAME_MAX_WIDTH, DEFAULT_FRAME_MAX_WIDTH),
                    ): NumberSelector(NumberSelectorConfig(min=160, max=1920, step=80, mode=NumberSelectorMode.SLIDER)),
                    vol.Required(
                        CONF_FRAME_MAX_HEIGHT,
                        default=self._data.get(CONF_FRAME_MAX_HEIGHT, DEFAULT_FRAME_MAX_HEIGHT),
                    ): NumberSelector(NumberSelectorConfig(min=120, max=1080, step=60, mode=NumberSelectorMode.SLIDER)),
                    vol.Required(
                        CONF_FRAME_QUALITY,
                        default=self._data.get(CONF_FRAME_QUALITY, DEFAULT_FRAME_QUALITY),
                    ): NumberSelector(NumberSelectorConfig(min=10, max=100, step=5, mode=NumberSelectorMode.SLIDER)),
                }
            ),
        )

    async def async_step_camera_map(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        placements: list[CameraPlacement] = getattr(store, CONF_CAMERA_PLACEMENTS)
        items = [
            f"{placement.name} ({placement.entity_id}) — {placement.side} side, facing {placement.facing}"
            for placement in placements
        ]
        return self.async_show_menu(
            step_id="camera_map",
            menu_options=["add_camera_placement", "remove_camera_placement", "init"],
            description_placeholders={
                "camera_summary": "**Mapped Cameras:**\n"
                + ("\n".join(f"• {item}" for item in items) if items else "None configured")
                + "\n\n💡 **Tip:** For a visual drag-and-drop editor, add the "
                + "`custom:jeeves-camera-map-panel` card to your dashboard."
            },
        )

    async def async_step_add_camera_placement(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        if user_input is not None:
            placements: list[CameraPlacement] = getattr(store, CONF_CAMERA_PLACEMENTS)
            placement = CameraPlacement(
                entity_id=user_input["entity_id"],
                name=user_input["name"],
                x=float(user_input.get("x", 0.5)),
                y=float(user_input.get("y", 0.5)),
                rotation=float(user_input.get("rotation", 0)),
                area_description=user_input.get("area_description", ""),
                is_doorbell=user_input.get("is_doorbell", False),
                ptz_up=_clean_text(user_input.get("ptz_up", "")),
                ptz_down=_clean_text(user_input.get("ptz_down", "")),
                ptz_left=_clean_text(user_input.get("ptz_left", "")),
                ptz_right=_clean_text(user_input.get("ptz_right", "")),
                ptz_return_to_monitor=_clean_text(user_input.get("ptz_return_to_monitor", "")),
            )
            setattr(
                store,
                CONF_CAMERA_PLACEMENTS,
                [item for item in placements if item.entity_id != placement.entity_id] + [placement],
            )
            await store.async_save_entities()
            return await self.async_step_camera_map()

        schema: dict[Any, Any] = {
            vol.Required("entity_id"): EntitySelector(EntitySelectorConfig(domain="camera")),
            vol.Required("name"): TextSelector(),
            vol.Required("rotation", default=0): NumberSelector(
                NumberSelectorConfig(min=0, max=360, step=5, mode=NumberSelectorMode.SLIDER,
                                     unit_of_measurement="°")
            ),
            vol.Optional("area_description", default=""): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Optional("is_doorbell", default=False): BooleanSelector(),
        }
        ptz_selector = EntitySelectorConfig(domain=["button", "script"])
        _add_optional_entity_selector(schema, "ptz_up", None, ptz_selector)
        _add_optional_entity_selector(schema, "ptz_down", None, ptz_selector)
        _add_optional_entity_selector(schema, "ptz_left", None, ptz_selector)
        _add_optional_entity_selector(schema, "ptz_right", None, ptz_selector)
        _add_optional_entity_selector(schema, "ptz_return_to_monitor", None, ptz_selector)
        return self.async_show_form(
            step_id="add_camera_placement",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "tip": "Use the Camera Map card on your dashboard for visual placement. "
                       "This form is for basic setup only."
            },
        )

    async def async_step_remove_camera_placement(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        placements: list[CameraPlacement] = getattr(store, CONF_CAMERA_PLACEMENTS)
        options = [
            {"value": placement.entity_id, "label": f"{placement.name} ({placement.entity_id})"}
            for placement in placements
        ]
        if not options:
            return await self.async_step_camera_map()
        if user_input is not None:
            setattr(
                store,
                CONF_CAMERA_PLACEMENTS,
                [placement for placement in placements if placement.entity_id != user_input["entity_id"]],
            )
            await store.async_save_entities()
            return await self.async_step_camera_map()
        return self.async_show_form(
            step_id="remove_camera_placement",
            data_schema=vol.Schema(
                {
                    vol.Required("entity_id"): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
        )

    async def async_step_audio(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            audio_mode = user_input.get(CONF_AUDIO_MODE, AUDIO_MODE_REOLINK)
            self._data[CONF_AUDIO_MODE] = audio_mode
            if audio_mode == AUDIO_MODE_REOLINK:
                return await self.async_step_audio_reolink()
            return await self.async_step_audio_manual()

        return self.async_show_form(
            step_id="audio",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AUDIO_MODE,
                        default=self._data.get(CONF_AUDIO_MODE, AUDIO_MODE_REOLINK),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": AUDIO_MODE_REOLINK, "label": "Reolink native (auto)"},
                                {"value": AUDIO_MODE_MANUAL, "label": "Manual setup"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_audio_reolink(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        reolink_options = _reolink_entry_selector_options(self.hass)
        if not reolink_options:
            errors["base"] = "no_reolink_found"
            return self.async_show_form(
                step_id="audio_reolink",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        if user_input is not None:
            reolink_entry_id = _clean_text(user_input.get(CONF_REOLINK_ENTRY_ID, ""))
            if not reolink_entry_id:
                errors[CONF_REOLINK_ENTRY_ID] = "required"
            elif not any(option["value"] == reolink_entry_id for option in reolink_options):
                errors["base"] = "no_reolink_found"
            else:
                self._data[CONF_AUDIO_MODE] = AUDIO_MODE_REOLINK
                self._data[CONF_REOLINK_ENTRY_ID] = reolink_entry_id
                self._data[CONF_AUDIO_OUTPUT_MODE] = AUDIO_OUTPUT_GO2RTC
                self._data.pop(CONF_AUDIO_MANUAL_MODE, None)
                self._data.pop(CONF_GO2RTC_STREAM_NAME, None)
                self._data.pop(CONF_GO2RTC_INPUT_STREAM_NAME, None)
                self._data.pop(CONF_GO2RTC_OUTPUT_STREAM_NAME, None)
                self._data.pop(CONF_MEDIA_PLAYER_ENTITY, None)
                self._data.pop(CONF_MICROPHONE_ENTITY, None)

                # Probe audio input methods and cache the best one
                await self._probe_reolink_audio(reolink_entry_id)

                trigger_entity = _auto_detect_reolink_trigger_entity(self.hass, reolink_entry_id)
                if trigger_entity:
                    self._data["doorbell_trigger_entity"] = trigger_entity
                return self._save_options()

        return self.async_show_form(
            step_id="audio_reolink",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_REOLINK_ENTRY_ID,
                        default=self._data.get(CONF_REOLINK_ENTRY_ID, ""),
                    ): SelectSelector(
                        SelectSelectorConfig(options=reolink_options, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_audio_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        manual_mode = self._data.get(CONF_AUDIO_MANUAL_MODE, DEFAULT_AUDIO_MANUAL_MODE)
        _sync_manual_stream_fields(self._data)

        if user_input is not None:
            manual_mode = user_input.get(CONF_AUDIO_MANUAL_MODE, DEFAULT_AUDIO_MANUAL_MODE)
            if manual_mode == AUDIO_MANUAL_EXTERNAL_GO2RTC:
                if not _clean_text(user_input.get(CONF_GO2RTC_INPUT_STREAM_NAME, "")):
                    errors[CONF_GO2RTC_INPUT_STREAM_NAME] = "required"
                if not _clean_text(user_input.get(CONF_GO2RTC_OUTPUT_STREAM_NAME, "")):
                    errors[CONF_GO2RTC_OUTPUT_STREAM_NAME] = "required"
            elif manual_mode == AUDIO_MANUAL_HA_ENTITIES:
                speaker_entity = _clean_text(user_input.get(CONF_MEDIA_PLAYER_ENTITY, ""))
                microphone_entity = _clean_text(user_input.get(CONF_MICROPHONE_ENTITY, ""))
                if not speaker_entity:
                    errors[CONF_MEDIA_PLAYER_ENTITY] = "required"
                elif not _is_valid_entity_for_domains(
                    self.hass,
                    speaker_entity,
                    _SPEAKER_ENTITY_DOMAINS,
                ):
                    errors[CONF_MEDIA_PLAYER_ENTITY] = "invalid_speaker_entity"
                if not microphone_entity:
                    errors[CONF_MICROPHONE_ENTITY] = "required"
                elif not _is_valid_entity_for_domains(
                    self.hass,
                    microphone_entity,
                    _MICROPHONE_ENTITY_DOMAINS,
                ):
                    errors[CONF_MICROPHONE_ENTITY] = "invalid_microphone_entity"

            if not errors:
                self._data[CONF_AUDIO_MODE] = AUDIO_MODE_MANUAL
                self._data[CONF_AUDIO_MANUAL_MODE] = str(manual_mode)
                self._data.pop(CONF_REOLINK_ENTRY_ID, None)
                self._data.pop("doorbell_trigger_entity", None)
                if manual_mode == AUDIO_MANUAL_EXTERNAL_GO2RTC:
                    input_stream = _clean_text(user_input.get(CONF_GO2RTC_INPUT_STREAM_NAME, ""))
                    output_stream = _clean_text(user_input.get(CONF_GO2RTC_OUTPUT_STREAM_NAME, ""))
                    self._data[CONF_GO2RTC_INPUT_STREAM_NAME] = input_stream
                    self._data[CONF_GO2RTC_OUTPUT_STREAM_NAME] = output_stream
                    self._data[CONF_GO2RTC_STREAM_NAME] = output_stream
                    self._data[CONF_AUDIO_OUTPUT_MODE] = AUDIO_OUTPUT_EVENT
                    self._data.pop(CONF_MEDIA_PLAYER_ENTITY, None)
                    self._data.pop(CONF_MICROPHONE_ENTITY, None)
                else:
                    self._data[CONF_MEDIA_PLAYER_ENTITY] = _clean_text(
                        user_input.get(CONF_MEDIA_PLAYER_ENTITY, "")
                    )
                    self._data[CONF_MICROPHONE_ENTITY] = _clean_text(
                        user_input.get(CONF_MICROPHONE_ENTITY, "")
                    )
                    self._data[CONF_AUDIO_OUTPUT_MODE] = AUDIO_OUTPUT_MEDIA_PLAYER
                    self._data.pop(CONF_GO2RTC_STREAM_NAME, None)
                    self._data.pop(CONF_GO2RTC_INPUT_STREAM_NAME, None)
                    self._data.pop(CONF_GO2RTC_OUTPUT_STREAM_NAME, None)
                return self._save_options()

        schema: dict[Any, Any] = {
            vol.Required(
                CONF_AUDIO_MANUAL_MODE,
                default=manual_mode,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"value": AUDIO_MANUAL_EXTERNAL_GO2RTC, "label": "External go2rtc stream"},
                        {
                            "value": AUDIO_MANUAL_HA_ENTITIES,
                            "label": "Home Assistant entities (speaker + microphone)",
                        },
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_GO2RTC_INPUT_STREAM_NAME,
                default=_manual_stream_default(self._data, CONF_GO2RTC_INPUT_STREAM_NAME),
            ): TextSelector(),
            vol.Optional(
                CONF_GO2RTC_OUTPUT_STREAM_NAME,
                default=_manual_stream_default(self._data, CONF_GO2RTC_OUTPUT_STREAM_NAME),
            ): TextSelector(),
        }
        _add_optional_entity_selector(
            schema,
            CONF_MEDIA_PLAYER_ENTITY,
            self._data.get(CONF_MEDIA_PLAYER_ENTITY),
            EntitySelectorConfig(domain=list(_SPEAKER_ENTITY_DOMAINS)),
        )
        _add_optional_entity_selector(
            schema,
            CONF_MICROPHONE_ENTITY,
            self._data.get(CONF_MICROPHONE_ENTITY),
            EntitySelectorConfig(domain=list(_MICROPHONE_ENTITY_DOMAINS)),
        )
        return self.async_show_form(
            step_id="audio_manual",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def async_step_timeline(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure optional LLM Vision timeline integration."""
        llmvision_entries = [
            entry
            for entry in self.hass.config_entries.async_entries("llmvision")
            if entry.state == config_entries.ConfigEntryState.LOADED
        ]
        llmvision_get_events = self.hass.services.has_service("llmvision", "get_events")
        llmvision_detected = bool(llmvision_entries) or llmvision_get_events
        errors: dict[str, str] = {}
        if user_input is not None:
            enabled = bool(user_input.get(CONF_LLMVISION_TIMELINE_ENABLED, False))
            if enabled and not llmvision_detected:
                errors["base"] = "llmvision_not_found"
            else:
                updated = dict(user_input)
                updated[CONF_LLMVISION_CAMERAS] = list(user_input.get(CONF_LLMVISION_CAMERAS, []))
                updated[CONF_LLMVISION_CATEGORIES] = [
                    str(category).strip().lower()
                    for category in user_input.get(CONF_LLMVISION_CATEGORIES, [])
                    if str(category).strip()
                ]
                self._data.update(updated)
                return self._save_options()

        category_options = [
            {"value": "person", "label": "person"},
            {"value": "people", "label": "people"},
            {"value": "animal", "label": "animal"},
            {"value": "vehicle", "label": "vehicle"},
            {"value": "package", "label": "package"},
            {"value": "motion", "label": "motion"},
            {"value": "no_activity", "label": "no_activity"},
            {"value": "unknown", "label": "unknown"},
        ]

        return self.async_show_form(
            step_id="timeline",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_LLMVISION_TIMELINE_ENABLED,
                        default=self._data.get(CONF_LLMVISION_TIMELINE_ENABLED, False),
                    ): BooleanSelector(),
                    vol.Optional(
                        CONF_LLMVISION_HOURS_BACK,
                        default=self._data.get(
                            CONF_LLMVISION_HOURS_BACK, DEFAULT_LLMVISION_HOURS_BACK
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(min=1, max=168, step=1, mode=NumberSelectorMode.SLIDER)
                    ),
                    vol.Optional(
                        CONF_LLMVISION_MAX_EVENTS,
                        default=self._data.get(
                            CONF_LLMVISION_MAX_EVENTS, DEFAULT_LLMVISION_MAX_EVENTS
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(min=1, max=200, step=1, mode=NumberSelectorMode.SLIDER)
                    ),
                    vol.Optional(
                        CONF_LLMVISION_INCLUDE_NO_ACTIVITY,
                        default=self._data.get(
                            CONF_LLMVISION_INCLUDE_NO_ACTIVITY,
                            DEFAULT_LLMVISION_INCLUDE_NO_ACTIVITY,
                        ),
                    ): BooleanSelector(),
                    vol.Optional(
                        CONF_LLMVISION_CAMERAS,
                        default=self._data.get(CONF_LLMVISION_CAMERAS, []),
                    ): EntitySelector(EntitySelectorConfig(domain="camera", multiple=True)),
                    vol.Optional(
                        CONF_LLMVISION_CATEGORIES,
                        default=self._data.get(CONF_LLMVISION_CATEGORIES, []),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=category_options,
                            mode=SelectSelectorMode.DROPDOWN,
                            multiple=True,
                            custom_value=True,
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "status": (
                    "Detected ✅ (native get_events service available)"
                    if llmvision_get_events
                    else "Detected ✅ (compatibility mode: get_events service not exposed)"
                    if llmvision_detected
                    else "Not detected ❌ (install and configure LLM Vision first)"
                )
            },
        )

    async def async_step_entities(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        readable = [entity for entity in store.managed_entities if entity.entity_id != GLOBAL_ACTIONS_ENTITY_ID]
        cameras = [entity for entity in readable if entity.entity_id.startswith("camera.")]
        calendars = [entity for entity in readable if entity.entity_id.startswith("calendar.")]
        others = [entity for entity in readable if not entity.entity_id.startswith(("camera.", "calendar."))]
        automation_actions = store.get_entity(GLOBAL_ACTIONS_ENTITY_ID)
        desc_parts: list[str] = []
        if cameras:
            desc_parts.append("**📷 Cameras (AI can view on demand):**\n" + "\n".join(f"• {entity.name}" for entity in cameras))
        if calendars:
            desc_parts.append("**📅 Calendars (AI can check schedules):**\n" + "\n".join(f"• {entity.name}" for entity in calendars))
        if others:
            desc_parts.append(
                "**🏠 Other Entities:**\n" + "\n".join(f"• {entity.name} - {len(entity.actions)} action(s)" for entity in others)
            )
        if automation_actions and automation_actions.actions:
            desc_parts.append(
                "**🤖 Automation Actions:**\n"
                + "\n".join(f"• {action.name}" for action in automation_actions.actions)
            )
        if store.notification_targets:
            desc_parts.append(
                "**🔔 Notifications:**\n" + "\n".join(f"• {target.name} ({target.service})" for target in store.notification_targets)
            )
        if store.audio_files:
            by_cat: dict[str, list[str]] = {}
            for af in store.audio_files:
                cat = af.category or "General"
                by_cat.setdefault(cat, []).append(af.name)
            audio_lines = []
            for cat, names in by_cat.items():
                audio_lines.append(f"  *{cat}:* " + ", ".join(names))
            desc_parts.append("**🔊 Audio Files:**\n" + "\n".join(audio_lines))
        desc = (
            "\n\n".join(desc_parts)
            if desc_parts
            else "No entities configured yet. Add cameras, calendars, devices, or automation actions below."
        )
        return self.async_show_menu(
            step_id="entities",
            menu_options=[
                "add_camera",
                "add_calendar",
                "add_entity",
                "edit_entity",
                "add_action",
                "edit_action",
                "add_notification",
                "add_audio_file",
                "remove_entity",
                "remove_action",
                "remove_notification",
                "remove_audio_file",
                "init",
            ],
            description_placeholders={"entity_summary": desc},
        )

    async def async_step_add_camera(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            store = await self._async_get_store()
            existing = store.get_entity(user_input["entity_id"])
            await store.async_add_entity(
                ManagedEntity(
                    entity_id=user_input["entity_id"],
                    name=user_input["name"],
                    description=user_input["description"],
                    actions=list(existing.actions) if existing else [],
                    security_mode=existing.security_mode if existing else SECURITY_MODE_AUTO,
                    require_visual_match=existing.require_visual_match if existing else False,
                    require_camera_feed=existing.require_camera_feed if existing else False,
                )
            )
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="add_camera",
            data_schema=vol.Schema(
                {
                    vol.Required("entity_id"): EntitySelector(EntitySelectorConfig(domain="camera")),
                    vol.Required("name"): TextSelector(),
                    vol.Required(
                        "description",
                        default="Camera feed the AI can view to check this area",
                    ): TextSelector(TextSelectorConfig(multiline=True)),
                }
            ),
        )

    async def async_step_add_calendar(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            store = await self._async_get_store()
            existing = store.get_entity(user_input["entity_id"])
            await store.async_add_entity(
                ManagedEntity(
                    entity_id=user_input["entity_id"],
                    name=user_input["name"],
                    description=user_input["description"],
                    actions=list(existing.actions) if existing else [],
                    security_mode=existing.security_mode if existing else SECURITY_MODE_AUTO,
                    require_visual_match=existing.require_visual_match if existing else False,
                    require_camera_feed=existing.require_camera_feed if existing else False,
                )
            )
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="add_calendar",
            data_schema=vol.Schema(
                {
                    vol.Required("entity_id"): EntitySelector(EntitySelectorConfig(domain="calendar")),
                    vol.Required("name"): TextSelector(),
                    vol.Required(
                        "description",
                        default="Owner's calendar — use to check if they are available",
                    ): TextSelector(TextSelectorConfig(multiline=True)),
                }
            ),
        )

    async def async_step_add_entity(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            store = await self._async_get_store()
            existing = store.get_entity(user_input["entity_id"])
            await store.async_add_entity(
                self._build_managed_entity_from_input(
                    user_input,
                    existing.actions if existing else None,
                )
            )
            return await self.async_step_entities()
        return self.async_show_form(step_id="add_entity", data_schema=vol.Schema(self._entity_form_schema()))

    async def async_step_edit_entity(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        options = self._managed_entity_options(store)
        if not options and not self._entity_edit:
            return await self.async_step_entities()
        if not self._entity_edit:
            if user_input is not None:
                self._entity_edit = {"original_entity_id": user_input["entity_id"]}
            else:
                return self.async_show_form(
                    step_id="edit_entity",
                    data_schema=vol.Schema(
                        {
                            vol.Required("entity_id"): SelectSelector(
                                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                            )
                        }
                    ),
                )
        entity = store.get_entity(self._entity_edit["original_entity_id"])
        if entity is None:
            self._entity_edit = {}
            return await self.async_step_entities()
        if user_input is not None and "name" in user_input:
            updated = self._build_managed_entity_from_input(user_input, entity.actions)
            await store.async_remove_entity(entity.entity_id)
            await store.async_add_entity(updated)
            self._entity_edit = {}
            return await self.async_step_entities()
        return self.async_show_form(step_id="edit_entity", data_schema=vol.Schema(self._entity_form_schema(entity)))

    async def async_step_remove_entity(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        options = self._managed_entity_options(store)
        if not options:
            return await self.async_step_entities()
        if user_input is not None:
            await store.async_remove_entity(user_input["entity_id"])
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="remove_entity",
            data_schema=vol.Schema(
                {
                    vol.Required("entity_id"): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
        )

    async def async_step_add_action(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        entity_options = self._managed_entity_options(store)
        has_automations = bool(self.hass.states.async_all("automation"))
        if not entity_options and not has_automations:
            return await self.async_step_entities()
        errors: dict[str, str] = {}
        if user_input is not None:
            target_type = user_input.get("action_target_type", "entity")
            action_input = dict(user_input)

            if target_type == "automation":
                automation_id = user_input.get("target_automation", "")
                if not automation_id:
                    errors["target_automation"] = "required"
                else:
                    action_input["target_entity"] = GLOBAL_ACTIONS_ENTITY_ID
                    action_input["action_config"] = {
                        "action": "automation.trigger",
                        "target": {"entity_id": automation_id},
                    }
            else:
                target_entity = user_input.get("target_entity", "")
                if not target_entity:
                    errors["target_entity"] = "required"

            if not errors:
                target_entity_id = action_input.get("target_entity", "")
                entity = store.get_entity(target_entity_id)
                if entity is None and target_entity_id == GLOBAL_ACTIONS_ENTITY_ID:
                    entity = await self._ensure_global_actions_entity(store)
                if entity is None:
                    errors["target_entity"] = "required"
                else:
                    new_action = self._build_action_from_input(action_input)
                    entity.actions = [action for action in entity.actions if action.id != new_action.id]
                    entity.actions.append(new_action)
                    await store.async_add_entity(entity)
                    return await self.async_step_entities()
        return self.async_show_form(
            step_id="add_action",
            data_schema=vol.Schema(
                self._action_form_schema(
                    entity_options=entity_options,
                    include_target_entity=bool(entity_options),
                    include_target_automation=has_automations,
                )
            ),
            errors=errors,
        )

    async def async_step_add_standalone_action(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        # Legacy step kept for backward compatibility with in-progress flows.
        return await self.async_step_add_action(user_input)

    async def async_step_edit_action(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        options = self._action_options(store)
        if not options and not self._action_edit:
            return await self.async_step_entities()
        if not self._action_edit:
            if user_input is not None:
                entity_id, action_id = self._decode_action_ref(user_input["action_ref"])
                self._action_edit = {"entity_id": entity_id, "original_action_id": action_id}
            else:
                return self.async_show_form(
                    step_id="edit_action",
                    data_schema=vol.Schema(
                        {
                            vol.Required("action_ref"): SelectSelector(
                                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                            )
                        }
                    ),
                )
        entity = store.get_entity(self._action_edit["entity_id"])
        action = next((item for item in (entity.actions if entity else []) if item.id == self._action_edit["original_action_id"]), None)
        if entity is None or action is None:
            self._action_edit = {}
            return await self.async_step_entities()
        if user_input is not None and "action_name" in user_input:
            action_input = dict(user_input)
            target_type = user_input.get("action_target_type", "entity")
            if target_type == "automation":
                automation_id = user_input.get("target_automation", "")
                if automation_id:
                    action_input["target_entity"] = GLOBAL_ACTIONS_ENTITY_ID
                    action_input["action_config"] = {
                        "action": "automation.trigger",
                        "target": {"entity_id": automation_id},
                    }

            updated_action = self._build_action_from_input(action_input)
            target_entity_id = action_input.get("target_entity", entity.entity_id)
            target_entity = store.get_entity(target_entity_id)
            if target_entity is None and target_entity_id == GLOBAL_ACTIONS_ENTITY_ID:
                target_entity = await self._ensure_global_actions_entity(store)
            if target_entity is None:
                target_entity = entity

            entity.actions = [item for item in entity.actions if item.id != action.id]
            await store.async_add_entity(entity)
            target_entity.actions = [
                item for item in target_entity.actions if item.id != updated_action.id
            ]
            target_entity.actions.append(updated_action)
            await store.async_add_entity(target_entity)
            self._action_edit = {}
            return await self.async_step_entities()
        include_target = entity.entity_id != GLOBAL_ACTIONS_ENTITY_ID
        return self.async_show_form(
            step_id="edit_action",
            data_schema=vol.Schema(
                self._action_form_schema(
                    defaults=self._action_defaults(action, entity.entity_id),
                    entity_options=self._managed_entity_options(store) if include_target else None,
                    include_target_entity=include_target,
                    include_target_automation=True,
                )
            ),
        )

    async def async_step_remove_action(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        options = self._action_options(store)
        if not options:
            return await self.async_step_entities()
        if user_input is not None:
            entity_id, action_id = self._decode_action_ref(user_input["action_ref"])
            entity = store.get_entity(entity_id)
            if entity:
                entity.actions = [action for action in entity.actions if action.id != action_id]
                await store.async_add_entity(entity)
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="remove_action",
            data_schema=vol.Schema(
                {
                    vol.Required("action_ref"): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
        )

    async def async_step_add_notification(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        if user_input is not None:
            await store.async_add_notification(
                NotificationTarget(
                    service=user_input["service"],
                    name=user_input["name"],
                    description=user_input["description"],
                )
            )
            return await self.async_step_entities()

        notify_services = sorted(
            service
            for service in self.hass.services.async_services().get("notify", {}).keys()
            if service != "reload"
        )
        if notify_services:
            service_selector: Any = SelectSelector(
                SelectSelectorConfig(
                    options=[{"value": f"notify.{service}", "label": f"notify.{service}"} for service in notify_services],
                    mode=SelectSelectorMode.DROPDOWN,
                    custom_value=True,
                )
            )
        else:
            service_selector = TextSelector(TextSelectorConfig(type="text"))

        return self.async_show_form(
            step_id="add_notification",
            data_schema=vol.Schema(
                {
                    vol.Required("service"): service_selector,
                    vol.Required("name"): TextSelector(),
                    vol.Required("description"): TextSelector(TextSelectorConfig(multiline=True)),
                }
            ),
        )

    async def async_step_remove_notification(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        options = [{"value": target.service, "label": f"{target.name} ({target.service})"} for target in store.notification_targets]
        if not options:
            return await self.async_step_entities()
        if user_input is not None:
            await store.async_remove_notification(user_input["service"])
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="remove_notification",
            data_schema=vol.Schema(
                {
                    vol.Required("service"): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
        )

    async def async_step_add_audio_file(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Add an audio file the agent can play over speakers."""
        from .models import AudioFile  # noqa: PLC0415

        if user_input is not None:
            store = await self._async_get_store()
            # Generate a slug ID from the name
            slug = re.sub(r"[^a-z0-9]+", "-", user_input["name"].lower()).strip("-")
            if not slug:
                slug = f"audio-{len(store.audio_files)}"
            # Ensure unique
            existing_ids = {af.id for af in store.audio_files}
            base_slug = slug
            counter = 1
            while slug in existing_ids:
                slug = f"{base_slug}-{counter}"
                counter += 1
            audio_file = AudioFile(
                id=slug,
                name=user_input["name"],
                description=user_input.get("description", ""),
                media_id=user_input["media_id"],
                media_type=user_input.get("media_type", "music"),
                category=user_input.get("category", ""),
            )
            store.audio_files.append(audio_file)
            await store.async_save_entities()
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="add_audio_file",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): TextSelector(TextSelectorConfig(
                        prefix="Name (shown to agent)"
                    )),
                    vol.Required("media_id"): TextSelector(TextSelectorConfig(
                        prefix="Media ID or URL (e.g. media-source://media_source/local/sounds/scream.mp3)"
                    )),
                    vol.Optional("description", default=""): TextSelector(TextSelectorConfig(
                        multiline=True,
                        prefix="Description (context for the agent, e.g. 'A scary scream sound')"
                    )),
                    vol.Optional("category", default=""): TextSelector(TextSelectorConfig(
                        prefix="Category (e.g. Halloween, Alerts, Doorbells)"
                    )),
                    vol.Optional("media_type", default="music"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": "music", "label": "Music/Audio"},
                                {"value": "sound", "label": "Sound Effect"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                }
            ),
        )

    async def async_step_remove_audio_file(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Remove an audio file."""
        store = await self._async_get_store()
        options = [
            {"value": af.id, "label": f"{af.name} ({af.category or 'General'})"}
            for af in store.audio_files
        ]
        if not options:
            return await self.async_step_entities()
        if user_input is not None:
            store.audio_files = [af for af in store.audio_files if af.id != user_input["audio_id"]]
            await store.async_save_entities()
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="remove_audio_file",
            data_schema=vol.Schema(
                {
                    vol.Required("audio_id"): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
        )

    async def async_step_security(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self._save_options()
        return self.async_show_form(
            step_id="security",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEFAULT_SECURITY_MODE,
                        default=self._data.get(CONF_DEFAULT_SECURITY_MODE, SECURITY_MODE_AUTO),
                    ): self._security_selector(),
                    vol.Optional(CONF_PIN_CODE, default=self._data.get(CONF_PIN_CODE, "")): TextSelector(
                        TextSelectorConfig(type="password")
                    ),
                    vol.Optional(CONF_VALIDATOR_MODEL, default=self._data.get(CONF_VALIDATOR_MODEL, DEFAULT_VALIDATOR_MODEL)): TextSelector(),
                }
            ),
        )

    async def async_step_triggers(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        from .const import (  # noqa: PLC0415
            CONF_CHIME_DELAY,
            CONF_SILENCE_TIMEOUT,
            CONF_TAKEOVER_AUDIO_ENERGY,
            CONF_TAKEOVER_ENERGY_THRESHOLD,
            CONF_TAKEOVER_POLL_INTERVAL,
            CONF_TAKEOVER_REOLINK_API,
            DEFAULT_CHIME_DELAY,
            DEFAULT_SILENCE_TIMEOUT,
            DEFAULT_TAKEOVER_ENERGY_THRESHOLD,
            DEFAULT_TAKEOVER_POLL_INTERVAL,
        )

        if user_input is not None:
            self._data["start_triggers_config"] = [
                {"entity_id": entity_id, "to_state": "on"} for entity_id in user_input.get("start_entities", [])
            ]
            self._data[CONF_STOP_ENTITIES] = user_input.get(CONF_STOP_ENTITIES, [])
            self._data[CONF_TAKEOVER_REOLINK_API] = user_input.get(CONF_TAKEOVER_REOLINK_API, True)
            self._data[CONF_TAKEOVER_AUDIO_ENERGY] = user_input.get(CONF_TAKEOVER_AUDIO_ENERGY, False)
            self._data[CONF_TAKEOVER_ENERGY_THRESHOLD] = user_input.get(CONF_TAKEOVER_ENERGY_THRESHOLD, DEFAULT_TAKEOVER_ENERGY_THRESHOLD)
            self._data[CONF_TAKEOVER_POLL_INTERVAL] = user_input.get(CONF_TAKEOVER_POLL_INTERVAL, DEFAULT_TAKEOVER_POLL_INTERVAL)
            self._data[CONF_CHIME_DELAY] = user_input.get(CONF_CHIME_DELAY, DEFAULT_CHIME_DELAY)
            self._data[CONF_SILENCE_TIMEOUT] = user_input.get(CONF_SILENCE_TIMEOUT, DEFAULT_SILENCE_TIMEOUT)
            return self._save_options()

        current_start = self._data.get("start_triggers_config", [])
        start_entity_ids = [trigger["entity_id"] for trigger in current_start if trigger.get("entity_id")]
        is_reolink = self._data.get(CONF_AUDIO_MODE) == AUDIO_MODE_REOLINK
        schema: dict[Any, Any] = {
            vol.Optional("start_entities", default=start_entity_ids): EntitySelector(
                EntitySelectorConfig(
                    domain=["binary_sensor", "input_boolean", "button"], multiple=True
                )
            ),
            vol.Optional(CONF_STOP_ENTITIES, default=self._data.get(CONF_STOP_ENTITIES, [])): EntitySelector(
                EntitySelectorConfig(multiple=True)
            ),
        }
        if is_reolink:
            schema[vol.Optional(CONF_TAKEOVER_REOLINK_API, default=self._data.get(CONF_TAKEOVER_REOLINK_API, True))] = BooleanSelector()
        schema[vol.Optional(CONF_TAKEOVER_AUDIO_ENERGY, default=self._data.get(CONF_TAKEOVER_AUDIO_ENERGY, False))] = BooleanSelector()
        schema[
            vol.Optional(
                CONF_TAKEOVER_ENERGY_THRESHOLD,
                default=self._data.get(CONF_TAKEOVER_ENERGY_THRESHOLD, DEFAULT_TAKEOVER_ENERGY_THRESHOLD),
            )
        ] = NumberSelector(NumberSelectorConfig(min=500, max=10000, step=100, mode=NumberSelectorMode.SLIDER))
        schema[
            vol.Optional(
                CONF_CHIME_DELAY,
                default=self._data.get(CONF_CHIME_DELAY, DEFAULT_CHIME_DELAY),
            )
        ] = NumberSelector(NumberSelectorConfig(min=0.0, max=10.0, step=0.5, mode=NumberSelectorMode.SLIDER))
        schema[
            vol.Optional(
                CONF_SILENCE_TIMEOUT,
                default=self._data.get(CONF_SILENCE_TIMEOUT, DEFAULT_SILENCE_TIMEOUT),
            )
        ] = NumberSelector(NumberSelectorConfig(min=5, max=300, step=5, mode=NumberSelectorMode.SLIDER))
        if is_reolink:
            schema[
                vol.Optional(
                    CONF_TAKEOVER_POLL_INTERVAL,
                    default=self._data.get(CONF_TAKEOVER_POLL_INTERVAL, DEFAULT_TAKEOVER_POLL_INTERVAL),
                )
            ] = NumberSelector(NumberSelectorConfig(min=0.5, max=10.0, step=0.5, mode=NumberSelectorMode.SLIDER))
        return self.async_show_form(step_id="triggers", data_schema=vol.Schema(schema))

    async def async_step_task_instructions(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        instructions: list[TaskInstruction] = getattr(store, CONF_TASK_INSTRUCTIONS)
        items = [instruction.title for instruction in instructions]
        return self.async_show_menu(
            step_id="task_instructions",
            menu_options=["add_task_instruction", "remove_task_instruction", "init"],
            description_placeholders={
                "instruction_summary": "**Current Instructions:**\n"
                + ("\n".join(f"• {item}" for item in items) if items else "None configured")
            },
        )

    async def async_step_add_task_instruction(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        if user_input is not None:
            instructions: list[TaskInstruction] = getattr(store, CONF_TASK_INSTRUCTIONS)
            instruction = TaskInstruction(
                title=_clean_text(user_input["title"]),
                text=user_input["text"],
            )
            setattr(
                store,
                CONF_TASK_INSTRUCTIONS,
                [item for item in instructions if item.title.lower() != instruction.title.lower()]
                + [instruction],
            )
            await store.async_save_entities()
            return await self.async_step_task_instructions()
        return self.async_show_form(
            step_id="add_task_instruction",
            data_schema=vol.Schema(
                {
                    vol.Required("title"): TextSelector(),
                    vol.Required("text"): TextSelector(TextSelectorConfig(multiline=True)),
                }
            ),
        )

    async def async_step_remove_task_instruction(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        instructions: list[TaskInstruction] = getattr(store, CONF_TASK_INSTRUCTIONS)
        options = [{"value": instruction.title, "label": instruction.title} for instruction in instructions]
        if not options:
            return await self.async_step_task_instructions()
        if user_input is not None:
            setattr(
                store,
                CONF_TASK_INSTRUCTIONS,
                [instruction for instruction in instructions if instruction.title != user_input["title"]],
            )
            await store.async_save_entities()
            return await self.async_step_task_instructions()
        return self.async_show_form(
            step_id="remove_task_instruction",
            data_schema=vol.Schema(
                {
                    vol.Required("title"): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
        )

    async def async_step_identities(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        from .models import KnownIdentity  # noqa: PLC0415

        store = await self._async_get_store()
        items = [f"{identity.name} ({identity.identity_type}, {identity.relationship})" for identity in store.known_identities]
        return self.async_show_menu(
            step_id="identities",
            menu_options=["add_identity", "remove_identity", "identity_settings", "init"],
            description_placeholders={
                "identity_summary": "**Known Identities:**\n" + ("\n".join(f"• {item}" for item in items) if items else "None configured")
            },
        )

    async def async_step_add_identity(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        from .models import KnownIdentity  # noqa: PLC0415

        if user_input is not None:
            store = await self._async_get_store()
            image_data = user_input.get("reference_image", "").strip()
            image_b64: str | None = None
            if image_data:
                if image_data.startswith(("http://", "https://")):
                    image_b64 = await self._download_image_as_base64(image_data)
                elif image_data.startswith("/"):
                    image_b64 = await self._read_local_image_as_base64(image_data)
                else:
                    image_b64 = image_data
            await store.async_add_identity(
                KnownIdentity(
                    name=user_input["name"],
                    identity_type=user_input.get("identity_type", "person"),
                    relationship=user_input.get("relationship", "guest"),
                    description=user_input.get("description", ""),
                    access_level=user_input.get("access_level", "guest"),
                    image_base64=image_b64,
                    notes=user_input.get("notes", ""),
                )
            )
            return await self.async_step_identities()
        return self.async_show_form(
            step_id="add_identity",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): TextSelector(),
                    vol.Required("identity_type", default="person"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": "person", "label": "Person"},
                                {"value": "animal", "label": "Animal"},
                                {"value": "object", "label": "Object/Vehicle"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required("relationship", default="guest"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": "owner", "label": "Owner"},
                                {"value": "family", "label": "Family"},
                                {"value": "friend", "label": "Friend"},
                                {"value": "pet", "label": "Pet"},
                                {"value": "delivery", "label": "Delivery"},
                                {"value": "guest", "label": "Guest"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required("description"): TextSelector(TextSelectorConfig(multiline=True)),
                    vol.Required("access_level", default="guest"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": "full", "label": "Full Access"},
                                {"value": "limited", "label": "Limited Access"},
                                {"value": "guest", "label": "Guest (no access)"},
                                {"value": "none", "label": "Blocked"},
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Optional("reference_image", default=""): TextSelector(TextSelectorConfig(type="text")),
                    vol.Optional("notes", default=""): TextSelector(TextSelectorConfig(multiline=True)),
                }
            ),
        )

    async def async_step_remove_identity(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        store = await self._async_get_store()
        options = [{"value": identity.name, "label": f"{identity.name} ({identity.identity_type})"} for identity in store.known_identities]
        if not options:
            return await self.async_step_identities()
        if user_input is not None:
            await store.async_remove_identity(user_input["name"])
            return await self.async_step_identities()
        return self.async_show_form(
            step_id="remove_identity",
            data_schema=vol.Schema(
                {
                    vol.Required("name"): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
        )

    async def async_step_identity_settings(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._data[CONF_IDENTITY_MODE] = user_input.get(CONF_IDENTITY_MODE, IDENTITY_MODE_NONE)
            self._data[CONF_FACE_SENSOR_ENTITY] = _clean_text(user_input.get(CONF_FACE_SENSOR_ENTITY, ""))
            return self._save_options()
        schema: dict[Any, Any] = {
            vol.Required(CONF_IDENTITY_MODE, default=self._data.get(CONF_IDENTITY_MODE, IDENTITY_MODE_NONE)): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"value": IDENTITY_MODE_NONE, "label": "Disabled"},
                        {"value": IDENTITY_MODE_SENSOR, "label": "External Sensor (e.g. Frigate)"},
                        {"value": IDENTITY_MODE_REFERENCE_IMAGES, "label": "Reference Images"},
                        {"value": IDENTITY_MODE_BOTH, "label": "Both Sensor + Reference Images"},
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
        _add_optional_entity_selector(
            schema,
            CONF_FACE_SENSOR_ENTITY,
            self._data.get(CONF_FACE_SENSOR_ENTITY),
            EntitySelectorConfig(domain="sensor"),
        )
        return self.async_show_form(
            step_id="identity_settings",
            data_schema=vol.Schema(schema),
        )

    async def async_step_prompt(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._data[CONF_SYSTEM_PROMPT] = user_input[CONF_SYSTEM_PROMPT]
            return self._save_options()
        return self.async_show_form(
            step_id="prompt",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SYSTEM_PROMPT, default=self._data.get(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT)): TextSelector(
                        TextSelectorConfig(multiline=True, type="text")
                    )
                }
            ),
        )

    def _save_options(self) -> FlowResult:
        return self.async_create_entry(title="", data=self._data)

    async def _async_get_store(self) -> DataStore:
        store = DataStore(self.hass, self._config_entry.entry_id)
        await store.async_load()
        return store

    def _entity_form_schema(self, entity: ManagedEntity | None = None) -> dict[Any, Any]:
        entity = entity or ManagedEntity(entity_id="", name="", description="", actions=[])
        return {
            vol.Required("entity_id", default=entity.entity_id): EntitySelector(EntitySelectorConfig()),
            vol.Required("name", default=entity.name): TextSelector(),
            vol.Required("description", default=entity.description): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Required("security_mode", default=entity.security_mode): self._security_selector(),
            vol.Optional("require_visual_match", default=entity.require_visual_match): BooleanSelector(),
            vol.Optional("require_camera_feed", default=entity.require_camera_feed): BooleanSelector(),
        }

    def _build_managed_entity_from_input(
        self,
        user_input: dict[str, Any],
        actions: list[EntityAction] | None = None,
    ) -> ManagedEntity:
        return ManagedEntity(
            entity_id=user_input["entity_id"],
            name=user_input["name"],
            description=user_input["description"],
            actions=list(actions or []),
            security_mode=user_input.get("security_mode", SECURITY_MODE_AUTO),
            require_visual_match=user_input.get("require_visual_match", False),
            require_camera_feed=user_input.get("require_camera_feed", False),
        )

    def _action_form_schema(
        self,
        defaults: dict[str, Any] | None = None,
        entity_options: list[dict[str, str]] | None = None,
        include_target_entity: bool = True,
        include_target_automation: bool = True,
    ) -> dict[Any, Any]:
        defaults = defaults or {}
        schema: dict[Any, Any] = {}
        target_type_options: list[dict[str, str]] = []
        if include_target_entity:
            target_type_options.append({"value": "entity", "label": "Managed Entity"})
        if include_target_automation:
            target_type_options.append({"value": "automation", "label": "Automation Trigger"})
        default_target_type = defaults.get(
            "action_target_type",
            "entity" if include_target_entity else "automation",
        )
        schema[vol.Required("action_target_type", default=default_target_type)] = SelectSelector(
            SelectSelectorConfig(options=target_type_options, mode=SelectSelectorMode.DROPDOWN)
        )
        if include_target_entity:
            schema[vol.Optional("target_entity", default=defaults.get("target_entity", ""))] = SelectSelector(
                SelectSelectorConfig(options=entity_options or [], mode=SelectSelectorMode.DROPDOWN)
            )
        if include_target_automation:
            target_automation = _clean_text(defaults.get("target_automation", ""))
            if target_automation:
                schema[vol.Optional("target_automation", default=target_automation)] = EntitySelector(
                    EntitySelectorConfig(domain="automation")
                )
            else:
                schema[vol.Optional("target_automation")] = EntitySelector(
                    EntitySelectorConfig(domain="automation")
                )
        schema[vol.Required("action_id", default=defaults.get("action_id", ""))] = TextSelector()
        schema[vol.Required("action_name", default=defaults.get("action_name", ""))] = TextSelector()
        schema[vol.Required("action_description", default=defaults.get("action_description", ""))] = TextSelector(
            TextSelectorConfig(multiline=True)
        )
        schema[vol.Required("action_config", default=defaults.get("action_config", {}))] = ActionSelector()
        for field in _ACTION_STEP_FIELDS[1:]:
            schema[vol.Optional(field, default=defaults.get(field, {}))] = ActionSelector()
        schema[
            vol.Required("action_security_mode", default=defaults.get("action_security_mode", SECURITY_MODE_AUTO))
        ] = self._security_selector()
        schema[vol.Optional("action_visual_match", default=defaults.get("action_visual_match", False))] = BooleanSelector()
        schema[vol.Optional("action_camera_feed", default=defaults.get("action_camera_feed", False))] = BooleanSelector()
        schema[vol.Optional("max_per_session", default=defaults.get("max_per_session", 0))] = NumberSelector(
            NumberSelectorConfig(min=0, max=50, step=1, mode=NumberSelectorMode.BOX)
        )
        schema[vol.Optional("cooldown_seconds", default=defaults.get("cooldown_seconds", 0))] = NumberSelector(
            NumberSelectorConfig(min=0, max=300, step=5, mode=NumberSelectorMode.BOX)
        )
        schema[vol.Optional("validator_prompt", default=defaults.get("validator_prompt", ""))] = TextSelector(
            TextSelectorConfig(multiline=True)
        )
        return schema

    def _build_action_from_input(self, user_input: dict[str, Any]) -> EntityAction:
        steps = [config for config in (self._normalize_action_config(user_input.get(field)) for field in _ACTION_STEP_FIELDS) if config]
        first_step = deepcopy(steps[0]) if steps else {}
        service = first_step.get("action") or first_step.get("service") or ""
        return EntityAction(
            id=self._slugify(user_input["action_id"]),
            name=user_input["action_name"],
            description=user_input.get("action_description", ""),
            service=service,
            service_data=first_step,
            steps=steps if len(steps) > 1 else [],
            security_mode=user_input.get("action_security_mode", SECURITY_MODE_AUTO),
            require_visual_match=user_input.get("action_visual_match", False),
            require_camera_feed=user_input.get("action_camera_feed", False),
            max_per_session=int(user_input.get("max_per_session", 0)),
            cooldown_seconds=float(user_input.get("cooldown_seconds", 0)),
            validator_prompt=user_input.get("validator_prompt", ""),
        )

    def _action_defaults(self, action: EntityAction, entity_id: str) -> dict[str, Any]:
        step_configs = [deepcopy(config) for config in (action.steps or [self._selector_config_from_action(action, entity_id)]) if config]
        target_type = "entity"
        target_automation = ""
        if step_configs:
            first = step_configs[0]
            if first.get("action") == "automation.trigger":
                target = first.get("target") or {}
                automation_id = target.get("entity_id")
                if isinstance(automation_id, str) and automation_id.startswith("automation."):
                    target_type = "automation"
                    target_automation = automation_id
        defaults: dict[str, Any] = {
            "action_target_type": target_type,
            "target_entity": entity_id,
            "target_automation": target_automation,
            "action_id": action.id,
            "action_name": action.name,
            "action_description": action.description,
            "action_security_mode": action.security_mode,
            "action_visual_match": action.require_visual_match,
            "action_camera_feed": action.require_camera_feed,
            "max_per_session": action.max_per_session,
            "cooldown_seconds": action.cooldown_seconds,
            "validator_prompt": action.validator_prompt,
            "action_config": step_configs[0] if step_configs else {},
        }
        for index, field in enumerate(_ACTION_STEP_FIELDS[1:], start=1):
            defaults[field] = step_configs[index] if index < len(step_configs) else {}
        return defaults

    def _selector_config_from_action(self, action: EntityAction, entity_id: str) -> dict[str, Any]:
        config = deepcopy(action.service_data or {})
        if any(key in config for key in ("action", "service", "target", "data")):
            config.setdefault("action", config.get("service") or action.service)
            config.pop("service", None)
            return config
        data = deepcopy(config)
        target: dict[str, Any] = {}
        service_entity_id = data.pop("entity_id", None)
        if service_entity_id and service_entity_id != entity_id:
            target["entity_id"] = service_entity_id
        selector_config: dict[str, Any] = {"action": action.service}
        if target:
            selector_config["target"] = target
        if data:
            selector_config["data"] = data
        return selector_config

    def _normalize_action_config(self, config: Any) -> dict[str, Any]:
        if not isinstance(config, dict):
            return {}
        action_name = config.get("action") or config.get("service")
        target = deepcopy(config.get("target") or {})
        data = deepcopy(config.get("data") or {})
        if not action_name and not target and not data:
            return {}
        result: dict[str, Any] = {}
        if action_name:
            result["action"] = action_name
        if target:
            result["target"] = target
        if data:
            result["data"] = data
        return result

    def _managed_entity_options(self, store: DataStore) -> list[dict[str, str]]:
        return [
            {"value": entity.entity_id, "label": f"{entity.name} ({entity.entity_id})"}
            for entity in store.managed_entities
            if entity.entity_id != GLOBAL_ACTIONS_ENTITY_ID
        ]

    def _action_options(self, store: DataStore) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        for entity in store.managed_entities:
            entity_label = GLOBAL_ACTIONS_ENTITY_NAME if entity.entity_id == GLOBAL_ACTIONS_ENTITY_ID else entity.name
            for action in entity.actions:
                options.append(
                    {
                        "value": self._encode_action_ref(entity.entity_id, action.id),
                        "label": f"{action.name} ({action.id}) — {entity_label}",
                    }
                )
        return options

    async def _ensure_global_actions_entity(self, store: DataStore) -> ManagedEntity:
        entity = store.get_entity(GLOBAL_ACTIONS_ENTITY_ID)
        if entity is None:
            entity = ManagedEntity(
                entity_id=GLOBAL_ACTIONS_ENTITY_ID,
                name=GLOBAL_ACTIONS_ENTITY_NAME,
                description="Automation triggers available to the AI.",
            )
            await store.async_add_entity(entity)
        return entity

    def _encode_action_ref(self, entity_id: str, action_id: str) -> str:
        return f"{entity_id}::{action_id}"

    def _decode_action_ref(self, action_ref: str) -> tuple[str, str]:
        if "::" not in action_ref:
            return action_ref, action_ref
        return tuple(action_ref.split("::", 1))  # type: ignore[return-value]

    def _security_selector(self) -> SelectSelector:
        return SelectSelector(
            SelectSelectorConfig(
                options=[{"value": mode, "label": mode.replace("_", " ").title()} for mode in SECURITY_MODES],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

    def _slugify(self, text: str) -> str:
        slug = text.lower().replace(" ", "_").replace("-", "_")
        slug = re.sub(r"[^a-z0-9_]+", "_", slug)
        slug = re.sub(r"_+", "_", slug).strip("_")
        if not slug:
            slug = "action"
        if slug[0].isdigit():
            slug = f"action_{slug}"
        return slug

    async def _download_image_as_base64(self, url: str) -> str | None:
        import aiohttp  # noqa: PLC0415
        import base64  # noqa: PLC0415

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return base64.b64encode(await resp.read()).decode("ascii")
        except Exception:
            _LOGGER.warning("Failed to download image from %s", url)
        return None

    async def _read_local_image_as_base64(self, path: str) -> str | None:
        import base64  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        try:
            file_path = Path(path)
            if file_path.exists() and file_path.stat().st_size < 5_000_000:
                return base64.b64encode(await self.hass.async_add_executor_job(file_path.read_bytes)).decode("ascii")
        except Exception:
            _LOGGER.warning("Failed to read local image: %s", path)
        return None
