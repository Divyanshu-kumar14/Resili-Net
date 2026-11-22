/* Replay mirror: poll the public map JSON on this origin and re-render locally.
   No external requests, no inline HTML from data, and no focus stealing. */
(() => {
  "use strict";

  const app = document.getElementById("replay-app");
  if (!app) return;

  const mapUrl = app.dataset.mapUrl;
  const svgNs = "http://www.w3.org/2000/svg";
  const markers = document.getElementById("markers");
  const needsBody = document.getElementById("needs-body");
  const suppliesBody = document.getElementById("supplies-body");
  const allocationsBody = document.getElementById("allocations-body");
  const statusLine = document.getElementById("live-status");
  const freshness = document.getElementById("freshness");
  const freshnessText = document.getElementById("freshness-text");

  let etag = app.dataset.etag || null;
  let refreshing = false;
  let generatedAt = freshness ? new Date(freshness.dataset.generatedAt) : new Date();
  let lastMessage = statusLine ? statusLine.textContent.trim() : "";

  function announce(message) {
    if (!statusLine || message === lastMessage) return;
    lastMessage = message;
    statusLine.textContent = message;
  }

  function cell(value, attributes) {
    const node = document.createElement("td");
    node.textContent = value;
    if (attributes) {
      for (const [name, attributeValue] of Object.entries(attributes)) {
        node.setAttribute(name, attributeValue);
      }
    }
    return node;
  }

  function shortLabel(name) {
    return name.length <= 28 ? name : `${name.slice(0, 27)}…`;
  }

  function project(entities) {
    const placed = entities.filter(
      (entity) => entity.lat !== null && entity.lon !== null
    );
    const positions = new Map();
    if (placed.length === 0) return positions;
    const lats = placed.map((entity) => entity.lat);
    const lons = placed.map((entity) => entity.lon);
    const minLat = Math.min(...lats);
    const maxLat = Math.max(...lats);
    const minLon = Math.min(...lons);
    const maxLon = Math.max(...lons);
    const latSpan = maxLat - minLat;
    const lonSpan = maxLon - minLon;
    for (const entity of placed) {
      const x = lonSpan === 0 ? 50 : 10 + 80 * ((entity.lon - minLon) / lonSpan);
      const y = latSpan === 0 ? 30 : 8 + 44 * ((maxLat - entity.lat) / latSpan);
      positions.set(entity.id, {
        x: Math.round(x * 10) / 10,
        y: Math.round(y * 10) / 10,
      });
    }
    return positions;
  }

  function svgElement(name, attributes) {
    const node = document.createElementNS(svgNs, name);
    for (const [attributeName, value] of Object.entries(attributes)) {
      node.setAttribute(attributeName, String(value));
    }
    return node;
  }

  function shapeFor(entity, position) {
    const { x, y } = position;
    if (entity.type === "shelter") {
      return svgElement("circle", { cx: x, cy: y, r: 2.2 });
    }
    if (entity.type === "depot") {
      return svgElement("rect", {
        x: x - 2.2,
        y: y - 1.7,
        width: 4.4,
        height: 3.4,
        rx: 0.3,
      });
    }
    if (entity.type === "hub") {
      return svgElement("polygon", {
        points: `${x},${y - 2.6} ${x + 2.6},${y} ${x},${y + 2.6} ${x - 2.6},${y}`,
      });
    }
    return svgElement("polygon", {
      points: `${x},${y - 2.6} ${x + 2.6},${y + 2} ${x - 2.6},${y + 2}`,
    });
  }

  function renderMarkers(payload) {
    const positions = project(payload.entities);
    const firstNeed = new Map();
    for (const need of payload.needs) {
      if (!firstNeed.has(need.entity_id)) firstNeed.set(need.entity_id, need.id);
    }
    const firstSupply = new Map();
    for (const supply of payload.supplies) {
      if (!firstSupply.has(supply.entity_id)) {
        firstSupply.set(supply.entity_id, supply.id);
      }
    }

    const active = document.activeElement;
    const focusedLabel =
      active && active.closest && active.closest("#markers")
        ? active.getAttribute("aria-label")
        : null;

    const next = [];
    for (const entity of payload.entities) {
      const position = positions.get(entity.id);
      if (!position) continue;
      let href = "#tables-heading";
      if (firstNeed.has(entity.id)) href = `#need-${firstNeed.get(entity.id)}`;
      else if (firstSupply.has(entity.id)) {
        href = `#supply-${firstSupply.get(entity.id)}`;
      }
      const link = document.createElementNS(svgNs, "a");
      link.setAttribute("href", href);
      link.setAttribute("class", `marker marker-${entity.type}`);
      link.setAttribute("aria-label", `${entity.name}, ${entity.type}`);
      const title = document.createElementNS(svgNs, "title");
      title.textContent = entity.name;
      link.append(title);
      link.append(shapeFor(entity, position));
      const label = document.createElementNS(svgNs, "text");
      label.setAttribute("x", position.x + 3.2);
      label.setAttribute("y", position.y + 1);
      label.textContent = shortLabel(entity.name);
      link.append(label);
      next.push(link);
    }
    markers.replaceChildren(...next);

    if (focusedLabel) {
      const restored = Array.from(markers.querySelectorAll("a")).find(
        (node) => node.getAttribute("aria-label") === focusedLabel
      );
      if (restored) restored.focus({ preventScroll: true });
    }
  }

  function renderNeeds(payload, entities) {
    const rows = payload.needs.map((need) => {
      const entity = entities.get(need.entity_id) || { name: "Unknown entity" };
      const flags = [];
      if (!need.verified) flags.push("Unverified");
      if (need.needs_human_review) flags.push("Human review");
      const basis = need.quantity_source;
      const row = document.createElement("tr");
      row.id = `need-${need.id}`;
      row.append(
        cell(entity.name),
        cell(need.resource.replaceAll("_", " ")),
        cell(`${need.quantity} ${need.unit}`),
        cell(basis.charAt(0).toUpperCase() + basis.slice(1), {
          "data-basis": basis,
        }),
        cell(need.urgency),
        cell(need.status),
        cell(flags.length ? flags.join(", ") : "—"),
        cell(need.ref),
        cell(need.updated_at)
      );
      return row;
    });
    needsBody.replaceChildren(...rows);
  }

  function renderSupplies(payload, entities) {
    const rows = payload.supplies.map((supply) => {
      const entity = entities.get(supply.entity_id) || { name: "Unknown entity" };
      const row = document.createElement("tr");
      row.id = `supply-${supply.id}`;
      row.append(
        cell(entity.name),
        cell(supply.resource.replaceAll("_", " ")),
        cell(supply.on_hand),
        cell(supply.available),
        cell(supply.unit),
        cell(supply.updated_at)
      );
      return row;
    });
    suppliesBody.replaceChildren(...rows);
  }

  function renderAllocations(payload, needs, entities) {
    const rows = payload.allocations.map((allocation) => {
      const need = needs.get(allocation.need_id) || {};
      const entity = entities.get(need.entity_id) || { name: "Unknown entity" };
      const row = document.createElement("tr");
      row.id = `allocation-${allocation.ref}`;
      row.append(
        cell(allocation.ref),
        cell(allocation.need_ref),
        cell(entity.name),
        cell(`${allocation.quantity} ${allocation.unit}`),
        cell(allocation.distance_km),
        cell(allocation.status),
        cell(allocation.updated_at)
      );
      return row;
    });
    allocationsBody.replaceChildren(...rows);
  }

  function render(payload) {
    const entities = new Map(
      payload.entities.map((entity) => [entity.id, entity])
    );
    const needs = new Map(payload.needs.map((need) => [need.id, need]));
    renderMarkers(payload);
    renderNeeds(payload, entities);
    renderSupplies(payload, entities);
    renderAllocations(payload, needs, entities);
    generatedAt = new Date(payload.generated_at);
    announce(
      `Map updated. Showing ${payload.needs.length} needs and ` +
        `${payload.supplies.length} supplies.`
    );
    updateFreshness();
  }

  function updateFreshness() {
    if (!freshnessText) return;
    const seconds = Math.max(
      0,
      Math.round((Date.now() - generatedAt.getTime()) / 1000)
    );
    freshnessText.textContent = `Updated ${seconds} s ago.`;
  }

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const headers = { Accept: "application/json" };
      if (etag) headers["If-None-Match"] = etag;
      const response = await fetch(mapUrl, { headers, cache: "no-store" });
      if (response.status === 200) {
        etag = response.headers.get("ETag");
        render(await response.json());
      } else if (response.status !== 304) {
        announce("Live refresh failed; showing the last snapshot.");
      }
    } catch {
      announce("Live refresh unavailable; showing the last snapshot.");
    } finally {
      refreshing = false;
    }
    updateFreshness();
  }

  updateFreshness();
  window.setInterval(refresh, 1000);
})();
