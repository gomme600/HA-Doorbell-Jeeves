"""Reolink audio handler – Baichuan protocol 2-way audio with Reolink doorbells.

This module handles:
  - Audio input: receives ADPCM from doorbell mic via Baichuan cmd 3 (preview stream)
  - Audio output: sends ADPCM to doorbell speaker via Baichuan cmd 202 (talk)
  - Both directions use the native Baichuan protocol (port 9000), bypassing RTSP entirely
  - go2rtc utilities are retained for non-Reolink device fallback
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant

from .const import AUDIO_INPUT_SAMPLE_RATE, DEFAULT_CHIME_DELAY

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
      INPUT:  Visitor speaks → go2rtc WS (if available) OR camera snapshot-based
      OUTPUT: AI responds → PCM 24kHz → ffmpeg → PCMA 8kHz → ffmpeg RTSP push to camera

    The output pipeline uses ffmpeg to push audio directly to the camera's
    RTSP backchannel. This bypasses go2rtc's limitations with backchannel
    (go2rtc WS is receive-only) by having ffmpeg maintain a persistent
    RTSP ANNOUNCE session to the camera.

    If RTSP is not available, falls back to the Reolink Baichuan protocol
    by accessing the Host object from the HA Reolink integration.
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
        self._camera_entity_id: str | None = None
        # Baichuan talk state (output — speaker)
        self._baichuan: Any = None
        self._talk_host: Any = None  # Dedicated Host object for talk connection
        self._talk_ability: dict | None = None
        self._talk_enc_type: Any = None
        self._pcm_buffer: bytearray = bytearray()
        self._ffmpeg_proc: asyncio.subprocess.Process | None = None
        self._chime_delay: float = DEFAULT_CHIME_DELAY
        # Baichuan audio input state (mic receive via cmd 3 stream)
        self._listen_host: Any = None  # Separate Host for preview/listen stream
        self._listen_bc: Any = None
        self._listen_active = False
        self._original_push_callback: Any = None
        self._audio_input_buffer: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._input_processor_task: asyncio.Task[None] | None = None

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        """Start 2-way audio: receive from doorbell mic, prepare output pipeline."""
        if self._active:
            return

        # Get Reolink connection details for direct camera access
        await self._discover_reolink_details()

        self._active = True

        # Start Baichuan talk output pipeline (ADPCM via port 9000)
        await self._start_output_pipeline()

        # Start Baichuan audio input (doorbell mic → AI via cmd 3 stream)
        await self._start_baichuan_audio_input()

        _LOGGER.warning(
            "Reolink audio handler started (stream=%s, host=%s, baichuan input=%s)",
            self._stream_name, self._reolink_host, self._listen_active,
        )

    async def _discover_reolink_details(self) -> None:
        """Get Reolink camera connection details from HA's Reolink integration.

        Accesses the runtime Host object to get the actual RTSP port and credentials.
        """
        # First try to get from the camera entity's config entry
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

            # Try to get the actual RTSP port from the runtime Host object
            entry = self._hass.config_entries.async_get_entry(reolink_entry_id)
            if entry and hasattr(entry, "runtime_data") and entry.runtime_data:
                try:
                    host_obj = entry.runtime_data.host
                    api = host_obj.api
                    port = api.rtsp_port
                    if port:
                        self._reolink_rtsp_port = port
                        _LOGGER.warning("Got RTSP port from Reolink integration: %d", port)

                    # Try to get verified RTSP URL
                    rtsp_url = await api.get_rtsp_stream_source(0, "sub", check=False)
                    if rtsp_url:
                        _LOGGER.warning("Reolink RTSP URL: %s",
                                       rtsp_url.replace(self._reolink_pass or "", "***"))
                except Exception as exc:
                    _LOGGER.warning("Could not get RTSP details from Reolink Host: %s", exc)

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

        # Stop Baichuan listen stream (cmd 4 = stop preview)
        await self._stop_baichuan_listen()

        # Logout dedicated talk connection
        if hasattr(self, "_talk_host") and self._talk_host:
            try:
                await self._talk_host.logout()
            except Exception:
                pass
            self._talk_host = None

        # Logout dedicated listen connection
        if hasattr(self, "_listen_host") and self._listen_host:
            try:
                await self._listen_host.logout()
            except Exception:
                pass
            self._listen_host = None

        # Cancel tasks
        for task in (self._output_reader_task, self._input_processor_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._output_reader_task = None
        self._input_processor_task = None

        self._baichuan = None
        self._listen_bc = None
        self._talk_ability = None
        self._talk_ability = None
        _LOGGER.warning("Reolink audio handler stopped (sent %d audio chunks)", self._send_count)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send AI audio to the doorbell speaker.

        Accepts 24kHz 16-bit PCM from Gemini. Buffers it for the output loop
        which encodes to IMA ADPCM and sends via Baichuan protocol (port 9000).
        """
        if not self._active:
            return
        self._pcm_buffer.extend(pcm_bytes)
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
        reolink_entry_id = find_reolink_entry_for_camera(self._hass, camera_entity)
        if not reolink_entry_id:
            _LOGGER.warning("No Reolink entry found for camera %s", camera_entity)
            return None

        entry = self._hass.config_entries.async_get_entry(reolink_entry_id)
        if not entry:
            return None

        # Get credentials and connection details from the Reolink config entry
        data = entry.data
        host = data.get("host", "")
        username = data.get("username", "admin")
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

        # Start ffmpeg resampler: 24kHz mono s16le → camera sample_rate mono s16le
        try:
            self._ffmpeg_proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
                "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _LOGGER.warning("ffmpeg resampler started (24kHz → %dHz)", sample_rate)
        except Exception as exc:
            _LOGGER.error("Failed to start ffmpeg resampler: %s", exc)
            await self._stop_baichuan_talk(channel)
            return

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

        # Use 1 block per payload for lower latency (neolink streaming mode)
        BLOCKS_PER_PAYLOAD = 1

        try:
            while self._active:
                # Feed buffered PCM to ffmpeg for resampling
                if self._pcm_buffer:
                    chunk = bytes(self._pcm_buffer)
                    self._pcm_buffer.clear()
                    try:
                        self._ffmpeg_proc.stdin.write(chunk)
                        await self._ffmpeg_proc.stdin.drain()
                    except Exception:
                        _LOGGER.warning("ffmpeg stdin write failed")
                        break

                # Read exactly one block's worth of resampled PCM
                read_size = pcm_bytes_per_block * BLOCKS_PER_PAYLOAD
                try:
                    resampled = await asyncio.wait_for(
                        self._ffmpeg_proc.stdout.read(read_size),
                        timeout=0.15,
                    )
                except asyncio.TimeoutError:
                    # No data ready yet — small delay and retry
                    await asyncio.sleep(0.01)
                    continue
                except Exception:
                    break

                if not resampled:
                    continue

                # Encode PCM to IMA ADPCM blocks
                adpcm_data, predictor, step_index = self._encode_ima_adpcm(
                    resampled, block_size, predictor, step_index
                )

                if not adpcm_data:
                    continue

                # Send each block individually for smooth streaming
                for offset in range(0, len(adpcm_data), block_size):
                    adpcm_block = adpcm_data[offset:offset + block_size]
                    if len(adpcm_block) < block_size:
                        break

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

                    # Neolink-style pacing: track expected end time to avoid drift
                    if now > expected_stream_end:
                        # We're behind — reset timing (gap in audio source)
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
            # Clean up ffmpeg
            if self._ffmpeg_proc and self._ffmpeg_proc.returncode is None:
                try:
                    self._ffmpeg_proc.stdin.close()
                    self._ffmpeg_proc.terminate()
                except Exception:
                    pass
            self._ffmpeg_proc = None

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

    # ─── Baichuan Audio Input (Mic Receive via cmd 3 Stream) ──────────────────

    async def _start_baichuan_audio_input(self) -> None:
        """Start receiving audio from the doorbell mic via Baichuan preview stream.

        Creates a SEPARATE Baichuan connection dedicated to receiving the
        video/audio stream (cmd 3). We intercept the TCP protocol's push
        callback to capture BcMedia ADPCM audio packets from the camera's mic.

        This replaces go2rtc-based audio input — works even when RTSP is broken.
        """
        camera_entity = self._camera_entity_id or ""
        reolink_entry_id = find_reolink_entry_for_camera(self._hass, camera_entity)
        if not reolink_entry_id:
            _LOGGER.warning("No Reolink entry found — Baichuan audio input disabled")
            return

        entry = self._hass.config_entries.async_get_entry(reolink_entry_id)
        if not entry:
            return

        data = entry.data
        host = data.get("host", "")
        username = data.get("username", "admin")
        password = data.get("password", "")
        http_port = data.get("port")
        use_https = data.get("use_https")
        bc_port = data.get("baichuan_port", 9000)

        if not host:
            _LOGGER.warning("Reolink entry has no host — audio input disabled")
            return

        try:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: PLC0415
            from reolink_aio.api import Host  # noqa: PLC0415

            listen_host = Host(
                host=host,
                username=username,
                password=password,
                port=http_port,
                use_https=use_https,
                bc_port=bc_port,
                aiohttp_get_session_callback=lambda: async_get_clientsession(self._hass),
            )
            listen_bc = listen_host.baichuan
            await listen_bc.login()
            self._listen_host = listen_host
            self._listen_bc = listen_bc
            _LOGGER.warning("Created dedicated Baichuan listen connection to %s:%d", host, bc_port)
        except Exception as exc:
            _LOGGER.warning("Failed to create Baichuan listen connection: %s", exc)
            return

        # Patch the TCP protocol to handle binary stream responses (payload_offset != 0)
        # The standard reolink_aio library rejects these, but cmd 3 stream requires it
        try:
            protocol = listen_bc._protocol
            if protocol:
                self._original_push_callback = protocol._push_callback
                protocol._push_callback = self._stream_push_callback
                self._patch_protocol_for_streaming(protocol)
                _LOGGER.info("Patched Baichuan protocol for binary stream reception")
        except Exception as exc:
            _LOGGER.warning("Failed to patch protocol: %s", exc)
            return

        # Request the sub-stream (cmd 3) — camera sends multiplexed video+audio
        # IMPORTANT: We write the request directly to the transport instead of using
        # send(), because send() waits for a response and tries to decrypt it as XML.
        # The stream response contains binary BcMedia data (payload_offset != 0),
        # which our patched parse_data handles and routes to audio extraction.
        self._listen_active = True  # Enable before writing so push callback processes data
        self._stream_buffer = bytearray()  # Buffer for accumulating partial BcMedia data
        try:
            from reolink_aio.baichuan import util as bc_util  # noqa: PLC0415

            # Build the Preview XML request body
            # NOTE: handle=256 for subStream (per neolink's protocol analysis)
            preview_body = (
                '<?xml version="1.0" encoding="UTF-8" ?>\n'
                "<body>\n"
                '<Preview version="1.1">\n'
                "<channelId>0</channelId>\n"
                "<handle>256</handle>\n"
                "<streamType>subStream</streamType>\n"
                "</Preview>\n"
                "</body>\n"
            )

            # Encrypt the body
            enc_type = bc_util.EncType.AES
            ch_id = 1  # channel 0 + 1
            enc_body = listen_bc._aes_encrypt(preview_body)

            # Increment message ID
            if not hasattr(listen_bc, "_mess_id"):
                listen_bc._mess_id = 0
            listen_bc._mess_id = (listen_bc._mess_id + 1) % 16777216
            mess_id = listen_bc._mess_id

            # Build 24-byte Baichuan header for cmd 3
            cmd_id = 3
            mess_len = len(enc_body)
            header = (
                bytes.fromhex(bc_util.HEADER_MAGIC)
                + int(cmd_id).to_bytes(4, "little")
                + int(mess_len).to_bytes(4, "little")
                + int(ch_id).to_bytes(1, "little")
                + int(mess_id).to_bytes(3, "little")
                + bytes.fromhex("00001464")  # status=0, class=0x6414
                + int(0).to_bytes(4, "little")  # payload_offset=0 (request has no binary)
            )

            packet = header + enc_body

            # Write directly to transport
            if hasattr(listen_bc, "_mutex") and hasattr(listen_bc, "_transport"):
                async with listen_bc._mutex:
                    listen_bc._transport.write(packet)
            elif hasattr(listen_bc, "_protocol") and hasattr(listen_bc._protocol, "transport"):
                listen_bc._protocol.transport.write(packet)
            else:
                raise RuntimeError("Cannot find Baichuan transport for listen connection")

            _LOGGER.warning(
                "Sent preview stream request directly to transport (handle=256, subStream)"
            )
        except Exception as exc:
            _LOGGER.warning("Failed to send preview stream request: %s", exc)
            self._listen_active = False
            return

        # Start the input processor task (decodes ADPCM → PCM and sends to Gemini)
        self._input_processor_task = asyncio.create_task(self._audio_input_processor())

    def _decrypt_binary_body(self, body: bytes) -> bytes:
        """Decrypt AES-CFB-128 encrypted binary stream body.

        Reolink cameras encrypt ALL body data (including binary stream payloads)
        with AES-CFB when in AES mode. Unlike neolink's assumption, Reolink's
        implementation DOES encrypt the binary stream.

        Uses the same key/IV as the Extension XML decryption.
        """
        if not self._listen_bc or not hasattr(self._listen_bc, "_aes_key"):
            return body
        aes_key = self._listen_bc._aes_key
        if aes_key is None:
            return body

        try:
            from Cryptodome.Cipher import AES as AES_Cipher  # noqa: PLC0415

            AES_IV = b"0123456789abcdef"
            cipher = AES_Cipher.new(key=aes_key, mode=AES_Cipher.MODE_CFB, iv=AES_IV, segment_size=128)
            return cipher.decrypt(body)
        except Exception as exc:
            if not hasattr(self, "_decrypt_err_logged"):
                self._decrypt_err_logged = True
                _LOGGER.warning("Binary stream decrypt failed: %s", exc)
            return body

    def _stream_push_callback(self, cmd_id: int, data: bytes, len_header: int) -> None:
        """Intercept Baichuan push messages to extract audio from cmd 3 stream.

        Called by the TCP protocol whenever an unsolicited message arrives.
        For cmd 3 (preview stream), we parse BcMedia packets and queue audio.

        The camera encrypts binary stream data with AES-CFB-128 (same key as
        Extension XML). We decrypt before parsing BcMedia packets.

        IMPORTANT: Must not raise exceptions — would kill the protocol parser.
        """
        try:
            if cmd_id == 3 and self._listen_active:
                # Extract body from the full packet (skip Baichuan header)
                body = data[len_header:] if len(data) > len_header else b""

                if body:
                    # Check if body is valid BcMedia (unencrypted) or needs decryption
                    KNOWN_MAGICS = (
                        b"\x31\x30\x30\x32", b"\x31\x30\x30\x31",
                        b"\x30\x31\x77\x62", b"\x30\x35\x77\x62",
                    )
                    if len(body) >= 4:
                        first4 = body[:4]
                        is_plaintext = (
                            first4 in KNOWN_MAGICS
                            or (len(body) > 3 and body[2:4] == b"\x64\x63")
                        )
                        if not is_plaintext:
                            body = self._decrypt_binary_body(body)

                    if not hasattr(self, "_push_count"):
                        self._push_count = 0
                    self._push_count += 1
                    if self._push_count <= 5 or self._push_count % 100 == 0:
                        _LOGGER.warning(
                            "Push callback cmd=3 #%d: %d body bytes, first8=%s",
                            self._push_count, len(body),
                            body[:8].hex() if len(body) >= 8 else body.hex(),
                        )
                    self._extract_audio_from_stream(body)
            elif self._original_push_callback:
                # Pass non-audio pushes to the original handler
                self._original_push_callback(cmd_id, data, len_header)
        except Exception as exc:
            if not hasattr(self, "_push_error_count"):
                self._push_error_count = 0
            self._push_error_count += 1
            if self._push_error_count <= 3:
                _LOGGER.warning("Push callback error #%d: %s", self._push_error_count, exc)

    def _patch_protocol_for_streaming(self, protocol) -> None:
        """Monkeypatch the TCP protocol's parse_data to handle binary streams.

        The standard reolink_aio library rejects messages with payload_offset != 0.
        For cmd 3 (preview/audio stream), the response contains:
          - 24-byte Baichuan header (includes payload_offset field)
          - payload_offset bytes: encrypted Extension XML
          - remaining bytes: raw BcMedia binary data

        We patch parse_data to accept these and route the binary data to our
        audio extraction method directly.
        """
        from reolink_aio.baichuan.util import HEADER_MAGIC  # noqa: PLC0415

        original_parse_data = protocol.parse_data
        handler = self  # Capture reference for closure

        def patched_parse_data():
            """Enhanced parse_data that handles payload_offset for binary streams."""
            if len(protocol._data) < 24:
                if len(protocol._data) >= 20:
                    # Check if this is a standard 20-byte header message
                    mess_class = protocol._data[18:20].hex()
                    if mess_class == "1466":
                        original_parse_data()
                        return
                # Wait for more data
                return

            # Read the header fields
            magic = protocol._data[0:4].hex()
            if magic != HEADER_MAGIC:
                original_parse_data()
                return

            mess_class = protocol._data[18:20].hex()

            # Only intercept 24-byte header messages that have payload_offset
            if mess_class not in ("1464", "0000"):
                original_parse_data()
                return

            rec_payload_offset = int.from_bytes(protocol._data[20:24], byteorder="little")

            # Diagnostic: log the first 10 messages' routing
            if not hasattr(handler, "_route_count"):
                handler._route_count = 0
            handler._route_count += 1
            if handler._route_count <= 10:
                rec_cmd_id_peek = int.from_bytes(protocol._data[4:8], byteorder="little")
                rec_len_body_peek = int.from_bytes(protocol._data[8:12], byteorder="little")
                _LOGGER.warning(
                    "Patched parse route #%d: cmd=%d, payload_offset=%d, body_len=%d, class=%s",
                    handler._route_count, rec_cmd_id_peek, rec_payload_offset,
                    rec_len_body_peek, mess_class,
                )

            if rec_payload_offset == 0:
                # Standard message — use original parser
                original_parse_data()
                return

            # --- Binary stream message (payload_offset != 0) ---
            rec_cmd_id = int.from_bytes(protocol._data[4:8], byteorder="little")
            rec_len_body = int.from_bytes(protocol._data[8:12], byteorder="little")
            rec_mess_id = int.from_bytes(protocol._data[12:16], byteorder="little")
            len_header = 24

            # Track how many messages we process
            if not hasattr(handler, "_parse_count"):
                handler._parse_count = 0
            handler._parse_count += 1

            len_body = len(protocol._data) - len_header
            if len_body < rec_len_body:
                # Wait for the rest of the body
                if handler._parse_count <= 10:
                    _LOGGER.warning(
                        "Patched parse: msg#%d cmd=%d waiting for body (%d/%d bytes)",
                        handler._parse_count, rec_cmd_id, len_body, rec_len_body,
                    )
                return

            # Extract the full chunk
            len_chunk = rec_len_body + len_header
            data_chunk = protocol._data[0:len_chunk]

            # Move past this message
            if len_body > rec_len_body:
                protocol._data = protocol._data[len_chunk:]
            else:
                protocol._data = b""

            # Check status code
            rec_status_code = int.from_bytes(data_chunk[16:18], byteorder="little")
            if rec_status_code != 200:
                # Error response — resolve any waiting future
                receive_future = protocol.receive_futures.get(rec_cmd_id, {}).get(rec_mess_id)
                if receive_future is not None and not receive_future.done():
                    from reolink_aio.exceptions import ApiError  # noqa: PLC0415
                    receive_future.set_exception(
                        ApiError(f"cmd {rec_cmd_id} status {rec_status_code}",
                                 rspCode=rec_status_code)
                    )
                return

            # Extract binary payload (after the Extension XML)
            # The first response's binary is NOT encrypted; continuation messages ARE.
            # Strategy: try raw first, if first 4 bytes match a known BcMedia magic use it.
            # Otherwise, decrypt and use the decrypted version.
            full_body = data_chunk[len_header:]
            binary_data = full_body[rec_payload_offset:] if rec_payload_offset < len(full_body) else b""

            # Check if binary_data is already valid BcMedia (unencrypted)
            KNOWN_MAGICS = (
                b"\x31\x30\x30\x32", b"\x31\x30\x30\x31",  # InfoV2, InfoV1
                b"\x30\x31\x77\x62", b"\x30\x35\x77\x62",  # ADPCM, AAC
            )
            if binary_data and len(binary_data) >= 4:
                first4 = binary_data[:4]
                is_plaintext = (
                    first4 in KNOWN_MAGICS
                    or (len(binary_data) > 3 and binary_data[2:4] == b"\x64\x63")  # IFrame/PFrame
                )
                if not is_plaintext:
                    # Try decryption
                    binary_data = handler._decrypt_binary_body(binary_data)

            # Resolve any waiting receive_future (first response to send())
            receive_future = protocol.receive_futures.get(rec_cmd_id, {}).get(rec_mess_id)
            if receive_future is not None and not receive_future.done():
                # Return Extension XML part to the caller (decrypt it)
                ext_encrypted = full_body[:rec_payload_offset]
                ext_data = handler._decrypt_binary_body(ext_encrypted)
                receive_future.set_result((ext_data, len_header))

            # Route binary data directly to audio extraction
            if binary_data and handler._listen_active:
                handler._extract_audio_from_stream(binary_data)
            elif not binary_data and handler._listen_active:
                _LOGGER.warning("Patched parse_data: binary_data empty (offset=%d, chunk_len=%d)", rec_payload_offset, len(data_chunk))

            # Continue parsing if there's more data in the buffer
            if protocol._data and len(protocol._data) >= 4:
                if protocol._data[0:4].hex() == HEADER_MAGIC:
                    patched_parse_data()

        protocol.parse_data = patched_parse_data

    def _extract_audio_from_stream(self, body: bytes) -> None:
        """Parse BcMedia packets from a cmd 3 stream chunk and extract audio.

        BcMedia multiplexes video and audio in a binary stream.
        Audio can be ADPCM (magic 0x62773130) or AAC (magic 0x62773530).
        Video frames use magics like 0x63643030 (IFrame) and 0x63643130 (PFrame).

        The stream arrives in chunks that may not align to BcMedia packet boundaries,
        so we accumulate data in self._stream_buffer and parse complete packets.

        This runs in the protocol callback (event loop) so must be FAST.
        """
        import struct as struct_mod  # noqa: PLC0415

        # BcMedia magic values (as u32 little-endian, stored as 4-byte sequences)
        MAGIC_ADPCM = b"\x30\x31\x77\x62"   # 0x62773130
        MAGIC_AAC = b"\x30\x35\x77\x62"     # 0x62773530
        MAGIC_INFO_V1 = b"\x31\x30\x30\x31"  # 0x31303031
        MAGIC_INFO_V2 = b"\x31\x30\x30\x32"  # 0x32303031 - what we actually received!
        # IFrame: 0x63643030-0x63643039, PFrame: 0x63643130-0x63643139
        MAGIC_IFRAME_BASE = b"\x30\x30\x64\x63"  # 0x63643030
        MAGIC_PFRAME_BASE = b"\x30\x31\x64\x63"  # 0x63643130

        # Diagnostic logging (first few calls only)
        if not hasattr(self, "_stream_extract_count"):
            self._stream_extract_count = 0
        self._stream_extract_count += 1
        if self._stream_extract_count <= 5:
            _LOGGER.warning(
                "Stream data #%d: %d bytes, first16hex=%s",
                self._stream_extract_count, len(body),
                body[:16].hex() if len(body) >= 16 else body.hex(),
            )

        # Accumulate into buffer for cross-chunk packet parsing
        self._stream_buffer.extend(body)
        buf = self._stream_buffer
        audio_found = 0
        pos = 0

        while pos + 8 <= len(buf):
            magic = bytes(buf[pos:pos + 4])

            # --- INFO V2 header (32 bytes total: 4 magic + 28 payload) ---
            if magic == MAGIC_INFO_V2 or magic == MAGIC_INFO_V1:
                if pos + 32 > len(buf):
                    break  # Need more data
                # Parse video dimensions for logging
                if self._stream_extract_count <= 3:
                    header_size = struct_mod.unpack_from("<I", buf, pos + 4)[0]
                    width = struct_mod.unpack_from("<I", buf, pos + 8)[0]
                    height = struct_mod.unpack_from("<I", buf, pos + 12)[0]
                    _LOGGER.warning(
                        "BcMedia InfoV2: %dx%d (header_size=%d)", width, height, header_size
                    )
                pos += 32  # Fixed size
                continue

            # --- ADPCM audio packet ---
            if magic == MAGIC_ADPCM:
                if pos + 12 > len(buf):
                    break  # Need more data
                payload_size = struct_mod.unpack_from("<H", buf, pos + 4)[0]
                # Total packet: 4 magic + 2 size + 2 size + payload_size + padding
                total_data = 8 + payload_size
                pad = (-total_data) % 8
                total_with_pad = total_data + pad
                if pos + total_with_pad > len(buf):
                    break  # Need more data

                # Extract ADPCM data (skip 4-byte sub-header: 2b data_magic + 2b half_block)
                adpcm_size = payload_size - 4
                if adpcm_size > 4:
                    adpcm_block = bytes(buf[pos + 12:pos + 12 + adpcm_size])
                    try:
                        self._audio_input_buffer.put_nowait(adpcm_block)
                        audio_found += 1
                    except asyncio.QueueFull:
                        pass
                pos += total_with_pad
                continue

            # --- AAC audio packet ---
            if magic == MAGIC_AAC:
                if pos + 8 > len(buf):
                    break
                payload_size = struct_mod.unpack_from("<H", buf, pos + 4)[0]
                total_data = 8 + payload_size
                pad = (-total_data) % 8
                total_with_pad = total_data + pad
                if pos + total_with_pad > len(buf):
                    break
                # Extract AAC frame data (starts at offset 8, after magic + 2x size)
                aac_data = bytes(buf[pos + 8:pos + 8 + payload_size])
                if aac_data and self._audio_input_buffer:
                    try:
                        self._audio_input_buffer.put_nowait(aac_data)
                        audio_found += 1
                    except asyncio.QueueFull:
                        pass  # Drop if queue is full
                if self._stream_extract_count <= 5:
                    _LOGGER.warning(
                        "BcMedia AAC packet: %d bytes (first2=%s), queued=%d",
                        payload_size,
                        aac_data[:2].hex() if len(aac_data) >= 2 else "?",
                        audio_found,
                    )
                pos += total_with_pad
                continue

            # --- IFrame / PFrame (video) — skip efficiently ---
            # Check if first byte matches IFrame/PFrame pattern
            # IFrame magic: 0x63643030-0x63643039 (bytes: 30 30/31/../39 64 63)
            # PFrame magic: 0x63643130-0x63643139 (bytes: 30 31/31/../39 64 63)
            if len(buf) > pos + 3 and buf[pos + 2:pos + 4] == b"\x64\x63":
                # This is a video frame (IFrame or PFrame)
                # Layout: 4 magic + 4 video_type_str + 4 payload_size + 4 additional_header
                #          + 4 microseconds + 4 unknown + additional_header bytes + payload + pad
                if pos + 24 > len(buf):
                    break
                payload_size = struct_mod.unpack_from("<I", buf, pos + 12)[0]
                additional = struct_mod.unpack_from("<I", buf, pos + 16)[0]
                # Total: 24 header + additional + payload + padding
                total_data = 24 + additional + payload_size
                pad = (-payload_size) % 8
                total_with_pad = total_data + pad
                if pos + total_with_pad > len(buf):
                    # Incomplete video frame — if very large, skip to save memory
                    if payload_size > 200000:
                        # Drop the incomplete frame data — too large to buffer
                        pos = len(buf)
                    break
                pos += total_with_pad
                continue

            # Unknown magic — try to skip forward to find the next known magic
            # Use bytes.find() to jump to the next potential packet start
            next_adpcm = buf.find(MAGIC_ADPCM, pos + 1)
            next_aac = buf.find(MAGIC_AAC, pos + 1)
            next_info = buf.find(MAGIC_INFO_V2, pos + 1)
            candidates = [x for x in (next_adpcm, next_aac, next_info) if x >= 0]
            if candidates:
                pos = min(candidates)
            else:
                # No known magic found — discard everything up to last 4 bytes
                pos = max(0, len(buf) - 4)
                break

        # Remove consumed data from buffer (keep unprocessed tail)
        if pos > 0:
            del self._stream_buffer[:pos]

        # Keep buffer from growing unbounded (cap at 512KB)
        if len(self._stream_buffer) > 524288:
            _LOGGER.debug("Stream buffer overflow (%d bytes) — flushing", len(self._stream_buffer))
            self._stream_buffer.clear()

        if audio_found and self._stream_extract_count <= 20:
            _LOGGER.warning(
                "Stream #%d: extracted %d audio blocks (buffer=%d bytes remaining)",
                self._stream_extract_count, audio_found, len(self._stream_buffer),
            )

    async def _audio_input_processor(self) -> None:
        """Process AAC audio frames from the Baichuan stream into PCM for Gemini.

        The camera sends AAC audio (ADTS-framed) via the cmd 3 preview stream.
        We use ffmpeg to decode AAC → 16kHz 16-bit mono PCM for Gemini input.
        """
        _LOGGER.warning(
            "Audio input processor started (Baichuan AAC → PCM %dHz for Gemini)",
            AUDIO_INPUT_SAMPLE_RATE,
        )
        blocks_decoded = 0
        decoder_proc: asyncio.subprocess.Process | None = None

        try:
            # Start ffmpeg AAC decoder → 16kHz s16le PCM
            decoder_proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner", "-loglevel", "error",
                "-f", "aac", "-i", "pipe:0",
                "-f", "s16le", "-ar", str(AUDIO_INPUT_SAMPLE_RATE), "-ac", "1", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _LOGGER.warning("Audio input decoder: AAC → PCM %dHz", AUDIO_INPUT_SAMPLE_RATE)

            # Start background reader for decoded PCM output
            read_task = asyncio.create_task(
                self._read_decoded_pcm(decoder_proc.stdout)
            )

            while self._active and self._listen_active:
                try:
                    aac_frame = await asyncio.wait_for(
                        self._audio_input_buffer.get(), timeout=2.0
                    )
                except asyncio.TimeoutError:
                    continue

                if not aac_frame:
                    continue

                blocks_decoded += 1
                if blocks_decoded == 1:
                    # Log the first frame for diagnostics
                    _LOGGER.warning(
                        "✓ First AAC frame from doorbell mic (%d bytes, sync=%s)",
                        len(aac_frame),
                        "ADTS" if aac_frame[0:1] == b"\xff" else f"0x{aac_frame[0:2].hex()}",
                    )
                elif blocks_decoded % 200 == 0:
                    _LOGGER.info("Audio input: %d AAC frames decoded", blocks_decoded)

                # Feed AAC frame to ffmpeg decoder
                if decoder_proc and decoder_proc.returncode is None:
                    try:
                        decoder_proc.stdin.write(aac_frame)
                        await decoder_proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        _LOGGER.warning("AAC decoder pipe broken after %d frames", blocks_decoded)
                        break
                    except Exception as exc:
                        _LOGGER.warning("AAC decoder write error: %s", exc)
                        break

            # Cancel the read task
            read_task.cancel()
            try:
                await read_task
            except asyncio.CancelledError:
                pass

        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.warning("Audio input processor error", exc_info=True)
        finally:
            if decoder_proc and decoder_proc.returncode is None:
                try:
                    decoder_proc.stdin.close()
                    decoder_proc.terminate()
                except Exception:
                    pass
            _LOGGER.warning("Audio input processor ended (%d AAC frames decoded)", blocks_decoded)

    async def _read_decoded_pcm(self, stdout: asyncio.StreamReader) -> None:
        """Read decoded PCM from ffmpeg stdout and send to Gemini.

        Reads in chunks matching ~20ms at 16kHz (640 bytes = 320 samples * 2 bytes).
        """
        CHUNK_SIZE = 640  # 20ms at 16kHz mono s16le
        first_sent = False

        try:
            while True:
                pcm_data = await stdout.read(CHUNK_SIZE)
                if not pcm_data:
                    break  # EOF — decoder closed

                if not first_sent:
                    first_sent = True
                    _LOGGER.warning(
                        "✓ First decoded PCM from doorbell mic (%d bytes @ %dHz) → Gemini",
                        len(pcm_data), AUDIO_INPUT_SAMPLE_RATE,
                    )

                # Send to Gemini
                await self._on_audio_received(pcm_data)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _LOGGER.warning("PCM reader error: %s", exc)

    def _decode_ima_adpcm(self, adpcm_block: bytes) -> bytes:
        """Decode a single IMA/DVI-4 ADPCM block to 16-bit PCM (little-endian).

        Block layout (standard DVI-4):
          - 2 bytes: initial predictor value (i16 LE)
          - 1 byte: step table index (0–88)
          - 1 byte: reserved (0)
          - remaining bytes: packed 4-bit nibbles (2 samples per byte, low nibble first)

        Returns raw 16-bit LE PCM bytes at the camera's native sample rate.
        """
        import struct as struct_mod  # noqa: PLC0415

        if len(adpcm_block) < 8:  # Need at least header + some data
            return b""

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

        # Parse block header
        predictor = struct_mod.unpack_from("<h", adpcm_block, 0)[0]
        step_index = adpcm_block[2]
        step_index = max(0, min(88, step_index))

        # Decode nibbles
        nibble_data = adpcm_block[4:]
        samples = []

        for byte_val in nibble_data:
            # Low nibble first, then high nibble
            for nibble in (byte_val & 0x0F, (byte_val >> 4) & 0x0F):
                step = IMA_STEP_TABLE[step_index]

                # Decode nibble to difference
                diff = step >> 3
                if nibble & 4:
                    diff += step
                if nibble & 2:
                    diff += step >> 1
                if nibble & 1:
                    diff += step >> 2

                if nibble & 8:
                    predictor -= diff
                else:
                    predictor += diff

                # Clamp to 16-bit range
                predictor = max(-32768, min(32767, predictor))

                # Update step index
                step_index += IMA_INDEX_TABLE[nibble]
                step_index = max(0, min(88, step_index))

                samples.append(predictor)

        if not samples:
            return b""

        return struct_mod.pack(f"<{len(samples)}h", *samples)

    async def _stop_baichuan_listen(self) -> None:
        """Stop the Baichuan preview stream (cmd 4 = stop video)."""
        if not self._listen_bc:
            return
        try:
            from reolink_aio.baichuan import util as bc_util  # noqa: PLC0415

            # Build stop command (cmd 4) with handle=256 (subStream)
            stop_body = (
                '<?xml version="1.0" encoding="UTF-8" ?>\n'
                "<body>\n"
                '<Preview version="1.1">\n'
                "<channelId>0</channelId>\n"
                "<handle>256</handle>\n"
                "</Preview>\n"
                "</body>\n"
            )

            # Write directly to transport (same as start)
            enc_body = self._listen_bc._aes_encrypt(stop_body)
            if not hasattr(self._listen_bc, "_mess_id"):
                self._listen_bc._mess_id = 0
            self._listen_bc._mess_id = (self._listen_bc._mess_id + 1) % 16777216
            mess_id = self._listen_bc._mess_id

            cmd_id = 4  # MSG_ID_VIDEO_STOP
            header = (
                bytes.fromhex(bc_util.HEADER_MAGIC)
                + int(cmd_id).to_bytes(4, "little")
                + int(len(enc_body)).to_bytes(4, "little")
                + int(1).to_bytes(1, "little")  # ch_id = channel 0 + 1
                + int(mess_id).to_bytes(3, "little")
                + bytes.fromhex("00001464")
                + int(0).to_bytes(4, "little")
            )

            packet = header + enc_body
            if hasattr(self._listen_bc, "_mutex") and hasattr(self._listen_bc, "_transport"):
                async with self._listen_bc._mutex:
                    self._listen_bc._transport.write(packet)
            elif hasattr(self._listen_bc, "_protocol") and hasattr(self._listen_bc._protocol, "transport"):
                self._listen_bc._protocol.transport.write(packet)

            _LOGGER.info("Baichuan preview stream stop sent (cmd 4)")
        except Exception as exc:
            _LOGGER.debug("Preview stop error (non-critical): %s", exc)

        # Restore original push callback
        if self._original_push_callback and self._listen_bc:
            try:
                protocol = self._listen_bc._protocol
                if protocol:
                    protocol._push_callback = self._original_push_callback
            except Exception:
                pass
        self._original_push_callback = None


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
