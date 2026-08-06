# Toki — Team Energy history API

This is a small private service that:

- logs in through Team Energy's anonymous guest flow;
- polls the full station snapshot on a configurable interval;
- keeps current station and connector state in DuckDB;
- stores a new history interval only when a status changes;
- creates a decision-friendly Excel dashboard with station names and utilization;
- exposes the current state and history through FastAPI.

No phone, simulator, certificate, or captured token is required.

When Telegram variables are configured, the same process also listens for bot
commands and posts a scheduled report every five hours.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m team_energy_service
```

Open:

- API documentation: <http://127.0.0.1:8010/docs>
- Interactive analytics map: <http://127.0.0.1:8010/dashboard>
- Health: <http://127.0.0.1:8010/health>
- Current summary: <http://127.0.0.1:8010/summary>
- Stations: <http://127.0.0.1:8010/stations>

The first Team Energy request runs immediately when the service starts. Until
it finishes, `/health` reports `initializing`. If web credentials are set,
every route except `/healthz` uses HTTP Basic authentication.

## Configuration

Environment variables and defaults:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `TEAM_ENERGY_DB_PATH` | `./data/team_energy.duckdb` | Persistent DuckDB file |
| `TEAM_ENERGY_POLL_INTERVAL_SECONDS` | `30` | Delay between successful polls |
| `TEAM_ENERGY_STALE_AFTER_SECONDS` | `120` | Age after which data is marked stale |
| `TEAM_ENERGY_GAP_THRESHOLD_SECONDS` | `120` | Longer time between successful polls becomes an unknown interval |
| `TEAM_ENERGY_REQUEST_TIMEOUT_SECONDS` | `30` | Upstream HTTP timeout |
| `TEAM_ENERGY_HOST` | `127.0.0.1` | Local bind host |
| `TEAM_ENERGY_PORT` | `8010` | Local port (`PORT` takes precedence) |
| `TEAM_ENERGY_WEB_USERNAME` | empty | Optional HTTP Basic-auth username |
| `TEAM_ENERGY_WEB_PASSWORD` | empty | Optional HTTP Basic-auth password; set with the username |
| `TELEGRAM_BOT_TOKEN` | empty | Secret token issued by `@BotFather` |
| `TELEGRAM_CHAT_ID` | empty | Numeric destination group ID |
| `TELEGRAM_REPORT_INTERVAL_HOURS` | `5` | Scheduled report interval |
| `TELEGRAM_TIMEZONE` | `Asia/Yerevan` | Report timestamp timezone |

For example, a ten-second local test:

```bash
TEAM_ENERGY_POLL_INTERVAL_SECONDS=10 python -m team_energy_service
```

Port 8010 is the project default because port 8000 was already occupied during
local verification. You can still choose another port:

```bash
TEAM_ENERGY_PORT=8020 python -m team_energy_service
```

Keep the production/private-host interval at 30 seconds or more unless Team
Energy authorizes a higher request frequency.

## API

```text
GET /health
GET /healthz
GET /summary
GET /analytics/map?hours=24
GET /collector/gaps?hours=720
GET /stations
GET /stations?status=available
GET /stations?q=Komitas
GET /stations/{station_id}
GET /stations/{station_id}/history?hours=24
GET /connectors/{connector_id}/history?hours=24
GET /telegram/status
```

The dashboard provides a polling-aware geographic utilization view with:

- 0.025° busy-section intensity cells and current-status station markers;
- selectable busy hours, busy utilization, charging time, and revenue scenario;
- one-hour through all-history filters, status filtering, and station search;
- station popups, ranked comparisons, and a sortable operational detail view.

The page refreshes automatically every minute. Its map background uses
OpenStreetMap tiles, so the browser needs internet access even though the
analytics API and database run locally.

Telegram group commands:

```text
/report
/status
/excel
/help
```

`/excel` sends one ZIP archive containing:

- an Excel workbook with a dashboard, station-level analytics, station names,
  readable interval history, revenue-scenario formulas, methodology, and raw
  data sheets;
- decision-ready CSV files for station analytics and readable station/connector
  history;
- every original DuckDB table as an individual UTF-8 CSV file.

The CSV BOM makes Armenian text display correctly in Microsoft Excel. The
workbook is created with XlsxWriter and has no Node.js runtime dependency.

Station history is derived from connector state:

- `available`: at least one connector is available;
- `busy`: none are available and at least one is busy or charging;
- `maintenance`: every connector is under maintenance;
- `unknown`: any other combination.

For unchanged polls, no new history interval is written. When a transition is
observed, the old interval records both the last poll that confirmed the old
status and the first poll that observed the new status. This preserves the
uncertainty window instead of pretending the exact transition time is known.
The analytical workbook splits that uncertainty window at its midpoint and
shows the raw bounds beside the estimate. When successful polls are more than
the configured gap threshold apart, the service closes the prior intervals,
records the unobserved span as `unknown`, and begins fresh intervals at the
recovery poll. Busy and availability percentages use known-status time only;
coverage shows how much of the complete collection window is known.

Revenue fields are explicitly planning scenarios. The source does not provide
metered kWh, transaction totals, or payments. The model therefore uses only
connector intervals marked `charging`, rated connector power, the provider
price field, and an editable load-factor assumption. Use billing records—not
this status feed—for actual revenue.

## Tests

```bash
pytest
```

The DuckDB file is intentionally ignored by Git. When this is deployed, mount
persistent storage at the directory containing the configured database path.

## Railway deployment

The production service uses one Amsterdam replica and a persistent volume at
`/data`. Railway provides `PORT`; do not define it yourself. Required service
variables are:

```text
TEAM_ENERGY_DB_PATH=/data/team_energy.duckdb
TEAM_ENERGY_HOST=0.0.0.0
TEAM_ENERGY_POLL_INTERVAL_SECONDS=30
TEAM_ENERGY_STALE_AFTER_SECONDS=120
TEAM_ENERGY_GAP_THRESHOLD_SECONDS=120
TEAM_ENERGY_REQUEST_TIMEOUT_SECONDS=30
TEAM_ENERGY_WEB_USERNAME=toki
TEAM_ENERGY_WEB_PASSWORD=<secret>
TELEGRAM_BOT_TOKEN=<secret>
TELEGRAM_CHAT_ID=<group id>
TELEGRAM_REPORT_INTERVAL_HOURS=5
TELEGRAM_TIMEZONE=Asia/Yerevan
```

Deploy future local changes from the repository root with:

```bash
railway up --service Toki
```

Only `/healthz` is public. The dashboard, API, docs, static files, detailed
health, and collector-gap audit endpoint require the shared web credentials.
Keep one active service instance so Telegram long polling is not duplicated.
