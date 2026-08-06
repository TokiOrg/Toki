import fs from "node:fs/promises";
import path from "node:path";

import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


const [inputPath, outputPath, previewDir] = process.argv.slice(2);
if (!inputPath || !outputPath) {
  throw new Error(
    "Usage: node tools/build_analytics_workbook.mjs input.json output.xlsx [preview-dir]",
  );
}

const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
const workbook = Workbook.create();

const COLORS = {
  navy: "#123047",
  teal: "#087E8B",
  green: "#0F9D58",
  greenLight: "#DCFCE7",
  amber: "#F59E0B",
  amberLight: "#FEF3C7",
  red: "#DC2626",
  redLight: "#FEE2E2",
  blue: "#2563EB",
  blueLight: "#DBEAFE",
  gray: "#64748B",
  grayLight: "#F1F5F9",
  white: "#FFFFFF",
  border: "#CBD5E1",
};

const sheetNames = [
  "Dashboard",
  "Station Analytics",
  "Readable History",
  "Connector History",
  "Methodology",
  "Raw Stations",
  "Raw Connectors",
  "Raw Station History",
  "Raw Connector History",
  "Collector State",
];
const sheets = Object.fromEntries(
  sheetNames.map((name) => [name, workbook.worksheets.add(name)]),
);

function excelColumn(columnNumber) {
  let value = columnNumber;
  let result = "";
  while (value > 0) {
    value -= 1;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
}

function titleCase(key) {
  return key
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function asDate(value) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed;
}

function normalizeValue(key, value) {
  if (
    typeof value === "string" &&
    (key.endsWith("_at") ||
      key === "observation_start" ||
      key === "observation_end")
  ) {
    return asDate(value);
  }
  return value ?? null;
}

function columnWidth(key, label) {
  const text = `${key} ${label}`.toLowerCase();
  if (key === "connector_id") return 72;
  if (key === "station_id" || key === "evse_id") return 38;
  if (key === "connector_key") return 32;
  if (key === "last_response_hash") return 68;
  if (text.includes("address")) return 52;
  if (text.includes("station name") || key === "station_name") return 38;
  if (text.includes("description")) return 27;
  if (text.includes("metadata basis")) return 36;
  if (text.includes("connector type group")) return 20;
  if (text.includes("connector type")) return 18;
  if (text.includes("evse name")) return 25;
  if (text.includes("phone")) return 18;
  if (text.includes("id")) return 22;
  if (key.endsWith("_at") || text.includes("observation")) return 21;
  if (text.includes("status")) return 17;
  if (text.includes("hours")) return 16;
  if (text.includes("revenue")) return 20;
  if (text.includes("uncertainty")) return 19;
  return Math.min(20, Math.max(12, label.length + 2));
}

function numberFormatFor(key) {
  const lower = key.toLowerCase();
  if (
    lower.endsWith("_at") ||
    lower === "observation_start" ||
    lower === "observation_end"
  ) return "yyyy-mm-dd hh:mm:ss";
  if (lower.includes("percent") || lower.includes("load_factor")) return "0.0%";
  if (lower.includes("latitude") || lower.includes("longitude")) return "0.000000";
  if (lower.includes("revenue") || lower === "price" || lower.includes("price")) {
    return "#,##0";
  }
  if (
    lower.includes("hours") ||
    lower.includes("kwh") ||
    lower.includes("power_kw")
  ) return "0.00";
  if (lower.includes("minutes")) return "0.0";
  return null;
}

function writeTableSheet(sheet, columns, rows, tableName, options = {}) {
  sheet.showGridLines = false;
  const headers = columns.map((column) => column.label);
  const matrix = [
    headers,
    ...rows.map((row) =>
      columns.map((column) => normalizeValue(column.key, row[column.key])),
    ),
  ];
  const lastColumn = excelColumn(columns.length);
  const lastRow = matrix.length;
  sheet.getRange(`A1:${lastColumn}${lastRow}`).values = matrix;
  const header = sheet.getRange(`A1:${lastColumn}1`);
  header.format = {
    fill: COLORS.navy,
    font: { bold: true, color: COLORS.white },
    wrapText: true,
    verticalAlignment: "center",
  };
  header.format.rowHeight = 32;
  if (rows.length > 0) {
    const table = sheet.tables.add(`A1:${lastColumn}${lastRow}`, true, tableName);
    table.style = "TableStyleMedium2";
    table.showFilterButton = true;
  }
  sheet.freezePanes.freezeRows(1);
  columns.forEach((column, index) => {
    const letter = excelColumn(index + 1);
    sheet.getRange(`${letter}1:${letter}${lastRow}`).format.columnWidth = columnWidth(
      column.key,
      column.label,
    );
    const numberFormat = numberFormatFor(column.key);
    if (numberFormat && lastRow >= 2) {
      sheet.getRange(`${letter}2:${letter}${lastRow}`).format.numberFormat =
        numberFormat;
    }
    if (options.wrapKeys?.includes(column.key) && lastRow >= 2) {
      sheet.getRange(`${letter}2:${letter}${lastRow}`).format.wrapText = true;
    }
  });
  if (options.dataRowHeight && lastRow >= 2) {
    sheet.getRange(`A2:${lastColumn}${lastRow}`).format.rowHeight =
      options.dataRowHeight;
  }
  return { lastColumn, lastRow };
}

function rawColumns(rows) {
  if (!rows.length) return [{ key: "no_rows", label: "No Rows" }];
  return Object.keys(rows[0]).map((key) => ({ key, label: key }));
}

const methodology = sheets.Methodology;
methodology.showGridLines = false;
methodology.getRange("A1:F1").merge();
methodology.getRange("A1").values = [["Team Energy analytics methodology"]];
methodology.getRange("A1:F1").format = {
  fill: COLORS.navy,
  font: { bold: true, color: COLORS.white, size: 18 },
  verticalAlignment: "center",
};
methodology.getRange("A1:F1").format.rowHeight = 34;
methodology.getRange("A3:C7").values = [
  ["Editable assumption", "Value", "Use"],
  [
    "Scenario charging load factor",
    payload.assumptions.scenario_load_factor,
    "Share of rated connector power assumed during a charging interval.",
  ],
  ["Currency", payload.assumptions.currency, "Used for revenue labels."],
  [
    "Provider price interpretation",
    payload.assumptions.price_interpretation,
    "Verify against Team Energy billing terms.",
  ],
  [
    "Revenue availability",
    "Scenario only",
    "The feed has no metered kWh, transaction amount, or payment total.",
  ],
];
methodology.getRange("A3:C3").format = {
  fill: COLORS.teal,
  font: { bold: true, color: COLORS.white },
};
methodology.getRange("B4").format = {
  fill: COLORS.amberLight,
  font: { bold: true, color: COLORS.blue },
  numberFormat: "0%",
};
methodology.getRange("A9:D17").values = [
  ["Metric", "Definition", "Decision use", "Caveat"],
  [
    "Midpoint-estimated status hours",
    "Each transition window is split at its midpoint between the last old-status poll and first new-status poll.",
    "Comparable station utilization without pretending the transition second is exact.",
    "The true transition can be anywhere in the displayed uncertainty window.",
  ],
  [
    "Busy utilization",
    "Midpoint-estimated station busy hours divided by all observed station-status hours.",
    "Find consistently constrained locations.",
    "A station is busy only when no connector is available and at least one is busy or charging.",
  ],
  [
    "Charging connector hours",
    "Midpoint-estimated time for connector intervals whose status is charging.",
    "Revenue-potential input.",
    "Charging time is not the same as delivered energy.",
  ],
  [
    "Rated energy ceiling",
    "Charging connector hours multiplied by rated connector kW.",
    "Upper-bound capacity comparison.",
    "Not metered kWh; assumes full rated power for the entire interval.",
  ],
  [
    "Scenario energy",
    "Rated energy ceiling multiplied by the editable load factor in B4.",
    "Test conservative or aggressive charging-energy scenarios.",
    "A planning scenario, not an observed quantity.",
  ],
  [
    "Scenario revenue",
    "Scenario energy multiplied by the interval price field.",
    "Compare revenue potential under one common assumption.",
    "Not actual revenue; taxes, discounts, sessions, downtime, and payment outcomes are unknown.",
  ],
  [
    "Full-power revenue ceiling",
    "Rated energy ceiling multiplied by the interval price field.",
    "A strict model ceiling for comparison.",
    "Not a realistic forecast unless charging remains at rated power continuously.",
  ],
  [
    "Observation boundary",
    "History begins at the first collector observation and ends at the latest successful poll.",
    "Defines the period covered by every KPI.",
    "Nothing is inferred before collection began.",
  ],
];
methodology.getRange("A9:D9").format = {
  fill: COLORS.teal,
  font: { bold: true, color: COLORS.white },
};
methodology.getRange("A19:D24").values = [
  ["Station status", "Definition", "Source", "Notes"],
  ["available", "At least one connector is available.", "Derived from connectors", ""],
  ["busy", "No connector is available and at least one is busy or charging.", "Derived from connectors", "Used for station busy utilization."],
  ["maintenance", "Every connector is under maintenance.", "Derived from connectors", ""],
  ["unknown", "Any remaining connector-status combination.", "Derived from connectors", "Review manually."],
  ["Metadata basis", "observed_at_interval_start or backfilled_from_current_connector", "Connector history", "Backfilled power/price is less reliable for earlier intervals."],
];
methodology.getRange("A19:D19").format = {
  fill: COLORS.teal,
  font: { bold: true, color: COLORS.white },
};
methodology.getRange("A3:D24").format.wrapText = true;
methodology.getRange("A:A").format.columnWidth = 31;
methodology.getRange("B:B").format.columnWidth = 27;
methodology.getRange("C:C").format.columnWidth = 46;
methodology.getRange("D:D").format.columnWidth = 50;
methodology.getRange("A9:D24").format.rowHeight = 48;
methodology.freezePanes.freezeRows(3);

const stationColumns = [
  ["station_name", "Station Name"],
  ["address", "Address"],
  ["current_status", "Current Status"],
  ["connector_count", "Connectors"],
  ["current_available_connectors", "Available Now"],
  ["observation_start", "Observation Start"],
  ["observation_end", "Observation End"],
  ["observed_station_hours", "Observed Station Hours"],
  ["available_hours", "Available Hours"],
  ["busy_hours", "Busy Hours"],
  ["maintenance_hours", "Maintenance Hours"],
  ["unknown_hours", "Unknown Hours"],
  ["busy_percent", "Busy %"],
  ["availability_percent", "Availability %"],
  ["maintenance_percent", "Maintenance %"],
  ["busy_events", "Busy Intervals"],
  ["charging_connector_hours", "Charging Connector Hours"],
  ["rated_energy_ceiling_kwh", "Rated Energy Ceiling (kWh)"],
  ["energy_weighted_price", "Weighted Price (AMD/kWh)"],
  ["scenario_load_factor", "Scenario Load Factor"],
  ["scenario_energy_kwh", "Scenario Energy (kWh)"],
  ["scenario_revenue_amd", "Scenario Revenue (AMD)"],
  ["full_power_revenue_ceiling_amd", "Full-Power Revenue Ceiling (AMD)"],
  ["charging_events", "Charging Intervals"],
  ["transition_uncertainty_minutes", "Total Transition Uncertainty (min)"],
  ["station_id", "Station ID"],
  ["latitude", "Latitude"],
  ["longitude", "Longitude"],
].map(([key, label]) => ({ key, label }));

const stationRows = payload.station_analytics.map((row) => ({
  ...row,
  observed_station_hours: null,
  busy_percent: null,
  availability_percent: null,
  maintenance_percent: null,
  scenario_load_factor: null,
  scenario_energy_kwh: null,
  scenario_revenue_amd: null,
  full_power_revenue_ceiling_amd: null,
}));
const stationSheet = sheets["Station Analytics"];
const stationRange = writeTableSheet(
  stationSheet,
  stationColumns,
  stationRows,
  "StationAnalyticsTable",
  { wrapKeys: ["station_name", "address"], dataRowHeight: 34 },
);
if (stationRows.length > 0) {
  const last = stationRange.lastRow;
  const formulaColumns = {
    H: "=SUM(I2:L2)",
    M: "=IFERROR(J2/H2,0)",
    N: "=IFERROR(I2/H2,0)",
    O: "=IFERROR(K2/H2,0)",
    T: "='Methodology'!$B$4",
    U: "=R2*T2",
    V: "=U2*S2",
    W: "=R2*S2",
  };
  for (const [column, formula] of Object.entries(formulaColumns)) {
    stationSheet.getRange(`${column}2`).formulas = [[formula]];
    if (last > 2) stationSheet.getRange(`${column}2:${column}${last}`).fillDown();
  }
  stationSheet.getRange(`M2:O${last}`).format.numberFormat = "0.0%";
  stationSheet.getRange(`T2:T${last}`).format.numberFormat = "0%";
  stationSheet.getRange(`V2:W${last}`).format.numberFormat = "#,##0";
  stationSheet.getRange(`C2:C${last}`).conditionalFormats.add("containsText", {
    text: "available",
    format: { fill: COLORS.greenLight, font: { color: "#166534" } },
  });
  stationSheet.getRange(`C2:C${last}`).conditionalFormats.add("containsText", {
    text: "busy",
    format: { fill: COLORS.redLight, font: { color: "#991B1B" } },
  });
  stationSheet.getRange(`C2:C${last}`).conditionalFormats.add("containsText", {
    text: "maintenance",
    format: { fill: COLORS.amberLight, font: { color: "#92400E" } },
  });
  stationSheet.getRange(`M2:M${last}`).conditionalFormats.add("colorScale", {
    colors: [COLORS.greenLight, COLORS.amberLight, COLORS.redLight],
    thresholds: ["min", "50%", "max"],
  });
}

const stationHistoryColumns = [
  ["station_name", "Station Name"],
  ["address", "Address"],
  ["station_id", "Station ID"],
  ["status", "Status"],
  ["first_observed_at", "First Observed"],
  ["last_confirmed_at", "Last Confirmed"],
  ["next_status_observed_at", "Next Status Observed"],
  ["estimated_started_at", "Estimated Start"],
  ["estimated_ended_at", "Estimated End"],
  ["midpoint_estimated_hours", "Midpoint Estimated Hours"],
  ["confirmed_hours_after_first_seen", "Confirmed Hours After First Seen"],
  ["possible_hours_until_next_observation", "Possible Hours Until Next Observation"],
  ["transition_uncertainty_minutes", "Transition Uncertainty (min)"],
  ["is_ongoing", "Ongoing"],
].map(([key, label]) => ({ key, label }));
writeTableSheet(
  sheets["Readable History"],
  stationHistoryColumns,
  payload.station_history,
  "ReadableStationHistoryTable",
  { wrapKeys: ["station_name", "address"], dataRowHeight: 34 },
);

const connectorHistoryColumns = [
  ["station_name", "Station Name"],
  ["address", "Address"],
  ["station_id", "Station ID"],
  ["evse_name", "EVSE Name"],
  ["connector_number", "Connector #"],
  ["connector_type", "Connector Type"],
  ["connector_type_group", "Connector Type Group"],
  ["connector_id", "Connector ID"],
  ["status_code", "Status Code"],
  ["status", "Status"],
  ["status_description", "Status Description"],
  ["power_kw", "Rated Power (kW)"],
  ["price", "Price Field"],
  ["metadata_basis", "Power/Price Metadata Basis"],
  ["first_observed_at", "First Observed"],
  ["last_confirmed_at", "Last Confirmed"],
  ["next_status_observed_at", "Next Status Observed"],
  ["estimated_started_at", "Estimated Start"],
  ["estimated_ended_at", "Estimated End"],
  ["midpoint_estimated_hours", "Midpoint Estimated Hours"],
  ["confirmed_hours_after_first_seen", "Confirmed Hours After First Seen"],
  ["possible_hours_until_next_observation", "Possible Hours Until Next Observation"],
  ["transition_uncertainty_minutes", "Transition Uncertainty (min)"],
  ["is_ongoing", "Ongoing"],
].map(([key, label]) => ({ key, label }));
writeTableSheet(
  sheets["Connector History"],
  connectorHistoryColumns,
  payload.connector_history,
  "ReadableConnectorHistoryTable",
  {
    wrapKeys: ["station_name", "address", "evse_name", "status_description"],
    dataRowHeight: 34,
  },
);

const rawSheetDefinitions = [
  ["Raw Stations", "stations", "RawStationsTable"],
  ["Raw Connectors", "connectors", "RawConnectorsTable"],
  ["Raw Station History", "station_status_intervals", "RawStationHistoryTable"],
  ["Raw Connector History", "connector_status_intervals", "RawConnectorHistoryTable"],
  ["Collector State", "collector_state", "CollectorStateTable"],
];
for (const [sheetName, tableKey, tableName] of rawSheetDefinitions) {
  const rows = payload.raw_tables[tableKey];
  writeTableSheet(sheets[sheetName], rawColumns(rows), rows, tableName);
}

const dashboard = sheets.Dashboard;
dashboard.showGridLines = false;
dashboard.getRange("A1:M1").merge();
dashboard.getRange("A1").values = [["Team Energy station performance dashboard"]];
dashboard.getRange("A1:M1").format = {
  fill: COLORS.navy,
  font: { bold: true, color: COLORS.white, size: 20 },
  verticalAlignment: "center",
};
dashboard.getRange("A1:M1").format.rowHeight = 38;
dashboard.getRange("A2:M2").merge();
dashboard.getRange("A2").values = [[
  "Utilization uses polling-aware midpoint estimates. Revenue is a configurable scenario, not an accounting total.",
]];
dashboard.getRange("A2:M2").format = {
  fill: COLORS.blueLight,
  font: { color: COLORS.navy, italic: true },
  wrapText: true,
};
dashboard.getRange("A2:M2").format.rowHeight = 28;
dashboard.getRange("A4:B4").values = [["Portfolio KPI", "Value"]];
dashboard.getRange("A4:B4").format = {
  fill: COLORS.teal,
  font: { bold: true, color: COLORS.white },
};
dashboard.getRange("A5:A13").values = [
  ["Stations monitored"],
  ["Currently available"],
  ["Currently busy"],
  ["Weighted busy utilization"],
  ["Weighted maintenance share"],
  ["Charging connector hours"],
  ["Scenario revenue (AMD)"],
  ["Full-power revenue ceiling (AMD)"],
  ["Successful polls"],
];
const analyticsLastRow = Math.max(2, stationRows.length + 1);
dashboard.getRange("B5:B13").formulas = [
  [`=COUNTA('Station Analytics'!$Z$2:$Z$${analyticsLastRow})`],
  [`=COUNTIF('Station Analytics'!$C$2:$C$${analyticsLastRow},"available")`],
  [`=COUNTIF('Station Analytics'!$C$2:$C$${analyticsLastRow},"busy")`],
  [`=IFERROR(SUM('Station Analytics'!$J$2:$J$${analyticsLastRow})/SUM('Station Analytics'!$H$2:$H$${analyticsLastRow}),0)`],
  [`=IFERROR(SUM('Station Analytics'!$K$2:$K$${analyticsLastRow})/SUM('Station Analytics'!$H$2:$H$${analyticsLastRow}),0)`],
  [`=SUM('Station Analytics'!$Q$2:$Q$${analyticsLastRow})`],
  [`=SUM('Station Analytics'!$V$2:$V$${analyticsLastRow})`],
  [`=SUM('Station Analytics'!$W$2:$W$${analyticsLastRow})`],
  ["='Collector State'!F2"],
];
dashboard.getRange("A5:B13").format.borders = {
  preset: "inside",
  style: "thin",
  color: COLORS.border,
};
dashboard.getRange("B8:B9").format.numberFormat = "0.0%";
dashboard.getRange("B10").format.numberFormat = "0.00";
dashboard.getRange("B11:B12").format.numberFormat = "#,##0";
dashboard.getRange("D4:F4").merge();
dashboard.getRange("D4").values = [["Coverage and assumptions"]];
dashboard.getRange("D4:F4").format = {
  fill: COLORS.teal,
  font: { bold: true, color: COLORS.white },
};
dashboard.getRange("D5:E11").values = [
  ["Collection begins", asDate(payload.observation_start)],
  ["Latest successful poll", asDate(payload.observation_end)],
  ["Workbook generated", asDate(payload.generated_at)],
  ["Transition estimate", "Midpoint of polling window"],
  ["Scenario load factor", null],
  ["Provider price basis", payload.assumptions.price_interpretation],
  ["Actual revenue in source", "No"],
];
dashboard.getRange("E9").formulas = [["='Methodology'!$B$4"]];
dashboard.getRange("E5:E7").format.numberFormat = "yyyy-mm-dd hh:mm:ss";
dashboard.getRange("E9").format.numberFormat = "0%";
dashboard.getRange("D5:E11").format.wrapText = true;
dashboard.getRange("A17:B17").values = [["Station", "Busy Hours"]];
dashboard.getRange("A17:B17").format = {
  fill: COLORS.teal,
  font: { bold: true, color: COLORS.white },
};
const topCount = Math.min(10, stationRows.length);
if (topCount > 0) {
  const helperFormulas = [];
  for (let index = 0; index < topCount; index += 1) {
    const sourceRow = index + 2;
    helperFormulas.push([
      `='Station Analytics'!A${sourceRow}`,
      `='Station Analytics'!J${sourceRow}`,
    ]);
  }
  dashboard.getRange(`A18:B${17 + topCount}`).formulas = helperFormulas;
  dashboard.getRange(`B18:B${17 + topCount}`).format.numberFormat = "0.00";
  const chart = dashboard.charts.add(
    "bar",
    dashboard.getRange(`A17:B${17 + topCount}`),
  );
  chart.title = "Stations with the most midpoint-estimated busy hours";
  chart.hasLegend = false;
  chart.xAxis = { axisType: "textAxis", textStyle: { fontSize: 9 } };
  chart.yAxis = { numberFormatCode: "0.0" };
  chart.setPosition("D16", "M31");
  if (chart.series.items.length > 0) chart.series.items[0].fill = COLORS.red;
}
dashboard.getRange("A34:M37").merge();
dashboard.getRange("A34").values = [[
  "Decision warning — Scenario revenue is not money collected. The source has status, rated power and a price field, but no metered energy or payment totals. Use it to compare locations under one assumption; use provider billing records for actual revenue. Earlier intervals marked backfilled_from_current_connector use today’s power/price metadata.",
]];
dashboard.getRange("A34:M37").format = {
  fill: COLORS.amberLight,
  font: { bold: true, color: "#7C2D12" },
  wrapText: true,
  verticalAlignment: "center",
  borders: { preset: "outside", style: "medium", color: COLORS.amber },
};
dashboard.getRange("A:A").format.columnWidth = 31;
dashboard.getRange("B:B").format.columnWidth = 18;
dashboard.getRange("C:C").format.columnWidth = 3;
dashboard.getRange("D:D").format.columnWidth = 24;
dashboard.getRange("E:E").format.columnWidth = 31;
dashboard.getRange("F:M").format.columnWidth = 12;
dashboard.freezePanes.freezeRows(2);

if (previewDir) {
  await fs.mkdir(previewDir, { recursive: true });
  const dashboardCheck = await workbook.inspect({
    kind: "table",
    range: "Dashboard!A1:M37",
    include: "values,formulas",
    tableMaxRows: 40,
    tableMaxCols: 13,
    maxChars: 7000,
  });
  const formulaErrors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 300 },
    summary: "final formula error scan",
    maxChars: 5000,
  });
  console.log(`DASHBOARD_CHECK\n${dashboardCheck.ndjson}`);
  console.log(`FORMULA_ERRORS\n${formulaErrors.ndjson}`);

  const previewRanges = {
    Dashboard: "A1:M37",
    "Station Analytics": `A1:AB${Math.min(stationRange.lastRow, 24)}`,
    "Readable History": `A1:N${Math.min(payload.station_history.length + 1, 24)}`,
    "Connector History": `A1:X${Math.min(payload.connector_history.length + 1, 20)}`,
    Methodology: "A1:F24",
    "Raw Stations": `A1:J${Math.min(payload.raw_tables.stations.length + 1, 20)}`,
    "Raw Connectors": `A1:P${Math.min(payload.raw_tables.connectors.length + 1, 16)}`,
    "Raw Station History": `A1:F${Math.min(payload.raw_tables.station_status_intervals.length + 1, 20)}`,
    "Raw Connector History": `A1:N${Math.min(payload.raw_tables.connector_status_intervals.length + 1, 16)}`,
    "Collector State": "A1:G2",
  };
  for (const [sheetName, range] of Object.entries(previewRanges)) {
    const preview = await workbook.render({
      sheetName,
      range,
      scale: 1,
      format: "png",
    });
    const safeName = sheetName.toLowerCase().replaceAll(" ", "_");
    await fs.writeFile(
      path.join(previewDir, `${safeName}.png`),
      new Uint8Array(await preview.arrayBuffer()),
    );
  }
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
