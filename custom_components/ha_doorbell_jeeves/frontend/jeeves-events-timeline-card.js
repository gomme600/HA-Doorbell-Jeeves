/**
 * Jeeves Events Timeline Card
 * A Lovelace card for displaying important events published by the AI agent
 * with severity indicators, photos, and acknowledgement.
 */

const SEVERITY_COLORS = {
  info: [33, 150, 243],
  warning: [255, 152, 0],
  urgent: [244, 67, 54],
};

const SEVERITY_ICONS = {
  info: "mdi:information",
  warning: "mdi:alert",
  urgent: "mdi:alert-octagon",
};

// ─── Card Editor ───────────────────────────────────────────────────────────────
class JeevesEventsTimelineCardEditor extends HTMLElement {
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
          .filter((e) => e.startsWith("sensor.") && e.includes("events_feed"))
          .map((e) => `<option value="${e}" ${this._config.entity === e ? "selected" : ""}>${this._hass.states[e].attributes.friendly_name || e}</option>`)
          .join("")
      : "";

    this.shadowRoot.innerHTML = `
      <style>
        .editor { padding: 16px; }
        .field { margin-bottom: 16px; }
        .field label { display: block; font-weight: 500; margin-bottom: 4px; font-size: 0.9rem; color: var(--primary-text-color); }
        .field input, .field select { width: 100%; padding: 8px 12px; border: 1px solid var(--divider-color); border-radius: 8px; font-size: 0.9rem; background: var(--card-background-color); color: var(--primary-text-color); box-sizing: border-box; }
      </style>
      <div class="editor">
        <div class="field">
          <label>Events Feed Entity</label>
          <select id="entity">${entities || '<option value="">No events feed entities found</option>'}</select>
        </div>
        <div class="field">
          <label>Title</label>
          <input type="text" id="title" value="${this._esc(this._config.title || "")}" placeholder="Important Events">
        </div>
        <div class="field">
          <label>Max Items</label>
          <input type="number" id="max_items" value="${this._config.max_items || 50}" min="1" max="200">
        </div>
      </div>
    `;

    this.shadowRoot.querySelector("#entity").addEventListener("change", (e) => this._update("entity", e.target.value));
    this.shadowRoot.querySelector("#title").addEventListener("input", (e) => this._update("title", e.target.value));
    this.shadowRoot.querySelector("#max_items").addEventListener("input", (e) => this._update("max_items", parseInt(e.target.value) || 50));
  }

  _update(key, value) {
    this._config = { ...this._config, [key]: value };
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: this._config } }));
  }

  _esc(str) {
    return str.replace(/"/g, "&quot;").replace(/</g, "&lt;");
  }
}

// ─── Main Card ─────────────────────────────────────────────────────────────────
class JeevesEventsTimelineCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("jeeves-events-timeline-card-editor");
  }

  static getStubConfig() {
    return { type: "custom:jeeves-events-timeline-card", title: "Important Events", max_items: 50 };
  }

  setConfig(config) {
    this._config = { max_items: 50, ...config };
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;

    const entityId = this._config.entity;
    const state = entityId ? this._hass.states[entityId] : null;
    const events = state?.attributes?.events || [];
    const maxItems = this._config.max_items || 50;
    const displayEvents = events.slice(0, maxItems);
    const title = this._config.title || "Important Events";
    const unackCount = state?.attributes?.unacknowledged_count || 0;

    const badgeHtml = unackCount > 0
      ? `<span class="badge">${unackCount}</span>`
      : "";

    let contentHtml = "";
    if (displayEvents.length === 0) {
      contentHtml = `
        <div class="empty-state">
          <ha-icon icon="mdi:bell-check"></ha-icon>
          <p>No events yet</p>
          <span class="empty-hint">Events will appear here when the AI agent saves important information.</span>
        </div>
      `;
    } else {
      contentHtml = displayEvents.map((evt) => this._renderEvent(evt)).join("");
    }

    this.shadowRoot.innerHTML = `
      <ha-card>
        <div class="card-header">
          <div class="title-row">
            <ha-icon icon="mdi:bell-alert"></ha-icon>
            <span class="title">${title}</span>
            ${badgeHtml}
          </div>
        </div>
        <div class="events-container">${contentHtml}</div>
      </ha-card>
      ${this._getStyles()}
    `;

    // Add click handlers for expand/collapse
    this.shadowRoot.querySelectorAll(".event-item").forEach((el) => {
      el.addEventListener("click", () => el.classList.toggle("expanded"));
    });
  }

  _renderEvent(evt) {
    const severity = evt.severity || "info";
    const [r, g, b] = SEVERITY_COLORS[severity] || SEVERITY_COLORS.info;
    const icon = SEVERITY_ICONS[severity] || SEVERITY_ICONS.info;
    const ts = new Date(evt.timestamp);
    const timeStr = ts.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const ackClass = evt.acknowledged ? "acknowledged" : "";

    const photosHtml = (evt.photo_urls || []).length > 0
      ? `<div class="photos">${evt.photo_urls.map((url) => `<img src="${url}" loading="lazy" />`).join("")}</div>`
      : "";

    return `
      <div class="event-item ${ackClass}" style="--severity-color: ${r}, ${g}, ${b}">
        <div class="event-header">
          <div class="severity-dot"></div>
          <div class="event-meta">
            <span class="event-title">${this._esc(evt.title)}</span>
            <span class="event-time">${timeStr}</span>
          </div>
          <ha-icon class="severity-icon" icon="${icon}"></ha-icon>
        </div>
        <div class="event-body">
          <p class="event-description">${this._esc(evt.description)}</p>
          ${photosHtml}
        </div>
      </div>
    `;
  }

  _esc(str) {
    if (!str) return "";
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  _getStyles() {
    return `
      <style>
        :host {
          display: block;
        }
        ha-card {
          overflow: hidden;
        }
        .card-header {
          padding: 16px 16px 8px;
        }
        .title-row {
          display: flex;
          align-items: center;
          gap: 8px;
        }
        .title-row ha-icon {
          --mdc-icon-size: 24px;
          color: var(--primary-color);
        }
        .title {
          font-size: 1.1rem;
          font-weight: 500;
          flex: 1;
        }
        .badge {
          background: rgb(244, 67, 54);
          color: white;
          border-radius: 12px;
          padding: 2px 8px;
          font-size: 0.75rem;
          font-weight: 600;
        }
        .events-container {
          padding: 0 16px 16px;
          max-height: 600px;
          overflow-y: auto;
        }
        .event-item {
          border-left: 3px solid rgba(var(--severity-color), 0.8);
          margin-bottom: 12px;
          padding: 10px 14px;
          border-radius: 0 8px 8px 0;
          background: rgba(var(--severity-color), 0.05);
          cursor: pointer;
          transition: background 0.2s;
        }
        .event-item:hover {
          background: rgba(var(--severity-color), 0.1);
        }
        .event-item.acknowledged {
          opacity: 0.6;
        }
        .event-header {
          display: flex;
          align-items: center;
          gap: 8px;
        }
        .severity-dot {
          width: 8px;
          height: 8px;
          border-radius: 50%;
          background: rgb(var(--severity-color));
          flex-shrink: 0;
        }
        .event-meta {
          flex: 1;
          display: flex;
          flex-direction: column;
        }
        .event-title {
          font-weight: 500;
          font-size: 0.9rem;
        }
        .event-time {
          font-size: 0.75rem;
          color: var(--secondary-text-color);
        }
        .severity-icon {
          --mdc-icon-size: 20px;
          color: rgb(var(--severity-color));
          opacity: 0.7;
        }
        .event-body {
          display: none;
          margin-top: 8px;
          padding-top: 8px;
          border-top: 1px solid rgba(var(--severity-color), 0.15);
        }
        .event-item.expanded .event-body {
          display: block;
        }
        .event-description {
          font-size: 0.85rem;
          margin: 0 0 8px;
          white-space: pre-wrap;
          color: var(--primary-text-color);
        }
        .photos {
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          margin-top: 8px;
        }
        .photos img {
          width: 120px;
          height: 90px;
          object-fit: cover;
          border-radius: 6px;
          border: 1px solid var(--divider-color);
        }
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

if (!customElements.get("jeeves-events-timeline-card-editor")) {
  customElements.define("jeeves-events-timeline-card-editor", JeevesEventsTimelineCardEditor);
}
if (!customElements.get("jeeves-events-timeline-card")) {
  customElements.define("jeeves-events-timeline-card", JeevesEventsTimelineCard);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "jeeves-events-timeline-card")) {
  window.customCards.push({
    type: "jeeves-events-timeline-card",
    name: "Jeeves Events Timeline",
    description: "Timeline card showing important events saved by the AI agent with severity, photos, and acknowledgement.",
    preview: true,
    documentationURL: "https://github.com/slucas/ha-doorbell-jeeves",
  });
}
