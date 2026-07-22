from __future__ import annotations

import os
from datetime import datetime, timedelta
from functools import wraps
from typing import Any
from urllib.parse import quote

from flask import Blueprint, current_app, g, redirect, render_template, request, session, url_for

import agent_store
from agent_security import (
    device_label,
    generate_backup_codes,
    generate_totp_secret,
    hash_backup_code,
    normalize_role,
    resolve_effective_role,
    role_permissions,
    verify_password,
    verify_totp_code,
)

agent_bp = Blueprint("agent", __name__, url_prefix="/agent")

_SESSION_USER_KEY = "ngf_agent_user_id"
_SESSION_VERSION_KEY = "ngf_agent_session_version"
_SESSION_LAST_SEEN_KEY = "ngf_agent_last_seen_at"
_PENDING_USER_KEY = "ngf_agent_pending_user_id"
_PENDING_SECRET_KEY = "ngf_agent_pending_totp_secret"
_NOTICE_KEY = "ngf_agent_notice"
_ERROR_KEY = "ngf_agent_error"
_BACKUP_CODES_KEY = "ngf_agent_backup_codes"


def _now_utc() -> datetime:
    return datetime.utcnow()


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _idle_timeout_minutes() -> int:
    raw = current_app.config.get("NGF_AGENT_IDLE_TIMEOUT_MINUTES")
    try:
        value = int(raw or os.getenv("NGF_AGENT_IDLE_TIMEOUT_MINUTES", "20"))
    except Exception:
        value = 20
    return max(15, min(30, value))


def _lock_minutes() -> int:
    raw = current_app.config.get("NGF_AGENT_LOCK_MINUTES")
    try:
        value = int(raw or os.getenv("NGF_AGENT_LOCK_MINUTES", "15"))
    except Exception:
        value = 15
    return max(5, min(60, value))


def _client_ip() -> str:
    forwarded = str(request.headers.get("X-Forwarded-For") or "").strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:80]
    return str(request.remote_addr or "").strip()[:80]


def _user_agent() -> str:
    return str(request.headers.get("User-Agent") or "").strip()[:255]


def _set_notice(*, notice: str = "", error: str = "") -> None:
    if notice:
        session[_NOTICE_KEY] = str(notice).strip()
    else:
        session.pop(_NOTICE_KEY, None)
    if error:
        session[_ERROR_KEY] = str(error).strip()
    else:
        session.pop(_ERROR_KEY, None)


def _pop_messages() -> tuple[str, str]:
    return (
        str(session.pop(_NOTICE_KEY, "") or "").strip(),
        str(session.pop(_ERROR_KEY, "") or "").strip(),
    )


def _clear_agent_session() -> None:
    session.pop(_SESSION_USER_KEY, None)
    session.pop(_SESSION_VERSION_KEY, None)
    session.pop(_SESSION_LAST_SEEN_KEY, None)
    session.pop(_PENDING_USER_KEY, None)
    session.pop(_PENDING_SECRET_KEY, None)


def _set_pending_user(user_id: int) -> None:
    session[_PENDING_USER_KEY] = int(user_id)
    session.pop(_PENDING_SECRET_KEY, None)


def _pending_user() -> dict[str, Any] | None:
    user_id = session.get(_PENDING_USER_KEY)
    if not user_id:
        return None
    return agent_store.get_user_by_id(int(user_id))


def _complete_sign_in(user: dict[str, Any], *, source: str) -> None:
    ip_address = _client_ip()
    updated = agent_store.mark_login_success(int(user.get("id") or 0), ip_address=ip_address) or user
    session[_SESSION_USER_KEY] = int(updated.get("id") or 0)
    session[_SESSION_VERSION_KEY] = int(updated.get("session_version") or 1)
    session[_SESSION_LAST_SEEN_KEY] = _now_iso()
    session.pop(_PENDING_USER_KEY, None)
    session.pop(_PENDING_SECRET_KEY, None)
    agent_store.record_login_event(
        user_id=int(updated.get("id") or 0),
        agency_id=int(updated.get("agency_id") or 0) or None,
        email=str(updated.get("email") or ""),
        event_type="login_success",
        success=True,
        ip_address=ip_address,
        user_agent=_user_agent(),
        details={"source": source, "device": device_label(_user_agent())},
    )
    agent_store.record_audit_log(
        action="agent_login",
        entity_type="agency_user",
        entity_id=str(updated.get("id") or ""),
        actor_user_id=int(updated.get("id") or 0),
        agency_id=int(updated.get("agency_id") or 0) or None,
        ip_address=ip_address,
        user_agent=_user_agent(),
        details={"source": source},
    )


def _bootstrap_if_configured() -> None:
    agent_store.ensure_db()
    if agent_store.has_any_user():
        return
    email = str(os.getenv("NGF_AGENT_BOOTSTRAP_EMAIL", "") or "").strip().lower()
    password = str(os.getenv("NGF_AGENT_BOOTSTRAP_PASSWORD", "") or "")
    if not email or not password:
        return
    agency_name = str(os.getenv("NGF_AGENT_BOOTSTRAP_AGENCY", "Skairova HQ") or "Skairova HQ").strip()
    first_name = str(os.getenv("NGF_AGENT_BOOTSTRAP_FIRST_NAME", "Skairova") or "Skairova").strip()
    last_name = str(os.getenv("NGF_AGENT_BOOTSTRAP_LAST_NAME", "Admin") or "Admin").strip()
    role = normalize_role(os.getenv("NGF_AGENT_BOOTSTRAP_ROLE", "super_admin"), default="super_admin")
    agency = agent_store.create_agency(agency_name, code="skairova-hq")
    user = agent_store.create_user(
        email=email,
        password=password,
        first_name=first_name,
        last_name=last_name,
        global_role=role,
        agency_id=int(agency.get("id") or 0),
        membership_role=role,
        two_factor_enabled=False,
    )
    agent_store.record_audit_log(
        action="bootstrap_user_created",
        entity_type="agency_user",
        entity_id=str(user.get("id") or ""),
        actor_user_id=int(user.get("id") or 0),
        agency_id=int(agency.get("id") or 0) or None,
        details={"email": email, "role": role},
    )


def _safe_next_path(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw.startswith("/agent"):
        return url_for("agent.dashboard")
    return raw


def _render_agent(template_name: str, **context: Any):
    notice, error = _pop_messages()
    payload = {
        "agent_notice": notice,
        "agent_error": error,
        "agent_user": getattr(g, "agent_user", None),
        "agent_effective_role": getattr(g, "agent_effective_role", ""),
        "agent_permissions": getattr(g, "agent_permissions", set()),
        "bootstrap_needed": not agent_store.has_any_user(),
        "idle_timeout_minutes": _idle_timeout_minutes(),
    }
    payload.update(context)
    return render_template(template_name, **payload)


def require_agent_auth(permission: str | None = None):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not getattr(g, "agent_user", None):
                return redirect(url_for("agent.login", next=request.path))
            if permission and permission not in getattr(g, "agent_permissions", set()):
                _set_notice(error="You do not have permission to access that area.")
                return redirect(url_for("agent.dashboard"))
            return func(*args, **kwargs)

        return wrapper

    return decorator


@agent_bp.before_request
def agent_request_context() -> None:
    _bootstrap_if_configured()
    g.agent_user = None
    g.agent_effective_role = ""
    g.agent_permissions = set()

    user_id = session.get(_SESSION_USER_KEY)
    if not user_id:
        return
    user = agent_store.get_user_by_id(int(user_id))
    if not user or not bool(user.get("is_active")):
        _clear_agent_session()
        _set_notice(error="Your agent session is no longer active.")
        return
    if int(session.get(_SESSION_VERSION_KEY) or 0) != int(user.get("session_version") or 0):
        _clear_agent_session()
        _set_notice(error="Your agent session was revoked. Please sign in again.")
        return
    try:
        last_seen = datetime.fromisoformat(str(session.get(_SESSION_LAST_SEEN_KEY) or ""))
    except Exception:
        last_seen = _now_utc() - timedelta(minutes=999)
    if _now_utc() - last_seen > timedelta(minutes=_idle_timeout_minutes()):
        _clear_agent_session()
        _set_notice(error="Your agent session expired due to inactivity.")
        agent_store.record_login_event(
            user_id=int(user.get("id") or 0),
            agency_id=int(user.get("agency_id") or 0) or None,
            email=str(user.get("email") or ""),
            event_type="session_expired",
            success=False,
            ip_address=_client_ip(),
            user_agent=_user_agent(),
            details={"idle_timeout_minutes": _idle_timeout_minutes()},
        )
        return
    session[_SESSION_LAST_SEEN_KEY] = _now_iso()
    agent_store.touch_last_seen(int(user.get("id") or 0), ip_address=_client_ip())
    g.agent_user = user
    g.agent_effective_role = resolve_effective_role(user.get("global_role"), user.get("membership_role"))
    g.agent_permissions = role_permissions(user.get("global_role"), user.get("membership_role"))


@agent_bp.route("/", methods=["GET"])
def landing():
    if getattr(g, "agent_user", None):
        return redirect(url_for("agent.dashboard"))
    return redirect(url_for("agent.login"))


@agent_bp.route("/login", methods=["GET", "POST"])
def login():
    if getattr(g, "agent_user", None):
        return redirect(url_for("agent.dashboard"))
    next_path = _safe_next_path(request.args.get("next") or request.form.get("next") or "")
    if request.method == "GET":
        return _render_agent("agent/login.html", next_path=next_path)

    email = str(request.form.get("email") or "").strip().lower()
    password = str(request.form.get("password") or "")
    user = agent_store.get_user_by_email(email)
    if not user:
        agent_store.record_login_event(
            user_id=None,
            agency_id=None,
            email=email,
            event_type="login_failed",
            success=False,
            ip_address=_client_ip(),
            user_agent=_user_agent(),
            details={"reason": "unknown_email"},
        )
        _set_notice(error="Invalid email or password.")
        return redirect(url_for("agent.login", next=next_path))
    if not bool(user.get("is_active")):
        agent_store.record_login_event(
            user_id=int(user.get("id") or 0),
            agency_id=int(user.get("agency_id") or 0) or None,
            email=email,
            event_type="login_blocked",
            success=False,
            ip_address=_client_ip(),
            user_agent=_user_agent(),
            details={"reason": "disabled"},
        )
        _set_notice(error="This agent account has been disabled.")
        return redirect(url_for("agent.login", next=next_path))
    if agent_store.is_locked(user):
        _set_notice(error="This account is temporarily locked after repeated failed logins.")
        return redirect(url_for("agent.login", next=next_path))
    if not verify_password(password, str(user.get("password_salt") or ""), str(user.get("password_hash") or "")):
        updated = agent_store.increment_failed_login(
            int(user.get("id") or 0),
            max_attempts=5,
            lock_minutes=_lock_minutes(),
        ) or user
        agent_store.record_login_event(
            user_id=int(updated.get("id") or 0),
            agency_id=int(updated.get("agency_id") or 0) or None,
            email=email,
            event_type="login_failed",
            success=False,
            ip_address=_client_ip(),
            user_agent=_user_agent(),
            details={"reason": "bad_password", "attempts": int(updated.get("failed_login_attempts") or 0)},
        )
        if agent_store.is_locked(updated):
            _set_notice(error="This account is temporarily locked after repeated failed logins.")
        else:
            _set_notice(error="Invalid email or password.")
        return redirect(url_for("agent.login", next=next_path))

    agent_store.reset_failed_login(int(user.get("id") or 0))
    _set_pending_user(int(user.get("id") or 0))
    session["ngf_agent_next_path"] = next_path
    if bool(user.get("two_factor_enabled")) and str(user.get("totp_secret") or "").strip():
        return redirect(url_for("agent.verify_2fa"))
    return redirect(url_for("agent.setup_2fa"))


@agent_bp.route("/setup-2fa", methods=["GET", "POST"])
def setup_2fa():
    if getattr(g, "agent_user", None):
        return redirect(url_for("agent.dashboard"))
    user = _pending_user()
    if not user:
        _set_notice(error="Sign in first to continue with two-factor setup.")
        return redirect(url_for("agent.login"))
    if bool(user.get("two_factor_enabled")) and str(user.get("totp_secret") or "").strip():
        return redirect(url_for("agent.verify_2fa"))
    secret = str(session.get(_PENDING_SECRET_KEY) or "").strip()
    if not secret:
        secret = generate_totp_secret()
        session[_PENDING_SECRET_KEY] = secret
    account_label = str(user.get("email") or "").strip()
    issuer = "Skairova Agent"
    otpauth_url = f"otpauth://totp/{quote(issuer)}:{quote(account_label)}?secret={secret}&issuer={quote(issuer)}"

    if request.method == "POST":
        code = str(request.form.get("verification_code") or "").strip()
        if not verify_totp_code(secret, code):
            _set_notice(error="Enter the 6-digit code from your authenticator app.")
            return redirect(url_for("agent.setup_2fa"))
        backup_codes = generate_backup_codes()
        backup_hashes = [hash_backup_code(code_value) for code_value in backup_codes]
        updated = agent_store.update_two_factor(int(user.get("id") or 0), secret=secret, backup_code_hashes=backup_hashes) or user
        session[_BACKUP_CODES_KEY] = backup_codes
        _complete_sign_in(updated, source="totp_setup")
        _set_notice(notice="Two-factor authentication is now active.")
        return redirect(session.pop("ngf_agent_next_path", None) or url_for("agent.dashboard"))

    return _render_agent(
        "agent/setup_2fa.html",
        pending_user=user,
        totp_secret=secret,
        otpauth_url=otpauth_url,
    )


@agent_bp.route("/verify-2fa", methods=["GET", "POST"])
def verify_2fa():
    if getattr(g, "agent_user", None):
        return redirect(url_for("agent.dashboard"))
    user = _pending_user()
    if not user:
        _set_notice(error="Sign in first to continue.")
        return redirect(url_for("agent.login"))
    if not bool(user.get("two_factor_enabled")) or not str(user.get("totp_secret") or "").strip():
        return redirect(url_for("agent.setup_2fa"))
    if request.method == "POST":
        code = str(request.form.get("verification_code") or "").strip()
        secret = str(user.get("totp_secret") or "").strip()
        used_backup = False
        if verify_totp_code(secret, code):
            pass
        elif agent_store.consume_backup_code(int(user.get("id") or 0), hash_backup_code(code)):
            used_backup = True
        else:
            agent_store.record_login_event(
                user_id=int(user.get("id") or 0),
                agency_id=int(user.get("agency_id") or 0) or None,
                email=str(user.get("email") or ""),
                event_type="two_factor_failed",
                success=False,
                ip_address=_client_ip(),
                user_agent=_user_agent(),
                details={"reason": "invalid_code"},
            )
            _set_notice(error="Invalid 2FA or backup code.")
            return redirect(url_for("agent.verify_2fa"))
        _complete_sign_in(user, source="backup_code" if used_backup else "totp")
        if used_backup:
            _set_notice(notice="Backup code used. Generate a fresh set soon.")
        return redirect(session.pop("ngf_agent_next_path", None) or url_for("agent.dashboard"))

    return _render_agent("agent/verify_2fa.html", pending_user=user)


@agent_bp.route("/dashboard", methods=["GET"])
@require_agent_auth("agent.dashboard.view")
def dashboard():
    agent_user = g.agent_user
    agency_id = None
    if g.agent_effective_role not in {"skairova_admin", "super_admin"}:
        agency_id = int(agent_user.get("agency_id") or 0) or None
    summary = agent_store.fetch_dashboard_summary(agency_id=agency_id)
    managed_users = []
    if "agent.users.view" in g.agent_permissions:
        managed_users = agent_store.list_users(agency_id=agency_id, limit=10)
    backup_codes = session.pop(_BACKUP_CODES_KEY, [])
    return _render_agent(
        "agent/dashboard.html",
        summary=summary,
        managed_users=managed_users,
        backup_codes=backup_codes,
        agent_scope_label="Platform-wide" if agency_id is None else str(agent_user.get("agency_name") or "Agency"),
    )


@agent_bp.route("/admin/users/<int:user_id>/disable", methods=["POST"])
@require_agent_auth("agent.users.disable")
def disable_user(user_id: int):
    actor = g.agent_user
    target = agent_store.get_user_by_id(user_id)
    if not target:
        _set_notice(error="Agent account not found.")
        return redirect(url_for("agent.dashboard"))
    if int(actor.get("id") or 0) == int(target.get("id") or 0):
        _set_notice(error="You cannot disable your own account.")
        return redirect(url_for("agent.dashboard"))
    if g.agent_effective_role not in {"skairova_admin", "super_admin"}:
        _set_notice(error="You do not have permission to disable agent accounts.")
        return redirect(url_for("agent.dashboard"))
    reason = str(request.form.get("reason") or "Disabled by admin").strip() or "Disabled by admin"
    updated = agent_store.disable_user(user_id, reason=reason)
    agent_store.record_audit_log(
        action="agent_disabled",
        entity_type="agency_user",
        entity_id=str(user_id),
        actor_user_id=int(actor.get("id") or 0),
        agency_id=int(updated.get("agency_id") or actor.get("agency_id") or 0) or None,
        ip_address=_client_ip(),
        user_agent=_user_agent(),
        details={"reason": reason, "target_email": str(updated.get("email") or "")},
    )
    _set_notice(notice=f"Disabled {updated.get('email')}.")
    return redirect(url_for("agent.dashboard"))


@agent_bp.route("/logout", methods=["POST"])
def logout():
    user = getattr(g, "agent_user", None)
    if user:
        agent_store.record_login_event(
            user_id=int(user.get("id") or 0),
            agency_id=int(user.get("agency_id") or 0) or None,
            email=str(user.get("email") or ""),
            event_type="logout",
            success=True,
            ip_address=_client_ip(),
            user_agent=_user_agent(),
            details={},
        )
        agent_store.record_audit_log(
            action="agent_logout",
            entity_type="agency_user",
            entity_id=str(user.get("id") or ""),
            actor_user_id=int(user.get("id") or 0),
            agency_id=int(user.get("agency_id") or 0) or None,
            ip_address=_client_ip(),
            user_agent=_user_agent(),
            details={},
        )
    _clear_agent_session()
    _set_notice(notice="Signed out of the agent portal.")
    return redirect(url_for("agent.login"))
