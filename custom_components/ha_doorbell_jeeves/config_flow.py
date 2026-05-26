"""Config flow for Doorbell Jeeves – multi-provider, flexible policies, auto-stop."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
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
    TextSelectorType,
)

from .const import (
    CONF_ACTION_POLICIES,
    CONF_ALLOWED_ENTITIES,
    CONF_API_BASE_URL,
    CONF_API_KEY,
    CONF_CAMERA_ENTITY,
    CONF_DEFAULT_SECURITY_MODE,
    CONF_FACE_SENSOR_ENTITY,
    CONF_FRAME_MAX_HEIGHT,
    CONF_FRAME_MAX_WIDTH,
    CONF_FRAME_QUALITY,
    CONF_IDENTITY_MODE,
    CONF_MEDIA_PLAYER_ENTITY,
    CONF_MODEL,
    CONF_NOTIFY_SERVICE,
    CONF_PIN_CODE,
    CONF_PROVIDER,
    CONF_SESSION_TIMEOUT,
    CONF_STOP_ENTITIES,
    CONF_STOP_ENTITY_STATES,
    CONF_STOP_EVENTS,
    CONF_SYSTEM_PROMPT,
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
    DEFAULT_VALIDATOR_MODEL,
    DEFAULT_VISION_FPS,
    DEFAULT_VOICE_GEMINI,
    DEFAULT_VOICE_OPENAI,
    DOMAIN,
    MODELS_GEMINI,
    MODELS_OPENAI,
    PROVIDER_GEMINI,
    PROVIDER_OPENAI,
    PROVIDERS,
    SECURITY_MODE_AUTO,
    SECURITY_MODE_PIN,
    SECURITY_MODE_PIN_AND_VALIDATED,
    SECURITY_MODE_VALIDATED,
    VOICES_GEMINI,
    VOICES_OPENAI,
)
from .tools import get_all_action_names

_LOGGER = logging.getLogger(__name__)


class DoorbellJeevesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Multi-step config flow.

    Steps:
      1. Provider & API credentials
      2. Camera, speaker, vision settings (FPS, frame size, quality)
      3. Entity allowlists
      4. Security policies
      5. Auto-stop triggers
      6. Identity & system prompt
    """

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DoorbellJeevesOptionsFlow(config_entry)

    # ─── Step 1: Provider & API ───────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            provider = user_input[CONF_PROVIDER]
            api_key = user_input[CONF_API_KEY]

            valid = await self._validate_credentials(provider, api_key, user_input.get(CONF_API_BASE_URL))
            if not valid:
                errors["base"] = "invalid_credentials"
            else:
                self._data.update(user_input)
                return await self.async_step_devices()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_PROVIDER, default=PROVIDER_GEMINI): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": PROVIDER_GEMINI, "label": "Google Gemini"},
                            {"value": PROVIDER_OPENAI, "label": "OpenAI / OpenAI-Compatible"},
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_API_KEY): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_API_BASE_URL, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                ),
                vol.Required(CONF_MODEL, default=DEFAULT_MODEL_GEMINI): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_VOICE, default=DEFAULT_VOICE_GEMINI): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }),
            errors=errors,
        )

    # ─── Step 2: Devices & Vision ─────────────────────────────────────────────

    async def async_step_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_entities()

        return self.async_show_form(
            step_id="devices",
            data_schema=vol.Schema({
                vol.Required(CONF_CAMERA_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="camera")
                ),
                vol.Required(CONF_MEDIA_PLAYER_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="media_player")
                ),
                vol.Optional(CONF_VISION_FPS, default=DEFAULT_VISION_FPS): NumberSelector(
                    NumberSelectorConfig(min=0.1, max=60.0, step=0.1, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_FRAME_MAX_WIDTH, default=DEFAULT_FRAME_MAX_WIDTH): NumberSelector(
                    NumberSelectorConfig(min=160, max=1920, step=10, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_FRAME_MAX_HEIGHT, default=DEFAULT_FRAME_MAX_HEIGHT): NumberSelector(
                    NumberSelectorConfig(min=120, max=1080, step=10, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_FRAME_QUALITY, default=DEFAULT_FRAME_QUALITY): NumberSelector(
                    NumberSelectorConfig(min=10, max=100, step=5, mode=NumberSelectorMode.SLIDER)
                ),
                vol.Optional(CONF_SESSION_TIMEOUT, default=DEFAULT_SESSION_TIMEOUT): NumberSelector(
                    NumberSelectorConfig(min=0, max=3600, step=10, mode=NumberSelectorMode.BOX, unit_of_measurement="s")
                ),
            }),
        )

    # ─── Step 3: Entity Allowlists ────────────────────────────────────────────

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data[CONF_ALLOWED_ENTITIES] = {
                "light": user_input.get("allowed_lights", []),
                "lock": user_input.get("allowed_locks", []),
                "switch": user_input.get("allowed_switches", []),
                "cover": user_input.get("allowed_covers", []),
                "sensor": user_input.get("allowed_sensors", []),
            }
            self._data[CONF_NOTIFY_SERVICE] = user_input.get(CONF_NOTIFY_SERVICE, "")
            return await self.async_step_security()

        return self.async_show_form(
            step_id="entities",
            data_schema=vol.Schema({
                vol.Optional("allowed_lights", default=[]): EntitySelector(
                    EntitySelectorConfig(domain="light", multiple=True)
                ),
                vol.Optional("allowed_locks", default=[]): EntitySelector(
                    EntitySelectorConfig(domain="lock", multiple=True)
                ),
                vol.Optional("allowed_switches", default=[]): EntitySelector(
                    EntitySelectorConfig(domain="switch", multiple=True)
                ),
                vol.Optional("allowed_covers", default=[]): EntitySelector(
                    EntitySelectorConfig(domain="cover", multiple=True)
                ),
                vol.Optional("allowed_sensors", default=[]): EntitySelector(
                    EntitySelectorConfig(domain="sensor", multiple=True)
                ),
                vol.Optional(CONF_NOTIFY_SERVICE, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }),
        )

    # ─── Step 4: Security Policies ────────────────────────────────────────────

    async def async_step_security(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            validated_actions = user_input.get("validated_actions", [])
            pin_actions = user_input.get("pin_actions", [])

            policies: dict[str, dict[str, Any]] = {}
            for action in validated_actions:
                policies[action] = {
                    "security_mode": SECURITY_MODE_VALIDATED,
                    "require_camera_feed": user_input.get("validator_uses_camera", True),
                    "require_visual_match": user_input.get("validator_requires_visual", False),
                    "max_per_session": int(user_input.get("max_per_session", 0)),
                    "cooldown_seconds": float(user_input.get("cooldown_seconds", 0)),
                }
            for action in pin_actions:
                existing = policies.get(action, {})
                if existing.get("security_mode") == SECURITY_MODE_VALIDATED:
                    existing["security_mode"] = SECURITY_MODE_PIN_AND_VALIDATED
                else:
                    policies[action] = {"security_mode": SECURITY_MODE_PIN}

            self._data[CONF_ACTION_POLICIES] = policies
            self._data[CONF_DEFAULT_SECURITY_MODE] = user_input.get(CONF_DEFAULT_SECURITY_MODE, SECURITY_MODE_AUTO)
            self._data[CONF_VALIDATOR_MODEL] = user_input.get(CONF_VALIDATOR_MODEL, DEFAULT_VALIDATOR_MODEL)
            self._data[CONF_PIN_CODE] = user_input.get(CONF_PIN_CODE, "")
            return await self.async_step_stop_triggers()

        available_actions = self._get_available_actions()

        return self.async_show_form(
            step_id="security",
            data_schema=vol.Schema({
                vol.Optional(CONF_DEFAULT_SECURITY_MODE, default=SECURITY_MODE_AUTO): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": "auto", "label": "Auto (no validation)"},
                            {"value": "validated", "label": "Validated (all actions checked)"},
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional("validated_actions", default=[]): SelectSelector(
                    SelectSelectorConfig(options=available_actions, mode=SelectSelectorMode.DROPDOWN, multiple=True)
                ),
                vol.Optional("pin_actions", default=[]): SelectSelector(
                    SelectSelectorConfig(options=available_actions, mode=SelectSelectorMode.DROPDOWN, multiple=True)
                ),
                vol.Optional("validator_uses_camera", default=True): BooleanSelector(),
                vol.Optional("validator_requires_visual", default=False): BooleanSelector(),
                vol.Optional("max_per_session", default=0): NumberSelector(
                    NumberSelectorConfig(min=0, max=50, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional("cooldown_seconds", default=0): NumberSelector(
                    NumberSelectorConfig(min=0, max=300, step=5, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_VALIDATOR_MODEL, default=DEFAULT_VALIDATOR_MODEL): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_PIN_CODE, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }),
        )

    # ─── Step 5: Auto-Stop Triggers ───────────────────────────────────────────

    async def async_step_stop_triggers(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data[CONF_STOP_ENTITIES] = user_input.get(CONF_STOP_ENTITIES, [])
            # Parse entity:state mappings from the text field
            state_map_raw = user_input.get("stop_entity_state_map", "")
            state_map = {}
            if state_map_raw:
                for line in state_map_raw.strip().splitlines():
                    if ":" in line:
                        eid, state = line.split(":", 1)
                        state_map[eid.strip()] = state.strip()
            self._data[CONF_STOP_ENTITY_STATES] = state_map
            self._data[CONF_STOP_EVENTS] = user_input.get(CONF_STOP_EVENTS, "").split(",") if user_input.get(CONF_STOP_EVENTS) else []
            self._data[CONF_STOP_EVENTS] = [e.strip() for e in self._data[CONF_STOP_EVENTS] if e.strip()]
            return await self.async_step_identity()

        return self.async_show_form(
            step_id="stop_triggers",
            data_schema=vol.Schema({
                vol.Optional(CONF_STOP_ENTITIES, default=[]): EntitySelector(
                    EntitySelectorConfig(multiple=True)
                ),
                vol.Optional("stop_entity_state_map", default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
                ),
                vol.Optional(CONF_STOP_EVENTS, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }),
        )

    # ─── Step 6: Identity & Prompt ────────────────────────────────────────────

    async def async_step_identity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            camera = self._data.get(CONF_CAMERA_ENTITY, "doorbell")
            return self.async_create_entry(title=f"Jeeves ({camera})", data=self._data)

        return self.async_show_form(
            step_id="identity",
            data_schema=vol.Schema({
                vol.Optional(CONF_IDENTITY_MODE, default="none"): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": "none", "label": "None"},
                            {"value": "sensor", "label": "Sensor (Frigate/CompreFace)"},
                            {"value": "reference_images", "label": "Reference Images"},
                            {"value": "both", "label": "Both"},
                        ],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_FACE_SENSOR_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_SYSTEM_PROMPT, default=DEFAULT_SYSTEM_PROMPT): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
                ),
            }),
        )

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _get_available_actions(self) -> list[str]:
        allowed = self._data.get(CONF_ALLOWED_ENTITIES, {})
        actions: list[str] = []
        domain_to_actions = {
            "light": ["turn_on_light", "turn_off_light"],
            "lock": ["unlock_door", "lock_door"],
            "switch": ["turn_on_switch", "turn_off_switch"],
            "cover": ["open_cover", "close_cover"],
            "sensor": ["get_sensor_state"],
        }
        for domain, acts in domain_to_actions.items():
            if allowed.get(domain):
                actions.extend(acts)
        if self._data.get(CONF_NOTIFY_SERVICE):
            actions.append("send_notification")
        return actions

    async def _validate_credentials(self, provider: str, api_key: str, base_url: str | None) -> bool:
        try:
            if provider == PROVIDER_GEMINI:
                from google import genai  # noqa: PLC0415
                client = genai.Client(api_key=api_key)
                await self.hass.async_add_executor_job(lambda: list(client.models.list()))
            else:
                import openai  # noqa: PLC0415
                kwargs: dict[str, Any] = {"api_key": api_key}
                if base_url:
                    kwargs["base_url"] = base_url
                client = openai.OpenAI(**kwargs)
                await self.hass.async_add_executor_job(lambda: list(client.models.list()))
            return True
        except Exception:
            _LOGGER.exception("Credential validation failed")
            return False


class DoorbellJeevesOptionsFlow(OptionsFlow):
    """Sectioned options flow for post-setup reconfiguration."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            section = user_input.get("section", "general")
            handler = getattr(self, f"async_step_{section}", None)
            if handler:
                return await handler()
            return await self.async_step_general()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("section", default="general"): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": "general", "label": "General (provider, model, voice)"},
                            {"value": "vision", "label": "Vision (camera, FPS, frame size)"},
                            {"value": "entities", "label": "Allowed Entities"},
                            {"value": "security", "label": "Security Policies"},
                            {"value": "stop_triggers", "label": "Auto-Stop Triggers"},
                            {"value": "prompt", "label": "System Prompt & Identity"},
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        c = dict(self._config_entry.data) | dict(self._config_entry.options)
        if user_input is not None:
            c.update(user_input)
            return self.async_create_entry(data=c)

        return self.async_show_form(
            step_id="general",
            data_schema=vol.Schema({
                vol.Optional(CONF_PROVIDER, default=c.get(CONF_PROVIDER, PROVIDER_GEMINI)): SelectSelector(
                    SelectSelectorConfig(options=[
                        {"value": PROVIDER_GEMINI, "label": "Google Gemini"},
                        {"value": PROVIDER_OPENAI, "label": "OpenAI / Compatible"},
                    ], mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional(CONF_API_KEY, default=c.get(CONF_API_KEY, "")): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_API_BASE_URL, default=c.get(CONF_API_BASE_URL, "")): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.URL)
                ),
                vol.Optional(CONF_MODEL, default=c.get(CONF_MODEL, DEFAULT_MODEL_GEMINI)): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_VOICE, default=c.get(CONF_VOICE, DEFAULT_VOICE_GEMINI)): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_SESSION_TIMEOUT, default=c.get(CONF_SESSION_TIMEOUT, DEFAULT_SESSION_TIMEOUT)): NumberSelector(
                    NumberSelectorConfig(min=0, max=3600, step=10, mode=NumberSelectorMode.BOX, unit_of_measurement="s")
                ),
            }),
        )

    async def async_step_vision(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        c = dict(self._config_entry.data) | dict(self._config_entry.options)
        if user_input is not None:
            c.update(user_input)
            return self.async_create_entry(data=c)

        return self.async_show_form(
            step_id="vision",
            data_schema=vol.Schema({
                vol.Optional(CONF_CAMERA_ENTITY, default=c.get(CONF_CAMERA_ENTITY, "")): EntitySelector(
                    EntitySelectorConfig(domain="camera")
                ),
                vol.Optional(CONF_MEDIA_PLAYER_ENTITY, default=c.get(CONF_MEDIA_PLAYER_ENTITY, "")): EntitySelector(
                    EntitySelectorConfig(domain="media_player")
                ),
                vol.Optional(CONF_VISION_FPS, default=c.get(CONF_VISION_FPS, DEFAULT_VISION_FPS)): NumberSelector(
                    NumberSelectorConfig(min=0.1, max=60.0, step=0.1, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_FRAME_MAX_WIDTH, default=c.get(CONF_FRAME_MAX_WIDTH, DEFAULT_FRAME_MAX_WIDTH)): NumberSelector(
                    NumberSelectorConfig(min=160, max=1920, step=10, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_FRAME_MAX_HEIGHT, default=c.get(CONF_FRAME_MAX_HEIGHT, DEFAULT_FRAME_MAX_HEIGHT)): NumberSelector(
                    NumberSelectorConfig(min=120, max=1080, step=10, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(CONF_FRAME_QUALITY, default=c.get(CONF_FRAME_QUALITY, DEFAULT_FRAME_QUALITY)): NumberSelector(
                    NumberSelectorConfig(min=10, max=100, step=5, mode=NumberSelectorMode.SLIDER)
                ),
            }),
        )

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        c = dict(self._config_entry.data) | dict(self._config_entry.options)
        allowed = c.get(CONF_ALLOWED_ENTITIES, {})

        if user_input is not None:
            c[CONF_ALLOWED_ENTITIES] = {
                "light": user_input.get("allowed_lights", []),
                "lock": user_input.get("allowed_locks", []),
                "switch": user_input.get("allowed_switches", []),
                "cover": user_input.get("allowed_covers", []),
                "sensor": user_input.get("allowed_sensors", []),
            }
            c[CONF_NOTIFY_SERVICE] = user_input.get(CONF_NOTIFY_SERVICE, "")
            return self.async_create_entry(data=c)

        return self.async_show_form(
            step_id="entities",
            data_schema=vol.Schema({
                vol.Optional("allowed_lights", default=allowed.get("light", [])): EntitySelector(
                    EntitySelectorConfig(domain="light", multiple=True)
                ),
                vol.Optional("allowed_locks", default=allowed.get("lock", [])): EntitySelector(
                    EntitySelectorConfig(domain="lock", multiple=True)
                ),
                vol.Optional("allowed_switches", default=allowed.get("switch", [])): EntitySelector(
                    EntitySelectorConfig(domain="switch", multiple=True)
                ),
                vol.Optional("allowed_covers", default=allowed.get("cover", [])): EntitySelector(
                    EntitySelectorConfig(domain="cover", multiple=True)
                ),
                vol.Optional("allowed_sensors", default=allowed.get("sensor", [])): EntitySelector(
                    EntitySelectorConfig(domain="sensor", multiple=True)
                ),
                vol.Optional(CONF_NOTIFY_SERVICE, default=c.get(CONF_NOTIFY_SERVICE, "")): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }),
        )

    async def async_step_security(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        c = dict(self._config_entry.data) | dict(self._config_entry.options)
        policies = c.get(CONF_ACTION_POLICIES, {})

        if user_input is not None:
            validated = user_input.get("validated_actions", [])
            pin_acts = user_input.get("pin_actions", [])
            new_policies: dict[str, dict[str, Any]] = {}
            for a in validated:
                new_policies[a] = {
                    "security_mode": SECURITY_MODE_VALIDATED,
                    "require_camera_feed": user_input.get("validator_uses_camera", True),
                    "require_visual_match": user_input.get("validator_requires_visual", False),
                    "max_per_session": int(user_input.get("max_per_session", 0)),
                    "cooldown_seconds": float(user_input.get("cooldown_seconds", 0)),
                    "validator_prompt": user_input.get("custom_validator_prompt", ""),
                }
            for a in pin_acts:
                e = new_policies.get(a, {})
                if e.get("security_mode") == SECURITY_MODE_VALIDATED:
                    e["security_mode"] = SECURITY_MODE_PIN_AND_VALIDATED
                else:
                    new_policies[a] = {"security_mode": SECURITY_MODE_PIN}
            c[CONF_ACTION_POLICIES] = new_policies
            c[CONF_DEFAULT_SECURITY_MODE] = user_input.get(CONF_DEFAULT_SECURITY_MODE, SECURITY_MODE_AUTO)
            c[CONF_VALIDATOR_MODEL] = user_input.get(CONF_VALIDATOR_MODEL, DEFAULT_VALIDATOR_MODEL)
            c[CONF_PIN_CODE] = user_input.get(CONF_PIN_CODE, "")
            return self.async_create_entry(data=c)

        currently_validated = [a for a, p in policies.items() if p.get("security_mode") in (SECURITY_MODE_VALIDATED, SECURITY_MODE_PIN_AND_VALIDATED)]
        currently_pinned = [a for a, p in policies.items() if p.get("security_mode") in (SECURITY_MODE_PIN, SECURITY_MODE_PIN_AND_VALIDATED)]
        example = next(iter(policies.values()), {}) if policies else {}

        return self.async_show_form(
            step_id="security",
            data_schema=vol.Schema({
                vol.Optional(CONF_DEFAULT_SECURITY_MODE, default=c.get(CONF_DEFAULT_SECURITY_MODE, SECURITY_MODE_AUTO)): SelectSelector(
                    SelectSelectorConfig(options=[
                        {"value": "auto", "label": "Auto"}, {"value": "validated", "label": "Validated"},
                    ], mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional("validated_actions", default=currently_validated): SelectSelector(
                    SelectSelectorConfig(options=get_all_action_names(), mode=SelectSelectorMode.DROPDOWN, multiple=True)
                ),
                vol.Optional("pin_actions", default=currently_pinned): SelectSelector(
                    SelectSelectorConfig(options=get_all_action_names(), mode=SelectSelectorMode.DROPDOWN, multiple=True)
                ),
                vol.Optional("validator_uses_camera", default=example.get("require_camera_feed", True)): BooleanSelector(),
                vol.Optional("validator_requires_visual", default=example.get("require_visual_match", False)): BooleanSelector(),
                vol.Optional("max_per_session", default=example.get("max_per_session", 0)): NumberSelector(
                    NumberSelectorConfig(min=0, max=50, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional("cooldown_seconds", default=example.get("cooldown_seconds", 0)): NumberSelector(
                    NumberSelectorConfig(min=0, max=300, step=5, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional("custom_validator_prompt", default=example.get("validator_prompt", "")): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
                ),
                vol.Optional(CONF_VALIDATOR_MODEL, default=c.get(CONF_VALIDATOR_MODEL, DEFAULT_VALIDATOR_MODEL)): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_PIN_CODE, default=c.get(CONF_PIN_CODE, "")): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }),
        )

    async def async_step_stop_triggers(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        c = dict(self._config_entry.data) | dict(self._config_entry.options)

        if user_input is not None:
            c[CONF_STOP_ENTITIES] = user_input.get(CONF_STOP_ENTITIES, [])
            state_map_raw = user_input.get("stop_entity_state_map", "")
            state_map = {}
            if state_map_raw:
                for line in state_map_raw.strip().splitlines():
                    if ":" in line:
                        eid, state = line.split(":", 1)
                        state_map[eid.strip()] = state.strip()
            c[CONF_STOP_ENTITY_STATES] = state_map
            c[CONF_STOP_EVENTS] = [e.strip() for e in user_input.get(CONF_STOP_EVENTS, "").split(",") if e.strip()]
            return self.async_create_entry(data=c)

        # Serialize current state map for display
        current_map = c.get(CONF_STOP_ENTITY_STATES, {})
        map_str = "\n".join(f"{k}: {v}" for k, v in current_map.items())
        events_str = ", ".join(c.get(CONF_STOP_EVENTS, []))

        return self.async_show_form(
            step_id="stop_triggers",
            data_schema=vol.Schema({
                vol.Optional(CONF_STOP_ENTITIES, default=c.get(CONF_STOP_ENTITIES, [])): EntitySelector(
                    EntitySelectorConfig(multiple=True)
                ),
                vol.Optional("stop_entity_state_map", default=map_str): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
                ),
                vol.Optional(CONF_STOP_EVENTS, default=events_str): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }),
        )

    async def async_step_prompt(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        c = dict(self._config_entry.data) | dict(self._config_entry.options)
        if user_input is not None:
            c.update(user_input)
            return self.async_create_entry(data=c)

        return self.async_show_form(
            step_id="prompt",
            data_schema=vol.Schema({
                vol.Optional(CONF_IDENTITY_MODE, default=c.get(CONF_IDENTITY_MODE, "none")): SelectSelector(
                    SelectSelectorConfig(options=[
                        {"value": "none", "label": "None"},
                        {"value": "sensor", "label": "Sensor"},
                        {"value": "reference_images", "label": "Reference Images"},
                        {"value": "both", "label": "Both"},
                    ], mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional(CONF_FACE_SENSOR_ENTITY, default=c.get(CONF_FACE_SENSOR_ENTITY)): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_SYSTEM_PROMPT, default=c.get(CONF_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT)): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
                ),
            }),
        )
