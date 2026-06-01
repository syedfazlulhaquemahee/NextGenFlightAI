from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import email_service

BASE_DIR = os.path.dirname(__file__)
ANALYTICS_DB_PATH = (
    os.getenv("NGF_ANALYTICS_DB_PATH", os.path.join(BASE_DIR, "data", "analytics.db")).strip()
    or os.path.join(BASE_DIR, "data", "analytics.db")
)
ANALYTICS_IP_SALT = os.getenv("NGF_ANALYTICS_IP_SALT", "nextgen-analytics-salt").strip() or "nextgen-analytics-salt"

_ANALYTICS_DB_LOCK = threading.RLock()
_ANALYTICS_DB_READY = False
_ACCOUNTS_DB_LOCK = threading.RLock()
_EST_TZ = ZoneInfo("America/New_York")
_RESET_CODE_SALT = (
    os.getenv("NGF_RESET_CODE_SALT", os.getenv("FLASK_SECRET_KEY", "ngf-reset-code-salt")).strip()
    or "ngf-reset-code-salt"
)


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


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _parse_utc_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        # Stored values are usually naive UTC ISO strings.
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def to_est_display(value: Any) -> str:
    dt = _parse_utc_datetime(value)
    if not dt:
        return str(value or "")
    return dt.astimezone(_EST_TZ).strftime("%Y-%m-%d %H:%M %Z")


def _est_day_key(value: Any) -> str:
    dt = _parse_utc_datetime(value)
    if not dt:
        return ""
    return dt.astimezone(_EST_TZ).date().isoformat()


def _anon_label(value: Any) -> str:
    anon_id = _normalize_text(value, limit=64)
    if not anon_id:
        return ""
    digest = hashlib.sha1(anon_id.encode("utf-8")).hexdigest().upper()[:8]
    return f"ANON-{digest}"


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


def _metadata_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raw = str(value or "{}")
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall() or []
    return {str(row[1]).strip().lower() for row in rows if len(row) >= 2}


def _search_identity_expr(prefix: str = "") -> str:
    p = f"{prefix}." if prefix else ""
    return f"COALESCE(NULLIF({p}search_id, ''), 'row:' || {p}id)"


def _user_identity_expr(prefix: str = "") -> str:
    p = f"{prefix}." if prefix else ""
    return (
        "CASE "
        f"WHEN NULLIF({p}account_email, '') IS NOT NULL THEN 'acct:' || lower({p}account_email) "
        f"WHEN NULLIF({p}anon_id, '') IS NOT NULL THEN 'anon:' || {p}anon_id "
        f"WHEN NULLIF({p}ip_hash, '') IS NOT NULL THEN 'ip:' || {p}ip_hash "
        f"WHEN NULLIF({p}search_id, '') IS NOT NULL THEN 'search:' || {p}search_id "
        f"ELSE 'row:' || {p}id END"
    )


def _normalize_event_label(event_type: Any) -> str:
    event = _normalize_text(event_type, limit=80).lower()
    labels = {
        "site_landed": "Landed",
        "search_completed": "Searched",
        "results_updated": "Updated results",
        "results_viewed": "Viewed results",
        "flight_selected": "Clicked flight",
        "booking_intent": "Booking intent",
        "booking_completed": "Booked",
    }
    return labels.get(event, event.replace("_", " ").title() or "Event")


def _timeline_price(metadata: Mapping[str, Any]) -> float | None:
    for key in ("price", "total_amount", "booking_amount"):
        value = _safe_float(metadata.get(key))
        if value is not None:
            return value
    return None


def _normalize_reset_code(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:6]


def _hash_reset_code(email: str, code: str) -> str:
    normalized_email = _normalize_email(email)
    normalized_code = _normalize_reset_code(code)
    return hashlib.sha256(
        f"{_RESET_CODE_SALT}|{normalized_email}|{normalized_code}".encode("utf-8")
    ).hexdigest()


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
                    search_id TEXT NOT NULL DEFAULT '',
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
                "search_id": "TEXT NOT NULL DEFAULT ''",
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_analytics_events_search_id ON analytics_events(search_id, occurred_at DESC)"
            )
            conn.commit()

        _ANALYTICS_DB_READY = True


def record_event(
    *,
    event_type: str,
    search_id: str = "",
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
    occurred = _normalize_text(occurred_at, limit=40) or _now_utc_iso()
    payload = (
        event,
        _normalize_text(search_id, limit=64),
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
                    search_id,
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            conn.commit()


def _since_iso(days: int) -> str:
    safe_days = max(1, min(3650, _safe_int(days, 30)))
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=safe_days)).isoformat(timespec="seconds")


def _fetch_scalar(conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(query, params).fetchone()
    if not row:
        return 0
    return _safe_int(row[0], 0)


def fetch_overview(*, days: int = 30) -> dict[str, Any]:
    ensure_analytics_db()
    since = _since_iso(days)
    search_key_expr = _search_identity_expr()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        total_events = _fetch_scalar(
            conn,
            "SELECT COUNT(*) FROM analytics_events WHERE occurred_at >= ?",
            (since,),
        )
        total_searches = _fetch_scalar(
            conn,
            f"""
            SELECT COUNT(DISTINCT {search_key_expr})
            FROM analytics_events
            WHERE event_type = 'search_completed'
              AND occurred_at >= ?
            """,
            (since,),
        )
        successful_searches = _fetch_scalar(
            conn,
            f"""
            SELECT COUNT(DISTINCT {search_key_expr})
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
    search_key_expr = _search_identity_expr()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                origin,
                destination,
                COUNT(DISTINCT {search_key_expr}) AS searches,
                COUNT(DISTINCT CASE WHEN success = 1 THEN {search_key_expr} END) AS successful_searches,
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
    search_key_expr = _search_identity_expr()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(NULLIF(search_mode, ''), 'unknown') AS mode,
                COUNT(DISTINCT {search_key_expr}) AS searches,
                COUNT(DISTINCT CASE WHEN success = 1 THEN {search_key_expr} END) AS successful_searches
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
    search_key_expr = _search_identity_expr()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(NULLIF(location_country, ''), 'UNKNOWN') AS country,
                origin,
                destination,
                COUNT(DISTINCT {search_key_expr}) AS searches
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
    search_key_expr = _search_identity_expr()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                origin,
                destination,
                COUNT(DISTINCT {search_key_expr}) AS searches
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
    search_key_expr = _search_identity_expr()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                anon_id,
                MAX(occurred_at) AS last_seen,
                COUNT(DISTINCT {search_key_expr}) AS searches,
                COUNT(DISTINCT CASE WHEN success = 1 THEN {search_key_expr} END) AS successful_searches,
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
                "anon_label": _anon_label(row["anon_id"]),
                "country": _normalize_text(row["country"], uppercase=True, limit=12) or "UNKNOWN",
                "searches": searches,
                "successful_searches": _safe_int(row["successful_searches"], 0),
                "total_results_seen": _safe_int(row["total_results_seen"], 0),
                "last_seen": to_est_display(row["last_seen"]),
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
                search_id,
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
                "search_id": _normalize_text(row["search_id"], limit=64),
                "occurred_at": to_est_display(row["occurred_at"]),
                "anon_id": _normalize_text(row["anon_id"], limit=64),
                "anon_label": _anon_label(row["anon_id"]),
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


def fetch_anonymous_users(
    *,
    days: int = 30,
    limit: int = 100,
    offset: int = 0,
    search: str = "",
) -> list[dict[str, Any]]:
    ensure_analytics_db()
    since = _since_iso(days)
    safe_limit = max(1, min(500, _safe_int(limit, 100)))
    safe_offset = max(0, _safe_int(offset, 0))
    search_term = str(search or "").strip().lower()
    search_key_expr = _search_identity_expr()

    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if search_term:
            base_rows = conn.execute(
                f"""
                SELECT
                    anon_id,
                    MIN(occurred_at) AS first_seen,
                    MAX(occurred_at) AS last_seen,
                    COUNT(*) AS total_events,
                    COUNT(DISTINCT CASE WHEN event_type = 'search_completed' THEN {search_key_expr} END) AS searches,
                    COUNT(DISTINCT CASE WHEN event_type = 'search_completed' AND success = 1 THEN {search_key_expr} END) AS successful_searches,
                    SUM(CASE WHEN event_type = 'booking_completed' THEN 1 ELSE 0 END) AS bookings,
                    SUM(COALESCE(result_count, 0)) AS total_results_seen,
                    COALESCE(NULLIF(MAX(location_country), ''), 'UNKNOWN') AS country,
                    COALESCE(NULLIF(MAX(location_region), ''), '') AS region,
                    COALESCE(NULLIF(MAX(location_city), ''), '') AS city
                FROM analytics_events
                WHERE anon_id <> ''
                  AND occurred_at >= ?
                  AND (
                    lower(anon_id) LIKE ?
                    OR lower(location_country) LIKE ?
                    OR lower(location_region) LIKE ?
                    OR lower(location_city) LIKE ?
                  )
                GROUP BY anon_id
                ORDER BY searches DESC, total_events DESC, last_seen DESC
                LIMIT ? OFFSET ?
                """,
                (
                    since,
                    f"%{search_term}%",
                    f"%{search_term}%",
                    f"%{search_term}%",
                    f"%{search_term}%",
                    safe_limit,
                    safe_offset,
                ),
            ).fetchall()
        else:
            base_rows = conn.execute(
                f"""
                SELECT
                    anon_id,
                    MIN(occurred_at) AS first_seen,
                    MAX(occurred_at) AS last_seen,
                    COUNT(*) AS total_events,
                    COUNT(DISTINCT CASE WHEN event_type = 'search_completed' THEN {search_key_expr} END) AS searches,
                    COUNT(DISTINCT CASE WHEN event_type = 'search_completed' AND success = 1 THEN {search_key_expr} END) AS successful_searches,
                    SUM(CASE WHEN event_type = 'booking_completed' THEN 1 ELSE 0 END) AS bookings,
                    SUM(COALESCE(result_count, 0)) AS total_results_seen,
                    COALESCE(NULLIF(MAX(location_country), ''), 'UNKNOWN') AS country,
                    COALESCE(NULLIF(MAX(location_region), ''), '') AS region,
                    COALESCE(NULLIF(MAX(location_city), ''), '') AS city
                FROM analytics_events
                WHERE anon_id <> ''
                  AND occurred_at >= ?
                GROUP BY anon_id
                ORDER BY searches DESC, total_events DESC, last_seen DESC
                LIMIT ? OFFSET ?
                """,
                (since, safe_limit, safe_offset),
            ).fetchall()

        out: list[dict[str, Any]] = []
        for row in base_rows:
            anon_id = _normalize_text(row["anon_id"], limit=64)
            searches = _safe_int(row["searches"], 0)
            successful_searches = _safe_int(row["successful_searches"], 0)
            if searches <= 1:
                stage = "cold_start"
                stage_label = "Cold Start"
            elif searches < 4:
                stage = "learning"
                stage_label = "Learning"
            else:
                stage = "personalized_ready"
                stage_label = "Personalized Ready"

            route_row = conn.execute(
                f"""
                SELECT
                    origin,
                    destination,
                    COUNT(DISTINCT {_search_identity_expr()}) AS searches
                FROM analytics_events
                WHERE anon_id = ?
                  AND event_type = 'search_completed'
                  AND occurred_at >= ?
                  AND origin <> ''
                  AND destination <> ''
                GROUP BY origin, destination
                ORDER BY searches DESC, origin ASC, destination ASC
                LIMIT 1
                """,
                (anon_id, since),
            ).fetchone()

            if route_row:
                top_route = f"{_normalize_text(route_row['origin'], uppercase=True, limit=8)} -> {_normalize_text(route_row['destination'], uppercase=True, limit=8)}"
                top_route_searches = _safe_int(route_row["searches"], 0)
            else:
                top_route = "-"
                top_route_searches = 0

            mode_rows = conn.execute(
                f"""
                SELECT
                    COALESCE(NULLIF(search_mode, ''), 'unknown') AS mode,
                    COUNT(DISTINCT {_search_identity_expr()}) AS searches
                FROM analytics_events
                WHERE anon_id = ?
                  AND event_type = 'search_completed'
                  AND occurred_at >= ?
                GROUP BY mode
                ORDER BY searches DESC
                LIMIT 3
                """,
                (anon_id, since),
            ).fetchall()
            mode_mix = ", ".join(
                f"{_normalize_text(mode_row['mode'], limit=16)} ({_safe_int(mode_row['searches'], 0)})"
                for mode_row in mode_rows
            )

            out.append(
                {
                    "anon_id": anon_id,
                    "anon_label": _anon_label(anon_id),
                    "first_seen": to_est_display(row["first_seen"]),
                    "last_seen": to_est_display(row["last_seen"]),
                    "country": _normalize_text(row["country"], uppercase=True, limit=12) or "UNKNOWN",
                    "region": _normalize_text(row["region"], uppercase=True, limit=16),
                    "city": _normalize_text(row["city"], limit=80),
                    "total_events": _safe_int(row["total_events"], 0),
                    "searches": searches,
                    "successful_searches": successful_searches,
                    "bookings": _safe_int(row["bookings"], 0),
                    "total_results_seen": _safe_int(row["total_results_seen"], 0),
                    "success_rate": round((successful_searches / searches) * 100.0, 2) if searches > 0 else 0.0,
                    "top_route": top_route,
                    "top_route_searches": top_route_searches,
                    "mode_mix": mode_mix,
                    "stage": stage,
                    "stage_label": stage_label,
                }
            )
    return out


def fetch_events_for_anonymous_user(
    anon_id: str,
    *,
    limit: int = 120,
) -> list[dict[str, Any]]:
    normalized = _normalize_text(anon_id, limit=64)
    if not normalized:
        return []
    ensure_analytics_db()
    safe_limit = max(1, min(1000, _safe_int(limit, 120)))
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                id,
                event_type,
                search_id,
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
            WHERE anon_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (normalized, safe_limit),
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
                "search_id": _normalize_text(row["search_id"], limit=64),
                "occurred_at": to_est_display(row["occurred_at"]),
                "anon_id": _normalize_text(row["anon_id"], limit=64),
                "anon_label": _anon_label(row["anon_id"]),
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


def fetch_funnel_summary(*, days: int = 30) -> dict[str, Any]:
    ensure_analytics_db()
    since = _since_iso(days)
    core_events = (
        "site_landed",
        "search_completed",
        "results_viewed",
        "flight_selected",
        "booking_intent",
        "booking_completed",
    )
    user_expr = _user_identity_expr()

    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                {user_expr} AS user_key,
                event_type
            FROM analytics_events
            WHERE occurred_at >= ?
              AND event_type IN ({",".join("?" for _ in core_events)})
            ORDER BY occurred_at ASC, id ASC
            """,
            (since, *core_events),
        ).fetchall()

    users: dict[str, set[str]] = {}
    for row in rows:
        user_key = _normalize_text(row["user_key"], limit=160)
        event_type = _normalize_text(row["event_type"], limit=80).lower()
        if not user_key or not event_type:
            continue
        users.setdefault(user_key, set()).add(event_type)

    total_users = len(users)
    landed_users = sum(1 for steps in users.values() if "site_landed" in steps)
    searched_users = sum(1 for steps in users.values() if "search_completed" in steps)
    results_users = sum(1 for steps in users.values() if "results_viewed" in steps)
    clicked_users = sum(1 for steps in users.values() if "flight_selected" in steps)
    intent_users = sum(1 for steps in users.values() if "booking_intent" in steps)
    booked_users = sum(1 for steps in users.values() if "booking_completed" in steps)
    base_users = landed_users or total_users

    def pct(count: int, base: int) -> float:
        return round((count / base) * 100.0, 2) if base > 0 else 0.0

    return {
        "days": max(1, min(3650, _safe_int(days, 30))),
        "total_users": total_users,
        "landed_users": landed_users,
        "searched_users": searched_users,
        "results_users": results_users,
        "clicked_users": clicked_users,
        "intent_users": intent_users,
        "booked_users": booked_users,
        "search_rate": pct(searched_users, base_users),
        "results_view_rate": pct(results_users, base_users),
        "click_rate": pct(clicked_users, base_users),
        "intent_rate": pct(intent_users, base_users),
        "booking_rate": pct(booked_users, base_users),
        "search_to_click_rate": pct(clicked_users, searched_users),
        "click_to_intent_rate": pct(intent_users, clicked_users),
        "intent_to_booking_rate": pct(booked_users, intent_users),
        "dropoff_search_no_click": sum(
            1 for steps in users.values() if "search_completed" in steps and "flight_selected" not in steps
        ),
        "dropoff_click_no_intent": sum(
            1 for steps in users.values() if "flight_selected" in steps and "booking_intent" not in steps
        ),
        "dropoff_intent_no_booking": sum(
            1 for steps in users.values() if "booking_intent" in steps and "booking_completed" not in steps
        ),
    }


def fetch_preference_summary(*, days: int = 30) -> dict[str, Any]:
    ensure_analytics_db()
    since = _since_iso(days)
    preferred_events = ("flight_selected", "booking_intent", "booking_completed", "search_completed")
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                event_type,
                origin,
                destination,
                metadata_json
            FROM analytics_events
            WHERE occurred_at >= ?
              AND event_type IN ({",".join("?" for _ in preferred_events)})
            ORDER BY occurred_at ASC, id ASC
            """,
            (since, *preferred_events),
        ).fetchall()

    airline_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    prices: list[float] = []
    nonstop_yes = 0
    nonstop_known = 0

    for row in rows:
        metadata = _metadata_dict(row["metadata_json"])
        airline = _normalize_text(metadata.get("airline"), limit=80)
        if airline:
            airline_counts[airline] = airline_counts.get(airline, 0) + 1

        origin = _normalize_text(row["origin"], uppercase=True, limit=8)
        destination = _normalize_text(row["destination"], uppercase=True, limit=8)
        if origin and destination:
            route = f"{origin} -> {destination}"
            route_counts[route] = route_counts.get(route, 0) + 1

        price = _timeline_price(metadata)
        if price is not None and price > 0:
            prices.append(price)

        nonstop = metadata.get("nonstop")
        if isinstance(nonstop, bool):
            nonstop_known += 1
            if nonstop:
                nonstop_yes += 1

    top_airline = max(airline_counts.items(), key=lambda item: (item[1], item[0]))[0] if airline_counts else ""
    top_route = max(route_counts.items(), key=lambda item: (item[1], item[0]))[0] if route_counts else ""
    price_cap = 0.0
    if prices:
        ordered = sorted(prices)
        idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * 0.75))))
        price_cap = ordered[idx]

    nonstop_share = round((nonstop_yes / nonstop_known) * 100.0, 2) if nonstop_known else 0.0
    return {
        "top_airline": top_airline,
        "top_route": top_route,
        "nonstop_share": nonstop_share,
        "nonstop_preference": nonstop_share >= 50.0 if nonstop_known else None,
        "price_cap": round(price_cap, 2) if price_cap else 0.0,
        "priced_event_count": len(prices),
        "avg_price": round(sum(prices) / len(prices), 2) if prices else 0.0,
    }


def fetch_city_breakdown(*, days: int = 30, limit: int = 12) -> list[dict[str, Any]]:
    ensure_analytics_db()
    since = _since_iso(days)
    safe_limit = max(1, min(100, _safe_int(limit, 12)))
    user_expr = _user_identity_expr()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(NULLIF(location_city, ''), 'Unknown') AS city,
                COALESCE(NULLIF(location_country, ''), 'UNKNOWN') AS country,
                COUNT(*) AS events,
                COUNT(DISTINCT CASE WHEN event_type = 'search_completed' THEN {user_expr} END) AS searching_users,
                COUNT(DISTINCT CASE WHEN event_type = 'booking_intent' THEN {user_expr} END) AS intent_users,
                COUNT(DISTINCT CASE WHEN event_type = 'booking_completed' THEN {user_expr} END) AS booked_users
            FROM analytics_events
            WHERE occurred_at >= ?
            GROUP BY city, country
            ORDER BY events DESC, city ASC
            LIMIT ?
            """,
            (since, safe_limit),
        ).fetchall()
    return [
        {
            "city": _normalize_text(row["city"], limit=80) or "Unknown",
            "country": _normalize_text(row["country"], uppercase=True, limit=12) or "UNKNOWN",
            "events": _safe_int(row["events"], 0),
            "searching_users": _safe_int(row["searching_users"], 0),
            "intent_users": _safe_int(row["intent_users"], 0),
            "booked_users": _safe_int(row["booked_users"], 0),
        }
        for row in rows
    ]


def fetch_results_update_summary(*, days: int = 30, limit: int = 8) -> dict[str, Any]:
    ensure_analytics_db()
    since = _since_iso(days)
    safe_limit = max(1, min(25, _safe_int(limit, 8)))
    user_expr = _user_identity_expr()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                {user_expr} AS user_key,
                metadata_json,
                origin,
                destination,
                result_count,
                success
            FROM analytics_events
            WHERE occurred_at >= ?
              AND event_type = 'results_updated'
            ORDER BY occurred_at ASC, id ASC
            """,
            (since,),
        ).fetchall()

    changed_field_counts: dict[str, int] = {}
    changed_value_counts: dict[str, int] = {}
    route_counts: dict[str, int] = {}
    sort_counts: dict[str, int] = {}
    nonstop_counts = {"enabled": 0, "disabled": 0}
    user_keys: set[str] = set()
    successful_updates = 0

    for row in rows:
        user_key = _normalize_text(row["user_key"], limit=160)
        if user_key:
            user_keys.add(user_key)
        if bool(_safe_int(row["success"], 0)):
            successful_updates += 1
        origin = _normalize_text(row["origin"], uppercase=True, limit=8)
        destination = _normalize_text(row["destination"], uppercase=True, limit=8)
        if origin and destination:
            route = f"{origin} -> {destination}"
            route_counts[route] = route_counts.get(route, 0) + 1

        metadata = _metadata_dict(row["metadata_json"])
        for field in metadata.get("changed_fields") or []:
            normalized_field = _normalize_text(field, limit=40)
            if not normalized_field:
                continue
            changed_field_counts[normalized_field] = changed_field_counts.get(normalized_field, 0) + 1

        for change in metadata.get("changes") or []:
            if not isinstance(change, dict):
                continue
            label = _normalize_text(change.get("label"), limit=60)
            after = str(change.get("after") or "").strip()
            if not label or not after:
                continue
            key = f"{label}: {after}"
            changed_value_counts[key] = changed_value_counts.get(key, 0) + 1

        current_search = metadata.get("current_search") if isinstance(metadata.get("current_search"), dict) else {}
        current_sort = _normalize_text(current_search.get("sort"), limit=30)
        if current_sort:
            sort_counts[current_sort] = sort_counts.get(current_sort, 0) + 1
        if isinstance(current_search.get("nonstop"), bool):
            nonstop_counts["enabled" if current_search.get("nonstop") else "disabled"] += 1

    top_changed_fields = [
        {"field": field, "updates": count}
        for field, count in sorted(changed_field_counts.items(), key=lambda item: (-item[1], item[0]))[:safe_limit]
    ]
    top_changed_values = [
        {"change": change, "updates": count}
        for change, count in sorted(changed_value_counts.items(), key=lambda item: (-item[1], item[0]))[:safe_limit]
    ]
    top_routes = [
        {"route": route, "updates": count}
        for route, count in sorted(route_counts.items(), key=lambda item: (-item[1], item[0]))[:safe_limit]
    ]
    top_sorts = [
        {"sort": sort, "updates": count}
        for sort, count in sorted(sort_counts.items(), key=lambda item: (-item[1], item[0]))[:safe_limit]
    ]
    return {
        "total_updates": len(rows),
        "users_updating": len(user_keys),
        "successful_updates": successful_updates,
        "top_changed_fields": top_changed_fields,
        "top_changed_values": top_changed_values,
        "top_routes": top_routes,
        "top_sorts": top_sorts,
        "nonstop_enabled_updates": nonstop_counts["enabled"],
        "nonstop_disabled_updates": nonstop_counts["disabled"],
    }


def fetch_results_click_summary(*, days: int = 30, limit: int = 8) -> dict[str, Any]:
    ensure_analytics_db()
    since = _since_iso(days)
    safe_limit = max(1, min(25, _safe_int(limit, 8)))
    user_expr = _user_identity_expr()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                {user_expr} AS user_key,
                origin,
                destination,
                booking_amount,
                currency,
                metadata_json
            FROM analytics_events
            WHERE occurred_at >= ?
              AND event_type = 'flight_selected'
            ORDER BY occurred_at ASC, id ASC
            """,
            (since,),
        ).fetchall()

    users: set[str] = set()
    route_counts: dict[str, int] = {}
    airline_counts: dict[str, int] = {}
    price_points: list[float] = []
    nonstop_yes = 0
    nonstop_known = 0

    for row in rows:
        user_key = _normalize_text(row["user_key"], limit=160)
        if user_key:
            users.add(user_key)
        origin = _normalize_text(row["origin"], uppercase=True, limit=8)
        destination = _normalize_text(row["destination"], uppercase=True, limit=8)
        if origin and destination:
            route = f"{origin} -> {destination}"
            route_counts[route] = route_counts.get(route, 0) + 1
        metadata = _metadata_dict(row["metadata_json"])
        airline = _normalize_text(metadata.get("airline"), limit=80)
        if airline:
            airline_counts[airline] = airline_counts.get(airline, 0) + 1
        price = _safe_float(metadata.get("price"))
        if price is None:
            price = _safe_float(row["booking_amount"])
        if price is not None and price > 0:
            price_points.append(price)
        nonstop = metadata.get("nonstop")
        if isinstance(nonstop, bool):
            nonstop_known += 1
            if nonstop:
                nonstop_yes += 1

    average_price = round(sum(price_points) / len(price_points), 2) if price_points else 0.0
    top_routes = [
        {"route": route, "clicks": count}
        for route, count in sorted(route_counts.items(), key=lambda item: (-item[1], item[0]))[:safe_limit]
    ]
    top_airlines = [
        {"airline": airline, "clicks": count}
        for airline, count in sorted(airline_counts.items(), key=lambda item: (-item[1], item[0]))[:safe_limit]
    ]
    return {
        "total_clicks": len(rows),
        "users_clicking": len(users),
        "top_routes": top_routes,
        "top_airlines": top_airlines,
        "average_price": average_price,
        "nonstop_share": round((nonstop_yes / nonstop_known) * 100.0, 2) if nonstop_known else 0.0,
    }


def fetch_next_page_summary(*, days: int = 30) -> list[dict[str, Any]]:
    ensure_analytics_db()
    since = _since_iso(days)
    user_expr = _user_identity_expr()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                {user_expr} AS user_key,
                event_type,
                metadata_json
            FROM analytics_events
            WHERE occurred_at >= ?
              AND event_type IN ('flight_selected', 'booking_intent', 'booking_completed')
            ORDER BY occurred_at ASC, id ASC
            """,
            (since,),
        ).fetchall()

    sections = {
        "review_page": {"label": "Review page", "events": 0, "users": set()},
        "seat_selection": {"label": "Seat selection", "events": 0, "users": set()},
        "traveler_checkout": {"label": "Traveler checkout", "events": 0, "users": set()},
        "booking_completed": {"label": "Booking completed", "events": 0, "users": set()},
    }

    for row in rows:
        user_key = _normalize_text(row["user_key"], limit=160)
        event_type = _normalize_text(row["event_type"], limit=80).lower()
        metadata = _metadata_dict(row["metadata_json"])
        step = _normalize_text(metadata.get("step"), limit=40).lower()

        section_key = ""
        if event_type == "flight_selected" and step == "review_page":
            section_key = "review_page"
        elif event_type == "booking_intent" and step == "seat_selection":
            section_key = "seat_selection"
        elif event_type == "booking_intent" and step == "traveler_checkout":
            section_key = "traveler_checkout"
        elif event_type == "booking_completed":
            section_key = "booking_completed"

        if not section_key:
            continue
        section = sections[section_key]
        section["events"] += 1
        if user_key:
            section["users"].add(user_key)

    return [
        {
            "section": key,
            "label": value["label"],
            "events": value["events"],
            "users": len(value["users"]),
        }
        for key, value in sections.items()
    ]


def fetch_user_journeys(
    *,
    days: int = 30,
    limit: int = 100,
    offset: int = 0,
    search: str = "",
) -> list[dict[str, Any]]:
    ensure_analytics_db()
    since = _since_iso(days)
    safe_limit = max(1, min(500, _safe_int(limit, 100)))
    safe_offset = max(0, _safe_int(offset, 0))
    search_term = str(search or "").strip().lower()
    core_events = (
        "site_landed",
        "search_completed",
        "results_viewed",
        "flight_selected",
        "booking_intent",
        "booking_completed",
    )
    user_expr = _user_identity_expr()

    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                {user_expr} AS user_key,
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
                metadata_json
            FROM analytics_events
            WHERE occurred_at >= ?
              AND event_type IN ({",".join("?" for _ in core_events)})
            ORDER BY occurred_at ASC, id ASC
            """,
            (since, *core_events),
        ).fetchall()

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        user_key = _normalize_text(row["user_key"], limit=160)
        if not user_key:
            continue
        metadata = _metadata_dict(row["metadata_json"])
        item = grouped.setdefault(
            user_key,
            {
                "user_key": user_key,
                "anon_id": _normalize_text(row["anon_id"], limit=64),
                "account_email": _normalize_email(row["account_email"]),
                "country": "",
                "region": "",
                "city": "",
                "first_seen_raw": str(row["occurred_at"] or ""),
                "last_seen_raw": str(row["occurred_at"] or ""),
                "events_count": 0,
                "steps": set(),
                "route_counts": {},
                "timeline": [],
                "search_mode": "",
                "last_airline": "",
                "last_price": None,
            },
        )
        item["events_count"] += 1
        item["last_seen_raw"] = str(row["occurred_at"] or item["last_seen_raw"])
        event_type = _normalize_text(row["event_type"], limit=80).lower()
        item["steps"].add(event_type)
        city = _normalize_text(row["location_city"], limit=80)
        region = _normalize_text(row["location_region"], uppercase=True, limit=16)
        country = _normalize_text(row["location_country"], uppercase=True, limit=12)
        if city:
            item["city"] = city
        if region:
            item["region"] = region
        if country:
            item["country"] = country
        search_mode = _normalize_text(row["search_mode"], limit=24)
        if search_mode:
            item["search_mode"] = search_mode
        origin = _normalize_text(row["origin"], uppercase=True, limit=8)
        destination = _normalize_text(row["destination"], uppercase=True, limit=8)
        if origin and destination:
            route = f"{origin} -> {destination}"
            item["route_counts"][route] = item["route_counts"].get(route, 0) + 1
        airline = _normalize_text(metadata.get("airline"), limit=80)
        if airline:
            item["last_airline"] = airline
        price = _timeline_price(metadata)
        if price is not None and price > 0:
            item["last_price"] = round(price, 2)

        item["timeline"].append(
            {
                "event_type": event_type,
                "label": _normalize_event_label(event_type),
                "time": to_est_display(row["occurred_at"]),
                "route": f"{origin} -> {destination}" if origin and destination else "",
                "airline": airline,
                "price": round(price, 2) if price is not None and price > 0 else None,
                "nonstop": metadata.get("nonstop") if isinstance(metadata.get("nonstop"), bool) else None,
            }
        )

    journeys: list[dict[str, Any]] = []
    for user_key, item in grouped.items():
        top_route = "-"
        if item["route_counts"]:
            top_route = max(item["route_counts"].items(), key=lambda pair: (pair[1], pair[0]))[0]
        display_name = item["account_email"] or _anon_label(item["anon_id"] or user_key)
        steps_order = [
            "site_landed",
            "search_completed",
            "results_viewed",
            "flight_selected",
            "booking_intent",
            "booking_completed",
        ]
        action_labels = [_normalize_event_label(step) for step in steps_order if step in item["steps"]]
        summary_bits = [f"{display_name}"]
        if item["city"]:
            summary_bits.append(f"from {item['city']}")
        if top_route != "-":
            summary_bits.append(f"focused on {top_route}")
        if item["last_airline"]:
            summary_bits.append(f"and last touched {item['last_airline']}")
        fallback_summary = " ".join(summary_bits) + "."
        if action_labels:
            fallback_summary += f" Journey: {' -> '.join(action_labels)}."
        if item["last_price"] is not None:
            fallback_summary += f" Latest tracked price was ${item['last_price']:.2f}."

        journey = {
            "user_key": user_key,
            "display_name": display_name,
            "anon_id": item["anon_id"],
            "anon_label": _anon_label(item["anon_id"] or user_key),
            "account_email": item["account_email"],
            "country": item["country"] or "UNKNOWN",
            "region": item["region"],
            "city": item["city"],
            "search_mode": item["search_mode"] or "unknown",
            "first_seen": to_est_display(item["first_seen_raw"]),
            "last_seen": to_est_display(item["last_seen_raw"]),
            "events_count": item["events_count"],
            "top_route": top_route,
            "last_airline": item["last_airline"],
            "last_price": item["last_price"],
            "searched": "search_completed" in item["steps"],
            "viewed_results": "results_viewed" in item["steps"],
            "clicked_flight": "flight_selected" in item["steps"],
            "booking_intent": "booking_intent" in item["steps"],
            "booked": "booking_completed" in item["steps"],
            "journey_path": " -> ".join(action_labels) if action_labels else "No core journey events",
            "fallback_summary": fallback_summary,
            "timeline": item["timeline"][-8:],
        }
        if search_term:
            haystack = " ".join(
                [
                    journey["display_name"],
                    journey["account_email"],
                    journey["city"],
                    journey["country"],
                    journey["top_route"],
                    journey["journey_path"],
                ]
            ).lower()
            if search_term not in haystack:
                continue
        journeys.append(journey)

    journeys.sort(key=lambda item: item["last_seen"], reverse=True)
    return journeys[safe_offset : safe_offset + safe_limit]


def clear_events() -> None:
    ensure_analytics_db()
    with _ANALYTICS_DB_LOCK:
        with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
            conn.execute("DELETE FROM analytics_events")
            conn.commit()


def clear_behavioral_events() -> int:
    ensure_analytics_db()
    core_keep = ("booking_completed",)
    with _ANALYTICS_DB_LOCK:
        with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM analytics_events
                WHERE event_type NOT IN ({",".join("?" for _ in core_keep)})
                """,
                core_keep,
            ).fetchone()
            deleted = _safe_int(row[0], 0) if row else 0
            conn.execute(
                f"""
                DELETE FROM analytics_events
                WHERE event_type NOT IN ({",".join("?" for _ in core_keep)})
                """,
                core_keep,
            )
            conn.commit()
    return deleted


# ---------------------------------------------------------------------------
# Extended analytics queries
# ---------------------------------------------------------------------------

def fetch_daily_trends(*, days: int = 30) -> list[dict[str, Any]]:
    ensure_analytics_db()
    since = _since_iso(days)
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, event_type, search_id, occurred_at, success
            FROM analytics_events
            WHERE occurred_at >= ?
            ORDER BY occurred_at ASC, id ASC
            """,
            (since,),
        ).fetchall()

    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        day_key = _est_day_key(row["occurred_at"])
        if not day_key:
            continue
        bucket = by_day.setdefault(
            day_key,
            {
                "search_ids": set(),
                "successful_search_ids": set(),
                "bookings": 0,
                "signups": 0,
            },
        )
        event_type = _normalize_text(row["event_type"], limit=80)
        if event_type == "search_completed":
            search_key = _normalize_text(row["search_id"], limit=64) or f"row:{_safe_int(row['id'], 0)}"
            bucket["search_ids"].add(search_key)
            if _safe_int(row["success"], 0):
                bucket["successful_search_ids"].add(search_key)
        elif event_type == "booking_completed":
            bucket["bookings"] += 1
        elif event_type == "account_signup":
            bucket["signups"] += 1

    return [
        {
            "day": day,
            "searches": len(bucket["search_ids"]),
            "bookings": _safe_int(bucket["bookings"], 0),
            "signups": _safe_int(bucket["signups"], 0),
            "successful_searches": len(bucket["successful_search_ids"]),
        }
        for day, bucket in sorted(by_day.items(), key=lambda item: item[0])
    ]


def fetch_revenue_series(*, days: int = 30) -> list[dict[str, Any]]:
    ensure_analytics_db()
    since = _since_iso(days)
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                occurred_at,
                booking_amount
            FROM analytics_events
            WHERE event_type = 'booking_completed'
              AND occurred_at >= ?
            ORDER BY occurred_at ASC, id ASC
            """,
            (since,),
        ).fetchall()

    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        day_key = _est_day_key(row["occurred_at"])
        if not day_key:
            continue
        bucket = by_day.setdefault(day_key, {"bookings": 0, "revenue": 0.0})
        bucket["bookings"] += 1
        bucket["revenue"] += float(_safe_float(row["booking_amount"]) or 0.0)

    out: list[dict[str, Any]] = []
    for day, bucket in sorted(by_day.items(), key=lambda item: item[0]):
        bookings = _safe_int(bucket["bookings"], 0)
        revenue = float(bucket["revenue"] or 0.0)
        avg_value = (revenue / bookings) if bookings > 0 else 0.0
        out.append(
            {
                "day": day,
                "bookings": bookings,
                "revenue": round(revenue, 2),
                "avg_value": round(avg_value, 2),
            }
        )
    return out


def fetch_trip_type_breakdown(*, days: int = 30) -> list[dict[str, Any]]:
    ensure_analytics_db()
    since = _since_iso(days)
    search_key_expr = _search_identity_expr()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(NULLIF(trip_type, ''), 'unknown') AS trip_type,
                COUNT(DISTINCT {search_key_expr}) AS searches,
                COUNT(DISTINCT CASE WHEN success = 1 THEN {search_key_expr} END) AS successful_searches
            FROM analytics_events
            WHERE event_type = 'search_completed'
              AND occurred_at >= ?
            GROUP BY trip_type
            ORDER BY searches DESC
            """,
            (since,),
        ).fetchall()
    return [
        {
            "trip_type": row["trip_type"],
            "searches": _safe_int(row["searches"], 0),
            "successful_searches": _safe_int(row["successful_searches"], 0),
        }
        for row in rows
    ]


def fetch_country_breakdown(*, days: int = 30, limit: int = 10) -> list[dict[str, Any]]:
    ensure_analytics_db()
    since = _since_iso(days)
    safe_limit = max(1, min(100, _safe_int(limit, 10)))
    search_key_expr = _search_identity_expr()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(NULLIF(location_country, ''), 'UNKNOWN') AS country,
                COUNT(*) AS events,
                COUNT(DISTINCT CASE WHEN event_type = 'search_completed' THEN {search_key_expr} END) AS searches,
                SUM(CASE WHEN event_type = 'booking_completed' THEN 1 ELSE 0 END) AS bookings,
                COUNT(DISTINCT anon_id) AS unique_users
            FROM analytics_events
            WHERE occurred_at >= ?
            GROUP BY country
            ORDER BY events DESC
            LIMIT ?
            """,
            (since, safe_limit),
        ).fetchall()
    return [
        {
            "country": row["country"],
            "events": _safe_int(row["events"], 0),
            "searches": _safe_int(row["searches"], 0),
            "bookings": _safe_int(row["bookings"], 0),
            "unique_users": _safe_int(row["unique_users"], 0),
        }
        for row in rows
    ]


def fetch_revenue_summary(*, days: int = 30) -> dict[str, Any]:
    ensure_analytics_db()
    since = _since_iso(days)
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_bookings,
                COALESCE(SUM(booking_amount), 0.0) AS total_revenue,
                COALESCE(AVG(booking_amount), 0.0) AS avg_booking_value,
                COALESCE(MAX(booking_amount), 0.0) AS max_booking_value
            FROM analytics_events
            WHERE event_type = 'booking_completed'
              AND occurred_at >= ?
            """,
            (since,),
        ).fetchone()
        currency_rows = conn.execute(
            """
            SELECT currency, COUNT(*) AS cnt, COALESCE(SUM(booking_amount), 0) AS rev
            FROM analytics_events
            WHERE event_type = 'booking_completed'
              AND occurred_at >= ?
              AND currency <> ''
            GROUP BY currency
            ORDER BY cnt DESC
            LIMIT 5
            """,
            (since,),
        ).fetchall()
    return {
        "total_bookings": _safe_int(row["total_bookings"], 0) if row else 0,
        "total_revenue": round(float(row["total_revenue"] or 0), 2) if row else 0.0,
        "avg_booking_value": round(float(row["avg_booking_value"] or 0), 2) if row else 0.0,
        "max_booking_value": round(float(row["max_booking_value"] or 0), 2) if row else 0.0,
        "currencies": [
            {
                "currency": r["currency"],
                "bookings": _safe_int(r["cnt"], 0),
                "revenue": round(float(r["rev"] or 0), 2),
            }
            for r in currency_rows
        ],
    }


def fetch_bookings_list(*, limit: int = 100, offset: int = 0, search: str = "") -> list[dict[str, Any]]:
    ensure_analytics_db()
    safe_limit = max(1, min(500, _safe_int(limit, 100)))
    safe_offset = max(0, _safe_int(offset, 0))
    search_term = str(search or "").strip().lower()
    with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if search_term:
            rows = conn.execute(
                """
                SELECT id, occurred_at, account_email, anon_id,
                       origin, destination, trip_type, booking_amount, currency,
                       location_country, metadata_json
                FROM analytics_events
                WHERE event_type = 'booking_completed'
                  AND (
                      lower(account_email) LIKE ?
                      OR lower(metadata_json) LIKE ?
                      OR lower(origin) LIKE ?
                      OR lower(destination) LIKE ?
                  )
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (f"%{search_term}%", f"%{search_term}%", f"%{search_term}%", f"%{search_term}%", safe_limit, safe_offset),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, occurred_at, account_email, anon_id,
                       origin, destination, trip_type, booking_amount, currency,
                       location_country, metadata_json
                FROM analytics_events
                WHERE event_type = 'booking_completed'
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (safe_limit, safe_offset),
            ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except Exception:
            meta = {}
        out.append(
            {
                "id": _safe_int(row["id"], 0),
                "occurred_at": to_est_display(row["occurred_at"]),
                "account_email": _normalize_email(row["account_email"]),
                "anon_id": row["anon_id"] or "",
                "origin": (row["origin"] or "").upper(),
                "destination": (row["destination"] or "").upper(),
                "route": f"{(row['origin'] or '').upper()} → {(row['destination'] or '').upper()}",
                "trip_type": row["trip_type"] or "",
                "booking_amount": _safe_float(row["booking_amount"]),
                "currency": (row["currency"] or "").upper(),
                "location_country": (row["location_country"] or "").upper(),
                "booking_reference": str(meta.get("booking_reference") or ""),
                "order_id": str(meta.get("order_id") or ""),
                "provider": str(meta.get("provider") or ""),
            }
        )
    return out


def fetch_accounts_list(
    accounts_db: str,
    *,
    limit: int = 100,
    offset: int = 0,
    search: str = "",
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(500, _safe_int(limit, 100)))
    safe_offset = max(0, _safe_int(offset, 0))
    search_term = str(search or "").strip().lower()
    try:
        with sqlite3.connect(accounts_db) as conn:
            conn.row_factory = sqlite3.Row
            if search_term:
                rows = conn.execute(
                    """
                    SELECT email, first_name, last_name, dob, created_at,
                           last_login_at, last_login_ip,
                           price_alerts_enabled, route_tracking_enabled,
                           saved_searches, linked_booking_references, updated_at
                    FROM manage_booking_accounts
                    WHERE lower(email) LIKE ?
                       OR lower(first_name) LIKE ?
                       OR lower(last_name) LIKE ?
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (f"%{search_term}%", f"%{search_term}%", f"%{search_term}%", safe_limit, safe_offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT email, first_name, last_name, dob, created_at,
                           last_login_at, last_login_ip,
                           price_alerts_enabled, route_tracking_enabled,
                           saved_searches, linked_booking_references, updated_at
                    FROM manage_booking_accounts
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (safe_limit, safe_offset),
                ).fetchall()
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            saved = json.loads(row["saved_searches"] or "[]")
            saved_count = len(saved) if isinstance(saved, list) else 0
        except Exception:
            saved_count = 0
        try:
            linked = json.loads(row["linked_booking_references"] or "[]")
            linked_count = len(linked) if isinstance(linked, list) else 0
        except Exception:
            linked_count = 0
        out.append(
            {
                "email": _normalize_email(row["email"]),
                "first_name": str(row["first_name"] or ""),
                "last_name": str(row["last_name"] or ""),
                "full_name": f"{row['first_name'] or ''} {row['last_name'] or ''}".strip() or "—",
                "dob": str(row["dob"] or ""),
                "created_at": to_est_display(row["created_at"]),
                "last_login_at": to_est_display(row["last_login_at"]),
                "last_login_ip": str(row["last_login_ip"] or ""),
                "price_alerts_enabled": bool(row["price_alerts_enabled"]),
                "route_tracking_enabled": bool(row["route_tracking_enabled"]),
                "saved_searches_count": saved_count,
                "linked_bookings_count": linked_count,
                "updated_at": to_est_display(row["updated_at"]),
            }
        )
    return out


def update_account(
    accounts_db: str,
    email: str,
    *,
    fields: dict[str, Any],
) -> bool:
    allowed = {"first_name", "last_name", "price_alerts_enabled", "route_tracking_enabled"}
    clean = {k: v for k, v in fields.items() if k in allowed}
    if not clean:
        return False
    norm_email = _normalize_email(email)
    if not norm_email:
        return False
    set_parts = ", ".join(f"{k} = ?" for k in clean)
    values = list(clean.values()) + [norm_email]
    try:
        with sqlite3.connect(accounts_db) as conn:
            cur = conn.execute(
                f"UPDATE manage_booking_accounts SET {set_parts}, updated_at = ? WHERE email = ?",
                values[:-1] + [_now_utc_iso(), norm_email],
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception:
        return False


def fetch_account_detail(accounts_db: str, email: str) -> dict[str, Any] | None:
    norm_email = _normalize_email(email)
    if not norm_email:
        return None
    try:
        with sqlite3.connect(accounts_db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT email, first_name, last_name, dob, created_at,
                       last_login_at, last_login_ip,
                       price_alerts_enabled, route_tracking_enabled,
                       saved_searches, linked_booking_references, updated_at
                FROM manage_booking_accounts
                WHERE email = ?
                """,
                (norm_email,),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        saved = json.loads(row["saved_searches"] or "[]")
    except Exception:
        saved = []
    try:
        linked = json.loads(row["linked_booking_references"] or "[]")
    except Exception:
        linked = []
    return {
        "email": _normalize_email(row["email"]),
        "first_name": str(row["first_name"] or ""),
        "last_name": str(row["last_name"] or ""),
        "dob": str(row["dob"] or ""),
        "created_at": to_est_display(row["created_at"]),
        "last_login_at": to_est_display(row["last_login_at"]),
        "last_login_ip": str(row["last_login_ip"] or ""),
        "price_alerts_enabled": bool(row["price_alerts_enabled"]),
        "route_tracking_enabled": bool(row["route_tracking_enabled"]),
        "saved_searches": saved if isinstance(saved, list) else [],
        "linked_booking_references": linked if isinstance(linked, list) else [],
        "updated_at": to_est_display(row["updated_at"]),
    }


def _ensure_account_reset_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS manage_booking_reset_codes (
            email TEXT PRIMARY KEY,
            code_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            requested_by TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_manage_booking_reset_codes_expires_at ON manage_booking_reset_codes(expires_at)"
    )


def clear_password_reset_code(accounts_db: str, email: str) -> None:
    norm_email = _normalize_email(email)
    if not norm_email:
        return
    with _ACCOUNTS_DB_LOCK:
        with sqlite3.connect(accounts_db) as conn:
            _ensure_account_reset_table(conn)
            conn.execute("DELETE FROM manage_booking_reset_codes WHERE email = ?", (norm_email,))
            conn.commit()


def validate_password_reset_code(
    accounts_db: str,
    email: str,
    code: str,
    *,
    consume: bool = False,
) -> bool:
    norm_email = _normalize_email(email)
    norm_code = _normalize_reset_code(code)
    if not norm_email or len(norm_code) != 6:
        return False
    expected_hash = _hash_reset_code(norm_email, norm_code)
    now_ts = int(time.time())
    with _ACCOUNTS_DB_LOCK:
        with sqlite3.connect(accounts_db) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_account_reset_table(conn)
            row = conn.execute(
                """
                SELECT code_hash, expires_at
                FROM manage_booking_reset_codes
                WHERE email = ?
                """,
                (norm_email,),
            ).fetchone()
            if not row:
                return False
            code_hash = str(row["code_hash"] or "").strip()
            expires_at = _safe_int(row["expires_at"], 0)
            valid = (
                bool(code_hash)
                and bool(expires_at)
                and now_ts <= expires_at
                and hmac.compare_digest(code_hash, expected_hash)
            )
            if consume and valid:
                conn.execute("DELETE FROM manage_booking_reset_codes WHERE email = ?", (norm_email,))
                conn.commit()
            return valid


def create_password_reset_request(
    accounts_db: str,
    email: str,
    *,
    ttl_minutes: int = 10,
    requested_by: str = "self",
) -> tuple[bool, str, str | None]:
    norm_email = _normalize_email(email)
    if not norm_email:
        return False, "invalid_recipient", None

    account = fetch_account_detail(accounts_db, norm_email)
    if not account:
        return False, "account_not_found", None

    numeric_code = int.from_bytes(os.urandom(3), byteorder="big") % 1_000_000
    verification_code = f"{numeric_code:06d}"
    ttl = max(1, _safe_int(ttl_minutes, 10))
    now_ts = int(time.time())
    expires_at = now_ts + (ttl * 60)
    code_hash = _hash_reset_code(norm_email, verification_code)
    requester = _normalize_text(requested_by, limit=48) or "self"

    with _ACCOUNTS_DB_LOCK:
        with sqlite3.connect(accounts_db) as conn:
            _ensure_account_reset_table(conn)
            conn.execute(
                """
                INSERT INTO manage_booking_reset_codes (email, code_hash, expires_at, created_at, requested_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    code_hash = excluded.code_hash,
                    expires_at = excluded.expires_at,
                    created_at = excluded.created_at,
                    requested_by = excluded.requested_by
                """,
                (norm_email, code_hash, expires_at, _now_utc_iso(), requester),
            )
            conn.commit()

    sent, reason = email_service.send_password_reset_code_email(
        to_email=norm_email,
        verification_code=verification_code,
        ttl_minutes=ttl,
        first_name=str(account.get("first_name") or ""),
    )
    if not sent:
        clear_password_reset_code(accounts_db, norm_email)
        return False, reason, None
    return True, "sent", verification_code


def clear_account_saved_searches(accounts_db: str, email: str) -> bool:
    norm_email = _normalize_email(email)
    if not norm_email:
        return False
    try:
        with _ACCOUNTS_DB_LOCK:
            with sqlite3.connect(accounts_db) as conn:
                cur = conn.execute(
                    """
                    UPDATE manage_booking_accounts
                    SET saved_searches = '[]', updated_at = ?
                    WHERE email = ?
                    """,
                    (_now_utc_iso(), norm_email),
                )
                conn.commit()
                return cur.rowcount > 0
    except Exception:
        return False


def delete_account(accounts_db: str, email: str) -> bool:
    norm_email = _normalize_email(email)
    if not norm_email:
        return False
    try:
        with _ACCOUNTS_DB_LOCK:
            with sqlite3.connect(accounts_db) as conn:
                _ensure_account_reset_table(conn)
                conn.execute("DELETE FROM manage_booking_reset_codes WHERE email = ?", (norm_email,))
                cur = conn.execute("DELETE FROM manage_booking_accounts WHERE email = ?", (norm_email,))
                conn.commit()
                return cur.rowcount > 0
    except Exception:
        return False


def clear_search_events_for_account(email: str) -> int:
    ensure_analytics_db()
    norm_email = _normalize_email(email)
    if not norm_email:
        return 0
    with _ANALYTICS_DB_LOCK:
        with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
            cur = conn.execute(
                """
                DELETE FROM analytics_events
                WHERE event_type = 'search_completed'
                  AND account_email = ?
                """,
                (norm_email,),
            )
            conn.commit()
            return max(0, _safe_int(cur.rowcount, 0))


def delete_non_booking_event(event_id: int) -> tuple[bool, str]:
    ensure_analytics_db()
    safe_id = _safe_int(event_id, 0)
    if safe_id <= 0:
        return False, "invalid_event_id"
    with _ANALYTICS_DB_LOCK:
        with sqlite3.connect(ANALYTICS_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT event_type FROM analytics_events WHERE id = ?",
                (safe_id,),
            ).fetchone()
            if not row:
                return False, "event_not_found"
            event_type = _normalize_text(row["event_type"], limit=80)
            if event_type == "booking_completed":
                return False, "booking_delete_forbidden"
            cur = conn.execute("DELETE FROM analytics_events WHERE id = ?", (safe_id,))
            conn.commit()
            if cur.rowcount <= 0:
                return False, "event_not_found"
            return True, "deleted"
