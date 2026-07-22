from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Mapping

BASE_DIR = os.path.dirname(__file__)
ANALYTICS_DB_PATH = (
    os.getenv("NGF_ANALYTICS_DB_PATH", os.path.join(BASE_DIR, "data", "analytics.db")).strip()
    or os.path.join(BASE_DIR, "data", "analytics.db")
)
ANALYTICS_IP_SALT = os.getenv("NGF_ANALYTICS_IP_SALT", "nextgen-analytics-salt").strip() or "nextgen-analytics-salt"

_ANALYTICS_DB_LOCK = threading.RLock()
_ANALYTICS_DB_READY = False


def configure(*, db_path: str | None = None, ip_salt: str | None = None) -> None:
    global ANALYTICS_DB_PATH, ANALYTICS_IP_SALT, _ANALYTICS_DB_READY
    if db_path is not None:
        candidate = str(db_path).strip()
        if candidate:
            ANALYTICS_DB_PATH = candidate
            _ANALYTICS_DB_READY = False
    if ip_salt is not None:
        ANALYTICS_IP_SALT = str(ip_salt).strip() or ANALYTICS_IP_SALT


def hash_ip(ip_address: str) -> str:
    raw = str(ip_address or "").strip()
    if not raw:
        return ""
    digest = hashlib.sha256(f"{ANALYTICS_IP_SALT}|{raw}".encode("utf-8")).hexdigest()
    return digest[:24]


def _normalize_text(value: Any, *, uppercase: bool = False, limit: int = 120) -> str:
    normalized = " ".join(str(value or "").split()).strip()
    if uppercase:
        normalized = normalized.upper()
    if limit > 0:
        normalized = normalized[:limit]
    return normalized


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "")[:120]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _safe_metadata_json(value: Mapping[str, Any] | None) -> str:
    payload = dict(value or {})
    try:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True, default=str)
    except Exception:
        return "{}"


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall() or []
    return {str(row[1]).strip().lower() for row in rows if len(row) >= 2}


def ensure_analytics_db() -> None:
    global _ANALYTICS_DB_READY
    if _ANALYTICS_DB_READY:
        return
    with _ANALYTICS_DB_LOCK:
        if _ANALYTICS_DB_READY:
            return

        db_dir = os.path.dirname(ANALYTICS_DB_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS analytics_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    anon_id TEXT NOT NULL DEFAULT '',
                    account_email TEXT NOT NULL DEFAULT '',
                    ip_hash TEXT NOT NULL DEFAULT '',
                    location_country TEXT NOT NULL DEFAULT '',
                    location_region TEXT NOT NULL DEFAULT '',
                    location_city TEXT NOT NULL DEFAULT '',
                    search_mode TEXT NOT NULL DEFAULT '',
                    origin TEXT NOT NULL DEFAULT '',
                    destination TEXT NOT NULL DEFAULT '',
                    trip_type TEXT NOT NULL DEFAULT '',
                    result_count INTEGER NOT NULL DEFAULT 0,
                    success INTEGER NOT NULL DEFAULT 0,
                    booking_amount REAL,
                    currency TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )

            existing = _table_columns(conn, "analytics_events")
            migrations = {
                "anon_id": "TEXT NOT NULL DEFAULT ''",
                "account_email": "TEXT NOT NULL DEFAULT ''",
                "ip_hash": "TEXT NOT NULL DEFAULT ''",
                "location_country": "TEXT NOT NULL DEFAULT ''",
                "location_region": "TEXT NOT NULL DEFAULT ''",
                "location_city": "TEXT NOT NULL DEFAULT ''",
                "search_mode": "TEXT NOT NULL DEFAULT ''",
                "origin": "TEXT NOT NULL DEFAULT ''",
                "destination": "TEXT NOT NULL DEFAULT ''",
                "trip_type": "TEXT NOT NULL DEFAULT ''",
                "result_count": "INTEGER NOT NULL DEFAULT 0",
                "success": "INTEGER NOT NULL DEFAULT 0",
                "booking_amount": "REAL",
                "currency": "TEXT NOT NULL DEFAULT ''",
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            }
            for column, definition in migrations.items():
                if column in existing:
                    continue
                conn.execute(f"ALTER TABLE analytics_events ADD COLUMN {column} {definition}")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_analytics_events_type_time ON analytics_events(event_type, occurred_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_analytics_events_route_time ON analytics_events(origin, destination, occurred_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_analytics_events_anon_time ON analytics_events(anon_id, occurred_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_analytics_events_country_time ON analytics_events(location_country, occurred_at DESC)"
            )
            conn.commit()

        _ANALYTICS_DB_READY = True


def record_event(
    *,
    event_type: str,
    occurred_at: str | None = None,
    anon_id: str = "",
    account_email: str = "",
    ip_hash: str = "",
    location_country: str = "",
    location_region: str = "",
    location_city: str = "",
    search_mode: str = "",
    origin: str = "",
    destination: str = "",
    trip_type: str = "",
    result_count: int = 0,
    success: bool = False,
    booking_amount: float | int | str | None = None,
    currency: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> None:
    ensure_analytics_db()
    event = _normalize_text(event_type, limit=80)
    if not event:
        return
    occurred = _normalize_text(occurred_at, limit=40) or datetime.utcnow().isoformat(timespec="seconds")
    payload = (
        event,
        occurred,
        _normalize_text(anon_id, limit=64),
        _normalize_email(account_email),
        _normalize_text(ip_hash, limit=64),
        _normalize_text(location_country, uppercase=True, limit=12),
        _normalize_text(location_region, uppercase=True, limit=16),
        _normalize_text(location_city, limit=80),
        _normalize_text(search_mode, limit=24),
        _normalize_text(origin, uppercase=True, limit=8),
        _normalize_text(destination, uppercase=True, limit=8),
        _normalize_text(trip_type, limit=24),
        max(0, _safe_int(result_count, 0)),
        1 if bool(success) else 0,
        _safe_float(booking_amount),
        _normalize_text(currency, uppercase=True, limit=8),
        _safe_metadata_json(metadata),
    )
    with _ANALYTICS_DB_LOCK:
        with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO analytics_events (
                    event_type,
                    occurred_at,
                    anon_id,
                    account_email,
                    ip_hash,
                    location_country,
                    location_region,
                    location_city,
                    search_mode,
                    origin,
                    destination,
                    trip_type,
                    result_count,
                    success,
                    booking_amount,
                    currency,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            conn.commit()


def _since_iso(days: int) -> str:
    safe_days = max(1, min(3650, _safe_int(days, 30)))
    return (datetime.utcnow() - timedelta(days=safe_days)).isoformat(timespec="seconds")


def _fetch_scalar(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(query, params).fetchone()
    if not row:
        return 0
    return _safe_int(row[0], 0)


def fetch_overview(*, days: int = 30) -> dict[str, Any]:
    ensure_analytics_db()
    since = _since_iso(days)
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        total_events = _fetch_scalar(
            conn,
            "SELECT COUNT(*) FROM analytics_events WHERE occurred_at >= ?",
            (since,),
        )
        total_searches = _fetch_scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM analytics_events
            WHERE event_type = 'search_completed'
              AND occurred_at >= ?
            """,
            (since,),
        )
        successful_searches = _fetch_scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM analytics_events
            WHERE event_type = 'search_completed'
              AND success = 1
              AND occurred_at >= ?
            """,
            (since,),
        )
        bookings = _fetch_scalar(
            conn,
            """
            SELECT COUNT(*)
            FROM analytics_events
            WHERE event_type = 'booking_completed'
              AND occurred_at >= ?
            """,
            (since,),
        )
        unique_anon = _fetch_scalar(
            conn,
            """
            SELECT COUNT(DISTINCT anon_id)
            FROM analytics_events
            WHERE anon_id <> ''
              AND occurred_at >= ?
            """,
            (since,),
        )
        signed_in_users = _fetch_scalar(
            conn,
            """
            SELECT COUNT(DISTINCT account_email)
            FROM analytics_events
            WHERE account_email <> ''
              AND occurred_at >= ?
            """,
            (since,),
        )

    conversion = (bookings / successful_searches) if successful_searches > 0 else 0.0
    return {
        "days": max(1, min(3650, _safe_int(days, 30))),
        "total_events": total_events,
        "total_searches": total_searches,
        "successful_searches": successful_searches,
        "bookings": bookings,
        "unique_anonymous_users": unique_anon,
        "signed_in_users": signed_in_users,
        "search_to_booking_rate": round(conversion * 100.0, 2),
    }


def fetch_top_routes(*, days: int = 30, limit: int = 12) -> list[dict[str, Any]]:
    ensure_analytics_db()
    since = _since_iso(days)
    safe_limit = max(1, min(200, _safe_int(limit, 12)))
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                origin,
                destination,
                COUNT(*) AS searches,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successful_searches,
                AVG(CASE WHEN result_count > 0 THEN result_count END) AS avg_results
            FROM analytics_events
            WHERE event_type = 'search_completed'
              AND occurred_at >= ?
              AND origin <> ''
              AND destination <> ''
            GROUP BY origin, destination
            ORDER BY searches DESC, successful_searches DESC, origin ASC, destination ASC
            LIMIT ?
            """,
            (since, safe_limit),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        searches = _safe_int(row["searches"], 0)
        success_count = _safe_int(row["successful_searches"], 0)
        success_rate = (success_count / searches) if searches > 0 else 0.0
        avg_results = row["avg_results"]
        out.append(
            {
                "origin": _normalize_text(row["origin"], uppercase=True, limit=8),
                "destination": _normalize_text(row["destination"], uppercase=True, limit=8),
                "route": f"{_normalize_text(row['origin'], uppercase=True, limit=8)} -> {_normalize_text(row['destination'], uppercase=True, limit=8)}",
                "searches": searches,
                "successful_searches": success_count,
                "success_rate": round(success_rate * 100.0, 2),
                "avg_results": round(float(avg_results), 1) if avg_results is not None else 0.0,
            }
        )
    return out


def fetch_mode_breakdown(*, days: int = 30) -> list[dict[str, Any]]:
    ensure_analytics_db()
    since = _since_iso(days)
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(search_mode, ''), 'unknown') AS mode,
                COUNT(*) AS searches,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successful_searches
            FROM analytics_events
            WHERE event_type = 'search_completed'
              AND occurred_at >= ?
            GROUP BY mode
            ORDER BY searches DESC
            """,
            (since,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        searches = _safe_int(row["searches"], 0)
        success_count = _safe_int(row["successful_searches"], 0)
        success_rate = (success_count / searches) if searches else 0.0
        out.append(
            {
                "mode": _normalize_text(row["mode"], limit=24) or "unknown",
                "searches": searches,
                "successful_searches": success_count,
                "success_rate": round(success_rate * 100.0, 2),
            }
        )
    return out


def fetch_top_routes_by_location(*, days: int = 30, limit: int = 16) -> list[dict[str, Any]]:
    ensure_analytics_db()
    since = _since_iso(days)
    safe_limit = max(1, min(300, _safe_int(limit, 16)))
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                COALESCE(NULLIF(location_country, ''), 'UNKNOWN') AS country,
                origin,
                destination,
                COUNT(*) AS searches
            FROM analytics_events
            WHERE event_type = 'search_completed'
              AND occurred_at >= ?
              AND origin <> ''
              AND destination <> ''
            GROUP BY country, origin, destination
            ORDER BY searches DESC, country ASC
            LIMIT ?
            """,
            (since, safe_limit),
        ).fetchall()
    return [
        {
            "country": _normalize_text(row["country"], uppercase=True, limit=12) or "UNKNOWN",
            "origin": _normalize_text(row["origin"], uppercase=True, limit=8),
            "destination": _normalize_text(row["destination"], uppercase=True, limit=8),
            "route": f"{_normalize_text(row['origin'], uppercase=True, limit=8)} -> {_normalize_text(row['destination'], uppercase=True, limit=8)}",
            "searches": _safe_int(row["searches"], 0),
        }
        for row in rows
    ]


def fetch_popular_routes_for_location(
    *,
    country: str,
    days: int = 90,
    limit: int = 8,
) -> list[dict[str, Any]]:
    ensure_analytics_db()
    safe_country = _normalize_text(country, uppercase=True, limit=12)
    if not safe_country:
        return fetch_top_routes(days=days, limit=limit)
    since = _since_iso(days)
    safe_limit = max(1, min(100, _safe_int(limit, 8)))
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                origin,
                destination,
                COUNT(*) AS searches
            FROM analytics_events
            WHERE event_type = 'search_completed'
              AND occurred_at >= ?
              AND location_country = ?
              AND origin <> ''
              AND destination <> ''
            GROUP BY origin, destination
            ORDER BY searches DESC, origin ASC, destination ASC
            LIMIT ?
            """,
            (since, safe_country, safe_limit),
        ).fetchall()
    return [
        {
            "origin": _normalize_text(row["origin"], uppercase=True, limit=8),
            "destination": _normalize_text(row["destination"], uppercase=True, limit=8),
            "route": f"{_normalize_text(row['origin'], uppercase=True, limit=8)} -> {_normalize_text(row['destination'], uppercase=True, limit=8)}",
            "searches": _safe_int(row["searches"], 0),
        }
        for row in rows
    ]


def fetch_personalization_readiness(*, days: int = 90, limit: int = 40) -> list[dict[str, Any]]:
    ensure_analytics_db()
    since = _since_iso(days)
    safe_limit = max(1, min(500, _safe_int(limit, 40)))
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                anon_id,
                MAX(occurred_at) AS last_seen,
                COUNT(*) AS searches,
                SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS successful_searches,
                SUM(COALESCE(result_count, 0)) AS total_results_seen,
                COALESCE(NULLIF(MAX(location_country), ''), 'UNKNOWN') AS country
            FROM analytics_events
            WHERE event_type = 'search_completed'
              AND occurred_at >= ?
              AND anon_id <> ''
            GROUP BY anon_id
            ORDER BY searches DESC, last_seen DESC
            LIMIT ?
            """,
            (since, safe_limit),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        searches = _safe_int(row["searches"], 0)
        if searches <= 1:
            stage = "cold_start"
            stage_label = "Cold Start"
        elif searches < 4:
            stage = "learning"
            stage_label = "Learning"
        else:
            stage = "personalized_ready"
            stage_label = "Personalized Ready"
        out.append(
            {
                "anon_id": _normalize_text(row["anon_id"], limit=64),
                "country": _normalize_text(row["country"], uppercase=True, limit=12) or "UNKNOWN",
                "searches": searches,
                "successful_searches": _safe_int(row["successful_searches"], 0),
                "total_results_seen": _safe_int(row["total_results_seen"], 0),
                "last_seen": _normalize_text(row["last_seen"], limit=40),
                "stage": stage,
                "stage_label": stage_label,
            }
        )
    return out


def fetch_recent_events(*, limit: int = 120) -> list[dict[str, Any]]:
    ensure_analytics_db()
    safe_limit = max(1, min(1000, _safe_int(limit, 120)))
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                id,
                event_type,
                occurred_at,
                anon_id,
                account_email,
                location_country,
                location_region,
                location_city,
                search_mode,
                origin,
                destination,
                trip_type,
                result_count,
                success,
                booking_amount,
                currency,
                metadata_json
            FROM analytics_events
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        metadata_text = str(row["metadata_json"] or "{}")
        try:
            metadata = json.loads(metadata_text)
            if not isinstance(metadata, dict):
                metadata = {}
        except Exception:
            metadata = {}
        out.append(
            {
                "id": _safe_int(row["id"], 0),
                "event_type": _normalize_text(row["event_type"], limit=80),
                "occurred_at": _normalize_text(row["occurred_at"], limit=40),
                "anon_id": _normalize_text(row["anon_id"], limit=64),
                "account_email": _normalize_email(row["account_email"]),
                "location_country": _normalize_text(row["location_country"], uppercase=True, limit=12),
                "location_region": _normalize_text(row["location_region"], uppercase=True, limit=16),
                "location_city": _normalize_text(row["location_city"], limit=80),
                "search_mode": _normalize_text(row["search_mode"], limit=24),
                "origin": _normalize_text(row["origin"], uppercase=True, limit=8),
                "destination": _normalize_text(row["destination"], uppercase=True, limit=8),
                "trip_type": _normalize_text(row["trip_type"], limit=24),
                "result_count": _safe_int(row["result_count"], 0),
                "success": bool(_safe_int(row["success"], 0)),
                "booking_amount": _safe_float(row["booking_amount"]),
                "currency": _normalize_text(row["currency"], uppercase=True, limit=8),
                "metadata": metadata,
            }
        )
    return out


def clear_events() -> None:
    ensure_analytics_db()
    with _ANALYTICS_DB_LOCK:
        with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
            conn.execute("DELETE FROM analytics_events")
            conn.commit()
