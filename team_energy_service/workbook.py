from __future__ import annotations

import io
from datetime import date, datetime
from typing import Any, Iterable

import xlsxwriter
from xlsxwriter.utility import xl_col_to_name


COLORS = {
    "navy": "#123047",
    "teal": "#087E8B",
    "green": "#0F9D58",
    "green_light": "#DCFCE7",
    "amber": "#F59E0B",
    "amber_light": "#FEF3C7",
    "red": "#DC2626",
    "red_light": "#FEE2E2",
    "blue": "#2563EB",
    "blue_light": "#DBEAFE",
    "gray": "#64748B",
    "gray_light": "#F1F5F9",
    "white": "#FFFFFF",
    "border": "#CBD5E1",
}


def _format_key(key: str) -> str | None:
    lowered = key.lower()
    if (
        lowered.endswith("_at")
        or lowered in {"observation_start", "observation_end"}
    ):
        return "date"
    if "percent" in lowered or "load_factor" in lowered:
        return "percent"
    if "latitude" in lowered or "longitude" in lowered:
        return "coordinate"
    if "revenue" in lowered or "price" in lowered:
        return "currency"
    if "hours" in lowered or "kwh" in lowered or "power_kw" in lowered:
        return "decimal"
    if "minutes" in lowered or "duration_seconds" in lowered:
        return "duration"
    return None


def _column_width(key: str, label: str) -> int:
    text = f"{key} {label}".lower()
    if key == "connector_id":
        return 60
    if key in {"station_id", "evse_id"}:
        return 36
    if key == "gap_id":
        return 68
    if key == "last_response_hash":
        return 64
    if "address" in text:
        return 48
    if "station name" in text or key in {"station_name", "name"}:
        return 36
    if "description" in text:
        return 28
    if key == "metadata_basis" or "metadata basis" in text:
        return 34
    if "reason" in text:
        return 34
    if "connector type group" in text:
        return 20
    if "connector type" in text:
        return 18
    if "evse name" in text:
        return 24
    if "phone" in text:
        return 18
    if key.endswith("_at") or "observation" in text:
        return 21
    if "status" in text:
        return 17
    if "hours" in text or "coverage" in text:
        return 17
    if "revenue" in text:
        return 20
    if "uncertainty" in text:
        return 20
    if "id" in text:
        return 22
    return min(22, max(12, len(label) + 2))


def _raw_columns(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    if not rows:
        return [("no_rows", "No Rows")]
    return [(key, key) for key in rows[0]]


class WorkbookBuilder:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.output = io.BytesIO()
        self.workbook = xlsxwriter.Workbook(
            self.output,
            {
                "in_memory": True,
                "remove_timezone": True,
            },
        )
        self.workbook.set_calc_mode("auto")
        self.workbook.set_properties(
            {
                "title": "Team Energy station analytics",
                "subject": "Charging-station utilization and status history",
                "author": "Toki",
                "comments": (
                    "Revenue fields are planning scenarios, not accounting totals."
                ),
            }
        )
        self.formats = self._create_formats()
        sheet_names = [
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
            "Collector Gaps",
        ]
        self.sheets: dict[str, Any] = {}
        for name in sheet_names:
            sheet = self.workbook.add_worksheet(name)
            sheet.hide_gridlines(2)
            self.sheets[name] = sheet

    def _create_formats(self) -> dict[str, Any]:
        add = self.workbook.add_format
        return {
            "title": add(
                {
                    "bg_color": COLORS["navy"],
                    "font_color": COLORS["white"],
                    "bold": True,
                    "font_size": 20,
                    "valign": "vcenter",
                }
            ),
            "subtitle": add(
                {
                    "bg_color": COLORS["blue_light"],
                    "font_color": COLORS["navy"],
                    "italic": True,
                    "text_wrap": True,
                    "valign": "vcenter",
                }
            ),
            "section": add(
                {
                    "bg_color": COLORS["teal"],
                    "font_color": COLORS["white"],
                    "bold": True,
                    "valign": "vcenter",
                }
            ),
            "table_header": add(
                {
                    "bg_color": COLORS["navy"],
                    "font_color": COLORS["white"],
                    "bold": True,
                    "text_wrap": True,
                    "valign": "vcenter",
                }
            ),
            "date": add({"num_format": "yyyy-mm-dd hh:mm:ss"}),
            "percent": add({"num_format": "0.0%"}),
            "coordinate": add({"num_format": "0.000000"}),
            "currency": add({"num_format": "#,##0"}),
            "decimal": add({"num_format": "0.00"}),
            "duration": add({"num_format": "0.0"}),
            "wrap": add({"text_wrap": True, "valign": "top"}),
            "input": add(
                {
                    "bg_color": COLORS["amber_light"],
                    "font_color": COLORS["blue"],
                    "bold": True,
                    "num_format": "0%",
                }
            ),
            "warning": add(
                {
                    "bg_color": COLORS["amber_light"],
                    "font_color": "#7C2D12",
                    "bold": True,
                    "text_wrap": True,
                    "valign": "vcenter",
                    "border": 2,
                    "border_color": COLORS["amber"],
                }
            ),
            "thin_bottom": add(
                {"bottom": 1, "bottom_color": COLORS["border"]}
            ),
            "status_available": add(
                {"bg_color": COLORS["green_light"], "font_color": "#166534"}
            ),
            "status_busy": add(
                {"bg_color": COLORS["red_light"], "font_color": "#991B1B"}
            ),
            "status_maintenance": add(
                {"bg_color": COLORS["amber_light"], "font_color": "#92400E"}
            ),
        }

    def add_sheet(self, name: str):
        return self.sheets[name]

    def _write_value(self, sheet, row: int, column: int, key: str, value: Any) -> None:
        format_name = _format_key(key)
        cell_format = self.formats.get(format_name) if format_name else None
        if value is None:
            sheet.write_blank(row, column, None, cell_format)
        elif isinstance(value, datetime):
            sheet.write_datetime(row, column, value, cell_format or self.formats["date"])
        elif isinstance(value, date):
            sheet.write_datetime(
                row,
                column,
                datetime.combine(value, datetime.min.time()),
                cell_format or self.formats["date"],
            )
        elif isinstance(value, bool):
            sheet.write_boolean(row, column, value, cell_format)
        elif isinstance(value, (int, float)):
            sheet.write_number(row, column, value, cell_format)
        else:
            sheet.write_string(row, column, str(value), cell_format)

    def write_table_sheet(
        self,
        sheet,
        columns: list[tuple[str, str]],
        rows: list[dict[str, Any]],
        table_name: str,
        *,
        wrap_keys: Iterable[str] = (),
        data_row_height: int | None = None,
    ) -> tuple[int, int]:
        wrap_keys = set(wrap_keys)
        for row_index, source in enumerate(rows, start=1):
            for column_index, (key, _) in enumerate(columns):
                self._write_value(
                    sheet,
                    row_index,
                    column_index,
                    key,
                    source.get(key),
                )
                if key in wrap_keys:
                    sheet.set_row(row_index, data_row_height or 30)

        if rows:
            sheet.add_table(
                0,
                0,
                len(rows),
                len(columns) - 1,
                {
                    "name": table_name,
                    "style": "Table Style Medium 2",
                    "columns": [
                        {
                            "header": label,
                            "header_format": self.formats["table_header"],
                        }
                        for _, label in columns
                    ],
                },
            )
        else:
            for column_index, (_, label) in enumerate(columns):
                sheet.write(0, column_index, label, self.formats["table_header"])

        sheet.set_row(0, 32)
        for column_index, (key, label) in enumerate(columns):
            sheet.set_column(
                column_index,
                column_index,
                _column_width(key, label),
                self.formats["wrap"] if key in wrap_keys else None,
            )
        if data_row_height:
            for row_index in range(1, len(rows) + 1):
                sheet.set_row(row_index, data_row_height)
        sheet.freeze_panes(1, 0)
        return len(rows) + 1, len(columns)

    def build_methodology(self) -> None:
        sheet = self.add_sheet("Methodology")
        sheet.merge_range("A1:F1", "Team Energy analytics methodology", self.formats["title"])
        sheet.set_row(0, 34)

        assumption_rows = [
            ["Editable assumption", "Value", "Use"],
            [
                "Scenario charging load factor",
                self.payload["assumptions"]["scenario_load_factor"],
                "Share of rated connector power assumed during a charging interval.",
            ],
            ["Currency", self.payload["assumptions"]["currency"], "Used for revenue labels."],
            [
                "Collector gap threshold (seconds)",
                self.payload["assumptions"]["gap_threshold_seconds"],
                "A longer interval between successful polls becomes unknown.",
            ],
            [
                "Provider price interpretation",
                self.payload["assumptions"]["price_interpretation"],
                "Verify against Team Energy billing terms.",
            ],
            [
                "Revenue availability",
                "Scenario only",
                "The feed has no metered kWh, transaction amount, or payment total.",
            ],
        ]
        for row_index, row in enumerate(assumption_rows, start=2):
            for column_index, value in enumerate(row):
                cell_format = self.formats["section"] if row_index == 2 else None
                sheet.write(row_index, column_index, value, cell_format)
        sheet.write_number(3, 1, assumption_rows[1][1], self.formats["input"])
        sheet.write_comment(
            3,
            1,
            "Editable planning assumption. Revenue outputs are not actual receipts.",
            {"author": "Toki"},
        )

        methodology_rows = [
            ["Metric", "Definition", "Decision use", "Caveat"],
            [
                "Midpoint-estimated status hours",
                "Each observed transition window is split at its midpoint between the last old-status poll and first new-status poll.",
                "Compare station utilization without claiming an exact transition second.",
                "The true transition can be anywhere in the displayed uncertainty window.",
            ],
            [
                "Busy utilization",
                "Busy station hours divided by hours with a known station status.",
                "Find consistently constrained locations without treating outages as demand.",
                "Unknown collector-gap time is excluded from the denominator.",
            ],
            [
                "Observed coverage",
                "Known station-status hours divided by the entire collected time window.",
                "Identify whether a utilization result has enough observation coverage.",
                "Low coverage means utilization should be interpreted cautiously.",
            ],
            [
                "Collector gap",
                "A period longer than the configured threshold between successful polls.",
                "Make cloud restarts and upstream outages visible in analytics.",
                "The whole unobserved span is marked unknown; no status is guessed.",
            ],
            [
                "Charging connector hours",
                "Midpoint-estimated connector time explicitly marked charging.",
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
                "Define the period covered by every KPI.",
                "Nothing is inferred before collection began.",
            ],
        ]
        for row_index, row in enumerate(methodology_rows, start=8):
            for column_index, value in enumerate(row):
                sheet.write(
                    row_index,
                    column_index,
                    value,
                    self.formats["section"] if row_index == 8 else self.formats["wrap"],
                )
            if row_index > 8:
                sheet.set_row(row_index, 50)

        status_start = 21
        status_rows = [
            ["Station status", "Definition", "Source", "Notes"],
            ["available", "At least one connector is available.", "Derived from connectors", ""],
            ["busy", "No connector is available and at least one is busy or charging.", "Derived from connectors", "Used for busy utilization."],
            ["maintenance", "Every connector is under maintenance.", "Derived from connectors", ""],
            ["unknown", "No reliable status observation exists.", "Provider or collector gap", "Excluded from utilization percentages."],
            ["Metadata basis", "observed_at_interval_start, backfilled_from_current_connector, or automatic_collector_gap", "Connector history", "Backfilled power/price is less reliable for earlier intervals."],
        ]
        for row_index, row in enumerate(status_rows, start=status_start - 1):
            for column_index, value in enumerate(row):
                sheet.write(
                    row_index,
                    column_index,
                    value,
                    self.formats["section"]
                    if row_index == status_start - 1
                    else self.formats["wrap"],
                )
            if row_index >= status_start:
                sheet.set_row(row_index, 44)

        sheet.set_column("A:A", 31)
        sheet.set_column("B:B", 31)
        sheet.set_column("C:C", 48)
        sheet.set_column("D:D", 50)
        sheet.freeze_panes(3, 0)

    def build_station_analytics(self) -> None:
        columns = [
            ("station_name", "Station Name"),
            ("address", "Address"),
            ("current_status", "Current Status"),
            ("connector_count", "Connectors"),
            ("current_available_connectors", "Available Now"),
            ("observation_start", "Observation Start"),
            ("observation_end", "Observation End"),
            ("observed_station_hours", "Observed Station Hours"),
            ("available_hours", "Available Hours"),
            ("busy_hours", "Busy Hours"),
            ("maintenance_hours", "Maintenance Hours"),
            ("unknown_hours", "Unknown Hours"),
            ("known_station_hours", "Known Station Hours"),
            ("coverage_percent", "Coverage %"),
            ("busy_percent", "Busy %"),
            ("availability_percent", "Availability %"),
            ("maintenance_percent", "Maintenance %"),
            ("busy_events", "Busy Intervals"),
            ("charging_connector_hours", "Charging Connector Hours"),
            ("rated_energy_ceiling_kwh", "Rated Energy Ceiling (kWh)"),
            ("energy_weighted_price", "Weighted Price (AMD/kWh)"),
            ("scenario_load_factor", "Scenario Load Factor"),
            ("scenario_energy_kwh", "Scenario Energy (kWh)"),
            ("scenario_revenue_amd", "Scenario Revenue (AMD)"),
            ("rated_power_revenue_ceiling_amd", "Full-Power Revenue Ceiling (AMD)"),
            ("charging_events", "Charging Intervals"),
            ("transition_uncertainty_minutes", "Total Transition Uncertainty (min)"),
            ("station_id", "Station ID"),
            ("latitude", "Latitude"),
            ("longitude", "Longitude"),
        ]
        rows = self.payload["station_analytics"]
        sheet = self.add_sheet("Station Analytics")
        self.write_table_sheet(
            sheet,
            columns,
            rows,
            "StationAnalyticsTable",
            wrap_keys={"station_name", "address"},
            data_row_height=34,
        )
        for row_index, row in enumerate(rows, start=1):
            excel_row = row_index + 1
            formulas = {
                7: (f"=SUM(I{excel_row}:L{excel_row})", row["observed_station_hours"]),
                12: (f"=SUM(I{excel_row}:K{excel_row})", row["known_station_hours"]),
                13: (f"=IFERROR(M{excel_row}/H{excel_row},0)", row["coverage_percent"]),
                14: (f"=IFERROR(J{excel_row}/M{excel_row},0)", row["busy_percent"]),
                15: (f"=IFERROR(I{excel_row}/M{excel_row},0)", row["availability_percent"]),
                16: (f"=IFERROR(K{excel_row}/M{excel_row},0)", row["maintenance_percent"]),
                21: ("='Methodology'!$B$4", row["scenario_load_factor"]),
                22: (f"=T{excel_row}*V{excel_row}", row["scenario_energy_kwh"]),
                23: (f"=W{excel_row}*U{excel_row}", row["scenario_revenue_amd"]),
                24: (f"=T{excel_row}*U{excel_row}", row["rated_power_revenue_ceiling_amd"]),
            }
            for column_index, (formula, cached_value) in formulas.items():
                key = columns[column_index][0]
                cell_format = self.formats.get(_format_key(key))
                sheet.write_formula(
                    row_index,
                    column_index,
                    formula,
                    cell_format,
                    cached_value,
                )

        if rows:
            last_row = len(rows)
            sheet.conditional_format(
                1,
                2,
                last_row,
                2,
                {
                    "type": "text",
                    "criteria": "containing",
                    "value": "available",
                    "format": self.formats["status_available"],
                },
            )
            sheet.conditional_format(
                1,
                2,
                last_row,
                2,
                {
                    "type": "text",
                    "criteria": "containing",
                    "value": "busy",
                    "format": self.formats["status_busy"],
                },
            )
            sheet.conditional_format(
                1,
                2,
                last_row,
                2,
                {
                    "type": "text",
                    "criteria": "containing",
                    "value": "maintenance",
                    "format": self.formats["status_maintenance"],
                },
            )
            sheet.conditional_format(
                1,
                14,
                last_row,
                14,
                {
                    "type": "3_color_scale",
                    "min_color": COLORS["green_light"],
                    "mid_color": COLORS["amber_light"],
                    "max_color": COLORS["red_light"],
                },
            )

    def build_history_sheets(self) -> None:
        station_columns = [
            ("station_name", "Station Name"),
            ("address", "Address"),
            ("station_id", "Station ID"),
            ("status", "Status"),
            ("first_observed_at", "First Observed"),
            ("last_confirmed_at", "Last Confirmed"),
            ("next_status_observed_at", "Next Status Observed"),
            ("estimated_started_at", "Estimated Start"),
            ("estimated_ended_at", "Estimated End"),
            ("midpoint_estimated_hours", "Midpoint Estimated Hours"),
            ("confirmed_hours_after_first_seen", "Confirmed Hours After First Seen"),
            ("possible_hours_until_next_observation", "Possible Hours Until Next Observation"),
            ("transition_uncertainty_minutes", "Transition Uncertainty (min)"),
            ("is_ongoing", "Ongoing"),
        ]
        station_sheet = self.add_sheet("Readable History")
        self.write_table_sheet(
            station_sheet,
            station_columns,
            self.payload["station_history"],
            "ReadableStationHistoryTable",
            wrap_keys={"station_name", "address"},
            data_row_height=34,
        )

        connector_columns = [
            ("station_name", "Station Name"),
            ("address", "Address"),
            ("station_id", "Station ID"),
            ("evse_name", "EVSE Name"),
            ("connector_number", "Connector #"),
            ("connector_type", "Connector Type"),
            ("connector_type_group", "Connector Type Group"),
            ("connector_id", "Connector ID"),
            ("status_code", "Status Code"),
            ("status", "Status"),
            ("status_description", "Status Description"),
            ("power_kw", "Rated Power (kW)"),
            ("price", "Price Field"),
            ("metadata_basis", "Power/Price Metadata Basis"),
            ("first_observed_at", "First Observed"),
            ("last_confirmed_at", "Last Confirmed"),
            ("next_status_observed_at", "Next Status Observed"),
            ("estimated_started_at", "Estimated Start"),
            ("estimated_ended_at", "Estimated End"),
            ("midpoint_estimated_hours", "Midpoint Estimated Hours"),
            ("confirmed_hours_after_first_seen", "Confirmed Hours After First Seen"),
            ("possible_hours_until_next_observation", "Possible Hours Until Next Observation"),
            ("transition_uncertainty_minutes", "Transition Uncertainty (min)"),
            ("is_ongoing", "Ongoing"),
        ]
        connector_sheet = self.add_sheet("Connector History")
        self.write_table_sheet(
            connector_sheet,
            connector_columns,
            self.payload["connector_history"],
            "ReadableConnectorHistoryTable",
            wrap_keys={
                "station_name",
                "address",
                "evse_name",
                "status_description",
            },
            data_row_height=34,
        )

    def build_raw_sheets(self) -> None:
        definitions = [
            ("Raw Stations", "stations", "RawStationsTable"),
            ("Raw Connectors", "connectors", "RawConnectorsTable"),
            ("Raw Station History", "station_status_intervals", "RawStationHistoryTable"),
            ("Raw Connector History", "connector_status_intervals", "RawConnectorHistoryTable"),
            ("Collector State", "collector_state", "CollectorStateTable"),
            ("Collector Gaps", "collector_gaps", "CollectorGapsTable"),
        ]
        for sheet_name, table_key, table_name in definitions:
            rows = self.payload["raw_tables"][table_key]
            sheet = self.add_sheet(sheet_name)
            self.write_table_sheet(
                sheet,
                _raw_columns(rows),
                rows,
                table_name,
            )

    def build_dashboard(self) -> None:
        sheet = self.add_sheet("Dashboard")
        sheet.merge_range(
            "A1:M1",
            "Team Energy station performance dashboard",
            self.formats["title"],
        )
        sheet.set_row(0, 38)
        sheet.merge_range(
            "A2:M2",
            "Utilization uses polling-aware midpoint estimates and excludes collector gaps. Revenue is a configurable scenario, not an accounting total.",
            self.formats["subtitle"],
        )
        sheet.set_row(1, 28)

        sheet.write_row("A4", ["Portfolio KPI", "Value"], self.formats["section"])
        labels = [
            "Stations monitored",
            "Currently available",
            "Currently busy",
            "Weighted busy utilization",
            "Observed coverage",
            "Weighted maintenance share",
            "Charging connector hours",
            "Scenario revenue (AMD)",
            "Full-power revenue ceiling (AMD)",
            "Successful polls",
        ]
        for index, label in enumerate(labels, start=4):
            sheet.write(index, 0, label, self.formats["thin_bottom"])

        analytics_last_row = max(2, len(self.payload["station_analytics"]) + 1)
        formulas = [
            (f"=COUNTA('Station Analytics'!$AB$2:$AB${analytics_last_row})", len(self.payload["station_analytics"])),
            (f'=COUNTIF(\'Station Analytics\'!$C$2:$C${analytics_last_row},"available")', sum(row.get("current_status") == "available" for row in self.payload["station_analytics"])),
            (f'=COUNTIF(\'Station Analytics\'!$C$2:$C${analytics_last_row},"busy")', sum(row.get("current_status") == "busy" for row in self.payload["station_analytics"])),
            (f"=IFERROR(SUM('Station Analytics'!$J$2:$J${analytics_last_row})/SUM('Station Analytics'!$M$2:$M${analytics_last_row}),0)", self._portfolio_ratio("busy_hours", "known_station_hours")),
            (f"=IFERROR(SUM('Station Analytics'!$M$2:$M${analytics_last_row})/SUM('Station Analytics'!$H$2:$H${analytics_last_row}),0)", self._portfolio_ratio("known_station_hours", "observed_station_hours")),
            (f"=IFERROR(SUM('Station Analytics'!$K$2:$K${analytics_last_row})/SUM('Station Analytics'!$M$2:$M${analytics_last_row}),0)", self._portfolio_ratio("maintenance_hours", "known_station_hours")),
            (f"=SUM('Station Analytics'!$S$2:$S${analytics_last_row})", self._portfolio_sum("charging_connector_hours")),
            (f"=SUM('Station Analytics'!$X$2:$X${analytics_last_row})", self._portfolio_sum("scenario_revenue_amd")),
            (f"=SUM('Station Analytics'!$Y$2:$Y${analytics_last_row})", self._portfolio_sum("rated_power_revenue_ceiling_amd")),
            ("='Collector State'!F2", self._collector_poll_count()),
        ]
        for row_index, (formula, cached_value) in enumerate(formulas, start=4):
            cell_format = None
            if row_index in {7, 8, 9}:
                cell_format = self.formats["percent"]
            elif row_index in {10}:
                cell_format = self.formats["decimal"]
            elif row_index in {11, 12}:
                cell_format = self.formats["currency"]
            sheet.write_formula(row_index, 1, formula, cell_format, cached_value)

        sheet.merge_range("D4:F4", "Coverage and assumptions", self.formats["section"])
        coverage_rows = [
            ["Collection begins", self.payload.get("observation_start")],
            ["Latest successful poll", self.payload.get("observation_end")],
            ["Workbook generated", self.payload.get("generated_at")],
            ["Transition estimate", "Midpoint of polling window"],
            [
                "Collector gap rule",
                (
                    "More than "
                    f"{self.payload['assumptions']['gap_threshold_seconds']:g} "
                    "seconds becomes unknown"
                ),
            ],
            ["Scenario load factor", None],
            ["Provider price basis", self.payload["assumptions"]["price_interpretation"]],
            ["Actual revenue in source", "No"],
        ]
        for row_index, row in enumerate(coverage_rows, start=4):
            sheet.write(row_index, 3, row[0])
            self._write_value(sheet, row_index, 4, "observation_end", row[1])
        sheet.write_formula(
            9,
            4,
            "='Methodology'!$B$4",
            self.formats["percent"],
            self.payload["assumptions"]["scenario_load_factor"],
        )
        sheet.set_column("D:D", 24)
        sheet.set_column("E:E", 35)

        sheet.write_row("A17", ["Station", "Busy Hours"], self.formats["section"])
        top_rows = self.payload["station_analytics"][:10]
        for index, station in enumerate(top_rows, start=17):
            source_row = index - 15
            sheet.write_formula(
                index,
                0,
                f"='Station Analytics'!A{source_row}",
                None,
                station["station_name"],
            )
            sheet.write_formula(
                index,
                1,
                f"='Station Analytics'!J{source_row}",
                self.formats["decimal"],
                station["busy_hours"],
            )

        if top_rows:
            chart = self.workbook.add_chart({"type": "column"})
            final_helper_row = 17 + len(top_rows)
            chart.add_series(
                {
                    "name": "Busy Hours",
                    "categories": f"=Dashboard!$A$18:$A${final_helper_row}",
                    "values": f"=Dashboard!$B$18:$B${final_helper_row}",
                    "fill": {"color": COLORS["red"]},
                    "border": {"none": True},
                }
            )
            chart.set_title(
                {"name": "Stations with the most midpoint-estimated busy hours"}
            )
            chart.set_legend({"none": True})
            chart.set_y_axis({"num_format": "0.00", "major_gridlines": {"visible": True}})
            chart.set_x_axis({"label_position": "low"})
            chart.set_style(10)
            sheet.insert_chart("D16", chart, {"x_scale": 1.75, "y_scale": 1.45})

        sheet.merge_range(
            "A34:M37",
            "Decision warning — Scenario revenue is not money collected. The source has status, rated power and a price field, but no metered energy or payment totals. Use it to compare locations under one assumption; use provider billing records for actual revenue. Collector gaps are recorded as unknown and excluded from utilization percentages.",
            self.formats["warning"],
        )
        sheet.set_column("A:A", 31)
        sheet.set_column("B:B", 18)
        sheet.set_column("C:C", 3)
        sheet.set_column("F:M", 12)
        sheet.freeze_panes(2, 0)

    def _portfolio_sum(self, key: str) -> float:
        return sum(float(row.get(key) or 0) for row in self.payload["station_analytics"])

    def _portfolio_ratio(self, numerator: str, denominator: str) -> float:
        denominator_value = self._portfolio_sum(denominator)
        return (
            self._portfolio_sum(numerator) / denominator_value
            if denominator_value
            else 0.0
        )

    def _collector_poll_count(self) -> int:
        rows = self.payload["raw_tables"]["collector_state"]
        return int(rows[0].get("poll_count") or 0) if rows else 0

    def build(self) -> bytes:
        self.build_methodology()
        self.build_station_analytics()
        self.build_history_sheets()
        self.build_raw_sheets()
        self.build_dashboard()
        self.sheets["Dashboard"].activate()
        self.workbook.close()
        return self.output.getvalue()


def build_analytics_workbook(payload: dict[str, Any]) -> bytes:
    """Create the portable analytical workbook entirely in Python."""
    return WorkbookBuilder(payload).build()
