from __future__ import annotations

import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any
from urllib.parse import quote, urlparse

from flask import Blueprint, abort, current_app, g, jsonify, redirect, render_template, request, session, url_for

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
_CSRF_SESSION_KEY = "ngf_agent_csrf"

_BOOTSTRAP_DONE = False


@agent_bp.after_request
def _apply_agent_no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


# ── CSRF helpers ──────────────────────────────────────────────────────────────

def _csrf_token() -> str:
    if _CSRF_SESSION_KEY not in session:
        session[_CSRF_SESSION_KEY] = secrets.token_urlsafe(32)
    return session[_CSRF_SESSION_KEY]  # type: ignore[return-value]


def _validate_csrf() -> bool:
    if current_app.config.get("TESTING"):
        return True
    token = str(session.get(_CSRF_SESSION_KEY) or "").strip()
    submitted = str(request.form.get("_csrf") or "").strip()
    return bool(token and submitted and hmac.compare_digest(token, submitted))


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
    global _BOOTSTRAP_DONE
    if _BOOTSTRAP_DONE:
        return
    agent_store.ensure_db()
    if agent_store.has_any_user():
        _BOOTSTRAP_DONE = True
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
    _BOOTSTRAP_DONE = True


def _safe_next_path(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlparse(raw)
        if parsed.scheme or parsed.netloc:
            return url_for("agent.dashboard")
        if not parsed.path.startswith("/agent"):
            return url_for("agent.dashboard")
        return parsed.path
    except Exception:
        return url_for("agent.dashboard")


def _render_agent(template_name: str, **context: Any):
    notice, error = _pop_messages()
    user = getattr(g, "agent_user", None)
    payload = {
        "agent_notice": notice,
        "agent_error": error,
        "agent_user": user,
        "agent_effective_role": getattr(g, "agent_effective_role", ""),
        "agent_permissions": getattr(g, "agent_permissions", set()),
        "agent_sidebar": _agent_sidebar_context(user),
        "bootstrap_needed": not agent_store.has_any_user(),
        "idle_timeout_minutes": _idle_timeout_minutes(),
        "csrf_token": _csrf_token(),
    }
    payload.update(context)
    return render_template(template_name, **payload)


def _agent_sidebar_context(user: dict[str, Any] | None) -> dict[str, Any]:
    if not user:
        return {}
    effective_role = getattr(g, "agent_effective_role", "")
    agency_id = None if effective_role in {"skairova_admin", "super_admin"} else int(user.get("agency_id") or 0) or None
    finance = agent_store.get_financial_summary(agency_id=agency_id)
    support_phone = str(os.getenv("NGF_SUPPORT_PHONE", "") or "").strip()
    support_email = str(os.getenv("NGF_SUPPORT_EMAIL", "") or "").strip()
    return {
        "scope_label": "Platform-wide" if agency_id is None else str(user.get("agency_name") or "Agency"),
        "finance": finance,
        "support": {
            "phone": support_phone,
            "email": support_email,
            "configured": bool(support_phone or support_email),
        },
    }


def _shared_search_module():
    import app as consumer_app
    return consumer_app


def _format_agent_datetime(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "Not recorded"
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local_dt = parsed.astimezone()
        return local_dt.strftime("%b %-d, %-I:%M %p")
    except Exception:
        return raw.replace("T", " ")[:16]


def _booking_reference_label(booking: dict[str, Any]) -> str:
    ref = str(booking.get("booking_reference") or "").strip().upper()
    if ref:
        return f"PNR {ref}"
    order_id = str(booking.get("duffel_order_id") or "").strip()
    if order_id:
        return f"Order {order_id[-6:].upper()}"
    return f"Booking {booking.get('id') or ''}".strip()


def _extract_booking_travelers(bookings: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for booking in bookings:
        try:
            names = json.loads(str(booking.get("passenger_names_json") or "[]"))
        except Exception:
            names = []
        if not isinstance(names, list):
            continue
        route = " → ".join(part for part in [str(booking.get("origin") or "").strip(), str(booking.get("destination") or "").strip()] if part)
        for name_value in names:
            name = str(name_value or "").strip()
            if not name:
                continue
            key = name.lower()
            entry = seen.setdefault(
                key,
                {
                    "name": name,
                    "initials": "".join(part[:1] for part in name.split()[:2]).upper() or name[:2].upper(),
                    "trip_count": 0,
                    "last_route": route,
                },
            )
            entry["trip_count"] = int(entry.get("trip_count") or 0) + 1
            if route:
                entry["last_route"] = route
    return sorted(seen.values(), key=lambda item: (-int(item.get("trip_count") or 0), str(item.get("name") or "")))[:limit]


def _decorate_agent_flights(flights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decorated: list[dict[str, Any]] = []
    for flight in flights:
        item = dict(flight)
        price = float(item.get("price") or 0)
        markup = agent_store.get_markup_for_fare(price)
        item["agent_markup_amount"] = float(markup.get("markup_amount") or 0)
        item["agent_agency_share"] = float(markup.get("agency_share") or 0)
        item["agent_platform_share"] = float(markup.get("platform_share") or 0)
        item["agent_split_pct"] = float(markup.get("agency_split_pct") or 0)
        if markup.get("markup_type") == "pct":
            item["agent_markup_label"] = f"{float(markup.get('markup_value') or 0):.1f}%"
        else:
            item["agent_markup_label"] = f"${float(markup.get('markup_amount') or 0):.2f}"
        decorated.append(item)
    return decorated


def _agent_search_workspace_context(consumer_app: Any) -> dict[str, Any]:
    user = getattr(g, "agent_user", {}) or {}
    agency_id = _scoped_agency_id()
    finance_summary = agent_store.get_financial_summary(agency_id=agency_id)
    status_counts = agent_store.get_booking_status_counts(agency_id=agency_id)
    recent_bookings = agent_store.list_platform_bookings(agency_id=agency_id, limit=5)
    traveler_bookings = agent_store.list_platform_bookings(agency_id=agency_id, limit=200)
    markup_preview = agent_store.get_markup_for_fare(0)
    support_phone = str(os.getenv("NGF_SUPPORT_PHONE", "") or "").strip()
    support_email = str(os.getenv("NGF_SUPPORT_EMAIL", "") or "").strip()
    duffel_token = str(getattr(consumer_app, "DUFFEL_ACCESS_TOKEN", "") or "").strip()
    duffel_env = str(getattr(consumer_app, "DUFFEL_ENV", "") or "").strip()
    return {
        "workspace": {
            "scope_label": "Platform-wide" if agency_id is None else str(user.get("agency_name") or "Agency"),
            "finance": finance_summary,
            "status_counts": status_counts,
            "recent_bookings": recent_bookings,
            "saved_travelers": _extract_booking_travelers(traveler_bookings),
            "recent_searches": agent_store.list_agent_searches(agency_id=agency_id, limit=4),
            "popular_routes": agent_store.list_popular_agent_routes(agency_id=agency_id, limit=5),
            "markup_preview": markup_preview,
            "support": {
                "phone": support_phone,
                "email": support_email,
                "configured": bool(support_phone or support_email),
            },
            "duffel_status": "Configured" if duffel_token else "Not configured",
            "duffel_env": duffel_env.upper() if duffel_env else "",
            "last_login_label": _format_agent_datetime(user.get("last_login_at")),
            "office_id": (str(user.get("agency_code") or "").strip().upper() or ("PLATFORM" if agency_id is None else f"AGENCY-{agency_id}")),
            "idle_timeout_minutes": _idle_timeout_minutes(),
        },
        "booking_reference_label": _booking_reference_label,
    }


def _password_meets_criteria(password: Any) -> bool:
    candidate = str(password or "")
    return (
        len(candidate) >= 8
        and any(ch.isalpha() for ch in candidate)
        and any(ch.isdigit() for ch in candidate)
    )


def _platform_booking_for_order(order_id: str) -> dict[str, Any] | None:
    if not order_id:
        return None
    booking = agent_store.get_platform_booking_by_order_id(order_id)
    if not booking:
        return None
    if _is_platform_admin():
        return booking
    scoped_agency_id = _scoped_agency_id()
    if int(booking.get("agency_id") or 0) != int(scoped_agency_id or 0):
        return None
    return booking


def _is_platform_admin() -> bool:
    return getattr(g, "agent_effective_role", "") in {"skairova_admin", "super_admin"}


def _scoped_agency_id() -> int | None:
    """Returns agency_id for scoped (non-platform-admin) users, else None."""
    if _is_platform_admin():
        return None
    user = getattr(g, "agent_user", None)
    if not user:
        return None
    return int(user.get("agency_id") or 0) or None


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
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
    except Exception:
        last_seen = _now_utc() - timedelta(minutes=999)
    now = _now_utc()
    if now - last_seen > timedelta(minutes=_idle_timeout_minutes()):
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
    if now - last_seen >= timedelta(seconds=60):
        agent_store.touch_last_seen(int(user.get("id") or 0), ip_address=_client_ip())
    g.agent_user = user
    g.agent_effective_role = resolve_effective_role(user.get("global_role"), user.get("membership_role"))
    g.agent_permissions = role_permissions(user.get("global_role"), user.get("membership_role"))


# ── Auth routes ───────────────────────────────────────────────────────────────

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
    signed_out = str(request.args.get("signed_out") or "").strip().lower() in {"1", "true", "yes"}
    if request.method == "GET":
        return _render_agent("agent/login.html", next_path=next_path, signed_out=signed_out)

    if not _validate_csrf():
        abort(403)

    email = str(request.form.get("email") or "").strip().lower()
    password = str(request.form.get("password") or "")
    user = agent_store.get_user_by_email(email)
    if not user:
        agent_store.record_login_event(
            user_id=None, agency_id=None, email=email,
            event_type="login_failed", success=False,
            ip_address=_client_ip(), user_agent=_user_agent(),
            details={"reason": "unknown_email"},
        )
        _set_notice(error="Invalid email or password.")
        return redirect(url_for("agent.login", next=next_path))
    if not bool(user.get("is_active")):
        agent_store.record_login_event(
            user_id=int(user.get("id") or 0),
            agency_id=int(user.get("agency_id") or 0) or None,
            email=email, event_type="login_blocked", success=False,
            ip_address=_client_ip(), user_agent=_user_agent(),
            details={"reason": "disabled"},
        )
        _set_notice(error="This agent account has been disabled.")
        return redirect(url_for("agent.login", next=next_path))
    if agent_store.is_locked(user):
        _set_notice(error="This account is temporarily locked after repeated failed logins.")
        return redirect(url_for("agent.login", next=next_path))
    if not verify_password(password, str(user.get("password_salt") or ""), str(user.get("password_hash") or "")):
        updated = agent_store.increment_failed_login(
            int(user.get("id") or 0), max_attempts=5, lock_minutes=_lock_minutes(),
        ) or user
        agent_store.record_login_event(
            user_id=int(updated.get("id") or 0),
            agency_id=int(updated.get("agency_id") or 0) or None,
            email=email, event_type="login_failed", success=False,
            ip_address=_client_ip(), user_agent=_user_agent(),
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
        if not _validate_csrf():
            abort(403)
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
        if not _validate_csrf():
            abort(403)
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
                event_type="two_factor_failed", success=False,
                ip_address=_client_ip(), user_agent=_user_agent(),
                details={"reason": "invalid_code"},
            )
            _set_notice(error="Invalid 2FA or backup code.")
            return redirect(url_for("agent.verify_2fa"))
        _complete_sign_in(user, source="backup_code" if used_backup else "totp")
        if used_backup:
            _set_notice(notice="Backup code used. Generate a fresh set soon.")
        return redirect(session.pop("ngf_agent_next_path", None) or url_for("agent.dashboard"))

    return _render_agent("agent/verify_2fa.html", pending_user=user)


@agent_bp.route("/logout", methods=["POST"])
def logout():
    if not _validate_csrf():
        abort(403)
    user = getattr(g, "agent_user", None)
    if user:
        agent_store.record_login_event(
            user_id=int(user.get("id") or 0),
            agency_id=int(user.get("agency_id") or 0) or None,
            email=str(user.get("email") or ""),
            event_type="logout", success=True,
            ip_address=_client_ip(), user_agent=_user_agent(),
            details={},
        )
        agent_store.record_audit_log(
            action="agent_logout", entity_type="agency_user",
            entity_id=str(user.get("id") or ""),
            actor_user_id=int(user.get("id") or 0),
            agency_id=int(user.get("agency_id") or 0) or None,
            ip_address=_client_ip(), user_agent=_user_agent(),
            details={},
        )
    _clear_agent_session()
    _set_notice(notice="Signed out of the agent portal.")
    return redirect(url_for("agent.login", signed_out=1))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@agent_bp.route("/dashboard", methods=["GET"])
@require_agent_auth("agent.dashboard.view")
def dashboard():
    agent_user = g.agent_user
    user_id = int(agent_user.get("id") or 0)
    agency_id = _scoped_agency_id()
    summary = agent_store.fetch_dashboard_summary(agency_id=agency_id)
    finance_summary = agent_store.get_financial_summary(agency_id=agency_id)
    backup_codes = session.pop(_BACKUP_CODES_KEY, [])
    notifications = agent_store.list_notifications(user_id, limit=10)
    unread_count = agent_store.count_unread_notifications(user_id)
    review_requests: list[dict[str, Any]] = []
    review_request_count = 0
    user_requests: list[dict[str, Any]] = []
    if "agent.requests.review" in g.agent_permissions:
        review_requests = agent_store.list_user_requests(status="pending", limit=8)
        review_request_count = len(agent_store.list_user_requests(status="pending", limit=500))
    elif "agent.users.request" in g.agent_permissions and agency_id:
        recent_requests = agent_store.list_user_requests(agency_id=agency_id, limit=50)
        user_requests = [
            req for req in recent_requests
            if int(req.get("requested_by_user_id") or 0) == user_id
        ][:8]
    return _render_agent(
        "agent/dashboard.html",
        summary=summary,
        finance_summary=finance_summary,
        backup_codes=backup_codes,
        notifications=notifications,
        unread_count=unread_count,
        review_requests=review_requests,
        review_request_count=review_request_count,
        user_requests=user_requests,
        agent_scope_label="Platform-wide" if agency_id is None else str(agent_user.get("agency_name") or "Agency"),
    )


# ── Notifications API ─────────────────────────────────────────────────────────

@agent_bp.route("/notifications", methods=["GET"])
@require_agent_auth()
def notifications_json():
    user_id = int(g.agent_user.get("id") or 0)
    items = agent_store.list_notifications(user_id, limit=10)
    unread = agent_store.count_unread_notifications(user_id)
    return jsonify({"notifications": items, "unread_count": unread})


@agent_bp.route("/notifications/read", methods=["POST"])
@require_agent_auth()
def notifications_mark_read():
    if not _validate_csrf():
        abort(403)
    agent_store.mark_notifications_read(int(g.agent_user.get("id") or 0))
    return jsonify({"ok": True})


# ── Agencies ──────────────────────────────────────────────────────────────────

@agent_bp.route("/agencies", methods=["GET"])
@require_agent_auth("agent.agencies.view")
def agencies_list():
    agencies = agent_store.get_agencies_with_stats()
    return _render_agent("agent/agencies.html", agencies=agencies)


@agent_bp.route("/agencies/<int:agency_id>", methods=["GET"])
@require_agent_auth("agent.agencies.view")
def agency_detail(agency_id: int):
    agency = agent_store.get_agency(agency_id)
    if not agency:
        _set_notice(error="Agency not found.")
        return redirect(url_for("agent.agencies_list"))
    users = agent_store.list_users_with_stats(agency_id=agency_id)
    bookings = agent_store.list_platform_bookings(agency_id=agency_id, limit=50)
    finance = agent_store.get_financial_summary(agency_id=agency_id)
    disbursements = agent_store.list_disbursements(agency_id=agency_id, limit=20)
    balance = agent_store.get_agency_balance(agency_id)
    return _render_agent(
        "agent/agency_detail.html",
        agency=agency,
        users=users,
        bookings=bookings,
        finance=finance,
        disbursements=disbursements,
        balance=balance,
    )


# ── Users ─────────────────────────────────────────────────────────────────────

@agent_bp.route("/users", methods=["GET"])
@require_agent_auth("agent.users.view")
def users_list():
    agency_id = _scoped_agency_id()
    # Platform admins may filter by agency via ?agency_id=
    filter_agency_id = agency_id
    selected_agency: dict[str, Any] | None = None
    if _is_platform_admin():
        raw_aid = request.args.get("agency_id", "").strip()
        if raw_aid:
            try:
                filter_agency_id = int(raw_aid)
                selected_agency = agent_store.get_agency(filter_agency_id)
            except ValueError:
                filter_agency_id = None
    users = agent_store.list_users_with_stats(agency_id=filter_agency_id, limit=200)
    agencies = agent_store.list_agencies() if _is_platform_admin() else []
    return _render_agent(
        "agent/users.html",
        users=users,
        agencies=agencies,
        filter_agency_id=filter_agency_id,
        selected_agency=selected_agency,
    )


# ── Logs ──────────────────────────────────────────────────────────────────────

@agent_bp.route("/logs", methods=["GET"])
@require_agent_auth("agent.logs.view")
def logs():
    agency_id = _scoped_agency_id()
    filter_agency_id = agency_id
    filter_user_id: int | None = None
    selected_agency: dict[str, Any] | None = None
    selected_user: dict[str, Any] | None = None
    log_type = request.args.get("type", "auth").strip().lower()
    if log_type not in {"auth", "audit"}:
        log_type = "auth"

    if _is_platform_admin():
        raw_aid = request.args.get("agency_id", "").strip()
        if raw_aid:
            try:
                filter_agency_id = int(raw_aid)
                selected_agency = agent_store.get_agency(filter_agency_id)
            except ValueError:
                filter_agency_id = None

    raw_uid = request.args.get("user_id", "").strip()
    if raw_uid:
        try:
            filter_user_id = int(raw_uid)
            selected_user = agent_store.get_user_by_id(filter_user_id)
        except ValueError:
            filter_user_id = None

    if log_type == "audit":
        entries = agent_store.list_audit_logs(agency_id=filter_agency_id, user_id=filter_user_id, limit=200)
    else:
        entries = agent_store.list_login_events(agency_id=filter_agency_id, user_id=filter_user_id, limit=200)

    agencies = agent_store.list_agencies() if _is_platform_admin() else []
    agency_users: list[dict[str, Any]] = []
    if filter_agency_id:
        agency_users = agent_store.list_users(agency_id=filter_agency_id, limit=200)
    return _render_agent(
        "agent/logs.html",
        entries=entries,
        log_type=log_type,
        agencies=agencies,
        agency_users=agency_users,
        filter_agency_id=filter_agency_id,
        filter_user_id=filter_user_id,
        selected_agency=selected_agency,
        selected_user=selected_user,
    )


# ── Bookings ──────────────────────────────────────────────────────────────────

@agent_bp.route("/bookings", methods=["GET"])
@require_agent_auth("agent.bookings.view")
def bookings_list():
    agency_id = _scoped_agency_id()
    filter_agency_id = agency_id
    filter_user_id: int | None = None
    selected_agency: dict[str, Any] | None = None
    selected_user: dict[str, Any] | None = None

    if _is_platform_admin():
        raw_aid = request.args.get("agency_id", "").strip()
        if raw_aid:
            try:
                filter_agency_id = int(raw_aid)
                selected_agency = agent_store.get_agency(filter_agency_id)
            except ValueError:
                filter_agency_id = None

    raw_uid = request.args.get("user_id", "").strip()
    if raw_uid:
        try:
            filter_user_id = int(raw_uid)
            selected_user = agent_store.get_user_by_id(filter_user_id)
        except ValueError:
            filter_user_id = None

    bookings = agent_store.list_platform_bookings(
        agency_id=filter_agency_id,
        agent_user_id=filter_user_id,
        limit=200,
    )
    finance = agent_store.get_financial_summary(agency_id=filter_agency_id)
    agencies = agent_store.list_agencies() if _is_platform_admin() else []
    agency_users: list[dict[str, Any]] = []
    if filter_agency_id:
        agency_users = agent_store.list_users(agency_id=filter_agency_id, limit=200)
    return _render_agent(
        "agent/bookings.html",
        bookings=bookings,
        finance=finance,
        agencies=agencies,
        agency_users=agency_users,
        filter_agency_id=filter_agency_id,
        filter_user_id=filter_user_id,
        selected_agency=selected_agency,
        selected_user=selected_user,
    )


@agent_bp.route("/bookings/<int:booking_id>", methods=["GET"])
@require_agent_auth("agent.bookings.view")
def booking_detail(booking_id: int):
    booking = agent_store.get_platform_booking_by_id(booking_id)
    if not booking:
        _set_notice(error="Booking not found.")
        return redirect(url_for("agent.bookings_list"))
    # Scope check: non-platform-admin can only see their own agency's bookings
    if not _is_platform_admin():
        agency_id = _scoped_agency_id()
        if booking.get("agency_id") != agency_id:
            _set_notice(error="You do not have access to that booking.")
            return redirect(url_for("agent.bookings_list"))
    return _render_agent("agent/booking_detail.html", booking=booking)


# ── Finance ───────────────────────────────────────────────────────────────────

@agent_bp.route("/finance", methods=["GET"])
@require_agent_auth("agent.finance.view")
def finance():
    agency_id = _scoped_agency_id()
    filter_agency_id = agency_id
    selected_agency: dict[str, Any] | None = None

    if _is_platform_admin():
        raw_aid = request.args.get("agency_id", "").strip()
        if raw_aid:
            try:
                filter_agency_id = int(raw_aid)
                selected_agency = agent_store.get_agency(filter_agency_id)
            except ValueError:
                filter_agency_id = None

    summary = agent_store.get_financial_summary(agency_id=filter_agency_id)
    disbursements = agent_store.list_disbursements(agency_id=filter_agency_id, limit=100)
    agencies = agent_store.list_agencies() if _is_platform_admin() else []
    markup_cfg = agent_store.get_markup_config() if _is_platform_admin() else None

    # Per-agency breakdowns for platform-wide view
    agency_breakdowns: list[dict[str, Any]] = []
    if _is_platform_admin() and not filter_agency_id:
        agency_breakdowns = agent_store.get_agencies_with_stats()

    return _render_agent(
        "agent/finance.html",
        summary=summary,
        disbursements=disbursements,
        agencies=agencies,
        agency_breakdowns=agency_breakdowns,
        filter_agency_id=filter_agency_id,
        selected_agency=selected_agency,
        markup_cfg=markup_cfg,
    )


@agent_bp.route("/finance/disburse", methods=["POST"])
@require_agent_auth("agent.finance.disburse")
def disburse():
    if not _validate_csrf():
        abort(403)
    actor = g.agent_user
    raw_agency_id = str(request.form.get("agency_id") or "").strip()
    raw_amount = str(request.form.get("amount_usd") or "").strip()
    note = str(request.form.get("note") or "").strip()[:240]

    try:
        agency_id = int(raw_agency_id)
    except ValueError:
        _set_notice(error="Select a valid agency.")
        return redirect(url_for("agent.finance"))

    try:
        amount = float(raw_amount)
        if amount <= 0:
            raise ValueError
    except ValueError:
        _set_notice(error="Enter a valid disbursement amount greater than zero.")
        return redirect(url_for("agent.finance", agency_id=agency_id))

    agency = agent_store.get_agency(agency_id)
    if not agency:
        _set_notice(error="Agency not found.")
        return redirect(url_for("agent.finance"))

    balance = agent_store.get_agency_balance(agency_id)
    if amount > balance["balance_usd"] + 0.005:
        _set_notice(error=f"Disbursement amount exceeds agency balance of ${balance['balance_usd']:.2f}.")
        return redirect(url_for("agent.finance", agency_id=agency_id))

    agent_store.create_disbursement(
        agency_id=agency_id,
        amount_usd=amount,
        note=note or f"Manual disbursement to {agency.get('name', '')}",
        created_by_user_id=int(actor.get("id") or 0),
    )
    agent_store.record_audit_log(
        action="disbursement_created",
        entity_type="agency",
        entity_id=str(agency_id),
        actor_user_id=int(actor.get("id") or 0),
        agency_id=agency_id,
        ip_address=_client_ip(),
        user_agent=_user_agent(),
        details={"amount_usd": amount, "note": note, "agency_name": agency.get("name", "")},
    )
    _set_notice(notice=f"Disbursement of ${amount:.2f} recorded for {agency.get('name', '')}.")
    return redirect(url_for("agent.finance", agency_id=agency_id))


# ── Markup config ─────────────────────────────────────────────────────────────

@agent_bp.route("/settings/markup", methods=["GET", "POST"])
@require_agent_auth("agent.markup.manage")
def markup_settings():
    cfg = agent_store.get_markup_config()
    tiers = agent_store.get_markup_tiers()
    if request.method == "POST":
        if not _validate_csrf():
            abort(403)
        try:
            flat = float(str(request.form.get("markup_flat_usd") or "0").strip())
            split = float(str(request.form.get("agency_split_pct") or "50").strip())
            if flat < 0 or split < 0 or split > 100:
                raise ValueError
        except ValueError:
            _set_notice(error="Enter valid markup values (flat ≥ 0, split 0–100%).")
            return _render_agent("agent/markup_settings.html", cfg=cfg, tiers=tiers)
        agent_store.set_markup_config(
            markup_flat_usd=flat,
            agency_split_pct=split,
            updated_by_user_id=int(g.agent_user.get("id") or 0),
        )
        agent_store.record_audit_log(
            action="markup_config_updated",
            entity_type="markup_config",
            entity_id="1",
            actor_user_id=int(g.agent_user.get("id") or 0),
            ip_address=_client_ip(),
            user_agent=_user_agent(),
            details={"markup_flat_usd": flat, "agency_split_pct": split},
        )
        _set_notice(notice="Fallback markup configuration saved.")
        return redirect(url_for("agent.markup_settings"))
    return _render_agent("agent/markup_settings.html", cfg=cfg, tiers=tiers)


# ── Flight search (booking workspace) ────────────────────────────────────────

@agent_bp.route("/search", methods=["GET", "POST"])
@require_agent_auth("agent.dashboard.view")
def search():
    consumer_app = _shared_search_module()
    defaults = {
        "origin": "",
        "destination": "",
        "trip_type": "roundtrip",
        "depart_date": "",
        "return_date": "",
        "passengers": 1,
        "cabin": "ECONOMY",
        "nonstop": False,
        "sort": "recommended",
        "combination_mode": "auto",
        "raw_text": "",
    }
    shared_options = {
        "cabin_options": [
            ("ECONOMY", "Economy"),
            ("PREMIUM_ECONOMY", "Premium Economy"),
            ("BUSINESS", "Business"),
            ("FIRST", "First"),
        ],
        "sort_options": [
            ("recommended", "Recommended"),
            ("cheapest", "Cheapest"),
            ("fastest", "Fastest"),
        ],
        "trip_options": [
            ("roundtrip", "Round-trip"),
            ("oneway", "One-way"),
        ],
    }
    if request.method == "GET":
        return _render_agent(
            "agent/search.html",
            query=defaults,
            flights=[],
            error="",
            searched=False,
            **_agent_search_workspace_context(consumer_app),
            **shared_options,
        )

    if not _validate_csrf():
        abort(403)

    raw_params = {
        "origin": str(request.form.get("origin") or "").strip().upper(),
        "destination": str(request.form.get("destination") or "").strip().upper(),
        "trip_type": str(request.form.get("trip_type") or "roundtrip").strip().lower(),
        "depart_date": str(request.form.get("depart_date") or "").strip(),
        "return_date": str(request.form.get("return_date") or "").strip(),
        "passengers": request.form.get("passengers", "1"),
        "cabin": str(request.form.get("cabin") or "ECONOMY").strip().upper(),
        "nonstop": str(request.form.get("nonstop") or "").strip().lower() in {"1", "true", "on", "yes"},
        "sort": str(request.form.get("sort") or "recommended").strip().lower(),
        "combination_mode": "auto",
        "raw_text": "",
    }
    params, error = consumer_app._validate_standard_search_params(raw_params)
    validation_error = error
    flights: list[dict[str, Any]] = []
    if not error:
        flights = consumer_app.search_flights(
            params,
            detailed=True,
            force_refresh=request.form.get("force_refresh") == "1",
        )
        if not flights:
            error = "No flights found for those dates. Try loosening nonstop or adjusting the route."
    if not validation_error:
        agent_store.record_agent_search(
            user_id=int(g.agent_user.get("id") or 0) or None,
            agency_id=int(g.agent_user.get("agency_id") or 0) or None,
            params=params,
            result_count=len(flights),
            error=error or "",
        )
    flights = _decorate_agent_flights(flights)
    return _render_agent(
        "agent/search.html",
        query=params,
        flights=flights,
        error=error or "",
        searched=True,
        **_agent_search_workspace_context(consumer_app),
        **shared_options,
    )


@agent_bp.route("/checkout/<offer_id>", methods=["GET", "POST"])
@require_agent_auth("agent.dashboard.view")
def checkout_offer(offer_id: str):
    consumer_app = _shared_search_module()
    mode_error = consumer_app._demo_checkout_lock_error() or consumer_app._booking_mode_error()
    if mode_error:
        return _render_agent(
            "agent/checkout.html",
            offer_summary=None,
            travelers=[],
            checkout_model=None,
            errors={},
            booking_error=mode_error,
            booking_enabled=False,
            selected_booking=None,
        ), 503

    try:
        offer = consumer_app.DUFF.get_offer(offer_id, return_available_services=True)
    except consumer_app.DuffelAPIError as exc:
        return _render_agent(
            "agent/checkout.html",
            offer_summary=None,
            travelers=[],
            checkout_model=None,
            errors={},
            booking_error=str(exc),
            booking_enabled=False,
            selected_booking=None,
        ), consumer_app._booking_status_code(exc.status_code)

    seat_maps: list[dict[str, Any]] = []
    payment_config = consumer_app._load_checkout_sidecars(offer)[1]
    travelers = consumer_app.build_traveler_forms(offer, request.form if request.method == "POST" else None)
    offer_summary = consumer_app.build_checkout_summary(offer, seat_maps=seat_maps, ancillaries_payload={})
    checkout_model = consumer_app.build_checkout_page_model(
        offer,
        travelers=travelers,
        seat_maps=seat_maps,
        ancillaries_payload={},
        payment_config=payment_config,
        duffel_env=consumer_app.DUFFEL_ENV,
    )
    is_expired = consumer_app.offer_has_expired(offer)
    expiry_error = "This offer has expired. Head back to results and choose a fresh option." if is_expired else ""

    if request.method == "POST":
        if not _validate_csrf():
            abort(403)
        if is_expired:
            return _render_agent(
                "agent/checkout.html",
                offer_summary=offer_summary,
                travelers=travelers,
                checkout_model=checkout_model,
                errors={},
                booking_error=expiry_error,
                booking_enabled=False,
                selected_booking=None,
            ), 410

        passengers_payload, travelers, errors = consumer_app.validate_checkout_form(offer, request.form)
        checkout_model = consumer_app.build_checkout_page_model(
            offer,
            travelers=travelers,
            seat_maps=seat_maps,
            ancillaries_payload={},
            payment_config=payment_config,
            duffel_env=consumer_app.DUFFEL_ENV,
        )
        if errors:
            return _render_agent(
                "agent/checkout.html",
                offer_summary=offer_summary,
                travelers=travelers,
                checkout_model=checkout_model,
                errors=errors,
                booking_error=errors.get("form", ""),
                booking_enabled=True,
                selected_booking=None,
            ), 400

        try:
            order = consumer_app.DUFF.create_order(
                offer_id=str(offer.get("id") or offer_id).strip(),
                passengers=passengers_payload,
                services=None,
                total_amount=str(offer_summary.get("total_amount") or "0.00"),
                total_currency=str(offer_summary.get("currency") or "USD"),
            )
        except consumer_app.DuffelAPIError as exc:
            return _render_agent(
                "agent/checkout.html",
                offer_summary=offer_summary,
                travelers=travelers,
                checkout_model=checkout_model,
                errors={},
                booking_error=str(exc),
                booking_enabled=True,
                selected_booking=None,
            ), consumer_app._booking_status_code(exc.status_code)

        order_id = str(order.get("id") or "").strip()
        if order_id:
            consumer_app.RECENT_ORDER_CACHE.set(order_id, order)
            consumer_app._capture_booking_email_links(order=order, passengers_payload=passengers_payload)
            consumer_app._track_booking_completed_event(order, offer=offer)
            consumer_app._record_agent_booking(order, offer=offer)
            try:
                consumer_app._send_itinerary_emails_after_booking(order=order, passengers_payload=passengers_payload)
            except Exception as exc:
                print(f"AGENT ITINERARY EMAIL ERROR: {type(exc).__name__}: {exc}")
            created_booking = _platform_booking_for_order(order_id)
            agent_store.record_audit_log(
                action="agent_booking_created",
                entity_type="platform_booking",
                entity_id=str((created_booking or {}).get("id") or order_id),
                actor_user_id=int(g.agent_user.get("id") or 0),
                agency_id=int(g.agent_user.get("agency_id") or 0) or None,
                ip_address=_client_ip(),
                user_agent=_user_agent(),
                details={
                    "order_id": order_id,
                    "offer_id": str(offer.get("id") or ""),
                    "booking_reference": str(order.get("booking_reference") or ""),
                },
            )
            return redirect(url_for("agent.booking_confirmation", order_id=order_id))

    return _render_agent(
        "agent/checkout.html",
        offer_summary=offer_summary,
        travelers=travelers,
        checkout_model=checkout_model,
        errors={},
        booking_error=expiry_error,
        booking_enabled=not is_expired,
        selected_booking=None,
    ), (410 if is_expired else 200)


@agent_bp.route("/booking/confirmation/<order_id>", methods=["GET"])
@require_agent_auth("agent.dashboard.view")
def booking_confirmation(order_id: str):
    consumer_app = _shared_search_module()
    order = consumer_app.RECENT_ORDER_CACHE.get(order_id)
    if order is None:
        mode_error = consumer_app._booking_mode_error()
        if mode_error:
            return _render_agent(
                "agent/booking_confirmation.html",
                order_summary=None,
                booking_error=mode_error,
                selected_booking=None,
            ), 503
        try:
            order = consumer_app.DUFF.get_order(order_id)
        except consumer_app.DuffelAPIError as exc:
            return _render_agent(
                "agent/booking_confirmation.html",
                order_summary=None,
                booking_error=str(exc),
                selected_booking=None,
            ), consumer_app._booking_status_code(exc.status_code)
        consumer_app.RECENT_ORDER_CACHE.set(order_id, order)

    selected_booking = _platform_booking_for_order(order_id)
    return _render_agent(
        "agent/booking_confirmation.html",
        order_summary=consumer_app.build_order_summary(order),
        booking_error="",
        selected_booking=selected_booking,
    )


# ── User admin ────────────────────────────────────────────────────────────────

@agent_bp.route("/admin/users/create", methods=["POST"])
@require_agent_auth("agent.users.create")
def create_user():
    if not _validate_csrf():
        abort(403)
    actor = g.agent_user
    first_name = str(request.form.get("first_name") or "").strip()[:80]
    last_name = str(request.form.get("last_name") or "").strip()[:80]
    email = str(request.form.get("email") or "").strip().lower()
    password = str(request.form.get("password") or "")
    password_confirm = str(request.form.get("password_confirm") or "")
    role = normalize_role(request.form.get("role"), default="agent_user")
    existing_agency_id = str(request.form.get("agency_id") or "").strip()
    new_agency_name = str(request.form.get("new_agency_name") or "").strip()[:160]

    if not first_name:
        _set_notice(error="Enter the agent's first name.")
        return redirect(url_for("agent.new_user_page"))
    if not last_name:
        _set_notice(error="Enter the agent's last name.")
        return redirect(url_for("agent.new_user_page"))
    if not email or "@" not in email:
        _set_notice(error="Enter a valid agent email.")
        return redirect(url_for("agent.new_user_page"))
    if password != password_confirm:
        _set_notice(error="Password confirmation does not match.")
        return redirect(url_for("agent.new_user_page"))
    if not _password_meets_criteria(password):
        _set_notice(error="Use at least 8 characters including letters and numbers.")
        return redirect(url_for("agent.new_user_page"))

    agency_id: int | None = None
    agency_name = ""
    if new_agency_name:
        agency = agent_store.create_agency(new_agency_name)
        agency_id = int(agency.get("id") or 0) or None
        agency_name = str(agency.get("name") or "").strip()
    elif existing_agency_id:
        agency = agent_store.get_agency(int(existing_agency_id))
        if not agency:
            _set_notice(error="Choose a valid agency.")
            return redirect(url_for("agent.new_user_page"))
        agency_id = int(agency.get("id") or 0) or None
        agency_name = str(agency.get("name") or "").strip()
    else:
        _set_notice(error="Choose an existing agency or enter a new agency name.")
        return redirect(url_for("agent.new_user_page"))

    try:
        created = agent_store.create_user(
            email=email, password=password,
            first_name=first_name, last_name=last_name,
            global_role=role, agency_id=agency_id,
            membership_role=role, two_factor_enabled=False,
        )
    except ValueError as exc:
        reason = str(exc)
        if reason == "email_exists":
            _set_notice(error="An agent account with that email already exists.")
        else:
            _set_notice(error="Could not create that agent account.")
        return redirect(url_for("agent.new_user_page"))

    agent_store.record_audit_log(
        action="agent_created", entity_type="agency_user",
        entity_id=str(created.get("id") or ""),
        actor_user_id=int(actor.get("id") or 0),
        agency_id=agency_id, ip_address=_client_ip(), user_agent=_user_agent(),
        details={"email": email, "role": role, "agency_name": agency_name},
    )
    _set_notice(notice=f"Created agent login for {email}.")
    return redirect(url_for("agent.users_list"))


@agent_bp.route("/admin/users/new", methods=["GET"])
@require_agent_auth("agent.users.create")
def new_user_page():
    agencies = agent_store.list_agencies(limit=200)
    return _render_agent("agent/create_user.html", agencies=agencies)


@agent_bp.route("/admin/users/<int:user_id>/disable", methods=["POST"])
@require_agent_auth("agent.users.disable")
def disable_user(user_id: int):
    if not _validate_csrf():
        abort(403)
    actor = g.agent_user
    target = agent_store.get_user_by_id(user_id)
    if not target:
        _set_notice(error="Agent account not found.")
        return redirect(url_for("agent.users_list"))
    if int(actor.get("id") or 0) == int(target.get("id") or 0):
        _set_notice(error="You cannot disable your own account.")
        return redirect(url_for("agent.users_list"))
    # Agency admins may only disable users in their own agency
    if not _is_platform_admin():
        scoped = _scoped_agency_id()
        if int(target.get("agency_id") or 0) != scoped:
            _set_notice(error="You can only disable users in your own agency.")
            return redirect(url_for("agent.users_list"))
    reason = str(request.form.get("reason") or "Disabled by admin").strip()[:240] or "Disabled by admin"
    updated = agent_store.disable_user(user_id, reason=reason)
    agent_store.record_audit_log(
        action="agent_disabled", entity_type="agency_user",
        entity_id=str(user_id),
        actor_user_id=int(actor.get("id") or 0),
        agency_id=int(updated.get("agency_id") or actor.get("agency_id") or 0) or None,
        ip_address=_client_ip(), user_agent=_user_agent(),
        details={"reason": reason, "target_email": str(updated.get("email") or "")},
    )
    _set_notice(notice=f"Disabled {updated.get('email')}.")
    return redirect(url_for("agent.users_list"))


# ── User management (detail + password reset) ─────────────────────────────────

@agent_bp.route("/users/<int:user_id>", methods=["GET"])
@require_agent_auth("agent.users.view")
def user_manage(user_id: int):
    target = agent_store.get_user_by_id(user_id)
    if not target:
        _set_notice(error="User not found.")
        return redirect(url_for("agent.users_list"))
    # Scope enforcement
    if not _is_platform_admin():
        if int(target.get("agency_id") or 0) != _scoped_agency_id():
            _set_notice(error="You do not have access to that user.")
            return redirect(url_for("agent.users_list"))
    bookings = agent_store.list_platform_bookings(agent_user_id=user_id, limit=50)
    login_events = agent_store.list_login_events(user_id=user_id, limit=20)
    return _render_agent(
        "agent/user_manage.html",
        target=target,
        bookings=bookings,
        login_events=login_events,
        can_reset_password="agent.users.manage" in g.agent_permissions,
        can_disable="agent.users.disable" in g.agent_permissions,
    )


@agent_bp.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@require_agent_auth("agent.users.manage")
def reset_password(user_id: int):
    if not _validate_csrf():
        abort(403)
    actor = g.agent_user
    target = agent_store.get_user_by_id(user_id)
    if not target:
        _set_notice(error="User not found.")
        return redirect(url_for("agent.users_list"))
    if int(actor.get("id") or 0) == user_id:
        _set_notice(error="Use your account settings to change your own password.")
        return redirect(url_for("agent.user_manage", user_id=user_id))
    new_password = str(request.form.get("new_password") or "")
    confirm = str(request.form.get("confirm_password") or "")
    if new_password != confirm:
        _set_notice(error="Password confirmation does not match.")
        return redirect(url_for("agent.user_manage", user_id=user_id))
    if not _password_meets_criteria(new_password):
        _set_notice(error="Password must be at least 8 characters with letters and numbers.")
        return redirect(url_for("agent.user_manage", user_id=user_id))
    agent_store.reset_user_password(
        user_id=user_id,
        new_password=new_password,
        reset_by_user_id=int(actor.get("id") or 0),
    )
    _set_notice(notice=f"Password reset for {target.get('email')}.")
    return redirect(url_for("agent.user_manage", user_id=user_id))


# ── User add requests ─────────────────────────────────────────────────────────

@agent_bp.route("/requests", methods=["GET"])
@require_agent_auth("agent.requests.review")
def user_requests():
    status_filter = request.args.get("status", "pending").strip().lower()
    if status_filter not in {"pending", "approved", "rejected", "all"}:
        status_filter = "pending"
    requests_list = agent_store.list_user_requests(
        status=None if status_filter == "all" else status_filter,
        limit=200,
    )
    pending_count = len([r for r in agent_store.list_user_requests(status="pending", limit=500)])
    return _render_agent(
        "agent/requests.html",
        requests=requests_list,
        status_filter=status_filter,
        pending_count=pending_count,
    )


@agent_bp.route("/requests/new", methods=["GET", "POST"])
@require_agent_auth("agent.users.request")
def new_user_request():
    agency_id = _scoped_agency_id()
    if not agency_id:
        _set_notice(error="Platform admins should use the direct user creation form.")
        return redirect(url_for("agent.dashboard"))
    if request.method == "GET":
        return _render_agent("agent/new_request.html")
    if not _validate_csrf():
        abort(403)
    first_name = str(request.form.get("first_name") or "").strip()[:80]
    last_name = str(request.form.get("last_name") or "").strip()[:80]
    email = str(request.form.get("email") or "").strip().lower()
    role = normalize_role(request.form.get("role"), default="agent_user")
    notes = str(request.form.get("notes") or "").strip()[:500]
    if not first_name or not last_name:
        _set_notice(error="Enter the full name of the user being requested.")
        return _render_agent("agent/new_request.html")
    if not email or "@" not in email:
        _set_notice(error="Enter a valid email address.")
        return _render_agent("agent/new_request.html")
    agent_store.create_user_request(
        agency_id=agency_id,
        requested_by_user_id=int(g.agent_user.get("id") or 0),
        first_name=first_name,
        last_name=last_name,
        email=email,
        role=role,
        notes=notes,
    )
    reviewers = agent_store.list_users(limit=500)
    reviewer_ids = {
        int(user.get("id") or 0)
        for user in reviewers
        if str(user.get("effective_role") or "") in {"skairova_admin", "super_admin"}
    }
    requester_name = f"{first_name} {last_name}".strip()
    requester_email = str(g.agent_user.get("email") or "")
    agency_name = str(g.agent_user.get("agency_name") or "Agency")
    for reviewer_id in reviewer_ids:
        if reviewer_id <= 0:
            continue
        agent_store.create_notification(
            user_id=reviewer_id,
            notification_type="user_request_submitted",
            title="New user request pending",
            body=f"{agency_name}: {requester_email} requested access for {requester_name} ({email}).",
        )
    _set_notice(notice="User add request submitted. A platform admin will review it shortly.")
    return redirect(url_for("agent.dashboard"))


@agent_bp.route("/requests/<int:request_id>/approve", methods=["POST"])
@require_agent_auth("agent.requests.review")
def approve_request(request_id: int):
    if not _validate_csrf():
        abort(403)
    actor = g.agent_user
    initial_password = str(request.form.get("initial_password") or "")
    if not _password_meets_criteria(initial_password):
        _set_notice(error="Set an initial password (8+ chars, letters and numbers) before approving.")
        return redirect(url_for("agent.user_requests"))
    # Fetch the request first so we can notify the requester
    req_row = agent_store.list_user_requests(status="pending", limit=500)
    req_data = next((r for r in req_row if r["id"] == request_id), None)

    result = agent_store.approve_user_request(
        request_id=request_id,
        reviewed_by_user_id=int(actor.get("id") or 0),
        review_note=str(request.form.get("review_note") or "").strip()[:500],
        initial_password=initial_password,
    )
    if result is None:
        _set_notice(error="Request not found or already reviewed.")
    else:
        _set_notice(notice=f"Request approved — account created for {result.get('email')}.")
        if req_data:
            requester_id = int(req_data.get("requested_by_user_id") or 0)
            name = f"{req_data.get('first_name', '')} {req_data.get('last_name', '')}".strip()
            review_note = str(request.form.get("review_note") or "").strip()[:500]
            body = f"Your request to add {name} ({req_data.get('email', '')}) has been approved. Their account is now active."
            if review_note:
                body += f" Note: {review_note}"
            agent_store.create_notification(
                user_id=requester_id,
                notification_type="user_request_approved",
                title="User request approved",
                body=body,
            )
    return redirect(url_for("agent.user_requests"))


@agent_bp.route("/requests/<int:request_id>/reject", methods=["POST"])
@require_agent_auth("agent.requests.review")
def reject_request(request_id: int):
    if not _validate_csrf():
        abort(403)
    actor = g.agent_user
    # Fetch the request before rejecting so we can notify the requester
    pending = agent_store.list_user_requests(status="pending", limit=500)
    req_data = next((r for r in pending if r["id"] == request_id), None)
    review_note = str(request.form.get("review_note") or "").strip()[:500]

    ok = agent_store.reject_user_request(
        request_id=request_id,
        reviewed_by_user_id=int(actor.get("id") or 0),
        review_note=review_note,
    )
    if ok:
        _set_notice(notice="Request rejected.")
        if req_data:
            requester_id = int(req_data.get("requested_by_user_id") or 0)
            name = f"{req_data.get('first_name', '')} {req_data.get('last_name', '')}".strip()
            body = f"Your request to add {name} ({req_data.get('email', '')}) was not approved."
            if review_note:
                body += f" Reason: {review_note}"
            agent_store.create_notification(
                user_id=requester_id,
                notification_type="user_request_rejected",
                title="User request declined",
                body=body,
            )
    else:
        _set_notice(error="Request not found or already reviewed.")
    return redirect(url_for("agent.user_requests"))


# ── Markup tiers ──────────────────────────────────────────────────────────────

@agent_bp.route("/settings/markup/tiers/add", methods=["POST"])
@require_agent_auth("agent.markup.manage")
def add_markup_tier():
    if not _validate_csrf():
        abort(403)
    try:
        min_fare = float(str(request.form.get("min_fare_usd") or "0").strip())
        max_fare_raw = str(request.form.get("max_fare_usd") or "").strip()
        max_fare: float | None = float(max_fare_raw) if max_fare_raw else None
        markup_type = str(request.form.get("markup_type") or "flat").strip()
        markup_value = float(str(request.form.get("markup_value") or "0").strip())
        agency_split = float(str(request.form.get("agency_split_pct") or "50").strip())
        if markup_type not in {"flat", "pct"}:
            raise ValueError
        if min_fare < 0 or markup_value < 0 or not (0 <= agency_split <= 100):
            raise ValueError
        if max_fare is not None and max_fare <= min_fare:
            raise ValueError("max must be > min")
    except (ValueError, TypeError) as exc:
        _set_notice(error=f"Invalid tier values: {exc}")
        return redirect(url_for("agent.markup_settings"))
    agent_store.add_markup_tier(
        min_fare_usd=min_fare,
        max_fare_usd=max_fare,
        markup_type=markup_type,
        markup_value=markup_value,
        agency_split_pct=agency_split,
        created_by_user_id=int(g.agent_user.get("id") or 0),
    )
    _set_notice(notice="Markup tier added.")
    return redirect(url_for("agent.markup_settings"))


@agent_bp.route("/settings/markup/tiers/<int:tier_id>/delete", methods=["POST"])
@require_agent_auth("agent.markup.manage")
def delete_markup_tier(tier_id: int):
    if not _validate_csrf():
        abort(403)
    agent_store.delete_markup_tier(tier_id)
    _set_notice(notice="Markup tier removed.")
    return redirect(url_for("agent.markup_settings"))
