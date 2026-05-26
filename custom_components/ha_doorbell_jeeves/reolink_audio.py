"""Reolink audio handler – manages go2rtc for 2-way audio with Reolink doorbells.

This module handles:
  - Auto-detection of Reolink doorbell RTSP details from HA's config entries
  - Configuration of go2rtc stream with backchannel support
  - Audio input: extracts PCM audio from go2rtc WebSocket stream
  - Audio output: sends PCM audio to doorbell speaker via go2rtc backchannel
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# go2rtc is embedded in HA Core and runs on localhost:1984
GO2RTC_BASE = "http://localhost:1984"

# Reolink RTSP URL patterns
REOLINK_RTSP_MAIN = "rtsp://{user}:{password}@{host}:554/h264Preview_01_main"
REOLINK_RTSP_SUB = "rtsp://{user}:{password}@{host}:554/h264Preview_01_sub"


def get_reolink_config(hass: HomeAssistant, config_entry_id: str) -> dict[str, Any] | None:
    """Extract Reolink connection details from its config entry.

    The Reolink integration stores host, username, and password in its config entry.
    We access this to auto-configure go2rtc without asking the user for redundant info.
    """
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if not entry or entry.domain != "reolink":
        return None

    data = dict(entry.data)
    return {
        "host": data.get("host", ""),
        "username": data.get("username", "admin"),
        "password": data.get("password", ""),
        "port": data.get("port", 443),
    }


def find_reolink_entry_for_camera(hass: HomeAssistant, camera_entity_id: str) -> str | None:
    """Find the Reolink config entry ID that owns a given camera entity."""
    from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
    registry = er.async_get(hass)
    entity_entry = registry.async_get(camera_entity_id)
    if entity_entry and entity_entry.config_entry_id:
        entry = hass.config_entries.async_get_entry(entity_entry.config_entry_id)
        if entry and entry.domain == "reolink":
            return entity_entry.config_entry_id
    return None


async def setup_go2rtc_stream(
    hass: HomeAssistant,
    stream_name: str,
    rtsp_url: str,
) -> bool:
    """Register a stream in go2rtc with backchannel support.

    go2rtc requires TWO source entries for 2-way audio:
      1. The RTSP URL (camera → HA: video + audio)
      2. An ffmpeg backchannel source (HA → camera: audio)

    Reference: https://community.home-assistant.io/t/2-way-audio-intercom-for-reolink-doorbell-made-easy/832189
    """
    try:
        session = aiohttp.ClientSession()
        try:
            url = f"{GO2RTC_BASE}/api/streams"

            # Source 1: RTSP stream (receives video + audio from doorbell)
            params1 = {"src": stream_name, "url": rtsp_url}
            async with session.put(url, params=params1) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    _LOGGER.error("Failed to register go2rtc RTSP source: %s", text)
                    return False

            # Source 2: ffmpeg backchannel (sends audio TO doorbell speaker)
            backchannel_url = f"ffmpeg:{stream_name}#audio=opus#audio=copy"
            params2 = {"src": stream_name, "url": backchannel_url}
            async with session.put(url, params=params2) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    _LOGGER.warning("Failed to register go2rtc backchannel: %s (2-way audio may not work)", text)

            _LOGGER.info("Registered go2rtc stream '%s' with backchannel", stream_name)
            return True
        finally:
            await session.close()
    except Exception:
        _LOGGER.exception("Error setting up go2rtc stream")
        return False


class ReolinkAudioHandler:
    """Handles 2-way audio for Reolink doorbells via go2rtc.

    Audio flow:
      Visitor speaks → RTSP → go2rtc → WebSocket → extract PCM → send to AI
      AI responds → PCM audio → POST to go2rtc backchannel → doorbell speaker
    """

    def __init__(
        self,
        hass: HomeAssistant,
        stream_name: str,
        on_audio_received: Any,  # Callable[[bytes], Awaitable[None]]
    ) -> None:
        self._hass = hass
        self._stream_name = stream_name
        self._on_audio_received = on_audio_received
        self._ws_task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start receiving audio from the doorbell via go2rtc WebSocket."""
        if self._active:
            return

        self._session = aiohttp.ClientSession()
        self._active = True
        self._ws_task = asyncio.create_task(self._audio_receive_loop())
        _LOGGER.info("Reolink audio handler started (stream=%s)", self._stream_name)

    async def stop(self) -> None:
        """Stop the audio handler."""
        self._active = False
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        self._ws_task = None
        if self._session:
            await self._session.close()
            self._session = None
        _LOGGER.info("Reolink audio handler stopped")

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send PCM audio to the doorbell speaker via go2rtc backchannel.

        go2rtc accepts audio via its stream talk endpoint.
        Format: raw PCM 16-bit LE, mono, at the camera's expected sample rate.
        """
        if not self._session or not self._active:
            return

        try:
            url = f"{GO2RTC_BASE}/api/stream"
            params = {"src": self._stream_name, "backchannel": "1"}
            async with self._session.post(
                url, params=params,
                data=pcm_bytes,
                headers={"Content-Type": "audio/pcm"},
            ) as resp:
                if resp.status not in (200, 201, 204):
                    text = await resp.text()
                    _LOGGER.debug("Backchannel send status %d: %s", resp.status, text[:100])
        except Exception:
            _LOGGER.debug("Failed to send audio to doorbell", exc_info=True)

    async def _audio_receive_loop(self) -> None:
        """Connect to go2rtc WebSocket and receive audio frames from the doorbell mic."""
        ws_url = f"{GO2RTC_BASE}/api/ws?src={self._stream_name}&media=audio"

        while self._active:
            try:
                if not self._session:
                    break

                async with self._session.ws_connect(ws_url) as ws:
                    _LOGGER.debug("Connected to go2rtc audio WebSocket")
                    async for msg in ws:
                        if not self._active:
                            break
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            # Raw audio data from the doorbell microphone
                            await self._on_audio_received(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break

            except asyncio.CancelledError:
                break
            except Exception:
                _LOGGER.debug("go2rtc audio WebSocket error, retrying in 2s", exc_info=True)
                await asyncio.sleep(2)


async def auto_configure_reolink(
    hass: HomeAssistant,
    camera_entity_id: str,
) -> dict[str, str] | None:
    """Auto-configure go2rtc for a Reolink doorbell.

    Returns a dict with the stream_name and status, or None on failure.
    This is called during integration setup to prepare the audio pipeline.
    """
    # Find the Reolink config entry
    reolink_entry_id = find_reolink_entry_for_camera(hass, camera_entity_id)
    if not reolink_entry_id:
        _LOGGER.warning("Could not find Reolink config entry for %s", camera_entity_id)
        return None

    # Get connection details
    reolink_config = get_reolink_config(hass, reolink_entry_id)
    if not reolink_config or not reolink_config.get("host"):
        _LOGGER.warning("Could not get Reolink connection details")
        return None

    # Build RTSP URL (sub stream for lower bandwidth)
    host = reolink_config["host"]
    user = reolink_config["username"]
    password = reolink_config["password"]
    rtsp_url = REOLINK_RTSP_SUB.format(host=host, user=user, password=password)

    # Create a unique stream name
    stream_name = f"jeeves_{camera_entity_id.replace('.', '_')}"

    # Register in go2rtc
    success = await setup_go2rtc_stream(hass, stream_name, rtsp_url)
    if not success:
        return None

    return {
        "stream_name": stream_name,
        "host": host,
        "rtsp_url_masked": f"rtsp://{user}:***@{host}:554/h264Preview_01_sub",
    }
