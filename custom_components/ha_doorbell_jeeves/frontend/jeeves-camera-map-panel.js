/**
 * Jeeves Camera Map Panel
 * Interactive visual editor for placing cameras around a property.
 * - Free placement anywhere on the map (drag to position)
 * - Rotation handle shows FOV direction triangle
 * - Click camera to edit settings (PTZ controls, area description)
 * - Auto-saves via WebSocket API
 *
 * Usage: type: custom:jeeves-camera-map-panel
 */

class JeevesCameraMapPanel extends HTMLElement {
  static getConfigElement() { return null; }
  static getStubConfig(hass) { return { title: "Camera Map" }; }

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("Invalid configuration");
    }
    this._config = { title: "Camera Map", ...config };
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }
  }

  getCardSize() { return 8; }

  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) {
      this._initialized = true;
      this._placements = [];
      this._availableCameras = [];
      this._dragging = null;
      this._rotating = null;
      this._selected = null;
      this._load();
    }
  }

  async _load() {
    try {
      const [placementsResult, camerasResult] = await Promise.all([
        this._hass.callWS({ type: "ha_doorbell_jeeves/camera_placements/list" }),
        this._hass.callWS({ type: "ha_doorbell_jeeves/camera_placements/cameras" }),
      ]);
      this._placements = (placementsResult.placements || []).map(p => ({
        ...p,
        x: p.x != null ? p.x : 0.5,
        y: p.y != null ? p.y : 0.5,
        rotation: p.rotation != null ? p.rotation : 0,
      }));
      this._availableCameras = camerasResult.cameras || [];
      this._render();
    } catch (e) {
      console.error("[Jeeves] Failed to load camera placements:", e);
      this.shadowRoot.innerHTML = `<ha-card><div style="padding:20px;color:var(--error-color)">Error: ${e.message}</div></ha-card>`;
    }
  }

  async _save() {
    try {
      await this._hass.callWS({
        type: "ha_doorbell_jeeves/camera_placements/save",
        placements: this._placements,
      });
    } catch (e) {
      console.error("[Jeeves] Save failed:", e);
    }
  }

  _render() {
    const unplaced = this._availableCameras.filter(
      c => !this._placements.some(p => p.entity_id === c.entity_id)
    );

    this.shadowRoot.innerHTML = `
      ${this._styles()}
      <ha-card>
        <div class="header">
          <ha-icon icon="mdi:floor-plan"></ha-icon>
          <span>${this._config.title || "Camera Map"}</span>
        </div>
        <div class="content">
          <div class="map-area" id="map">
            <div class="house-box">
              <span class="house-label">HOUSE</span>
            </div>
            ${this._placements.map((p, i) => this._renderCamera(p, i)).join("")}
          </div>
          ${unplaced.length > 0 ? `
            <div class="sidebar">
              <div class="sidebar-title">Drag to place:</div>
              ${unplaced.map(c => `
                <div class="unplaced-cam" draggable="true" data-entity="${c.entity_id}" data-name="${c.name}">
                  <ha-icon icon="mdi:cctv"></ha-icon>
                  <span>${c.name}</span>
                </div>
              `).join("")}
            </div>
          ` : ""}
        </div>
        ${this._selected != null ? this._renderEditPanel() : ""}
      </ha-card>
    `;
    this._bindEvents();
  }

  _renderCamera(p, idx) {
    const selected = this._selected === idx;
    const rot = p.rotation || 0;
    // FOV triangle points (50px long, 60deg wide)
    const fovLen = 40;
    const fovAngle = 30; // half-angle in degrees
    const rad = rot * Math.PI / 180;
    const lRad = (rot - fovAngle) * Math.PI / 180;
    const rRad = (rot + fovAngle) * Math.PI / 180;
    const tip1x = Math.sin(lRad) * fovLen;
    const tip1y = -Math.cos(lRad) * fovLen;
    const tip2x = Math.sin(rRad) * fovLen;
    const tip2y = -Math.cos(rRad) * fovLen;

    return `
      <div class="placed-cam ${selected ? "selected" : ""}" data-idx="${idx}"
           style="left:${p.x * 100}%;top:${p.y * 100}%">
        <svg class="fov-triangle" width="100" height="100" viewBox="-50 -50 100 100">
          <polygon points="0,0 ${tip1x},${tip1y} ${tip2x},${tip2y}"
                   fill="var(--primary-color)" opacity="0.25" stroke="var(--primary-color)" stroke-width="1"/>
        </svg>
        <div class="cam-icon">
          <ha-icon icon="${p.is_doorbell ? "mdi:doorbell-video" : "mdi:cctv"}"></ha-icon>
        </div>
        <div class="cam-label">${p.name}</div>
        <div class="rotate-handle" data-idx="${idx}" title="Drag to rotate">
          <ha-icon icon="mdi:rotate-right"></ha-icon>
        </div>
      </div>
    `;
  }

  _renderEditPanel() {
    const p = this._placements[this._selected];
    if (!p) return "";
    return `
      <div class="edit-panel">
        <div class="edit-header">
          <span>${p.name}</span>
          <button class="close-btn" id="close-edit">&times;</button>
        </div>
        <div class="edit-body">
          <label>Area Description</label>
          <textarea id="edit-area" rows="2" placeholder="e.g. Front garden, driveway">${p.area_description || ""}</textarea>
          <label>Is Doorbell</label>
          <input type="checkbox" id="edit-doorbell" ${p.is_doorbell ? "checked" : ""}>
          <label>Rotation: <span id="rot-val">${Math.round(p.rotation || 0)}°</span></label>
          <input type="range" id="edit-rotation" min="0" max="360" value="${p.rotation || 0}">
          <div class="ptz-section">
            <label class="section-label">PTZ Controls (entity IDs)</label>
            <input type="text" id="ptz-up" placeholder="PTZ Up entity" value="${p.ptz_up || ""}">
            <input type="text" id="ptz-down" placeholder="PTZ Down entity" value="${p.ptz_down || ""}">
            <input type="text" id="ptz-left" placeholder="PTZ Left entity" value="${p.ptz_left || ""}">
            <input type="text" id="ptz-right" placeholder="PTZ Right entity" value="${p.ptz_right || ""}">
            <input type="text" id="ptz-return" placeholder="PTZ Return to monitor" value="${p.ptz_return_to_monitor || ""}">
          </div>
          <button class="save-btn" id="save-edit">Save</button>
          <button class="remove-btn" id="remove-cam">Remove Camera</button>
        </div>
      </div>
    `;
  }

  _bindEvents() {
    const map = this.shadowRoot.getElementById("map");
    if (!map) return;

    // Drag unplaced cameras onto map
    this.shadowRoot.querySelectorAll(".unplaced-cam").forEach(el => {
      el.addEventListener("dragstart", e => {
        e.dataTransfer.setData("text/plain", el.dataset.entity);
      });
    });

    map.addEventListener("dragover", e => e.preventDefault());
    map.addEventListener("drop", e => {
      e.preventDefault();
      const entityId = e.dataTransfer.getData("text/plain");
      if (!entityId) return;
      const rect = map.getBoundingClientRect();
      const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      const y = Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height));
      const cam = this._availableCameras.find(c => c.entity_id === entityId);
      this._placements.push({
        entity_id: entityId,
        name: cam ? cam.name : entityId,
        x, y, rotation: 0,
        area_description: cam ? cam.description : "",
        is_doorbell: false,
        ptz_up: "", ptz_down: "", ptz_left: "", ptz_right: "", ptz_return_to_monitor: "",
      });
      this._save();
      this._render();
    });

    // Drag placed cameras to reposition
    this.shadowRoot.querySelectorAll(".placed-cam").forEach(el => {
      const idx = parseInt(el.dataset.idx);
      let startX, startY, origX, origY;

      const onMouseDown = (e) => {
        if (e.target.closest(".rotate-handle")) return; // handle rotation separately
        e.preventDefault();
        const rect = map.getBoundingClientRect();
        startX = e.clientX;
        startY = e.clientY;
        origX = this._placements[idx].x;
        origY = this._placements[idx].y;
        const onMove = (ev) => {
          const dx = (ev.clientX - startX) / rect.width;
          const dy = (ev.clientY - startY) / rect.height;
          this._placements[idx].x = Math.max(0, Math.min(1, origX + dx));
          this._placements[idx].y = Math.max(0, Math.min(1, origY + dy));
          el.style.left = (this._placements[idx].x * 100) + "%";
          el.style.top = (this._placements[idx].y * 100) + "%";
        };
        const onUp = () => {
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
          this._save();
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      };
      el.querySelector(".cam-icon").addEventListener("mousedown", onMouseDown);

      // Click to select/edit
      el.querySelector(".cam-icon").addEventListener("click", (e) => {
        if (Math.abs(e.clientX - startX) < 5 && Math.abs(e.clientY - startY) < 5) {
          this._selected = this._selected === idx ? null : idx;
          this._render();
        }
      });
    });

    // Rotation handles
    this.shadowRoot.querySelectorAll(".rotate-handle").forEach(handle => {
      const idx = parseInt(handle.dataset.idx);
      handle.addEventListener("mousedown", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const el = handle.closest(".placed-cam");
        const rect = el.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;

        const onMove = (ev) => {
          const angle = Math.atan2(ev.clientX - cx, -(ev.clientY - cy)) * 180 / Math.PI;
          this._placements[idx].rotation = (angle + 360) % 360;
          this._render();
        };
        const onUp = () => {
          document.removeEventListener("mousemove", onMove);
          document.removeEventListener("mouseup", onUp);
          this._save();
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp);
      });
    });

    // Edit panel events
    const closeBtn = this.shadowRoot.getElementById("close-edit");
    if (closeBtn) closeBtn.addEventListener("click", () => { this._selected = null; this._render(); });

    const saveBtn = this.shadowRoot.getElementById("save-edit");
    if (saveBtn) {
      saveBtn.addEventListener("click", () => {
        const p = this._placements[this._selected];
        p.area_description = this.shadowRoot.getElementById("edit-area").value;
        p.is_doorbell = this.shadowRoot.getElementById("edit-doorbell").checked;
        p.rotation = parseFloat(this.shadowRoot.getElementById("edit-rotation").value);
        p.ptz_up = this.shadowRoot.getElementById("ptz-up").value.trim();
        p.ptz_down = this.shadowRoot.getElementById("ptz-down").value.trim();
        p.ptz_left = this.shadowRoot.getElementById("ptz-left").value.trim();
        p.ptz_right = this.shadowRoot.getElementById("ptz-right").value.trim();
        p.ptz_return_to_monitor = this.shadowRoot.getElementById("ptz-return").value.trim();
        this._save();
        this._render();
      });
    }

    const removeBtn = this.shadowRoot.getElementById("remove-cam");
    if (removeBtn) {
      removeBtn.addEventListener("click", () => {
        this._placements.splice(this._selected, 1);
        this._selected = null;
        this._save();
        this._render();
      });
    }

    // Rotation slider live update
    const rotSlider = this.shadowRoot.getElementById("edit-rotation");
    if (rotSlider) {
      rotSlider.addEventListener("input", (e) => {
        const val = parseFloat(e.target.value);
        this.shadowRoot.getElementById("rot-val").textContent = Math.round(val) + "°";
        this._placements[this._selected].rotation = val;
        // Re-render FOV triangle
        const camEl = this.shadowRoot.querySelector(`.placed-cam[data-idx="${this._selected}"]`);
        if (camEl) {
          const fovLen = 40, fovAngle = 30;
          const rad = val * Math.PI / 180;
          const lRad = (val - fovAngle) * Math.PI / 180;
          const rRad = (val + fovAngle) * Math.PI / 180;
          const svg = camEl.querySelector(".fov-triangle polygon");
          if (svg) svg.setAttribute("points", `0,0 ${Math.sin(lRad)*fovLen},${-Math.cos(lRad)*fovLen} ${Math.sin(rRad)*fovLen},${-Math.cos(rRad)*fovLen}`);
        }
      });
    }
  }

  _styles() {
    return `<style>
      :host { display: block; }
      ha-card { overflow: hidden; }
      .header { display: flex; align-items: center; gap: 8px; padding: 16px 16px 8px; font-size: 1.1rem; font-weight: 500; }
      .content { display: flex; gap: 12px; padding: 8px 16px 16px; }
      .map-area {
        position: relative;
        flex: 1;
        min-height: 350px;
        background: var(--secondary-background-color);
        border-radius: 12px;
        border: 2px dashed var(--divider-color);
        overflow: hidden;
      }
      .house-box {
        position: absolute;
        left: 25%; top: 25%; width: 50%; height: 50%;
        border: 3px solid var(--primary-color);
        border-radius: 8px;
        opacity: 0.5;
        display: flex; align-items: center; justify-content: center;
      }
      .house-label { font-size: 0.9rem; font-weight: 700; opacity: 0.5; color: var(--primary-color); text-transform: uppercase; letter-spacing: 2px; }
      .placed-cam {
        position: absolute;
        transform: translate(-50%, -50%);
        cursor: grab;
        z-index: 10;
      }
      .placed-cam.selected .cam-icon { box-shadow: 0 0 0 3px var(--primary-color); }
      .cam-icon {
        width: 36px; height: 36px;
        background: var(--card-background-color);
        border: 2px solid var(--primary-color);
        border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        position: relative; z-index: 2;
        --mdc-icon-size: 20px;
      }
      .cam-label {
        position: absolute; top: 38px; left: 50%; transform: translateX(-50%);
        font-size: 0.7rem; font-weight: 600; white-space: nowrap;
        background: var(--card-background-color); padding: 1px 5px; border-radius: 4px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.2); z-index: 2;
      }
      .fov-triangle {
        position: absolute;
        top: 50%; left: 50%;
        transform: translate(-50%, -50%);
        pointer-events: none;
        z-index: 1;
      }
      .rotate-handle {
        position: absolute; top: -20px; left: 50%; transform: translateX(-50%);
        width: 20px; height: 20px;
        background: var(--primary-color); border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        cursor: grab; z-index: 3;
        --mdc-icon-size: 14px; color: white;
        opacity: 0.7; transition: opacity 0.2s;
      }
      .rotate-handle:hover { opacity: 1; }
      .sidebar {
        width: 140px; flex-shrink: 0;
        background: var(--secondary-background-color);
        border-radius: 12px; padding: 12px;
      }
      .sidebar-title { font-size: 0.8rem; font-weight: 600; margin-bottom: 8px; opacity: 0.7; }
      .unplaced-cam {
        display: flex; align-items: center; gap: 6px;
        padding: 8px; margin-bottom: 6px;
        background: var(--card-background-color);
        border-radius: 8px; cursor: grab;
        font-size: 0.8rem; border: 1px solid var(--divider-color);
        --mdc-icon-size: 16px;
      }
      .unplaced-cam:hover { border-color: var(--primary-color); }
      .edit-panel {
        border-top: 1px solid var(--divider-color);
        padding: 16px;
      }
      .edit-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; font-weight: 600; }
      .close-btn { background: none; border: none; font-size: 1.4rem; cursor: pointer; color: var(--primary-text-color); }
      .edit-body label { display: block; font-size: 0.8rem; font-weight: 500; margin: 8px 0 4px; color: var(--secondary-text-color); }
      .edit-body textarea, .edit-body input[type="text"] {
        width: 100%; padding: 6px 8px; border: 1px solid var(--divider-color);
        border-radius: 6px; font-size: 0.85rem; box-sizing: border-box;
        background: var(--card-background-color); color: var(--primary-text-color);
      }
      .edit-body input[type="range"] { width: 100%; }
      .ptz-section { margin-top: 12px; }
      .section-label { font-size: 0.75rem; text-transform: uppercase; color: var(--primary-color); font-weight: 700; }
      .ptz-section input { margin-bottom: 4px; }
      .save-btn {
        display: block; width: 100%; padding: 8px; margin-top: 12px;
        background: var(--primary-color); color: white; border: none;
        border-radius: 8px; font-size: 0.85rem; cursor: pointer;
      }
      .save-btn:hover { opacity: 0.9; }
      .remove-btn {
        display: block; width: 100%; padding: 8px; margin-top: 6px;
        background: var(--error-color); color: white; border: none;
        border-radius: 8px; font-size: 0.85rem; cursor: pointer;
      }
      .remove-btn:hover { opacity: 0.9; }
    </style>`;
  }
}

if (!customElements.get("jeeves-camera-map-panel")) {
  customElements.define("jeeves-camera-map-panel", JeevesCameraMapPanel);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "jeeves-camera-map-panel")) {
  window.customCards.push({
    type: "jeeves-camera-map-panel",
    name: "Jeeves Camera Map",
    description: "Interactive visual camera placement editor with free positioning and rotation.",
    preview: false,
    documentationURL: "https://github.com/gomme600/HA-Doorbell-Jeeves",
  });
}
