/**
 * Jeeves Camera Map Panel
 * Interactive visual editor for placing cameras around a house outline.
 * Cameras are dragged onto the perimeter of a house rectangle.
 * Communicates with HA via WebSocket API to load/save placements.
 *
 * Usage: Add as a custom Lovelace card:
 *   type: custom:jeeves-camera-map-panel
 */

class JeevesCameraMapPanel extends HTMLElement {
  static getConfigElement() { return null; }
  static getStubConfig() { return {}; }

  setConfig(config) {
    if (!config || typeof config !== "object") {
      throw new Error("Invalid configuration");
    }
    this._config = config;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }
  }

  getCardSize() { return 6; }

  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) {
      this._initialized = true;
      this._placements = [];
      this._availableCameras = [];
      this._dragging = null;
      this._load();
    }
  }

  async _load() {
    try {
      const [placementsResult, camerasResult] = await Promise.all([
        this._hass.callWS({ type: "ha_doorbell_jeeves/camera_placements/list" }),
        this._hass.callWS({ type: "ha_doorbell_jeeves/camera_placements/cameras" }),
      ]);
      this._placements = placementsResult.placements || [];
      this._availableCameras = camerasResult.cameras || [];
      this._render();
    } catch (e) {
      console.error("Failed to load camera placements:", e);
      this._renderError(e.message || "Failed to load camera data");
    }
  }

  async _save() {
    try {
      await this._hass.callWS({
        type: "ha_doorbell_jeeves/camera_placements/save",
        placements: this._placements,
      });
      this._showToast("Camera placements saved!");
    } catch (e) {
      console.error("Failed to save:", e);
      this._showToast("Failed to save: " + e.message, true);
    }
  }

  _render() {
    const unplaced = this._availableCameras.filter(
      (c) => !this._placements.some((p) => p.entity_id === c.entity_id)
    );

    this.shadowRoot.innerHTML = `
      ${this._styles()}
      <ha-card>
        <div class="card-header">
          <ha-icon icon="mdi:floor-plan"></ha-icon>
          <span>Camera Placement Map</span>
        </div>
        <div class="map-container">
          <div class="instructions">
            Drag cameras from the sidebar onto the house perimeter to place them.
            Click a placed camera to edit its settings. Changes save automatically.
          </div>
          <div class="layout">
            <div class="sidebar">
              <div class="sidebar-title">Available Cameras</div>
              ${unplaced.length === 0 ? '<div class="no-cameras">All cameras placed ✓</div>' : ""}
              ${unplaced.map((c) => `
                <div class="camera-chip unplaced" draggable="true" data-entity="${c.entity_id}">
                  <ha-icon icon="mdi:camera"></ha-icon>
                  <span>${this._esc(c.name)}</span>
                </div>
              `).join("")}
              ${this._placements.length > 0 ? '<div class="sidebar-title" style="margin-top:16px">Placed Cameras</div>' : ""}
              ${this._placements.map((p) => `
                <div class="camera-chip placed" data-entity="${p.entity_id}">
                  <ha-icon icon="${p.is_doorbell ? "mdi:doorbell-video" : "mdi:cctv"}"></ha-icon>
                  <span>${this._esc(p.name || p.entity_id)}</span>
                  <button class="remove-btn" data-entity="${p.entity_id}" title="Remove">×</button>
                </div>
              `).join("")}
            </div>
            <div class="house-area">
              <div class="compass">
                <span class="compass-n">N</span>
                <span class="compass-s">S</span>
                <span class="compass-e">E</span>
                <span class="compass-w">W</span>
              </div>
              <div class="house" id="house">
                <div class="house-label">HOUSE</div>
                ${this._placements.map((p) => this._renderPlacedCamera(p)).join("")}
              </div>
            </div>
          </div>
        </div>
        ${this._editingCamera ? this._renderEditPanel() : ""}
      </ha-card>
    `;

    this._attachEvents();
  }

  _renderPlacedCamera(placement) {
    const pos = this._getPosition(placement.side, placement.offset);
    const facingArrow = this._getFacingArrow(placement.side, placement.facing);
    const icon = placement.is_doorbell ? "mdi:doorbell-video" : "mdi:cctv";
    const ptzBadge = (placement.ptz_up || placement.ptz_down || placement.ptz_left || placement.ptz_right)
      ? '<span class="ptz-badge">PTZ</span>' : "";

    return `
      <div class="placed-camera" 
           style="left:${pos.x}%;top:${pos.y}%"
           data-entity="${placement.entity_id}"
           draggable="true"
           title="${this._esc(placement.name || placement.entity_id)}${placement.area_description ? '\nCovers: ' + placement.area_description : ''}">
        <div class="camera-icon">
          <ha-icon icon="${icon}"></ha-icon>
          ${ptzBadge}
        </div>
        <div class="facing-arrow">${facingArrow}</div>
        <div class="camera-label">${this._esc(placement.name || "").substring(0, 12)}</div>
      </div>
    `;
  }

  _renderEditPanel() {
    const p = this._editingCamera;
    return `
      <div class="edit-panel">
        <div class="edit-header">
          <span>Edit: ${this._esc(p.name || p.entity_id)}</span>
          <button class="close-edit">×</button>
        </div>
        <div class="edit-field">
          <label>Name</label>
          <input type="text" id="edit-name" value="${this._esc(p.name || "")}">
        </div>
        <div class="edit-field">
          <label>Side</label>
          <select id="edit-side">
            ${["north", "south", "east", "west"].map((s) => `<option value="${s}" ${p.side === s ? "selected" : ""}>${s.charAt(0).toUpperCase() + s.slice(1)}</option>`).join("")}
          </select>
        </div>
        <div class="edit-field">
          <label>Position along side</label>
          <input type="range" id="edit-offset" min="0" max="1" step="0.05" value="${p.offset || 0.5}">
        </div>
        <div class="edit-field">
          <label>Facing</label>
          <select id="edit-facing">
            <option value="away" ${p.facing === "away" ? "selected" : ""}>Away from house</option>
            <option value="along_left" ${p.facing === "along_left" ? "selected" : ""}>Along wall (left)</option>
            <option value="along_right" ${p.facing === "along_right" ? "selected" : ""}>Along wall (right)</option>
          </select>
        </div>
        <div class="edit-field">
          <label>Area description (what it covers)</label>
          <textarea id="edit-area" rows="2">${this._esc(p.area_description || "")}</textarea>
        </div>
        <div class="edit-field">
          <label><input type="checkbox" id="edit-doorbell" ${p.is_doorbell ? "checked" : ""}> This is the doorbell camera</label>
        </div>
        <div class="edit-section-title">PTZ Controls (optional)</div>
        <div class="edit-field">
          <label>PTZ Up entity</label>
          <input type="text" id="edit-ptz-up" value="${this._esc(p.ptz_up || "")}" placeholder="button.cam_ptz_up">
        </div>
        <div class="edit-field">
          <label>PTZ Down entity</label>
          <input type="text" id="edit-ptz-down" value="${this._esc(p.ptz_down || "")}" placeholder="button.cam_ptz_down">
        </div>
        <div class="edit-field">
          <label>PTZ Left entity</label>
          <input type="text" id="edit-ptz-left" value="${this._esc(p.ptz_left || "")}" placeholder="button.cam_ptz_left">
        </div>
        <div class="edit-field">
          <label>PTZ Right entity</label>
          <input type="text" id="edit-ptz-right" value="${this._esc(p.ptz_right || "")}" placeholder="button.cam_ptz_right">
        </div>
        <div class="edit-field">
          <label>PTZ Return to Monitor</label>
          <input type="text" id="edit-ptz-return" value="${this._esc(p.ptz_return_to_monitor || "")}" placeholder="button.cam_return_home">
        </div>
        <button class="save-btn" id="save-edit">Save Changes</button>
      </div>
    `;
  }

  _attachEvents() {
    const house = this.shadowRoot.getElementById("house");
    if (!house) return;

    // Drag from sidebar to house
    this.shadowRoot.querySelectorAll(".camera-chip.unplaced").forEach((chip) => {
      chip.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", chip.dataset.entity);
        this._dragging = { type: "new", entity_id: chip.dataset.entity };
      });
    });

    // Drag existing placed cameras
    this.shadowRoot.querySelectorAll(".placed-camera").forEach((el) => {
      el.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", el.dataset.entity);
        this._dragging = { type: "move", entity_id: el.dataset.entity };
      });
      el.addEventListener("click", (e) => {
        if (e.target.closest(".remove-btn")) return;
        this._editingCamera = this._placements.find((p) => p.entity_id === el.dataset.entity) || null;
        this._render();
      });
    });

    // Drop on house
    house.addEventListener("dragover", (e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; });
    house.addEventListener("drop", (e) => {
      e.preventDefault();
      if (!this._dragging) return;

      const rect = house.getBoundingClientRect();
      const x = (e.clientX - rect.left) / rect.width;
      const y = (e.clientY - rect.top) / rect.height;
      const { side, offset } = this._positionToSideOffset(x, y);

      if (this._dragging.type === "new") {
        const cam = this._availableCameras.find((c) => c.entity_id === this._dragging.entity_id);
        this._placements.push({
          entity_id: this._dragging.entity_id,
          name: cam ? cam.name : this._dragging.entity_id,
          side,
          offset,
          facing: "away",
          area_description: "",
          is_doorbell: false,
          ptz_up: "", ptz_down: "", ptz_left: "", ptz_right: "", ptz_return_to_monitor: "",
        });
      } else {
        const existing = this._placements.find((p) => p.entity_id === this._dragging.entity_id);
        if (existing) {
          existing.side = side;
          existing.offset = offset;
        }
      }
      this._dragging = null;
      this._save();
      this._render();
    });

    // Remove buttons
    this.shadowRoot.querySelectorAll(".remove-btn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const entityId = btn.dataset.entity;
        this._placements = this._placements.filter((p) => p.entity_id !== entityId);
        this._editingCamera = null;
        this._save();
        this._render();
      });
    });

    // Edit panel events
    const closeEdit = this.shadowRoot.querySelector(".close-edit");
    if (closeEdit) {
      closeEdit.addEventListener("click", () => { this._editingCamera = null; this._render(); });
    }
    const saveEdit = this.shadowRoot.getElementById("save-edit");
    if (saveEdit) {
      saveEdit.addEventListener("click", () => this._saveEdit());
    }
  }

  _saveEdit() {
    if (!this._editingCamera) return;
    const p = this._editingCamera;
    p.name = this.shadowRoot.getElementById("edit-name").value;
    p.side = this.shadowRoot.getElementById("edit-side").value;
    p.offset = parseFloat(this.shadowRoot.getElementById("edit-offset").value);
    p.facing = this.shadowRoot.getElementById("edit-facing").value;
    p.area_description = this.shadowRoot.getElementById("edit-area").value;
    p.is_doorbell = this.shadowRoot.getElementById("edit-doorbell").checked;
    p.ptz_up = this.shadowRoot.getElementById("edit-ptz-up").value.trim();
    p.ptz_down = this.shadowRoot.getElementById("edit-ptz-down").value.trim();
    p.ptz_left = this.shadowRoot.getElementById("edit-ptz-left").value.trim();
    p.ptz_right = this.shadowRoot.getElementById("edit-ptz-right").value.trim();
    p.ptz_return_to_monitor = this.shadowRoot.getElementById("edit-ptz-return").value.trim();
    this._editingCamera = null;
    this._save();
    this._render();
  }

  _positionToSideOffset(x, y) {
    // Determine which side of the house the drop is closest to
    const distances = {
      north: y,
      south: 1 - y,
      west: x,
      east: 1 - x,
    };
    const side = Object.entries(distances).sort((a, b) => a[1] - b[1])[0][0];
    let offset;
    if (side === "north" || side === "south") {
      offset = Math.max(0, Math.min(1, x));
    } else {
      offset = Math.max(0, Math.min(1, y));
    }
    return { side, offset };
  }

  _getPosition(side, offset) {
    // Convert side+offset to x,y percentage within the house div
    // Cameras are placed on the EDGE of the house rectangle
    const margin = 8; // % inset from edge for visibility
    switch (side) {
      case "north": return { x: margin + offset * (100 - 2 * margin), y: 0 };
      case "south": return { x: margin + offset * (100 - 2 * margin), y: 94 };
      case "west":  return { x: 0, y: margin + offset * (100 - 2 * margin) };
      case "east":  return { x: 94, y: margin + offset * (100 - 2 * margin) };
      default: return { x: 50, y: 50 };
    }
  }

  _getFacingArrow(side, facing) {
    if (facing === "away") {
      switch (side) {
        case "north": return "↑";
        case "south": return "↓";
        case "west": return "←";
        case "east": return "→";
      }
    }
    if (facing === "along_left") {
      switch (side) {
        case "north": return "←";
        case "south": return "→";
        case "west": return "↓";
        case "east": return "↑";
      }
    }
    if (facing === "along_right") {
      switch (side) {
        case "north": return "→";
        case "south": return "←";
        case "west": return "↑";
        case "east": return "↓";
      }
    }
    return "•";
  }

  _showToast(message, isError = false) {
    const toast = document.createElement("div");
    toast.textContent = message;
    toast.style.cssText = `
      position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
      padding: 12px 24px; border-radius: 8px; font-size: 0.9rem; z-index: 9999;
      background: ${isError ? "#d32f2f" : "#388e3c"}; color: white;
      box-shadow: 0 4px 12px rgba(0,0,0,0.3); transition: opacity 0.3s;
    `;
    document.body.appendChild(toast);
    setTimeout(() => { toast.style.opacity = "0"; setTimeout(() => toast.remove(), 300); }, 2500);
  }

  _renderError(msg) {
    this.shadowRoot.innerHTML = `
      <ha-card>
        <div style="padding:24px; text-align:center; color:var(--error-color)">
          <ha-icon icon="mdi:alert-circle"></ha-icon>
          <p>${msg}</p>
          <p style="font-size:0.85rem; color:var(--secondary-text-color)">
            Make sure you have cameras added in the Entities step first.
          </p>
        </div>
      </ha-card>
    `;
  }

  _esc(str) {
    if (!str) return "";
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  _styles() {
    return `<style>
      :host { display: block; }
      ha-card { overflow: visible; }
      .card-header { display: flex; align-items: center; gap: 8px; padding: 16px; font-size: 1.1rem; font-weight: 500; }
      .card-header ha-icon { --mdc-icon-size: 24px; color: var(--primary-color); }
      .map-container { padding: 0 16px 16px; }
      .instructions { font-size: 0.82rem; color: var(--secondary-text-color); margin-bottom: 12px; padding: 8px 12px; background: rgba(var(--rgb-primary-color), 0.05); border-radius: 8px; }
      .layout { display: grid; grid-template-columns: 200px 1fr; gap: 16px; min-height: 400px; }
      @media (max-width: 600px) { .layout { grid-template-columns: 1fr; } }
      .sidebar { border-right: 1px solid var(--divider-color); padding-right: 12px; }
      .sidebar-title { font-size: 0.8rem; font-weight: 600; text-transform: uppercase; color: var(--secondary-text-color); margin-bottom: 8px; letter-spacing: 0.5px; }
      .no-cameras { font-size: 0.85rem; color: var(--secondary-text-color); font-style: italic; }
      .camera-chip { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-radius: 8px; margin-bottom: 6px; cursor: grab; transition: background 0.15s; font-size: 0.85rem; }
      .camera-chip.unplaced { background: rgba(var(--rgb-primary-color), 0.08); border: 1px dashed var(--primary-color); }
      .camera-chip.unplaced:hover { background: rgba(var(--rgb-primary-color), 0.15); }
      .camera-chip.placed { background: rgba(76, 175, 80, 0.1); border: 1px solid rgba(76, 175, 80, 0.3); }
      .camera-chip ha-icon { --mdc-icon-size: 18px; }
      .camera-chip span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .remove-btn { background: none; border: none; color: var(--error-color); cursor: pointer; font-size: 1.2rem; padding: 0 4px; opacity: 0.6; }
      .remove-btn:hover { opacity: 1; }
      .house-area { position: relative; display: flex; align-items: center; justify-content: center; }
      .compass { position: absolute; top: 0; left: 0; right: 0; bottom: 0; pointer-events: none; }
      .compass span { position: absolute; font-size: 0.75rem; font-weight: 700; color: var(--secondary-text-color); opacity: 0.5; }
      .compass-n { top: 2px; left: 50%; transform: translateX(-50%); }
      .compass-s { bottom: 2px; left: 50%; transform: translateX(-50%); }
      .compass-e { right: 2px; top: 50%; transform: translateY(-50%); }
      .compass-w { left: 2px; top: 50%; transform: translateY(-50%); }
      .house { position: relative; width: 300px; height: 300px; border: 3px solid var(--primary-color); border-radius: 4px; background: rgba(var(--rgb-primary-color), 0.03); margin: 20px; }
      .house-label { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); font-size: 1.2rem; font-weight: 600; color: var(--primary-color); opacity: 0.2; pointer-events: none; }
      .placed-camera { position: absolute; transform: translate(-50%, -50%); cursor: pointer; text-align: center; z-index: 2; transition: transform 0.1s; }
      .placed-camera:hover { transform: translate(-50%, -50%) scale(1.15); z-index: 10; }
      .camera-icon { width: 36px; height: 36px; border-radius: 50%; background: var(--primary-color); color: white; display: flex; align-items: center; justify-content: center; box-shadow: 0 2px 6px rgba(0,0,0,0.3); position: relative; }
      .camera-icon ha-icon { --mdc-icon-size: 20px; color: white; }
      .ptz-badge { position: absolute; top: -4px; right: -8px; font-size: 0.55rem; background: #ff9800; color: white; border-radius: 4px; padding: 1px 3px; font-weight: 700; }
      .facing-arrow { font-size: 1rem; line-height: 1; margin-top: 2px; }
      .camera-label { font-size: 0.65rem; margin-top: 1px; white-space: nowrap; max-width: 60px; overflow: hidden; text-overflow: ellipsis; color: var(--primary-text-color); }
      .edit-panel { padding: 16px; border-top: 1px solid var(--divider-color); margin-top: 12px; }
      .edit-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; font-weight: 500; }
      .close-edit { background: none; border: none; font-size: 1.5rem; cursor: pointer; color: var(--secondary-text-color); }
      .edit-field { margin-bottom: 10px; }
      .edit-field label { display: block; font-size: 0.8rem; color: var(--secondary-text-color); margin-bottom: 3px; }
      .edit-field input[type="text"], .edit-field input[type="range"], .edit-field select, .edit-field textarea {
        width: 100%; padding: 6px 10px; border: 1px solid var(--divider-color); border-radius: 6px;
        font-size: 0.85rem; background: var(--card-background-color); color: var(--primary-text-color); box-sizing: border-box;
      }
      .edit-field textarea { resize: vertical; }
      .edit-section-title { font-size: 0.8rem; font-weight: 600; color: var(--primary-color); margin: 12px 0 8px; text-transform: uppercase; }
      .save-btn { display: block; width: 100%; padding: 10px; background: var(--primary-color); color: white; border: none; border-radius: 8px; font-size: 0.9rem; cursor: pointer; margin-top: 12px; }
      .save-btn:hover { opacity: 0.9; }
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
    description: "Interactive visual editor for placing cameras around your house outline. Drag cameras to position them.",
    preview: false,
    documentationURL: "https://github.com/slucas/ha-doorbell-jeeves",
  });
}
