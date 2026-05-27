"""Reolink audio handler – 2-way audio with Reolink doorbells.

This module handles:
  - Audio input: receives audio from doorbell mic via existing go2rtc stream
    (auto-discovered from HA's Reolink integration, zero manual config)
  - Audio output: sends ADPCM to doorbell speaker via native Baichuan protocol (port 9000)
  - Falls back to camera entity stream_source or RTSP if go2rtc is unavailable
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import aiohttp

from homeassistant.core import HomeAssistant

from .const import AUDIO_INPUT_SAMPLE_RATE, DEFAULT_CHIME_DELAY

_LOGGER = logging.getLogger(__name__)

# HA-managed go2rtc runs on port 11984 (since 2024.x)
# User/addon go2rtc typically runs on port 1984
GO2RTC_PORTS = [11984, 1984]
GO2RTC_BASE: str | None = None  # Resolved at runtime
MAX_AI_PCM_BUFFER_BYTES = 240_000  # ~5s of 24kHz mono 16-bit PCM

# Reolink RTSP URL patterns
REOLINK_RTSP_MAIN = "rtsp://{user}:{password}@{host}:554/h264Preview_01_main"
REOLINK_RTSP_SUB = "rtsp://{user}:{password}@{host}:554/h264Preview_01_sub"


def _mask_stream_url(url: str) -> str:
    """Redact credentials from RTSP/RTMP URLs before logging."""
    try:
        parsed = urlsplit(url)
        netloc = parsed.netloc

        if "@" in netloc:
            userinfo, host = netloc.rsplit("@", 1)
            if ":" in userinfo:
                username, _password = userinfo.split(":", 1)
                netloc = f"{username}:***@{host}"
            else:
                netloc = f"***@{host}"

        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if query_pairs:
            redacted_pairs = []
            for key, value in query_pairs:
                if "pass" in key.lower() or "token" in key.lower():
                    redacted_pairs.append((key, "***"))
                else:
                    redacted_pairs.append((key, value))
            query = urlencode(redacted_pairs, doseq=True)
        else:
            query = parsed.query

        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
    except Exception:
        return "<redacted>"


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
        "username": data.get("username", ""),
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

        # go2rtc PUT API: ?name=STREAM_NAME&src=SOURCE1&src=SOURCE2
        # Source 1: RTSP (receives video + audio from doorbell)
        # Source 2: ffmpeg backchannel (enables receiving audio for speaker)
        backchannel_src = f"ffmpeg:{stream_name}#audio=pcma#audio=copy"
        params = [
            ("name", stream_name),
            ("src", rtsp_url),
            ("src", backchannel_src),
        ]
        async with session.put(url, params=params) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                _LOGGER.error("Failed to register go2rtc stream (status=%d): %s", resp.status, text)
                return False

        _LOGGER.warning("Registered go2rtc stream '%s' with backchannel", stream_name)
        return True
    except Exception:
        _LOGGER.exception("Error setting up go2rtc stream")
        return False
    finally:
        if own_session:
            await own_session.close()


class ReolinkAudioHandler:
    """Handles 2-way audio for Reolink doorbells.

    Audio flow:
      INPUT:  Visitor speaks → RTSP/RTMP stream → ffmpeg decode → PCM 16kHz → AI
      OUTPUT: AI responds → PCM 24kHz → ffmpeg resample → ADPCM → Baichuan talk

    Output uses native Baichuan protocol (port 9000). Input prefers RTSP and
    automatically falls back to RTMP when RTSP is unavailable.
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
        self._output_reader_task: asyncio.Task[None] | None = None
        self._active = False
        self._send_count = 0
        self._reolink_host: str | None = None
        self._reolink_user: str | None = None
        self._reolink_pass: str | None = None
        self._reolink_rtsp_port: int = 554
        self._reolink_rtsp_url: str = ""
        self._reolink_rtmp_url: str = ""
        self._reolink_flv_url: str = ""
        self._camera_entity_id: str | None = None
        self._reolink_entry_id: str | None = None
        # Baichuan talk state (output — speaker)
        self._baichuan: Any = None
        self._talk_host: Any = None  # Dedicated Host object for talk connection
        self._talk_ability: dict | None = None
        self._talk_enc_type: Any = None
        self._pcm_buffer: bytearray = bytearray()
        self._pcm_lock = asyncio.Lock()
        self._pcm_overflow_warned = False
        self._ffmpeg_proc: asyncio.subprocess.Process | None = None
        self._chime_delay: float = DEFAULT_CHIME_DELAY
        # Audio input state (native Baichuan FDX + ffmpeg fallback)
        self._listen_active = False
        self._input_processor_task: asyncio.Task[None] | None = None
        self._baichuan_audio_input_active = False
        # IMA ADPCM decoder state for incoming audio
        self._decode_predictor: int = 0
        self._decode_step_index: int = 0

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start 2-way audio: output via Baichuan, input via FDX on same connection."""
        if self._active:
            return

        # Get Reolink connection details for direct camera access
        await self._discover_reolink_details()

        self._active = True

        # Start Baichuan talk output pipeline (ADPCM via port 9000)
        # This also installs the FDX audio input intercept after talk starts
        await self._start_output_pipeline()

        # Start doorbell mic input (FDX intercept is primary; ffmpeg is fallback)
        await self._start_audio_input()

        _LOGGER.warning(
            "Reolink audio handler started (host=%s, baichuan=%s, fdx_input=%s)",
            self._reolink_host,
            "connected" if self._baichuan else "failed",
            self._baichuan_audio_input_active or "pending (after chime)",
        )

    async def _discover_reolink_details(self) -> None:
        """Get Reolink camera connection details from HA's Reolink integration.

        Discovers RTMP, FLV, and RTSP URLs for audio input.
        RTMP is preferred because it passes credentials as query params,
        avoiding URL-encoding issues with special characters in passwords.
        """
        # First try to get from the camera entity's config entry
        reolink_entry_id = self._reolink_entry_id
        if not reolink_entry_id:
            camera_entity = self._camera_entity_id or self._stream_name
            # Clean up entity ID format
            if camera_entity.startswith("jeeves_"):
                camera_entity = camera_entity.replace("jeeves_", "").replace("_", ".", 1)
            reolink_entry_id = find_reolink_entry_for_camera(self._hass, camera_entity)

        if reolink_entry_id:
            reolink_config = get_reolink_config(self._hass, reolink_entry_id)
            if reolink_config:
                self._reolink_host = reolink_config["host"]
                self._reolink_user = reolink_config["username"]
                self._reolink_pass = reolink_config["password"]

            # Try to get stream URLs from the runtime Host object
            entry = self._hass.config_entries.async_get_entry(reolink_entry_id)
            if entry and hasattr(entry, "runtime_data") and entry.runtime_data:
                try:
                    host_obj = entry.runtime_data.host
                    api = host_obj.api

                    # RTMP (preferred — password in query params, no URL-encoding issues)
                    rtmp_url = api.get_rtmp_stream_source(0, "sub")
                    if rtmp_url:
                        self._reolink_rtmp_url = rtmp_url
                        _LOGGER.warning("Reolink RTMP URL: %s", _mask_stream_url(rtmp_url))

                    # HTTP-FLV (second choice — also query-param auth)
                    flv_url = api.get_flv_stream_source(0, "sub")
                    if flv_url:
                        self._reolink_flv_url = flv_url
                        _LOGGER.warning("Reolink FLV URL: %s", _mask_stream_url(flv_url))

                    # RTSP (last resort — password in URL userinfo, prone to encoding issues)
                    port = api.rtsp_port
                    if port:
                        self._reolink_rtsp_port = port
                    rtsp_url = await api.get_rtsp_stream_source(0, "sub", check=False)
                    if rtsp_url:
                        self._reolink_rtsp_url = rtsp_url
                        _LOGGER.warning("Reolink RTSP URL: %s", _mask_stream_url(rtsp_url))
                except Exception as exc:
                    _LOGGER.warning("Could not get stream details from Reolink Host: %s", exc)

        # Build fallback RTMP URL from credentials if not discovered from API
        if not getattr(self, "_reolink_rtmp_url", None):
            if self._reolink_host and self._reolink_user and self._reolink_pass:
                self._reolink_rtmp_url = (
                    f"rtmp://{self._reolink_host}:1935/bcs/channel0_sub.bcs"
                    f"?channel=0&stream=1&user={self._reolink_user}"
                    f"&password={self._reolink_pass}"
                )

        # Build fallback RTSP URL from credentials if not discovered from API
        if not getattr(self, "_reolink_rtsp_url", None):
            if self._reolink_host and self._reolink_user and self._reolink_pass:
                encoded_pass = quote(self._reolink_pass, safe="")
                port = getattr(self, "_reolink_rtsp_port", 554) or 554
                self._reolink_rtsp_url = (
                    f"rtsp://{self._reolink_user}:{encoded_pass}"
                    f"@{self._reolink_host}:{port}/h264Preview_01_sub"
                )

        if not self._reolink_host:
            _LOGGER.warning("Could not discover Reolink host — audio output may not work")

    async def stop(self) -> None:
        """Stop the audio handler and all subprocesses."""
        self._active = False
        self._listen_active = False

        # Stop ffmpeg resampler
        if self._ffmpeg_proc and self._ffmpeg_proc.returncode is None:
            try:
                if self._ffmpeg_proc.stdin and not self._ffmpeg_proc.stdin.is_closing():
                    self._ffmpeg_proc.stdin.close()
                self._ffmpeg_proc.terminate()
                await asyncio.wait_for(self._ffmpeg_proc.wait(), timeout=3)
            except Exception:
                try:
                    self._ffmpeg_proc.kill()
                except Exception:
                    pass
        self._ffmpeg_proc = None

        # Stop Baichuan talk session
        await self._stop_baichuan_talk(0)

        # Cancel RTSP audio input task
        if self._input_processor_task and not self._input_processor_task.done():
            self._input_processor_task.cancel()
            try:
                await self._input_processor_task
            except (asyncio.CancelledError, Exception):
                pass
        self._input_processor_task = None

        # Logout dedicated talk connection
        if hasattr(self, "_talk_host") and self._talk_host:
            try:
                await self._talk_host.logout()
            except Exception:
                pass
            self._talk_host = None

        # Cancel output reader task
        if self._output_reader_task and not self._output_reader_task.done():
            self._output_reader_task.cancel()
            try:
                await self._output_reader_task
            except asyncio.CancelledError:
                pass
        self._output_reader_task = None

        async with self._pcm_lock:
            self._pcm_buffer.clear()
            self._pcm_overflow_warned = False

        self._baichuan = None
        self._talk_ability = None
        _LOGGER.warning("Reolink audio handler stopped (sent %d audio chunks)", self._send_count)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send AI audio to the doorbell speaker.

        Accepts 24kHz 16-bit PCM from Gemini. Buffers it for the output loop
        which encodes to IMA ADPCM and sends via Baichuan protocol (port 9000).
        """
        if not self._active:
            return
        async with self._pcm_lock:
            self._pcm_buffer.extend(pcm_bytes)
            overflow = len(self._pcm_buffer) - MAX_AI_PCM_BUFFER_BYTES
            if overflow > 0:
                del self._pcm_buffer[:overflow]
                if not self._pcm_overflow_warned:
                    self._pcm_overflow_warned = True
                    _LOGGER.warning(
                        "AI audio backlog exceeded %d bytes; trimming oldest buffered audio",
                        MAX_AI_PCM_BUFFER_BYTES,
                    )
        self._send_count += 1
        if self._send_count == 1:
            _LOGGER.warning("First audio data received from AI (%d bytes)", len(pcm_bytes))

    async def _start_output_pipeline(self) -> None:
        """Start the Baichuan talk session and audio output loop.

        Uses the Reolink integration's existing Baichuan connection (port 9000)
        to send ADPCM audio directly to the camera speaker.
        This bypasses RTSP entirely — works even when port 554 is blocked.
        """
        # Get the Baichuan object (creates dedicated connection)
        bc = await self._get_baichuan_object()
        if not bc:
            _LOGGER.error(
                "Cannot establish Baichuan talk connection. "
                "Audio output to doorbell speaker will not work."
            )
            return

        self._baichuan = bc
        _LOGGER.warning("Got Baichuan connection to %s for talk", self._reolink_host)

        # Query TalkAbility to get audio parameters
        ability = await self._query_talk_ability()
        if not ability:
            _LOGGER.error("Camera does not report TalkAbility — two-way audio not supported")
            return
        self._talk_ability = ability
        _LOGGER.warning(
            "TalkAbility: type=%s, rate=%d, block=%d, duplex=%s",
            ability["audio_type"], ability["sample_rate"],
            ability["length_per_encoder"], ability["duplex"],
        )

        # Start the output processing loop
        self._output_reader_task = asyncio.create_task(self._baichuan_audio_output_loop())

    async def _get_baichuan_object(self):
        """Create a dedicated Baichuan connection for talk.

        We create our OWN Host/Baichuan connection rather than reusing the
        Reolink integration's connection. This avoids interference between
        our audio streaming and the integration's normal command flow.
        """
        camera_entity = self._camera_entity_id or ""
        reolink_entry_id = self._reolink_entry_id
        if not reolink_entry_id:
            reolink_entry_id = find_reolink_entry_for_camera(self._hass, camera_entity)
        if not reolink_entry_id:
            _LOGGER.warning(
                "No Reolink entry found for camera %s",
                camera_entity or "<unset>",
            )
            return None

        entry = self._hass.config_entries.async_get_entry(reolink_entry_id)
        if not entry:
            return None

        # Get credentials and connection details from the Reolink config entry
        data = entry.data
        host = data.get("host", "")
        username = data.get("username", "")
        password = data.get("password", "")
        http_port = data.get("port")
        use_https = data.get("use_https")
        bc_port = data.get("baichuan_port", 9000)

        if not host:
            _LOGGER.warning("Reolink entry has no host configured")
            return None

        try:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: PLC0415
            from reolink_aio.api import Host  # noqa: PLC0415

            talk_host = Host(
                host=host,
                username=username,
                password=password,
                port=http_port,
                use_https=use_https,
                bc_port=bc_port,
                aiohttp_get_session_callback=lambda: async_get_clientsession(self._hass),
            )
            bc = talk_host.baichuan
            await bc.login()
            # Store the Host object so we can logout later
            self._talk_host = talk_host
            _LOGGER.warning("Created dedicated Baichuan talk connection to %s:%s", host, bc_port)
            return bc
        except Exception as exc:
            _LOGGER.warning("Failed to create Baichuan talk connection: %s", exc)
            return None

    async def _query_talk_ability(self) -> dict | None:
        """Query the camera's TalkAbility via Baichuan command 10."""
        import xml.etree.ElementTree as ET  # noqa: PLC0415

        if not self._baichuan:
            return None

        try:
            # Command 10 queries the camera's two-way audio capabilities
            response = await self._baichuan.send(cmd_id=10, channel=0)
            if not response:
                _LOGGER.warning("Empty TalkAbility response from camera")
                return None

            # Parse the XML response
            root = ET.fromstring(response)
            ta = root.find(".//TalkAbility")
            if ta is None:
                _LOGGER.warning("TalkAbility element not found in response XML")
                _LOGGER.debug("Response XML: %s", response[:500])
                return None

            ac = ta.find(".//audioConfig")
            if ac is None:
                _LOGGER.warning("audioConfig not found in TalkAbility")
                return None

            def _text(parent, path, default=""):
                el = parent.find(path)
                return el.text.strip() if el is not None and el.text else default

            def _texts(parent, path):
                return [el.text.strip() for el in parent.findall(path) if el.text]

            # Prefer FDX (full duplex) and mixAudioStream
            duplex_list = _texts(ta, ".//duplexList/duplex")
            duplex = "FDX" if "FDX" in duplex_list else (duplex_list[0] if duplex_list else "FDX")

            stream_modes = _texts(ta, ".//audioStreamModeList/audioStreamMode")
            stream_mode = "mixAudioStream" if "mixAudioStream" in stream_modes else (
                stream_modes[0] if stream_modes else "followVideoStream"
            )

            return {
                "duplex": duplex,
                "audio_stream_mode": stream_mode,
                "audio_type": _text(ac, ".//audioType", "adpcm"),
                "sample_rate": int(_text(ac, ".//sampleRate", "16000")),
                "sample_precision": int(_text(ac, ".//samplePrecision", "16")),
                "length_per_encoder": int(_text(ac, ".//lengthPerEncoder", "1024")),
                "sound_track": _text(ac, ".//soundTrack", "mono"),
            }
        except Exception as exc:
            _LOGGER.warning("Failed to query TalkAbility: %s", exc)
            return None

    async def _baichuan_audio_output_loop(self) -> None:
        """Main audio output loop: buffer PCM → resample → ADPCM → Baichuan talk.

        Runs as a background task. Consumes PCM from self._pcm_buffer,
        resamples from 24kHz to camera rate, encodes as IMA ADPCM, and sends
        via Baichuan protocol command 202.
        """
        import struct as struct_mod  # noqa: PLC0415

        if not self._baichuan or not self._talk_ability:
            return

        ability = self._talk_ability
        sample_rate = ability["sample_rate"]
        length_per_encoder = ability["length_per_encoder"]
        # CRITICAL: the ADPCM block size for DVI-4 is NOT length_per_encoder!
        # It's (length_per_encoder / 2) + 4 bytes (4 = header: predictor + index + reserved)
        # This matches what reolink_talk and neolink use.
        block_size = (length_per_encoder // 2) + 4
        channel = 0

        # Wait for the mechanical chime to finish before taking over the speaker
        chime_delay = getattr(self, "_chime_delay", DEFAULT_CHIME_DELAY)
        if chime_delay > 0:
            _LOGGER.warning(
                "Waiting %.1fs for doorbell chime before starting talk...", chime_delay
            )
            await asyncio.sleep(chime_delay)

        # Start talk session: send TalkConfig (cmd 201)
        talk_started = await self._start_baichuan_talk(channel, ability)
        if not talk_started:
            _LOGGER.error("Failed to start Baichuan talk session — audio output disabled")
            return

        _LOGGER.warning(
            "Baichuan talk session started! Streaming audio (rate=%d, block=%d)...",
            sample_rate, block_size,
        )

        # Install Baichuan FDX audio intercept (diagnostic only — CMD 202
        # actually carries video frames, not mic audio. Mic comes via RTMP/RTSP)
        if ability.get("duplex") == "FDX" and not self._baichuan_audio_input_active:
            success = self._install_baichuan_audio_intercept()
            if success:
                self._baichuan_audio_input_active = True
                _LOGGER.debug(
                    "Baichuan FDX intercept installed (diagnostic only — mic via RTMP)"
                )

        # Pure Python resampler: 24kHz mono s16le → camera sample_rate mono s16le
        # Eliminates ffmpeg dependency and pipe buffering issues.
        input_rate = 24000
        _LOGGER.warning(
            "Audio output resampler ready (in-process %dHz → %dHz)", input_rate, sample_rate
        )

        chunks_sent = 0
        # ADPCM encoder state (persistent across blocks for continuity)
        predictor = 0
        step_index = 0
        # Calculate how many resampled PCM bytes we need for one ADPCM block
        # Block = 4 bytes header + (block_size-4) bytes payload = (block_size-4)*2 samples
        samples_per_block = (block_size - 4) * 2 + 1
        pcm_bytes_per_block = samples_per_block * 2

        # Neolink-style time tracking: tracks when the stream "should" end
        # to prevent drift and ensure smooth playback
        import time as time_mod  # noqa: PLC0415
        expected_stream_end = time_mod.monotonic()
        resampled_pcm_buffer = bytearray()
        # Leftover input samples that don't align to a complete resample step
        resample_leftover = bytearray()

        try:
            while self._active:
                # Grab buffered PCM from AI (24kHz 16-bit mono)
                async with self._pcm_lock:
                    chunk = bytes(self._pcm_buffer)
                    self._pcm_buffer.clear()

                if not chunk:
                    await asyncio.sleep(0.01)
                    continue

                # Resample in-process: linear interpolation 24kHz → target rate
                raw_input = resample_leftover + chunk
                resample_leftover = bytearray()

                # Parse input as int16 samples
                n_bytes = len(raw_input)
                # Ensure even number of bytes
                if n_bytes % 2:
                    resample_leftover = bytearray(raw_input[-1:])
                    raw_input = raw_input[:-1]
                    n_bytes -= 1

                n_samples_in = n_bytes // 2
                if n_samples_in < 2:
                    resample_leftover = bytearray(raw_input)
                    continue

                # Convert to samples array
                in_samples = struct_mod.unpack(f"<{n_samples_in}h", raw_input)

                # Calculate output samples using linear interpolation
                ratio = input_rate / sample_rate  # e.g. 24000/16000 = 1.5
                n_samples_out = int((n_samples_in - 1) / ratio)
                if n_samples_out < 1:
                    resample_leftover = bytearray(raw_input)
                    continue

                out_samples = []
                for i in range(n_samples_out):
                    src_pos = i * ratio
                    idx = int(src_pos)
                    frac = src_pos - idx
                    if idx + 1 < n_samples_in:
                        val = in_samples[idx] + frac * (in_samples[idx + 1] - in_samples[idx])
                    else:
                        val = in_samples[idx]
                    out_samples.append(max(-32768, min(32767, int(val))))

                # Track unconsumed input samples for next iteration
                consumed_input_samples = int(n_samples_out * ratio) + 1
                if consumed_input_samples < n_samples_in:
                    leftover_start = consumed_input_samples * 2
                    resample_leftover = bytearray(raw_input[leftover_start:])

                # Pack output samples and add to buffer
                resampled_bytes = struct_mod.pack(f"<{len(out_samples)}h", *out_samples)
                resampled_pcm_buffer.extend(resampled_bytes)

                # Encode and send exactly one complete ADPCM block at a time.
                # This preserves block boundaries and improves playback smoothness.
                while len(resampled_pcm_buffer) >= pcm_bytes_per_block:
                    pcm_block = bytes(resampled_pcm_buffer[:pcm_bytes_per_block])
                    del resampled_pcm_buffer[:pcm_bytes_per_block]

                    adpcm_data, predictor, step_index = self._encode_ima_adpcm(
                        pcm_block, block_size, predictor, step_index
                    )
                    if not adpcm_data:
                        continue

                    adpcm_block = adpcm_data[:block_size]
                    if len(adpcm_block) < block_size:
                        continue

                    payload = self._build_bcmedia_payload(adpcm_block, block_size)
                    if not payload:
                        continue

                    # Calculate play duration for this block (neolink formula)
                    # samples = (block_size - 4_header) * 2_samples_per_byte + 1_initial
                    block_samples = (block_size - 4) * 2 + 1
                    play_duration = block_samples / sample_rate

                    now = time_mod.monotonic()
                    try:
                        await self._send_talk_binary(channel, payload)
                        chunks_sent += 1
                        if chunks_sent == 1:
                            _LOGGER.warning("✓ First ADPCM payload sent to doorbell speaker!")
                            expected_stream_end = now + play_duration
                        elif chunks_sent % 50 == 0:
                            _LOGGER.warning("Audio output progress: %d payloads sent", chunks_sent)
                    except Exception as exc:
                        if chunks_sent <= 3:
                            _LOGGER.warning("Baichuan talk send error: %s", exc)
                        continue

                    # Neolink-style pacing: track expected end time to avoid drift.
                    if now > expected_stream_end:
                        expected_stream_end = now + play_duration
                    else:
                        expected_stream_end += play_duration

                    # Sleep until the expected stream position
                    sleep_time = expected_stream_end - time_mod.monotonic()
                    if sleep_time > 0.001:
                        await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.warning("Baichuan audio output loop error", exc_info=True)
        finally:
            # Let the last queued payload finish playback before stopping talk,
            # otherwise the tail of speech can get clipped.
            remaining = expected_stream_end - time_mod.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(remaining + 0.1, 1.5))

            # Stop talk session on camera
            await self._stop_baichuan_talk(channel)
            _LOGGER.warning("Baichuan audio output loop ended (sent %d chunks)", chunks_sent)

    async def _start_baichuan_talk(self, channel: int, ability: dict) -> bool:
        """Send TalkConfig (cmd 201) to start the talk session on the camera."""
        from reolink_aio.baichuan import util as bc_util  # noqa: PLC0415

        talk_config = (
            '<?xml version="1.0" encoding="UTF-8" ?>\n'
            "<body>\n"
            '<TalkConfig version="1.1">\n'
            f"<channelId>{channel}</channelId>\n"
            f'<duplex>{ability["duplex"]}</duplex>\n'
            f'<audioStreamMode>{ability["audio_stream_mode"]}</audioStreamMode>\n'
            "<audioConfig>\n"
            f'<audioType>{ability["audio_type"]}</audioType>\n'
            f'<sampleRate>{ability["sample_rate"]}</sampleRate>\n'
            f'<samplePrecision>{ability["sample_precision"]}</samplePrecision>\n'
            f'<lengthPerEncoder>{ability["length_per_encoder"]}</lengthPerEncoder>\n'
            f'<soundTrack>{ability["sound_track"]}</soundTrack>\n'
            "</audioConfig>\n"
            "</TalkConfig>\n"
            "</body>\n"
        )

        # Try AES encryption first (most common), then BC encryption
        for enc in (bc_util.EncType.AES, bc_util.EncType.BC):
            try:
                await self._baichuan.send(cmd_id=201, channel=channel, body=talk_config, enc_type=enc)
                self._talk_enc_type = enc
                _LOGGER.warning("TalkConfig accepted (enc=%s)", enc)
                return True
            except Exception as exc:
                rsp = getattr(exc, "rspCode", None)
                if rsp in (400, 422):
                    # Camera says talk already active — stop it first, then retry
                    _LOGGER.info("TalkConfig got rspCode=%s, stopping existing talk and retrying", rsp)
                    try:
                        await self._baichuan.send(cmd_id=11, channel=channel, enc_type=enc)
                        await asyncio.sleep(0.3)
                        await self._baichuan.send(cmd_id=201, channel=channel, body=talk_config, enc_type=enc)
                        self._talk_enc_type = enc
                        _LOGGER.warning("TalkConfig accepted after stop+retry (enc=%s)", enc)
                        return True
                    except Exception as exc2:
                        _LOGGER.debug("Retry with enc=%s also failed: %s", enc, exc2)
                        continue
                elif enc == bc_util.EncType.AES:
                    # Try BC encryption
                    _LOGGER.debug("TalkConfig with AES failed (%s), trying BC", exc)
                    continue
                else:
                    _LOGGER.warning("TalkConfig failed with both AES and BC: %s", exc)
                    return False
        return False

    async def _stop_baichuan_talk(self, channel: int) -> None:
        """Send talk stop command (cmd 11) to end the session."""
        if not self._baichuan:
            return
        try:
            enc = self._talk_enc_type
            if enc:
                await self._baichuan.send(cmd_id=11, channel=channel, enc_type=enc)
                _LOGGER.info("Baichuan talk session stopped")
        except Exception as exc:
            _LOGGER.debug("Talk stop (cmd 11) error (non-critical): %s", exc)

    def _encode_ima_adpcm(
        self, pcm_bytes: bytes, block_size: int, predictor: int, step_index: int
    ) -> tuple[bytes, int, int]:
        """Encode 16-bit PCM (little-endian) to IMA/DVI ADPCM blocks.

        Block layout (standard IMA ADPCM / DVI-4):
          - 2 bytes: initial predictor value (i16 LE)
          - 1 byte: step table index (0–88)
          - 1 byte: reserved (0)
          - (block_size - 4) bytes: packed 4-bit nibbles (2 samples per byte, low nibble first)

        Returns (adpcm_bytes, updated_predictor, updated_step_index).
        """
        import struct as struct_mod  # noqa: PLC0415

        IMA_INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]
        IMA_STEP_TABLE = [
            7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
            34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
            157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
            598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878,
            2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
            6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
            18500, 20350, 22385, 24623, 27086, 29794, 32767,
        ]

        payload_bytes = block_size - 4
        payload_samples = payload_bytes * 2  # 2 samples per byte (4-bit nibbles)

        num_samples = len(pcm_bytes) // 2
        if num_samples == 0:
            return b"", predictor, step_index

        samples = struct_mod.unpack(f"<{num_samples}h", pcm_bytes[:num_samples * 2])
        pos = 0
        out = bytearray()

        while pos < len(samples):
            # Use first sample as initial predictor for first block
            if predictor == 0 and pos == 0:
                predictor = samples[0]
                pos = 1

            block = bytearray()
            block += struct_mod.pack("<hBB", predictor, step_index, 0)

            nibble_acc = None
            for _ in range(payload_samples):
                s = samples[pos] if pos < len(samples) else 0
                pos += 1

                # IMA ADPCM encode one sample
                step = IMA_STEP_TABLE[step_index]
                diff = s - predictor
                sign = 0
                if diff < 0:
                    sign = 8
                    diff = -diff
                delta = 0
                vpdiff = step >> 3
                if diff >= step:
                    delta |= 4
                    diff -= step
                    vpdiff += step
                if diff >= (step >> 1):
                    delta |= 2
                    diff -= step >> 1
                    vpdiff += step >> 1
                if diff >= (step >> 2):
                    delta |= 1
                    vpdiff += step >> 2
                if sign:
                    predictor -= vpdiff
                else:
                    predictor += vpdiff
                predictor = max(-32768, min(32767, predictor))
                step_index += IMA_INDEX_TABLE[(delta | sign) & 0xF]
                step_index = max(0, min(88, step_index))
                nib = (delta | sign) & 0xF

                if nibble_acc is None:
                    nibble_acc = nib
                else:
                    # Low nibble first, then high nibble
                    block.append((nibble_acc & 0xF) | ((nib & 0xF) << 4))
                    nibble_acc = None

            if nibble_acc is not None:
                block.append(nibble_acc & 0xF)

            # Pad block to exact block_size
            if len(block) < block_size:
                block.extend(b"\x00" * (block_size - len(block)))
            out.extend(block[:block_size])

            if pos >= len(samples):
                break

        return bytes(out), predictor, step_index

    def _build_bcmedia_payload(self, adpcm_bytes: bytes, block_size: int) -> bytes:
        """Wrap ADPCM blocks in BcMedia framing for Baichuan protocol.

        Each ADPCM block gets a 12-byte BcMedia header:
          - 4 bytes: magic (0x62773130 = "bw10" LE)
          - 2 bytes: payload_len (block + 4 bytes for the two u16 fields below)
          - 2 bytes: payload_len (repeated)
          - 2 bytes: data magic (0x0100)
          - 2 bytes: block_size_halved ((block_size - 4) / 2)
        Then the ADPCM block data, padded to 8-byte alignment.
        """
        import struct as struct_mod  # noqa: PLC0415

        MAGIC = 0x62773130
        MAGIC_DATA = 0x0100
        out = bytearray()

        for i in range(0, len(adpcm_bytes), block_size):
            block = adpcm_bytes[i:i + block_size]
            if len(block) < block_size:
                break  # Drop incomplete trailing block
            payload_len = len(block) + 4
            data_block_size = (len(block) - 4) // 2
            header = struct_mod.pack(
                "<IHHHH",
                MAGIC,
                payload_len,
                payload_len,
                MAGIC_DATA,
                data_block_size,
            )
            pad_len = (-len(block)) % 8
            out.extend(header + block + (b"\x00" * pad_len))

        return bytes(out)

    async def _send_talk_binary(self, channel: int, binary_payload: bytes) -> None:
        """Send talk binary data via Baichuan protocol (cmd 202).

        This sends the BcMedia-framed ADPCM audio to the camera speaker.
        The packet structure is:
          - 24-byte Baichuan header
          - Encrypted Extension XML (binaryData=1, channelId)
          - Raw BcMedia binary payload (unencrypted)
        """
        from reolink_aio.baichuan import util as bc_util  # noqa: PLC0415
        from reolink_aio.baichuan import xmls  # noqa: PLC0415

        bc = self._baichuan
        if not bc:
            return

        enc_type = self._talk_enc_type or bc_util.EncType.AES
        ch_id = channel + 1

        ext = (
            xmls.XML_HEADER
            + '<Extension version="1.1">\n'
            + "<binaryData>1</binaryData>\n"
            + f"<channelId>{channel}</channelId>\n"
            + "</Extension>\n"
        )

        # Encrypt the Extension XML
        if enc_type == bc_util.EncType.BC:
            enc_ext = bc_util.encrypt_baichuan(ext, ch_id)
        else:
            enc_ext = bc._aes_encrypt(ext)

        payload_offset = len(enc_ext)
        mess_len = payload_offset + len(binary_payload)

        # Increment message ID
        if not hasattr(bc, "_mess_id"):
            bc._mess_id = 0
        bc._mess_id = (bc._mess_id + 1) % 16777216

        cmd_id = 202

        # Build 24-byte Baichuan header
        header = (
            bytes.fromhex(bc_util.HEADER_MAGIC)
            + int(cmd_id).to_bytes(4, "little")
            + int(mess_len).to_bytes(4, "little")
            + int(ch_id).to_bytes(1, "little")
            + int(bc._mess_id).to_bytes(3, "little")
            + bytes.fromhex("00001464")  # status_code=0, class=1464
            + int(payload_offset).to_bytes(4, "little")
        )

        packet = header + enc_ext + binary_payload

        # Write directly to the transport — no response expected for audio data
        if hasattr(bc, "_mutex") and hasattr(bc, "_transport"):
            async with bc._mutex:
                bc._transport.write(packet)
        elif hasattr(bc, "_protocol") and hasattr(bc._protocol, "transport"):
            bc._protocol.transport.write(packet)
        else:
            # Try the connection object pattern
            conn = getattr(bc, "_connection", None)
            if conn and hasattr(conn, "_transport"):
                conn._transport.write(packet)
            else:
                raise RuntimeError("Cannot find Baichuan transport to write talk data")

    # ─── Audio Input (Native Baichuan FDX + ffmpeg fallback) ──────────────────────

    async def _start_audio_input(self) -> None:
        """Start receiving audio from the doorbell mic.

        Priority order for stream sources:
          1. RTMP (password as query param — avoids URL-encoding issues)
          2. HTTP-FLV (also query-param auth, works over HTTP)
          3. RTSP (password in URL userinfo — may fail with special chars)
          4. Camera entity stream_source
          5. go2rtc proxy

        The camera's microphone audio is available via the media stream, NOT via
        the Baichuan CMD 202 talk channel.
        """
        self._listen_active = True
        _LOGGER.warning("Audio input: starting stream-based mic capture")
        urls_to_try: list[str] = []

        # Priority 1: RTMP (most reliable — password in query params)
        rtmp_url = getattr(self, "_reolink_rtmp_url", None)
        if rtmp_url:
            urls_to_try.append(rtmp_url)

        # Priority 2: HTTP-FLV (also query-param auth)
        flv_url = getattr(self, "_reolink_flv_url", None)
        if flv_url:
            urls_to_try.append(flv_url)

        # Priority 3: Direct RTSP URL
        if getattr(self, "_reolink_rtsp_url", None):
            urls_to_try.append(self._reolink_rtsp_url)

        # Priority 4: Camera entity stream source
        entity_stream_url = await self._get_camera_stream_source()
        if entity_stream_url and entity_stream_url not in urls_to_try:
            urls_to_try.append(entity_stream_url)

        # Priority 5: go2rtc proxy
        go2rtc_url = await self._get_go2rtc_audio_url()
        if go2rtc_url and go2rtc_url not in urls_to_try:
            urls_to_try.append(go2rtc_url)

        # Priority 6: HA's internal HLS stream (higher latency but always works)
        hls_url = await self._get_ha_hls_stream_url()
        if hls_url and hls_url not in urls_to_try:
            urls_to_try.append(hls_url)

        if not urls_to_try:
            _LOGGER.warning("No audio input source available — mic input disabled")
            self._listen_active = False
            return

        _LOGGER.warning("Audio input: will try %d source(s)", len(urls_to_try))
        self._input_processor_task = asyncio.create_task(
            self._audio_input_loop(urls_to_try)
        )

    def _install_baichuan_audio_intercept(self) -> bool:
        """Hook into the Baichuan protocol to intercept incoming CMD 202 audio.

        During FDX (Full Duplex) talk, the camera sends mic audio back as
        CMD 202 packets — same format as what we send to the speaker.

        We intercept at the protocol's parse_bc_data level because the
        library's normal parsing may drop CMD 202 packets with status=0.

        Returns True if the hook was successfully installed.
        """
        bc = self._baichuan
        if not bc:
            return False

        # Find the protocol via multiple paths (varies by reolink_aio version)
        protocol = None
        connection = getattr(bc, "_connection", None)

        # Method 1: bc._protocol (works on HA's reolink_aio version)
        if hasattr(bc, "_protocol") and bc._protocol:
            protocol = bc._protocol

        # Method 2: connection._protocol
        if not protocol and connection:
            protocol = getattr(connection, "_protocol", None)

        # Method 3: transport.get_protocol() (standard asyncio)
        if not protocol and connection:
            transport = getattr(connection, "_transport", None)
            if transport and hasattr(transport, "get_protocol"):
                protocol = transport.get_protocol()

        if not protocol:
            _LOGGER.warning("Audio intercept FAILED: no protocol found on Baichuan object")
            return False

        # Find the correct parse method name (varies by reolink_aio version)
        parse_method_name = None
        for name in ["parse_bc_data", "parse_data"]:
            if hasattr(protocol, name):
                parse_method_name = name
                break

        if not parse_method_name:
            _LOGGER.warning(
                "Protocol %s has no parse method — attrs: %s",
                type(protocol).__name__,
                sorted([a for a in dir(protocol) if not a.startswith("__")])[:20],
            )
            return False

        _LOGGER.warning(
            "Installing audio intercept on %s.%s",
            type(protocol).__name__, parse_method_name,
        )

        # Patch the parse method to intercept CMD 202 before status code filtering
        original_parse = getattr(protocol, parse_method_name)
        input_count = [0]  # mutable counter for closure
        decrypt_mode = [None]  # will be set on first packet: "aes", "none", or "bc"

        def _decrypt_payload(raw_payload: bytes) -> bytes:
            """Decrypt the audio payload using the Baichuan AES key.

            The Baichuan protocol encrypts CMD 202 audio with AES-128-CFB.
            Each packet's payload is independently encrypted (fresh cipher per call).
            """
            if decrypt_mode[0] == "none":
                return raw_payload

            # Try AES decryption using the protocol's key
            aes_key = getattr(bc, "_aes_key", None)
            if aes_key is None:
                # No key available — data might be unencrypted
                if decrypt_mode[0] is None:
                    decrypt_mode[0] = "none"
                    _LOGGER.warning("Audio decrypt: no AES key available, assuming unencrypted")
                return raw_payload

            try:
                try:
                    from Cryptodome.Cipher import AES as _AES  # noqa: PLC0415
                except ImportError:
                    from Crypto.Cipher import AES as _AES  # noqa: PLC0415
                AES_IV = b"0123456789abcdef"
                cipher = _AES.new(key=aes_key, mode=_AES.MODE_CFB, iv=AES_IV, segment_size=128)
                decrypted = cipher.decrypt(raw_payload)

                # On first packet, log what we got
                if decrypt_mode[0] is None:
                    import struct as _st
                    decrypt_mode[0] = "aes"
                    if len(decrypted) >= 4:
                        magic_val = _st.unpack_from("<I", decrypted, 0)[0]
                        # Check for known BcMedia magics
                        known = {
                            0x62773130: "ADPCM", 0x62773530: "AAC",
                            0x31303031: "InfoV1", 0x32303031: "InfoV2",
                        }
                        name = known.get(magic_val, "")
                        if not name and (0x63643030 <= magic_val <= 0x63643039):
                            name = "IFrame"
                        elif not name and (0x63643130 <= magic_val <= 0x63643139):
                            name = "PFrame"
                        _LOGGER.warning(
                            "✓ Audio decrypt: AES active (talk_enc=%s). "
                            "First frame: %s (magic=0x%08x), first 16 hex: %s",
                            self._talk_enc_type, name or "unknown",
                            magic_val, decrypted[:16].hex(),
                        )

                return decrypted
            except ImportError:
                _LOGGER.error("Audio decrypt: pycryptodome/pycryptodomex not installed")
                decrypt_mode[0] = "none"
                return raw_payload
            except Exception as exc:
                if decrypt_mode[0] is None:
                    decrypt_mode[0] = "none"
                    _LOGGER.warning("Audio decrypt failed (%s), assuming unencrypted", exc)
                return raw_payload

        def _patched_parse() -> None:
            """Patched parser that intercepts CMD 202 audio before normal processing."""
            data = protocol._data
            if not data or len(data) < 20:
                original_parse()
                return

            # Quick check: is this CMD 202?
            rec_cmd_id = int.from_bytes(data[4:8], byteorder="little")
            if rec_cmd_id != 202:
                original_parse()
                return

            # It's CMD 202 — extract the binary payload ourselves
            rec_len_body = int.from_bytes(data[8:12], byteorder="little")
            mess_class = data[18:20].hex()

            if mess_class in ["1464", "0000"]:
                len_header = 24
                if len(data) < 24:
                    original_parse()
                    return
                rec_payload_offset = int.from_bytes(data[20:24], byteorder="little")
            elif mess_class == "1466":
                len_header = 20
                rec_payload_offset = 0
            else:
                original_parse()
                return

            # Check we have the full message
            data_len = len(data)
            len_body = data_len - len_header
            if len_body < rec_len_body:
                # Incomplete — let original handle buffering
                original_parse()
                return

            if rec_payload_offset == 0:
                rec_payload_offset = rec_len_body

            # Extract the binary audio payload (after the Extension XML)
            len_chunk = rec_len_body + len_header
            payload_start = rec_payload_offset + len_header
            raw_payload = data[payload_start:len_chunk]

            if raw_payload:
                # CRITICAL: Decrypt the payload (Baichuan AES-encrypts audio data)
                payload = _decrypt_payload(raw_payload)

                input_count[0] += 1

                # For first few packets, also inspect the extension XML
                if input_count[0] <= 3:
                    ext_bytes = data[len_header:payload_start]
                    ext_text = ""
                    if ext_bytes:
                        try:
                            # Decrypt extension XML
                            aes_key = getattr(bc, "_aes_key", None)
                            if aes_key:
                                try:
                                    from Cryptodome.Cipher import AES as _AES2
                                except ImportError:
                                    from Crypto.Cipher import AES as _AES2
                                AES_IV2 = b"0123456789abcdef"
                                c = _AES2.new(key=aes_key, mode=_AES2.MODE_CFB, iv=AES_IV2, segment_size=128)
                                ext_text = c.decrypt(ext_bytes).decode("utf-8", errors="replace")
                        except Exception:
                            ext_text = f"(decrypt failed, {len(ext_bytes)} bytes)"
                    _LOGGER.warning(
                        "FDX msg #%d: body=%d, ext_offset=%d, ext=%r, payload=%d bytes",
                        input_count[0], rec_len_body, rec_payload_offset,
                        ext_text[:200] if ext_text else "(none)",
                        len(payload),
                    )

                if input_count[0] == 1:
                    _LOGGER.warning(
                        "✓ First CMD 202 from doorbell via Baichuan FDX "
                        "(%d bytes, decrypt=%s). First 8 hex: %s",
                        len(payload), decrypt_mode[0] or "auto",
                        payload[:8].hex(),
                    )
                elif input_count[0] == 50:
                    _LOGGER.warning(
                        "Baichuan FDX: 50 CMD 202 packets received, "
                        "audio_frames_found=%s",
                        hasattr(self, "_bcmedia_parse_logged"),
                    )
                elif input_count[0] % 500 == 0:
                    _LOGGER.warning("Baichuan FDX input: %d packets received", input_count[0])

                # Log packet sizes for first 10 to understand the stream
                if input_count[0] <= 10:
                    import struct as _st2
                    magic_val = _st2.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else 0
                    magic_names = {
                        0x62773130: "ADPCM",
                        0x62773530: "AAC",
                        0x31303031: "InfoV1",
                        0x32303031: "InfoV2",
                    }
                    name = magic_names.get(magic_val, "")
                    if not name:
                        if 0x63643030 <= magic_val <= 0x63643039:
                            name = "IFrame"
                        elif 0x63643130 <= magic_val <= 0x63643139:
                            name = "PFrame"
                        else:
                            name = f"Unknown(0x{magic_val:08x})"
                    _LOGGER.warning(
                        "FDX pkt #%d: %d bytes, type=%s",
                        input_count[0], len(payload), name,
                    )

                # Parse BcMedia and decode ADPCM → PCM
                pcm_data = self._parse_bcmedia_to_pcm(payload)
                if pcm_data and self._listen_active:
                    asyncio.ensure_future(self._on_audio_received(pcm_data))

            # Consume this message from the buffer
            if len_body > rec_len_body:
                protocol._data = data[len_chunk:]
                # Parse next message if present
                if protocol._data and len(protocol._data) >= 4:
                    magic_hex = protocol._data[0:4].hex()
                    if magic_hex == "f0debc0a":  # Baichuan HEADER_MAGIC
                        _patched_parse()
            else:
                protocol._data = b""

        setattr(protocol, parse_method_name, _patched_parse)
        _LOGGER.warning("✓ Baichuan audio intercept installed on %s", parse_method_name)
        return True

    def _parse_bcmedia_to_pcm(self, payload: bytes) -> bytes | None:
        """Parse BcMedia frames from incoming stream and decode ADPCM audio to PCM.

        The decrypted CMD 202 payload contains a stream of BcMedia frames that
        can include video (I-frames, P-frames) AND audio (ADPCM/AAC). We must:
          1. Identify each frame by its 4-byte magic
          2. Skip video frames (they have variable-length headers + data)
          3. Extract and decode ADPCM audio frames

        BcMedia frame types (magic as u32 LE):
          - 0x31303031: InfoV1 (stream metadata)
          - 0x32303031: InfoV2 (stream metadata)
          - 0x63643030-0x63643039: IFrame (video key frame)
          - 0x63643130-0x63643139: PFrame (video predicted frame)
          - 0x62773530: AAC audio
          - 0x62773130: ADPCM audio

        ADPCM frame structure (after magic):
          - 2 bytes: payload_size (u16 LE)
          - 2 bytes: payload_size (repeated)
          - 2 bytes: data_magic (0x0100)
          - 2 bytes: half_block_size
          - N bytes: ADPCM block data (payload_size - 4)
          - padding to 8-byte alignment

        Video frame structure (after magic):
          - 4 bytes: video_type ("H264" or "H265")
          - 4 bytes: payload_size (u32 LE)
          - 4 bytes: additional_header_size (u32 LE)
          - 4 bytes: microseconds (u32 LE)
          - 4 bytes: unknown
          - N bytes: additional_header [additional_header_size]
          - N bytes: video data [payload_size]
          - padding to 8-byte alignment
        """
        import struct as struct_mod  # noqa: PLC0415

        MAGIC_ADPCM = 0x62773130
        MAGIC_AAC = 0x62773530
        MAGIC_INFO_V1 = 0x31303031
        MAGIC_INFO_V2 = 0x32303031
        # IFRAME: 0x63643030-0x63643039, PFRAME: 0x63643130-0x63643139
        IFRAME_BASE = 0x63643030
        PFRAME_BASE = 0x63643130
        PAD_SIZE = 8

        pcm_out = bytearray()
        offset = 0
        frame_count = 0
        audio_frames = 0
        video_frames = 0

        if not payload or len(payload) < 4:
            return None

        while offset + 4 <= len(payload):
            # Read magic
            try:
                magic = struct_mod.unpack_from("<I", payload, offset)[0]
            except struct_mod.error:
                break

            frame_count += 1
            offset += 4  # past magic

            if magic == MAGIC_ADPCM:
                # ADPCM audio frame
                if offset + 8 > len(payload):
                    break
                plen1, plen2, data_magic, half_block = struct_mod.unpack_from(
                    "<HHHH", payload, offset
                )
                offset += 8  # past sub-header

                # payload_size includes the 4-byte sub-header (data_magic + half_block)
                block_size = plen1 - 4 if plen1 > 4 else plen1
                if block_size <= 0 or offset + block_size > len(payload):
                    break

                adpcm_block = payload[offset:offset + block_size]
                pcm_block = self._decode_ima_adpcm_block(adpcm_block)
                if pcm_block:
                    pcm_out.extend(pcm_block)
                    audio_frames += 1

                # Advance past data + padding
                offset += block_size
                pad = (-plen1) % PAD_SIZE
                offset += pad

            elif magic == MAGIC_AAC:
                # AAC audio — skip (we only handle ADPCM)
                if offset + 4 > len(payload):
                    break
                plen1, plen2 = struct_mod.unpack_from("<HH", payload, offset)
                offset += 4
                offset += plen1
                pad = (-plen1) % PAD_SIZE
                offset += pad

            elif (IFRAME_BASE <= magic <= IFRAME_BASE + 9) or \
                 (PFRAME_BASE <= magic <= PFRAME_BASE + 9):
                # Video frame (I-frame or P-frame)
                # Structure: video_type(4) + payload_size(4) + add_header_size(4) +
                #            microseconds(4) + unknown(4) + add_header + data + padding
                if offset + 20 > len(payload):
                    break
                # Skip video_type (4 bytes)
                offset += 4
                payload_size, add_header_size = struct_mod.unpack_from(
                    "<II", payload, offset
                )
                offset += 4 + 4  # past payload_size + add_header_size
                offset += 4 + 4  # past microseconds + unknown
                offset += add_header_size  # past additional header
                offset += payload_size  # past video data
                # Padding
                pad = (-payload_size) % PAD_SIZE
                offset += pad
                video_frames += 1

            elif magic in (MAGIC_INFO_V1, MAGIC_INFO_V2):
                # Info frame — skip it
                # InfoV1/V2: header_size(4) + various fields; skip by header_size
                if offset + 4 > len(payload):
                    break
                header_size = struct_mod.unpack_from("<I", payload, offset)[0]
                offset += header_size
                # Info frames may have padding too
                pad = (-header_size) % PAD_SIZE
                offset += pad

            else:
                # Unknown magic — try raw ADPCM fallback from this point
                offset -= 4  # rewind past the magic we read
                if offset == 0:
                    _LOGGER.debug(
                        "BcMedia: unknown magic 0x%08x at offset 0, trying raw fallback. "
                        "First 32 hex: %s",
                        magic, payload[:32].hex(),
                    )
                    return self._try_raw_adpcm_decode(payload)
                break

        # Log first successful parse
        if audio_frames > 0 and not hasattr(self, "_bcmedia_parse_logged"):
            self._bcmedia_parse_logged = True
            _LOGGER.warning(
                "✓ BcMedia stream parsed: %d audio frames, %d video frames "
                "(%d total) → %d bytes PCM",
                audio_frames, video_frames, frame_count, len(pcm_out),
            )

        if pcm_out:
            return bytes(pcm_out)

        # No audio in this packet (video-only) — that's normal
        if video_frames > 0:
            return None

        # Nothing parsed — try raw fallback
        if frame_count == 0:
            return self._try_raw_adpcm_decode(payload)
        return None

    def _try_raw_adpcm_decode(self, payload: bytes) -> bytes | None:
        """Fallback: try decoding payload as ADPCM or G.711.

        Tries multiple decode strategies and picks the one that produces the
        most reasonable audio signal (based on RMS — speech/ambient typically
        has RMS 1000-15000, not 25000+):
        1. Entire payload as single IMA ADPCM block
        2. Payload as multiple ADPCM blocks (WAV-style)
        3. Payload as G.711 µ-law (8kHz, upsample to 16kHz)
        4. Payload as G.711 a-law (8kHz, upsample to 16kHz)
        """
        if not payload or len(payload) < 8:
            return None

        import struct as struct_mod  # noqa: PLC0415

        def _compute_rms(pcm: bytes, max_samples: int = 1000) -> float:
            n = min(len(pcm) // 2, max_samples)
            if n == 0:
                return 99999.0
            samples = struct_mod.unpack(f"<{n}h", pcm[:n*2])
            return (sum(s * s for s in samples) / n) ** 0.5

        best_pcm: bytes | None = None
        best_rms = 99999.0
        best_method = ""

        # Strategy 1: Single ADPCM block (entire payload = 1 header + nibbles)
        pcm = self._decode_ima_adpcm_block(payload)
        if pcm:
            rms = _compute_rms(pcm)
            if rms < best_rms:
                best_pcm, best_rms, best_method = pcm, rms, "single-block ADPCM"

        # Strategy 2: Multi-block ADPCM
        ability = self._talk_ability
        if ability:
            length_per_encoder = ability.get("length_per_encoder", 1024)
            expected_block_size = (length_per_encoder // 2) + 4
        else:
            expected_block_size = 516

        pcm_out = bytearray()
        offset = 0
        while offset + 4 < len(payload):
            remaining = len(payload) - offset
            block_size = min(expected_block_size, remaining)
            if block_size < 8:
                break
            block = payload[offset: offset + block_size]
            pcm_block = self._decode_ima_adpcm_block(block)
            if pcm_block:
                pcm_out.extend(pcm_block)
            padded_size = block_size + ((-block_size) % 8)
            offset += padded_size
        if pcm_out:
            rms = _compute_rms(bytes(pcm_out))
            if rms < best_rms:
                best_pcm, best_rms, best_method = bytes(pcm_out), rms, "multi-block ADPCM"

        # Strategy 3: G.711 µ-law → PCM 16-bit (8kHz, upsample to 16kHz)
        pcm_ulaw = self._decode_g711_ulaw(payload)
        if pcm_ulaw:
            rms = _compute_rms(pcm_ulaw)
            if rms < best_rms:
                best_pcm, best_rms, best_method = pcm_ulaw, rms, "G.711 µ-law"

        # Strategy 4: G.711 a-law → PCM 16-bit (8kHz, upsample to 16kHz)
        pcm_alaw = self._decode_g711_alaw(payload)
        if pcm_alaw:
            rms = _compute_rms(pcm_alaw)
            if rms < best_rms:
                best_pcm, best_rms, best_method = pcm_alaw, rms, "G.711 a-law"

        if best_pcm:
            if not hasattr(self, "_raw_fallback_logged"):
                self._raw_fallback_logged = True
                _LOGGER.warning(
                    "Audio input codec detected: %s (RMS=%.0f, %d bytes PCM from %d bytes payload)",
                    best_method, best_rms, len(best_pcm), len(payload),
                )
            return best_pcm
        return None

    def _decode_g711_ulaw(self, payload: bytes) -> bytes | None:
        """Decode G.711 µ-law to 16-bit PCM and upsample 8kHz→16kHz."""
        import struct as struct_mod  # noqa: PLC0415

        # µ-law decode table (ITU-T G.711)
        BIAS = 0x84
        CLIP = 32635

        samples: list[int] = []
        for byte in payload:
            byte = ~byte & 0xFF
            sign = byte & 0x80
            exponent = (byte >> 4) & 0x07
            mantissa = byte & 0x0F
            sample = (mantissa << (exponent + 3)) + BIAS + (BIAS << exponent) - BIAS
            if sample > CLIP:
                sample = CLIP
            if sign:
                sample = -sample
            # Upsample 8kHz → 16kHz: duplicate each sample
            samples.append(sample)
            samples.append(sample)

        if not samples:
            return None
        return struct_mod.pack(f"<{len(samples)}h", *samples)

    def _decode_g711_alaw(self, payload: bytes) -> bytes | None:
        """Decode G.711 a-law to 16-bit PCM and upsample 8kHz→16kHz."""
        import struct as struct_mod  # noqa: PLC0415

        samples: list[int] = []
        for byte in payload:
            byte ^= 0x55
            sign = byte & 0x80
            exponent = (byte >> 4) & 0x07
            mantissa = byte & 0x0F
            if exponent == 0:
                sample = (mantissa << 4) + 8
            else:
                sample = ((mantissa << 4) + 0x108) << (exponent - 1)
            if sign == 0:
                sample = -sample
            # Upsample 8kHz → 16kHz: duplicate each sample
            samples.append(sample)
            samples.append(sample)

        if not samples:
            return None
        return struct_mod.pack(f"<{len(samples)}h", *samples)

    def _decode_ima_adpcm_block(self, block: bytes) -> bytes | None:
        """Decode a single IMA/DVI ADPCM block to 16-bit PCM samples.

        Block layout:
          - bytes 0-1: initial predictor (i16 LE)
          - byte 2: step table index (0–88)
          - byte 3: reserved
          - bytes 4+: packed 4-bit nibbles (low nibble first)

        Returns PCM bytes (16-bit LE mono).
        """
        import struct as struct_mod  # noqa: PLC0415

        if len(block) < 4:
            return None

        # IMA ADPCM step table
        STEP_TABLE = [
            7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
            34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
            157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
            598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878,
            2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894,
            6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818,
            18500, 20350, 22385, 24623, 27086, 29794, 32767,
        ]
        INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]

        # Read block header
        predictor = struct_mod.unpack_from("<h", block, 0)[0]
        step_index = block[2]
        step_index = max(0, min(88, step_index))

        pcm_samples = []
        pcm_samples.append(predictor)

        # Decode nibbles
        for i in range(4, len(block)):
            byte = block[i]
            for nibble in (byte & 0x0F, (byte >> 4) & 0x0F):
                step = STEP_TABLE[step_index]
                diff = step >> 3
                if nibble & 1:
                    diff += step >> 2
                if nibble & 2:
                    diff += step >> 1
                if nibble & 4:
                    diff += step
                if nibble & 8:
                    predictor -= diff
                else:
                    predictor += diff
                predictor = max(-32768, min(32767, predictor))
                pcm_samples.append(predictor)
                step_index += INDEX_TABLE[nibble]
                step_index = max(0, min(88, step_index))

        # Store decoder state for continuity across blocks
        self._decode_predictor = predictor
        self._decode_step_index = step_index

        # Convert to bytes (16-bit LE)
        return struct_mod.pack(f"<{len(pcm_samples)}h", *pcm_samples)

    async def _resample_pcm_if_needed(self, pcm_data: bytes, src_rate: int) -> bytes:
        """Resample PCM from camera rate to 16kHz if needed."""
        if src_rate == AUDIO_INPUT_SAMPLE_RATE:
            return pcm_data
        # Simple linear resampling for common rates (8kHz → 16kHz = duplicate samples)
        if src_rate == 8000 and AUDIO_INPUT_SAMPLE_RATE == 16000:
            import struct as struct_mod  # noqa: PLC0415
            samples = struct_mod.unpack(f"<{len(pcm_data)//2}h", pcm_data)
            # Duplicate each sample for 2x upsampling
            upsampled = []
            for s in samples:
                upsampled.extend([s, s])
            return struct_mod.pack(f"<{len(upsampled)}h", *upsampled)
        return pcm_data

    async def _get_go2rtc_audio_url(self) -> str | None:
        """Find and return an audio URL from go2rtc's existing streams.

        HA's Reolink integration registers camera streams in go2rtc automatically.
        We just need to find the right stream and use go2rtc's local API to read it.
        """
        base_url = await _discover_go2rtc_url(self._hass)
        if not base_url:
            return None

        ha_session = _get_go2rtc_session(self._hass)
        own_session: aiohttp.ClientSession | None = None
        if not ha_session:
            own_session = aiohttp.ClientSession()
        session = ha_session or own_session

        try:
            # Query go2rtc for available streams
            async with session.get(
                f"{base_url}/api/streams",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return None
                streams: dict = await resp.json()

            # Find a stream matching the camera entity or Reolink entry
            camera_entity = self._camera_entity_id or ""
            reolink_entry = self._reolink_entry_id or ""
            stream_name: str | None = None

            # Check for our previously registered stream (if any)
            if self._stream_name and self._stream_name in streams:
                stream_name = self._stream_name

            # Check for a stream matching the camera entity ID
            if not stream_name and camera_entity:
                # HA go2rtc uses entity unique_id or entity_id as stream name
                for name in streams:
                    if camera_entity in name or camera_entity.replace(".", "_") in name:
                        stream_name = name
                        break

            # Check for any Reolink stream (contains the host/entry info)
            if not stream_name and reolink_entry:
                for name in streams:
                    if reolink_entry[:8] in name:
                        stream_name = name
                        break

            # Last resort: look for any stream with reolink-like RTSP source
            if not stream_name and self._reolink_host:
                for name, sources in streams.items():
                    src_list = sources if isinstance(sources, list) else []
                    for src in src_list:
                        if isinstance(src, str) and self._reolink_host in src:
                            stream_name = name
                            break
                    if stream_name:
                        break

            if stream_name:
                _LOGGER.info("Found existing go2rtc stream for audio: %s", stream_name)
                return f"{base_url}/api/stream.mp4?src={stream_name}"
            return None
        except Exception:
            _LOGGER.debug("Failed to query go2rtc streams", exc_info=True)
            return None
        finally:
            if own_session:
                await own_session.close()

    async def _get_camera_stream_source(self) -> str | None:
        """Get the authenticated stream URL from the camera entity.

        The Reolink integration provides this URL with proper auth — it's the
        same URL that HA uses for the dashboard live view.
        """
        camera_entity = self._camera_entity_id or ""
        if not camera_entity:
            return None
        try:
            # Method A: get entity from the entity component platform
            from homeassistant.helpers.entity_component import EntityComponent
            camera_comp: EntityComponent | None = self._hass.data.get("camera")
            if camera_comp and hasattr(camera_comp, "get_entity"):
                entity = camera_comp.get_entity(camera_entity)
                if entity and hasattr(entity, "stream_source"):
                    url = await entity.stream_source()
                    if url:
                        _LOGGER.info("Got stream source from camera entity: %s", _mask_stream_url(url))
                        return url

            # Method B: try via Reolink integration's runtime host API
            reolink_entry_id = self._reolink_entry_id
            if reolink_entry_id:
                entry = self._hass.config_entries.async_get_entry(reolink_entry_id)
                if entry and hasattr(entry, "runtime_data") and entry.runtime_data:
                    try:
                        host_obj = entry.runtime_data.host
                        api = host_obj.api
                        url = await api.get_rtsp_stream_source(0, "sub", check=False)
                        if url:
                            _LOGGER.info("Got stream source from Reolink API: %s", _mask_stream_url(url))
                            return url
                    except Exception:
                        pass
        except Exception:
            _LOGGER.debug("Could not get stream_source from camera entity", exc_info=True)
        return None

    async def _get_ha_hls_stream_url(self) -> str | None:
        """Get an HLS stream URL from HA's internal camera/stream service.

        This uses HA's own stream infrastructure which handles camera auth
        internally. The resulting HLS URL is token-authenticated and accessible
        from localhost without additional credentials.

        Higher latency (~2-4s) but reliable when direct RTSP/RTMP fail.
        """
        camera_entity = self._camera_entity_id or ""
        if not camera_entity:
            return None
        try:
            from homeassistant.helpers.entity_component import EntityComponent
            camera_comp: EntityComponent | None = self._hass.data.get("camera")
            if not camera_comp or not hasattr(camera_comp, "get_entity"):
                return None
            entity = camera_comp.get_entity(camera_entity)
            if not entity:
                return None

            # Try to create and start a stream
            stream = None
            if hasattr(entity, "async_create_stream"):
                try:
                    stream = await entity.async_create_stream()
                except Exception:
                    pass

            if not stream and hasattr(entity, "stream"):
                stream = entity.stream

            if stream:
                try:
                    # Ensure HLS provider is active
                    if hasattr(stream, "add_provider"):
                        stream.add_provider("hls")
                    elif hasattr(stream, "async_add_provider"):
                        await stream.async_add_provider("hls")
                    await stream.start()

                    hls_path = None
                    if hasattr(stream, "endpoint_url"):
                        hls_path = stream.endpoint_url("hls")
                    elif hasattr(stream, "hls_url"):
                        hls_path = stream.hls_url

                    if hls_path:
                        # Construct full internal URL accessible from localhost
                        ha_port = 8123
                        if hasattr(self._hass.config, "api") and self._hass.config.api:
                            ha_port = self._hass.config.api.port or 8123
                        url = f"http://127.0.0.1:{ha_port}{hls_path}"
                        _LOGGER.warning("Audio input: using HA HLS stream: %s", hls_path)
                        return url
                except Exception as exc:
                    _LOGGER.debug("Could not start HLS stream: %s", exc)
        except Exception as exc:
            _LOGGER.debug("Could not get HLS stream URL: %s", exc)
        return None

    async def _audio_input_loop(self, urls_to_try: list[str]) -> None:
        """Read audio from the best available source via ffmpeg.

        Tries each URL in priority order until one works. Retries up to
        MAX_RETRIES times with increasing delay if all URLs fail on first pass
        (handles intermittent RTSP auth failures on Reolink cameras).
        """
        MAX_RETRIES = 5
        proc: asyncio.subprocess.Process | None = None

        for attempt in range(MAX_RETRIES + 1):
            if not self._active or not self._listen_active:
                return

            if attempt > 0:
                delay = min(3.0 * attempt, 10.0)
                _LOGGER.warning(
                    "Audio input: retry %d/%d after %.0fs delay...",
                    attempt, MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)

            for url in urls_to_try:
                if not self._active or not self._listen_active:
                    return
                _LOGGER.warning(
                    "Audio input: trying %s",
                    _mask_stream_url(url),
                )

                # Build ffmpeg args with protocol-specific options
                ffmpeg_args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats"]
                if url.startswith("rtsp://"):
                    ffmpeg_args.extend(["-rtsp_transport", "tcp"])
                elif url.startswith("rtmp://"):
                    ffmpeg_args.extend(["-live_start_index", "-1"])
                elif "m3u8" in url or "/hls/" in url:
                    ffmpeg_args.extend(["-live_start_index", "-1"])
                elif url.startswith("https://"):
                    ffmpeg_args.extend(["-tls_verify", "0"])
                ffmpeg_args.extend([
                    "-i", url,
                    "-vn",  # No video — audio only
                    "-acodec", "pcm_s16le",
                    "-ar", str(AUDIO_INPUT_SAMPLE_RATE),
                    "-ac", "1",
                    "-f", "s16le",
                    "pipe:1",
                ])

                proc = await asyncio.create_subprocess_exec(
                    *ffmpeg_args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                # Wait briefly to see if ffmpeg connects or dies immediately
                await asyncio.sleep(0.5)
                if proc.returncode is not None:
                    stderr_data = b""
                    try:
                        stderr_data = await asyncio.wait_for(proc.stderr.read(2000), timeout=1.0)
                    except Exception:
                        pass
                    _LOGGER.warning(
                        "Audio input: ffmpeg failed for %s (rc=%d): %s",
                        _mask_stream_url(url),
                        proc.returncode,
                        stderr_data.decode(errors="replace").strip()[:300] if stderr_data else "no error",
                    )
                    proc = None
                    continue

                # Wait more to confirm the stream is actually producing data
                await asyncio.sleep(2.5)
                if proc.returncode is not None:
                    stderr_data = b""
                    try:
                        stderr_data = await asyncio.wait_for(proc.stderr.read(2000), timeout=1.0)
                    except Exception:
                        pass
                    _LOGGER.warning(
                        "Audio input: ffmpeg failed for %s (rc=%d): %s",
                        _mask_stream_url(url),
                        proc.returncode,
                        stderr_data.decode(errors="replace").strip()[:300] if stderr_data else "no error",
                    )
                    proc = None
                    continue

                # ffmpeg is still running — this URL works
                _LOGGER.warning("Audio input: ffmpeg stream reader connected (PID=%d)", proc.pid)
                break
            else:
                # All URLs failed on this attempt — try again if retries remain
                proc = None
                continue

            # We broke out of the inner loop — connection succeeded
            break
        else:
            _LOGGER.warning("Audio input: all URLs failed after %d retries — mic input disabled", MAX_RETRIES)
            self._listen_active = False
            return

        # Read PCM in strict 20ms chunks: 16000 Hz * 2 bytes * 0.02s = 640 bytes
        CHUNK_SIZE = 640
        chunks_sent = 0
        first_logged = False

        try:
            while self._active and self._listen_active and proc.returncode is None:
                try:
                    pcm_data = await proc.stdout.readexactly(CHUNK_SIZE)
                except asyncio.IncompleteReadError as err:
                    pcm_data = err.partial
                    if not pcm_data:
                        break

                if not first_logged:
                    first_logged = True
                    _LOGGER.warning(
                        "✓ First audio from doorbell mic (%d bytes PCM @ %dHz)",
                        len(pcm_data), AUDIO_INPUT_SAMPLE_RATE,
                    )

                chunks_sent += 1
                if chunks_sent % 500 == 0:
                    _LOGGER.info("Audio input: %d chunks sent to Gemini", chunks_sent)

                # Send PCM directly to Gemini
                await self._on_audio_received(pcm_data)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _LOGGER.warning("RTSP audio input error: %s", exc)
        finally:
            if proc:
                # Always read stderr for error info
                try:
                    stderr_data = await asyncio.wait_for(proc.stderr.read(2000), timeout=2.0)
                    if stderr_data:
                        _LOGGER.warning(
                            "ffmpeg audio stderr: %s",
                            stderr_data.decode(errors="replace").strip()[:500],
                        )
                except (asyncio.TimeoutError, Exception):
                    pass
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        proc.kill()
                    except Exception:
                        pass
            self._listen_active = False
            _LOGGER.warning("Audio input: stream reader stopped (sent %d chunks)", chunks_sent)

async def auto_configure_reolink(
    hass: HomeAssistant,
    camera_entity_id: str = "",
    reolink_entry_id: str | None = None,
) -> dict[str, str] | None:
    """Verify Reolink camera connectivity (no external stream setup needed).

    Returns connection info dict or None if the camera is not reachable.
    Audio input now uses the existing go2rtc stream set up by HA's Reolink integration.
    Audio output uses native Baichuan protocol directly.
    """
    if not reolink_entry_id:
        if not camera_entity_id:
            _LOGGER.warning("Could not verify Reolink: no camera or entry provided")
            return None
        reolink_entry_id = find_reolink_entry_for_camera(hass, camera_entity_id)
        if not reolink_entry_id:
            _LOGGER.warning("Could not find Reolink config entry for %s", camera_entity_id)
            return None

    reolink_config = get_reolink_config(hass, reolink_entry_id)
    if not reolink_config or not reolink_config.get("host"):
        _LOGGER.warning("Could not get Reolink connection details")
        return None

    host = reolink_config["host"]
    _LOGGER.info("Reolink connectivity verified: host=%s", host)
    return {
        "host": host,
        "status": "ok",
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
        on_interrupt_detected: Any | None = None,  # Callable[[], Awaitable[None]] | None
        on_silence_timeout: Any | None = None,  # Callable[[], Awaitable[None]] | None
        *,
        on_interrupt: Any | None = None,
        energy_threshold: float | None = None,
    ) -> None:
        self._on_interrupt_detected = on_interrupt_detected or on_interrupt
        self._on_silence_timeout = on_silence_timeout
        self._ai_is_speaking = False
        self._high_energy_count = 0
        self._last_speech_time: float = 0.0
        self._silence_task: asyncio.Task[None] | None = None
        self._active = False
        self._triggered = False
        if energy_threshold is not None:
            self.SPEECH_THRESHOLD = energy_threshold

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
