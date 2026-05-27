class JeevesMemoryTimelineCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("jeeves-memory-timeline-card requires an entity");
    }
    this._config = {
      title: "Jeeves Memories",
      max_items: 50,
      show_images: true,
      relative_time: true,
      height: "460px",
      ...config,
    };
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 7;
  }

  _escape(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  _normalizeHeight(height) {
    if (typeof height === "number" && Number.isFinite(height)) {
      return `${Math.max(160, Math.round(height))}px`;
    }
    if (typeof height === "string" && height.trim()) {
      return height.trim();
    }
    return "460px";
  }

  _safeImageUrl(url) {
    const value = String(url ?? "").trim();
    if (!value) {
      return "";
    }
    if (value.startsWith("/") || value.startsWith("http://") || value.startsWith("https://")) {
      return value;
    }
    return "";
  }

  _formatTimestamp(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return { short: "Unknown time", full: "Unknown time" };
    }
    if (!this._config.relative_time) {
      const text = date.toLocaleString();
      return { short: text, full: text };
    }
    const nowMs = Date.now();
    const diffSeconds = Math.round((date.getTime() - nowMs) / 1000);
    const absSeconds = Math.abs(diffSeconds);
    const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    let amount = diffSeconds;
    let unit = "second";
    if (absSeconds >= 86400) {
      amount = Math.round(diffSeconds / 86400);
      unit = "day";
    } else if (absSeconds >= 3600) {
      amount = Math.round(diffSeconds / 3600);
      unit = "hour";
    } else if (absSeconds >= 60) {
      amount = Math.round(diffSeconds / 60);
      unit = "minute";
    }
    return {
      short: formatter.format(amount, unit),
      full: date.toLocaleString(),
    };
  }

  _render() {
    if (!this.shadowRoot || !this._config || !this._hass) {
      return;
    }
    const state = this._hass.states[this._config.entity];
    const title = this._escape(this._config.title || "Jeeves Memories");
    if (!state) {
      this.shadowRoot.innerHTML = `
        <ha-card header="${title}">
          <div class="empty">Entity not found: ${this._escape(this._config.entity)}</div>
          ${this._styles()}
        </ha-card>
      `;
      return;
    }

    const entriesRaw = Array.isArray(state.attributes.memories)
      ? state.attributes.memories
      : Array.isArray(state.attributes.entries)
        ? state.attributes.entries
        : [];
    const maxItems = Number.isFinite(Number(this._config.max_items))
      ? Math.max(1, Math.floor(Number(this._config.max_items)))
      : 50;
    const entries = entriesRaw.slice(0, maxItems);
    const height = this._normalizeHeight(this._config.height);
    const showImages = this._config.show_images !== false;

    const content =
      entries.length > 0
        ? entries
            .map((entry, index) => {
              const summary = this._escape(entry.summary || "No summary available.");
              const visitor = this._escape(entry.visitor_name || entry.visitor_description || "Unknown visitor");
              const outcome = this._escape(entry.outcome || "");
              const duration =
                Number.isFinite(Number(entry.duration_seconds))
                  ? `${Math.round(Number(entry.duration_seconds) * 10) / 10}s`
                  : "";
              const details = this._escape([visitor, duration, outcome].filter(Boolean).join(" • "));
              const imageUrl = showImages ? this._safeImageUrl(entry.image_url) : "";
              const entryId = this._escape(entry.id || `memory-${index}`);
              const time = this._formatTimestamp(entry.timestamp || entry.created_at || "");
              const badge = this._escape(entry.outcome || "memory");
              return `
                <article class="event">
                  <div class="timeline-dot" aria-hidden="true"></div>
                  <div class="event-body">
                    <div class="event-head">
                      <span class="event-time" title="${this._escape(time.full)}">${this._escape(
                        time.short
                      )}</span>
                      <span class="event-badge">${badge}</span>
                    </div>
                    <div class="event-summary">${summary}</div>
                    ${
                      details
                        ? `<div class="event-details">${details}</div>`
                        : ""
                    }
                    ${
                      imageUrl
                        ? `<img class="event-image" loading="lazy" src="${this._escape(
                            imageUrl
                          )}" alt="Memory image ${entryId}">`
                        : ""
                    }
                  </div>
                </article>
              `;
            })
            .join("")
        : `<div class="empty">No saved memories yet.</div>`;

    this.shadowRoot.innerHTML = `
      <ha-card header="${title}">
        <div class="meta">
          <span>${entriesRaw.length} memories</span>
          <span>Entity: ${this._escape(this._config.entity)}</span>
        </div>
        <div class="timeline" style="max-height:${this._escape(height)}">
          ${content}
        </div>
        ${this._styles()}
      </ha-card>
    `;
  }

  _styles() {
    return `
      <style>
        :host {
          display: block;
        }
        ha-card {
          overflow: hidden;
        }
        .meta {
          display: flex;
          justify-content: space-between;
          gap: 12px;
          padding: 0 16px 8px;
          font-size: 0.8rem;
          color: var(--secondary-text-color);
        }
        .timeline {
          overflow-y: auto;
          padding: 0 16px 16px 28px;
          scrollbar-width: thin;
        }
        .event {
          position: relative;
          margin: 0;
          padding: 0 0 18px 14px;
          border-left: 2px solid var(--divider-color);
        }
        .event:last-child {
          border-left-color: transparent;
          padding-bottom: 0;
        }
        .timeline-dot {
          position: absolute;
          left: -7px;
          top: 3px;
          width: 10px;
          height: 10px;
          border-radius: 50%;
          background: var(--primary-color);
          box-shadow: 0 0 0 2px var(--card-background-color);
        }
        .event-body {
          display: flex;
          flex-direction: column;
          gap: 8px;
        }
        .event-head {
          display: flex;
          justify-content: space-between;
          align-items: center;
          gap: 8px;
        }
        .event-time {
          font-size: 0.78rem;
          color: var(--secondary-text-color);
        }
        .event-badge {
          font-size: 0.72rem;
          text-transform: uppercase;
          letter-spacing: 0.05em;
          color: var(--secondary-text-color);
          background: var(--secondary-background-color);
          border-radius: 8px;
          padding: 2px 8px;
          white-space: nowrap;
        }
        .event-summary {
          font-size: 0.96rem;
          line-height: 1.35;
        }
        .event-details {
          font-size: 0.85rem;
          line-height: 1.35;
          color: var(--secondary-text-color);
        }
        .event-image {
          width: 100%;
          border-radius: 10px;
          border: 1px solid var(--divider-color);
          object-fit: cover;
          background: var(--secondary-background-color);
        }
        .empty {
          padding: 12px 16px 16px;
          color: var(--secondary-text-color);
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
    description: "Scrollable timeline card for Jeeves memory summaries and images.",
    preview: false,
  });
}
