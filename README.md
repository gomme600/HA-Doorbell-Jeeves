# 🔔 Doorbell Jeeves

**Production-ready multimodal AI concierge** for smart doorbells — entity-centric design, multi-provider (Gemini + OpenAI + local models), per-action security policies, and native Reolink doorbell support with automatic go2rtc configuration.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🎙️ Real-time speech | Gemini Live or OpenAI Realtime API (native audio) |
| 🌐 Multi-provider | Google Gemini, OpenAI cloud, or any OpenAI-compatible local model |
| 📷 Configurable vision | 0.1–10 FPS with automatic frame downscaling |
| 🖼️ Frame downscaling | Pillow-based resize before sending — faster responses |
| 👤 Identity (sensor) | Frigate / CompreFace / Double Take integration |
| 📸 Identity (photos) | Reference images of people, animals & objects |
| 🏠 Custom actions | Admin-defined tools per entity (not predefined) |
| 🔒 Entity allowlisting | AI can ONLY access admin-approved entities |
| 🛡️ Per-action security | Each action: auto / validated / PIN / PIN+validated |
| 🤖 Validator agent | Independent AI verifies sensitive actions |
| ⚡ Start triggers | Auto-start on doorbell press (built-in) |
| ⏹️ Stop triggers | Auto-stop on state change, events, or human takeover |
| 📱 Reolink Quick Setup | Auto-configures go2rtc 2-way audio |
| 🔔 Notifications | AI can notify admin via configured targets |
| ⏱️ Session timeout | Configurable auto-hangup (default 120s) |
| 🔍 Audit trail | Full session logging per action |
| ⚙️ GUI config | No YAML required — full menu-driven options flow |

---

## 🌐 Supported Providers

| Provider | Models | Use Case |
|----------|--------|----------|
| **Google Gemini** | `gemini-2.5-flash-native-audio-dialog` | Best native audio quality |
| **OpenAI** | `gpt-4o-realtime-preview` | Alternative cloud provider |
| **Local (OpenAI-compatible)** | Any model via custom base URL | Privacy, offline, self-hosted |

For local models, select "OpenAI / Compatible" and set the base URL to your server (e.g., `http://192.168.1.100:8080/v1`). Compatible with LocalAI, vLLM, Ollama (with OpenAI adapter), and any server implementing the OpenAI Realtime API.

---

## 📱 Reolink Doorbell Quick Setup

The integration includes a dedicated **Reolink Quick Setup** mode that:

1. **Auto-detects** your Reolink doorbell from the existing HA Reolink integration
2. **Auto-configures** a go2rtc stream with backchannel support for 2-way audio
3. **Auto-discovers** the doorbell button sensor for start triggers
4. **Zero manual RTSP configuration** — credentials are read from the Reolink config entry

### How it works
- **Audio IN** (visitor → AI): Audio is extracted from the go2rtc RTSP stream via WebSocket
- **Audio OUT** (AI → speaker): PCM audio is sent via go2rtc's backchannel API
- **Video**: Camera snapshots are taken via HA's camera component at your configured FPS

### Prerequisites for Reolink mode
- Reolink integration already set up and loaded in HA
- go2rtc available (built into HA Core since 2023.7)
- Camera entity visible (e.g., `camera.reolink_video_doorbell_poe_fluent`)

### Manual Setup (non-Reolink)
For other doorbells, choose "Manual Setup" and configure:
- Camera entity (any HA camera)
- Audio output: Media Player (HA speaker) or Event (custom handling)

---

## ⚙️ Setup Wizard

### Initial Setup (5 steps)
1. **Setup Mode** — Reolink Quick Setup or Manual
2. **Reolink** *(if selected)* — Select your doorbell camera
3. **AI Provider** — Gemini/OpenAI/local, API key, model, voice
4. **Camera & Vision** — FPS, frame resolution, JPEG quality
5. **System Prompt** — Define AI personality and rules

### Post-Setup (Options Flow Menu)
After initial setup, configure via **Settings → Integrations → Configure**:

| Section | What you configure |
|---------|-------------------|
| **General** | AI provider, model, voice, timeout, validator model |
| **Vision** | Camera entity, FPS, resolution, quality |
| **Entities & Actions** | Add/remove managed entities, custom actions, notifications |
| **Security** | Default security mode, PIN code, validator model |
| **Triggers** | Start triggers (doorbell press), stop triggers (door opens) |
| **Identities** | Known people/animals with descriptions & reference images |
| **Prompt** | Edit the system prompt |

---

## 🏠 Entity-Centric Architecture

Unlike traditional integrations with predefined tools, Jeeves uses an **admin-defined entity model**:

### Adding Entities
Each managed entity has:
- **Name** — How the AI refers to it (e.g., "Porch Light")
- **Description** — What it does (sent to AI for context)
- **Security mode** — Default policy for this entity

### Adding Actions
Each action on an entity has:
- **Action ID** — Unique slug (e.g., `unlock_gate`)
- **Service** — HA service to call (e.g., `lock.unlock`)
- **Security mode** — Per-action override (auto/validated/PIN/PIN+validated)
- **Visual match** — Require camera identity verification
- **Camera feed** — Send frame to validator
- **Rate limits** — Max uses per session, cooldown between uses
- **Validator prompt** — Custom instructions for the security AI

### Example Configuration
```
Entity: lock.front_gate
  Name: "Front Gate"
  Description: "The main entrance gate"
  Actions:
    - unlock_gate: lock.unlock (security: pin_and_validated, visual_match: true, max: 1/session)
    - lock_gate: lock.lock (security: auto)

Entity: light.porch
  Name: "Porch Light"
  Description: "Outdoor light next to doorbell"
  Actions:
    - toggle_porch: light.toggle (security: auto)

Entity: sensor.weather_temperature
  Name: "Outdoor Temperature"
  Description: "Current temperature sensor"
  (read-only — no actions)
```

---

## 🔒 Security Model

### Per-Action Security Modes

| Mode | Behavior |
|------|----------|
| `auto` | Execute immediately (no extra checks) |
| `validated` | Security validator AI must approve |
| `pin` | Visitor must speak correct PIN |
| `pin_and_validated` | Both PIN + validator approval required |

### Validator Agent
- **Independent** API call (separate model instance, not the conversation AI)
- **Immutable** system prompt (hardcoded — cannot be overridden by visitors)
- **Configurable per-action**: receives camera frame, reference photo, custom instructions
- **Confidence threshold**: ≥80% required, auto-reject below
- **Threat detection**: Reports indicators (coercion, social engineering)

### Anti-Tamper Layers
1. **Tool enum constraints** — model only sees allowed entity IDs
2. **Server-side allowlist** — rejects even hallucinated entities
3. **Per-action rate limiting** and cooldowns
4. **Independent validator agent** — separate AI with immutable rules
5. **PIN protection** — verbal PIN code verification
6. **Visual match** — camera image compared to reference photos
7. **Prompt hardening** — security directives marked as immutable
8. **Audit trail** — every action logged with timestamps

---

## 👤 Known Identities

Add known people, animals, or objects with:
- **Name** — How to greet them
- **Type** — Person, animal, or object/vehicle
- **Relationship** — Owner, family, friend, pet, delivery, guest
- **Description** — Physical appearance for visual matching
- **Access level** — Full, limited, guest, or blocked
- **Reference image** — URL, local path, or base64 data

The AI uses descriptions + reference images to identify visitors and adjust behavior accordingly.

---

## 🤖 Automation Examples

### Using Built-in Start Triggers (Recommended)
Configure start triggers in the options flow — no automation needed:
- Set `binary_sensor.reolink_video_doorbell_poe_visitor` as a start trigger
- Jeeves automatically activates when the doorbell is pressed

### Manual Automation
```yaml
automation:
  - alias: "Jeeves - Answer Doorbell"
    trigger:
      - platform: state
        entity_id: binary_sensor.doorbell_button
        to: "on"
    action:
      - service: ha_doorbell_jeeves.start_session

  - alias: "Jeeves - Stop on Door Open"
    trigger:
      - platform: state
        entity_id: binary_sensor.front_door
        to: "on"
    action:
      - service: ha_doorbell_jeeves.stop_session

  - alias: "Jeeves - Security Alert → Phone"
    trigger:
      - platform: event
        event_type: ha_doorbell_jeeves_security_alert
    action:
      - service: notify.mobile_app
        data:
          title: "🚨 Doorbell Security Alert"
          message: "{{ trigger.event.data.reasoning }}"
```

---

## 🔊 Events

| Event | Fired when |
|-------|-----------|
| `ha_doorbell_jeeves_session_started` | AI session begins |
| `ha_doorbell_jeeves_session_ended` | AI session ends |
| `ha_doorbell_jeeves_audio_output` | AI generates speech (contains audio_base64) |
| `ha_doorbell_jeeves_tool_call` | AI attempts any action |
| `ha_doorbell_jeeves_action_blocked` | Action rejected by security |
| `ha_doorbell_jeeves_security_alert` | Threat indicators detected |
| `ha_doorbell_jeeves_validator_decision` | Validator AI decision logged |

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

1. Download or clone this repo.
2. Copy `custom_components/ha_doorbell_jeeves/` to your HA `config/custom_components/`:
   ```bash
   cp -r custom_components/ha_doorbell_jeeves /path/to/ha/config/custom_components/
   ```
3. Restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration → Doorbell Jeeves**.

### Requirements
- Home Assistant 2024.12+
- Python 3.12+
- Gemini API key, OpenAI API key, or local model server
- Camera entity (Reolink recommended, or any HA camera)

---

## 📁 Project Structure

```
custom_components/ha_doorbell_jeeves/
├── __init__.py           # Entry point, service registration
├── client_base.py        # Abstract client protocol
├── config_flow.py        # Setup wizard + options flow (menu-driven)
├── const.py              # Constants, providers, modes
├── frame_processor.py    # Pillow-based downscaling
├── gemini_client.py      # Gemini Live API implementation
├── manifest.json         # Integration metadata
├── models.py             # ManagedEntity, EntityAction, KnownIdentity
├── openai_client.py      # OpenAI Realtime implementation
├── reolink_audio.py      # Reolink go2rtc 2-way audio handler
├── security.py           # Validator, rate limiting, audit
├── services.yaml         # Service definitions
├── session_manager.py    # Session orchestrator + triggers
├── store.py              # Persistent storage (HA .storage)
├── strings.json          # UI strings
├── tools.py              # Dynamic tool generation + execution
└── translations/en.json  # English translations
```

---

## 📄 Services

| Service | Description |
|---------|-------------|
| `ha_doorbell_jeeves.start_session` | Start the AI session |
| `ha_doorbell_jeeves.stop_session` | Stop the active session |
| `ha_doorbell_jeeves.send_audio` | Send PCM audio to the AI |
| `ha_doorbell_jeeves.add_entity` | Add a managed entity |
| `ha_doorbell_jeeves.remove_entity` | Remove a managed entity |
| `ha_doorbell_jeeves.add_action` | Add an action to an entity |
| `ha_doorbell_jeeves.remove_action` | Remove an action |
| `ha_doorbell_jeeves.add_identity` | Add a known identity |
| `ha_doorbell_jeeves.remove_identity` | Remove a known identity |

---

## License

MIT
