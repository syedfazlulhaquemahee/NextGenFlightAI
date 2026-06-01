from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_security import generate_password_salt, hash_password, normalize_role, resolve_effective_role

BASE_DIR = os.path.dirname(__file__)
AGENT_DB_PATH = (
    os.getenv("NGF_AGENT_DB_PATH", os.path.join(BASE_DIR, "data", "agent_portal.db")).strip()
    or os.path.join(BASE_DIR, "data", "agent_portal.db")
)
_AGENT_DB_READY = False
_DB_LOCK = threading.Lock()


def configure(*, db_path: str | None = None) -> None:
    global AGENT_DB_PATH, _AGENT_DB_READY
    if db_path:
        AGENT_DB_PATH = str(db_path).strip() or AGENT_DB_PATH
    _AGENT_DB_READY = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _norm_email(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "")[:160]


def _norm_text(value: Any, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _dict_from_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = dict(row)
    payload["is_active"] = bool(_coerce_int(payload.get("is_active"), 0))
    payload["two_factor_enabled"] = bool(_coerce_int(payload.get("two_factor_enabled"), 0))
    payload["failed_login_attempts"] = _coerce_int(payload.get("failed_login_attempts"), 0)
    payload["session_version"] = _coerce_int(payload.get("session_version"), 1)
    payload["effective_role"] = resolve_effective_role(payload.get("global_role"), payload.get("membership_role"))
    return payload


def _connect() -> sqlite3.Connection:
    ensure_db()
    conn = sqlite3.connect(AGENT_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db() -> None:
    global _AGENT_DB_READY
    if _AGENT_DB_READY:
        return
    with _DB_LOCK:
        if _AGENT_DB_READY:
            return
        os.makedirs(os.path.dirname(AGENT_DB_PATH), exist_ok=True)
        _ensure_db_tables()
        _AGENT_DB_READY = True


def _ensure_db_tables() -> None:
    with sqlite3.connect(AGENT_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agencies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agency_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                global_role TEXT NOT NULL DEFAULT 'agent_user',
                is_active INTEGER NOT NULL DEFAULT 1,
                disabled_reason TEXT NOT NULL DEFAULT '',
                totp_secret TEXT NOT NULL DEFAULT '',
                two_factor_enabled INTEGER NOT NULL DEFAULT 0,
                failed_login_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT NOT NULL DEFAULT '',
                last_login_at TEXT NOT NULL DEFAULT '',
                last_login_ip TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT NOT NULL DEFAULT '',
                session_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agency_memberships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agency_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'agent_user',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(agency_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_backup_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code_hash TEXT NOT NULL,
                used_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_login_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                agency_id INTEGER,
                email TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 0,
                ip_address TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agency_id INTEGER,
                actor_user_id INTEGER,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL DEFAULT '',
                ip_address TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        # ── Financial tables ─────────────────────────────────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS markup_config (
                id INTEGER PRIMARY KEY,
                markup_flat_usd REAL NOT NULL DEFAULT 50.0,
                agency_split_pct REAL NOT NULL DEFAULT 50.0,
                updated_by_user_id INTEGER,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS markup_tiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                min_fare_usd REAL NOT NULL DEFAULT 0.0,
                max_fare_usd REAL,
                markup_type TEXT NOT NULL DEFAULT 'flat',
                markup_value REAL NOT NULL DEFAULT 50.0,
                agency_split_pct REAL NOT NULL DEFAULT 50.0,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agency_user_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agency_id INTEGER NOT NULL,
                requested_by_user_id INTEGER NOT NULL,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'agent_user',
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                reviewed_by_user_id INTEGER,
                review_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                duffel_order_id TEXT NOT NULL UNIQUE,
                duffel_offer_id TEXT NOT NULL DEFAULT '',
                booking_reference TEXT NOT NULL DEFAULT '',
                agent_user_id INTEGER,
                agency_id INTEGER,
                origin TEXT NOT NULL DEFAULT '',
                destination TEXT NOT NULL DEFAULT '',
                depart_date TEXT NOT NULL DEFAULT '',
                return_date TEXT NOT NULL DEFAULT '',
                trip_type TEXT NOT NULL DEFAULT 'oneway',
                cabin TEXT NOT NULL DEFAULT '',
                passenger_count INTEGER NOT NULL DEFAULT 1,
                passenger_names_json TEXT NOT NULL DEFAULT '[]',
                airline_name TEXT NOT NULL DEFAULT '',
                base_fare_usd REAL NOT NULL DEFAULT 0.0,
                markup_amount_usd REAL NOT NULL DEFAULT 0.0,
                platform_share_usd REAL NOT NULL DEFAULT 0.0,
                agency_share_usd REAL NOT NULL DEFAULT 0.0,
                currency TEXT NOT NULL DEFAULT 'USD',
                status TEXT NOT NULL DEFAULT 'confirmed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                agency_id INTEGER,
                origin TEXT NOT NULL DEFAULT '',
                destination TEXT NOT NULL DEFAULT '',
                depart_date TEXT NOT NULL DEFAULT '',
                return_date TEXT NOT NULL DEFAULT '',
                trip_type TEXT NOT NULL DEFAULT 'oneway',
                cabin TEXT NOT NULL DEFAULT '',
                passenger_count INTEGER NOT NULL DEFAULT 1,
                nonstop INTEGER NOT NULL DEFAULT 0,
                sort TEXT NOT NULL DEFAULT 'recommended',
                result_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS disbursements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agency_id INTEGER NOT NULL,
                amount_usd REAL NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_by_user_id INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agency_users_email ON agency_users(email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memberships_user ON agency_memberships(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON agent_audit_logs(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_platform_bookings_agency ON platform_bookings(agency_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_platform_bookings_agent ON platform_bookings(agent_user_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_platform_bookings_order ON platform_bookings(duffel_order_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_search_history_agency ON agent_search_history(agency_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_search_history_user ON agent_search_history(user_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_disbursements_agency ON disbursements(agency_id, created_at DESC)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                is_read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_markup_tiers_min ON markup_tiers(min_fare_usd)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_requests_agency ON agency_user_requests(agency_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_requests_status ON agency_user_requests(status, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read, created_at DESC)")
        conn.commit()


# ── Agency CRUD ───────────────────────────────────────────────────────────────

def create_agency(name: str, *, code: str = "") -> dict[str, Any]:
    ensure_db()
    now_value = _utc_now()
    safe_name = _norm_text(name, limit=160) or "Agency"
    normalized_code = _norm_text(code, limit=48).lower().replace(" ", "-")
    if not normalized_code:
        normalized_code = safe_name.lower().replace(" ", "-")
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agencies (code, name, status, created_at, updated_at)
            VALUES (?, ?, 'active', ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                updated_at = excluded.updated_at
            """,
            (normalized_code, safe_name, now_value, now_value),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM agencies WHERE code = ?", (normalized_code,)).fetchone()
    return dict(row or {})


def get_agency(agency_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM agencies WHERE id = ?", (_coerce_int(agency_id),)).fetchone()
    return dict(row or {}) if row else None


def list_agencies(*, limit: int = 200) -> list[dict[str, Any]]:
    safe_limit = max(1, min(500, _coerce_int(limit, 200)))
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, code, name, status, created_at, updated_at
            FROM agencies
            ORDER BY name COLLATE NOCASE ASC, id ASC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_agencies_with_stats() -> list[dict[str, Any]]:
    """Agencies enriched with user count, booking totals, earned balance, disbursed, owed."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                a.id,
                a.code,
                a.name,
                a.status,
                a.created_at,
                COUNT(DISTINCT m.user_id) AS user_count,
                COUNT(DISTINCT pb.id) AS booking_count,
                COALESCE(SUM(pb.base_fare_usd + pb.markup_amount_usd), 0) AS gross_revenue_usd,
                COALESCE(SUM(pb.markup_amount_usd), 0) AS total_markup_usd,
                COALESCE(SUM(pb.agency_share_usd), 0) AS total_earned_usd,
                COALESCE((
                    SELECT SUM(d.amount_usd) FROM disbursements d WHERE d.agency_id = a.id
                ), 0) AS total_disbursed_usd
            FROM agencies a
            LEFT JOIN agency_memberships m ON m.agency_id = a.id AND m.status = 'active'
            LEFT JOIN platform_bookings pb ON pb.agency_id = a.id AND pb.status != 'cancelled'
            GROUP BY a.id
            ORDER BY a.name COLLATE NOCASE ASC
            """
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["balance_owed_usd"] = _coerce_float(d.get("total_earned_usd")) - _coerce_float(d.get("total_disbursed_usd"))
        result.append(d)
    return result


# ── User CRUD ─────────────────────────────────────────────────────────────────

def create_user(
    *,
    email: str,
    password: str,
    first_name: str = "",
    last_name: str = "",
    global_role: str = "agent_user",
    agency_id: int | None = None,
    membership_role: str | None = None,
    is_active: bool = True,
    totp_secret: str = "",
    two_factor_enabled: bool = False,
) -> dict[str, Any]:
    ensure_db()
    now_value = _utc_now()
    safe_email = _norm_email(email)
    if not safe_email:
        raise ValueError("email_required")
    if get_user_by_email(safe_email):
        raise ValueError("email_exists")
    salt_hex = generate_password_salt()
    password_hash = hash_password(password, salt_hex)
    normalized_global_role = normalize_role(global_role)
    normalized_membership_role = normalize_role(membership_role or global_role)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agency_users (
                email, first_name, last_name, password_salt, password_hash,
                global_role, is_active, disabled_reason, totp_secret, two_factor_enabled,
                failed_login_attempts, locked_until, last_login_at, last_login_ip, last_seen_at,
                session_version, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, 0, '', '', '', '', 1, ?, ?)
            """,
            (
                safe_email,
                _norm_text(first_name, limit=80),
                _norm_text(last_name, limit=80),
                salt_hex,
                password_hash,
                normalized_global_role,
                1 if is_active else 0,
                _norm_text(totp_secret, limit=64),
                1 if two_factor_enabled else 0,
                now_value,
                now_value,
            ),
        )
        user_id = _coerce_int(conn.execute("SELECT last_insert_rowid()").fetchone()[0], 0)
        if agency_id:
            conn.execute(
                """
                INSERT OR REPLACE INTO agency_memberships (agency_id, user_id, role, status, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (_coerce_int(agency_id), user_id, normalized_membership_role, now_value, now_value),
            )
        conn.commit()
    return get_user_by_id(user_id) or {}


def get_user_by_email(email: str) -> dict[str, Any] | None:
    safe_email = _norm_email(email)
    if not safe_email:
        return None
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                u.*,
                m.agency_id,
                m.role AS membership_role,
                m.status AS membership_status,
                a.name AS agency_name,
                a.code AS agency_code
            FROM agency_users u
            LEFT JOIN agency_memberships m ON m.user_id = u.id AND m.status = 'active'
            LEFT JOIN agencies a ON a.id = m.agency_id
            WHERE u.email = ?
            ORDER BY m.id ASC
            LIMIT 1
            """,
            (safe_email,),
        ).fetchone()
    return _dict_from_row(row)


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                u.*,
                m.agency_id,
                m.role AS membership_role,
                m.status AS membership_status,
                a.name AS agency_name,
                a.code AS agency_code
            FROM agency_users u
            LEFT JOIN agency_memberships m ON m.user_id = u.id AND m.status = 'active'
            LEFT JOIN agencies a ON a.id = m.agency_id
            WHERE u.id = ?
            ORDER BY m.id ASC
            LIMIT 1
            """,
            (_coerce_int(user_id),),
        ).fetchone()
    return _dict_from_row(row)


def list_users(*, agency_id: int | None = None, limit: int = 12) -> list[dict[str, Any]]:
    safe_limit = max(1, min(100, _coerce_int(limit, 12)))
    params: list[Any] = []
    where_sql = ""
    if agency_id:
        where_sql = "WHERE m.agency_id = ?"
        params.append(_coerce_int(agency_id))
    params.append(safe_limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                u.*,
                m.agency_id,
                m.role AS membership_role,
                m.status AS membership_status,
                a.name AS agency_name,
                a.code AS agency_code
            FROM agency_users u
            LEFT JOIN agency_memberships m ON m.user_id = u.id AND m.status = 'active'
            LEFT JOIN agencies a ON a.id = m.agency_id
            {where_sql}
            ORDER BY u.updated_at DESC, u.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [_dict_from_row(row) or {} for row in rows]


def list_users_with_stats(*, agency_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Users with booking count and revenue totals."""
    safe_limit = max(1, min(500, _coerce_int(limit, 200)))
    params: list[Any] = []
    where_sql = ""
    if agency_id:
        where_sql = "WHERE m.agency_id = ?"
        params.append(_coerce_int(agency_id))
    params.append(safe_limit)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                u.id, u.email, u.first_name, u.last_name,
                u.global_role, u.is_active, u.locked_until,
                u.last_login_at, u.last_seen_at, u.created_at,
                m.agency_id, m.role AS membership_role,
                a.name AS agency_name,
                COUNT(DISTINCT pb.id) AS booking_count,
                COALESCE(SUM(pb.base_fare_usd + pb.markup_amount_usd), 0) AS gross_revenue_usd,
                COALESCE(SUM(pb.markup_amount_usd), 0) AS total_markup_usd
            FROM agency_users u
            LEFT JOIN agency_memberships m ON m.user_id = u.id AND m.status = 'active'
            LEFT JOIN agencies a ON a.id = m.agency_id
            LEFT JOIN platform_bookings pb ON pb.agent_user_id = u.id AND pb.status != 'cancelled'
            {where_sql}
            GROUP BY u.id
            ORDER BY u.last_login_at DESC, u.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["is_active"] = bool(_coerce_int(d.get("is_active"), 0))
        d["effective_role"] = resolve_effective_role(d.get("global_role"), d.get("membership_role"))
        result.append(d)
    return result


# ── Auth events / audit logs ──────────────────────────────────────────────────

def record_login_event(
    *,
    user_id: int | None,
    agency_id: int | None,
    email: str,
    event_type: str,
    success: bool,
    ip_address: str = "",
    user_agent: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_login_events (
                user_id, agency_id, email, event_type, success, ip_address, user_agent, details_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _coerce_int(user_id, 0) or None,
                _coerce_int(agency_id, 0) or None,
                _norm_email(email),
                _norm_text(event_type, limit=48),
                1 if success else 0,
                _norm_text(ip_address, limit=80),
                _norm_text(user_agent, limit=255),
                _json_dumps(details or {}),
                _utc_now(),
            ),
        )
        conn.commit()


def record_audit_log(
    *,
    action: str,
    entity_type: str,
    entity_id: str = "",
    actor_user_id: int | None = None,
    agency_id: int | None = None,
    ip_address: str = "",
    user_agent: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_audit_logs (
                agency_id, actor_user_id, action, entity_type, entity_id, ip_address, user_agent, details_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _coerce_int(agency_id, 0) or None,
                _coerce_int(actor_user_id, 0) or None,
                _norm_text(action, limit=80),
                _norm_text(entity_type, limit=80),
                _norm_text(entity_id, limit=80),
                _norm_text(ip_address, limit=80),
                _norm_text(user_agent, limit=255),
                _json_dumps(details or {}),
                _utc_now(),
            ),
        )
        conn.commit()


def list_login_events(
    *,
    agency_id: int | None = None,
    user_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    clauses: list[str] = []
    if agency_id:
        clauses.append("e.agency_id = ?")
        params.append(_coerce_int(agency_id))
    if user_id:
        clauses.append("e.user_id = ?")
        params.append(_coerce_int(user_id))
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(500, _coerce_int(limit, 100))))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT e.*, a.name AS agency_name,
                   u.first_name || ' ' || u.last_name AS actor_name
            FROM agent_login_events e
            LEFT JOIN agencies a ON a.id = e.agency_id
            LEFT JOIN agency_users u ON u.id = e.user_id
            {where_sql}
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def list_audit_logs(
    *,
    agency_id: int | None = None,
    user_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    clauses: list[str] = []
    if agency_id:
        clauses.append("l.agency_id = ?")
        params.append(_coerce_int(agency_id))
    if user_id:
        clauses.append("l.actor_user_id = ?")
        params.append(_coerce_int(user_id))
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(500, _coerce_int(limit, 100))))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT l.*, a.name AS agency_name, u.email AS actor_email,
                   u.first_name || ' ' || u.last_name AS actor_name
            FROM agent_audit_logs l
            LEFT JOIN agencies a ON a.id = l.agency_id
            LEFT JOIN agency_users u ON u.id = l.actor_user_id
            {where_sql}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


# ── Dashboard summary ─────────────────────────────────────────────────────────

def fetch_dashboard_summary(*, agency_id: int | None = None) -> dict[str, Any]:
    membership_filter = ""
    membership_params: list[Any] = []
    event_filter = ""
    event_params: list[Any] = []
    audit_filter = ""
    audit_params: list[Any] = []
    if agency_id:
        membership_filter = "WHERE m.agency_id = ?"
        membership_params.append(_coerce_int(agency_id))
        event_filter = "WHERE e.agency_id = ?"
        event_params.append(_coerce_int(agency_id))
        audit_filter = "WHERE l.agency_id = ?"
        audit_params.append(_coerce_int(agency_id))
    extra_active_clause = "AND u.is_active = 1" if membership_filter else "WHERE u.is_active = 1"
    extra_locked_clause = "AND u.locked_until != ''" if membership_filter else "WHERE u.locked_until != ''"
    with _connect() as conn:
        agency_count = 1 if agency_id else _coerce_int(conn.execute("SELECT COUNT(*) FROM agencies").fetchone()[0], 0)
        user_count = _coerce_int(
            conn.execute(
                f"""
                SELECT COUNT(DISTINCT u.id)
                FROM agency_users u
                LEFT JOIN agency_memberships m ON m.user_id = u.id AND m.status = 'active'
                {membership_filter}
                """,
                tuple(membership_params),
            ).fetchone()[0],
            0,
        )
        active_user_count = _coerce_int(
            conn.execute(
                f"""
                SELECT COUNT(DISTINCT u.id)
                FROM agency_users u
                LEFT JOIN agency_memberships m ON m.user_id = u.id AND m.status = 'active'
                {membership_filter}
                {extra_active_clause}
                """,
                tuple(membership_params),
            ).fetchone()[0],
            0,
        )
        locked_user_count = _coerce_int(
            conn.execute(
                f"""
                SELECT COUNT(DISTINCT u.id)
                FROM agency_users u
                LEFT JOIN agency_memberships m ON m.user_id = u.id AND m.status = 'active'
                {membership_filter}
                {extra_locked_clause}
                """,
                tuple(membership_params),
            ).fetchone()[0],
            0,
        )
        recent_logins = conn.execute(
            f"""
            SELECT e.*, a.name AS agency_name
            FROM agent_login_events e
            LEFT JOIN agencies a ON a.id = e.agency_id
            {event_filter}
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT 8
            """,
            tuple(event_params),
        ).fetchall()
        recent_audit = conn.execute(
            f"""
            SELECT l.*, a.name AS agency_name, u.email AS actor_email
            FROM agent_audit_logs l
            LEFT JOIN agencies a ON a.id = l.agency_id
            LEFT JOIN agency_users u ON u.id = l.actor_user_id
            {audit_filter}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT 8
            """,
            tuple(audit_params),
        ).fetchall()
    return {
        "agency_count": agency_count,
        "user_count": user_count,
        "active_user_count": active_user_count,
        "locked_user_count": locked_user_count,
        "recent_logins": [dict(row) for row in recent_logins],
        "recent_audit": [dict(row) for row in recent_audit],
    }


# ── 2FA helpers ───────────────────────────────────────────────────────────────

def update_two_factor(user_id: int, *, secret: str, backup_code_hashes: list[str]) -> dict[str, Any] | None:
    now_value = _utc_now()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE agency_users
            SET totp_secret = ?, two_factor_enabled = 1, updated_at = ?
            WHERE id = ?
            """,
            (_norm_text(secret, limit=64), now_value, _coerce_int(user_id)),
        )
        conn.execute("DELETE FROM agent_backup_codes WHERE user_id = ?", (_coerce_int(user_id),))
        for code_hash in backup_code_hashes:
            conn.execute(
                """
                INSERT INTO agent_backup_codes (user_id, code_hash, used_at, created_at)
                VALUES (?, ?, '', ?)
                """,
                (_coerce_int(user_id), _norm_text(code_hash, limit=80), now_value),
            )
        conn.commit()
    return get_user_by_id(user_id)


def consume_backup_code(user_id: int, code_hash: str) -> bool:
    now_value = _utc_now()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id FROM agent_backup_codes
            WHERE user_id = ? AND code_hash = ? AND used_at = ''
            ORDER BY id ASC
            LIMIT 1
            """,
            (_coerce_int(user_id), _norm_text(code_hash, limit=80)),
        ).fetchone()
        if row is None:
            return False
        conn.execute("UPDATE agent_backup_codes SET used_at = ? WHERE id = ?", (now_value, _coerce_int(row["id"])))
        conn.commit()
    return True


# ── Login tracking ────────────────────────────────────────────────────────────

def increment_failed_login(user_id: int, *, max_attempts: int = 5, lock_minutes: int = 15) -> dict[str, Any] | None:
    user = get_user_by_id(user_id)
    if not user:
        return None
    attempts = _coerce_int(user.get("failed_login_attempts"), 0) + 1
    locked_until = ""
    if attempts >= max(1, int(max_attempts)):
        locked_until = (datetime.now(timezone.utc) + timedelta(minutes=max(1, int(lock_minutes)))).isoformat(timespec="seconds")
    with _connect() as conn:
        conn.execute(
            """
            UPDATE agency_users
            SET failed_login_attempts = ?, locked_until = ?, updated_at = ?
            WHERE id = ?
            """,
            (attempts, locked_until, _utc_now(), _coerce_int(user_id)),
        )
        conn.commit()
    return get_user_by_id(user_id)


def reset_failed_login(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE agency_users
            SET failed_login_attempts = 0, locked_until = '', updated_at = ?
            WHERE id = ?
            """,
            (_utc_now(), _coerce_int(user_id)),
        )
        conn.commit()


def mark_login_success(user_id: int, *, ip_address: str = "") -> dict[str, Any] | None:
    now_value = _utc_now()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE agency_users
            SET failed_login_attempts = 0,
                locked_until = '',
                last_login_at = ?,
                last_login_ip = ?,
                last_seen_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now_value, _norm_text(ip_address, limit=80), now_value, now_value, _coerce_int(user_id)),
        )
        conn.commit()
    return get_user_by_id(user_id)


def touch_last_seen(user_id: int, *, ip_address: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE agency_users
            SET last_seen_at = ?, last_login_ip = COALESCE(NULLIF(?, ''), last_login_ip), updated_at = ?
            WHERE id = ?
            """,
            (_utc_now(), _norm_text(ip_address, limit=80), _utc_now(), _coerce_int(user_id)),
        )
        conn.commit()


def disable_user(user_id: int, *, reason: str = "") -> dict[str, Any] | None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE agency_users
            SET is_active = 0,
                disabled_reason = ?,
                session_version = session_version + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (_norm_text(reason, limit=240), _utc_now(), _coerce_int(user_id)),
        )
        conn.commit()
    return get_user_by_id(user_id)


def is_locked(user: dict[str, Any] | None) -> bool:
    if not user:
        return False
    locked_until = str(user.get("locked_until") or "").strip()
    if not locked_until:
        return False
    try:
        lock_dt = datetime.fromisoformat(locked_until)
        now_dt = datetime.now(timezone.utc)
        if lock_dt.tzinfo is None:
            lock_dt = lock_dt.replace(tzinfo=timezone.utc)
        return now_dt < lock_dt
    except Exception:
        return False


def has_any_user() -> bool:
    with _connect() as conn:
        count = _coerce_int(conn.execute("SELECT COUNT(*) FROM agency_users").fetchone()[0], 0)
    return count > 0


# ── Markup config ─────────────────────────────────────────────────────────────

def get_markup_config() -> dict[str, Any]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM markup_config WHERE id = 1").fetchone()
    if row:
        return dict(row)
    return {"id": 1, "markup_flat_usd": 50.0, "agency_split_pct": 50.0, "updated_by_user_id": None, "updated_at": ""}


def set_markup_config(
    *,
    markup_flat_usd: float,
    agency_split_pct: float,
    updated_by_user_id: int | None = None,
) -> dict[str, Any]:
    safe_flat = max(0.0, _coerce_float(markup_flat_usd))
    safe_split = max(0.0, min(100.0, _coerce_float(agency_split_pct)))
    now_value = _utc_now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO markup_config (id, markup_flat_usd, agency_split_pct, updated_by_user_id, updated_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                markup_flat_usd = excluded.markup_flat_usd,
                agency_split_pct = excluded.agency_split_pct,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_at = excluded.updated_at
            """,
            (safe_flat, safe_split, _coerce_int(updated_by_user_id, 0) or None, now_value),
        )
        conn.commit()
    return get_markup_config()


# ── Markup tiers ──────────────────────────────────────────────────────────────

def get_markup_tiers() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM markup_tiers ORDER BY sort_order ASC, min_fare_usd ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def add_markup_tier(
    *,
    min_fare_usd: float,
    max_fare_usd: float | None,
    markup_type: str,
    markup_value: float,
    agency_split_pct: float,
    sort_order: int = 0,
    created_by_user_id: int | None = None,
) -> dict[str, Any]:
    safe_min = max(0.0, _coerce_float(min_fare_usd))
    safe_max = None if max_fare_usd is None else max(safe_min, _coerce_float(max_fare_usd))
    safe_type = "pct" if str(markup_type or "").strip() == "pct" else "flat"
    safe_value = max(0.0, _coerce_float(markup_value))
    safe_split = max(0.0, min(100.0, _coerce_float(agency_split_pct)))
    now_value = _utc_now()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO markup_tiers
                (min_fare_usd, max_fare_usd, markup_type, markup_value,
                 agency_split_pct, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (safe_min, safe_max, safe_type, safe_value, safe_split,
             int(sort_order), now_value, now_value),
        )
        conn.commit()
        tier_id = cur.lastrowid
    with _connect() as conn:
        row = conn.execute("SELECT * FROM markup_tiers WHERE id = ?", (tier_id,)).fetchone()
    return dict(row) if row else {}


def delete_markup_tier(tier_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM markup_tiers WHERE id = ?", (_coerce_int(tier_id),))
        conn.commit()
    return (cur.rowcount or 0) > 0


def get_markup_for_fare(base_fare_usd: float) -> dict[str, Any]:
    """Find matching markup tier for a base fare and return split amounts. Falls back to markup_config."""
    fare = max(0.0, _coerce_float(base_fare_usd))
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM markup_tiers
            WHERE min_fare_usd <= ?
              AND (max_fare_usd IS NULL OR max_fare_usd > ?)
            ORDER BY min_fare_usd DESC
            LIMIT 1
            """,
            (fare, fare),
        ).fetchone()
    if row:
        tier = dict(row)
        markup_type = tier.get("markup_type", "flat")
        markup_value = _coerce_float(tier.get("markup_value", 50.0))
        agency_split_pct = _coerce_float(tier.get("agency_split_pct", 50.0))
        markup_amount = round(fare * markup_value / 100.0, 4) if markup_type == "pct" else markup_value
        agency_share = round(markup_amount * agency_split_pct / 100.0, 4)
        platform_share = round(markup_amount - agency_share, 4)
        return {
            "tier_id": tier.get("id"),
            "markup_amount": markup_amount,
            "platform_share": platform_share,
            "agency_share": agency_share,
            "markup_type": markup_type,
            "markup_value": markup_value,
            "agency_split_pct": agency_split_pct,
        }
    cfg = get_markup_config()
    markup_amount = _coerce_float(cfg.get("markup_flat_usd", 50.0))
    agency_split = _coerce_float(cfg.get("agency_split_pct", 50.0))
    agency_share = round(markup_amount * agency_split / 100.0, 4)
    platform_share = round(markup_amount - agency_share, 4)
    return {
        "tier_id": None,
        "markup_amount": markup_amount,
        "platform_share": platform_share,
        "agency_share": agency_share,
        "markup_type": "flat",
        "markup_value": markup_amount,
        "agency_split_pct": agency_split,
    }


# ── Platform bookings ─────────────────────────────────────────────────────────

def record_platform_booking(
    *,
    duffel_order_id: str,
    duffel_offer_id: str = "",
    booking_reference: str = "",
    agent_user_id: int | None = None,
    agency_id: int | None = None,
    origin: str = "",
    destination: str = "",
    depart_date: str = "",
    return_date: str = "",
    trip_type: str = "oneway",
    cabin: str = "",
    passenger_count: int = 1,
    passenger_names: list[str] | None = None,
    airline_name: str = "",
    base_fare_usd: float = 0.0,
    currency: str = "USD",
) -> dict[str, Any] | None:
    """Record a booking and compute markup splits from tiered markup config."""
    split = get_markup_for_fare(base_fare_usd)
    markup_amount = split["markup_amount"]
    agency_share = split["agency_share"]
    platform_share = split["platform_share"]
    now_value = _utc_now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO platform_bookings (
                duffel_order_id, duffel_offer_id, booking_reference,
                agent_user_id, agency_id,
                origin, destination, depart_date, return_date, trip_type, cabin,
                passenger_count, passenger_names_json, airline_name,
                base_fare_usd, markup_amount_usd, platform_share_usd, agency_share_usd,
                currency, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)
            """,
            (
                _norm_text(duffel_order_id, limit=80),
                _norm_text(duffel_offer_id, limit=80),
                _norm_text(booking_reference, limit=40).upper(),
                _coerce_int(agent_user_id, 0) or None,
                _coerce_int(agency_id, 0) or None,
                _norm_text(origin, limit=10).upper(),
                _norm_text(destination, limit=10).upper(),
                _norm_text(depart_date, limit=20),
                _norm_text(return_date, limit=20),
                _norm_text(trip_type, limit=20),
                _norm_text(cabin, limit=30),
                max(1, _coerce_int(passenger_count, 1)),
                _json_dumps(passenger_names or []),
                _norm_text(airline_name, limit=120),
                max(0.0, _coerce_float(base_fare_usd)),
                markup_amount,
                platform_share,
                agency_share,
                _norm_text(currency, limit=10).upper() or "USD",
                now_value,
                now_value,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM platform_bookings WHERE duffel_order_id = ?",
            (_norm_text(duffel_order_id, limit=80),),
        ).fetchone()
    return dict(row) if row else None


def get_platform_booking_by_order_id(order_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT pb.*,
                   a.name AS agency_name,
                   u.email AS agent_email,
                   u.first_name || ' ' || u.last_name AS agent_name
            FROM platform_bookings pb
            LEFT JOIN agencies a ON a.id = pb.agency_id
            LEFT JOIN agency_users u ON u.id = pb.agent_user_id
            WHERE pb.duffel_order_id = ?
            LIMIT 1
            """,
            (_norm_text(order_id, limit=80),),
        ).fetchone()
    return dict(row) if row else None


def get_platform_booking_by_id(booking_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT pb.*,
                   a.name AS agency_name,
                   u.email AS agent_email,
                   u.first_name || ' ' || u.last_name AS agent_name
            FROM platform_bookings pb
            LEFT JOIN agencies a ON a.id = pb.agency_id
            LEFT JOIN agency_users u ON u.id = pb.agent_user_id
            WHERE pb.id = ?
            LIMIT 1
            """,
            (_coerce_int(booking_id),),
        ).fetchone()
    return dict(row) if row else None


def list_platform_bookings(
    *,
    agency_id: int | None = None,
    agent_user_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    clauses: list[str] = []
    if agency_id:
        clauses.append("pb.agency_id = ?")
        params.append(_coerce_int(agency_id))
    if agent_user_id:
        clauses.append("pb.agent_user_id = ?")
        params.append(_coerce_int(agent_user_id))
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.extend([max(1, min(500, _coerce_int(limit, 100))), max(0, _coerce_int(offset, 0))])
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT pb.*,
                   a.name AS agency_name,
                   u.email AS agent_email,
                   u.first_name || ' ' || u.last_name AS agent_name
            FROM platform_bookings pb
            LEFT JOIN agencies a ON a.id = pb.agency_id
            LEFT JOIN agency_users u ON u.id = pb.agent_user_id
            {where_sql}
            ORDER BY pb.created_at DESC, pb.id DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


# ── Financial summaries ───────────────────────────────────────────────────────

def get_financial_summary(*, agency_id: int | None = None) -> dict[str, Any]:
    """
    Platform-wide (agency_id=None) or per-agency financial totals.
    Returns gross revenue, markup, platform share, agency share, disbursed, balance owed.
    """
    params: list[Any] = []
    clauses: list[str] = ["pb.status != 'cancelled'"]
    if agency_id:
        clauses.append("pb.agency_id = ?")
        params.append(_coerce_int(agency_id))
    where_sql = "WHERE " + " AND ".join(clauses)

    disb_params: list[Any] = []
    disb_where = ""
    if agency_id:
        disb_where = "WHERE agency_id = ?"
        disb_params.append(_coerce_int(agency_id))

    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS booking_count,
                COALESCE(SUM(pb.base_fare_usd + pb.markup_amount_usd), 0) AS gross_revenue_usd,
                COALESCE(SUM(pb.base_fare_usd), 0) AS total_base_fare_usd,
                COALESCE(SUM(pb.markup_amount_usd), 0) AS total_markup_usd,
                COALESCE(SUM(pb.platform_share_usd), 0) AS total_platform_share_usd,
                COALESCE(SUM(pb.agency_share_usd), 0) AS total_agency_share_usd
            FROM platform_bookings pb
            {where_sql}
            """,
            tuple(params),
        ).fetchone()
        disb_row = conn.execute(
            f"SELECT COALESCE(SUM(amount_usd), 0) AS total_disbursed_usd FROM disbursements {disb_where}",
            tuple(disb_params),
        ).fetchone()
    totals = dict(row) if row else {}
    totals["total_disbursed_usd"] = _coerce_float(dict(disb_row).get("total_disbursed_usd", 0) if disb_row else 0)
    totals["balance_owed_usd"] = _coerce_float(totals.get("total_agency_share_usd", 0)) - totals["total_disbursed_usd"]
    return totals


def get_agency_balance(agency_id: int) -> dict[str, Any]:
    aid = _coerce_int(agency_id)
    with _connect() as conn:
        earned_row = conn.execute(
            "SELECT COALESCE(SUM(agency_share_usd), 0) AS earned FROM platform_bookings WHERE agency_id = ? AND status != 'cancelled'",
            (aid,),
        ).fetchone()
        disbursed_row = conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) AS disbursed FROM disbursements WHERE agency_id = ?",
            (aid,),
        ).fetchone()
    earned = _coerce_float(earned_row["earned"] if earned_row else 0)
    disbursed = _coerce_float(disbursed_row["disbursed"] if disbursed_row else 0)
    return {"earned_usd": earned, "disbursed_usd": disbursed, "balance_usd": earned - disbursed}


def get_booking_status_counts(*, agency_id: int | None = None) -> dict[str, int]:
    params: list[Any] = []
    where_sql = ""
    if agency_id:
        where_sql = "WHERE agency_id = ?"
        params.append(_coerce_int(agency_id))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT status, COUNT(*) AS count
            FROM platform_bookings
            {where_sql}
            GROUP BY status
            """,
            tuple(params),
        ).fetchall()
    counts = {str(row["status"] or "unknown").lower(): _coerce_int(row["count"]) for row in rows}
    return {
        "total": sum(counts.values()),
        "confirmed": counts.get("confirmed", 0),
        "ticketed": counts.get("ticketed", 0),
        "pending": counts.get("pending", 0),
        "on_hold": counts.get("on_hold", 0) + counts.get("hold", 0),
        "cancelled": counts.get("cancelled", 0),
        "refunded": counts.get("refunded", 0),
        "refund_requested": counts.get("refund_requested", 0),
        "unpaid": counts.get("unpaid", 0),
    }


# ── Agent search history ─────────────────────────────────────────────────────

def record_agent_search(
    *,
    user_id: int | None,
    agency_id: int | None,
    params: dict[str, Any],
    result_count: int = 0,
    error: str = "",
) -> dict[str, Any] | None:
    now_value = _utc_now()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO agent_search_history (
                user_id, agency_id, origin, destination, depart_date, return_date,
                trip_type, cabin, passenger_count, nonstop, sort, result_count, error, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _coerce_int(user_id, 0) or None,
                _coerce_int(agency_id, 0) or None,
                _norm_text(params.get("origin"), limit=10).upper(),
                _norm_text(params.get("destination"), limit=10).upper(),
                _norm_text(params.get("depart_date"), limit=20),
                _norm_text(params.get("return_date"), limit=20),
                _norm_text(params.get("trip_type"), limit=20) or "oneway",
                _norm_text(params.get("cabin"), limit=30).upper(),
                max(1, _coerce_int(params.get("passengers"), 1)),
                1 if bool(params.get("nonstop")) else 0,
                _norm_text(params.get("sort"), limit=30) or "recommended",
                max(0, _coerce_int(result_count, 0)),
                _norm_text(error, limit=240),
                now_value,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM agent_search_history WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row) if row else None


def list_agent_searches(
    *,
    agency_id: int | None = None,
    user_id: int | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    clauses: list[str] = []
    if agency_id:
        clauses.append("agency_id = ?")
        params.append(_coerce_int(agency_id))
    if user_id:
        clauses.append("user_id = ?")
        params.append(_coerce_int(user_id))
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(50, _coerce_int(limit, 8))))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM agent_search_history
            {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def list_popular_agent_routes(*, agency_id: int | None = None, limit: int = 5) -> list[dict[str, Any]]:
    params: list[Any] = []
    where_sql = "WHERE origin != '' AND destination != ''"
    if agency_id:
        where_sql += " AND agency_id = ?"
        params.append(_coerce_int(agency_id))
    params.append(max(1, min(20, _coerce_int(limit, 5))))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT
                origin,
                destination,
                cabin,
                COUNT(*) AS search_count,
                MAX(created_at) AS last_searched_at,
                COALESCE(MIN(NULLIF(result_count, 0)), 0) AS min_result_count
            FROM agent_search_history
            {where_sql}
            GROUP BY origin, destination, cabin
            ORDER BY search_count DESC, last_searched_at DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


# ── Disbursements ─────────────────────────────────────────────────────────────

def create_disbursement(
    *,
    agency_id: int,
    amount_usd: float,
    note: str = "",
    created_by_user_id: int | None = None,
) -> dict[str, Any]:
    now_value = _utc_now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO disbursements (agency_id, amount_usd, note, created_by_user_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                _coerce_int(agency_id),
                max(0.01, _coerce_float(amount_usd)),
                _norm_text(note, limit=240),
                _coerce_int(created_by_user_id, 0) or None,
                now_value,
            ),
        )
        row_id = _coerce_int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()
        row = conn.execute("SELECT * FROM disbursements WHERE id = ?", (row_id,)).fetchone()
    return dict(row) if row else {}


def list_disbursements(*, agency_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    params: list[Any] = []
    where_sql = ""
    if agency_id:
        where_sql = "WHERE d.agency_id = ?"
        params.append(_coerce_int(agency_id))
    params.append(max(1, min(500, _coerce_int(limit, 100))))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT d.*, a.name AS agency_name,
                   u.email AS created_by_email,
                   u.first_name || ' ' || u.last_name AS created_by_name
            FROM disbursements d
            LEFT JOIN agencies a ON a.id = d.agency_id
            LEFT JOIN agency_users u ON u.id = d.created_by_user_id
            {where_sql}
            ORDER BY d.created_at DESC, d.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


# ── Notifications ─────────────────────────────────────────────────────────────

def create_notification(
    *,
    user_id: int,
    notification_type: str,
    title: str,
    body: str,
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO notifications (user_id, type, title, body, is_read, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            (
                _coerce_int(user_id),
                _norm_text(notification_type, limit=80),
                _norm_text(title, limit=200),
                _norm_text(body, limit=1000),
                _utc_now(),
            ),
        )
        conn.commit()


def list_notifications(user_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (_coerce_int(user_id), max(1, min(100, _coerce_int(limit, 20)))),
        ).fetchall()
    return [dict(r) for r in rows]


def count_unread_notifications(user_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND is_read = 0",
            (_coerce_int(user_id),),
        ).fetchone()
    return int(dict(row).get("n", 0)) if row else 0


def mark_notifications_read(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0",
            (_coerce_int(user_id),),
        )
        conn.commit()


# ── Password reset ─────────────────────────────────────────────────────────────

def reset_user_password(
    *,
    user_id: int,
    new_password: str,
    reset_by_user_id: int | None = None,
) -> bool:
    """Set a new password for any agent portal user. Logs the action."""
    from agent_security import generate_password_salt, hash_password
    salt = generate_password_salt()
    pw_hash = hash_password(new_password, salt)
    now_value = _utc_now()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE agency_users SET password_salt = ?, password_hash = ?, updated_at = ? WHERE id = ?",
            (salt, pw_hash, now_value, _coerce_int(user_id)),
        )
        conn.commit()
        updated = (cur.rowcount or 0) > 0
    if updated:
        record_audit_log(
            action="password_reset",
            entity_type="agency_user",
            entity_id=str(user_id),
            actor_user_id=reset_by_user_id,
            details={"reset_by": reset_by_user_id},
        )
    return updated


# ── Agency user requests ───────────────────────────────────────────────────────

def create_user_request(
    *,
    agency_id: int,
    requested_by_user_id: int,
    first_name: str,
    last_name: str,
    email: str,
    role: str = "agent_user",
    notes: str = "",
) -> dict[str, Any]:
    from agent_security import normalize_role
    now_value = _utc_now()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO agency_user_requests
                (agency_id, requested_by_user_id, first_name, last_name, email,
                 role, notes, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                _coerce_int(agency_id),
                _coerce_int(requested_by_user_id),
                _norm_text(first_name, limit=80),
                _norm_text(last_name, limit=80),
                _norm_text(email, limit=160).lower(),
                normalize_role(role),
                _norm_text(notes, limit=500),
                now_value,
                now_value,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM agency_user_requests WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row) if row else {}


def list_user_requests(
    *,
    agency_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    clauses: list[str] = []
    if agency_id:
        clauses.append("r.agency_id = ?")
        params.append(_coerce_int(agency_id))
    if status:
        clauses.append("r.status = ?")
        params.append(str(status).strip())
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(500, _coerce_int(limit, 100))))
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT r.*,
                   a.name AS agency_name,
                   req.email AS requested_by_email,
                   req.first_name || ' ' || req.last_name AS requested_by_name,
                   rev.email AS reviewed_by_email
            FROM agency_user_requests r
            LEFT JOIN agencies a ON a.id = r.agency_id
            LEFT JOIN agency_users req ON req.id = r.requested_by_user_id
            LEFT JOIN agency_users rev ON rev.id = r.reviewed_by_user_id
            {where_sql}
            ORDER BY r.created_at DESC, r.id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


def approve_user_request(
    *,
    request_id: int,
    reviewed_by_user_id: int,
    review_note: str = "",
    initial_password: str,
) -> dict[str, Any] | None:
    """Approve a pending request — creates the agency user account."""
    from agent_security import normalize_role, generate_password_salt, hash_password
    now_value = _utc_now()
    with _connect() as conn:
        req_row = conn.execute(
            "SELECT * FROM agency_user_requests WHERE id = ? AND status = 'pending'",
            (_coerce_int(request_id),),
        ).fetchone()
    if not req_row:
        return None
    req = dict(req_row)
    salt = generate_password_salt()
    pw_hash = hash_password(initial_password, salt)
    new_user_id: int | None = None
    with _connect() as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO agency_users
                    (email, first_name, last_name, password_salt, password_hash,
                     global_role, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    req["email"],
                    req["first_name"],
                    req["last_name"],
                    salt,
                    pw_hash,
                    normalize_role(req.get("role")),
                    now_value,
                    now_value,
                ),
            )
            new_user_id = cur.lastrowid
            conn.execute(
                """
                INSERT OR IGNORE INTO agency_memberships (user_id, agency_id, role, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (new_user_id, req["agency_id"], normalize_role(req.get("role")), now_value),
            )
            conn.execute(
                """
                UPDATE agency_user_requests
                SET status = 'approved', reviewed_by_user_id = ?, review_note = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    _coerce_int(reviewed_by_user_id),
                    _norm_text(review_note, limit=500),
                    now_value,
                    _coerce_int(request_id),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    record_audit_log(
        action="user_request_approved",
        entity_type="agency_user_request",
        entity_id=str(request_id),
        actor_user_id=reviewed_by_user_id,
        agency_id=req.get("agency_id"),
        details={"new_user_id": new_user_id},
    )
    return get_user_by_id(new_user_id)


def reject_user_request(
    *,
    request_id: int,
    reviewed_by_user_id: int,
    review_note: str = "",
) -> bool:
    now_value = _utc_now()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE agency_user_requests
            SET status = 'rejected', reviewed_by_user_id = ?, review_note = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (
                _coerce_int(reviewed_by_user_id),
                _norm_text(review_note, limit=500),
                now_value,
                _coerce_int(request_id),
            ),
        )
        conn.commit()
    updated = (cur.rowcount or 0) > 0
    if updated:
        record_audit_log(
            action="user_request_rejected",
            entity_type="agency_user_request",
            entity_id=str(request_id),
            actor_user_id=reviewed_by_user_id,
            details={"rejected_by": reviewed_by_user_id},
        )
    return updated
