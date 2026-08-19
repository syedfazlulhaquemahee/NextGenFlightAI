
from __future__ import annotations

import csv
import hashlib
import hmac
import heapq
import importlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from calendar import monthrange
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, Callable, Iterator, Mapping, Sequence

import jwt as pyjwt
import requests
from flask import Flask, Response, has_request_context, jsonify, redirect, render_template, request, session, stream_with_context, url_for
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

try:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict as psycopg_conninfo_to_dict
    from psycopg.rows import dict_row as psycopg_dict_row
except ImportError:  # Keeps local SQLite-only development usable before install.
    psycopg = None
    psycopg_conninfo_to_dict = None
    psycopg_dict_row = None

import analytics_store
import email_service
from destinations_data import CATEGORIES, DESTINATIONS, DOMESTIC_DESTINATIONS, destinations_for_category, get_destination, get_destination_by_code, get_domestic_destination_by_code
from agent_portal import agent_bp
from liteapi_client import (
    LITE_ENABLED,
    LITE_ENV,
    LiteAPIClient,
    LiteAPIError,
    SingleFlightTTLCache,
    FILTERABLE_AMENITIES,
    LITE_DEFAULT_CURRENCY,
    build_detail_view,
    build_hotel_cards,
    build_prebook_summary,
    build_rooms_view,
    sanitize_description,
)
from hotel_booking import build_hotel_traveler_form, validate_hotel_checkout_form

HOTEL_AMENITY_FILTERS = [label for label, _ in FILTERABLE_AMENITIES]
from duffel_booking import (
    build_checkout_page_model,
    build_checkout_summary,
    build_duffel_ancillaries_embed_model,
    build_order_summary,
    build_traveler_forms,
    calculate_total_amount,
    extract_ancillaries_payload,
    normalize_create_order_services,
    offer_has_expired,
    parse_duffel_datetime,
    selected_services_from_payload,
    validate_checkout_form,
)

# ------------------------------------------------------------
# Config
# ------------------------------------------------------------
load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()
# Demo safeguard: keeps the checkout/booking flow visible and browsable but
# blocks it from actually creating an order. On by default since this build
# is a live public demo; set NGF_DEMO_BOOKING_LOCK=0 to allow real checkouts
# (e.g. once this stops being a demo, or for local testing of the flow).
NGF_DEMO_BOOKING_LOCK = os.getenv("NGF_DEMO_BOOKING_LOCK", "1").strip().lower() not in ("0", "false", "no")
DUFFEL_ACCESS_TOKEN = os.getenv("DUFFEL_ACCESS_TOKEN", "").strip()
DUFFEL_BASE = os.getenv("DUFFEL_BASE", "https://api.duffel.com").strip() or "https://api.duffel.com"
DUFFEL_VERSION = os.getenv("DUFFEL_VERSION", "v2").strip() or "v2"
DUFFEL_ENV = "test" if DUFFEL_ACCESS_TOKEN.startswith("duffel_test_") else "live"
DUFFEL_COMPONENTS_VERSION = os.getenv("DUFFEL_COMPONENTS_VERSION", "3.16.1").strip() or "3.16.1"
DUFFEL_PAYMENT_MODE = os.getenv("DUFFEL_PAYMENT_MODE", "card").strip().lower() or "card"
if DUFFEL_PAYMENT_MODE not in {"card", "balance"}:
    DUFFEL_PAYMENT_MODE = "card"
DUFFEL_SUPPLIER_TIMEOUT_MS = int(os.getenv("DUFFEL_SUPPLIER_TIMEOUT_MS", "20000"))
DUFFEL_HTTP_TIMEOUT = float(os.getenv("DUFFEL_HTTP_TIMEOUT", "28"))
DUFFEL_PLACE_TIMEOUT = float(os.getenv("DUFFEL_PLACE_TIMEOUT", "6"))

# Voice AI: Flask never talks to Deepgram directly — it only mints short-lived
# JWTs that the voice_service/ proxy (a separate async process) verifies
# before opening the real Deepgram streaming connection. Both processes must
# share VOICE_PROXY_SECRET. See voice_service/main.py for the proxy itself.
VOICE_PROXY_SECRET = os.getenv("VOICE_PROXY_SECRET", "").strip()
VOICE_PROXY_WS_URL = os.getenv("VOICE_PROXY_WS_URL", "ws://localhost:8781/ws/voice").strip()
VOICE_SESSION_TOKEN_TTL_SECONDS = int(os.getenv("VOICE_SESSION_TOKEN_TTL_SECONDS", "45"))
VOICE_AI_ENABLED = bool(VOICE_PROXY_SECRET) and bool(os.getenv("DEEPGRAM_API_KEY", "").strip())
VOICE_WEB_ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.getenv("VOICE_WEB_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}

BASE_DIR = os.path.dirname(__file__)
AIRPORTS_CSV_PATH = os.getenv("NGF_AIRPORTS_CSV_PATH", os.path.join(BASE_DIR, "Data", "airports.csv")).strip() or os.path.join(BASE_DIR, "Data", "airports.csv")
# Vercel Functions mount the deployed project read-only.  Keep local Flask
# development on the repository's Data directory, but use the one writable
# serverless scratch directory unless a durable database path is explicitly
# configured.  This prevents account signup/reset from crashing with
# "unable to open database file" on a demo deployment.  /tmp is deliberately
# only a short-term fallback: a persistent account database should override
# NGF_ACCOUNTS_DB_PATH in production.
_SERVERLESS_SCRATCH_DIR = "/tmp/nextgenflightai" if os.getenv("VERCEL") else os.path.join(BASE_DIR, "Data")
DEFAULT_ACCOUNT_DB_PATH = os.path.join(_SERVERLESS_SCRATCH_DIR, "accounts.db")
ACCOUNT_DB_PATH = os.getenv("NGF_ACCOUNTS_DB_PATH", "").strip() or DEFAULT_ACCOUNT_DB_PATH
ANALYTICS_DB_PATH = os.getenv("NGF_ANALYTICS_DB_PATH", "").strip() or os.path.join(_SERVERLESS_SCRATCH_DIR, "analytics.db")
NGF_DATABASE_URL = os.getenv("NGF_DATABASE_URL", "").strip()
ANALYTICS_IP_SALT = os.getenv("NGF_ANALYTICS_IP_SALT", "nextgen-analytics-salt").strip() or "nextgen-analytics-salt"
ANALYTICS_ENABLED = os.getenv("NGF_ANALYTICS_ENABLED", "1").strip().lower() not in {"0", "false", "off", "no"}

REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "18"))
LIGHT_REQUEST_TIMEOUT = float(os.getenv("LIGHT_REQUEST_TIMEOUT", "7"))
HTTP_POOL_CONNECTIONS = int(os.getenv("HTTP_POOL_CONNECTIONS", "32"))
HTTP_POOL_MAXSIZE = int(os.getenv("HTTP_POOL_MAXSIZE", "64"))
FLEX_SCAN_WORKERS = max(4, min(8, os.cpu_count() or 4))
FLEX_SCAN_RPS = float(os.getenv("FLEX_SCAN_RPS", "6"))     # max Duffel requests/sec during flex scan
FLEX_PROVISIONAL_MIN_INTERVAL = float(os.getenv("FLEX_PROVISIONAL_MIN_INTERVAL", "0.25"))  # min seconds between provisional card NDJSON bursts
FLEX_SCAN_RETRY_MAX = int(os.getenv("FLEX_SCAN_RETRY_MAX", "2"))    # 429 retries per date
FLEX_SCAN_RETRY_CAP = float(os.getenv("FLEX_SCAN_RETRY_CAP", "10")) # max seconds to honour from reset header
CHEAPEST_VERIFY_TOP_N = 6
FLEX_MAX_SCAN_DAYS = 31
FLEX_CHALLENGER_POOL = 10
FLEX_NEARBY_DAY_WINDOW = 2
FLEX_SAMPLE_INITIAL = int(os.getenv("FLEX_SAMPLE_INITIAL", "8"))     # dates sampled in phase 1
FLEX_REFINE_NEIGHBORS = int(os.getenv("FLEX_REFINE_NEIGHBORS", "12")) # neighbor dates probed in phase 2
FLEX_PHASE2_MAX_CHALLENGERS = 10
FLEX_PHASE2_SPREAD_PROBES = 4
FLEX_PHASE2_COVERAGE_THRESHOLD = 0.55
FLEX_SKIP_PHASE2_COVERAGE = 0.72
FLEX_SKIP_PHASE2_VERIFIED_MIN = 6
FLEX_SKIP_PHASE2_TOP_CANDIDATES = 3
SEARCH_RESULTS_FETCH_LIMIT = int(os.getenv("SEARCH_RESULTS_FETCH_LIMIT", "250"))
LIGHT_SEARCH_RESULTS_FETCH_LIMIT = int(os.getenv("LIGHT_SEARCH_RESULTS_FETCH_LIMIT", "24"))
RECOMMENDED_RESULTS_LIMIT = 200
RESULTS_PAGE_LIMIT = 200
AIRPORT_SUGGEST_LIMIT = 8

# ------------------------------------------------------------
# OAuth configuration
# ------------------------------------------------------------
GOOGLE_OAUTH_CLIENT_ID     = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "").strip()
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
APPLE_OAUTH_CLIENT_ID      = os.getenv("APPLE_OAUTH_CLIENT_ID", "").strip()   # Service ID e.g. com.skairova.web
APPLE_OAUTH_TEAM_ID        = os.getenv("APPLE_OAUTH_TEAM_ID", "").strip()
APPLE_OAUTH_KEY_ID         = os.getenv("APPLE_OAUTH_KEY_ID", "").strip()
APPLE_OAUTH_PRIVATE_KEY    = os.getenv("APPLE_OAUTH_PRIVATE_KEY", "").strip().replace("\\n", "\n")

GOOGLE_AUTH_URL    = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL   = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL= "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_JWKS_URL    = "https://www.googleapis.com/oauth2/v3/certs"

APPLE_AUTH_URL  = "https://appleid.apple.com/auth/authorize"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_JWKS_URL  = "https://appleid.apple.com/auth/keys"

# Homepage AI chips: each row is one chip (shown on desktop only; hidden in CSS under 761px).
# Prompts use explicit city/airport pairs plus ISO dates or month names so parse_ai_flight_request
# and _extract_route_pair_from_text can resolve routes and dates (avoid vague destinations like "anywhere").
AI_HOME_SUGGESTION_CHIPS: list[dict[str, str]] = [
    {
        "label": "Round trip",
        "hint": "Edit cities and dates",
        "prompt": "Round trip JFK to LAX departing 2026-06-10 returning 2026-06-17, economy",
        "aria_label": "Insert a round-trip example with dates",
    },
    {
        "label": "One way",
        "hint": "Edit route and date",
        "prompt": "One way SFO to SEA on 2026-07-15, economy",
        "aria_label": "Insert a one-way example with a date",
    },
    {
        "label": "Flexible week",
        "hint": "Month + trip length",
        "prompt": "Cheapest 7 day trip from BOS to MIA in September, economy",
        "aria_label": "Insert a flexible week in a month",
    },
    {
        "label": "Under budget",
        "hint": "Route + month",
        "prompt": "Fly from Chicago to Phoenix in November under $400, economy",
        "aria_label": "Insert a budget example",
    },
    {
        "label": "Nonstop",
        "hint": "Route + month",
        "prompt": "Nonstop from LAX to JFK in December, economy",
        "aria_label": "Insert a nonstop example",
    },
]
RECOMMENDED_DIVERSITY_WINDOW = 12
RECOMMENDED_REPEAT_AIRLINE_PENALTY = 8.0
RECOMMENDED_CONSECUTIVE_REPEAT_PENALTY = 3.0
RECOMMENDED_ALT_FETCH_ROUNDS = 2
RECOMMENDED_ALT_DOMINANCE_THRESHOLD = 0.8
RECOMMENDED_ALT_MIN_UNIQUE_AIRLINES = 3
RECOMMENDED_ALT_SAMPLE_SIZE = 20
RECOMMENDED_ALT_MIN_RESULTS = 20
RECOMMENDED_REBALANCE_SCAN_MULTIPLIER = 2
RECOMMENDED_TOP10_MIN_UNIQUE_AIRLINES = 3
RECOMMENDED_TOP20_MIN_UNIQUE_AIRLINES = 4
RECOMMENDED_TOP10_MAX_PER_AIRLINE = 4
RECOMMENDED_TOP20_MAX_PER_AIRLINE = 6
RECOMMENDED_DOMESTIC_TOP10_MIN_UNIQUE_AIRLINES = 3
RECOMMENDED_DOMESTIC_TOP20_MIN_UNIQUE_AIRLINES = 4
RECOMMENDED_DOMESTIC_TOP10_MAX_PER_AIRLINE = 5
RECOMMENDED_DOMESTIC_TOP20_MAX_PER_AIRLINE = 7
RECOMMENDED_INTL_TOP10_MIN_UNIQUE_AIRLINES = 2
RECOMMENDED_INTL_TOP20_MIN_UNIQUE_AIRLINES = 3
RECOMMENDED_INTL_TOP10_MAX_PER_AIRLINE = 6
RECOMMENDED_INTL_TOP20_MAX_PER_AIRLINE = 8
VALID_TRIP_TYPES = {"roundtrip", "oneway", "multicity"}
VALID_CABINS = {"ECONOMY", "PREMIUM_ECONOMY", "BUSINESS", "FIRST"}
VALID_SORTS = {"recommended", "cheapest", "fastest", "earliest_departure", "earliest_arrival", "fewest_stops"}
VALID_COMBINATION_MODES = {"auto", "manual"}
DEFAULT_PASSENGERS = 1
MIN_PASSENGERS = 1
MAX_PASSENGERS = 9
DEFAULT_FLEX_TRIP_LENGTH_DAYS = 7
MIN_FLEX_TRIP_LENGTH_DAYS = 1
MAX_FLEX_TRIP_LENGTH_DAYS = 30
AI_PARSE_WARMUP_MIN_CHARS = int(os.getenv("AI_PARSE_WARMUP_MIN_CHARS", "12"))

_airline_name_cache: dict[str, str] = {}



US_STATES = {
    "alabama":"AL","alaska":"AK","arizona":"AZ","arkansas":"AR","california":"CA","colorado":"CO",
    "connecticut":"CT","delaware":"DE","florida":"FL","georgia":"GA","hawaii":"HI","idaho":"ID",
    "illinois":"IL","indiana":"IN","iowa":"IA","kansas":"KS","kentucky":"KY","louisiana":"LA",
    "maine":"ME","maryland":"MD","massachusetts":"MA","michigan":"MI","minnesota":"MN",
    "mississippi":"MS","missouri":"MO","montana":"MT","nebraska":"NE","nevada":"NV",
    "new hampshire":"NH","new jersey":"NJ","new mexico":"NM","new york":"NY","north carolina":"NC",
    "north dakota":"ND","ohio":"OH","oklahoma":"OK","oregon":"OR","pennsylvania":"PA",
    "rhode island":"RI","south carolina":"SC","south dakota":"SD","tennessee":"TN","texas":"TX",
    "utah":"UT","vermont":"VT","virginia":"VA","washington":"WA","west virginia":"WV",
    "wisconsin":"WI","wyoming":"WY"
}
US_ABBR_TO_NAME = {v.lower(): k for k, v in US_STATES.items()}

COUNTRY_NAME_ALIASES = {
    "us": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "united states": "US",
    "united states of america": "US",
    "uk": "GB",
    "u.k.": "GB",
    "britain": "GB",
    "great britain": "GB",
    "united kingdom": "GB",
    "england": "GB",
    "uae": "AE",
    "u.a.e.": "AE",
    "united arab emirates": "AE",
    "south korea": "KR",
    "north korea": "KP",
    "russia": "RU",
    "vietnam": "VN",
    "laos": "LA",
    "bolivia": "BO",
    "tanzania": "TZ",
    "venezuela": "VE",
    "brunei": "BN",
    "iran": "IR",
    "syria": "SY",
}

# metro aliases / common travel intent
METRO_ALIASES = {
    "nyc": ["JFK", "LGA", "EWR"],
    "new york city": ["JFK", "LGA", "EWR"],
    "new york": ["JFK", "LGA", "EWR"],
    "new": ["JFK", "LGA", "EWR"],
    "la": ["LAX", "BUR", "SNA", "LGB", "ONT"],
    "los angeles": ["LAX", "BUR", "SNA", "LGB", "ONT"],
    "los": ["LAX", "BUR", "SNA", "LGB", "ONT"],
    "bay area": ["SFO", "OAK", "SJC"],
    "san francisco bay area": ["SFO", "OAK", "SJC"],
    "dc": ["DCA", "IAD", "BWI"],
    "washington dc": ["DCA", "IAD", "BWI"],
    "was": ["DCA", "IAD", "BWI"],
    "chicago": ["ORD", "MDW"],
    "chi": ["ORD", "MDW"],
    "london": ["LHR", "LGW", "LCY", "LTN", "STN"],
    "lon": ["LHR", "LGW", "LCY", "LTN", "STN"],
    "paris": ["CDG", "ORY"],
    "par": ["CDG", "ORY"],
    "tokyo": ["HND", "NRT"],
    "tok": ["HND", "NRT"],
    "seoul": ["ICN", "GMP"],
    "seo": ["ICN", "GMP"],
    "dhaka": ["DAC"],
    "dha": ["DAC"],
    "delhi": ["DEL"],
    "del": ["DEL"],
    "bangalore": ["BLR"],
    "bengaluru": ["BLR"],
    "mumbai": ["BOM"],
}

# IATA city / metropolitan area codes (multi-airport). Shown as grouped "(all airports)"
# suggestions; airport CSV rows win first when the same 3 letters are a single airport.
CITY_METRO_GROUPS: dict[str, dict[str, Any]] = {
    "BJS": {"name": "Beijing", "country": "CN", "airports": ("PEK", "PKX"), "aliases": ("beijing",)},
    "CHI": {"name": "Chicago", "country": "US", "airports": ("ORD", "MDW")},
    "LON": {"name": "London", "country": "GB", "airports": ("LHR", "LGW", "LCY", "LTN", "STN")},
    "MIL": {"name": "Milan", "country": "IT", "airports": ("MXP", "LIN", "BGY")},
    "MOW": {"name": "Moscow", "country": "RU", "airports": ("SVO", "DME", "VKO")},
    "NYC": {"name": "New York", "country": "US", "airports": ("JFK", "LGA", "EWR")},
    "OSA": {"name": "Osaka", "country": "JP", "airports": ("KIX", "ITM")},
    "PAR": {"name": "Paris", "country": "FR", "airports": ("CDG", "ORY")},
    "ROM": {"name": "Rome", "country": "IT", "airports": ("FCO", "CIA"), "aliases": ("rome", "roma")},
    "SEL": {"name": "Seoul", "country": "KR", "airports": ("ICN", "GMP")},
    "STO": {"name": "Stockholm", "country": "SE", "airports": ("ARN", "BMA", "NYO")},
    "TYO": {"name": "Tokyo", "country": "JP", "airports": ("HND", "NRT")},
    "WAS": {"name": "Washington DC", "country": "US", "airports": ("DCA", "IAD", "BWI")},
    "YMQ": {"name": "Montreal", "country": "CA", "airports": ("YUL", "YMX")},
    "YTO": {"name": "Toronto", "country": "CA", "airports": ("YYZ", "YTZ", "YHM")},
    "SAO": {"name": "São Paulo", "country": "BR", "airports": ("GRU", "CGH", "VCP")},
    "RIO": {"name": "Rio de Janeiro", "country": "BR", "airports": ("GIG", "SDU")},
    "BER": {"name": "Berlin", "country": "DE", "airports": ("BER",)},
    "MEX": {"name": "Mexico City", "country": "MX", "airports": ("MEX", "NLU"), "aliases": ("ciudad de mexico",)},
    "BUE": {"name": "Buenos Aires", "country": "AR", "airports": ("EZE", "AEP")},
    "EAP": {"name": "Basel / Mulhouse / Freiburg", "country": "FR", "airports": ("BSL", "MLH")},
}

# major-airport weight map
MAJOR_AIRPORT_BOOST = {
    "ATL": 1800, "LAX": 1750, "ORD": 1750, "DFW": 1750, "DEN": 1700,
    "JFK": 1700, "LGA": 1550, "EWR": 1650,
    "SFO": 1650, "SEA": 1550, "LAS": 1500, "MIA": 1550,
    "BOS": 1500, "PHL": 1450, "IAD": 1450, "DCA": 1450, "BWI": 1300,
    "DTW": 1550, "MSP": 1500, "CLT": 1500, "PHX": 1500,
    "IAH": 1500, "AUS": 1300, "SAN": 1400, "SJC": 1300, "OAK": 1250,
    "BUR": 1200, "ONT": 1200, "SNA": 1250, "LGB": 1000,
    "MCO": 1450, "TPA": 1300, "FLL": 1300,
    "PDX": 1300, "SLC": 1400, "HNL": 1450, "ANC": 1200,
    "BUF": 1150, "ROC": 1100, "SYR": 1100, "ALB": 1100,
    "CDG": 1700, "ORY": 1450, "LHR": 1800, "LGW": 1450, "LCY": 1300,
    "NRT": 1500, "HND": 1700, "ICN": 1700, "GMP": 1350, "DAC": 1600,
}

BLOCKED_TYPES = {"heliport", "seaplane_base", "balloonport", "closed", "small_airport"}

def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def _airport_type_score(t: str) -> int:
    t = (t or "").lower()
    if t == "large_airport":
        return 700
    if t == "medium_airport":
        return 380
    return -2000

def _bad_name_penalty(name: str) -> int:
    n = _norm(name)
    bad_words = [
        "heliport", "seaplane", "skyport", "municipal", "county", "executive",
        "airfield", "aerodrome", "private", "memorial", "regional"
    ]
    return -900 if any(word in n for word in bad_words) else 0

app = Flask(__name__)

_flask_secret = os.getenv("FLASK_SECRET_KEY", "").strip()
if not _flask_secret:
    import warnings
    warnings.warn(
        "FLASK_SECRET_KEY is not set. A temporary random key will be used, which means "
        "all sessions will be invalidated on every restart. Set FLASK_SECRET_KEY in your "
        "environment for production deployments.",
        stacklevel=1,
    )
    _flask_secret = secrets.token_hex(32)
app.secret_key = _flask_secret

# ── Session / cookie hardening ────────────────────────────────────────────────
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Enable Secure flag when running behind HTTPS (set NGF_SESSION_COOKIE_SECURE=1 in prod)
app.config["SESSION_COOKIE_SECURE"] = os.getenv("NGF_SESSION_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes"}
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)

# Absolute base URL of the backend API. When the frontend is served from a
# different origin (e.g. Vercel) than the backend (e.g. Render), set this to the
# backend URL so voice/session-token requests target the backend, not the
# frontend origin. Empty string keeps requests same-origin.
app.config["SKAIR_API_BASE_URL"] = os.getenv("SKAIR_API_BASE_URL", "").strip()

app.jinja_env.auto_reload = True
analytics_store.configure(db_path=ANALYTICS_DB_PATH, ip_salt=ANALYTICS_IP_SALT)
app.register_blueprint(agent_bp)


@app.after_request
def _apply_security_headers(response: Response) -> Response:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Geolocation stays off everywhere except the Flights and Hotels landing
    # pages, which use it for their nearby-hotel rails. Camera/mic remain
    # blocked, and the browser still prompts for consent — this only stops the
    # platform from refusing the request outright.
    geolocation = "(self)" if request.endpoint in {"index", "hotels"} else "()"
    response.headers.setdefault(
        "Permissions-Policy", f"geolocation={geolocation}, camera=(), microphone=()"
    )
    if os.getenv("NGF_SESSION_COOKIE_SECURE", "0").strip().lower() in {"1", "true", "yes"}:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.after_request
def _apply_portal_no_cache(response: Response) -> Response:
    path = (request.path or "").strip()
    protected_prefixes = (
        "/portal",
        "/auth",
        "/manage-booking",
    )
    if path.startswith(protected_prefixes):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.after_request
def _apply_voice_cors(response: Response) -> Response:
    if request.path != "/voice/session-token":
        return response

    origin = str(request.headers.get("Origin") or "").strip()
    if not origin:
      return response

    if VOICE_WEB_ALLOWED_ORIGINS and origin not in VOICE_WEB_ALLOWED_ORIGINS:
        return response

    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Accept, Content-Type"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


def _session_ancillaries_key(offer_id: str) -> str:
    return f"ngf_anc_{(offer_id or '').strip()}"


@app.context_processor
def _inject_b2c_csrf():
    try:
        return {"csrf_token": _b2c_csrf_token()}
    except RuntimeError:
        return {"csrf_token": ""}


@app.template_filter("from_json")
def _from_json_filter(value: str) -> Any:
    import json as _json
    try:
        return _json.loads(value or "[]")
    except Exception:
        return []


@app.template_filter("format_date")
def _format_date_filter(iso_date: str) -> str:
    """Convert YYYY-MM-DD to 'Jun 10' style."""
    try:
        from datetime import datetime as _dt_cls
        d = _dt_cls.strptime(iso_date, "%Y-%m-%d")
        return d.strftime("%b %-d")
    except Exception:
        return iso_date or ""

# ------------------------------------------------------------
# Generic helpers
# ------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default

def _analytics_enabled_for_request() -> bool:
    if not ANALYTICS_ENABLED:
        return False
    if app.config.get("TESTING") and not app.config.get("NGF_ENABLE_ANALYTICS_IN_TESTS"):
        return False
    return True

def _analytics_anon_id() -> str:
    anon_id = str(session.get("ngf_anon_id") or "").strip()
    if anon_id:
        return anon_id
    anon_id = f"anon_{os.urandom(10).hex()}"
    session["ngf_anon_id"] = anon_id
    return anon_id

def _analytics_header_value(*keys: str) -> str:
    for key in keys:
        raw = str(request.headers.get(key) or "").strip()
        if raw:
            return raw
    return ""

def _analytics_location_context() -> dict[str, str]:
    country = _analytics_header_value(
        "CF-IPCountry",
        "X-Country-Code",
        "X-Appengine-Country",
        "X-Vercel-IP-Country",
        "X-Geo-Country",
    ).upper()
    region = _analytics_header_value(
        "X-Vercel-IP-Country-Region",
        "X-Appengine-Region",
        "X-Region-Code",
        "X-Geo-Region",
    ).upper()
    city = _analytics_header_value(
        "X-Vercel-IP-City",
        "X-Appengine-City",
        "X-City",
        "X-Geo-City",
    )
    if not country:
        country = "UNKNOWN"
    return {
        "country": country[:12],
        "region": region[:16],
        "city": city[:80],
    }

def _analytics_ip_hash() -> str:
    forwarded = str(request.headers.get("X-Forwarded-For") or "").strip()
    source_ip = forwarded.split(",", 1)[0].strip() if forwarded else ""
    if not source_ip:
        source_ip = str(request.remote_addr or "").strip()
    return analytics_store.hash_ip(source_ip)

def _analytics_account_email(override: str | None = None) -> str:
    if override is not None:
        return _normalize_email(override)
    raw_session_email = str(session.get("ngf_account_email") or "").strip()
    if raw_session_email:
        return _normalize_email(raw_session_email)
    return ""

def _analytics_search_id(explicit: str | None = None) -> str:
    if explicit:
        return str(explicit).strip()[:64]
    existing = str(request.environ.get("ngf_search_id") or "").strip()
    if existing:
        return existing
    generated = f"s_{int(time.time() * 1000):x}_{os.urandom(4).hex()}"
    request.environ["ngf_search_id"] = generated
    return generated


def _is_real_search_submission(form: Mapping[str, Any] | None = None) -> bool:
    payload = form if form is not None else request.form
    raw = str(payload.get("search_submitted") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _analytics_search_context() -> dict[str, str]:
    raw = session.get("ngf_analytics_search_context")
    if not isinstance(raw, dict):
        return {}
    return {
        "search_id": str(raw.get("search_id") or "").strip()[:64],
        "search_mode": str(raw.get("search_mode") or "").strip().lower()[:24],
        "origin": str(raw.get("origin") or "").strip().upper()[:8],
        "destination": str(raw.get("destination") or "").strip().upper()[:8],
        "trip_type": str(raw.get("trip_type") or "").strip().lower()[:24],
    }


def _remember_analytics_search_context(
    *,
    search_id: str,
    search_mode: str,
    origin: str,
    destination: str,
    trip_type: str,
) -> None:
    session["ngf_analytics_search_context"] = {
        "search_id": str(search_id or "").strip()[:64],
        "search_mode": str(search_mode or "").strip().lower()[:24],
        "origin": str(origin or "").strip().upper()[:8],
        "destination": str(destination or "").strip().upper()[:8],
        "trip_type": str(trip_type or "").strip().lower()[:24],
    }

def _track_analytics_event(
    *,
    event_type: str,
    search_id: str | None = None,
    account_email: str | None = None,
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
    if not _analytics_enabled_for_request():
        return
    try:
        location = _analytics_location_context()
        analytics_store.record_event(
            event_type=event_type,
            search_id=_analytics_search_id(search_id),
            anon_id=_analytics_anon_id(),
            account_email=_analytics_account_email(account_email),
            ip_hash=_analytics_ip_hash(),
            location_country=location["country"],
            location_region=location["region"],
            location_city=location["city"],
            search_mode=str(search_mode or "").strip().lower(),
            origin=str(origin or "").strip().upper(),
            destination=str(destination or "").strip().upper(),
            trip_type=str(trip_type or "").strip().lower(),
            result_count=max(0, _safe_int(result_count, 0)),
            success=bool(success),
            booking_amount=booking_amount,
            currency=str(currency or "").strip().upper(),
            metadata=dict(metadata or {}),
        )
    except Exception:
        # Analytics must never interfere with user-facing search/booking paths.
        pass


def _offer_analytics_context(offer: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(offer, Mapping):
        return {
            "origin": "",
            "destination": "",
            "trip_type": "",
            "price": "",
            "currency": "USD",
            "airline": "",
            "nonstop": None,
            "stops": 0,
        }

    slices = list(offer.get("slices") or [])
    first_segments = list((slices[0] or {}).get("segments") or []) if slices else []
    last_segments = list((slices[-1] or {}).get("segments") or []) if slices else []
    origin = ""
    destination = ""
    if first_segments:
        origin = str(((first_segments[0] or {}).get("origin") or {}).get("iata_code") or "").strip().upper()
    if last_segments:
        destination = str(((last_segments[-1] or {}).get("destination") or {}).get("iata_code") or "").strip().upper()

    owner = offer.get("owner") or {}
    airline = str(owner.get("name") or owner.get("iata_code") or "").strip()
    total_stops = 0
    if slices:
        total_stops = sum(max(0, len(list((item or {}).get("segments") or [])) - 1) for item in slices)

    return {
        "origin": origin,
        "destination": destination,
        "trip_type": "roundtrip" if len(slices) > 1 else "oneway",
        "price": str(offer.get("total_amount") or "").strip(),
        "currency": str(offer.get("total_currency") or "USD").strip().upper() or "USD",
        "airline": airline,
        "nonstop": total_stops == 0 if slices else None,
        "stops": total_stops,
    }


def _track_offer_funnel_event(
    *,
    event_type: str,
    offer: Mapping[str, Any] | None,
    step: str = "",
    success: bool = True,
) -> None:
    search_ctx = _analytics_search_context()
    offer_ctx = _offer_analytics_context(offer)
    metadata = {
        "step": step,
        "price": offer_ctx["price"],
        "airline": offer_ctx["airline"],
        "nonstop": offer_ctx["nonstop"],
        "stops": offer_ctx["stops"],
    }
    _track_analytics_event(
        event_type=event_type,
        search_id=search_ctx.get("search_id"),
        search_mode=search_ctx.get("search_mode") or "booking",
        origin=offer_ctx["origin"] or search_ctx.get("origin", ""),
        destination=offer_ctx["destination"] or search_ctx.get("destination", ""),
        trip_type=offer_ctx["trip_type"] or search_ctx.get("trip_type", ""),
        result_count=1,
        success=success,
        booking_amount=offer_ctx["price"] or None,
        currency=offer_ctx["currency"],
        metadata=metadata,
    )

def _search_tracking_payload(mode: str, params: Mapping[str, Any] | None) -> tuple[str, str, str, dict[str, Any]]:
    payload = dict(params or {})
    return (
        str(payload.get("origin") or "").strip().upper(),
        str(payload.get("destination") or "").strip().upper(),
        str(payload.get("trip_type") or "").strip().lower(),
        {
            "source": mode,
            "depart_date": str(payload.get("depart_date") or "").strip(),
            "return_date": str(payload.get("return_date") or "").strip(),
            "flex_month": str(payload.get("flex_month") or "").strip(),
            "trip_length_days": _safe_int(payload.get("trip_length_days"), 0),
            "passengers": _safe_int(payload.get("passengers"), 0),
            "cabin": str(payload.get("cabin") or "").strip().upper(),
            "nonstop": bool(payload.get("nonstop")),
            "combination_mode": str(payload.get("combination_mode") or "").strip().lower(),
            "sort": str(payload.get("sort") or "").strip().lower(),
        },
    )


def _search_change_snapshot(params: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = dict(params or {})
    return {
        "origin": str(payload.get("origin") or "").strip().upper(),
        "destination": str(payload.get("destination") or "").strip().upper(),
        "trip_type": str(payload.get("trip_type") or "").strip().lower(),
        "depart_date": str(payload.get("depart_date") or "").strip(),
        "return_date": str(payload.get("return_date") or "").strip(),
        "flex_month": str(payload.get("flex_month") or "").strip(),
        "trip_length_days": _safe_int(payload.get("trip_length_days"), 0),
        "passengers": _safe_int(payload.get("passengers"), DEFAULT_PASSENGERS),
        "cabin": str(payload.get("cabin") or "").strip().upper(),
        "nonstop": bool(payload.get("nonstop")),
        "combination_mode": str(payload.get("combination_mode") or "").strip().lower(),
        "sort": str(payload.get("sort") or "").strip().lower(),
    }


def _search_update_changes(previous: Mapping[str, Any] | None, current: Mapping[str, Any] | None) -> dict[str, Any]:
    labels = {
        "origin": "Origin",
        "destination": "Destination",
        "trip_type": "Trip type",
        "depart_date": "Depart date",
        "return_date": "Return date",
        "flex_month": "Flex month",
        "trip_length_days": "Trip length",
        "passengers": "Travelers",
        "cabin": "Cabin",
        "nonstop": "Nonstop",
        "combination_mode": "Combination mode",
        "sort": "Sort",
    }
    before = _search_change_snapshot(previous)
    after = _search_change_snapshot(current)
    changes: list[dict[str, Any]] = []
    changed_fields: list[str] = []
    for field, label in labels.items():
        if before.get(field) == after.get(field):
            continue
        changed_fields.append(field)
        changes.append(
            {
                "field": field,
                "label": label,
                "before": before.get(field),
                "after": after.get(field),
            }
        )
    return {
        "changed_fields": changed_fields,
        "changed_count": len(changed_fields),
        "changes": changes,
        "previous_search": before,
        "current_search": after,
    }


def _track_results_updated_event(
    *,
    search_mode: str,
    params: Mapping[str, Any] | None,
    previous_params: Mapping[str, Any] | None,
    result_count: int,
    success: bool,
    error: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> str:
    origin, destination, trip_type, base_meta = _search_tracking_payload("results_update", params)
    search_ctx = _analytics_search_context()
    resolved_search_id = _analytics_search_id(search_ctx.get("search_id") or None)
    base_meta.update(_search_update_changes(previous_params, params))
    if error:
        base_meta["error"] = str(error).strip()[:220]
    extra_meta = dict(metadata or {})
    if extra_meta:
        base_meta.update(extra_meta)
    _track_analytics_event(
        event_type="results_updated",
        search_id=resolved_search_id,
        search_mode=search_mode,
        origin=origin,
        destination=destination,
        trip_type=trip_type,
        result_count=result_count,
        success=success,
        metadata=base_meta,
    )
    return resolved_search_id


def _track_search_completed_event(
    *,
    source: str,
    search_mode: str,
    search_id: str = "",
    params: Mapping[str, Any] | None,
    result_count: int,
    success: bool,
    error: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> str:
    origin, destination, trip_type, base_meta = _search_tracking_payload(source, params)
    resolved_search_id = _analytics_search_id(search_id)
    if error:
        base_meta["error"] = str(error).strip()[:220]
    extra_meta = dict(metadata or {})
    if extra_meta:
        base_meta.update(extra_meta)
    _track_analytics_event(
        event_type="search_completed",
        search_id=resolved_search_id,
        search_mode=search_mode,
        origin=origin,
        destination=destination,
        trip_type=trip_type,
        result_count=result_count,
        success=success,
        metadata=base_meta,
    )
    _remember_analytics_search_context(
        search_id=resolved_search_id,
        search_mode=search_mode,
        origin=origin,
        destination=destination,
        trip_type=trip_type,
    )
    _track_analytics_event(
        event_type="results_viewed",
        search_id=resolved_search_id,
        search_mode=search_mode,
        origin=origin,
        destination=destination,
        trip_type=trip_type,
        result_count=result_count,
        success=success,
        metadata=base_meta,
    )
    return resolved_search_id

def _request_form_search_hint(form: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    mode = (form.get("mode") or "standard").strip().lower()
    trip_type = form.get("trip_type", "roundtrip" if form.get("return_date") else "oneway")
    payload = {
        "origin": str(form.get("origin") or "").strip().upper(),
        "destination": str(form.get("destination") or "").strip().upper(),
        "trip_type": str(trip_type or "roundtrip").strip().lower(),
        "depart_date": str(form.get("depart_date") or "").strip(),
        "return_date": str(form.get("return_date") or "").strip(),
        "flex_month": str(form.get("flex_month") or "").strip(),
        "trip_length_days": _safe_int(form.get("trip_length_days"), 0),
        "passengers": _safe_int(form.get("passengers"), DEFAULT_PASSENGERS),
        "cabin": str(form.get("cabin") or "ECONOMY").strip().upper(),
        "nonstop": str(form.get("nonstop") or "").strip().lower() in {"on", "1", "true", "yes"},
        "combination_mode": str(form.get("combination_mode") or "").strip().lower(),
        "sort": str(form.get("sort") or "").strip().lower(),
    }
    return mode, payload

def _record_agent_booking(order: Mapping[str, Any], offer: Mapping[str, Any] | None = None) -> None:
    """Persist the booking into the agent portal's platform_bookings table with markup splits."""
    try:
        import agent_store as _agent_store
        agent_user_id: int | None = None
        agency_id: int | None = None
        # Detect if this checkout was initiated by a logged-in agent
        raw_agent_id = session.get("ngf_agent_user_id")
        if raw_agent_id:
            try:
                agent_user_id = int(raw_agent_id)
                agent_user = _agent_store.get_user_by_id(agent_user_id)
                if agent_user:
                    agency_id = int(agent_user.get("agency_id") or 0) or None
            except Exception:
                agent_user_id = None

        origin = ""
        destination = ""
        depart_date = ""
        return_date = ""
        trip_type = "oneway"
        airline_name = ""
        cabin = ""
        passenger_names: list[str] = []
        slices = list((order if isinstance(order, Mapping) else {}).get("slices") or [])
        if not slices and isinstance(offer, Mapping):
            slices = list(offer.get("slices") or [])
        if slices:
            first_segs = list((slices[0] or {}).get("segments") or [])
            last_segs = list((slices[-1] or {}).get("segments") or [])
            if first_segs:
                origin = str(((first_segs[0] or {}).get("origin") or {}).get("iata_code") or "").upper()
                depart_date = str((first_segs[0] or {}).get("departing_at") or "")[:10]
            if last_segs:
                destination = str(((last_segs[-1] or {}).get("destination") or {}).get("iata_code") or "").upper()
                if len(slices) > 1:
                    trip_type = "roundtrip"
                    return_date = str((last_segs[-1] or {}).get("arriving_at") or "")[:10]
        if isinstance(offer, Mapping):
            airline_name = str((offer.get("owner") or {}).get("name") or "").strip()
            cabin_raw = ""
            for sl in offer.get("slices") or []:
                for seg in (sl or {}).get("segments") or []:
                    for pax in (seg or {}).get("passengers") or []:
                        cabin_raw = str((pax or {}).get("cabin_class") or "").strip()
                        break
                    if cabin_raw:
                        break
                if cabin_raw:
                    break
            cabin = cabin_raw.upper()

        passengers_raw = list((order if isinstance(order, Mapping) else {}).get("passengers") or [])
        for p in passengers_raw:
            given = str((p or {}).get("given_name") or "").strip()
            family = str((p or {}).get("family_name") or "").strip()
            name = (given + " " + family).strip()
            if name:
                passenger_names.append(name)

        base_fare_usd = float(str(order.get("total_amount") or "0").strip() or "0")
        currency = str(order.get("total_currency") or "USD").strip().upper() or "USD"
        _agent_store.record_platform_booking(
            duffel_order_id=str(order.get("id") or "").strip(),
            duffel_offer_id=str((offer or {}).get("id") or "").strip(),
            booking_reference=str(order.get("booking_reference") or "").strip().upper(),
            agent_user_id=agent_user_id,
            agency_id=agency_id,
            origin=origin,
            destination=destination,
            depart_date=depart_date,
            return_date=return_date,
            trip_type=trip_type,
            cabin=cabin,
            passenger_count=max(1, len(passengers_raw)),
            passenger_names=passenger_names,
            airline_name=airline_name,
            base_fare_usd=base_fare_usd,
            currency=currency,
        )
    except Exception as exc:
        print(f"AGENT BOOKING RECORD ERROR: {type(exc).__name__}: {exc}")


def _track_booking_completed_event(order: Mapping[str, Any], offer: Mapping[str, Any] | None = None) -> None:
    origin = ""
    destination = ""
    slices = []
    if isinstance(order, Mapping):
        slices = list(order.get("slices") or [])
    if not slices and isinstance(offer, Mapping):
        slices = list(offer.get("slices") or [])
    if slices:
        first_segments = list((slices[0] or {}).get("segments") or [])
        last_segments = list((slices[-1] or {}).get("segments") or [])
        if first_segments:
            origin = str(((first_segments[0] or {}).get("origin") or {}).get("iata_code") or "").strip().upper()
        if last_segments:
            destination = str(((last_segments[-1] or {}).get("destination") or {}).get("iata_code") or "").strip().upper()

    total_amount = str(order.get("total_amount") or "").strip()
    currency = str(order.get("total_currency") or "USD").strip().upper() or "USD"
    booking_reference = str(order.get("booking_reference") or "").strip().upper()
    order_id = str(order.get("id") or "").strip()
    search_ctx = _analytics_search_context()

    _track_analytics_event(
        event_type="booking_completed",
        search_id=search_ctx.get("search_id"),
        search_mode=search_ctx.get("search_mode") or "booking",
        origin=origin or search_ctx.get("origin", ""),
        destination=destination or search_ctx.get("destination", ""),
        trip_type=("roundtrip" if len(slices) > 1 else "oneway") or search_ctx.get("trip_type", ""),
        result_count=1,
        success=True,
        booking_amount=total_amount or None,
        currency=currency,
        metadata={
            "order_id": order_id,
            "booking_reference": booking_reference,
            "slice_count": len(slices),
            "price": total_amount or "",
            "airline": str(((offer or {}).get("owner") or {}).get("name") or "").strip(),
        },
    )

def minutes_to_hm(minutes: int, show_total: bool = False) -> str:
    h = minutes // 60
    m = minutes % 60
    base = f"{h}h {m}m"
    return f"{base} total" if show_total else base

def fmt_dt(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%b %d, %Y · %H:%M")
    except Exception:
        return iso_str

def _hhmm(dt_str: str) -> str:
    if not dt_str or "T" not in dt_str:
        return ""
    return dt_str.split("T", 1)[1][:5]

def _dt(iso_str: str) -> datetime:
    return datetime.fromisoformat(iso_str)

def _minutes_between(a_iso: str, b_iso: str) -> int:
    return max(0, int((_dt(b_iso) - _dt(a_iso)).total_seconds() // 60))

def _fmt_clock(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    try:
        return _dt(iso_str).strftime("%I:%M %p").lstrip("0")
    except Exception:
        return _hhmm(iso_str)

def _fmt_day_short(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    try:
        return _dt(iso_str).strftime("%a, %b %d")
    except Exception:
        return iso_str

def _iso_duration_to_minutes(iso_dur: str) -> int:
    if not iso_dur:
        return 0
    h = re.search(r"(\d+)H", iso_dur)
    m = re.search(r"(\d+)M", iso_dur)
    return (int(h.group(1)) if h else 0) * 60 + (int(m.group(1)) if m else 0)

def _display_duration_minutes(duration_iso: str | None, start_iso: str | None, end_iso: str | None) -> int:
    parsed_duration = _iso_duration_to_minutes(duration_iso or "")
    if parsed_duration > 0:
        return parsed_duration
    if start_iso and end_iso:
        return _minutes_between(start_iso, end_iso)
    return 0

def _to_date(d: str) -> date:
    return date.fromisoformat(d)

def _month_bounds(month_yyyy_mm: str) -> tuple[date, date]:
    y, m = map(int, month_yyyy_mm.split("-"))
    last = monthrange(y, m)[1]
    return date(y, m, 1), date(y, m, last)

def _daterange(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)

def _is_past_date(yyyy_mm_dd: str) -> bool:
    try:
        return _to_date(yyyy_mm_dd) < date.today()
    except Exception:
        return True

def _is_valid_iso_date(value: str | None) -> bool:
    if not value:
        return False
    try:
        _to_date(str(value))
        return True
    except Exception:
        return False

def _is_valid_flex_month(value: str | None) -> bool:
    value = (value or "").strip()
    if not re.fullmatch(r"^\d{4}-\d{2}$", value):
        return False
    try:
        _month_bounds(value)
        return True
    except Exception:
        return False

def _is_past_flex_month(value: str | None) -> bool:
    if not _is_valid_flex_month(value):
        return True
    month_start, _ = _month_bounds(str(value).strip())
    return month_start < date.today().replace(day=1)

def _coerce_trip_type(value: Any, *, fallback: str = "roundtrip") -> str:
    trip_type = (value or "").strip().lower()
    return trip_type if trip_type in VALID_TRIP_TYPES else fallback

def _coerce_cabin(value: Any) -> str:
    cabin = (value or "").strip().upper()
    return cabin if cabin in VALID_CABINS else "ECONOMY"

def _coerce_sort(value: Any, *, fallback: str = "recommended") -> str:
    sort = (value or "").strip().lower()
    return sort if sort in VALID_SORTS else fallback

def _coerce_combination_mode(value: Any, *, fallback: str = "auto") -> str:
    combination_mode = (value or "").strip().lower()
    return combination_mode if combination_mode in VALID_COMBINATION_MODES else fallback

def _coerce_passengers(value: Any, *, default: int = DEFAULT_PASSENGERS) -> int:
    passengers = _safe_int(value, default)
    return min(MAX_PASSENGERS, max(MIN_PASSENGERS, passengers))

def _coerce_trip_length_days(value: Any, *, default: int = DEFAULT_FLEX_TRIP_LENGTH_DAYS) -> int:
    trip_length_days = _safe_int(value, default)
    return min(MAX_FLEX_TRIP_LENGTH_DAYS, max(MIN_FLEX_TRIP_LENGTH_DAYS, trip_length_days))

def _extract_embedded_airport_code(value: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"[A-Za-z]{3}", raw):
        return raw.upper()

    patterns = [
        r"\(([A-Za-z]{3})\)",
        r"^\s*([A-Za-z]{3})\s*[—-]",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw)
        if match:
            return match.group(1).upper()
    return None

def _normalize_airport_input(value: Any) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None

    code = _extract_embedded_airport_code(raw)
    if code:
        return code

    qn = _norm(raw)
    if qn in METRO_ALIASES and METRO_ALIASES[qn]:
        return METRO_ALIASES[qn][0]

    suggestions = _local_airport_suggest(raw, limit=1)
    if suggestions:
        return (suggestions[0].get("code") or "").strip().upper() or None

    country_code = _country_code_from_text(raw)
    if country_code:
        return _best_airport_for_country(country_code)
    return None

@lru_cache(maxsize=1)
def _get_pycountry_module():
    try:
        return importlib.import_module("pycountry")
    except Exception:
        return None

@lru_cache(maxsize=512)
def _country_code_from_text(value: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None

    cleaned = re.sub(r"[^A-Za-z.\s]", " ", raw)
    qn = _norm(cleaned)
    if not qn:
        return None

    if qn in COUNTRY_NAME_ALIASES:
        return COUNTRY_NAME_ALIASES[qn]

    compact = qn.replace(" ", "")
    if len(compact) == 2 and compact.isalpha():
        return compact.upper()

    pycountry_module = _get_pycountry_module()
    if pycountry_module:
        if len(compact) == 3 and compact.isalpha():
            by_alpha3 = pycountry_module.countries.get(alpha_3=compact.upper())
            if by_alpha3 and getattr(by_alpha3, "alpha_2", None):
                return by_alpha3.alpha_2

        direct = (
            pycountry_module.countries.get(name=raw)
            or pycountry_module.countries.get(common_name=raw)
            or pycountry_module.countries.get(official_name=raw)
        )
        if direct and getattr(direct, "alpha_2", None):
            return direct.alpha_2

        try:
            fuzzy = pycountry_module.countries.search_fuzzy(raw)
            if fuzzy:
                code = getattr(fuzzy[0], "alpha_2", None)
                if code:
                    return code
        except Exception:
            pass
    return None

def _best_airport_for_country(country_code: str) -> str | None:
    cc = (country_code or "").strip().upper()
    if not cc:
        return None

    best_code = None
    best_score = -10**9
    for a in _load_local_airports():
        if (a.get("country") or "").strip().upper() != cc:
            continue
        code = (a.get("code") or "").strip().upper()
        if not code:
            continue
        score = _airport_type_score(a.get("type") or "") + MAJOR_AIRPORT_BOOST.get(code, 0) + _bad_name_penalty(a.get("name") or "")
        if score > best_score:
            best_score = score
            best_code = code
    return best_code

def _ai_text_has_time_signal(user_text: str) -> bool:
    txt = (user_text or "").strip().lower()
    if not txt:
        return False
    patterns = [
        r"\btoday\b",
        r"\btomorrow\b",
        r"\bnext month\b",
        r"\b(?:this|current) month\b",
        r"\b20\d{2}-\d{2}(?:-\d{2})?\b",
        rf"\b(?:{MONTH_NAME_PATTERN})\b",
    ]
    return any(re.search(pat, txt) for pat in patterns)

def _route_missing_error(origin_raw: str, destination_raw: str, *, ai: bool = False, user_text: str = "") -> str:
    if ai:
        if not origin_raw and not destination_raw:
            if _ai_text_has_time_signal(user_text):
                return (
                    "I can see you have dates in mind — great start! I just need to know "
                    "where you're flying from and where you're headed. "
                    "Try something like: \"NYC to London, June 10 to June 17\"."
                )
            return (
                "Happy to help find a flight! I need two things: "
                "where you're flying from (city or airport) and where you're going. "
                "Example: \"New York to Paris, round trip in July\"."
            )
        if not origin_raw:
            return (
                "I know where you're heading — I just need your departure city or airport too. "
                "Try something like: \"from JFK\" or \"from New York\"."
            )
        return (
            "I see you're departing from {origin_raw} — where are you flying to? "
            "Add a destination like \"to London\" or \"to LHR\"."
        ).format(origin_raw=origin_raw)

    if not origin_raw and not destination_raw:
        return "Please enter both origin and destination (e.g., JFK → LAX)."
    if not origin_raw:
        return "Please enter an origin airport or city."
    return "Please enter a destination airport or city."

def _route_invalid_error(which: str, *, ai: bool = False) -> str:
    label = "origin" if which == "origin" else "destination"
    if ai:
        if label == "origin":
            return (
                "I couldn't match a departure airport from what you wrote. "
                "Try using a city name (e.g. \"New York\") or an IATA code (e.g. \"JFK\")."
            )
        return (
            "I couldn't match a destination airport from what you wrote. "
            "Try using a city name (e.g. \"London\") or an IATA code (e.g. \"LHR\")."
        )
    return f"Please choose a valid {label} airport or city."

def _manual_combination_unavailable_error(*, ai: bool = False) -> str:
    if ai:
        return (
            "Choosing flights separately is only available for fixed-date round trips right now. "
            "Please include exact departure and return dates, or let us choose the combination for flexible trips."
        )
    return (
        "Choose-your-own flight combinations are only available for fixed-date round trips. "
        "For cheapest-week or custom-duration trips, we currently choose the best pairing for you."
    )

def _validate_route_inputs(params: dict[str, Any], *, ai: bool = False) -> tuple[dict[str, Any], str | None]:
    normalized = dict(params)
    origin_raw = (normalized.get("origin") or "").strip()
    destination_raw = (normalized.get("destination") or "").strip()
    if not origin_raw or not destination_raw:
        return normalized, _route_missing_error(origin_raw, destination_raw, ai=ai, user_text=(normalized.get("raw_text") or ""))

    origin_code = _normalize_airport_input(origin_raw)
    if not origin_code:
        return normalized, _route_invalid_error("origin", ai=ai)

    destination_code = _normalize_airport_input(destination_raw)
    if not destination_code:
        return normalized, _route_invalid_error("destination", ai=ai)

    if origin_code == destination_code:
        return normalized, "Origin and destination can't be the same airport."

    normalized["origin"] = origin_code
    normalized["destination"] = destination_code
    return normalized, None


def _validate_multicity_legs(
    params: dict[str, Any],
    *,
    ai: bool = False,
) -> tuple[dict[str, Any], str | None]:
    normalized = dict(params)
    raw_legs = normalized.get("legs") or []
    if not isinstance(raw_legs, list):
        return normalized, "Please provide at least 2 legs for a multi-city trip."

    cleaned_legs: list[dict[str, str]] = []
    prev_depart: date | None = None
    for idx, leg in enumerate(raw_legs, start=1):
        if not isinstance(leg, Mapping):
            continue
        origin_raw = str(leg.get("origin") or "").strip()
        destination_raw = str(leg.get("destination") or "").strip()
        depart_raw = str(leg.get("depart_date") or "").strip()

        if not origin_raw or not destination_raw or not depart_raw:
            return normalized, f"Leg {idx} needs origin, destination, and departure date."

        origin_code = _normalize_airport_input(origin_raw)
        if not origin_code:
            return normalized, f"Leg {idx} origin is invalid."
        destination_code = _normalize_airport_input(destination_raw)
        if not destination_code:
            return normalized, f"Leg {idx} destination is invalid."
        if origin_code == destination_code:
            return normalized, f"Leg {idx} origin and destination can't be the same."
        if not _is_valid_iso_date(depart_raw):
            return normalized, f"Leg {idx} departure date must be YYYY-MM-DD."
        if _is_past_date(depart_raw):
            return normalized, f"Leg {idx} departure date must be today or in the future."
        depart_d = _to_date(depart_raw)
        if prev_depart and depart_d < prev_depart:
            return normalized, f"Leg {idx} departs before leg {idx - 1}. Please order legs chronologically."
        prev_depart = depart_d
        cleaned_legs.append(
            {
                "origin": origin_code,
                "destination": destination_code,
                "depart_date": depart_raw,
            }
        )

    if len(cleaned_legs) < 2:
        msg = "Please include at least 2 legs for a multi-city trip."
        return normalized, msg

    normalized["legs"] = cleaned_legs
    normalized["origin"] = cleaned_legs[0]["origin"]
    normalized["destination"] = cleaned_legs[-1]["destination"]
    normalized["depart_date"] = cleaned_legs[0]["depart_date"]
    normalized["return_date"] = None
    normalized["combination_mode"] = "auto"
    return normalized, None


def _build_multicity_form_legs(
    leg_origins: list[str],
    leg_destinations: list[str],
    leg_dates: list[str],
) -> list[dict[str, str]]:
    built_legs: list[dict[str, str]] = []
    previous_destination = ""
    for origin_raw, destination_raw, depart_raw in zip(leg_origins, leg_destinations, leg_dates):
        origin_value = (origin_raw or "").strip().upper()
        destination_value = (destination_raw or "").strip().upper()
        depart_value = (depart_raw or "").strip()

        if not origin_value and previous_destination:
            origin_value = previous_destination

        if not (origin_value or destination_value or depart_value):
            continue

        built_legs.append(
            {
                "origin": origin_value,
                "destination": destination_value,
                "depart_date": depart_value,
            }
        )
        if destination_value:
            previous_destination = destination_value

    return built_legs

def _validate_standard_search_params(params: dict[str, Any], *, ai: bool = False) -> tuple[dict[str, Any], str | None]:
    normalized = dict(params)
    fallback_trip_type = "roundtrip" if normalized.get("return_date") else "oneway"
    normalized["trip_type"] = _coerce_trip_type(normalized.get("trip_type"), fallback=fallback_trip_type)
    normalized["passengers"] = _coerce_passengers(normalized.get("passengers"))
    normalized["cabin"] = _coerce_cabin(normalized.get("cabin"))
    normalized["sort"] = _coerce_sort(normalized.get("sort"), fallback="recommended")
    normalized["combination_mode"] = _coerce_combination_mode(normalized.get("combination_mode"))

    if normalized.get("trip_type") == "multicity":
        normalized, multi_error = _validate_multicity_legs(normalized, ai=ai)
        if multi_error:
            return normalized, multi_error
        return normalized, None

    normalized, route_error = _validate_route_inputs(normalized, ai=ai)
    if route_error:
        return normalized, route_error

    depart_date = (normalized.get("depart_date") or "").strip()
    if not depart_date:
        if ai:
            return normalized, (
                "I have the route — I just need travel dates. "
                "Tell me when you'd like to fly (e.g. \"on June 10th\") or say something like "
                "\"in July\" if you want me to find the cheapest week."
            )
        return normalized, "Please provide a departure date. Example: 'JFK to LAX on 2026-03-10'."

    if not _is_valid_iso_date(depart_date):
        return normalized, "Please use a valid departure date in YYYY-MM-DD format."

    if _is_past_date(depart_date):
        return normalized, "Departure date must be today or in the future."

    if normalized.get("trip_type") == "oneway":
        normalized["return_date"] = None
        normalized["combination_mode"] = "auto"
        return normalized, None

    return_date = (normalized.get("return_date") or "").strip()
    if not return_date:
        if ai:
            return normalized, (
                "Looks like a round trip — when are you coming back? "
                "Add a return date (e.g. \"returning June 17\") or say \"one way\" if you're not coming back."
            )
        return normalized, "Please provide a return date or switch to one-way."

    if not _is_valid_iso_date(return_date):
        return normalized, "Please use a valid return date in YYYY-MM-DD format."

    if _to_date(return_date) < _to_date(depart_date):
        return normalized, "Return date must be the same day or after departure."

    return normalized, None

def _validate_flex_search_params(params: dict[str, Any], *, ai: bool = False) -> tuple[dict[str, Any], str | None]:
    normalized = dict(params)
    normalized["trip_type"] = _coerce_trip_type(normalized.get("trip_type"), fallback="roundtrip")
    normalized["passengers"] = _coerce_passengers(normalized.get("passengers"))
    normalized["cabin"] = _coerce_cabin(normalized.get("cabin"))
    normalized["sort"] = "cheapest"
    normalized["combination_mode"] = _coerce_combination_mode(normalized.get("combination_mode"))

    if normalized["trip_type"] == "multicity":
        return normalized, "Multi-city currently supports fixed dates only. Please use specific dates mode."

    normalized, route_error = _validate_route_inputs(normalized, ai=ai)
    if route_error:
        return normalized, route_error

    if normalized["combination_mode"] == "manual":
        return normalized, _manual_combination_unavailable_error(ai=ai)

    flex_month = (normalized.get("flex_month") or "").strip()
    if not flex_month:
        if ai:
            return normalized, "Please include a target month like 'in July' or 'in 2026-07'."
        return normalized, "Please choose a month in YYYY-MM format (e.g., 2026-05)."

    if not _is_valid_flex_month(flex_month):
        return normalized, "Please choose a valid month in YYYY-MM format (e.g., 2026-05)."

    if _is_past_flex_month(flex_month):
        return normalized, "Please choose the current month or a future month."

    normalized["flex_month"] = flex_month

    if normalized["trip_type"] == "oneway":
        normalized["return_date"] = None
        normalized.pop("trip_length_days", None)
        normalized["combination_mode"] = "auto"
        return normalized, None

    normalized["combination_mode"] = "auto"
    normalized["trip_length_days"] = _coerce_trip_length_days(normalized.get("trip_length_days"))
    return normalized, None

def _unique_preserve(values: list[str]) -> list[str]:
    out = []
    seen = set()
    for value in values:
        item = (value or "").strip().upper()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out

def _register_carrier(code: str | None, name: str | None) -> None:
    iata = (code or "").strip().upper()
    label = (name or "").strip()
    if iata and label and iata not in _airline_name_cache:
        _airline_name_cache[iata] = label

def _carrier_name(code: str | None) -> str:
    carrier_code = (code or "").strip().upper() or "UNKNOWN"
    if carrier_code in ("UNKNOWN", "ZZ"):
        return "Partner airline"
    return _airline_name_cache.get(carrier_code, carrier_code)

# Maps airline IATA codes to their web check-in URLs
_AIRLINE_CHECKIN_URLS: dict[str, str] = {
    "AA": "https://www.aa.com/checkin",
    "UA": "https://www.united.com/checkin",
    "DL": "https://www.delta.com/us/en/check-in/overview",
    "WN": "https://www.southwest.com/flight/retrieveCheckinDoc.html",
    "B6": "https://www.jetblue.com/checkin",
    "AS": "https://www.alaskaair.com/checkin",
    "NK": "https://www.spirit.com/checkin",
    "F9": "https://www.flyfrontier.com/travel/travel-info/baggage/check-in/",
    "HA": "https://www.hawaiianairlines.com/check-in",
    "G4": "https://www.allegiantair.com/check-in",
    "SY": "https://www.suncountry.com/check-in",
    "BA": "https://www.britishairways.com/travel/olcilandingpageauthreq/public/en_us",
    "LH": "https://www.lufthansa.com/us/en/online-check-in",
    "AF": "https://wwws.airfrance.us/checkin",
    "KL": "https://www.klm.com/travel/us_en/prepare_for_travel/at_the_airport/checkin_options/online_checkin.htm",
    "IB": "https://www.iberia.com/us/check-in/",
    "AZ": "https://www.ita-airways.com/en_us/fly-with-us/at-the-airport/check-in.html",
    "SK": "https://www.flysas.com/en/us/booking/checkin/",
    "AY": "https://www.finnair.com/us-en/check-in",
    "LX": "https://www.swiss.com/us/en/prepare/checkin",
    "OS": "https://www.austrian.com/us/en/check-in",
    "TK": "https://www.turkishairlines.com/en-us/flights/flight-services/online-check-in/",
    "EK": "https://www.emirates.com/us/english/manage-booking/online-check-in/",
    "EY": "https://www.etihad.com/en-us/manage/online-check-in",
    "QR": "https://www.qatarairways.com/en-us/check-in.html",
    "SQ": "https://www.singaporeair.com/en_UK/us/travel-info/check-in/online-check-in/",
    "CX": "https://www.cathaypacific.com/cx/en_US/manage-booking/check-in.html",
    "NH": "https://www.ana.co.jp/en/us/travel-information/check-in/web/",
    "JL": "https://www.jal.com/en/inter/boarding/checkin/webci/",
    "KE": "https://www.koreanair.com/us/en/support/check-in.html",
    "OZ": "https://www.flyasiana.com/C/US/EN/contents/online-check-in",
    "MH": "https://www.malaysiaairlines.com/my/en/manage-booking/online-check-in.html",
    "TG": "https://www.thaiairways.com/en_US/travel_information/timetable_onlinecheckin/",
    "QF": "https://www.qantas.com/us/en/travel-info/check-in.html",
    "NZ": "https://www.airnewzealand.us/check-in",
    "AC": "https://www.aircanada.com/ca/en/aco/home/fly/prepare-for-flight/check-in.html",
    "WS": "https://www.westjet.com/en-us/checkin/index",
    "AM": "https://www.aeromexico.com/en-us/travel-information/check-in",
    "LA": "https://www.latamairlines.com/us/en/check-in",
    "G3": "https://www.voegol.com.br/en/informacoes/check-in",
    "AD": "https://www.voeazul.com.br/en/check-in",
    "FR": "https://www.ryanair.com/en/check-in",
    "U2": "https://www.easyjet.com/en/check-in",
    "VY": "https://www.vueling.com/en/travel-information/at-the-airport/check-in",
    "W6": "https://www.wizzair.com/#/booking/check-in",
    "LS": "https://www.jet2.com/check-in",
    "PC": "https://www.flypgs.com/en/check-in",
    "AK": "https://www.airasia.com/check-in/en/gb",
    "FD": "https://www.airasia.com/check-in/en/gb",
    "D7": "https://www.airasia.com/check-in/en/gb",
}

def _airline_checkin_url(iata_code: str | None) -> str:
    code = (iata_code or "").strip().upper()
    return _AIRLINE_CHECKIN_URLS.get(code, "")

def _marketing_carrier_codes_for_segments(segments: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for seg in segments or []:
        marketing = seg.get("marketing_carrier") or {}
        iata = marketing.get("iata_code") or ""
        _register_carrier(iata, marketing.get("name"))
        codes.append(iata)
        codes.append(seg.get("carrierCode") or "")
    return _unique_preserve(codes)

def _operating_carrier_codes_for_segments(segments: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for seg in segments or []:
        operating = seg.get("operating_carrier") or {}
        iata = operating.get("iata_code") or ""
        _register_carrier(iata, operating.get("name"))
        codes.append(iata)
        operating_fallback = seg.get("operating") or {}
        codes.append(operating_fallback.get("carrierCode") or "")
    return _unique_preserve(codes)

def _carrier_label(codes: list[str]) -> str:
    names = [_carrier_name(code) for code in _unique_preserve(codes)]
    if not names:
        return "Unknown Airline"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} + {names[1]}"
    extra = len(names) - 1
    suffix = "" if extra == 1 else "s"
    return f"{names[0]} + {extra} more airline{suffix}"

def _carrier_code_label(codes: list[str]) -> str:
    unique_codes = _unique_preserve(codes)
    if not unique_codes:
        return "UNKNOWN"
    if len(unique_codes) <= 3:
        return " + ".join(unique_codes)
    extra = len(unique_codes) - 1
    return f"{unique_codes[0]} + {extra} more"

def _segment_via_codes(segments: list[dict[str, Any]]) -> list[str]:
    via_codes = []
    for seg in (segments or [])[:-1]:
        code = ((seg.get("destination") or {}).get("iata_code")) or (seg.get("arrival") or {}).get("iataCode")
        if code:
            via_codes.append(code)
    return via_codes

def _partner_operating_codes(marketing_codes: list[str], operating_codes: list[str]) -> list[str]:
    marketing_set = {str(code or "").strip().upper() for code in marketing_codes if str(code or "").strip()}
    partner_codes: list[str] = []
    for code in _unique_preserve(operating_codes):
        normalized = str(code or "").strip().upper()
        if normalized and normalized not in marketing_set:
            partner_codes.append(normalized)
    return partner_codes

def _slice_operating_note(segments: list[dict[str, Any]]) -> str:
    marketing_codes = _marketing_carrier_codes_for_segments(segments)
    operating_codes = _operating_carrier_codes_for_segments(segments)
    partner_codes = _partner_operating_codes(marketing_codes, operating_codes)
    if not partner_codes:
        return ""
    partner_names = [_carrier_name(code) for code in partner_codes]
    if len(partner_names) == 1:
        return f"Operated by {partner_names[0]}"
    if len(partner_names) == 2:
        return f"Operated by {partner_names[0]} and {partner_names[1]}"
    return f"Includes {len(partner_names)} operating airlines"

def _airline_mix_label(out_codes: list[str], in_codes: list[str]) -> str:
    all_codes = _unique_preserve([*out_codes, *in_codes])
    out_label = _carrier_label(out_codes)
    if in_codes:
        in_label = _carrier_label(in_codes)
        if out_label == in_label:
            if len(all_codes) == 1:
                return "Same airline both ways"
            return "Same airline mix both ways"
        return f"Outbound {out_label} • Return {in_label}"
    if len(all_codes) <= 1:
        return "Single-airline itinerary"
    return f"{len(all_codes)} airlines on this itinerary"

def _offer_airline_mix_label(slice_meta: list[dict[str, Any]]) -> str:
    if not slice_meta:
        return ""
    if len(slice_meta) == 1:
        return str(slice_meta[0].get("operating_note") or "").strip()

    first = slice_meta[0]
    second = slice_meta[1]
    first_airline = str(first.get("airline") or "").strip()
    second_airline = str(second.get("airline") or "").strip()
    labels: list[str] = []
    if first_airline and second_airline and first_airline != second_airline:
        labels.append(f"Outbound {first_airline} • Return {second_airline}")
    elif first_airline:
        labels.append(f"{first_airline} both ways")

    notes: list[str] = []
    first_note = str(first.get("operating_note") or "").strip()
    second_note = str(second.get("operating_note") or "").strip()
    if first_note and second_note and first_note == second_note:
        notes.append(first_note)
    else:
        if first_note:
            notes.append(f"Outbound {first_note.lower()}")
        if second_note:
            notes.append(f"Return {second_note.lower()}")

    return " • ".join([*labels, *notes]).strip()

# ------------------------------------------------------------
# Thread-safe TTL cache
# ------------------------------------------------------------
_CACHE_NONE = object()


class TTLCache:
    def __init__(self, maxsize: int = 1024, ttl_seconds: int = 300):
        self.maxsize = maxsize
        self.ttl = ttl_seconds
        self._lock = threading.RLock()
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def lookup(self, key: Any) -> tuple[bool, Any | None]:
        now = time.time()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return False, None
            exp, value = item
            if exp <= now:
                self._data.pop(key, None)
                return False, None
            self._data.move_to_end(key)
            return True, None if value is _CACHE_NONE else value

    def get(self, key: Any) -> Any | None:
        found, value = self.lookup(key)
        return value if found else None

    def set(self, key: Any, value: Any) -> None:
        now = time.time()
        exp = now + self.ttl
        stored_value = _CACHE_NONE if value is None else value
        with self._lock:
            if key in self._data:
                self._data.pop(key, None)
            self._data[key] = (exp, stored_value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def get_or_set(self, key: Any, builder):
        found, value = self.lookup(key)
        if found:
            return value
        value = builder()
        self.set(key, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

SEARCH_CACHE = TTLCache(maxsize=4096, ttl_seconds=15 * 60)
RAW_SEARCH_CACHE = TTLCache(maxsize=4096, ttl_seconds=15 * 60)
RESULTS_RELOAD_CACHE = TTLCache(maxsize=2048, ttl_seconds=45 * 60)
REVIEW_FARE_OPTIONS_CACHE = TTLCache(maxsize=4096, ttl_seconds=10 * 60)
REVIEW_FARE_OPTIONS_BY_ITINERARY_CACHE = TTLCache(maxsize=2048, ttl_seconds=10 * 60)
REVIEW_FARE_OPTIONS_CACHE_SCHEMA = "20260424-v2"
AIRPORT_SUGGEST_CACHE = TTLCache(maxsize=1024, ttl_seconds=15 * 60)
AIRPORT_NAME_CACHE = TTLCache(maxsize=2048, ttl_seconds=24 * 3600)
FLEX_RESULT_CACHE = TTLCache(maxsize=1024, ttl_seconds=20 * 60)
FLIGHT_DATES_CACHE = TTLCache(maxsize=1024, ttl_seconds=20 * 60)
CHEAPEST_SNAPSHOT_CACHE = TTLCache(maxsize=4096, ttl_seconds=15 * 60)
RECENT_ORDER_CACHE = TTLCache(maxsize=256, ttl_seconds=10 * 60)
# Booking-reference → full order dict; populated after cancellation / confirmation so that
# the linked-bookings list on the manage page sees updated status without relying on Duffel
# list_orders propagation timing.
RECENT_REF_CACHE = TTLCache(maxsize=256, ttl_seconds=20 * 60)
# LiteAPI room offer_id -> the prebook (rate lock) built for it, so checkout's
# GET (review) and POST (book) steps share one lock instead of re-prebooking
# on every request. 15 min mirrors RECENT_ORDER_CACHE's ballpark for the
# flight analog; re-prebooked again at POST time regardless (see M6 price drift).
HOTEL_PREBOOK_CACHE = TTLCache(maxsize=512, ttl_seconds=15 * 60)
AI_PARSE_PREVIEW_CACHE = TTLCache(maxsize=4096, ttl_seconds=90)
MANAGE_BOOKING_ATTEMPT_CACHE = TTLCache(maxsize=2048, ttl_seconds=15 * 60)
USER_ACCOUNT_CACHE = TTLCache(maxsize=2048, ttl_seconds=30 * 24 * 3600)
ORDER_CHANGE_OPTIONS_CACHE = TTLCache(maxsize=1024, ttl_seconds=20 * 60)
B2C_LOGIN_ATTEMPT_CACHE = TTLCache(maxsize=4096, ttl_seconds=15 * 60)  # brute-force tracking
_ACCOUNT_DB_LOCK = threading.RLock()
_ACCOUNT_DB_READY = False
MAX_SAVED_SEARCHES_PER_ACCOUNT = 40

_RESULTS_RELOAD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,96}$")


def _new_results_reload_token() -> str:
    return os.urandom(16).hex()


def _coerce_results_reload_token(value: Any) -> str:
    token = str(value or "").strip()
    return token if _RESULTS_RELOAD_TOKEN_RE.fullmatch(token) else ""


def _results_reload_token_from_form(form: Mapping[str, Any] | None = None) -> str:
    payload = form if form is not None else request.form
    return _coerce_results_reload_token(payload.get("results_reload_token"))


def _store_results_reload_html(html: str, *, token: str | None = None) -> str:
    reload_token = _coerce_results_reload_token(token) or _new_results_reload_token()
    RESULTS_RELOAD_CACHE.set(
        reload_token,
        {
            "html": html,
            "created_at": int(time.time()),
        },
    )
    return reload_token


def _results_complete_stream_event(html: str) -> dict[str, str]:
    reload_token = _store_results_reload_html(html, token=_results_reload_token_from_form())
    return {
        "type": "complete",
        "html": html,
        "url": url_for("results_reload", token=reload_token),
    }


def _normalize_ai_text_for_cache(ai_text: str) -> str:
    return re.sub(r"\s+", " ", (ai_text or "").strip()).lower()


def _ai_parse_cache_key(ai_text: str) -> str:
    normalized = _normalize_ai_text_for_cache(ai_text)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _cache_ai_parse_result(ai_text: str, parsed: Mapping[str, Any]) -> str:
    normalized = _normalize_ai_text_for_cache(ai_text)
    key = _ai_parse_cache_key(ai_text)
    token = f"ai_{key[:16]}"
    cached_payload = {
        "token": token,
        "key": key,
        "normalized_text": normalized,
        "params": dict(parsed),
    }
    AI_PARSE_PREVIEW_CACHE.set(token, cached_payload)
    AI_PARSE_PREVIEW_CACHE.set(key, cached_payload)
    return token


def _get_cached_ai_parse_result(ai_text: str, parse_token: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    normalized = _normalize_ai_text_for_cache(ai_text)
    if not normalized:
        return None, None

    lookup_keys: list[str] = []
    if parse_token:
        lookup_keys.append(parse_token.strip())
    lookup_keys.append(_ai_parse_cache_key(ai_text))

    for cache_key in lookup_keys:
        if not cache_key:
            continue
        cached = AI_PARSE_PREVIEW_CACHE.get(cache_key)
        if not isinstance(cached, Mapping):
            continue
        if cached.get("normalized_text") != normalized:
            continue
        params = cached.get("params")
        if not isinstance(params, Mapping):
            continue
        token = str(cached.get("token") or "").strip() or None
        return dict(params), token
    return None, None


def _resolve_ai_flight_params(ai_text: str, parse_token: str = "") -> tuple[dict[str, Any] | None, str | None]:
    """Flight-shaped params for an AI search's flight rendering step.

    A cached parse under this ai_text may be flight-only ("flights" kind,
    the common case) or a combined flight+stay parse ("both" kind, cached by
    /search/shell when the same request also asked for a hotel) — either way
    the flight results renderer only ever wants the flat flight dict. Falls
    back to a fresh flight-only parse when nothing usable is cached.
    """
    cached, cached_token = _get_cached_ai_parse_result(ai_text, parse_token=parse_token)
    if cached:
        if cached.get("kind") == "both":
            flight = cached.get("flight")
            if isinstance(flight, Mapping) and flight:
                return dict(flight), cached_token
        else:
            return cached, cached_token

    params = parse_ai_flight_request(ai_text)
    if not params:
        return None, None
    params = dict(params)
    params["kind"] = "flights"
    cached_token = _cache_ai_parse_result(ai_text, params)
    return params, cached_token

# ------------------------------------------------------------
# Flex-scan rate limiter
# ------------------------------------------------------------
class _RateLimiter:
    """Token-bucket rate limiter: enforces a minimum gap between calls."""
    def __init__(self, rate: float):
        self._min_gap = 1.0 / max(rate, 0.01)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            gap = self._last + self._min_gap - time.time()
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()


_FLEX_RATE_LIMITER = _RateLimiter(rate=FLEX_SCAN_RPS)

# Shared rate-limit reset time written by any thread that receives a 429.
_rl_reset_lock = threading.Lock()
_rl_reset_at: float = 0.0  # absolute epoch seconds; 0 means "no active reset"

# ------------------------------------------------------------
# Local airport index
# ------------------------------------------------------------
@lru_cache(maxsize=1)
def _load_local_airports():
    out = []
    if not os.path.exists(AIRPORTS_CSV_PATH):
        print("WARNING: airports.csv not found at", AIRPORTS_CSV_PATH)
        return out

    with open(AIRPORTS_CSV_PATH, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            code = (row.get("iata_code") or "").strip().upper()
            if len(code) != 3:
                continue

            airport_type = (row.get("type") or "").strip().lower()
            scheduled = (row.get("scheduled_service") or "").strip().lower()

            # hard reject junk classes
            if airport_type in BLOCKED_TYPES:
                continue

            # keep only large/medium commercial airports
            if airport_type not in {"large_airport", "medium_airport"}:
                continue
            if scheduled != "yes":
                continue

            name = (row.get("name") or "").strip()
            city = (row.get("municipality") or "").strip()
            country = (row.get("iso_country") or "").strip().upper()
            region = (row.get("iso_region") or "").strip().upper()

            out.append({
                "code": code,
                "name": name,
                "city": city,
                "country": country,
                "region": region,
                "scheduled": scheduled,
                "type": airport_type,
                "_name_norm": _norm(name),
                "_city_norm": _norm(city),
            })
    return out

@lru_cache(maxsize=1)
def _airport_code_map():
    return {a["code"]: a for a in _load_local_airports()}


def _airport_city_for_code(iata_code: str | None) -> str:
    """City name for an IATA code, or "" if unknown — used to anchor a hotel
    search on a flight's destination without a second geocoding round trip."""
    code = str(iata_code or "").strip().upper()
    if not code:
        return ""
    row = _airport_code_map().get(code)
    return str(row.get("city") or "").strip() if row else ""


@lru_cache(maxsize=1)
def _load_airport_coords():
    """Same large/medium-commercial-airport filter as _load_local_airports,
    but keeping lat/lng for the "airport nearest a GPS point" lookup used by
    the homepage's location-aware popular-flights widget."""
    out = []
    if not os.path.exists(AIRPORTS_CSV_PATH):
        return out
    with open(AIRPORTS_CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = (row.get("iata_code") or "").strip().upper()
            if len(code) != 3:
                continue
            airport_type = (row.get("type") or "").strip().lower()
            # Large only, not medium: a nearest-by-distance search over
            # medium airports too tends to surface small regional/GA fields
            # (e.g. Hawthorne over LAX for downtown LA) that aren't the
            # recognizable major hub this widget wants as "your airport".
            if airport_type != "large_airport":
                continue
            if (row.get("scheduled_service") or "").strip().lower() != "yes":
                continue
            try:
                lat = float(row.get("latitude_deg") or "")
                lng = float(row.get("longitude_deg") or "")
            except (TypeError, ValueError):
                continue
            out.append({
                "code": code,
                "city": (row.get("municipality") or "").strip(),
                "country": (row.get("iso_country") or "").strip().upper(),
                "lat": lat,
                "lng": lng,
            })
    return out


def _nearest_airport(lat: float, lng: float) -> dict[str, Any] | None:
    """Haversine great-circle distance to every candidate airport; returns
    the closest one. A linear scan over a few thousand rows is fast enough
    for a once-per-session lookup."""
    best = None
    best_dist = float("inf")
    lat_rad = math.radians(lat)
    for row in _load_airport_coords():
        dlat = math.radians(row["lat"] - lat)
        dlng = math.radians(row["lng"] - lng)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat_rad) * math.cos(math.radians(row["lat"])) * math.sin(dlng / 2) ** 2
        )
        dist = 2 * math.asin(min(1, math.sqrt(a)))
        if dist < best_dist:
            best_dist = dist
            best = row
    return best

def _airport_display_name_local(iata_code: str) -> str:
    code = (iata_code or "").strip().upper()
    if not code:
        return ""
    cached = AIRPORT_NAME_CACHE.get(code)
    if cached is not None:
        return cached
    row = _airport_code_map().get(code)
    val = f"{row['name']} ({code})" if row and row.get("name") else code
    AIRPORT_NAME_CACHE.set(code, val)
    return val

def _airport_header_label_local(iata_code: str) -> str:
    code = (iata_code or "").strip().upper()
    if not code:
        return ""
    row = _airport_code_map().get(code)
    if row:
        city = str(row.get("city") or "").strip()
        if city:
            return f"{city} ({code})"
    return _airport_display_name_local(code)

def _build_label(a: dict) -> dict:
    code = a["code"]
    display_city = (a.get("city") or "").strip()
    display_name = (a.get("name") or "").strip()
    country = (a.get("country") or "").strip()
    loc_parts = [x for x in [display_city, country] if x]
    loc = f" ({', '.join(loc_parts)})" if loc_parts else ""
    return {
        "code": code,
        "label": f"{code} — {display_name}{loc}".strip(),
        "subType": "AIRPORT"
    }


def _iata_city_row(code: str) -> dict[str, Any] | None:
    spec = CITY_METRO_GROUPS.get(code.upper())
    if not spec:
        return None
    name = (spec.get("name") or code).strip()
    country = (spec.get("country") or "").strip().upper()
    loc = f" ({name}, {country})" if name or country else ""
    return {
        "code": code.upper(),
        "label": f"{code.upper()} — {name} (all airports){loc}".strip(),
        "subType": "CITY",
    }


def _query_matches_city_metro_group(qn: str, city_code: str, spec: dict[str, Any]) -> bool:
    if len(qn) < 3:
        return False
    ck = city_code.lower()
    if ck == qn or ck.startswith(qn):
        return True
    nm = _norm(str(spec.get("name") or ""))
    if nm and (nm == qn or nm.startswith(qn)):
        return True
    for alias in spec.get("aliases", ()):
        an = _norm(str(alias))
        if an and (an == qn or an.startswith(qn)):
            return True
    return False


def _airport_in_matching_city_metro_group(qn: str, code: str) -> bool:
    for c_metro, spec in CITY_METRO_GROUPS.items():
        if not _query_matches_city_metro_group(qn, c_metro, spec):
            continue
        if code in spec["airports"]:
            return True
    return False


def _airport_text_matches_query(qn: str, code_l: str, name: str, city: str) -> bool:
    """True if the query clearly ties to this airport's code, city, or name."""
    if not qn:
        return False
    if code_l == qn or (code_l.startswith(qn) and len(qn) >= 2):
        return True
    if len(qn) >= 3 and qn in code_l:
        return True
    if city == qn or (qn and city.startswith(qn)):
        return True
    if len(qn) >= 3 and qn in city:
        return True
    if name == qn or (qn and name.startswith(qn)):
        return True
    if len(qn) >= 3 and qn in name:
        return True
    return False


def _remote_place_matches_query(qn: str, item: dict[str, Any]) -> bool:
    """Drop Duffel place rows that do not relate to the typed query (avoid global hubs)."""
    if not qn:
        return True
    code = (item.get("code") or "").strip().lower()
    raw = (item.get("label") or "").strip()
    label_lo = raw.lower()
    body = raw.split("—", 1)[-1].strip().lower() if "—" in raw else label_lo
    if code == qn or code.startswith(qn) or (len(code) == 3 and qn.startswith(code) and len(qn) <= 4):
        return True
    if len(qn) < 3:
        return False
    if qn in label_lo or label_lo.startswith(qn):
        return True
    if qn in body or body.startswith(qn):
        return True
    for part in re.split(r"[\s,()/]+", body):
        if len(part) < 3:
            continue
        if part.startswith(qn) or qn in part:
            return True
    return False


def _local_iata_city_suggestions(q: str) -> list[dict[str, Any]]:
    """Grouped IATA city/metro codes (e.g. BJS) for autocomplete."""
    qn = _norm(q)
    if len(qn) < 3:
        return []

    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for city_code, spec in CITY_METRO_GROUPS.items():
        if not _query_matches_city_metro_group(qn, city_code, spec):
            continue
        row = _iata_city_row(city_code)
        if not row:
            continue
        ck = city_code.lower()
        if ck == qn:
            pri = 0
        elif ck.startswith(qn):
            pri = 10 + len(ck) - len(qn)
        else:
            pri = 50
        ranked.append((pri, city_code, row))

    ranked.sort(key=lambda t: (t[0], t[1]))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, c, row in ranked:
        if c in seen:
            continue
        seen.add(c)
        out.append(row)
    return out[:5]


def _query_intent(qn: str) -> str:
    if qn in METRO_ALIASES:
        return "metro"
    if qn in US_STATES or qn in US_ABBR_TO_NAME:
        return "state"
    if re.fullmatch(r"[a-z]{3}", qn):
        u = qn.upper()
        if u in _airport_code_map():
            return "iata"
        if u in CITY_METRO_GROUPS:
            return "city_code"
    if "airport" in qn or "international" in qn:
        return "airport_name"
    return "city"

def _score_airport_for_query(a: dict, qn: str, intent: str) -> int:
    code = (a.get("code") or "").upper()
    code_l = code.lower()
    name = a.get("_name_norm") or ""
    city = a.get("_city_norm") or ""
    country = (a.get("country") or "").upper()
    region = (a.get("region") or "").upper()
    airport_type = (a.get("type") or "").lower()
    scheduled = (a.get("scheduled") or "").lower()

    score = 0

    if scheduled == "yes":
        score += 300

    score += _airport_type_score(airport_type)
    score += MAJOR_AIRPORT_BOOST.get(code, 0)
    score += _bad_name_penalty(name)

    # Intent-specific logic
    if intent == "iata":
        if qn == code_l:
            score += 6000
        elif code_l.startswith(qn):
            score += 2600
        elif qn in code_l:
            score += 900

        if qn in city:
            score += 180
        if qn in name:
            score += 120

        # Never rank mega-hubs on base boosts alone — require a real IATA tie.
        if not (qn == code_l or code_l.startswith(qn) or (len(qn) >= 3 and qn in code_l)):
            return -999999

    elif intent == "city_code":
        spec = CITY_METRO_GROUPS.get(qn.upper())
        if not spec:
            return -999999
        preferred = spec["airports"]
        if code not in preferred:
            return -999999
        score += 4500 - (preferred.index(code) * 250)

    elif intent == "metro":
        preferred = METRO_ALIASES.get(qn, [])
        if preferred and code not in preferred:
            return -999999

        if code in preferred:
            score += 4500 - (preferred.index(code) * 250)

        if city == qn:
            score += 1000
        elif qn in city:
            score += 500
        if qn in name:
            score += 250

    elif intent == "state":
        state_abbr = US_STATES.get(qn) or US_STATES.get(US_ABBR_TO_NAME.get(qn, ""))
        if not state_abbr:
            return -999999

        if not (country == "US" and region == f"US-{state_abbr}"):
            return -999999

        score += 2600
        if airport_type == "large_airport":
            score += 1500
        elif airport_type == "medium_airport":
            score += 900

    elif intent == "airport_name":
        if name == qn:
            score += 4000
        elif name.startswith(qn):
            score += 2200
        elif qn in name:
            score += 1200

        if city == qn:
            score += 900
        elif qn in city:
            score += 400

        if code_l.startswith(qn):
            score += 500

        if not _airport_text_matches_query(qn, code_l, name, city):
            return -999999

    else:  # city
        if city == qn:
            score += 4200
        elif city.startswith(qn):
            score += 2400
        elif qn in city:
            score += 1000

        if name == qn:
            score += 1800
        elif name.startswith(qn):
            score += 1000
        elif qn in name:
            score += 450

        if code_l == qn:
            score += 4000
        elif code_l.startswith(qn):
            score += 1200

        # if city maps to known metro airports, boost them
        if qn in METRO_ALIASES and code in METRO_ALIASES[qn]:
            score += 2500 - (METRO_ALIASES[qn].index(code) * 200)

        for c_metro, spec in CITY_METRO_GROUPS.items():
            if not _query_matches_city_metro_group(qn, c_metro, spec):
                continue
            ports = spec["airports"]
            if code in ports:
                score += 4000 - (ports.index(code) * 200)
                break

        if not (
            _airport_text_matches_query(qn, code_l, name, city)
            or (qn in METRO_ALIASES and code in METRO_ALIASES[qn])
            or _airport_in_matching_city_metro_group(qn, code)
        ):
            return -999999

    # small cleanup penalty for weak fuzzy matches
    if len(qn) >= 3 and qn not in city and qn not in name and not code_l.startswith(qn):
        score -= 120

    return score

def _local_airport_suggest(q: str, limit: int = AIRPORT_SUGGEST_LIMIT):
    qn = _norm(q)
    if len(qn) < 3:
        return []

    intent = _query_intent(qn)
    airports = _load_local_airports()

    scored = []
    for a in airports:
        s = _score_airport_for_query(a, qn, intent)
        if s <= 0:
            continue
        label = _build_label(a)
        scored.append((s, -len(label["label"]), label["code"], label))

    top = heapq.nlargest(limit, scored, key=lambda item: item[:3])
    return [item[3] for item in top]
# ------------------------------------------------------------
# Gemini
# ------------------------------------------------------------
GEMINI_MODEL_NAME = "gemini-2.5-flash"


class _GeminiModel:
    def __init__(self, client, model_name: str):
        self.client = client
        self.model_name = model_name

    def generate_content(self, prompt: str, *, json_mode: bool = False):
        config = _genai_types.GenerateContentConfig(response_mime_type="application/json") if json_mode and _genai_types else None
        return self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            **({"config": config} if config else {}),
        )


try:
    from google import genai
    from google.genai import types as _genai_types

    model = _GeminiModel(genai.Client(api_key=GOOGLE_API_KEY), GEMINI_MODEL_NAME) if GOOGLE_API_KEY else None
except Exception:
    model = None
    _genai_types = None

MONTH_NAME_TO_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
MONTH_NAME_PATTERN = "|".join(MONTH_NAME_TO_NUM.keys())

def _extract_ai_flex_month(user_text: str) -> str | None:
    txt = (user_text or "").strip().lower()
    if not txt:
        return None

    m = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", txt)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    today = date.today()
    if re.search(r"\bnext month\b", txt):
        next_month_anchor = today.replace(day=28) + timedelta(days=4)
        next_month = next_month_anchor.replace(day=1)
        return next_month.strftime("%Y-%m")

    if re.search(r"\b(?:this|current) month\b", txt):
        return today.strftime("%Y-%m")

    m = re.search(rf"\b(?:in|for|during)\s+({MONTH_NAME_PATTERN})(?:\s+(20\d{{2}}))?\b", txt)
    if not m:
        m = re.search(rf"\b({MONTH_NAME_PATTERN})(?:\s+(20\d{{2}}))?\b", txt)
    if not m:
        return None

    month_num = MONTH_NAME_TO_NUM[m.group(1)]
    year_str = m.group(2)
    year = int(year_str) if year_str else today.year
    if not year_str and month_num < today.month:
        year += 1
    return f"{year:04d}-{month_num:02d}"

def _user_text_has_explicit_day_precision(user_text: str) -> bool:
    txt = (user_text or "").strip().lower()
    if not txt:
        return False

    patterns = [
        r"\b20\d{2}-\d{2}-\d{2}\b",
        r"\b(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])(?:[/-](?:20)?\d{2})?\b",
        rf"\b(?:{MONTH_NAME_PATTERN})\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*20\d{{2}})?\b",
        rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{MONTH_NAME_PATTERN})(?:\s+20\d{{2}})?\b",
        r"\b(?:on\s+)?(?:the\s+)?([1-9]|[12]\d|3[01])(?:st|nd|rd|th)\s+(?:of\s+)?(?:next|this|current)\s+month\b",
    ]
    return any(re.search(pattern, txt) for pattern in patterns)

def _clear_inferred_ai_dates_for_month_only_request(parsed: dict[str, Any], user_text: str) -> dict[str, Any]:
    if not parsed:
        return parsed
    if _extract_ai_flex_month(user_text) is None:
        return parsed
    if _user_text_has_explicit_day_precision(user_text):
        return parsed

    cleaned = dict(parsed)
    cleaned["depart_date"] = None
    cleaned["return_date"] = None
    return cleaned

def _extract_ai_trip_length_days(user_text: str) -> int | None:
    txt = (user_text or "").strip().lower()
    if not txt:
        return None

    patterns = [
        (r"\b(\d{1,2})\s*day\s+trip\b", lambda n: int(n)),
        (r"\btrip\s+for\s+(\d{1,2})\s*days?\b", lambda n: int(n)),
        (r"\b(\d{1,2})\s*days?\b", lambda n: int(n)),
        (r"\b(\d{1,2})\s*nights?\b", lambda n: int(n)),
        (r"\b(\d{1,2})\s*week(?:s)?\b", lambda n: int(n) * 7),
        (r"\bone\s+week\b", lambda _: 7),
        (r"\btwo\s+weeks\b", lambda _: 14),
    ]
    for pat, fn in patterns:
        m = re.search(pat, txt)
        if m:
            return fn(m.group(1) if m.groups() else None)
    return None

def _extract_ai_trip_type(user_text: str, parsed: dict[str, Any] | None = None) -> str | None:
    txt = (user_text or "").strip().lower()
    if not txt and not parsed:
        return None

    if re.search(r"\bmulti[\s-]?city\b", txt) or re.search(r"\bmultiple\s+cities\b", txt):
        return "multicity"
    if re.search(r"\bone[\s-]?way\b", txt):
        return "oneway"
    if re.search(r"\bround[\s-]?trip\b", txt):
        return "roundtrip"
    if parsed and parsed.get("return_date"):
        return "roundtrip"
    if _extract_ai_trip_length_days(txt) is not None:
        return "roundtrip"
    return None

def _extract_ai_combination_mode(user_text: str) -> str | None:
    txt = (user_text or "").strip().lower()
    if not txt:
        return None

    manual_patterns = [
        r"\bgoogle flights\b",
        r"\bchoose (?:my|our|the)?\s*own (?:combination|combinations|flights?)\b",
        r"\bpick (?:my|our|the)?\s*own (?:combination|combinations|flights?)\b",
        r"\bchoose flights separately\b",
        r"\bpick flights separately\b",
        r"\bchoose departure first\b",
        r"\bshow departure flights first\b",
        r"\boutbound first\b",
        r"\breturn second\b",
        r"\blet me choose (?:the )?(?:departure|outbound|return|combination|flights?)\b",
        r"\bi want to choose (?:the )?(?:departure|outbound|return|combination|flights?)\b",
        r"\bseparate(?:ly)?\s+(?:choose|pick|select)\b",
    ]
    if any(re.search(pattern, txt) for pattern in manual_patterns):
        return "manual"

    return None

def _looks_like_ai_flex_request(user_text: str, parsed: dict[str, Any] | None = None) -> bool:
    txt = (user_text or "").strip().lower()
    if not txt:
        return False
    if _user_text_has_explicit_day_precision(txt):
        return False
    has_month = _extract_ai_flex_month(txt) is not None
    has_length = _extract_ai_trip_length_days(txt) is not None
    trip_type = _extract_ai_trip_type(txt, parsed)
    if trip_type == "multicity":
        return False
    has_relative_month_phrase = bool(re.search(r"\bnext month\b|\b(?:this|current) month\b", txt))
    flex_words = any(w in txt for w in ["cheapest week", "cheapest ", "best ", "flexible", "any time in", "during "])
    return has_month and (has_length or flex_words or trip_type == "oneway" or has_relative_month_phrase)

def _extract_ai_relative_depart_date(user_text: str) -> str | None:
    txt = (user_text or "").strip().lower()
    if not txt:
        return None
    today = date.today()

    relative_day_match = re.search(
        r"\b(?:on\s+)?(?:the\s+)?([1-9]|[12]\d|3[01])(?:st|nd|rd|th)\s+(?:of\s+)?(next|this|current)\s+month\b",
        txt,
    )
    if relative_day_match:
        day = int(relative_day_match.group(1))
        relative_month = relative_day_match.group(2)
        if relative_month == "next":
            month_anchor = today.replace(day=28) + timedelta(days=4)
            year = month_anchor.year
            month = month_anchor.month
        else:
            year = today.year
            month = today.month
        _, last_day = monthrange(year, month)
        if day <= last_day:
            return date(year, month, day).isoformat()
        return None

    if re.search(r"\btoday\b", txt):
        return today.isoformat()
    if re.search(r"\btomorrow\b", txt):
        return (today + timedelta(days=1)).isoformat()
    return None


def _extract_ai_relative_return_offset_days(user_text: str) -> int | None:
    """
    Detect an explicit "how long after departure do you come back" signal,
    e.g. "come back the next day", "returning the following day", "same day
    return", "back in 3 days". Returns the number of days between depart and
    return the user actually asked for, or None if they didn't say.

    This exists so phrasing like "...and come back the next day" always wins
    over a generic default window (e.g. the standard Thanksgiving weekend
    default), instead of being silently discarded.
    """
    txt = (user_text or "").strip().lower()
    if not txt:
        return None

    return_verbs = r"(?:come\s+back|coming\s+back|comes\s+back|return(?:ing)?|head(?:ing)?\s+back|fly(?:ing)?\s+back|be\s+back|get\s+back)"
    next_day_phrase = r"(?:the\s+)?(?:next|following)\s+day"

    if re.search(rf"\b{return_verbs}\b[^.?!]{{0,25}}\b{next_day_phrase}\b", txt):
        return 1
    if re.search(rf"\b{next_day_phrase}\b[^.?!]{{0,25}}\b{return_verbs}\b", txt):
        return 1
    if re.search(rf"\b{return_verbs}\b[^.?!]{{0,20}}\ba\s+day\s+(?:after|later)\b", txt):
        return 1
    if re.search(r"\bsame[\s-]day\s+(?:return|round[\s-]?trip)\b", txt) or re.search(
        r"\bthere\s+and\s+back\s+in\s+a\s+day\b", txt
    ):
        return 0

    m = re.search(rf"\b{return_verbs}\b[^.?!]{{0,20}}\bin\s+(\d{{1,2}})\s+days?\b", txt)
    if m:
        return int(m.group(1))
    m = re.search(rf"\b{return_verbs}\b[^.?!]{{0,20}}\b(\d{{1,2}})\s+days?\s+later\b", txt)
    if m:
        return int(m.group(1))

    return None


def _us_thanksgiving(year: int) -> date:
    """Fourth Thursday of November (US)."""
    nov1 = date(year, 11, 1)
    days_to_thu = (3 - nov1.weekday()) % 7
    first_thu = nov1 + timedelta(days=days_to_thu)
    return first_thu + timedelta(weeks=3)


def _western_easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm; returns Easter Sunday."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday_of_month(year: int, month: int, weekday: int, nth: int) -> date:
    """weekday: Monday=0...Sunday=6, nth >= 1."""
    first = date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return first + timedelta(days=delta + (nth - 1) * 7)


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """weekday: Monday=0...Sunday=6."""
    last_day = monthrange(year, month)[1]
    last = date(year, month, last_day)
    delta = (last.weekday() - weekday) % 7
    return last - timedelta(days=delta)


# Movable lunar/lunisolar holidays don't have a closed-form formula, so they're
# tracked as a lookup table (civil-calendar approximations; Islamic dates in
# particular can shift by a day depending on regional moon sighting). Extend
# these tables periodically as new years approach.
_DIWALI_DATES: dict[int, date] = {
    2025: date(2025, 10, 20), 2026: date(2026, 11, 8), 2027: date(2027, 10, 29),
    2028: date(2028, 10, 17), 2029: date(2029, 11, 5), 2030: date(2030, 10, 26),
    2031: date(2031, 11, 14), 2032: date(2032, 11, 2),
}
_LUNAR_NEW_YEAR_DATES: dict[int, date] = {
    2025: date(2025, 1, 29), 2026: date(2026, 2, 17), 2027: date(2027, 2, 6),
    2028: date(2028, 1, 26), 2029: date(2029, 2, 13), 2030: date(2030, 2, 3),
    2031: date(2031, 1, 23), 2032: date(2032, 2, 11),
}
_EID_AL_FITR_DATES: dict[int, date] = {
    2025: date(2025, 3, 30), 2026: date(2026, 3, 20), 2027: date(2027, 3, 9),
    2028: date(2028, 2, 26), 2029: date(2029, 2, 14), 2030: date(2030, 2, 4),
    2031: date(2031, 1, 24), 2032: date(2032, 1, 13),
}
_EID_AL_ADHA_DATES: dict[int, date] = {
    2025: date(2025, 6, 6), 2026: date(2026, 5, 27), 2027: date(2027, 5, 16),
    2028: date(2028, 5, 5), 2029: date(2029, 4, 24), 2030: date(2030, 4, 13),
    2031: date(2031, 4, 2), 2032: date(2032, 3, 21),
}
_HANUKKAH_START_DATES: dict[int, date] = {
    2025: date(2025, 12, 14), 2026: date(2026, 12, 4), 2027: date(2027, 12, 24),
    2028: date(2028, 12, 12), 2029: date(2029, 12, 1), 2030: date(2030, 12, 20),
    2031: date(2031, 12, 9), 2032: date(2032, 11, 27),
}


def _diwali_date(year: int) -> date | None:
    return _DIWALI_DATES.get(year)


def _lunar_new_year_date(year: int) -> date | None:
    return _LUNAR_NEW_YEAR_DATES.get(year)


def _eid_al_fitr_date(year: int) -> date | None:
    return _EID_AL_FITR_DATES.get(year)


def _eid_al_adha_date(year: int) -> date | None:
    return _EID_AL_ADHA_DATES.get(year)


def _hanukkah_start_date(year: int) -> date | None:
    return _HANUKKAH_START_DATES.get(year)


# Phrases like "on Thanksgiving day" or "on Diwali" are an explicit signal
# that the user wants to depart on the holiday's actual date, overriding the
# generic "day before / days after" default windows below (which exist only
# because most defaults assume the traveler wants the holiday itself in the
# middle of the trip, e.g. flying in the eve of Thanksgiving).
_SINGLE_DAY_HOLIDAY_PIN_PATTERNS: list[tuple[re.Pattern, Callable[[int], date | None]]] = [
    (re.compile(r"\bon\s+(?:the\s+)?thanksgiving(?:\s+day)?\b"), _us_thanksgiving),
    (re.compile(r"\bon\s+(?:the\s+)?(?:christmas|xmas)(?:\s+day)?\b"), lambda y: date(y, 12, 25)),
    (re.compile(r"\bon\s+halloween\b"), lambda y: date(y, 10, 31)),
    (
        re.compile(r"\bon\s+(?:the\s+)?(?:4th|fourth)\s+of\s+july\b|\bon\s+independence\s+day\b"),
        lambda y: date(y, 7, 4),
    ),
    (re.compile(r"\bon\s+new\s+year'?s?\s+(?:day|eve)?\b"), lambda y: date(y, 1, 1)),
    (re.compile(r"\bon\s+valentine'?s(?:\s+day)?\b"), lambda y: date(y, 2, 14)),
    (re.compile(r"\bon\s+veterans?\s+day\b"), lambda y: date(y, 11, 11)),
    (re.compile(r"\bon\s+labor\s+day\b"), lambda y: _nth_weekday_of_month(y, 9, 0, 1)),
    (re.compile(r"\bon\s+memorial\s+day\b"), lambda y: _last_weekday_of_month(y, 5, 0)),
    (re.compile(r"\bon\s+(?:mlk|martin\s+luther\s+king)(?:\s+day)?\b"), lambda y: _nth_weekday_of_month(y, 1, 0, 3)),
    (re.compile(r"\bon\s+presidents?\s+day\b"), lambda y: _nth_weekday_of_month(y, 2, 0, 3)),
    (re.compile(r"\bon\s+easter(?:\s+sunday)?\b"), _western_easter_sunday),
    (re.compile(r"\bon\s+diwali\b"), _diwali_date),
    (re.compile(r"\bon\s+(?:lunar\s+new\s+year|chinese\s+new\s+year)\b"), _lunar_new_year_date),
    (re.compile(r"\bon\s+eid\s+al[\s-]?adha\b"), _eid_al_adha_date),
    (re.compile(r"\bon\s+eid(?:\s+al[\s-]?fitr)?\b"), _eid_al_fitr_date),
    (re.compile(r"\bon\s+hanukkah\b"), _hanukkah_start_date),
    (re.compile(r"\bon\s+bastille\s+day\b"), lambda y: date(y, 7, 14)),
    (re.compile(r"\bon\s+canada\s+day\b"), lambda y: date(y, 7, 1)),
    (re.compile(r"\bon\s+australia\s+day\b"), lambda y: date(y, 1, 26)),
    (re.compile(r"\bon\s+anzac\s+day\b"), lambda y: date(y, 4, 25)),
    (re.compile(r"\bon\s+cinco\s+de\s+mayo\b"), lambda y: date(y, 5, 5)),
]


def _explicit_holiday_day_pin_date(user_text: str, *, anchor: date) -> date | None:
    txt = (user_text or "").strip().lower()
    if not txt:
        return None
    for pattern, date_fn in _SINGLE_DAY_HOLIDAY_PIN_PATTERNS:
        if not pattern.search(txt):
            continue
        for y in range(anchor.year, anchor.year + 5):
            try:
                candidate = date_fn(y)
            except Exception:
                candidate = None
            if candidate and candidate >= anchor:
                return candidate
        return None
    return None


def _finalize_holiday_pair(
    pair: tuple[str, str] | None, user_text: str, anchor: date
) -> tuple[str, str] | None:
    """
    Applies explicit user overrides on top of a default holiday round-trip
    window: pinning departure to the holiday's actual date ("on Thanksgiving
    day") and/or an explicit return offset ("come back the next day", "same
    day return", "back in 3 days"). Without these, the generic default window
    is returned unchanged.
    """
    if not pair:
        return pair

    dep_date = _to_date(pair[0])
    ret_date = _to_date(pair[1])

    pinned = _explicit_holiday_day_pin_date(user_text, anchor=anchor)
    if pinned and pinned != dep_date:
        shift_days = (pinned - dep_date).days
        dep_date = pinned
        ret_date = ret_date + timedelta(days=shift_days)

    return_offset = _extract_ai_relative_return_offset_days(user_text)
    if return_offset is not None:
        ret_date = dep_date + timedelta(days=max(0, return_offset))

    if ret_date < dep_date:
        ret_date = dep_date

    return dep_date.isoformat(), ret_date.isoformat()


def _next_round_trip_window(anchor: date, build: Callable[[int], tuple[date, date] | None]) -> tuple[str, str] | None:
    """
    build(year) -> (depart, return) for that calendar year's primary occurrence,
    or None if that year isn't covered (e.g. a lookup table doesn't extend that
    far). Picks the first future window (departure may snap to anchor if we
    are already inside it).
    """
    for y in range(anchor.year, anchor.year + 6):
        built = build(y)
        if built is None:
            continue
        d0, d1 = built
        if d0 > d1:
            d0, d1 = d1, d0
        if d1 < anchor:
            continue
        if d0 < anchor <= d1:
            d0 = anchor
        return d0.isoformat(), d1.isoformat()
    return None


def _infer_holiday_season_round_trip(
    user_text: str,
    *,
    anchor: date,
    parsed: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    """
    Map common US travel phrases to a default round-trip date pair (editable in the UI).
    Skips if the user asked for one-way or open-jaw.
    """
    txt = (user_text or "").strip().lower()
    if not txt:
        return None
    if re.search(r"\bone[\s-]?way\b", txt):
        return None
    if re.search(r"\bopen[\s-]?jaw\b", txt):
        return None
    if parsed and parsed.get("trip_type") == "oneway":
        return None
    origin_country = _place_country_from_code(parsed.get("origin")) if parsed else None
    if not origin_country:
        inferred_origin, _ = _extract_route_pair_from_text(user_text)
        origin_country = _place_country_from_code(inferred_origin)
    origin_country = (origin_country or "").strip().upper()
    uk_profile_countries = {"GB", "IE"}
    eu_profile_countries = {
        "FR", "DE", "ES", "IT", "PT", "NL", "BE", "CH", "AT",
        "SE", "NO", "DK", "FI", "PL", "CZ", "HU", "GR", "RO", "BG", "HR",
        "SI", "SK", "LT", "LV", "EE", "LU", "MT", "CY",
    }
    eu_uk_profile_countries = {
        "GB", "IE", "FR", "DE", "ES", "IT", "PT", "NL", "BE", "CH", "AT",
        "SE", "NO", "DK", "FI", "PL", "CZ", "HU", "GR", "RO", "BG", "HR",
        "SI", "SK", "LT", "LV", "EE", "LU", "MT", "CY",
    }
    use_eu_profile = origin_country in eu_uk_profile_countries
    use_uk_profile = origin_country in uk_profile_countries
    use_continental_eu_profile = origin_country in eu_profile_countries

    def tg_rt(y: int) -> tuple[date, date]:
        tg = _us_thanksgiving(y)
        depart = tg - timedelta(days=1)
        ret = tg + timedelta(days=3)
        return depart, ret

    def xmas_rt(y: int) -> tuple[date, date]:
        return date(y, 12, 22), date(y, 12, 27)

    def spring_break_rt(y: int) -> tuple[date, date]:
        return date(y, 3, 15), date(y, 3, 22)
    
    def uk_spring_break_rt(y: int) -> tuple[date, date]:
        return date(y, 4, 1), date(y, 4, 8)

    def eu_spring_break_rt(y: int) -> tuple[date, date]:
        return date(y, 4, 5), date(y, 4, 12)

    def easter_rt(y: int) -> tuple[date, date]:
        e = _western_easter_sunday(y)
        return e - timedelta(days=2), e + timedelta(days=1)

    def summer_rt(y: int) -> tuple[date, date]:
        return date(y, 7, 6), date(y, 7, 13)
    
    def uk_summer_rt(y: int) -> tuple[date, date]:
        return date(y, 7, 25), date(y, 8, 8)

    def eu_summer_rt(y: int) -> tuple[date, date]:
        return date(y, 8, 5), date(y, 8, 19)

    def fall_rt(y: int) -> tuple[date, date]:
        return date(y, 10, 7), date(y, 10, 14)
    
    def uk_fall_rt(y: int) -> tuple[date, date]:
        return date(y, 10, 24), date(y, 10, 31)

    def eu_fall_rt(y: int) -> tuple[date, date]:
        return date(y, 10, 24), date(y, 10, 31)

    def winter_rt(y: int) -> tuple[date, date]:
        return date(y, 12, 20), date(y, 12, 27)

    def spring_generic_rt(y: int) -> tuple[date, date]:
        return date(y, 4, 10), date(y, 4, 17)

    def new_year_rt(y: int) -> tuple[date, date]:
        return date(y, 12, 28), date(y + 1, 1, 2)

    def mlk_weekend_rt(y: int) -> tuple[date, date]:
        mlk = _nth_weekday_of_month(y, 1, 0, 3)  # 3rd Monday Jan
        return mlk - timedelta(days=2), mlk

    def presidents_weekend_rt(y: int) -> tuple[date, date]:
        pres = _nth_weekday_of_month(y, 2, 0, 3)  # 3rd Monday Feb
        return pres - timedelta(days=2), pres

    def memorial_weekend_rt(y: int) -> tuple[date, date]:
        mem = _last_weekday_of_month(y, 5, 0)  # last Monday May
        return mem - timedelta(days=2), mem

    def july4_rt(y: int) -> tuple[date, date]:
        july4 = date(y, 7, 4)
        return july4 - timedelta(days=1), july4 + timedelta(days=2)

    def labor_day_rt(y: int) -> tuple[date, date]:
        labor = _nth_weekday_of_month(y, 9, 0, 1)  # first Monday Sep
        return labor - timedelta(days=2), labor

    def columbus_day_rt(y: int) -> tuple[date, date]:
        columbus = _nth_weekday_of_month(y, 10, 0, 2)  # second Monday Oct
        return columbus - timedelta(days=2), columbus

    def veterans_day_rt(y: int) -> tuple[date, date]:
        vets = date(y, 11, 11)
        return vets - timedelta(days=1), vets + timedelta(days=1)
    
    def may_bank_holiday_rt(y: int) -> tuple[date, date]:
        may_day = _nth_weekday_of_month(y, 5, 0, 1)  # first Monday May
        return may_day - timedelta(days=2), may_day

    def late_august_bank_holiday_rt(y: int) -> tuple[date, date]:
        aug_bank = _last_weekday_of_month(y, 8, 0)  # last Monday Aug
        return aug_bank - timedelta(days=2), aug_bank

    def easter_monday_bank_holiday_rt(y: int) -> tuple[date, date]:
        easter_monday = _western_easter_sunday(y) + timedelta(days=1)
        return easter_monday - timedelta(days=2), easter_monday

    def halloween_rt(y: int) -> tuple[date, date]:
        hw = date(y, 10, 31)
        return hw - timedelta(days=1), hw + timedelta(days=1)

    def valentine_rt(y: int) -> tuple[date, date]:
        val = date(y, 2, 14)
        return val - timedelta(days=1), val + timedelta(days=1)

    def mardi_gras_rt(y: int) -> tuple[date, date]:
        mg = _western_easter_sunday(y) - timedelta(days=47)
        return mg - timedelta(days=2), mg + timedelta(days=1)

    def carnival_rt(y: int) -> tuple[date, date]:
        mg = _western_easter_sunday(y) - timedelta(days=47)
        return mg - timedelta(days=4), mg + timedelta(days=1)

    def semana_santa_rt(y: int) -> tuple[date, date]:
        e = _western_easter_sunday(y)
        return e - timedelta(days=6), e + timedelta(days=1)

    def diwali_rt(y: int) -> tuple[date, date] | None:
        d = _diwali_date(y)
        if d is None:
            return None
        return d - timedelta(days=1), d + timedelta(days=3)

    def lunar_new_year_rt(y: int) -> tuple[date, date] | None:
        d = _lunar_new_year_date(y)
        if d is None:
            return None
        return d - timedelta(days=1), d + timedelta(days=4)

    def eid_al_fitr_rt(y: int) -> tuple[date, date] | None:
        d = _eid_al_fitr_date(y)
        if d is None:
            return None
        return d - timedelta(days=1), d + timedelta(days=3)

    def eid_al_adha_rt(y: int) -> tuple[date, date] | None:
        d = _eid_al_adha_date(y)
        if d is None:
            return None
        return d - timedelta(days=1), d + timedelta(days=3)

    def hanukkah_rt(y: int) -> tuple[date, date] | None:
        d = _hanukkah_start_date(y)
        if d is None:
            return None
        return d - timedelta(days=1), d + timedelta(days=6)

    def golden_week_rt(y: int) -> tuple[date, date]:
        return date(y, 4, 29), date(y, 5, 5)

    def bastille_day_rt(y: int) -> tuple[date, date]:
        d = date(y, 7, 14)
        return d - timedelta(days=1), d + timedelta(days=1)

    def canada_day_rt(y: int) -> tuple[date, date]:
        d = date(y, 7, 1)
        return d - timedelta(days=1), d + timedelta(days=1)

    def australia_day_rt(y: int) -> tuple[date, date]:
        d = date(y, 1, 26)
        return d - timedelta(days=1), d + timedelta(days=1)

    def anzac_day_rt(y: int) -> tuple[date, date]:
        d = date(y, 4, 25)
        return d - timedelta(days=1), d + timedelta(days=1)

    def cinco_de_mayo_rt(y: int) -> tuple[date, date]:
        d = date(y, 5, 5)
        return d - timedelta(days=1), d + timedelta(days=1)

    def day_of_the_dead_rt(y: int) -> tuple[date, date]:
        return date(y, 10, 31), date(y, 11, 3)

    def oktoberfest_rt(y: int) -> tuple[date, date]:
        start = _nth_weekday_of_month(y, 9, 5, 3)  # 3rd Saturday of September (approx. start)
        return start - timedelta(days=1), start + timedelta(days=6)

    def songkran_rt(y: int) -> tuple[date, date]:
        return date(y, 4, 12), date(y, 4, 16)

    if re.search(r"\bthanksgiving\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, tg_rt), user_text, anchor)
    if re.search(r"\bblack\s+friday\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, tg_rt), user_text, anchor)
    if re.search(r"\bboxing\s+day\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, xmas_rt), user_text, anchor)
    if re.search(r"\b(christmas|xmas)\b", txt) or re.search(r"\bholiday\s+season\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, xmas_rt), user_text, anchor)
    if re.search(r"\b(?:lunar\s+new\s+year|chinese\s+new\s+year)\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, lunar_new_year_rt), user_text, anchor)
    if re.search(r"\bnew\s+year'?s?\b", txt) or re.search(r"\bnew\s+year\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, new_year_rt), user_text, anchor)
    if re.search(r"\bdiwali\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, diwali_rt), user_text, anchor)
    if re.search(r"\beid\s+al[\s-]?adha\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, eid_al_adha_rt), user_text, anchor)
    if re.search(r"\beid(?:\s+al[\s-]?fitr)?\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, eid_al_fitr_rt), user_text, anchor)
    if re.search(r"\bhanukkah\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, hanukkah_rt), user_text, anchor)
    if re.search(r"\bgolden\s+week\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, golden_week_rt), user_text, anchor)
    if re.search(r"\bbastille\s+day\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, bastille_day_rt), user_text, anchor)
    if re.search(r"\bcanada\s+day\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, canada_day_rt), user_text, anchor)
    if re.search(r"\baustralia\s+day\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, australia_day_rt), user_text, anchor)
    if re.search(r"\banzac\s+day\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, anzac_day_rt), user_text, anchor)
    if re.search(r"\bcinco\s+de\s+mayo\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, cinco_de_mayo_rt), user_text, anchor)
    if re.search(r"\b(?:day\s+of\s+the\s+dead|d[ií]a\s+de\s+(?:los\s+)?muertos)\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, day_of_the_dead_rt), user_text, anchor)
    if re.search(r"\boktoberfest\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, oktoberfest_rt), user_text, anchor)
    if re.search(r"\bsongkran\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, songkran_rt), user_text, anchor)
    if re.search(r"\b(?:rio\s+)?carnival\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, carnival_rt), user_text, anchor)
    if re.search(r"\bsemana\s+santa\b|\bholy\s+week\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, semana_santa_rt), user_text, anchor)
    if re.search(r"\bbank\s+holiday\b", txt):
        if re.search(r"\beaster\b", txt) and use_uk_profile:
            return _finalize_holiday_pair(_next_round_trip_window(anchor, easter_monday_bank_holiday_rt), user_text, anchor)
        if re.search(r"\baug(?:ust)?\b", txt):
            return _finalize_holiday_pair(_next_round_trip_window(anchor, late_august_bank_holiday_rt), user_text, anchor)
        return _finalize_holiday_pair(_next_round_trip_window(anchor, may_bank_holiday_rt), user_text, anchor)
    if re.search(r"\bspring\s+break\b", txt) or re.search(r"\bspringbreak\b", txt):
        if use_uk_profile:
            return _finalize_holiday_pair(_next_round_trip_window(anchor, uk_spring_break_rt), user_text, anchor)
        if use_continental_eu_profile:
            return _finalize_holiday_pair(_next_round_trip_window(anchor, eu_spring_break_rt), user_text, anchor)
        return _finalize_holiday_pair(_next_round_trip_window(anchor, spring_break_rt), user_text, anchor)
    if re.search(r"\bhalf\s*term\b", txt):
        if re.search(r"\boct(?:ober)?\b|\bautumn\b|\bfall\b", txt):
            return _finalize_holiday_pair(_next_round_trip_window(anchor, eu_fall_rt if use_eu_profile else fall_rt), user_text, anchor)
        if re.search(r"\bmay\b|\bspring\b", txt):
            return _finalize_holiday_pair(_next_round_trip_window(anchor, may_bank_holiday_rt if use_eu_profile else spring_generic_rt), user_text, anchor)
        return _finalize_holiday_pair(_next_round_trip_window(anchor, eu_fall_rt if use_eu_profile else fall_rt), user_text, anchor)
    if re.search(r"\bmlk\b", txt) or re.search(r"\bmartin\s+luther\s+king\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, mlk_weekend_rt), user_text, anchor)
    if re.search(r"\bpresidents?\s+day\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, presidents_weekend_rt), user_text, anchor)
    if re.search(r"\bvalentine'?s\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, valentine_rt), user_text, anchor)
    if re.search(r"\bmardi\s+gras\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, mardi_gras_rt), user_text, anchor)
    if re.search(r"\beaster\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, easter_rt), user_text, anchor)
    if re.search(r"\bmemorial\s+day\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, memorial_weekend_rt), user_text, anchor)
    if re.search(r"\b(4th\s+of\s+july|fourth\s+of\s+july|independence\s+day)\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, july4_rt), user_text, anchor)
    if re.search(r"\blabor\s+day\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, labor_day_rt), user_text, anchor)
    if re.search(r"\b(columbus\s+day|indigenous\s+peoples'?(\s+day)?)\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, columbus_day_rt), user_text, anchor)
    if re.search(r"\bhalloween\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, halloween_rt), user_text, anchor)
    if re.search(r"\bveterans?\s+day\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, veterans_day_rt), user_text, anchor)
    if re.search(r"\bsummer\b", txt):
        if use_uk_profile:
            return _finalize_holiday_pair(_next_round_trip_window(anchor, uk_summer_rt), user_text, anchor)
        if use_continental_eu_profile:
            return _finalize_holiday_pair(_next_round_trip_window(anchor, eu_summer_rt), user_text, anchor)
        return _finalize_holiday_pair(_next_round_trip_window(anchor, summer_rt), user_text, anchor)
    if re.search(r"\bsummer\s+holiday\b", txt) or re.search(r"\bschool\s+holiday\b", txt):
        if use_uk_profile:
            return _finalize_holiday_pair(_next_round_trip_window(anchor, uk_summer_rt), user_text, anchor)
        if use_continental_eu_profile:
            return _finalize_holiday_pair(_next_round_trip_window(anchor, eu_summer_rt), user_text, anchor)
        return _finalize_holiday_pair(_next_round_trip_window(anchor, summer_rt), user_text, anchor)
    if re.search(r"\b(fall|autumn)\b", txt):
        if use_uk_profile:
            return _finalize_holiday_pair(_next_round_trip_window(anchor, uk_fall_rt), user_text, anchor)
        if use_continental_eu_profile:
            return _finalize_holiday_pair(_next_round_trip_window(anchor, eu_fall_rt), user_text, anchor)
        return _finalize_holiday_pair(_next_round_trip_window(anchor, fall_rt), user_text, anchor)
    if re.search(r"\bwinter\b", txt) and not re.search(r"\bchristmas\b", txt):
        return _finalize_holiday_pair(_next_round_trip_window(anchor, winter_rt), user_text, anchor)
    if re.search(r"\bspring\b", txt):
        if use_uk_profile:
            return _finalize_holiday_pair(_next_round_trip_window(anchor, uk_spring_break_rt), user_text, anchor)
        if use_continental_eu_profile:
            return _finalize_holiday_pair(_next_round_trip_window(anchor, eu_spring_break_rt), user_text, anchor)
        return _finalize_holiday_pair(_next_round_trip_window(anchor, spring_generic_rt), user_text, anchor)

    return None


def _clean_route_fragment(value: str) -> str:
    cleaned = (value or "").strip()
    cleaned = re.sub(r"[;|]", " ", cleaned)
    cleaned = re.sub(r"\b(on|for|in|during|around|about|from|to|departing|leaving|returning|trip|flight|flights)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    return cleaned

def _extract_route_pair_from_text(user_text: str) -> tuple[str | None, str | None]:
    txt = (user_text or "").strip()
    if not txt:
        return None, None

    patterns = [
        r"\bfrom\s+([A-Za-z][A-Za-z\s.'-]{1,40}?)\s+to\s+([A-Za-z][A-Za-z\s.'-]{1,40})(?:\b|,|$)",
        r"\b([A-Za-z][A-Za-z\s.'-]{1,40}?)\s+to\s+([A-Za-z][A-Za-z\s.'-]{1,40})(?:\b|,|$)",
    ]
    for pat in patterns:
        m = re.search(pat, txt, flags=re.IGNORECASE)
        if not m:
            continue
        origin_text = _clean_route_fragment(m.group(1))
        destination_text = _clean_route_fragment(m.group(2))
        origin_code = _normalize_airport_input(origin_text) if origin_text else None
        destination_code = _normalize_airport_input(destination_text) if destination_text else None
        if origin_code or destination_code:
            return origin_code, destination_code
    return None, None


def _extract_route_chain_from_text(user_text: str) -> list[str]:
    txt = (user_text or "").strip()
    if not txt:
        return []
    normalized = re.sub(r"\s*(?:->|→|=>)\s*", " to ", txt, flags=re.IGNORECASE)
    normalized = re.sub(r"\bthen\b", " to ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s*[,;/]\s*", " to ", normalized)
    if " to " not in normalized.lower():
        return []
    parts = re.split(r"\bto\b", normalized, flags=re.IGNORECASE)
    if len(parts) < 3:
        return []
    codes: list[str] = []
    for part in parts:
        cleaned = _clean_route_fragment(part)
        if not cleaned:
            continue
        code = _normalize_airport_input(cleaned)
        if not code:
            continue
        if not codes or codes[-1] != code:
            codes.append(code)
    return codes if len(codes) >= 3 else []

_TRAVELER_COMPANION_TERMS: dict[str, str] = {
    "mom": "mom", "mother": "mom", "dad": "dad", "father": "dad",
    "grandma": "grandma", "grandmother": "grandma", "grandpa": "grandpa", "grandfather": "grandpa",
    "wife": "wife", "husband": "husband", "spouse": "spouse", "partner": "partner",
    "kids": "kids", "kid": "kid", "children": "children", "child": "child",
    "baby": "baby", "infant": "infant", "toddler": "toddler",
    "son": "son", "daughter": "daughter", "parents": "parents",
    "girlfriend": "girlfriend", "boyfriend": "boyfriend",
    "fiancee": "fiancee", "fiance": "fiance",
}
_SENIOR_OR_CHILD_COMPANION_LABELS = {
    "mom", "dad", "grandma", "grandpa", "kids", "kid", "children", "child",
    "baby", "infant", "toddler", "parents",
}


def _extract_ai_traveler_context(user_text: str) -> dict[str, Any]:
    """
    Detect mentions of travel companions ("my mom", "with my kids") and comfort
    preferences ("longer layover", "quick connection") that the sort/ranking
    and result explanations should reflect. This never changes passenger
    counts by itself — that stays under the LLM's explicit `passengers` field
    since it directly affects how many seats get booked. This only powers
    softer ranking nudges and "why this is the top pick" copy.
    """
    txt = (user_text or "").strip().lower()
    context: dict[str, Any] = {
        "companion_labels": [],
        "has_senior_or_child_companion": False,
        "prefers_longer_layover": False,
        "prefers_shorter_layover": False,
    }
    if not txt:
        return context

    found_labels: list[str] = []
    for term, label in _TRAVELER_COMPANION_TERMS.items():
        if re.search(rf"\b(?:my|our)\s+{re.escape(term)}\b", txt) or re.search(
            rf"\b(?:with|and)\s+(?:my|our)\s+{re.escape(term)}\b", txt
        ):
            if label not in found_labels:
                found_labels.append(label)

    if re.search(r"\belderly\b", txt) or re.search(r"\bsenior\s+citizen\b", txt) or re.search(r"\bwheelchair\b", txt):
        if "elderly/assistance" not in found_labels:
            found_labels.append("elderly/assistance")

    context["companion_labels"] = found_labels
    context["has_senior_or_child_companion"] = bool(
        found_labels and (set(found_labels) & _SENIOR_OR_CHILD_COMPANION_LABELS or "elderly/assistance" in found_labels)
    )

    if re.search(r"\b(?:long|longer|extra|extended)\s+(?:layover|stopover|connection)\b", txt) or re.search(
        r"\bstopover\s+in\b", txt
    ) or re.search(r"\btime\s+to\s+explore\b", txt):
        context["prefers_longer_layover"] = True
    if re.search(r"\b(?:short|shorter|quick|minimal|tight)\s+(?:layover|stopover|connection)\b", txt) or re.search(
        r"\bavoid\s+long\s+layovers?\b", txt
    ):
        context["prefers_shorter_layover"] = True

    return context


_STAY_INTENT_TERMS = (
    "hotel", "hotels", "stay", "stays", "staying", "accommodation", "accomodation",
    "resort", "hostel", "airbnb", "room", "rooms", "suite", "motel", "lodge",
    "guesthouse", "guest house", "villa", "apartment", "place to stay", "night in",
    "nights in", "check in", "check-in", "checkin",
)
_FLIGHT_INTENT_TERMS = (
    "flight", "flights", "fly", "flying", "airline", "airfare", "fare", "plane",
    "nonstop", "non-stop", "layover", "one way", "one-way", "round trip",
    "round-trip", "business class", "economy", "depart", "landing",
)


def detect_search_intent(user_text: str) -> str:
    """Route a natural-language query to the flights, stays, or both pipeline.

    Cheap keyword pass first — it settles the overwhelming majority of queries
    without a model round trip. Only genuinely ambiguous text goes to Gemini.
    """
    txt = (user_text or "").strip().lower()
    if not txt:
        return "flights"

    stay_hits = sum(1 for term in _STAY_INTENT_TERMS if re.search(rf"\b{re.escape(term)}\b", txt))
    flight_hits = sum(1 for term in _FLIGHT_INTENT_TERMS if re.search(rf"\b{re.escape(term)}\b", txt))

    if stay_hits and not flight_hits:
        # "New York to Miami ... and a hotel with free cancellation" never
        # says "flight" or "fly" — naming two real places joined by "to" is
        # itself a flight signal worth catching before falling back to
        # stays-only and quietly dropping the trip.
        origin_code, destination_code = _extract_route_pair_from_text(user_text)
        if origin_code or destination_code:
            return "both"
        return "stays"
    if flight_hits and not stay_hits:
        return "flights"
    if stay_hits and flight_hits:
        # Mentions both ("flight to Paris and a hotel") — the flight is the
        # trip's spine, but the hotel intent is real and gets its own pass.
        return "both"

    if not model:
        return "flights"

    prompt = (
        'Classify this travel search as exactly one word: "flights", "stays", or "both". '
        'Choose "stays" only if the user is looking for somewhere to sleep '
        '(hotel, resort, apartment) with no flight mentioned. Choose "both" if the '
        "user wants a flight AND a place to stay in the same request (e.g. "
        '"business class to Tokyo next month, nonstop, and a 5-star hotel with a pool"). '
        'Otherwise choose "flights". '
        f'Reply with the single word only.\n\nQuery: """{user_text}"""'
    )
    try:
        raw = (getattr(model.generate_content(prompt), "text", "") or "").strip().lower()
    except Exception as exc:
        print("INTENT DETECT ERROR:", repr(exc))
        return "flights"
    if "both" in raw:
        return "both"
    return "stays" if "stay" in raw else "flights"


def parse_ai_stay_request(user_text: str) -> dict | None:
    """Natural language -> hotel search parameters."""
    if not model or not user_text:
        return None

    today = date.today().isoformat()
    prompt = f"""
You are a hotel search assistant.

Convert the user's request into valid JSON with these fields:
- destination (city, area, or landmark name as plain text, e.g. "Dubai", "Paris", "Bali")
- checkin (YYYY-MM-DD or null)
- checkout (YYYY-MM-DD or null)
- nights (integer or null — use when the user says a duration but no dates)
- adults (integer, default 2)
- children_ages (array of integers, empty when none mentioned)
- rooms (integer, default 1)
- min_stars (integer 1-5 or null — e.g. "5 star hotel" -> 5, "at least 4 stars" -> 4)
- min_rating (number 1-10 or null — e.g. "well reviewed" -> 8, "highly rated" -> 8.5)
- max_price_per_night (number or null, in USD)
- free_cancellation (true/false — true when the user wants refundable/flexible)
- breakfast (true/false — true when the user wants breakfast included)
- amenities (array from: Free WiFi, Pool, Breakfast, Parking, Gym, Spa, Restaurant, Airport shuttle, Air conditioning, Pet friendly, Family rooms, Bar)
- sort (recommended, price_low, price_high, rating, stars)

Rules:
- Use null when information is missing
- Dates must be ISO format (YYYY-MM-DD)
- If a date has no year, assume the next future occurrence
- If the user gives a duration but no dates (e.g. "3 nights in Rome"), set nights and leave checkin/checkout null
- Count every guest implied: "me and my wife" is 2 adults, "family of four" is 4
- Children are people under 18 — put their ages in children_ages and exclude them from adults
- "cheap"/"budget" -> sort "price_low"; "best"/"nicest"/"top rated" -> sort "rating"; "luxury" -> min_stars 5
- Today is {today}
- Only return JSON

User request:
\"\"\"{user_text}\"\"\"
"""
    try:
        response = model.generate_content(prompt, json_mode=True)
        text = (getattr(response, "text", "") or "").strip()
        if text.startswith("```"):
            text = text.strip().strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        parsed = json.loads(text)
    except Exception as exc:
        print("AI STAY PARSE ERROR:", repr(exc))
        return None

    return _normalize_stay_parse(parsed, user_text)


def _normalize_stay_parse(parsed: dict, user_text: str) -> dict | None:
    """Raw Gemini stay JSON -> typed/defaulted hotel search params.

    Shared by parse_ai_stay_request and parse_ai_combined_request so the two
    entry points can't drift on date-defaulting/traveler-counting rules.
    """
    destination = str(parsed.get("destination") or "").strip()
    if not destination:
        return None

    checkin = str(parsed.get("checkin") or "").strip()
    checkout = str(parsed.get("checkout") or "").strip()

    # Fill in dates the model left open so the user always lands on real results.
    try:
        nights = int(parsed.get("nights") or 0)
    except (TypeError, ValueError):
        nights = 0
    if not checkin:
        checkin = (date.today() + timedelta(days=30)).isoformat()
    if not checkout:
        span = nights if nights > 0 else 3
        try:
            checkout = (datetime.strptime(checkin, "%Y-%m-%d").date() + timedelta(days=span)).isoformat()
        except ValueError:
            checkin = (date.today() + timedelta(days=30)).isoformat()
            checkout = (date.today() + timedelta(days=30 + span)).isoformat()

    def _int(value, default, lo, hi):
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return default

    ages = []
    for age in parsed.get("children_ages") or []:
        try:
            ages.append(max(0, min(17, int(age))))
        except (TypeError, ValueError):
            continue

    sort = str(parsed.get("sort") or "recommended").strip().lower()
    if sort not in {"recommended", "price_low", "price_high", "rating", "stars"}:
        sort = "recommended"

    return {
        "destination": destination,
        "checkin": checkin,
        "checkout": checkout,
        "adults": _int(parsed.get("adults"), 2, 1, 8),
        "children_ages": ages[:4],
        "rooms": _int(parsed.get("rooms"), 1, 1, 4),
        "min_stars": _int(parsed.get("min_stars"), 0, 0, 5) or None,
        "min_rating": _money_or_none(parsed.get("min_rating")),
        "max_price_per_night": _money_or_none(parsed.get("max_price_per_night")),
        "free_cancellation": bool(parsed.get("free_cancellation")),
        "breakfast": bool(parsed.get("breakfast")),
        "amenities": [str(a).strip() for a in (parsed.get("amenities") or []) if str(a).strip()][:6],
        "sort": sort,
        "raw_text": user_text,
    }


def _money_or_none(value: Any) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def parse_ai_flight_request(user_text: str) -> dict | None:
    if not model or not user_text:
        return None

    today = date.today().isoformat()
    prompt = f"""
You are a flight search assistant.

Convert the user's request into valid JSON with these fields:
- origin (IATA code or null)
- destination (IATA code or null)
- depart_date (YYYY-MM-DD or null)
- return_date (YYYY-MM-DD or null)
- legs (array of objects: origin, destination, depart_date) for multi-city requests, otherwise null
- trip_type (oneway, roundtrip, multicity, or null)
- passengers (integer, default 1)
- cabin (ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST)
- nonstop (true/false)
- max_price (number or null)
- sort (cheapest, fastest, recommended, earliest_departure, earliest_arrival, fewest_stops)
  • Use "earliest_departure" when user wants the first/earliest departing flight of the day (e.g. "earliest flight", "first flight out", "morning flight", "leave as early as possible")
  • Use "earliest_arrival" when user wants to arrive as early as possible (e.g. "get there earliest", "arrive before noon")
  • Use "fewest_stops" when user prioritizes directness (e.g. "most direct", "fewest connections", "avoid layovers")
  • Use "cheapest" when user prioritizes price (e.g. "cheapest", "lowest price", "best deal", "budget")
  • Use "fastest" when user prioritizes speed (e.g. "fastest", "shortest trip", "least travel time")
  • Use "recommended" as the default when no clear preference is stated

Rules:
- Use null if information is missing
- If a city is mentioned, infer the main airport (e.g., Dhaka -> DAC)
- Dates must be ISO format (YYYY-MM-DD)
- If the user gives a date without a year, assume the next future occurrence
- If the user mentions holiday/season timing without exact calendar dates, set depart_date and return_date to null; the server applies default round-trip windows the user can edit. This includes (but is not limited to): Thanksgiving, Black Friday, Christmas, Boxing Day, New Year's, spring break, Easter, Semana Santa/Holy Week, Mardi Gras, Carnival, Memorial Day, 4th of July/Independence Day, Labor Day, Columbus Day, Halloween, Day of the Dead, Veterans Day, MLK Day, Presidents Day, Valentine's Day, summer/fall/winter/spring, Diwali, Lunar New Year/Chinese New Year, Eid al-Fitr, Eid al-Adha, Hanukkah, Golden Week, Bastille Day, Canada Day, Australia Day, ANZAC Day, Cinco de Mayo, Oktoberfest, and Songkran.
- If the user gives an explicit relative return instruction (e.g. "come back the next day", "same day return", "back in 3 days"), still set return_date to null when paired with a holiday/season name — the server resolves the exact combination of holiday date + relative return instruction.
- Count every traveler implied by the request into `passengers`, not just the speaker: "me and my mom" or "my wife and I" is 2, "my family of four" is 4, "traveling with my two kids" (plus the speaker) is 3, etc. Only default to 1 when no companions are mentioned.
- For multi-city requests, include legs in order.
- Today is {today}
- Only return JSON

User request:
\"\"\"{user_text}\"\"\"
"""
    try:
        response = model.generate_content(prompt, json_mode=True)
        raw = (getattr(response, "text", "") or "").strip()
        text = raw
        if text.startswith("```"):
            text = text.strip().strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        parsed = json.loads(text)
        return _normalize_flight_parse(parsed, user_text)
    except Exception as e:
        print("JSON PARSE ERROR:", e)
        return None


def _normalize_flight_parse(parsed: dict, user_text: str) -> dict:
    """Raw Gemini flight JSON -> typed/defaulted flight search params.

    Shared by parse_ai_flight_request and parse_ai_combined_request so the
    two entry points can't drift on date-defaulting/traveler-counting rules.
    Callers are responsible for catching exceptions this raises.
    """
    parsed["origin"] = _normalize_airport_input(parsed.get("origin")) if parsed.get("origin") else None
    parsed["destination"] = _normalize_airport_input(parsed.get("destination")) if parsed.get("destination") else None
    parsed["passengers"] = int(parsed.get("passengers") or 1)
    parsed["cabin"] = parsed.get("cabin") or "ECONOMY"
    parsed["nonstop"] = bool(parsed.get("nonstop") or False)
    parsed["sort"] = parsed.get("sort") or "recommended"
    fallback_trip_type = "roundtrip" if parsed.get("return_date") else "oneway"
    parsed["trip_type"] = _coerce_trip_type(parsed.get("trip_type"), fallback=fallback_trip_type)
    parsed.setdefault("raw_text", user_text)

    raw_legs = parsed.get("legs")
    normalized_legs: list[dict[str, str]] = []
    if isinstance(raw_legs, list):
        for idx, leg in enumerate(raw_legs):
            if not isinstance(leg, Mapping):
                continue
            o = _normalize_airport_input(leg.get("origin")) if leg.get("origin") else None
            d = _normalize_airport_input(leg.get("destination")) if leg.get("destination") else None
            dep = str(leg.get("depart_date") or "").strip()
            if not dep:
                dep = (date.today() + timedelta(days=(idx * 3) + 7)).isoformat()
            if o and d and _is_valid_iso_date(dep):
                normalized_legs.append({"origin": o, "destination": d, "depart_date": dep})
    if not normalized_legs:
        route_chain = _extract_route_chain_from_text(user_text)
        if route_chain:
            for idx in range(len(route_chain) - 1):
                normalized_legs.append(
                    {
                        "origin": route_chain[idx],
                        "destination": route_chain[idx + 1],
                        "depart_date": (date.today() + timedelta(days=(idx * 3) + 7)).isoformat(),
                    }
                )
    if len(normalized_legs) >= 2:
        parsed["trip_type"] = "multicity"
        parsed["legs"] = normalized_legs

    inferred_origin, inferred_destination = _extract_route_pair_from_text(user_text)
    if not parsed.get("origin") and inferred_origin:
        parsed["origin"] = inferred_origin
    if not parsed.get("destination") and inferred_destination:
        parsed["destination"] = inferred_destination

    if parsed.get("trip_type") == "multicity" and parsed.get("legs"):
        parsed["origin"] = parsed["legs"][0]["origin"]
        parsed["destination"] = parsed["legs"][-1]["destination"]
        parsed["depart_date"] = parsed["legs"][0]["depart_date"]
        parsed["return_date"] = None
        parsed.pop("search_mode", None)
        parsed.pop("flex_month", None)
        parsed.pop("trip_length_days", None)
        parsed["combination_mode"] = "auto"
        return parsed

    if not parsed.get("depart_date"):
        parsed["depart_date"] = _extract_ai_relative_depart_date(user_text)

    parsed = _clear_inferred_ai_dates_for_month_only_request(parsed, user_text)

    trip_type = _extract_ai_trip_type(user_text, parsed)
    if trip_type:
        parsed["trip_type"] = trip_type

    holiday_rt = _infer_holiday_season_round_trip(user_text, anchor=date.today(), parsed=parsed)
    holiday_dates_applied = False
    already_has_explicit_dates = (
        _is_valid_iso_date(parsed.get("depart_date"))
        and _is_valid_iso_date(parsed.get("return_date"))
        and _user_text_has_explicit_day_precision(user_text)
    )
    if holiday_rt and not already_has_explicit_dates:
        dep_iso, ret_iso = holiday_rt
        parsed["depart_date"] = dep_iso
        parsed["return_date"] = ret_iso
        parsed["trip_type"] = "roundtrip"
        holiday_dates_applied = True

    combination_mode = _extract_ai_combination_mode(user_text)
    if combination_mode:
        parsed["combination_mode"] = combination_mode

    traveler_context = _extract_ai_traveler_context(user_text)
    if traveler_context.get("companion_labels") or traveler_context.get("prefers_longer_layover") or traveler_context.get("prefers_shorter_layover"):
        parsed["traveler_context"] = traveler_context

    if _looks_like_ai_flex_request(user_text, parsed) and not holiday_dates_applied:
        parsed["search_mode"] = "flex"
        parsed["trip_type"] = parsed.get("trip_type") or "roundtrip"
        parsed["flex_month"] = _extract_ai_flex_month(user_text)
        parsed["sort"] = "cheapest"
        parsed["depart_date"] = None
        parsed["return_date"] = None
        if parsed["trip_type"] == "oneway":
            parsed.pop("trip_length_days", None)
        else:
            parsed["trip_length_days"] = _extract_ai_trip_length_days(user_text) or 7

    return parsed


def parse_ai_combined_request(user_text: str) -> dict | None:
    """Natural language -> flight params, and hotel params when the same
    request also asks for a place to stay ("flight to Paris and a hotel").

    Returns {"flight": {...}, "stay": {...} | None, "wants_hotel": bool}, or
    None if the flight side couldn't be parsed at all (callers should fall
    back to parse_ai_flight_request in that case). The flight is always the
    trip's spine; the stay is best-effort and dropped rather than guessed at
    if Gemini's stay block comes back empty or destination-less.
    """
    if not model or not user_text:
        return None

    today = date.today().isoformat()
    prompt = f"""
You are a combined flight + hotel trip-planning assistant. The user wants a
flight AND a place to stay in one request.

Convert the user's request into valid JSON with these top-level fields:
- flight: object with fields
  - origin (IATA code or null), destination (IATA code or null)
  - depart_date (YYYY-MM-DD or null), return_date (YYYY-MM-DD or null)
  - legs (array of objects: origin, destination, depart_date) for multi-city, otherwise null
  - trip_type (oneway, roundtrip, multicity, or null)
  - passengers (integer, default 1), cabin (ECONOMY, PREMIUM_ECONOMY, BUSINESS, FIRST)
  - nonstop (true/false), max_price (number or null)
  - sort (cheapest, fastest, recommended, earliest_departure, earliest_arrival, fewest_stops)
- stay: object with fields
  - destination (city/area/landmark as plain text, or null if no hotel was requested)
  - checkin (YYYY-MM-DD or null), checkout (YYYY-MM-DD or null)
  - nights (integer or null — use when a duration is given but no dates)
  - adults (integer, default 2), children_ages (array of integers)
  - rooms (integer, default 1)
  - min_stars (integer 1-5 or null), min_rating (number 1-10 or null)
  - max_price_per_night (number or null, USD)
  - free_cancellation (true/false), breakfast (true/false)
  - amenities (array from: Free WiFi, Pool, Breakfast, Parking, Gym, Spa, Restaurant, Airport shuttle, Air conditioning, Pet friendly, Family rooms, Bar)
  - sort (recommended, price_low, price_high, rating, stars)
- stay_dates_explicit: true only if the user gave the hotel its own dates or
  duration separate from the flight (e.g. "for 4 nights", "check in the 3rd");
  false if the hotel should simply span the flight's travel dates.

Rules:
- Use null when information is missing. If the request doesn't actually ask
  for a hotel/stay at all, set the entire "stay" object's fields to null.
- If a city is mentioned for the flight, infer the main airport (e.g., Dhaka -> DAC)
- Dates must be ISO format (YYYY-MM-DD). If a date has no year, assume the next future occurrence.
- If the user mentions holiday/season timing without exact calendar dates for the
  flight, set flight.depart_date and flight.return_date to null; the server applies
  default windows. This includes (but is not limited to): Thanksgiving, Christmas,
  New Year's, spring break, Easter, summer/fall/winter/spring, and similar.
- Count every traveler implied by the request into flight.passengers and stay.adults,
  not just the speaker: "me and my wife" is 2, "family of four" is 4.
- Children are people under 18 — put their ages in stay.children_ages and exclude them from stay.adults.
- "cheap"/"budget" -> stay.sort "price_low"; "best"/"nicest"/"top rated" -> stay.sort "rating"; "luxury" -> stay.min_stars 5
- For multi-city flight requests, include flight.legs in order.
- Today is {today}
- Only return JSON

User request:
\"\"\"{user_text}\"\"\"
"""
    parsed = None
    last_exc: Exception | None = None
    # The combined schema is the largest of the three prompts, and even in
    # JSON mode an LLM occasionally emits a syntax slip (stray comma, an
    # unescaped quote). One retry turns a ~10% residual failure rate into
    # roughly 1% rather than dropping the hotel half of the request.
    for attempt in range(2):
        try:
            response = model.generate_content(prompt, json_mode=True)
            text = (getattr(response, "text", "") or "").strip()
            if text.startswith("```"):
                text = text.strip().strip("`").strip()
                if text.lower().startswith("json"):
                    text = text[4:].strip()
            parsed = json.loads(text)
            break
        except Exception as exc:
            last_exc = exc
    if parsed is None:
        print("AI COMBINED PARSE ERROR:", repr(last_exc))
        return None

    flight_raw = parsed.get("flight") if isinstance(parsed.get("flight"), Mapping) else {}
    try:
        flight = _normalize_flight_parse(dict(flight_raw), user_text)
    except Exception as exc:
        # Flight is the trip's spine — if it can't be normalized, bail
        # entirely rather than return a hotel-only result under a combined
        # shape the rest of the pipeline doesn't expect from this function.
        print("AI COMBINED PARSE ERROR (flight normalize):", repr(exc))
        return None

    stay_raw = parsed.get("stay") if isinstance(parsed.get("stay"), Mapping) else {}
    stay: dict | None = None
    if str(stay_raw.get("destination") or "").strip():
        try:
            stay = _normalize_stay_parse(dict(stay_raw), user_text)
        except Exception as exc:
            print("AI COMBINED PARSE ERROR (stay normalize):", repr(exc))
            stay = None

    if stay:
        stay_dates_explicit = bool(parsed.get("stay_dates_explicit"))
        depart = flight.get("depart_date")
        if not stay_dates_explicit and depart and _is_valid_iso_date(depart):
            checkout = flight.get("return_date")
            if not checkout or not _is_valid_iso_date(checkout):
                try:
                    span = max(1, (datetime.strptime(stay["checkout"], "%Y-%m-%d").date()
                                    - datetime.strptime(stay["checkin"], "%Y-%m-%d").date()).days)
                except (ValueError, KeyError):
                    span = 3
                checkout = (datetime.strptime(depart, "%Y-%m-%d").date() + timedelta(days=span)).isoformat()
            stay["checkin"] = depart
            stay["checkout"] = checkout

        # The flight destination is a hard-validated IATA code; the stay
        # destination is free text and more error-prone. If they disagree,
        # trust the flight and keep only the stay's filters (stars, price, etc).
        flight_city = _airport_city_for_code(flight.get("destination"))
        if flight_city and flight_city.lower() not in str(stay.get("destination") or "").lower():
            print(
                f"AI COMBINED PARSE: stay destination '{stay.get('destination')}' disagreed with "
                f"flight destination city '{flight_city}' — using the flight destination for the hotel search."
            )
            stay["destination"] = flight_city

    return {
        "flight": flight,
        "stay": stay,
        "wants_hotel": bool(stay),
        # Exposed so the flight-selection step can tell whether it's safe to
        # re-anchor hotel dates to the *actually selected* flight's real
        # dates (relevant for flex/holiday parses, where depart_date is only
        # a guess at parse time) — never overwrite dates the user stated explicitly.
        "stay_dates_explicit": bool(parsed.get("stay_dates_explicit")) if stay else False,
    }

# ------------------------------------------------------------
# Pooled Duffel client
# ------------------------------------------------------------
def _build_session(*, retry_total: int = 3, backoff_factor: float = 0.35) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retry_total,
        connect=retry_total,
        read=retry_total,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=HTTP_POOL_CONNECTIONS,
        pool_maxsize=HTTP_POOL_MAXSIZE,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"Accept": "application/json"})
    return session


class DuffelAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class DuffelClient:
    def __init__(self):
        self.session = _build_session()
        self.fast_session = _build_session(retry_total=0, backoff_factor=0.0)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {DUFFEL_ACCESS_TOKEN}",
            "Duffel-Version": DUFFEL_VERSION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None, timeout: float = DUFFEL_HTTP_TIMEOUT, fast: bool = False) -> requests.Response:
        session = self.fast_session if fast else self.session
        return session.request(
            method=method,
            url=f"{DUFFEL_BASE}{path}",
            params=params,
            json=json_body,
            headers=self._headers(),
            timeout=timeout,
        )

    def _error_message(self, resp: requests.Response, fallback: str) -> str:
        try:
            payload = resp.json()
        except ValueError:
            return fallback

        messages: list[str] = []
        for err in payload.get("errors") or []:
            title = str(err.get("title") or "").strip()
            detail = str(err.get("message") or err.get("detail") or "").strip()
            combined = ": ".join(part for part in [title, detail] if part)
            if combined:
                messages.append(combined)

        if messages:
            return " ".join(messages[:3])

        for key in ("message", "detail", "error"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value

        return fallback

    def get_offer(
        self,
        offer_id: str,
        *,
        return_available_services: bool = False,
        timeout: float = DUFFEL_HTTP_TIMEOUT,
        fast: bool = False,
    ) -> dict[str, Any]:
        try:
            params = {"return_available_services": "true"} if return_available_services else None
            resp = self._request("GET", f"/air/offers/{offer_id}", params=params, timeout=timeout, fast=fast)
        except requests.RequestException as exc:
            print("DUFFEL OFFER EXCEPTION:", repr(exc))
            raise DuffelAPIError("We couldn't reach Duffel to refresh that offer.")

        if not resp.ok:
            message = self._error_message(resp, "We couldn't refresh that Duffel offer.")
            print("DUFFEL OFFER STATUS:", resp.status_code)
            print("DUFFEL OFFER BODY:", resp.text[:800])
            raise DuffelAPIError(message, status_code=resp.status_code)

        try:
            data = resp.json().get("data") or {}
        except Exception as exc:
            print("DUFFEL OFFER JSON ERROR:", repr(exc))
            raise DuffelAPIError("Duffel returned an unexpected offer response.")

        return data

    def get_seat_maps(self, offer_id: str) -> list[dict[str, Any]]:
        try:
            resp = self._request("GET", "/air/seat_maps", params={"offer_id": offer_id}, timeout=10)
        except requests.RequestException as exc:
            print("DUFFEL SEAT MAP EXCEPTION:", repr(exc))
            raise DuffelAPIError("We couldn't load seats for this offer.")

        if not resp.ok:
            message = self._error_message(resp, "We couldn't load seats for this offer.")
            print("DUFFEL SEAT MAP STATUS:", resp.status_code)
            print("DUFFEL SEAT MAP BODY:", resp.text[:800])
            raise DuffelAPIError(message, status_code=resp.status_code)

        try:
            data = resp.json().get("data") or []
        except Exception as exc:
            print("DUFFEL SEAT MAP JSON ERROR:", repr(exc))
            raise DuffelAPIError("Duffel returned an unexpected seat map response.")

        return data if isinstance(data, list) else []

    def create_component_client_key(self) -> str:
        payload = {"data": {}}
        try:
            resp = self._request("POST", "/identity/component_client_keys", json_body=payload, timeout=10)
        except requests.RequestException as exc:
            print("DUFFEL COMPONENT KEY EXCEPTION:", repr(exc))
            raise DuffelAPIError("We couldn't prepare Duffel's secure checkout components.")

        if not resp.ok:
            message = self._error_message(resp, "We couldn't prepare Duffel's secure checkout components.")
            print("DUFFEL COMPONENT KEY STATUS:", resp.status_code)
            print("DUFFEL COMPONENT KEY BODY:", resp.text[:800])
            raise DuffelAPIError(message, status_code=resp.status_code)

        try:
            data = resp.json().get("data") or {}
        except Exception as exc:
            print("DUFFEL COMPONENT KEY JSON ERROR:", repr(exc))
            raise DuffelAPIError("Duffel returned an unexpected component key response.")

        return str(data.get("component_client_key") or data.get("client_key") or "").strip()

    def create_order(
        self,
        *,
        offer_id: str,
        passengers: list[dict[str, Any]],
        total_amount: str,
        total_currency: str,
        services: list[dict[str, Any]] | None = None,
        payments: list[dict[str, Any]] | None = None,
        order_type: str = "instant",
    ) -> dict[str, Any]:
        payload = {
            "type": order_type,
            "selected_offers": [offer_id],
            "payments": payments or [
                {
                    "type": "balance",
                    "currency": total_currency,
                    "amount": total_amount,
                }
            ],
            "passengers": passengers,
        }
        if services:
            payload["services"] = services
        print(
            "DUFFEL ORDER CREATE:",
            json.dumps(
                {
                    "offer_id": offer_id,
                    "passenger_count": len(passengers),
                    "service_count": len(services or []),
                    "currency": total_currency,
                    "amount": total_amount,
                    "env": DUFFEL_ENV,
                }
            ),
        )
        try:
            resp = self._request("POST", "/air/orders", json_body={"data": payload})
        except requests.RequestException as exc:
            print("DUFFEL ORDER EXCEPTION:", repr(exc))
            raise DuffelAPIError("We couldn't reach Duffel to create the booking.")

        if not resp.ok:
            message = self._error_message(resp, "Duffel couldn't create this booking.")
            print("DUFFEL ORDER STATUS:", resp.status_code)
            print("DUFFEL ORDER BODY:", resp.text[:1200])
            raise DuffelAPIError(message, status_code=resp.status_code)

        try:
            data = resp.json().get("data") or {}
        except Exception as exc:
            print("DUFFEL ORDER JSON ERROR:", repr(exc))
            raise DuffelAPIError("Duffel returned an unexpected order response.")

        print("DUFFEL ORDER CREATED:", data.get("id"), data.get("booking_reference"))
        return data

    def get_order(self, order_id: str) -> dict[str, Any]:
        try:
            resp = self._request("GET", f"/air/orders/{order_id}")
        except requests.RequestException as exc:
            print("DUFFEL ORDER LOOKUP EXCEPTION:", repr(exc))
            raise DuffelAPIError("We couldn't refresh that Duffel order.")

        if not resp.ok:
            message = self._error_message(resp, "We couldn't refresh that Duffel order.")
            print("DUFFEL ORDER LOOKUP STATUS:", resp.status_code)
            print("DUFFEL ORDER LOOKUP BODY:", resp.text[:800])
            raise DuffelAPIError(message, status_code=resp.status_code)

        try:
            data = resp.json().get("data") or {}
        except Exception as exc:
            print("DUFFEL ORDER LOOKUP JSON ERROR:", repr(exc))
            raise DuffelAPIError("Duffel returned an unexpected order response.")

        return data

    def list_orders(
        self,
        *,
        booking_reference: str | None = None,
        passenger_last_name: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 10), 50))}
        if booking_reference:
            params["booking_reference"] = booking_reference
        if passenger_last_name:
            params["passenger_name[]"] = [passenger_last_name]
        try:
            resp = self._request("GET", "/air/orders", params=params)
        except requests.RequestException as exc:
            print("DUFFEL ORDER LIST EXCEPTION:", repr(exc))
            raise DuffelAPIError("We couldn't reach Duffel to find that booking.")

        if not resp.ok:
            message = self._error_message(resp, "We couldn't find that booking right now.")
            print("DUFFEL ORDER LIST STATUS:", resp.status_code)
            print("DUFFEL ORDER LIST BODY:", resp.text[:800])
            raise DuffelAPIError(message, status_code=resp.status_code)

        try:
            data = resp.json().get("data") or []
        except Exception as exc:
            print("DUFFEL ORDER LIST JSON ERROR:", repr(exc))
            raise DuffelAPIError("Duffel returned an unexpected order list response.")

        return data if isinstance(data, list) else []

    def create_order_cancellation(self, order_id: str) -> dict[str, Any]:
        payload = {"data": {"order_id": order_id}}
        try:
            resp = self._request("POST", "/air/order_cancellations", json_body=payload)
        except requests.RequestException as exc:
            print("DUFFEL ORDER CANCELLATION EXCEPTION:", repr(exc))
            raise DuffelAPIError("We couldn't request a cancellation quote right now.")
        if not resp.ok:
            message = self._error_message(resp, "We couldn't request a cancellation quote right now.")
            raise DuffelAPIError(message, status_code=resp.status_code)
        try:
            data = resp.json().get("data") or {}
        except Exception as exc:
            print("DUFFEL ORDER CANCELLATION JSON ERROR:", repr(exc))
            raise DuffelAPIError("Duffel returned an unexpected cancellation response.")
        return data

    def confirm_order_cancellation(self, order_cancellation_id: str) -> dict[str, Any]:
        try:
            resp = self._request("POST", f"/air/order_cancellations/{order_cancellation_id}/actions/confirm", json_body={"data": {}})
        except requests.RequestException as exc:
            print("DUFFEL CONFIRM CANCELLATION EXCEPTION:", repr(exc))
            raise DuffelAPIError("We couldn't confirm cancellation right now.")
        if not resp.ok:
            message = self._error_message(resp, "We couldn't confirm cancellation right now.")
            raise DuffelAPIError(message, status_code=resp.status_code)
        try:
            data = resp.json().get("data") or {}
        except Exception as exc:
            print("DUFFEL CONFIRM CANCELLATION JSON ERROR:", repr(exc))
            raise DuffelAPIError("Duffel returned an unexpected cancellation confirmation response.")
        return data

    def create_order_change_request(
        self,
        *,
        order_id: str,
        slice_id_to_remove: str,
        origin: str,
        destination: str,
        departure_date: str,
        cabin_class: str = "economy",
    ) -> dict[str, Any]:
        payload = {
            "data": {
                "order_id": order_id,
                "slices": {
                    "remove": [{"slice_id": slice_id_to_remove}],
                    "add": [
                        {
                            "origin": origin,
                            "destination": destination,
                            "departure_date": departure_date,
                            "cabin_class": cabin_class,
                        }
                    ],
                },
            }
        }
        try:
            resp = self._request("POST", "/air/order_change_requests", json_body=payload)
        except requests.RequestException as exc:
            print("DUFFEL ORDER CHANGE REQUEST EXCEPTION:", repr(exc))
            raise DuffelAPIError("We couldn't request flight change options right now.")
        if not resp.ok:
            message = self._error_message(resp, "We couldn't request flight change options right now.")
            raise DuffelAPIError(message, status_code=resp.status_code)
        try:
            data = resp.json().get("data") or {}
        except Exception as exc:
            print("DUFFEL ORDER CHANGE REQUEST JSON ERROR:", repr(exc))
            raise DuffelAPIError("Duffel returned an unexpected order change request response.")
        return data

    def create_order_change(self, order_change_offer_id: str) -> dict[str, Any]:
        payload = {"data": {"selected_order_change_offer": order_change_offer_id}}
        try:
            resp = self._request("POST", "/air/order_changes", json_body=payload)
        except requests.RequestException as exc:
            print("DUFFEL CREATE ORDER CHANGE EXCEPTION:", repr(exc))
            raise DuffelAPIError("We couldn't create the selected flight change right now.")
        if not resp.ok:
            message = self._error_message(resp, "We couldn't create the selected flight change right now.")
            raise DuffelAPIError(message, status_code=resp.status_code)
        try:
            data = resp.json().get("data") or {}
        except Exception as exc:
            print("DUFFEL CREATE ORDER CHANGE JSON ERROR:", repr(exc))
            raise DuffelAPIError("Duffel returned an unexpected order change response.")
        return data

    def confirm_order_change(self, order_change_id: str, *, amount: str, currency: str) -> dict[str, Any]:
        payload = {
            "data": {
                "payment": {
                    "type": "balance",
                    "currency": currency,
                    "amount": amount,
                }
            }
        }
        try:
            resp = self._request("POST", f"/air/order_changes/{order_change_id}/actions/confirm", json_body=payload)
        except requests.RequestException as exc:
            print("DUFFEL CONFIRM ORDER CHANGE EXCEPTION:", repr(exc))
            raise DuffelAPIError("We couldn't confirm that flight change right now.")
        if not resp.ok:
            message = self._error_message(resp, "We couldn't confirm that flight change right now.")
            raise DuffelAPIError(message, status_code=resp.status_code)
        try:
            data = resp.json().get("data") or {}
        except Exception as exc:
            print("DUFFEL CONFIRM ORDER CHANGE JSON ERROR:", repr(exc))
            raise DuffelAPIError("Duffel returned an unexpected order change confirmation response.")
        return data

    def search_places(self, keyword: str, limit: int = 12) -> list[dict[str, Any]]:
        if not DUFFEL_ACCESS_TOKEN:
            return []
        try:
            resp = self._request(
                "GET",
                "/places/suggestions",
                params={"query": keyword},
                timeout=DUFFEL_PLACE_TIMEOUT,
                fast=True,
            )
        except requests.RequestException as exc:
            print("DUFFEL PLACE EXCEPTION:", repr(exc))
            return []
        if not resp.ok:
            print("DUFFEL PLACE STATUS:", resp.status_code)
            print("DUFFEL PLACE BODY:", resp.text[:400])
            return []

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in resp.json().get("data", []):
            place_type = (item.get("type") or "").lower()
            code = (item.get("iata_code") or "").strip().upper()
            if not code or len(code) != 3 or code in seen:
                continue

            country = (item.get("iata_country_code") or "").strip().upper()

            if place_type == "city":
                seen.add(code)
                display = (item.get("name") or item.get("city_name") or code).strip()
                city_name = (item.get("city_name") or "").strip()
                loc_parts = [x for x in [city_name or display, country] if x]
                loc = f" ({', '.join(loc_parts)})" if loc_parts else ""
                results.append({
                    "code": code,
                    "label": f"{code} — {display} (all airports){loc}".strip(),
                    "subType": "CITY",
                })
            elif place_type == "airport":
                seen.add(code)
                city = (item.get("city_name") or "").strip()
                name = (item.get("name") or "").strip()
                loc_parts = [x for x in [city, country] if x]
                loc = f" ({', '.join(loc_parts)})" if loc_parts else ""
                results.append({"code": code, "label": f"{code} — {name}{loc}".strip(), "subType": "AIRPORT"})
            else:
                continue

            if len(results) >= limit:
                break
        return results

    def flight_offers_raw(
        self,
        payload: dict[str, Any],
        *,
        supplier_timeout_ms: int = DUFFEL_SUPPLIER_TIMEOUT_MS,
        timeout: float = DUFFEL_HTTP_TIMEOUT,
        fast: bool = False,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]] | None:
        global _rl_reset_at
        cache_key = (json.dumps(payload, sort_keys=True), int(supplier_timeout_ms))
        if not force_refresh:
            cached = RAW_SEARCH_CACHE.get(cache_key)
            if cached is not None:
                return cached

        max_attempts = FLEX_SCAN_RETRY_MAX if fast else 1
        for attempt in range(max_attempts):
            # Respect the global rate-limit cooldown written by any earlier 429.
            with _rl_reset_lock:
                wait_until = _rl_reset_at
            cooldown = wait_until - time.time()
            if cooldown > 0:
                print(f"DUFFEL rate-limit cooldown {cooldown:.1f}s (attempt {attempt + 1})")
                time.sleep(min(cooldown, FLEX_SCAN_RETRY_CAP))

            # Throttle request rate during fast/flex scans.
            if fast:
                _FLEX_RATE_LIMITER.wait()

            try:
                resp = self._request(
                    "POST",
                    "/air/offer_requests",
                    params={"supplier_timeout": int(supplier_timeout_ms), "return_offers": "true"},
                    json_body={"data": payload},
                    timeout=timeout,
                    fast=fast,
                )
            except requests.RequestException as exc:
                print("DUFFEL SEARCH EXCEPTION:", repr(exc))
                return None

            if resp.status_code == 429:
                # Duffel sends ratelimit-reset as *seconds until reset* (relative),
                # NOT as a Unix timestamp. Guard against both interpretations.
                raw_hdr = resp.headers.get("ratelimit-reset") or resp.headers.get("x-ratelimit-reset") or ""
                try:
                    hdr_val = float(raw_hdr)
                    # If the value looks like a Unix timestamp (> 1e9) treat it as
                    # absolute; otherwise assume it is seconds-until-reset.
                    if hdr_val > 1_000_000_000:
                        reset_epoch = hdr_val
                    else:
                        reset_epoch = time.time() + max(hdr_val, 1.0)
                except (ValueError, TypeError):
                    # No header or unparseable: back off exponentially.
                    reset_epoch = time.time() + min(2 ** attempt, FLEX_SCAN_RETRY_CAP)

                with _rl_reset_lock:
                    if reset_epoch > _rl_reset_at:
                        _rl_reset_at = reset_epoch

                sleep_for = min(reset_epoch - time.time(), FLEX_SCAN_RETRY_CAP)
                print(f"DUFFEL 429 – sleeping {sleep_for:.1f}s (attempt {attempt + 1}/{max_attempts})")
                if attempt < max_attempts - 1:
                    time.sleep(max(sleep_for, 0.0))
                    continue
                # Exhausted retries.
                print("DUFFEL SEARCH STATUS:", resp.status_code)
                return None

            if not resp.ok:
                print("DUFFEL SEARCH STATUS:", resp.status_code)
                print("DUFFEL SEARCH BODY:", resp.text[:800])
                return None

            try:
                data = (resp.json().get("data") or {}).get("offers") or []
            except Exception as exc:
                print("DUFFEL SEARCH JSON ERROR:", repr(exc))
                return None

            RAW_SEARCH_CACHE.set(cache_key, data)
            return data

        return None


DUFF = DuffelClient()
LITE = LiteAPIClient()

# ------------------------------------------------------------
# Search core
# ------------------------------------------------------------
def _build_layovers(segments: list[dict], detailed: bool = True) -> list[dict]:
    if not detailed or len(segments) < 2:
        return []
    layovers = []
    for i in range(len(segments) - 1):
        arr = segments[i]["arrival"]["at"]
        next_dep = segments[i + 1]["departure"]["at"]
        airport_code = segments[i]["arrival"]["iataCode"]
        layovers.append({
            "code": airport_code,
            "name": _airport_display_name_local(airport_code),
            "minutes": _minutes_between(arr, next_dep),
            "arrival_at": arr,
            "next_depart_at": next_dep,
            "overnight": _dt(next_dep).date() > _dt(arr).date(),
        })
    return layovers


def _slice_duration_floor_minutes(
    segments: list[dict[str, Any]],
    layovers: list[dict[str, Any]],
) -> int:
    if len(segments) < 2:
        return 0

    def _seg_depart_iso(segment: Mapping[str, Any]) -> str | None:
        return (
            segment.get("departing_at")
            or ((segment.get("departure") or {}).get("at"))
        )

    def _seg_arrive_iso(segment: Mapping[str, Any]) -> str | None:
        return (
            segment.get("arriving_at")
            or ((segment.get("arrival") or {}).get("at"))
        )

    total = sum(
        _display_duration_minutes(
            segment.get("duration"),
            _seg_depart_iso(segment),
            _seg_arrive_iso(segment),
        )
        for segment in segments
    )
    total += sum(int(layover.get("minutes", 0) or 0) for layover in (layovers or []))
    return total

def _traveler_context_cache_sig(params: dict[str, Any]) -> tuple:
    """
    Hashable fingerprint of the soft ranking/reasoning preferences so two
    requests that differ only by "flying with my mom" or a layover
    preference don't share a cached result set with stale badge reasoning.
    """
    traveler_context = params.get("traveler_context") or {}
    return (
        tuple(traveler_context.get("companion_labels") or ()),
        bool(traveler_context.get("prefers_longer_layover")),
        bool(traveler_context.get("prefers_shorter_layover")),
    )

def _normalize_search_key(params: dict[str, Any], detailed: bool) -> tuple:
    legs_sig = tuple(
        (
            str((leg or {}).get("origin") or "").strip().upper(),
            str((leg or {}).get("destination") or "").strip().upper(),
            str((leg or {}).get("depart_date") or "").strip(),
        )
        for leg in (params.get("legs") or [])
        if isinstance(leg, Mapping)
    )
    return (
        params.get("trip_type", "roundtrip"),
        params.get("origin"),
        params.get("destination"),
        params.get("depart_date"),
        params.get("return_date"),
        legs_sig,
        int(params.get("passengers", 1) or 1),
        params.get("cabin", "ECONOMY"),
        bool(params.get("nonstop", False)),
        params.get("sort", "recommended"),
        params.get("max_price"),
        bool(detailed),
        _traveler_context_cache_sig(params),
    )

def _normalize_cheapest_snapshot_key(params: dict[str, Any]) -> tuple:
    legs_sig = tuple(
        (
            str((leg or {}).get("origin") or "").strip().upper(),
            str((leg or {}).get("destination") or "").strip().upper(),
            str((leg or {}).get("depart_date") or "").strip(),
        )
        for leg in (params.get("legs") or [])
        if isinstance(leg, Mapping)
    )
    return (
        params.get("trip_type", "roundtrip"),
        params.get("origin"),
        params.get("destination"),
        params.get("depart_date"),
        params.get("return_date"),
        legs_sig,
        int(params.get("passengers", 1) or 1),
        params.get("cabin", "ECONOMY"),
        bool(params.get("nonstop", False)),
        params.get("max_price"),
    )

def _flight_offer_query(params: dict[str, Any], *, detailed: bool) -> dict[str, Any]:
    cabin_map = {
        "ECONOMY": "economy",
        "PREMIUM_ECONOMY": "premium_economy",
        "BUSINESS": "business",
        "FIRST": "first",
    }
    if params.get("trip_type") == "multicity" and isinstance(params.get("legs"), list):
        slices = []
        for leg in params.get("legs") or []:
            if not isinstance(leg, Mapping):
                continue
            origin = str(leg.get("origin") or "").strip().upper()
            destination = str(leg.get("destination") or "").strip().upper()
            depart = str(leg.get("depart_date") or "").strip()
            if not origin or not destination or not depart:
                continue
            slices.append({"origin": origin, "destination": destination, "departure_date": depart})
        if not slices:
            slices = [
                {
                    "origin": params["origin"],
                    "destination": params["destination"],
                    "departure_date": params["depart_date"],
                }
            ]
    else:
        slices = [
            {
                "origin": params["origin"],
                "destination": params["destination"],
                "departure_date": params["depart_date"],
            }
        ]
        if params.get("return_date"):
            slices.append({
                "origin": params["destination"],
                "destination": params["origin"],
                "departure_date": params["return_date"],
            })

    payload: dict[str, Any] = {
        "slices": slices,
        "passengers": [{"type": "adult"} for _ in range(int(params.get("passengers", 1) or 1))],
        "cabin_class": cabin_map.get(params.get("cabin"), "economy"),
    }
    if params.get("nonstop"):
        payload["max_connections"] = 0
    max_price = params.get("max_price")
    if max_price not in (None, ""):
        try:
            payload["max_amount"] = f"{float(max_price):.2f}"
        except Exception:
            pass
    return payload

def _cheapest_offer_snapshot(params: dict[str, Any]) -> dict[str, Any] | None:
    cache_key = _normalize_cheapest_snapshot_key(params)
    cached_found, cached = CHEAPEST_SNAPSHOT_CACHE.lookup(cache_key)
    if cached_found:
        return cached

    light_supplier_ms = min(int(LIGHT_REQUEST_TIMEOUT * 1000) - 1500, DUFFEL_SUPPLIER_TIMEOUT_MS)
    raw = DUFF.flight_offers_raw(
        _flight_offer_query(params, detailed=False),
        supplier_timeout_ms=max(3000, light_supplier_ms),
        timeout=LIGHT_REQUEST_TIMEOUT,
        fast=True,
    )
    if raw is None:
        return None
    if not raw:
        CHEAPEST_SNAPSHOT_CACHE.set(cache_key, None)
        return None

    cheapest: dict[str, Any] | None = None
    for offer in raw:
        total_price = _safe_float(offer.get("total_amount"))
        currency = offer.get("total_currency") or "USD"
        if total_price <= 0:
            continue
        snapshot = {
            "scan_price_total": total_price,
            "scan_currency": currency,
            "raw_offer": offer,
        }
        if cheapest is None or total_price < float(cheapest["scan_price_total"]):
            cheapest = snapshot

    CHEAPEST_SNAPSHOT_CACHE.set(cache_key, cheapest)
    return cheapest


FLEX_FINAL_SUPPLIER_TIMEOUT_MS = int(os.getenv("FLEX_FINAL_SUPPLIER_TIMEOUT_MS", "8000"))
FLEX_FINAL_HTTP_TIMEOUT = float(os.getenv("FLEX_FINAL_HTTP_TIMEOUT", "10"))


def _fetch_live_offer_rows(
    params: dict[str, Any],
    *,
    detailed: bool,
    flex_final: bool = False,
    force_refresh: bool = False,
) -> list[dict[str, Any]] | None:
    payload = _flight_offer_query(params, detailed=detailed)
    if flex_final and detailed:
        supplier_timeout_ms = FLEX_FINAL_SUPPLIER_TIMEOUT_MS
        timeout = FLEX_FINAL_HTTP_TIMEOUT
    elif detailed:
        supplier_timeout_ms = DUFFEL_SUPPLIER_TIMEOUT_MS
        timeout = DUFFEL_HTTP_TIMEOUT
    else:
        supplier_timeout_ms = max(3000, min(int(LIGHT_REQUEST_TIMEOUT * 1000) - 1500, DUFFEL_SUPPLIER_TIMEOUT_MS))
        timeout = LIGHT_REQUEST_TIMEOUT
    return DUFF.flight_offers_raw(
        payload,
        supplier_timeout_ms=supplier_timeout_ms,
        timeout=timeout,
        fast=not detailed,
        force_refresh=force_refresh,
    )

def _fallback_flights_from_snapshot(params: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = _cheapest_offer_snapshot(params)
    if not snapshot:
        return []

    raw_offer = snapshot.get("raw_offer")
    if not raw_offer:
        return []

    flights = _collect_best_presentations([raw_offer], params, detailed=False)
    if not flights:
        return []

    sort_mode = params.get("sort", "recommended")
    flights = _sort_flights(flights, sort_mode, params=params)
    _assign_smart_badges(flights, sort_mode, params=params)
    _annotate_comparison_metrics(flights)
    flights = _decorate_flights_for_display(flights, params)
    return _clean_flights_for_render(flights)

def _offer_signature(offer: dict[str, Any]) -> tuple:
    sig = []
    for slc in offer.get("slices") or []:
        for seg in slc.get("segments") or []:
            marketing = seg.get("marketing_carrier") or {}
            operating = seg.get("operating_carrier") or {}
            sig.append((
                marketing.get("iata_code") or operating.get("iata_code") or "",
                seg.get("marketing_carrier_flight_number") or seg.get("number") or "",
                ((seg.get("origin") or {}).get("iata_code")) or ((seg.get("departure") or {}).get("iataCode")) or "",
                seg.get("departing_at") or (seg.get("departure") or {}).get("at") or "",
                ((seg.get("destination") or {}).get("iata_code")) or ((seg.get("arrival") or {}).get("iataCode")) or "",
                seg.get("arriving_at") or (seg.get("arrival") or {}).get("at") or "",
            ))
    return tuple(sig)

def _offer_identity_signature(offer: dict[str, Any]) -> tuple[Any, ...]:
    offer_id = str(offer.get("id") or "").strip()
    if offer_id:
        return ("id", offer_id)
    return (
        "fallback",
        _offer_signature(offer),
        str(offer.get("total_currency") or "").strip().upper(),
        str(offer.get("total_amount") or "").strip(),
    )

def _offer_selection_token(offer: dict[str, Any]) -> str:
    payload = json.dumps(_offer_signature(offer), separators=(",", ":"), default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]

def _offer_carrier_codes(offer: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    for slc in offer.get("slices") or []:
        for segment in slc.get("segments") or []:
            for key in ("marketing_carrier", "operating_carrier"):
                carrier = segment.get(key) or {}
                iata = carrier.get("iata_code") or ""
                _register_carrier(iata, carrier.get("name"))
                codes.append(iata)
    return _unique_preserve(codes)

def _offer_primary_carrier_code(offer: dict[str, Any]) -> str:
    codes = _offer_carrier_codes(offer)
    return codes[0] if codes else "UNKNOWN"

def _offer_owner_key(offer: Mapping[str, Any]) -> tuple[str, str]:
    owner = offer.get("owner") or {}
    owner_code = str(owner.get("iata_code") or owner.get("id") or "").strip().upper()
    owner_name = _clean_marketing_label(owner.get("name") or "").lower()
    return owner_code, owner_name

def _clean_marketing_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())

def _humanize_marketing_label(value: Any) -> str:
    label = _clean_marketing_label(value)
    if not label:
        return ""
    compact = re.sub(r"[\s\-–—/._]+", "", label)
    if compact and compact.isupper():
        if " " not in label and len(compact) <= 4:
            return label
        return label.title()
    return label

def _trim_fare_brand_label(value: Any) -> str:
    # Preserve Duffel's exact fare-brand wording; only normalize whitespace.
    return _clean_marketing_label(value)

def _offer_cabin_label(offer: Mapping[str, Any]) -> str:
    for slice_data in offer.get("slices") or []:
        for segment in slice_data.get("segments") or []:
            for passenger in segment.get("passengers") or []:
                label = _clean_marketing_label(
                    passenger.get("cabin_class")
                    or passenger.get("cabin_class_marketing_name")
                    or ((passenger.get("cabin") or {}).get("name"))
                    or ""
                )
                if label:
                    return label.replace("_", " ").title()
    return ""

def _offer_fare_brand_label(offer: Mapping[str, Any]) -> str:
    slice_labels: list[str] = []
    for slice_data in offer.get("slices") or []:
        if not isinstance(slice_data, Mapping):
            continue
        for key in ("fare_brand_name", "fare_brand"):
            label = _clean_marketing_label(slice_data.get(key) or "")
            if label:
                slice_labels.append(label)
                break
    if slice_labels:
        unique_labels: list[str] = []
        seen_labels: set[str] = set()
        for label in slice_labels:
            normalized = _clean_marketing_label(label).lower()
            if not normalized or normalized in seen_labels:
                continue
            seen_labels.add(normalized)
            unique_labels.append(_trim_fare_brand_label(label))
        if unique_labels:
            return " + ".join(unique_labels)

    # Do not synthesize fare-brand from cabin/cabin-marketing fields.
    # If Duffel doesn't provide a fare brand, leave blank and show neutral fallback in UI.
    for slice_data in offer.get("slices") or []:
        for segment in slice_data.get("segments") or []:
            for passenger in segment.get("passengers") or []:
                label = _clean_marketing_label(
                    passenger.get("fare_brand_name")
                    or passenger.get("fare_brand")
                    or ""
                )
                if label:
                    return _trim_fare_brand_label(label)
    return ""

def _normalize_cabin_rank(label: str | None) -> int:
    value = str(label or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not value:
        return 0
    if "first" in value:
        return 4
    if "business" in value:
        return 3
    if "premium" in value and "econom" in value:
        return 2
    if "econom" in value:
        return 1
    return 0

def _offer_checked_bag_benefit(offer: Mapping[str, Any]) -> str:
    passengers = offer.get("passengers") or []
    for passenger in passengers:
        for bag in (passenger or {}).get("baggages") or []:
            quantity = int(bag.get("quantity") or 0)
            if quantity > 0:
                return "Checked bag included"
    services = offer.get("available_services") or []
    has_paid_bag = False
    for service in services:
        service_type = str(service.get("type") or service.get("service_type") or "").strip().lower()
        if "bag" not in service_type:
            continue
        amount = _safe_float(service.get("total_amount") or service.get("amount"), 0.0)
        if amount <= 0:
            return "Checked bag included"
        has_paid_bag = True
    return "Paid checked bag available" if has_paid_bag else "No checked bag option"

def _offer_tier_benefits(offer: Mapping[str, Any], *, seat_policy: Mapping[str, Any]) -> list[str]:
    _ = seat_policy
    rows = _offer_feature_rows(offer, seat_policy=seat_policy)
    return [str(row.get("value") or "").strip() for row in rows if str(row.get("value") or "").strip()]

def _format_price_display(amount: float, currency: str) -> str:
    ccy = (currency or "USD").strip().upper() or "USD"
    if ccy == "USD":
        return f"${amount:.2f}"
    return f"{ccy} {amount:.2f}"

def _offer_tier_features(offer: Mapping[str, Any]) -> list[str]:
    features: list[str] = []
    conditions = offer.get("conditions") or {}
    change_rule = (conditions.get("change_before_departure") or {}) if isinstance(conditions, Mapping) else {}
    refund_rule = (conditions.get("refund_before_departure") or {}) if isinstance(conditions, Mapping) else {}

    if change_rule:
        allowed = change_rule.get("allowed")
        if allowed is True:
            penalty_amount = str(change_rule.get("penalty_amount") or "").strip()
            penalty_currency = str(change_rule.get("penalty_currency") or "").strip()
            if penalty_amount:
                features.append(f"Changes allowed with {penalty_currency} {penalty_amount} fee".strip())
            else:
                features.append("Changes allowed")
        elif allowed is False:
            features.append("Changes not allowed")

    if refund_rule:
        allowed = refund_rule.get("allowed")
        if allowed is True:
            penalty_amount = str(refund_rule.get("penalty_amount") or "").strip()
            penalty_currency = str(refund_rule.get("penalty_currency") or "").strip()
            if penalty_amount:
                features.append(f"Refunds allowed with {penalty_currency} {penalty_amount} fee".strip())
            else:
                features.append("Refunds allowed")
        elif allowed is False:
            features.append("Refunds not allowed")

    available_services = offer.get("available_services") or []
    has_free_bag = False
    has_paid_bag = False
    has_seat_selection = False
    for service in available_services:
        service_type = str(service.get("type") or service.get("service_type") or "").strip().lower()
        amount = _safe_float(service.get("total_amount") or service.get("amount"), 0.0)
        if "bag" in service_type:
            if amount <= 0:
                has_free_bag = True
            else:
                has_paid_bag = True
        if "seat" in service_type:
            has_seat_selection = True

    if has_free_bag:
        features.append("Checked bag included")
    elif has_paid_bag:
        features.append("Checked bags available as add-on")
    if has_seat_selection:
        features.append("Seat selection available")

    payment = offer.get("payment_requirements") or {}
    hold_supported = (payment.get("requires_instant_payment") is False) or (payment.get("requires_immediate_payment") is False)
    if hold_supported:
        features.append("Hold booking option available")

    seen: set[str] = set()
    unique_features: list[str] = []
    for item in features:
        if not item:
            continue
        key = item.strip().upper()
        if key in seen:
            continue
        seen.add(key)
        unique_features.append(item)
    return unique_features[:4]

def _offer_fare_rows(
    carry_on_label: str | None,
    checked_bag_label: str | None,
    change_label: str | None,
    refund_label: str | None,
    hold_supported: bool,
) -> list[dict[str, str]]:
    """Structured (label, value, state) rows for the fare & baggage summary.

    state drives icon/color in the UI: "positive" (green check), "negative"
    (muted x), or "fee" (amber check — allowed, but costs something).
    """
    rows: list[dict[str, str]] = []

    def bag_row(label: str, text: str | None) -> None:
        if not text:
            return
        is_included = "includes" in text.lower()
        rows.append({
            "label": label,
            "value": "Included" if is_included else "Not included",
            "state": "positive" if is_included else "negative",
        })

    bag_row("Carry-on bag", carry_on_label)
    bag_row("Checked bag", checked_bag_label)

    def policy_row(label: str, text: str | None) -> None:
        if not text:
            return
        lowered = text.lower()
        if lowered.startswith("not "):
            state = "negative"
        elif "fee" in lowered:
            state = "fee"
        else:
            state = "positive"
        rows.append({"label": label, "value": text, "state": state})

    policy_row("Changes", change_label)
    policy_row("Refunds", refund_label)

    if hold_supported:
        rows.append({"label": "Hold booking", "value": "Available", "state": "positive"})

    return rows

def _offer_tier_summary(offer: dict[str, Any]) -> dict[str, Any]:
    total_price = _safe_float(offer.get("total_amount"))
    currency = str(offer.get("total_currency") or "USD").strip() or "USD"
    fare_brand = _offer_fare_brand_label(offer)
    cabin_label = _offer_cabin_label(offer)

    if fare_brand:
        tier_name = fare_brand
    elif cabin_label:
        tier_name = cabin_label
    else:
        tier_name = "Standard fare"

    features = _offer_tier_features(offer)
    if not features:
        features = ["Fare details provided by the airline at checkout"]

    return {
        "offer_id": str(offer.get("id") or "").strip(),
        "name": tier_name,
        "fare_brand": fare_brand,
        "cabin_label": cabin_label,
        "price": total_price,
        "currency": currency,
        "price_label": _format_price_display(total_price, currency),
        "features": features,
    }

def _offer_request_payload_from_offer(offer: Mapping[str, Any]) -> dict[str, Any] | None:
    slices_payload: list[dict[str, Any]] = []
    for slice_data in offer.get("slices") or []:
        segments = slice_data.get("segments") or []
        if not segments:
            continue
        first = segments[0]
        origin = str(((first.get("origin") or {}).get("iata_code")) or ((first.get("departure") or {}).get("iataCode")) or "").strip().upper()
        destination = str(((segments[-1].get("destination") or {}).get("iata_code")) or ((segments[-1].get("arrival") or {}).get("iataCode")) or "").strip().upper()
        departing_at = str(first.get("departing_at") or (first.get("departure") or {}).get("at") or "").strip()
        departure_date = departing_at[:10] if len(departing_at) >= 10 else ""
        if not origin or not destination or not departure_date:
            return None
        slices_payload.append({
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
        })

    if not slices_payload:
        return None

    passengers_payload: list[dict[str, str]] = []
    for passenger in offer.get("passengers") or []:
        p_type = str((passenger or {}).get("type") or "adult").strip().lower() or "adult"
        passengers_payload.append({"type": p_type})
    if not passengers_payload:
        passengers_payload = [{"type": "adult"}]

    return {
        "slices": slices_payload,
        "passengers": passengers_payload,
    }

def _condition_compact_label(condition: Mapping[str, Any] | None) -> str:
    if not condition:
        return "Not provided"
    allowed = condition.get("allowed")
    penalty_amount = str(condition.get("penalty_amount") or "").strip()
    penalty_currency = str(condition.get("penalty_currency") or "").strip()
    penalty = f"{penalty_currency} {penalty_amount}".strip()
    if allowed is True:
        return f"Allowed ({penalty})" if penalty else "Allowed"
    if allowed is False:
        return "Not allowed"
    return "Not provided"


def _condition_allowed(condition: Mapping[str, Any] | None) -> bool | None:
    if not condition:
        return None
    allowed = condition.get("allowed")
    if allowed is True:
        return True
    if allowed is False:
        return False
    return None


def _condition_feature_state(condition: Mapping[str, Any] | None) -> str:
    allowed = _condition_allowed(condition)
    if allowed is True:
        penalty_amount = str((condition or {}).get("penalty_amount") or "").strip()
        return "caution" if penalty_amount else "positive"
    if allowed is False:
        return "negative"
    return "muted"

def _condition_fee_label(condition: Mapping[str, Any] | None) -> str:
    if not condition:
        return ""
    amount_raw = str(condition.get("penalty_amount") or "").strip()
    if not amount_raw:
        return ""
    amount = _safe_float(amount_raw, 0.0)
    currency = str(condition.get("penalty_currency") or "").strip().upper()
    if currency == "USD":
        return f"US${amount:.2f}"
    if currency:
        return f"{currency} {amount:.2f}"
    return f"${amount:.2f}"

def _duffel_like_change_label(change_rule: Mapping[str, Any] | None) -> tuple[str, str, str] | None:
    allowed = _condition_allowed(change_rule)
    fee = _condition_fee_label(change_rule)
    if allowed is True:
        label = f"Changeable ({fee} fee)" if fee else "Changeable"
        return label, "caution", "changes"
    if allowed is False:
        return "Not changeable", "negative", "xmark"
    return None

def _duffel_like_refund_label(refund_rule: Mapping[str, Any] | None) -> tuple[str, str, str] | None:
    allowed = _condition_allowed(refund_rule)
    fee = _condition_fee_label(refund_rule)
    if allowed is True:
        label = f"Refundable ({fee} fee)" if fee else "Refundable"
        return label, "caution", "refunds"
    if allowed is False:
        return "Not refundable", "negative", "xmark"
    return None

def _offer_bag_inclusion_counts(offer: Mapping[str, Any]) -> tuple[int, int, bool, bool]:
    checked_qty = 0
    carry_on_qty = 0
    saw_checked = False
    saw_carry_on = False
    passenger_sets: list[Mapping[str, Any]] = []

    for passenger in offer.get("passengers") or []:
        if isinstance(passenger, Mapping):
            passenger_sets.append(passenger)

    for slice_data in offer.get("slices") or []:
        for segment in (slice_data or {}).get("segments") or []:
            for passenger in (segment or {}).get("passengers") or []:
                if isinstance(passenger, Mapping):
                    passenger_sets.append(passenger)

    for passenger in passenger_sets:
        for bag in (passenger or {}).get("baggages") or []:
            qty = int((bag or {}).get("quantity") or 0)
            bag_type = str((bag or {}).get("type") or "").strip().lower()
            if bag_type == "checked":
                saw_checked = True
                checked_qty += max(0, qty)
            elif bag_type in {"carry_on", "cabin"}:
                saw_carry_on = True
                carry_on_qty += max(0, qty)
    return checked_qty, carry_on_qty, saw_checked, saw_carry_on

def _offer_hold_supported(offer: Mapping[str, Any]) -> bool:
    payment = offer.get("payment_requirements") or {}
    instant_flag = payment.get("requires_instant_payment")
    immediate_flag = payment.get("requires_immediate_payment")
    return (instant_flag is False) or (immediate_flag is False)

def _duffel_like_baggage_labels(offer: Mapping[str, Any]) -> tuple[str | None, str | None]:
    checked_qty, carry_on_qty, saw_checked, saw_carry_on = _offer_bag_inclusion_counts(offer)
    carry_label: str | None = None
    if carry_on_qty > 0:
        carry_label = "Includes carry-on bags"
    elif saw_carry_on:
        carry_label = "No carry-on bag option"

    checked_label: str | None
    if checked_qty > 0:
        checked_label = "Includes checked bags"
    else:
        services = offer.get("available_services") or []
        has_paid_bag = any(
            "bag" in str((svc or {}).get("type") or (svc or {}).get("service_type") or "").strip().lower()
            and _safe_float((svc or {}).get("total_amount") or (svc or {}).get("amount"), 0.0) > 0
            for svc in services
        )
        if has_paid_bag:
            checked_label = None
        elif saw_checked:
            checked_label = "No checked bag option"
        else:
            checked_label = None
    return carry_label, checked_label


def _fare_profile_label_from_rules(
    change_rule: Mapping[str, Any] | None,
    refund_rule: Mapping[str, Any] | None,
) -> str:
    change_allowed = _condition_allowed(change_rule)
    refund_allowed = _condition_allowed(refund_rule)

    if change_allowed is False and refund_allowed is False:
        return "No changes or refunds"
    if change_allowed is True and refund_allowed is True:
        return "Changes and refunds"
    if change_allowed is True and refund_allowed is False:
        return "Changes only"
    if change_allowed is False and refund_allowed is True:
        return "Refunds only"
    if change_allowed is True:
        return "Changeable fare"
    if refund_allowed is True:
        return "Refundable fare"
    return "Rules vary by airline"

def _fare_penalty_hint(
    change_rule: Mapping[str, Any] | None,
    refund_rule: Mapping[str, Any] | None,
) -> str:
    def _penalty(rule: Mapping[str, Any] | None) -> str:
        if not rule:
            return ""
        amount = str(rule.get("penalty_amount") or "").strip()
        currency = str(rule.get("penalty_currency") or "").strip()
        if not amount:
            return ""
        return f"{currency} {amount}".strip()

    change_fee = _penalty(change_rule)
    refund_fee = _penalty(refund_rule)
    if change_fee and refund_fee:
        if change_fee == refund_fee:
            return f"Fees {change_fee}"
        return f"Change {change_fee} / Refund {refund_fee}"
    if change_fee:
        return f"Change {change_fee}"
    if refund_fee:
        return f"Refund {refund_fee}"
    return ""


def _service_label_state(label: str) -> str:
    txt = str(label or "").strip().lower()
    if not txt:
        return "muted"
    if "included" in txt:
        return "positive"
    if "paid" in txt or "from " in txt:
        return "caution"
    if "not offered" in txt or "no " in txt:
        return "negative"
    return "muted"


def _offer_feature_rows(
    offer: Mapping[str, Any],
    *,
    seat_policy: Mapping[str, Any],
) -> list[dict[str, str]]:
    conditions = offer.get("conditions") or {}
    change_rule = (conditions.get("change_before_departure") or {}) if isinstance(conditions, Mapping) else {}
    refund_rule = (conditions.get("refund_before_departure") or {}) if isinstance(conditions, Mapping) else {}
    change_row = _duffel_like_change_label(change_rule if isinstance(change_rule, Mapping) else None)
    refund_row = _duffel_like_refund_label(refund_rule if isinstance(refund_rule, Mapping) else None)
    carry_on_label, checked_label = _duffel_like_baggage_labels(offer)

    rows: list[dict[str, str]] = []
    if change_row:
        change_label, change_state, change_icon = change_row
        rows.append({
            "key": "changes",
            "title": "",
            "value": change_label,
            "state": change_state,
            "icon": change_icon,
        })
    if refund_row:
        refund_label, refund_state, refund_icon = refund_row
        rows.append({
            "key": "refunds",
            "title": "",
            "value": refund_label,
            "state": refund_state,
            "icon": refund_icon,
        })
    if _offer_hold_supported(offer):
        rows.append({
            "key": "hold",
            "title": "",
            "value": "Hold price & space",
            "state": "muted",
            "icon": "hold",
        })
    if carry_on_label:
        carry_state = "muted" if "Includes" in carry_on_label else "negative"
        rows.append({
            "key": "carry_on_bags",
            "title": "",
            "value": carry_on_label,
            "state": carry_state,
            "icon": "carry_on",
        })
    if checked_label:
        checked_state = "muted" if "Includes" in checked_label else "negative"
        rows.append({
            "key": "checked_bags",
            "title": "",
            "value": checked_label,
            "state": checked_state,
            "icon": "checked_bag",
        })
    return rows

def _seat_services_from_offer_and_maps(
    offer: Mapping[str, Any],
    seat_maps: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    seat_services: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def _add_service(service: Mapping[str, Any], *, force_seat: bool = False) -> None:
        service_id = str(service.get("id") or "").strip()
        service_type = str(service.get("type") or service.get("service_type") or "").strip().lower()
        is_seat = force_seat or ("seat" in service_type)
        if not is_seat:
            return
        if service_id and service_id in seen_ids:
            return
        amount = _safe_float(service.get("total_amount") or service.get("amount"), 0.0)
        currency = str(service.get("total_currency") or service.get("currency") or "").strip().upper() or "USD"
        seat_services.append({
            "id": service_id,
            "amount": amount,
            "currency": currency,
            "included": amount <= 0,
        })
        if service_id:
            seen_ids.add(service_id)

    for service in offer.get("available_services") or []:
        _add_service(service)

    for seat_map in seat_maps or []:
        for cabin in seat_map.get("cabins") or []:
            for row in cabin.get("rows") or []:
                for section in row.get("sections") or []:
                    for element in section.get("elements") or []:
                        for service in element.get("available_services") or []:
                            _add_service(service, force_seat=True)

    return seat_services

def _seat_selection_policy(
    offer: Mapping[str, Any],
    *,
    seat_maps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    seat_services = _seat_services_from_offer_and_maps(offer, seat_maps=seat_maps)
    has_any = bool(seat_services)
    has_included = any(bool(item.get("included")) for item in seat_services)
    paid = [item for item in seat_services if float(item.get("amount", 0.0) or 0.0) > 0]
    has_paid = bool(paid)
    can_select_map = has_any and bool(seat_maps)

    paid_from_amount = min((float(item.get("amount", 0.0) or 0.0) for item in paid), default=0.0)
    paid_from_currency = next((str(item.get("currency") or "USD") for item in paid), "USD")
    paid_from_label = f"{paid_from_currency} {paid_from_amount:.2f}" if has_paid else ""

    if not has_any:
        availability_label = "Not offered"
        detail_label = "Seat selection is not offered for this fare."
        status = "not_offered"
    elif has_included and not has_paid:
        availability_label = "Included"
        detail_label = "Seat selection is included for this fare."
        status = "included_only"
    elif has_paid and not has_included:
        availability_label = f"From {paid_from_label} per seat"
        detail_label = f"Seat selection is available for a fee from {paid_from_label} per seat."
        status = "paid_only"
    else:
        availability_label = f"Some included, paid from {paid_from_label}"
        detail_label = f"Some seats are included; others are paid from {paid_from_label} per seat."
        status = "mixed"

    if has_any and not can_select_map:
        detail_label = "Seat pricing exists, but an interactive seat map is not available for this offer."

    return {
        "status": status,
        "is_offered": has_any,
        "can_select_map": can_select_map,
        "has_included": has_included,
        "has_paid": has_paid,
        "paid_from_label": paid_from_label,
        "availability_label": availability_label,
        "detail_label": detail_label,
    }

def _offer_service_compact_labels(
    offer: Mapping[str, Any],
    *,
    seat_policy: dict[str, Any] | None = None,
) -> tuple[str, str]:
    services = offer.get("available_services") or []
    has_bag = False
    has_bag_included = False
    for service in services:
        service_type = str(service.get("type") or service.get("service_type") or "").strip().lower()
        amount = _safe_float(service.get("total_amount") or service.get("amount"), 0.0)
        if "bag" in service_type:
            has_bag = True
            if amount <= 0:
                has_bag_included = True

    if has_bag_included:
        bag_label = "Included"
    elif has_bag:
        bag_label = "Paid add-on"
    else:
        bag_label = "Not offered"

    policy = seat_policy or _seat_selection_policy(offer, seat_maps=[])
    if not policy.get("is_offered"):
        seat_label = "Not offered"
    elif policy.get("has_paid"):
        if policy.get("has_included"):
            seat_label = f"Included / paid from {policy.get('paid_from_label')}"
        else:
            seat_label = f"Paid from {policy.get('paid_from_label')}"
    else:
        seat_label = "Included"
    return bag_label, seat_label

def _review_fare_options_with_selection(
    options: Sequence[Mapping[str, Any]] | None,
    selected_offer_id: str,
) -> list[dict[str, Any]]:
    selected_offer_id = str(selected_offer_id or "").strip()
    selected: list[dict[str, Any]] = []
    for item in options or []:
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        offer_id = str(row.get("offer_id") or "").strip()
        row["is_selected"] = bool(selected_offer_id and offer_id == selected_offer_id)
        selected.append(row)
    if selected and not any(bool(item.get("is_selected")) for item in selected) and len(selected) == 1:
        selected[0]["is_selected"] = True
    return selected

def _review_fare_options_without_selection(
    options: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in options or []:
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        row["is_selected"] = False
        normalized.append(row)
    return normalized

def _review_fare_option_from_offer(
    offer: Mapping[str, Any],
    *,
    selected_offer_id: str,
    fallback_offer: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    base_offer = fallback_offer if isinstance(fallback_offer, Mapping) else offer
    offer_id = str(offer.get("id") or base_offer.get("id") or "").strip()
    if not offer_id:
        return None

    fare_brand = _offer_fare_brand_label(offer) or _offer_fare_brand_label(base_offer)
    cabin_label = _offer_cabin_label(offer) or _offer_cabin_label(base_offer)
    tier_name = fare_brand or "Fare by airline"

    seat_policy = _seat_selection_policy(offer, seat_maps=[])
    bag_label, seat_label = _offer_service_compact_labels(offer, seat_policy=seat_policy)
    conditions = offer.get("conditions") or {}
    change_rule = (conditions.get("change_before_departure") or {}) if isinstance(conditions, Mapping) else {}
    refund_rule = (conditions.get("refund_before_departure") or {}) if isinstance(conditions, Mapping) else {}
    changes_label = _condition_compact_label(change_rule if isinstance(change_rule, Mapping) else None)
    refunds_label = _condition_compact_label(refund_rule if isinstance(refund_rule, Mapping) else None)
    fare_profile_label = _fare_profile_label_from_rules(
        change_rule if isinstance(change_rule, Mapping) else None,
        refund_rule if isinstance(refund_rule, Mapping) else None,
    )
    fare_penalty_hint = _fare_penalty_hint(
        change_rule if isinstance(change_rule, Mapping) else None,
        refund_rule if isinstance(refund_rule, Mapping) else None,
    )
    feature_rows = _offer_feature_rows(offer, seat_policy=seat_policy)

    amount = _safe_float(offer.get("total_amount") or base_offer.get("total_amount"))
    currency = str(offer.get("total_currency") or base_offer.get("total_currency") or "USD").strip() or "USD"
    return {
        "offer_id": offer_id,
        "tier_name": tier_name,
        "fare_brand": fare_brand,
        "cabin_label": cabin_label,
        "price": amount,
        "currency": currency,
        "price_label": _format_price_display(amount, currency),
        "changes_label": changes_label,
        "refunds_label": refunds_label,
        "bags_label": bag_label,
        "seats_label": seat_label,
        "seat_policy": seat_policy,
        "benefits": _offer_tier_benefits(offer, seat_policy=seat_policy),
        "feature_rows": feature_rows,
        "fare_profile_label": fare_profile_label,
        "fare_penalty_hint": fare_penalty_hint,
        "tier_variant_label": "",
        "is_selected": offer_id == selected_offer_id,
    }

def _build_review_fare_options(selected_offer: dict[str, Any]) -> list[dict[str, Any]]:
    if app.config.get("TESTING"):
        return []

    selected_offer_id = str(selected_offer.get("id") or "").strip()
    itinerary_sig = _offer_signature(selected_offer)
    selected_cache_key = f"{REVIEW_FARE_OPTIONS_CACHE_SCHEMA}:{selected_offer_id}" if selected_offer_id else ""
    itinerary_cache_key = (REVIEW_FARE_OPTIONS_CACHE_SCHEMA, itinerary_sig)

    def _selected_only_options(*, cache_by_itinerary: bool) -> list[dict[str, Any]]:
        option = _review_fare_option_from_offer(
            selected_offer,
            selected_offer_id=selected_offer_id,
            fallback_offer=selected_offer,
        )
        if not option:
            return []
        canonical = _review_fare_options_without_selection([option])
        selected_rows = _review_fare_options_with_selection(canonical, selected_offer_id)
        if cache_by_itinerary and itinerary_sig:
            REVIEW_FARE_OPTIONS_BY_ITINERARY_CACHE.set(itinerary_cache_key, canonical)
        if selected_offer_id:
            REVIEW_FARE_OPTIONS_CACHE.set(selected_cache_key, selected_rows)
        return selected_rows

    itinerary_cached_options: list[dict[str, Any]] | None = None
    if itinerary_sig:
        cached_by_itinerary = REVIEW_FARE_OPTIONS_BY_ITINERARY_CACHE.get(itinerary_cache_key)
        if cached_by_itinerary is not None:
            itinerary_cached_options = _review_fare_options_with_selection(cached_by_itinerary, selected_offer_id)
            has_selected = any(str(item.get("offer_id") or "").strip() == selected_offer_id for item in itinerary_cached_options)
            if (not selected_offer_id) or has_selected:
                if selected_offer_id:
                    REVIEW_FARE_OPTIONS_CACHE.set(selected_cache_key, itinerary_cached_options)
                return itinerary_cached_options

    if selected_offer_id:
        cached = REVIEW_FARE_OPTIONS_CACHE.get(selected_cache_key)
        if cached is not None:
            return list(cached)

    payload = _offer_request_payload_from_offer(selected_offer)
    if not payload:
        return itinerary_cached_options or _selected_only_options(cache_by_itinerary=True)

    candidates = DUFF.flight_offers_raw(
        payload,
        supplier_timeout_ms=min(7000, DUFFEL_SUPPLIER_TIMEOUT_MS),
        timeout=max(4.0, min(DUFFEL_HTTP_TIMEOUT, 7.0)),
        fast=True,
    )
    if not candidates:
        if itinerary_cached_options is not None:
            return itinerary_cached_options
        return _selected_only_options(cache_by_itinerary=True)

    matched = [offer for offer in candidates if _offer_signature(offer) == itinerary_sig]
    if not matched:
        if itinerary_cached_options is not None:
            return itinerary_cached_options
        return _selected_only_options(cache_by_itinerary=True)

    # Keep fare comparison aligned with Duffel's display by preferring offers
    # from the same owner/seller as the currently selected offer.
    selected_owner_key = _offer_owner_key(selected_offer)
    owner_matched = [offer for offer in matched if _offer_owner_key(offer) == selected_owner_key]
    if owner_matched:
        matched = owner_matched

    matched_by_id: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for offer in matched:
        offer_id = str((offer or {}).get("id") or "").strip()
        if not offer_id or offer_id in matched_by_id:
            continue
        matched_by_id[offer_id] = offer

    if selected_offer_id and selected_offer_id not in matched_by_id:
        matched_by_id[selected_offer_id] = selected_offer

    # Cap the amount of follow-up work to keep review-page TTFB predictable.
    matched_compact = sorted(
        matched_by_id.values(),
        key=lambda offer: _safe_float((offer or {}).get("total_amount"), 0.0),
    )[:5]

    verified_by_id: dict[str, Mapping[str, Any]] = {}
    if selected_offer_id:
        verified_by_id[selected_offer_id] = selected_offer

    fetch_targets = []
    for offer in matched_compact:
        offer_id = str((offer or {}).get("id") or "").strip()
        if not offer_id or offer_id == selected_offer_id:
            continue
        fetch_targets.append((offer_id, offer))

    if fetch_targets:
        with ThreadPoolExecutor(max_workers=min(4, len(fetch_targets))) as executor:
            future_to_target = {
                executor.submit(
                    DUFF.get_offer,
                    offer_id,
                    return_available_services=True,
                    timeout=max(4.0, min(DUFFEL_HTTP_TIMEOUT, 7.0)),
                    fast=True,
                ): (offer_id, fallback_offer)
                for offer_id, fallback_offer in fetch_targets
            }
            for future in as_completed(future_to_target):
                offer_id, fallback_offer = future_to_target[future]
                try:
                    verified = future.result()
                except Exception:
                    verified = fallback_offer
                if isinstance(verified, Mapping):
                    verified_by_id[offer_id] = verified
                else:
                    verified_by_id[offer_id] = fallback_offer

    options_by_id: dict[str, dict[str, Any]] = {}
    for offer in matched_compact:
        offer_id = str(offer.get("id") or "").strip()
        if not offer_id or offer_id in options_by_id:
            continue
        verified_offer = verified_by_id.get(offer_id) or offer
        option = _review_fare_option_from_offer(
            verified_offer,
            selected_offer_id=selected_offer_id,
            fallback_offer=offer,
        )
        if option is None:
            continue
        options_by_id[offer_id] = option

    options = list(options_by_id.values())
    if not options:
        if itinerary_cached_options is not None:
            return itinerary_cached_options
        return _selected_only_options(cache_by_itinerary=True)

    # Keep one option per visible fare-brand label (prefer currently selected, else cheapest).
    options.sort(
        key=lambda item: (
            float(item.get("price", 0.0) or 0.0),
            str(item.get("tier_name") or ""),
            str(item.get("offer_id") or ""),
        )
    )
    brand_groups: OrderedDict[tuple[str, str], list[dict[str, Any]]] = OrderedDict()
    for item in options:
        brand_key = (
            str(item.get("cabin_label") or "").strip().lower(),
            str(item.get("tier_name") or "").strip().lower(),
        )
        brand_groups.setdefault(brand_key, []).append(item)

    deduped_options: list[dict[str, Any]] = []
    for grouped in brand_groups.values():
        if not grouped:
            continue
        selected_in_group = next(
            (candidate for candidate in grouped if str(candidate.get("offer_id") or "").strip() == selected_offer_id),
            None,
        )
        deduped_options.append(selected_in_group or grouped[0])
    options = deduped_options
    for idx, item in enumerate(options):
        item["is_lowest_price"] = idx == 0
    canonical_options = _review_fare_options_without_selection(options)
    selected_options = _review_fare_options_with_selection(canonical_options, selected_offer_id)
    if itinerary_sig:
        REVIEW_FARE_OPTIONS_BY_ITINERARY_CACHE.set(itinerary_cache_key, canonical_options)
    if selected_offer_id:
        REVIEW_FARE_OPTIONS_CACHE.set(selected_cache_key, selected_options)
    return selected_options

def _merge_unique_offers(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = list(existing)
    seen = {_offer_identity_signature(offer) for offer in merged}
    for offer in incoming:
        sig = _offer_identity_signature(offer)
        if sig in seen:
            continue
        seen.add(sig)
        merged.append(offer)
    return merged

def _collect_best_presentations(raw: list[dict[str, Any]], params: dict[str, Any], *, detailed: bool) -> list[dict[str, Any]]:
    grouped_offers: OrderedDict[tuple[Any, ...], list[dict[str, Any]]] = OrderedDict()
    seen_identity: set[tuple[Any, ...]] = set()
    for offer in raw:
        identity_sig = _offer_identity_signature(offer)
        if identity_sig in seen_identity:
            continue
        seen_identity.add(identity_sig)
        itinerary_sig = _offer_signature(offer)
        grouped_offers.setdefault(itinerary_sig, []).append(offer)

    presentation_best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for itinerary_offers in grouped_offers.values():
        parsed_candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for offer in itinerary_offers:
            parsed = _parse_offer(offer, params, detailed=detailed)
            if parsed:
                parsed_candidates.append((parsed, offer))
        if not parsed_candidates:
            continue

        parsed_candidates.sort(key=lambda item: (
            float(item[0].get("price", 0.0) or 0.0),
            int(item[0].get("_sort_total_duration", 0) or 0),
            int(item[0].get("out_stops", 0) or 0) + int(item[0].get("in_stops", 0) or 0),
        ))
        base_parsed, _ = parsed_candidates[0]

        tiers = [_offer_tier_summary(offer) for _, offer in parsed_candidates]
        tiers.sort(key=lambda tier: (float(tier.get("price", 0.0) or 0.0), tier.get("name") or ""))
        base_parsed["tiers"] = tiers
        if tiers:
            base_parsed["offer_id"] = tiers[0].get("offer_id") or base_parsed.get("offer_id")
            base_parsed["price"] = float(tiers[0].get("price", 0.0) or base_parsed.get("price", 0.0))
            base_parsed["currency"] = tiers[0].get("currency") or base_parsed.get("currency")

        display_sig = _presentation_signature(base_parsed)
        current = presentation_best.get(display_sig)
        if current is None or _prefer_display_offer(base_parsed, current):
            presentation_best[display_sig] = base_parsed

    return list(presentation_best.values())

def _recommended_results_are_too_narrow(flights: list[dict[str, Any]], limit: int = RECOMMENDED_RESULTS_LIMIT) -> bool:
    min_expected = max(4, min(limit, RECOMMENDED_ALT_MIN_RESULTS))
    if len(flights) < min_expected:
        return True

    sample = flights[:min(len(flights), limit)]
    counts = Counter((flight.get("_airline_key") or "UNKNOWN") for flight in sample)
    counts.pop("UNKNOWN", None)
    if not counts:
        return False

    dominant_count = counts.most_common(1)[0][1]
    dominant_share = dominant_count / float(len(sample))
    return dominant_share >= RECOMMENDED_ALT_DOMINANCE_THRESHOLD or len(counts) < RECOMMENDED_ALT_MIN_UNIQUE_AIRLINES

def _expand_recommended_offers_if_needed(
    params: dict[str, Any],
    raw: list[dict[str, Any]],
    ranked_flights: list[dict[str, Any]],
    *,
    detailed: bool,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    # Fast mode already trades depth for speed; skip extra recommendation
    # expansion rounds that can add several network-bound seconds.
    if not detailed:
        return raw
    if not ranked_flights or not _recommended_results_are_too_narrow(ranked_flights):
        return raw

    expanded = list(raw)
    payload = _flight_offer_query(params, detailed=detailed)
    base_supplier_timeout = DUFFEL_SUPPLIER_TIMEOUT_MS if detailed else max(3000, min(int(LIGHT_REQUEST_TIMEOUT * 1000) - 1500, DUFFEL_SUPPLIER_TIMEOUT_MS))
    sample = ranked_flights[:min(len(ranked_flights), RECOMMENDED_RESULTS_LIMIT)]
    airline_counts = Counter((flight.get("_airline_key") or "UNKNOWN") for flight in sample)
    dominant_share = 0.0
    if sample and airline_counts:
        dominant_share = airline_counts.most_common(1)[0][1] / float(len(sample))
    dynamic_rounds = RECOMMENDED_ALT_FETCH_ROUNDS
    if dominant_share >= RECOMMENDED_ALT_DOMINANCE_THRESHOLD:
        dynamic_rounds += 1
    if len(sample) < RECOMMENDED_ALT_MIN_RESULTS:
        dynamic_rounds += 1

    for round_idx in range(max(1, dynamic_rounds)):
        supplier_timeout_ms = min(22000, base_supplier_timeout + ((round_idx + 1) * 4000))
        alt_raw = DUFF.flight_offers_raw(
            payload,
            supplier_timeout_ms=supplier_timeout_ms,
            timeout=DUFFEL_HTTP_TIMEOUT,
            fast=not detailed,
            force_refresh=force_refresh,
        )
        if not alt_raw:
            continue
        expanded = _merge_unique_offers(expanded, alt_raw)
        if len(expanded) >= SEARCH_RESULTS_FETCH_LIMIT * 2:
            break

    return expanded

def _carrier_meta(segments: list[dict[str, Any]]) -> tuple[list[str], str, str]:
    codes = _marketing_carrier_codes_for_segments(segments)
    return codes, _carrier_code_label(codes), _carrier_label(codes)


def _carrier_logo_url(carrier: Any) -> str | None:
    if not isinstance(carrier, dict):
        return None
    for key in ("logo_symbol_url", "logo_lockup_url"):
        raw = str(carrier.get(key) or "").strip()
        if raw.startswith("https://") or raw.startswith("http://"):
            return raw
    return None


def _first_segment_marketing_logo(segments: list[dict[str, Any]]) -> str | None:
    if not segments:
        return None
    return _carrier_logo_url((segments[0].get("marketing_carrier") or {}))


def _parse_offer(offer: dict[str, Any], params: dict[str, Any], detailed: bool) -> dict[str, Any] | None:
    try:
        slices = offer.get("slices") or []
        if not slices:
            return None

        total_price = _safe_float(offer.get("total_amount"))
        currency = offer.get("total_currency") or "USD"
        passengers = offer.get("passengers") or []
        passenger_count = max(1, len(passengers) or int(params.get("passengers", 1)))
        price_per_pax = round(total_price / passenger_count, 2)

        def dep_iso(segment: dict[str, Any]) -> str | None:
            return segment.get("departing_at") or (segment.get("departure") or {}).get("at")

        def arr_iso(segment: dict[str, Any]) -> str | None:
            return segment.get("arriving_at") or (segment.get("arrival") or {}).get("at")

        def dep_code(segment: dict[str, Any]) -> str:
            return (((segment.get("origin") or {}).get("iata_code")) or ((segment.get("departure") or {}).get("iataCode")) or "").strip().upper()

        def arr_code(segment: dict[str, Any]) -> str:
            return (((segment.get("destination") or {}).get("iata_code")) or ((segment.get("arrival") or {}).get("iataCode")) or "").strip().upper()

        def segment_number(segment: dict[str, Any]) -> str:
            marketing = segment.get("marketing_carrier") or {}
            code = (marketing.get("iata_code") or "").strip().upper()
            num = str(segment.get("marketing_carrier_flight_number") or segment.get("number") or "").strip()
            return f"{code}{num}" if code and num else num

        def to_amadeus_segment(segment: dict[str, Any]) -> dict[str, Any]:
            marketing = segment.get("marketing_carrier") or {}
            operating = segment.get("operating_carrier") or {}
            return {
                "carrierCode": (marketing.get("iata_code") or operating.get("iata_code") or "").strip().upper(),
                "number": segment_number(segment).replace((marketing.get("iata_code") or "").strip().upper(), "", 1) if segment_number(segment) else "",
                "departure": {"iataCode": dep_code(segment), "at": dep_iso(segment)},
                "arrival": {"iataCode": arr_code(segment), "at": arr_iso(segment)},
                "operating": {"carrierCode": (operating.get("iata_code") or marketing.get("iata_code") or "").strip().upper()},
            }

        slice_meta: list[dict[str, Any]] = []
        all_carrier_codes: list[str] = []
        for idx, slice_data in enumerate(slices):
            segs = slice_data.get("segments") or []
            if not segs:
                continue
            s_depart = dep_iso(segs[0])
            s_arrive = arr_iso(segs[-1])
            s_stops = max(0, len(segs) - 1)
            s_via = _segment_via_codes(segs)
            s_codes, s_code_label, s_airline = _carrier_meta(segs)
            raw_layovers = _build_layovers([to_amadeus_segment(s) for s in segs], detailed=True)
            s_layovers = raw_layovers if detailed else []
            s_duration = _display_duration_minutes(slice_data.get("duration"), s_depart, s_arrive)
            s_duration_floor = _slice_duration_floor_minutes(segs, raw_layovers)
            if s_duration_floor > 0 and (s_duration <= 0 or s_duration < s_duration_floor):
                s_duration = s_duration_floor
            s_origin_code = dep_code(segs[0])
            s_dest_code = arr_code(segs[-1])
            s_logo = _first_segment_marketing_logo(segs)
            all_carrier_codes.extend(s_codes)
            slice_meta.append(
                {
                    "index": idx,
                    "label": f"Leg {idx + 1}",
                    "depart_at": s_depart,
                    "arrive_at": s_arrive,
                    "stops": s_stops,
                    "duration_min": s_duration,
                    "via_codes": s_via,
                    "carrier_codes": s_codes,
                    "carrier_code_label": s_code_label,
                    "airline": s_airline,
                    "layovers": s_layovers,
                    "origin_code": s_origin_code,
                    "dest_code": s_dest_code,
                    "origin_name": _airport_display_name_local(s_origin_code),
                    "dest_name": _airport_display_name_local(s_dest_code),
                    "airline_logo_url": s_logo,
                    "operating_note": _slice_operating_note(segs),
                }
            )
        if not slice_meta:
            return None

        out = slice_meta[0]
        out_depart_at = out.get("depart_at")
        out_arrive_at = out.get("arrive_at")
        out_stops = out.get("stops")
        out_duration_min = out.get("duration_min")
        out_via_codes = out.get("via_codes") or []
        out_carrier_codes = out.get("carrier_codes") or []
        out_carrier_code = out.get("carrier_code_label")
        out_airline = out.get("airline")
        out_layovers = out.get("layovers") or []
        out_origin_code = out.get("origin_code") or ""
        out_dest_code = out.get("dest_code") or ""
        out_origin_name = out.get("origin_name") or ""
        out_dest_name = out.get("dest_name") or ""

        in_meta = slice_meta[1] if len(slice_meta) > 1 else None
        in_airline = in_meta.get("airline") if in_meta else None
        in_carrier_code = in_meta.get("carrier_code_label") if in_meta else None
        in_depart_at = in_meta.get("depart_at") if in_meta else None
        in_arrive_at = in_meta.get("arrive_at") if in_meta else None
        in_stops = in_meta.get("stops") if in_meta else None
        in_duration_min = in_meta.get("duration_min") if in_meta else None
        in_layovers = in_meta.get("layovers") if in_meta else []
        in_via_codes: list[str] = (in_meta.get("via_codes") if in_meta else []) or []
        in_carrier_codes: list[str] = (in_meta.get("carrier_codes") if in_meta else []) or []
        in_origin_code = in_meta.get("origin_code") if in_meta else ""
        in_dest_code = in_meta.get("dest_code") if in_meta else ""
        in_origin_name = in_meta.get("origin_name") if in_meta else ""
        in_dest_name = in_meta.get("dest_name") if in_meta else ""

        total_trip_duration = sum(int(item.get("duration_min") or 0) for item in slice_meta)
        total_stop_count = sum(int(item.get("stops") or 0) for item in slice_meta)
        first_depart_at = slice_meta[0].get("depart_at")
        final_arrive_at = slice_meta[-1].get("arrive_at")
        all_carrier_codes = _unique_preserve([*all_carrier_codes, *out_carrier_codes, *in_carrier_codes])

        owner_logo = _carrier_logo_url(offer.get("owner"))
        out_leg_logo = out.get("airline_logo_url") or owner_logo
        in_leg_logo = in_meta.get("airline_logo_url") if in_meta else None
        if not in_leg_logo and in_meta:
            in_leg_logo = owner_logo
        headline_logo = owner_logo or out_leg_logo
        tier_summary = _offer_tier_summary(offer)
        carry_on_label, checked_bag_label = _duffel_like_baggage_labels(offer)
        conditions = offer.get("conditions") or {}
        change_rule = (conditions.get("change_before_departure") or {}) if isinstance(conditions, Mapping) else {}
        refund_rule = (conditions.get("refund_before_departure") or {}) if isinstance(conditions, Mapping) else {}
        change_label = _duffel_like_change_label(change_rule if isinstance(change_rule, Mapping) else None)
        refund_label = _duffel_like_refund_label(refund_rule if isinstance(refund_rule, Mapping) else None)
        hold_ok = _offer_hold_supported(offer)
        connection_airports = _unique_preserve([*out_via_codes, *in_via_codes])

        return {
            "price": total_price,
            "price_per_pax": price_per_pax,
            "passenger_count": passenger_count,
            "currency": currency,
            "selection_token": _offer_selection_token(offer),
            "offer_id": offer.get("id"),
            "expires_at": offer.get("expires_at"),
            "airline_logo_url": headline_logo,
            "out_airline_logo_url": out_leg_logo,
            "in_airline_logo_url": in_leg_logo,
            "airline_summary": _carrier_label(all_carrier_codes),
            "airline_code_summary": _carrier_code_label(all_carrier_codes),
            "airline_mix_label": _offer_airline_mix_label(slice_meta) or _airline_mix_label(out_carrier_codes, in_carrier_codes),
            "out_airline": out_airline,
            "out_airline_code": out_carrier_code,
            "out_depart_at": out_depart_at,
            "out_arrive_at": out_arrive_at,
            "out_duration_min": out_duration_min,
            "out_stops": out_stops,
            "out_layovers": out_layovers,
            "out_origin_code": out_origin_code,
            "out_dest_code": out_dest_code,
            "out_origin_name": out_origin_name,
            "out_dest_name": out_dest_name,
            "in_origin_code": in_origin_code,
            "in_dest_code": in_dest_code,
            "in_origin_name": in_origin_name,
            "in_dest_name": in_dest_name,
            "in_airline": in_airline,
            "in_airline_code": in_carrier_code,
            "in_depart_at": in_depart_at,
            "in_arrive_at": in_arrive_at,
            "in_duration_min": in_duration_min,
            "in_stops": in_stops,
            "in_layovers": in_layovers,
            "first_depart_at": first_depart_at,
            "final_arrive_at": final_arrive_at,
            "total_duration_min": total_trip_duration,
            "total_stop_count": total_stop_count,
            "fare_name": tier_summary.get("name"),
            "fare_brand": tier_summary.get("fare_brand"),
            "cabin_label": tier_summary.get("cabin_label"),
            "fare_features": tier_summary.get("features") or [],
            "carry_on_label": carry_on_label,
            "checked_bag_label": checked_bag_label,
            "change_label": change_label[0] if change_label else "",
            "refund_label": refund_label[0] if refund_label else "",
            "fare_rows": _offer_fare_rows(
                carry_on_label,
                checked_bag_label,
                change_label[0] if change_label else None,
                refund_label[0] if refund_label else None,
                hold_ok,
            ),
            "fare_profile_label": _fare_profile_label_from_rules(
                change_rule if isinstance(change_rule, Mapping) else None,
                refund_rule if isinstance(refund_rule, Mapping) else None,
            ),
            "fare_penalty_hint": _fare_penalty_hint(
                change_rule if isinstance(change_rule, Mapping) else None,
                refund_rule if isinstance(refund_rule, Mapping) else None,
            ),
            "hold_supported": hold_ok,
            "connection_airports": connection_airports,
            "is_multicity": len(slice_meta) > 2,
            "_slice_meta": slice_meta,
            "_airline_key": "|".join(sorted(all_carrier_codes)) or "UNKNOWN",
            "_out_via_codes": out_via_codes,
            "_in_via_codes": in_via_codes,
            "_sort_total_duration": total_trip_duration,
        }
    except Exception as exc:
        print("DUFFEL PARSE ERROR:", repr(exc))
        return None


def _time_of_day_label(iso_str: str | None) -> str:
    if not iso_str:
        return "Anytime"
    try:
        hour = _dt(iso_str).hour
    except Exception:
        return "Anytime"
    if 0 <= hour < 5:
        return "Red-eye departure"
    if 5 <= hour < 8:
        return "Early morning departure"
    if 8 <= hour < 12:
        return "Morning departure"
    if 12 <= hour < 15:
        return "Midday departure"
    if 15 <= hour < 18:
        return "Afternoon departure"
    if 18 <= hour < 22:
        return "Evening departure"
    return "Late night departure"

def _stop_label(stops: int | None) -> str:
    count = max(0, int(stops or 0))
    if count == 0:
        return "Nonstop"
    if count == 1:
        return "1 stop"
    return f"{count} stops"

def _total_stop_label(out_stops: int | None, in_stops: int | None) -> str:
    total = max(0, int(out_stops or 0)) + max(0, int(in_stops or 0))
    if in_stops is None:
        return _stop_label(total)
    if total == 0:
        return "Nonstop both ways"
    if total == 1:
        return "1 stop total"
    return f"{total} stops total"

def _via_summary(via_codes: list[str], stops: int | None) -> str:
    stop_count = max(0, int(stops or 0))
    if stop_count == 0 or not via_codes:
        return "Direct route"
    if len(via_codes) == 1:
        return f"Via {via_codes[0]}"
    if len(via_codes) == 2:
        return f"Via {via_codes[0]} and {via_codes[1]}"
    return f"Via {via_codes[0]} + {len(via_codes) - 1} more"

def _layover_quality(minutes: int, overnight: bool = False) -> tuple[str, str]:
    if overnight:
        return "Overnight layover", "warn"
    if minutes < 60:
        return "Quick connection", "warn"
    if minutes <= 180:
        return "Comfortable layover", "good"
    if minutes <= 360:
        return "Long layover", "neutral"
    return "Extended layover", "warn"

def _dominant_layover_chip(layovers: list[dict[str, Any]]) -> dict[str, str] | None:
    if not layovers:
        return None
    prioritized = sorted(
        layovers,
        key=lambda layover: (
            0 if layover.get("overnight") else 1,
            int(layover.get("minutes", 0) or 0),
        ),
    )
    label, tone = _layover_quality(
        int(prioritized[0].get("minutes", 0) or 0),
        bool(prioritized[0].get("overnight")),
    )
    return {"label": label, "tone": tone}

def _build_segment_display(
    *,
    label: str,
    origin: str,
    destination: str,
    airline: str | None,
    airline_code: str | None,
    airline_note: str | None = None,
    airline_logo_url: str | None = None,
    depart_at: str | None,
    arrive_at: str | None,
    duration_min: int | None,
    stops: int | None,
    layovers: list[dict[str, Any]],
    via_codes: list[str],
) -> dict[str, Any]:
    stop_count = max(0, int(stops or 0))
    layover_items: list[dict[str, Any]] = []
    layover_count = len(layovers or [])
    for index, layover in enumerate(layovers or [], start=1):
        minutes = int(layover.get("minutes", 0) or 0)
        quality_label, tone = _layover_quality(minutes, bool(layover.get("overnight")))
        layover_items.append({
            **layover,
            "duration_label": minutes_to_hm(minutes),
            "quality_label": quality_label,
            "tone": tone,
            "line_position_pct": round((index * 100.0) / (layover_count + 1), 2),
        })
    if stop_count > 0 and not layover_items:
        # Some providers return via airport codes without full layover blocks.
        # Build minimal timeline stops so "1 stop / 2+ stops" still renders visibly.
        via_fallback = [str(code or "").strip().upper() for code in (via_codes or []) if str(code or "").strip()]
        fallback_count = max(1, stop_count)
        for index in range(1, fallback_count + 1):
            code = via_fallback[index - 1] if index - 1 < len(via_fallback) else "Stop"
            layover_items.append({
                "code": code,
                "name": _airport_display_name_local(code) if code != "Stop" else "Connection stop",
                "duration_label": "Details unavailable",
                "quality_label": "Connection details unavailable",
                "tone": "neutral",
                "line_position_pct": round((index * 100.0) / (fallback_count + 1), 2),
            })

    narrative_bits = [_time_of_day_label(depart_at), _stop_label(stop_count).lower()]
    if stop_count > 0:
        narrative_bits.append(_via_summary(via_codes, stop_count).lower())

    return {
        "label": label,
        "origin": origin,
        "origin_name": _airport_display_name_local(origin),
        "destination": destination,
        "destination_name": _airport_display_name_local(destination),
        "airline": airline or "Unknown airline",
        "airline_code": airline_code or "",
        "airline_note": (airline_note or "").strip(),
        "airline_logo_url": airline_logo_url,
        "depart_time": _fmt_clock(depart_at),
        "depart_day": _fmt_day_short(depart_at),
        "arrive_time": _fmt_clock(arrive_at),
        "arrive_day": _fmt_day_short(arrive_at),
        "duration": minutes_to_hm(int(duration_min or 0)),
        "stops_label": _stop_label(stop_count),
        "route_chip": _via_summary(via_codes, stop_count),
        "time_chip": _time_of_day_label(depart_at),
        "layovers": layover_items,
        "narrative": " • ".join(narrative_bits),
    }

def _flight_story(flight: dict[str, Any], segments: list[dict[str, Any]]) -> str:
    if not segments:
        return "Clear flight breakdown unavailable."

    if len(segments) > 2 or flight.get("is_multicity"):
        first = segments[0]
        last = segments[-1]
        total_stops = sum(len(seg.get("layovers") or []) for seg in segments)
        hop_label = f"{len(segments)} legs"
        stop_label = "nonstop legs" if total_stops == 0 else f"{total_stops} stops total"
        return f"{hop_label}, {first['origin']} to {last['destination']}, {stop_label}"

    total_stops = _total_stop_label(flight.get("out_stops"), flight.get("in_stops"))
    parts = [
        f"{segments[0]['time_chip']} outbound",
        total_stops.lower(),
    ]
    if len(segments) > 1:
        parts.insert(1, f"{segments[1]['time_chip'].lower()} return")

    all_layovers = [*flight.get("out_layovers", []), *flight.get("in_layovers", [])]
    dominant = _dominant_layover_chip(all_layovers)
    if dominant:
        parts.append(dominant["label"].lower())

    return ", ".join(parts)

def _decorate_flights_for_display(flights: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    for flight in flights:
        segments: list[dict[str, Any]] = []
        slice_meta = flight.get("_slice_meta") or []
        if slice_meta:
            default_labels = []
            if len(slice_meta) == 1:
                default_labels = ["Flight"]
            elif len(slice_meta) == 2 and params.get("trip_type") != "multicity":
                default_labels = ["Outbound", "Return"]
            else:
                default_labels = [f"Leg {idx + 1}" for idx in range(len(slice_meta))]
            for idx, meta in enumerate(slice_meta):
                segments.append(
                    _build_segment_display(
                        label=default_labels[idx],
                        origin=(meta.get("origin_code") or "").strip(),
                        destination=(meta.get("dest_code") or "").strip(),
                        airline=meta.get("airline"),
                        airline_code=meta.get("carrier_code_label"),
                        airline_note=meta.get("operating_note"),
                        airline_logo_url=meta.get("airline_logo_url"),
                        depart_at=meta.get("depart_at"),
                        arrive_at=meta.get("arrive_at"),
                        duration_min=meta.get("duration_min"),
                        stops=meta.get("stops"),
                        layovers=meta.get("layovers") or [],
                        via_codes=meta.get("via_codes") or [],
                    )
                )
        else:
            out_o = (flight.get("out_origin_code") or params.get("origin") or "").strip()
            out_d = (flight.get("out_dest_code") or params.get("destination") or "").strip()
            segments = [
                _build_segment_display(
                    label="Outbound",
                    origin=out_o,
                    destination=out_d,
                    airline=flight.get("out_airline"),
                    airline_code=flight.get("out_airline_code"),
                    airline_note="",
                    airline_logo_url=flight.get("out_airline_logo_url"),
                    depart_at=flight.get("out_depart_at"),
                    arrive_at=flight.get("out_arrive_at"),
                    duration_min=flight.get("out_duration_min"),
                    stops=flight.get("out_stops"),
                    layovers=flight.get("out_layovers") or [],
                    via_codes=flight.get("_out_via_codes") or [],
                )
            ]
            if flight.get("in_depart_at"):
                in_o = (flight.get("in_origin_code") or params.get("destination") or "").strip()
                in_d = (flight.get("in_dest_code") or params.get("origin") or "").strip()
                segments.append(
                    _build_segment_display(
                        label="Return",
                        origin=in_o,
                        destination=in_d,
                        airline=flight.get("in_airline"),
                        airline_code=flight.get("in_airline_code"),
                        airline_note="",
                        airline_logo_url=flight.get("in_airline_logo_url"),
                        depart_at=flight.get("in_depart_at"),
                        arrive_at=flight.get("in_arrive_at"),
                        duration_min=flight.get("in_duration_min"),
                        stops=flight.get("in_stops"),
                        layovers=flight.get("in_layovers") or [],
                        via_codes=flight.get("_in_via_codes") or [],
                    )
                )

        flight["segments_ui"] = segments
        flight["trip_story"] = _flight_story(flight, segments)

    return flights


def _score_departure_quality(depart_iso: str | None) -> float:
    if not depart_iso:
        return 0.0
    try:
        hour = _dt(depart_iso).hour
    except Exception:
        return 0.0
    if 6 <= hour <= 10:
        return 12.0
    if 10 < hour <= 18:
        return 8.0
    if 18 < hour <= 22:
        return 3.0
    if 0 <= hour < 5:
        return -10.0
    return 0.0

def _score_price_position(price: float, min_price: float, p90_price: float) -> float:
    if price <= 0:
        return 0.0
    if p90_price <= min_price:
        return 25.0
    rel = (price - min_price) / max(1.0, (p90_price - min_price))
    rel = max(0.0, min(1.3, rel))
    return 30.0 * (1.0 - rel)

def _score_duration_position(duration: int, min_duration: int, p90_duration: int) -> float:
    if duration <= 0:
        return 0.0
    if p90_duration <= min_duration:
        return 18.0
    rel = (duration - min_duration) / max(1.0, (p90_duration - min_duration))
    rel = max(0.0, min(1.3, rel))
    return 20.0 * (1.0 - rel)

def _value_adjusted_cost(price: float, duration: int, max_duration: int, time_value_per_min: float) -> float:
    """Effective cost after accounting for time savings. Lower = better value."""
    time_saved = max(0, max_duration - duration)
    return price - time_saved * time_value_per_min

def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    idx = int(round((len(vals) - 1) * p))
    idx = max(0, min(len(vals) - 1, idx))
    return vals[idx]

def _apply_recommended_scoring(
    flights: list[dict[str, Any]], *, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    if not flights:
        return flights

    traveler_context = (params or {}).get("traveler_context") or {}
    prefers_longer_layover = bool(traveler_context.get("prefers_longer_layover"))
    prefers_shorter_layover = bool(
        traveler_context.get("prefers_shorter_layover") or traveler_context.get("has_senior_or_child_companion")
    )

    prices = [float(f.get("price", 0) or 0) for f in flights if float(f.get("price", 0) or 0) > 0]
    durations = [int(f.get("_sort_total_duration", 0) or 0) for f in flights if int(f.get("_sort_total_duration", 0) or 0) > 0]
    if not prices:
        return flights

    min_price = min(prices)
    max_price = max(prices)
    price_range = max(1.0, max_price - min_price)
    min_duration = min(durations) if durations else 0
    max_duration = max(durations) if durations else 0
    duration_range = max(1, max_duration - min_duration)
    p90_duration = int(_percentile([float(x) for x in durations], 0.9)) if durations else max_duration

    # Conservative fixed time value: $20/hr = $0.333/min.
    # Deliberately low — most leisure travellers would rather save $100 than
    # 5 hours. Adaptive rates produced absurd results when price and duration
    # were inversely correlated (e.g. cheap slow vs expensive fast routes).
    TIME_VALUE_PER_MIN = 20.0 / 60.0  # $0.333/min

    # Compute effective cost for every real flight, then normalise from those
    # actual values (not from a phantom min_price+min_duration combo).
    ecs = []
    for f in flights:
        p = float(f.get("price", 0) or 0)
        d = int(f.get("_sort_total_duration", 0) or 0)
        ecs.append(_value_adjusted_cost(p, d, max_duration, TIME_VALUE_PER_MIN) if p else float("inf"))

    valid_ecs = [e for e in ecs if e != float("inf")]
    best_ec  = min(valid_ecs) if valid_ecs else 0.0
    worst_ec = max(valid_ecs) if valid_ecs else 1.0
    ec_range = max(1.0, worst_ec - best_ec)

    for f, ec in zip(flights, ecs):
        price    = float(f.get("price", 0) or 0)
        duration = int(f.get("_sort_total_duration", 0) or 0)
        stops    = int(f.get("out_stops", 0) or 0) + int(f.get("in_stops", 0) or 0)

        if not price:
            f["_recommended_score"] = -999.0
            continue

        # ── Price position (0–60 pts) — dominant factor ───────────────
        # Pure price position within the result set, no time-value adjustment.
        # A flight $630 more expensive can never overcome this gap via speed.
        price_pos = (price - min_price) / price_range
        price_score = 60.0 * max(0.0, 1.0 - price_pos)

        # ── Value-adjusted cost tiebreaker (0–10 pts) ─────────────────
        # Within a similar price band, prefer faster flights. Uses the
        # conservative $20/hr rate so time savings don't dominate.
        ec_score = 10.0 * max(0.0, 1.0 - (ec - best_ec) / ec_range)

        # ── Stops (0–18 pts) ─────────────────────────────────────────
        stop_score = 18.0 if stops == 0 else (6.0 if stops == 1 else 0.0)

        # ── Departure quality (0–8 pts) ──────────────────────────────
        depart_score = _score_departure_quality(f.get("out_depart_at"))
        if f.get("in_depart_at"):
            depart_score = (depart_score + _score_departure_quality(f.get("in_depart_at"))) / 2
        depart_score = min(depart_score, 8.0)

        # ── Layover quality ──────────────────────────────────────────
        layover_score = 0.0
        layover_minutes = [
            int(x.get("minutes", 0) or 0)
            for x in [*(f.get("out_layovers") or []), *(f.get("in_layovers") or [])]
        ]
        if layover_minutes:
            shortest, longest = min(layover_minutes), max(layover_minutes)
            if shortest < 45:   layover_score -= 10.0
            elif shortest < 60: layover_score -= 5.0
            elif 75 <= shortest <= 180: layover_score += 3.0
            if longest > 300:   layover_score -= 6.0
            elif longest > 240: layover_score -= 3.0

            # Soft nudges from explicit traveler preferences. The <45min
            # missed-connection penalty above still applies regardless —
            # that's a real risk, not a preference.
            if prefers_longer_layover and 90 <= longest <= 240:
                layover_score += 4.0
            if prefers_shorter_layover and longest > 150:
                layover_score -= 4.0

        # ── Duration bonus (0–4 pts) — only for meaningfully faster flights
        # Cap is intentionally tiny so a faster-but-expensive flight can't
        # leapfrog over a much cheaper one just because it's 30 min quicker.
        duration_bonus = 0.0
        if duration < p90_duration:
            saved_vs_p90 = p90_duration - duration
            if saved_vs_p90 >= 30:
                duration_bonus = min(4.0, saved_vs_p90 / 60.0 * 4.0)

        score = price_score + ec_score + stop_score + depart_score + layover_score + duration_bonus
        f["_recommended_score"] = round(score, 2)

    flights.sort(
        key=lambda x: (
            -(x.get("_recommended_score", 0)),
            x.get("price", float("inf")),
            x.get("_sort_total_duration", float("inf")),
        )
    )
    return flights

def _presentation_signature(flight: dict[str, Any]) -> tuple[Any, ...]:
    return (
        flight.get("out_depart_at"),
        flight.get("out_arrive_at"),
        tuple(flight.get("_out_via_codes") or []),
        int(flight.get("out_stops", 0) or 0),
        flight.get("in_depart_at"),
        flight.get("in_arrive_at"),
        tuple(flight.get("_in_via_codes") or []),
        int(flight.get("in_stops", 0) or 0),
        flight.get("_airline_key") or "UNKNOWN",
    )

def _prefer_display_offer(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    candidate_price = float(candidate.get("price", 0) or 0)
    current_price = float(current.get("price", 0) or 0)
    if candidate_price != current_price:
        return candidate_price < current_price

    candidate_duration = int(candidate.get("_sort_total_duration", 0) or 0)
    current_duration = int(current.get("_sort_total_duration", 0) or 0)
    if candidate_duration != current_duration:
        return candidate_duration < current_duration

    candidate_stops = int(candidate.get("out_stops", 0) or 0) + int(candidate.get("in_stops", 0) or 0)
    current_stops = int(current.get("out_stops", 0) or 0) + int(current.get("in_stops", 0) or 0)
    return candidate_stops < current_stops

def _diversify_recommended_flights(flights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique_airlines = {f.get("_airline_key") or "UNKNOWN" for f in flights}
    if len(flights) < 4 or len(unique_airlines) < 2:
        return flights

    remaining = list(flights)
    diversified: list[dict[str, Any]] = []
    airline_counts: Counter[str] = Counter()

    while remaining:
        window = remaining[:min(RECOMMENDED_DIVERSITY_WINDOW, len(remaining))]
        best_idx = 0
        best_score: float | None = None

        for idx, flight in enumerate(window):
            airline_key = flight.get("_airline_key") or "UNKNOWN"
            adjusted_score = float(flight.get("_recommended_score", 0) or 0)
            adjusted_score -= airline_counts[airline_key] * RECOMMENDED_REPEAT_AIRLINE_PENALTY

            if diversified and diversified[-1].get("_airline_key") == airline_key:
                adjusted_score -= RECOMMENDED_CONSECUTIVE_REPEAT_PENALTY

            if best_score is None or adjusted_score > best_score or (
                adjusted_score == best_score and idx < best_idx
            ):
                best_idx = idx
                best_score = adjusted_score

        chosen = remaining.pop(best_idx)
        airline_counts[chosen.get("_airline_key") or "UNKNOWN"] += 1
        diversified.append(chosen)

    return diversified

def _recommended_airline_cap(unique_airlines: int, limit: int) -> int | None:
    if unique_airlines <= 1 or limit <= 0:
        return None
    target_unique = max(2, min(unique_airlines, 6))
    return max(2, ((limit + target_unique - 1) // target_unique) + 1)

def _rebalance_recommended_flights(flights: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(flights) <= 1 or limit <= 0:
        return flights

    pool_size = min(len(flights), max(limit * RECOMMENDED_REBALANCE_SCAN_MULTIPLIER, RECOMMENDED_DIVERSITY_WINDOW))
    pool = list(flights[:pool_size])
    tail = list(flights[pool_size:])

    unique_airlines = len({flight.get("_airline_key") or "UNKNOWN" for flight in pool})
    cap = _recommended_airline_cap(unique_airlines, limit)
    if cap is None:
        return flights

    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()

    for flight in pool:
        airline_key = flight.get("_airline_key") or "UNKNOWN"
        if counts[airline_key] < cap:
            selected.append(flight)
            counts[airline_key] += 1
        else:
            deferred.append(flight)

    return selected + deferred + tail

def _ensure_recommended_airline_variety(flights: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(flights) <= 1 or limit <= 0:
        return flights

    target_unique = min(RECOMMENDED_ALT_MIN_UNIQUE_AIRLINES, max(1, len({f.get("_airline_key") or "UNKNOWN" for f in flights})))
    if target_unique <= 1:
        return flights

    head = list(flights[:limit])
    tail = list(flights[limit:])
    seen = {f.get("_airline_key") or "UNKNOWN" for f in head}
    if len(seen) >= target_unique:
        return flights

    missing = target_unique - len(seen)
    replacements: list[dict[str, Any]] = []
    used_indices: set[int] = set()
    for idx, candidate in enumerate(tail):
        key = candidate.get("_airline_key") or "UNKNOWN"
        if key in seen:
            continue
        seen.add(key)
        used_indices.add(idx)
        replacements.append(candidate)
        if len(replacements) >= missing:
            break

    if not replacements:
        return flights

    # Replace from the tail of head where repeated-airline penalty is highest.
    head_counts: Counter[str] = Counter((item.get("_airline_key") or "UNKNOWN") for item in head)
    removable_indices = [
        idx for idx in range(len(head) - 1, -1, -1)
        if head_counts[(head[idx].get("_airline_key") or "UNKNOWN")] > 1
    ]
    if len(removable_indices) < len(replacements):
        return flights

    for repl, remove_idx in zip(replacements, removable_indices):
        head[remove_idx] = repl

    remaining_tail = [candidate for idx, candidate in enumerate(tail) if idx not in used_indices]
    return head + remaining_tail

def _enforce_airline_constraints(
    flights: list[dict[str, Any]],
    *,
    top_n: int,
    min_unique: int,
    max_per_airline: int,
) -> list[dict[str, Any]]:
    if len(flights) <= 1 or top_n <= 0:
        return flights

    head = list(flights[:top_n])
    tail = list(flights[top_n:])
    if not head:
        return flights

    all_airlines = {(flight.get("_airline_key") or "UNKNOWN") for flight in flights}
    target_unique = min(min_unique, len(all_airlines))
    if target_unique <= 1:
        return flights

    head_counts: Counter[str] = Counter((item.get("_airline_key") or "UNKNOWN") for item in head)
    if len(head_counts) < target_unique:
        seen = set(head_counts.keys())
        needed = target_unique - len(head_counts)
        replacements: list[tuple[int, dict[str, Any]]] = []
        for idx, candidate in enumerate(tail):
            key = candidate.get("_airline_key") or "UNKNOWN"
            if key in seen:
                continue
            seen.add(key)
            replacements.append((idx, candidate))
            if len(replacements) >= needed:
                break
        removable_indices = [
            idx for idx in range(len(head) - 1, -1, -1)
            if head_counts[(head[idx].get("_airline_key") or "UNKNOWN")] > 1
        ]
        if len(replacements) and len(removable_indices) >= len(replacements):
            used_tail_idx: set[int] = set()
            for (tail_idx, candidate), remove_idx in zip(replacements, removable_indices):
                old_key = head[remove_idx].get("_airline_key") or "UNKNOWN"
                head_counts[old_key] -= 1
                new_key = candidate.get("_airline_key") or "UNKNOWN"
                head_counts[new_key] += 1
                head[remove_idx] = candidate
                used_tail_idx.add(tail_idx)
            tail = [item for idx, item in enumerate(tail) if idx not in used_tail_idx]

    if max_per_airline > 0:
        head_counts = Counter((item.get("_airline_key") or "UNKNOWN") for item in head)
        overflow_indices = [
            idx for idx in range(len(head) - 1, -1, -1)
            if head_counts[(head[idx].get("_airline_key") or "UNKNOWN")] > max_per_airline
        ]
        if overflow_indices:
            used_tail_idx: set[int] = set()
            for remove_idx in overflow_indices:
                replacement_idx = None
                replacement = None
                for idx, candidate in enumerate(tail):
                    if idx in used_tail_idx:
                        continue
                    key = candidate.get("_airline_key") or "UNKNOWN"
                    if head_counts[key] >= max_per_airline:
                        continue
                    replacement_idx = idx
                    replacement = candidate
                    break
                if replacement is None or replacement_idx is None:
                    continue
                old_key = head[remove_idx].get("_airline_key") or "UNKNOWN"
                head_counts[old_key] -= 1
                new_key = replacement.get("_airline_key") or "UNKNOWN"
                head_counts[new_key] += 1
                head[remove_idx] = replacement
                used_tail_idx.add(replacement_idx)
            if used_tail_idx:
                tail = [item for idx, item in enumerate(tail) if idx not in used_tail_idx]

    return head + tail

def _place_country_from_code(code: str | None) -> str | None:
    c = (code or "").strip().upper()
    if not c:
        return None
    airport = _airport_code_map().get(c)
    if airport:
        country = str(airport.get("country") or "").strip().upper()
        return country or None
    metro = CITY_METRO_GROUPS.get(c)
    if metro:
        country = str(metro.get("country") or "").strip().upper()
        return country or None
    return None

def _airline_mix_limits_for_route(params: dict[str, Any] | None) -> tuple[int, int, int, int]:
    if not params:
        return (
            RECOMMENDED_TOP10_MIN_UNIQUE_AIRLINES,
            RECOMMENDED_TOP10_MAX_PER_AIRLINE,
            RECOMMENDED_TOP20_MIN_UNIQUE_AIRLINES,
            RECOMMENDED_TOP20_MAX_PER_AIRLINE,
        )

    origin_country = _place_country_from_code(params.get("origin"))
    destination_country = _place_country_from_code(params.get("destination"))
    if origin_country and destination_country and origin_country == destination_country:
        return (
            RECOMMENDED_DOMESTIC_TOP10_MIN_UNIQUE_AIRLINES,
            RECOMMENDED_DOMESTIC_TOP10_MAX_PER_AIRLINE,
            RECOMMENDED_DOMESTIC_TOP20_MIN_UNIQUE_AIRLINES,
            RECOMMENDED_DOMESTIC_TOP20_MAX_PER_AIRLINE,
        )

    if origin_country and destination_country and origin_country != destination_country:
        return (
            RECOMMENDED_INTL_TOP10_MIN_UNIQUE_AIRLINES,
            RECOMMENDED_INTL_TOP10_MAX_PER_AIRLINE,
            RECOMMENDED_INTL_TOP20_MIN_UNIQUE_AIRLINES,
            RECOMMENDED_INTL_TOP20_MAX_PER_AIRLINE,
        )

    return (
        RECOMMENDED_TOP10_MIN_UNIQUE_AIRLINES,
        RECOMMENDED_TOP10_MAX_PER_AIRLINE,
        RECOMMENDED_TOP20_MIN_UNIQUE_AIRLINES,
        RECOMMENDED_TOP20_MAX_PER_AIRLINE,
    )

def _apply_google_like_airline_mix(flights: list[dict[str, Any]], *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if len(flights) <= 1:
        return flights
    top10_min_unique, top10_max_per_airline, top20_min_unique, top20_max_per_airline = _airline_mix_limits_for_route(params)
    mixed = _enforce_airline_constraints(
        flights,
        top_n=10,
        min_unique=top10_min_unique,
        max_per_airline=top10_max_per_airline,
    )
    mixed = _enforce_airline_constraints(
        mixed,
        top_n=20,
        min_unique=top20_min_unique,
        max_per_airline=top20_max_per_airline,
    )
    return mixed

def _describe_recommended_top_pick(
    flight: dict[str, Any], flights: list[dict[str, Any]], params: dict[str, Any] | None
) -> str:
    """
    Builds a concrete, request-specific explanation for why this flight is
    the top pick, grounded in real numbers from this search plus anything the
    user explicitly asked for (budget, nonstop, cabin, traveling with family,
    layover comfort) — never a static, one-size-fits-all caption.
    """
    params = params or {}
    prices = [float(f.get("price", 0) or 0) for f in flights if f.get("price")]
    durations = [int(f.get("_sort_total_duration", 0) or 0) for f in flights if f.get("_sort_total_duration")]
    price = float(flight.get("price", 0) or 0)
    duration = int(flight.get("_sort_total_duration", 0) or 0)
    stops = int(flight.get("out_stops", 0) or 0) + int(flight.get("in_stops", 0) or 0)
    currency = flight.get("currency") or flight.get("total_currency") or "USD"

    reasons: list[str] = []

    if prices and price <= min(prices) + 0.01:
        reasons.append("the lowest fare in this search")
    elif prices:
        avg_price = sum(prices) / len(prices)
        if price < avg_price - 1:
            reasons.append(f"~{currency} {avg_price - price:,.0f} below the average fare for this search")

    max_price_raw = params.get("max_price")
    try:
        max_price_val = float(max_price_raw) if max_price_raw not in (None, "") else None
    except (TypeError, ValueError):
        max_price_val = None
    if max_price_val and price and price <= max_price_val:
        reasons.append(f"stays within your {currency} {max_price_val:,.0f} budget")

    if params.get("nonstop") and stops == 0:
        reasons.append("nonstop, as requested")
    elif stops == 0 and durations and duration <= min(durations) + 15:
        reasons.append("nonstop and one of the fastest options here")
    elif stops == 0:
        reasons.append("nonstop")

    traveler_context = params.get("traveler_context") or {}
    companions = traveler_context.get("companion_labels") or []
    layover_minutes = [
        int(x.get("minutes", 0) or 0)
        for x in [*(flight.get("out_layovers") or []), *(flight.get("in_layovers") or [])]
    ]
    if traveler_context.get("prefers_longer_layover") and layover_minutes and max(layover_minutes) >= 90:
        reasons.append("gives you extra time during the layover, like you asked")
    elif (
        (traveler_context.get("prefers_shorter_layover") or traveler_context.get("has_senior_or_child_companion"))
        and stops >= 1
        and layover_minutes
        and max(layover_minutes) <= 150
    ):
        who = companions[0] if companions else "your group"
        reasons.append(f"keeps the connection short, easier for traveling with {who}")

    cabin = str(params.get("cabin") or "").upper()
    if cabin and cabin != "ECONOMY":
        reasons.append(f"booked in {cabin.replace('_', ' ').title()}")

    if not reasons:
        return "Best overall balance of price, duration, and flight timing for this search"
    joined = "; ".join(reasons[:3])
    return joined[0].upper() + joined[1:]


def _assign_smart_badges(
    flights: list[dict[str, Any]], sort: str, *, params: dict[str, Any] | None = None
) -> None:
    if not flights:
        return

    assigned: set[int] = set()

    def assign(index: int | None, label: str, reasoning: str = "") -> None:
        if index is None or index >= len(flights) or index in assigned:
            return
        flights[index]["smart_badge"] = label
        if reasoning:
            flights[index]["badge_reasoning"] = reasoning
        assigned.add(index)

    cheapest_idx = min(range(len(flights)), key=lambda i: (float(flights[i].get("price", 0) or 0), i))
    fastest_idx = min(range(len(flights)), key=lambda i: (int(flights[i].get("_sort_total_duration", 0) or 0), i))

    alt_airline_idx = None
    primary_airline = flights[0].get("_airline_key") or "UNKNOWN"
    for idx, flight in enumerate(flights[1:], start=1):
        if (flight.get("_airline_key") or "UNKNOWN") != primary_airline:
            alt_airline_idx = idx
            break

    if sort == "cheapest":
        assign(0, "Lowest price", "Best value for these dates")
        assign(fastest_idx, "Fastest", "Shortest total travel time")
        assign(alt_airline_idx, "Alternative option", "Different carrier for this route")
        return

    if sort == "fastest":
        assign(0, "Fastest trip", "Shortest total travel time")
        assign(cheapest_idx, "Cheapest", "Lowest fare available")
        assign(alt_airline_idx, "Alternative option", "Different carrier for this route")
        return

    if sort == "earliest_departure":
        assign(0, "Earliest departure", "First flight out for this route")
        assign(cheapest_idx, "Cheapest", "Lowest fare available")
        assign(fastest_idx, "Fastest", "Shortest total travel time")
        return

    if sort == "earliest_arrival":
        assign(0, "Arrives earliest", "Gets you there soonest")
        assign(cheapest_idx, "Cheapest", "Lowest fare available")
        assign(fastest_idx, "Fastest", "Shortest total travel time")
        return

    if sort == "fewest_stops":
        assign(0, "Most direct", "Fewest connections for this route")
        assign(cheapest_idx, "Cheapest", "Lowest fare available")
        assign(fastest_idx, "Fastest", "Shortest total travel time")
        return

    assign(0, "Top pick", _describe_recommended_top_pick(flights[0], flights, params))
    assign(cheapest_idx, "Cheapest", "Lowest fare available")
    assign(fastest_idx, "Fastest", "Shortest total travel time")
    assign(alt_airline_idx, "Alternative option", "Different carrier for this route")

def _annotate_comparison_metrics(flights: list[dict[str, Any]]) -> None:
    if not flights:
        return

    cheapest_idx = min(range(len(flights)), key=lambda i: (float(flights[i].get("price", 0) or 0), i))
    fastest_idx = min(range(len(flights)), key=lambda i: (int(flights[i].get("_sort_total_duration", 0) or 0), i))
    cheapest_price = float(flights[cheapest_idx].get("price", 0) or 0)

    for idx, flight in enumerate(flights):
        price = float(flight.get("price", 0) or 0)
        delta = max(0.0, round(price - cheapest_price, 2))
        flight["metric_is_best"] = idx == 0
        flight["metric_is_cheapest"] = idx == cheapest_idx
        flight["metric_is_fastest"] = idx == fastest_idx
        flight["price_vs_cheapest"] = delta

def _clean_flights_for_render(flights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for flight in flights:
        for key in ("_sort_total_duration", "_recommended_score", "_airline_key", "_out_via_codes", "_in_via_codes", "_slice_meta"):
            flight.pop(key, None)
    return flights

def _depart_time_sort_key(x: dict[str, Any]) -> str:
    # ISO datetime string sorts lexicographically — "2024-01-15T06:00" < "2024-01-15T22:00"
    return x.get("out_depart_at") or x.get("first_depart_at") or "9999"

def _arrive_time_sort_key(x: dict[str, Any]) -> str:
    return x.get("out_arrive_at") or x.get("final_arrive_at") or "9999"

def _sort_flights(flights: list[dict[str, Any]], sort: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if sort == "cheapest":
        flights.sort(key=lambda x: (x["price"], x.get("_sort_total_duration", 0), x.get("out_stops", 0), x.get("in_stops", 0) or 0))
        flights = _rebalance_recommended_flights(flights, RESULTS_PAGE_LIMIT)
        flights = _ensure_recommended_airline_variety(flights, RESULTS_PAGE_LIMIT)
        flights = _apply_google_like_airline_mix(flights, params=params)
    elif sort == "fastest":
        flights.sort(key=lambda x: (x.get("_sort_total_duration", 0), x["price"], x.get("out_stops", 0), x.get("in_stops", 0) or 0))
        flights = _rebalance_recommended_flights(flights, RESULTS_PAGE_LIMIT)
        flights = _ensure_recommended_airline_variety(flights, RESULTS_PAGE_LIMIT)
        flights = _apply_google_like_airline_mix(flights, params=params)
    elif sort == "earliest_departure":
        flights.sort(key=lambda x: (_depart_time_sort_key(x), x.get("out_stops", 0), x["price"]))
        flights = _apply_google_like_airline_mix(flights, params=params)
    elif sort == "earliest_arrival":
        flights.sort(key=lambda x: (_arrive_time_sort_key(x), x.get("out_stops", 0), x["price"]))
        flights = _apply_google_like_airline_mix(flights, params=params)
    elif sort == "fewest_stops":
        flights.sort(key=lambda x: (x.get("out_stops", 0), x.get("in_stops", 0) or 0, x["price"], x.get("_sort_total_duration", 0)))
        flights = _apply_google_like_airline_mix(flights, params=params)
    else:
        flights = _apply_recommended_scoring(flights, params=params)
        flights = _diversify_recommended_flights(flights)
        flights = _rebalance_recommended_flights(flights, RECOMMENDED_RESULTS_LIMIT)
        flights = _ensure_recommended_airline_variety(flights, RECOMMENDED_RESULTS_LIMIT)
        flights = _apply_google_like_airline_mix(flights, params=params)
    return flights

def search_flights(
    params: dict[str, Any],
    *,
    detailed: bool = True,
    flex_final: bool = False,
    force_refresh: bool = False,
) -> list[dict[str, Any]]:
    cache_key = _normalize_search_key(params, detailed=detailed)
    if not force_refresh:
        cached = SEARCH_CACHE.get(cache_key)
        if cached is not None:
            return cached

    raw = _fetch_live_offer_rows(
        params,
        detailed=detailed,
        flex_final=flex_final,
        force_refresh=force_refresh,
    )
    if raw is None:
        return []
    flights = _collect_best_presentations(raw, params, detailed=detailed)

    sort_mode = params.get("sort", "recommended")
    if sort_mode == "recommended":
        ranked_flights = _sort_flights(flights, sort_mode, params=params)
        expanded_raw = _expand_recommended_offers_if_needed(
            params,
            raw,
            ranked_flights,
            detailed=detailed,
            force_refresh=force_refresh,
        )
        if expanded_raw is not raw:
            flights = _collect_best_presentations(expanded_raw, params, detailed=detailed)
            ranked_flights = _sort_flights(flights, sort_mode, params=params)
        flights = ranked_flights[:RECOMMENDED_RESULTS_LIMIT]
    else:
        flights = _sort_flights(flights, sort_mode, params=params)
        flights = flights[:RESULTS_PAGE_LIMIT]

    _assign_smart_badges(flights, sort_mode, params=params)
    _annotate_comparison_metrics(flights)
    flights = _decorate_flights_for_display(flights, params)
    flights = _clean_flights_for_render(flights)
    SEARCH_CACHE.set(cache_key, flights)
    return flights


def _flex_ndjson_line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str) + "\n"


def _flex_stream_flight_preview(flight: dict[str, Any]) -> dict[str, Any]:
    total_duration = flight.get("total_duration_min")
    if total_duration is None:
        total_duration = int(flight.get("out_duration_min") or 0) + int(flight.get("in_duration_min") or 0)
    total_stops = flight.get("total_stop_count")
    if total_stops is None:
        total_stops = int(flight.get("out_stops") or 0) + int(flight.get("in_stops") or 0)
    out_stops = int(flight.get("out_stops") or 0)
    in_stops_raw = flight.get("in_stops")
    stop_filter_count = out_stops if in_stops_raw is None else max(out_stops, int(in_stops_raw or 0))
    return {
        "price": flight.get("price"),
        "currency": flight.get("currency") or flight.get("total_currency"),
        "trip_story": flight.get("trip_story"),
        "out_airline": flight.get("out_airline"),
        "smart_badge": flight.get("smart_badge"),
        "airline_summary": flight.get("airline_summary"),
        "airline_logo_url": flight.get("airline_logo_url"),
        "segments_ui": flight.get("segments_ui") or [],
        "metric_is_best": flight.get("metric_is_best"),
        "metric_is_cheapest": flight.get("metric_is_cheapest"),
        "metric_is_fastest": flight.get("metric_is_fastest"),
        "price_vs_cheapest": flight.get("price_vs_cheapest"),
        "first_depart_at": flight.get("first_depart_at") or flight.get("out_depart_at") or "",
        "final_arrive_at": flight.get("final_arrive_at") or flight.get("out_arrive_at") or "",
        "total_duration_min": int(total_duration or 0),
        "total_stop_count": int(total_stops or 0),
        "stop_filter_count": int(stop_filter_count or 0),
    }


def _query_params_from_flex_best(base: dict[str, Any], best: dict[str, Any]) -> dict[str, Any]:
    params = dict(base)
    params["depart_date"] = best["depart_date"]
    if params.get("trip_type") == "oneway":
        params["return_date"] = None
        params["best_week"] = None
        params["best_scan_label"] = "Best day found"
        params["best_scan_value"] = best["depart_date"]
        params["scan_price_note"] = (
            f"Smart scan found ~{best['scan_currency']} ${best['scan_price_total']:.2f} "
            "for the strongest one-way departure date."
        )
    else:
        params["return_date"] = best["return_date"]
        params["sort"] = "cheapest"
        params["best_week"] = f"{best['depart_date']} → {best['return_date']}"
        params["best_scan_label"] = "Best week found"
        params["best_scan_value"] = params["best_week"]
        params["scan_price_note"] = (
            f"Smart scan found ~{best['scan_currency']} ${best['scan_price_total']:.2f} "
            "for the cheapest date pair (final prices may differ)"
        )
    if best.get("fallback_notice"):
        params["scan_price_note"] = f"{params['scan_price_note']} {best['fallback_notice']}"
    return params


def _flex_final_offers_with_stream(
    final_params: dict[str, Any],
    emit: Callable[[dict[str, Any]], None],
) -> tuple[list[dict[str, Any]], str | None]:
    """Cheapest detailed search for flex final leg; emits preview rows; mirrors find_best_week follow-up."""
    sort_mode = "cheapest"
    final_params = dict(final_params)
    final_params["sort"] = sort_mode

    cache_key = _normalize_search_key(final_params, detailed=True)
    cached = SEARCH_CACHE.get(cache_key)
    if cached is not None:
        emit({"type": "sorting", "label": "Loaded from cache — sorted cheapest first"})
        for i, fl in enumerate(cached):
            emit({"type": "flight_row", "rank": i + 1, "flight": _flex_stream_flight_preview(fl)})
        return cached, None

    emit({"type": "final_leg", "label": "Cheapest dates from the scan — loading full itineraries and exact prices"})
    emit({"type": "final_fetch_start"})
    raw = _fetch_live_offer_rows(final_params, detailed=True, flex_final=True, force_refresh=False)
    flights: list[dict[str, Any]] = []
    fallback_notice: str | None = None

    if raw is not None:
        emit({"type": "offers_received", "count": len(raw)})
        flights = _collect_best_presentations(raw, final_params, detailed=True)
        flights = _sort_flights(flights, sort_mode, params=final_params)
        flights = flights[:RESULTS_PAGE_LIMIT]
        _assign_smart_badges(flights, sort_mode, params=final_params)
        _annotate_comparison_metrics(flights)
        flights = _decorate_flights_for_display(flights, final_params)
        flights = _clean_flights_for_render(flights)

    if not flights:
        emit({"type": "flex_fallback_try"})
        fb = _fallback_flights_from_snapshot(final_params)
        if not fb:
            emit({"type": "final_fetch_empty"})
            return [], None
        fallback_notice = (
            "Duffel temporarily limited the full follow-up search, so this uses the best live-priced itinerary "
            "captured during the flex scan."
        )
        emit({"type": "fallback_notice", "message": fallback_notice})
        flights = fb

    emit({"type": "sorting", "label": "Sorting by total price — lowest first"})
    for i, fl in enumerate(flights):
        emit({"type": "flight_row", "rank": i + 1, "flight": _flex_stream_flight_preview(fl)})
    if fallback_notice is None:
        SEARCH_CACHE.set(cache_key, flights)
    return flights, fallback_notice


def _iter_flex_search_ndjson(params: dict[str, Any]) -> Iterator[str]:
    """NDJSON stream for flexible-date search (manual flex + AI flex)."""

    def emit(obj: dict[str, Any]) -> Any:
        return _flex_ndjson_line(obj)

    trip_oneway = params.get("trip_type") == "oneway"
    if trip_oneway:
        cache_key = (
            "oneway",
            params.get("origin"),
            params.get("destination"),
            params.get("flex_month"),
            0,
            int(params.get("passengers", 1) or 1),
            params.get("cabin", "ECONOMY"),
            bool(params.get("nonstop", False)),
            params.get("max_price"),
        )
    else:
        cache_key = (
            params.get("trip_type", "roundtrip"),
            params.get("origin"),
            params.get("destination"),
            params.get("flex_month"),
            int(params.get("trip_length_days", 7) or 7),
            int(params.get("passengers", 1) or 1),
            params.get("cabin", "ECONOMY"),
            bool(params.get("nonstop", False)),
            params.get("max_price"),
        )

    cached_found, cached = FLEX_RESULT_CACHE.lookup(cache_key)
    if cached_found and cached is not None:
        yield emit({"type": "flex_cache_hit", "trip_oneway": trip_oneway})
        query_params = _query_params_from_flex_best(params, cached)
        for idx, fl in enumerate(cached["offers"]):
            yield emit({"type": "flight_row", "rank": idx + 1, "flight": _flex_stream_flight_preview(fl)})
        html = render_template(
            "results.html",
            query=query_params,
            flights=cached["offers"],
            error="",
            minutes_to_hm=minutes_to_hm,
            fmt_dt=fmt_dt,
        )
        yield emit(_results_complete_stream_event(html))
        return
    if cached_found and cached is None:
        yield emit({"type": "error", "message": _format_flex_no_results_error(params)})
        return

    month_start, month_end = _month_bounds(params["flex_month"])
    today = date.today()
    departures: list[date] = []
    for dep_d in _daterange(month_start, month_end):
        if dep_d < today:
            continue
        if not trip_oneway:
            trip_len = int(params.get("trip_length_days", 7) or 7)
            if dep_d + timedelta(days=trip_len) < today:
                continue
        departures.append(dep_d)

    if not departures:
        yield emit({"type": "error", "message": _format_flex_no_results_error(params)})
        return

    departures_set = set(departures)
    trip_len = int(params.get("trip_length_days", 7) or 7) if not trip_oneway else 0

    if len(departures) <= FLEX_SAMPLE_INITIAL + FLEX_REFINE_NEIGHBORS:
        sample_dates = departures
    else:
        sample_dates = _bias_sample_with_cheap_days(departures, FLEX_SAMPLE_INITIAL)

    yield emit(
        {
            "type": "start",
            "trip_oneway": trip_oneway,
            "origin": str(params.get("origin") or "").strip().upper(),
            "destination": str(params.get("destination") or "").strip().upper(),
            "flex_month": str(params.get("flex_month") or ""),
            "initial_sample_count": len(sample_dates),
            "month_candidate_days": len(departures),
        },
    )

    yield emit(
        {
            "type": "phase",
            "phase": 1,
            "label": f"Broad sweep: quoting {len(sample_dates)} departure dates in {params.get('flex_month') or 'your month'}",
            "dates_planned": len(sample_dates),
        },
    )

    scan_results: list[dict[str, Any]] = []
    scanned_isos = {d.isoformat() for d in sample_dates}
    provisional_top_n = 3
    provisional_last_emit_mono = 0.0
    provisional_last_fingerprint: tuple[Any, ...] | None = None

    def _scan_hit_public_payload(hit: dict[str, Any]) -> dict[str, Any]:
        return {
            "depart_date": hit.get("depart_date"),
            "return_date": hit.get("return_date"),
            "scan_price_total": hit.get("scan_price_total"),
            "scan_currency": hit.get("scan_currency"),
        }

    def _candidate_key(hit: dict[str, Any]) -> tuple[str, str]:
        return (
            str(hit.get("depart_date") or ""),
            str(hit.get("return_date") or ""),
        )

    def _ranked_scan_hits_for_provisional() -> list[dict[str, Any]]:
        return sorted(
            scan_results,
            key=lambda c: (
                float(c.get("scan_price_total") or 0.0),
                str(c.get("depart_date") or ""),
                str(c.get("return_date") or ""),
            ),
        )[:provisional_top_n]

    def _provisional_fingerprint() -> tuple[Any, ...]:
        return tuple(
            (_candidate_key(h), float(h.get("scan_price_total") or 0.0))
            for h in _ranked_scan_hits_for_provisional()
        )

    def _emit_provisional_updates(*, force: bool) -> Iterator[str]:
        nonlocal provisional_last_emit_mono, provisional_last_fingerprint
        fp = _provisional_fingerprint()
        if fp == provisional_last_fingerprint:
            return
        now = time.monotonic()
        if (
            not force
            and provisional_last_emit_mono > 0.0
            and (now - provisional_last_emit_mono) < FLEX_PROVISIONAL_MIN_INTERVAL
        ):
            return
        ranked = _ranked_scan_hits_for_provisional()
        emitted = False
        for rank, hit in enumerate(ranked, start=1):
            preview = _flex_provisional_preview_from_scan_hit(params, hit, trip_oneway=trip_oneway)
            if not preview:
                continue
            yield emit({"type": "provisional_flight", "rank": rank, "flight": preview})
            emitted = True
        done_mono = time.monotonic()
        if emitted:
            provisional_last_fingerprint = fp
            provisional_last_emit_mono = done_mono
        elif not ranked:
            provisional_last_fingerprint = fp
            provisional_last_emit_mono = done_mono
        elif force:
            provisional_last_fingerprint = fp
            provisional_last_emit_mono = done_mono

    if trip_oneway:
        for r in _iter_parallel_flex_scan_oneway(params, sample_dates):
            scan_results.append(r)
            yield emit({"type": "scan_hit", "phase": 1, "hit": _scan_hit_public_payload(r)})
            yield from _emit_provisional_updates(force=False)
        yield from _emit_provisional_updates(force=True)
    else:
        for r in _iter_parallel_flex_scan_roundtrip(params, sample_dates, trip_len):
            scan_results.append(r)
            yield emit({"type": "scan_hit", "phase": 1, "hit": _scan_hit_public_payload(r)})
            yield from _emit_provisional_updates(force=False)
        yield from _emit_provisional_updates(force=True)

    if scan_results and len(departures) > FLEX_SAMPLE_INITIAL + FLEX_REFINE_NEIGHBORS:
        if trip_oneway:
            ndates = _refine_neighbor_dates_oneway(scan_results, departures_set, scanned_isos)
        else:
            ndates = _refine_neighbor_dates_roundtrip(scan_results, trip_len, departures_set, scanned_isos)
        yield emit(
            {
                "type": "phase",
                "phase": 2,
                "label": "Refine pass: extra quotes around the best totals so far",
                "dates_planned": len(ndates),
            },
        )
        if trip_oneway:
            for r in _iter_parallel_flex_scan_oneway(params, ndates):
                scan_results.append(r)
                yield emit({"type": "scan_hit", "phase": 2, "hit": _scan_hit_public_payload(r)})
                yield from _emit_provisional_updates(force=False)
            yield from _emit_provisional_updates(force=True)
        else:
            for r in _iter_parallel_flex_scan_roundtrip(params, ndates, trip_len):
                scan_results.append(r)
                yield emit({"type": "scan_hit", "phase": 2, "hit": _scan_hit_public_payload(r)})
                yield from _emit_provisional_updates(force=False)
            yield from _emit_provisional_updates(force=True)

    if not scan_results:
        FLEX_RESULT_CACHE.set(cache_key, None)
        yield emit({"type": "error", "message": _format_flex_no_results_error(params)})
        return

    best_light = _choose_balanced_flex_candidate(scan_results, month_start, month_end)
    yield emit(
        {
            "type": "picked_dates",
            "depart_date": best_light["depart_date"],
            "return_date": best_light.get("return_date"),
            "scan_price_total": best_light["scan_price_total"],
            "scan_currency": best_light.get("scan_currency", "USD"),
        },
    )

    final_params = dict(params)
    final_params["depart_date"] = best_light["depart_date"]
    if trip_oneway:
        final_params.pop("return_date", None)
    else:
        final_params["return_date"] = best_light["return_date"]
    final_params["sort"] = "cheapest"

    collected: list[dict[str, Any]] = []

    def collect_emit(obj: dict[str, Any]) -> None:
        collected.append(obj)

    offers, fallback_notice = _flex_final_offers_with_stream(final_params, collect_emit)
    for obj in collected:
        yield emit(obj)

    if not offers:
        FLEX_RESULT_CACHE.set(cache_key, None)
        yield emit({"type": "error", "message": _format_flex_no_results_error(params)})
        return

    best_result = {
        "depart_date": best_light["depart_date"],
        "scan_price_total": best_light["scan_price_total"],
        "scan_currency": best_light["scan_currency"],
        "offers": offers,
    }
    if not trip_oneway:
        best_result["return_date"] = best_light["return_date"]
    if fallback_notice:
        best_result["fallback_notice"] = fallback_notice

    FLEX_RESULT_CACHE.set(cache_key, best_result)
    query_params = _query_params_from_flex_best(params, best_result)
    html = render_template(
        "results.html",
        query=query_params,
        flights=offers,
        error="",
        minutes_to_hm=minutes_to_hm,
        fmt_dt=fmt_dt,
    )
    yield emit(_results_complete_stream_event(html))


def _clone_segments_ui(segments: list[dict[str, Any]] | None, *, first_label: str | None = None) -> list[dict[str, Any]]:
    cloned: list[dict[str, Any]] = []
    for index, segment in enumerate(segments or []):
        next_segment = dict(segment)
        next_segment["layovers"] = [dict(item) for item in (segment.get("layovers") or [])]
        if first_label and index == 0:
            next_segment["label"] = first_label
        cloned.append(next_segment)
    return cloned

def _clone_flight_for_manual(
    flight: dict[str, Any],
    *,
    first_label: str | None = None,
    smart_badge: str | None = None,
) -> dict[str, Any]:
    cloned = dict(flight)
    cloned["segments_ui"] = _clone_segments_ui(flight.get("segments_ui") or [], first_label=first_label)
    if smart_badge is None:
        cloned.pop("smart_badge", None)
    else:
        cloned["smart_badge"] = smart_badge
    cloned.pop("manual_action_fields", None)
    cloned.pop("manual_total_price", None)
    cloned.pop("manual_total_label", None)
    cloned.pop("manual_price_delta", None)
    cloned.pop("manual_price_delta_label", None)
    return cloned

def _find_flight_by_selection_token(flights: list[dict[str, Any]], token: str | None) -> dict[str, Any] | None:
    token_value = (token or "").strip()
    if not token_value:
        return None
    for flight in flights:
        if flight.get("selection_token") == token_value:
            return flight
    return None

def _manual_combination_base_fields(params: dict[str, Any]) -> dict[str, str]:
    fields = {
        "mode": "standard",
        "origin": params.get("origin", ""),
        "destination": params.get("destination", ""),
        "trip_type": "roundtrip",
        "depart_date": params.get("depart_date", "") or "",
        "return_date": params.get("return_date", "") or "",
        "passengers": str(int(params.get("passengers", 1) or 1)),
        "cabin": params.get("cabin", "ECONOMY"),
        "sort": params.get("sort", "recommended"),
        "combination_mode": "manual",
    }
    if params.get("nonstop"):
        fields["nonstop"] = "on"
    return fields

def _manual_combination_price_label(amount: float) -> str | None:
    delta = round(float(amount or 0.0), 2)
    if delta <= 0:
        return None
    return f"+${delta:.2f}"

def _manual_leg_signature(flight: dict[str, Any], *, leg: str) -> tuple[Any, ...]:
    segments = flight.get("segments_ui") or []
    if leg == "outbound":
        segment = segments[0] if segments else {}
        return (
            flight.get("out_depart_at"),
            flight.get("out_arrive_at"),
            flight.get("out_airline_code"),
            int(flight.get("out_stops", 0) or 0),
            segment.get("route_chip"),
            segment.get("duration"),
        )

    segment = segments[1] if len(segments) > 1 else {}
    return (
        flight.get("in_depart_at"),
        flight.get("in_arrive_at"),
        flight.get("in_airline_code"),
        int(flight.get("in_stops", 0) or 0),
        segment.get("route_chip"),
        segment.get("duration"),
    )

def _build_manual_leg_option(
    flight: dict[str, Any],
    *,
    leg: str,
    smart_badge: str | None = None,
) -> dict[str, Any]:
    cloned = _clone_flight_for_manual(flight, smart_badge=smart_badge)
    source_segments = flight.get("segments_ui") or []
    if leg == "outbound":
        segment = source_segments[:1]
        fallback_airline = flight.get("out_airline") or flight.get("airline_summary")
        label = "Outbound"
    else:
        segment = source_segments[1:2]
        fallback_airline = flight.get("in_airline") or flight.get("airline_summary")
        label = "Return"

    cloned["segments_ui"] = _clone_segments_ui(segment, first_label=label)
    cloned["airline_summary"] = (cloned["segments_ui"][0].get("airline") if cloned["segments_ui"] else None) or fallback_airline or "Selected flight"
    seg0 = cloned["segments_ui"][0] if cloned["segments_ui"] else {}
    cloned["airline_logo_url"] = seg0.get("airline_logo_url") or cloned.get("airline_logo_url")
    return cloned

def _group_manual_roundtrip_offers(flights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
    for flight in flights:
        group_key = _manual_leg_signature(flight, leg="outbound")
        group = groups.get(group_key)
        if group is None:
            group = {
                "key": group_key,
                "offers": [],
                "tokens": set(),
            }
            groups[group_key] = group
        group["offers"].append(flight)
        group["tokens"].add(flight.get("selection_token"))

    grouped = list(groups.values())
    for group in grouped:
        group["offers"].sort(key=lambda item: (float(item.get("price", 0.0) or 0.0), item.get("selection_token", "")))
        group["best_offer"] = group["offers"][0] if group["offers"] else None
        group["group_token"] = (group["best_offer"] or {}).get("selection_token")
    return grouped

def _find_manual_group_by_token(groups: list[dict[str, Any]], token: str | None) -> dict[str, Any] | None:
    token_value = (token or "").strip()
    if not token_value:
        return None
    for group in groups:
        if token_value in group.get("tokens", set()):
            return group
    return None

def _manual_offer_pool(params: dict[str, Any]) -> list[dict[str, Any]]:
    # Build a richer pool than a single ranked pass so manual leg selection
    # can surface many return options for a selected outbound.
    cheapest_params = dict(params)
    cheapest_params["sort"] = "cheapest"
    fastest_params = dict(params)
    fastest_params["sort"] = "fastest"

    candidates = [*search_flights(cheapest_params, detailed=True), *search_flights(fastest_params, detailed=True)]
    deduped: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    for flight in candidates:
        token = str(flight.get("selection_token") or "").strip()
        if not token or token in seen_tokens:
            continue
        seen_tokens.add(token)
        deduped.append(flight)
    deduped.sort(key=lambda item: (float(item.get("price", 0.0) or 0.0), item.get("selection_token", "")))
    return deduped[:max(RESULTS_PAGE_LIMIT * 2, 50)]

def build_manual_combination_flow(params: dict[str, Any]) -> dict[str, Any]:
    base_fields = _manual_combination_base_fields(params)
    roundtrip_offers = _manual_offer_pool(params)
    outbound_groups = _group_manual_roundtrip_offers(roundtrip_offers)

    overall_cheapest_total = min(
        (float(flight.get("price", 0.0) or 0.0) for flight in roundtrip_offers if float(flight.get("price", 0.0) or 0.0) > 0),
        default=0.0,
    )

    outbound_options: list[dict[str, Any]] = []
    for group in outbound_groups:
        best_offer = group.get("best_offer")
        if not best_offer:
            continue
        option = _build_manual_leg_option(best_offer, leg="outbound")
        delta = max(0.0, round(float(option.get("price", 0.0) or 0.0) - overall_cheapest_total, 2))
        option["manual_price_delta"] = delta
        option["manual_price_delta_label"] = _manual_combination_price_label(delta)
        option["manual_total_price"] = float(option.get("price", 0.0) or 0.0)
        option["manual_total_label"] = f"{option.get('currency', 'USD')} ${float(option.get('price', 0.0) or 0.0):.2f}"
        option["manual_action_fields"] = {
            **base_fields,
            "selected_outbound_token": group.get("group_token", "") or "",
        }
        outbound_options.append(option)

    selected_group = _find_manual_group_by_token(outbound_groups, params.get("selected_outbound_token"))
    if not selected_group:
        return {
            "stage": "outbound",
            "base_fields": base_fields,
            "outbound_options": outbound_options,
            "outbound_cheapest_delta_label": _manual_combination_price_label(0),
        }

    selected_outbound_offer = selected_group.get("best_offer")
    selected_outbound = _build_manual_leg_option(
        selected_outbound_offer,
        leg="outbound",
        smart_badge="Selected outbound",
    ) if selected_outbound_offer else None

    grouped_returns: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
    for offer in selected_group.get("offers", []):
        return_key = _manual_leg_signature(offer, leg="return")
        current = grouped_returns.get(return_key)
        if current is None or float(offer.get("price", 0.0) or 0.0) < float(current.get("price", 0.0) or 0.0):
            grouped_returns[return_key] = offer

    return_source_offers = sorted(
        grouped_returns.values(),
        key=lambda item: (float(item.get("price", 0.0) or 0.0), item.get("selection_token", "")),
    )
    cheapest_return_total = min(
        (float(flight.get("price", 0.0) or 0.0) for flight in return_source_offers if float(flight.get("price", 0.0) or 0.0) > 0),
        default=0.0,
    )

    inbound_options: list[dict[str, Any]] = []
    for offer in return_source_offers:
        option = _build_manual_leg_option(offer, leg="return")
        delta = max(0.0, round(float(option.get("price", 0.0) or 0.0) - cheapest_return_total, 2))
        option["manual_price_delta"] = delta
        option["manual_price_delta_label"] = _manual_combination_price_label(delta)
        option["manual_total_price"] = float(option.get("price", 0.0) or 0.0)
        option["manual_total_label"] = f"{option.get('currency', 'USD')} ${float(option.get('price', 0.0) or 0.0):.2f}"
        option["manual_action_fields"] = {
            **base_fields,
            "selected_outbound_token": selected_group.get("group_token", "") or "",
            "selected_return_token": option.get("selection_token", ""),
        }
        inbound_options.append(option)

    selected_return_offer = _find_flight_by_selection_token(
        return_source_offers,
        params.get("selected_return_token"),
    )
    if not selected_return_offer:
        return {
            "stage": "return",
            "base_fields": base_fields,
            "outbound_options": outbound_options,
            "selected_outbound": selected_outbound,
            "return_options": inbound_options,
            "reset_outbound_fields": base_fields,
            "return_cheapest_delta_label": _manual_combination_price_label(0),
        }

    selected_return = _build_manual_leg_option(
        selected_return_offer,
        leg="return",
        smart_badge="Selected return",
    )
    combined_summary = _clone_flight_for_manual(selected_return_offer, smart_badge="Your combination")

    return {
        "stage": "complete",
        "base_fields": base_fields,
        "outbound_options": outbound_options,
        "selected_outbound": selected_outbound,
        "return_options": inbound_options,
        "selected_return": selected_return,
        "combined_summary": combined_summary,
        "reset_outbound_fields": base_fields,
        "reset_return_fields": {
            **base_fields,
            "selected_outbound_token": selected_group.get("group_token", "") or "",
        },
        "return_cheapest_delta_label": _manual_combination_price_label(0),
    }

# ------------------------------------------------------------
# Flex search
# ------------------------------------------------------------
def _light_cheapest_for_date(base_params: dict[str, Any], depart_date: str, return_date: str) -> dict[str, Any] | None:
    params = dict(base_params)
    params["depart_date"] = depart_date
    params["return_date"] = return_date
    snapshot = _cheapest_offer_snapshot(params)
    if not snapshot:
        return None
    return {
        "depart_date": depart_date,
        "return_date": return_date,
        "scan_price_total": float(snapshot["scan_price_total"]),
        "scan_currency": snapshot.get("scan_currency", "USD"),
        "_raw_offer": snapshot.get("raw_offer"),
    }

def _light_cheapest_oneway_for_date(base_params: dict[str, Any], depart_date: str) -> dict[str, Any] | None:
    params = dict(base_params)
    params["depart_date"] = depart_date
    params.pop("return_date", None)
    snapshot = _cheapest_offer_snapshot(params)
    if not snapshot:
        return None
    return {
        "depart_date": depart_date,
        "scan_price_total": float(snapshot["scan_price_total"]),
        "scan_currency": snapshot.get("scan_currency", "USD"),
        "_raw_offer": snapshot.get("raw_offer"),
    }


def _flex_provisional_preview_from_scan_hit(
    base_params: dict[str, Any],
    hit: dict[str, Any],
    *,
    trip_oneway: bool,
) -> dict[str, Any] | None:
    """Build a lightweight card preview from a scan hit's cheapest raw offer."""
    raw_offer = hit.get("_raw_offer")
    if not isinstance(raw_offer, dict):
        return None
    params = dict(base_params)
    params["depart_date"] = hit.get("depart_date")
    if trip_oneway:
        params.pop("return_date", None)
    else:
        params["return_date"] = hit.get("return_date")
    flights = _collect_best_presentations([raw_offer], params, detailed=False)
    if not flights:
        return None
    flights = _sort_flights(flights, "cheapest", params=params)
    _assign_smart_badges(flights, "cheapest", params=params)
    _annotate_comparison_metrics(flights)
    flights = _decorate_flights_for_display(flights, params)
    flights = _clean_flights_for_render(flights)
    if not flights:
        return None
    preview = _flex_stream_flight_preview(flights[0])
    preview["smart_badge"] = "Live estimate"
    return preview

def _verify_candidate(base_params: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any] | None:
    params = dict(base_params)
    params["depart_date"] = candidate["depart_date"]
    params["return_date"] = candidate["return_date"]
    snapshot = _cheapest_offer_snapshot(params)
    if not snapshot:
        return None
    return {
        "depart_date": params["depart_date"],
        "return_date": params["return_date"],
        "scan_price_total": float(snapshot["scan_price_total"]),
        "scan_currency": snapshot.get("scan_currency", "USD"),
    }


def _weekday_bias(dep_date: str) -> float:
    try:
        wd = _to_date(dep_date).weekday()
    except Exception:
        return 0.0
    # Mild preference for Tue/Wed/Thu; slight penalty for Fri/Sun
    return {
        0: 0.5,
        1: 3.0,
        2: 3.5,
        3: 2.0,
        4: -1.0,
        5: -2.0,
        6: -1.0,
    }.get(wd, 0.0)

def _candidate_priority(candidate: dict[str, Any], best_known_price: float | None = None) -> tuple:
    price = float(candidate.get("price_total") or candidate.get("scan_price_total") or 0.0)
    dep = candidate.get("depart_date") or ""
    tie_bias = _weekday_bias(dep)
    if best_known_price and best_known_price > 0 and price > 0:
        premium_ratio = price / best_known_price
    else:
        premium_ratio = 1.0
    return (premium_ratio, -tie_bias, dep)

def _expand_neighbor_candidates(candidates: list[dict[str, Any]], trip_len: int, month_start: date, month_end: date) -> list[dict[str, Any]]:
    seen = {(c.get("depart_date"), c.get("return_date")) for c in candidates}
    out = list(candidates)
    seeds = candidates[:min(4, len(candidates))]
    for seed in seeds:
        try:
            dep_d = _to_date(seed["depart_date"])
        except Exception:
            continue
        for offset in range(-FLEX_NEARBY_DAY_WINDOW, FLEX_NEARBY_DAY_WINDOW + 1):
            if offset == 0:
                continue
            new_dep = dep_d + timedelta(days=offset)
            new_ret = new_dep + timedelta(days=trip_len)
            if new_dep < month_start or new_dep > month_end:
                continue
            tup = (new_dep.isoformat(), new_ret.isoformat())
            if tup in seen or new_dep < date.today():
                continue
            seen.add(tup)
            out.append({"depart_date": tup[0], "return_date": tup[1], "price_total": float(seed.get("price_total") or 0.0)})
    return out

def _dedupe_flex_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        dep = candidate.get("depart_date")
        ret = candidate.get("return_date")
        if not dep or not ret:
            continue

        pair = (dep, ret)
        current = best_by_pair.get(pair)
        candidate_price = float(candidate.get("price_total") or candidate.get("scan_price_total") or 0.0)
        current_price = float((current or {}).get("price_total") or (current or {}).get("scan_price_total") or 0.0)
        if current is None or (candidate_price > 0 and (current_price <= 0 or candidate_price < current_price)):
            best_by_pair[pair] = candidate
    return list(best_by_pair.values())

def _candidate_coverage_ratio(candidates: list[dict[str, Any]], departures_count: int) -> float:
    if departures_count <= 0:
        return 0.0
    pairs = {
        (candidate.get("depart_date"), candidate.get("return_date"))
        for candidate in candidates
        if candidate.get("depart_date") and candidate.get("return_date")
    }
    return len(pairs) / float(departures_count)

def _sample_evenly_spaced_pairs(pairs: list[tuple[str, str]], limit: int) -> list[tuple[str, str]]:
    if limit <= 0 or len(pairs) <= limit:
        return list(pairs)
    if limit == 1:
        return [pairs[len(pairs) // 2]]

    chosen: list[tuple[str, str]] = []
    used_indexes: set[int] = set()
    step = (len(pairs) - 1) / float(limit - 1)

    for i in range(limit):
        target = int(round(i * step))
        probe_indexes = [target]
        for delta in range(1, len(pairs)):
            left = target - delta
            right = target + delta
            if left >= 0:
                probe_indexes.append(left)
            if right < len(pairs):
                probe_indexes.append(right)

        for idx in probe_indexes:
            if idx in used_indexes:
                continue
            used_indexes.add(idx)
            chosen.append(pairs[idx])
            break

    return chosen

def _should_skip_phase2_scan(
    candidates: list[dict[str, Any]],
    verified: list[dict[str, Any]],
    departures_count: int,
) -> bool:
    if not candidates or not verified:
        return False
    if len(verified) < min(FLEX_SKIP_PHASE2_VERIFIED_MIN, len(candidates)):
        return False
    if _candidate_coverage_ratio(candidates, departures_count) < FLEX_SKIP_PHASE2_COVERAGE:
        return False

    best_verified = min(
        verified,
        key=lambda item: (item["scan_price_total"], -_weekday_bias(item["depart_date"]), item["depart_date"]),
    )
    top_pairs = {
        (candidate["depart_date"], candidate["return_date"])
        for candidate in candidates[:min(FLEX_SKIP_PHASE2_TOP_CANDIDATES, len(candidates))]
        if candidate.get("depart_date") and candidate.get("return_date")
    }
    return (best_verified["depart_date"], best_verified["return_date"]) in top_pairs

def _select_flex_scan_pairs(
    *,
    departures: list[date],
    trip_len: int,
    candidates: list[dict[str, Any]],
    verified_pairs: set[tuple[str, str]],
) -> list[tuple[str, str]]:
    all_pairs = [
        (dep_d.isoformat(), (dep_d + timedelta(days=trip_len)).isoformat())
        for dep_d in departures
    ]
    available_pairs = [pair for pair in all_pairs if pair not in verified_pairs]
    if not available_pairs:
        return []

    deduped_candidates = _dedupe_flex_candidates(candidates)
    if not deduped_candidates:
        return available_pairs

    best_known_price = min(
        (
            float(candidate.get("price_total") or candidate.get("scan_price_total") or 0.0)
            for candidate in deduped_candidates
            if float(candidate.get("price_total") or candidate.get("scan_price_total") or 0.0) > 0
        ),
        default=None,
    )

    selected: list[tuple[str, str]] = []
    seen_pairs = set(verified_pairs)
    for candidate in sorted(deduped_candidates, key=lambda item: _candidate_priority(item, best_known_price)):
        pair = (candidate["depart_date"], candidate["return_date"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        selected.append(pair)
        if len(selected) >= FLEX_PHASE2_MAX_CHALLENGERS:
            break

    spread_probe_limit = FLEX_PHASE2_SPREAD_PROBES
    if verified_pairs and _candidate_coverage_ratio(deduped_candidates, len(departures)) >= FLEX_PHASE2_COVERAGE_THRESHOLD:
        spread_probe_limit = max(1, FLEX_PHASE2_SPREAD_PROBES // 2)

    spread_source = [pair for pair in available_pairs if pair not in seen_pairs]
    for pair in _sample_evenly_spaced_pairs(spread_source, spread_probe_limit):
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        selected.append(pair)

    return selected



def _month_midpoint(month_start: date, month_end: date) -> date:
    return month_start + timedelta(days=(month_end - month_start).days // 2)


def _choose_balanced_flex_candidate(candidates: list[dict[str, Any]], month_start: date, month_end: date) -> dict[str, Any]:
    valid_prices = [float(c.get("scan_price_total") or 0.0) for c in candidates if float(c.get("scan_price_total") or 0.0) > 0]
    best_price = min(valid_prices) if valid_prices else 0.0
    tolerance = max(5.0, best_price * 0.015) if best_price > 0 else 0.0
    cheapest_band = [
        c for c in candidates
        if best_price <= 0 or float(c.get("scan_price_total") or 0.0) <= best_price + tolerance
    ] or list(candidates)
    month_mid = _month_midpoint(month_start, month_end)

    def score(candidate: dict[str, Any]) -> tuple:
        dep = _to_date(candidate["depart_date"])
        midpoint_distance = abs((dep - month_mid).days)
        return (
            float(candidate.get("scan_price_total") or 0.0),
            midpoint_distance,
            -_weekday_bias(candidate["depart_date"]),
            candidate["depart_date"],
        )

    return min(cheapest_band, key=score)

def _sample_dates_evenly(departures: list[date], limit: int) -> list[date]:
    """Pick *limit* dates spread evenly across *departures*."""
    if limit <= 0 or len(departures) <= limit:
        return list(departures)
    if limit == 1:
        return [departures[len(departures) // 2]]
    step = (len(departures) - 1) / (limit - 1)
    chosen: list[date] = []
    used: set[int] = set()
    for i in range(limit):
        idx = int(round(i * step))
        idx = min(idx, len(departures) - 1)
        if idx in used:
            for delta in range(1, len(departures)):
                for candidate_idx in (idx + delta, idx - delta):
                    if 0 <= candidate_idx < len(departures) and candidate_idx not in used:
                        idx = candidate_idx
                        break
                else:
                    continue
                break
        used.add(idx)
        chosen.append(departures[idx])
    return chosen


def _bias_sample_with_cheap_days(departures: list[date], limit: int) -> list[date]:
    """Evenly-spaced sample that always includes a Tue and Wed if available."""
    base = _sample_dates_evenly(departures, max(0, limit - 2))
    base_set = set(base)
    tue = next((d for d in departures if d.weekday() == 1 and d not in base_set), None)
    wed = next((d for d in departures if d.weekday() == 2 and d not in base_set), None)
    extras = [d for d in (tue, wed) if d is not None]
    return (base + extras)[:limit]


def _iter_parallel_flex_scan_roundtrip(
    base_params: dict[str, Any],
    dates: list[date],
    trip_len: int,
) -> Iterator[dict[str, Any]]:
    """Yield each non-empty _light_cheapest_for_date result as worker tasks complete."""
    if not dates:
        return
    workers = min(FLEX_SCAN_WORKERS, len(dates))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [
            ex.submit(
                _light_cheapest_for_date,
                base_params,
                dep_d.isoformat(),
                (dep_d + timedelta(days=trip_len)).isoformat(),
            )
            for dep_d in dates
        ]
        for fut in as_completed(futures):
            try:
                r = fut.result()
            except Exception as exc:
                print("FLEX SCAN ERROR:", repr(exc))
                continue
            if r:
                yield r


def _parallel_flex_scan_roundtrip(
    base_params: dict[str, Any],
    dates: list[date],
    trip_len: int,
) -> list[dict[str, Any]]:
    """Fire _light_cheapest_for_date for each date in parallel, return results."""
    return list(_iter_parallel_flex_scan_roundtrip(base_params, dates, trip_len))


def _iter_parallel_flex_scan_oneway(
    base_params: dict[str, Any],
    dates: list[date],
) -> Iterator[dict[str, Any]]:
    """Yield each non-empty _light_cheapest_oneway_for_date result as tasks complete."""
    if not dates:
        return
    workers = min(FLEX_SCAN_WORKERS, len(dates))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [
            ex.submit(_light_cheapest_oneway_for_date, base_params, dep_d.isoformat())
            for dep_d in dates
        ]
        for fut in as_completed(futures):
            try:
                r = fut.result()
            except Exception as exc:
                print("FLEX SCAN ERROR:", repr(exc))
                continue
            if r:
                yield r


def _parallel_flex_scan_oneway(
    base_params: dict[str, Any],
    dates: list[date],
) -> list[dict[str, Any]]:
    """Fire _light_cheapest_oneway_for_date for each date in parallel, return results."""
    return list(_iter_parallel_flex_scan_oneway(base_params, dates))


def _refine_neighbor_dates_roundtrip(
    scan_results: list[dict[str, Any]],
    trip_len: int,
    all_departures_set: set[date],
    already_scanned: set[str],
) -> list[date]:
    """Departure dates for phase-2 flex refinement (round-trip)."""
    top = sorted(scan_results, key=lambda c: c["scan_price_total"])[:5]
    neighbor_dates: list[date] = []
    seen: set[str] = set(already_scanned)
    today = date.today()
    for candidate in top:
        try:
            dep_d = _to_date(candidate["depart_date"])
        except Exception:
            continue
        for offset in range(-FLEX_NEARBY_DAY_WINDOW, FLEX_NEARBY_DAY_WINDOW + 1):
            if offset == 0:
                continue
            nd = dep_d + timedelta(days=offset)
            iso = nd.isoformat()
            if nd < today or nd not in all_departures_set or iso in seen:
                continue
            seen.add(iso)
            neighbor_dates.append(nd)
    return neighbor_dates[:FLEX_REFINE_NEIGHBORS]


def _refine_neighbor_dates_oneway(
    scan_results: list[dict[str, Any]],
    all_departures_set: set[date],
    already_scanned: set[str],
) -> list[date]:
    """Departure dates for phase-2 flex refinement (one-way)."""
    top = sorted(scan_results, key=lambda c: c["scan_price_total"])[:5]
    neighbor_dates: list[date] = []
    seen: set[str] = set(already_scanned)
    today = date.today()
    for candidate in top:
        try:
            dep_d = _to_date(candidate["depart_date"])
        except Exception:
            continue
        for offset in range(-FLEX_NEARBY_DAY_WINDOW, FLEX_NEARBY_DAY_WINDOW + 1):
            if offset == 0:
                continue
            nd = dep_d + timedelta(days=offset)
            iso = nd.isoformat()
            if nd < today or nd not in all_departures_set or iso in seen:
                continue
            seen.add(iso)
            neighbor_dates.append(nd)
    return neighbor_dates[:FLEX_REFINE_NEIGHBORS]


def _refine_neighbors_roundtrip(
    base_params: dict[str, Any],
    scan_results: list[dict[str, Any]],
    trip_len: int,
    all_departures_set: set[date],
    already_scanned: set[str],
) -> list[dict[str, Any]]:
    """Probe ±FLEX_NEARBY_DAY_WINDOW days around the cheapest scan results."""
    neighbor_dates = _refine_neighbor_dates_roundtrip(
        scan_results, trip_len, all_departures_set, already_scanned,
    )
    return _parallel_flex_scan_roundtrip(base_params, neighbor_dates, trip_len)


def _refine_neighbors_oneway(
    base_params: dict[str, Any],
    scan_results: list[dict[str, Any]],
    all_departures_set: set[date],
    already_scanned: set[str],
) -> list[dict[str, Any]]:
    """Probe ±FLEX_NEARBY_DAY_WINDOW days around the cheapest one-way scan results."""
    neighbor_dates = _refine_neighbor_dates_oneway(scan_results, all_departures_set, already_scanned)
    return _parallel_flex_scan_oneway(base_params, neighbor_dates)


def find_best_week_in_month(params: dict[str, Any]) -> dict[str, Any] | None:
    cache_key = (
        params.get("trip_type", "roundtrip"),
        params.get("origin"),
        params.get("destination"),
        params.get("flex_month"),
        int(params.get("trip_length_days", 7) or 7),
        int(params.get("passengers", 1) or 1),
        params.get("cabin", "ECONOMY"),
        bool(params.get("nonstop", False)),
        params.get("max_price"),
    )
    cached_found, cached = FLEX_RESULT_CACHE.lookup(cache_key)
    if cached_found:
        return cached

    month_start, month_end = _month_bounds(params["flex_month"])
    trip_len = int(params.get("trip_length_days", 7) or 7)
    today = date.today()
    departures: list[date] = []
    for dep_d in _daterange(month_start, month_end):
        if dep_d < today:
            continue
        if dep_d + timedelta(days=trip_len) < today:
            continue
        departures.append(dep_d)

    if not departures:
        FLEX_RESULT_CACHE.set(cache_key, None)
        return None

    departures_set = set(departures)

    # Phase 1: sample a small spread of dates across the month
    if len(departures) <= FLEX_SAMPLE_INITIAL + FLEX_REFINE_NEIGHBORS:
        sample_dates = departures
    else:
        sample_dates = _bias_sample_with_cheap_days(departures, FLEX_SAMPLE_INITIAL)

    scan_results = _parallel_flex_scan_roundtrip(params, sample_dates, trip_len)
    scanned_isos = {d.isoformat() for d in sample_dates}

    print(f"FLEX SCAN phase1: {len(scan_results)}/{len(sample_dates)} sampled dates returned prices")

    # Phase 2: refine around the cheapest results from phase 1
    if scan_results and len(departures) > FLEX_SAMPLE_INITIAL + FLEX_REFINE_NEIGHBORS:
        refine_results = _refine_neighbors_roundtrip(
            params, scan_results, trip_len, departures_set, scanned_isos,
        )
        scan_results.extend(refine_results)
        print(f"FLEX SCAN phase2: +{len(refine_results)} neighbor dates returned prices")

    print(f"FLEX SCAN total: {len(scan_results)} prices from {len(departures)} eligible dates")
    if not scan_results:
        FLEX_RESULT_CACHE.set(cache_key, None)
        return None

    best_light = _choose_balanced_flex_candidate(scan_results, month_start, month_end)

    final_params = dict(params)
    final_params["depart_date"] = best_light["depart_date"]
    final_params["return_date"] = best_light["return_date"]
    final_params["sort"] = "cheapest"
    detailed_offers = search_flights(final_params, detailed=True, flex_final=True)
    fallback_notice = None
    if not detailed_offers:
        detailed_offers = _fallback_flights_from_snapshot(final_params)
        if detailed_offers:
            fallback_notice = (
                "Duffel temporarily limited the full follow-up search, so this uses the best live-priced itinerary "
                "captured during the flex scan."
            )
    if not detailed_offers:
        FLEX_RESULT_CACHE.set(cache_key, None)
        return None

    result = {
        "depart_date": best_light["depart_date"],
        "return_date": best_light["return_date"],
        "scan_price_total": best_light["scan_price_total"],
        "scan_currency": best_light["scan_currency"],
        "offers": detailed_offers,
    }
    if fallback_notice:
        result["fallback_notice"] = fallback_notice
    FLEX_RESULT_CACHE.set(cache_key, result)
    return result

def find_best_oneway_day_in_month(params: dict[str, Any]) -> dict[str, Any] | None:
    cache_key = (
        "oneway",
        params.get("origin"),
        params.get("destination"),
        params.get("flex_month"),
        0,
        int(params.get("passengers", 1) or 1),
        params.get("cabin", "ECONOMY"),
        bool(params.get("nonstop", False)),
        params.get("max_price"),
    )
    cached_found, cached = FLEX_RESULT_CACHE.lookup(cache_key)
    if cached_found:
        return cached

    month_start, month_end = _month_bounds(params["flex_month"])
    today = date.today()
    departures: list[date] = []
    for dep_d in _daterange(month_start, month_end):
        if dep_d < today:
            continue
        departures.append(dep_d)

    if not departures:
        FLEX_RESULT_CACHE.set(cache_key, None)
        return None

    departures_set = set(departures)

    # Phase 1: sample a small spread of dates across the month
    if len(departures) <= FLEX_SAMPLE_INITIAL + FLEX_REFINE_NEIGHBORS:
        sample_dates = departures
    else:
        sample_dates = _bias_sample_with_cheap_days(departures, FLEX_SAMPLE_INITIAL)

    scan_results = _parallel_flex_scan_oneway(params, sample_dates)
    scanned_isos = {d.isoformat() for d in sample_dates}

    print(f"FLEX SCAN oneway phase1: {len(scan_results)}/{len(sample_dates)} sampled dates returned prices")

    # Phase 2: refine around the cheapest results from phase 1
    if scan_results and len(departures) > FLEX_SAMPLE_INITIAL + FLEX_REFINE_NEIGHBORS:
        refine_results = _refine_neighbors_oneway(
            params, scan_results, departures_set, scanned_isos,
        )
        scan_results.extend(refine_results)
        print(f"FLEX SCAN oneway phase2: +{len(refine_results)} neighbor dates returned prices")

    print(f"FLEX SCAN oneway total: {len(scan_results)} prices from {len(departures)} eligible dates")
    if not scan_results:
        FLEX_RESULT_CACHE.set(cache_key, None)
        return None

    best_light = _choose_balanced_flex_candidate(scan_results, month_start, month_end)

    final_params = dict(params)
    final_params["depart_date"] = best_light["depart_date"]
    final_params.pop("return_date", None)
    final_params["sort"] = "cheapest"
    detailed_offers = search_flights(final_params, detailed=True, flex_final=True)
    fallback_notice = None
    if not detailed_offers:
        detailed_offers = _fallback_flights_from_snapshot(final_params)
        if detailed_offers:
            fallback_notice = (
                "Duffel temporarily limited the full follow-up search, so this uses the best live-priced itinerary "
                "captured during the flex scan."
            )

    if not detailed_offers:
        FLEX_RESULT_CACHE.set(cache_key, None)
        return None

    result = {
        "depart_date": best_light["depart_date"],
        "scan_price_total": best_light["scan_price_total"],
        "scan_currency": best_light["scan_currency"],
        "offers": detailed_offers,
    }
    if fallback_notice:
        result["fallback_notice"] = fallback_notice
    FLEX_RESULT_CACHE.set(cache_key, result)
    return result

def _format_flex_no_results_error(params: dict[str, Any]) -> str:
    origin = (params.get("origin") or "").strip().upper()
    destination = (params.get("destination") or "").strip().upper()
    route = f"{origin} → {destination}" if origin and destination else "that route"
    flex_month = params.get("flex_month") or "that month"
    trip_type = params.get("trip_type", "roundtrip")

    if trip_type == "oneway":
        message = (
            f"No good one-way options found for {route} in {flex_month}. "
            "Try another month, broader airports, or remove constraints like nonstop."
        )
    else:
        trip_length_days = int(params.get("trip_length_days", 7) or 7)
        message = (
            f"No good {trip_length_days}-day round-trip options found for {route} in {flex_month}. "
            "Try another month, broader airports, or remove constraints like nonstop."
        )
    if DUFFEL_ENV == "test":
        message += " This app is currently using Duffel test data, so some routes or prices may differ from live inventory."
    return message


def _booking_lookup_error() -> str | None:
    if not DUFFEL_ACCESS_TOKEN:
        return "Booking lookup is not configured yet. Add DUFFEL_ACCESS_TOKEN to your .env file and try again."
    return None


def _demo_checkout_lock_error() -> str | None:
    """
    Demo safeguard for the final checkout/payment step specifically — the
    review-trip and seat-selection pages before it stay fully browsable.
    """
    if not NGF_DEMO_BOOKING_LOCK:
        return None
    return (
        "This is a demo build of Skairova, so checkout and payment are turned off here — no real "
        "orders can be created. Feel free to search, browse flights, and go through the flow up to "
        "this point."
    )


def _booking_mode_error() -> str | None:
    if not DUFFEL_ACCESS_TOKEN:
        return "Duffel booking is not configured yet. Add DUFFEL_ACCESS_TOKEN to your .env file and try again."
    if DUFFEL_ENV != "test":
        return "Booking is locked to Duffel test mode for this prototype. Switch back to a duffel_test_ token before creating orders."
    return None


def _booking_status_code(status_code: int | None, default: int = 502) -> int:
    if status_code is None:
        return default
    if 400 <= status_code < 500:
        return 400
    return default


def _normalize_booking_reference(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", (value or "").strip().upper())


def _normalize_last_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _valid_dob(value: str) -> bool:
    try:
        parsed = date.fromisoformat((value or "").strip())
    except ValueError:
        return False
    return parsed < date.today()


def _manage_booking_attempt_key(ip_addr: str, booking_reference: str) -> str:
    safe_ip = (ip_addr or "unknown").strip()
    return f"{safe_ip}:{booking_reference}"


def _record_manage_booking_attempt(ip_addr: str, booking_reference: str) -> int:
    key = _manage_booking_attempt_key(ip_addr, booking_reference)
    attempts = int(MANAGE_BOOKING_ATTEMPT_CACHE.get(key) or 0) + 1
    MANAGE_BOOKING_ATTEMPT_CACHE.set(key, attempts)
    return attempts


def _reset_manage_booking_attempts(ip_addr: str, booking_reference: str) -> None:
    key = _manage_booking_attempt_key(ip_addr, booking_reference)
    MANAGE_BOOKING_ATTEMPT_CACHE.set(key, 0)


def _order_matches_guest_lookup(order: Mapping[str, Any], *, booking_reference: str, last_name: str, dob: str) -> bool:
    order_ref = _normalize_booking_reference(str(order.get("booking_reference") or ""))
    if not order_ref or order_ref != booking_reference:
        return False
    normalized_last_name = _normalize_last_name(last_name)
    expected_dob = (dob or "").strip()
    for passenger in order.get("passengers") or []:
        family_name = _normalize_last_name(str((passenger or {}).get("family_name") or ""))
        born_on = str((passenger or {}).get("born_on") or "").strip()
        if family_name == normalized_last_name and born_on == expected_dob:
            return True
    return False


def _normalize_email(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip().lower())


# ── Booking authorization helpers ────────────────────────────────────────────
_SESSION_AUTHORIZED_ORDERS_KEY = "ngf_authorized_orders"
_MAX_SESSION_AUTHORIZED_ORDERS = 20


def _session_authorize_order(order_id: str) -> None:
    authorized = list(session.get(_SESSION_AUTHORIZED_ORDERS_KEY) or [])
    if order_id not in authorized:
        authorized.append(order_id)
    session[_SESSION_AUTHORIZED_ORDERS_KEY] = authorized[-_MAX_SESSION_AUTHORIZED_ORDERS:]


def _session_is_order_authorized(order_id: str) -> bool:
    authorized = session.get(_SESSION_AUTHORIZED_ORDERS_KEY) or []
    if order_id in authorized:
        return True
    if str(session.get("ngf_manage_order_id") or "") == order_id:
        return True
    account_email = str(session.get("ngf_account_email") or "").strip().lower()
    if account_email:
        return True
    return False


# ── B2C login rate-limit helpers ──────────────────────────────────────────────
_B2C_LOGIN_MAX_ATTEMPTS = 10
_B2C_LOGIN_LOCKOUT_SECONDS = 15 * 60


def _b2c_login_attempt_key(ip: str, email: str) -> str:
    return f"login:{(ip or 'unknown').strip()}:{(email or '').strip().lower()}"


def _b2c_login_record_failure(ip: str, email: str) -> int:
    key = _b2c_login_attempt_key(ip, email)
    attempts = int(B2C_LOGIN_ATTEMPT_CACHE.get(key) or 0) + 1
    B2C_LOGIN_ATTEMPT_CACHE.set(key, attempts)
    return attempts


def _b2c_login_is_locked(ip: str, email: str) -> bool:
    key = _b2c_login_attempt_key(ip, email)
    return int(B2C_LOGIN_ATTEMPT_CACHE.get(key) or 0) >= _B2C_LOGIN_MAX_ATTEMPTS


def _b2c_login_reset(ip: str, email: str) -> None:
    key = _b2c_login_attempt_key(ip, email)
    B2C_LOGIN_ATTEMPT_CACHE.set(key, 0)


def _b2c_client_ip() -> str:
    forwarded = str(request.headers.get("X-Forwarded-For") or "").strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:80]
    return str(request.remote_addr or "").strip()[:80]


# ── B2C CSRF helpers ──────────────────────────────────────────────────────────
_B2C_CSRF_SESSION_KEY = "ngf_b2c_csrf"


def _b2c_csrf_token() -> str:
    if _B2C_CSRF_SESSION_KEY not in session:
        session[_B2C_CSRF_SESSION_KEY] = secrets.token_urlsafe(32)
    return session[_B2C_CSRF_SESSION_KEY]  # type: ignore[return-value]


def _validate_b2c_csrf() -> bool:
    if app.config.get("TESTING"):
        return True
    token = str(session.get(_B2C_CSRF_SESSION_KEY) or "").strip()
    submitted = str(request.form.get("_csrf") or "").strip()
    return bool(token and submitted and hmac.compare_digest(token, submitted))


def _collect_itinerary_email_recipients(
    passengers_payload: Sequence[Mapping[str, Any]] | None,
    *,
    account_email: str = "",
) -> list[str]:
    recipients: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        email = _normalize_email(candidate)
        if not email or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is None:
            return
        if email in seen:
            return
        seen.add(email)
        recipients.append(email)

    for passenger in passengers_payload or []:
        if not isinstance(passenger, Mapping):
            continue
        _add(str(passenger.get("email") or ""))
    _add(account_email)
    return recipients


def _order_passenger_emails(order: Mapping[str, Any] | None) -> list[str]:
    emails: list[str] = []
    seen: set[str] = set()
    for passenger in (order or {}).get("passengers") or []:
        if not isinstance(passenger, Mapping):
            continue
        email = _normalize_email(str(passenger.get("email") or ""))
        if not email or email in seen:
            continue
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is None:
            continue
        seen.add(email)
        emails.append(email)
    return emails


def _send_itinerary_emails_after_booking(
    *,
    order: Mapping[str, Any],
    passengers_payload: Sequence[Mapping[str, Any]] | None,
) -> None:
    summary = build_order_summary(order)
    recipients = _collect_itinerary_email_recipients(
        passengers_payload,
        account_email=_session_account_email(),
    )
    if not recipients:
        return

    booking_reference = _normalize_booking_reference(str(summary.get("booking_reference") or ""))
    manage_url = url_for("manage_booking", booking_reference=booking_reference) if booking_reference else ""

    pdf_bytes: bytes | None = None
    try:
        pdf_bytes = _render_itinerary_pdf(summary)
    except Exception as exc:
        print(f"ITINERARY PDF GENERATION FAILED (email attachment): {exc}")

    sent = 0
    failed = 0
    for email in recipients:
        ok, reason = email_service.send_itinerary_email(
            to_email=email,
            order_summary=summary,
            manage_url=manage_url,
            pdf_bytes=pdf_bytes,
        )
        if ok:
            sent += 1
        else:
            failed += 1
            print(f"ITINERARY EMAIL FAILED for {email}: {reason}")
    print(f"ITINERARY EMAIL RESULT sent={sent} failed={failed} recipients={len(recipients)}")


def _normalize_person_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    return cleaned[:80]


def _safe_saved_searches(value: Any) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        out.append(dict(item))
    return out


def _account_display_name(account: Mapping[str, Any]) -> str:
    first_name = _normalize_person_name(str(account.get("first_name") or ""))
    last_name = _normalize_person_name(str(account.get("last_name") or ""))
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    if full_name:
        return full_name
    return str(account.get("email") or "").strip().lower()


def _account_initials(account: Mapping[str, Any]) -> str:
    first_name = _normalize_person_name(str(account.get("first_name") or ""))
    last_name = _normalize_person_name(str(account.get("last_name") or ""))
    initials = ""
    if first_name:
        initials += first_name[:1].upper()
    if last_name:
        initials += last_name[:1].upper()
    if initials:
        return initials[:2]
    email = str(account.get("email") or "").strip().lower()
    return (email[:1] or "U").upper()


def _password_hash(password: str, salt_hex: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8"),
        bytes.fromhex(salt_hex),
        260_000,
    )
    return dk.hex()


def _password_meets_criteria(password: str) -> bool:
    candidate = str(password or "")
    if len(candidate) < 8:
        return False
    if re.search(r"[A-Za-z]", candidate) is None:
        return False
    if re.search(r"\d", candidate) is None:
        return False
    return True


def _ensure_account_booking_email_link_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS manage_booking_email_links (
            email TEXT NOT NULL,
            booking_reference TEXT NOT NULL,
            order_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (email, booking_reference)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_manage_booking_email_links_email ON manage_booking_email_links(email, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_manage_booking_email_links_reference ON manage_booking_email_links(booking_reference, updated_at DESC)"
    )


def _ensure_hotel_bookings_table(conn: sqlite3.Connection) -> None:
    """LiteAPI has no "list bookings by reference+name" endpoint the way
    Duffel's list_orders does, so guest (non-account) manage-booking lookups
    for hotels need this local table — the same DB, same migration style as
    the account tables above."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hotel_bookings (
            booking_reference TEXT PRIMARY KEY,
            liteapi_booking_id TEXT NOT NULL,
            liteapi_prebook_id TEXT NOT NULL DEFAULT '',
            hotel_id TEXT NOT NULL,
            hotel_name TEXT NOT NULL DEFAULT '',
            hotel_address TEXT NOT NULL DEFAULT '',
            hotel_photo TEXT NOT NULL DEFAULT '',
            room_name TEXT NOT NULL DEFAULT '',
            board_name TEXT NOT NULL DEFAULT '',
            checkin TEXT NOT NULL,
            checkout TEXT NOT NULL,
            holder_first_name TEXT NOT NULL DEFAULT '',
            holder_last_name TEXT NOT NULL DEFAULT '',
            holder_email TEXT NOT NULL DEFAULT '',
            total_amount TEXT NOT NULL DEFAULT '',
            currency TEXT NOT NULL DEFAULT 'USD',
            status TEXT NOT NULL DEFAULT 'confirmed',
            linked_flight_order_id TEXT NOT NULL DEFAULT '',
            linked_flight_booking_reference TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hotel_bookings_holder_email ON hotel_bookings(holder_email, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hotel_bookings_updated_at ON hotel_bookings(updated_at)"
    )


def _use_postgres_account_store() -> bool:
    """Use Supabase/Postgres in deployed environments when configured.

    Tests deliberately keep using their temporary SQLite database, which lets
    the existing account-flow test suite run without a hosted dependency.
    """
    if not (
        NGF_DATABASE_URL
        and psycopg is not None
        and psycopg_conninfo_to_dict is not None
        # Test suites intentionally replace ACCOUNT_DB_PATH with an isolated
        # temporary SQLite file, even while exercising non-TESTING code paths.
        and os.path.abspath(ACCOUNT_DB_PATH) == os.path.abspath(DEFAULT_ACCOUNT_DB_PATH)
        and not bool(app.config.get("TESTING"))
    ):
        return False
    try:
        # A malformed URI must never take account pages down. Keep the prior
        # SQLite fallback active until a valid Supabase URI is configured.
        psycopg_conninfo_to_dict(NGF_DATABASE_URL, sslmode="require")
    except Exception:
        return False
    return True


def _postgres_account_connection():
    if not _use_postgres_account_store() or psycopg is None or psycopg_dict_row is None:
        raise RuntimeError("Postgres account store is not configured.")
    # Supabase pooler connections require TLS. Passing sslmode separately
    # keeps copied pooler URIs valid even when they omit that query parameter.
    return psycopg.connect(
        NGF_DATABASE_URL,
        connect_timeout=10,
        sslmode="require",
        row_factory=psycopg_dict_row,
    )


def _ensure_postgres_account_db() -> None:
    with _postgres_account_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS manage_booking_accounts (
                    email TEXT PRIMARY KEY,
                    salt_hex TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    dob TEXT NOT NULL DEFAULT '',
                    terms_accepted_at TEXT NOT NULL DEFAULT '',
                    saved_searches TEXT NOT NULL DEFAULT '[]',
                    linked_booking_references TEXT NOT NULL DEFAULT '[]',
                    session_nonce TEXT NOT NULL DEFAULT '',
                    last_login_at TEXT NOT NULL DEFAULT '',
                    last_login_ip TEXT NOT NULL DEFAULT '',
                    price_alerts_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    route_tracking_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    phone_number TEXT NOT NULL DEFAULT '',
                    nationality TEXT NOT NULL DEFAULT '',
                    passport_number TEXT NOT NULL DEFAULT '',
                    gender TEXT NOT NULL DEFAULT '',
                    oauth_provider TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_manage_booking_accounts_updated_at "
                "ON manage_booking_accounts(updated_at)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS manage_booking_email_links (
                    email TEXT NOT NULL,
                    booking_reference TEXT NOT NULL,
                    order_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (email, booking_reference)
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_manage_booking_email_links_email "
                "ON manage_booking_email_links(email, updated_at DESC)"
            )


def _ensure_account_db() -> None:
    global _ACCOUNT_DB_READY
    if _ACCOUNT_DB_READY:
        return
    with _ACCOUNT_DB_LOCK:
        if _ACCOUNT_DB_READY:
            return
        if _use_postgres_account_store():
            _ensure_postgres_account_db()
            _ACCOUNT_DB_READY = True
            return
        os.makedirs(os.path.dirname(ACCOUNT_DB_PATH), exist_ok=True)
        with sqlite3.connect(ACCOUNT_DB_PATH) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS manage_booking_accounts (
                    email TEXT PRIMARY KEY,
                    salt_hex TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    first_name TEXT NOT NULL DEFAULT '',
                    last_name TEXT NOT NULL DEFAULT '',
                    dob TEXT NOT NULL DEFAULT '',
                    terms_accepted_at TEXT NOT NULL DEFAULT '',
                    saved_searches TEXT NOT NULL DEFAULT '[]',
                    linked_booking_references TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_manage_booking_accounts_updated_at ON manage_booking_accounts(updated_at)"
            )
            existing_columns = {
                str(row[1]).strip().lower()
                for row in (conn.execute("PRAGMA table_info(manage_booking_accounts)").fetchall() or [])
            }
            migration_columns = {
                "first_name": "TEXT NOT NULL DEFAULT ''",
                "last_name": "TEXT NOT NULL DEFAULT ''",
                "dob": "TEXT NOT NULL DEFAULT ''",
                "terms_accepted_at": "TEXT NOT NULL DEFAULT ''",
                "saved_searches": "TEXT NOT NULL DEFAULT '[]'",
                "session_nonce": "TEXT NOT NULL DEFAULT ''",
                "last_login_at": "TEXT NOT NULL DEFAULT ''",
                "last_login_ip": "TEXT NOT NULL DEFAULT ''",
                "price_alerts_enabled": "INTEGER NOT NULL DEFAULT 1",
                "route_tracking_enabled": "INTEGER NOT NULL DEFAULT 1",
                "phone_number": "TEXT NOT NULL DEFAULT ''",
                "nationality": "TEXT NOT NULL DEFAULT ''",
                "passport_number": "TEXT NOT NULL DEFAULT ''",
                "gender": "TEXT NOT NULL DEFAULT ''",
                "oauth_provider": "TEXT NOT NULL DEFAULT ''",
            }
            for column_name, column_type in migration_columns.items():
                if column_name in existing_columns:
                    continue
                conn.execute(
                    f"ALTER TABLE manage_booking_accounts ADD COLUMN {column_name} {column_type}"
                )
            _ensure_account_booking_email_link_table(conn)
            _ensure_hotel_bookings_table(conn)
            conn.commit()
        _ACCOUNT_DB_READY = True


def _db_fetch_account(email: str) -> dict[str, Any] | None:
    _ensure_account_db()
    key = _normalize_email(email)
    if not key:
        return None
    query = """
            SELECT
                email,
                salt_hex,
                password_hash,
                created_at,
                first_name,
                last_name,
                dob,
                terms_accepted_at,
                saved_searches,
                linked_booking_references,
                session_nonce,
                last_login_at,
                last_login_ip,
                price_alerts_enabled,
                route_tracking_enabled,
                COALESCE(phone_number, '')    AS phone_number,
                COALESCE(nationality, '')     AS nationality,
                COALESCE(passport_number, '') AS passport_number,
                COALESCE(gender, '')          AS gender,
                COALESCE(oauth_provider, '')  AS oauth_provider
            FROM manage_booking_accounts
            WHERE email = {placeholder}
            """
    if _use_postgres_account_store():
        with _postgres_account_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query.format(placeholder="%s"), (key,))
                row = cur.fetchone()
    else:
        with sqlite3.connect(ACCOUNT_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                query.format(placeholder="?"),
                (key,),
            ).fetchone()
    if row is None:
        return None
    try:
        linked = json.loads(row["linked_booking_references"] or "[]")
    except json.JSONDecodeError:
        linked = []
    linked_refs = [str(item).strip().upper() for item in (linked or []) if str(item).strip()]
    try:
        saved_searches_raw = json.loads(row["saved_searches"] or "[]")
    except json.JSONDecodeError:
        saved_searches_raw = []
    saved_searches = _safe_saved_searches(saved_searches_raw)
    return {
        "email": str(row["email"] or "").strip().lower(),
        "salt_hex": str(row["salt_hex"] or "").strip(),
        "password_hash": str(row["password_hash"] or "").strip(),
        "created_at": str(row["created_at"] or "").strip(),
        "first_name": _normalize_person_name(str(row["first_name"] or "")),
        "last_name": _normalize_person_name(str(row["last_name"] or "")),
        "dob": str(row["dob"] or "").strip(),
        "terms_accepted_at": str(row["terms_accepted_at"] or "").strip(),
        "saved_searches": saved_searches,
        "linked_booking_references": sorted(set(linked_refs)),
        "session_nonce": str(row["session_nonce"] or "").strip(),
        "last_login_at": str(row["last_login_at"] or "").strip(),
        "last_login_ip": str(row["last_login_ip"] or "").strip(),
        "price_alerts_enabled": bool(int(row["price_alerts_enabled"] or 0)),
        "route_tracking_enabled": bool(int(row["route_tracking_enabled"] or 0)),
        "phone_number": str(row["phone_number"] or "").strip(),
        "nationality": str(row["nationality"] or "").strip(),
        "passport_number": str(row["passport_number"] or "").strip(),
        "gender": str(row["gender"] or "").strip(),
        "oauth_provider": str(row["oauth_provider"] or "").strip().lower(),
    }


def _db_upsert_account(account: Mapping[str, Any]) -> None:
    _ensure_account_db()
    email = _normalize_email(str(account.get("email") or ""))
    if not email:
        return
    salt_hex = str(account.get("salt_hex") or "").strip()
    password_hash = str(account.get("password_hash") or "").strip()
    created_at = str(account.get("created_at") or "").strip() or datetime.utcnow().isoformat()
    first_name = _normalize_person_name(str(account.get("first_name") or ""))
    last_name = _normalize_person_name(str(account.get("last_name") or ""))
    dob = str(account.get("dob") or "").strip()
    terms_accepted_at = str(account.get("terms_accepted_at") or "").strip()
    session_nonce = str(account.get("session_nonce") or "").strip()
    last_login_at = str(account.get("last_login_at") or "").strip()
    last_login_ip = str(account.get("last_login_ip") or "").strip()
    price_alerts_enabled = bool(account.get("price_alerts_enabled", True))
    route_tracking_enabled = bool(account.get("route_tracking_enabled", True))
    phone_number = str(account.get("phone_number") or "").strip()
    nationality = str(account.get("nationality") or "").strip()
    passport_number = str(account.get("passport_number") or "").strip()
    gender = str(account.get("gender") or "").strip()
    oauth_provider = str(account.get("oauth_provider") or "").strip().lower()
    saved_searches = _safe_saved_searches(account.get("saved_searches"))
    if len(saved_searches) > MAX_SAVED_SEARCHES_PER_ACCOUNT:
        saved_searches = saved_searches[:MAX_SAVED_SEARCHES_PER_ACCOUNT]
    saved_searches_json = json.dumps(saved_searches, separators=(",", ":"), ensure_ascii=True)
    linked_refs = [str(item).strip().upper() for item in (account.get("linked_booking_references") or []) if str(item).strip()]
    linked_json = json.dumps(sorted(set(linked_refs)))
    updated_at = datetime.utcnow().isoformat()
    query = """
            INSERT INTO manage_booking_accounts (
                email,
                salt_hex,
                password_hash,
                created_at,
                first_name,
                last_name,
                dob,
                terms_accepted_at,
                session_nonce,
                last_login_at,
                last_login_ip,
                price_alerts_enabled,
                route_tracking_enabled,
                saved_searches,
                linked_booking_references,
                phone_number,
                nationality,
                passport_number,
                gender,
                oauth_provider,
                updated_at
            )
            VALUES ({placeholders})
            ON CONFLICT(email) DO UPDATE SET
                salt_hex = excluded.salt_hex,
                password_hash = excluded.password_hash,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                dob = excluded.dob,
                terms_accepted_at = excluded.terms_accepted_at,
                session_nonce = excluded.session_nonce,
                last_login_at = excluded.last_login_at,
                last_login_ip = excluded.last_login_ip,
                price_alerts_enabled = excluded.price_alerts_enabled,
                route_tracking_enabled = excluded.route_tracking_enabled,
                saved_searches = excluded.saved_searches,
                linked_booking_references = excluded.linked_booking_references,
                phone_number = excluded.phone_number,
                nationality = excluded.nationality,
                passport_number = excluded.passport_number,
                gender = excluded.gender,
                oauth_provider = CASE
                    WHEN excluded.oauth_provider != '' THEN excluded.oauth_provider
                    ELSE manage_booking_accounts.oauth_provider
                END,
                updated_at = excluded.updated_at
            """
    values = (
                email,
                salt_hex,
                password_hash,
                created_at,
                first_name,
                last_name,
                dob,
                terms_accepted_at,
                session_nonce,
                last_login_at,
                last_login_ip,
                price_alerts_enabled,
                route_tracking_enabled,
                saved_searches_json,
                linked_json,
                phone_number,
                nationality,
                passport_number,
                gender,
                oauth_provider,
                updated_at,
            )
    if _use_postgres_account_store():
        with _postgres_account_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query.format(placeholders=", ".join(["%s"] * len(values))), values)
    else:
        with sqlite3.connect(ACCOUNT_DB_PATH) as conn:
            conn.execute(query.format(placeholders=", ".join(["?"] * len(values))), values)
            conn.commit()


def _generate_hotel_booking_reference() -> str:
    """SKH-prefixed so hotel refs are recognizable at a glance next to
    Duffel's flight refs (which are un-prefixed alphanumeric codes)."""
    return f"SKH{secrets.token_hex(3).upper()}"


def _save_hotel_booking(row: Mapping[str, Any]) -> None:
    _ensure_account_db()
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(ACCOUNT_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO hotel_bookings (
                booking_reference, liteapi_booking_id, liteapi_prebook_id,
                hotel_id, hotel_name, hotel_address, hotel_photo,
                room_name, board_name, checkin, checkout,
                holder_first_name, holder_last_name, holder_email,
                total_amount, currency, status,
                linked_flight_order_id, linked_flight_booking_reference,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(booking_reference) DO UPDATE SET
                status = excluded.status,
                linked_flight_order_id = excluded.linked_flight_order_id,
                linked_flight_booking_reference = excluded.linked_flight_booking_reference,
                updated_at = excluded.updated_at
            """,
            (
                str(row.get("booking_reference") or "").strip().upper(),
                str(row.get("liteapi_booking_id") or "").strip(),
                str(row.get("liteapi_prebook_id") or "").strip(),
                str(row.get("hotel_id") or "").strip(),
                str(row.get("hotel_name") or "").strip(),
                str(row.get("hotel_address") or "").strip(),
                str(row.get("hotel_photo") or "").strip(),
                str(row.get("room_name") or "").strip(),
                str(row.get("board_name") or "").strip(),
                str(row.get("checkin") or "").strip(),
                str(row.get("checkout") or "").strip(),
                _normalize_person_name(str(row.get("holder_first_name") or "")),
                _normalize_person_name(str(row.get("holder_last_name") or "")),
                _normalize_email(str(row.get("holder_email") or "")),
                str(row.get("total_amount") or ""),
                str(row.get("currency") or "USD").upper(),
                str(row.get("status") or "confirmed"),
                str(row.get("linked_flight_order_id") or "").strip(),
                _normalize_booking_reference(str(row.get("linked_flight_booking_reference") or "")),
                str(row.get("created_at") or now),
                now,
            ),
        )
        conn.commit()


def _hotel_booking_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "booking_reference": str(row["booking_reference"] or "").strip().upper(),
        "liteapi_booking_id": str(row["liteapi_booking_id"] or "").strip(),
        "liteapi_prebook_id": str(row["liteapi_prebook_id"] or "").strip(),
        "hotel_id": str(row["hotel_id"] or "").strip(),
        "hotel_name": str(row["hotel_name"] or "").strip(),
        "hotel_address": str(row["hotel_address"] or "").strip(),
        "hotel_photo": str(row["hotel_photo"] or "").strip(),
        "room_name": str(row["room_name"] or "").strip(),
        "board_name": str(row["board_name"] or "").strip(),
        "checkin": str(row["checkin"] or "").strip(),
        "checkout": str(row["checkout"] or "").strip(),
        "holder_first_name": str(row["holder_first_name"] or "").strip(),
        "holder_last_name": str(row["holder_last_name"] or "").strip(),
        "holder_email": str(row["holder_email"] or "").strip(),
        "total_amount": str(row["total_amount"] or "").strip(),
        "currency": str(row["currency"] or "USD").upper(),
        "status": str(row["status"] or "confirmed").strip().lower(),
        "linked_flight_order_id": str(row["linked_flight_order_id"] or "").strip(),
        "linked_flight_booking_reference": str(row["linked_flight_booking_reference"] or "").strip().upper(),
        "created_at": str(row["created_at"] or "").strip(),
        "updated_at": str(row["updated_at"] or "").strip(),
    }


def _hotel_booking_by_reference(booking_reference: str) -> dict[str, Any] | None:
    ref = _normalize_booking_reference(booking_reference)
    if not ref:
        return None
    _ensure_account_db()
    with sqlite3.connect(ACCOUNT_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM hotel_bookings WHERE booking_reference = ?", (ref,)
        ).fetchone()
    return _hotel_booking_row_to_dict(row) if row else None


def _account_lookup(email: str) -> dict[str, Any] | None:
    key = _normalize_email(email)
    cached = USER_ACCOUNT_CACHE.get(key)
    if isinstance(cached, Mapping):
        return dict(cached)
    account = _db_fetch_account(key)
    if account:
        USER_ACCOUNT_CACHE.set(key, dict(account))
    return account


def _account_save(email: str, account: Mapping[str, Any]) -> None:
    key = _normalize_email(email)
    payload = dict(account)
    payload["email"] = key
    _db_upsert_account(payload)
    USER_ACCOUNT_CACHE.set(key, dict(payload))


def _session_account_email() -> str:
    email = _normalize_email(str(session.get("ngf_account_email") or ""))
    if not email:
        return ""
    session_nonce = str(session.get("ngf_account_nonce") or "").strip()
    if not session_nonce:
        return email
    account = _account_lookup(email)
    if not account:
        _set_session_account_email("")
        return ""
    if str(account.get("session_nonce") or "").strip() != session_nonce:
        _set_session_account_email("")
        return ""
    return email


def _set_session_account_email(email: str, *, session_nonce: str = "") -> None:
    normalized = _normalize_email(email)
    if normalized:
        session["ngf_account_email"] = normalized
        if session_nonce:
            session["ngf_account_nonce"] = str(session_nonce).strip()
        else:
            session.pop("ngf_account_nonce", None)
    else:
        session.pop("ngf_account_email", None)
        session.pop("ngf_account_nonce", None)


def _saved_search_summary(mode: str, params: Mapping[str, Any]) -> str:
    normalized_mode = str(mode or "").strip().lower()
    origin = str(params.get("origin") or "").strip().upper()
    destination = str(params.get("destination") or "").strip().upper()
    depart_date = str(params.get("depart_date") or "").strip()
    return_date = str(params.get("return_date") or "").strip()
    trip_type = str(params.get("trip_type") or "roundtrip").strip().lower()

    if normalized_mode == "ai":
        raw_text = str(params.get("raw_text") or "").strip()
        if raw_text:
            compact = re.sub(r"\s+", " ", raw_text)
            if len(compact) > 116:
                compact = f"{compact[:113]}..."
            return compact

    if normalized_mode == "flex":
        flex_month = str(params.get("flex_month") or "").strip()
        if origin and destination and flex_month:
            return f"Cheapest week {origin} → {destination} in {flex_month}"

    if origin and destination:
        route = f"{origin} → {destination}"
        if trip_type == "multicity":
            legs = params.get("legs") or []
            return f"Multi-city search · {len(legs) or 2} legs"
        if depart_date and return_date:
            return f"{route} · {depart_date} to {return_date}"
        if depart_date:
            return f"{route} · {depart_date}"
        return route

    return "Saved search"


def _saved_search_form_fields_from_params(mode: str, params: Mapping[str, Any]) -> list[tuple[str, str]]:
    normalized_mode = str(mode or "").strip().lower()
    fields: list[tuple[str, str]] = []
    if normalized_mode == "ai":
        ai_text = str(params.get("raw_text") or "").strip()
        if ai_text:
            fields.append(("mode", "ai"))
            fields.append(("ai_text", ai_text))
            parse_token = str(params.get("parse_token") or "").strip()
            if parse_token:
                fields.append(("parse_token", parse_token))
        return fields

    if normalized_mode == "flex":
        fields = [
            ("mode", "flex"),
            ("origin", str(params.get("origin") or "").strip().upper()),
            ("destination", str(params.get("destination") or "").strip().upper()),
            ("trip_type", str(params.get("trip_type") or "roundtrip").strip().lower() or "roundtrip"),
            ("flex_month", str(params.get("flex_month") or "").strip()),
            ("trip_length_days", str(params.get("trip_length_days") or DEFAULT_FLEX_TRIP_LENGTH_DAYS)),
            ("passengers", str(params.get("passengers") or DEFAULT_PASSENGERS)),
            ("cabin", str(params.get("cabin") or "ECONOMY").strip().upper() or "ECONOMY"),
            ("combination_mode", str(params.get("combination_mode") or "auto").strip().lower() or "auto"),
        ]
        if bool(params.get("nonstop")):
            fields.append(("nonstop", "on"))
        return [(name, value) for name, value in fields if value]

    fields = [
        ("mode", "standard"),
        ("origin", str(params.get("origin") or "").strip().upper()),
        ("destination", str(params.get("destination") or "").strip().upper()),
        ("trip_type", str(params.get("trip_type") or "roundtrip").strip().lower() or "roundtrip"),
        ("depart_date", str(params.get("depart_date") or "").strip()),
        ("return_date", str(params.get("return_date") or "").strip()),
        ("passengers", str(params.get("passengers") or DEFAULT_PASSENGERS)),
        ("cabin", str(params.get("cabin") or "ECONOMY").strip().upper() or "ECONOMY"),
        ("sort", str(params.get("sort") or "recommended").strip().lower() or "recommended"),
        ("combination_mode", str(params.get("combination_mode") or "auto").strip().lower() or "auto"),
    ]
    if bool(params.get("nonstop")):
        fields.append(("nonstop", "on"))

    if str(params.get("trip_type") or "").strip().lower() == "multicity":
        for leg in params.get("legs") or []:
            if not isinstance(leg, Mapping):
                continue
            fields.append(("leg_origin", str(leg.get("origin") or "").strip().upper()))
            fields.append(("leg_destination", str(leg.get("destination") or "").strip().upper()))
            fields.append(("leg_date", str(leg.get("depart_date") or "").strip()))

    return [(name, value) for name, value in fields if value]


def _record_search_for_signed_in_account(mode: str, params: Mapping[str, Any]) -> None:
    account_email = _session_account_email()
    if not account_email:
        return
    account = _account_lookup(account_email)
    if not account:
        return

    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"ai", "standard", "flex"}:
        return

    fields = _saved_search_form_fields_from_params(normalized_mode, params)
    if not fields:
        return

    fingerprint_payload = json.dumps({"mode": normalized_mode, "fields": fields}, separators=(",", ":"), ensure_ascii=True)
    fingerprint = hashlib.sha1(fingerprint_payload.encode("utf-8")).hexdigest()
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    entry = {
        "id": os.urandom(8).hex(),
        "mode": ("manual" if normalized_mode in {"standard", "flex"} else "ai"),
        "summary": _saved_search_summary(normalized_mode, params),
        "created_at": now_iso,
        "fingerprint": fingerprint,
        "fields": [[name, value] for name, value in fields],
    }

    existing = _safe_saved_searches(account.get("saved_searches"))
    if existing and str(existing[0].get("fingerprint") or "") == fingerprint:
        existing = existing[1:]
    account["saved_searches"] = [entry, *existing][:MAX_SAVED_SEARCHES_PER_ACCOUNT]
    _account_save(account_email, account)


def _saved_search_pairs_to_dict(pairs: Any) -> dict[str, str]:
    payload: dict[str, str] = {}
    for pair in pairs or []:
        if not pair or not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        key = str(pair[0] or "").strip()
        value = str(pair[1] or "").strip()
        if not key:
            continue
        payload[key] = value
    return payload


def _saved_search_card_view(item: Mapping[str, Any]) -> dict[str, Any]:
    pairs = item.get("fields") or []
    values = _saved_search_pairs_to_dict(pairs)
    origin = (values.get("origin") or "").upper()
    destination = (values.get("destination") or "").upper()
    mode = str(item.get("mode") or "manual").strip().lower()
    trip_type = (values.get("trip_type") or "roundtrip").strip().lower()
    depart_date = (values.get("depart_date") or "").strip()
    return_date = (values.get("return_date") or "").strip()
    flex_month = (values.get("flex_month") or "").strip()
    trip_length = (values.get("trip_length_days") or "").strip()
    cabin_raw = (values.get("cabin") or "ECONOMY").strip().replace("_", " ").title()
    cabin = "Premium Economy" if cabin_raw.lower() == "premium economy" else cabin_raw
    budget_raw = (values.get("max_price") or values.get("budget_max") or "").strip()
    budget = f"${budget_raw}" if budget_raw.isdigit() else ""

    route = f"{origin} → {destination}" if origin and destination else "Flexible route"
    date_label = "Flexible dates"
    if mode == "ai":
        date_label = "AI itinerary"
    elif flex_month:
        duration = f" ({trip_length} days)" if trip_length else ""
        date_label = f"{flex_month}{duration}"
    elif depart_date and return_date:
        date_label = f"{depart_date} to {return_date}"
    elif depart_date:
        date_label = depart_date

    insight = ""
    if mode == "flex":
        insight = "AI insight: Mid-week departures are typically lower fare."
    elif trip_type == "roundtrip":
        insight = "AI insight: Round-trip combinations can reduce total fare."
    elif trip_type == "oneway":
        insight = "AI insight: One-way fares are best when departure is flexible."
    else:
        insight = "AI insight: Multi-city works best when legs are spaced by 2-4 days."

    return {
        "mode_label": "AI Search" if mode == "ai" else "Manual Search",
        "summary": str(item.get("summary") or "Saved search").strip() or "Saved search",
        "created_at": str(item.get("created_at") or "").replace("T", " ").strip(),
        "route": route,
        "date_label": date_label,
        "cabin": cabin or "Economy",
        "budget": budget,
        "insight": insight,
        "fields": pairs,
    }


def _top_routes_from_saved_searches(saved_searches: Sequence[Mapping[str, Any]]) -> list[str]:
    route_counts: Counter[str] = Counter()
    for item in saved_searches:
        payload = _saved_search_pairs_to_dict(item.get("fields") or [])
        origin = (payload.get("origin") or "").upper()
        destination = (payload.get("destination") or "").upper()
        if origin and destination:
            route_counts[f"{origin} → {destination}"] += 1
    return [route for route, _ in route_counts.most_common(3)]


def _update_account_login_metadata(account: dict[str, Any]) -> dict[str, Any]:
    payload = dict(account)
    payload["last_login_at"] = datetime.utcnow().isoformat(timespec="seconds")
    payload["last_login_ip"] = str(request.headers.get("X-Forwarded-For") or request.remote_addr or "").strip()
    payload["session_nonce"] = os.urandom(12).hex()
    return payload


@app.context_processor
def inject_portal_account_context() -> dict[str, Any]:
    account_email = _session_account_email()
    account = _account_lookup(account_email) if account_email else None
    base = {
        "airport_header_label_local": _airport_header_label_local,
    }
    if not account:
        return {
            **base,
            "portal_signed_in": False,
            "portal_account_email": "",
            "portal_account_name": "",
            "portal_account_initials": "",
        }
    return {
        **base,
        "portal_signed_in": True,
        "portal_account_email": account_email,
        "portal_account_name": _account_display_name(account),
        "portal_account_initials": _account_initials(account),
    }


def _set_manage_account_notice(*, notice: str = "", error: str = "") -> None:
    if notice:
        session["ngf_manage_account_notice"] = notice
    else:
        session.pop("ngf_manage_account_notice", None)
    if error:
        session["ngf_manage_account_error"] = error
    else:
        session.pop("ngf_manage_account_error", None)


def _pop_manage_account_notice() -> tuple[str, str]:
    return (
        str(session.pop("ngf_manage_account_notice", "") or "").strip(),
        str(session.pop("ngf_manage_account_error", "") or "").strip(),
    )


def _set_global_notice(message: str = "") -> None:
    value = str(message or "").strip()
    if value:
        session["ngf_global_notice"] = value
    else:
        session.pop("ngf_global_notice", None)


def _pop_global_notice() -> str:
    return str(session.pop("ngf_global_notice", "") or "").strip()


def _set_edit_search_fields(fields: list[tuple[str, str]]) -> None:
    session["ngf_edit_search_fields"] = [[str(name or ""), str(value or "")] for name, value in (fields or [])]


def _pop_edit_search_fields() -> list[list[str]]:
    payload = session.pop("ngf_edit_search_fields", [])
    if not isinstance(payload, list):
        return []
    cleaned: list[list[str]] = []
    for pair in payload:
        if not isinstance(pair, (list, tuple)) or len(pair) < 2:
            continue
        key = str(pair[0] or "").strip()
        value = str(pair[1] or "")
        if not key:
            continue
        cleaned.append([key, value])
    return cleaned


# ---------------------------------------------------------------------------
# Combined flight + hotel trip intent
#
# Carries "the user also wants a hotel, with these filters, for these dates"
# from the initial AI parse through flight selection into a pre-filtered
# hotel search, and from hotel selection into a combined checkout. Follows
# the same discipline as every other "big object" in this codebase (recent
# order cache, AI parse cache, etc.): the session itself is a signed
# client-side cookie with a small size cap, so only IDs and tiny display
# snapshots live here — never a full flight offer or hotel prebook payload.
# ---------------------------------------------------------------------------
TRIP_INTENT_SESSION_KEY = "ngf_trip_intent"
TRIP_INTENT_VERSION = 1


def _start_trip_intent(combined_parse: Mapping[str, Any]) -> None:
    """Seed a fresh trip_intent from a combined (flight+stay) AI parse.

    A no-op (clears any stale trip_intent) if the combined parse didn't
    actually produce a usable stay — never starts a hotel leg with nothing
    to search for.
    """
    stay = combined_parse.get("stay") if isinstance(combined_parse.get("stay"), Mapping) else None
    if not stay or not str(stay.get("destination") or "").strip():
        _clear_trip_intent()
        return

    flight = combined_parse.get("flight") if isinstance(combined_parse.get("flight"), Mapping) else {}
    session[TRIP_INTENT_SESSION_KEY] = {
        "v": TRIP_INTENT_VERSION,
        "created_at": int(time.time()),
        "raw_text": str(flight.get("raw_text") or stay.get("raw_text") or "")[:300],
        "wants_hotel": True,
        "stay_dates_explicit": bool(combined_parse.get("stay_dates_explicit")),
        "hotel_filters": {
            "min_stars": stay.get("min_stars"),
            "min_rating": stay.get("min_rating"),
            "max_price_per_night": stay.get("max_price_per_night"),
            "free_cancellation": bool(stay.get("free_cancellation")),
            "breakfast": bool(stay.get("breakfast")),
            "amenities": list(stay.get("amenities") or [])[:6],
            "sort": stay.get("sort") or "recommended",
            "adults": int(stay.get("adults") or 2),
            "rooms": int(stay.get("rooms") or 1),
            "children_ages": list(stay.get("children_ages") or [])[:4],
        },
        "hotel_dates": {
            "checkin": str(stay.get("checkin") or ""),
            "checkout": str(stay.get("checkout") or ""),
        },
        "hotel_destination_text": str(stay.get("destination") or ""),
        "destination_iata": str(flight.get("destination") or "").strip().upper(),
        "stage": "searching",
        "flight_offer_id": None,
        "flight_snapshot": None,
        "hotel_place_id": None,
        "hotel_id": None,
        "hotel_offer_id": None,     # LiteAPI room-rate offer_id — needed to re-prebook for a price refresh
        "hotel_prebook_id": None,
        "hotel_checkin": None,
        "hotel_checkout": None,
        "hotel_rooms": None,
        "hotel_adults": None,
        "hotel_snapshot": None,
    }


def _get_trip_intent() -> dict[str, Any] | None:
    payload = session.get(TRIP_INTENT_SESSION_KEY)
    if not isinstance(payload, dict) or not payload.get("wants_hotel"):
        return None
    return payload


def _update_trip_intent(**fields: Any) -> dict[str, Any] | None:
    """Shallow-merge fields into the existing trip_intent. No-op if there
    isn't one (callers should already have checked _get_trip_intent)."""
    current = session.get(TRIP_INTENT_SESSION_KEY)
    if not isinstance(current, dict):
        return None
    current.update(fields)
    if app.debug:
        # Flask's session here is a signed client-side cookie (~4KB cap) —
        # a well-intentioned future addition (e.g. caching a full offer)
        # could silently blow past that and start truncating sessions in
        # production. Catch it in dev rather than in someone's browser.
        size = len(json.dumps(current, default=str))
        assert size < 3000, f"ngf_trip_intent grew to {size} bytes — keep it to IDs/snapshots, not full payloads"
    session[TRIP_INTENT_SESSION_KEY] = current
    return current


def _clear_trip_intent() -> None:
    session.pop(TRIP_INTENT_SESSION_KEY, None)


@app.context_processor
def inject_trip_intent_context() -> dict[str, Any]:
    return {"trip_intent": _get_trip_intent()}


def _safe_next_url(value: str | None) -> str:
    """Only allow same-origin relative paths as a post-auth redirect target.

    Rejects absolute URLs and protocol-relative ("//host/...") values so a
    crafted `next` param can't be used as an open redirect.
    """
    text = str(value or "").strip()
    if not text or not text.startswith("/") or text.startswith("//"):
        return ""
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme or parsed.netloc:
        return ""
    return text


def _auth_redirect(
    *,
    mode: str = "login",
    booking_reference: str = "",
    reset_email: str = "",
    reset_token: str = "",
    next_url: str = "",
):
    payload: dict[str, Any] = {"mode": (mode or "login").strip().lower()}
    if booking_reference:
        payload["booking_reference"] = _normalize_booking_reference(booking_reference)
    if reset_email:
        payload["reset_email"] = _normalize_email(reset_email)
    if reset_token:
        payload["reset_token"] = str(reset_token).strip()
    safe_next = _safe_next_url(next_url)
    if safe_next:
        payload["next"] = safe_next
    return redirect(url_for("auth_page", **payload))


def _clear_manage_reset_state() -> None:
    for key in (
        "ngf_reset_email_pending",
        "ngf_reset_code",
        "ngf_reset_code_expires_at",
        "ngf_reset_token",
        "ngf_reset_token_email",
        "ngf_reset_token_expires_at",
    ):
        session.pop(key, None)


def _store_manage_reset_code(email: str, code: str, *, ttl_seconds: int = 10 * 60) -> None:
    session["ngf_reset_email_pending"] = _normalize_email(email)
    session["ngf_reset_code"] = str(code or "").strip()
    session["ngf_reset_code_expires_at"] = int(time.time()) + max(60, int(ttl_seconds))


def _issue_manage_reset_token(email: str, *, ttl_seconds: int = 15 * 60) -> str:
    token = os.urandom(24).hex()
    session["ngf_reset_token"] = token
    session["ngf_reset_token_email"] = _normalize_email(email)
    session["ngf_reset_token_expires_at"] = int(time.time()) + max(60, int(ttl_seconds))
    return token


def _link_booking_to_account(email: str, booking_reference: str) -> None:
    key = _normalize_email(email)
    ref = _normalize_booking_reference(booking_reference)
    if not key or not ref:
        return
    account = _account_lookup(key)
    if not account:
        return
    linked = {str(item).strip().upper() for item in (account.get("linked_booking_references") or []) if str(item).strip()}
    linked.add(ref)
    account["linked_booking_references"] = sorted(linked)
    _account_save(key, account)


def _record_booking_email_link(*, email: str, booking_reference: str, order_id: str = "") -> None:
    key = _normalize_email(email)
    ref = _normalize_booking_reference(booking_reference)
    order = str(order_id or "").strip()
    if not key or re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", key) is None or not ref:
        return

    _ensure_account_db()
    now_iso = datetime.utcnow().isoformat(timespec="seconds")
    with _ACCOUNT_DB_LOCK:
        query = """
                INSERT INTO manage_booking_email_links (
                    email,
                    booking_reference,
                    order_id,
                    created_at,
                    updated_at
                )
                VALUES ({placeholders})
                ON CONFLICT(email, booking_reference) DO UPDATE SET
                    order_id = CASE
                        WHEN excluded.order_id <> '' THEN excluded.order_id
                        ELSE manage_booking_email_links.order_id
                    END,
                    updated_at = excluded.updated_at
                """
        values = (key, ref, order, now_iso, now_iso)
        if _use_postgres_account_store():
            with _postgres_account_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query.format(placeholders=", ".join(["%s"] * len(values))), values)
        else:
            with sqlite3.connect(ACCOUNT_DB_PATH) as conn:
                _ensure_account_booking_email_link_table(conn)
                conn.execute(query.format(placeholders=", ".join(["?"] * len(values))), values)
                conn.commit()


def _booking_links_for_email(email: str) -> list[str]:
    key = _normalize_email(email)
    if not key:
        return []
    _ensure_account_db()
    with _ACCOUNT_DB_LOCK:
        query = """
                SELECT booking_reference
                FROM manage_booking_email_links
                WHERE email = {placeholder}
                ORDER BY updated_at DESC
                LIMIT 1000
                """
        if _use_postgres_account_store():
            with _postgres_account_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(query.format(placeholder="%s"), (key,))
                    rows = cur.fetchall()
        else:
            with sqlite3.connect(ACCOUNT_DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                _ensure_account_booking_email_link_table(conn)
                rows = conn.execute(query.format(placeholder="?"), (key,)).fetchall()
    refs: list[str] = []
    seen: set[str] = set()
    for row in rows:
        ref = _normalize_booking_reference(str(row["booking_reference"] or ""))
        if not ref or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
    return refs


def _sync_account_bookings_by_email(email: str) -> int:
    key = _normalize_email(email)
    if not key:
        return 0
    account = _account_lookup(key)
    if not account:
        return 0
    existing = {
        _normalize_booking_reference(str(item))
        for item in (account.get("linked_booking_references") or [])
        if _normalize_booking_reference(str(item))
    }
    linked_before = len(existing)
    for ref in _booking_links_for_email(key):
        existing.add(ref)
    if len(existing) == linked_before:
        return 0
    account["linked_booking_references"] = sorted(existing)
    _account_save(key, account)
    return len(existing) - linked_before


def _capture_booking_email_links(
    *,
    order: Mapping[str, Any],
    passengers_payload: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    booking_reference = _normalize_booking_reference(str(order.get("booking_reference") or ""))
    if not booking_reference:
        return
    order_id = str(order.get("id") or "").strip()
    account_email = _session_account_email() if has_request_context() else ""
    recipients = _collect_itinerary_email_recipients(
        passengers_payload,
        account_email=account_email,
    )
    for candidate in _order_passenger_emails(order):
        if candidate not in recipients:
            recipients.append(candidate)
    for email in recipients:
        _record_booking_email_link(
            email=email,
            booking_reference=booking_reference,
            order_id=order_id,
        )
        _link_booking_to_account(email, booking_reference)


def _discover_recent_booking_links_for_email(email: str, *, max_orders: int = 50) -> int:
    key = _normalize_email(email)
    if not key:
        return 0
    if app.config.get("TESTING"):
        return 0
    if not DUFFEL_ACCESS_TOKEN:
        return 0

    try:
        recent_orders = DUFF.list_orders(limit=max(1, min(int(max_orders or 50), 50)))
    except DuffelAPIError:
        return 0
    except Exception:
        return 0

    linked_refs: set[str] = set()
    for order in recent_orders:
        if not isinstance(order, Mapping):
            continue
        booking_reference = _normalize_booking_reference(str(order.get("booking_reference") or ""))
        if not booking_reference:
            continue
        if key not in _order_passenger_emails(order):
            continue
        linked_refs.add(booking_reference)
        _record_booking_email_link(
            email=key,
            booking_reference=booking_reference,
            order_id=str(order.get("id") or "").strip(),
        )
        _link_booking_to_account(key, booking_reference)
    return len(linked_refs)


def _latest_order_for_reference(booking_reference: str) -> dict[str, Any] | None:
    ref = _normalize_booking_reference(booking_reference)
    if not ref:
        return None
    try:
        orders = DUFF.list_orders(booking_reference=ref, limit=10)
    except DuffelAPIError:
        return None
    for order in orders:
        order_ref = _normalize_booking_reference(str(order.get("booking_reference") or ""))
        if order_ref == ref:
            return order
    return None


def _order_available_actions(order: Mapping[str, Any]) -> set[str]:
    actions = order.get("available_actions") or []
    out: set[str] = set()
    if isinstance(actions, list):
        for item in actions:
            value = str(item).strip().lower()
            if value:
                out.add(value)
    return out


def _order_change_candidates(order: Mapping[str, Any]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    for idx, slice_data in enumerate(order.get("slices") or []):
        segments = slice_data.get("segments") or []
        if not segments:
            continue
        first = segments[0]
        last = segments[-1]
        origin = str(((first.get("origin") or {}).get("iata_code")) or "").strip().upper()
        destination = str(((last.get("destination") or {}).get("iata_code")) or "").strip().upper()
        slice_id = str(slice_data.get("id") or "").strip()
        if not origin or not destination or not slice_id:
            continue
        label = "Outbound" if idx == 0 else ("Return" if idx == 1 else f"Leg {idx + 1}")
        candidates.append(
            {
                "slice_id": slice_id,
                "origin": origin,
                "destination": destination,
                "label": label,
                "route": f"{origin} -> {destination}",
            }
        )
    return candidates


def _build_manage_booking_model(order: Mapping[str, Any]) -> dict[str, Any]:
    actions = _order_available_actions(order)
    return {
        "order_id": str(order.get("id") or "").strip(),
        "booking_reference": _normalize_booking_reference(str(order.get("booking_reference") or "")),
        "can_cancel": "cancel" in actions,
        "can_change": bool({"change", "change_flight", "change_flights"} & actions),
        "change_candidates": _order_change_candidates(order),
    }


def _render_manage_booking_page(
    *,
    booking_error: str = "",
    order: Mapping[str, Any] | None = None,
    form_values: Mapping[str, Any] | None = None,
    verification_note: str = "",
    signup_offer: bool = False,
    account_notice: str = "",
    account_error: str = "",
    change_notice: str = "",
    change_error: str = "",
    change_offers: list[dict[str, Any]] | None = None,
    auth_mode: str = "",
    reset_email: str = "",
    reset_token: str = "",
) -> str:
    account_email = _session_account_email()
    account = _account_lookup(account_email) if account_email else None
    linked_bookings = (account or {}).get("linked_booking_references") or []
    linked_booking_summaries: list[dict[str, str]] = []
    for linked_ref in linked_bookings:
        normalized_ref = _normalize_booking_reference(str(linked_ref or ""))
        if not normalized_ref:
            continue
        summary = {
            "booking_reference": normalized_ref,
            "route": "Route unavailable",
            "status": "Booked",
            "departure": "",
        }
        # Hotel bookings live in the local table (SKH-prefixed refs), not
        # Duffel — resolve those first so they never hit list_orders/get_offer.
        if normalized_ref.startswith("SKH"):
            hotel_booking = _hotel_booking_by_reference(normalized_ref)
            if hotel_booking:
                summary["route"] = hotel_booking.get("hotel_name") or "Hotel stay"
                summary["status"] = (hotel_booking.get("status") or "confirmed").title()
                summary["departure"] = hotel_booking.get("checkin") or ""
            linked_booking_summaries.append(summary)
            continue
        # Prefer the ref cache (updated after cancellation/confirmation) over a fresh
        # list_orders call which may lag in reflecting booking_status changes.
        linked_order = RECENT_REF_CACHE.get(normalized_ref) or _latest_order_for_reference(normalized_ref)
        if linked_order:
            linked_order_summary = build_order_summary(linked_order)
            if linked_order_summary:
                slices = linked_order_summary.get("slices") or []
                first_slice = slices[0] if slices else {}
                route = str((first_slice or {}).get("route") or "").strip()
                if route:
                    summary["route"] = route
                departure = str((first_slice or {}).get("depart_label") or "").strip()
                if departure:
                    summary["departure"] = departure
                status_value = linked_order_summary.get("status_label")
                status = str(status_value or "").strip()
                if status and "," not in status and "none" not in status.lower():
                    summary["status"] = status
                else:
                    raw_status = str(linked_order.get("booking_status") or linked_order.get("status") or "").strip().replace("_", " ").title()
                    if raw_status:
                        summary["status"] = raw_status
        linked_booking_summaries.append(summary)
    account_profile = {
        "first_name": _normalize_person_name(str((account or {}).get("first_name") or "")),
        "last_name": _normalize_person_name(str((account or {}).get("last_name") or "")),
        "dob": str((account or {}).get("dob") or "").strip(),
    }
    order_summary = build_order_summary(order) if order else None
    manage_model = _build_manage_booking_model(order) if order else None
    return render_template(
        "manage_booking.html",
        booking_error=booking_error,
        order_summary=order_summary,
        manage_model=manage_model,
        form_values=dict(form_values or {"booking_reference": "", "last_name": "", "dob": ""}),
        verification_note=verification_note,
        signup_offer=signup_offer,
        account_notice=account_notice,
        account_error=account_error,
        account_email=account_email,
        account_profile=account_profile,
        linked_bookings=linked_bookings,
        linked_booking_summaries=linked_booking_summaries,
        change_notice=change_notice,
        change_error=change_error,
        change_offers=change_offers or [],
        auth_mode=auth_mode,
        reset_email=reset_email,
        reset_token=reset_token,
        duffel_env=DUFFEL_ENV,
        csrf_token=_b2c_csrf_token(),
    )


def _load_checkout_sidecars(offer: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if app.config.get("TESTING"):
        return [], {
            "mode": "balance",
            "three_d_secure_supported": False,
            "billing_required": False,
            "component_client_key": "",
        }

    seat_maps: list[dict[str, Any]] = []
    payment_config: dict[str, Any] = {
        "mode": DUFFEL_PAYMENT_MODE,
        "three_d_secure_supported": True,
        "billing_required": DUFFEL_PAYMENT_MODE == "card",
        "component_client_key": "",
    }
    offer_id = str(offer.get("id") or "").strip()
    if not offer_id:
        return seat_maps, payment_config

    try:
        seat_maps = DUFF.get_seat_maps(offer_id)
    except DuffelAPIError as exc:
        print("OPTIONAL SEAT MAP LOAD FAILED:", str(exc))

    if payment_config["mode"] == "card":
        try:
            payment_config["component_client_key"] = DUFF.create_component_client_key()
        except DuffelAPIError as exc:
            print("OPTIONAL COMPONENT KEY LOAD FAILED:", str(exc))

    return seat_maps, payment_config

# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@app.route("/checkout/<offer_id>", methods=["GET"])
def checkout_offer(offer_id: str):
    mode_error = _booking_mode_error()
    if mode_error:
        return render_template(
            "booking_review.html",
            offer_summary=None,
            checkout_model=None,
            travelers=[],
            errors={},
            booking_error=mode_error,
            booking_enabled=False,
            duffel_env=DUFFEL_ENV,
        ), 503

    try:
        offer = DUFF.get_offer(offer_id)
    except DuffelAPIError as exc:
        return render_template(
            "booking_review.html",
            offer_summary=None,
            checkout_model=None,
            travelers=[],
            errors={},
            booking_error=str(exc),
            booking_enabled=False,
            duffel_env=DUFFEL_ENV,
        ), _booking_status_code(exc.status_code)

    seat_policy = _seat_selection_policy(offer, seat_maps=[])
    travelers = build_traveler_forms(offer)
    offer_summary = build_checkout_summary(offer, seat_maps=[], ancillaries_payload={})
    selected_fare_option = _review_fare_option_from_offer(
        offer,
        selected_offer_id=offer_id,
        fallback_offer=offer,
    )
    checkout_model = {
        "fare_options": [selected_fare_option] if selected_fare_option else [],
        "seat_policy": seat_policy,
    }
    is_expired = offer_has_expired(offer)
    expiry_error = "This offer has expired. Please head back to the results and choose a fresh option." if is_expired else ""
    if not is_expired:
        _track_offer_funnel_event(event_type="flight_selected", offer=offer, step="review_page")

    return render_template(
        "booking_review.html",
        offer_summary=offer_summary,
        travelers=travelers,
        checkout_model=checkout_model,
        errors={},
        booking_error=expiry_error,
        booking_enabled=not is_expired,
        duffel_env=DUFFEL_ENV,
    ), (410 if is_expired else 200)


@app.route("/checkout/<offer_id>/fare-options", methods=["GET"])
def checkout_fare_options(offer_id: str):
    try:
        offer = DUFF.get_offer(offer_id, return_available_services=True)
    except DuffelAPIError as exc:
        return "", _booking_status_code(exc.status_code)

    fare_options = _build_review_fare_options(offer)
    offer_summary = build_checkout_summary(offer, seat_maps=[], ancillaries_payload={})
    html = render_template(
        "partials/fare_options_cards.html",
        fare_options=fare_options,
        cabin_label=offer_summary.get("cabin_label", ""),
    )
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/checkout/<offer_id>/seats", methods=["GET"])
def checkout_seats(offer_id: str):
    mode_error = _booking_mode_error()
    if mode_error:
        return render_template(
            "seat_selection.html",
            offer_summary=None,
            checkout_model=None,
            travelers=[],
            booking_error=mode_error,
            booking_enabled=False,
            duffel_env=DUFFEL_ENV,
        ), 503

    try:
        offer = DUFF.get_offer(offer_id, return_available_services=True)
    except DuffelAPIError as exc:
        return render_template(
            "seat_selection.html",
            offer_summary=None,
            checkout_model=None,
            travelers=[],
            booking_error=str(exc),
            booking_enabled=False,
            duffel_env=DUFFEL_ENV,
        ), _booking_status_code(exc.status_code)

    seat_maps, payment_config = _load_checkout_sidecars(offer)
    seat_policy = _seat_selection_policy(offer, seat_maps=seat_maps)
    ancillaries_payload = extract_ancillaries_payload(request.args)
    travelers = build_traveler_forms(offer)
    offer_summary = build_checkout_summary(offer, seat_maps=seat_maps, ancillaries_payload=ancillaries_payload)
    checkout_model = build_checkout_page_model(
        offer,
        travelers=travelers,
        seat_maps=seat_maps,
        ancillaries_payload=ancillaries_payload,
        payment_config=payment_config,
        duffel_env=DUFFEL_ENV,
    )
    checkout_model["seat_policy"] = seat_policy
    checkout_model["duffel_ancillaries_embed"] = build_duffel_ancillaries_embed_model(
        offer,
        seat_maps=seat_maps,
        travelers=travelers,
        seat_policy=seat_policy,
    )
    is_expired = offer_has_expired(offer)
    expiry_error = "This offer has expired. Please head back to the results and choose a fresh option." if is_expired else ""
    if not is_expired:
        _track_offer_funnel_event(event_type="booking_intent", offer=offer, step="seat_selection")

    return render_template(
        "seat_selection.html",
        offer_summary=offer_summary,
        checkout_model=checkout_model,
        travelers=travelers,
        booking_error=expiry_error,
        booking_enabled=not is_expired,
        duffel_env=DUFFEL_ENV,
    ), (410 if is_expired else 200)


@app.route("/checkout/<offer_id>/ancillaries", methods=["POST"])
def checkout_ancillaries_to_session(offer_id: str):
    """Persist Duffel ancillaries selection server-side (payload can exceed query-string limits)."""
    if _booking_mode_error():
        return redirect(url_for("checkout_details", offer_id=offer_id))
    payload = extract_ancillaries_payload(request.form)
    session[_session_ancillaries_key(offer_id)] = json.dumps(payload)
    return redirect(url_for("checkout_details", offer_id=offer_id))


@app.route("/checkout/<offer_id>/details", methods=["GET", "POST"])
def checkout_details(offer_id: str):
    mode_error = _demo_checkout_lock_error() or _booking_mode_error()
    if mode_error:
        return render_template(
            "checkout.html",
            offer_summary=None,
            checkout_model=None,
            travelers=[],
            errors={},
            booking_error=mode_error,
            booking_enabled=False,
            duffel_env=DUFFEL_ENV,
        ), 503

    try:
        offer = DUFF.get_offer(offer_id, return_available_services=True)
    except DuffelAPIError as exc:
        return render_template(
            "checkout.html",
            offer_summary=None,
            checkout_model=None,
            travelers=[],
            errors={},
            booking_error=str(exc),
            booking_enabled=False,
            duffel_env=DUFFEL_ENV,
        ), _booking_status_code(exc.status_code)

    seat_maps, payment_config = _load_checkout_sidecars(offer)
    seat_policy = _seat_selection_policy(offer, seat_maps=seat_maps)
    ancillaries_payload = extract_ancillaries_payload(request.form if request.method == "POST" else request.args)
    if request.method == "GET" and not (ancillaries_payload.get("services") or ancillaries_payload.get("selected_services")):
        raw = session.get(_session_ancillaries_key(offer_id))
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                ancillaries_payload = parsed
    travelers = build_traveler_forms(offer, request.form if request.method == "POST" else None)
    offer_summary = build_checkout_summary(offer, seat_maps=seat_maps, ancillaries_payload=ancillaries_payload)
    checkout_model = build_checkout_page_model(
        offer,
        travelers=travelers,
        seat_maps=seat_maps,
        ancillaries_payload=ancillaries_payload,
        payment_config=payment_config,
        duffel_env=DUFFEL_ENV,
    )
    checkout_model["seat_policy"] = seat_policy
    is_expired = offer_has_expired(offer)
    expiry_error = "This offer has expired. Please head back to the results and choose a fresh option." if is_expired else ""
    seat_notice = str(request.args.get("seat_notice") or "").strip() if request.method == "GET" else ""
    payment_ready = (
        str(payment_config.get("mode") or "").lower() != "card"
        or bool(str(payment_config.get("component_client_key") or "").strip())
    )
    payment_notice = "" if payment_ready else "Secure card collection could not be prepared. Refresh checkout and try again."
    pre_submit_notice = seat_notice or expiry_error or payment_notice
    if request.method == "GET" and not is_expired:
        _track_offer_funnel_event(event_type="booking_intent", offer=offer, step="traveler_checkout")

    if request.method == "POST":
        if not _validate_b2c_csrf():
            return "Invalid or missing CSRF token.", 403
        if is_expired:
            return render_template(
                "checkout.html",
                offer_summary=offer_summary,
                travelers=travelers,
                checkout_model=checkout_model,
                errors={},
                booking_error=pre_submit_notice,
                booking_enabled=False,
                duffel_env=DUFFEL_ENV,
                duffel_components_version=DUFFEL_COMPONENTS_VERSION,
            ), 410

        passengers_payload, travelers, errors = validate_checkout_form(offer, request.form)
        checkout_model = build_checkout_page_model(
            offer,
            travelers=travelers,
            seat_maps=seat_maps,
            ancillaries_payload=ancillaries_payload,
            payment_config=payment_config,
            duffel_env=DUFFEL_ENV,
        )
        checkout_model["seat_policy"] = seat_policy
        if errors:
            return render_template(
                "checkout.html",
                offer_summary=offer_summary,
                travelers=travelers,
                checkout_model=checkout_model,
                errors=errors,
                booking_error=errors.get("form", ""),
                booking_enabled=payment_ready,
                duffel_env=DUFFEL_ENV,
                duffel_components_version=DUFFEL_COMPONENTS_VERSION,
            ), 400

        selected_services = selected_services_from_payload(ancillaries_payload)
        order_services = normalize_create_order_services(selected_services)
        total_amount = calculate_total_amount(offer, ancillaries_payload, seat_maps=seat_maps)
        total_currency = str(offer_summary.get("currency") or "USD")
        total_amount_str = str(total_amount or offer_summary.get("total_amount") or "0.00")
        payments_payload = None
        if str(payment_config.get("mode") or "").lower() == "card":
            three_d_secure_session_id = str(request.form.get("duffel_three_d_secure_session_id") or "").strip()
            if not three_d_secure_session_id:
                return render_template(
                    "checkout.html",
                    offer_summary=offer_summary,
                    travelers=travelers,
                    checkout_model=checkout_model,
                    errors={},
                    booking_error="Please enter your card details and complete card authentication before booking.",
                    booking_enabled=payment_ready,
                    duffel_env=DUFFEL_ENV,
                    duffel_components_version=DUFFEL_COMPONENTS_VERSION,
                ), 400
            payments_payload = [
                {
                    "type": "card",
                    "currency": total_currency,
                    "amount": total_amount_str,
                    "three_d_secure_session_id": three_d_secure_session_id,
                }
            ]
        try:
            order = DUFF.create_order(
                offer_id=(offer.get("id") or offer_id).strip(),
                passengers=passengers_payload,
                services=order_services or None,
                total_amount=total_amount_str,
                total_currency=total_currency,
                payments=payments_payload,
            )
        except DuffelAPIError as exc:
            return render_template(
                "checkout.html",
                offer_summary=offer_summary,
                travelers=travelers,
                checkout_model=checkout_model,
                errors={},
                booking_error=str(exc),
                booking_enabled=payment_ready,
                duffel_env=DUFFEL_ENV,
                duffel_components_version=DUFFEL_COMPONENTS_VERSION,
            ), _booking_status_code(exc.status_code)

        order_id = str(order.get("id") or "").strip()
        if order_id:
            session.pop(_session_ancillaries_key(offer_id), None)
            RECENT_ORDER_CACHE.set(order_id, order)
            _session_authorize_order(order_id)
            _capture_booking_email_links(
                order=order,
                passengers_payload=passengers_payload,
            )
            _track_booking_completed_event(order, offer=offer)
            _record_agent_booking(order, offer=offer)
            try:
                _send_itinerary_emails_after_booking(
                    order=order,
                    passengers_payload=passengers_payload,
                )
            except Exception as exc:
                print(f"ITINERARY EMAIL ERROR: {type(exc).__name__}: {exc}")
            return redirect(url_for("booking_confirmation", order_id=order_id))

        return render_template(
            "confirmation.html",
            order_summary=build_order_summary(order),
            booking_error="",
            duffel_env=DUFFEL_ENV,
        )

    return render_template(
        "checkout.html",
        offer_summary=offer_summary,
        travelers=travelers,
        checkout_model=checkout_model,
        errors={},
        booking_error=pre_submit_notice,
        booking_enabled=not is_expired and payment_ready,
        duffel_env=DUFFEL_ENV,
        duffel_components_version=DUFFEL_COMPONENTS_VERSION,
    ), (410 if is_expired else 200)


@app.route("/booking/confirmation/<order_id>")
def booking_confirmation(order_id: str):
    if not _session_is_order_authorized(order_id):
        return redirect(url_for("manage_booking"))
    order = RECENT_ORDER_CACHE.get(order_id)
    if order is None:
        mode_error = _booking_mode_error()
        if mode_error:
            return render_template(
                "confirmation.html",
                order_summary=None,
                booking_error=mode_error,
                duffel_env=DUFFEL_ENV,
            ), 503
        try:
            order = DUFF.get_order(order_id)
        except DuffelAPIError as exc:
            return render_template(
                "confirmation.html",
                order_summary=None,
                booking_error=str(exc),
                duffel_env=DUFFEL_ENV,
            ), _booking_status_code(exc.status_code)
        RECENT_ORDER_CACHE.set(order_id, order)

    summary = build_order_summary(order)
    return render_template(
        "confirmation.html",
        order_summary=summary,
        booking_error="",
        duffel_env=DUFFEL_ENV,
        checkin_url=_airline_checkin_url(summary.get("airline_iata")),
    )


@app.route("/booking/itinerary/<order_id>")
def booking_itinerary(order_id: str):
    if not _session_is_order_authorized(order_id):
        return redirect(url_for("manage_booking"))
    order = RECENT_ORDER_CACHE.get(order_id)
    if order is None:
        try:
            order = DUFF.get_order(order_id)
        except DuffelAPIError as exc:
            return str(exc), _booking_status_code(exc.status_code)
        RECENT_ORDER_CACHE.set(order_id, order)
    summary = build_order_summary(order)
    ref = summary.get("booking_reference") or order_id
    # ?inline=1  → open in browser viewer (View receipt); omitted → download
    inline = request.args.get("inline", "").strip() in {"1", "true", "yes"}
    disposition = "inline" if inline else f'attachment; filename="itinerary-{ref}.pdf"'
    try:
        pdf_bytes = _render_itinerary_pdf(summary)
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": disposition},
        )
    except Exception:
        html = _render_itinerary_html(summary)
        html_disp = "inline" if inline else f'attachment; filename="itinerary-{ref}.html"'
        return Response(
            html,
            mimetype="text/html",
            headers={"Content-Disposition": html_disp},
        )


@app.route("/booking/calendar/<order_id>")
def booking_calendar(order_id: str):
    if not _session_is_order_authorized(order_id):
        return redirect(url_for("manage_booking"))
    order = RECENT_ORDER_CACHE.get(order_id)
    if order is None:
        try:
            order = DUFF.get_order(order_id)
        except DuffelAPIError as exc:
            return str(exc), _booking_status_code(exc.status_code)
        RECENT_ORDER_CACHE.set(order_id, order)
    summary = build_order_summary(order)
    ics = _render_ics(summary)
    ref = summary.get("booking_reference") or order_id
    return Response(
        ics,
        mimetype="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="flight-{ref}.ics"'},
    )


def _render_itinerary_pdf(summary: dict) -> bytes:
    """E-ticket PDF matching Skairova reference design — white, clean, professional."""
    import io
    from datetime import datetime as _dt
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.graphics.barcode.code128 import Code128

    ref              = summary.get("booking_reference") or "UNKNOWN"
    airline          = summary.get("airline_name") or "Airline"
    currency         = summary.get("currency") or "USD"
    total            = summary.get("total_amount") or "—"
    passengers       = summary.get("passenger_names") or []
    pax_detail       = summary.get("passengers_detail") or []
    slices           = summary.get("slices") or []
    bdate            = summary.get("booking_date") or ""
    contact_email    = summary.get("contact_email") or ""
    baggage_allowance = summary.get("baggage_allowance") or []
    conditions       = summary.get("conditions") or {}
    change_summ      = conditions.get("changes") or {}
    refund_summ      = conditions.get("refunds") or {}
    change_allowed   = change_summ.get("status") == "allowed"
    refund_allowed   = refund_summ.get("status") == "allowed"
    change_label     = change_summ.get("label") or "Contact airline for change rules"
    refund_label     = refund_summ.get("label") or "Contact airline for refund rules"

    W, H = A4          # 595.28 × 841.89 pt
    M    = 40           # left / right margin

    # ── Palette ──────────────────────────────────────────────────────────────
    C_IND    = colors.HexColor("#4338ca")
    C_IND_M  = colors.HexColor("#6366f1")
    C_IND_BG = colors.HexColor("#eef2ff")
    C_IND_BD = colors.HexColor("#c7d2fe")
    C_GRN    = colors.HexColor("#22c55e")
    C_GRN_D  = colors.HexColor("#166534")
    C_GRN_BG = colors.HexColor("#dcfce7")
    C_GRN_BD = colors.HexColor("#86efac")
    C_RED    = colors.HexColor("#ef4444")
    C_TXT    = colors.HexColor("#0f172a")
    C_DARK   = colors.HexColor("#1e293b")
    C_MUT    = colors.HexColor("#64748b")
    C_SOF    = colors.HexColor("#94a3b8")
    C_SEP    = colors.HexColor("#e2e8f0")
    C_WHI    = colors.white

    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"Skairova E-Ticket — {ref}")
    c.setAuthor("Skairova")

    today_str  = _dt.now().strftime("%B %-d, %Y")
    pax_count  = len(passengers)
    CW         = W - 2 * M    # content width ≈ 515 pt

    def sw(text, font, size):
        return pdfmetrics.stringWidth(text, font, size)

    def fit_text(text, font, size, max_width):
        text = str(text or "")
        if sw(text, font, size) <= max_width:
            return text
        ell = "..."
        ell_w = sw(ell, font, size)
        out = text
        while out and sw(out, font, size) + ell_w > max_width:
            out = out[:-1]
        return (out.rstrip() + ell) if out else ell

    # ── Drawing helpers ───────────────────────────────────────────────────────
    def hline(x1, yy, x2, col=None, lw=0.5):
        c.setStrokeColor(col or C_SEP); c.setLineWidth(lw)
        c.line(x1, yy, x2, yy)

    def vline(xx, y1, y2, col=None, lw=0.4):
        c.setStrokeColor(col or C_SEP); c.setLineWidth(lw)
        c.line(xx, y1, xx, y2)

    def card(x, yb, w, h, r=5):
        c.setFillColor(C_WHI)
        c.roundRect(x, yb, w, h, r, fill=1, stroke=0)
        c.setStrokeColor(C_SEP); c.setLineWidth(0.6)
        c.roundRect(x, yb, w, h, r, fill=0, stroke=1)

    def badge(x, yb, text, bg, fg, border=None, r=3, fs=6.5):
        tw = sw(text, "Helvetica-Bold", fs)
        bw = tw + 10; bh = 14
        c.setFillColor(bg)
        c.roundRect(x, yb, bw, bh, r, fill=1, stroke=0)
        if border:
            c.setStrokeColor(border); c.setLineWidth(0.5)
            c.roundRect(x, yb, bw, bh, r, fill=0, stroke=1)
        c.setFillColor(fg); c.setFont("Helvetica-Bold", fs)
        c.drawCentredString(x + bw / 2, yb + 4, text)
        return bw

    def check_circle(cx, cy, r, col):
        c.setFillColor(col); c.circle(cx, cy, r, fill=1, stroke=0)
        c.setStrokeColor(C_WHI); c.setLineWidth(1.6); c.setLineCap(1)
        p2 = c.beginPath()
        p2.moveTo(cx - r*.38, cy + r*.02)
        p2.lineTo(cx - r*.06, cy - r*.32)
        p2.lineTo(cx + r*.44, cy + r*.38)
        c.drawPath(p2, fill=0, stroke=1)
        c.setLineCap(0); c.setLineWidth(1)

    def cross_circle(cx, cy, r, col):
        c.setFillColor(col); c.circle(cx, cy, r, fill=1, stroke=0)
        c.setStrokeColor(C_WHI); c.setLineWidth(1.4)
        c.line(cx - r*.45, cy + r*.45, cx + r*.45, cy - r*.45)
        c.line(cx - r*.45, cy - r*.45, cx + r*.45, cy + r*.45)

    def logo(lx, ly, sz=11):
        r = sz * 0.52; cx = lx + r; cy = ly + r
        c.setFillColor(C_IND_M); c.circle(cx, cy, r, fill=1, stroke=0)
        c.setFillColor(C_WHI); c.setFont("Helvetica-Bold", sz * 0.72)
        c.drawCentredString(cx, cy - sz * 0.25, "S")
        c.setFillColor(C_TXT); c.setFont("Helvetica-Bold", sz * 1.1)
        c.drawString(lx + r * 2 + 4, cy - sz * 0.36, "Skairova")

    def icon_sq(x, yb, sz=11):
        c.setFillColor(C_IND_BG)
        c.roundRect(x, yb, sz, sz, 2, fill=1, stroke=0)
        return x + sz / 2, yb + sz / 2

    def draw_svg_icon(name, x, yb, size, color="#6366f1"):
        """Render project SVG icons in the PDF, with a native fallback.

        Some uploaded SVGs have hard-coded fills or oversized viewboxes, so keep
        the itinerary on the curated lucide-style SVGs and normalize color first.
        """
        try:
            import io as _io
            import os as _os
            import re as _re
            from svglib.svglib import svg2rlg
            from reportlab.graphics import renderPDF

            svg_name = {
                "plane-fill": "plane",
            }.get(name, name)
            svg_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "static", "icons", f"{svg_name}.svg")
            with open(svg_path, "r", encoding="utf-8") as _f:
                raw = _f.read()

            raw = _re.sub(r'fill="(?!none")[^"]*"', 'fill="currentColor"', raw)
            raw = _re.sub(r"fill='(?!none')[^']*'", "fill='currentColor'", raw)
            raw = _re.sub(r'stroke="[^"]*"', 'stroke="currentColor"', raw)
            raw = _re.sub(r"stroke='[^']*'", "stroke='currentColor'", raw)
            raw = raw.replace("currentColor", color)

            drw = svg2rlg(_io.BytesIO(raw.encode("utf-8")))
            if drw is not None and drw.width and drw.height:
                c.saveState()
                c.translate(x, yb)
                c.scale(size / drw.width, size / drw.height)
                renderPDF.draw(drw, c, 0, 0)
                c.restoreState()
                return
        except Exception:
            pass

        col = colors.HexColor(color) if isinstance(color, str) else color

        def line(x1, y1, x2, y2):
            c.line(x1, y1, x2, y2)

        def path(points, close=False, fill=0):
            p = c.beginPath()
            p.moveTo(points[0][0], points[0][1])
            for px2, py2 in points[1:]:
                p.lineTo(px2, py2)
            if close:
                p.close()
            c.drawPath(p, fill=fill, stroke=0 if fill else 1)

        c.saveState()
        c.translate(x, yb)
        c.scale(size / 24.0, size / 24.0)
        c.setStrokeColor(col)
        c.setFillColor(col)
        c.setLineWidth(2)
        c.setLineCap(1)
        c.setLineJoin(1)

        if name in {"plane-fill", "plane"}:
            c.setLineWidth(2.4 if name == "plane-fill" else 2)
            line(4, 12, 20, 12)
            line(12, 12, 8, 20)
            line(12, 12, 8, 4)
            line(7, 12, 4, 16)
            line(7, 12, 4, 8)
        elif name == "plane-takeoff":
            c.setLineWidth(2)
            line(4, 10, 20, 16)
            line(11, 12.5, 7, 20)
            line(11, 12.5, 7, 5)
            line(7, 11, 4, 15)
            line(7, 11, 4, 8)
            line(3, 4, 21, 4)
        elif name == "luggage":
            c.roundRect(7, 5, 10, 14, 2, fill=0, stroke=1)
            path([(10, 19), (10, 21), (14, 21), (14, 19)])
            line(10, 9, 10, 15)
            line(14, 9, 14, 15)
            c.circle(9, 4, .7, fill=1, stroke=0)
            c.circle(15, 4, .7, fill=1, stroke=0)
        elif name == "armchair":
            c.roundRect(6, 10, 12, 7, 2, fill=0, stroke=1)
            path([(7, 10), (5, 6), (19, 6), (17, 10)])
            line(8, 6, 8, 3)
            line(16, 6, 16, 3)
        elif name == "clock-3":
            c.circle(12, 12, 8, fill=0, stroke=1)
            line(12, 12, 12, 16)
            line(12, 12, 16, 12)
        elif name == "tag":
            path([(4, 12), (12, 4), (21, 13), (13, 21), (4, 12)], close=True)
            c.circle(9, 12, 1.2, fill=1, stroke=0)
        elif name == "user-round":
            c.circle(12, 16, 4, fill=0, stroke=1)
            path([(5, 4), (6.5, 8), (9, 10), (15, 10), (17.5, 8), (19, 4)])
        elif name == "credit-card":
            c.roundRect(3, 6, 18, 12, 2, fill=0, stroke=1)
            line(3, 14, 21, 14)
            line(7, 9, 11, 9)
        elif name == "list":
            for yy in (17, 12, 7):
                c.circle(6, yy, 1, fill=1, stroke=0)
                line(10, yy, 20, yy)
        elif name == "file-text":
            path([(6, 3), (18, 3), (18, 15), (13, 21), (6, 21), (6, 3)])
            path([(13, 21), (13, 15), (18, 15)])
            line(9, 11, 15, 11)
            line(9, 8, 15, 8)
        elif name == "globe":
            c.circle(12, 12, 8, fill=0, stroke=1)
            line(4, 12, 20, 12)
            c.ellipse(8, 4, 16, 20, fill=0, stroke=1)
        elif name == "shield-check":
            path([(12, 21), (19, 18), (19, 11), (17, 7), (12, 3), (7, 7), (5, 11), (5, 18), (12, 21)])
            path([(8.5, 12), (11, 9.5), (16, 14.5)])
        elif name == "bell":
            path([(6, 9), (7.5, 11), (7.5, 15), (9, 18), (12, 19), (15, 18), (16.5, 15), (16.5, 11), (18, 9)])
            line(5, 9, 19, 9)
            path([(10, 6), (12, 5), (14, 6)])
        elif name == "message-square":
            c.roundRect(4, 6, 16, 12, 2, fill=0, stroke=1)
            path([(8, 6), (5, 3), (5.5, 8)])
        else:
            c.circle(12, 12, 4, fill=1, stroke=0)

        c.restoreState()

    def icon_cell(x, yb, sz, icon_name, bg=None, color=None):
        """Indigo square background + SVG icon centred inside."""
        c.setFillColor(bg or C_IND_BG)
        c.roundRect(x, yb, sz, sz, 2, fill=1, stroke=0)
        pad = max(1.5, sz * 0.12)
        draw_svg_icon(icon_name, x + pad, yb + pad, sz - 2 * pad, color or "#6366f1")

    def section_title(x, y_top, title, icon_name=None, *, info=False):
        icon_size = 13
        icon_y = y_top - icon_size
        if info:
            c.setFillColor(C_IND_BG)
            c.circle(x + icon_size / 2, icon_y + icon_size / 2, icon_size / 2, fill=1, stroke=0)
            c.setFillColor(C_IND_M)
            c.setFont("Helvetica-Bold", 7.8)
            c.drawCentredString(x + icon_size / 2, icon_y + 3.2, "i")
        elif icon_name:
            icon_cell(x, icon_y, icon_size, icon_name)

        text_x = x + icon_size + 7
        c.setFont("Helvetica-Bold", 9.5)
        c.setFillColor(C_TXT)
        c.drawString(text_x, y_top - 9.2, title)

    def draw_qr(x, yb, sz, data):
        try:
            from reportlab.graphics.barcode.qr import QrCodeWidget
            from reportlab.graphics.shapes import Drawing
            from reportlab.graphics import renderPDF
            qr = QrCodeWidget(data)
            b = qr.getBounds()
            qw, qh = b[2] - b[0], b[3] - b[1]
            d = Drawing(sz, sz, transform=[sz / qw, 0, 0, sz / qh, 0, 0])
            d.add(qr)
            renderPDF.draw(d, c, x, yb)
        except Exception:
            try:
                bc = Code128(data, barHeight=sz * .55, barWidth=0.65, humanReadable=False)
                bc.drawOn(c, x, yb + sz * .22)
            except Exception:
                pass

    def continuation_header():
        c.showPage()
        c.setFillColor(C_IND); c.rect(0, H - 22, W, 22, fill=1, stroke=0)
        c.setFillColor(C_WHI); c.setFont("Helvetica-Bold", 8)
        c.drawString(M, H - 14, "SKAIROVA  —  E-Ticket & Flight Itinerary (continued)")
        return H - 34

    # ════════════════════════════════════════════════════════════════════════
    # HEADER
    # ════════════════════════════════════════════════════════════════════════
    y = H - 20

    logo(M, y - 13, sz=11)

    c.setFont("Helvetica-Bold", 9); c.setFillColor(C_TXT)
    c.drawRightString(W - M, y - 5, "E-TICKET & ITINERARY")
    c.setFont("Helvetica", 7.5); c.setFillColor(C_MUT)
    c.drawRightString(W - M, y - 16, f"Generated on {today_str}")

    y -= 30
    hline(M, y, W - M, lw=0.7)
    y -= 14

    # ════════════════════════════════════════════════════════════════════════
    # CONFIRMATION BANNER  (3 columns, no card bg)
    # ════════════════════════════════════════════════════════════════════════
    BANNER_H = 64
    C1W = 168; C2W = 178; C3W = CW - C1W - C2W
    c1x = M; c2x = M + C1W; c3x = M + C1W + C2W

    ck_r = 7; ck_cx = c1x + ck_r; ck_cy = y - ck_r - 2
    check_circle(ck_cx, ck_cy, ck_r, C_GRN)
    c.setFillColor(C_GRN); c.setFont("Helvetica-Bold", 10)
    c.drawString(c1x + ck_r * 2 + 5, y - 10, "CONFIRMED")
    c.setFont("Helvetica", 7.5); c.setFillColor(C_MUT)
    c.drawString(c1x, y - 25, "Ticket status")
    c.setFont("Helvetica-Bold", 7.5); c.setFillColor(C_TXT)
    c.drawString(c1x, y - 38, "Booking confirmed")

    vline(c2x - 6, y, y - BANNER_H)
    vline(c3x - 6, y, y - BANNER_H)

    c.setFont("Helvetica", 6.5); c.setFillColor(C_MUT)
    c.drawString(c2x, y - 7, "BOOKING REFERENCE")
    c.setFont("Helvetica-Bold", 22); c.setFillColor(C_TXT)
    c.drawString(c2x, y - 29, ref)
    badge(c2x, y - 46, "CONFIRMED", C_GRN_BG, C_GRN_D, border=C_GRN_BD)

    c.setFont("Helvetica", 6.5); c.setFillColor(C_MUT)
    c.drawString(c3x, y - 7, "PASSENGERS")
    c.setFont("Helvetica-Bold", 10); c.setFillColor(C_TXT)
    c.drawString(c3x, y - 24, f"{pax_count} Passenger{'s' if pax_count != 1 else ''}")
    if passengers:
        c.setFont("Helvetica", 7.2); c.setFillColor(C_MUT)
        c.drawString(c3x, y - 39, fit_text(", ".join(passengers), "Helvetica", 7.2, C3W - 4))

    y -= BANNER_H + 10
    hline(M, y, W - M, lw=0.7)
    y -= 16

    # ════════════════════════════════════════════════════════════════════════
    # FLIGHT DETAILS (one card per slice)
    # ════════════════════════════════════════════════════════════════════════
    for si, sl in enumerate(slices):
        segs = sl.get("segments") or []
        if not segs:
            continue
        fs = segs[0]; ls = segs[-1]

        CARD_H = 214
        if y - CARD_H - 22 < 60:
            y = continuation_header()

        section_title(M, y, "FLIGHT DETAILS", "plane-takeoff")

        stops_lbl = sl.get("stops_label") or "Nonstop"

        y -= 20
        card(M, y - CARD_H, CW, CARD_H)

        # Sub-header row
        lbl_text  = (sl.get("label") or f"Leg {si+1}").upper()
        air_lbl   = sl.get("airline_label") or airline
        flight_no = fs.get("flight_number") or ""
        operating = fs.get("operating_carrier") or air_lbl
        air_line  = "  ·  ".join(filter(None, [air_lbl, flight_no, f"Operated by {operating}"]))
        bw2 = badge(M + 6, y - 16, lbl_text, C_IND_M, C_WHI, r=3, fs=7)
        c.setFont("Helvetica", 7.5); c.setFillColor(C_MUT)
        c.drawString(M + 6 + bw2 + 8, y - 12, air_line)
        hline(M + 1, y - 22, W - M - 1, lw=0.4)

        TL = y - 32
        LX = M + 8; RX = W - M - 8; MX = W / 2

        dep_date = fs.get("depart_date_full") or fs.get("depart_label") or ""
        arr_date = ls.get("arrive_date_full") or ls.get("arrive_label") or ""
        dep_time = fs.get("depart_time") or "—"
        arr_time = ls.get("arrive_time") or "—"
        dep_code = fs.get("origin_code") or "—"
        arr_code = ls.get("destination_code") or "—"
        dep_city = (fs.get("origin_city") or "")[:28]
        arr_city = (ls.get("destination_city") or "")[:28]
        dep_apt  = (fs.get("origin_airport_name") or dep_city)[:34]
        arr_apt  = (ls.get("destination_airport_name") or arr_city)[:34]
        dep_term = fs.get("departure_terminal") or ""
        arr_term = ls.get("arrival_terminal") or ""
        dep_gate = fs.get("departure_gate") or fs.get("origin_gate") or ""
        arr_gate = ls.get("arrival_gate") or ls.get("destination_gate") or ""
        duration = sl.get("duration_label") or ""
        aircraft = fs.get("aircraft") or ""
        cabin    = (pax_detail[0].get("cabin") or "Economy") if pax_detail else "Economy"

        # +N day detection
        plus_day = ""
        try:
            d1 = _dt.fromisoformat(fs.get("depart_iso", "").replace("Z", "+00:00"))
            d2 = _dt.fromisoformat(ls.get("arrive_iso", "").replace("Z", "+00:00"))
            nd = (d2.date() - d1.date()).days
            if nd > 0:
                plus_day = f"+{nd} day{'s' if nd > 1 else ''}"
        except Exception:
            pass

        # Row 1 — date labels + duration
        c.setFont("Helvetica-Bold", 7.5); c.setFillColor(C_MUT)
        c.drawString(LX, TL, dep_date)
        c.drawRightString(RX, TL, arr_date)
        if plus_day:
            c.setFont("Helvetica-Bold", 7)
            c.setFillColor(colors.HexColor("#f97316"))
            c.drawRightString(RX, TL - 10, plus_day)
        c.setFont("Helvetica", 7); c.setFillColor(C_MUT)
        c.drawCentredString(MX, TL, duration)

        # Row 2 — large times + dashed path + plane
        T2 = TL - (34 if plus_day else 26)
        c.setFont("Helvetica-Bold", 22); c.setFillColor(C_TXT)
        c.drawString(LX, T2, dep_time)
        arr_tw = sw(arr_time, "Helvetica-Bold", 22)
        c.drawRightString(RX, T2, arr_time)

        LINE_Y = T2 + 8
        dep_tw = sw(dep_time, "Helvetica-Bold", 22)
        ll = LX + dep_tw + 8; lr = RX - arr_tw - 8
        c.setStrokeColor(C_SOF); c.setLineWidth(1); c.setDash(2, 4)
        c.line(ll, LINE_Y, MX - 12, LINE_Y)
        c.line(MX + 12, LINE_Y, lr, LINE_Y)
        c.setDash()
        c.setFillColor(C_SOF)
        c.circle(ll, LINE_Y, 2.5, fill=1, stroke=0)
        c.circle(lr, LINE_Y, 2.5, fill=1, stroke=0)
        c.setFillColor(C_IND_M); c.circle(MX, LINE_Y, 8, fill=1, stroke=0)
        _ico_sz = 13
        draw_svg_icon("plane-fill", MX - _ico_sz / 2, LINE_Y - _ico_sz / 2, _ico_sz, color="#ffffff")

        # Row 3 — IATA codes
        T3 = T2 - 24
        c.setFont("Helvetica-Bold", 18); c.setFillColor(C_TXT)
        c.drawString(LX, T3, dep_code)
        c.drawRightString(RX, T3, arr_code)

        # Row 4 — cities + stops label
        T4 = T3 - 18
        c.setFont("Helvetica", 7.5); c.setFillColor(C_MUT)
        c.drawString(LX, T4, dep_city)
        c.drawRightString(RX, T4, arr_city)
        c.setFont("Helvetica", 7)
        c.drawCentredString(MX, T4, stops_lbl)

        # Row 5 — airport names + aircraft
        T5 = T4 - 13
        c.setFont("Helvetica", 7); c.setFillColor(C_SOF)
        c.drawString(LX, T5, dep_apt)
        c.drawRightString(RX, T5, arr_apt)
        if aircraft:
            c.drawCentredString(MX, T5, aircraft)

        # Row 6 — terminals + cabin
        T6 = T5 - 12
        if dep_term:
            c.drawString(LX, T6, dep_term)
        if arr_term:
            c.drawRightString(RX, T6, arr_term)
        c.drawCentredString(MX, T6, cabin)

        # Info grid separator
        SEP_Y = y - CARD_H + 74
        hline(M + 1, SEP_Y, W - M - 1, lw=0.4)

        # 4-column info grid — fixed y positions so all columns align
        _bag_lines = baggage_allowance[:2] if baggage_allowance else ["See airline for details"]
        dep_terminal_line = " / ".join(filter(None, [dep_term, f"Gate {dep_gate}" if dep_gate else ""]))
        arr_terminal_line = " / ".join(filter(None, [arr_term, f"Gate {arr_gate}" if arr_gate else ""]))
        terminal_lines = [
            f"Depart: {dep_terminal_line or 'TBD'}",
            f"Arrive: {arr_terminal_line or 'TBD'}",
        ]
        icw = CW / 4
        info_grid = [
            ("BAGGAGE",       _bag_lines,                         "luggage"),
            ("TERMINAL/GATE", terminal_lines,                    "bell"),
            ("CABIN",         [cabin],                            "armchair"),
            ("STATUS",        ["Confirmed"],                      "tag"),
        ]
        _ICON_SZ  = 13
        _ICON_BOT = SEP_Y - 8 - _ICON_SZ   # icon square bottom  (top = SEP_Y - 8)
        _LBL_Y    = _ICON_BOT - 9           # label baseline (fixed for all cols)
        _VAL_Y    = _LBL_Y - 14            # values start (fixed — no variable label lines)
        for ci, (lbl2, vals, icon_name) in enumerate(info_grid):
            ix = M + ci * icw + 6
            icon_cell(ix, _ICON_BOT, _ICON_SZ, icon_name)
            c.setFont("Helvetica-Bold", 6.5); c.setFillColor(C_MUT)
            c.drawString(ix, _LBL_Y, lbl2)
            c.setFont("Helvetica", 7); c.setFillColor(C_TXT)
            _vy = _VAL_Y
            for v in vals:
                c.drawString(ix, _vy, v); _vy -= 11
            if ci < 3:
                vline(M + (ci + 1) * icw, SEP_Y - 1, y - CARD_H + 1, lw=0.4)

        y -= CARD_H + 12

    # ════════════════════════════════════════════════════════════════════════
    # PASSENGER DETAILS
    # ════════════════════════════════════════════════════════════════════════
    ROW_H = 32
    PAX_H = 24 + len(passengers) * ROW_H
    if y - PAX_H - 20 < 60:
        y = continuation_header()

    section_title(M, y, "PASSENGER DETAILS", "user-round")
    y -= 20

    card(M, y - PAX_H, CW, PAX_H)
    PAX_COLS = [
        ("Passenger", 0,   128),
        ("Type",      142, 38),
        ("Seat",      184, 30),
        ("Class",     222, 58),
        ("Baggage",   292, 118),
        ("Ticket No.", 420, 50),
        ("FF#",       478, 28),
    ]
    c.setFont("Helvetica", 6.5); c.setFillColor(C_MUT)
    for hdr, ox, _w in PAX_COLS:
        c.drawString(M + 8 + ox, y - 11, hdr)
    hline(M + 1, y - 15, W - M - 1, lw=0.4)

    for pi, name in enumerate(passengers):
        ry = y - 15 - (pi + 1) * ROW_H
        pd = pax_detail[pi] if pi < len(pax_detail) else {}
        row_vals = [name, pd.get("type") or "Adult", pd.get("seat") or "—",
                    pd.get("cabin") or "Economy", pd.get("baggage") or "—",
                    pd.get("ticket_number") or "—", pd.get("frequent_flyer") or "—"]
        for vi, (val, (_, ox, col_w)) in enumerate(zip(row_vals, PAX_COLS)):
            font = "Helvetica-Bold" if vi == 0 else "Helvetica"
            size = 8 if vi == 0 else 7.2
            c.setFont(font, size)
            c.setFillColor(C_TXT if vi == 0 else C_DARK)
            c.drawString(M + 8 + ox, ry + 12, fit_text(val, font, size, col_w))

    y -= PAX_H + 10

    # ════════════════════════════════════════════════════════════════════════
    # PAYMENT SUMMARY
    # ════════════════════════════════════════════════════════════════════════
    PAY_H = 94
    if y - PAY_H - 5 < 60:
        y = continuation_header()

    px = M
    card(px, y - PAY_H, CW, PAY_H)

    section_title(px + 6, y - 4, "PAYMENT SUMMARY", "credit-card")
    hline(px + 1, y - 22, px + CW - 1, lw=0.4)

    pnr = f"{ref} ({(airline or 'XX')[:2].upper()})"
    pay_rows = [
        ("Total Paid", f"{currency} {total}", True),
        ("Booking Reference", ref, False),
        ("Airline Reference (PNR)", pnr, False),
        ("Ticket Status", "Confirmed", False),
    ]
    col_w = (CW - 32) / 2
    for idx, (lbl3, val3, bold) in enumerate(pay_rows):
        col = idx % 2
        row = idx // 2
        x0 = px + 8 + col * (col_w + 16)
        ry2 = y - 38 - row * 26
        c.setFont("Helvetica", 7); c.setFillColor(C_MUT)
        c.drawString(x0, ry2, lbl3)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9.5 if bold else 7.5)
        c.setFillColor(C_TXT)
        c.drawString(x0, ry2 - 12, fit_text(val3, "Helvetica-Bold" if bold else "Helvetica", 9.5 if bold else 7.5, col_w))

    y -= PAY_H + 10

    # ════════════════════════════════════════════════════════════════════════
    # IMPORTANT INFORMATION
    # ════════════════════════════════════════════════════════════════════════
    INFO_H = 82
    if y - INFO_H - 20 < 60:
        y = continuation_header()

    section_title(M, y, "IMPORTANT INFORMATION", info=True)
    y -= 18

    card(M, y - INFO_H, CW, INFO_H)
    info_items = [
        ("Travel Documents", "Name must match your\ntravel document. Carry\nvalid ID/passport.",       "file-text"),
        ("Entry Rules",      "Check visa and entry\nrequirements before\ndeparture.",                 "globe"),
        ("Schedule Changes", "Airline schedules may\nchange. Reconfirm before\nyou travel.",            "shield-check"),
        ("Airport Time",     "Arrive early and allow\ntime for baggage and\nsecurity.",               "bell"),
    ]
    icw2 = CW / 4
    _INFO_ICON_SZ  = 13
    _INFO_ICON_BOT = y - 8 - _INFO_ICON_SZ   # icon bottom (top = y - 8)
    _INFO_TITLE_Y  = _INFO_ICON_BOT - 9       # title baseline (fixed)
    _INFO_DESC_Y   = _INFO_TITLE_Y - 14       # description start (fixed)
    for ii, (title, desc, icon_name) in enumerate(info_items):
        ix2 = M + ii * icw2 + 6
        icon_cell(ix2, _INFO_ICON_BOT, _INFO_ICON_SZ, icon_name)
        c.setFont("Helvetica-Bold", 7); c.setFillColor(C_TXT)
        c.drawString(ix2, _INFO_TITLE_Y, title)
        c.setFont("Helvetica", 6.5); c.setFillColor(C_MUT)
        _dy = _INFO_DESC_Y
        for ln in desc.split("\n"):
            c.drawString(ix2, _dy, ln); _dy -= 9
        if ii < 3:
            vline(M + (ii + 1) * icw2, y - 2, y - INFO_H + 2, lw=0.4)

    y -= INFO_H + 8

    # ════════════════════════════════════════════════════════════════════════
    # NEED HELP? (Live Chat + Email only — no Phone)
    # ════════════════════════════════════════════════════════════════════════
    if y - 55 < 30:
        y = continuation_header()

    hline(M, y, W - M, lw=0.7)
    y -= 12

    section_title(M, y, "SUPPORT CONTACT", "message-square")
    c.setFont("Helvetica", 7); c.setFillColor(C_MUT)
    c.drawString(M + 20, y - 22, "Our support team is available 24/7")

    for hci, (htitle, hval) in enumerate([("Live Chat", "skairova.com/support"),
                                           ("Email", "support@skairova.com")]):
        hx2 = M + 168 + hci * 148
        c.setFont("Helvetica-Bold", 8); c.setFillColor(C_TXT)
        c.drawString(hx2, y - 9, htitle)
        c.setFont("Helvetica", 7); c.setFillColor(C_MUT)
        c.drawString(hx2, y - 20, hval)

    y -= 48

    # ════════════════════════════════════════════════════════════════════════
    # FOOTER
    # ════════════════════════════════════════════════════════════════════════
    hline(M, y, W - M, lw=0.7)
    y -= 13

    logo(M, y - 14, sz=10)

    c.setFont("Helvetica", 7.5); c.setFillColor(C_MUT)
    c.drawCentredString(W / 2, y - 8,  "Thank you for choosing Skairova.")
    c.drawCentredString(W / 2, y - 19, "We wish you a safe and pleasant journey!")

    c.setFont("Helvetica", 7.5); c.setFillColor(C_MUT)
    c.drawRightString(W - M, y - 12, "skairova.com")

    c.save()
    return buf.getvalue()


def _render_itinerary_html(summary: dict) -> str:
    ref = summary.get("booking_reference") or "—"
    airline = summary.get("airline_name") or "—"
    currency = summary.get("currency") or "USD"
    total = summary.get("total_amount") or "—"
    passengers = summary.get("passenger_names") or []
    slices = summary.get("slices") or []

    seg_rows = []
    for sl in slices:
        for seg in (sl.get("segments") or []):
            seg_rows.append(
                f"<tr><td>{seg.get('flight_number','')}</td>"
                f"<td>{seg.get('origin_code','')} {seg.get('origin_city','')}</td>"
                f"<td>{seg.get('depart_label','')}</td>"
                f"<td>{seg.get('destination_code','')} {seg.get('destination_city','')}</td>"
                f"<td>{seg.get('arrive_label','')}</td>"
                f"<td>{seg.get('duration_label','')}</td></tr>"
            )

    pax_rows = "".join(f"<li>{p}</li>" for p in passengers)
    seg_html = "".join(seg_rows)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Itinerary — {ref}</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#0f172a;background:#fff;padding:32px}}
  h1{{font-size:1.5rem;font-weight:800;letter-spacing:-.02em;margin-bottom:4px}}
  .sub{{color:#64748b;font-size:.9rem;margin-bottom:28px}}
  .section{{margin-bottom:24px}}
  h2{{font-size:.7rem;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:#94a3b8;margin-bottom:10px}}
  table{{width:100%;border-collapse:collapse;font-size:.88rem}}
  th{{text-align:left;padding:8px 10px;background:#f8fafc;border-bottom:2px solid #e2e8f0;font-weight:700;font-size:.75rem;letter-spacing:.08em;text-transform:uppercase;color:#64748b}}
  td{{padding:10px 10px;border-bottom:1px solid #f1f5f9;vertical-align:top}}
  ul{{list-style:none;padding:0}}
  li{{padding:6px 0;border-bottom:1px solid #f1f5f9;font-size:.92rem}}
  li:last-child{{border:0}}
  .kv{{display:flex;gap:24px;flex-wrap:wrap}}
  .kv-item{{padding:12px 18px;border-radius:10px;background:#f8fafc;border:1px solid #e2e8f0}}
  .kv-label{{font-size:.7rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#94a3b8}}
  .kv-val{{font-size:1.1rem;font-weight:700;color:#0f172a;margin-top:3px}}
  @media print{{body{{padding:16px}}}}
</style>
</head>
<body>
  <h1>Flight Itinerary</h1>
  <p class="sub">{airline} &nbsp;·&nbsp; Booking ref: <strong>{ref}</strong></p>
  <div class="section">
    <h2>Booking summary</h2>
    <div class="kv">
      <div class="kv-item"><div class="kv-label">Booking reference</div><div class="kv-val">{ref}</div></div>
      <div class="kv-item"><div class="kv-label">Total paid</div><div class="kv-val">{currency} {total}</div></div>
      <div class="kv-item"><div class="kv-label">Passengers</div><div class="kv-val">{len(passengers)}</div></div>
    </div>
  </div>
  <div class="section">
    <h2>Flights</h2>
    <table><thead><tr><th>Flight</th><th>From</th><th>Departs</th><th>To</th><th>Arrives</th><th>Duration</th></tr></thead>
    <tbody>{seg_html}</tbody></table>
  </div>
  <div class="section">
    <h2>Passengers</h2>
    <ul>{pax_rows}</ul>
  </div>
</body>
</html>"""


def _ics_dt(iso: str | None) -> str:
    if not iso:
        return ""
    from datetime import datetime as _dt, timezone as _tz
    try:
        dt = _dt.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        utc = dt.astimezone(_tz.utc)
        return utc.strftime("%Y%m%dT%H%M%SZ")
    except Exception:
        return ""


def _render_ics(summary: dict) -> str:
    import uuid as _uuid
    ref = summary.get("booking_reference") or "UNKNOWN"
    airline = summary.get("airline_name") or "Airline"
    passengers = summary.get("passenger_names") or []
    pax_str = ", ".join(passengers) if passengers else "Traveler"
    slices = summary.get("slices") or []

    events = []
    for sl in slices:
        for seg in (sl.get("segments") or []):
            dtstart = _ics_dt(seg.get("depart_iso"))
            dtend = _ics_dt(seg.get("arrive_iso"))
            if not dtstart or not dtend:
                continue
            fn = seg.get("flight_number") or airline
            orig = seg.get("origin_code") or ""
            dest = seg.get("destination_code") or ""
            orig_city = seg.get("origin_city") or orig
            dest_city = seg.get("destination_city") or dest
            summary_line = f"{fn}: {orig_city} → {dest_city}"
            desc = (
                f"Booking reference: {ref}\\n"
                f"Flight: {fn}\\n"
                f"From: {orig_city} ({orig})\\n"
                f"To: {dest_city} ({dest})\\n"
                f"Passengers: {pax_str}"
            )
            event_uid = str(_uuid.uuid4())
            events.append(
                f"BEGIN:VEVENT\r\n"
                f"UID:{event_uid}\r\n"
                f"DTSTART:{dtstart}\r\n"
                f"DTEND:{dtend}\r\n"
                f"SUMMARY:{summary_line}\r\n"
                f"DESCRIPTION:{desc}\r\n"
                f"LOCATION:{orig_city} ({orig})\r\n"
                f"END:VEVENT\r\n"
            )

    events_str = "".join(events)
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Skairova//Flight Booking//EN\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:PUBLISH\r\n"
        f"{events_str}"
        "END:VCALENDAR\r\n"
    )


@app.route("/manage-booking", methods=["GET", "POST"])
def manage_booking():
    mode_error = _booking_lookup_error()
    account_notice, account_error = _pop_manage_account_notice()
    valid_auth_modes = {"signup", "login", "reset_request", "reset_verify", "reset_complete"}
    requested_auth_mode = str(request.args.get("auth_mode") or "").strip().lower()
    auth_mode = requested_auth_mode if requested_auth_mode in valid_auth_modes else ""
    reset_email = _normalize_email(str(request.args.get("reset_email") or ""))
    pending_reset_email = _normalize_email(str(session.get("ngf_reset_email_pending") or ""))
    token_email = _normalize_email(str(session.get("ngf_reset_token_email") or ""))
    if not reset_email:
        reset_email = pending_reset_email or token_email

    reset_token = str(session.get("ngf_reset_token") or "").strip()
    reset_token_expires_at = int(session.get("ngf_reset_token_expires_at") or 0)
    now_ts = int(time.time())
    if reset_token and reset_token_expires_at and now_ts > reset_token_expires_at:
        _clear_manage_reset_state()
        reset_token = ""
        token_email = ""
        if auth_mode == "reset_complete":
            auth_mode = "reset_request"
    if auth_mode == "reset_verify" and not reset_email:
        auth_mode = "reset_request"
    if auth_mode == "reset_complete":
        if reset_token and token_email:
            reset_email = token_email
        else:
            auth_mode = "reset_request"

    auth_context = {
        "auth_mode": auth_mode,
        "reset_email": reset_email,
        "reset_token": reset_token,
    }
    form_values = {
        "booking_reference": str(request.values.get("booking_reference") or "").strip().upper(),
        "last_name": str(request.values.get("last_name") or "").strip(),
        "dob": str(request.values.get("dob") or "").strip(),
    }
    if mode_error:
        return (
            _render_manage_booking_page(
            booking_error=mode_error,
            form_values=form_values,
            account_notice=account_notice,
            account_error=account_error,
            **auth_context,
            ),
            503,
        )

    if request.method == "GET":
        prefilled_ref = _normalize_booking_reference(form_values.get("booking_reference") or "")
        if not prefilled_ref:
            return _render_manage_booking_page(
            form_values=form_values,
            account_notice=account_notice,
            account_error=account_error,
            **auth_context,
            )
        if prefilled_ref.startswith("SKH"):
            if _hotel_booking_by_reference(prefilled_ref):
                return redirect(url_for("hotel_booking_confirmation", booking_reference=prefilled_ref))
            return _render_manage_booking_page(
                booking_error="We couldn't load that booking right now.",
                form_values=form_values,
                account_notice=account_notice,
                account_error=account_error,
                **auth_context,
            )
        order = _latest_order_for_reference(prefilled_ref)
        if order:
            _capture_booking_email_links(order=order, passengers_payload=None)
            order_id = str(order.get("id") or "").strip()
            if order_id:
                RECENT_ORDER_CACHE.set(order_id, order)
            if prefilled_ref:
                RECENT_REF_CACHE.set(prefilled_ref, order)
            session["ngf_manage_order_id"] = order_id
            session["ngf_manage_booking_reference"] = prefilled_ref
            return redirect(url_for("booking_detail"))
        return _render_manage_booking_page(
            booking_error="We couldn't load that booking right now.",
            form_values=form_values,
            account_notice=account_notice,
            account_error=account_error,
            **auth_context,
        )

    booking_reference = _normalize_booking_reference(form_values["booking_reference"])
    last_name = _normalize_last_name(form_values["last_name"])
    dob = form_values["dob"]
    if not booking_reference or len(booking_reference) < 5:
        return (
            _render_manage_booking_page(
            booking_error="Enter a valid booking reference.",
            form_values=form_values,
            account_notice=account_notice,
            account_error=account_error,
            **auth_context,
            ),
            400,
        )
    if not last_name:
        return (
            _render_manage_booking_page(
            booking_error="Enter the passenger's last name.",
            form_values=form_values,
            account_notice=account_notice,
            account_error=account_error,
            **auth_context,
            ),
            400,
        )

    # Hotel bookings (SKH-prefixed) live in the local table, not Duffel, and
    # don't collect a guest DOB at checkout — verify on reference + last name
    # instead (the reference itself is a high-entropy secret) rather than
    # asking this shared form for a field hotel checkout never captured.
    if booking_reference.startswith("SKH"):
        attempts = _record_manage_booking_attempt(request.remote_addr or "", booking_reference)
        if attempts > 8:
            return (
                _render_manage_booking_page(
                    booking_error="Too many attempts for this booking reference. Please wait 15 minutes and try again.",
                    form_values=form_values,
                    account_notice=account_notice,
                    account_error=account_error,
                    **auth_context,
                ),
                429,
            )
        hotel_booking = _hotel_booking_by_reference(booking_reference)
        if not hotel_booking or _normalize_person_name(hotel_booking.get("holder_last_name") or "").lower() != last_name.lower():
            return (
                _render_manage_booking_page(
                    booking_error="We couldn't find a hotel booking with that reference and last name.",
                    form_values=form_values,
                    account_notice=account_notice,
                    account_error=account_error,
                    **auth_context,
                ),
                404,
            )
        if hotel_booking.get("holder_email"):
            _record_booking_email_link(
                email=hotel_booking["holder_email"],
                booking_reference=booking_reference,
                order_id=hotel_booking.get("liteapi_booking_id", ""),
            )
        return redirect(url_for("hotel_booking_confirmation", booking_reference=booking_reference))

    if not _valid_dob(dob):
        return (
            _render_manage_booking_page(
            booking_error="Enter a valid date of birth in YYYY-MM-DD format.",
            form_values=form_values,
            account_notice=account_notice,
            account_error=account_error,
            **auth_context,
            ),
            400,
        )

    attempts = _record_manage_booking_attempt(request.remote_addr or "", booking_reference)
    if attempts > 8:
        return (
            _render_manage_booking_page(
            booking_error="Too many attempts for this booking reference. Please wait 15 minutes and try again.",
            form_values=form_values,
            account_notice=account_notice,
            account_error=account_error,
            **auth_context,
            ),
            429,
        )

    try:
        candidate_orders = DUFF.list_orders(
            booking_reference=booking_reference,
            passenger_last_name=last_name,
            limit=10,
        )
    except DuffelAPIError as exc:
        return (
            _render_manage_booking_page(
            booking_error=str(exc),
            form_values=form_values,
            account_notice=account_notice,
            account_error=account_error,
            **auth_context,
            ),
            _booking_status_code(exc.status_code),
        )

    matched_order = next(
        (
            order
            for order in candidate_orders
            if _order_matches_guest_lookup(
                order,
                booking_reference=booking_reference,
                last_name=last_name,
                dob=dob,
            )
        ),
        None,
    )
    if not matched_order:
        return (
            _render_manage_booking_page(
            booking_error="We couldn't verify that booking with those details. Double-check the reference, last name, and date of birth.",
            form_values=form_values,
            account_notice=account_notice,
            account_error=account_error,
            **auth_context,
            ),
            404,
        )

    _reset_manage_booking_attempts(request.remote_addr or "", booking_reference)
    _capture_booking_email_links(order=matched_order, passengers_payload=None)
    account_email = _session_account_email()
    if account_email:
        _link_booking_to_account(account_email, booking_reference)
    order_id = str(matched_order.get("id") or "").strip()
    if order_id:
        RECENT_ORDER_CACHE.set(order_id, matched_order)
    booking_ref_norm = _normalize_booking_reference(str(matched_order.get("booking_reference") or booking_reference))
    if booking_ref_norm:
        RECENT_REF_CACHE.set(booking_ref_norm, matched_order)
    session["ngf_manage_order_id"] = order_id
    session["ngf_manage_booking_reference"] = booking_reference
    return redirect(url_for("booking_detail"))


@app.route("/manage-booking/account/signup", methods=["POST"])
def manage_booking_account_signup():
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    email = _normalize_email(str(request.form.get("account_email") or ""))
    first_name = _normalize_person_name(str(request.form.get("first_name") or ""))
    last_name = _normalize_person_name(str(request.form.get("last_name") or ""))
    dob = str(request.form.get("dob") or "").strip()
    accepted_terms = str(request.form.get("accept_terms") or "").strip().lower() in {"on", "1", "true", "yes"}
    password = str(request.form.get("account_password") or "")
    password_confirm = str(request.form.get("account_password_confirm") or "")
    booking_reference = _normalize_booking_reference(str(request.form.get("booking_reference") or ""))
    next_url = _safe_next_url(request.form.get("next"))

    if not email or "@" not in email:
        _set_manage_account_notice(error="Enter a valid email for account signup.")
        return _auth_redirect(mode="signup", booking_reference=booking_reference, next_url=next_url)
    if not first_name:
        _set_manage_account_notice(error="Enter your first name.")
        return _auth_redirect(mode="signup", booking_reference=booking_reference, next_url=next_url)
    if not last_name:
        _set_manage_account_notice(error="Enter your last name.")
        return _auth_redirect(mode="signup", booking_reference=booking_reference, next_url=next_url)
    # DOB is optional for account creation in auth/signup flows.
    if dob and not _valid_dob(dob):
        _set_manage_account_notice(error="Enter a valid date of birth in YYYY-MM-DD format, or leave it blank.")
        return _auth_redirect(mode="signup", booking_reference=booking_reference, next_url=next_url)
    if not accepted_terms:
        _set_manage_account_notice(error="Accept the Terms and Conditions to create an account.")
        return _auth_redirect(mode="signup", booking_reference=booking_reference, next_url=next_url)
    if password != password_confirm:
        _set_manage_account_notice(error="Password confirmation does not match.")
        return _auth_redirect(mode="signup", booking_reference=booking_reference, next_url=next_url)
    if not _password_meets_criteria(password):
        _set_manage_account_notice(error="Use at least 8 characters including letters and numbers.")
        return _auth_redirect(mode="signup", booking_reference=booking_reference, next_url=next_url)
    if _account_lookup(email):
        _set_manage_account_notice(error="An account with that email already exists. Please log in instead.")
        return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)

    salt_hex = os.urandom(16).hex()
    account = {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "dob": dob,
        "terms_accepted_at": datetime.utcnow().isoformat(timespec="seconds"),
        "salt_hex": salt_hex,
        "password_hash": _password_hash(password, salt_hex),
        "created_at": datetime.utcnow().isoformat(),
        "session_nonce": "",
        "last_login_at": "",
        "last_login_ip": "",
        "price_alerts_enabled": True,
        "route_tracking_enabled": True,
        "saved_searches": [],
        "linked_booking_references": [booking_reference] if booking_reference else [],
    }
    account = _update_account_login_metadata(account)
    _account_save(email, account)
    auto_linked_count = _sync_account_bookings_by_email(email)
    if auto_linked_count == 0 and _discover_recent_booking_links_for_email(email) > 0:
        auto_linked_count = _sync_account_bookings_by_email(email)
    _clear_manage_reset_state()
    _set_session_account_email(email, session_nonce=str(account.get("session_nonce") or ""))
    _track_analytics_event(
        event_type="account_signup",
        account_email=email,
        search_mode="account",
        success=True,
        metadata={
            "has_booking_reference": bool(booking_reference),
            "provider": "email",
        },
    )
    welcome_ok, welcome_reason = email_service.send_welcome_email(
        to_email=email,
        first_name=first_name,
    )
    if not welcome_ok:
        print(f"WELCOME EMAIL FAILED for {email}: {welcome_reason}")
    notice = "Account created. Future bookings can be linked for faster access."
    if auto_linked_count == 1:
        notice = "Account created. 1 previous booking was linked automatically."
    elif auto_linked_count > 1:
        notice = f"Account created. {auto_linked_count} previous bookings were linked automatically."
    _set_manage_account_notice(notice=notice)
    # The account is already signed in above — land them signed in, not back
    # on the login form (that used to read as "did my account even work?").
    if booking_reference:
        return redirect(url_for("manage_booking", booking_reference=booking_reference))
    if next_url:
        return redirect(next_url)
    return redirect(url_for("user_portal"))


@app.route("/manage-booking/account/login", methods=["POST"])
def manage_booking_account_login():
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    email = _normalize_email(str(request.form.get("account_email") or ""))
    password = str(request.form.get("account_password") or "")
    booking_reference = _normalize_booking_reference(str(request.form.get("booking_reference") or ""))
    next_url = _safe_next_url(request.form.get("next"))
    ip = _b2c_client_ip()

    if _b2c_login_is_locked(ip, email):
        _set_manage_account_notice(error="Too many failed login attempts. Please wait 15 minutes before trying again.")
        return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)

    account = _account_lookup(email)
    if not account:
        _b2c_login_record_failure(ip, email)
        _set_manage_account_notice(error="Invalid email or password.")
        return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)

    expected_hash = str(account.get("password_hash") or "")
    salt_hex = str(account.get("salt_hex") or "")
    if not expected_hash or not salt_hex:
        _set_manage_account_notice(error="That account is missing credentials. Please create a new one.")
        return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)

    if not hmac.compare_digest(_password_hash(password, salt_hex), expected_hash):
        attempts = _b2c_login_record_failure(ip, email)
        remaining = max(0, _B2C_LOGIN_MAX_ATTEMPTS - attempts)
        if remaining == 0:
            _set_manage_account_notice(error="Too many failed attempts. Account login locked for 15 minutes.")
        else:
            _set_manage_account_notice(error="Invalid email or password.")
        return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)

    _b2c_login_reset(ip, email)
    account = _update_account_login_metadata(dict(account))
    _account_save(email, account)
    _clear_manage_reset_state()
    _set_session_account_email(email, session_nonce=str(account.get("session_nonce") or ""))
    if booking_reference:
        _link_booking_to_account(email, booking_reference)
    linked_now = _sync_account_bookings_by_email(email)
    if linked_now == 0 and _discover_recent_booking_links_for_email(email) > 0:
        _sync_account_bookings_by_email(email)
    _track_analytics_event(
        event_type="account_login",
        account_email=email,
        search_mode="account",
        success=True,
        metadata={
            "has_booking_reference": bool(booking_reference),
            "provider": "email",
        },
    )
    first_name = str(account.get("first_name") or "").strip() or "User"
    _set_global_notice(f"Logged in as {first_name}.")
    if booking_reference:
        return redirect(url_for("manage_booking", booking_reference=booking_reference))
    if next_url:
        return redirect(next_url)
    return redirect(url_for("index"))


@app.route("/manage-booking/account/reset/request", methods=["POST"])
def manage_booking_account_reset_request():
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    email = _normalize_email(str(request.form.get("account_email") or ""))
    booking_reference = _normalize_booking_reference(str(request.form.get("booking_reference") or ""))
    if not email or "@" not in email:
        _set_manage_account_notice(error="Enter a valid email to send a verification code.")
        return _auth_redirect(mode="reset_request", booking_reference=booking_reference)

    account = _account_lookup(email)
    if not account:
        # Return a neutral message to avoid confirming whether the email is registered.
        _set_manage_account_notice(
            notice="If an account exists for that email, a verification code has been sent."
        )
        return _auth_redirect(mode="reset_request", booking_reference=booking_reference)

    _clear_manage_reset_state()
    numeric_code = int.from_bytes(os.urandom(3), byteorder="big") % 1_000_000
    verification_code = f"{numeric_code:06d}"
    ttl_minutes = 10
    _store_manage_reset_code(email, verification_code, ttl_seconds=ttl_minutes * 60)
    sent, reason = email_service.send_password_reset_code_email(
        to_email=email,
        verification_code=verification_code,
        ttl_minutes=ttl_minutes,
        first_name=str(account.get("first_name") or ""),
    )
    if sent:
        _set_manage_account_notice(
            notice="Verification code sent to your email. Enter it below to continue."
        )
        return _auth_redirect(mode="reset_verify", booking_reference=booking_reference, reset_email=email)

    dev_fallback_enabled = bool(app.config.get("TESTING") or email_service.allow_dev_code_fallback())
    if reason == "email_not_configured" and dev_fallback_enabled:
        _set_manage_account_notice(
            notice=(
                "Email is not configured yet. "
                f"Demo code: {verification_code}. Enter it below to continue."
            )
        )
        return _auth_redirect(mode="reset_verify", booking_reference=booking_reference, reset_email=email)

    reason_normalized = str(reason or "").strip()
    reason_key = reason_normalized.lower()

    if reason_key == "invalid_recipient":
        user_error = "That email address looks invalid. Please check it and try again."
    else:
        user_error = "We couldn't send the verification email right now. Please try again in a moment."

    _clear_manage_reset_state()
    _set_manage_account_notice(error=user_error)
    # Log the detailed reason server-side only — never expose SMTP config details to the browser.
    print(f"RESET CODE EMAIL FAILED for {email}: {reason}")
    return _auth_redirect(mode="reset_request", booking_reference=booking_reference, reset_email=email)


@app.route("/manage-booking/account/reset/verify", methods=["POST"])
def manage_booking_account_reset_verify():
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    email = _normalize_email(str(request.form.get("account_email") or ""))
    verification_code = str(request.form.get("verification_code") or "").strip()
    booking_reference = _normalize_booking_reference(str(request.form.get("booking_reference") or ""))

    expected_email = _normalize_email(str(session.get("ngf_reset_email_pending") or ""))
    expected_code = str(session.get("ngf_reset_code") or "").strip()
    expires_at = int(session.get("ngf_reset_code_expires_at") or 0)
    now_ts = int(time.time())

    if not email or "@" not in email:
        _set_manage_account_notice(error="Enter a valid email for verification.")
        return _auth_redirect(mode="reset_verify", booking_reference=booking_reference)
    if not re.fullmatch(r"\d{6}", verification_code):
        _set_manage_account_notice(error="Enter the 6-digit verification code.")
        return _auth_redirect(mode="reset_verify", booking_reference=booking_reference, reset_email=email)
    if not _account_lookup(email):
        _clear_manage_reset_state()
        _set_manage_account_notice(error="Verification failed. Please restart the password reset process.")
        return _auth_redirect(mode="reset_request", booking_reference=booking_reference)

    session_code_valid = (
        bool(expected_email)
        and bool(expected_code)
        and bool(expires_at)
        and now_ts <= expires_at
        and email == expected_email
        and hmac.compare_digest(verification_code, expected_code)
    )
    if expires_at and now_ts > expires_at:
        session.pop("ngf_reset_email_pending", None)
        session.pop("ngf_reset_code", None)
        session.pop("ngf_reset_code_expires_at", None)

    # Reset codes issued by this flow live in the signed browser session. The
    # old SQLite fallback table is only relevant for SQLite deployments; once
    # accounts use Supabase, attempting to open that ephemeral Vercel path
    # during verification can raise a 500 even when the emailed code is valid.
    db_code_valid = False
    if not _use_postgres_account_store():
        try:
            db_code_valid = analytics_store.validate_password_reset_code(
                ACCOUNT_DB_PATH,
                email,
                verification_code,
                consume=True,
            )
        except sqlite3.Error:
            db_code_valid = False
    if not session_code_valid and not db_code_valid:
        if expires_at and now_ts > expires_at:
            _set_manage_account_notice(error="Verification code expired. Request a new code.")
            return _auth_redirect(mode="reset_request", booking_reference=booking_reference, reset_email=email)
        _set_manage_account_notice(error="Invalid verification code. Please try again.")
        return _auth_redirect(mode="reset_verify", booking_reference=booking_reference, reset_email=email)

    reset_token = _issue_manage_reset_token(email)
    session.pop("ngf_reset_email_pending", None)
    session.pop("ngf_reset_code", None)
    session.pop("ngf_reset_code_expires_at", None)
    _set_manage_account_notice(notice="Verification complete. Secure reset link opened.")
    return _auth_redirect(
        mode="reset_complete",
        booking_reference=booking_reference,
        reset_email=email,
        reset_token=reset_token,
    )


@app.route("/manage-booking/account/reset-password", methods=["POST"])
def manage_booking_account_reset_password():
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    email = _normalize_email(str(request.form.get("account_email") or ""))
    reset_token = str(request.form.get("reset_token") or "").strip()
    new_password = str(request.form.get("new_password") or "")
    confirm_password = str(request.form.get("confirm_new_password") or "")
    booking_reference = _normalize_booking_reference(str(request.form.get("booking_reference") or ""))
    expected_token = str(session.get("ngf_reset_token") or "").strip()
    expected_email = _normalize_email(str(session.get("ngf_reset_token_email") or ""))
    token_expires_at = int(session.get("ngf_reset_token_expires_at") or 0)
    now_ts = int(time.time())

    if not email or "@" not in email:
        _set_manage_account_notice(error="Enter a valid email to reset password.")
        return _auth_redirect(mode="reset_complete", booking_reference=booking_reference)

    if (
        not reset_token
        or not expected_token
        or not expected_email
        or email != expected_email
        or not hmac.compare_digest(reset_token, expected_token)
        or (token_expires_at and now_ts > token_expires_at)
    ):
        _clear_manage_reset_state()
        _set_manage_account_notice(error="Reset link is invalid or expired. Request a new verification code.")
        return _auth_redirect(mode="reset_request", booking_reference=booking_reference, reset_email=email)

    if new_password != confirm_password:
        _set_manage_account_notice(error="New password confirmation does not match.")
        return _auth_redirect(mode="reset_complete", booking_reference=booking_reference, reset_email=email)
    if not _password_meets_criteria(new_password):
        _set_manage_account_notice(error="Use at least 8 characters including letters and numbers.")
        return _auth_redirect(mode="reset_complete", booking_reference=booking_reference, reset_email=email)

    account = _account_lookup(email)
    if not account:
        _clear_manage_reset_state()
        _set_manage_account_notice(error="Session expired. Please restart the password reset process.")
        return _auth_redirect(mode="reset_request", booking_reference=booking_reference)

    new_salt_hex = os.urandom(16).hex()
    account["salt_hex"] = new_salt_hex
    account["password_hash"] = _password_hash(new_password, new_salt_hex)
    _account_save(email, account)
    _clear_manage_reset_state()
    _set_manage_account_notice(notice="Password updated. Please sign in with your new password.")
    return _auth_redirect(mode="login", booking_reference=booking_reference)


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

def _apple_client_secret() -> str:
    """Generate a short-lived Apple client_secret JWT signed with the Apple ES256 private key."""
    now = int(time.time())
    headers = {"kid": APPLE_OAUTH_KEY_ID, "alg": "ES256"}
    payload = {
        "iss": APPLE_OAUTH_TEAM_ID,
        "iat": now,
        "exp": now + 86400 * 180,
        "aud": "https://appleid.apple.com",
        "sub": APPLE_OAUTH_CLIENT_ID,
    }
    return pyjwt.encode(payload, APPLE_OAUTH_PRIVATE_KEY, algorithm="ES256", headers=headers)


def _fetch_jwks(url: str) -> list[dict]:
    try:
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        return r.json().get("keys", [])
    except Exception:
        return []


def _verify_oidc_id_token(id_token_str: str, jwks_url: str, audience: str, issuer: str) -> dict:
    """Verify an OIDC id_token against a JWKS endpoint; return claims dict or raise."""
    header = pyjwt.get_unverified_header(id_token_str)
    kid = header.get("kid")
    alg = header.get("alg", "RS256")
    keys = _fetch_jwks(jwks_url)
    public_key = None
    for key_data in keys:
        if key_data.get("kid") == kid:
            if alg.startswith("RS"):
                from jwt.algorithms import RSAAlgorithm
                public_key = RSAAlgorithm.from_jwk(json.dumps(key_data))
            else:
                from jwt.algorithms import ECAlgorithm
                public_key = ECAlgorithm.from_jwk(json.dumps(key_data))
            break
    if public_key is None:
        raise ValueError(f"No matching JWKS key found for kid={kid}")
    return pyjwt.decode(
        id_token_str,
        public_key,
        algorithms=[alg],
        audience=audience,
        issuer=issuer,
        options={"verify_exp": True},
    )


def _oauth_login_or_create(
    email: str, first_name: str, last_name: str, provider: str, booking_reference: str, next_url: str = ""
):
    """Shared post-OAuth handler: log in existing account or create a new one, then redirect."""
    if not email:
        _set_manage_account_notice(
            error=f"{provider.title()} did not share an email address. Use email sign-in instead."
        )
        return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)

    account = _account_lookup(email)
    if account:
        account = _update_account_login_metadata(dict(account))
        account["oauth_provider"] = account.get("oauth_provider") or provider
        _account_save(email, account)
        _clear_manage_reset_state()
        _set_session_account_email(email, session_nonce=str(account.get("session_nonce") or ""))
        if booking_reference:
            _link_booking_to_account(email, booking_reference)
        _sync_account_bookings_by_email(email)
        _track_analytics_event(
            event_type="account_login",
            account_email=email,
            search_mode="account",
            success=True,
            metadata={"provider": provider},
        )
    else:
        salt_hex = os.urandom(16).hex()
        account = {
            "email": email,
            "first_name": first_name or email.split("@")[0],
            "last_name": last_name or "",
            "dob": "",
            "terms_accepted_at": datetime.utcnow().isoformat(timespec="seconds"),
            "salt_hex": salt_hex,
            "password_hash": "",
            "created_at": datetime.utcnow().isoformat(),
            "session_nonce": "",
            "last_login_at": "",
            "last_login_ip": "",
            "price_alerts_enabled": True,
            "route_tracking_enabled": True,
            "saved_searches": [],
            "linked_booking_references": [booking_reference] if booking_reference else [],
            "oauth_provider": provider,
        }
        account = _update_account_login_metadata(account)
        _account_save(email, account)
        _sync_account_bookings_by_email(email)
        _clear_manage_reset_state()
        _set_session_account_email(email, session_nonce=str(account.get("session_nonce") or ""))
        email_service.send_welcome_email(
            to_email=email,
            first_name=account["first_name"],
        )
        _track_analytics_event(
            event_type="account_signup",
            account_email=email,
            search_mode="account",
            success=True,
            metadata={"provider": provider},
        )

    if booking_reference:
        return redirect(url_for("manage_booking", booking_reference=booking_reference))
    if next_url:
        return redirect(next_url)
    return redirect(url_for("user_portal"))


# ---------------------------------------------------------------------------
# Google OAuth routes
# ---------------------------------------------------------------------------

@app.route("/auth/google")
def oauth_google_start():
    if not GOOGLE_OAUTH_CLIENT_ID or not GOOGLE_OAUTH_CLIENT_SECRET:
        _set_manage_account_notice(error="Google sign-in is not configured yet.")
        return redirect(url_for("auth_page"))
    state = secrets.token_urlsafe(32)
    session["oauth_google_state"] = state
    session["oauth_booking_ref"] = _normalize_booking_reference(
        str(request.args.get("booking_reference") or "")
    )
    session["oauth_next"] = _safe_next_url(request.args.get("next"))
    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": url_for("oauth_google_callback", _external=True),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return redirect(GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params))


@app.route("/auth/google/callback")
def oauth_google_callback():
    error = request.args.get("error")
    booking_reference = str(session.pop("oauth_booking_ref", "") or "")
    next_url = _safe_next_url(session.pop("oauth_next", "") or "")
    if error:
        _set_manage_account_notice(error="Google sign-in was cancelled.")
        return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)

    state = request.args.get("state", "")
    expected_state = session.pop("oauth_google_state", None)
    if not state or state != expected_state:
        _set_manage_account_notice(error="Invalid sign-in state. Please try again.")
        return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)

    code = request.args.get("code", "")
    try:
        token_resp = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": url_for("oauth_google_callback", _external=True),
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()
        access_token = str(token_data.get("access_token") or "")
        if not access_token:
            raise ValueError("No access_token in Google response")
        userinfo_resp = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=8,
        )
        userinfo_resp.raise_for_status()
        userinfo = userinfo_resp.json()
    except Exception as exc:
        print(f"Google OAuth error: {exc}")
        _set_manage_account_notice(error="Google sign-in failed. Please try again.")
        return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)

    email = _normalize_email(str(userinfo.get("email") or ""))
    first_name = str(userinfo.get("given_name") or "").strip()
    last_name = str(userinfo.get("family_name") or "").strip()
    return _oauth_login_or_create(email, first_name, last_name, "google", booking_reference, next_url)


# ---------------------------------------------------------------------------
# Apple Sign In routes
# ---------------------------------------------------------------------------

@app.route("/auth/apple")
def oauth_apple_start():
    if not APPLE_OAUTH_CLIENT_ID or not APPLE_OAUTH_TEAM_ID or not APPLE_OAUTH_KEY_ID or not APPLE_OAUTH_PRIVATE_KEY:
        _set_manage_account_notice(error="Apple sign-in is not configured yet.")
        return redirect(url_for("auth_page"))
    state = secrets.token_urlsafe(32)
    session["oauth_apple_state"] = state
    session["oauth_booking_ref"] = _normalize_booking_reference(
        str(request.args.get("booking_reference") or "")
    )
    session["oauth_next"] = _safe_next_url(request.args.get("next"))
    params = {
        "client_id": APPLE_OAUTH_CLIENT_ID,
        "redirect_uri": url_for("oauth_apple_callback", _external=True),
        "response_type": "code id_token",
        "scope": "name email",
        "state": state,
        "response_mode": "form_post",
    }
    return redirect(APPLE_AUTH_URL + "?" + urllib.parse.urlencode(params))


@app.route("/auth/apple/callback", methods=["POST"])
def oauth_apple_callback():
    error = request.form.get("error")
    booking_reference = str(session.pop("oauth_booking_ref", "") or "")
    next_url = _safe_next_url(session.pop("oauth_next", "") or "")
    if error:
        _set_manage_account_notice(error="Apple sign-in was cancelled.")
        return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)

    state = request.form.get("state", "")
    expected_state = session.pop("oauth_apple_state", None)
    if not state or state != expected_state:
        _set_manage_account_notice(error="Invalid sign-in state. Please try again.")
        return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)

    id_token_str = str(request.form.get("id_token") or "")
    user_json_str = str(request.form.get("user") or "")  # only present on first auth

    try:
        claims = _verify_oidc_id_token(
            id_token_str,
            jwks_url=APPLE_JWKS_URL,
            audience=APPLE_OAUTH_CLIENT_ID,
            issuer="https://appleid.apple.com",
        )
        email = _normalize_email(str(claims.get("email") or ""))
    except Exception as exc:
        print(f"Apple id_token verification error: {exc}")
        _set_manage_account_notice(error="Apple sign-in verification failed. Please try again.")
        return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)

    first_name, last_name = "", ""
    if user_json_str:
        try:
            user_data = json.loads(user_json_str)
            name = user_data.get("name") or {}
            first_name = str(name.get("firstName") or "").strip()
            last_name = str(name.get("lastName") or "").strip()
        except Exception:
            pass

    return _oauth_login_or_create(email, first_name, last_name, "apple", booking_reference, next_url)


@app.route("/manage-booking/account/social/<provider>", methods=["POST"])
def manage_booking_account_social(provider: str):
    booking_reference = _normalize_booking_reference(str(request.form.get("booking_reference") or ""))
    next_url = _safe_next_url(request.form.get("next"))
    _set_manage_account_notice(error="Unsupported social sign-in provider.")
    return _auth_redirect(mode="login", booking_reference=booking_reference, next_url=next_url)


@app.route("/manage-booking/account/logout", methods=["POST"])
def manage_booking_account_logout():
    _set_session_account_email("")
    _set_manage_account_notice(notice="Logged out of booking account.")
    return redirect(url_for("auth_page", mode="login", signed_out=1))


@app.route("/auth", methods=["GET"])
def auth_page():
    requested_mode = str(request.args.get("mode") or "").strip().lower()
    valid_modes = {"signup", "login", "reset_request", "reset_verify", "reset_complete"}
    mode = requested_mode if requested_mode in valid_modes else "login"
    reset_email = _normalize_email(str(request.args.get("reset_email") or ""))
    reset_token = str(request.args.get("reset_token") or "").strip()
    booking_reference = _normalize_booking_reference(str(request.args.get("booking_reference") or ""))
    next_url = _safe_next_url(request.args.get("next"))
    signed_out = str(request.args.get("signed_out") or "").strip().lower() in {"1", "true", "yes"}

    # A password-reset request can legitimately begin while the user is still
    # signed in (for example, they are changing a password from the account
    # menu).  Do not bounce those reset steps back to the portal: doing so
    # previously hid the six-digit-code screen immediately after a successful
    # request.
    reset_modes = {"reset_request", "reset_verify", "reset_complete"}
    if not signed_out and _session_account_email() and mode not in reset_modes:
        if booking_reference:
            return redirect(url_for("manage_booking", booking_reference=booking_reference))
        if next_url:
            return redirect(next_url)
        return redirect(url_for("user_portal"))

    account_notice, account_error = _pop_manage_account_notice()
    return render_template(
        "auth.html",
        auth_mode=mode,
        reset_email=reset_email,
        reset_token=reset_token,
        booking_reference=booking_reference,
        next=next_url,
        signed_out=signed_out,
        account_notice=account_notice,
        account_error=account_error,
        csrf_token=_b2c_csrf_token(),
    )


@app.route("/portal", methods=["GET"])
def user_portal():
    account_email = _session_account_email()
    account = _account_lookup(account_email) if account_email else None
    if not account:
        return redirect(url_for("index"))

    saved_searches = _safe_saved_searches(account.get("saved_searches"))
    saved_search_cards = [_saved_search_card_view(item) for item in saved_searches]
    top_routes = _top_routes_from_saved_searches(saved_searches)
    active_sessions = [
        {
            "label": "Current browser session",
            "ip": str(account.get("last_login_ip") or "Unknown"),
            "last_seen": str(account.get("last_login_at") or "").replace("T", " "),
        }
    ]
    account_notice, account_error = _pop_manage_account_notice()
    return render_template(
        "user_portal.html",
        account=account,
        saved_searches=saved_searches,
        saved_search_cards=saved_search_cards,
        top_routes=top_routes,
        active_sessions=active_sessions,
        page_notice="",
        account_notice=account_notice,
        account_error=account_error,
        csrf_token=_b2c_csrf_token(),
    )


@app.route("/portal/searches/clear", methods=["POST"])
def user_portal_clear_searches():
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    account_email = _session_account_email()
    account = _account_lookup(account_email) if account_email else None
    if not account_email or not account:
        return _auth_redirect(mode="login")

    account["saved_searches"] = []
    _account_save(account_email, account)
    return redirect(url_for("user_portal"))


@app.route("/portal/logout", methods=["POST"])
def user_portal_logout():
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    _set_session_account_email("")
    _set_manage_account_notice(notice="Signed out of your account.")
    return redirect(url_for("auth_page", mode="login", signed_out=1))


@app.route("/portal/preferences", methods=["POST"])
def user_portal_update_preferences():
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    account_email = _session_account_email()
    account = _account_lookup(account_email) if account_email else None
    if not account_email or not account:
        return _auth_redirect(mode="login")
    account["price_alerts_enabled"] = str(request.form.get("price_alerts_enabled") or "").strip().lower() in {"1", "true", "on", "yes"}
    account["route_tracking_enabled"] = str(request.form.get("route_tracking_enabled") or "").strip().lower() in {"1", "true", "on", "yes"}
    _account_save(account_email, account)
    return redirect(url_for("user_portal"))


@app.route("/portal/profile", methods=["POST"])
def user_portal_update_profile():
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    account_email = _session_account_email()
    account = _account_lookup(account_email) if account_email else None
    if not account_email or not account:
        return _auth_redirect(mode="login")
    account["first_name"] = _normalize_person_name(str(request.form.get("first_name") or "").strip())
    account["last_name"] = _normalize_person_name(str(request.form.get("last_name") or "").strip())
    dob_raw = str(request.form.get("dob") or "").strip()
    if not dob_raw or _valid_dob(dob_raw):
        account["dob"] = dob_raw
    account["phone_number"] = str(request.form.get("phone_number") or "").strip()[:32]
    account["nationality"] = str(request.form.get("nationality") or "").strip()[:64]
    account["passport_number"] = str(request.form.get("passport_number") or "").strip()[:32]
    valid_genders = {"", "male", "female", "non_binary", "prefer_not_to_say"}
    gender_raw = str(request.form.get("gender") or "").strip().lower()
    account["gender"] = gender_raw if gender_raw in valid_genders else ""
    _account_save(account_email, account)
    return redirect(url_for("user_portal") + "?tab=profile&saved=1")


@app.route("/portal/security/revoke", methods=["POST"])
def user_portal_revoke_sessions():
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    account_email = _session_account_email()
    account = _account_lookup(account_email) if account_email else None
    if not account_email or not account:
        return _auth_redirect(mode="login")
    account["session_nonce"] = os.urandom(12).hex()
    account["last_login_at"] = datetime.utcnow().isoformat(timespec="seconds")
    _account_save(account_email, account)
    _set_session_account_email(account_email, session_nonce=str(account.get("session_nonce") or ""))
    return redirect(url_for("user_portal"))


@app.route("/terms", methods=["GET"])
def terms_and_conditions():
    return render_template("terms.html")


@app.route("/manage-booking/detail")
def booking_detail():
    order_id = str(session.get("ngf_manage_order_id") or "").strip()
    booking_reference = str(session.get("ngf_manage_booking_reference") or "").strip()
    if not order_id and not booking_reference:
        return redirect(url_for("manage_booking"))
    change_notice = str(session.pop("ngf_detail_change_notice", "") or "").strip()
    change_error = str(session.pop("ngf_detail_change_error", "") or "").strip()
    # If we just performed a cancel/change, bypass the cache so Duffel's updated
    # status is always reflected immediately rather than serving a stale "confirmed".
    force_refresh = bool(change_notice or change_error)
    order = None
    if order_id:
        order = None if force_refresh else RECENT_ORDER_CACHE.get(order_id)
        if not order:
            try:
                order = DUFF.get_order(order_id)
                if order:
                    RECENT_ORDER_CACHE.set(order_id, order)
                    ref_norm = _normalize_booking_reference(str(order.get("booking_reference") or booking_reference))
                    if ref_norm:
                        RECENT_REF_CACHE.set(ref_norm, order)
            except Exception:
                pass
    if not order and booking_reference:
        order = _latest_order_for_reference(booking_reference)
    if not order:
        return redirect(url_for("manage_booking"))
    _capture_booking_email_links(order=order, passengers_payload=None)
    order_summary = build_order_summary(order)
    manage_model = _build_manage_booking_model(order)
    return render_template(
        "booking_detail.html",
        order_summary=order_summary,
        manage_model=manage_model,
        change_notice=change_notice,
        change_error=change_error,
        checkin_url=_airline_checkin_url(order_summary.get("airline_iata")),
        duffel_env=DUFFEL_ENV,
    )


@app.route("/manage-booking/linked/<booking_reference>", methods=["POST"])
def open_linked_booking(booking_reference: str):
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    account_email = _session_account_email()
    account = _account_lookup(account_email) if account_email else None
    normalized_ref = _normalize_booking_reference(booking_reference)
    linked = {
        _normalize_booking_reference(str(item))
        for item in ((account or {}).get("linked_booking_references") or [])
    }
    if not account_email or not account or normalized_ref not in linked:
        _set_manage_account_notice(error="That booking is not linked to your account.")
        return redirect(url_for("manage_booking"))

    order = _latest_order_for_reference(normalized_ref)
    if not order:
        _set_manage_account_notice(error="We couldn't refresh that linked booking right now.")
        return redirect(url_for("manage_booking", booking_reference=normalized_ref))

    order_id = str(order.get("id") or normalized_ref)
    RECENT_ORDER_CACHE.set(order_id, order)
    session["ngf_manage_order_id"] = order_id
    session["ngf_manage_booking_reference"] = normalized_ref
    return redirect(url_for("booking_detail"))


@app.route("/manage-booking/<order_id>/cancel", methods=["POST"])
def manage_booking_cancel(order_id: str):
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    stored_order_id = str(session.get("ngf_manage_order_id") or "").strip()
    if not stored_order_id or stored_order_id != order_id:
        _set_manage_account_notice(error="Please verify your booking again before canceling.")
        return redirect(url_for("manage_booking"))
    try:
        order = DUFF.get_order(order_id)
    except DuffelAPIError as exc:
        session["ngf_detail_change_error"] = str(exc)
        return redirect(url_for("booking_detail"))
    model = _build_manage_booking_model(order)
    if not model.get("can_cancel"):
        session["ngf_detail_change_error"] = "Cancellation is not available for this booking."
        return redirect(url_for("booking_detail"))
    try:
        quote = DUFF.create_order_cancellation(order_id)
        cancellation_id = str(quote.get("id") or "").strip()
        if not cancellation_id:
            raise DuffelAPIError("Cancellation quote ID missing from Duffel response.")
        confirmed = DUFF.confirm_order_cancellation(cancellation_id)
    except DuffelAPIError as exc:
        session["ngf_detail_change_error"] = str(exc)
        return redirect(url_for("booking_detail"))
    refreshed_order = DUFF.get_order(order_id)
    if refreshed_order and order_id:
        RECENT_ORDER_CACHE.set(order_id, refreshed_order)
        ref_norm = _normalize_booking_reference(str(refreshed_order.get("booking_reference") or ""))
        if ref_norm:
            RECENT_REF_CACHE.set(ref_norm, refreshed_order)
    def _parse_refund(val: Any) -> float:
        try:
            return float(val) if val is not None else 0.0
        except (ValueError, TypeError):
            return 0.0

    confirmed_val = _parse_refund(confirmed.get("refund_amount"))
    quote_val = _parse_refund(quote.get("refund_amount"))
    raw_refund = confirmed_val if confirmed_val > 0 else quote_val

    # Duffel test mode returns total_amount as refund_amount without deducting
    # the cancellation penalty from fare conditions. Apply it ourselves so the
    # displayed amount is never higher than total - penalty.
    raw_cond = order.get("conditions") or {}
    refund_cond = (raw_cond.get("refund_before_departure") or {}) if isinstance(raw_cond, Mapping) else {}
    penalty_val = _parse_refund(refund_cond.get("penalty_amount"))
    total_val = _parse_refund(order.get("total_amount"))
    if penalty_val > 0 and total_val > 0:
        refund_value = max(0.0, min(raw_refund, total_val - penalty_val))
    else:
        refund_value = raw_refund
    refund_amount = f"{refund_value:.2f}" if refund_value > 0 else ""
    refund_currency = str(
        confirmed.get("refund_currency") or quote.get("refund_currency")
        or order.get("total_currency") or "USD"
    ).strip()
    notice = "Cancellation confirmed."
    if refund_value > 0:
        notice = f"Cancellation confirmed. Refund of {refund_currency} {refund_amount} will be returned to your original payment method."
    session["ngf_detail_change_notice"] = notice
    try:
        summary = build_order_summary(refreshed_order or order)
        recipients = _order_passenger_emails(refreshed_order or order)
        acct_email = _session_account_email()
        if acct_email and acct_email not in recipients:
            recipients.append(acct_email)
        manage_url = url_for("manage_booking", _external=True)
        for recipient in recipients:
            email_service.send_cancellation_email(
                to_email=recipient,
                order_summary=summary,
                refund_amount=refund_amount,
                refund_currency=refund_currency,
                manage_url=manage_url,
            )
    except Exception:
        pass
    return redirect(url_for("booking_detail"))


@app.route("/manage-booking/<order_id>/change-options", methods=["POST"])
def manage_booking_change_options(order_id: str):
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    stored_order_id = str(session.get("ngf_manage_order_id") or "").strip()
    if not stored_order_id or stored_order_id != order_id:
        _set_manage_account_notice(error="Please verify your booking again before requesting changes.")
        return redirect(url_for("manage_booking"))
    try:
        order = DUFF.get_order(order_id)
    except DuffelAPIError as exc:
        return _render_manage_booking_page(booking_error=str(exc))
    model = _build_manage_booking_model(order)
    if not model.get("can_change"):
        return _render_manage_booking_page(
            order=order,
            change_error="Flight changes are not available for this booking via Duffel.",
            signup_offer=True,
        )
    slice_id = str(request.form.get("slice_id") or "").strip()
    departure_date = str(request.form.get("departure_date") or "").strip()
    cabin_class = str(request.form.get("cabin_class") or "economy").strip().lower() or "economy"
    candidate = next((item for item in model.get("change_candidates", []) if item.get("slice_id") == slice_id), None)
    if not candidate:
        return _render_manage_booking_page(order=order, change_error="Choose a leg to change.", signup_offer=True)
    try:
        req_payload = DUFF.create_order_change_request(
            order_id=order_id,
            slice_id_to_remove=slice_id,
            origin=str(candidate.get("origin") or ""),
            destination=str(candidate.get("destination") or ""),
            departure_date=departure_date,
            cabin_class=cabin_class,
        )
    except DuffelAPIError as exc:
        return _render_manage_booking_page(order=order, change_error=str(exc), signup_offer=True)
    offers = req_payload.get("order_change_offers") or []
    if not isinstance(offers, list):
        offers = []
    parsed_offers: list[dict[str, Any]] = []
    for offer in offers:
        offer_id = str(offer.get("id") or "").strip()
        if not offer_id:
            continue
        change_total = str(offer.get("change_total_amount") or "").strip()
        currency = str(offer.get("change_total_currency") or order.get("total_currency") or "USD").strip() or "USD"
        penalty_amount = str(offer.get("penalty_total_amount") or "").strip()
        parsed_offers.append(
            {
                "id": offer_id,
                "change_total_amount": change_total or "0.00",
                "change_total_currency": currency,
                "penalty_total_amount": penalty_amount or "0.00",
                "expires_at": str(offer.get("expires_at") or "").strip(),
            }
        )
    ORDER_CHANGE_OPTIONS_CACHE.set(order_id, parsed_offers)
    if not parsed_offers:
        return _render_manage_booking_page(
            order=order,
            change_error="No change offers are currently available from the airline for that selection.",
            signup_offer=True,
        )
    return _render_manage_booking_page(
        order=order,
        change_notice=f"Found {len(parsed_offers)} live change option(s).",
        change_offers=parsed_offers,
        signup_offer=True,
    )


@app.route("/manage-booking/<order_id>/change-apply", methods=["POST"])
def manage_booking_change_apply(order_id: str):
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403
    stored_order_id = str(session.get("ngf_manage_order_id") or "").strip()
    if not stored_order_id or stored_order_id != order_id:
        _set_manage_account_notice(error="Please verify your booking again before applying changes.")
        return redirect(url_for("manage_booking"))
    offer_id = str(request.form.get("order_change_offer_id") or "").strip()
    if not offer_id:
        return _render_manage_booking_page(change_error="Select a change option to continue.")
    cached_offers = ORDER_CHANGE_OPTIONS_CACHE.get(order_id) or []
    selected_offer = next((item for item in cached_offers if str(item.get("id") or "").strip() == offer_id), None)
    if not selected_offer:
        return _render_manage_booking_page(change_error="That change offer has expired. Request new options.")

    amount = str(selected_offer.get("change_total_amount") or "0.00").strip() or "0.00"
    currency = str(selected_offer.get("change_total_currency") or "USD").strip() or "USD"
    try:
        pending_change = DUFF.create_order_change(offer_id)
        order_change_id = str(pending_change.get("id") or "").strip()
        if not order_change_id:
            raise DuffelAPIError("Duffel did not return an order change ID.")
        if amount.startswith("-"):
            # Refund scenario: confirm with zero balance payment details.
            DUFF.confirm_order_change(order_change_id, amount="0.00", currency=currency)
        else:
            DUFF.confirm_order_change(order_change_id, amount=amount, currency=currency)
        refreshed = DUFF.get_order(order_id)
    except DuffelAPIError as exc:
        try:
            current_order = DUFF.get_order(order_id)
        except DuffelAPIError:
            current_order = None
        return _render_manage_booking_page(
            order=current_order,
            change_error=str(exc),
            change_offers=cached_offers,
            signup_offer=True,
        )

    ORDER_CHANGE_OPTIONS_CACHE.set(order_id, [])
    return _render_manage_booking_page(
        order=refreshed,
        change_notice="Flight change confirmed with Duffel and synchronized to your booking.",
        signup_offer=True,
    )


@app.route("/airports")
def airports():
    q = (request.args.get("q") or "").strip()
    qn = _norm(q)
    if len(qn) < 3:
        return jsonify([])

    cache_key = f"v6:{qn}"
    cached = AIRPORT_SUGGEST_CACHE.get(cache_key)
    if cached is not None:
        return jsonify(cached)

    intent = _query_intent(qn)
    local_city = _local_iata_city_suggestions(q)
    local_airports = _local_airport_suggest(q, limit=AIRPORT_SUGGEST_LIMIT)

    # Exact airport IATA / US state: local airports only, but still prepend any
    # matching grouped city rows (e.g. ROM when searching "rome"). Metro and
    # tri-letter IATA *city* codes (BJS) merge with Duffel below.
    if local_airports and (
        intent in {"iata", "state"}
        or (len(qn) <= 4 and intent not in {"metro", "city_code"})
    ):
        merged_sc = (local_city + local_airports)[:AIRPORT_SUGGEST_LIMIT]
        AIRPORT_SUGGEST_CACHE.set(cache_key, merged_sc)
        _track_analytics_event(
            event_type="airport_suggestions_served",
            search_mode="airport_autocomplete",
            result_count=len(merged_sc),
            success=bool(merged_sc),
            metadata={
                "query": q[:80],
                "intent": intent,
                "source": "local_only",
                "top_codes": [str(item.get("code") or "").strip().upper() for item in merged_sc[:5]],
            },
        )
        return jsonify(merged_sc)

    remote = []
    try:
        data = DUFF.search_places(q, limit=12)
        seen = set()
        for item in data:
            code = item.get("code")
            if not code or code in seen:
                continue
            if not _remote_place_matches_query(qn, item):
                continue
            seen.add(code)
            remote.append(item)
    except Exception:
        remote = []

    remote_cities = [it for it in remote if (it.get("subType") or "").upper() == "CITY"]
    remote_airports = [it for it in remote if (it.get("subType") or "").upper() != "CITY"]
    stream = local_city + remote_cities + local_airports + remote_airports

    merged = []
    seen = set()

    for item in stream:
        code = item["code"]
        if code in seen:
            continue
        seen.add(code)
        merged.append(item)
        if len(merged) >= AIRPORT_SUGGEST_LIMIT:
            break

    AIRPORT_SUGGEST_CACHE.set(cache_key, merged)
    _track_analytics_event(
        event_type="airport_suggestions_served",
        search_mode="airport_autocomplete",
        result_count=len(merged),
        success=bool(merged),
        metadata={
            "query": q[:80],
            "intent": intent,
            "source": "merged_local_remote",
            "top_codes": [str(item.get("code") or "").strip().upper() for item in merged[:5]],
        },
    )
    return jsonify(merged)

@app.route("/")
def index():
    # One landing page serves every product: ?tab=hotels|ai deep-links the
    # matching search tab (hotels falls back to flights while Stays is off).
    requested_tab = (request.args.get("tab") or "").strip().lower()
    if requested_tab in ("hotels", "hotel", "stays") and LITE_ENABLED:
        initial_tab = "stays"
    elif requested_tab in ("ai", "ask-ai", "askai"):
        initial_tab = "ai"
    else:
        initial_tab = "flights"

    _track_analytics_event(
        event_type="site_landed",
        search_mode="browse",
        success=True,
        metadata={"page": "home", "tab": initial_tab},
    )
    return render_template(
        "index.html",
        ai_suggestion_chips=AI_HOME_SUGGESTION_CHIPS,
        global_notice=_pop_global_notice(),
        edit_search_fields=_pop_edit_search_fields(),
        destinations=DESTINATIONS,
        destination_categories=CATEGORIES,
        domestic_destinations=DOMESTIC_DESTINATIONS,
        voice_ai_enabled=VOICE_AI_ENABLED,
        initial_tab=initial_tab,
        hotels_enabled=LITE_ENABLED,
    )


def _hotel_nights(checkin: str, checkout: str) -> int:
    try:
        start = datetime.strptime(checkin, "%Y-%m-%d").date()
        end = datetime.strptime(checkout, "%Y-%m-%d").date()
    except ValueError:
        return 0
    return max(0, (end - start).days)


def _hotel_search_defaults() -> dict[str, str]:
    today = date.today()
    return {
        "checkin": (today + timedelta(days=30)).isoformat(),
        "checkout": (today + timedelta(days=33)).isoformat(),
    }


@app.route("/hotels")
def hotels():
    if not LITE_ENABLED:
        _track_analytics_event(
            event_type="coming_soon_viewed",
            search_mode="browse",
            success=True,
            metadata={"page": "hotels"},
        )
        return render_template(
            "coming_soon.html",
            feature_name="Hotels",
            feature_label="Hotel stays",
            feature_description="Beautiful places to stay, matched to your trip rhythm, budget, and travel style.",
        )

    # Hotels no longer has a separate landing page: the home page hosts the
    # Flights / Hotels / Ask AI tabs, so land on it with Hotels selected.
    # (Deeper hotel routes — results, detail, AI search — are unchanged, and
    # their error redirects to url_for("hotels") arrive here too.)
    return redirect(url_for("index", tab="hotels"))


# Landing-page showcase rails. Pricing properties takes seconds, so these are
# fetched async by the client and memoised here for a short period.  The price
# cache is intentionally much shorter than static hotel-content caching: rate
# availability is live, while repeated browser refreshes should not create a
# supplier-call stampede.
HOTEL_SHOWCASE_CITIES = ["Paris", "Rome", "Dubai", "Tokyo", "Barcelona", "Istanbul"]
HOTEL_SHOWCASE_TTL = float(os.getenv("HOTEL_SHOWCASE_TTL", "120"))
HOTEL_SHOWCASE_SIZE = 12
HOTEL_SHOWCASE_INITIAL_CITIES = max(
    1,
    min(len(HOTEL_SHOWCASE_CITIES), int(os.getenv("HOTEL_SHOWCASE_INITIAL_CITIES", "2"))),
)
HOTEL_LIVE_CARD_TTL = float(os.getenv("HOTEL_LIVE_CARD_TTL", "90"))
HOTEL_LIVE_CARD_CACHE_SIZE = int(os.getenv("HOTEL_LIVE_CARD_CACHE_SIZE", "192"))
HOTEL_LIVE_CARD_MAX_INFLIGHT = int(os.getenv("HOTEL_LIVE_CARD_MAX_INFLIGHT", "24"))
_hotel_showcase_cache = SingleFlightTTLCache(
    maxsize=8,
    ttl_seconds=HOTEL_SHOWCASE_TTL,
    max_inflight=4,
)
_hotel_live_card_cache = SingleFlightTTLCache(
    maxsize=HOTEL_LIVE_CARD_CACHE_SIZE,
    ttl_seconds=HOTEL_LIVE_CARD_TTL,
    max_inflight=HOTEL_LIVE_CARD_MAX_INFLIGHT,
)


def _showcase_cached(key: str, producer):
    return _hotel_showcase_cache.get_or_build(key, producer)


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    from math import asin, cos, radians, sin, sqrt
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 3958.8 * 2 * asin(sqrt(a))


def _showcase_dates() -> tuple[str, str, int]:
    """One night, 30 days out — matches the reference's 'x 1 night' framing."""
    start = date.today() + timedelta(days=30)
    return start.isoformat(), (start + timedelta(days=1)).isoformat(), 1


def _price_content_rows(
    content: Sequence[Mapping[str, Any]],
    *,
    limit: int = 50,
    checkin: str | None = None,
    checkout: str | None = None,
    nights: int | None = None,
    adults: int = 2,
    rooms: int = 1,
) -> list[dict[str, Any]]:
    if not checkin or not checkout or nights is None:
        checkin, checkout, nights = _showcase_dates()
    by_id = {str(r.get("id") or "").strip(): r for r in content[:limit]}
    by_id = {hotel_id: row for hotel_id, row in by_id.items() if hotel_id}
    if not by_id:
        return []
    rate_rows = LITE.search_rates(
        hotel_ids=list(by_id.keys()), checkin=checkin, checkout=checkout, adults=adults, rooms=rooms,
    )
    return build_hotel_cards(rate_rows, by_id, nights=nights, facility_names=LITE.facility_names())


def _priced_coordinate_cards(
    *,
    latitude: float,
    longitude: float,
    checkin: str,
    checkout: str,
    nights: int,
    adults: int = 2,
    rooms: int = 1,
    radius: int = 25000,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Price nearby inventory once for a rounded browser-location cell.

    The cached cards deliberately exclude distance and URL fields. Those are
    request-specific and are added by the caller using the visitor's precise
    coordinate, while expensive content/rate work is coalesced for people in
    the same small area.
    """
    rounded_latitude = round(latitude, 2)
    rounded_longitude = round(longitude, 2)
    key = (
        "coordinate-cards",
        f"{rounded_latitude:.2f}",
        f"{rounded_longitude:.2f}",
        int(radius),
        int(limit),
        checkin,
        checkout,
        int(adults),
        int(rooms),
    )

    def build() -> list[dict[str, Any]]:
        content = LITE.hotels_for_coordinates(
            rounded_latitude, rounded_longitude, radius=radius, limit=limit,
        )
        cards = _price_content_rows(
            content,
            limit=limit,
            checkin=checkin,
            checkout=checkout,
            nights=nights,
            adults=adults,
            rooms=rooms,
        )
        return [card for card in cards if card.get("photo")]

    return _hotel_live_card_cache.get_or_build(key, build)


def _card_link(card: Mapping[str, Any], place_id: str = "", place_name: str = "") -> str:
    checkin, checkout, _ = _showcase_dates()
    return url_for(
        "hotel_detail", hotel_id=card["hotel_id"], checkin=checkin, checkout=checkout,
        adults=2, rooms=1, place_id=place_id, place_name=place_name,
    )


@app.route("/api/hotels/recommended", methods=["GET"])
def hotel_recommended():
    """Marquee properties across a bounded set of cities.

    The former cold path priced six cities one after another.  This starts with
    two curated cities, prices them concurrently, and takes more cards from
    each.  The rail remains full when inventory is available without making a
    first-time visitor wait for six separate supplier rate calls.
    """
    checkin, checkout, nights = _showcase_dates()
    cities = HOTEL_SHOWCASE_CITIES[:HOTEL_SHOWCASE_INITIAL_CITIES]
    cards_per_city = max(1, (HOTEL_SHOWCASE_SIZE + len(cities) - 1) // len(cities))

    def city_cards(city: str) -> list[dict[str, Any]]:
        key = ("showcase-city", city.casefold(), checkin, checkout, cards_per_city)

        def build_city() -> list[dict[str, Any]]:
            places = LITE.search_places(city)
            if not places:
                return []
            place = places[0]
            content = LITE.hotels_for_place(place["place_id"], limit=50)
            cards = _price_content_rows(
                content,
                checkin=checkin,
                checkout=checkout,
                nights=nights,
                adults=2,
                rooms=1,
            )

            # Curated shelf, not a bargain bin — but rank on rating *and*
            # review volume, otherwise every slot is a 10/10 with 3 reviews.
            def rank(card: Mapping[str, Any]) -> tuple[float, float]:
                rating = float(card.get("rating") or 0)
                reviews = float(card.get("review_count") or 0)
                weighted = (rating * reviews + 8.0 * 200) / (reviews + 200)
                return (-weighted, float(card["offer"]["total_amount"]))

            cards = [card for card in cards if card.get("photo")]
            cards.sort(key=rank)
            out: list[dict[str, Any]] = []
            for card in cards[:cards_per_city]:
                # `url_for` needs the request context, so retain these tiny
                # routing hints here and turn them into public card fields on
                # the request thread after the concurrent workers finish.
                card["_showcase_place_id"] = place["place_id"]
                card["_showcase_place_name"] = place["name"]
                out.append(card)
            return out

        try:
            return _hotel_live_card_cache.get_or_build(key, build_city)
        except LiteAPIError as exc:
            print("SHOWCASE RECOMMENDED ERROR:", city, exc)
            return []

    def build():
        by_city: dict[str, list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=len(cities)) as pool:
            futures = {pool.submit(city_cards, city): city for city in cities}
            for future in as_completed(futures):
                city = futures[future]
                try:
                    by_city[city] = future.result()
                except Exception as exc:
                    print("SHOWCASE RECOMMENDED WORKER ERROR:", city, repr(exc))
                    by_city[city] = []

        picks: list[dict[str, Any]] = []
        for city in cities:
            for cached_card in by_city.get(city, []):
                card = dict(cached_card)
                place_id = str(card.pop("_showcase_place_id", "") or "")
                place_name = str(card.pop("_showcase_place_name", "") or "")
                card["url"] = _card_link(card, place_id, place_name)
                card["place_name"] = place_name
                picks.append(card)
        return picks[:HOTEL_SHOWCASE_SIZE]

    return jsonify(_showcase_cached(f"recommended:{checkin}:{checkout}", build))


@app.route("/api/hotels/nearby", methods=["GET"])
def hotel_nearby():
    """Properties around the visitor's coordinates, with distance from centre."""
    try:
        lat = float(request.args.get("lat"))
        lng = float(request.args.get("lng"))
    except (TypeError, ValueError):
        return jsonify([])
    if not LITE_ENABLED or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify([])

    checkin, checkout, nights = _showcase_dates()
    try:
        base_cards = _priced_coordinate_cards(
            latitude=lat,
            longitude=lng,
            checkin=checkin,
            checkout=checkout,
            nights=nights,
            adults=2,
            rooms=1,
            radius=25000,
            limit=50,
        )
    except LiteAPIError as exc:
        print("SHOWCASE NEARBY ERROR:", exc)
        return jsonify([])

    # Distances are deliberately calculated outside the rounded-coordinate
    # price cache, so every visitor still sees their own exact proximity.
    cards = [dict(card) for card in base_cards]
    for card in cards:
        try:
            card["distance_miles"] = round(
                _haversine_miles(lat, lng, float(card["latitude"]), float(card["longitude"])), 1
            )
        except (TypeError, ValueError):
            card["distance_miles"] = None
        card["url"] = _card_link(card)
    cards.sort(key=lambda c: (c.get("distance_miles") is None, c.get("distance_miles") or 0))
    return jsonify(cards[:HOTEL_SHOWCASE_SIZE])


@app.route("/api/hotels/flight-stays", methods=["GET"])
def flight_destination_stays():
    """Bookable stays at the flight destination for the selected trip dates.

    This is deliberately fetched after a flight-results page has loaded: hotel
    rate lookups are comparatively slow, and should never hold up flight
    results.  It shares the exact same LiteAPI content/rate path as Hotels.
    """
    destination_input = (request.args.get("destination") or "").strip()
    checkin = (request.args.get("checkin") or "").strip()
    checkout = (request.args.get("checkout") or "").strip()
    nights = _hotel_nights(checkin, checkout)
    if not LITE_ENABLED or not destination_input or nights <= 0:
        return jsonify({"recommended": [], "nearby": []})

    adults = _coerce_passengers(request.args.get("adults"), default=2)
    airport_code = _normalize_airport_input(destination_input)
    airport = _airport_code_map().get(airport_code or "")
    destination_name = str((airport or {}).get("city") or destination_input).strip()
    # Some airport source rows carry a municipality qualifier, e.g.
    # "Paris (Roissy-en-France, Val-d'Oise)".  It helps an airport picker,
    # but is a poorer query/heading for a city-wide hotel search.
    destination_name = destination_name.split(" (", 1)[0].strip()
    if not destination_name:
        return jsonify({"recommended": [], "nearby": []})

    cache_key = (
        "flight-destination-cards",
        destination_name.casefold(),
        checkin,
        checkout,
        adults,
        1,
    )

    def build_payload() -> dict[str, Any]:
        places = LITE.search_places(destination_name)
        if not places:
            return {"recommended": [], "nearby": []}
        place = places[0]
        content = LITE.hotels_for_place(place["place_id"], limit=50)
        if not any(str(row.get("id") or "").strip() for row in content):
            return {"recommended": [], "nearby": []}
        cards = _price_content_rows(
            content,
            limit=50,
            checkin=checkin,
            checkout=checkout,
            nights=nights,
            adults=adults,
            rooms=1,
        )

        cards = [card for card in cards if card.get("photo")]
        for card in cards:
            card["url"] = url_for(
                "hotel_detail", hotel_id=card["hotel_id"], checkin=checkin, checkout=checkout,
                adults=adults, rooms=1, place_id=place["place_id"], place_name=place["name"],
            )

        # Favour well-reviewed hotels for the curated rail, then show the most
        # compelling remaining options from the destination area as "nearby".
        def recommended_rank(card: Mapping[str, Any]) -> tuple[float, float]:
            rating = float(card.get("rating") or 0)
            reviews = float(card.get("review_count") or 0)
            weighted_rating = (rating * reviews + 8.0 * 200) / (reviews + 200)
            return (-weighted_rating, float(card["offer"]["total_amount"]))

        recommended = sorted(cards, key=recommended_rank)[:4]
        selected_ids = {card["hotel_id"] for card in recommended}
        nearby = [card for card in cards if card["hotel_id"] not in selected_ids]
        nearby.sort(key=lambda card: float(card["offer"]["total_amount"]))

        browse_url = url_for(
            "hotel_search", place_id=place["place_id"], place_name=place["name"],
            checkin=checkin, checkout=checkout, adults=adults, rooms=1,
        )
        return {
            "destination": destination_name,
            "nights": nights,
            "price_display": "total",
            "browse_url": browse_url,
            "recommended": recommended,
            "nearby": nearby[:4],
        }

    try:
        payload = _hotel_live_card_cache.get_or_build(cache_key, build_payload)
    except LiteAPIError as exc:
        print("FLIGHT DESTINATION STAYS ERROR:", destination_name, exc)
        payload = {"recommended": [], "nearby": []}
    return jsonify(payload)


@app.route("/api/hotels/flight-stays/nearby", methods=["GET"])
def flight_local_stays():
    """Tonight's priced stays around the visitor's browser-provided location.

    The flights landing page uses this as its useful default before a traveller
    has selected a destination and dates.  It deliberately uses a one-night
    stay so the card headline is an actual current nightly rate, rather than a
    future showcase estimate.
    """
    try:
        latitude = float(request.args.get("lat"))
        longitude = float(request.args.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"recommended": [], "nearby": []})
    if not LITE_ENABLED or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return jsonify({"recommended": [], "nearby": []})

    checkin = date.today().isoformat()
    checkout = (date.today() + timedelta(days=1)).isoformat()
    nights = 1
    try:
        base_cards = _priced_coordinate_cards(
            latitude=latitude,
            longitude=longitude,
            checkin=checkin,
            checkout=checkout,
            nights=nights,
            adults=2,
            rooms=1,
            radius=25000,
            limit=50,
        )
    except LiteAPIError as exc:
        print("FLIGHT LOCAL STAYS ERROR:", exc)
        return jsonify({"recommended": [], "nearby": []})

    # Keep exact user distance outside the rounded-coordinate rate cache.
    cards = [dict(card) for card in base_cards]
    for card in cards:
        try:
            card["distance_miles"] = round(
                _haversine_miles(latitude, longitude, float(card["latitude"]), float(card["longitude"])), 1
            )
        except (TypeError, ValueError):
            card["distance_miles"] = None
        card["url"] = url_for(
            "hotel_detail", hotel_id=card["hotel_id"], checkin=checkin, checkout=checkout,
            adults=2, rooms=1,
        )

    def recommended_rank(card: Mapping[str, Any]) -> tuple[float, float, float]:
        rating = float(card.get("rating") or 0)
        reviews = float(card.get("review_count") or 0)
        weighted_rating = (rating * reviews + 8.0 * 200) / (reviews + 200)
        distance = float(card.get("distance_miles") or 9999)
        return (-weighted_rating, distance, float(card["offer"]["nightly_amount"] or 0))

    recommended = sorted(cards, key=recommended_rank)[:4]
    selected_ids = {card["hotel_id"] for card in recommended}
    nearby = [card for card in cards if card["hotel_id"] not in selected_ids]
    nearby.sort(key=lambda card: (card.get("distance_miles") is None, card.get("distance_miles") or 0))

    return jsonify({
        "nights": nights,
        "checkin": checkin,
        "checkout": checkout,
        "price_display": "nightly",
        "recommended": recommended,
        "nearby": nearby[:4],
    })


@app.route("/api/hotels/places", methods=["GET"])
def hotel_places():
    """Destination autocomplete for the hotel search bar."""
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify([])
    try:
        places = LITE.search_places(query)
    except LiteAPIError as exc:
        print("HOTEL PLACES ERROR:", exc)
        return jsonify([])
    return jsonify(places[:8])


def _resolve_hotel_place_for_airport(iata_code: str) -> dict[str, str] | None:
    """City-level LiteAPI place for a flight destination's IATA code.

    The "airport code -> city name -> LiteAPI place" resolution the
    sequential flight -> hotel handoff needs to anchor a hotel search on
    whichever flight the user just picked.
    """
    code = _normalize_airport_input(iata_code)
    if not code:
        return None
    airport = _airport_code_map().get(code) or {}
    destination_name = str(airport.get("city") or "").strip()
    # Some airport source rows carry a municipality qualifier, e.g.
    # "Paris (Roissy-en-France, Val-d'Oise)" — a poor heading for a city search.
    destination_name = destination_name.split(" (", 1)[0].strip()
    if not destination_name:
        return None
    try:
        places = LITE.search_places(destination_name)
    except LiteAPIError as exc:
        print("HOTEL PLACE RESOLVE ERROR:", exc)
        return None
    if not places:
        return None
    place = places[0]
    return {"place_id": str(place.get("place_id") or ""), "name": str(place.get("name") or destination_name)}


def _run_hotel_search(
    *,
    place_id: str,
    place_name: str,
    checkin: str,
    checkout: str,
    adults: int,
    rooms: int,
    children_ages: Sequence[int] | None = None,
    ai_filters: Mapping[str, Any] | None = None,
    ai_text: str = "",
):
    """Shared by the manual form and the AI entry point."""
    nights = _hotel_nights(checkin, checkout)
    if not place_id or nights <= 0:
        _set_global_notice("Pick a destination and your check-in and check-out dates.")
        return redirect(url_for("hotels"))

    # A full city can easily contain 150+ properties.  The rate provider only
    # accepts 50 IDs per request, so pricing every candidate before rendering
    # makes the first result page wait for three upstream batches.  Render one
    # live-priced batch immediately; the results page requests later batches
    # only if the traveller asks to see more.
    initial_batch_size = 50
    started = time.time()
    try:
        content = LITE.hotels_for_place(place_id)
        valid_content = [row for row in content if str(row.get("id") or "").strip()]
        initial_content = valid_content[:initial_batch_size]
        content_by_id = {str(row.get("id") or "").strip(): row for row in initial_content}
        rate_rows = LITE.search_rates(
            hotel_ids=list(content_by_id.keys()),
            checkin=checkin,
            checkout=checkout,
            adults=adults,
            children_ages=children_ages,
            rooms=rooms,
        )
        cards = build_hotel_cards(
            rate_rows, content_by_id, nights=nights,
            facility_names=LITE.facility_names(),
        )
    except LiteAPIError as exc:
        print("HOTEL SEARCH ERROR:", exc)
        _set_global_notice(str(exc))
        return redirect(url_for("hotels"))

    prices = [card["offer"]["total_amount"] for card in cards]
    currency = cards[0]["offer"]["currency"] if cards else LITE_DEFAULT_CURRENCY

    _track_analytics_event(
        event_type="search_completed",
        search_mode="hotels_ai" if ai_text else "hotels",
        destination=place_name or place_id,
        result_count=len(cards),
        success=bool(cards),
        currency=currency,
        metadata={
            "page": "hotels",
            "nights": nights,
            "adults": adults,
            "rooms": rooms,
            "properties_priced": len(content_by_id),
            "properties_available": len(valid_content),
            "elapsed_ms": int((time.time() - started) * 1000),
            "ai": bool(ai_text),
        },
    )

    return render_template(
        "hotel_results.html",
        cards=cards,
        search={
            "place_id": place_id,
            "place_name": place_name,
            "checkin": checkin,
            "checkout": checkout,
            "adults": adults,
            "rooms": rooms,
            "nights": nights,
            "children_ages": list(children_ages or []),
        },
        facets={
            "min_price": int(min(prices)) if prices else 0,
            "max_price": int(max(prices)) + 1 if prices else 0,
            "currency": currency,
            "amenities": HOTEL_AMENITY_FILTERS,
        },
        ai_filters=ai_filters or {},
        ai_text=ai_text,
        searched_count=len(content_by_id),
        has_more=len(valid_content) > len(initial_content),
        next_offset=len(initial_content),
        lite_env=LITE_ENV,
        global_notice=_pop_global_notice(),
    )


@app.route("/trip/select-flight/<offer_id>", methods=["GET"])
def trip_select_flight(offer_id: str):
    """Picking a flight when a hotel is also wanted doesn't go straight to
    checkout — it hands off into a destination/date-prefiltered hotel search
    first. Falls straight through to ordinary checkout when there's no
    pending hotel intent, hotels are unavailable, or the offer can't be
    read — this handoff is additive sugar, never a blocker on booking a
    flight alone."""
    trip_intent = _get_trip_intent()
    if not trip_intent or not LITE_ENABLED:
        return redirect(url_for("checkout_offer", offer_id=offer_id))

    try:
        offer = DUFF.get_offer(offer_id)
    except DuffelAPIError:
        return redirect(url_for("checkout_offer", offer_id=offer_id))
    if offer_has_expired(offer):
        return redirect(url_for("checkout_offer", offer_id=offer_id))

    slices = offer.get("slices") or []
    outbound_segments = slices[0].get("segments") if slices else None
    if not outbound_segments:
        return redirect(url_for("checkout_offer", offer_id=offer_id))
    destination_iata = str((outbound_segments[-1].get("destination") or {}).get("iata_code") or "").strip().upper()

    offer_summary = build_checkout_summary(offer, seat_maps=[], ancillaries_payload={})
    flight_snapshot = {
        "route": offer_summary.get("route_summary") or "",
        "total_amount": offer_summary.get("total_amount"),
        "currency": offer_summary.get("currency"),
        "airline_name": offer_summary.get("airline_name"),
    }

    # Re-anchor hotel dates to this flight's real dates when the user never
    # gave the stay its own explicit dates — the parse-time guess (flex
    # search especially) is often just a placeholder.
    hotel_dates = dict(trip_intent.get("hotel_dates") or {})
    if not trip_intent.get("stay_dates_explicit") and destination_iata:
        checkin_dt = parse_duffel_datetime(outbound_segments[-1].get("arriving_at"))
        checkin = checkin_dt.date().isoformat() if checkin_dt else ""
        checkout = ""
        if len(slices) >= 2:
            return_segments = slices[1].get("segments") or []
            if return_segments:
                checkout_dt = parse_duffel_datetime(return_segments[0].get("departing_at"))
                checkout = checkout_dt.date().isoformat() if checkout_dt else ""
        if checkin:
            hotel_dates["checkin"] = checkin
            if checkout and checkout > checkin:
                hotel_dates["checkout"] = checkout
            else:
                # One-way flight, or a same/odd-day return — fall back to
                # whichever nights count the original stay parse implied.
                span = 3
                original_dates = trip_intent.get("hotel_dates") or {}
                prior_checkin = original_dates.get("checkin")
                prior_checkout = original_dates.get("checkout")
                if prior_checkin and prior_checkout:
                    try:
                        span = max(1, (datetime.strptime(prior_checkout, "%Y-%m-%d").date()
                                        - datetime.strptime(prior_checkin, "%Y-%m-%d").date()).days)
                    except ValueError:
                        span = 3
                hotel_dates["checkout"] = (datetime.strptime(checkin, "%Y-%m-%d").date() + timedelta(days=span)).isoformat()

    updates: dict[str, Any] = {
        "flight_offer_id": offer_id,
        "flight_snapshot": flight_snapshot,
        "hotel_dates": hotel_dates,
        "stage": "flight_selected",
    }
    # A different destination than whatever hotel search (if any) was
    # already underway invalidates the in-progress hotel pick.
    if trip_intent.get("destination_iata") and trip_intent["destination_iata"] != destination_iata:
        updates.update({
            "hotel_place_id": None,
            "hotel_id": None,
            "hotel_offer_id": None,
            "hotel_prebook_id": None,
            "hotel_checkin": None,
            "hotel_checkout": None,
            "hotel_snapshot": None,
            "stage": "flight_selected",
        })
    updates["destination_iata"] = destination_iata
    _update_trip_intent(**updates)

    _track_offer_funnel_event(event_type="flight_selected", offer=offer, step="trip_combo_flight")
    return redirect(url_for("trip_hotel_search"))


@app.route("/trip/hotel-search", methods=["GET"])
def trip_hotel_search():
    """Hotel results pre-filtered to the flight just picked — the second
    step of a combined "flight + hotel" AI search."""
    trip_intent = _get_trip_intent()
    if not trip_intent or not LITE_ENABLED:
        return redirect(url_for("index"))

    destination_iata = str(trip_intent.get("destination_iata") or "")
    place = _resolve_hotel_place_for_airport(destination_iata) if destination_iata else None
    if not place:
        _set_global_notice(
            "We couldn't find stays for that destination — pick a hotel destination manually below."
        )
        return redirect(url_for("hotels"))

    hotel_dates = trip_intent.get("hotel_dates") or {}
    filters = trip_intent.get("hotel_filters") or {}
    _update_trip_intent(hotel_place_id=place["place_id"], stage="hotel_searching")

    return _run_hotel_search(
        place_id=place["place_id"],
        place_name=place["name"],
        checkin=str(hotel_dates.get("checkin") or ""),
        checkout=str(hotel_dates.get("checkout") or ""),
        adults=int(filters.get("adults") or 2),
        rooms=int(filters.get("rooms") or 1),
        children_ages=filters.get("children_ages") or [],
        ai_filters=filters,
        ai_text=str(trip_intent.get("raw_text") or ""),
    )


@app.route("/trip/skip-hotel", methods=["GET"])
def trip_skip_hotel():
    """Opt out of the pending hotel leg — "just book the flight." Never a
    dead end: whichever flight the user picks next goes straight to
    ordinary checkout instead of the hotel handoff."""
    _clear_trip_intent()
    next_url = _safe_next_url(request.args.get("next"))
    return redirect(next_url or url_for("index"))


@app.route("/api/hotels/search-more", methods=["GET"])
def hotel_search_more():
    """Return the next live-priced 50-property hotel batch for an open search."""
    place_id = (request.args.get("place_id") or "").strip()
    place_name = (request.args.get("place_name") or "").strip()
    checkin = (request.args.get("checkin") or "").strip()
    checkout = (request.args.get("checkout") or "").strip()
    nights = _hotel_nights(checkin, checkout)
    if not LITE_ENABLED or not place_id or nights <= 0:
        return jsonify({"html": "", "has_more": False, "next_offset": 0}), 400

    try:
        adults = max(1, min(8, int(request.args.get("adults") or 2)))
    except ValueError:
        adults = 2
    try:
        rooms = max(1, min(4, int(request.args.get("rooms") or 1)))
    except ValueError:
        rooms = 1
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        offset = 0
    # Align requests to the provider's safe 50-ID batch size.
    batch_size = 50
    offset = (offset // batch_size) * batch_size
    children_ages = []
    for value in (request.args.get("children_ages") or "").split(","):
        try:
            children_ages.append(max(0, min(17, int(value))))
        except (TypeError, ValueError):
            continue

    try:
        content = LITE.hotels_for_place(place_id)
        valid_content = [row for row in content if str(row.get("id") or "").strip()]
        batch = valid_content[offset:offset + batch_size]
        by_id = {str(row.get("id") or "").strip(): row for row in batch}
        rate_rows = LITE.search_rates(
            hotel_ids=list(by_id.keys()), checkin=checkin, checkout=checkout,
            adults=adults, children_ages=children_ages, rooms=rooms,
        )
        cards = build_hotel_cards(
            rate_rows, by_id, nights=nights, facility_names=LITE.facility_names(),
        )
    except LiteAPIError as exc:
        print("HOTEL SEARCH MORE ERROR:", exc)
        return jsonify({"html": "", "has_more": False, "next_offset": offset}), 502

    next_offset = offset + len(batch)
    search = {
        "place_id": place_id,
        "place_name": place_name,
        "checkin": checkin,
        "checkout": checkout,
        "adults": adults,
        "rooms": rooms,
        "nights": nights,
    }
    return jsonify({
        "html": render_template("partials/hotel_result_cards.html", cards=cards, search=search, card_offset=offset),
        "has_more": next_offset < len(valid_content),
        "next_offset": next_offset,
    })


@app.route("/hotels/search", methods=["GET", "POST"])
def hotel_search():
    source = request.form if request.method == "POST" else request.args

    try:
        adults = max(1, min(8, int(source.get("adults") or 2)))
    except ValueError:
        adults = 2
    try:
        rooms = max(1, min(4, int(source.get("rooms") or 1)))
    except ValueError:
        rooms = 1

    return _run_hotel_search(
        place_id=(source.get("place_id") or "").strip(),
        place_name=(source.get("place_name") or "").strip(),
        checkin=(source.get("checkin") or "").strip(),
        checkout=(source.get("checkout") or "").strip(),
        adults=adults,
        rooms=rooms,
    )


@app.route("/hotels/ai-search", methods=["GET", "POST"])
def hotel_ai_search():
    """Natural-language stay search: parse -> resolve destination -> results."""
    source = request.form if request.method == "POST" else request.args
    ai_text = (source.get("q") or source.get("ai_text") or "").strip()
    if not ai_text:
        return redirect(url_for("hotels"))

    parsed = parse_ai_stay_request(ai_text)
    if not parsed:
        _set_global_notice("We couldn't read that stay request. Try naming a city and your dates.")
        return redirect(url_for("hotels"))

    try:
        places = LITE.search_places(parsed["destination"])
    except LiteAPIError as exc:
        print("HOTEL AI PLACE ERROR:", exc)
        places = []
    if not places:
        _set_global_notice(f"We couldn't find stays in \"{parsed['destination']}\".")
        return redirect(url_for("hotels"))

    return _run_hotel_search(
        place_id=places[0]["place_id"],
        place_name=places[0]["name"],
        checkin=parsed["checkin"],
        checkout=parsed["checkout"],
        adults=parsed["adults"],
        rooms=parsed["rooms"],
        children_ages=parsed["children_ages"],
        ai_filters={
            "min_stars": parsed["min_stars"],
            "min_rating": parsed["min_rating"],
            "max_price_per_night": parsed["max_price_per_night"],
            "free_cancellation": parsed["free_cancellation"],
            "breakfast": parsed["breakfast"],
            "amenities": parsed["amenities"],
            "sort": parsed["sort"],
        },
        ai_text=ai_text,
    )


@app.route("/hotels/<hotel_id>", methods=["GET"])
def hotel_detail(hotel_id: str):
    checkin = (request.args.get("checkin") or "").strip()
    checkout = (request.args.get("checkout") or "").strip()
    try:
        adults = max(1, min(8, int(request.args.get("adults") or 2)))
    except ValueError:
        adults = 2
    try:
        rooms = max(1, min(4, int(request.args.get("rooms") or 1)))
    except ValueError:
        rooms = 1

    nights = _hotel_nights(checkin, checkout)
    if nights <= 0:
        _set_global_notice("Pick your check-in and check-out dates to see room rates.")
        return redirect(url_for("hotels"))

    try:
        content = LITE.hotel_detail(hotel_id)
        rate_rows = LITE.search_rates(
            hotel_ids=[hotel_id],
            checkin=checkin,
            checkout=checkout,
            adults=adults,
            rooms=rooms,
        )
    except LiteAPIError as exc:
        print("HOTEL DETAIL ERROR:", exc)
        _set_global_notice(str(exc))
        return redirect(url_for("hotels"))

    rooms_view = build_rooms_view(rate_rows[0], nights=nights) if rate_rows else []
    cheapest = min((r["total_amount"] for r in rooms_view), default=None)

    return render_template(
        "hotel_detail.html",
        hotel=build_detail_view(content),
        hotel_description=sanitize_description(content.get("hotelDescription")),
        rooms=rooms_view,
        cheapest_total=cheapest,
        search={
            "place_id": (request.args.get("place_id") or "").strip(),
            "place_name": (request.args.get("place_name") or "").strip(),
            "checkin": checkin,
            "checkout": checkout,
            "adults": adults,
            "rooms": rooms,
            "nights": nights,
        },
        lite_env=LITE_ENV,
        global_notice=_pop_global_notice(),
    )


def _hotel_checkout_context(*, request_source: Mapping[str, Any]) -> dict[str, Any]:
    """Pull the room/date/party shape a hotel checkout page needs out of
    whatever combination of query string (GET) or form (POST) carried it."""
    try:
        adults = max(1, min(8, int(request_source.get("adults") or 2)))
    except (TypeError, ValueError):
        adults = 2
    try:
        rooms_count = max(1, min(4, int(request_source.get("rooms") or 1)))
    except (TypeError, ValueError):
        rooms_count = 1
    return {
        "offer_id": str(request_source.get("offer_id") or "").strip(),
        "checkin": str(request_source.get("checkin") or "").strip(),
        "checkout": str(request_source.get("checkout") or "").strip(),
        "place_id": str(request_source.get("place_id") or "").strip(),
        "place_name": str(request_source.get("place_name") or "").strip(),
        "adults": adults,
        "rooms": rooms_count,
    }


@app.route("/hotels/<hotel_id>/checkout", methods=["GET", "POST"])
def hotel_checkout(hotel_id: str):
    """Real hotel checkout: prebook (price lock) -> holder/guest form -> book.

    Gated by the same demo safeguard as flight checkout (_demo_checkout_lock_error)
    — browsable end-to-end, but the live public demo doesn't create real
    LiteAPI bookings any more than it creates real Duffel orders.
    """
    if not LITE_ENABLED:
        return redirect(url_for("hotels"))

    ctx = _hotel_checkout_context(request_source=(request.form if request.method == "POST" else request.args))
    nights = _hotel_nights(ctx["checkin"], ctx["checkout"])
    if not ctx["offer_id"] or nights <= 0:
        _set_global_notice("Pick your dates and a room before checkout.")
        return redirect(url_for(
            "hotel_detail", hotel_id=hotel_id, checkin=ctx["checkin"], checkout=ctx["checkout"],
            adults=ctx["adults"], rooms=ctx["rooms"], place_id=ctx["place_id"], place_name=ctx["place_name"],
        ))

    try:
        content = LITE.hotel_detail(hotel_id)
    except LiteAPIError as exc:
        print("HOTEL CHECKOUT CONTENT ERROR:", exc)
        _set_global_notice(str(exc))
        return redirect(url_for("hotel_detail", hotel_id=hotel_id, checkin=ctx["checkin"], checkout=ctx["checkout"]))
    hotel_view = build_detail_view(content)

    def render_checkout(*, prebook_summary, traveler_form, errors, booking_error, booking_enabled, status=200):
        return render_template(
            "hotel_checkout.html",
            hotel=hotel_view,
            prebook=prebook_summary,
            traveler_form=traveler_form,
            errors=errors,
            booking_error=booking_error,
            booking_enabled=booking_enabled,
            offer_id=ctx["offer_id"], hotel_id=hotel_id,
            checkin=ctx["checkin"], checkout=ctx["checkout"],
            adults=ctx["adults"], rooms=ctx["rooms"],
            place_id=ctx["place_id"], place_name=ctx["place_name"],
            csrf_token=_b2c_csrf_token(),
            lite_env=LITE_ENV,
        ), status

    mode_error = _demo_checkout_lock_error()
    if mode_error:
        return render_checkout(
            prebook_summary=None,
            traveler_form=build_hotel_traveler_form(room_count=ctx["rooms"]),
            errors={}, booking_error=mode_error, booking_enabled=False, status=503,
        )

    prebook_cache_key = ("hotel_prebook", ctx["offer_id"])
    prebook = HOTEL_PREBOOK_CACHE.get(prebook_cache_key)
    if not prebook:
        try:
            prebook = LITE.prebook(ctx["offer_id"])
        except LiteAPIError as exc:
            return render_checkout(
                prebook_summary=None,
                traveler_form=build_hotel_traveler_form(room_count=ctx["rooms"]),
                errors={}, booking_error=str(exc), booking_enabled=False, status=502,
            )
        HOTEL_PREBOOK_CACHE.set(prebook_cache_key, prebook)
    prebook_summary = build_prebook_summary(prebook, nights=nights)

    # A flight is already picked for this trip — hand off into the combined
    # checkout instead of a standalone hotel booking. GET only: the combined
    # form posts straight to /trip/checkout, so a bare POST here (bypassing
    # that form entirely) still completes as an ordinary hotel-only booking.
    if request.method == "GET":
        trip_intent = _get_trip_intent()
        if trip_intent and trip_intent.get("flight_offer_id"):
            _update_trip_intent(
                hotel_id=hotel_id,
                hotel_offer_id=ctx["offer_id"],
                hotel_prebook_id=str(prebook.get("prebookId") or ""),
                hotel_checkin=ctx["checkin"],
                hotel_checkout=ctx["checkout"],
                hotel_rooms=ctx["rooms"],
                hotel_adults=ctx["adults"],
                hotel_snapshot={
                    "hotel_name": hotel_view.get("name"),
                    "room_name": prebook_summary.get("room_name"),
                    "total_amount": prebook_summary.get("total_amount"),
                    "currency": prebook_summary.get("currency"),
                },
                stage="hotel_selected",
            )
            return redirect(url_for("trip_checkout"))

    if request.method == "GET":
        _track_analytics_event(
            event_type="hotel_booking_intent", search_mode="hotels", success=True,
            metadata={"hotel_id": hotel_id, "nights": nights},
        )
        return render_checkout(
            prebook_summary=prebook_summary,
            traveler_form=build_hotel_traveler_form(room_count=ctx["rooms"]),
            errors={}, booking_error="", booking_enabled=True,
        )

    # POST — actually book it.
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403

    holder_payload, guests_payload, errors = validate_hotel_checkout_form(room_count=ctx["rooms"], form=request.form)
    traveler_form = build_hotel_traveler_form(room_count=ctx["rooms"], form=request.form)
    if errors:
        return render_checkout(
            prebook_summary=prebook_summary, traveler_form=traveler_form,
            errors=errors, booking_error="", booking_enabled=True,
        )

    # Re-check the price hasn't drifted since the page was first shown —
    # never trust a stale prebook when actually charging.
    try:
        fresh_prebook = LITE.prebook(ctx["offer_id"])
    except LiteAPIError as exc:
        return render_checkout(
            prebook_summary=prebook_summary, traveler_form=traveler_form,
            errors={}, booking_error=str(exc), booking_enabled=False, status=502,
        )
    HOTEL_PREBOOK_CACHE.set(prebook_cache_key, fresh_prebook)
    fresh_summary = build_prebook_summary(fresh_prebook, nights=nights)
    if fresh_summary.get("price_changed"):
        return render_checkout(
            prebook_summary=fresh_summary, traveler_form=traveler_form,
            errors={},
            booking_error=(
                f"The price for this room changed to {fresh_summary['currency']} "
                f"{fresh_summary['total_amount']:.2f} since you started checkout. "
                "Review the new total and submit again to confirm."
            ),
            booking_enabled=True,
        )

    client_reference = secrets.token_urlsafe(16)
    try:
        booking = LITE.book(
            prebook_id=str(fresh_prebook.get("prebookId") or ""),
            holder=holder_payload,
            guests=guests_payload,
            payment={"method": "ACC_CREDIT_CARD"},
            client_reference=client_reference,
        )
    except LiteAPIError as exc:
        return render_checkout(
            prebook_summary=fresh_summary, traveler_form=traveler_form,
            errors={}, booking_error=str(exc), booking_enabled=True, status=502,
        )

    booking_reference = _generate_hotel_booking_reference()
    _save_hotel_booking({
        "booking_reference": booking_reference,
        "liteapi_booking_id": str(booking.get("bookingId") or ""),
        "liteapi_prebook_id": str(fresh_prebook.get("prebookId") or ""),
        "hotel_id": hotel_id,
        "hotel_name": hotel_view.get("name"),
        "hotel_address": hotel_view.get("address"),
        "hotel_photo": hotel_view.get("hero"),
        "room_name": fresh_summary.get("room_name"),
        "board_name": fresh_summary.get("board_name"),
        "checkin": ctx["checkin"],
        "checkout": ctx["checkout"],
        "holder_first_name": holder_payload["firstName"],
        "holder_last_name": holder_payload["lastName"],
        "holder_email": holder_payload["email"],
        "total_amount": f"{fresh_summary.get('total_amount') or 0:.2f}",
        "currency": fresh_summary.get("currency"),
        "status": "confirmed",
    })
    _session_authorize_order(f"hotel:{booking_reference}")
    _record_booking_email_link(email=holder_payload["email"], booking_reference=booking_reference, order_id=str(booking.get("bookingId") or ""))
    _link_booking_to_account(holder_payload["email"], booking_reference)
    _track_analytics_event(
        event_type="hotel_booking_completed", search_mode="hotels", success=True,
        currency=fresh_summary.get("currency"),
        metadata={"hotel_id": hotel_id, "booking_reference": booking_reference, "nights": nights},
    )
    try:
        ok, reason = email_service.send_hotel_confirmation_email(
            to_email=holder_payload["email"],
            booking_summary={
                "booking_reference": booking_reference,
                "hotel_name": hotel_view.get("name"),
                "hotel_address": hotel_view.get("address"),
                "hotel_photo": hotel_view.get("hero"),
                "room_name": fresh_summary.get("room_name"),
                "board_name": fresh_summary.get("board_name"),
                "checkin": ctx["checkin"],
                "checkout": ctx["checkout"],
                "nights": nights,
                "guest_name": f"{holder_payload['firstName']} {holder_payload['lastName']}".strip(),
                "total_amount": fresh_summary.get("total_amount"),
                "currency": fresh_summary.get("currency"),
            },
        )
        if not ok:
            print(f"HOTEL CONFIRMATION EMAIL FAILED for {holder_payload['email']}: {reason}")
    except Exception as exc:
        print("HOTEL CONFIRMATION EMAIL ERROR:", repr(exc))

    return redirect(url_for("hotel_booking_confirmation", booking_reference=booking_reference))


@app.route("/hotels/booking/confirmation/<booking_reference>", methods=["GET"])
def hotel_booking_confirmation(booking_reference: str):
    """Post-booking confirmation, and also the manage-booking display target
    for hotel bookings looked up by reference (see manage_booking())."""
    booking = _hotel_booking_by_reference(booking_reference)
    if not booking:
        return redirect(url_for("hotels"))
    return render_template(
        "hotel_confirmation.html",
        booking=booking,
        lite_env=LITE_ENV,
    )


@app.route("/trip/checkout", methods=["GET", "POST"])
def trip_checkout():
    """Combined flight + hotel checkout: one traveler/holder form, two
    sequential provider calls (Duffel then LiteAPI), one confirmation.

    Never renders with a hole in it — bounces back to whichever step is
    incomplete rather than guessing. Demo-locked the same way both
    single-product checkouts are (see _demo_checkout_lock_error).
    """
    trip_intent = _get_trip_intent()
    if not trip_intent or not trip_intent.get("flight_offer_id"):
        return redirect(url_for("index"))
    if not LITE_ENABLED:
        # Hotels went unavailable mid-session (e.g. config reload) — never
        # trust a stale session flag. Degrade to an ordinary flight checkout
        # rather than dead-ending on a combined page that can't render.
        _update_trip_intent(hotel_id=None, hotel_offer_id=None, hotel_prebook_id=None, hotel_snapshot=None, stage="flight_selected")
        _set_global_notice("Hotels aren't available right now — continuing with your flight only.")
        return redirect(url_for("checkout_offer", offer_id=trip_intent["flight_offer_id"]))
    if not trip_intent.get("hotel_id") or not trip_intent.get("hotel_offer_id"):
        return redirect(url_for("trip_hotel_search"))

    flight_offer_id = trip_intent["flight_offer_id"]
    hotel_id = trip_intent["hotel_id"]
    hotel_offer_id = trip_intent["hotel_offer_id"]
    hotel_checkin = str(trip_intent.get("hotel_checkin") or "")
    hotel_checkout_date = str(trip_intent.get("hotel_checkout") or "")
    hotel_rooms = int(trip_intent.get("hotel_rooms") or 1)
    hotel_filters = trip_intent.get("hotel_filters") or {}
    nights = _hotel_nights(hotel_checkin, hotel_checkout_date)

    if nights <= 0:
        # trip_intent got into an inconsistent state — safest recovery is a fresh hotel pick.
        _update_trip_intent(hotel_id=None, hotel_offer_id=None, hotel_prebook_id=None, hotel_snapshot=None, stage="flight_selected")
        return redirect(url_for("trip_hotel_search"))

    def render_trip_checkout(
        *, offer_summary=None, checkout_model=None, travelers=None,
        hotel_view=None, prebook_summary=None, hotel_traveler_form=None,
        errors=None, booking_error="", booking_enabled=False, status=200,
    ):
        combined_total = None
        if offer_summary and prebook_summary and offer_summary.get("currency") == prebook_summary.get("currency"):
            try:
                combined_total = float(offer_summary["total_amount"]) + float(prebook_summary["total_amount"])
            except (TypeError, ValueError):
                combined_total = None
        return render_template(
            "trip_checkout.html",
            offer_summary=offer_summary,
            checkout_model=checkout_model,
            travelers=travelers or [],
            hotel=hotel_view,
            prebook=prebook_summary,
            hotel_traveler_form=hotel_traveler_form or build_hotel_traveler_form(room_count=hotel_rooms),
            errors=errors or {},
            booking_error=booking_error,
            booking_enabled=booking_enabled,
            combined_total=combined_total,
            combined_currency=(offer_summary or {}).get("currency") if combined_total is not None else None,
            checkout_token=trip_intent.get("checkout_token") or "",
            hotel_id=hotel_id,
            hotel_checkin=hotel_checkin,
            hotel_checkout=hotel_checkout_date,
            hotel_rooms=hotel_rooms,
            duffel_env=DUFFEL_ENV,
            duffel_components_version=DUFFEL_COMPONENTS_VERSION,
            lite_env=LITE_ENV,
            csrf_token=_b2c_csrf_token(),
        ), status

    mode_error = _demo_checkout_lock_error() or _booking_mode_error()
    if mode_error:
        return render_trip_checkout(booking_error=mode_error, booking_enabled=False, status=503)

    try:
        offer = DUFF.get_offer(flight_offer_id, return_available_services=True)
    except DuffelAPIError as exc:
        return render_trip_checkout(booking_error=str(exc), booking_enabled=False, status=_booking_status_code(exc.status_code))

    if offer_has_expired(offer):
        # F3 — the hotel pick is untouched; only the flight needs re-picking.
        _update_trip_intent(flight_offer_id=None, flight_snapshot=None, stage="hotel_selected")
        _set_global_notice(
            "Your flight offer expired while you were choosing a hotel. Your hotel pick is still saved — "
            "search again to pick a fresh flight and you'll come straight back here."
        )
        return redirect(url_for("index"))

    hotel_cache_key = ("hotel_prebook", hotel_offer_id)
    prebook = HOTEL_PREBOOK_CACHE.get(hotel_cache_key)
    if not prebook:
        try:
            prebook = LITE.prebook(hotel_offer_id)
        except LiteAPIError as exc:
            return render_trip_checkout(booking_error=f"Hotel: {exc}", booking_enabled=False, status=502)
        HOTEL_PREBOOK_CACHE.set(hotel_cache_key, prebook)
    prebook_summary = build_prebook_summary(prebook, nights=nights)

    try:
        hotel_content = LITE.hotel_detail(hotel_id)
    except LiteAPIError as exc:
        return render_trip_checkout(booking_error=f"Hotel: {exc}", booking_enabled=False, status=502)
    hotel_view = build_detail_view(hotel_content)

    seat_maps, payment_config = _load_checkout_sidecars(offer)
    travelers = build_traveler_forms(offer, request.form if request.method == "POST" else None)
    offer_summary = build_checkout_summary(offer, seat_maps=seat_maps, ancillaries_payload={})
    checkout_model = build_checkout_page_model(
        offer, travelers=travelers, seat_maps=seat_maps, ancillaries_payload={},
        payment_config=payment_config, duffel_env=DUFFEL_ENV,
    )

    if request.method == "GET":
        checkout_token = secrets.token_urlsafe(16)
        _update_trip_intent(checkout_token=checkout_token, stage="checkout")
        trip_intent["checkout_token"] = checkout_token
        _track_offer_funnel_event(event_type="booking_intent", offer=offer, step="trip_combo_checkout")
        return render_trip_checkout(
            offer_summary=offer_summary, checkout_model=checkout_model, travelers=travelers,
            hotel_view=hotel_view, prebook_summary=prebook_summary, booking_enabled=True,
        )

    # POST — actually book both.
    if not _validate_b2c_csrf():
        return "Invalid or missing CSRF token.", 403

    submitted_token = str(request.form.get("checkout_token") or "").strip()
    stored_token = str(trip_intent.get("checkout_token") or "").strip()
    if not submitted_token or submitted_token != stored_token:
        # Either a resubmission of an already-processed form, or a stale
        # session — never silently re-run two provider charges.
        if trip_intent.get("stage") == "done" and trip_intent.get("flight_order_id") and trip_intent.get("hotel_booking_reference"):
            return redirect(url_for(
                "trip_confirmation",
                flight_order_id=trip_intent["flight_order_id"],
                hotel_booking_reference=trip_intent["hotel_booking_reference"],
            ))
        return render_trip_checkout(
            offer_summary=offer_summary, checkout_model=checkout_model, travelers=travelers,
            hotel_view=hotel_view, prebook_summary=prebook_summary, booking_enabled=False,
            booking_error="This checkout session expired or was already submitted. Please review your trip and try again.",
            status=409,
        )
    # Consume the token immediately — a resubmission of this exact request
    # (double-click, browser back+resubmit) now fails the check above
    # instead of firing a second pair of provider calls.
    _update_trip_intent(checkout_token="")

    holder_payload, guests_payload, hotel_errors = validate_hotel_checkout_form(room_count=hotel_rooms, form=request.form)
    passengers_payload, travelers, flight_errors = validate_checkout_form(offer, request.form)
    hotel_traveler_form = build_hotel_traveler_form(room_count=hotel_rooms, form=request.form)
    errors = {**flight_errors, **hotel_errors}
    checkout_model = build_checkout_page_model(
        offer, travelers=travelers, seat_maps=seat_maps, ancillaries_payload={},
        payment_config=payment_config, duffel_env=DUFFEL_ENV,
    )
    if errors:
        return render_trip_checkout(
            offer_summary=offer_summary, checkout_model=checkout_model, travelers=travelers,
            hotel_view=hotel_view, prebook_summary=prebook_summary, hotel_traveler_form=hotel_traveler_form,
            errors=errors, booking_error=errors.get("form", ""), booking_enabled=True, status=400,
        )

    # Re-check the hotel price hasn't drifted right before charging anything.
    try:
        fresh_prebook = LITE.prebook(hotel_offer_id)
    except LiteAPIError as exc:
        return render_trip_checkout(
            offer_summary=offer_summary, checkout_model=checkout_model, travelers=travelers,
            hotel_view=hotel_view, prebook_summary=prebook_summary, hotel_traveler_form=hotel_traveler_form,
            booking_error=f"Hotel: {exc}", booking_enabled=True, status=502,
        )
    HOTEL_PREBOOK_CACHE.set(hotel_cache_key, fresh_prebook)
    fresh_summary = build_prebook_summary(fresh_prebook, nights=nights)
    if fresh_summary.get("price_changed"):
        return render_trip_checkout(
            offer_summary=offer_summary, checkout_model=checkout_model, travelers=travelers,
            hotel_view=hotel_view, prebook_summary=fresh_summary, hotel_traveler_form=hotel_traveler_form,
            booking_error=(
                f"The hotel price changed to {fresh_summary['currency']} {fresh_summary['total_amount']:.2f} "
                "since you started checkout. Review the new total and submit again to confirm."
            ),
            booking_enabled=True, status=409,
        )

    # 1) Flight first — the trip's spine. Nothing is charged on either side yet.
    total_amount = calculate_total_amount(offer, {}, seat_maps=seat_maps)
    total_currency = str(offer_summary.get("currency") or "USD")
    total_amount_str = str(total_amount or offer_summary.get("total_amount") or "0.00")
    payments_payload = None
    if str(payment_config.get("mode") or "").lower() == "card":
        three_d_secure_session_id = str(request.form.get("duffel_three_d_secure_session_id") or "").strip()
        if not three_d_secure_session_id:
            return render_trip_checkout(
                offer_summary=offer_summary, checkout_model=checkout_model, travelers=travelers,
                hotel_view=hotel_view, prebook_summary=fresh_summary, hotel_traveler_form=hotel_traveler_form,
                booking_error="Please enter your card details and complete card authentication before booking.",
                booking_enabled=True, status=400,
            )
        payments_payload = [{
            "type": "card", "currency": total_currency, "amount": total_amount_str,
            "three_d_secure_session_id": three_d_secure_session_id,
        }]

    try:
        order = DUFF.create_order(
            offer_id=(offer.get("id") or flight_offer_id).strip(),
            passengers=passengers_payload,
            total_amount=total_amount_str,
            total_currency=total_currency,
            payments=payments_payload,
        )
    except DuffelAPIError as exc:
        return render_trip_checkout(
            offer_summary=offer_summary, checkout_model=checkout_model, travelers=travelers,
            hotel_view=hotel_view, prebook_summary=fresh_summary, hotel_traveler_form=hotel_traveler_form,
            booking_error=str(exc), booking_enabled=True, status=_booking_status_code(exc.status_code),
        )

    order_id = str(order.get("id") or "").strip()
    if not order_id:
        return render_trip_checkout(
            offer_summary=offer_summary, checkout_model=checkout_model, travelers=travelers,
            hotel_view=hotel_view, prebook_summary=fresh_summary, hotel_traveler_form=hotel_traveler_form,
            booking_error="Something went wrong creating your flight order. Please try again.",
            booking_enabled=True, status=502,
        )

    # The flight is real from here on — everything else is best-effort recording.
    RECENT_ORDER_CACHE.set(order_id, order)
    _session_authorize_order(order_id)
    _capture_booking_email_links(order=order, passengers_payload=passengers_payload)
    _track_booking_completed_event(order, offer=offer)
    _record_agent_booking(order, offer=offer)
    flight_booking_reference = _normalize_booking_reference(str(order.get("booking_reference") or ""))

    # 2) Hotel — if this fails, the flight is already booked and paid for.
    # Never show a generic error here; the user needs to know that.
    client_reference = secrets.token_urlsafe(16)
    hotel_booking = None
    hotel_error = ""
    try:
        hotel_booking = LITE.book(
            prebook_id=str(fresh_prebook.get("prebookId") or ""),
            holder=holder_payload,
            guests=guests_payload,
            payment={"method": "ACC_CREDIT_CARD"},
            client_reference=client_reference,
        )
    except LiteAPIError as exc:
        hotel_error = str(exc)

    if not hotel_booking:
        # Partial success: clear only the flight side of trip_intent so a
        # retry lands on ordinary standalone hotel checkout, not back in
        # here trying (and failing) to re-book an offer that's already an order.
        _update_trip_intent(flight_offer_id=None, flight_order_id=order_id, stage="hotel_selected")
        retry_url = url_for(
            "hotel_checkout", hotel_id=hotel_id, offer_id=hotel_offer_id,
            checkin=hotel_checkin, checkout=hotel_checkout_date,
            adults=int(hotel_filters.get("adults") or 2), rooms=hotel_rooms,
            place_id=trip_intent.get("hotel_place_id") or "",
        )
        return render_template(
            "trip_partial_success.html",
            flight_booking_reference=flight_booking_reference or order_id,
            flight_order_id=order_id,
            hotel_name=hotel_view.get("name"),
            hotel_error=hotel_error,
            retry_url=retry_url,
        ), 200

    hotel_booking_reference = _generate_hotel_booking_reference()
    _save_hotel_booking({
        "booking_reference": hotel_booking_reference,
        "liteapi_booking_id": str(hotel_booking.get("bookingId") or ""),
        "liteapi_prebook_id": str(fresh_prebook.get("prebookId") or ""),
        "hotel_id": hotel_id,
        "hotel_name": hotel_view.get("name"),
        "hotel_address": hotel_view.get("address"),
        "hotel_photo": hotel_view.get("hero"),
        "room_name": fresh_summary.get("room_name"),
        "board_name": fresh_summary.get("board_name"),
        "checkin": hotel_checkin,
        "checkout": hotel_checkout_date,
        "holder_first_name": holder_payload["firstName"],
        "holder_last_name": holder_payload["lastName"],
        "holder_email": holder_payload["email"],
        "total_amount": f"{fresh_summary.get('total_amount') or 0:.2f}",
        "currency": fresh_summary.get("currency"),
        "status": "confirmed",
        "linked_flight_order_id": order_id,
        "linked_flight_booking_reference": flight_booking_reference,
    })
    _session_authorize_order(f"hotel:{hotel_booking_reference}")
    _record_booking_email_link(email=holder_payload["email"], booking_reference=hotel_booking_reference, order_id=str(hotel_booking.get("bookingId") or ""))
    _link_booking_to_account(holder_payload["email"], hotel_booking_reference)
    _track_analytics_event(
        event_type="hotel_booking_completed", search_mode="hotels", success=True,
        currency=fresh_summary.get("currency"),
        metadata={"hotel_id": hotel_id, "booking_reference": hotel_booking_reference, "combined": True},
    )

    try:
        _send_itinerary_emails_after_booking(order=order, passengers_payload=passengers_payload)
    except Exception as exc:
        print(f"ITINERARY EMAIL ERROR: {type(exc).__name__}: {exc}")
    try:
        ok, reason = email_service.send_hotel_confirmation_email(
            to_email=holder_payload["email"],
            booking_summary={
                "booking_reference": hotel_booking_reference,
                "hotel_name": hotel_view.get("name"),
                "hotel_address": hotel_view.get("address"),
                "room_name": fresh_summary.get("room_name"),
                "board_name": fresh_summary.get("board_name"),
                "checkin": hotel_checkin,
                "checkout": hotel_checkout_date,
                "nights": nights,
                "guest_name": f"{holder_payload['firstName']} {holder_payload['lastName']}".strip(),
                "total_amount": fresh_summary.get("total_amount"),
                "currency": fresh_summary.get("currency"),
            },
        )
        if not ok:
            print(f"HOTEL CONFIRMATION EMAIL FAILED for {holder_payload['email']}: {reason}")
    except Exception as exc:
        print("HOTEL CONFIRMATION EMAIL ERROR:", repr(exc))

    _clear_trip_intent()
    return redirect(url_for("trip_confirmation", flight_order_id=order_id, hotel_booking_reference=hotel_booking_reference))


@app.route("/trip/confirmation/<flight_order_id>/<hotel_booking_reference>", methods=["GET"])
def trip_confirmation(flight_order_id: str, hotel_booking_reference: str):
    """Combined confirmation: both bookings, one page."""
    if not _session_is_order_authorized(flight_order_id) or not _session_is_order_authorized(f"hotel:{hotel_booking_reference}"):
        return redirect(url_for("manage_booking"))

    order = RECENT_ORDER_CACHE.get(flight_order_id)
    if order is None:
        try:
            order = DUFF.get_order(flight_order_id)
        except DuffelAPIError as exc:
            return render_template(
                "confirmation.html", order_summary=None, booking_error=str(exc), duffel_env=DUFFEL_ENV,
            ), _booking_status_code(exc.status_code)
        RECENT_ORDER_CACHE.set(flight_order_id, order)

    hotel_booking = _hotel_booking_by_reference(hotel_booking_reference)
    if not hotel_booking:
        return redirect(url_for("booking_confirmation", order_id=flight_order_id))

    order_summary = build_order_summary(order)
    combined_total = None
    combined_currency = None
    try:
        flight_total = float(order_summary.get("total_amount") or 0)
        hotel_total = float(hotel_booking.get("total_amount") or 0)
        if str(order_summary.get("currency") or "") == str(hotel_booking.get("currency") or ""):
            combined_total = flight_total + hotel_total
            combined_currency = order_summary.get("currency")
    except (TypeError, ValueError):
        pass

    return render_template(
        "trip_confirmation.html",
        order_summary=order_summary,
        hotel_booking=hotel_booking,
        combined_total=combined_total,
        combined_currency=combined_currency,
        duffel_env=DUFFEL_ENV,
    )


@app.route("/deals")
def deals():
    _track_analytics_event(
        event_type="coming_soon_viewed",
        search_mode="browse",
        success=True,
        metadata={"page": "deals"},
    )
    return render_template(
        "coming_soon.html",
        feature_name="Deals",
        feature_label="Travel deals",
        feature_description="Smart fare drops, bundle savings, and trip-worthy offers are being prepared.",
    )


@app.route("/destinations/<slug>")
def destination_landing(slug: str):
    destination = get_destination(slug)
    if not destination:
        return redirect(url_for("index"))

    _track_analytics_event(
        event_type="destination_landing_viewed",
        search_mode="browse",
        success=True,
        metadata={"destination": destination["slug"]},
    )

    related = [
        d for d in DESTINATIONS
        if d["slug"] != destination["slug"] and set(d["categories"]) & set(destination["categories"])
    ][:3]
    if len(related) < 3:
        filler = [d for d in DESTINATIONS if d["slug"] != destination["slug"] and d not in related]
        related = (related + filler)[:3]

    return render_template(
        "destination.html",
        destination=destination,
        related=related,
        all_destinations=DESTINATIONS,
    )


def _next_upcoming_weekend(anchor: date) -> tuple[date, date]:
    """
    The next Friday-to-Sunday window from `anchor` (today's own Fri/Sat/Sun
    counts as "this weekend" rather than skipping ahead a full week).
    """
    days_until_friday = (4 - anchor.weekday()) % 7
    friday = anchor + timedelta(days=days_until_friday)
    sunday = friday + timedelta(days=2)
    return friday, sunday


def _destination_price_lookup(origin: str, dest_code: str, depart_date: str, return_date: str) -> dict[str, Any] | None:
    params = {
        "origin": origin,
        "destination": dest_code,
        "depart_date": depart_date,
        "return_date": return_date,
        "trip_type": "roundtrip",
        "passengers": 1,
        "cabin": "ECONOMY",
        "nonstop": False,
    }
    try:
        snapshot = _cheapest_offer_snapshot(params)
    except Exception as exc:
        print("DESTINATION PRICE LOOKUP ERROR:", dest_code, repr(exc))
        return None
    if not snapshot:
        return None
    price = _safe_float(snapshot.get("scan_price_total"))
    if price <= 0:
        return None
    return {"price": round(price), "currency": snapshot.get("scan_currency") or "USD"}


@app.route("/api/nearest-airport", methods=["POST"])
def nearest_airport():
    """Browser geolocation -> nearest commercial airport, for the homepage's
    location-aware 'Popular flights near you' widget. Never guesses a city
    from IP/headers — only real device coordinates the user granted."""
    payload = request.get_json(silent=True) or {}
    try:
        lat = float(payload.get("lat"))
        lng = float(payload.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid coordinates"}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"error": "invalid coordinates"}), 400

    airport = _nearest_airport(lat, lng)
    if not airport:
        return jsonify({"error": "no airport found"}), 404
    return jsonify({
        "code": airport["code"],
        "city": airport["city"],
        "country": airport["country"],
    })


@app.route("/api/destination-prices", methods=["POST"])
def destination_prices():
    """
    Real, live fares for the homepage destination cards, for the next actual
    upcoming Friday-to-Sunday weekend — queried through the same
    flight-search backend (and 15-minute cache) the rest of the app uses.
    Never fabricates a number: a destination with no live result is simply
    omitted from the response so the card can hide its price line. The
    response includes the exact depart/return dates so the UI can show (and
    search) those specific dates rather than a bare price.
    """
    payload = request.get_json(silent=True) or {}
    origin = _normalize_airport_input(str(payload.get("origin") or "").strip()) or "JFK"

    requested_codes = payload.get("destinations") or []
    codes: list[str] = []
    for raw_code in requested_codes:
        code = str(raw_code or "").strip().upper()
        if code and (get_destination_by_code(code) or get_domestic_destination_by_code(code)) and code not in codes:
            codes.append(code)
    codes = codes[:16]
    if not codes:
        return jsonify({"origin": origin, "prices": {}})

    friday, sunday = _next_upcoming_weekend(date.today())
    depart_date = friday.isoformat()
    return_date = sunday.isoformat()

    prices: dict[str, Any] = {}
    workers = min(8, len(codes))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_code = {
            executor.submit(_destination_price_lookup, origin, code, depart_date, return_date): code
            for code in codes
        }
        for future in as_completed(future_to_code):
            code = future_to_code[future]
            try:
                result = future.result()
            except Exception as exc:
                print("DESTINATION PRICE FUTURE ERROR:", code, repr(exc))
                continue
            if result:
                prices[code] = result

    return jsonify({
        "origin": origin,
        "depart_date": depart_date,
        "return_date": return_date,
        "prices": prices,
    })


def _smart_destination_date_candidates(is_domestic: bool) -> list[tuple[date, date]]:
    """
    Three real candidate trips per destination, grounded in published 2026
    fare-timing research rather than one arbitrary shared weekend:

    - Domestic fares bottom out roughly 31-45 days before departure; for
      international the sweet spot is more like 2-3 months (~56-90 days)
      out (Going.com / NerdWallet / Kayak "best time to book" 2026 data).
    - Midweek (Tue/Wed) departures run ~10-20% cheaper than Fri-Sun on
      domestic leisure routes; for international itineraries, Friday
      departures are the statistically cheaper day (Expedia 2026 Air Hacks,
      NerdWallet). This mirrors the weekday bias already used by the
      existing "cheapest week" flex-month search (_weekday_bias below).

    Checking a short weekend, a midweek trip, and a long weekend spread
    across the appropriate booking window lets each destination land on
    whichever shape turns out cheapest for that specific route — verified
    by a real price lookup, never assumed. Two destinations only share
    dates if that's genuinely what the live fares came back as.
    """
    today = date.today()
    offsets = (21, 35, 49) if is_domestic else (49, 70, 91)

    def next_weekday_on_or_after(base: date, weekday: int) -> date:
        return base + timedelta(days=(weekday - base.weekday()) % 7)

    early = today + timedelta(days=offsets[0])
    mid = today + timedelta(days=offsets[1])
    late = today + timedelta(days=offsets[2])

    shapes = [
        (next_weekday_on_or_after(early, 4), 2),  # Fri -> Sun: short weekend
        (next_weekday_on_or_after(mid, 1), 3),    # Tue -> Fri: midweek (cheapest day)
        (next_weekday_on_or_after(late, 4), 3),   # Fri -> Mon: long weekend
    ]
    return [(dep, dep + timedelta(days=nights)) for dep, nights in shapes]


def _best_smart_candidate(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Cheapest of the verified candidates wins. Ties within ~2% (or $5,
    whichever is larger) are broken by the same weekday-cheapness bias the
    flex-month search uses, so real-world noise doesn't arbitrarily pick a
    Sunday over an equally-priced Tuesday."""
    best_price = min(r["price"] for r in results)
    tolerance = max(5.0, best_price * 0.02)
    band = [r for r in results if r["price"] <= best_price + tolerance]
    return max(band, key=lambda r: _weekday_bias(r["depart_date"]))


@app.route("/api/popular-flights", methods=["POST"])
def popular_flights():
    """
    Real live fares for the homepage's 'Popular flights near you' widget.
    Unlike /api/destination-prices (one shared weekend applied to every
    card, kept as-is for the destination landing pages that still use it),
    each destination here gets its own independently-optimized dates: three
    real candidate trips are priced per destination (see
    _smart_destination_date_candidates) across a domestic- or
    international-appropriate booking window, and whichever comes back
    cheapest for that specific route wins. Every (destination x candidate)
    lookup across every destination is flattened into one shared parallel
    batch rather than nesting a thread pool per destination. Never
    fabricates a number or a date pairing: a destination with no live
    result across all three candidates is simply omitted.
    """
    payload = request.get_json(silent=True) or {}
    origin = _normalize_airport_input(str(payload.get("origin") or "").strip()) or "JFK"

    requested_codes = payload.get("destinations") or []
    codes: list[str] = []
    for raw_code in requested_codes:
        code = str(raw_code or "").strip().upper()
        if code and code not in codes and (get_destination_by_code(code) or get_domestic_destination_by_code(code)):
            codes.append(code)
    codes = codes[:16]
    if not codes:
        return jsonify({"origin": origin, "prices": {}})

    tasks: list[tuple[str, date, date]] = []
    for code in codes:
        is_domestic = get_domestic_destination_by_code(code) is not None
        for depart, ret in _smart_destination_date_candidates(is_domestic):
            tasks.append((code, depart, ret))

    by_code: dict[str, list[dict[str, Any]]] = {code: [] for code in codes}
    workers = min(16, len(tasks))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_task = {
            executor.submit(_destination_price_lookup, origin, code, dep.isoformat(), ret.isoformat()): (code, dep, ret)
            for code, dep, ret in tasks
        }
        for future in as_completed(future_to_task):
            code, dep, ret = future_to_task[future]
            try:
                result = future.result()
            except Exception as exc:
                print("POPULAR FLIGHTS FUTURE ERROR:", code, repr(exc))
                continue
            if result:
                by_code[code].append({
                    "price": result["price"],
                    "currency": result["currency"],
                    "depart_date": dep.isoformat(),
                    "return_date": ret.isoformat(),
                })

    prices: dict[str, Any] = {}
    for code, results in by_code.items():
        if results:
            prices[code] = _best_smart_candidate(results)

    return jsonify({"origin": origin, "prices": prices})


@app.route("/results/<token>", methods=["GET"])
def results_reload(token: str):
    reload_token = _coerce_results_reload_token(token)
    payload = RESULTS_RELOAD_CACHE.get(reload_token) if reload_token else None
    if isinstance(payload, Mapping):
        html = str(payload.get("html") or "")
        if html:
            return Response(html, mimetype="text/html")

    return (
        render_template(
            "results.html",
            query={},
            flights=[],
            error="These cached results have expired. Please run the search again to refresh live fares.",
            minutes_to_hm=minutes_to_hm,
            fmt_dt=fmt_dt,
        ),
        410,
    )


@app.after_request
def _redirect_post_search_results_to_reload_url(response: Response):
    if (
        request.endpoint == "search"
        and request.method == "POST"
        and response.status_code == 200
        and response.mimetype == "text/html"
        and not response.is_streamed
    ):
        try:
            html = response.get_data(as_text=True)
        except Exception:
            return response
        if html:
            reload_token = _store_results_reload_html(html, token=_results_reload_token_from_form())
            return redirect(url_for("results_reload", token=reload_token), code=303)
    return response


@app.route("/internal/analytics/popular-routes", methods=["GET"])
def analytics_popular_routes():
    caller_ip = str(request.remote_addr or "").strip()
    if caller_ip not in {"127.0.0.1", "::1"}:
        return jsonify({"error": "Not found"}), 404
    requested_country = str(request.args.get("country") or "").strip().upper()
    if not requested_country:
        requested_country = _analytics_location_context().get("country", "")
    days = max(1, min(365, _safe_int(request.args.get("days"), 90)))
    limit = max(1, min(50, _safe_int(request.args.get("limit"), 8)))
    routes = analytics_store.fetch_popular_routes_for_location(
        country=requested_country,
        days=days,
        limit=limit,
    )
    return jsonify(
        {
            "country": requested_country or "GLOBAL",
            "days": days,
            "limit": limit,
            "routes": routes,
        }
    )


def _ai_parse_preview_payload(parsed: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a cached/fresh parse result for the JSON preview response.

    Flight and stay parses are flat dicts; a combined ("both") parse nests
    them under "flight"/"stay". The composer and the voice bridge both read
    `preview.kind` to decide how to render/confirm before submitting.
    """
    kind = str(parsed.get("kind") or "flights")
    if kind == "both":
        flight = parsed.get("flight") if isinstance(parsed.get("flight"), Mapping) else {}
        stay = parsed.get("stay") if isinstance(parsed.get("stay"), Mapping) else None
        preview: dict[str, Any] = {
            "kind": "both",
            "trip_type": flight.get("trip_type"),
            "search_mode": flight.get("search_mode"),
            "origin": flight.get("origin"),
            "destination": flight.get("destination"),
            "flex_month": flight.get("flex_month"),
        }
        if stay:
            nights = None
            if stay.get("checkin") and stay.get("checkout"):
                nights = _hotel_nights(str(stay["checkin"]), str(stay["checkout"])) or None
            preview["stay_summary"] = {
                "destination": stay.get("destination"),
                "nights": nights,
                "min_stars": stay.get("min_stars"),
            }
        return preview
    if kind == "stays":
        return {
            "kind": "stays",
            "destination": parsed.get("destination"),
            "checkin": parsed.get("checkin"),
            "checkout": parsed.get("checkout"),
        }
    return {
        "kind": "flights",
        "trip_type": parsed.get("trip_type"),
        "search_mode": parsed.get("search_mode"),
        "origin": parsed.get("origin"),
        "destination": parsed.get("destination"),
        "flex_month": parsed.get("flex_month"),
    }


def _ai_parse_analytics_fields(parsed: Mapping[str, Any]) -> dict[str, str]:
    """Best-effort origin/destination/trip_type for analytics, regardless of
    which of the three parsers (flight/stay/combined) produced `parsed`."""
    kind = str(parsed.get("kind") or "flights")
    source: Mapping[str, Any] = parsed
    if kind == "both" and isinstance(parsed.get("flight"), Mapping):
        source = parsed["flight"]
    return {
        "origin": str(source.get("origin") or "").strip().upper(),
        "destination": str(source.get("destination") or "").strip().upper(),
        "trip_type": str(source.get("trip_type") or "").strip().lower(),
        "search_mode": str(source.get("search_mode") or ""),
    }


@app.route("/search/ai-parse-preview", methods=["POST"])
def search_ai_parse_preview():
    """Warm-parse endpoint shared by the typed composer (debounced while
    typing) and 100% of voice input — this is the one place that decides
    whether a query is flight-only, hotel-only, or both."""
    payload = request.get_json(silent=True) or {}
    ai_text = str(payload.get("ai_text") or "").strip()
    if len(ai_text) < AI_PARSE_WARMUP_MIN_CHARS:
        _track_analytics_event(
            event_type="ai_parse_preview",
            search_mode="ai",
            success=False,
            metadata={"reason": "too_short", "query_length": len(ai_text)},
        )
        return jsonify({"ok": False, "message": "too_short"})

    cached, token = _get_cached_ai_parse_result(ai_text)
    if cached:
        preview = _ai_parse_preview_payload(cached)
        fields = _ai_parse_analytics_fields(cached)
        _track_analytics_event(
            event_type="ai_parse_preview",
            search_mode="ai",
            origin=fields["origin"],
            destination=fields["destination"],
            trip_type=fields["trip_type"],
            success=True,
            metadata={"cached": True, "search_mode": fields["search_mode"], "kind": preview.get("kind")},
        )
        return jsonify({"ok": True, "parse_token": token, "cached": True, "preview": preview})

    intent = detect_search_intent(ai_text) if LITE_ENABLED else "flights"
    if intent == "stays":
        parsed = parse_ai_stay_request(ai_text)
        kind = "stays"
    elif intent == "both":
        parsed = parse_ai_combined_request(ai_text)
        kind = "both"
    else:
        parsed = parse_ai_flight_request(ai_text)
        kind = "flights"

    if not parsed:
        _track_analytics_event(
            event_type="ai_parse_preview",
            search_mode="ai",
            success=False,
            metadata={"reason": "parse_failed", "kind": kind},
        )
        return jsonify({"ok": False, "message": "parse_failed"})

    parsed = dict(parsed)
    parsed["kind"] = kind
    token = _cache_ai_parse_result(ai_text, parsed)
    preview = _ai_parse_preview_payload(parsed)
    fields = _ai_parse_analytics_fields(parsed)
    _track_analytics_event(
        event_type="ai_parse_preview",
        search_mode="ai",
        origin=fields["origin"],
        destination=fields["destination"],
        trip_type=fields["trip_type"],
        success=True,
        metadata={"cached": False, "search_mode": fields["search_mode"], "kind": kind},
    )
    return jsonify({"ok": True, "parse_token": token, "cached": False, "preview": preview})


def _flex_stream_shell_hidden_fields(mode: str, params: dict[str, Any], *, ai_text: str = "", parse_token: str = "") -> dict[str, str]:
    """POST fields required to replay the same flexible search on `/search/flex-stream`."""
    if mode == "ai":
        fields = {"mode": "ai", "ai_text": ai_text, "search_submitted": "1"}
        if parse_token:
            fields["parse_token"] = parse_token
        return fields
    fields: dict[str, str] = {
        "mode": "flex",
        "search_submitted": "1",
        "origin": str(params.get("origin") or ""),
        "destination": str(params.get("destination") or ""),
        "trip_type": str(params.get("trip_type") or "roundtrip"),
        "flex_month": str(params.get("flex_month") or ""),
        "trip_length_days": str(params.get("trip_length_days") or DEFAULT_FLEX_TRIP_LENGTH_DAYS),
        "passengers": str(params.get("passengers") or DEFAULT_PASSENGERS),
        "cabin": str(params.get("cabin") or "ECONOMY"),
        "combination_mode": str(params.get("combination_mode") or "auto"),
        "sort": "cheapest",
    }
    if params.get("nonstop"):
        fields["nonstop"] = "on"
    return fields


@app.route("/search/flex-shell", methods=["POST"])
def search_flex_shell():
    """Render the real results page with skeleton rows; client streams into `#resultsCards` via `/search/flex-stream`."""
    mode = (request.form.get("mode") or "standard").strip().lower()
    params: dict[str, Any] | None = None
    error: str | None = None
    ai_text = ""
    parse_token = ""

    if mode == "flex":
        params = {
            "origin": request.form.get("origin", "").strip().upper(),
            "destination": request.form.get("destination", "").strip().upper(),
            "trip_type": request.form.get("trip_type", "roundtrip"),
            "flex_month": (request.form.get("flex_month", "").strip() or None),
            "trip_length_days": request.form.get("trip_length_days", str(DEFAULT_FLEX_TRIP_LENGTH_DAYS)),
            "passengers": request.form.get("passengers", str(DEFAULT_PASSENGERS)),
            "cabin": request.form.get("cabin", "ECONOMY"),
            "nonstop": request.form.get("nonstop") == "on",
            "sort": "cheapest",
            "combination_mode": request.form.get("combination_mode", "auto"),
            "raw_text": "",
        }
        params, error = _validate_flex_search_params(params)
    elif mode == "ai":
        ai_text = request.form.get("ai_text", "").strip()
        parse_token = request.form.get("parse_token", "").strip()
        if not ai_text:
            error = "Please describe the trip before searching."
        else:
            # search-loader.js routes every #aiForm submission through this
            # endpoint first — not /search/shell, which only fires when JS
            # fails to load. So this, not search_shell(), is the real place
            # a stays-only query needs to bail out to the hotel pipeline.
            # Cheap keyword-only check — safe to run synchronously (no
            # Gemini call in the common case), unlike the full combined
            # parse below, which stays deferred to /search/flex-stream.
            if LITE_ENABLED and detect_search_intent(ai_text) == "stays":
                return redirect(url_for("hotel_ai_search", q=ai_text))
            # Defer `parse_ai_flight_request` to `/search/flex-stream` so the homepage is not blocked.
            # Non-flex AI is handed off there to the same `/search` POST as the pending shell.
            params = {
                "origin": "",
                "destination": "",
                "trip_type": "roundtrip",
                "flex_month": None,
                "trip_length_days": str(DEFAULT_FLEX_TRIP_LENGTH_DAYS),
                "passengers": str(DEFAULT_PASSENGERS),
                "cabin": "ECONOMY",
                "nonstop": False,
                "sort": "cheapest",
                "combination_mode": "auto",
                "raw_text": ai_text,
            }
    elif mode == "standard":
        return _render_search_shell_pending()
    else:
        error = "Invalid search mode for this endpoint."

    if error or not params:
        q = params if isinstance(params, dict) else {}
        return render_template(
            "results.html",
            query=q,
            flights=[],
            error=error or "Invalid search",
            minutes_to_hm=minutes_to_hm,
            fmt_dt=fmt_dt,
        )

    display_query = dict(params)
    display_query["mode"] = mode
    if mode == "ai":
        display_query["raw_text"] = ai_text

    flex_stream_fields = _flex_stream_shell_hidden_fields(mode, params, ai_text=ai_text, parse_token=parse_token)

    return render_template(
        "search_shell.html",
        query=display_query,
        flex_streaming=True,
        flex_stream_fields=flex_stream_fields,
        flex_ai_deferred=(mode == "ai"),
    )


@app.route("/search/flex-stream", methods=["POST"])
def search_flex_stream():
    mode = (request.form.get("mode") or "standard").strip().lower()
    tracking_mode, tracking_hint = _request_form_search_hint(request.form)
    real_search_submission = _is_real_search_submission(request.form)
    params: dict[str, Any] | None = None
    error: str | None = None

    if mode == "flex":
        params = {
            "origin": request.form.get("origin", "").strip().upper(),
            "destination": request.form.get("destination", "").strip().upper(),
            "trip_type": request.form.get("trip_type", "roundtrip"),
            "flex_month": (request.form.get("flex_month", "").strip() or None),
            "trip_length_days": request.form.get("trip_length_days", str(DEFAULT_FLEX_TRIP_LENGTH_DAYS)),
            "passengers": request.form.get("passengers", str(DEFAULT_PASSENGERS)),
            "cabin": request.form.get("cabin", "ECONOMY"),
            "nonstop": request.form.get("nonstop") == "on",
            "sort": "cheapest",
            "combination_mode": request.form.get("combination_mode", "auto"),
            "raw_text": "",
        }
        params, error = _validate_flex_search_params(params)
    elif mode == "ai":
        ai_text = request.form.get("ai_text", "").strip()
        parse_token = request.form.get("parse_token", "").strip()
        if not ai_text:
            error = "Please describe the trip before searching."
        else:
            # This is the real place a "both" (flight + hotel) query gets
            # detected for browser submissions — search-loader.js sends
            # every #aiForm POST here first, so search_shell()'s own combined
            # handling below only runs for the no-JS fallback. Reuse an
            # already-warm-parsed "both" cache entry if the debounce beat
            # the submit; otherwise run the same detection + combined parse
            # here (the full Gemini call was already being deferred to this
            # endpoint for the flight-only case, so this doesn't add a new
            # blocking round trip that wasn't already happening).
            cached_for_intent, _ = _get_cached_ai_parse_result(ai_text, parse_token=parse_token)
            if LITE_ENABLED and cached_for_intent and cached_for_intent.get("kind") == "both" and cached_for_intent.get("wants_hotel"):
                _start_trip_intent(cached_for_intent)
            elif not cached_for_intent and LITE_ENABLED and detect_search_intent(ai_text) == "both":
                combined = parse_ai_combined_request(ai_text)
                if combined and combined.get("wants_hotel"):
                    combined_cached = dict(combined)
                    combined_cached["kind"] = "both"
                    _cache_ai_parse_result(ai_text, combined_cached)
                    _start_trip_intent(combined)

            parsed, cached_token = _resolve_ai_flight_params(ai_text, parse_token)
            if not parsed:
                error = (
                    "I wasn't quite able to follow that search. "
                    "For a flexible search, try something like: "
                    "\"JFK to Cancun in March, 7 days, economy\"."
                )
            elif parsed.get("search_mode") != "flex":
                error = "not_flex"
            else:
                p2, verr = _validate_flex_search_params(parsed, ai=True)
                if verr:
                    error = verr
                else:
                    params = p2
                    params.setdefault("nonstop", False)
                    params["raw_text"] = ai_text
                    if cached_token:
                        params["parse_token"] = cached_token
    else:
        error = "Flexible stream is only available for Cheapest week or AI flexible trips."

    if error == "not_flex":
        ai_text_nf = request.form.get("ai_text", "").strip()
        if real_search_submission:
            _track_search_completed_event(
                source="search_flex_stream",
                search_mode="ai",
                params=tracking_hint,
                result_count=0,
                success=False,
                error="handoff_to_standard_stream",
                metadata={"handoff": "standard_stream"},
            )

        def gen_ai_standard_handoff() -> Iterator[str]:
            action = url_for("search")
            fields: list[list[str]] = [
                ["mode", "ai"],
                ["ai_text", ai_text_nf],
                ["instant", "1"],
            ]
            parse_token_nf = request.form.get("parse_token", "").strip()
            if parse_token_nf:
                fields.append(["parse_token", parse_token_nf])
            yield _flex_ndjson_line({"type": "ai_standard_handoff", "action": action, "fields": fields})

        return Response(stream_with_context(gen_ai_standard_handoff()), mimetype="application/x-ndjson")

    if error or not params:
        if real_search_submission:
            _track_search_completed_event(
                source="search_flex_stream",
                search_mode=("ai" if mode == "ai" else "flex"),
                params=(params or tracking_hint),
                result_count=0,
                success=False,
                error=error or "invalid_request",
            )

        def gen_err() -> Iterator[str]:
            yield _flex_ndjson_line({"type": "error", "message": error or "Invalid request"})

        return Response(stream_with_context(gen_err()), mimetype="application/x-ndjson")

    _record_search_for_signed_in_account("ai" if mode == "ai" else "flex", params)

    def generate() -> Iterator[str]:
        emitted_rows = 0
        last_error = ""
        saw_handoff = False
        for line in _iter_flex_search_ndjson(params):
            try:
                parsed = json.loads(line)
            except Exception:
                parsed = {}
            event_type = str(parsed.get("type") or "").strip().lower()
            if event_type == "flight_row":
                emitted_rows += 1
            elif event_type == "error":
                last_error = str(parsed.get("message") or "").strip()
            elif event_type == "ai_standard_handoff":
                saw_handoff = True
            yield line

        if saw_handoff:
            if real_search_submission:
                _track_search_completed_event(
                    source="search_flex_stream",
                    search_mode="ai",
                    params=params,
                    result_count=0,
                    success=False,
                    error="handoff_to_standard_stream",
                    metadata={"handoff": "standard_stream"},
                )
            return

        if real_search_submission:
            _track_search_completed_event(
                source="search_flex_stream",
                search_mode=tracking_mode if tracking_mode in {"ai", "flex"} else "flex",
                params=params,
                result_count=emitted_rows,
                success=(emitted_rows > 0 and not last_error),
                error=last_error,
                metadata={"stream": True},
            )

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


def _render_search_shell_pending() -> Any:
    """Render `search_shell.html` with pending POST replay to `/search` (standard + non-flex AI)."""
    mode = (request.form.get("mode") or "standard").strip().lower()

    pending_fields: list[tuple[str, str]] = []
    for key in request.form.keys():
        values = request.form.getlist(key)
        if not values:
            pending_fields.append((key, ""))
            continue
        for value in values:
            pending_fields.append((key, value))
    if request.form.get("instant") != "1":
        pending_fields.append(("instant", "1"))

    display_query: dict[str, Any] = {
        "origin": request.form.get("origin", "").strip().upper(),
        "destination": request.form.get("destination", "").strip().upper(),
        "trip_type": request.form.get("trip_type", "roundtrip" if request.form.get("return_date") else "oneway"),
        "depart_date": (request.form.get("depart_date", "").strip() or None),
        "return_date": (request.form.get("return_date", "").strip() or None),
        "passengers": request.form.get("passengers", str(DEFAULT_PASSENGERS)),
        "cabin": request.form.get("cabin", "ECONOMY"),
        "nonstop": request.form.get("nonstop") == "on",
        "sort": request.form.get("sort", "recommended"),
        "combination_mode": request.form.get("combination_mode", "auto"),
        "raw_text": "",
    }
    if mode == "ai":
        display_query["raw_text"] = request.form.get("ai_text", "").strip()
    if mode == "standard":
        leg_origins = request.form.getlist("leg_origin")
        leg_destinations = request.form.getlist("leg_destination")
        leg_dates = request.form.getlist("leg_date")
        multi_legs = _build_multicity_form_legs(leg_origins, leg_destinations, leg_dates)
        if multi_legs:
            display_query["legs"] = multi_legs

    return render_template(
        "search_shell.html",
        query=display_query,
        pending_action=url_for("search"),
        pending_fields=pending_fields,
    )


def _iter_standard_search_ndjson() -> Iterator[str]:
    """NDJSON stream for standard + fixed-date AI searches (search shell). Keep in sync with search()."""

    def emit(obj: dict[str, Any]) -> str:
        return _flex_ndjson_line(obj)

    form = request.form
    mode = (form.get("mode") or "standard").strip().lower()
    instant_mode = form.get("instant") == "1"

    def complete_html(html: str) -> Iterator[str]:
        yield emit(_results_complete_stream_event(html))

    if mode == "flex":
        html = render_template(
            "results.html",
            query={"mode": "flex"},
            flights=[],
            error="Flexible-month searches use the dedicated flex flow. Go back to the homepage and choose Cheapest week / flex.",
            minutes_to_hm=minutes_to_hm,
            fmt_dt=fmt_dt,
        )
        yield from complete_html(html)
        return

    params: dict[str, Any] | None = None

    if mode == "ai":
        ai_text = form.get("ai_text", "").strip()
        parse_token = form.get("parse_token", "").strip()
        if not ai_text:
            html = render_template(
                "results.html",
                query={"raw_text": ai_text},
                flights=[],
                error="Please describe the trip before searching.",
                minutes_to_hm=minutes_to_hm,
                fmt_dt=fmt_dt,
            )
            yield from complete_html(html)
            return
        params, cached_token = _resolve_ai_flight_params(ai_text, parse_token)
        if not params:
            html = render_template(
                "results.html",
                query={"raw_text": ai_text},
                flights=[],
                error=(
                    "I wasn’t quite able to understand that search. "
                    "Try including: a departure city or airport, a destination, and travel dates. "
                    "For example: \"New York to London, June 10 to June 17, economy, 2 passengers\"."
                ),
                minutes_to_hm=minutes_to_hm,
                fmt_dt=fmt_dt,
            )
            yield from complete_html(html)
            return
        params["raw_text"] = ai_text
        if cached_token:
            params["parse_token"] = cached_token

        if params.get("search_mode") == "flex":
            params, error = _validate_flex_search_params(params, ai=True)
            if error:
                html = render_template("results.html", query=params, flights=[], error=error, minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)
                yield from complete_html(html)
                return

            _record_search_for_signed_in_account("ai", params)
            params.setdefault("nonstop", False)
            if params.get("trip_type") == "oneway":
                best = find_best_oneway_day_in_month(params)
            else:
                best = find_best_week_in_month(params)
            if not best:
                html = render_template(
                    "results.html",
                    query=params,
                    flights=[],
                    error=_format_flex_no_results_error(params),
                    minutes_to_hm=minutes_to_hm,
                    fmt_dt=fmt_dt,
                )
                yield from complete_html(html)
                return

            params["depart_date"] = best["depart_date"]
            if params.get("trip_type") == "oneway":
                params["return_date"] = None
                params["best_week"] = None
                params["best_scan_label"] = "Best day found"
                params["best_scan_value"] = best["depart_date"]
                params["scan_price_note"] = (
                    f"Smart scan found ~{best['scan_currency']} ${best['scan_price_total']:.2f} for the strongest one-way departure date."
                )
            else:
                params["return_date"] = best["return_date"]
                params["sort"] = "cheapest"
                params["best_week"] = f"{best['depart_date']} → {best['return_date']}"
                params["best_scan_label"] = "Best week found"
                params["best_scan_value"] = params["best_week"]
                params["scan_price_note"] = (
                    f"Smart scan found ~{best['scan_currency']} ${best['scan_price_total']:.2f} for the cheapest date pair (final prices may differ)"
                )
            if best.get("fallback_notice"):
                params["scan_price_note"] = f"{params['scan_price_note']} {best['fallback_notice']}"

            offers = best["offers"]
            yield emit({"type": "standard_search", "stage": "fetch"})
            yield emit({"type": "standard_search", "stage": "ranking", "count": len(offers)})
            for i, fl in enumerate(offers):
                yield emit({"type": "flight_row", "rank": i + 1, "flight": _flex_stream_flight_preview(fl)})
            html = render_template("results.html", query=params, flights=offers, error="", minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)
            yield from complete_html(html)
            return
    else:
        trip_type_form = form.get("trip_type", "roundtrip" if form.get("return_date") else "oneway")
        multi_legs: list[dict[str, str]] = []
        if trip_type_form == "multicity":
            leg_origins = form.getlist("leg_origin")
            leg_destinations = form.getlist("leg_destination")
            leg_dates = form.getlist("leg_date")
            multi_legs = _build_multicity_form_legs(leg_origins, leg_destinations, leg_dates)
        params = {
            "origin": form.get("origin", "").strip().upper(),
            "destination": form.get("destination", "").strip().upper(),
            "trip_type": trip_type_form,
            "depart_date": (form.get("depart_date", "").strip() or None),
            "return_date": (form.get("return_date", "").strip() or None),
            "passengers": form.get("passengers", str(DEFAULT_PASSENGERS)),
            "cabin": form.get("cabin", "ECONOMY"),
            "nonstop": form.get("nonstop") == "on",
            "sort": form.get("sort", "recommended"),
            "combination_mode": form.get("combination_mode", "auto"),
            "selected_outbound_token": (form.get("selected_outbound_token", "").strip() or None),
            "selected_return_token": (form.get("selected_return_token", "").strip() or None),
            "raw_text": "",
        }
        if multi_legs:
            params["legs"] = multi_legs
        params, error = _validate_standard_search_params(params)
        if error:
            html = render_template("results.html", query=params, flights=[], error=error, minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)
            yield from complete_html(html)
            return

    if mode == "ai":
        assert params is not None
        params, error = _validate_standard_search_params(params, ai=True)
        if error:
            html = render_template("results.html", query=params, flights=[], error=error, minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)
            yield from complete_html(html)
            return

    assert params is not None
    _record_search_for_signed_in_account("ai" if mode == "ai" else "standard", params)
    if params.get("combination_mode") == "manual" and params.get("trip_type") == "roundtrip":
        manual_flow = build_manual_combination_flow(params)
        manual_error = ""
        if not manual_flow.get("outbound_options"):
            manual_error = "No departure flights found. Try different dates or remove constraints like nonstop."
        elif manual_flow.get("stage") in {"return", "complete"} and not manual_flow.get("return_options"):
            manual_error = "No return flights found for that outbound choice. Try a different outbound or loosen the filters."
        html = render_template(
            "results.html",
            query=params,
            flights=[],
            manual_flow=manual_flow,
            error=manual_error,
            minutes_to_hm=minutes_to_hm,
            fmt_dt=fmt_dt,
        )
        yield from complete_html(html)
        return

    force_refresh = form.get("force_refresh") == "1"
    search_detailed = not instant_mode
    yield emit({"type": "standard_search", "stage": "fetch"})
    flights = search_flights(params, detailed=search_detailed, force_refresh=force_refresh)
    if not flights:
        html = render_template(
            "results.html",
            query=params,
            flights=[],
            error="No flights found. Try different dates or remove constraints like max price.",
            minutes_to_hm=minutes_to_hm,
            fmt_dt=fmt_dt,
        )
        yield from complete_html(html)
        return

    yield emit({"type": "standard_search", "stage": "ranking", "count": len(flights)})
    for i, fl in enumerate(flights):
        yield emit({"type": "flight_row", "rank": i + 1, "flight": _flex_stream_flight_preview(fl)})
    html = render_template("results.html", query=params, flights=flights, error="", minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)
    yield from complete_html(html)


@app.route("/search/stream", methods=["POST"])
def search_stream():
    """Stream ranked flight previews then full results HTML (same contract as flex stream for flight_row + complete)."""
    tracking_mode, tracking_hint = _request_form_search_hint(request.form)
    real_search_submission = _is_real_search_submission(request.form)

    def generate() -> Iterator[str]:
        emitted_rows = 0
        saw_complete = False
        last_error = ""
        for line in _iter_standard_search_ndjson():
            try:
                parsed = json.loads(line)
            except Exception:
                parsed = {}
            event_type = str(parsed.get("type") or "").strip().lower()
            if event_type == "flight_row":
                emitted_rows += 1
            elif event_type == "error":
                last_error = str(parsed.get("message") or "").strip()
            elif event_type == "complete":
                saw_complete = True
            yield line

        manual_mode = (
            str(tracking_hint.get("combination_mode") or "").strip().lower() == "manual"
            and str(tracking_hint.get("trip_type") or "").strip().lower() == "roundtrip"
        )
        success = (emitted_rows > 0 or (saw_complete and manual_mode)) and not last_error
        if real_search_submission:
            _track_search_completed_event(
                source="search_stream",
                search_mode=tracking_mode if tracking_mode in {"standard", "ai"} else "standard",
                params=tracking_hint,
                result_count=emitted_rows,
                success=success,
                error=last_error,
                metadata={"stream": True, "manual_mode": manual_mode},
            )

    return Response(stream_with_context(generate()), mimetype="application/x-ndjson")


@app.route("/search/shell", methods=["POST"])
def search_shell():
    """Render results immediately, then let client fetch heavy `/search` HTML."""
    mode = (request.form.get("mode") or "standard").strip().lower()

    # One AI box serves both products: a stay-shaped query is handed to the
    # hotel pipeline instead of being forced through flight search. A query
    # that asks for both parses both halves up front and caches the combined
    # result (keyed by the same ai_text /search reuses below) so the flight
    # results that render next already know a hotel is part of this trip.
    # Any search that ISN'T reaffirming a combined ask starts a clean slate —
    # carrying a stale hotel intent into an unrelated search would show a
    # "you're also booking a hotel" banner nobody asked for on this run.
    started_combined = False
    if mode == "ai" and LITE_ENABLED:
        ai_text = (request.form.get("ai_text") or request.form.get("q") or "").strip()
        if ai_text:
            intent = detect_search_intent(ai_text)
            if intent == "stays":
                _clear_trip_intent()
                return redirect(url_for("hotel_ai_search", q=ai_text))
            if intent == "both":
                combined = parse_ai_combined_request(ai_text)
                if combined and combined.get("wants_hotel"):
                    combined_cached = dict(combined)
                    combined_cached["kind"] = "both"
                    _cache_ai_parse_result(ai_text, combined_cached)
                    _start_trip_intent(combined)
                    started_combined = True

    if not started_combined:
        _clear_trip_intent()

    if mode == "flex":
        # Keep flexible flow on the dedicated streaming shell.
        return search_flex_shell()

    return _render_search_shell_pending()


@app.route("/search/edit", methods=["POST"])
def search_edit():
    edit_fields: list[tuple[str, str]] = []
    for key in request.form.keys():
        values = request.form.getlist(key)
        if not values:
            edit_fields.append((key, ""))
            continue
        for value in values:
            edit_fields.append((key, value))
    _set_edit_search_fields(edit_fields)
    return redirect(url_for("index"))


@app.route("/search", methods=["POST"])
def search():
    mode = (request.form.get("mode") or "standard").strip().lower()
    instant_mode = request.form.get("instant") == "1"
    tracking_mode, tracking_hint = _request_form_search_hint(request.form)
    real_search_submission = _is_real_search_submission(request.form)
    previous_search_context = _analytics_search_context()

    if mode == "flex":
        params = {
            "origin": request.form.get("origin", "").strip().upper(),
            "destination": request.form.get("destination", "").strip().upper(),
            "trip_type": request.form.get("trip_type", "roundtrip"),
            "flex_month": (request.form.get("flex_month", "").strip() or None),
            "trip_length_days": request.form.get("trip_length_days", str(DEFAULT_FLEX_TRIP_LENGTH_DAYS)),
            "passengers": request.form.get("passengers", str(DEFAULT_PASSENGERS)),
            "cabin": request.form.get("cabin", "ECONOMY"),
            "nonstop": request.form.get("nonstop") == "on",
            "sort": "cheapest",
            "combination_mode": request.form.get("combination_mode", "auto"),
            "raw_text": "",
        }
        params, error = _validate_flex_search_params(params)
        if error:
            if real_search_submission:
                _track_search_completed_event(
                    source="search",
                    search_mode="flex",
                    params=params,
                    result_count=0,
                    success=False,
                    error=error,
                )
            return render_template("results.html", query=params, flights=[], error=error, minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)

        _record_search_for_signed_in_account("flex", params)
        if params.get("trip_type") == "oneway":
            best = find_best_oneway_day_in_month(params)
        else:
            best = find_best_week_in_month(params)
        if not best:
            no_results_error = _format_flex_no_results_error(params)
            if real_search_submission:
                _track_search_completed_event(
                    source="search",
                    search_mode="flex",
                    params=params,
                    result_count=0,
                    success=False,
                    error=no_results_error,
                )
            return render_template("results.html", query=params, flights=[], error=_format_flex_no_results_error(params), minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)

        params["depart_date"] = best["depart_date"]
        if params.get("trip_type") == "oneway":
            params["return_date"] = None
            params["best_week"] = None
            params["best_scan_label"] = "Best day found"
            params["best_scan_value"] = best["depart_date"]
            params["scan_price_note"] = f"Smart scan found ~{best['scan_currency']} ${best['scan_price_total']:.2f} for the strongest one-way departure date."
        else:
            params["return_date"] = best["return_date"]
            params["sort"] = "cheapest"
            params["best_week"] = f"{best['depart_date']} → {best['return_date']}"
            params["best_scan_label"] = "Best week found"
            params["best_scan_value"] = params["best_week"]
            params["scan_price_note"] = f"Smart scan found ~{best['scan_currency']} ${best['scan_price_total']:.2f} for the cheapest date pair (final prices may differ)"
        if best.get("fallback_notice"):
            params["scan_price_note"] = f"{params['scan_price_note']} {best['fallback_notice']}"

        flex_offers = list(best.get("offers") or [])
        if real_search_submission:
            _track_search_completed_event(
                source="search",
                search_mode="flex",
                params=params,
                result_count=len(flex_offers),
                success=bool(flex_offers),
                metadata={
                    "best_depart_date": str(best.get("depart_date") or ""),
                    "best_return_date": str(best.get("return_date") or ""),
                },
            )
        return render_template("results.html", query=params, flights=best["offers"], error="", minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)

    if mode == "ai":
        ai_text = request.form.get("ai_text", "").strip()
        parse_token = request.form.get("parse_token", "").strip()
        if not ai_text:
            if real_search_submission:
                _track_search_completed_event(
                    source="search",
                    search_mode="ai",
                    params=tracking_hint,
                    result_count=0,
                    success=False,
                    error="blank_ai_prompt",
                )
            return render_template("results.html", query={"raw_text": ai_text}, flights=[], error="Please describe the trip before searching.", minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)
        params, cached_token = _resolve_ai_flight_params(ai_text, parse_token)
        if not params:
            if real_search_submission:
                _track_search_completed_event(
                    source="search",
                    search_mode="ai",
                    params=tracking_hint,
                    result_count=0,
                    success=False,
                    error="ai_parse_failed",
                )
            return render_template("results.html", query={"raw_text": ai_text}, flights=[], error="Sorry, I couldn’t understand that request. Try: 'JFK to LAX on 2026-03-10'.", minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)
        params["raw_text"] = ai_text
        if cached_token:
            params["parse_token"] = cached_token

        if params.get("search_mode") == "flex":
            params, error = _validate_flex_search_params(params, ai=True)
            if error:
                if real_search_submission:
                    _track_search_completed_event(
                        source="search",
                        search_mode="ai",
                        params=params,
                        result_count=0,
                        success=False,
                        error=error,
                    )
                return render_template("results.html", query=params, flights=[], error=error, minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)

            _record_search_for_signed_in_account("ai", params)
            params.setdefault("nonstop", False)
            if params.get("trip_type") == "oneway":
                best = find_best_oneway_day_in_month(params)
            else:
                best = find_best_week_in_month(params)
            if not best:
                no_results_error = _format_flex_no_results_error(params)
                if real_search_submission:
                    _track_search_completed_event(
                        source="search",
                        search_mode="ai",
                        params=params,
                        result_count=0,
                        success=False,
                        error=no_results_error,
                        metadata={"search_mode": "flex"},
                    )
                return render_template("results.html", query=params, flights=[], error=_format_flex_no_results_error(params), minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)

            params["depart_date"] = best["depart_date"]
            if params.get("trip_type") == "oneway":
                params["return_date"] = None
                params["best_week"] = None
                params["best_scan_label"] = "Best day found"
                params["best_scan_value"] = best["depart_date"]
                params["scan_price_note"] = f"Smart scan found ~{best['scan_currency']} ${best['scan_price_total']:.2f} for the strongest one-way departure date."
            else:
                params["return_date"] = best["return_date"]
                params["sort"] = "cheapest"
                params["best_week"] = f"{best['depart_date']} → {best['return_date']}"
                params["best_scan_label"] = "Best week found"
                params["best_scan_value"] = params["best_week"]
                params["scan_price_note"] = f"Smart scan found ~{best['scan_currency']} ${best['scan_price_total']:.2f} for the cheapest date pair (final prices may differ)"
            if best.get("fallback_notice"):
                params["scan_price_note"] = f"{params['scan_price_note']} {best['fallback_notice']}"
            ai_flex_offers = list(best.get("offers") or [])
            if real_search_submission:
                _track_search_completed_event(
                    source="search",
                    search_mode="ai",
                    params=params,
                    result_count=len(ai_flex_offers),
                    success=bool(ai_flex_offers),
                    metadata={
                        "search_mode": "flex",
                        "best_depart_date": str(best.get("depart_date") or ""),
                        "best_return_date": str(best.get("return_date") or ""),
                    },
                )
            return render_template("results.html", query=params, flights=best["offers"], error="", minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)
    else:
        trip_type_form = request.form.get("trip_type", "roundtrip" if request.form.get("return_date") else "oneway")
        multi_legs: list[dict[str, str]] = []
        if trip_type_form == "multicity":
            leg_origins = request.form.getlist("leg_origin")
            leg_destinations = request.form.getlist("leg_destination")
            leg_dates = request.form.getlist("leg_date")
            multi_legs = _build_multicity_form_legs(leg_origins, leg_destinations, leg_dates)
        params = {
            "origin": request.form.get("origin", "").strip().upper(),
            "destination": request.form.get("destination", "").strip().upper(),
            "trip_type": trip_type_form,
            "depart_date": (request.form.get("depart_date", "").strip() or None),
            "return_date": (request.form.get("return_date", "").strip() or None),
            "passengers": request.form.get("passengers", str(DEFAULT_PASSENGERS)),
            "cabin": request.form.get("cabin", "ECONOMY"),
            "nonstop": request.form.get("nonstop") == "on",
            "sort": request.form.get("sort", "recommended"),
            "combination_mode": request.form.get("combination_mode", "auto"),
            "selected_outbound_token": (request.form.get("selected_outbound_token", "").strip() or None),
            "selected_return_token": (request.form.get("selected_return_token", "").strip() or None),
            "raw_text": "",
        }
        if multi_legs:
            params["legs"] = multi_legs
        params, error = _validate_standard_search_params(params)
        if error:
            if real_search_submission:
                _track_search_completed_event(
                    source="search",
                    search_mode="standard",
                    params=params,
                    result_count=0,
                    success=False,
                    error=error,
                )
            return render_template("results.html", query=params, flights=[], error=error, minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)

    if mode == "ai":
        params, error = _validate_standard_search_params(params, ai=True)
        if error:
            if real_search_submission:
                _track_search_completed_event(
                    source="search",
                    search_mode="ai",
                    params=params,
                    result_count=0,
                    success=False,
                    error=error,
                )
            return render_template("results.html", query=params, flights=[], error=error, minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)

    _record_search_for_signed_in_account("ai" if mode == "ai" else "standard", params)
    if params.get("combination_mode") == "manual" and params.get("trip_type") == "roundtrip":
        manual_flow = build_manual_combination_flow(params)
        manual_error = ""
        if not manual_flow.get("outbound_options"):
            manual_error = "No departure flights found. Try different dates or remove constraints like nonstop."
        elif manual_flow.get("stage") in {"return", "complete"} and not manual_flow.get("return_options"):
            manual_error = "No return flights found for that outbound choice. Try a different outbound or loosen the filters."
        manual_result_count = len(manual_flow.get("outbound_options") or []) + len(manual_flow.get("return_options") or [])
        if real_search_submission:
            _track_search_completed_event(
                source="search",
                search_mode=("ai" if mode == "ai" else "standard"),
                params=params,
                result_count=manual_result_count,
                success=bool(manual_result_count) and not manual_error,
                error=manual_error,
                metadata={"manual_mode": True, "manual_stage": str(manual_flow.get("stage") or "")},
            )
        return render_template(
            "results.html",
            query=params,
            flights=[],
            manual_flow=manual_flow,
            error=manual_error,
            minutes_to_hm=minutes_to_hm,
            fmt_dt=fmt_dt,
        )

    force_refresh = request.form.get("force_refresh") == "1"
    # Fast-first render for initial shell requests; refine/update/search refreshes
    # without `instant=1` still run the full detailed pipeline.
    search_detailed = not instant_mode
    flights = search_flights(params, detailed=search_detailed, force_refresh=force_refresh)
    if not flights:
        if real_search_submission:
            _track_search_completed_event(
                source="search",
                search_mode=("ai" if mode == "ai" else tracking_mode),
                params=params,
                result_count=0,
                success=False,
                error="no_flights_found",
                metadata={"detailed": bool(search_detailed)},
            )
            if force_refresh:
                _track_results_updated_event(
                    search_mode=("ai" if mode == "ai" else tracking_mode),
                    params=params,
                    previous_params=previous_search_context,
                    result_count=0,
                    success=False,
                    error="no_flights_found",
                    metadata={"detailed": bool(search_detailed)},
                )
        return render_template("results.html", query=params, flights=[], error="No flights found. Try different dates or remove constraints like max price.", minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)

    if real_search_submission:
        _track_search_completed_event(
            source="search",
            search_mode=("ai" if mode == "ai" else tracking_mode),
            params=params,
            result_count=len(flights),
            success=True,
            metadata={"detailed": bool(search_detailed)},
        )
        if force_refresh:
            _track_results_updated_event(
                search_mode=("ai" if mode == "ai" else tracking_mode),
                params=params,
                previous_params=previous_search_context,
                result_count=len(flights),
                success=True,
                metadata={"detailed": bool(search_detailed)},
            )
    return render_template("results.html", query=params, flights=flights, error="", minutes_to_hm=minutes_to_hm, fmt_dt=fmt_dt)

# ------------------------------------------------------------
# AI Assistant endpoints
# ------------------------------------------------------------

def _fmt_flight_line(f: dict, idx: int) -> str:
    rank    = f.get("rank") or idx
    airline = f.get("airline", "Unknown airline")
    price   = f.get("price", "")
    stops   = f.get("stops", "")
    dur     = f.get("duration", "")
    badges  = f.get("badges", [])
    segs    = f.get("segments", [])
    pnote   = f.get("price_note", "")

    seg_parts = []
    for s in segs[:2]:
        frm  = s.get("from", "")
        to   = s.get("to", "")
        dep  = s.get("depart", "")
        arr  = s.get("arrive", "")
        lay  = s.get("layovers", "")
        part = f"{frm} {dep}→{to} {arr}".strip()
        if lay:
            part += f" (via {lay})"
        seg_parts.append(part)

    line = f"{rank}. {airline} · {price} · {stops} · {dur}"
    if seg_parts:
        line += " | " + " / ".join(seg_parts)
    if badges:
        line += " [" + ", ".join(badges[:3]) + "]"
    if pnote and "Lowest" not in pnote:
        line += f" ({pnote})"
    return line


def _build_ai_chat_system(context: dict) -> str:
    page_type   = str(context.get("page_type") or "results").strip()
    origin      = str(context.get("origin") or "").strip()
    destination = str(context.get("destination") or "").strip()
    depart      = str(context.get("depart_date") or "").strip()
    ret         = str(context.get("return_date") or "").strip()
    cabin       = (str(context.get("cabin_label") or context.get("cabin") or "Economy")).replace("_", " ").title()
    passengers  = context.get("passengers") or context.get("traveler_count") or 1
    trip_type   = str(context.get("trip_type") or "roundtrip")
    airline     = str(context.get("airline_name") or context.get("airline") or "").strip()
    currency    = str(context.get("currency") or "USD").strip()
    total       = str(context.get("total_amount") or "").strip()
    route       = str(context.get("route_summary") or "").strip()
    booking_ref = str(context.get("booking_reference") or "").strip()

    BASE = (
        "You are Skairova AI, a concise flight expert embedded in the Skairova travel platform. "
        "TOPIC RULE: Only discuss travel, flights, airports, airlines, baggage, and logistics. Decline anything off-topic in one sentence. "
        "CONCISENESS RULE: Reply in 1-2 sentences maximum. If listing multiple items, use 3-5 short bullets — never paragraphs. "
        "Never start with filler phrases like 'Great question', 'Of course', or 'Certainly'. "
        "CAPABILITY LIMITS — you CANNOT: run flight searches, book flights, cancel or change bookings, apply filters, or take any action on the user's behalf. "
        "If asked to do any of these things, say in one sentence that you can't do it, then redirect to what you CAN help with. "
        "Be direct and confident like a seasoned travel expert."
    )

    # ── CHECKOUT ──────────────────────────────────────────────────────────────
    if page_type == "checkout":
        slices = context.get("slices") or []
        slice_lines = []
        for sl in slices[:4]:
            lbl = sl.get("label", "")
            dep = sl.get("depart_label", "")
            dur = sl.get("duration_label", "")
            stops = sl.get("stops_label", "")
            slice_lines.append(f"  • {lbl}: departs {dep}, {dur}, {stops}")
        itinerary = "\n".join(slice_lines) if slice_lines else ""
        price_line = f"{currency} {total}" if total else ""
        return (
            f"{BASE}\n\n"
            f"CURRENT PAGE: Checkout — the user is about to confirm and pay for their booking.\n"
            f"SELECTED FLIGHT: {airline or 'Selected airline'}" +
            (f" — {route}" if route else "") +
            (f"\nITINERARY:\n{itinerary}" if itinerary else "") +
            (f"\nTOTAL: {price_line}" if price_line else "") +
            f"\nCABIN: {cabin}, {passengers} traveler{'s' if int(passengers) != 1 else ''}\n\n"
            "Your job: help the user feel confident before they pay. Answer questions about:\n"
            "- What documents they need (passport, visa requirements for the destination)\n"
            "- Baggage policy for this airline\n"
            "- Check-in process and timing\n"
            "- What to expect at departure and arrival airports\n"
            "- Cancellation and change policy for this type of fare\n"
            "- Whether travel insurance is worth adding\n"
            "Do NOT invent specific baggage fees or policies — give general guidance and suggest checking the airline's site for exact figures."
        )

    # ── SEAT SELECTION ────────────────────────────────────────────────────────
    if page_type == "seat_selection":
        return (
            f"{BASE}\n\n"
            f"CURRENT PAGE: Seat selection — the user is choosing seats for their flight.\n"
            f"FLIGHT: {airline or 'Selected airline'}" +
            (f" — {route}" if route else "") +
            f"\nCABIN: {cabin}, {passengers} traveler{'s' if int(passengers) != 1 else ''}\n\n"
            "Your job: help the user pick the best seat for their needs. Cover:\n"
            "- Front vs back (boarding, deplaning, turbulence)\n"
            "- Window vs aisle vs middle trade-offs\n"
            "- Exit row benefits (legroom) and restrictions (must assist in emergency)\n"
            "- Seats to avoid (near lavatories, galley, non-reclining last row)\n"
            "- Best seats for long-haul comfort on this aircraft type if known\n"
            "- Whether paying for a preferred seat is worth it\n"
            "If the user mentions a specific seat number, give tailored advice."
        )

    # ── BOOKING CONFIRMATION ──────────────────────────────────────────────────
    if page_type == "confirmation":
        pax = context.get("passenger_names") or []
        pax_line = ", ".join(pax[:4]) if pax else ""
        slices = context.get("slices") or []
        slice_lines = []
        for sl in slices[:4]:
            lbl = sl.get("label", "")
            dep = sl.get("depart_label", "")
            arr = sl.get("arrive_label", "")
            dur = sl.get("duration_label", "")
            stops = sl.get("stops_label", "")
            line = f"  • {lbl}: departs {dep}" + (f", arrives {arr}" if arr else "") + (f", {dur}" if dur else "") + (f", {stops}" if stops else "")
            slice_lines.append(line)
        itinerary = "\n".join(slice_lines) if slice_lines else ""
        return (
            f"{BASE}\n\n"
            f"CURRENT PAGE: Booking confirmation — the booking is confirmed.\n"
            f"BOOKING REFERENCE: {booking_ref or '(unknown)'}\n"
            f"AIRLINE: {airline or 'Unknown'}" +
            (f" — {route}" if route else "") +
            (f"\nITINERARY:\n{itinerary}" if itinerary else "") +
            (f"\nPASSENGERS: {pax_line}" if pax_line else "") +
            (f"\nTOTAL PAID: {currency} {total}" if total else "") +
            "\n\nYour job: help the user now that their booking is confirmed. Focus on:\n"
            "- How early to arrive at the airport (general rule: 2h domestic, 3h international)\n"
            "- Online check-in availability and timing (usually 24-48h before departure)\n"
            "- Baggage allowance and what to pack\n"
            "- Visa and entry requirements for the destination\n"
            "- What to expect at the airports on this route\n"
            "- Local tips for the destination if asked\n"
            "The booking is already confirmed — do not suggest changes unless the user asks."
        )

    # ── MANAGE BOOKING ────────────────────────────────────────────────────────
    if page_type == "manage_booking":
        status = str(context.get("status_label") or context.get("status") or "").strip()
        refund_policy = str(context.get("refund_policy") or "").strip()
        change_policy = str(context.get("change_policy") or "").strip()
        slices = context.get("slices") or []
        slice_lines = []
        for sl in slices[:4]:
            lbl = sl.get("label", "")
            dep = sl.get("depart_label", "")
            dur = sl.get("duration_label", "")
            stops = sl.get("stops_label", "")
            slice_lines.append(f"  • {lbl}: departs {dep}, {dur}, {stops}")
        itinerary = "\n".join(slice_lines) if slice_lines else ""
        return (
            f"{BASE}\n\n"
            f"CURRENT PAGE: Manage booking — the user is managing an existing booking.\n"
            f"BOOKING REFERENCE: {booking_ref or '(unknown)'}\n"
            f"AIRLINE: {airline or 'Unknown'}" +
            (f" — {route}" if route else "") +
            (f"\nSTATUS: {status}" if status else "") +
            (f"\nITINERARY:\n{itinerary}" if itinerary else "") +
            (f"\nTOTAL PAID: {currency} {total}" if total else "") +
            (f"\nREFUND POLICY: {refund_policy}" if refund_policy else "") +
            (f"\nCHANGE POLICY: {change_policy}" if change_policy else "") +
            "\n\nYour job: help the user understand their options for this booking:\n"
            "- Whether they can change or cancel and what fees apply\n"
            "- How refunds work and how long they take\n"
            "- What to do if they miss their flight\n"
            "- How to add baggage or other services\n"
            "- What the airline's policies are for delays and cancellations\n"
            "Be honest about fees — if a fare is non-refundable, say so clearly."
        )

    # ── BOOKING REVIEW ────────────────────────────────────────────────────────
    if page_type == "booking_review":
        slices = context.get("slices") or []
        slice_lines = []
        for sl in slices[:4]:
            lbl = sl.get("label", "")
            dep = sl.get("depart_label", "")
            dur = sl.get("duration_label", "")
            stops = sl.get("stops_label", "")
            slice_lines.append(f"  • {lbl}: departs {dep}, {dur}, {stops}")
        itinerary = "\n".join(slice_lines) if slice_lines else ""
        return (
            f"{BASE}\n\n"
            f"CURRENT PAGE: Booking review — the user is reviewing their trip before proceeding to checkout.\n"
            f"FLIGHT: {airline or 'Selected airline'}" +
            (f" — {route}" if route else "") +
            (f"\nITINERARY:\n{itinerary}" if itinerary else "") +
            (f"\nTOTAL: {currency} {total}" if total else "") +
            f"\nCABIN: {cabin}, {passengers} traveler{'s' if int(passengers) != 1 else ''}\n\n"
            "Your job: help the user decide if this is the right flight. Answer questions about:\n"
            "- What's included in this fare class (baggage, changes, refunds)\n"
            "- The airline's reputation, on-time performance, and in-flight experience\n"
            "- What to expect at the airports on this route\n"
            "- Whether the price is good value\n"
            "- Layover airports if this is a connecting flight"
        )

    # ── RESULTS (default) ─────────────────────────────────────────────────────
    flights    = context.get("flights", [])
    focused    = context.get("focused_flight")
    focus_mode = context.get("focus_mode", "none")

    parts = []
    if origin and destination:
        parts.append(f"from {origin} to {destination}")
    if depart and ret:
        parts.append(f"departing {depart}, returning {ret}")
    elif depart:
        parts.append(f"departing {depart}")
    if cabin:
        parts.append(f"{cabin} class")
    if passengers:
        parts.append(f"{passengers} traveler{'s' if int(passengers) != 1 else ''}")
    parts.append("round trip" if trip_type != "oneway" else "one-way")
    search_ctx = "The user is searching for a flight " + ", ".join(parts) + "." if parts else "The user is browsing flights."

    flights_section = ""
    if flights:
        lines = [_fmt_flight_line(f, i + 1) for i, f in enumerate(flights[:12])]
        flights_section = (
            "\n\nFLIGHTS CURRENTLY SHOWN ON THE RESULTS PAGE:\n"
            + "\n".join(lines)
            + "\n\nOnly reference these flights when discussing options. "
            "If asked which is cheapest/fastest/best, use the actual data above."
        )

    focused_section = ""
    if focused:
        f = focused
        focused_line = _fmt_flight_line(f, f.get("rank", 1))
        if focus_mode == "selected":
            focused_section = (
                f"\n\nSELECTED FLIGHT — the user has chosen this flight:\n"
                f"{focused_line}\n"
                f"Provide detailed expert information: baggage policy for {f.get('airline','this airline')}, "
                f"what to expect at the airports, check-in timing, seat recommendations, "
                f"layover tips if applicable. Do not bring up other flights unless asked."
            )
        else:
            focused_section = (
                f"\n\nFLIGHT BEING VIEWED — the user expanded this flight:\n"
                f"{focused_line}\n"
                f"Prioritise answering questions about this specific flight. "
                f"You may reference other results for comparison if helpful."
            )

    return (
        f"{BASE}\n\n"
        f"CURRENT PAGE: Flight search results.\n"
        f"{search_ctx}"
        f"{flights_section}"
        f"{focused_section}"
        "\n\nNever invent flight data, prices, or schedules not listed above."
    )


def _build_ai_insight_prompt(context: dict) -> str:
    origin = context.get("origin", "")
    destination = context.get("destination", "")
    depart = context.get("depart_date", "")
    ret = context.get("return_date", "")
    cabin = context.get("cabin", "ECONOMY").replace("_", " ").title()
    passengers = int(context.get("passengers", 1))
    cheapest_price = context.get("cheapest_price")
    cheapest_date = context.get("cheapest_date")
    best_airline = context.get("best_airline")
    fastest_duration_min = context.get("fastest_duration_min")
    total_results = context.get("total_results", 0)
    flex_month = context.get("flex_month")
    trip_type = context.get("trip_type", "roundtrip")

    price_str = f"${cheapest_price:.0f}" if cheapest_price else "unknown"
    dur_str = ""
    if fastest_duration_min:
        h = int(fastest_duration_min) // 60
        m = int(fastest_duration_min) % 60
        dur_str = f"{h}h {m}m" if m else f"{h}h"

    prompt = (
        f"You are a flight booking expert AI for Skairova. "
        f"A flex search just completed for a {trip_type} flight "
        f"from {origin or 'the origin'} to {destination or 'the destination'}"
    )
    if flex_month:
        prompt += f" in {flex_month}"
    if depart and ret:
        prompt += f" (departing {depart}, returning {ret})"
    elif depart:
        prompt += f" (departing {depart})"
    prompt += f", {cabin} class, {passengers} passenger{'s' if passengers != 1 else ''}. "
    prompt += f"We found {total_results} flight options. "
    if cheapest_price:
        prompt += f"The cheapest fare is {price_str} per person"
        if cheapest_date:
            prompt += f" on {cheapest_date}"
        prompt += ". "
    if best_airline:
        prompt += f"The top-ranked airline is {best_airline}. "
    if dur_str:
        prompt += f"The fastest available option takes {dur_str}. "
    prompt += (
        "Return EXACTLY two lines separated by a single newline character. "
        "Line 1 (headline): one short punchy sentence with the key numbers — price, airline, date. Lead with the best fact. "
        "Line 2 (detail): one plain sentence — a practical tip, observation, or why this deal is good. "
        "Be warm and expert. No markdown, no bullet points, no extra lines, plain text only."
    )
    return prompt


@app.route("/api/ai/insight", methods=["POST"])
def ai_insight():
    if not model:
        return jsonify({"error": "AI not configured"}), 503
    try:
        data = request.get_json(silent=True) or {}
        prompt = _build_ai_insight_prompt(data)
        result = model.generate_content(prompt)
        text = ""
        if hasattr(result, "text"):
            text = str(result.text or "").strip()
        elif hasattr(result, "candidates") and result.candidates:
            c = result.candidates[0]
            if hasattr(c, "content") and c.content:
                for part in getattr(c.content, "parts", []):
                    text += getattr(part, "text", "")
            text = text.strip()
        if not text:
            return jsonify({"error": "No response"}), 500
        return jsonify({"insight": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai/chat", methods=["POST"])
def ai_chat():
    if not model:
        return jsonify({"error": "AI not configured"}), 503
    try:
        data = request.get_json(silent=True) or {}
        user_message = str(data.get("message", "")).strip()[:500]
        if not user_message:
            return jsonify({"error": "Empty message"}), 400
        context = data.get("context", {})
        history = data.get("history", [])

        system_prompt = _build_ai_chat_system(context)

        convo_parts = [f"System: {system_prompt}\n"]
        for h in history[-8:]:
            role = h.get("role", "user")
            msg = str(h.get("content", "")).strip()[:300]
            if role == "user":
                convo_parts.append(f"User: {msg}")
            else:
                convo_parts.append(f"Assistant: {msg}")
        convo_parts.append(f"User: {user_message}")
        convo_parts.append("Assistant:")
        full_prompt = "\n".join(convo_parts)

        result = model.generate_content(full_prompt)
        text = ""
        if hasattr(result, "text"):
            text = str(result.text or "").strip()
        elif hasattr(result, "candidates") and result.candidates:
            c = result.candidates[0]
            if hasattr(c, "content") and c.content:
                for part in getattr(c.content, "parts", []):
                    text += getattr(part, "text", "")
            text = text.strip()
        if not text:
            return jsonify({"error": "No response"}), 500
        return jsonify({"reply": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ai/filter", methods=["POST"])
def ai_filter():
    if not model:
        return jsonify({"error": "AI not configured"}), 503
    try:
        data = request.get_json(silent=True) or {}
        user_text = str(data.get("query", "")).strip()[:300]
        if not user_text:
            return jsonify({"error": "Empty query"}), 400

        prompt = (
            "You are a flight filter parser. Given a natural language filter request for flights, "
            "output a JSON object (no markdown, no explanation) with any of these fields that apply:\n"
            "- max_price: number (maximum price in USD)\n"
            "- min_price: number (minimum price)\n"
            "- max_stops: number (0 = nonstop only, 1 = max 1 stop, 2 = 2+ stops allowed)\n"
            "- departure_times: array of strings from [\"morning\", \"afternoon\", \"evening\", \"night\"]\n"
            "- arrival_times: array of strings from [\"morning\", \"afternoon\", \"evening\", \"night\"]\n"
            "- max_duration_min: number (max total trip duration in minutes)\n"
            "- airlines: array of airline name keywords (lowercase)\n"
            "- sort: one of \"cheapest\", \"fastest\", \"best\"\n"
            "Only include fields that the user mentioned. "
            "Time of day ranges: morning=5am-12pm, afternoon=12pm-5pm, evening=5pm-9pm, night=9pm-5am.\n"
            f"User filter request: \"{user_text}\"\n"
            "JSON output:"
        )
        result = model.generate_content(prompt)
        text = ""
        if hasattr(result, "text"):
            text = str(result.text or "").strip()
        elif hasattr(result, "candidates") and result.candidates:
            c = result.candidates[0]
            if hasattr(c, "content") and c.content:
                for part in getattr(c.content, "parts", []):
                    text += getattr(part, "text", "")
            text = text.strip()

        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text.strip())
        parsed = json.loads(text)
        return jsonify({"filters": parsed})
    except Exception as e:
        return jsonify({"error": str(e), "filters": {}}), 200


# ---------------------------------------------------------------------------
# Voice AI  (/voice/*) — session-token minting for the Deepgram streaming proxy
# ---------------------------------------------------------------------------

VOICE_TOKEN_ATTEMPT_CACHE = TTLCache(maxsize=4096, ttl_seconds=60)
VOICE_TOKEN_MAX_PER_MINUTE = 20  # one per listening attempt; generous but bounded


def _voice_token_rate_key(ip: str) -> str:
    return f"voicetoken:{(ip or 'unknown').strip()}"


@app.route("/voice/session-token", methods=["POST"])
def voice_session_token():
    """Mint a short-lived JWT the browser hands to the voice proxy's WebSocket.

    The token carries no Deepgram credentials — it only proves to the proxy
    that this connection was authorized by Flask a few seconds ago. The
    proxy (voice_service/main.py) verifies it with the same VOICE_PROXY_SECRET
    and rejects anything expired, forged, or missing the expected claims.
    """
    if not VOICE_AI_ENABLED:
        return jsonify({"ok": False, "error": "voice_unavailable"}), 503

    caller_ip = _b2c_client_ip()
    rate_key = _voice_token_rate_key(caller_ip)
    attempts = int(VOICE_TOKEN_ATTEMPT_CACHE.get(rate_key) or 0)
    if attempts >= VOICE_TOKEN_MAX_PER_MINUTE:
        return jsonify({"ok": False, "error": "rate_limited"}), 429
    VOICE_TOKEN_ATTEMPT_CACHE.set(rate_key, attempts + 1)

    now = int(time.time())
    session_id = secrets.token_urlsafe(16)
    claims = {
        "aud": "voice-proxy",
        "sid": session_id,
        "iat": now,
        "exp": now + VOICE_SESSION_TOKEN_TTL_SECONDS,
    }
    token = pyjwt.encode(claims, VOICE_PROXY_SECRET, algorithm="HS256")

    _track_analytics_event(
        event_type="voice_session_started",
        search_mode="voice",
        success=True,
        metadata={"session_id": session_id},
    )

    return jsonify(
        {
            "ok": True,
            "token": token,
            "ws_url": VOICE_PROXY_WS_URL,
            "expires_in": VOICE_SESSION_TOKEN_TTL_SECONDS,
            "session_id": session_id,
        }
    )


# ---------------------------------------------------------------------------
# Mobile API  (/api/mobile/*)
# ---------------------------------------------------------------------------

@app.route("/api/mobile/search", methods=["POST"])
def mobile_search():
    """JSON flight search endpoint for the Skairova mobile app.

    Accepts JSON body matching the same field names as the web search form,
    returns a JSON array of decorated flight objects (same shape as the web
    results page uses internally).
    """
    data = request.get_json(silent=True) or {}

    params: dict[str, Any] = {
        "origin": str(data.get("origin", "")).strip().upper(),
        "destination": str(data.get("destination", "")).strip().upper(),
        "trip_type": str(data.get("trip_type", "roundtrip")).strip().lower(),
        "depart_date": (str(data.get("depart_date", "")).strip() or None),
        "return_date": (str(data.get("return_date", "")).strip() or None),
        "passengers": str(data.get("passengers", str(DEFAULT_PASSENGERS))),
        "cabin": str(data.get("cabin", "ECONOMY")).strip().upper(),
        "nonstop": bool(data.get("nonstop", False)),
        "sort": str(data.get("sort", "recommended")).strip().lower(),
        "combination_mode": "auto",
        "raw_text": "",
    }

    params, error = _validate_standard_search_params(params)
    if error:
        return jsonify({"error": error, "flights": []}), 400

    try:
        flights = search_flights(params)
    except Exception as exc:
        return jsonify({"error": str(exc), "flights": []}), 502

    safe_flights = []
    for f in flights:
        try:
            safe_flights.append(json.loads(json.dumps(f, default=str)))
        except Exception:
            pass

    return jsonify({
        "flights": safe_flights,
        "count": len(safe_flights),
        "query": {
            "origin": params.get("origin"),
            "destination": params.get("destination"),
            "trip_type": params.get("trip_type"),
            "depart_date": params.get("depart_date"),
            "return_date": params.get("return_date"),
            "passengers": params.get("passengers"),
            "cabin": params.get("cabin"),
            "nonstop": params.get("nonstop"),
            "sort": params.get("sort"),
        },
    })


@app.route("/api/mobile/airports", methods=["GET"])
def mobile_airports():
    """Airport autocomplete — thin alias for /airports with CORS-friendly response."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    return airports()


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1", port=int(os.getenv("PORT", 5055)))
