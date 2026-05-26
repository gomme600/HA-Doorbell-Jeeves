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

# HA-managed go2rtc runs on port 11984 (since 2024.x)
# User/addon go2rtc typically runs on port 1984
GO2RTC_PORTS = [11984, 1984]
GO2RTC_BASE: str | None = None  # Resolved at runtime

# Reolink RTSP URL patterns
REOLINK_RTSP_MAIN = "rtsp://{user}:{password}@{host}:554/h264Preview_01_main"
REOLINK_RTSP_SUB = "rtsp://{user}:{password}@{host}:554/h264Preview_01_sub"


async def _discover_go2rtc_url(hass: HomeAssistant) -> str | None:
    """Discover the active go2rtc API URL.

    Strategy:
    1. Access HA's internal go2rtc data (url + authenticated session)
    2. Try port 11984 (HA-managed go2rtc since 2024.x)
    3. Try port 1984 (user-installed/addon go2rtc)
    """
    global GO2RTC_BASE  # noqa: PLW0603

    if GO2RTC_BASE:
        return GO2RTC_BASE

    # Method 1: Access HA's go2rtc data — it stores the URL and session with auth
    try:
        go2rtc_data = hass.data.get("go2rtc")
        if go2rtc_data and hasattr(go2rtc_data, "url"):
            url = go2rtc_data.url.rstrip("/")
            _LOGGER.info("Found go2rtc via HA internal data: %s", url)
            GO2RTC_BASE = url
            return GO2RTC_BASE
    except (AttributeError, KeyError, TypeError):
        pass

    # Method 2: Try known ports on localhost (with and without common auth)
    session = aiohttp.ClientSession()
    try:
        for port in GO2RTC_PORTS:
            try:
                url = f"http://127.0.0.1:{port}"
                async with session.get(
                    f"{url}/api/streams",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as resp:
                    if resp.status in (200, 401):
                        # 200 = accessible, 401 = exists but needs auth
                        if resp.status == 200:
                            _LOGGER.info("Discovered go2rtc at %s (no auth)", url)
                            GO2RTC_BASE = url
                            return GO2RTC_BASE
                        else:
                            _LOGGER.info("Found go2rtc at %s (requires auth)", url)
                            GO2RTC_BASE = url
                            return GO2RTC_BASE
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                continue
    finally:
        await session.close()

    _LOGGER.warning("Could not discover go2rtc — tried ports %s", GO2RTC_PORTS)
    return None


def _get_go2rtc_session(hass: HomeAssistant) -> aiohttp.ClientSession | None:
    """Get HA's authenticated go2rtc session if available.

    HA-managed go2rtc uses random BasicAuth credentials, and communicates
    via a Unix socket session. We can reuse this session for our API calls.
    """
    try:
        go2rtc_data = hass.data.get("go2rtc")
        if go2rtc_data and hasattr(go2rtc_data, "session"):
            return go2rtc_data.session
    except (AttributeError, KeyError, TypeError):
        pass
    return None


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
    base_url = await _discover_go2rtc_url(hass)
    if not base_url:
        _LOGGER.error("Cannot setup go2rtc stream — go2rtc not found")
        return False

    # Use HA's authenticated session if available (for HA-managed go2rtc)
    ha_session = _get_go2rtc_session(hass)
    own_session = None
    if ha_session:
        session = ha_session
    else:
        own_session = aiohttp.ClientSession()
        session = own_session

    try:
        url = f"{base_url}/api/streams"

        # Source 1: RTSP stream (receives video + audio from doorbell)
        params1 = {"src": stream_name, "url": rtsp_url}
        async with session.put(url, params=params1) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                _LOGGER.error("Failed to register go2rtc RTSP source (status=%d): %s", resp.status, text)
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
    except Exception:
        _LOGGER.exception("Error setting up go2rtc stream")
        return False
    finally:
        if own_session:
            await own_session.close()


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
        self._owns_session = True
        self._active = False
        self._go2rtc_url: str | None = None

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start receiving audio from the doorbell via go2rtc WebSocket."""
        if self._active:
            return

        # Discover go2rtc URL before starting
        self._go2rtc_url = await _discover_go2rtc_url(self._hass)
        if not self._go2rtc_url:
            _LOGGER.error("Cannot start audio handler — go2rtc not found")
            return

        # Use HA's authenticated session if available
        ha_session = _get_go2rtc_session(self._hass)
        if ha_session:
            self._session = ha_session
            self._owns_session = False
        else:
            self._session = aiohttp.ClientSession()
            self._owns_session = True

        self._active = True
        self._ws_task = asyncio.create_task(self._audio_receive_loop())
        _LOGGER.warning(
            "Reolink audio handler started (stream=%s, go2rtc=%s, owns_session=%s)",
            self._stream_name, self._go2rtc_url, self._owns_session,
        )

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
        if self._session and self._owns_session:
            await self._session.close()
        self._session = None
        _LOGGER.info("Reolink audio handler stopped")

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send PCM audio to the doorbell speaker via go2rtc backchannel.

        go2rtc accepts audio via its stream talk endpoint.
        Format: raw PCM 16-bit LE, mono, at the camera's expected sample rate.
        """
        if not self._session or not self._active or not self._go2rtc_url:
            _LOGGER.warning("send_audio: not ready (session=%s, active=%s, url=%s)",
                           bool(self._session), self._active, self._go2rtc_url)
            return

        try:
            url = f"{self._go2rtc_url}/api/stream"
            params = {"src": self._stream_name, "backchannel": "1"}
            async with self._session.post(
                url, params=params,
                data=pcm_bytes,
                headers={"Content-Type": "audio/pcm"},
            ) as resp:
                if resp.status not in (200, 201, 204):
                    text = await resp.text()
                    _LOGGER.warning("Backchannel send status %d: %s", resp.status, text[:200])
        except Exception:
            _LOGGER.warning("Failed to send audio to doorbell", exc_info=True)

    async def _audio_receive_loop(self) -> None:
        """Connect to go2rtc WebSocket and receive audio frames from the doorbell mic."""
        ws_url = f"{self._go2rtc_url}/api/ws?src={self._stream_name}&media=audio"
        _LOGGER.warning("Audio receive loop connecting to: %s", ws_url)

        while self._active:
            try:
                if not self._session:
                    break

                async with self._session.ws_connect(ws_url) as ws:
                    _LOGGER.warning("Connected to go2rtc audio WebSocket")
                    async for msg in ws:
                        if not self._active:
                            break
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            # Raw audio data from the doorbell microphone
                            await self._on_audio_received(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            _LOGGER.warning("go2rtc WS closed/error: %s", msg.type)
                            break

            except asyncio.CancelledError:
                break
            except Exception:
                _LOGGER.warning("go2rtc audio WebSocket error, retrying in 2s", exc_info=True)
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


# ─── Talk State Monitor ────────────────────────────────────────────────────────


class ReolinkTalkMonitor:
    """Monitors the Reolink camera's talk (backchannel) state via HTTP API.

    Reolink cameras expose a `GetTalkState` endpoint that indicates whether
    someone is currently using 2-way audio (e.g., from the Reolink app).
    
    When detected, this fires a callback so the AI session can be paused/stopped
    to let the human take over the conversation.
    
    Also monitors audio energy levels from the go2rtc stream to detect when
    someone starts speaking through the doorbell app (backup method).
    """

    # How often to poll the camera's talk state (seconds)
    POLL_INTERVAL = 2.0

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        username: str,
        password: str,
        on_human_takeover: Any,  # Callable[[], Awaitable[None]]
        poll_interval: float = 2.0,
    ) -> None:
        self._hass = hass
        self._host = host
        self._username = username
        self._password = password
        self._on_human_takeover = on_human_takeover
        self._poll_interval = poll_interval
        self._poll_task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._active = False
        self._talk_was_active = False

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start polling the camera for talk state changes."""
        if self._active:
            return
        self._active = True
        self._session = aiohttp.ClientSession()
        self._poll_task = asyncio.create_task(self._poll_loop())
        _LOGGER.info("Reolink talk monitor started (host=%s)", self._host)

    async def stop(self) -> None:
        """Stop the monitor."""
        self._active = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        if self._session:
            await self._session.close()
            self._session = None
        self._token = None
        _LOGGER.info("Reolink talk monitor stopped")

    async def _poll_loop(self) -> None:
        """Periodically check talk state."""
        # Initial login
        await self._login()

        while self._active:
            try:
                talk_active = await self._check_talk_state()
                if talk_active and not self._talk_was_active:
                    # Transition: talk just became active (human took over)
                    _LOGGER.info("Human takeover detected via Reolink talk state")
                    self._talk_was_active = True
                    await self._on_human_takeover()
                elif not talk_active:
                    self._talk_was_active = False
            except asyncio.CancelledError:
                break
            except Exception:
                _LOGGER.debug("Talk state poll error", exc_info=True)
            await asyncio.sleep(self._poll_interval)

    async def _login(self) -> bool:
        """Authenticate with the Reolink camera to get a session token."""
        if not self._session:
            return False
        try:
            url = f"http://{self._host}/api.cgi?cmd=Login"
            payload = [{
                "cmd": "Login",
                "action": 0,
                "param": {
                    "User": {
                        "userName": self._username,
                        "password": self._password,
                    }
                },
            }]
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and data[0].get("code") == 0:
                        self._token = data[0]["value"]["Token"]["name"]
                        _LOGGER.debug("Reolink API login successful")
                        return True
            _LOGGER.warning("Reolink API login failed")
            return False
        except Exception:
            _LOGGER.debug("Reolink API login error", exc_info=True)
            return False

    async def _check_talk_state(self) -> bool:
        """Query the camera's talk/backchannel state.
        
        Returns True if someone is actively using 2-way audio (e.g., from the app).
        """
        if not self._session or not self._token:
            # Try re-login if token is missing
            if not await self._login():
                return False

        try:
            url = f"http://{self._host}/api.cgi?cmd=GetTalkState&token={self._token}"
            payload = [{
                "cmd": "GetTalkState",
                "action": 0,
                "param": {"channel": 0},
            }]
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and data[0].get("code") == 0:
                        state = data[0].get("value", {}).get("state", 0)
                        return state == 1
                    elif data and data[0].get("code") == -6:
                        # Token expired — re-login
                        self._token = None
                        return False
            return False
        except Exception:
            _LOGGER.debug("GetTalkState request failed", exc_info=True)
            return False


class AudioInterruptDetector:
    """Detects human speech interruption by monitoring audio energy levels.
    
    This is a backup detection method for when the Reolink API isn't available
    or for non-Reolink setups. It monitors the incoming audio stream energy and
    detects when someone is speaking while the AI is also outputting audio.
    
    Detection logic:
    - While AI is speaking (outputting audio), the doorbell mic picks up the 
      AI's own voice at a consistent level
    - If a HUMAN starts talking through the Reolink app, the speaker output is 
      louder than normal visitor speech (speaker is next to mic)
    - A sudden energy spike during AI output → likely human takeover
    
    Also detects extended silence (visitor left) as a stop signal.
    """

    # Energy threshold for "someone is speaking" (RMS of 16-bit PCM)
    SPEECH_THRESHOLD = 800
    # How many consecutive frames above threshold = human speaking
    CONSECUTIVE_FRAMES_FOR_DETECTION = 5
    # Silence duration to consider visitor gone (seconds)
    SILENCE_TIMEOUT = 30.0

    def __init__(
        self,
        on_interrupt_detected: Any,  # Callable[[], Awaitable[None]]
        on_silence_timeout: Any | None = None,  # Callable[[], Awaitable[None]] | None
    ) -> None:
        self._on_interrupt_detected = on_interrupt_detected
        self._on_silence_timeout = on_silence_timeout
        self._ai_is_speaking = False
        self._high_energy_count = 0
        self._last_speech_time: float = 0.0
        self._silence_task: asyncio.Task[None] | None = None
        self._active = False
        self._triggered = False

    def start(self) -> None:
        self._active = True
        self._triggered = False
        self._last_speech_time = asyncio.get_event_loop().time()

    def stop(self) -> None:
        self._active = False
        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()

    def set_ai_speaking(self, is_speaking: bool) -> None:
        """Called when AI starts/stops outputting audio."""
        self._ai_is_speaking = is_speaking

    def process_audio_frame(self, pcm_bytes: bytes) -> None:
        """Process an incoming audio frame from the doorbell mic.
        
        Call this for every audio chunk received from go2rtc.
        """
        if not self._active or self._triggered:
            return

        # Calculate RMS energy of the frame
        energy = self._calculate_rms(pcm_bytes)

        if energy > self.SPEECH_THRESHOLD:
            self._last_speech_time = asyncio.get_event_loop().time()

            if self._ai_is_speaking:
                # Audio detected while AI is speaking = potential human interruption
                self._high_energy_count += 1
                if self._high_energy_count >= self.CONSECUTIVE_FRAMES_FOR_DETECTION:
                    self._triggered = True
                    _LOGGER.info(
                        "Audio interrupt detected (energy=%d, consecutive=%d)",
                        energy, self._high_energy_count,
                    )
                    asyncio.create_task(self._on_interrupt_detected())
            else:
                self._high_energy_count = 0
        else:
            self._high_energy_count = 0

    @staticmethod
    def _calculate_rms(pcm_bytes: bytes) -> float:
        """Calculate RMS energy of 16-bit PCM audio."""
        import struct  # noqa: PLC0415
        if len(pcm_bytes) < 4:
            return 0.0
        num_samples = len(pcm_bytes) // 2
        samples = struct.unpack(f"<{num_samples}h", pcm_bytes[:num_samples * 2])
        if not samples:
            return 0.0
        sum_squares = sum(s * s for s in samples)
        return (sum_squares / num_samples) ** 0.5
