# 🔔 Doorbell Jeeves

**Production-ready multimodal AI concierge** for smart doorbells — entity-centric design, multi-provider (Gemini + OpenAI + local models), per-action security policies, and native Reolink doorbell support with automatic go2rtc configuration.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🎙️ Real-time speech | Gemini Live or OpenAI Realtime API (native audio) |
| 🌐 Multi-provider | Google Gemini, OpenAI cloud, or any OpenAI-compatible local model |
| 🧠 Dual-model | Separate voice model + tool-calling model for best of both worlds |
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
| 📷 On-demand cameras | AI can view any camera to answer visitor questions |
| 📅 Calendar access | AI can check schedules for availability questions |
| 📜 Event history | AI can search recent motion/detection events |
| 🕒 LLM Vision timeline | Optional `llmvision.get_events` tool for recent detections |
| 🧾 Memory timeline card | Scrollable custom dashboard card with summaries + images |
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

## 🧠 Dual-Model Architecture

**Problem:** Native audio models like `gemini-2.5-flash-native-audio-dialog` do NOT support function calling. They provide excellent voice quality but cannot execute tools.

**Solution:** Enable **Dual Model Mode** to use two models simultaneously:

| Role | Model | Purpose |
|------|-------|---------|
| **Voice Model** | `gemini-2.5-flash-native-audio-dialog` | Handles real-time speech conversation |
| **Tool Model** | `gemini-2.5-flash` or `gpt-4.1` | Monitors transcript, executes tool calls |

### How it works
1. The **voice model** handles all audio I/O (speech-to-speech) with no tools declared
2. The voice model is instructed to verbally state its intentions ("Let me turn on the porch light")
3. A **tool router** monitors the conversation transcript in real-time
4. When the transcript indicates an action is needed, the **tool model** decides which tools to call
5. Tool results are injected back into the voice session as text context
6. The voice model naturally responds with the results

### Configuration
- Go to **Options → Dual Model** in the integration settings
- Enable "Dual Model Mode"
- Select the tool model provider and model name
- Optionally use a different API key for the tool model (leave blank to reuse the voice model key)

### When to use
- ✅ **Enable** if your voice model is `gemini-2.5-flash-native-audio-dialog` (no native tool support)
- ❌ **Disable** if using `gpt-4o-realtime-preview` or `gemini-2.0-flash-live-001` (these support tools natively)

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

## 🖐️ Human Takeover Detection

The AI agent automatically stops when a human takes over the conversation:

### Detection Methods

| Method | How it works | When to use |
|--------|-------------|-------------|
| **Reolink API** | Polls the camera's `GetTalkState` endpoint every 2s | Reolink users — zero config needed |
| **Audio Energy** | Monitors doorbell mic for loud audio while AI speaks | Backup for any camera type |
| **HA Entity Trigger** | Watches for entity state changes (e.g., door opens) | Any setup — configure in Triggers |

### Reolink Talk State Monitor
When you use Reolink Quick Setup, the integration automatically monitors whether someone activated 2-way audio via the Reolink mobile app. When detected:
1. AI playback stops immediately
2. Session ends gracefully
3. The human can speak uninterrupted

### Stop Triggers (HA Entities)
You can configure any HA entity as a stop trigger (e.g., `binary_sensor.front_door`). When it changes to the specified state, the AI session ends. Configure via **Options → Triggers**.

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
| **Timeline Integration** | LLM Vision event tool settings (lookback, filters, limits) |
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
| `ha_doorbell_jeeves_memory` | Session memory recap is stored |

---

## 🧩 Dashboard Memory Cards

Doorbell Jeeves now exposes memory entities you can add directly to the dashboard:

- **Memories** (`sensor`) — total stored memory count
- **Latest Memory Summary** (`sensor`) — newest recap summary
- **Memory Feed** (`sensor`) — all saved memories with image URLs + prebuilt markdown feed
- **Latest Memory Image** (`camera`) — newest saved snapshot image

### Recommended dashboard setup
1. Add a **Tile card** for **Latest Memory Summary**.
2. Add a **Tile card** for **Memories** (optional).
3. Add a **Picture Entity card** for **Latest Memory Image** to display the snapshot.

### All-in-one scrollable memory card (summaries + images)
Use one **Markdown card** with the `Memory Feed` sensor:

```yaml
type: markdown
title: Doorbell Memory Feed
content: >
  {{ state_attr('sensor.YOUR_MEMORY_FEED_ENTITY', 'dashboard_markdown') }}
```

Replace `sensor.YOUR_MEMORY_FEED_ENTITY` with your actual `Memory Feed` entity.

### Jeeves Memory Timeline card (LLM timeline-style)
The integration also serves a custom Lovelace card resource at:

```
/ha_doorbell_jeeves/jeeves-memory-timeline-card.js
```

1. Add this dashboard resource (**Settings → Dashboards → Resources**):

```yaml
url: /ha_doorbell_jeeves/jeeves-memory-timeline-card.js
type: module
```

2. Add the custom card:

```yaml
type: custom:jeeves-memory-timeline-card
entity: sensor.YOUR_MEMORY_FEED_ENTITY
title: Doorbell Memories
max_items: 100
show_images: true
relative_time: true
height: 70vh
```

The card renders a single scrollable timeline of saved memory summaries and images.

If you run multiple Jeeves instances, each entry creates its own set of memory entities.

## 🕒 LLM Vision Timeline Integration

If you use the [LLM Vision integration](https://github.com/valentinfrlch/ha-llmvision), Jeeves can query recent timeline events through a dedicated tool.

### Setup
1. Install and configure LLM Vision so `llmvision.get_events` is available.
2. Open **Doorbell Jeeves → Configure → Timeline Integration**.
3. Enable **LLM Vision Timeline Tool** and set defaults:
   - lookback window (hours),
   - max events per query,
   - optional camera/category filters.

Jeeves supports both LLM Vision modes:
- Native mode (`llmvision.get_events` service available)
- Compatibility mode (service missing but LLM Vision timeline backend is installed)

When enabled, Jeeves can answer questions like: "Did you recently see a football in the garden?"

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
- `ffmpeg` available in the Home Assistant runtime (required for Reolink audio pipelines)

---

## 📁 Project Structure

```
custom_components/ha_doorbell_jeeves/
├── __init__.py           # Entry point, service registration
├── camera.py             # Latest memory snapshot camera entity
├── client_base.py        # Abstract client protocol
├── config_flow.py        # Setup wizard + options flow (menu-driven)
├── const.py              # Constants, providers, modes
├── frame_processor.py    # Pillow-based downscaling
├── frontend/
│   └── jeeves-memory-timeline-card.js  # Custom Lovelace memory timeline card
├── gemini_client.py      # Gemini Live API implementation
├── manifest.json         # Integration metadata
├── memory_views.py       # HTTP endpoints for stored memory images
├── models.py             # ManagedEntity, EntityAction, KnownIdentity
├── openai_client.py      # OpenAI Realtime implementation
├── reolink_audio.py      # Reolink go2rtc 2-way audio handler
├── sensor.py             # Memory summary/feed dashboard sensors
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
