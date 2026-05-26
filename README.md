# 🔔 ha-doorbell-jeeves

**Production-ready multimodal AI concierge** for smart doorbells — multi-provider (Gemini + OpenAI + local models), configurable per-action security, and native Reolink support.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🎙️ Real-time speech | Gemini Live or OpenAI Realtime API (native audio) |
| 🌐 Multi-provider | Google Gemini, OpenAI cloud, or any OpenAI-compatible local model |
| 📷 Configurable vision | 0.1 FPS (10s intervals) to 60 FPS (real-time video) |
| 🖼️ Frame downscaling | Pillow-based resize before sending — faster responses |
| 👤 Identity (sensor) | Frigate / CompreFace / Double Take integration |
| 📸 Identity (photos) | Upload reference images of people & pets |
| 🏠 Tool calling | Lights, locks, switches, covers, sensors, notifications |
| 🔒 Entity allowlisting | AI can ONLY access admin-approved entities |
| 🛡️ Flexible policies | Any action: auto / validated / PIN / PIN+validated |
| 🤖 Validator agent | Independent AI verifies actions (per-action config) |
| ⏹️ Auto-stop triggers | Entity state change, HA events, or human takeover |
| 📱 Reolink compatible | Works with Reolink POE doorbells out of the box |
| ⏱️ Session timeout | Configurable auto-hangup (0 = unlimited) |
| 🔍 Audit trail | Full session logging |
| ⚙️ 6-step GUI config | No YAML required |

---

## 🌐 Supported Providers

| Provider | Models | Use Case |
|----------|--------|----------|
| **Google Gemini** | `gemini-2.5-flash-native-audio-dialog`, `gemini-2.0-flash-live-001` | Best native audio quality |
| **OpenAI** | `gpt-4o-realtime-preview`, `gpt-4o-mini-realtime-preview` | Alternative cloud provider |
| **Local (OpenAI-compatible)** | Any model via custom base URL | Privacy, offline, self-hosted |

For local models, select "OpenAI / Compatible" and set the base URL to your server (e.g., `http://192.168.1.100:8080/v1`). Compatible with LocalAI, vLLM, Ollama (with OpenAI adapter), and any server implementing the OpenAI Realtime API.

---

## 📱 Reolink POE Doorbell Setup

This integration works natively with Reolink POE doorbells:

1. **Camera entity**: The Reolink integration exposes `camera.doorbell` — use this for vision.
2. **Audio input**: Use go2rtc to extract audio from the RTSP stream (see Audio Pipeline below).
3. **Audio output**: Target the Reolink's media player entity for 2-way talk output.
4. **Auto-stop on human takeover**: Add `binary_sensor.reolink_visitor_active` to stop triggers — when you open the Reolink app and start talking, Jeeves automatically yields.
5. **Doorbell button trigger**: Use `binary_sensor.doorbell_button` in your automation to start the session.

### Reolink Auto-Stop Configuration

In Step 5 (Stop Triggers):
- **Stop entities**: `binary_sensor.reolink_visitor_active`
- **State map**: `binary_sensor.reolink_visitor_active: on`

This means: when the Reolink app goes active (human takeover), immediately stop Jeeves.

---

## 🖼️ Vision & Frame Processing

| Setting | Range | Description |
|---------|-------|-------------|
| **Vision FPS** | 0.1 – 60 | Frames per second sent to the AI |
| **Max Width** | 160 – 1920 px | Downscale target width |
| **Max Height** | 120 – 1080 px | Downscale target height |
| **JPEG Quality** | 10 – 100 | Compression (lower = smaller/faster) |

**Recommended for low-latency conversations:**
- FPS: 1.0 (one frame per second — plenty for doorbell use)
- Resolution: 640×480
- Quality: 60-70

**For high-detail scenarios (package inspection, face verification):**
- FPS: 2-5
- Resolution: 1280×720
- Quality: 80

Frame processing uses Pillow's LANCZOS resampling and runs in an executor thread to avoid blocking the HA event loop.

---

## ⏹️ Auto-Stop Triggers

The session automatically stops when:

| Trigger Type | Example | Use Case |
|------|---------|----------|
| **Entity state change** | `binary_sensor.reolink_visitor_active` → `on` | Human opens Reolink app |
| **Entity state change** | `lock.front_door` → `unlocked` | Door was opened physically |
| **Entity state change** | `binary_sensor.front_door` → `on` | Door contact sensor |
| **HA event** | Custom event type | Any automation can stop Jeeves |
| **Timeout** | Configurable (default 120s) | No-activity safety net |

### Stop on Human Takeover (Reolink)

When you start 2-way talk via the Reolink app, the `binary_sensor.reolink_visitor_active` entity becomes `on`. Configure this as a stop trigger and Jeeves immediately yields to you.

### Stop via HA Action

Any automation can call `ha_doorbell_jeeves.stop_session` — e.g., when you tap a notification button saying "I'll handle it."

---

## 🔒 Security Model

Every action independently configured:

```
turn_on_light    → auto (no checks)
unlock_door      → pin_and_validated + camera + visual match + max 1/session
open_cover       → validated + camera + cooldown 30s
send_notification → auto
get_sensor_state → auto
```

### Validator Agent
- Completely independent API call (different model instance)
- Hardcoded immutable system prompt (cannot be social-engineered)
- Receives camera frame + reference photo (configurable per action)
- Requires ≥80% confidence
- Reports threat indicators

### Anti-Tamper Layers
1. Tool enum constraints — model only sees allowed entity IDs
2. Server-side allowlist — rejects even hallucinated entities
3. Per-action rate limiting and cooldowns
4. Independent validator agent
5. PIN protection
6. Prompt hardening with immutable security directives
7. Audit trail for forensic review

---

## 📦 Installation

### Option 1: HACS (Recommended)

1. Make sure [HACS](https://hacs.xyz/) is installed.
2. In HACS, click the 3-dot menu → **Custom repositories**.
3. Add `https://github.com/gomme600/HA-Doorbell-Jeeves` as type **Integration**.
4. Search for "Doorbell Jeeves" in HACS and click **Install**.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services → Add Integration → Doorbell Jeeves**.

### Option 2: Manual

1. Download the [latest release](https://github.com/gomme600/HA-Doorbell-Jeeves/releases) or clone this repo.
2. Copy the `custom_components/ha_doorbell_jeeves/` folder into your Home Assistant `config/custom_components/` directory:
   ```bash
   # From the repo root
   cp -r custom_components/ha_doorbell_jeeves /path/to/ha/config/custom_components/
   ```
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration → Doorbell Jeeves**.

### Requirements
- Home Assistant 2024.12+
- Python 3.12+
- One of: Gemini API key, OpenAI API key, or local model server
- Camera entity (Reolink, Frigate, generic)
- Media player entity for audio output

---

## ⚙️ Setup Wizard (6 Steps)

1. **Provider & API** — Gemini/OpenAI/local, key, model, voice
2. **Camera & Vision** — camera entity, speaker, FPS, frame size, quality, timeout
3. **Allowed Entities** — per-domain multi-select
4. **Security Policies** — per-action modes, validator config, PIN
5. **Auto-Stop Triggers** — entities, state conditions, events
6. **Identity & Prompt** — face recognition mode, system prompt

All settings editable post-setup via **Settings → Integrations → Configure**.

---

## 🤖 Automation Example

```yaml
automation:
  - alias: "Jeeves - Answer Doorbell"
    trigger:
      - platform: state
        entity_id: binary_sensor.doorbell_button
        to: "on"
    condition:
      - condition: state
        entity_id: input_boolean.jeeves_enabled
        state: "on"
    action:
      - service: ha_doorbell_jeeves.start_session
        data:
          entry_id: !secret jeeves_entry_id

  - alias: "Jeeves - Security Alert → Phone"
    trigger:
      - platform: event
        event_type: ha_doorbell_jeeves_security_alert
    action:
      - service: notify.mobile_app
        data:
          title: "🚨 Doorbell Alert"
          message: "{{ trigger.event.data.reasoning }}"
```

---

## 🔊 Audio Pipeline (Reolink)

```bash
# go2rtc streams Reolink RTSP → extract audio → convert to PCM → send to HA
ffmpeg -i rtsp://doorbell:554/h264Preview_01_sub \
  -vn -acodec pcm_s16le -ar 24000 -ac 1 -f s16le pipe:1 | \
  python3 stream_to_ha.py --entry-id YOUR_ID --ha-url http://ha:8123
```

Or use go2rtc's built-in audio extraction with a lightweight relay script.

---

## 📁 Structure

```
custom_components/ha_doorbell_jeeves/
├── __init__.py           # Entry point
├── client_base.py        # Abstract client protocol
├── config_flow.py        # 6-step wizard + options flow
├── const.py              # Constants, providers, modes
├── frame_processor.py    # Pillow-based downscaling
├── gemini_client.py      # Gemini Live implementation
├── identity.py           # Known faces + persistent storage
├── manifest.json
├── models.py             # ActionPolicy, KnownFace, etc.
├── openai_client.py      # OpenAI Realtime implementation
├── security.py           # Validator, rate limiting, audit
├── services.yaml
├── session_manager.py    # Orchestrator + auto-stop
├── strings.json
├── tools.py              # Dual-format tool declarations
└── translations/en.json
```

---

## License

MIT
