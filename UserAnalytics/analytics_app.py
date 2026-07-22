from __future__ import annotations

import hmac
import os
from datetime import datetime
from typing import Any

from flask import Flask, abort, jsonify, render_template, request

import analytics_store

BASE_DIR = os.path.dirname(__file__)
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates_analytics")
STATIC_DIR = os.path.join(BASE_DIR, "static_analytics")

ANALYTICS_DB_PATH = (
    os.getenv("NGF_ANALYTICS_DB_PATH", os.path.join(BASE_DIR, "data", "analytics.db")).strip()
    or os.path.join(BASE_DIR, "data", "analytics.db")
)
ANALYTICS_IP_SALT = os.getenv("NGF_ANALYTICS_IP_SALT", "nextgen-analytics-salt").strip() or "nextgen-analytics-salt"
DASHBOARD_TOKEN = os.getenv("NGF_ANALYTICS_DASHBOARD_TOKEN", "").strip()

analytics_store.configure(db_path=ANALYTICS_DB_PATH, ip_salt=ANALYTICS_IP_SALT)

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _coerce_days(value: Any, default: int = 30) -> int:
    return max(1, min(365, _safe_int(value, default)))


def _coerce_limit(value: Any, default: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, _safe_int(value, default)))


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


def _require_auth_token() -> None:
    if not DASHBOARD_TOKEN:
        return
    provided = str(
        request.args.get("token")
        or request.headers.get("X-Analytics-Token")
        or ""
    ).strip()
    if not provided or not hmac.compare_digest(provided, DASHBOARD_TOKEN):
        abort(401)


@app.before_request
def _before_request_auth() -> None:
    _require_auth_token()


@app.route("/", methods=["GET"])
def dashboard() -> str:
    days = _coerce_days(request.args.get("days"), default=30)
    overview = analytics_store.fetch_overview(days=days)
    top_routes = analytics_store.fetch_top_routes(days=days, limit=12)
    mode_breakdown = analytics_store.fetch_mode_breakdown(days=days)
    routes_by_location = analytics_store.fetch_top_routes_by_location(days=days, limit=16)
    readiness = analytics_store.fetch_personalization_readiness(days=max(days, 30), limit=40)
    recent_events = analytics_store.fetch_recent_events(limit=120)[:40]

    max_route_searches = max((int(item.get("searches") or 0) for item in top_routes), default=1)
    max_location_route_searches = max((int(item.get("searches") or 0) for item in routes_by_location), default=1)
    max_mode_searches = max((int(item.get("searches") or 0) for item in mode_breakdown), default=1)

    for item in top_routes:
        item["bar_pct"] = round((float(item.get("searches") or 0) / max_route_searches) * 100.0, 2)
    for item in routes_by_location:
        item["bar_pct"] = round((float(item.get("searches") or 0) / max_location_route_searches) * 100.0, 2)
    for item in mode_breakdown:
        item["bar_pct"] = round((float(item.get("searches") or 0) / max_mode_searches) * 100.0, 2)
    for event in recent_events:
        event["metadata_preview"] = _metadata_preview(dict(event.get("metadata") or {}))

    return render_template(
        "dashboard.html",
        days=days,
        day_options=[7, 30, 60, 90],
        generated_at=datetime.utcnow().isoformat(timespec="seconds"),
        overview=overview,
        top_routes=top_routes,
        mode_breakdown=mode_breakdown,
        routes_by_location=routes_by_location,
        readiness=readiness,
        recent_events=recent_events,
    )


@app.route("/api/overview", methods=["GET"])
def api_overview():
    days = _coerce_days(request.args.get("days"), default=30)
    payload = {
        "overview": analytics_store.fetch_overview(days=days),
        "top_routes": analytics_store.fetch_top_routes(days=days, limit=12),
        "mode_breakdown": analytics_store.fetch_mode_breakdown(days=days),
        "routes_by_location": analytics_store.fetch_top_routes_by_location(days=days, limit=20),
    }
    return jsonify(payload)


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


@app.route("/api/recent-events", methods=["GET"])
def api_recent_events():
    limit = _coerce_limit(request.args.get("limit"), default=100, minimum=1, maximum=1000)
    return jsonify({"events": analytics_store.fetch_recent_events(limit=limit)})


if __name__ == "__main__":
    port = _coerce_limit(os.getenv("NGF_ANALYTICS_PORT", "5010"), default=5010, minimum=1024, maximum=65535)
    app.run(host="127.0.0.1", port=port, debug=True)
