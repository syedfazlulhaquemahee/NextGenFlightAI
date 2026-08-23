"""LiteAPI (Nuitée Connect) flight search and booking client.

Second flight provider alongside Duffel (see duffel_booking.py). Flow is
search -> verify -> prebook -> (optional attach_services) -> book, where
verify locks pricing and prebook creates the provider-side hold — LiteAPI's
own docs describe prebook as generating a Stripe PaymentIntent by default,
but passing usePaymentSdk=False routes billing through the agency's on-file
credit line via payment method ACC_CREDIT_CARD instead, the same no-card,
no-Stripe pattern this app already uses for LiteAPI hotel bookings
(liteapi_client.py's prebook/book). get_booking/cancel_booking are not
defined here — hotels already proved these hit a shared, non-hotel-specific
/bookings/{id} endpoint (see LiteAPIClient.get_booking/cancel_booking), so
the existing LITE client instance's methods are reused for flight bookings
too rather than duplicated.

offerId here is LiteAPI's raw ID. app.py is responsible for the "LF-" prefix
that distinguishes a LiteAPI offer_id from a Duffel one in URLs/routes — this
module only ever deals in raw (unprefixed) LiteAPI IDs.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Mapping, Sequence

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from liteapi_client import (
    LITE_API_KEY,
    LITE_DATA_BASE,
    LITE_BOOK_BASE,
    LiteAPIError,
    SingleFlightTTLCache,
    _build_session,
    _RateLimiter,
)


def _build_search_session() -> requests.Session:
    """A separate session for /flights/rates specifically, with retries
    disabled. The shared _build_session() (imported above, used for
    verify/prebook/attach_services/book) retries on read timeout — sensible
    for those small, fast, single-item calls, but actively harmful here:
    a slow search response means the server is still legitimately
    aggregating real GDS/NDC/LCC providers, not a transient blip, and
    retrying just multiplies the wait (measured: a single call approaching
    its timeout got silently retried up to 3x total, turning a ~10s cap
    into ~25s+ in practice). No retries + a hard timeout means a slow
    search fails fast and search_flights() degrades to Duffel-only,
    instead of one slow provider dragging out the whole page load."""
    session = requests.Session()
    retry = Retry(total=0, connect=0, read=0, status_forcelist=())
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=32)
    session.mount("https://", adapter)
    session.headers.update({"Accept": "application/json", "Accept-Encoding": "gzip"})
    return session

# Independently overridable in case flights turns out to live on a different
# host than hotels' api./book. split — confirm against sandbox before trusting
# these defaults for anything that spends money.
LITE_FLIGHTS_SEARCH_BASE = os.getenv("LITE_FLIGHTS_SEARCH_BASE", LITE_DATA_BASE).strip() or LITE_DATA_BASE
LITE_FLIGHTS_BOOK_BASE = os.getenv("LITE_FLIGHTS_BOOK_BASE", LITE_BOOK_BASE).strip() or LITE_BOOK_BASE
LITE_FLIGHTS_ENABLED = os.getenv("LITE_FLIGHTS_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
# Independent from LITE_FLIGHTS_ENABLED on purpose: that flag only controls
# whether LiteAPI offers are searched/displayed for price comparison.
# Checkout (prebook/book — real holds, real charges) stays off behind this
# second flag until Phase 3 is actually built and explicitly turned on, so
# turning on search never accidentally exposes a live booking path.
LITE_FLIGHTS_CHECKOUT_ENABLED = os.getenv("LITE_FLIGHTS_CHECKOUT_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
LITE_FLIGHTS_HTTP_TIMEOUT = float(os.getenv("LITE_FLIGHTS_HTTP_TIMEOUT", "20"))
LITE_FLIGHTS_SEARCH_TIMEOUT = float(os.getenv("LITE_FLIGHTS_SEARCH_TIMEOUT_S", "12"))
# Used only by the async /search/liteapi-supplement path (app.py), which runs
# after the page has already rendered — nothing is blocked waiting on it, so
# it can afford to wait out LiteAPI's real worst-case latency (measured live:
# 13-17s clean) instead of the tight cap the synchronous/blocking search path
# needs to fail fast on.
LITE_FLIGHTS_SUPPLEMENT_TIMEOUT = float(os.getenv("LITE_FLIGHTS_SUPPLEMENT_TIMEOUT_S", "25"))

FLIGHTS_SEARCH_CACHE_TTL = float(os.getenv("LITE_FLIGHTS_SEARCH_CACHE_TTL", "120"))


class LiteAPIFlightsClient:
    def __init__(self) -> None:
        self.session = _build_session()
        self.search_session = _build_search_session()
        self._limiter = _RateLimiter(float(os.getenv("LITE_FLIGHTS_MIN_INTERVAL", "0.25")))
        self._search_cache = SingleFlightTTLCache(maxsize=512, ttl_seconds=FLIGHTS_SEARCH_CACHE_TTL, max_inflight=16)

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
        json_body: Mapping[str, Any] | None = None,
        timeout: float = LITE_FLIGHTS_HTTP_TIMEOUT,
        session: requests.Session | None = None,
    ) -> dict[str, Any]:
        if not LITE_API_KEY:
            raise LiteAPIError("LiteAPI is not configured. Set LITE_API_KEY.")

        self._limiter.wait()
        try:
            resp = (session or self.session).request(
                method=method,
                url=f"{base}{path}",
                json=json_body,
                headers=self._headers(),
                timeout=timeout,
            )
        except requests.RequestException as exc:
            print("LITEAPI FLIGHTS EXCEPTION:", path, repr(exc))
            raise LiteAPIError("We couldn't reach our flight provider.")

        if not resp.ok:
            print("LITEAPI FLIGHTS STATUS:", path, resp.status_code)
            print("LITEAPI FLIGHTS BODY:", resp.text[:600])
            raise LiteAPIError(self._error_message(resp), status_code=resp.status_code)

        try:
            return resp.json() or {}
        except ValueError:
            raise LiteAPIError("Our flight provider returned an unexpected response.")

    def _error_message(self, resp: requests.Response) -> str:
        try:
            payload = resp.json()
        except ValueError:
            return "We couldn't load flights right now."
        error = payload.get("error")
        if isinstance(error, Mapping):
            detail = str(error.get("message") or error.get("description") or "").strip()
            if detail:
                return detail
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "We couldn't load flights right now."

    # -- search ------------------------------------------------------
    def search(
        self,
        *,
        legs: Sequence[Mapping[str, Any]],
        adults: int,
        children: int = 0,
        infants: int = 0,
        currency: str = "USD",
        cabin_class: str = "ECONOMY",
        timeout: float = LITE_FLIGHTS_SEARCH_TIMEOUT,
    ) -> list[dict[str, Any]]:
        """POST /flights/rates. Returns a list of raw offer dicts (unparsed).

        Confirmed via a live probe: this is a plain buffered JSON response
        (Content-Type: application/json, not text/event-stream) — the "SSE
        fan-out" in LiteAPI's docs describes their own backend aggregation
        across real GDS/NDC/LCC providers, not something this endpoint
        streams to clients. There is no way to get partial results early;
        the server only responds once its own aggregation is fully done,
        which is why this can legitimately take anywhere from ~2s to 20s+
        depending on which upstream providers answer. Uses search_session
        (no retries) rather than self.session — retrying a slow-but-valid
        response just multiplies the wait for no benefit; a hard timeout
        that fails fast lets search_flights() degrade to Duffel-only
        instead of one slow provider dragging out the whole page load.
        """
        body: dict[str, Any] = {
            "legs": [dict(leg) for leg in legs],
            "adults": int(adults),
            "currency": currency,
        }
        if children:
            body["children"] = int(children)
        if infants:
            body["infants"] = int(infants)
        if cabin_class:
            body["cabinClass"] = cabin_class
        payload = self._request(
            "POST", LITE_FLIGHTS_SEARCH_BASE, "/flights/rates",
            json_body=body, timeout=timeout, session=self.search_session,
        )
        data = payload.get("data")
        if isinstance(data, list):
            return data
        offers = payload.get("offers")
        return offers if isinstance(offers, list) else []

    # -- checkout funnel ----------------------------------------------
    def verify(self, offer_id: str) -> dict[str, Any]:
        """POST /flights/verify — locks pricing, detects provider-side price changes."""
        payload = self._request("POST", LITE_FLIGHTS_BOOK_BASE, "/flights/verify", json_body={"offerId": offer_id})
        return payload.get("data") or {}

    def prebook(self, offer_id: str, *, use_payment_sdk: bool = False) -> dict[str, Any]:
        """POST /flights/prebooks. usePaymentSdk=False bypasses Stripe entirely,
        routing to agency credit-line billing (ACC_CREDIT_CARD) — see module
        docstring. Never pass True without Stripe infra actually existing."""
        payload = self._request(
            "POST", LITE_FLIGHTS_BOOK_BASE, "/flights/prebooks",
            json_body={"offerId": offer_id, "usePaymentSdk": bool(use_payment_sdk)},
        )
        data = payload.get("data") or {}
        if not data.get("prebookId"):
            raise LiteAPIError("That fare is no longer available. Please search again.")
        return data

    def attach_services(self, prebook_id: str, services: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """POST /flights/prebooks/{id}/services — optional seats/baggage add-ons."""
        payload = self._request(
            "POST", LITE_FLIGHTS_BOOK_BASE, f"/flights/prebooks/{prebook_id}/services",
            json_body={"services": [dict(s) for s in services]},
        )
        return payload.get("data") or {}

    def book(
        self,
        *,
        prebook_id: str,
        holder: Mapping[str, Any],
        passengers: Sequence[Mapping[str, Any]],
        payment: Mapping[str, Any],
        client_reference: str = "",
    ) -> dict[str, Any]:
        """POST /flights/bookings — finalizes the reservation and charges."""
        body: dict[str, Any] = {
            "prebookId": prebook_id,
            "holder": dict(holder),
            "passengers": [dict(p) for p in passengers],
            "payment": dict(payment),
        }
        if client_reference:
            body["clientReference"] = client_reference
        payload = self._request("POST", LITE_FLIGHTS_BOOK_BASE, "/flights/bookings", json_body=body)
        return payload.get("data") or {}
