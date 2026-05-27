/**
 * Jeeves Memory Timeline Card
 * A polished Lovelace card for displaying Doorbell Jeeves visitor memories
 * in a beautiful, chronological timeline with images and expandable details.
 */

// ─── Outcome colors ────────────────────────────────────────────────────────────
const OUTCOME_COLORS = {
  greeted: [76, 175, 80],
  assisted: [33, 150, 243],
  denied: [244, 67, 54],
  escalated: [255, 152, 0],
  timeout: [158, 158, 158],
  memory: [103, 58, 183],
  unknown: [158, 158, 158],
};

const OUTCOME_ICONS = {
  greeted: "mdi:hand-wave",
  assisted: "mdi:handshake",
  denied: "mdi:shield-lock",
  escalated: "mdi:phone-alert",
  timeout: "mdi:timer-sand",
  memory: "mdi:brain",
  unknown: "mdi:account-question",
};

// ─── Card Editor ───────────────────────────────────────────────────────────────
class JeevesMemoryTimelineCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._rendered) this._render();
  }

  _render() {
    if (!this._config) return;
    this._rendered = true;

    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }

    const entities = this._hass
      ? Object.keys(this._hass.states)
          .filter((e) => e.startsWith("sensor.") && e.includes("memory_feed"))
          .map((e) => `<option value="${e}" ${this._config.entity === e ? "selected" : ""}>${this._hass.states[e].attributes.friendly_name || e}</option>`)
          .join("")
      : "";

    this.shadowRoot.innerHTML = `
      <style>
        .editor { padding: 16px; }
        .field { margin-bottom: 16px; }
        .field label { display: block; font-weight: 500; margin-bottom: 4px; font-size: 0.9rem; color: var(--primary-text-color); }
        .field input, .field select { width: 100%; padding: 8px 12px; border: 1px solid var(--divider-color); border-radius: 8px; font-size: 0.9rem; background: var(--card-background-color); color: var(--primary-text-color); box-sizing: border-box; }
        .field input:focus, .field select:focus { outline: none; border-color: var(--primary-color); }
        .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        .field small { color: var(--secondary-text-color); font-size: 0.78rem; margin-top: 4px; display: block; }
      </style>
      <div class="editor">
        <div class="field">
          <label>Memory Feed Entity</label>
          <select id="entity">${entities || '<option value="">No memory feed entities found</option>'}</select>
          <small>Select the Jeeves Memory Feed sensor entity</small>
        </div>
        <div class="field">
          <label>Title</label>
          <input type="text" id="title" value="${this._esc(this._config.title || "")}" placeholder="Jeeves Memories">
        </div>
        <div class="field-row">
          <div class="field">
            <label>Max Items</label>
            <input type="number" id="max_items" value="${this._config.max_items || 50}" min="1" max="200">
          </div>
          <div class="field">
            <label>Days to Show</label>
            <input type="number" id="number_of_days" value="${this._config.number_of_days || 30}" min="1" max="365">
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>Time Format</label>
            <select id="time_format">
              <option value="relative" ${this._config.time_format !== "absolute" ? "selected" : ""}>Relative (2 hours ago)</option>
              <option value="absolute" ${this._config.time_format === "absolute" ? "selected" : ""}>Absolute (14:30)</option>
            </select>
          </div>
          <div class="field">
            <label>Image Size</label>
            <select id="image_size">
              <option value="small" ${this._config.image_size === "small" ? "selected" : ""}>Small (thumbnail)</option>
              <option value="medium" ${(this._config.image_size || "medium") === "medium" ? "selected" : ""}>Medium</option>
              <option value="large" ${this._config.image_size === "large" ? "selected" : ""}>Large (full width)</option>
              <option value="none" ${this._config.image_size === "none" ? "selected" : ""}>Hidden</option>
            </select>
          </div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>Group by Date</label>
            <select id="group_by_date">
              <option value="true" ${this._config.group_by_date !== false ? "selected" : ""}>Yes</option>
              <option value="false" ${this._config.group_by_date === false ? "selected" : ""}>No</option>
            </select>
          </div>
          <div class="field">
            <label>Compact Mode</label>
            <select id="compact">
              <option value="false" ${!this._config.compact ? "selected" : ""}>Normal</option>
              <option value="true" ${this._config.compact ? "selected" : ""}>Compact</option>
            </select>
          </div>
        </div>
      </div>
    `;

    // Wire up change events
    ["entity", "title", "max_items", "number_of_days", "time_format", "image_size", "group_by_date", "compact"].forEach((id) => {
      const el = this.shadowRoot.getElementById(id);
      if (el) el.addEventListener("change", () => this._onChange());
    });
    // Also listen to input for text fields
    ["title", "max_items", "number_of_days"].forEach((id) => {
      const el = this.shadowRoot.getElementById(id);
      if (el) el.addEventListener("input", () => this._onChange());
    });

    // Auto-emit config if entity dropdown has a value but config doesn't
    const entitySelect = this.shadowRoot.getElementById("entity");
    if (entitySelect && entitySelect.value && !this._config.entity) {
      this._onChange();
    }
  }

  _onChange() {
    const sr = this.shadowRoot;
    const newConfig = {
      ...this._config,
      entity: sr.getElementById("entity")?.value || this._config.entity,
      title: sr.getElementById("title")?.value || "",
      max_items: parseInt(sr.getElementById("max_items")?.value) || 50,
      number_of_days: parseInt(sr.getElementById("number_of_days")?.value) || 30,
      time_format: sr.getElementById("time_format")?.value || "relative",
      image_size: sr.getElementById("image_size")?.value || "medium",
      group_by_date: sr.getElementById("group_by_date")?.value !== "false",
      compact: sr.getElementById("compact")?.value === "true",
    };
    this._config = newConfig;
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: newConfig } }));
  }

  _esc(val) {
    return String(val ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
}

if (!customElements.get("jeeves-memory-timeline-card-editor")) {
  customElements.define("jeeves-memory-timeline-card-editor", JeevesMemoryTimelineCardEditor);
}

// ─── Main Timeline Card ────────────────────────────────────────────────────────
class JeevesMemoryTimelineCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("jeeves-memory-timeline-card-editor");
  }

  static getStubConfig(hass) {
    // Auto-detect a memory feed entity if possible
    let entity = "";
    if (hass && hass.states) {
      const feedEntity = Object.keys(hass.states).find(
        (e) => e.startsWith("sensor.") && e.includes("memory_feed")
      );
      if (feedEntity) entity = feedEntity;
    }
    return {
      entity,
      title: "Jeeves Memories",
      max_items: 50,
      number_of_days: 30,
      time_format: "relative",
      image_size: "medium",
      group_by_date: true,
      compact: false,
    };
  }

  setConfig(config) {
    this._config = {
      entity: "",
      title: "Jeeves Memories",
      max_items: 50,
      number_of_days: 30,
      time_format: "relative",
      image_size: "medium",
      group_by_date: true,
      compact: false,
      ...config,
    };
    this._needsEntity = !this._config.entity;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }
    this._lastHash = null;
    this._expanded = new Set();
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 6;
  }

  getGridOptions() {
    return { rows: 5, columns: 12, min_rows: 3, max_rows: 12, min_columns: 6, max_columns: 24 };
  }

  // ─── Helpers ───────────────────────────────────────────────────────────────────
  _esc(value) {
    return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  _safeUrl(url) {
    const v = String(url ?? "").trim();
    if (!v) return "";
    if (v.startsWith("/") || v.startsWith("http://") || v.startsWith("https://")) return v;
    return "";
  }

  _outcomeColor(outcome) {
    const key = (outcome || "unknown").toLowerCase();
    const rgb = OUTCOME_COLORS[key] || OUTCOME_COLORS.unknown;
    return `rgb(${rgb[0]}, ${rgb[1]}, ${rgb[2]})`;
  }

  _outcomeColorAlpha(outcome, alpha) {
    const key = (outcome || "unknown").toLowerCase();
    const rgb = OUTCOME_COLORS[key] || OUTCOME_COLORS.unknown;
    return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`;
  }

  _outcomeIcon(outcome) {
    const key = (outcome || "unknown").toLowerCase();
    return OUTCOME_ICONS[key] || OUTCOME_ICONS.unknown;
  }

  _formatTime(value) {
    const date = new Date(value);
    if (isNaN(date.getTime())) return { display: "Unknown", full: "Unknown" };
    const full = date.toLocaleString();

    if (this._config.time_format === "absolute") {
      return { display: date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), full };
    }

    const nowMs = Date.now();
    const diffMs = nowMs - date.getTime();
    const diffSec = Math.round(diffMs / 1000);

    if (diffSec < 60) return { display: "Just now", full };
    if (diffSec < 3600) return { display: `${Math.floor(diffSec / 60)}m ago`, full };
    if (diffSec < 86400) return { display: `${Math.floor(diffSec / 3600)}h ago`, full };
    if (diffSec < 172800) return { display: "Yesterday", full };
    return { display: `${Math.floor(diffSec / 86400)}d ago`, full };
  }

  _dateLabel(value) {
    const date = new Date(value);
    if (isNaN(date.getTime())) return "Unknown";
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const target = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    const diff = Math.round((today - target) / 86400000);
    if (diff === 0) return "Today";
    if (diff === 1) return "Yesterday";
    if (diff < 7) return date.toLocaleDateString([], { weekday: "long" });
    return date.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  }

  _duration(seconds) {
    if (!Number.isFinite(Number(seconds))) return "";
    const s = Math.round(Number(seconds));
    if (s < 60) return `${s}s`;
    return `${Math.floor(s / 60)}m ${s % 60}s`;
  }

  // ─── Render ────────────────────────────────────────────────────────────────────
  _render() {
    if (!this.shadowRoot || !this._config || !this._hass) return;

    if (this._needsEntity || !this._config.entity) {
      this.shadowRoot.innerHTML = `
        <ha-card>
          <div class="card-header"><span class="header-title">${this._esc(this._config.title || "Jeeves Memories")}</span></div>
          <div class="empty-state">
            <ha-icon icon="mdi:cog"></ha-icon>
            <p>Select a Memory Feed entity</p>
            <span class="empty-hint">Open the card editor to choose your Jeeves memory feed sensor</span>
          </div>
        </ha-card>
        ${this._styles()}
      `;
      return;
    }

    const state = this._hass.states[this._config.entity];
    if (!state) {
      this.shadowRoot.innerHTML = `
        <ha-card>
          <div class="card-header"><span class="header-title">${this._esc(this._config.title)}</span></div>
          <div class="empty-state">
            <ha-icon icon="mdi:database-off"></ha-icon>
            <p>Entity not found: <code>${this._esc(this._config.entity)}</code></p>
          </div>
        </ha-card>
        ${this._styles()}
      `;
      return;
    }

    const allMemories = Array.isArray(state.attributes.memories)
      ? state.attributes.memories
      : Array.isArray(state.attributes.entries)
        ? state.attributes.entries
        : [];

    // Filter by days
    const cutoff = Date.now() - this._config.number_of_days * 86400000;
    const filtered = allMemories.filter((m) => {
      const ts = new Date(m.timestamp || m.created_at || 0).getTime();
      return ts >= cutoff;
    });

    const maxItems = Math.max(1, Math.floor(Number(this._config.max_items) || 50));
    const entries = filtered.slice(0, maxItems);

    // Check if content actually changed
    const hash = JSON.stringify(entries.map((e) => e.id || e.timestamp));
    if (hash === this._lastHash && this.shadowRoot.querySelector(".timeline")) {
      return; // no update needed
    }
    this._lastHash = hash;

    if (entries.length === 0) {
      this.shadowRoot.innerHTML = `
        <ha-card>
          ${this._config.title ? `<div class="card-header">${this._esc(this._config.title)}</div>` : ""}
          <div class="empty-state">
            <ha-icon icon="mdi:brain"></ha-icon>
            <p>No memories yet</p>
            <span class="empty-hint">Memories will appear here after Jeeves interacts with visitors</span>
          </div>
        </ha-card>
        ${this._styles()}
      `;
      return;
    }

    // Group by date if enabled
    let contentHtml;
    if (this._config.group_by_date) {
      const groups = new Map();
      for (const entry of entries) {
        const label = this._dateLabel(entry.timestamp || entry.created_at || "");
        if (!groups.has(label)) groups.set(label, []);
        groups.get(label).push(entry);
      }
      contentHtml = Array.from(groups.entries())
        .map(([label, items]) => `
          <div class="date-group">
            <div class="date-header"><span>${this._esc(label)}</span></div>
            ${items.map((entry, i) => this._renderEntry(entry, i)).join("")}
          </div>
        `)
        .join("");
    } else {
      contentHtml = entries.map((entry, i) => this._renderEntry(entry, i)).join("");
    }

    const countLabel = filtered.length === 1 ? "1 memory" : `${filtered.length} memories`;

    this.shadowRoot.innerHTML = `
      <ha-card>
        ${this._config.title ? `<div class="card-header"><span class="header-title">${this._esc(this._config.title)}</span><span class="header-count">${countLabel}</span></div>` : ""}
        <div class="timeline">
          ${contentHtml}
        </div>
      </ha-card>
      ${this._styles()}
    `;

    // Wire up expand/collapse
    this.shadowRoot.querySelectorAll(".memory-card").forEach((card) => {
      card.addEventListener("click", (e) => {
        if (e.target.closest("img")) return; // don't toggle when clicking image
        card.classList.toggle("expanded");
      });
    });
  }

  _renderEntry(entry, index) {
    const visitor = entry.visitor_name || entry.visitor_description || "Visitor";
    const summary = entry.summary || "No summary available.";
    const outcome = entry.outcome || "memory";
    const imageUrl = this._config.image_size !== "none" ? this._safeUrl(entry.image_url) : "";
    const time = this._formatTime(entry.timestamp || entry.created_at || "");
    const duration = this._duration(entry.duration_seconds);
    const compact = this._config.compact;
    const imgSize = this._config.image_size || "medium";
    const imgClass = `entry-image img-${imgSize}`;

    return `
      <div class="memory-card ${compact ? "compact" : ""}" data-id="${this._esc(entry.id || index)}">
        <div class="card-left">
          <div class="dot-container">
            <div class="timeline-dot" style="background: ${this._outcomeColor(outcome)}"></div>
            <div class="timeline-line"></div>
          </div>
        </div>
        <div class="card-right">
          <div class="card-content-wrap">
            <div class="card-top-row">
              <div class="visitor-info">
                <span class="visitor-name">${this._esc(visitor)}</span>
                <span class="time-label" title="${this._esc(time.full)}">${this._esc(time.display)}</span>
              </div>
              <span class="outcome-badge" style="background: ${this._outcomeColorAlpha(outcome, 0.12)}; color: ${this._outcomeColor(outcome)}">
                ${this._esc(outcome)}
              </span>
            </div>
            <div class="summary-text">${this._esc(summary)}</div>
            ${duration ? `<div class="meta-row"><ha-icon icon="mdi:clock-outline" class="meta-icon"></ha-icon><span>${duration}</span></div>` : ""}
            ${imageUrl ? `<img class="${imgClass}" loading="lazy" src="${this._esc(imageUrl)}" alt="Snapshot of ${this._esc(visitor)}">` : ""}
          </div>
        </div>
      </div>
    `;
  }

  // ─── Styles ────────────────────────────────────────────────────────────────────
  _styles() {
    return `
      <style>
        :host {
          display: block;
          height: 100%;
        }
        ha-card {
          height: 100%;
          display: flex;
          flex-direction: column;
          overflow: hidden;
        }
        .card-header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 16px 16px 8px;
          gap: 8px;
        }
        .header-title {
          font-size: 1.2rem;
          font-weight: 500;
          color: var(--primary-text-color);
        }
        .header-count {
          font-size: 0.8rem;
          color: var(--secondary-text-color);
          background: var(--secondary-background-color, rgba(0,0,0,0.05));
          border-radius: 12px;
          padding: 2px 10px;
          white-space: nowrap;
        }

        /* Timeline container */
        .timeline {
          flex: 1;
          overflow-y: auto;
          padding: 8px 16px 16px;
          scrollbar-width: thin;
          scrollbar-color: var(--divider-color) transparent;
        }
        .timeline::-webkit-scrollbar { width: 4px; }
        .timeline::-webkit-scrollbar-thumb { background: var(--divider-color); border-radius: 4px; }

        /* Date groups */
        .date-group { margin-bottom: 4px; }
        .date-header {
          position: sticky;
          top: 0;
          z-index: 2;
          padding: 8px 0 6px;
          background: var(--card-background-color, #fff);
        }
        .date-header span {
          font-size: 0.78rem;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.06em;
          color: var(--secondary-text-color);
        }

        /* Memory card */
        .memory-card {
          display: flex;
          gap: 0;
          cursor: pointer;
          transition: background 0.15s ease;
          border-radius: 12px;
          padding: 2px 0;
        }
        .memory-card:hover {
          background: var(--secondary-background-color, rgba(0,0,0,0.03));
        }

        /* Left timeline column */
        .card-left {
          display: flex;
          flex-direction: column;
          align-items: center;
          width: 24px;
          flex-shrink: 0;
          padding-top: 6px;
        }
        .dot-container {
          display: flex;
          flex-direction: column;
          align-items: center;
          flex: 1;
        }
        .timeline-dot {
          width: 10px;
          height: 10px;
          border-radius: 50%;
          flex-shrink: 0;
          box-shadow: 0 0 0 3px var(--card-background-color, #fff);
        }
        .timeline-line {
          width: 2px;
          flex: 1;
          min-height: 8px;
          background: var(--divider-color);
          margin-top: 4px;
        }
        .memory-card:last-child .timeline-line,
        .date-group:last-child .memory-card:last-child .timeline-line {
          background: transparent;
        }

        /* Right content column */
        .card-right {
          flex: 1;
          min-width: 0;
          padding: 4px 8px 12px;
        }
        .card-content-wrap {
          display: flex;
          flex-direction: column;
          gap: 6px;
        }
        .card-top-row {
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          gap: 8px;
        }
        .visitor-info {
          display: flex;
          flex-direction: column;
          gap: 2px;
          min-width: 0;
        }
        .visitor-name {
          font-size: 0.92rem;
          font-weight: 500;
          color: var(--primary-text-color);
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .time-label {
          font-size: 0.75rem;
          color: var(--secondary-text-color);
        }
        .outcome-badge {
          font-size: 0.7rem;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.04em;
          border-radius: 6px;
          padding: 2px 8px;
          white-space: nowrap;
          flex-shrink: 0;
        }

        /* Summary */
        .summary-text {
          font-size: 0.86rem;
          line-height: 1.4;
          color: var(--primary-text-color);
          display: -webkit-box;
          -webkit-line-clamp: 2;
          -webkit-box-orient: vertical;
          overflow: hidden;
        }
        .memory-card.expanded .summary-text {
          -webkit-line-clamp: unset;
          overflow: visible;
        }

        /* Meta row */
        .meta-row {
          display: flex;
          align-items: center;
          gap: 4px;
          font-size: 0.75rem;
          color: var(--secondary-text-color);
        }
        .meta-icon {
          --mdc-icon-size: 14px;
          width: 14px;
          height: 14px;
        }

        /* Images */
        .entry-image {
          border-radius: 10px;
          border: 1px solid var(--divider-color);
          object-fit: cover;
          background: var(--secondary-background-color);
          margin-top: 4px;
          display: none;
        }
        .memory-card.expanded .entry-image,
        .img-large {
          display: block;
        }
        .img-small { max-width: 80px; max-height: 60px; border-radius: 8px; }
        .img-medium { max-width: 100%; max-height: 160px; border-radius: 10px; }
        .img-large { width: 100%; max-height: 200px; border-radius: 10px; }

        /* Always show small thumbnails inline */
        .memory-card .img-small { display: block; }

        /* Compact mode */
        .memory-card.compact .card-right { padding: 2px 8px 8px; }
        .memory-card.compact .summary-text { -webkit-line-clamp: 1; font-size: 0.82rem; }
        .memory-card.compact .entry-image { display: none; }
        .memory-card.compact.expanded .entry-image { display: block; }

        /* Empty state */
        .empty-state {
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          padding: 48px 16px;
          gap: 12px;
          color: var(--secondary-text-color);
        }
        .empty-state ha-icon {
          --mdc-icon-size: 48px;
          opacity: 0.4;
        }
        .empty-state p {
          font-size: 1rem;
          margin: 0;
        }
        .empty-hint {
          font-size: 0.82rem;
          opacity: 0.7;
        }
      </style>
    `;
  }
}

if (!customElements.get("jeeves-memory-timeline-card")) {
  customElements.define("jeeves-memory-timeline-card", JeevesMemoryTimelineCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "jeeves-memory-timeline-card")) {
  window.customCards.push({
    type: "jeeves-memory-timeline-card",
    name: "Jeeves Memory Timeline",
    description: "Beautiful timeline card showing Doorbell Jeeves visitor memories with images, outcomes, and expandable details.",
    preview: true,
    documentationURL: "https://github.com/slucas/ha-doorbell-jeeves",
  });
}
