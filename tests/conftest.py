from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any, Callable

import pytest


def _install_custom_component_namespace() -> None:
    """Load custom component modules without executing integration __init__.py."""
    repo_root = Path(__file__).resolve().parents[1]
    custom_components_root = repo_root / "custom_components"
    component_root = custom_components_root / "ha_doorbell_jeeves"

    cc_pkg = sys.modules.get("custom_components")
    if cc_pkg is None:
        cc_pkg = types.ModuleType("custom_components")
        cc_pkg.__path__ = [str(custom_components_root)]
        sys.modules["custom_components"] = cc_pkg

    jeeves_pkg_name = "custom_components.ha_doorbell_jeeves"
    if jeeves_pkg_name not in sys.modules:
        jeeves_pkg = types.ModuleType(jeeves_pkg_name)
        jeeves_pkg.__path__ = [str(component_root)]
        sys.modules[jeeves_pkg_name] = jeeves_pkg


def _install_homeassistant_stubs() -> None:
    """Provide lightweight Home Assistant stubs for unit tests."""
    if "homeassistant" in sys.modules:
        return

    def _module(name: str, *, package: bool = False) -> types.ModuleType:
        module = types.ModuleType(name)
        if package:
            module.__path__ = []
        sys.modules[name] = module
        return module

    homeassistant = _module("homeassistant", package=True)
    core = _module("homeassistant.core")
    config_entries = _module("homeassistant.config_entries")
    const = _module("homeassistant.const")

    helpers = _module("homeassistant.helpers", package=True)
    helpers_event = _module("homeassistant.helpers.event")
    helpers_storage = _module("homeassistant.helpers.storage")
    helpers_device_registry = _module("homeassistant.helpers.device_registry")
    helpers_entity_platform = _module("homeassistant.helpers.entity_platform")

    components = _module("homeassistant.components", package=True)
    components_camera = _module("homeassistant.components.camera")
    components_http = _module("homeassistant.components.http")
    components_sensor = _module("homeassistant.components.sensor")

    class _Bus:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def async_fire(self, event_type: str, event_data: dict[str, Any] | None = None) -> None:
            self.events.append((event_type, event_data or {}))

        def async_listen(self, _event_type: str, _callback: Callable[..., Any]) -> Callable[[], None]:
            return lambda: None

    class _Services:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any], bool, bool]] = []

        async def async_call(
            self,
            domain: str,
            service: str,
            data: dict[str, Any] | None = None,
            blocking: bool = False,
            return_response: bool = False,
        ) -> dict[str, Any]:
            self.calls.append((domain, service, data or {}, blocking, return_response))
            return {}

        def has_service(self, _domain: str, _service: str) -> bool:
            return False

        def async_services(self) -> dict[str, dict[str, Any]]:
            return {}

    class Event:
        def __init__(self, data: dict[str, Any] | None = None) -> None:
            self.data = data or {}

    class ServiceCall:
        def __init__(self, data: dict[str, Any] | None = None) -> None:
            self.data = data or {}

    class HomeAssistant:
        def __init__(self) -> None:
            self.data: dict[str, Any] = {}
            self.bus = _Bus()
            self.services = _Services()
            self.config_entries = types.SimpleNamespace(
                async_entries=lambda _domain=None: [],
                async_get_entry=lambda _entry_id: None,
                async_update_entry=lambda *_args, **_kwargs: None,
            )
            self.http = types.SimpleNamespace(register_view=lambda _view: None)
            self.states = types.SimpleNamespace(get=lambda _entity_id: None)

        async def async_add_executor_job(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
            return asyncio.create_task(coro)

    def callback(func: Callable[..., Any]) -> Callable[..., Any]:
        return func

    core.Event = Event
    core.ServiceCall = ServiceCall
    core.HomeAssistant = HomeAssistant
    core.CALLBACK_TYPE = Callable[..., Any]
    core.callback = callback

    class ConfigEntryState:
        LOADED = "loaded"

    class ConfigEntry:
        def __init__(
            self,
            *,
            data: dict[str, Any] | None = None,
            options: dict[str, Any] | None = None,
            entry_id: str = "entry-id",
            title: str = "Doorbell Jeeves",
            state: str = ConfigEntryState.LOADED,
        ) -> None:
            self.data = data or {}
            self.options = options or {}
            self.entry_id = entry_id
            self.title = title
            self.state = state

        def add_update_listener(self, listener: Callable[..., Any]) -> Callable[..., Any]:
            return listener

        def async_on_unload(self, _listener: Callable[..., Any]) -> None:
            return None

    config_entries.ConfigEntryState = ConfigEntryState
    config_entries.ConfigEntry = ConfigEntry

    class Platform:
        SENSOR = "sensor"
        CAMERA = "camera"

    const.Platform = Platform

    def async_track_state_change_event(
        _hass: HomeAssistant, _entity_ids: list[str], _callback: Callable[..., Any]
    ) -> Callable[[], None]:
        return lambda: None

    helpers_event.async_track_state_change_event = async_track_state_change_event

    class Store:
        _data_by_key: dict[str, Any] = {}

        def __init__(self, _hass: HomeAssistant, _version: int, key: str) -> None:
            self._key = key

        async def async_load(self) -> Any:
            return Store._data_by_key.get(self._key)

        async def async_save(self, data: Any) -> None:
            Store._data_by_key[self._key] = data

    helpers_storage.Store = Store

    class DeviceInfo(dict):
        pass

    helpers_device_registry.DeviceInfo = DeviceInfo
    helpers_entity_platform.AddEntitiesCallback = Callable[[list[Any]], None]

    class Camera:
        def __init__(self) -> None:
            self.hass: HomeAssistant | None = None

        def async_on_remove(self, _func: Callable[..., Any]) -> None:
            return None

        def async_write_ha_state(self) -> None:
            return None

    async def async_get_image(_hass: HomeAssistant, _camera_entity: str, timeout: int = 10) -> Any:
        return None

    components_camera.Camera = Camera
    components_camera.async_get_image = async_get_image

    class HomeAssistantView:
        url = ""
        name = ""
        requires_auth = True

    components_http.HomeAssistantView = HomeAssistantView

    class SensorEntity:
        def __init__(self) -> None:
            self.hass: HomeAssistant | None = None

        def async_on_remove(self, _func: Callable[..., Any]) -> None:
            return None

        def async_write_ha_state(self) -> None:
            return None

    components_sensor.SensorEntity = SensorEntity

    homeassistant.core = core
    homeassistant.config_entries = config_entries
    homeassistant.const = const
    homeassistant.helpers = helpers
    homeassistant.components = components

    helpers.event = helpers_event
    helpers.storage = helpers_storage
    helpers.device_registry = helpers_device_registry
    helpers.entity_platform = helpers_entity_platform

    components.camera = components_camera
    components.http = components_http
    components.sensor = components_sensor


def _install_openai_stub() -> None:
    """Install a tiny OpenAI client stub for module import."""
    if "openai" in sys.modules:
        return

    openai = types.ModuleType("openai")

    class AsyncOpenAI:
        def __init__(self, **_kwargs: Any) -> None:
            pass

    openai.AsyncOpenAI = AsyncOpenAI
    sys.modules["openai"] = openai


_install_custom_component_namespace()
_install_homeassistant_stubs()
_install_openai_stub()


@pytest.fixture
def hass() -> Any:
    from homeassistant.core import HomeAssistant

    return HomeAssistant()

