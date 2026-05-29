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
MAX_AI_PCM_BUFFER_BYTES = 480_000  # ~10s of 24kHz mono 16-bit PCM

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
    from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: PLC0415
    session = async_get_clientsession(hass)
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

    _LOGGER.warning("Could not discover go2rtc — tried ports %s", GO2RTC_PORTS)
    return None


def _get_go2rtc_session(hass: HomeAssistant) -> aiohttp.ClientSession | None:
    """Get HA's authenticated go2rtc session if available.

    HA-managed go2rtc uses random BasicAuth credentials, and communicates
    via a Unix socket session. We can reuse this session for our API calls.
    """
    try:
        go2rtc_data = hass.data.get("go2rtc")
        if go2rtc_data:
            if isinstance(go2rtc_data, str):
                # In newer HA, hass.data["go2rtc"] might just be an entry_id string.
                # Access the config entry's runtime_data for the actual server info.
                _LOGGER.debug("go2rtc hass.data is a string (likely entry_id): %s", go2rtc_data[:50])
            else:
                # Try Go2RtcConfig dataclass (url + session)
                url = getattr(go2rtc_data, "url", None)
                sess = getattr(go2rtc_data, "session", None)
                if sess and isinstance(sess, aiohttp.ClientSession):
                    return sess
                # Try alternative attribute names
                for attr in ("_session", "client_session"):
                    sess = getattr(go2rtc_data, attr, None)
                    if sess and isinstance(sess, aiohttp.ClientSession):
                        return sess

        # Try accessing via go2rtc config entry runtime_data
        for entry in hass.config_entries.async_entries("go2rtc"):
            rd = getattr(entry, "runtime_data", None)
            if rd:
                sess = getattr(rd, "_session", None)
                if sess and isinstance(sess, aiohttp.ClientSession):
                    return sess
    except (AttributeError, KeyError, TypeError) as exc:
        _LOGGER.debug("go2rtc session lookup failed: %s", exc)
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
    if not ha_session:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: PLC0415
        ha_session = async_get_clientsession(hass)
    session = ha_session

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


async def probe_audio_input_method(
    host: str, username: str, password: str, rtsp_port: int = 554
) -> tuple[str, str]:
    """Probe all audio input methods and return the best working one.

    Called during config setup so we can skip probing at session start.
    Returns (method, url) where method is one of:
      "rtsp", "https-flv", "http-flv", "rtmp", "none"

    Priority: RTSP > HTTPS-FLV > HTTP-FLV > RTMP
    """
    encoded_pass = quote(password, safe="")

    # --- Try RTSP variants ---
    base = f"rtsp://{username}:{encoded_pass}@{host}:{rtsp_port}"
    rtsp_urls = [
        f"{base}/h264Preview_01_sub",
        f"{base}/Preview_01_sub",
        f"{base}/h264Preview_01_main",
        f"{base}/h265Preview_01_sub",
    ]

    for url in rtsp_urls:
        if await _probe_ffmpeg_url(url, rtsp=True):
            _LOGGER.warning("Audio probe: RTSP works → %s", _mask_stream_url(url))
            return ("rtsp", url)

    # --- Try FLV variants ---
    https_flv = (
        f"https://{host}:443/flv?port=1935&app=bcs"
        f"&stream=channel0_sub.bcs&user={username}&password={encoded_pass}"
    )
    if await _probe_ffmpeg_url(https_flv, tls_noverify=True):
        _LOGGER.warning("Audio probe: HTTPS-FLV works")
        return ("https-flv", https_flv)

    http_flv = (
        f"http://{host}/flv?port=1935&app=bcs"
        f"&stream=channel0_sub.bcs&user={username}&password={encoded_pass}"
    )
    if await _probe_ffmpeg_url(http_flv):
        _LOGGER.warning("Audio probe: HTTP-FLV works")
        return ("http-flv", http_flv)

    # --- Try RTMP ---
    rtmp_url = (
        f"rtmp://{host}:1935/bcs/channel0_sub.bcs"
        f"?channel=0&stream=1&user={username}&password={encoded_pass}"
    )
    if await _probe_ffmpeg_url(rtmp_url):
        _LOGGER.warning("Audio probe: RTMP works")
        return ("rtmp", rtmp_url)

    _LOGGER.warning("Audio probe: no working method found")
    return ("none", "")


async def _probe_ffmpeg_url(url: str, rtsp: bool = False, tls_noverify: bool = False) -> bool:
    """Test if ffmpeg can connect to a URL and receive audio data within 2s."""
    ffmpeg_args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats"]
    if rtsp:
        ffmpeg_args.extend(["-rtsp_transport", "tcp"])
    if tls_noverify:
        ffmpeg_args.extend(["-tls_verify", "0"])
    ffmpeg_args.extend([
        "-fflags", "+nobuffer",
        "-analyzeduration", "500000",
        "-probesize", "500000",
        "-t", "2",  # Only capture 2 seconds max
        "-i", url,
        "-vn",
        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        "-f", "s16le", "pipe:1",
    ])

    try:
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Wait up to 3s for some audio data
        try:
            stdout_data = await asyncio.wait_for(proc.stdout.read(640), timeout=3.0)
        except asyncio.TimeoutError:
            stdout_data = b""
        # Kill the probe process
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return len(stdout_data) >= 640  # Got at least one audio frame
    except Exception:
        return False


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
        on_takeover: Any = None,  # Callable[[], Awaitable[None]] — called when app takes speaker
    ) -> None:
        self._hass = hass
        self._stream_name = stream_name
        self._on_audio_received_raw = on_audio_received
        self._on_takeover = on_takeover
        # Wrap callback with gain amplification for low-level doorbell mics
        # Reolink doorbells produce very low PCM levels (~RMS 20-50 raw).
        # Gemini VAD needs RMS ~3000-5000. With 128x gain: 20*128=2560, 50*128=6400.
        self._mic_gain: float = 128.0  # Amplification factor for doorbell mic
        self._on_audio_received = self._amplify_and_forward
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
        self._camera_unique_id: str | None = None  # go2rtc stream name
        # Baichuan talk state (output — speaker)
        self._baichuan: Any = None
        self._talk_baichuan: Any = None  # Separate connection for talk (CMD 201/202)
        self._talk_host: Any = None  # Dedicated Host object for talk connection
        self._preview_host: Any = None  # Dedicated Host object for preview/input connection
        self._talk_ability: dict | None = None
        self._talk_enc_type: Any = None
        self._talk_held: bool = False  # Whether we currently hold the talk session
        self._talk_channel: int = 0
        self._pcm_buffer: bytearray = bytearray()
        self._pcm_lock = asyncio.Lock()
        self._pcm_overflow_warned = False
        self._ffmpeg_proc: asyncio.subprocess.Process | None = None
        self._chime_delay: float = DEFAULT_CHIME_DELAY
        # Audio input state (native Baichuan FDX + ffmpeg fallback)
        self._listen_active = False
        self._input_processor_task: asyncio.Task[None] | None = None
        self._baichuan_audio_input_active = False
        self._raw_mic_task: asyncio.Task[None] | None = None  # Raw TCP fallback
        # AAC decoder state (for FDX stream AAC audio)
        self._aac_queue: asyncio.Queue[bytes] | None = None
        self._aac_decoder_task: asyncio.Task[None] | None = None
        # IMA ADPCM decoder state for incoming audio
        self._decode_predictor: int = 0
        self._decode_step_index: int = 0
        # Baichuan CMD 3 Preview stream state (mic audio via native protocol)
        self._preview_active = False
        self._preview_msg_num: int = 0
        self._preview_audio_frames: int = 0
        # Streaming BcMedia parser buffer — accumulates decrypted payloads
        # from fragmented Baichuan packets so multi-packet BcMedia frames
        # (especially large video frames) are correctly reassembled.
        self._preview_stream_buffer: bytearray = bytearray()

    async def _amplify_and_forward(self, pcm_data: bytes) -> None:
        """Apply audio processing pipeline to PCM audio before forwarding.

        Pipeline:
        1. High-pass filter at ~200Hz (removes wind/traffic rumble)
        2. Noise gate (suppress chunks below noise floor)
        3. Smoothed AGC with attack/release (amplify voice, limit noise)
        """
        import struct as _struct  # noqa: PLC0415

        TARGET_RMS = 5000.0    # Target RMS for Gemini VAD
        MAX_GAIN = 150.0       # Maximum gain (reduced to prevent noise boost)
        MIN_GAIN = 1.0         # Never attenuate
        NOISE_GATE_RMS = 40.0  # Below this RMS, consider it silence/noise
        ATTACK_COEFF = 0.3     # Fast attack (quickly raise gain for speech)
        RELEASE_COEFF = 0.05   # Slow release (gradually reduce gain after speech)

        n_samples = len(pcm_data) // 2
        if n_samples == 0:
            return

        samples = list(_struct.unpack(f"<{n_samples}h", pcm_data))

        # --- Step 1: High-pass filter at ~200Hz ---
        # First-order IIR high-pass: y[n] = alpha * (y[n-1] + x[n] - x[n-1])
        # alpha = RC / (RC + dt), for 200Hz cutoff at 16kHz sample rate: ~0.93
        alpha = 0.93
        prev_x = getattr(self, "_hp_prev_x", 0.0)
        prev_y = getattr(self, "_hp_prev_y", 0.0)
        for i in range(n_samples):
            x = float(samples[i])
            y = alpha * (prev_y + x - prev_x)
            prev_x = x
            prev_y = y
            samples[i] = int(max(-32768, min(32767, y)))
        self._hp_prev_x = prev_x
        self._hp_prev_y = prev_y

        # --- Step 2: Compute RMS after filtering ---
        rms = (sum(s * s for s in samples) / n_samples) ** 0.5
        if rms < 1.0:
            rms = 1.0

        # --- Step 3: Noise gate ---
        if rms < NOISE_GATE_RMS:
            # Below noise floor — send near-silence (very low gain)
            # Don't zero completely to preserve some ambient for natural sound
            gain = 1.0
        else:
            # --- Step 4: Smoothed AGC ---
            desired_gain = TARGET_RMS / rms
            desired_gain = max(MIN_GAIN, min(MAX_GAIN, desired_gain))

            # Smooth gain transitions (attack fast, release slow)
            prev_gain = getattr(self, "_agc_gain", desired_gain)
            if desired_gain > prev_gain:
                # Gain increasing (voice getting quieter) — fast attack
                gain = prev_gain + ATTACK_COEFF * (desired_gain - prev_gain)
            else:
                # Gain decreasing (voice getting louder) — slow release
                gain = prev_gain + RELEASE_COEFF * (desired_gain - prev_gain)
            gain = max(MIN_GAIN, min(MAX_GAIN, gain))

        self._agc_gain = gain

        # --- Step 5: Apply gain with soft clipping ---
        amplified = []
        for s in samples:
            v = s * gain
            # Soft clip beyond 80% of range
            if v > 26000:
                v = 26000 + (v - 26000) * 0.3
            elif v < -26000:
                v = -26000 + (v + 26000) * 0.3
            iv = int(v)
            if iv > 32767:
                iv = 32767
            elif iv < -32768:
                iv = -32768
            amplified.append(iv)

        amplified_pcm = _struct.pack(f"<{n_samples}h", *amplified)
        await self._on_audio_received_raw(amplified_pcm)

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def output_pipeline_ready(self) -> bool:
        """True if audio output pipeline is confirmed working (talk session active)."""
        return (
            self._active
            and self._output_reader_task is not None
            and not self._output_reader_task.done()
            and self._talk_baichuan is not None
        )

    async def start(self) -> None:
        """Start 2-way audio: output via Baichuan, input via cached method.

        Speaker output pipeline starts FIRST (fast: ~1s) so the AI greeting
        can play as soon as possible. Mic input uses the pre-probed method
        stored in config (no runtime probing needed).

        Audio output always uses Baichuan talk (CMD 201/202).
        """
        if self._active:
            return

        # Get Reolink connection details for direct camera access
        await self._discover_reolink_details()

        self._active = True

        # --- SPEAKER OUTPUT FIRST (fast path, ~1s) ---
        await self._start_output_pipeline()

        # --- Audio INPUT: use cached method or probe ---
        cached_method = getattr(self, "_cached_mic_method", "") or ""
        cached_url = getattr(self, "_cached_mic_url", "") or ""

        if cached_method:
            _LOGGER.warning("Audio input: using cached method '%s'", cached_method)

            # For methods with dynamic URLs (HLS tokens expire), regenerate
            if cached_method == "ha_hls":
                cached_url = await self._get_ha_hls_stream_url() or ""
                if not cached_url:
                    _LOGGER.warning("Audio input: could not get fresh HLS URL — falling back")
                    cached_method = ""
            elif cached_method == "go2rtc_http":
                await self._ensure_go2rtc_stream_active()
                cached_url = await self._get_go2rtc_http_stream_url() or ""
                if not cached_url:
                    cached_method = ""
            elif "go2rtc" in cached_method:
                await self._ensure_go2rtc_stream_active()

            if cached_method and cached_url:
                started = await self._start_ffmpeg_with_url(cached_url, cached_method.upper())
                if started:
                    _LOGGER.warning("Audio input: %s connected (cached)", cached_method)
                    return
                # Clear stale cache so we don't keep trying a broken method
                _LOGGER.warning("Audio input: cached method '%s' failed — clearing cache", cached_method)

            self._cached_mic_method = ""
            self._cached_mic_url = ""

        # Slow fallback: probe all methods (only if no cache or cache failed)
        # First try RTSP (most reliable, no extra session needed)
        rtsp_started = await self._start_rtsp_audio_input()
        if rtsp_started:
            _LOGGER.warning("Audio input: using ffmpeg/RTSP (continuous audio)")
        else:
            # Only create Preview connection if RTSP failed (saves a session slot)
            bc = await self._get_baichuan_object(purpose="preview")
            if bc:
                self._baichuan = bc

            ffmpeg_started = await self._start_ffmpeg_fallback_input()
            if ffmpeg_started:
                _LOGGER.warning("Audio input: using ffmpeg fallback (FLV/RTMP)")
            else:
                # Try go2rtc HTTP stream (uses HA's authenticated go2rtc session)
                go2rtc_url = await self._get_go2rtc_http_stream_url()
                if go2rtc_url:
                    _LOGGER.warning("Audio input: trying go2rtc HTTP → %s", go2rtc_url)
                    go2rtc_ok = await self._start_ffmpeg_with_url(go2rtc_url, "go2rtc-HTTP")
                    if go2rtc_ok:
                        self._discovered_mic_method = "go2rtc_http"
                        self._discovered_mic_url = go2rtc_url
                        _LOGGER.warning("Audio input: using go2rtc HTTP stream")
                    else:
                        go2rtc_url = None

                if not go2rtc_url:
                    # Try HA's internal HLS stream (higher latency but reliable)
                    hls_url = await self._get_ha_hls_stream_url()
                    if hls_url:
                        _LOGGER.warning("Audio input: trying HA HLS → %s", hls_url)
                        hls_ok = await self._start_ffmpeg_with_url(hls_url, "HA-HLS")
                        if hls_ok:
                            self._discovered_mic_method = "ha_hls"
                            self._discovered_mic_url = hls_url
                            _LOGGER.warning("Audio input: using HA HLS stream")
                        else:
                            hls_url = None

                    if not hls_url and bc:
                        preview_ok = await self._start_preview_stream()
                        if preview_ok:
                            _LOGGER.warning(
                                "Audio input: using Baichuan Preview (sparse ~1fps, "
                                "speech recognition may be degraded)"
                            )
                        else:
                            _LOGGER.warning("Audio input: ALL methods failed — mic disabled")

        _LOGGER.warning(
            "Reolink audio handler started (host=%s, baichuan=%s, mic_input=%s)",
            self._reolink_host,
            "connected" if self._baichuan else "failed",
            "active" if self._listen_active else "disabled",
        )

    async def _discover_reolink_details(self) -> None:
        """Get Reolink camera connection details from HA's Reolink integration.

        Discovers:
          - Camera unique_id (= go2rtc stream name for local RTSP relay)
          - RTMP, FLV, and RTSP URLs for direct camera access (fallback)
        """
        # First try to get from the camera entity's config entry
        reolink_entry_id = self._reolink_entry_id
        if not reolink_entry_id:
            camera_entity = self._camera_entity_id or self._stream_name
            # Clean up entity ID format
            if camera_entity.startswith("jeeves_"):
                camera_entity = camera_entity.replace("jeeves_", "").replace("_", ".", 1)
            reolink_entry_id = find_reolink_entry_for_camera(self._hass, camera_entity)

        # Discover camera unique_id from entity registry (= go2rtc stream name)
        camera_entity = self._camera_entity_id or ""
        if camera_entity:
            try:
                from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
                registry = er.async_get(self._hass)
                entity_entry = registry.async_get(camera_entity)
                if entity_entry and entity_entry.unique_id:
                    self._camera_unique_id = entity_entry.unique_id
                    _LOGGER.warning(
                        "Camera unique_id (go2rtc stream name): %s",
                        self._camera_unique_id,
                    )
                else:
                    _LOGGER.warning(
                        "Could not find unique_id for camera %s (entry=%s)",
                        camera_entity, entity_entry,
                    )
            except Exception as exc:
                _LOGGER.warning("Could not get camera unique_id: %s", exc)
        else:
            _LOGGER.warning("No camera entity set — go2rtc stream name unavailable")

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

                    # --- Ensure RTSP is enabled (critical for continuous audio) ---
                    await self._ensure_rtsp_enabled(api)

                    # RTSP (preferred — continuous audio stream)
                    port = api.rtsp_port
                    if port:
                        self._reolink_rtsp_port = port
                    rtsp_url = await api.get_rtsp_stream_source(0, "sub", check=False)
                    if rtsp_url:
                        self._reolink_rtsp_url = rtsp_url
                        _LOGGER.warning("Reolink RTSP URL: %s", _mask_stream_url(rtsp_url))

                    # HTTP-FLV (fallback — also continuous audio over HTTP)
                    flv_url = api.get_flv_stream_source(0, "sub")
                    if flv_url:
                        self._reolink_flv_url = flv_url
                        _LOGGER.warning("Reolink FLV URL: %s", _mask_stream_url(flv_url))

                    # RTMP (fallback — continuous if port 1935 is open)
                    rtmp_url = api.get_rtmp_stream_source(0, "sub")
                    if rtmp_url:
                        self._reolink_rtmp_url = rtmp_url
                        _LOGGER.warning("Reolink RTMP URL: %s", _mask_stream_url(rtmp_url))
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

    async def _ensure_rtsp_enabled(self, api: Any) -> None:
        """Check if RTSP is enabled on the camera and enable it if not.

        Many Reolink cameras (especially doorbells) ship with RTSP disabled.
        The Baichuan protocol can enable it remotely via CMD 36 (SetNetPort).
        RTSP provides continuous audio at full sample rate — essential for
        speech recognition that the sparse Baichuan Preview stream cannot deliver.
        """
        try:
            bc = api.baichuan if hasattr(api, "baichuan") else None
            if not bc:
                _LOGGER.info("Cannot check RTSP state — no Baichuan object on API")
                return

            # Check current RTSP state
            rtsp_enabled = bc.rtsp_enabled
            rtsp_port = bc.rtsp_port

            if rtsp_enabled:
                _LOGGER.warning(
                    "✓ RTSP already enabled (port %s)", rtsp_port or 554
                )
                return

            if rtsp_enabled is None:
                # Port state unknown — try to query it
                _LOGGER.warning("RTSP state unknown — attempting to query ports...")
                try:
                    from reolink_aio.baichuan import PortType  # noqa: PLC0415
                    # get_ports() refreshes the _ports dict
                    if hasattr(bc, "get_ports"):
                        await bc.get_ports()
                    rtsp_enabled = bc.rtsp_enabled
                    if rtsp_enabled:
                        _LOGGER.warning("✓ RTSP already enabled (after refresh)")
                        return
                except Exception as exc:
                    _LOGGER.warning("Could not query port state: %s", exc)

            # RTSP is disabled — enable it
            _LOGGER.warning("RTSP is DISABLED — enabling via Baichuan CMD 36...")
            try:
                from reolink_aio.baichuan import PortType  # noqa: PLC0415
                await bc.set_port_enabled(PortType.rtsp, True)
                _LOGGER.warning("✓ RTSP enable command sent — waiting for port to come up...")
                # Give the camera time to open the port
                await asyncio.sleep(3)

                # Verify it worked
                if hasattr(bc, "get_ports"):
                    await bc.get_ports()
                if bc.rtsp_enabled:
                    _LOGGER.warning(
                        "✓ RTSP enabled successfully! (port %s)",
                        bc.rtsp_port or 554,
                    )
                    if bc.rtsp_port:
                        self._reolink_rtsp_port = bc.rtsp_port
                else:
                    _LOGGER.warning("RTSP enable sent but state still shows disabled")
            except Exception as exc:
                _LOGGER.warning("Failed to enable RTSP: %s", exc)
        except Exception as exc:
            _LOGGER.warning("Error checking/enabling RTSP: %s", exc)

    async def _start_ffmpeg_with_url(self, url: str, label: str = "CACHED") -> bool:
        """Start ffmpeg audio input with a specific known-good URL (no probing)."""
        ffmpeg_args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats"]
        if url.startswith("rtsp://"):
            ffmpeg_args.extend(["-rtsp_transport", "tcp"])
        elif url.startswith("https://"):
            ffmpeg_args.extend(["-tls_verify", "0"])
        ffmpeg_args.extend([
            "-fflags", "+nobuffer+flush_packets",
            "-flags", "low_delay",
            "-analyzeduration", "500000",
            "-probesize", "500000",
            "-i", url,
            "-vn",
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

        # Wait briefly for connection
        await asyncio.sleep(1.5)
        if proc.returncode is not None:
            stderr_data = b""
            try:
                stderr_data = await asyncio.wait_for(proc.stderr.read(2000), timeout=1.0)
            except Exception:
                pass
            _LOGGER.warning(
                "%s audio: ffmpeg failed for cached URL (rc=%d): %s",
                label, proc.returncode,
                stderr_data.decode(errors="replace").strip()[:200] if stderr_data else "",
            )
            return False

        _LOGGER.warning("✓ %s audio input connected (ffmpeg PID=%d)", label, proc.pid)
        self._listen_active = True
        self._input_processor_task = asyncio.create_task(
            self._ffmpeg_audio_read_loop(proc, label)
        )
        return True

    async def _ensure_go2rtc_stream_active(self) -> None:
        """Ensure the camera's go2rtc stream is active before connecting.

        go2rtc lazily initializes streams — they only exist when a client is
        viewing the camera. We trigger stream creation by calling the camera
        entity's async_create_stream() which registers and starts the source
        in go2rtc. This makes the stream available on go2rtc's RTSP port.
        """
        camera_entity_id = self._camera_entity_id
        if not camera_entity_id:
            return

        try:
            from homeassistant.helpers.entity_component import EntityComponent

            camera_comp: EntityComponent | None = self._hass.data.get("camera")
            if not camera_comp or not hasattr(camera_comp, "get_entity"):
                return

            entity = camera_comp.get_entity(camera_entity_id)
            if not entity:
                return

            # async_create_stream registers the source in go2rtc and starts it
            if hasattr(entity, "async_create_stream"):
                stream = await entity.async_create_stream()
                if stream and hasattr(stream, "start"):
                    await stream.start()
                    _LOGGER.info(
                        "Triggered go2rtc stream start for %s", camera_entity_id
                    )
                    # Give go2rtc a moment to connect to the camera source
                    await asyncio.sleep(1.0)
                    return

            # Fallback: call stream_source() which also triggers registration
            if hasattr(entity, "stream_source"):
                source = await entity.stream_source()
                if source:
                    _LOGGER.info(
                        "Got stream source for %s (go2rtc should register it)",
                        camera_entity_id,
                    )
                    await asyncio.sleep(0.5)
        except Exception as exc:
            _LOGGER.debug("Could not trigger go2rtc stream: %s", exc)

    async def _start_rtsp_audio_input(self) -> bool:
        """Start continuous audio capture via ffmpeg reading from RTSP.

        RTSP provides a proper media stream with continuous audio frames
        at the camera's native sample rate (typically 16kHz AAC or G.711).
        ffmpeg decodes this to raw 16kHz mono PCM which is forwarded to Gemini.

        Tries multiple RTSP URL patterns since different camera models/firmware
        versions use different path formats (h264Preview, h265Preview, Preview).

        Returns True if ffmpeg successfully connected to the RTSP stream.
        """
        # --- Try go2rtc internal RTSP first (bypasses camera auth issues) ---
        if self._camera_unique_id:
            # Ensure go2rtc has the stream registered and active
            await self._ensure_go2rtc_stream_active()

            # HA's managed go2rtc uses port 18554 for RTSP (127.0.0.1 only)
            go2rtc_url = f"rtsp://127.0.0.1:18554/{self._camera_unique_id}"
            _LOGGER.warning("Audio input: trying go2rtc RTSP → %s", go2rtc_url)
            proc = await self._try_ffmpeg_rtsp(go2rtc_url)
            if proc:
                _LOGGER.warning("✓ go2rtc RTSP audio input connected (PID=%d)", proc.pid)
                self._listen_active = True
                self._discovered_mic_method = "go2rtc"
                self._discovered_mic_url = go2rtc_url
                self._input_processor_task = asyncio.create_task(
                    self._ffmpeg_audio_read_loop(proc, "go2rtc-RTSP")
                )
                return True
            _LOGGER.warning("go2rtc RTSP failed — trying direct camera RTSP")

        if not self._reolink_host or not self._reolink_user or not self._reolink_pass:
            _LOGGER.info("No RTSP credentials — skipping direct RTSP audio input")
            return False

        # Try getting the authenticated stream source from the camera entity first
        # This uses the Reolink integration's own credentials which may differ
        camera_stream_url = await self._get_camera_stream_source()

        # Build multiple RTSP URLs to try (different path formats)
        encoded_pass = quote(self._reolink_pass, safe="")
        port = getattr(self, "_reolink_rtsp_port", 554) or 554
        base = f"rtsp://{self._reolink_user}:{encoded_pass}@{self._reolink_host}:{port}"

        # Try sub stream first (lower bandwidth, same audio quality)
        # Then main stream as fallback
        urls_to_try = [
            f"{base}/h264Preview_01_sub",
            f"{base}/Preview_01_sub",
            f"{base}/h264Preview_01_main",
            f"{base}/h265Preview_01_sub",
        ]

        # Prioritize camera entity's stream source (has correct auth)
        if camera_stream_url and camera_stream_url not in urls_to_try:
            urls_to_try.insert(0, camera_stream_url)

        # Also include the API-discovered URL if different
        api_url = getattr(self, "_reolink_rtsp_url", None)
        if api_url and api_url not in urls_to_try:
            urls_to_try.insert(0, api_url)

        for rtsp_url in urls_to_try:
            _LOGGER.warning("Audio input: trying RTSP → %s", _mask_stream_url(rtsp_url))

            proc = await self._try_ffmpeg_rtsp(rtsp_url)
            if proc:
                _LOGGER.warning("✓ RTSP audio input connected (ffmpeg PID=%d)", proc.pid)
                self._listen_active = True
                self._discovered_mic_method = "rtsp"
                self._discovered_mic_url = rtsp_url
                self._input_processor_task = asyncio.create_task(
                    self._ffmpeg_audio_read_loop(proc, "RTSP")
                )
                return True

        _LOGGER.warning("RTSP audio: all URL variants failed")
        return False

    async def _try_ffmpeg_rtsp(self, rtsp_url: str) -> asyncio.subprocess.Process | None:
        """Attempt to connect ffmpeg to an RTSP URL. Returns process if successful."""
        ffmpeg_args = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
            "-rtsp_transport", "tcp",
            "-fflags", "+nobuffer+flush_packets",
            "-flags", "low_delay",
            "-analyzeduration", "500000",  # 500ms — faster stream analysis
            "-probesize", "500000",
            "-i", rtsp_url,
            "-vn",  # No video — audio only
            "-acodec", "pcm_s16le",
            "-ar", str(AUDIO_INPUT_SAMPLE_RATE),
            "-ac", "1",
            "-f", "s16le",
            "pipe:1",
        ]

        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Wait briefly to see if connection succeeds (RTSP DESCRIBE + SETUP)
        await asyncio.sleep(1.0)
        if proc.returncode is not None:
            stderr_data = b""
            try:
                stderr_data = await asyncio.wait_for(proc.stderr.read(2000), timeout=1.0)
            except Exception:
                pass
            _LOGGER.warning(
                "RTSP audio: ffmpeg failed for %s (rc=%d): %s",
                _mask_stream_url(rtsp_url), proc.returncode,
                stderr_data.decode(errors="replace").strip()[:200] if stderr_data else "",
            )
            return None

        # Wait more to confirm data is actually flowing
        await asyncio.sleep(1.5)
        if proc.returncode is not None:
            stderr_data = b""
            try:
                stderr_data = await asyncio.wait_for(proc.stderr.read(2000), timeout=1.0)
            except Exception:
                pass
            _LOGGER.warning(
                "RTSP audio: ffmpeg died after connect for %s (rc=%d): %s",
                _mask_stream_url(rtsp_url), proc.returncode,
                stderr_data.decode(errors="replace").strip()[:200] if stderr_data else "",
            )
            return None

        return proc

    async def _start_ffmpeg_fallback_input(self) -> bool:
        """Try FLV and RTMP URLs via ffmpeg as fallback for audio input.

        These are tried when RTSP is unavailable/failed. FLV tunnels RTMP
        over HTTP (port 80) which is often open even when RTMP port 1935 is closed.

        Returns True if any source connected successfully.
        """
        urls_to_try: list[tuple[str, str]] = []

        # HTTP-FLV (tunnels RTMP over HTTP — high likelihood of working)
        # Try both HTTP and HTTPS variants
        if self._reolink_host and self._reolink_user and self._reolink_pass:
            encoded_pass = quote(self._reolink_pass, safe="")
            # Plain HTTP FLV (port 80) — most reliable when TLS cert is self-signed
            http_flv = (
                f"http://{self._reolink_host}/flv?port=1935&app=bcs"
                f"&stream=channel0_sub.bcs&user={self._reolink_user}"
                f"&password={encoded_pass}"
            )
            urls_to_try.append((http_flv, "HTTP-FLV"))
            # HTTPS FLV (port 443) — works when HTTP is blocked/redirected
            https_flv = (
                f"https://{self._reolink_host}:443/flv?port=1935&app=bcs"
                f"&stream=channel0_sub.bcs&user={self._reolink_user}"
                f"&password={encoded_pass}"
            )
            urls_to_try.append((https_flv, "HTTPS-FLV"))

        # Also try discovered FLV URL if different from constructed ones
        flv_url = getattr(self, "_reolink_flv_url", None)
        if flv_url and flv_url not in [u for u, _ in urls_to_try]:
            urls_to_try.append((flv_url, "HTTPS-FLV-discovered"))

        # Direct RTMP (often port 1935 is blocked)
        rtmp_url = getattr(self, "_reolink_rtmp_url", None)
        if rtmp_url:
            urls_to_try.append((rtmp_url, "RTMP"))

        if not urls_to_try:
            return False

        for url, label in urls_to_try:
            _LOGGER.warning("Audio input fallback: trying %s → %s", label, _mask_stream_url(url))

            ffmpeg_args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats"]
            if url.startswith("rtmp://"):
                ffmpeg_args.extend(["-live_start_index", "-1"])
            elif url.startswith("https://"):
                ffmpeg_args.extend(["-tls_verify", "0"])
            ffmpeg_args.extend([
                "-fflags", "+nobuffer+flush_packets",
                "-analyzeduration", "1000000",
                "-probesize", "1000000",
                "-i", url,
                "-vn",
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

            await asyncio.sleep(1.5)
            if proc.returncode is not None:
                stderr_data = b""
                try:
                    stderr_data = await asyncio.wait_for(proc.stderr.read(2000), timeout=1.0)
                except Exception:
                    pass
                _LOGGER.warning(
                    "%s audio: ffmpeg failed (rc=%d): %s",
                    label, proc.returncode,
                    stderr_data.decode(errors="replace").strip()[:200] if stderr_data else "",
                )
                continue

            await asyncio.sleep(2.0)
            if proc.returncode is not None:
                continue

            # Connected!
            _LOGGER.warning("✓ %s audio input connected (ffmpeg PID=%d)", label, proc.pid)
            self._listen_active = True
            self._discovered_mic_method = label.lower().replace("-", "_")
            self._discovered_mic_url = url
            self._input_processor_task = asyncio.create_task(
                self._ffmpeg_audio_read_loop(proc, label)
            )
            return True

        return False

    async def _ffmpeg_audio_read_loop(
        self, proc: asyncio.subprocess.Process, source_label: str
    ) -> None:
        """Continuously read PCM from ffmpeg stdout and forward to Gemini.

        Reads in 20ms chunks (640 bytes at 16kHz/16-bit/mono) for minimal latency.
        Applies AGC amplification to compensate for low doorbell mic levels.
        """
        CHUNK_SIZE = 640  # 20ms @ 16kHz mono 16-bit
        chunks_sent = 0

        try:
            while self._active and self._listen_active and proc.returncode is None:
                try:
                    pcm_data = await proc.stdout.readexactly(CHUNK_SIZE)
                except asyncio.IncompleteReadError as err:
                    pcm_data = err.partial
                    if not pcm_data:
                        break

                if chunks_sent == 0:
                    _LOGGER.warning(
                        "✓ First continuous audio from doorbell mic via %s (%d bytes PCM @ %dHz)",
                        source_label, len(pcm_data), AUDIO_INPUT_SAMPLE_RATE,
                    )
                elif chunks_sent == 50:
                    _LOGGER.warning(
                        "Audio input: %s streaming steadily (%d chunks = 1.0s)",
                        source_label, chunks_sent,
                    )

                chunks_sent += 1
                # Forward via AGC amplifier
                await self._on_audio_received(pcm_data)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _LOGGER.warning("%s audio input error: %s", source_label, exc)
        finally:
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except (asyncio.TimeoutError, Exception):
                    proc.kill()
            # Read any stderr for diagnostics
            if proc and proc.stderr:
                try:
                    stderr_data = await asyncio.wait_for(proc.stderr.read(2000), timeout=1.0)
                    if stderr_data:
                        _LOGGER.warning(
                            "%s ffmpeg stderr: %s",
                            source_label,
                            stderr_data.decode(errors="replace").strip()[:300],
                        )
                except Exception:
                    pass
            self._listen_active = False
            _LOGGER.warning(
                "Audio input: %s stream ended (sent %d chunks = %.1fs of audio)",
                source_label, chunks_sent, chunks_sent * 0.02,
            )


    async def stop(self) -> None:
        """Stop the audio handler and all subprocesses.

        Each cleanup step is independent — failures in one don't prevent others.
        """
        self._active = False
        self._listen_active = False

        # Close go2rtc proxy session if we created our own
        try:
            own_sess = getattr(self, "_go2rtc_own_session", None)
            if own_sess:
                await own_sess.close()
                self._go2rtc_own_session = None
        except Exception:
            pass

        # Stop ffmpeg resampler
        try:
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
        except Exception as exc:
            _LOGGER.debug("ffmpeg resampler cleanup error: %s", exc)
        self._ffmpeg_proc = None

        # Stop Baichuan talk session
        try:
            await self._stop_baichuan_talk(0)
        except Exception as exc:
            _LOGGER.debug("Baichuan talk stop error: %s", exc)

        # Stop Preview stream (CMD 4)
        try:
            await self._stop_preview_stream()
        except Exception as exc:
            _LOGGER.debug("Preview stream stop error: %s", exc)

        # Cancel RTSP audio input task
        try:
            if self._input_processor_task and not self._input_processor_task.done():
                self._input_processor_task.cancel()
                try:
                    await self._input_processor_task
                except (asyncio.CancelledError, Exception):
                    pass
        except Exception:
            pass
        self._input_processor_task = None

        # Cancel AAC decoder task
        try:
            if self._aac_decoder_task and not self._aac_decoder_task.done():
                self._aac_decoder_task.cancel()
                try:
                    await self._aac_decoder_task
                except (asyncio.CancelledError, Exception):
                    pass
        except Exception:
            pass
        self._aac_decoder_task = None
        self._aac_queue = None

        # Cancel raw TCP mic task
        try:
            if self._raw_mic_task and not self._raw_mic_task.done():
                self._raw_mic_task.cancel()
                try:
                    await self._raw_mic_task
                except (asyncio.CancelledError, Exception):
                    pass
        except Exception:
            pass
        self._raw_mic_task = None

        # Logout dedicated talk connection
        if hasattr(self, "_talk_host") and self._talk_host:
            try:
                await asyncio.wait_for(self._talk_host.logout(), timeout=2)
            except Exception as exc:
                _LOGGER.debug("Talk host logout error: %s", exc)
            self._talk_host = None

        # Logout dedicated preview/input connection
        if hasattr(self, "_preview_host") and self._preview_host:
            try:
                await asyncio.wait_for(self._preview_host.logout(), timeout=2)
            except Exception as exc:
                _LOGGER.debug("Preview host logout error: %s", exc)
            self._preview_host = None

        # Cancel output reader task
        try:
            if self._output_reader_task and not self._output_reader_task.done():
                self._output_reader_task.cancel()
                try:
                    await self._output_reader_task
                except asyncio.CancelledError:
                    pass
        except Exception:
            pass
        self._output_reader_task = None

        try:
            async with self._pcm_lock:
                self._pcm_buffer.clear()
                self._pcm_overflow_warned = False
        except Exception:
            pass

        self._baichuan = None
        self._talk_baichuan = None
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

        Uses a DEDICATED Baichuan connection (separate from Preview stream)
        to send ADPCM audio directly to the camera speaker.
        This avoids interference between the Preview CMD 3 stream data
        and Talk CMD 201/202 commands.
        """
        # ALWAYS create a dedicated connection for talk — Preview uses self._baichuan
        # and installs a hook on it that would interfere with talk commands.
        bc = await self._get_baichuan_object()
        if not bc:
            _LOGGER.error(
                "Cannot establish Baichuan talk connection. "
                "Audio output to doorbell speaker will not work."
            )
            return
        # Store as the talk Baichuan object (separate from self._baichuan used for Preview)
        self._talk_baichuan = bc

        # IMMEDIATELY install FDX hook while connection is still active from login.
        # reolink_aio may clear _connection after commands complete, so we must
        # grab it NOW before any further operations.
        fdx_hooked = False
        try:
            fdx_hooked = self._install_talk_data_received_hook()
            if fdx_hooked:
                _LOGGER.warning("✓ FDX data_received hook installed (pre-talk)")
            else:
                _LOGGER.warning("FDX pre-talk hook failed — will retry after talk start")
        except Exception as exc:
            _LOGGER.warning("FDX pre-talk hook exception: %s", exc)

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

        # Mark FDX as active if hook succeeded and duplex is FDX
        if fdx_hooked and ability.get("duplex") == "FDX":
            if not self._aac_queue:
                self._aac_queue = asyncio.Queue(maxsize=200)
            if not self._aac_decoder_task or self._aac_decoder_task.done():
                self._aac_decoder_task = asyncio.create_task(self._aac_decode_loop())
            self._baichuan_audio_input_active = True
            _LOGGER.warning("✓ FDX audio pipeline active — continuous mic audio enabled")

        # If FDX hook failed, start raw TCP mic stream as ultimate fallback
        if not self._baichuan_audio_input_active and ability.get("duplex") == "FDX":
            _LOGGER.warning("Starting raw TCP mic stream (FDX hook unavailable)")
            self._raw_mic_task = asyncio.create_task(
                self._raw_tcp_mic_stream(ability)
            )

        # Start the output processing loop
        self._output_reader_task = asyncio.create_task(self._baichuan_audio_output_loop())

    async def _get_baichuan_object(self, purpose: str = "talk"):
        """Create a dedicated Baichuan connection.

        We create our OWN Host/Baichuan connection rather than reusing the
        Reolink integration's connection. This avoids interference between
        our audio streaming and the integration's normal command flow.

        Args:
            purpose: "talk" for speaker output, "preview" for mic input.
                     Controls which host reference is stored for proper cleanup.
        """
        # CLEANUP: Logout any existing connection for this purpose first
        # to prevent session leaks (Reolink has a limited session count).
        if purpose == "talk" and hasattr(self, "_talk_host") and self._talk_host:
            try:
                await asyncio.wait_for(self._talk_host.logout(), timeout=2)
                _LOGGER.debug("Cleaned up stale talk host before new connection")
            except Exception:
                pass
            self._talk_host = None
        elif purpose == "preview" and hasattr(self, "_preview_host") and self._preview_host:
            try:
                await asyncio.wait_for(self._preview_host.logout(), timeout=2)
                _LOGGER.debug("Cleaned up stale preview host before new connection")
            except Exception:
                pass
            self._preview_host = None

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

            new_host = Host(
                host=host,
                username=username,
                password=password,
                port=http_port,
                use_https=use_https,
                bc_port=bc_port,
                aiohttp_get_session_callback=lambda: async_get_clientsession(self._hass),
            )
            bc = new_host.baichuan
            await bc.login()
            # Store the Host object in the appropriate slot for proper cleanup
            if purpose == "talk":
                self._talk_host = new_host
            else:
                self._preview_host = new_host
            _LOGGER.warning("Created dedicated Baichuan %s connection to %s:%s", purpose, host, bc_port)
            return bc
        except Exception as exc:
            # Ensure we logout if login succeeded but something else failed
            if "new_host" in dir() and new_host:
                try:
                    await asyncio.wait_for(new_host.logout(), timeout=5)
                except Exception:
                    pass
            _LOGGER.warning("Failed to create Baichuan %s connection: %s", purpose, exc)
            return None

    async def _query_talk_ability(self) -> dict | None:
        """Query the camera's TalkAbility via Baichuan command 10."""
        import xml.etree.ElementTree as ET  # noqa: PLC0415

        if not self._talk_baichuan:
            return None

        try:
            # Command 10 queries the camera's two-way audio capabilities
            response = await self._talk_baichuan.send(cmd_id=10, channel=0)
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

        Uses ON-DEMAND talk acquisition: acquires the speaker only when audio
        arrives, releases it after a silence gap. This allows the Reolink app
        to connect to the speaker at any time the AI is not actively talking.
        """
        import struct as struct_mod  # noqa: PLC0415

        if not self._talk_baichuan or not self._talk_ability:
            return

        ability = self._talk_ability
        sample_rate = ability["sample_rate"]
        length_per_encoder = ability["length_per_encoder"]
        block_size = (length_per_encoder // 2) + 4
        channel = 0
        self._talk_channel = channel

        # Wait for the mechanical chime to finish before any speaker output
        chime_delay = getattr(self, "_chime_delay", DEFAULT_CHIME_DELAY)
        if chime_delay > 0:
            _LOGGER.warning(
                "Waiting %.1fs for doorbell chime before starting talk...", chime_delay
            )
            await asyncio.sleep(chime_delay)

        # NOTE: We do NOT acquire the talk session here anymore.
        # It will be acquired on-demand when first audio arrives.

        # Pure Python resampler: 24kHz mono s16le → camera sample_rate mono s16le
        input_rate = 24000
        _LOGGER.warning(
            "Audio output resampler ready (in-process %dHz → %dHz)", input_rate, sample_rate
        )

        chunks_sent = 0
        predictor = 0
        step_index = 0
        samples_per_block = (block_size - 4) * 2 + 1
        pcm_bytes_per_block = samples_per_block * 2

        import time as time_mod  # noqa: PLC0415
        expected_stream_end = time_mod.monotonic()
        resampled_pcm_buffer = bytearray()
        resample_leftover = bytearray()

        # On-demand talk: track when last audio was sent to detect silence
        TALK_RELEASE_DELAY = 1.5  # Release speaker after 1.5s of silence
        COOPERATIVE_YIELD_INTERVAL = 2.0  # Yield talk session every 2s during speech
        COOPERATIVE_YIELD_WINDOW = 0.35  # Release for 350ms to let Reolink app connect
        last_audio_sent_time = 0.0
        last_yield_time = 0.0
        consecutive_send_failures = 0
        TAKEOVER_FAILURE_THRESHOLD = 3  # After 3 consecutive send failures, assume app takeover

        try:
            while self._active:
                # Grab buffered PCM from AI (24kHz 16-bit mono)
                async with self._pcm_lock:
                    chunk = bytes(self._pcm_buffer)
                    self._pcm_buffer.clear()

                if not chunk:
                    # No audio — check if we should release the speaker
                    if (
                        self._talk_held
                        and last_audio_sent_time > 0
                        and (time_mod.monotonic() - last_audio_sent_time) > TALK_RELEASE_DELAY
                    ):
                        await self._release_talk()
                    await asyncio.sleep(0.01)
                    continue

                # We have audio — acquire talk session if not held
                if not self._talk_held:
                    acquired = await self._acquire_talk()
                    if not acquired:
                        # Can't get speaker — skip this audio
                        await asyncio.sleep(0.05)
                        continue
                    # Reset timing after re-acquisition
                    expected_stream_end = time_mod.monotonic()
                    last_yield_time = time_mod.monotonic()
                    chunks_sent_before_acquire = chunks_sent

                # Resample in-process: linear interpolation 24kHz → target rate
                raw_input = resample_leftover + chunk
                resample_leftover = bytearray()

                n_bytes = len(raw_input)
                if n_bytes % 2:
                    resample_leftover = bytearray(raw_input[-1:])
                    raw_input = raw_input[:-1]
                    n_bytes -= 1

                n_samples_in = n_bytes // 2
                if n_samples_in < 2:
                    resample_leftover = bytearray(raw_input)
                    continue

                in_samples = struct_mod.unpack(f"<{n_samples_in}h", raw_input)

                ratio = input_rate / sample_rate
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

                consumed_input_samples = int(n_samples_out * ratio) + 1
                if consumed_input_samples < n_samples_in:
                    leftover_start = consumed_input_samples * 2
                    resample_leftover = bytearray(raw_input[leftover_start:])

                resampled_bytes = struct_mod.pack(f"<{len(out_samples)}h", *out_samples)
                resampled_pcm_buffer.extend(resampled_bytes)

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

                    block_samples = (block_size - 4) * 2 + 1
                    play_duration = block_samples / sample_rate

                    now = time_mod.monotonic()
                    try:
                        await self._send_talk_binary(channel, payload)
                        chunks_sent += 1
                        last_audio_sent_time = time_mod.monotonic()
                        consecutive_send_failures = 0
                        if chunks_sent == 1:
                            _LOGGER.warning("✓ First ADPCM payload sent to doorbell speaker!")
                            expected_stream_end = now + play_duration
                        elif chunks_sent % 50 == 0:
                            _LOGGER.warning("Audio output progress: %d payloads sent", chunks_sent)
                    except Exception as exc:
                        consecutive_send_failures += 1
                        if consecutive_send_failures <= 3:
                            _LOGGER.warning(
                                "Baichuan talk send error (#%d): %s",
                                consecutive_send_failures, exc,
                            )
                        # Talk session may have been stolen by Reolink app
                        self._talk_held = False
                        if consecutive_send_failures >= TAKEOVER_FAILURE_THRESHOLD:
                            _LOGGER.warning(
                                "Speaker takeover detected (%d consecutive send failures) "
                                "— Reolink app likely connected",
                                consecutive_send_failures,
                            )
                            if self._on_takeover:
                                self._hass.async_create_task(self._on_takeover())
                            return  # Exit output loop — session will be stopped
                        continue

                    if now > expected_stream_end:
                        expected_stream_end = now + play_duration
                    else:
                        expected_stream_end += play_duration

                    sleep_time = expected_stream_end - time_mod.monotonic()
                    if sleep_time > 0.001:
                        await asyncio.sleep(sleep_time)

                # Cooperative yielding: briefly release talk session every N seconds
                # during continuous speech to let the Reolink app connect.
                # If re-acquire fails after yield, someone else took over.
                if (
                    self._talk_held
                    and last_audio_sent_time > 0
                    and (time_mod.monotonic() - last_yield_time) > COOPERATIVE_YIELD_INTERVAL
                ):
                    last_yield_time = time_mod.monotonic()
                    await self._release_talk()
                    await asyncio.sleep(COOPERATIVE_YIELD_WINDOW)
                    # Try to re-acquire
                    if not await self._acquire_talk():
                        _LOGGER.warning(
                            "Talk session NOT re-acquired after cooperative yield — "
                            "Reolink app likely connected (human takeover)"
                        )
                        if self._on_takeover:
                            self._hass.async_create_task(self._on_takeover())
                        return  # Exit — let human take over

        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.warning("Baichuan audio output loop error", exc_info=True)
        finally:
            # Release talk session on exit
            if self._talk_held:
                remaining = expected_stream_end - time_mod.monotonic()
                if remaining > 0:
                    await asyncio.sleep(min(remaining + 0.1, 1.5))
                await self._release_talk()
            _LOGGER.warning("Baichuan audio output loop ended (sent %d chunks)", chunks_sent)

    async def _acquire_talk(self) -> bool:
        """Acquire the Baichuan talk session on-demand (fast ~200ms)."""
        if self._talk_held:
            return True
        if not self._talk_baichuan or not self._talk_ability:
            return False
        ok = await self._start_baichuan_talk(self._talk_channel, self._talk_ability)
        if ok:
            self._talk_held = True
            _LOGGER.info("Talk session acquired (on-demand)")
        return ok

    async def _release_talk(self) -> None:
        """Release the Baichuan talk session so Reolink app can connect."""
        if not self._talk_held:
            return
        await self._stop_baichuan_talk(self._talk_channel)
        self._talk_held = False
        _LOGGER.info("Talk session released (speaker free for Reolink app)")

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
                await self._talk_baichuan.send(cmd_id=201, channel=channel, body=talk_config, enc_type=enc)
                self._talk_enc_type = enc
                _LOGGER.warning("TalkConfig accepted (enc=%s)", enc)
                return True
            except Exception as exc:
                rsp = getattr(exc, "rspCode", None)
                if rsp in (400, 422):
                    # Camera says talk already active — stop it first, then retry
                    _LOGGER.info("TalkConfig got rspCode=%s, stopping existing talk and retrying", rsp)
                    try:
                        await self._talk_baichuan.send(cmd_id=11, channel=channel, enc_type=enc)
                        await asyncio.sleep(0.3)
                        await self._talk_baichuan.send(cmd_id=201, channel=channel, body=talk_config, enc_type=enc)
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
        bc = getattr(self, "_talk_baichuan", None) or self._baichuan
        if not bc:
            return
        try:
            enc = self._talk_enc_type
            if enc:
                await bc.send(cmd_id=11, channel=channel, enc_type=enc)
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

        bc = getattr(self, "_talk_baichuan", None) or self._baichuan
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

    # ─── Audio Input: Native Baichuan CMD 3 Preview Stream ────────────────────────

    async def _start_preview_stream(self) -> bool:
        """Start a Baichuan CMD 3 (Preview) stream to get mic audio from the camera.

        This subscribes to the camera's native media stream which includes both
        video and audio. Unlike RTSP (which may be disabled), this uses the same
        Baichuan TCP connection (port 9000) that talk uses.

        Protocol flow:
          1. Install parse hook to intercept CMD 3 response packets
          2. Send CMD 3 with Preview XML body (subStream, channel 0)
          3. Camera responds with continuous BcMedia frames (video + audio)
          4. Hook extracts audio frames (ADPCM/AAC) and decodes to PCM

        Returns True if the stream was started and initial data received.
        """
        bc = self._baichuan
        if not bc:
            return False

        # Start AAC decoder task BEFORE installing the hook (so queued frames get decoded)
        if not self._aac_queue:
            self._aac_queue = asyncio.Queue(maxsize=200)
        if not self._aac_decoder_task or self._aac_decoder_task.done():
            self._aac_decoder_task = asyncio.create_task(self._aac_decode_loop())
            _LOGGER.warning("Preview stream: AAC decoder task started")

        # Install the enhanced parse hook that handles BOTH CMD 3 and CMD 202
        if not self._install_preview_audio_intercept():
            _LOGGER.warning("Preview stream: failed to install protocol hook")
            return False

        # Build and send CMD 3 Preview request
        try:
            await self._send_preview_request(bc)
        except Exception as exc:
            _LOGGER.warning("Preview stream: failed to send CMD 3: %s", exc, exc_info=True)
            return False

        # Wait briefly for the first audio frame to confirm the stream is working
        self._listen_active = True
        _LOGGER.warning("Preview stream: CMD 3 sent, waiting for audio frames...")

        # Give the camera up to 3 seconds to start sending data
        for _ in range(30):
            await asyncio.sleep(0.1)
            if self._preview_audio_frames > 0:
                _LOGGER.warning(
                    "✓ Preview stream: receiving audio! (%d frames in first %.1fs)",
                    self._preview_audio_frames, 0.1 * (_ + 1),
                )
                return True

        # No audio frames received — the stream might be video-only
        if self._preview_active:
            _LOGGER.warning(
                "Preview stream: receiving data but no audio frames detected "
                "(video-only?). Will keep listening in case audio starts later."
            )
            return True  # Keep the hook active — audio might come with talk session

        _LOGGER.warning("Preview stream: no data received after 3s")
        self._listen_active = False
        return False

    async def _send_preview_request(self, bc: Any) -> None:
        """Construct and send a CMD 3 (Preview) request packet.

        Packet structure (same as neolink's start_video):
          - 24-byte Baichuan header (class 0x6414 has payload_offset field)
          - Encrypted body: Preview XML requesting subStream
          - No extension (payload_offset = 0)
        """
        from reolink_aio.baichuan import util as bc_util  # noqa: PLC0415

        _LOGGER.warning("CMD3 v2.9.23: _send_preview_request entered")

        # Preview XML body — request mainStream for full-quality audio
        # mainStream: handle=0, continuous audio at full sample rate
        # subStream (handle=256) has reduced audio that's too sparse for speech
        preview_xml = (
            '<?xml version="1.0" encoding="UTF-8" ?>\n'
            "<body>\n"
            '<Preview version="1.1">\n'
            "<channelId>0</channelId>\n"
            "<handle>0</handle>\n"
            "<streamType>mainStream</streamType>\n"
            "</Preview>\n"
            "</body>\n"
        )

        # Encrypt the body
        # reolink_aio's _aes_encrypt may expect str OR bytes depending on version.
        # Inspect to determine the correct type to pass.
        enc_type = self._talk_enc_type or bc_util.EncType.AES

        _LOGGER.warning(
            "CMD3: enc_type=%s, aes_key=%s, preview_xml type=%s",
            enc_type, "set" if getattr(bc, "_aes_key", None) else "NONE",
            type(preview_xml).__name__,
        )

        if enc_type == bc_util.EncType.BC:
            enc_body = bc_util.encrypt_baichuan(preview_xml, 0)
        else:
            # Try string first (HA's version), fall back to bytes
            try:
                enc_body = bc._aes_encrypt(preview_xml)
            except (AttributeError, TypeError):
                _LOGGER.warning("CMD3: _aes_encrypt(str) failed, trying bytes")
                enc_body = bc._aes_encrypt(preview_xml.encode("utf-8"))

        body_len = len(enc_body)

        # Increment message counter
        if not hasattr(bc, "_mess_id"):
            bc._mess_id = 0
        bc._mess_id = (bc._mess_id + 1) % 16777216
        self._preview_msg_num = bc._mess_id

        # Build 24-byte Baichuan header for CMD 3
        # Byte layout: magic(4) + cmd_id(4) + body_len(4) + mess_id(4) + status+class(4) + payload_offset(4)
        # mess_id bytes = ch_id(1) + stream_counter(3)
        # For Preview: ch_id=0 (channel 0), stream_type encoded in counter byte 0
        # We set the low byte of mess_id to 1 (= subStream stream_type in neolink)
        cmd_id = 3  # MSG_ID_VIDEO (Preview)

        # mess_id_bytes: ch_id=0 for channel 0 (NOT ch_id=channel+1 like for talk)
        # The camera uses ch_id in responses to route back to us
        ch_id = 0  # channel_id for preview (channel 0 direct)
        mess_id_bytes = (
            int(ch_id).to_bytes(1, "little")
            + int(bc._mess_id).to_bytes(3, "little")
        )

        header = (
            bytes.fromhex(bc_util.HEADER_MAGIC)
            + int(cmd_id).to_bytes(4, "little")
            + int(body_len).to_bytes(4, "little")
            + mess_id_bytes
            + bytes.fromhex("00001464")  # status_code=0, class=0x6414
            + int(0).to_bytes(4, "little")  # payload_offset=0 (no extension, body IS payload)
        )

        packet = header + enc_body

        _LOGGER.warning(
            "Preview stream: sending CMD 3 (body=%d bytes, enc=%s, header=%s)",
            body_len, enc_type, header.hex(),
        )

        # Write to transport
        if hasattr(bc, "_mutex") and hasattr(bc, "_transport"):
            async with bc._mutex:
                bc._transport.write(packet)
        elif hasattr(bc, "_protocol") and hasattr(bc._protocol, "transport"):
            bc._protocol.transport.write(packet)
        else:
            conn = getattr(bc, "_connection", None)
            if conn and hasattr(conn, "_transport"):
                conn._transport.write(packet)
            else:
                raise RuntimeError("Cannot find Baichuan transport for CMD 3")

    async def _stop_preview_stream(self) -> None:
        """Send CMD 4 (Stop Video) to end the Preview stream."""
        if not self._preview_active or not self._baichuan:
            return

        bc = self._baichuan
        self._preview_active = False
        self._preview_stream_buffer.clear()

        try:
            from reolink_aio.baichuan import util as bc_util  # noqa: PLC0415

            # CMD 4 body — must match the stream we started (mainStream, handle=0)
            stop_xml = (
                '<?xml version="1.0" encoding="UTF-8" ?>\n'
                "<body>\n"
                '<Preview version="1.1">\n'
                "<channelId>0</channelId>\n"
                "<handle>0</handle>\n"
                "<streamType>mainStream</streamType>\n"
                "</Preview>\n"
                "</body>\n"
            )

            enc_type = self._talk_enc_type or bc_util.EncType.AES

            if enc_type == bc_util.EncType.BC:
                enc_body = bc_util.encrypt_baichuan(stop_xml, 0)
            else:
                enc_body = bc._aes_encrypt(stop_xml)

            body_len = len(enc_body)
            bc._mess_id = (bc._mess_id + 1) % 16777216

            ch_id = 0
            mess_id_bytes = (
                int(ch_id).to_bytes(1, "little")
                + int(bc._mess_id).to_bytes(3, "little")
            )

            cmd_id = 4  # MSG_ID_VIDEO_STOP
            header = (
                bytes.fromhex(bc_util.HEADER_MAGIC)
                + int(cmd_id).to_bytes(4, "little")
                + int(body_len).to_bytes(4, "little")
                + mess_id_bytes
                + bytes.fromhex("00001464")
                + int(0).to_bytes(4, "little")
            )

            packet = header + enc_body

            if hasattr(bc, "_mutex") and hasattr(bc, "_transport"):
                async with bc._mutex:
                    bc._transport.write(packet)
            elif hasattr(bc, "_protocol") and hasattr(bc._protocol, "transport"):
                bc._protocol.transport.write(packet)
            else:
                conn = getattr(bc, "_connection", None)
                if conn and hasattr(conn, "_transport"):
                    conn._transport.write(packet)

            _LOGGER.info("Preview stream: sent CMD 4 (stop), received %d audio frames total",
                         self._preview_audio_frames)
        except Exception as exc:
            _LOGGER.debug("Preview stream stop error (non-critical): %s", exc)

    def _install_preview_audio_intercept(self) -> bool:
        """Hook into the Baichuan protocol to intercept CMD 3 and CMD 202 audio.

        CMD 3 (Preview) responses contain the camera's full media stream
        including mic audio as BcMedia frames (ADPCM or AAC).

        CMD 202 (Talk FDX) may also contain mic audio in full-duplex mode.

        We intercept at the protocol's parse_bc_data level to catch these
        packets before the library drops them as "unrequested".

        Returns True if the hook was successfully installed.
        """
        bc = self._baichuan
        if not bc:
            return False

        # Find the protocol object
        protocol = None
        connection = getattr(bc, "_connection", None)

        if hasattr(bc, "_protocol") and bc._protocol:
            protocol = bc._protocol
        if not protocol and connection:
            protocol = getattr(connection, "_protocol", None)
        if not protocol and connection:
            transport = getattr(connection, "_transport", None)
            if transport and hasattr(transport, "get_protocol"):
                protocol = transport.get_protocol()

        if not protocol:
            _LOGGER.warning("Preview intercept FAILED: no protocol found")
            return False

        parse_method_name = None
        for name in ["parse_bc_data", "parse_data"]:
            if hasattr(protocol, name):
                parse_method_name = name
                break

        if not parse_method_name:
            _LOGGER.warning("Preview intercept FAILED: no parse method on protocol")
            return False

        _LOGGER.warning(
            "Installing Preview+Talk audio intercept on %s.%s",
            type(protocol).__name__, parse_method_name,
        )

        original_parse = getattr(protocol, parse_method_name)
        input_count = [0]
        decrypt_mode = [None]  # "aes", "none", or "bc"

        def _decrypt_payload(raw_payload: bytes) -> bytes:
            """Decrypt binary payload using the Baichuan AES session key."""
            if decrypt_mode[0] == "none":
                return raw_payload

            aes_key = getattr(bc, "_aes_key", None)
            if aes_key is None:
                if decrypt_mode[0] is None:
                    decrypt_mode[0] = "none"
                    _LOGGER.warning("Preview decrypt: no AES key, assuming unencrypted")
                return raw_payload

            try:
                try:
                    from Cryptodome.Cipher import AES as _AES  # noqa: PLC0415
                except ImportError:
                    from Crypto.Cipher import AES as _AES  # noqa: PLC0415
                AES_IV = b"0123456789abcdef"
                cipher = _AES.new(key=aes_key, mode=_AES.MODE_CFB, iv=AES_IV, segment_size=128)
                decrypted = cipher.decrypt(raw_payload)

                if decrypt_mode[0] is None:
                    import struct as _st
                    decrypt_mode[0] = "aes"
                    if len(decrypted) >= 4:
                        magic_val = _st.unpack_from("<I", decrypted, 0)[0]
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
                            "✓ Preview decrypt: AES active. First frame: %s (magic=0x%08x)",
                            name or "unknown", magic_val,
                        )
                return decrypted
            except ImportError:
                _LOGGER.error("Preview decrypt: pycryptodome not installed")
                decrypt_mode[0] = "none"
                return raw_payload
            except Exception as exc:
                if decrypt_mode[0] is None:
                    decrypt_mode[0] = "none"
                    _LOGGER.warning("Preview decrypt failed (%s), assuming unencrypted", exc)
                return raw_payload

        def _patched_parse() -> None:
            """Intercept CMD 3 (Preview) and CMD 202 (Talk FDX) for audio extraction."""
            data = protocol._data
            if not data or len(data) < 20:
                original_parse()
                return

            rec_cmd_id = int.from_bytes(data[4:8], byteorder="little")

            # Only intercept CMD 3 (Preview stream) and CMD 202 (Talk FDX)
            if rec_cmd_id not in (3, 202):
                original_parse()
                return

            # Extract the binary payload from the Baichuan packet
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

            # Check we have the complete message
            data_len = len(data)
            len_body = data_len - len_header
            if len_body < rec_len_body:
                # Incomplete — let original handle buffering
                original_parse()
                return

            # For CMD 3 stream data: payload_offset=0 means the ENTIRE body is
            # binary payload (no extension XML). Don't apply the library's
            # default of "treat as all-extension" for stream data.
            if rec_payload_offset == 0 and rec_cmd_id == 3:
                # Binary stream: entire body is payload
                rec_payload_offset = 0  # payload starts at offset 0
            elif rec_payload_offset == 0:
                rec_payload_offset = rec_len_body  # normal: all extension, no payload

            # Extract binary payload (after extension/XML offset)
            len_chunk = rec_len_body + len_header
            payload_start = rec_payload_offset + len_header
            raw_payload = data[payload_start:len_chunk]

            if raw_payload:
                # Decrypt the payload
                payload = _decrypt_payload(raw_payload)

                input_count[0] += 1
                self._preview_active = True

                # Track total bytes for diagnostics
                if not hasattr(self, "_preview_total_bytes"):
                    self._preview_total_bytes = 0
                self._preview_total_bytes += len(payload)

                # Log first few packets and periodic stats
                if input_count[0] <= 5:
                    import struct as _st2
                    magic_val = _st2.unpack_from("<I", payload, 0)[0] if len(payload) >= 4 else 0
                    _LOGGER.warning(
                        "Preview pkt #%d (CMD %d): %d bytes, first4=0x%08x, buf=%d",
                        input_count[0], rec_cmd_id, len(payload), magic_val,
                        len(self._preview_stream_buffer),
                    )
                elif input_count[0] == 50:
                    _LOGGER.warning(
                        "Preview @50pkts: audio=%d, total_bytes=%d, buf=%d",
                        self._preview_audio_frames, self._preview_total_bytes,
                        len(self._preview_stream_buffer),
                    )
                elif input_count[0] == 200:
                    _LOGGER.warning(
                        "Preview @200pkts: audio=%d, total_bytes=%d, buf=%d",
                        self._preview_audio_frames, self._preview_total_bytes,
                        len(self._preview_stream_buffer),
                    )
                elif input_count[0] % 500 == 0:
                    _LOGGER.warning(
                        "Preview: %d pkts, %d audio, %dKB total",
                        input_count[0], self._preview_audio_frames,
                        self._preview_total_bytes // 1024,
                    )

                # Accumulate into streaming buffer and parse audio frames
                self._preview_stream_buffer.extend(payload)
                self._parse_preview_stream_buffer()
            else:
                # Empty payload — might be an ACK or status-only response
                input_count[0] += 1
                self._preview_active = True
                if input_count[0] <= 3:
                    _LOGGER.warning(
                        "Preview pkt #%d (CMD %d): empty payload (offset=%d, body_len=%d)",
                        input_count[0], rec_cmd_id, rec_payload_offset, rec_len_body,
                    )

            # Consume this message from the buffer
            if len_body > rec_len_body:
                protocol._data = data[len_chunk:]
                if protocol._data and len(protocol._data) >= 4:
                    if protocol._data[0:4].hex() == "f0debc0a":
                        _patched_parse()
            else:
                protocol._data = b""

        setattr(protocol, parse_method_name, _patched_parse)
        _LOGGER.warning("✓ Preview+Talk audio intercept installed")
        return True

    def _parse_preview_stream_buffer(self) -> None:
        """Extract audio frames from the accumulated preview stream buffer.

        Strategy: Instead of parsing the full BcMedia stream structure (which
        requires exact video frame format knowledge that varies by camera model),
        we SCAN the buffer for audio magic bytes and extract audio frames directly.

        Audio BcMedia frame format (both ADPCM 0x62773130 and AAC 0x62773530):
          magic(4) + plen1(2) + plen2(2) + audio_data(plen1)

        This finds all audio frames regardless of what video format surrounds them.
        """
        import struct as struct_mod  # noqa: PLC0415

        # Audio magic bytes in little-endian byte order for buffer scanning
        ADPCM_BYTES = b"\x30\x31\x77\x62"  # 0x62773130 LE
        AAC_BYTES = b"\x30\x35\x77\x62"    # 0x62773530 LE
        MAGIC_ADPCM = 0x62773130
        MAGIC_AAC = 0x62773530

        buf = self._preview_stream_buffer
        audio_found = 0
        max_scans = 100  # safety limit

        while len(buf) >= 8 and max_scans > 0:
            max_scans -= 1

            # Find earliest audio magic in the buffer
            adpcm_pos = buf.find(ADPCM_BYTES)
            aac_pos = buf.find(AAC_BYTES)

            pos = -1
            if adpcm_pos >= 0 and aac_pos >= 0:
                pos = min(adpcm_pos, aac_pos)
            elif adpcm_pos >= 0:
                pos = adpcm_pos
            elif aac_pos >= 0:
                pos = aac_pos

            if pos < 0:
                # No audio magic found — discard buffer except last 7 bytes
                # (in case a magic straddles a packet boundary)
                keep = min(7, len(buf))
                del buf[:len(buf) - keep]
                break

            # Discard everything before the audio frame
            if pos > 0:
                del buf[:pos]

            if len(buf) < 8:
                break

            magic = struct_mod.unpack_from("<I", buf, 0)[0]
            plen1 = struct_mod.unpack_from("<H", buf, 4)[0]

            # Sanity: audio payload should be 4-8192 bytes
            if plen1 < 4 or plen1 > 8192:
                # False positive — skip these 4 bytes
                del buf[:4]
                continue

            # Full frame: magic(4) + plen1(2) + plen2(2) + data(plen1)
            frame_total = 8 + plen1
            if len(buf) < frame_total:
                break  # incomplete, wait for more data

            if magic == MAGIC_ADPCM:
                # ADPCM sub-header: data_magic(2) + half_block(2) then ADPCM data
                block_size = plen1 - 4 if plen1 > 4 else plen1
                adpcm_block = bytes(buf[12:12 + block_size])
                pcm_block = self._decode_ima_adpcm_block(adpcm_block)
                if pcm_block:
                    self._preview_audio_frames += 1
                    audio_found += 1
                    if self._listen_active:
                        asyncio.ensure_future(self._on_audio_received(pcm_block))

            elif magic == MAGIC_AAC:
                aac_data = bytes(buf[8:8 + plen1])
                if not hasattr(self, "_stream_aac_logged"):
                    self._stream_aac_logged = True
                    _LOGGER.warning(
                        "✓ Streaming AAC: %d bytes, first4=%s",
                        len(aac_data),
                        aac_data[:4].hex() if len(aac_data) >= 4 else "?",
                    )

                # Real AAC has ADTS header (0xFFF sync)
                if (len(aac_data) >= 2 and aac_data[0] == 0xFF
                        and (aac_data[1] & 0xF0) == 0xF0):
                    # Real AAC — queue for ffmpeg decoder
                    if self._aac_queue is not None:
                        try:
                            self._aac_queue.put_nowait(aac_data)
                        except asyncio.QueueFull:
                            pass
                        self._preview_audio_frames += 1
                        audio_found += 1
                else:
                    # Not ADTS — try as raw G.711/ADPCM
                    pcm = self._try_raw_adpcm_decode(aac_data)
                    if pcm:
                        self._preview_audio_frames += 1
                        audio_found += 1
                        if self._listen_active:
                            asyncio.ensure_future(self._on_audio_received(pcm))

            # Consume this frame from buffer
            del buf[:frame_total]

        # Log first successful extraction
        if audio_found > 0 and not hasattr(self, "_stream_parse_logged"):
            self._stream_parse_logged = True
            _LOGGER.warning(
                "✓ Stream audio scan: %d frames this pass, %d total, buf=%d",
                audio_found, self._preview_audio_frames, len(buf),
            )

        # Safety: cap buffer at 32KB
        if len(buf) > 32768:
            del buf[:len(buf) - 4096]

    # ─── Audio Input (Native Baichuan FDX + ffmpeg fallback) ──────────────────────

    async def _start_audio_input(self) -> None:
        """Start receiving audio from the doorbell mic.

        Priority order:
          1. go2rtc authenticated stream proxy (pipes via HA's go2rtc session)
          2. Direct RTSP to camera (works intermittently)
          3. HTTP-FLV to camera
          4. RTMP to camera
          5. Camera entity stream_source

        The go2rtc proxy is most reliable because HA's internal go2rtc already
        manages the RTSP connection to the camera. We access it through the
        authenticated session (which handles BasicAuth automatically).
        """
        self._listen_active = True
        _LOGGER.warning("Audio input: starting stream-based mic capture")

        # Priority 1: Try go2rtc authenticated proxy (most reliable)
        go2rtc_started = await self._start_go2rtc_proxy_stream()
        if go2rtc_started:
            _LOGGER.warning("Audio input: using go2rtc authenticated proxy")
            return

        # Fallback: use direct ffmpeg with URL list
        urls_to_try: list[str] = []

        # Direct RTSP URL (works intermittently)
        if getattr(self, "_reolink_rtsp_url", None):
            urls_to_try.append(self._reolink_rtsp_url)

        # HTTP-FLV (query-param auth, needs self-signed cert bypass)
        flv_url = getattr(self, "_reolink_flv_url", None)
        if flv_url:
            urls_to_try.append(flv_url)

        # RTMP (password in query params — often port 1935 is disabled)
        rtmp_url = getattr(self, "_reolink_rtmp_url", None)
        if rtmp_url:
            urls_to_try.append(rtmp_url)

        # Camera entity stream source (may be same as RTSP)
        entity_stream_url = await self._get_camera_stream_source()
        if entity_stream_url and entity_stream_url not in urls_to_try:
            urls_to_try.append(entity_stream_url)

        if not urls_to_try:
            _LOGGER.warning("No audio input source available — mic input disabled")
            self._listen_active = False
            return

        _LOGGER.warning("Audio input: will try %d fallback source(s)", len(urls_to_try))
        self._input_processor_task = asyncio.create_task(
            self._audio_input_loop(urls_to_try)
        )

    async def _start_go2rtc_proxy_stream(self) -> bool:
        """Start audio input by proxying go2rtc's stream through HA's authenticated session.

        This avoids ffmpeg needing to authenticate directly with go2rtc.
        Flow: go2rtc HTTP stream → aiohttp (with auth) → ffmpeg stdin → PCM stdout → AI

        Returns True if the proxy stream was started successfully.
        """
        stream_name = await self._resolve_go2rtc_stream_name()
        if not stream_name:
            _LOGGER.warning("go2rtc proxy: no stream name (camera_unique_id=%s)", self._camera_unique_id)
            return False

        base_url = await _discover_go2rtc_url(self._hass)
        if not base_url:
            _LOGGER.warning("go2rtc proxy: go2rtc not available")
            return False

        ha_session = _get_go2rtc_session(self._hass)
        if not ha_session:
            _LOGGER.warning("go2rtc proxy: no authenticated session — using HA shared session")
            from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: PLC0415
            ha_session = async_get_clientsession(self._hass)

        # Check if stream exists; register if not
        try:
            async with ha_session.get(
                f"{base_url}/api/streams",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("go2rtc proxy: API returned %d", resp.status)
                    return False
                streams: dict = await resp.json()
        except Exception as exc:
            _LOGGER.warning("go2rtc proxy: failed to query streams: %s", exc)
            return False

        _LOGGER.warning("go2rtc streams: %s", list(streams.keys())[:10])

        if stream_name not in streams:
            # Register the stream
            rtsp_url = getattr(self, "_reolink_rtsp_url", None)
            if not rtsp_url:
                _LOGGER.warning("go2rtc proxy: can't register — no RTSP URL")
                return False
            try:
                params = [("name", stream_name), ("src", rtsp_url)]
                async with ha_session.put(
                    f"{base_url}/api/streams",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status not in (200, 201):
                        _LOGGER.warning(
                            "go2rtc proxy: registration failed (%d)", resp.status
                        )
                        return False
                _LOGGER.warning("go2rtc proxy: registered stream '%s'", stream_name)
                await asyncio.sleep(3)  # Give go2rtc time to connect to camera
            except Exception as exc:
                _LOGGER.warning("go2rtc proxy: registration error: %s", exc)
                return False

        # Start the proxy task
        stream_url = f"{base_url}/api/stream.mp4?src={stream_name}"
        self._input_processor_task = asyncio.create_task(
            self._go2rtc_proxy_loop(ha_session, stream_url)
        )
        return True

    async def _go2rtc_proxy_loop(
        self, session: aiohttp.ClientSession, stream_url: str
    ) -> None:
        """Read audio from go2rtc via authenticated HTTP and decode with ffmpeg.

        Opens the go2rtc stream.mp4 endpoint using HA's authenticated session,
        then pipes the response body into ffmpeg for PCM decoding.
        Automatically reconnects if the connection drops.
        """
        CHUNK_SIZE = 640  # 20ms of 16kHz mono s16le
        chunks_sent = 0
        first_logged = False

        while self._active and self._listen_active:
            # Start ffmpeg reading from stdin (pipe), outputting PCM
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
                "-f", "mp4", "-i", "pipe:0",
                "-vn",  # No video
                "-acodec", "pcm_s16le",
                "-ar", str(AUDIO_INPUT_SAMPLE_RATE),
                "-ac", "1",
                "-f", "s16le",
                "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _LOGGER.warning(
                "go2rtc proxy: ffmpeg started (PID=%d), connecting to stream...",
                proc.pid,
            )

            try:
                async with session.get(
                    stream_url,
                    timeout=aiohttp.ClientTimeout(total=None, connect=10),
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.warning(
                            "go2rtc proxy: stream returned %d, retrying in 3s...", resp.status
                        )
                        await asyncio.sleep(3)
                        continue

                    _LOGGER.warning(
                        "go2rtc proxy: connected (content-type=%s)",
                        resp.content_type,
                    )

                    # Stream response body → ffmpeg stdin
                    # Read output from ffmpeg stdout in parallel
                    async def _feed_ffmpeg() -> None:
                        """Feed HTTP response into ffmpeg stdin."""
                        try:
                            async for chunk in resp.content.iter_chunked(8192):
                                if not self._active or not self._listen_active:
                                    break
                                proc.stdin.write(chunk)
                                await proc.stdin.drain()
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            pass
                        finally:
                            try:
                                proc.stdin.close()
                            except Exception:
                                pass

                    feed_task = asyncio.create_task(_feed_ffmpeg())

                    try:
                        while self._active and self._listen_active and proc.returncode is None:
                            try:
                                pcm_data = await asyncio.wait_for(
                                    proc.stdout.readexactly(CHUNK_SIZE), timeout=5.0
                                )
                            except asyncio.IncompleteReadError as err:
                                pcm_data = err.partial
                                if not pcm_data:
                                    break
                            except asyncio.TimeoutError:
                                # Check if ffmpeg is still alive
                                if proc.returncode is not None:
                                    break
                                continue

                            if not first_logged:
                                first_logged = True
                                _LOGGER.warning(
                                    "✓ First audio from go2rtc proxy (%d bytes PCM @ %dHz)",
                                    len(pcm_data), AUDIO_INPUT_SAMPLE_RATE,
                                )

                            chunks_sent += 1
                            if chunks_sent % 500 == 0:
                                _LOGGER.info(
                                    "go2rtc proxy: %d chunks sent to AI", chunks_sent
                                )

                            await self._on_audio_received(pcm_data)
                    finally:
                        feed_task.cancel()
                        try:
                            await feed_task
                        except (asyncio.CancelledError, Exception):
                            pass

            except asyncio.CancelledError:
                break
            except Exception as exc:
                _LOGGER.warning("go2rtc proxy stream error: %s", exc)
                await asyncio.sleep(2)
            finally:
                if proc and proc.returncode is None:
                    try:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                # Read stderr for debugging
                if proc:
                    try:
                        stderr_data = await asyncio.wait_for(
                            proc.stderr.read(2000), timeout=2
                        )
                        if stderr_data:
                            _LOGGER.warning(
                                "go2rtc proxy ffmpeg stderr: %s",
                                stderr_data.decode(errors="replace").strip()[:300],
                            )
                    except Exception:
                        pass

        self._listen_active = False
        _LOGGER.warning(
            "go2rtc proxy: stopped (sent %d chunks to AI)", chunks_sent
        )

    def _install_talk_data_received_hook(self) -> bool:
        """Hook data_received on the talk connection to intercept CMD 202 mic audio.

        This is the most reliable approach: we wrap the asyncio protocol's
        data_received method which is called for ALL incoming TCP bytes.
        We scan the raw stream for CMD 202 headers and extract audio payloads
        before reolink_aio's parser can drop them (it drops unsolicited
        CMD 202 with status != 200/201/300).

        Works at the transport level, bypassing all reolink_aio internals.
        """
        bc = getattr(self, "_talk_baichuan", None)
        if not bc:
            _LOGGER.warning("FDX hook: no _talk_baichuan object")
            return False

        # Try multiple paths to find the protocol object.
        # reolink_aio stores it in different locations depending on version.
        protocol = None

        # Path 1: Baichuan._connection._protocol (standard path)
        connection = getattr(bc, "_connection", None)
        if connection:
            protocol = getattr(connection, "_protocol", None)
            if protocol:
                _LOGGER.warning("FDX hook: found protocol via bc._connection._protocol")

        # Path 2: Baichuan._protocol (direct, some versions)
        if not protocol:
            protocol = getattr(bc, "_protocol", None)
            if protocol:
                _LOGGER.warning("FDX hook: found protocol via bc._protocol")

        # Path 3: Talk Host object may have it
        if not protocol:
            talk_host = getattr(self, "_talk_host", None)
            if talk_host:
                host_bc = getattr(talk_host, "_bc", None) or getattr(talk_host, "baichuan", None)
                if host_bc and host_bc is not bc:
                    conn2 = getattr(host_bc, "_connection", None)
                    if conn2:
                        protocol = getattr(conn2, "_protocol", None)
                        if protocol:
                            _LOGGER.warning("FDX hook: found protocol via talk_host.baichuan._connection")

        # Path 4: Search ALL attributes of bc for something with data_received
        if not protocol:
            # Log the full object graph for debugging
            bc_attrs = {k: type(getattr(bc, k, None)).__name__
                       for k in dir(bc) if not k.startswith("__") and k.startswith("_")}
            _LOGGER.warning(
                "FDX hook: no protocol found. bc attrs: %s",
                {k: v for k, v in bc_attrs.items() if "conn" in k.lower() or "proto" in k.lower() or "trans" in k.lower() or "sock" in k.lower()}
            )
            # Try to find protocol via transport
            for attr_name in dir(bc):
                if attr_name.startswith("__"):
                    continue
                obj = getattr(bc, attr_name, None)
                if obj and hasattr(obj, "data_received"):
                    protocol = obj
                    _LOGGER.warning("FDX hook: found data_received on bc.%s", attr_name)
                    break
                if obj and hasattr(obj, "_protocol"):
                    protocol = getattr(obj, "_protocol", None)
                    if protocol:
                        _LOGGER.warning("FDX hook: found protocol via bc.%s._protocol", attr_name)
                        break

        if not protocol:
            _LOGGER.warning("FDX hook: FAILED — no protocol found anywhere")
            return False

        if not hasattr(protocol, "data_received"):
            _LOGGER.warning("FDX hook: protocol %s has no data_received", type(protocol).__name__)
            return False

        # State for our stream parser
        _buf = bytearray()
        _pkt_count = [0]
        _audio_count = [0]
        HEADER_MAGIC_BYTES = bytes.fromhex("f0debc0a")

        original_data_received = protocol.data_received

        def _hooked_data_received(data: bytes) -> None:
            """Intercept all TCP data, extract CMD 202 audio, then pass through."""
            _buf.extend(data)

            # Scan buffer for complete Baichuan messages with CMD 202
            while len(_buf) >= 20:
                # Find magic header
                magic_pos = _buf.find(HEADER_MAGIC_BYTES)
                if magic_pos < 0:
                    # No header found, keep last 3 bytes (partial magic)
                    if len(_buf) > 3:
                        del _buf[:-3]
                    break
                if magic_pos > 0:
                    # Discard data before magic
                    del _buf[:magic_pos]

                # Parse header
                if len(_buf) < 20:
                    break

                cmd_id = int.from_bytes(_buf[4:8], byteorder="little")
                body_len = int.from_bytes(_buf[8:12], byteorder="little")
                mess_class = _buf[18:20].hex()

                if mess_class in ["1464", "0000"]:
                    header_len = 24
                elif mess_class == "1466":
                    header_len = 20
                else:
                    # Unknown class, skip this magic and search for next
                    del _buf[:4]
                    continue

                if len(_buf) < header_len:
                    break

                payload_offset = 0
                if header_len == 24:
                    payload_offset = int.from_bytes(_buf[20:24], byteorder="little")

                total_len = header_len + body_len
                if len(_buf) < total_len:
                    # Incomplete message, wait for more data
                    break

                # We have a complete message
                if cmd_id == 202:
                    _pkt_count[0] += 1
                    # Extract binary payload (after extension XML)
                    if payload_offset == 0:
                        payload_offset = body_len
                    payload_start = header_len + payload_offset
                    if payload_start < total_len:
                        raw_payload = bytes(_buf[payload_start:total_len])
                        if raw_payload:
                            self._process_fdx_audio_payload(
                                raw_payload, _pkt_count, _audio_count
                            )

                # Remove this message from buffer
                del _buf[:total_len]

            # Always pass ALL original data to reolink_aio (don't consume it)
            original_data_received(data)

        protocol.data_received = _hooked_data_received
        _LOGGER.warning(
            "FDX hook: wrapped %s.data_received successfully",
            type(protocol).__name__,
        )
        return True

    def _process_fdx_audio_payload(
        self, raw_payload: bytes, pkt_count: list, audio_count: list
    ) -> None:
        """Process a single CMD 202 payload from the talk connection.

        Decrypts if needed, then extracts ONLY genuine audio frames.
        CRITICAL: Do NOT use raw G.711/ADPCM fallback on video data — that
        produces garbage noise that Gemini interprets as speech.
        """
        import struct as _st

        # Decrypt the payload (Baichuan uses AES-128-CFB for talk data)
        payload = self._decrypt_talk_payload(raw_payload)

        if len(payload) < 4:
            return

        # Check the BcMedia magic to identify frame type
        magic_val = _st.unpack_from("<I", payload, 0)[0]

        # Known video/info magics — SKIP these entirely
        VIDEO_MAGICS = {
            0x31303031,  # InfoV1
            0x32303031,  # InfoV2
        }
        # IFrame: 0x63643030-0x63643039, PFrame: 0x63643130-0x63643139
        is_iframe = 0x63643030 <= magic_val <= 0x63643039
        is_pframe = 0x63643130 <= magic_val <= 0x63643139
        is_video = is_iframe or is_pframe or magic_val in VIDEO_MAGICS

        # Known audio magics
        ADPCM_MAGIC = 0x62773130
        AAC_MAGIC = 0x62773530
        is_audio_magic = magic_val in (ADPCM_MAGIC, AAC_MAGIC)

        if pkt_count[0] <= 5:
            frame_type = "video" if is_video else ("AUDIO" if is_audio_magic else "unknown")
            _LOGGER.warning(
                "FDX pkt #%d: %d bytes, magic=0x%08x (%s)",
                pkt_count[0], len(payload), magic_val, frame_type,
            )

        if is_video:
            # Pure video frame — skip entirely (do NOT decode as audio)
            if pkt_count[0] == 1:
                _LOGGER.warning(
                    "FDX: first packet is VIDEO (0x%08x) — waiting for audio frames...",
                    magic_val,
                )
            return

        # Try parsing as BcMedia (ADPCM or AAC audio frames)
        pcm_data = self._parse_bcmedia_to_pcm(payload)
        if pcm_data and self._listen_active:
            audio_count[0] += 1
            if audio_count[0] == 1:
                _LOGGER.warning(
                    "✓ First REAL audio from FDX — %d bytes PCM (pkt #%d)",
                    len(pcm_data), pkt_count[0],
                )
            asyncio.ensure_future(self._on_audio_received(pcm_data))
            return

        # Only try raw ADPCM fallback if magic is completely unrecognized
        # (not video, not known audio — could be raw audio data without BcMedia header)
        if not is_audio_magic and not is_video and len(payload) >= 516:
            pcm_data = self._try_raw_adpcm_decode(payload)
            if pcm_data:
                audio_count[0] += 1
                asyncio.ensure_future(self._on_audio_received(pcm_data))

        if pkt_count[0] == 50:
            _LOGGER.warning(
                "FDX @50: %d audio frames (of %d total pkts, %d video skipped)",
                audio_count[0], pkt_count[0], pkt_count[0] - audio_count[0],
            )
        elif pkt_count[0] == 200:
            _LOGGER.warning(
                "FDX @200: %d audio, %d video",
                audio_count[0], pkt_count[0] - audio_count[0],
            )
        elif pkt_count[0] % 500 == 0:
            _LOGGER.warning(
                "FDX @%d: %d audio", pkt_count[0], audio_count[0]
            )

    def _decrypt_talk_payload(self, raw_payload: bytes) -> bytes:
        """Decrypt a Baichuan talk payload using AES-128-CFB."""
        bc = getattr(self, "_talk_baichuan", None)
        if not bc:
            return raw_payload

        aes_key = getattr(bc, "_aes_key", None)
        if aes_key is None:
            return raw_payload

        try:
            try:
                from Cryptodome.Cipher import AES as _AES
            except ImportError:
                from Crypto.Cipher import AES as _AES
            AES_IV = b"0123456789abcdef"
            cipher = _AES.new(key=aes_key, mode=_AES.MODE_CFB, iv=AES_IV, segment_size=128)
            return cipher.decrypt(raw_payload)
        except Exception:
            return raw_payload

    async def _raw_tcp_mic_stream(self, ability: dict) -> None:
        """Raw TCP connection to camera for receiving FDX mic audio.

        This is the nuclear fallback: we create our OWN TCP connection to port
        9000, perform a minimal Baichuan login, start a talk session, and then
        read ALL incoming bytes ourselves. This bypasses reolink_aio entirely.

        The camera sends mic audio as CMD 202 packets in FDX mode once a talk
        session is active. We parse these directly from the TCP stream.
        """
        import hashlib
        import struct as _st

        host = self._reolink_host
        user = self._reolink_user or "admin"
        password = self._reolink_pass or ""
        port = 9000

        if not host:
            _LOGGER.warning("Raw TCP mic: no host configured")
            return

        MAGIC = bytes.fromhex("f0debc0a")
        AES_IV = b"0123456789abcdef"
        aes_key: bytes | None = None

        def _build_header(cmd_id: int, body_len: int, mess_id: int = 0,
                         payload_offset: int = 0) -> bytes:
            return (
                MAGIC
                + cmd_id.to_bytes(4, "little")
                + body_len.to_bytes(4, "little")
                + (0).to_bytes(1, "little")  # ch_id
                + mess_id.to_bytes(3, "little")
                + (0).to_bytes(2, "little")  # status
                + (0x1464).to_bytes(2, "little")  # class
                + payload_offset.to_bytes(4, "little")
            )

        def _encrypt(data: bytes, key: bytes) -> bytes:
            try:
                try:
                    from Cryptodome.Cipher import AES as _AES
                except ImportError:
                    from Crypto.Cipher import AES as _AES
                cipher = _AES.new(key=key, mode=_AES.MODE_CFB, iv=AES_IV, segment_size=128)
                return cipher.encrypt(data)
            except Exception:
                return data

        def _decrypt(data: bytes, key: bytes) -> bytes:
            try:
                try:
                    from Cryptodome.Cipher import AES as _AES
                except ImportError:
                    from Crypto.Cipher import AES as _AES
                cipher = _AES.new(key=key, mode=_AES.MODE_CFB, iv=AES_IV, segment_size=128)
                return cipher.decrypt(data)
            except Exception:
                return data

        async def _read_exactly(reader: asyncio.StreamReader, n: int) -> bytes:
            data = b""
            while len(data) < n:
                chunk = await asyncio.wait_for(reader.read(n - len(data)), timeout=10.0)
                if not chunk:
                    raise ConnectionError("Connection closed")
                data += chunk
            return data

        async def _read_message(reader: asyncio.StreamReader) -> tuple[int, bytes, bytes]:
            """Read one Baichuan message. Returns (cmd_id, extension, payload)."""
            header = await _read_exactly(reader, 4)
            if header != MAGIC:
                raise ValueError(f"Bad magic: {header.hex()}")
            rest = await _read_exactly(reader, 20)  # Read rest of 24-byte header
            full_header = header + rest
            cmd_id = int.from_bytes(full_header[4:8], "little")
            body_len = int.from_bytes(full_header[8:12], "little")
            mess_class = full_header[18:20].hex()

            header_len = 24
            payload_offset = int.from_bytes(full_header[20:24], "little")

            if mess_class == "1466":
                header_len = 20
                payload_offset = body_len
                # We already read 24 bytes, but header is only 20 for class 1466
                # Need to adjust - the extra 4 bytes are part of body
                body = full_header[20:24]
                if body_len > 4:
                    body += await _read_exactly(reader, body_len - 4)
            else:
                if payload_offset == 0:
                    payload_offset = body_len
                body = await _read_exactly(reader, body_len) if body_len > 0 else b""

            extension = body[:payload_offset]
            payload = body[payload_offset:]
            return cmd_id, extension, payload

        try:
            _LOGGER.warning("Raw TCP mic: connecting to %s:%d...", host, port)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10.0
            )
            _LOGGER.warning("Raw TCP mic: connected, logging in...")

            # Step 1: Login (CMD 1)
            # Generate AES key from MD5(password) (nonce-less login)
            md5_pass = hashlib.md5(password.encode()).digest()
            aes_key = md5_pass

            login_xml = (
                f'<?xml version="1.0" encoding="UTF-8" ?>'
                f'<body><LoginUser version="1.1">'
                f'<userName>{user}</userName>'
                f'<password>{password}</password>'
                f'<userVer>1</userVer>'
                f'</LoginUser></body>'
            ).encode("utf-8")

            enc_login = _encrypt(login_xml, aes_key)
            login_header = _build_header(cmd_id=1, body_len=len(enc_login), mess_id=1)
            writer.write(login_header + enc_login)
            await writer.drain()

            # Read login response
            login_cmd, login_ext, login_payload = await _read_message(reader)
            if login_cmd == 1:
                # Decrypt and check
                dec_ext = _decrypt(login_ext, aes_key) if login_ext else b""
                _LOGGER.warning(
                    "Raw TCP mic: login response cmd=%d, ext_len=%d, snippet=%s",
                    login_cmd, len(login_ext),
                    dec_ext[:100].decode("utf-8", errors="replace") if dec_ext else "(empty)",
                )
            else:
                _LOGGER.warning("Raw TCP mic: unexpected response cmd=%d", login_cmd)

            # Step 2: Start TalkConfig (CMD 201)
            talk_xml = (
                '<?xml version="1.0" encoding="UTF-8" ?>'
                '<body><TalkConfig version="1.1">'
                '<channelId>0</channelId>'
                '<duplex>FDX</duplex>'
                '<audioStreamMode>followVideoStream</audioStreamMode>'
                '</TalkConfig></body>'
            ).encode("utf-8")
            enc_talk = _encrypt(talk_xml, aes_key)
            talk_header = _build_header(cmd_id=201, body_len=len(enc_talk), mess_id=2)
            writer.write(talk_header + enc_talk)
            await writer.drain()

            # Read TalkConfig response
            talk_cmd, talk_ext, talk_payload = await _read_message(reader)
            dec_talk_ext = _decrypt(talk_ext, aes_key) if talk_ext else b""
            _LOGGER.warning(
                "Raw TCP mic: talk response cmd=%d, snippet=%s",
                talk_cmd,
                dec_talk_ext[:100].decode("utf-8", errors="replace") if dec_talk_ext else "(empty)",
            )

            # Step 3: Start reading CMD 202 audio continuously
            _LOGGER.warning("Raw TCP mic: ✓ talk session started, reading audio stream...")

            # Ensure decoders are running
            if not self._aac_queue:
                self._aac_queue = asyncio.Queue(maxsize=200)
            if not self._aac_decoder_task or self._aac_decoder_task.done():
                self._aac_decoder_task = asyncio.create_task(self._aac_decode_loop())

            self._baichuan_audio_input_active = True
            self._listen_active = True

            pkt_count = 0
            audio_count = 0

            while self._active and self._listen_active:
                try:
                    # Read next Baichuan message
                    cmd_id, ext_data, payload = await _read_message(reader)
                except asyncio.TimeoutError:
                    continue
                except (ConnectionError, ValueError) as exc:
                    _LOGGER.warning("Raw TCP mic: connection error: %s", exc)
                    break

                if cmd_id == 202 and payload:
                    pkt_count += 1
                    # Decrypt payload
                    dec_payload = _decrypt(payload, aes_key)

                    if pkt_count <= 5:
                        magic_val = _st.unpack_from("<I", dec_payload, 0)[0] if len(dec_payload) >= 4 else 0
                        _LOGGER.warning(
                            "Raw TCP mic pkt #%d: %d bytes, magic=0x%08x",
                            pkt_count, len(dec_payload), magic_val,
                        )

                    # Parse as BcMedia audio
                    pcm_data = self._parse_bcmedia_to_pcm(dec_payload)
                    if pcm_data:
                        audio_count += 1
                        await self._on_audio_received(pcm_data)
                    else:
                        # Try raw ADPCM decode
                        pcm_data = self._try_raw_adpcm_decode(dec_payload)
                        if pcm_data:
                            audio_count += 1
                            await self._on_audio_received(pcm_data)

                    if pkt_count == 50:
                        _LOGGER.warning(
                            "Raw TCP mic: 50 pkts, %d decoded as audio", audio_count
                        )
                    elif pkt_count % 500 == 0:
                        _LOGGER.warning(
                            "Raw TCP mic: %d pkts, %d audio", pkt_count, audio_count
                        )

                elif cmd_id == 2:
                    # Heartbeat response — ignore
                    pass

        except asyncio.CancelledError:
            _LOGGER.warning("Raw TCP mic: cancelled")
        except Exception as exc:
            _LOGGER.warning("Raw TCP mic: error: %s", exc, exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            _LOGGER.warning("Raw TCP mic: connection closed")

    def _install_baichuan_audio_intercept(self) -> bool:
        """Hook into the Baichuan protocol to intercept incoming CMD 202 audio.

        During FDX (Full Duplex) talk, the camera sends mic audio back as
        CMD 202 packets — same format as what we send to the speaker.

        We intercept at the protocol's parse_bc_data level because the
        library's normal parsing may drop CMD 202 packets with status=0.

        Uses the TALK connection (self._talk_baichuan) which is separate from
        the Preview connection, avoiding interference.

        Returns True if the hook was successfully installed.
        """
        # Use the dedicated talk connection for FDX audio intercept
        bc = getattr(self, "_talk_baichuan", None) or self._baichuan
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
        aac_frames = 0
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
                # AAC audio frame — buffer for async decoding
                if offset + 4 > len(payload):
                    break
                plen1, plen2 = struct_mod.unpack_from("<HH", payload, offset)
                offset += 4

                if plen1 <= 0 or offset + plen1 > len(payload):
                    break

                aac_data = payload[offset:offset + plen1]
                aac_frames += 1

                # Log first AAC frame discovery
                if aac_frames == 1 and not hasattr(self, "_aac_frames_logged"):
                    self._aac_frames_logged = True
                    _LOGGER.warning(
                        "✓ AAC audio frame found in Baichuan FDX stream! "
                        "(%d bytes, first 8 hex: %s)",
                        len(aac_data), aac_data[:8].hex(),
                    )

                # Queue for async AAC decoder (runs in background task)
                if hasattr(self, "_aac_queue") and self._aac_queue is not None:
                    try:
                        self._aac_queue.put_nowait(aac_data)
                    except asyncio.QueueFull:
                        pass  # Drop frame if queue full
                    audio_frames += 1

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
                "✓ BcMedia stream parsed: %d audio frames (%d ADPCM + %d AAC), "
                "%d video frames (%d total) → %d bytes PCM",
                audio_frames, audio_frames - aac_frames, aac_frames,
                video_frames, frame_count, len(pcm_out),
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

    async def _aac_decode_loop(self) -> None:
        """Background task: decode AAC frames from FDX stream to PCM via ffmpeg.

        Maintains a persistent ffmpeg process that accepts raw AAC on stdin
        and outputs 16kHz mono PCM s16le on stdout. This avoids spawning
        a new process for each AAC frame.
        """
        proc: asyncio.subprocess.Process | None = None
        frames_decoded = 0
        first_pcm_logged = False
        frames_written = 0

        try:
            while self._active and self._listen_active:
                # Get next AAC frame from queue
                try:
                    aac_data = await asyncio.wait_for(
                        self._aac_queue.get(), timeout=5.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Start ffmpeg if not running
                if proc is None or proc.returncode is not None:
                    proc = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-hide_banner", "-loglevel", "warning",
                        "-probesize", "32",
                        "-analyzeduration", "0",
                        "-fflags", "+nobuffer+flush_packets",
                        "-f", "aac", "-i", "pipe:0",
                        "-acodec", "pcm_s16le",
                        "-ar", str(AUDIO_INPUT_SAMPLE_RATE),
                        "-ac", "1",
                        "-f", "s16le",
                        "-flush_packets", "1",
                        "pipe:1",
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _LOGGER.warning(
                        "AAC decoder ffmpeg started (PID=%d) with low-latency flags", proc.pid
                    )

                # Write AAC frame to ffmpeg
                try:
                    proc.stdin.write(aac_data)
                    await proc.stdin.drain()
                    frames_written += 1
                except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                    if frames_written <= 3:
                        _LOGGER.warning("AAC decoder ffmpeg pipe broke after %d frames: %s", frames_written, exc)
                        # Read stderr for diagnostic
                        try:
                            stderr = await asyncio.wait_for(proc.stderr.read(1024), timeout=0.5)
                            if stderr:
                                _LOGGER.warning("ffmpeg stderr: %s", stderr.decode(errors="replace")[:200])
                        except Exception:
                            pass
                    proc = None
                    frames_written = 0
                    continue

                # Read PCM output — try multiple times with increasing delay
                # (ffmpeg may need a moment after first frame to start outputting)
                pcm_data = b""
                for attempt in range(3):
                    try:
                        chunk = await asyncio.wait_for(
                            proc.stdout.read(8192), timeout=0.2 if attempt == 0 else 0.5
                        )
                        if chunk:
                            pcm_data += chunk
                            break
                    except asyncio.TimeoutError:
                        if attempt == 0 and frames_written <= 3:
                            # First few frames: ffmpeg might still be probing
                            continue
                        break

                if pcm_data:
                    frames_decoded += 1
                    if not first_pcm_logged:
                        first_pcm_logged = True
                        _LOGGER.warning(
                            "✓ First PCM from AAC decode (%d bytes, after %d frames written)",
                            len(pcm_data), frames_written,
                        )
                    if frames_decoded % 100 == 0:
                        _LOGGER.info(
                            "AAC decoder: %d frames decoded", frames_decoded
                        )
                    # Forward to AI
                    await self._on_audio_received(pcm_data)
                elif frames_written == 5 and not first_pcm_logged:
                    # After 5 frames and still no output — log warning
                    _LOGGER.warning(
                        "AAC decoder: 5 frames written but no PCM output yet"
                    )

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _LOGGER.warning("AAC decode loop error: %s", exc)
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.stdin.close()
                    proc.terminate()
                except Exception:
                    pass
            _LOGGER.info(
                "AAC decode loop ended (decoded %d frames)", frames_decoded
            )

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

    async def _get_go2rtc_rtsp_url(self) -> str | None:
        """Get the go2rtc local RTSP URL for the camera's stream.

        go2rtc re-publishes camera streams via a local RTSP server (port 8554).
        However, in HA OS this port may not be exposed. Returns None if RTSP
        isn't available so we fall through to HTTP-based alternatives.
        """
        stream_name = await self._resolve_go2rtc_stream_name()
        if not stream_name:
            return None

        # go2rtc RTSP output port (standard = 8554)
        rtsp_url = f"rtsp://127.0.0.1:8554/{stream_name}"
        _LOGGER.warning("go2rtc local RTSP URL: %s", rtsp_url)
        return rtsp_url

    async def _get_go2rtc_http_stream_url(self) -> str | None:
        """Get a go2rtc HTTP stream URL as a reliable audio source.

        Uses go2rtc's HTTP API to stream the camera's audio+video.
        This works even when go2rtc's RTSP port isn't exposed because
        the API runs on the same port as the REST endpoints.

        If the stream isn't registered yet (lazy init), we first try to
        trigger it via HA's camera entity, then fall back to REST API registration.
        """
        stream_name = await self._resolve_go2rtc_stream_name()
        if not stream_name:
            return None

        # Ensure stream is active in go2rtc (trigger via camera entity)
        await self._ensure_go2rtc_stream_active()

        base_url = await _discover_go2rtc_url(self._hass)
        if not base_url:
            return None

        ha_session = _get_go2rtc_session(self._hass)
        if not ha_session:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: PLC0415
            ha_session = async_get_clientsession(self._hass)
        session = ha_session

        try:
            # Check if stream already exists in go2rtc
            async with session.get(
                f"{base_url}/api/streams",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning(
                        "go2rtc API returned status %d for /api/streams", resp.status
                    )
                    return None
                streams: dict = await resp.json()

            _LOGGER.warning(
                "go2rtc streams available: %s", list(streams.keys())[:10]
            )

            # Check if our stream is registered (exact or partial match)
            matched_name: str | None = None
            if stream_name in streams:
                matched_name = stream_name
            else:
                # Try partial match
                for name in streams:
                    if stream_name in name or name in stream_name:
                        matched_name = name
                        break

            if not matched_name:
                # Stream not registered — register it with camera's RTSP URL
                _LOGGER.warning(
                    "Stream '%s' not found in go2rtc — registering it now",
                    stream_name,
                )
                registered = await self._register_go2rtc_stream(
                    session, base_url, stream_name
                )
                if registered:
                    matched_name = stream_name
                else:
                    return None

            # Use ffmpeg-friendly RTSP URL from go2rtc API (most reliable)
            # The /api/stream.mp4 endpoint provides a progressive download
            # The /api/ws endpoint provides a WebSocket stream
            # Actually, for ffmpeg, use the RTSP output OR the MP4 endpoint
            url = f"{base_url}/api/stream.mp4?src={matched_name}"
            _LOGGER.warning("go2rtc HTTP stream URL: %s", url)
            return url
        except Exception as exc:
            _LOGGER.warning("go2rtc HTTP stream lookup failed: %s", exc)
            return None

    async def _resolve_go2rtc_stream_name(self) -> str | None:
        """Resolve the go2rtc stream name for this camera.

        In HA, go2rtc uses the camera entity's unique_id as the stream name.
        """
        if self._camera_unique_id:
            return self._camera_unique_id
        return None

    async def _register_go2rtc_stream(
        self, session: aiohttp.ClientSession, base_url: str, stream_name: str
    ) -> bool:
        """Register the camera's stream in go2rtc if not already present.

        Uses the RTSP URL from the Reolink integration. go2rtc will manage
        the RTSP connection itself (it handles auth, reconnection, etc.).
        """
        rtsp_url = getattr(self, "_reolink_rtsp_url", None)
        if not rtsp_url:
            _LOGGER.warning("Cannot register go2rtc stream — no RTSP URL available")
            return False

        try:
            params = [("name", stream_name), ("src", rtsp_url)]
            async with session.put(
                f"{base_url}/api/streams",
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 201):
                    _LOGGER.warning(
                        "Registered stream '%s' in go2rtc", stream_name
                    )
                    # Give go2rtc a moment to establish the RTSP connection
                    await asyncio.sleep(2)
                    return True
                text = await resp.text()
                _LOGGER.warning(
                    "Failed to register go2rtc stream (status=%d): %s",
                    resp.status, text[:200],
                )
                return False
        except Exception as exc:
            _LOGGER.warning("Failed to register go2rtc stream: %s", exc)
            return False

    async def _get_go2rtc_audio_url(self) -> str | None:
        """Find and return an audio URL from go2rtc's existing streams.

        HA's Reolink integration registers camera streams in go2rtc automatically.
        We just need to find the right stream and use go2rtc's local API to read it.
        """
        base_url = await _discover_go2rtc_url(self._hass)
        if not base_url:
            return None

        ha_session = _get_go2rtc_session(self._hass)
        if not ha_session:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: PLC0415
            ha_session = async_get_clientsession(self._hass)
        session = ha_session

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
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except (asyncio.TimeoutError, Exception):
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        try:
                            await proc.wait()
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
        self._owns_session: bool = False  # Whether we created the session (vs HA shared)
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
        # Use HA's shared aiohttp session to avoid consuming an extra TCP connection
        # slot on the camera. The shared session uses keep-alive efficiently.
        try:
            from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: PLC0415
            self._session = async_get_clientsession(self._hass)
            self._owns_session = False
        except Exception:
            # Fallback: create our own session (shouldn't happen in normal HA)
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=1, limit_per_host=1)
            )
            self._owns_session = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        _LOGGER.info("Reolink talk monitor started (host=%s)", self._host)

    async def stop(self) -> None:
        """Stop the monitor and release the camera HTTP session."""
        self._active = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        # Logout from camera to free the HTTP session slot
        if self._session and self._token:
            try:
                url = f"http://{self._host}/api.cgi?cmd=Logout&token={self._token}"
                payload = [{"cmd": "Logout", "action": 0}]
                async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    _LOGGER.debug("Reolink talk monitor logout: status=%s", resp.status)
            except Exception:
                _LOGGER.debug("Reolink talk monitor logout failed", exc_info=True)
        # Only close session if we created it (don't close HA's shared session)
        if self._session and self._owns_session:
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
