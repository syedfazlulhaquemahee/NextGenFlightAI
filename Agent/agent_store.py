from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from agent_security import generate_password_salt, hash_password, normalize_role, resolve_effective_role

BASE_DIR = os.path.dirname(__file__)
AGENT_DB_PATH = (
    os.getenv("NGF_AGENT_DB_PATH", os.path.join(BASE_DIR, "data", "agent_portal.db")).strip()
    or os.path.join(BASE_DIR, "data", "agent_portal.db")
)
_AGENT_DB_READY = False


def configure(*, db_path: str | None = None) -> None:
    global AGENT_DB_PATH, _AGENT_DB_READY
    if db_path:
        AGENT_DB_PATH = str(db_path).strip() or AGENT_DB_PATH
    _AGENT_DB_READY = False


def _utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
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
    os.makedirs(os.path.dirname(AGENT_DB_PATH), exist_ok=True)
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agency_users_email ON agency_users(email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memberships_user ON agency_memberships(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON agent_audit_logs(created_at DESC)")
        conn.commit()
    _AGENT_DB_READY = True


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


def increment_failed_login(user_id: int, *, max_attempts: int = 5, lock_minutes: int = 15) -> dict[str, Any] | None:
    user = get_user_by_id(user_id)
    if not user:
        return None
    attempts = _coerce_int(user.get("failed_login_attempts"), 0) + 1
    locked_until = ""
    if attempts >= max(1, int(max_attempts)):
        locked_until = (datetime.utcnow() + timedelta(minutes=max(1, int(lock_minutes)))).isoformat(timespec="seconds")
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
        return datetime.utcnow() < datetime.fromisoformat(locked_until)
    except Exception:
        return False


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


def has_any_user() -> bool:
    with _connect() as conn:
        count = _coerce_int(conn.execute("SELECT COUNT(*) FROM agency_users").fetchone()[0], 0)
    return count > 0
