"""Config flow for Doorbell Jeeves integration.

Supports two setup paths:
  1. Reolink Quick Setup – auto-detects Reolink doorbell and configures go2rtc
  2. Manual Setup – user provides camera, audio input/output settings

Post-setup configuration is handled via options flow with section-based menus.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
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
    AUDIO_MODE_MANUAL,
    AUDIO_MODE_REOLINK,
    AUDIO_MODES,
    AUDIO_OUTPUT_EVENT,
    AUDIO_OUTPUT_GO2RTC,
    AUDIO_OUTPUT_MEDIA_PLAYER,
    AUDIO_OUTPUT_MODES,
    CONF_API_BASE_URL,
    CONF_API_KEY,
    CONF_AUDIO_MODE,
    CONF_AUDIO_OUTPUT_MODE,
    CONF_CAMERA_ENTITY,
    CONF_DEFAULT_SECURITY_MODE,
    CONF_DUAL_MODEL_ENABLED,
    CONF_FACE_SENSOR_ENTITY,
    CONF_FRAME_MAX_HEIGHT,
    CONF_FRAME_MAX_WIDTH,
    CONF_FRAME_QUALITY,
    CONF_GO2RTC_STREAM_NAME,
    CONF_IDENTITY_MODE,
    CONF_MEDIA_PLAYER_ENTITY,
    CONF_MODEL,
    CONF_PIN_CODE,
    CONF_PROVIDER,
    CONF_REOLINK_ENTRY_ID,
    CONF_SESSION_TIMEOUT,
    CONF_STOP_ENTITIES,
    CONF_STOP_ENTITY_STATES,
    CONF_SYSTEM_PROMPT,
    CONF_TOOL_API_KEY,
    CONF_TOOL_BASE_URL,
    CONF_TOOL_MODEL,
    CONF_TOOL_PROVIDER,
    CONF_VALIDATOR_MODEL,
    CONF_VISION_FPS,
    CONF_VOICE,
    DEFAULT_FRAME_MAX_HEIGHT,
    DEFAULT_FRAME_MAX_WIDTH,
    DEFAULT_FRAME_QUALITY,
    DEFAULT_MODEL_GEMINI,
    DEFAULT_MODEL_OPENAI,
    DEFAULT_SESSION_TIMEOUT,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TOOL_MODEL_GEMINI,
    DEFAULT_TOOL_MODEL_OPENAI,
    DEFAULT_VALIDATOR_MODEL,
    DEFAULT_VISION_FPS,
    DEFAULT_VOICE_GEMINI,
    DEFAULT_VOICE_OPENAI,
    DOMAIN,
    IDENTITY_MODE_BOTH,
    IDENTITY_MODE_NONE,
    IDENTITY_MODE_REFERENCE_IMAGES,
    IDENTITY_MODE_SENSOR,
    IDENTITY_MODES,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    PROVIDERS,
    SECURITY_MODE_AUTO,
    SECURITY_MODES,
)

_LOGGER = logging.getLogger(__name__)


# ─── Config Flow (Initial Setup) ─────────────────────────────────────────────


class DoorbellJeevesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle initial setup of Doorbell Jeeves."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow handler."""
        return DoorbellJeevesOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 1: Choose setup mode (Reolink or Manual)."""
        if user_input is not None:
            self._data[CONF_AUDIO_MODE] = user_input[CONF_AUDIO_MODE]
            if user_input[CONF_AUDIO_MODE] == AUDIO_MODE_REOLINK:
                return await self.async_step_reolink()
            return await self.async_step_provider()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_AUDIO_MODE, default=AUDIO_MODE_REOLINK): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": AUDIO_MODE_REOLINK, "label": "Reolink Doorbell (recommended)"},
                            {"value": AUDIO_MODE_MANUAL, "label": "Manual Setup"},
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }),
            description_placeholders={
                "reolink_description": "Auto-configures 2-way audio using your existing Reolink integration.",
            },
        )

    async def async_step_reolink(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 2a: Reolink auto-detection – select from detected Reolink cameras."""
        errors: dict[str, str] = {}

        # Find Reolink config entries
        reolink_entries = [
            entry for entry in self.hass.config_entries.async_entries("reolink")
            if entry.state == config_entries.ConfigEntryState.LOADED
        ]

        if not reolink_entries:
            errors["base"] = "no_reolink_found"
            return self.async_show_form(
                step_id="reolink",
                data_schema=vol.Schema({}),
                errors=errors,
                description_placeholders={
                    "error_msg": "No active Reolink integrations found. Please set up a Reolink device first.",
                },
            )

        # Find camera entities from Reolink entries
        registry = er.async_get(self.hass)
        reolink_cameras: list[dict[str, str]] = []
        for entry in reolink_entries:
            entities = er.async_entries_for_config_entry(registry, entry.entry_id)
            for entity in entities:
                if entity.domain == "camera" and not entity.disabled:
                    reolink_cameras.append({
                        "value": entity.entity_id,
                        "label": f"{entity.original_name or entity.entity_id} ({entry.title})",
                    })

        if user_input is not None:
            camera_id = user_input[CONF_CAMERA_ENTITY]
            # Find which Reolink entry this camera belongs to
            entity_entry = registry.async_get(camera_id)
            if entity_entry:
                self._data[CONF_CAMERA_ENTITY] = camera_id
                self._data[CONF_REOLINK_ENTRY_ID] = entity_entry.config_entry_id
                self._data[CONF_AUDIO_OUTPUT_MODE] = AUDIO_OUTPUT_GO2RTC

                # Find the doorbell binary sensor for start trigger
                triggers = er.async_entries_for_config_entry(registry, entity_entry.config_entry_id)
                doorbell_sensor = None
                for t in triggers:
                    if "visitor" in (t.entity_id or "") or "button" in (t.entity_id or ""):
                        doorbell_sensor = t.entity_id
                        break
                if doorbell_sensor:
                    self._data["doorbell_trigger_entity"] = doorbell_sensor

            return await self.async_step_provider()

        return self.async_show_form(
            step_id="reolink",
            data_schema=vol.Schema({
                vol.Required(CONF_CAMERA_ENTITY): SelectSelector(
                    SelectSelectorConfig(
                        options=reolink_cameras or [{"value": "", "label": "No cameras found"}],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
            }),
        )

    async def async_step_provider(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 3: AI provider configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            provider = user_input[CONF_PROVIDER]
            api_key = user_input[CONF_API_KEY]

            # Validate API key
            valid = await self._validate_api_key(provider, api_key, user_input.get(CONF_API_BASE_URL))
            if not valid:
                errors["base"] = "invalid_api_key"
            else:
                self._data.update(user_input)
                return await self.async_step_camera()

        provider = self._data.get(CONF_PROVIDER, PROVIDER_GEMINI)
        default_model = DEFAULT_MODEL_GEMINI if provider == PROVIDER_GEMINI else DEFAULT_MODEL_OPENAI
        default_voice = DEFAULT_VOICE_GEMINI if provider == PROVIDER_GEMINI else DEFAULT_VOICE_OPENAI

        return self.async_show_form(
            step_id="provider",
            data_schema=vol.Schema({
                vol.Required(CONF_PROVIDER, default=PROVIDER_GEMINI): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": PROVIDER_GEMINI, "label": "Google Gemini"},
                            {"value": PROVIDER_OPENAI, "label": "OpenAI / Compatible"},
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_API_KEY): TextSelector(
                    TextSelectorConfig(type="password")
                ),
                vol.Optional(CONF_API_BASE_URL, default=""): TextSelector(
                    TextSelectorConfig(type="url")
                ),
                vol.Required(CONF_MODEL, default=default_model): TextSelector(),
                vol.Required(CONF_VOICE, default=default_voice): TextSelector(),
            }),
            errors=errors,
        )

    async def async_step_camera(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 4: Camera & vision settings (skipped for Reolink if already set)."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_prompt()

        # For Reolink mode, camera is already selected — just show vision config
        has_camera = bool(self._data.get(CONF_CAMERA_ENTITY))

        schema_fields: dict[Any, Any] = {}
        if not has_camera:
            schema_fields[vol.Required(CONF_CAMERA_ENTITY, default="")] = EntitySelector(
                EntitySelectorConfig(domain="camera")
            )
            schema_fields[vol.Required(CONF_AUDIO_OUTPUT_MODE, default=AUDIO_OUTPUT_EVENT)] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"value": AUDIO_OUTPUT_MEDIA_PLAYER, "label": "Media Player (HA speaker)"},
                        {"value": AUDIO_OUTPUT_EVENT, "label": "Event (custom handling)"},
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
            schema_fields[vol.Optional(CONF_MEDIA_PLAYER_ENTITY, default="")] = EntitySelector(
                EntitySelectorConfig(domain="media_player")
            )

        schema_fields[vol.Required(CONF_VISION_FPS, default=DEFAULT_VISION_FPS)] = NumberSelector(
            NumberSelectorConfig(min=0.1, max=10.0, step=0.1, mode=NumberSelectorMode.SLIDER)
        )
        schema_fields[vol.Required(CONF_FRAME_MAX_WIDTH, default=DEFAULT_FRAME_MAX_WIDTH)] = NumberSelector(
            NumberSelectorConfig(min=160, max=1920, step=80, mode=NumberSelectorMode.SLIDER)
        )
        schema_fields[vol.Required(CONF_FRAME_MAX_HEIGHT, default=DEFAULT_FRAME_MAX_HEIGHT)] = NumberSelector(
            NumberSelectorConfig(min=120, max=1080, step=60, mode=NumberSelectorMode.SLIDER)
        )
        schema_fields[vol.Required(CONF_FRAME_QUALITY, default=DEFAULT_FRAME_QUALITY)] = NumberSelector(
            NumberSelectorConfig(min=10, max=100, step=5, mode=NumberSelectorMode.SLIDER)
        )

        return self.async_show_form(
            step_id="camera",
            data_schema=vol.Schema(schema_fields),
        )

    async def async_step_prompt(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Step 5: System prompt."""
        if user_input is not None:
            self._data[CONF_SYSTEM_PROMPT] = user_input[CONF_SYSTEM_PROMPT]
            self._data[CONF_SESSION_TIMEOUT] = user_input.get(CONF_SESSION_TIMEOUT, DEFAULT_SESSION_TIMEOUT)
            return self._create_entry()

        return self.async_show_form(
            step_id="prompt",
            data_schema=vol.Schema({
                vol.Required(CONF_SYSTEM_PROMPT, default=DEFAULT_SYSTEM_PROMPT): TextSelector(
                    TextSelectorConfig(multiline=True, type="text")
                ),
                vol.Required(CONF_SESSION_TIMEOUT, default=DEFAULT_SESSION_TIMEOUT): NumberSelector(
                    NumberSelectorConfig(min=10, max=600, step=10, mode=NumberSelectorMode.SLIDER, unit_of_measurement="s")
                ),
            }),
        )

    def _create_entry(self) -> FlowResult:
        """Create the config entry with all collected data."""
        camera = self._data.get(CONF_CAMERA_ENTITY, "")
        title = f"Jeeves ({camera})" if camera else "Doorbell Jeeves"
        return self.async_create_entry(title=title, data=self._data)

    async def _validate_api_key(self, provider: str, api_key: str, base_url: str | None = None) -> bool:
        """Validate the API key by making a simple request.
        
        Note: genai.Client() and the import itself do blocking I/O (SSL cert loading,
        file system reads). We must run everything in an executor to avoid event loop warnings.
        """
        if not api_key:
            return False
        try:
            if provider == PROVIDER_GEMINI:
                def _validate_gemini() -> bool:
                    from google import genai  # noqa: PLC0415
                    client = genai.Client(api_key=api_key)
                    models = list(client.models.list())
                    return len(models) > 0

                return await self.hass.async_add_executor_job(_validate_gemini)
            else:
                import openai  # noqa: PLC0415
                kwargs: dict[str, Any] = {"api_key": api_key}
                if base_url:
                    kwargs["base_url"] = base_url
                client = openai.AsyncOpenAI(**kwargs)
                await client.models.list()
                return True
        except Exception as err:
            _LOGGER.debug("API key validation failed: %s", err)
            return False


# ─── Options Flow (Post-Setup Configuration) ─────────────────────────────────


class DoorbellJeevesOptionsFlow(OptionsFlow):
    """Handle options for Doorbell Jeeves."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        self._config_entry = config_entry
        self._data: dict[str, Any] = dict(config_entry.data) | dict(config_entry.options)
        self._entity_edit: dict[str, Any] = {}
        self._action_edit: dict[str, Any] = {}
        self._identity_edit: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Options menu – choose which section to configure."""
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "general",
                "dual_model",
                "vision",
                "entities",
                "security",
                "triggers",
                "identities",
                "prompt",
            ],
        )

    # ─── General Settings ─────────────────────────────────────────────────────

    async def async_step_general(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """General AI settings."""
        if user_input is not None:
            self._data.update(user_input)
            return self._save_options()

        provider = self._data.get(CONF_PROVIDER, PROVIDER_GEMINI)
        default_model = self._data.get(CONF_MODEL, DEFAULT_MODEL_GEMINI if provider == PROVIDER_GEMINI else DEFAULT_MODEL_OPENAI)
        default_voice = self._data.get(CONF_VOICE, DEFAULT_VOICE_GEMINI if provider == PROVIDER_GEMINI else DEFAULT_VOICE_OPENAI)

        return self.async_show_form(
            step_id="general",
            data_schema=vol.Schema({
                vol.Required(CONF_PROVIDER, default=self._data.get(CONF_PROVIDER, PROVIDER_GEMINI)): SelectSelector(
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
                vol.Required(CONF_MODEL, default=default_model): TextSelector(),
                vol.Required(CONF_VOICE, default=default_voice): TextSelector(),
                vol.Required(CONF_SESSION_TIMEOUT, default=self._data.get(CONF_SESSION_TIMEOUT, DEFAULT_SESSION_TIMEOUT)): NumberSelector(
                    NumberSelectorConfig(min=10, max=600, step=10, mode=NumberSelectorMode.SLIDER, unit_of_measurement="s")
                ),
                vol.Optional(CONF_VALIDATOR_MODEL, default=self._data.get(CONF_VALIDATOR_MODEL, DEFAULT_VALIDATOR_MODEL)): TextSelector(),
            }),
        )

    # ─── Dual Model Settings ──────────────────────────────────────────────────

    async def async_step_dual_model(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Configure dual-model architecture (separate voice + tool-calling model).
        
        Native audio models like gemini-2.5-flash-native-audio-dialog don't support
        function calling. This option enables a separate text model for tool calling.
        """
        if user_input is not None:
            self._data.update(user_input)
            return self._save_options()

        tool_provider = self._data.get(CONF_TOOL_PROVIDER, self._data.get(CONF_PROVIDER, PROVIDER_GEMINI))
        default_tool_model = (
            DEFAULT_TOOL_MODEL_GEMINI if tool_provider == PROVIDER_GEMINI else DEFAULT_TOOL_MODEL_OPENAI
        )

        return self.async_show_form(
            step_id="dual_model",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_DUAL_MODEL_ENABLED,
                    default=self._data.get(CONF_DUAL_MODEL_ENABLED, False),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_TOOL_PROVIDER,
                    default=tool_provider,
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": PROVIDER_GEMINI, "label": "Google Gemini"},
                            {"value": PROVIDER_OPENAI, "label": "OpenAI / Compatible"},
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_TOOL_MODEL,
                    default=self._data.get(CONF_TOOL_MODEL, default_tool_model),
                ): TextSelector(),
                vol.Optional(
                    CONF_TOOL_API_KEY,
                    default=self._data.get(CONF_TOOL_API_KEY, ""),
                    description={"suffix": "(leave blank to use same as voice model)"},
                ): TextSelector(TextSelectorConfig(type="password")),
                vol.Optional(
                    CONF_TOOL_BASE_URL,
                    default=self._data.get(CONF_TOOL_BASE_URL, ""),
                ): TextSelector(TextSelectorConfig(type="url")),
            }),
            description_placeholders={
                "info": (
                    "Native audio models (gemini-2.5-flash-native-audio-dialog) do NOT "
                    "support function calling. Enable dual-model to use a separate text "
                    "model for tool execution while the audio model handles conversation."
                ),
            },
        )

    # ─── Vision Settings ──────────────────────────────────────────────────────

    async def async_step_vision(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Camera and vision configuration."""
        if user_input is not None:
            self._data.update(user_input)
            return self._save_options()

        return self.async_show_form(
            step_id="vision",
            data_schema=vol.Schema({
                vol.Required(CONF_CAMERA_ENTITY, default=self._data.get(CONF_CAMERA_ENTITY, "")): EntitySelector(
                    EntitySelectorConfig(domain="camera")
                ),
                vol.Required(CONF_VISION_FPS, default=self._data.get(CONF_VISION_FPS, DEFAULT_VISION_FPS)): NumberSelector(
                    NumberSelectorConfig(min=0.1, max=10.0, step=0.1, mode=NumberSelectorMode.SLIDER)
                ),
                vol.Required(CONF_FRAME_MAX_WIDTH, default=self._data.get(CONF_FRAME_MAX_WIDTH, DEFAULT_FRAME_MAX_WIDTH)): NumberSelector(
                    NumberSelectorConfig(min=160, max=1920, step=80, mode=NumberSelectorMode.SLIDER)
                ),
                vol.Required(CONF_FRAME_MAX_HEIGHT, default=self._data.get(CONF_FRAME_MAX_HEIGHT, DEFAULT_FRAME_MAX_HEIGHT)): NumberSelector(
                    NumberSelectorConfig(min=120, max=1080, step=60, mode=NumberSelectorMode.SLIDER)
                ),
                vol.Required(CONF_FRAME_QUALITY, default=self._data.get(CONF_FRAME_QUALITY, DEFAULT_FRAME_QUALITY)): NumberSelector(
                    NumberSelectorConfig(min=10, max=100, step=5, mode=NumberSelectorMode.SLIDER)
                ),
            }),
        )

    # ─── Entity Management ────────────────────────────────────────────────────

    async def async_step_entities(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Entity management menu."""
        from .store import DataStore  # noqa: PLC0415
        store = DataStore(self.hass, self._config_entry.entry_id)
        await store.async_load()

        entity_list = [
            f"{e.name} ({e.entity_id}) - {len(e.actions)} action(s)"
            for e in store.managed_entities
        ]
        notif_list = [
            f"{n.name} ({n.service})"
            for n in store.notification_targets
        ]

        # Categorize entities by type
        cameras = [e for e in store.managed_entities if e.entity_id.startswith("camera.")]
        calendars = [e for e in store.managed_entities if e.entity_id.startswith("calendar.")]
        others = [e for e in store.managed_entities
                  if not e.entity_id.startswith("camera.") and not e.entity_id.startswith("calendar.")]

        desc = ""
        if cameras:
            desc += "**📷 Cameras (AI can view on demand):**\n"
            desc += "\n".join(f"• {e.name}" for e in cameras) + "\n\n"
        if calendars:
            desc += "**📅 Calendars (AI can check schedules):**\n"
            desc += "\n".join(f"• {e.name}" for e in calendars) + "\n\n"
        if others:
            desc += "**🏠 Other Entities:**\n"
            desc += "\n".join(f"• {e.name} - {len(e.actions)} action(s)" for e in others) + "\n\n"
        if notif_list:
            desc += "**🔔 Notifications:**\n"
            desc += "\n".join(f"• {n}" for n in notif_list)
        if not desc:
            desc = "No entities configured yet. Add cameras, calendars, and devices below."

        return self.async_show_menu(
            step_id="entities",
            menu_options=[
                "add_camera",
                "add_calendar",
                "add_entity",
                "add_action",
                "add_notification",
                "remove_entity",
                "remove_notification",
                "init",
            ],
            description_placeholders={"entity_summary": desc},
        )

    async def async_step_add_camera(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Add a camera for on-demand AI viewing."""
        if user_input is not None:
            from .models import ManagedEntity  # noqa: PLC0415
            from .store import DataStore  # noqa: PLC0415

            store = DataStore(self.hass, self._config_entry.entry_id)
            await store.async_load()

            entity = ManagedEntity(
                entity_id=user_input["entity_id"],
                name=user_input["name"],
                description=user_input.get("description", "Camera the AI can view on demand"),
            )
            await store.async_add_entity(entity)
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="add_camera",
            data_schema=vol.Schema({
                vol.Required("entity_id"): EntitySelector(EntitySelectorConfig(domain="camera")),
                vol.Required("name"): TextSelector(),
                vol.Required("description", default="Camera feed the AI can view to check this area"): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
            }),
        )

    async def async_step_add_calendar(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Add a calendar for schedule queries."""
        if user_input is not None:
            from .models import ManagedEntity  # noqa: PLC0415
            from .store import DataStore  # noqa: PLC0415

            store = DataStore(self.hass, self._config_entry.entry_id)
            await store.async_load()

            entity = ManagedEntity(
                entity_id=user_input["entity_id"],
                name=user_input["name"],
                description=user_input.get("description", "Calendar for checking availability"),
            )
            await store.async_add_entity(entity)
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="add_calendar",
            data_schema=vol.Schema({
                vol.Required("entity_id"): EntitySelector(EntitySelectorConfig(domain="calendar")),
                vol.Required("name"): TextSelector(),
                vol.Required("description", default="Owner's calendar — use to check if they are available"): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
            }),
        )

    async def async_step_add_entity(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Add a new managed entity."""
        if user_input is not None:
            from .models import ManagedEntity  # noqa: PLC0415
            from .store import DataStore  # noqa: PLC0415

            store = DataStore(self.hass, self._config_entry.entry_id)
            await store.async_load()

            entity = ManagedEntity(
                entity_id=user_input["entity_id"],
                name=user_input["name"],
                description=user_input["description"],
                security_mode=user_input.get("security_mode", SECURITY_MODE_AUTO),
                require_visual_match=user_input.get("require_visual_match", False),
                require_camera_feed=user_input.get("require_camera_feed", False),
            )
            await store.async_add_entity(entity)
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="add_entity",
            data_schema=vol.Schema({
                vol.Required("entity_id"): EntitySelector(EntitySelectorConfig()),
                vol.Required("name"): TextSelector(),
                vol.Required("description"): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
                vol.Required("security_mode", default=SECURITY_MODE_AUTO): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": m, "label": m.replace("_", " ").title()}
                            for m in SECURITY_MODES
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional("require_visual_match", default=False): BooleanSelector(),
                vol.Optional("require_camera_feed", default=False): BooleanSelector(),
            }),
        )

    async def async_step_remove_entity(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Remove a managed entity."""
        from .store import DataStore  # noqa: PLC0415

        store = DataStore(self.hass, self._config_entry.entry_id)
        await store.async_load()

        if user_input is not None:
            entity_id = user_input["entity_id"]
            await store.async_remove_entity(entity_id)
            return await self.async_step_entities()

        options = [
            {"value": e.entity_id, "label": f"{e.name} ({e.entity_id})"}
            for e in store.managed_entities
        ]
        if not options:
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="remove_entity",
            data_schema=vol.Schema({
                vol.Required("entity_id"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                ),
            }),
        )

    async def async_step_add_action(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Add an action to a managed entity."""
        from .models import EntityAction  # noqa: PLC0415
        from .store import DataStore  # noqa: PLC0415

        store = DataStore(self.hass, self._config_entry.entry_id)
        await store.async_load()

        if user_input is not None:
            entity_id = user_input["target_entity"]
            entity = store.get_entity(entity_id)
            if entity:
                action_id = user_input["action_id"].lower().replace(" ", "_").replace("-", "_")
                action = EntityAction(
                    id=action_id,
                    name=user_input["action_name"],
                    description=user_input["action_description"],
                    service=user_input["service"],
                    service_data={},
                    security_mode=user_input.get("action_security_mode", SECURITY_MODE_AUTO),
                    require_visual_match=user_input.get("action_visual_match", False),
                    require_camera_feed=user_input.get("action_camera_feed", False),
                    max_per_session=int(user_input.get("max_per_session", 0)),
                    cooldown_seconds=float(user_input.get("cooldown_seconds", 0)),
                    validator_prompt=user_input.get("validator_prompt", ""),
                )
                entity.actions.append(action)
                await store.async_add_entity(entity)
            return await self.async_step_entities()

        entity_options = [
            {"value": e.entity_id, "label": f"{e.name} ({e.entity_id})"}
            for e in store.managed_entities
        ]
        if not entity_options:
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="add_action",
            data_schema=vol.Schema({
                vol.Required("target_entity"): SelectSelector(
                    SelectSelectorConfig(options=entity_options, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Required("action_id"): TextSelector(),
                vol.Required("action_name"): TextSelector(),
                vol.Required("action_description"): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Required("service"): TextSelector(
                    TextSelectorConfig(type="text")
                ),
                vol.Required("action_security_mode", default=SECURITY_MODE_AUTO): SelectSelector(
                    SelectSelectorConfig(
                        options=[{"value": m, "label": m.replace("_", " ").title()} for m in SECURITY_MODES],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional("action_visual_match", default=False): BooleanSelector(),
                vol.Optional("action_camera_feed", default=False): BooleanSelector(),
                vol.Optional("max_per_session", default=0): NumberSelector(
                    NumberSelectorConfig(min=0, max=50, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional("cooldown_seconds", default=0): NumberSelector(
                    NumberSelectorConfig(min=0, max=300, step=5, mode=NumberSelectorMode.BOX, unit_of_measurement="s")
                ),
                vol.Optional("validator_prompt", default=""): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
            }),
        )

    async def async_step_add_notification(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Add a notification target."""
        from .models import NotificationTarget  # noqa: PLC0415
        from .store import DataStore  # noqa: PLC0415

        if user_input is not None:
            store = DataStore(self.hass, self._config_entry.entry_id)
            await store.async_load()
            target = NotificationTarget(
                service=user_input["service"],
                name=user_input["name"],
                description=user_input["description"],
            )
            await store.async_add_notification(target)
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="add_notification",
            data_schema=vol.Schema({
                vol.Required("service"): TextSelector(),
                vol.Required("name"): TextSelector(),
                vol.Required("description"): TextSelector(TextSelectorConfig(multiline=True)),
            }),
        )

    async def async_step_remove_notification(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Remove a notification target."""
        from .store import DataStore  # noqa: PLC0415

        store = DataStore(self.hass, self._config_entry.entry_id)
        await store.async_load()

        if user_input is not None:
            await store.async_remove_notification(user_input["service"])
            return await self.async_step_entities()

        options = [
            {"value": n.service, "label": f"{n.name} ({n.service})"}
            for n in store.notification_targets
        ]
        if not options:
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="remove_notification",
            data_schema=vol.Schema({
                vol.Required("service"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                ),
            }),
        )

    # ─── Security Settings ────────────────────────────────────────────────────

    async def async_step_security(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Security configuration."""
        if user_input is not None:
            self._data.update(user_input)
            return self._save_options()

        return self.async_show_form(
            step_id="security",
            data_schema=vol.Schema({
                vol.Required(CONF_DEFAULT_SECURITY_MODE, default=self._data.get(CONF_DEFAULT_SECURITY_MODE, SECURITY_MODE_AUTO)): SelectSelector(
                    SelectSelectorConfig(
                        options=[{"value": m, "label": m.replace("_", " ").title()} for m in SECURITY_MODES],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_PIN_CODE, default=self._data.get(CONF_PIN_CODE, "")): TextSelector(
                    TextSelectorConfig(type="password")
                ),
                vol.Optional(CONF_VALIDATOR_MODEL, default=self._data.get(CONF_VALIDATOR_MODEL, DEFAULT_VALIDATOR_MODEL)): TextSelector(),
            }),
        )

    # ─── Triggers ─────────────────────────────────────────────────────────────

    async def async_step_triggers(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Start/stop trigger and human takeover configuration."""
        if user_input is not None:
            # Parse start triggers
            start_entities = user_input.get("start_entities", [])
            start_triggers = []
            for eid in start_entities:
                start_triggers.append({"entity_id": eid, "to_state": "on"})
            self._data["start_triggers_config"] = start_triggers

            # Stop triggers
            stop_entities = user_input.get(CONF_STOP_ENTITIES, [])
            self._data[CONF_STOP_ENTITIES] = stop_entities

            # Human takeover detection methods
            from .const import (  # noqa: PLC0415
                CONF_TAKEOVER_REOLINK_API,
                CONF_TAKEOVER_AUDIO_ENERGY,
                CONF_TAKEOVER_ENERGY_THRESHOLD,
                CONF_TAKEOVER_POLL_INTERVAL,
                DEFAULT_TAKEOVER_ENERGY_THRESHOLD,
                DEFAULT_TAKEOVER_POLL_INTERVAL,
            )
            self._data[CONF_TAKEOVER_REOLINK_API] = user_input.get(CONF_TAKEOVER_REOLINK_API, True)
            self._data[CONF_TAKEOVER_AUDIO_ENERGY] = user_input.get(CONF_TAKEOVER_AUDIO_ENERGY, False)
            self._data[CONF_TAKEOVER_ENERGY_THRESHOLD] = user_input.get(
                CONF_TAKEOVER_ENERGY_THRESHOLD, DEFAULT_TAKEOVER_ENERGY_THRESHOLD
            )
            self._data[CONF_TAKEOVER_POLL_INTERVAL] = user_input.get(
                CONF_TAKEOVER_POLL_INTERVAL, DEFAULT_TAKEOVER_POLL_INTERVAL
            )

            return self._save_options()

        # Get current values
        from .const import (  # noqa: PLC0415
            CONF_TAKEOVER_REOLINK_API,
            CONF_TAKEOVER_AUDIO_ENERGY,
            CONF_TAKEOVER_ENERGY_THRESHOLD,
            CONF_TAKEOVER_POLL_INTERVAL,
            DEFAULT_TAKEOVER_ENERGY_THRESHOLD,
            DEFAULT_TAKEOVER_POLL_INTERVAL,
        )

        current_start = self._data.get("start_triggers_config", [])
        start_entity_ids = [t["entity_id"] for t in current_start] if current_start else []
        is_reolink = self._data.get(CONF_AUDIO_MODE) == AUDIO_MODE_REOLINK

        schema_fields: dict[Any, Any] = {
            vol.Optional("start_entities", default=start_entity_ids): EntitySelector(
                EntitySelectorConfig(domain=["binary_sensor", "input_boolean"], multiple=True)
            ),
            vol.Optional(CONF_STOP_ENTITIES, default=self._data.get(CONF_STOP_ENTITIES, [])): EntitySelector(
                EntitySelectorConfig(multiple=True)
            ),
        }

        # Human takeover detection section
        if is_reolink:
            schema_fields[vol.Optional(
                CONF_TAKEOVER_REOLINK_API,
                default=self._data.get(CONF_TAKEOVER_REOLINK_API, True),
            )] = BooleanSelector()

        schema_fields[vol.Optional(
            CONF_TAKEOVER_AUDIO_ENERGY,
            default=self._data.get(CONF_TAKEOVER_AUDIO_ENERGY, False),
        )] = BooleanSelector()

        schema_fields[vol.Optional(
            CONF_TAKEOVER_ENERGY_THRESHOLD,
            default=self._data.get(CONF_TAKEOVER_ENERGY_THRESHOLD, DEFAULT_TAKEOVER_ENERGY_THRESHOLD),
        )] = NumberSelector(
            NumberSelectorConfig(min=500, max=10000, step=100, mode=NumberSelectorMode.SLIDER)
        )

        if is_reolink:
            schema_fields[vol.Optional(
                CONF_TAKEOVER_POLL_INTERVAL,
                default=self._data.get(CONF_TAKEOVER_POLL_INTERVAL, DEFAULT_TAKEOVER_POLL_INTERVAL),
            )] = NumberSelector(
                NumberSelectorConfig(min=0.5, max=10.0, step=0.5, unit_of_measurement="s", mode=NumberSelectorMode.SLIDER)
            )

        return self.async_show_form(
            step_id="triggers",
            data_schema=vol.Schema(schema_fields),
        )

    # ─── Identity Management ──────────────────────────────────────────────────

    async def async_step_identities(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Identity management menu."""
        from .store import DataStore  # noqa: PLC0415

        store = DataStore(self.hass, self._config_entry.entry_id)
        await store.async_load()

        identity_list = [
            f"{i.name} ({i.identity_type}, {i.relationship})"
            for i in store.known_identities
        ]
        desc = "**Known Identities:**\n"
        if identity_list:
            desc += "\n".join(f"• {i}" for i in identity_list)
        else:
            desc += "None configured"

        return self.async_show_menu(
            step_id="identities",
            menu_options=[
                "add_identity",
                "remove_identity",
                "identity_settings",
                "init",
            ],
            description_placeholders={"identity_summary": desc},
        )

    async def async_step_add_identity(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Add a known identity."""
        from .models import KnownIdentity  # noqa: PLC0415
        from .store import DataStore  # noqa: PLC0415

        if user_input is not None:
            store = DataStore(self.hass, self._config_entry.entry_id)
            await store.async_load()

            # Handle image (URL or base64 — we store what's provided)
            image_data = user_input.get("reference_image", "").strip()
            image_b64: str | None = None
            if image_data:
                if image_data.startswith(("http://", "https://")):
                    # Download and convert to base64
                    image_b64 = await self._download_image_as_base64(image_data)
                elif image_data.startswith("/"):
                    # Local path
                    image_b64 = await self._read_local_image_as_base64(image_data)
                else:
                    # Assume already base64
                    image_b64 = image_data

            identity = KnownIdentity(
                name=user_input["name"],
                identity_type=user_input.get("identity_type", "person"),
                relationship=user_input.get("relationship", "guest"),
                description=user_input.get("description", ""),
                access_level=user_input.get("access_level", "guest"),
                image_base64=image_b64,
                notes=user_input.get("notes", ""),
            )
            await store.async_add_identity(identity)
            return await self.async_step_identities()

        return self.async_show_form(
            step_id="add_identity",
            data_schema=vol.Schema({
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
                vol.Required("description"): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
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
                vol.Optional("reference_image", default=""): TextSelector(
                    TextSelectorConfig(type="text")
                ),
                vol.Optional("notes", default=""): TextSelector(
                    TextSelectorConfig(multiline=True)
                ),
            }),
        )

    async def async_step_remove_identity(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Remove a known identity."""
        from .store import DataStore  # noqa: PLC0415

        store = DataStore(self.hass, self._config_entry.entry_id)
        await store.async_load()

        if user_input is not None:
            await store.async_remove_identity(user_input["name"])
            return await self.async_step_identities()

        options = [
            {"value": i.name, "label": f"{i.name} ({i.identity_type})"}
            for i in store.known_identities
        ]
        if not options:
            return await self.async_step_identities()

        return self.async_show_form(
            step_id="remove_identity",
            data_schema=vol.Schema({
                vol.Required("name"): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                ),
            }),
        )

    async def async_step_identity_settings(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Identity detection mode settings."""
        if user_input is not None:
            self._data.update(user_input)
            return self._save_options()

        return self.async_show_form(
            step_id="identity_settings",
            data_schema=vol.Schema({
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
                ),
                vol.Optional(CONF_FACE_SENSOR_ENTITY, default=self._data.get(CONF_FACE_SENSOR_ENTITY, "")): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
            }),
        )

    # ─── System Prompt ────────────────────────────────────────────────────────

    async def async_step_prompt(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Edit system prompt."""
        if user_input is not None:
            self._data[CONF_SYSTEM_PROMPT] = user_input[CONF_SYSTEM_PROMPT]
            return self._save_options()

        return self.async_show_form(
            step_id="prompt",
            data_schema=vol.Schema({
                vol.Required(CONF_SYSTEM_PROMPT, default=self._data.get(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT)): TextSelector(
                    TextSelectorConfig(multiline=True, type="text")
                ),
            }),
        )

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _save_options(self) -> FlowResult:
        """Save options and finish."""
        return self.async_create_entry(title="", data=self._data)

    async def _download_image_as_base64(self, url: str) -> str | None:
        """Download an image from URL and return base64."""
        import aiohttp  # noqa: PLC0415
        import base64  # noqa: PLC0415
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        return base64.b64encode(data).decode("ascii")
        except Exception:
            _LOGGER.warning("Failed to download image from %s", url)
        return None

    async def _read_local_image_as_base64(self, path: str) -> str | None:
        """Read a local image file and return base64."""
        import base64  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415
        try:
            file_path = Path(path)
            if file_path.exists() and file_path.stat().st_size < 5_000_000:
                data = await self.hass.async_add_executor_job(file_path.read_bytes)
                return base64.b64encode(data).decode("ascii")
        except Exception:
            _LOGGER.warning("Failed to read local image: %s", path)
        return None
