from __future__ import annotations

import hmac
import json
import os
import secrets
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from dotenv import load_dotenv

import analytics_store

load_dotenv()

BASE_DIR = os.path.dirname(__file__)
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates_analytics")
STATIC_DIR = os.path.join(BASE_DIR, "static_analytics")

ANALYTICS_DB_PATH = (
    os.getenv("NGF_ANALYTICS_DB_PATH", os.path.join(BASE_DIR, "data", "analytics.db")).strip()
    or os.path.join(BASE_DIR, "data", "analytics.db")
)
ANALYTICS_IP_SALT = os.getenv("NGF_ANALYTICS_IP_SALT", "nextgen-analytics-salt").strip() or "nextgen-analytics-salt"
ACCOUNTS_DB_PATH = (
    os.getenv("NGF_ACCOUNTS_DB_PATH", os.path.join(BASE_DIR, "data", "accounts.db")).strip()
    or os.path.join(BASE_DIR, "data", "accounts.db")
)
EST_TZ = ZoneInfo("America/New_York")

analytics_store.configure(db_path=ANALYTICS_DB_PATH, ip_salt=ANALYTICS_IP_SALT)

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.secret_key = (
    os.getenv("NGF_ANALYTICS_SECRET_KEY", os.getenv("FLASK_SECRET_KEY", "nextgen-analytics-admin-key")).strip()
    or "nextgen-analytics-admin-key"
)

_AUTH_KEY = "ngf_analytics_admin_authed"
_AUTH_EMAIL_KEY = "ngf_analytics_admin_email"
_CSRF_KEY = "ngf_analytics_csrf"
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()
GEMINI_MODEL_NAME = "gemini-2.5-flash"


class _GeminiModel:
    def __init__(self, client, model_name: str):
        self.client = client
        self.model_name = model_name

    def generate_content(self, prompt: str):
        return self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
        )


try:
    from google import genai

    if GOOGLE_API_KEY:
        _analytics_model = _GeminiModel(genai.Client(api_key=GOOGLE_API_KEY), GEMINI_MODEL_NAME)
    else:
        _analytics_model = None
except Exception:
    _analytics_model = None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _coerce_days(value: Any, default: int = 30) -> int:
    return max(1, min(365, _safe_int(value, default)))


def _coerce_limit(value: Any, default: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, _safe_int(value, default)))


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "")[:120]


def _metadata_preview(metadata: dict[str, Any]) -> str:
    if not metadata:
        return "-"
    bits: list[str] = []
    for key in ("source", "error", "query", "order_id", "booking_reference", "provider"):
        raw = str(metadata.get(key) or "").strip()
        if not raw:
            continue
        bits.append(f"{key}={raw}")
    if bits:
        return " | ".join(bits)[:180]
    return "details"


def _generated_at_est() -> str:
    return datetime.now(EST_TZ).strftime("%Y-%m-%d %H:%M %Z")


_BANNED_AI_PHRASES = (
    "likely",
    "probably",
    "maybe",
    "perhaps",
    "possibly",
    "might",
    "could",
    "appears",
    "seems",
    "suggests",
    "we think",
    "it looks like",
    "interesting",
    "healthy",
    "strong engagement",
    "great engagement",
    "users love",
)


def _normalized_text_for_validation(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _contains_banned_ai_phrase(text: str) -> bool:
    normalized = _normalized_text_for_validation(text)
    return any(phrase in normalized for phrase in _BANNED_AI_PHRASES)


def _overall_summary_required_tokens(funnel: dict[str, Any]) -> list[str]:
    total = str(_safe_int(funnel.get("landed_users") or funnel.get("total_users"), 0))
    search_rate = f"{float(funnel.get('search_rate') or 0.0):.2f}%"
    click_rate = f"{float(funnel.get('click_rate') or 0.0):.2f}%"
    intent_rate = f"{float(funnel.get('intent_rate') or 0.0):.2f}%"
    return [total, search_rate, click_rate, intent_rate]


def _is_grounded_overall_summary(text: str, funnel: dict[str, Any], preferences: dict[str, Any]) -> bool:
    normalized = _normalized_text_for_validation(text)
    if not normalized or _contains_banned_ai_phrase(normalized):
        return False
    if any(token.lower() not in normalized for token in _overall_summary_required_tokens(funnel)):
        return False

    route = str(preferences.get("top_route") or "").strip()
    airline = str(preferences.get("top_airline") or "").strip()
    if route and route != "-" and "route" in normalized and route.lower() not in normalized:
        return False
    if airline and "airline" in normalized and airline.lower() not in normalized:
        return False
    return True


def _journey_allowed_keywords(journey: dict[str, Any]) -> set[str]:
    allowed = {
        _normalized_text_for_validation(journey.get("display_name")),
        _normalized_text_for_validation(journey.get("city")),
        _normalized_text_for_validation(journey.get("country")),
        _normalized_text_for_validation(journey.get("top_route")),
        _normalized_text_for_validation(journey.get("last_airline")),
        _normalized_text_for_validation(journey.get("journey_path")),
    }
    for event in journey.get("timeline") or []:
        if not isinstance(event, dict):
            continue
        allowed.add(_normalized_text_for_validation(event.get("label")))
        allowed.add(_normalized_text_for_validation(event.get("route")))
        allowed.add(_normalized_text_for_validation(event.get("airline")))
        price = event.get("price")
        if price is not None:
            allowed.add(f"${float(price):.2f}".lower())
    return {item for item in allowed if item and item != "-"}


def _is_grounded_user_summary(text: str, journey: dict[str, Any]) -> bool:
    normalized = _normalized_text_for_validation(text)
    if not normalized or _contains_banned_ai_phrase(normalized):
        return False
    if journey.get("booked") is False and "booked" in normalized:
        return False
    if journey.get("booking_intent") is False and "booking intent" in normalized:
        return False
    if journey.get("clicked_flight") is False and "clicked flight" in normalized:
        return False

    allowed_keywords = _journey_allowed_keywords(journey)
    anchored = any(keyword in normalized for keyword in allowed_keywords)
    return anchored


def _fallback_overall_summary(funnel: dict[str, Any], preferences: dict[str, Any]) -> str:
    total = _safe_int(funnel.get("landed_users") or funnel.get("total_users"), 0)
    searched = float(funnel.get("search_rate") or 0.0)
    clicked = float(funnel.get("click_rate") or 0.0)
    intent = float(funnel.get("intent_rate") or 0.0)
    route = str(preferences.get("top_route") or "").strip()
    airline = str(preferences.get("top_airline") or "").strip()
    price_cap = float(preferences.get("price_cap") or 0.0)
    nonstop_preference = preferences.get("nonstop_preference")
    dropoff = _safe_int(funnel.get("dropoff_search_no_click"), 0)

    preference_bits: list[str] = []
    if route and route != "-":
        preference_bits.append(f"the strongest route was {route}")
    if airline:
        preference_bits.append(f"{airline} showed up most often in high-intent activity")
    if price_cap > 0:
        preference_bits.append(f"most high-intent activity stayed under about ${price_cap:,.0f}")
    if nonstop_preference is True:
        preference_bits.append("users leaned toward nonstop options")
    elif nonstop_preference is False:
        preference_bits.append("users were willing to consider connecting options")

    summary = (
        f"Out of {total} users, {searched:.2f}% searched, {clicked:.2f}% clicked a flight, "
        f"and {intent:.2f}% showed booking intent."
    )
    if preference_bits:
        summary += " " + "; ".join(preference_bits) + "."
    if dropoff > 0:
        summary += f" The biggest visible gap is after search, where {dropoff} users searched without clicking a flight."
    return summary


def _fallback_results_update_story(results_updates: dict[str, Any]) -> str:
    total_updates = _safe_int(results_updates.get("total_updates"), 0)
    users_updating = _safe_int(results_updates.get("users_updating"), 0)
    top_fields = results_updates.get("top_changed_fields") or []
    if total_updates <= 0:
        return "No users updated results in the selected window."
    summary = f"There were {total_updates} explicit result updates from {users_updating} users."
    if top_fields:
        top = top_fields[0]
        summary += f" The most common change was {top.get('field', 'search criteria')} ({_safe_int(top.get('updates'), 0)} updates)."
    return summary


def _fallback_results_click_story(results_clicks: dict[str, Any]) -> str:
    total_clicks = _safe_int(results_clicks.get("total_clicks"), 0)
    users_clicking = _safe_int(results_clicks.get("users_clicking"), 0)
    top_airlines = results_clicks.get("top_airlines") or []
    if total_clicks <= 0:
        return "No result clicks were tracked in the selected window."
    summary = f"There were {total_clicks} result clicks from {users_clicking} users."
    if top_airlines:
        top = top_airlines[0]
        summary += f" The most-clicked airline was {top.get('airline', 'Unknown')} ({_safe_int(top.get('clicks'), 0)} clicks)."
    avg_price = float(results_clicks.get("average_price") or 0.0)
    if avg_price > 0:
        summary += f" Average clicked price was ${avg_price:,.2f}."
    return summary


def _fallback_user_summary(journey: dict[str, Any]) -> str:
    return str(journey.get("fallback_summary") or "").strip() or "No meaningful journey data yet."


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    raw = str(raw_text or "").strip()
    if not raw:
        return {}
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(raw[start : end + 1])
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _generate_ai_story(
    *,
    funnel: dict[str, Any],
    preferences: dict[str, Any],
    journeys: list[dict[str, Any]],
) -> dict[str, Any]:
    fallback = {
        "overall_summary": _fallback_overall_summary(funnel, preferences),
        "user_summaries": {item["user_key"]: _fallback_user_summary(item) for item in journeys},
        "source": "fallback",
    }
    if _is_testing() or _analytics_model is None:
        return fallback

    prompt_payload = {
        "funnel": funnel,
        "preferences": preferences,
        "users": [
            {
                "user_key": item["user_key"],
                "display_name": item["display_name"],
                "city": item["city"],
                "country": item["country"],
                "top_route": item["top_route"],
                "journey_path": item["journey_path"],
                "events_count": item["events_count"],
                "last_airline": item["last_airline"],
                "last_price": item["last_price"],
                "timeline": item["timeline"],
            }
            for item in journeys[:20]
        ],
    }
    prompt = (
        "You are writing analytics copy for a flight product manager. "
        "Use only the supplied data. Do not invent. Do not guess. Do not generalize. "
        "Do not use filler, marketing language, or unsupported interpretation. "
        "If a fact is missing, omit it. Every sentence must be grounded in the provided fields. "
        "Return strict JSON with keys overall_summary and user_summaries. "
        "overall_summary must be a single paragraph and must explicitly include the exact user total, exact search rate, exact click rate, and exact booking-intent rate from the data. "
        "user_summaries must be an array of objects with user_key and summary, where each summary is one sentence "
        "explaining only what that user did from landing through booking or drop-off. "
        "Do not use words like likely, probably, maybe, seems, appears, suggests, interesting, healthy, or strong engagement.\n\n"
        f"DATA:\n{json.dumps(prompt_payload, ensure_ascii=True, separators=(',', ':'))}"
    )
    try:
        response = _analytics_model.generate_content(prompt)
        parsed = _extract_json_object(getattr(response, "text", ""))
        overall_summary = str(parsed.get("overall_summary") or "").strip()
        raw_user_summaries = parsed.get("user_summaries")
        summary_map: dict[str, str] = {}
        if isinstance(raw_user_summaries, list):
            for item in raw_user_summaries:
                if not isinstance(item, dict):
                    continue
                user_key = str(item.get("user_key") or "").strip()
                summary = str(item.get("summary") or "").strip()
                if user_key and summary:
                    summary_map[user_key] = summary
        if not overall_summary or not _is_grounded_overall_summary(overall_summary, funnel, preferences):
            return fallback
        validated_user_summaries = {}
        for item in journeys:
            ai_summary = summary_map.get(item["user_key"], "")
            if ai_summary and _is_grounded_user_summary(ai_summary, item):
                validated_user_summaries[item["user_key"]] = ai_summary
            else:
                validated_user_summaries[item["user_key"]] = _fallback_user_summary(item)
        return {
            "overall_summary": overall_summary,
            "user_summaries": validated_user_summaries,
            "source": "gemini_validated",
        }
    except Exception:
        return fallback


def _is_testing() -> bool:
    return bool(app.config.get("TESTING"))


def _admin_credentials() -> tuple[str, str]:
    email = _normalize_email(
        os.getenv("NGF_ANALYTICS_ADMIN_EMAIL")
        or os.getenv("NGF_SMTP_USERNAME")
        or ""
    )
    password = str(
        os.getenv("NGF_ANALYTICS_ADMIN_APP_PASSWORD")
        or os.getenv("NGF_SMTP_PASSWORD")
        or ""
    ).strip()
    return email, password


def _admin_credentials_configured() -> bool:
    email, password = _admin_credentials()
    return bool(email and password)


def _ensure_csrf_token() -> str:
    token = str(session.get(_CSRF_KEY) or "").strip()
    if token:
        return token
    token = secrets.token_hex(24)
    session[_CSRF_KEY] = token
    return token


def _rotate_csrf_token() -> str:
    token = secrets.token_hex(24)
    session[_CSRF_KEY] = token
    return token


def _is_authenticated() -> bool:
    if _is_testing():
        return True
    return bool(session.get(_AUTH_KEY))


def _is_api_request() -> bool:
    return request.path.startswith("/api/")


def _csrf_matches_request() -> bool:
    if _is_testing():
        return True
    expected = str(session.get(_CSRF_KEY) or "").strip()
    if not expected:
        return False
    provided = str(
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
        or request.args.get("csrf_token")
        or ""
    ).strip()
    return bool(provided) and hmac.compare_digest(provided, expected)


def _unauthorized_response():
    if _is_api_request():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    next_path = request.full_path if request.query_string else request.path
    return redirect(url_for("login_page", next=next_path))


def _forbidden_csrf_response():
    if _is_api_request():
        return jsonify({"ok": False, "error": "csrf_failed"}), 403
    return redirect(url_for("login_page"))


def _safe_next_path(raw_value: str) -> str:
    raw = str(raw_value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return ""
    if not raw.startswith("/"):
        return ""
    if raw.startswith("//"):
        return ""
    return raw


@app.before_request
def _before_request_security() -> Any | None:
    _ensure_csrf_token()

    if request.endpoint == "static":
        return None

    if request.endpoint == "login_page":
        if request.method == "POST" and not _csrf_matches_request():
            return _forbidden_csrf_response()
        return None

    if not _is_authenticated():
        return _unauthorized_response()

    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_matches_request():
        return _forbidden_csrf_response()

    return None


def _render(template_name: str, **context: Any) -> str:
    payload = {
        "generated_at": _generated_at_est(),
        "admin_email": str(session.get(_AUTH_EMAIL_KEY) or ""),
        "csrf_token": _ensure_csrf_token(),
    }
    payload.update(context)
    return render_template(template_name, **payload)


@app.route("/login", methods=["GET", "POST"])
def login_page():
    next_path = _safe_next_path(str(request.args.get("next") or request.form.get("next") or ""))
    if request.method == "GET":
        if _is_authenticated():
            return redirect(next_path or url_for("overview_page"))
        return _render(
            "login.html",
            next_path=next_path,
            error="",
            credentials_configured=_admin_credentials_configured() or _is_testing(),
        )

    if not (_admin_credentials_configured() or _is_testing()):
        return _render(
            "login.html",
            next_path=next_path,
            error="Admin credentials are not configured in environment variables.",
            credentials_configured=False,
        ), 503

    submitted_email = _normalize_email(request.form.get("email"))
    submitted_password = str(request.form.get("password") or "")
    expected_email, expected_password = _admin_credentials()

    valid = (
        bool(submitted_email)
        and bool(submitted_password)
        and hmac.compare_digest(submitted_email, expected_email)
        and hmac.compare_digest(submitted_password, expected_password)
    )

    if not valid:
        return _render(
            "login.html",
            next_path=next_path,
            error="Invalid admin email or app password.",
            credentials_configured=True,
        ), 401

    session[_AUTH_KEY] = True
    session[_AUTH_EMAIL_KEY] = submitted_email
    _rotate_csrf_token()
    return redirect(next_path or url_for("overview_page"))


@app.route("/logout", methods=["POST"])
def logout():
    session.pop(_AUTH_KEY, None)
    session.pop(_AUTH_EMAIL_KEY, None)
    _rotate_csrf_token()
    return redirect(url_for("login_page"))


@app.route("/", methods=["GET"])
def root_page():
    return redirect(url_for("overview_page"))


@app.route("/overview", methods=["GET"])
def overview_page():
    days = _coerce_days(request.args.get("days"), default=30)
    return _render(
        "overview.html",
        page="overview",
        days=days,
        day_options=[7, 30, 60, 90],
        overview=analytics_store.fetch_overview(days=days),
        revenue=analytics_store.fetch_revenue_summary(days=days),
    )


@app.route("/bookings", methods=["GET"])
def bookings_page():
    days = _coerce_days(request.args.get("days"), default=30)
    limit = _coerce_limit(request.args.get("limit"), default=50, minimum=10, maximum=500)
    offset = max(0, _safe_int(request.args.get("offset"), 0))
    search = str(request.args.get("q") or "").strip()

    rows = analytics_store.fetch_bookings_list(limit=limit + 1, offset=offset, search=search)
    has_more = len(rows) > limit

    return _render(
        "bookings.html",
        page="bookings",
        days=days,
        day_options=[7, 30, 60, 90],
        limit=limit,
        offset=offset,
        has_more=has_more,
        search=search,
        bookings=rows[:limit],
        revenue=analytics_store.fetch_revenue_summary(days=days),
    )


@app.route("/accounts", methods=["GET"])
def accounts_page():
    limit = _coerce_limit(request.args.get("limit"), default=50, minimum=10, maximum=500)
    offset = max(0, _safe_int(request.args.get("offset"), 0))
    search = str(request.args.get("q") or "").strip()

    rows = analytics_store.fetch_accounts_list(
        ACCOUNTS_DB_PATH,
        limit=limit + 1,
        offset=offset,
        search=search,
    )
    has_more = len(rows) > limit

    return _render(
        "accounts.html",
        page="accounts",
        limit=limit,
        offset=offset,
        has_more=has_more,
        search=search,
        accounts=rows[:limit],
    )


@app.route("/analytics", methods=["GET"])
def analytics_page():
    days = _coerce_days(request.args.get("days"), default=30)
    journey_q = str(request.args.get("journey_q") or request.args.get("anon_q") or "").strip()
    journey_limit = _coerce_limit(request.args.get("journey_limit") or request.args.get("anon_limit"), default=50, minimum=10, maximum=200)
    journey_offset = max(0, _safe_int(request.args.get("journey_offset") or request.args.get("anon_offset"), 0))
    funnel = analytics_store.fetch_funnel_summary(days=days)
    preferences = analytics_store.fetch_preference_summary(days=days)
    results_updates = analytics_store.fetch_results_update_summary(days=days, limit=8)
    results_clicks = analytics_store.fetch_results_click_summary(days=days, limit=8)
    next_pages = analytics_store.fetch_next_page_summary(days=days)
    journey_rows = analytics_store.fetch_user_journeys(
        days=days,
        limit=journey_limit + 1,
        offset=journey_offset,
        search=journey_q,
    )
    journey_has_more = len(journey_rows) > journey_limit
    city_rows = analytics_store.fetch_city_breakdown(days=days, limit=12)
    visible_journeys = journey_rows[:journey_limit]
    ai_story = _generate_ai_story(funnel=funnel, preferences=preferences, journeys=visible_journeys)
    for item in visible_journeys:
        item["ai_summary"] = str(ai_story.get("user_summaries", {}).get(item["user_key"]) or item["fallback_summary"]).strip()

    return _render(
        "analytics.html",
        page="analytics",
        days=days,
        day_options=[7, 30, 60, 90],
        overview=analytics_store.fetch_overview(days=days),
        funnel=funnel,
        preferences=preferences,
        results_updates=results_updates,
        results_update_story=_fallback_results_update_story(results_updates),
        results_clicks=results_clicks,
        results_click_story=_fallback_results_click_story(results_clicks),
        next_pages=next_pages,
        ai_story=ai_story,
        top_routes=analytics_store.fetch_top_routes(days=days, limit=12),
        cities=city_rows,
        journey_q=journey_q,
        journey_limit=journey_limit,
        journey_offset=journey_offset,
        journey_has_more=journey_has_more,
        journeys=visible_journeys,
    )


@app.route("/api/overview", methods=["GET"])
def api_overview():
    days = _coerce_days(request.args.get("days"), default=30)
    payload = {
        "overview": analytics_store.fetch_overview(days=days),
        "funnel": analytics_store.fetch_funnel_summary(days=days),
        "preferences": analytics_store.fetch_preference_summary(days=days),
        "results_updates": analytics_store.fetch_results_update_summary(days=days, limit=8),
        "results_clicks": analytics_store.fetch_results_click_summary(days=days, limit=8),
        "next_pages": analytics_store.fetch_next_page_summary(days=days),
        "top_routes": analytics_store.fetch_top_routes(days=days, limit=12),
        "mode_breakdown": analytics_store.fetch_mode_breakdown(days=days),
        "routes_by_location": analytics_store.fetch_top_routes_by_location(days=days, limit=20),
        "trip_types": analytics_store.fetch_trip_type_breakdown(days=days),
        "countries": analytics_store.fetch_country_breakdown(days=days, limit=10),
        "cities": analytics_store.fetch_city_breakdown(days=days, limit=10),
    }
    return jsonify(payload)


@app.route("/api/analytics/clear-behavioral-events", methods=["POST"])
def api_clear_behavioral_events():
    deleted = analytics_store.clear_behavioral_events()
    return jsonify({"ok": True, "deleted_events": deleted})


@app.route("/api/popular-near", methods=["GET"])
def api_popular_near():
    country = str(request.args.get("country") or "").strip().upper()
    days = _coerce_days(request.args.get("days"), default=90)
    limit = _coerce_limit(request.args.get("limit"), default=8, minimum=1, maximum=50)
    routes = analytics_store.fetch_popular_routes_for_location(country=country, days=days, limit=limit)
    return jsonify(
        {
            "country": country or "GLOBAL",
            "days": days,
            "routes": routes,
        }
    )


@app.route("/api/trends", methods=["GET"])
def api_trends():
    days = _coerce_days(request.args.get("days"), default=30)
    return jsonify({"days": days, "trends": analytics_store.fetch_daily_trends(days=days)})


@app.route("/api/revenue", methods=["GET"])
def api_revenue():
    days = _coerce_days(request.args.get("days"), default=30)
    return jsonify(
        {
            "days": days,
            "series": analytics_store.fetch_revenue_series(days=days),
            "summary": analytics_store.fetch_revenue_summary(days=days),
        }
    )


@app.route("/api/events", methods=["GET"])
def api_events():
    limit = _coerce_limit(request.args.get("limit"), default=80, minimum=1, maximum=1000)
    events = analytics_store.fetch_recent_events(limit=limit)
    for event in events:
        event["metadata_preview"] = _metadata_preview(dict(event.get("metadata") or {}))
    return jsonify({"events": events})


@app.route("/api/recent-events", methods=["GET"])
def api_recent_events_alias():
    return api_events()


@app.route("/api/accounts/<path:email>", methods=["PATCH"])
def api_update_account(email: str):
    norm_email = _normalize_email(email)
    if not norm_email:
        return jsonify({"ok": False, "error": "invalid_email"}), 400

    payload = request.get_json(silent=True) or {}
    fields: dict[str, Any] = {}

    if "first_name" in payload:
        fields["first_name"] = str(payload.get("first_name") or "").strip()[:80]
    if "last_name" in payload:
        fields["last_name"] = str(payload.get("last_name") or "").strip()[:80]
    if "price_alerts_enabled" in payload:
        fields["price_alerts_enabled"] = 1 if bool(payload.get("price_alerts_enabled")) else 0
    if "route_tracking_enabled" in payload:
        fields["route_tracking_enabled"] = 1 if bool(payload.get("route_tracking_enabled")) else 0

    if not fields:
        return jsonify({"ok": False, "error": "no_fields"}), 400

    updated = analytics_store.update_account(ACCOUNTS_DB_PATH, norm_email, fields=fields)
    if not updated:
        return jsonify({"ok": False, "error": "account_not_found"}), 404

    detail = analytics_store.fetch_account_detail(ACCOUNTS_DB_PATH, norm_email)
    return jsonify({"ok": True, "account": detail})


@app.route("/api/accounts/<path:email>", methods=["DELETE"])
def api_delete_account(email: str):
    norm_email = _normalize_email(email)
    if not norm_email:
        return jsonify({"ok": False, "error": "invalid_email"}), 400

    deleted = analytics_store.delete_account(ACCOUNTS_DB_PATH, norm_email)
    if not deleted:
        return jsonify({"ok": False, "error": "account_not_found"}), 404

    deleted_search_events = analytics_store.clear_search_events_for_account(norm_email)
    return jsonify(
        {
            "ok": True,
            "deleted": True,
            "deleted_search_events": deleted_search_events,
        }
    )


@app.route("/api/accounts/<path:email>/saved-searches/clear", methods=["POST"])
def api_clear_account_saved_searches(email: str):
    norm_email = _normalize_email(email)
    if not norm_email:
        return jsonify({"ok": False, "error": "invalid_email"}), 400

    cleared = analytics_store.clear_account_saved_searches(ACCOUNTS_DB_PATH, norm_email)
    if not cleared:
        return jsonify({"ok": False, "error": "account_not_found"}), 404

    return jsonify({"ok": True, "cleared": True})


@app.route("/api/accounts/<path:email>/searches/clear", methods=["POST"])
def api_clear_account_searches(email: str):
    norm_email = _normalize_email(email)
    if not norm_email:
        return jsonify({"ok": False, "error": "invalid_email"}), 400

    deleted_count = analytics_store.clear_search_events_for_account(norm_email)
    return jsonify({"ok": True, "deleted_search_events": deleted_count})


@app.route("/api/accounts/<path:email>/reset-request", methods=["POST"])
def api_request_account_reset(email: str):
    norm_email = _normalize_email(email)
    if not norm_email:
        return jsonify({"ok": False, "error": "invalid_email"}), 400

    requested_by = str(session.get(_AUTH_EMAIL_KEY) or "admin").strip() or "admin"
    sent, reason, _ = analytics_store.create_password_reset_request(
        ACCOUNTS_DB_PATH,
        norm_email,
        ttl_minutes=10,
        requested_by=requested_by,
    )
    if sent:
        return jsonify({"ok": True, "status": "sent"})

    reason_key = str(reason or "").strip().lower()
    error_message = "Unable to send reset email right now."
    if reason_key == "account_not_found":
        error_message = "Account not found."
    elif reason_key == "invalid_recipient":
        error_message = "Invalid email address."
    elif reason_key == "email_not_configured":
        error_message = "SMTP email is not configured in environment variables."
    elif reason_key.startswith("smtp_error:smtpauthenticationerror"):
        error_message = "SMTP authentication failed. Check SMTP username and app password."
    elif reason_key.startswith("smtp_error:"):
        error_message = "SMTP provider error. Check host, port, TLS/SSL, and credentials."

    return jsonify({"ok": False, "error": reason_key or "send_failed", "message": error_message}), 400


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
def api_delete_event(event_id: int):
    success, reason = analytics_store.delete_non_booking_event(event_id)
    if success:
        return jsonify({"ok": True, "status": "deleted"})

    if reason == "booking_delete_forbidden":
        return jsonify({"ok": False, "error": reason, "message": "Booking events are read-only."}), 403
    if reason == "event_not_found":
        return jsonify({"ok": False, "error": reason}), 404
    return jsonify({"ok": False, "error": reason}), 400


if __name__ == "__main__":
    port = _coerce_limit(os.getenv("NGF_ANALYTICS_PORT", "5010"), default=5010, minimum=1024, maximum=65535)
    app.run(host="127.0.0.1", port=port, debug=True)
