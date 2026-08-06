"use strict";

const GRID_DEGREES = 0.025;
const STATUS_COLORS = {
  available: "#16875b",
  busy: "#c93c35",
  maintenance: "#d08b16",
  unknown: "#74808c",
};
const AREA_COLORS = ["#fff7ed", "#fed7aa", "#fb923c", "#c2410c", "#7c2d12"];

const metricDefinitions = {
  busy_hours: {
    label: "Busy station-hours",
    shortLabel: "Busy hours",
    rankTitle: "Top stations by busy hours",
    subtitle: "Midpoint-estimated station busy time in the selected window.",
    format: (value) => formatNumber(value, 2),
  },
  busy_percent: {
    label: "Busy utilization",
    shortLabel: "Busy %",
    rankTitle: "Top stations by busy utilization",
    subtitle: "Busy hours divided by time with a known station status.",
    format: (value) => formatPercent(value),
  },
  charging_connector_hours: {
    label: "Charging connector-hours",
    shortLabel: "Charging hours",
    rankTitle: "Top stations by charging time",
    subtitle: "Midpoint-estimated connector time explicitly marked charging.",
    format: (value) => formatNumber(value, 2),
  },
  scenario_revenue_amd: {
    label: "Scenario revenue (AMD)",
    shortLabel: "Scenario AMD",
    rankTitle: "Top stations by scenario revenue",
    subtitle: "Planning scenario based on charging time, rated power and load factor—not receipts.",
    format: (value) => formatMoney(value),
  },
};

const appState = {
  data: null,
  map: null,
  areaLayer: null,
  stationLayer: null,
  markerByStation: new Map(),
  layerMode: "both",
  didFitMap: false,
};

const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  cacheElements();
  attachEvents();
  initializeMap();
  loadData();
  window.setInterval(() => {
    if (document.visibilityState === "visible") loadData({ quiet: true });
  }, 60_000);
});

function cacheElements() {
  const ids = [
    "hours-filter",
    "metric-filter",
    "status-filter",
    "station-search",
    "refresh-button",
    "error-banner",
    "last-poll",
    "kpi-stations",
    "kpi-status-context",
    "kpi-busy-percent",
    "kpi-busy-hours",
    "kpi-charging-hours",
    "kpi-revenue",
    "kpi-revenue-context",
    "kpi-coverage",
    "kpi-window",
    "map-subtitle",
    "ranking-title",
    "ranking-subtitle",
    "ranking-chart",
    "table-subtitle",
    "visible-station-count",
    "station-table-body",
    "load-factor-note",
  ];
  for (const id of ids) elements[id] = document.getElementById(id);
}

function attachEvents() {
  elements["hours-filter"].addEventListener("change", () => loadData());
  elements["metric-filter"].addEventListener("change", renderDashboard);
  elements["status-filter"].addEventListener("change", renderDashboard);
  elements["station-search"].addEventListener("input", debounce(renderDashboard, 120));
  elements["refresh-button"].addEventListener("click", () => loadData());
  document.querySelectorAll("[data-layer]").forEach((button) => {
    button.addEventListener("click", () => {
      appState.layerMode = button.dataset.layer;
      document.querySelectorAll("[data-layer]").forEach((item) => {
        item.classList.toggle("active", item === button);
      });
      renderMap(getFilteredStations());
    });
  });
}

function initializeMap() {
  if (typeof window.L === "undefined") {
    showError("The map library could not load. Check this computer's internet connection and refresh the page.");
    return;
  }
  appState.map = window.L.map("map", {
    zoomControl: true,
    preferCanvas: true,
  }).setView([40.22, 44.62], 8);
  window.L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(appState.map);
  appState.areaLayer = window.L.layerGroup().addTo(appState.map);
  appState.stationLayer = window.L.layerGroup().addTo(appState.map);
  window.addEventListener("resize", debounce(() => appState.map.invalidateSize(), 100));
}

async function loadData(options = {}) {
  const refreshButton = elements["refresh-button"];
  if (!options.quiet) {
    refreshButton.disabled = true;
    refreshButton.textContent = "Loading…";
  }
  hideError();
  try {
    const hours = elements["hours-filter"].value;
    const response = await fetch(`/analytics/map?hours=${encodeURIComponent(hours)}`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`Analytics request returned HTTP ${response.status}`);
    appState.data = await response.json();
    renderDashboard();
  } catch (error) {
    showError(`Could not load dashboard data: ${error.message}`);
  } finally {
    refreshButton.disabled = false;
    refreshButton.textContent = "Refresh data";
  }
}

function renderDashboard() {
  if (!appState.data) return;
  const stations = getFilteredStations();
  renderKpis();
  renderMap(stations);
  renderRanking(stations);
  renderTable(stations);
}

function getFilteredStations() {
  const status = elements["status-filter"].value;
  const query = elements["station-search"].value.trim().toLocaleLowerCase();
  return appState.data.stations.filter((station) => {
    const statusMatches = status === "all" || station.current_status === status;
    const searchable = `${station.name || ""} ${station.address || ""}`.toLocaleLowerCase();
    return statusMatches && (!query || searchable.includes(query));
  });
}

function renderKpis() {
  const { portfolio, window: dataWindow, assumptions } = appState.data;
  elements["last-poll"].textContent = formatDateTime(dataWindow.end);
  elements["kpi-stations"].textContent = formatInteger(portfolio.station_count);
  elements["kpi-status-context"].textContent =
    `${formatInteger(portfolio.current_available)} available · ` +
    `${formatInteger(portfolio.current_busy)} busy · ` +
    `${formatInteger(portfolio.current_maintenance)} maintenance`;
  elements["kpi-busy-percent"].textContent = formatPercent(portfolio.busy_percent);
  elements["kpi-busy-hours"].textContent = `${formatNumber(portfolio.busy_hours, 2)} busy station-hours`;
  elements["kpi-charging-hours"].textContent = formatNumber(portfolio.charging_connector_hours, 2);
  elements["kpi-revenue"].textContent = formatMoney(portfolio.scenario_revenue_amd);
  elements["kpi-revenue-context"].textContent =
    `${formatPercent(assumptions.scenario_load_factor)} load factor · planning estimate`;
  elements["kpi-coverage"].textContent = formatPercent(portfolio.coverage_percent);
  elements["kpi-window"].textContent =
    `${formatNumber(dataWindow.coverage_hours, 2)} h window · ` +
    `${formatNumber(portfolio.unknown_station_hours, 2)} unknown station-hours`;
  elements["load-factor-note"].textContent = formatPercent(assumptions.scenario_load_factor);
}

function renderMap(stations) {
  if (!appState.map) return;
  appState.areaLayer.clearLayers();
  appState.stationLayer.clearLayers();
  appState.markerByStation.clear();

  const metricKey = elements["metric-filter"].value;
  const metric = metricDefinitions[metricKey];
  const areas = aggregateAreas(stations);
  const maxAreaValue = Math.max(0, ...areas.map((area) => area[metricKey] || 0));
  const maxStationValue = Math.max(0, ...stations.map((station) => station[metricKey] || 0));

  elements["map-subtitle"].textContent =
    `Grid intensity shows ${metric.label.toLocaleLowerCase()}. Marker color shows current status; marker size uses the same selected metric.`;

  if (appState.layerMode !== "stations") {
    for (const area of areas) {
      const value = area[metricKey] || 0;
      const rectangle = window.L.rectangle(
        [[area.south, area.west], [area.north, area.east]],
        {
          color: "#7c2d12",
          weight: 0.7,
          opacity: 0.7,
          fillColor: areaColor(value, maxAreaValue),
          fillOpacity: value > 0 ? 0.68 : 0.16,
        },
      );
      rectangle.bindPopup(areaPopup(area, metricKey));
      rectangle.addTo(appState.areaLayer);
    }
  }

  if (appState.layerMode !== "areas") {
    for (const station of stations) {
      if (!Number.isFinite(station.latitude) || !Number.isFinite(station.longitude)) continue;
      const status = STATUS_COLORS[station.current_status] ? station.current_status : "unknown";
      const value = station[metricKey] || 0;
      const radius = maxStationValue > 0
        ? 5 + 8 * Math.sqrt(Math.max(0, value) / maxStationValue)
        : 6;
      const marker = window.L.circleMarker([station.latitude, station.longitude], {
        radius,
        color: "#ffffff",
        weight: 2,
        fillColor: STATUS_COLORS[status],
        fillOpacity: 0.9,
      });
      marker.bindPopup(stationPopup(station));
      marker.addTo(appState.stationLayer);
      appState.markerByStation.set(station.station_id, marker);
    }
  }

  if (!appState.didFitMap) {
    const points = stations
      .filter((station) => Number.isFinite(station.latitude) && Number.isFinite(station.longitude))
      .map((station) => [station.latitude, station.longitude]);
    appState.map.invalidateSize();
    if (points.length) appState.map.fitBounds(points, { padding: [24, 24], maxZoom: 11 });
    appState.didFitMap = true;
  }
  window.requestAnimationFrame(() => appState.map.invalidateSize());
}

function aggregateAreas(stations) {
  const areaMap = new Map();
  for (const station of stations) {
    if (!Number.isFinite(station.latitude) || !Number.isFinite(station.longitude)) continue;
    const latIndex = Math.floor(station.latitude / GRID_DEGREES);
    const lonIndex = Math.floor(station.longitude / GRID_DEGREES);
    const key = `${latIndex}:${lonIndex}`;
    if (!areaMap.has(key)) {
      areaMap.set(key, {
        south: latIndex * GRID_DEGREES,
        west: lonIndex * GRID_DEGREES,
        north: (latIndex + 1) * GRID_DEGREES,
        east: (lonIndex + 1) * GRID_DEGREES,
        station_count: 0,
        current_busy_stations: 0,
        observed_station_hours: 0,
        known_station_hours: 0,
        unknown_station_hours: 0,
        coverage_percent: 0,
        busy_hours: 0,
        busy_percent: 0,
        charging_connector_hours: 0,
        scenario_revenue_amd: 0,
        top_station_name: null,
        top_station_busy_hours: -1,
      });
    }
    const area = areaMap.get(key);
    area.station_count += 1;
    area.current_busy_stations += station.current_status === "busy" ? 1 : 0;
    area.observed_station_hours += station.observed_hours || 0;
    area.known_station_hours += station.known_hours || 0;
    area.unknown_station_hours += station.unknown_hours || 0;
    area.busy_hours += station.busy_hours || 0;
    area.charging_connector_hours += station.charging_connector_hours || 0;
    area.scenario_revenue_amd += station.scenario_revenue_amd || 0;
    if ((station.busy_hours || 0) > area.top_station_busy_hours) {
      area.top_station_name = station.name;
      area.top_station_busy_hours = station.busy_hours || 0;
    }
  }
  const areas = [...areaMap.values()];
  for (const area of areas) {
    area.coverage_percent = area.observed_station_hours > 0
      ? area.known_station_hours / area.observed_station_hours
      : 0;
    area.busy_percent = area.known_station_hours > 0
      ? area.busy_hours / area.known_station_hours
      : 0;
  }
  return areas;
}

function renderRanking(stations) {
  const metricKey = elements["metric-filter"].value;
  const metric = metricDefinitions[metricKey];
  elements["ranking-title"].textContent = metric.rankTitle;
  elements["ranking-subtitle"].textContent = metric.subtitle;
  const ranked = [...stations]
    .sort((left, right) => (right[metricKey] || 0) - (left[metricKey] || 0))
    .slice(0, 10);
  const maxValue = Math.max(0, ...ranked.map((station) => station[metricKey] || 0));
  if (!ranked.length) {
    elements["ranking-chart"].innerHTML = '<div class="empty-state">No stations match these filters.</div>';
    return;
  }
  elements["ranking-chart"].innerHTML = ranked.map((station) => {
    const value = station[metricKey] || 0;
    const width = maxValue > 0 ? Math.max(1.5, (value / maxValue) * 100) : 0;
    return `
      <button class="rank-row" type="button" data-station-id="${escapeHtml(station.station_id)}">
        <span class="rank-label" title="${escapeHtml(station.name)}">${escapeHtml(station.name)}</span>
        <span class="rank-value">${metric.format(value)}</span>
        <span class="rank-track"><span class="rank-bar" style="width:${width.toFixed(2)}%"></span></span>
      </button>`;
  }).join("");
  elements["ranking-chart"].querySelectorAll("[data-station-id]").forEach((button) => {
    button.addEventListener("click", () => locateStation(button.dataset.stationId));
  });
}

function renderTable(stations) {
  const metricKey = elements["metric-filter"].value;
  const sorted = [...stations].sort(
    (left, right) => (right[metricKey] || 0) - (left[metricKey] || 0),
  );
  elements["visible-station-count"].textContent = `${formatInteger(sorted.length)} stations`;
  elements["table-subtitle"].textContent =
    `Sorted by ${metricDefinitions[metricKey].label.toLocaleLowerCase()}; select a row to locate it on the map.`;
  elements["station-table-body"].innerHTML = sorted.slice(0, 100).map((station) => `
    <tr tabindex="0" data-station-id="${escapeHtml(station.station_id)}">
      <td class="station-cell">
        <strong>${escapeHtml(station.name)}</strong>
        <span title="${escapeHtml(station.address || "")}">${escapeHtml(station.address || "Address not provided")}</span>
      </td>
      <td><span class="status-pill ${escapeHtml(station.current_status || "unknown")}">${escapeHtml(station.current_status || "unknown")}</span></td>
      <td class="number">${formatPercent(station.busy_percent)}</td>
      <td class="number">${formatPercent(station.coverage_percent)}</td>
      <td class="number">${formatNumber(station.busy_hours, 2)}</td>
      <td class="number">${formatNumber(station.charging_connector_hours, 2)}</td>
      <td class="number">${formatMoney(station.scenario_revenue_amd)}</td>
      <td class="number">${formatInteger(station.available_connectors)} / ${formatInteger(station.connector_count)}</td>
    </tr>
  `).join("");
  elements["station-table-body"].querySelectorAll("[data-station-id]").forEach((row) => {
    row.addEventListener("click", () => locateStation(row.dataset.stationId));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") locateStation(row.dataset.stationId);
    });
  });
}

function locateStation(stationId) {
  const station = appState.data.stations.find((item) => item.station_id === stationId);
  if (!station || !appState.map) return;
  if (appState.layerMode === "areas") {
    appState.layerMode = "both";
    document.querySelectorAll("[data-layer]").forEach((button) => {
      button.classList.toggle("active", button.dataset.layer === "both");
    });
    renderMap(getFilteredStations());
  }
  const marker = appState.markerByStation.get(stationId);
  appState.map.setView([station.latitude, station.longitude], 15, { animate: true });
  if (marker) marker.openPopup();
}

function stationPopup(station) {
  const status = station.current_status || "unknown";
  return `
    <div class="map-popup">
      <h3>${escapeHtml(station.name)}</h3>
      <p class="popup-address">${escapeHtml(station.address || "Address not provided")}</p>
      <span class="status-pill ${escapeHtml(status)}">${escapeHtml(status)}</span>
      <div class="popup-grid">
        <span>Busy utilization</span><strong>${formatPercent(station.busy_percent)}</strong>
        <span>Observed coverage</span><strong>${formatPercent(station.coverage_percent)}</strong>
        <span>Busy station-hours</span><strong>${formatNumber(station.busy_hours, 2)}</strong>
        <span>Charging connector-hours</span><strong>${formatNumber(station.charging_connector_hours, 2)}</strong>
        <span>Scenario revenue</span><strong>${formatMoney(station.scenario_revenue_amd)}</strong>
        <span>Available connectors</span><strong>${formatInteger(station.available_connectors)} / ${formatInteger(station.connector_count)}</strong>
      </div>
    </div>`;
}

function areaPopup(area, metricKey) {
  const metric = metricDefinitions[metricKey];
  return `
    <div class="map-popup">
      <h3>Geographic section</h3>
      <p class="popup-address">0.025° grid cell · ${formatInteger(area.station_count)} stations</p>
      <div class="popup-grid">
        <span>${escapeHtml(metric.shortLabel)}</span><strong>${metric.format(area[metricKey] || 0)}</strong>
        <span>Busy utilization</span><strong>${formatPercent(area.busy_percent)}</strong>
        <span>Observed coverage</span><strong>${formatPercent(area.coverage_percent)}</strong>
        <span>Busy station-hours</span><strong>${formatNumber(area.busy_hours, 2)}</strong>
        <span>Currently busy</span><strong>${formatInteger(area.current_busy_stations)}</strong>
        <span>Top busy station</span><strong>${escapeHtml(area.top_station_name || "—")}</strong>
      </div>
    </div>`;
}

function areaColor(value, maximum) {
  if (maximum <= 0 || value <= 0) return "#e6eaed";
  const ratio = Math.min(1, value / maximum);
  const index = Math.min(AREA_COLORS.length - 1, Math.floor(ratio * AREA_COLORS.length));
  return AREA_COLORS[index];
}

function formatNumber(value, digits = 1) {
  return new Intl.NumberFormat(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(value) || 0);
}

function formatInteger(value) {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(Number(value) || 0);
}

function formatPercent(value) {
  return new Intl.NumberFormat(undefined, {
    style: "percent",
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }).format(Number(value) || 0);
}

function formatMoney(value) {
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(Number(value) || 0)} AMD`;
}

function formatDateTime(value) {
  if (!value) return "Not available";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showError(message) {
  elements["error-banner"].textContent = message;
  elements["error-banner"].hidden = false;
}

function hideError() {
  elements["error-banner"].hidden = true;
  elements["error-banner"].textContent = "";
}

function debounce(callback, delay) {
  let timeout;
  return (...args) => {
    window.clearTimeout(timeout);
    timeout = window.setTimeout(() => callback(...args), delay);
  };
}
