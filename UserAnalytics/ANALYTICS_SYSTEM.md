# Skairova Analytics System

This project now includes a backend-only analytics pipeline plus a separate internal dashboard app.

## What Is Captured

- `search_completed` from:
  - `/search`
  - `/search/stream`
  - `/search/flex-stream`
- `airport_suggestions_served` from `/airports`
- `ai_parse_preview` from `/search/ai-parse-preview`
- `booking_completed` after successful Duffel order creation
- `account_signup` and `account_login`

Each event stores:

- Anonymous profile ID (`anon_id`)
- Optional signed-in account email
- Hashed IP (`ip_hash`)
- Coarse location (`country`, `region`, `city`) when headers are available
- Route/mode/trip details
- Result counts, success flag, optional metadata JSON

## Datastore

- SQLite database path:
  - `NGF_ANALYTICS_DB_PATH`
  - Default: `data/analytics.db`

## Run Main Platform (existing app)

```bash
./venv/bin/flask --app app run
```

## Run Internal Analytics Dashboard (separate app)

```bash
./venv/bin/flask --app analytics_app run --port 5010
```

Open:

- `http://127.0.0.1:5010/`

## Optional Security

Set `NGF_ANALYTICS_DASHBOARD_TOKEN` to require:

- Query param: `?token=...`
- Or header: `X-Analytics-Token: ...`

## Useful APIs

- Main app: `GET /internal/analytics/popular-routes`
  - Query params: `country`, `days`, `limit`
- Dashboard app:
  - `GET /api/overview`
  - `GET /api/popular-near`
  - `GET /api/recent-events`

## Test Coverage

`tests/test_analytics_system.py` validates:

- search event capture
- signup event capture
- dashboard overview route reads collected top routes
