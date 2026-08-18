"""LiteAPI (Nuitée) hotel search and booking client.

Flow mirrors the Duffel air flow in duffel_booking.py: search -> prebook ->
book, where prebook plays the same role as a Duffel quote (revalidates
availability and locks price before we charge).

Two quirks of the upstream API drive the design here, both discovered by
probing the live sandbox:

1. `cityName` search on /hotels/rates silently returns zero for many major
   cities (Dubai, London, Paris, Bangkok, Istanbul all returned 0) while the
   exact same properties return live rates when asked for by `hotelIds`. So we
   always resolve a destination to hotel IDs from the static content DB first,
   then ask for rates by ID.
2. /hotels/rates fast-fails to an empty `data` array — HTTP 200, no error —
   when given more than ~50 hotel IDs. RATES_BATCH_SIZE must stay at 50.
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from typing import Any, Iterable, Mapping, Sequence

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

# app.py imports this module before it calls load_dotenv(), so load here too —
# it is idempotent and keeps the config below from reading an empty environment.
load_dotenv()

LITE_API_KEY = os.getenv("LITE_API_KEY", "").strip()
LITE_DATA_BASE = os.getenv("LITE_DATA_BASE", "https://api.liteapi.travel/v3.0").strip() or "https://api.liteapi.travel/v3.0"
LITE_BOOK_BASE = os.getenv("LITE_BOOK_BASE", "https://book.liteapi.travel/v3.0").strip() or "https://book.liteapi.travel/v3.0"
LITE_ENV = "sandbox" if LITE_API_KEY.startswith("sand") else ("live" if LITE_API_KEY else "unconfigured")
# White-label domain the official search-bar widget authenticates against.
LITE_WHITELABEL_DOMAIN = os.getenv("LITE_WHITELABEL_DOMAIN", "mahisyed-mahee-7kufz.nuitee.link").strip()
LITE_ENABLED = bool(LITE_API_KEY)

LITE_HTTP_TIMEOUT = float(os.getenv("LITE_HTTP_TIMEOUT", "45"))
LITE_CONTENT_TIMEOUT = float(os.getenv("LITE_CONTENT_TIMEOUT", "20"))
LITE_DEFAULT_CURRENCY = os.getenv("LITE_DEFAULT_CURRENCY", "USD").strip().upper() or "USD"
LITE_DEFAULT_NATIONALITY = os.getenv("LITE_DEFAULT_NATIONALITY", "US").strip().upper() or "US"

# Hard upstream ceiling — 75+ IDs returns an empty array with HTTP 200.
RATES_BATCH_SIZE = 50
# How many properties we price per search. Each batch is a round trip, so this
# trades breadth against latency.
RATES_MAX_HOTELS = int(os.getenv("LITE_RATES_MAX_HOTELS", "150"))
RATES_WORKERS = int(os.getenv("LITE_RATES_WORKERS", "4"))
# Sandbox is capped at 5 rps; production allows far more.
RATES_MIN_INTERVAL = float(os.getenv("LITE_RATES_MIN_INTERVAL", "0.25"))
# A rate quote is live data, but a visitor can easily trigger the identical
# lookup more than once (initial rails, search retries, or two browser tabs).
# Keep those identical responses briefly so we do not turn a single action into
# several supplier calls.  The cache is deliberately short lived because offer
# IDs and availability can change quickly; checkout still prebooks/revalidates
# every selected offer.
RATES_CACHE_TTL = float(os.getenv("LITE_RATES_CACHE_TTL", "90"))
RATES_CACHE_MAXSIZE = int(os.getenv("LITE_RATES_CACHE_MAXSIZE", "256"))
RATES_CACHE_MAX_INFLIGHT = int(os.getenv("LITE_RATES_CACHE_MAX_INFLIGHT", "32"))

CONTENT_CACHE_TTL = float(os.getenv("LITE_CONTENT_CACHE_TTL", "86400"))

REFUNDABLE_TAGS = {"RFN", "REF", "FC"}

# Google Places types worth offering as a stay destination. Anything else
# (stores, restaurants, businesses) is dropped from autocomplete.
PLACE_TYPES_PRIMARY = {
    "locality", "postal_town", "administrative_area_level_1",
    "administrative_area_level_2", "country", "neighborhood", "sublocality",
}
# Deliberately excludes "point_of_interest", "establishment" and "geocode" —
# Google tags every shop and restaurant with those, so allowing them lets
# "Dubai Chocolate-My Chocolate" outrank Dubai.
PLACE_TYPES_ALLOWED = PLACE_TYPES_PRIMARY | {
    "airport", "international_airport", "tourist_attraction",
    "natural_feature", "train_station", "transit_station",
    "administrative_area_level_3", "colloquial_area",
}


class LiteAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.4,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "PUT"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=32)
    session.mount("https://", adapter)
    session.headers.update({"Accept": "application/json"})
    return session


class _RateLimiter:
    """Spaces outbound calls so the sandbox's 5 rps cap doesn't eat results."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            self._last = time.monotonic()


class _SingleFlight:
    """One in-progress computation for a cache key."""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.error: BaseException | None = None


class SingleFlightTTLCache:
    """Small process-local TTL cache that coalesces identical work.

    Unlike a simple get-or-set, concurrent misses for one key wait for the
    first caller instead of each issuing a supplier request.  Entries and
    in-flight markers are both bounded so untrusted search inputs cannot grow
    process memory without limit.
    """

    def __init__(self, *, maxsize: int = 256, ttl_seconds: float = 90, max_inflight: int = 32) -> None:
        self.maxsize = max(1, int(maxsize))
        self.ttl = max(0.0, float(ttl_seconds))
        self.max_inflight = max(1, int(max_inflight))
        self._lock = threading.RLock()
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._inflight: dict[Any, _SingleFlight] = {}

    def _get_locked(self, key: Any, now: float) -> tuple[bool, Any]:
        item = self._data.get(key)
        if item is None:
            return False, None
        expires_at, value = item
        if expires_at <= now:
            self._data.pop(key, None)
            return False, None
        self._data.move_to_end(key)
        return True, value

    def _store_locked(self, key: Any, value: Any, now: float) -> None:
        self._data[key] = (now + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def get_or_build(self, key: Any, builder):
        """Return a fresh cached value or make exactly one caller build it."""
        while True:
            with self._lock:
                found, value = self._get_locked(key, time.monotonic())
                if found:
                    return value

                flight = self._inflight.get(key)
                is_builder = False
                untracked = False
                if flight is None:
                    # If all tracked flights are occupied, preserve the memory
                    # bound and run this uncommon key without coalescing it.
                    if len(self._inflight) >= self.max_inflight:
                        untracked = True
                    else:
                        flight = _SingleFlight()
                        self._inflight[key] = flight
                        is_builder = True

                if is_builder:
                    break

            if untracked:
                return builder()

            # A concurrent caller waits outside the lock. If the first caller
            # failed, expose that same error rather than stampeding the API.
            flight.ready.wait()
            if flight.error is not None:
                raise flight.error

        try:
            value = builder()
        except BaseException as exc:
            with self._lock:
                self._inflight.pop(key, None)
                flight.error = exc
                flight.ready.set()
            raise

        with self._lock:
            self._store_locked(key, value, time.monotonic())
            self._inflight.pop(key, None)
            flight.ready.set()
        return value


class LiteAPIClient:
    def __init__(self):
        self.session = _build_session()
        self._limiter = _RateLimiter(RATES_MIN_INTERVAL)
        self._content_cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        self._rates_cache = SingleFlightTTLCache(
            maxsize=RATES_CACHE_MAXSIZE,
            ttl_seconds=RATES_CACHE_TTL,
            max_inflight=RATES_CACHE_MAX_INFLIGHT,
        )

    # -- transport -------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": LITE_API_KEY,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        base: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        timeout: float = LITE_HTTP_TIMEOUT,
    ) -> dict[str, Any]:
        if not LITE_API_KEY:
            raise LiteAPIError("LiteAPI is not configured. Set LITE_API_KEY.")

        self._limiter.wait()
        try:
            resp = self.session.request(
                method=method,
                url=f"{base}{path}",
                params=params,
                json=json_body,
                headers=self._headers(),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            print("LITEAPI EXCEPTION:", path, repr(exc))
            raise LiteAPIError("We couldn't reach our accommodation provider.")

        if not resp.ok:
            print("LITEAPI STATUS:", path, resp.status_code)
            print("LITEAPI BODY:", resp.text[:600])
            raise LiteAPIError(self._error_message(resp), status_code=resp.status_code)

        try:
            return resp.json() or {}
        except ValueError:
            raise LiteAPIError("Our accommodation provider returned an unexpected response.")

    def _error_message(self, resp: requests.Response) -> str:
        try:
            payload = resp.json()
        except ValueError:
            return "We couldn't load accommodation right now."

        error = payload.get("error")
        if isinstance(error, Mapping):
            detail = str(error.get("message") or error.get("description") or "").strip()
            if detail:
                return detail
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "We couldn't load accommodation right now."

    # -- cache -----------------------------------------------------
    def _cached(self, key: str, producer, ttl: float = CONTENT_CACHE_TTL):
        now = time.monotonic()
        with self._cache_lock:
            hit = self._content_cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = producer()
        with self._cache_lock:
            self._content_cache[key] = (now, value)
        return value

    # -- content ---------------------------------------------------
    def search_places(self, query: str) -> list[dict[str, str]]:
        """Autocomplete destinations. Backed by Google Places upstream, which
        also returns shops and businesses — we keep only place types you could
        plausibly search for a hotel around, cities first."""
        query = (query or "").strip()
        if len(query) < 2:
            return []

        def load():
            payload = self._request(
                "GET", LITE_DATA_BASE, "/data/places",
                params={"textQuery": query}, timeout=LITE_CONTENT_TIMEOUT,
            )
            out = []
            for row in payload.get("data") or []:
                name = str(row.get("displayName") or "").strip()
                types = {str(t).strip().lower() for t in row.get("types") or []}
                if not name or not (types & PLACE_TYPES_ALLOWED):
                    continue
                out.append({
                    "place_id": str(row.get("placeId") or "").strip(),
                    "name": name,
                    "address": str(row.get("formattedAddress") or "").strip(),
                    "types": sorted(types),
                    "_rank": 0 if types & PLACE_TYPES_PRIMARY else 1,
                })
            out.sort(key=lambda row: row["_rank"])
            for row in out:
                row.pop("_rank", None)
            return out

        return self._cached(f"places:{query.lower()}", load, ttl=3600)

    def _hotel_content(self, lookup: Mapping[str, Any], cache_key: str, limit: int) -> list[dict[str, Any]]:
        """Page the static content DB. `lookup` must carry one of the accepted
        location keys — placeId, countryCode(+cityName), or latitude/longitude."""

        def load():
            collected: list[dict[str, Any]] = []
            offset = 0
            while len(collected) < limit:
                payload = self._request(
                    "GET", LITE_DATA_BASE, "/data/hotels",
                    params={**lookup, "limit": min(200, limit - len(collected)), "offset": offset},
                    timeout=LITE_CONTENT_TIMEOUT,
                )
                page = payload.get("data") or []
                if not page:
                    break
                collected.extend(page)
                offset += len(page)
                if len(page) < 200:
                    break
            return collected[:limit]

        return self._cached(cache_key, load)

    def hotels_for_place(self, place_id: str, *, limit: int = RATES_MAX_HOTELS) -> list[dict[str, Any]]:
        """Static content for an autocomplete placeId — the primary resolver."""
        place_id = (place_id or "").strip()
        if not place_id:
            return []
        return self._hotel_content({"placeId": place_id}, f"hotels:place:{place_id}:{limit}", limit)

    def facility_names(self) -> dict[int, str]:
        """facility_id -> English name, for turning card facilityIds into chips."""

        def load():
            payload = self._request(
                "GET", LITE_DATA_BASE, "/data/facilities", timeout=LITE_CONTENT_TIMEOUT,
            )
            names: dict[int, str] = {}
            for row in payload.get("data") or []:
                try:
                    fid = int(row.get("facility_id"))
                except (TypeError, ValueError):
                    continue
                label = _clean_text(row.get("facility"))
                if label:
                    names[fid] = label
            return names

        try:
            return self._cached("facilities", load)
        except LiteAPIError as exc:
            print("LITEAPI FACILITIES FAILED:", exc)
            return {}

    def hotel_detail(self, hotel_id: str) -> dict[str, Any]:
        """Full static record for one property."""
        hotel_id = (hotel_id or "").strip()
        if not hotel_id:
            return {}

        def load():
            payload = self._request(
                "GET", LITE_DATA_BASE, "/data/hotel",
                params={"hotelId": hotel_id}, timeout=LITE_CONTENT_TIMEOUT,
            )
            return payload.get("data") or {}

        return self._cached(f"hotel:{hotel_id}", load)

    def hotels_for_coordinates(self, latitude: float, longitude: float, *, radius: int = 15000, limit: int = RATES_MAX_HOTELS) -> list[dict[str, Any]]:
        return self._hotel_content(
            {"latitude": latitude, "longitude": longitude, "radius": radius},
            f"hotels:geo:{latitude:.4f}:{longitude:.4f}:{radius}:{limit}",
            limit,
        )

    def hotels_for_city(self, city: str, country_code: str, *, limit: int = RATES_MAX_HOTELS) -> list[dict[str, Any]]:
        """Fallback resolver. Requires a country code — city name alone 400s."""
        city = (city or "").strip()
        country_code = (country_code or "").strip().upper()
        if not city or not country_code:
            return []
        return self._hotel_content(
            {"countryCode": country_code, "cityName": city},
            f"hotels:city:{country_code}:{city.lower()}:{limit}",
            limit,
        )

    # -- rates -----------------------------------------------------
    def _rates_batch(self, hotel_ids: Sequence[str], payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        body = dict(payload)
        body["hotelIds"] = list(hotel_ids)
        try:
            data = self._request("POST", LITE_BOOK_BASE, "/hotels/rates", json_body=body)
        except LiteAPIError as exc:
            print("LITEAPI RATES BATCH FAILED:", len(hotel_ids), exc)
            return []
        return data.get("data") or []

    def search_rates(
        self,
        *,
        hotel_ids: Sequence[str],
        checkin: str,
        checkout: str,
        adults: int = 2,
        children_ages: Sequence[int] | None = None,
        rooms: int = 1,
        currency: str = LITE_DEFAULT_CURRENCY,
        nationality: str = LITE_DEFAULT_NATIONALITY,
    ) -> list[dict[str, Any]]:
        """Price a set of hotels. Batched at 50 IDs and fanned out in parallel."""
        # Preserve a deterministic list while dropping duplicate IDs. The
        # resulting key is identical for duplicate homepage/route refreshes.
        ids = list(dict.fromkeys(str(h).strip() for h in hotel_ids if str(h or "").strip()))
        if not ids:
            return []

        occupancy: dict[str, Any] = {"adults": max(1, int(adults))}
        if children_ages:
            occupancy["children"] = [int(age) for age in children_ages]

        payload = {
            "checkin": checkin,
            "checkout": checkout,
            "currency": (currency or LITE_DEFAULT_CURRENCY).upper(),
            "guestNationality": (nationality or LITE_DEFAULT_NATIONALITY).upper(),
            "occupancies": [dict(occupancy) for _ in range(max(1, int(rooms)))],
        }
        # Do not sort `ids`: Lite may return offers in supplier order, while
        # the ordered tuple is still stable for all content-backed calls.
        cache_key = (
            "rates",
            tuple(ids),
            str(checkin),
            str(checkout),
            int(adults),
            tuple(int(age) for age in (children_ages or [])),
            int(rooms),
            str(currency or LITE_DEFAULT_CURRENCY).upper(),
            str(nationality or LITE_DEFAULT_NATIONALITY).upper(),
        )

        def load() -> list[dict[str, Any]]:
            batches = [ids[i:i + RATES_BATCH_SIZE] for i in range(0, len(ids), RATES_BATCH_SIZE)]
            merged: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=min(RATES_WORKERS, len(batches))) as pool:
                futures = [pool.submit(self._rates_batch, batch, payload) for batch in batches]
                for future in as_completed(futures):
                    try:
                        merged.extend(future.result())
                    except Exception as exc:
                        print("LITEAPI RATES FANOUT ERROR:", repr(exc))
            return merged

        return self._rates_cache.get_or_build(cache_key, load)

    # -- booking ---------------------------------------------------
    def prebook(self, offer_id: str, *, use_payment_sdk: bool = False) -> dict[str, Any]:
        """Revalidate a rate and lock its price. Duffel-quote equivalent."""
        payload = self._request(
            "POST", LITE_BOOK_BASE, "/rates/prebook",
            json_body={"offerId": offer_id, "usePaymentSdk": bool(use_payment_sdk)},
        )
        data = payload.get("data") or {}
        if not data.get("prebookId"):
            raise LiteAPIError("That room is no longer available. Please pick another rate.")
        return data

    def book(
        self,
        *,
        prebook_id: str,
        holder: Mapping[str, Any],
        guests: Sequence[Mapping[str, Any]],
        payment: Mapping[str, Any],
        client_reference: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "prebookId": prebook_id,
            "holder": dict(holder),
            "guests": [dict(g) for g in guests],
            "payment": dict(payment),
        }
        if client_reference:
            # LiteAPI's own idempotency key — a resubmitted booking with the
            # same clientReference is deduped provider-side rather than
            # creating a second booking.
            body["clientReference"] = client_reference
        payload = self._request("POST", LITE_BOOK_BASE, "/rates/book", json_body=body)
        return payload.get("data") or {}

    def get_booking(self, booking_id: str) -> dict[str, Any]:
        payload = self._request("GET", LITE_BOOK_BASE, f"/bookings/{urllib.parse.quote(booking_id)}")
        return payload.get("data") or {}

    def cancel_booking(self, booking_id: str) -> dict[str, Any]:
        payload = self._request("PUT", LITE_BOOK_BASE, f"/bookings/{urllib.parse.quote(booking_id)}")
        return payload.get("data") or {}


# ------------------------------------------------------------
# View models
# ------------------------------------------------------------
_ALLOWED_DESC_TAGS = {"p", "br", "b", "strong", "i", "em", "ul", "ol", "li", "h3", "h4"}


def sanitize_description(html: Any, *, max_chars: int = 4000) -> str:
    """Supplier descriptions arrive as raw HTML from third-party content feeds.

    We never render that unescaped. Everything is escaped first, then a narrow
    allowlist of formatting tags is restored — no attributes survive, so there
    is no route for href/src/on* payloads.
    """
    raw = str(html or "").strip()
    if not raw:
        return ""

    escaped = escape(raw[:max_chars])

    def restore(match: re.Match[str]) -> str:
        tag = match.group(1).lower()
        if tag not in _ALLOWED_DESC_TAGS:
            return ""
        closing = "/" if match.group(0).startswith("&lt;/") else ""
        return f"<{closing}{tag}>"

    return re.sub(r"&lt;\s*/?\s*([a-zA-Z0-9]+)[^&]*?&gt;", restore, escaped)


# The content feed uses these as placeholders rather than omitting the field.
_NULLISH_TEXT = {"", "n/a", "na", "none", "null", "not available", "unknown", "-"}


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in _NULLISH_TEXT else text


def _money(value: Any) -> float | None:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _rate_total(rate: Mapping[str, Any]) -> tuple[float | None, str]:
    retail = rate.get("retailRate") or {}
    total = retail.get("total")
    if isinstance(total, list) and total:
        first = total[0] or {}
        return _money(first.get("amount")), str(first.get("currency") or "").upper()
    if isinstance(total, Mapping):
        return _money(total.get("amount")), str(total.get("currency") or "").upper()
    return None, ""


def _is_refundable(rate: Mapping[str, Any]) -> bool:
    policies = rate.get("cancellationPolicies") or {}
    tag = str(policies.get("refundableTag") or "").strip().upper()
    return tag in REFUNDABLE_TAGS


def summarize_offers(room_types: Iterable[Mapping[str, Any]], *, nights: int) -> dict[str, Any] | None:
    """Collapse a hotel's room types (there can be 200) into a headline offer."""
    best: dict[str, Any] | None = None
    refundable_available = False
    board_types: set[str] = set()

    for room_type in room_types or []:
        for rate in room_type.get("rates") or []:
            amount, currency = _rate_total(rate)
            if amount is None:
                continue
            refundable = _is_refundable(rate)
            refundable_available = refundable_available or refundable
            board = str(rate.get("boardName") or "").strip()
            if board:
                board_types.add(board)
            if best is None or amount < best["total_amount"]:
                retail = rate.get("retailRate") or {}
                suggested = retail.get("suggestedSellingPrice")
                was_amount = None
                if isinstance(suggested, list) and suggested:
                    was_amount = _money((suggested[0] or {}).get("amount"))
                best = {
                    "offer_id": str(room_type.get("offerId") or "").strip(),
                    "rate_id": str(rate.get("rateId") or "").strip(),
                    "room_name": str(rate.get("name") or "Standard room").strip(),
                    "board_name": board or "Room only",
                    "refundable": refundable,
                    "total_amount": amount,
                    "was_amount": was_amount if was_amount and was_amount > amount else None,
                    "currency": currency or LITE_DEFAULT_CURRENCY,
                    "max_occupancy": rate.get("maxOccupancy"),
                    "supplier": str(room_type.get("supplier") or "").strip(),
                }

    if not best:
        return None

    best["nightly_amount"] = round(best["total_amount"] / nights, 2) if nights > 0 else None
    best["refundable_available"] = refundable_available
    best["board_options"] = sorted(board_types)[:4]
    best["has_breakfast"] = any("breakfast" in b.lower() for b in board_types)
    if best["was_amount"]:
        best["savings_percent"] = int(round((1 - best["total_amount"] / best["was_amount"]) * 100))
    else:
        best["savings_percent"] = 0
    return best


def review_label(rating: float | None) -> str:
    """Guest-score wording, matching the convention OTAs use on result cards."""
    if not rating:
        return ""
    if rating >= 9:
        return "Exceptional"
    if rating >= 8:
        return "Excellent"
    if rating >= 7:
        return "Very good"
    if rating >= 6:
        return "Good"
    return "Pleasant"


# Amenities travellers actually filter on, in the order we surface them.
FILTERABLE_AMENITIES: list[tuple[str, tuple[str, ...]]] = [
    ("Free WiFi", ("free wifi", "free internet", "wifi")),
    ("Pool", ("swimming pool", "outdoor pool", "indoor pool", "pool")),
    ("Breakfast", ("breakfast",)),
    ("Parking", ("parking",)),
    ("Gym", ("fitness", "gym", "health club")),
    ("Spa", ("spa", "sauna", "massage")),
    ("Restaurant", ("restaurant", "dining")),
    ("Airport shuttle", ("airport transportation", "airport shuttle", "shuttle")),
    ("Air conditioning", ("air conditioning", "air-conditioning")),
    ("Pet friendly", ("pet", "pets allowed")),
    ("Family rooms", ("family room", "childcare", "children")),
    ("Bar", ("bar", "lounge")),
]


def match_amenities(raw_names: Iterable[str]) -> list[str]:
    """Collapse a property's raw facility list onto our filter vocabulary."""
    haystack = " | ".join(str(n or "").lower() for n in raw_names)
    if not haystack:
        return []
    return [label for label, needles in FILTERABLE_AMENITIES if any(n in haystack for n in needles)]


def build_hotel_cards(
    rate_rows: Sequence[Mapping[str, Any]],
    content_by_id: Mapping[str, Mapping[str, Any]],
    *,
    nights: int,
    facility_names: Mapping[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Join priced rates onto static content and shape them for the results grid."""
    facility_names = facility_names or {}
    cards: list[dict[str, Any]] = []
    for row in rate_rows:
        hotel_id = str(row.get("hotelId") or "").strip()
        content = content_by_id.get(hotel_id) or {}
        offer = summarize_offers(row.get("roomTypes") or [], nights=nights)
        if not offer or not content:
            continue

        stars = _money(content.get("stars")) or _money(content.get("starRating")) or 0
        rating = _money(content.get("rating")) or 0

        raw_facilities = [
            facility_names.get(int(fid))
            for fid in content.get("facilityIds") or []
            if str(fid).lstrip("-").isdigit() and facility_names.get(int(fid))
        ]
        amenities = match_amenities(raw_facilities)

        cards.append({
            "amenities": amenities,
            "review_label": review_label(rating),
            "hotel_id": hotel_id,
            "name": _clean_text(content.get("name")) or "Hotel",
            "address": _clean_text(content.get("address")),
            "city": _clean_text(content.get("city")),
            "country": _clean_text(content.get("country")).upper(),
            "thumbnail": _clean_text(content.get("thumbnail")) or _clean_text(content.get("main_photo")),
            "photo": _clean_text(content.get("main_photo")) or _clean_text(content.get("thumbnail")),
            "stars": int(stars) if stars else 0,
            "rating": round(rating, 1) if rating else None,
            "review_count": int(_money(content.get("reviewCount")) or 0),
            "chain": _clean_text(content.get("chain")),
            "latitude": content.get("latitude"),
            "longitude": content.get("longitude"),
            "offer": offer,
            "room_type_count": len(row.get("roomTypes") or []),
        })

    cards.sort(key=lambda card: card["offer"]["total_amount"])
    return cards


def build_rooms_view(
    rate_row: Mapping[str, Any],
    *,
    nights: int,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Every bookable rate for one hotel, cheapest first, deduped by room+board."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, bool]] = set()

    for room_type in rate_row.get("roomTypes") or []:
        offer_id = str(room_type.get("offerId") or "").strip()
        for rate in room_type.get("rates") or []:
            amount, currency = _rate_total(rate)
            if amount is None:
                continue
            name = str(rate.get("name") or "Standard room").strip()
            board = str(rate.get("boardName") or "Room only").strip()
            refundable = _is_refundable(rate)
            key = (name.lower(), board.lower(), refundable)
            if key in seen:
                continue
            seen.add(key)

            taxes = 0.0
            for tax in (rate.get("retailRate") or {}).get("taxesAndFees") or []:
                if not tax.get("included"):
                    taxes += _money(tax.get("amount")) or 0.0

            rows.append({
                "offer_id": offer_id,
                "rate_id": str(rate.get("rateId") or "").strip(),
                "name": name,
                "board_name": board,
                "board_type": str(rate.get("boardType") or "").strip(),
                "refundable": refundable,
                "max_occupancy": rate.get("maxOccupancy"),
                "adult_count": rate.get("adultCount"),
                "total_amount": amount,
                "nightly_amount": round(amount / nights, 2) if nights > 0 else None,
                "currency": currency or LITE_DEFAULT_CURRENCY,
                "excluded_taxes": round(taxes, 2) if taxes else None,
                "remarks": str(rate.get("remarks") or "").strip(),
            })

    rows.sort(key=lambda row: row["total_amount"])
    return rows[:limit]


def build_detail_view(content: Mapping[str, Any]) -> dict[str, Any]:
    """Property page model: gallery, guest score, amenity groups, policies, POIs."""
    images: list[str] = []
    for img in content.get("hotelImages") or []:
        url = _clean_text(img.get("url") if isinstance(img, Mapping) else img)
        if url and url not in images:
            images.append(url)
    hero = _clean_text(content.get("main_photo"))
    if hero and hero not in images:
        images.insert(0, hero)

    facilities = [_clean_text(f) for f in content.get("hotelFacilities") or []]
    facilities = [f for f in facilities if f]

    rating = _money(content.get("rating")) or 0
    stars = _money(content.get("starRating")) or _money(content.get("stars")) or 0

    times = content.get("checkinCheckoutTimes") or {}
    pois = []
    for poi in (content.get("poi") or [])[:8]:
        if not isinstance(poi, Mapping):
            continue
        name = _clean_text(poi.get("name") or poi.get("poi"))
        if name:
            pois.append({"name": name, "distance": _clean_text(poi.get("distance"))})

    return {
        "id": _clean_text(content.get("id")),
        "name": _clean_text(content.get("name")) or "Property",
        "address": _clean_text(content.get("address")),
        "city": _clean_text(content.get("city")),
        "country": _clean_text(content.get("country")).upper(),
        "chain": _clean_text(content.get("chain")),
        "hotel_type": _clean_text(content.get("hotelType")),
        "images": images[:24],
        "hero": images[0] if images else "",
        "stars": int(stars) if stars else 0,
        "rating": round(rating, 1) if rating else None,
        "review_label": review_label(rating),
        "review_count": int(_money(content.get("reviewCount")) or 0),
        "facilities": facilities,
        "amenities": match_amenities(facilities),
        "checkin_time": _clean_text(times.get("checkin")),
        "checkout_time": _clean_text(times.get("checkout")),
        "pois": pois,
        "latitude": (content.get("location") or {}).get("latitude") if isinstance(content.get("location"), Mapping) else content.get("latitude"),
        "longitude": (content.get("location") or {}).get("longitude") if isinstance(content.get("location"), Mapping) else content.get("longitude"),
    }


def build_prebook_summary(prebook: Mapping[str, Any], *, nights: int) -> dict[str, Any]:
    """Checkout-facing summary, including any price drift since search."""
    price = _money(prebook.get("price")) or 0.0
    drift = _money(prebook.get("priceDifferencePercent"))
    room_types = prebook.get("roomTypes") or []
    first_room = room_types[0] if room_types else {}
    first_rate = (first_room.get("rates") or [{}])[0] if isinstance(first_room, Mapping) else {}
    refundable_tag = str((first_rate.get("cancellationPolicies") or {}).get("refundableTag") or "").strip().upper()
    return {
        "prebook_id": str(prebook.get("prebookId") or "").strip(),
        "offer_id": str(prebook.get("offerId") or "").strip(),
        "hotel_id": str(prebook.get("hotelId") or "").strip(),
        "checkin": str(prebook.get("checkin") or "").strip(),
        "checkout": str(prebook.get("checkout") or "").strip(),
        "nights": nights,
        "total_amount": price,
        "nightly_amount": round(price / nights, 2) if nights > 0 else None,
        "currency": str(prebook.get("currency") or LITE_DEFAULT_CURRENCY).upper(),
        "commission": _money(prebook.get("commission")),
        "price_changed": bool(drift),
        "price_difference_percent": drift,
        "cancellation_changed": bool(prebook.get("cancellationChanged")),
        "board_changed": bool(prebook.get("boardChanged")),
        "payment_types": list(prebook.get("paymentTypes") or []),
        "terms": str(prebook.get("termsAndConditions") or "").strip(),
        "room_name": str(first_rate.get("name") or first_room.get("name") or "Room").strip(),
        "board_name": str(first_rate.get("boardName") or "Room only").strip(),
        "refundable": refundable_tag in REFUNDABLE_TAGS,
        "max_occupancy": first_rate.get("maxOccupancy"),
    }
