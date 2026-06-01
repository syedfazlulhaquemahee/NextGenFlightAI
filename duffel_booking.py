from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ in supported environments.
    ZoneInfo = None


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
COUNTRY_RE = re.compile(r"^[A-Za-z]{2}$")
PASSPORT_RE = re.compile(r"^[A-Za-z0-9]{5,20}$")
PHONE_CLEAN_RE = re.compile(r"[^\d+]")

_AIRPORT_TIMEZONE_FALLBACKS = {
    "BCN": "Europe/Madrid",
    "BOS": "America/New_York",
    "CDG": "Europe/Paris",
    "EWR": "America/New_York",
    "HND": "Asia/Tokyo",
    "JFK": "America/New_York",
    "LAX": "America/Los_Angeles",
    "LGA": "America/New_York",
    "NRT": "Asia/Tokyo",
    "ORD": "America/Chicago",
    "SFO": "America/Los_Angeles",
}

_DUFFEL_AIRLINE_LOGO_BASE = (
    "https://assets.duffel.com/img/airlines/for-light-background/full-color-logo"
)


def _carrier_dict_logo_url(carrier: Any) -> str | None:
    """Resolve a logo URL from a Duffel carrier dict (owner or marketing_carrier)."""
    if not isinstance(carrier, dict):
        return None
    for key in ("logo_symbol_url", "logo_lockup_url"):
        raw = str(carrier.get(key) or "").strip()
        if raw.startswith("https://") or raw.startswith("http://"):
            return raw
    code = str(carrier.get("iata_code") or "").strip().upper()
    if len(code) >= 2:
        return f"{_DUFFEL_AIRLINE_LOGO_BASE}/{code[:2]}.svg"
    return None


def _segment_marketing_logo_url(segment: Mapping[str, Any]) -> str | None:
    marketing = segment.get("marketing_carrier") or {}
    return _carrier_dict_logo_url(marketing)


def _resource_airline_logo_url(resource: Mapping[str, Any]) -> str | None:
    """Owner logo when present; otherwise first segment marketing carrier (owner dict often omits iata_code)."""
    url = _carrier_dict_logo_url(resource.get("owner") or {})
    if url:
        return url
    for slice_data in resource.get("slices") or []:
        segments = slice_data.get("segments") or []
        if segments:
            u = _segment_marketing_logo_url(segments[0])
            if u:
                return u
    return None


def parse_duffel_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed


def offer_has_expired(offer: Mapping[str, Any]) -> bool:
    expires_at = parse_duffel_datetime(offer.get("expires_at"))
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def build_checkout_summary(
    offer: Mapping[str, Any],
    *,
    seat_maps: list[dict[str, Any]] | None = None,
    ancillaries_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    slices = [_build_slice_summary(slice_data, idx) for idx, slice_data in enumerate(offer.get("slices") or [])]
    slices = [item for item in slices if item]
    owner = _owner(offer)
    price_breakdown = _price_breakdown(
        offer,
        ancillaries_payload=ancillaries_payload,
        seat_maps=seat_maps,
    )
    traveler_price_lines = _traveler_price_lines(offer, price_breakdown)
    ancillaries = _build_ancillaries_summary(offer, seat_maps or [], ancillaries_payload=ancillaries_payload)
    flexibility = _build_flexibility_summary(offer)
    baggage_summary = _build_baggage_summary(offer)
    first_slice = slices[0] if slices else {}
    airline_logo_url = _resource_airline_logo_url(offer)
    return {
        "offer_id": offer.get("id") or "",
        "airline_name": owner.get("name"),
        "airline_logo": owner.get("logo_symbol"),
        "airline_logo_url": airline_logo_url,
        "route_summary": first_slice.get("route", "Itinerary unavailable"),
        "city_summary": first_slice.get("city_route", ""),
        "total_amount": price_breakdown["total_amount"],
        "currency": price_breakdown["currency"],
        "cabin_label": _cabin_label(offer),
        "fare_brand": _fare_brand(offer),
        "expires_at_label": _format_datetime(parse_duffel_datetime(offer.get("expires_at"))),
        "expires_at_iso": _isoformat(parse_duffel_datetime(offer.get("expires_at"))),
        "traveler_count": max(1, len(offer.get("passengers") or [])),
        "traveler_price_lines": traveler_price_lines,
        "requires_gender": True,
        "requires_identity_documents": bool(offer.get("passenger_identity_documents_required")),
        "identity_document_types": _identity_document_types(offer),
        "slices": slices,
        "price_breakdown": price_breakdown,
        "ancillaries": ancillaries,
        "baggage_summary": baggage_summary,
        "flexibility": flexibility,
        "trust_badges": [],
        "services_available": bool(offer.get("available_services")),
        "hold_supported": _hold_supported(offer),
    }


def _bag_weight_label(bag: Mapping[str, Any]) -> str:
    kg = bag.get("maximum_weight_kg")
    lb = bag.get("maximum_weight_lb") or bag.get("maximum_weight_lbs")
    if kg is not None:
        try:
            return f"up to {float(kg):g} kg"
        except Exception:
            pass
    if lb is not None:
        try:
            return f"up to {float(lb):g} lb"
        except Exception:
            pass
    return "weight not provided"


def _build_baggage_summary(offer: Mapping[str, Any]) -> list[dict[str, str]]:
    """Summarize included baggage from the first flown segment.

    Duffel reports bag allowance per segment/passenger. The first segment is the
    clearest checkout summary proxy; details can vary later by airline/segment.
    """
    try:
        first_slice = (offer.get("slices") or [])[0] if offer.get("slices") else {}
        first_segment = ((first_slice or {}).get("segments") or [])[0]
    except Exception:
        first_segment = {}
    segment_passengers = (first_segment or {}).get("passengers") or []
    if not segment_passengers:
        return [
            {
                "type": "unknown",
                "label": "Baggage",
                "quantity_label": "Airline policy",
                "weight_label": "not provided",
            }
        ]

    by_passenger: dict[str, dict[str, int]] = {}
    weights: dict[str, str] = {}
    saw_types: set[str] = set()
    for index, passenger in enumerate(segment_passengers):
        passenger_id = str((passenger or {}).get("passenger_id") or (passenger or {}).get("id") or index)
        if passenger_id not in by_passenger:
            by_passenger[passenger_id] = {}
        for bag in (passenger or {}).get("baggages") or []:
            bag_type = str((bag or {}).get("type") or "").strip().lower()
            if bag_type == "cabin":
                bag_type = "carry_on"
            if bag_type not in {"carry_on", "checked"}:
                continue
            saw_types.add(bag_type)
            quantity = int((bag or {}).get("quantity") or 0)
            by_passenger[passenger_id][bag_type] = by_passenger[passenger_id].get(bag_type, 0) + max(0, quantity)
            if bag_type not in weights:
                weights[bag_type] = _bag_weight_label(bag)

    output: list[dict[str, str]] = []
    passenger_count = max(1, len(by_passenger))
    labels = {"carry_on": "Carry-on", "checked": "Checked bag"}
    for bag_type in ("carry_on", "checked"):
        quantities = [allowances.get(bag_type, 0) for allowances in by_passenger.values()]
        total_quantity = sum(quantities)
        if total_quantity <= 0:
            if bag_type in saw_types:
                output.append(
                    {
                        "type": bag_type,
                        "label": labels[bag_type],
                        "quantity_label": "0 included",
                        "weight_label": weights.get(bag_type, "weight not provided"),
                    }
                )
            continue
        if quantities and len(set(quantities)) == 1:
            per_traveler = quantities[0]
            quantity_label = f"{per_traveler} per traveler"
        else:
            quantity_label = f"{total_quantity} total for {passenger_count} travelers"
        output.append(
            {
                "type": bag_type,
                "label": labels[bag_type],
                "quantity_label": quantity_label,
                "weight_label": weights.get(bag_type, "weight not provided"),
            }
        )

    if not output:
        output.append(
            {
                "type": "unknown",
                "label": "Baggage",
                "quantity_label": "Airline policy",
                "weight_label": "not provided",
            }
        )
    return output


def build_checkout_page_model(
    offer: Mapping[str, Any],
    *,
    travelers: list[dict[str, Any]],
    seat_maps: list[dict[str, Any]] | None = None,
    ancillaries_payload: Mapping[str, Any] | None = None,
    payment_config: Mapping[str, Any] | None = None,
    duffel_env: str = "test",
) -> dict[str, Any]:
    summary = build_checkout_summary(offer, seat_maps=seat_maps, ancillaries_payload=ancillaries_payload)
    return {
        "header": {
            "title": "Traveler and payment details",
            "subtitle": "Complete the required traveler details and confirm the booking.",
            "steps": [
                {"label": "Search", "state": "complete"},
                {"label": "Select", "state": "complete"},
                {"label": "Checkout", "state": "current"},
                {"label": "Confirm", "state": "upcoming"},
            ],
        },
        "summary": summary,
        "travelers": travelers,
        "ancillaries": summary["ancillaries"],
        "payment": {
            "headline": "Payment",
            "title": "Secure payment",
            "supporting_copy": "Card details stay inside Duffel's secure payment flow when card collection is enabled.",
            "mode": str((payment_config or {}).get("mode") or "card"),
            "component_client_key_available": bool((payment_config or {}).get("component_client_key")),
            "component_client_key": str((payment_config or {}).get("component_client_key") or ""),
            "three_d_secure_supported": bool((payment_config or {}).get("three_d_secure_supported", True)),
            "billing_required": bool((payment_config or {}).get("billing_required")),
        },
        "client_state": {
            "offer_id": offer.get("id") or "",
            "expires_at": summary["expires_at_iso"],
            "currency": summary["currency"],
            "base_total": summary["price_breakdown"]["base_total_amount"],
            "initial_total": summary["price_breakdown"]["total_amount"],
            "offer_services": summary["ancillaries"]["service_catalog"],
            "selected_services": summary["ancillaries"]["selected_services"],
            "seat_traveler_count": len(travelers),
            "seat_maps": seat_maps or [],
            "payment": {
                "mode": str((payment_config or {}).get("mode") or "card"),
                "component_client_key": str((payment_config or {}).get("component_client_key") or ""),
            },
        },
    }


def build_traveler_forms(offer: Mapping[str, Any], form: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    passengers = offer.get("passengers") or []
    if not passengers:
        passengers = [{"type": "adult"}]
    values = form or {}
    needs_identity_documents = bool(offer.get("passenger_identity_documents_required"))
    document_types = _identity_document_types(offer)
    travelers: list[dict[str, Any]] = []
    for idx, passenger in enumerate(passengers):
        travelers.append(
            {
                "index": idx,
                "passenger_id": str((passenger or {}).get("id") or "").strip(),
                "label": f"Traveler {idx + 1}",
                "type": (passenger.get("type") or "adult").strip() or "adult",
                "type_label": _titleize_passenger_type(passenger.get("type") or "adult"),
                "title": _value(values, idx, "title", passenger.get("title")),
                "given_name": _value(values, idx, "given_name", passenger.get("given_name")),
                "family_name": _value(values, idx, "family_name", passenger.get("family_name")),
                "born_on": _value(values, idx, "born_on", passenger.get("born_on")),
                "gender": _value(values, idx, "gender", passenger.get("gender")),
                "email": _value(values, idx, "email", passenger.get("email")),
                "phone_number": _value(values, idx, "phone_number", passenger.get("phone_number")),
                "passport_number": _value(values, idx, "passport_number"),
                "passport_country_code": _value(values, idx, "passport_country_code"),
                "passport_expires_on": _value(values, idx, "passport_expires_on"),
                "needs_identity_documents": needs_identity_documents,
                "document_types": document_types,
                "expanded": idx == 0,
                "completion_state": "incomplete",
            }
        )
    return travelers


def validate_checkout_form(offer: Mapping[str, Any], form: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    passengers = offer.get("passengers") or []
    travelers = build_traveler_forms(offer, form)
    errors: dict[str, str] = {}
    payload_passengers: list[dict[str, Any]] = []
    needs_identity_documents = bool(offer.get("passenger_identity_documents_required"))

    if len(passengers) != len(travelers):
        errors["form"] = "This offer no longer matches the traveler count from your search. Please search again."
        return payload_passengers, travelers, errors

    for traveler in travelers:
        idx = traveler["index"]
        passenger_id = (passengers[idx].get("id") or "").strip()
        if not passenger_id:
            errors["form"] = "Duffel did not return a passenger ID for this offer. Please search again."
            continue

        given_name = traveler["given_name"].strip()
        family_name = traveler["family_name"].strip()
        title = traveler["title"].strip().lower()
        born_on = traveler["born_on"].strip()
        gender = traveler["gender"].strip().lower()
        email = traveler["email"].strip().lower()
        phone_number = _normalize_phone_number(traveler["phone_number"])

        _require(errors, _field_name(idx, "title"), title, "Choose a title.")
        _require(errors, _field_name(idx, "given_name"), given_name, "Enter a first name.")
        _require(errors, _field_name(idx, "family_name"), family_name, "Enter a last name.")
        _validate_birth_date(errors, idx, born_on)
        if gender not in {"m", "f"}:
            errors[_field_name(idx, "gender")] = "Choose a gender."
        if not EMAIL_RE.match(email):
            errors[_field_name(idx, "email")] = "Enter a valid email address."
        if not phone_number:
            errors[_field_name(idx, "phone_number")] = "Enter a phone number with country code, like +15551234567."

        passenger_payload: dict[str, Any] = {
            "id": passenger_id,
            "type": (passengers[idx].get("type") or "adult").strip() or "adult",
            "title": title,
            "given_name": given_name,
            "family_name": family_name,
            "born_on": born_on,
            "gender": gender,
            "email": email,
            "phone_number": phone_number or traveler["phone_number"].strip(),
        }

        if needs_identity_documents:
            passport_number = traveler["passport_number"].strip().upper()
            passport_country_code = traveler["passport_country_code"].strip().upper()
            passport_expires_on = traveler["passport_expires_on"].strip()

            if not PASSPORT_RE.match(passport_number):
                errors[_field_name(idx, "passport_number")] = "Enter a valid passport number."
            if not COUNTRY_RE.match(passport_country_code):
                errors[_field_name(idx, "passport_country_code")] = "Use a two-letter country code, like US."
            _validate_future_date(errors, idx, "passport_expires_on", passport_expires_on, "Enter a valid passport expiry date.")

            passenger_payload["identity_documents"] = [
                {
                    "unique_identifier": passport_number,
                    "type": "passport",
                    "issuing_country_code": passport_country_code,
                    "expires_on": passport_expires_on,
                }
            ]

        payload_passengers.append(passenger_payload)

    return payload_passengers, travelers, errors


def build_order_summary(order: Mapping[str, Any]) -> dict[str, Any]:
    passengers = order.get("passengers") or []
    slices = [_build_slice_summary(slice_data, idx) for idx, slice_data in enumerate(order.get("slices") or [])]
    slices = [item for item in slices if item]
    owner = _owner(order)
    ancillaries = _summarize_order_services(order)
    price_breakdown = _price_breakdown(order, ancillaries_payload={"selected_services": ancillaries["selected_services"]})
    # Booking date from Duffel's created_at ISO field
    created_raw = str(order.get("created_at") or "")[:10]
    try:
        from datetime import datetime as _dt
        booking_date_label = _dt.strptime(created_raw, "%Y-%m-%d").strftime("%b %-d, %Y") if created_raw else ""
    except Exception:
        booking_date_label = created_raw

    # Per-passenger detail (cabin class, type, baggage)
    passengers_detail = []
    for p in passengers:
        name = _passenger_name(p)
        if not name:
            continue
        pid   = str(p.get("id") or "").strip()
        ptype = str(p.get("type") or "adult").strip().title()
        cabin = ""
        pax_bags: list[str] = []
        for sl in (order.get("slices") or []):
            for seg in (sl.get("segments") or []):
                for sp in (seg.get("passengers") or []):
                    sp_id = str(sp.get("passenger_id") or sp.get("id") or "").strip()
                    if sp_id and pid and sp_id != pid:
                        continue
                    if not cabin:
                        cabin = str(sp.get("cabin_class_marketing_name") or sp.get("cabin_class") or "").replace("_", " ").title().strip()
                    if not pax_bags:
                        for bag in (sp.get("baggages") or []):
                            btype = str(bag.get("type") or "").strip().lower()
                            bqty  = int(bag.get("quantity") or 0)
                            if btype and bqty > 0:
                                readable = btype.replace("_", "-").title()
                                pax_bags.append(f"{bqty}x {readable}")
                    if cabin and pax_bags:
                        break
                if cabin:
                    break
            if cabin:
                break
        baggage_str = ", ".join(pax_bags) if pax_bags else "—"
        passengers_detail.append({"name": name, "type": ptype, "cabin": cabin or "Economy", "baggage": baggage_str})

    # Contact email — first passenger who has one
    contact_email = ""
    for p in passengers:
        em = str(p.get("email") or "").strip().lower()
        if em:
            contact_email = em
            break

    # Global baggage allowance summary (aggregated across first segment passengers)
    baggage_allowance: list[str] = []
    try:
        first_segs = ((order.get("slices") or [])[0] if order.get("slices") else {}).get("segments") or []
        first_seg  = first_segs[0] if first_segs else {}
        bag_counts: dict[str, int] = {}
        seen_pax: set[str] = set()
        for sp in (first_seg.get("passengers") or []):
            sp_id = str(sp.get("passenger_id") or sp.get("id") or "")
            if sp_id in seen_pax:
                continue
            seen_pax.add(sp_id)
            for bag in (sp.get("baggages") or []):
                btype = str(bag.get("type") or "").strip().lower()
                bqty  = int(bag.get("quantity") or 0)
                if btype and bqty > 0:
                    bag_counts[btype] = bag_counts.get(btype, 0) + bqty
        for btype in ("carry_on", "checked"):
            if btype in bag_counts:
                q = bag_counts[btype]
                label = "Carry-on" if btype == "carry_on" else "Checked bag"
                # Pick up weight from the first bag object if Duffel provided it
                weight_str = ""
                for sp in (first_seg.get("passengers") or []):
                    for bag in (sp.get("baggages") or []):
                        if str(bag.get("type") or "").strip().lower() == btype:
                            wkg = bag.get("maximum_weight_kg")
                            if wkg is not None:
                                try:
                                    weight_str = f", up to {float(wkg):.0f} kg"
                                except Exception:
                                    pass
                            break
                    if weight_str:
                        break
                baggage_allowance.append(f"{q} {label}{'s' if q > 1 else ''} included{weight_str}")
        if not baggage_allowance:
            baggage_allowance = ["Carry-on bag included"]
    except Exception:
        baggage_allowance = ["See airline for baggage policy"]

    # Order conditions (change / refund rules)
    raw_cond = order.get("conditions") or {}
    if isinstance(raw_cond, Mapping):
        change_cond = raw_cond.get("change_before_departure") or {}
        refund_cond = raw_cond.get("refund_before_departure") or {}
    else:
        change_cond = {}
        refund_cond = {}
    order_conditions = {
        "changes": _condition_summary(change_cond, fallback="Contact airline for change rules."),
        "refunds": _condition_summary(refund_cond, fallback="Contact airline for refund rules."),
    }

    owner_raw = order.get("owner") or {}
    return {
        "order_id": order.get("id") or "",
        "booking_reference": str(order.get("booking_reference") or "").strip(),
        "airline_name": owner.get("name"),
        "airline_iata": str(owner_raw.get("iata_code") or owner.get("code") or "").strip().upper(),
        "airline_logo": owner.get("logo_symbol"),
        "airline_logo_url": _resource_airline_logo_url(order),
        "status_label": _status_label(order),
        "total_amount": str(order.get("total_amount") or "").strip() or price_breakdown["total_amount"],
        "currency": str(order.get("total_currency") or "USD").strip() or "USD",
        "passenger_names": [_passenger_name(passenger) for passenger in passengers if _passenger_name(passenger)],
        "passengers_detail": passengers_detail,
        "contact_email": contact_email,
        "booking_date": booking_date_label,
        "slices": slices,
        "ancillaries": ancillaries,
        "conditions": order_conditions,
        "baggage_allowance": baggage_allowance,
        "next_actions": [
            {"label": "Manage booking", "kind": "manage"},
            {"label": "Download itinerary", "kind": "download"},
            {"label": "Add to calendar", "kind": "calendar"},
        ],
    }


def extract_ancillaries_payload(form: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(form.get("ancillaries_payload") or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def selected_services_from_payload(ancillaries_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    services = ancillaries_payload.get("services") or ancillaries_payload.get("selected_services") or []
    normalized: list[dict[str, Any]] = []
    for service in services:
        if isinstance(service, str) and service.strip():
            normalized.append({"id": service.strip()})
            continue
        if not isinstance(service, Mapping):
            continue
        service_id = str(service.get("id") or service.get("service_id") or "").strip()
        if not service_id:
            continue
        qty = service.get("quantity") or 1
        try:
            qty_int = int(qty)
        except (TypeError, ValueError):
            qty_int = 1
        entry: dict[str, Any] = {"id": service_id, "quantity": max(1, qty_int)}
        for amount_key in ("amount", "total_amount"):
            raw_amt = str(service.get(amount_key) or "").strip()
            if raw_amt:
                entry[amount_key] = raw_amt
                break
        if service.get("label"):
            entry["label"] = str(service.get("label")).strip()
        normalized.append(entry)
    return normalized


def normalize_create_order_services(services: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Strip ancillary UI fields so only Duffel order `services` keys remain."""
    out: list[dict[str, Any]] = []
    for service in services or []:
        sid = str((service or {}).get("id") or "").strip()
        if not sid:
            continue
        qty = (service or {}).get("quantity") or 1
        try:
            qty_int = int(qty)
        except (TypeError, ValueError):
            qty_int = 1
        out.append({"id": sid, "quantity": max(1, qty_int)})
    return out


def build_passengers_for_duffel_ancillaries(
    offer: Mapping[str, Any],
    travelers: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Minimal passenger rows for Duffel's ancillaries UI (prefilled from the offer + traveler shells)."""
    passengers = offer.get("passengers") or []
    travelers = travelers or []
    rows: list[dict[str, Any]] = []
    for idx, p in enumerate(passengers):
        pid = str((p or {}).get("id") or "").strip()
        if not pid:
            continue
        tr = next((t for t in travelers if int(t.get("index", -1)) == idx), {}) or {}
        def pick(field: str, fallback: str) -> str:
            for source in (p, tr):
                if not isinstance(source, Mapping):
                    continue
                val = str(source.get(field) or "").strip()
                if val:
                    return val
            return fallback

        gender = pick("gender", "m").lower()[:1]
        if gender not in {"m", "f"}:
            gender = "m"
        title = pick("title", "mr").lower() or "mr"
        phone_raw = pick("phone_number", "+10000000000")
        phone = _normalize_phone_number(phone_raw) or phone_raw.strip() or "+10000000000"
        rows.append(
            {
                "id": pid,
                "type": str((p or {}).get("type") or "adult").strip() or "adult",
                "title": title,
                "given_name": pick("given_name", "Traveler"),
                "family_name": pick("family_name", str(idx + 1)),
                "born_on": pick("born_on", "1990-01-01"),
                "gender": gender,
                "email": pick("email", "traveler@example.com"),
                "phone_number": phone,
            }
        )
    return rows


def build_duffel_ancillary_ui_services(
    offer: Mapping[str, Any],
    seat_maps: list[dict[str, Any]] | None,
    seat_policy: Mapping[str, Any] | None,
) -> list[str]:
    """Which flows to mount in Duffel Ancillaries (requires at least one)."""
    types: list[str] = []
    for svc in offer.get("available_services") or []:
        if not isinstance(svc, Mapping):
            continue
        t = str(svc.get("type") or svc.get("service_type") or "").strip().lower()
        if t:
            types.append(t)

    modes: list[str] = []
    if any("bag" in t for t in types):
        modes.append("bags")
    policy = seat_policy or {}
    if policy.get("can_select_map") and (seat_maps or []):
        modes.append("seats")
    if any("cancel_for_any_reason" in t or "cancel for any reason" in t or "duffel_protection" in t for t in types):
        modes.append("cancel_for_any_reason")

    if not modes:
        modes = ["bags"]
    if "cancel_for_any_reason" not in modes:
        modes.append("cancel_for_any_reason")
    return modes


def build_duffel_ancillary_styles() -> dict[str, str]:
    """Theme tokens consumed by Duffel Ancillaries (`styles` prop)."""
    return {
        "accentColor": "50, 111, 221",
        "buttonCornerRadius": "10px",
        "fontFamily": "system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
    }


def build_duffel_ancillaries_embed_model(
    offer: Mapping[str, Any],
    *,
    seat_maps: list[dict[str, Any]] | None,
    travelers: list[dict[str, Any]],
    seat_policy: Mapping[str, Any] | None,
    cdn_version: str = "3.14.0",
) -> dict[str, Any]:
    return {
        "cdn_version": cdn_version,
        "services": build_duffel_ancillary_ui_services(offer, seat_maps, seat_policy),
        "styles": build_duffel_ancillary_styles(),
        "passengers": build_passengers_for_duffel_ancillaries(offer, travelers),
        "offer": offer,
        "seat_maps": seat_maps or [],
    }


def calculate_total_amount(
    offer: Mapping[str, Any],
    ancillaries_payload: Mapping[str, Any] | None = None,
    *,
    seat_maps: list[dict[str, Any]] | None = None,
) -> str:
    currency = str(offer.get("total_currency") or "USD").strip() or "USD"
    base_total = _to_cents(str(offer.get("total_amount") or "0.00"))
    selected_services = selected_services_from_payload(ancillaries_payload or {})
    if not selected_services:
        return _from_cents(base_total, currency)
    catalog = _service_catalog(offer, seat_maps or [])
    services_total = 0
    for service in selected_services:
        service_id = str(service.get("id") or "").strip()
        if not service_id:
            continue
        catalog_item = catalog.get(service_id) or {}
        cents = _to_cents(str(catalog_item.get("amount") or "0.00"))
        if cents == 0:
            cents = _to_cents(str(service.get("amount") or service.get("total_amount") or "0.00"))
        services_total += cents
    return _from_cents(base_total + services_total, currency)


def _owner(resource: Mapping[str, Any]) -> dict[str, str]:
    owner = resource.get("owner") or {}
    name = str(owner.get("name") or "").strip() or "Selected flight"
    code = str(owner.get("iata_code") or name[:2]).strip().upper()[:3]
    return {
        "name": name,
        "code": code,
        "logo_symbol": code or "FLY",
    }


def _traveler_price_lines(offer: Mapping[str, Any], price_breakdown: Mapping[str, Any]) -> list[dict[str, str]]:
    """Equal split of the current trip total for display (airlines may price passengers differently)."""
    passengers = offer.get("passengers") or []
    currency = str(price_breakdown.get("currency") or "USD").strip() or "USD"
    total_cents = _to_cents(str(price_breakdown.get("total_amount") or "0.00"))
    if not passengers:
        return [
            {
                "label": "Traveler 1",
                "type_label": "Adult",
                "amount": _from_cents(total_cents, currency),
            }
        ]
    n = len(passengers)
    base_share = total_cents // n
    remainder = total_cents - base_share * n
    lines: list[dict[str, str]] = []
    for idx, p in enumerate(passengers):
        share = base_share + (1 if idx < remainder else 0)
        lines.append(
            {
                "label": f"Traveler {idx + 1}",
                "type_label": _titleize_passenger_type(str((p or {}).get("type") or "adult")),
                "amount": _from_cents(share, currency),
            }
        )
    return lines


def _price_breakdown(
    resource: Mapping[str, Any],
    *,
    ancillaries_payload: Mapping[str, Any] | None = None,
    seat_maps: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    currency = str(resource.get("total_currency") or resource.get("currency") or "USD").strip() or "USD"
    base_total = _to_cents(str(resource.get("total_amount") or "0.00"))
    tax_total = _to_cents(str(resource.get("tax_amount") or resource.get("tax_total") or "0.00"))
    if tax_total and tax_total > base_total:
        tax_total = 0
    base_fare = max(base_total - tax_total, 0)
    payload = ancillaries_payload if isinstance(ancillaries_payload, Mapping) else {}
    catalog = _service_catalog(resource, seat_maps or [])
    raw_entries: list[Mapping[str, Any]] = []
    for key in ("selected_services", "services"):
        block = payload.get(key) or []
        if isinstance(block, list):
            for item in block:
                if isinstance(item, Mapping):
                    raw_entries.append(item)
    ancillaries_total = 0
    for svc in selected_services_from_payload(payload):
        sid = str(svc.get("id") or "").strip()
        if not sid:
            continue
        cents = _to_cents(str((catalog.get(sid) or {}).get("amount") or "0.00"))
        if cents == 0:
            cents = _to_cents(str(svc.get("amount") or svc.get("total_amount") or "0.00"))
        if cents == 0:
            for entry in raw_entries:
                eid = str(entry.get("id") or entry.get("service_id") or "").strip()
                if eid == sid:
                    cents = _to_cents(str(entry.get("amount") or entry.get("total_amount") or "0.00"))
                    if cents:
                        break
        ancillaries_total += cents
    total_amount = base_total + ancillaries_total
    return {
        "currency": currency,
        "base_fare_amount": _from_cents(base_fare, currency),
        "tax_amount": _from_cents(tax_total, currency),
        "ancillaries_amount": _from_cents(ancillaries_total, currency),
        "base_total_amount": _from_cents(base_total, currency),
        "total_amount": _from_cents(total_amount, currency),
        "service_fee_amount": "0.00",
    }


def _build_slice_summary(slice_data: Mapping[str, Any], idx: int) -> dict[str, Any] | None:
    segments = slice_data.get("segments") or []
    if not segments:
        return None
    first = segments[0]
    last = segments[-1]
    first_departure_at = parse_duffel_datetime(_segment_datetime(first, "departure"))
    last_arrival_at = parse_duffel_datetime(_segment_datetime(last, "arrival"))
    carriers = []
    seen_carriers = set()
    segment_cards = []
    segment_moments: list[dict[str, datetime | None]] = []
    for segment in segments:
        depart_at = parse_duffel_datetime(_segment_datetime(segment, "departure"))
        arrive_at = parse_duffel_datetime(_segment_datetime(segment, "arrival"))
        carrier_name = _carrier_name(segment)
        if carrier_name and carrier_name not in seen_carriers:
            seen_carriers.add(carrier_name)
            carriers.append(carrier_name)
        mkt = segment.get("marketing_carrier") or {}
        mark_fallback = str(mkt.get("iata_code") or "").strip().upper()[:2] or "—"
        segment_moments.append({"depart_at": depart_at, "arrive_at": arrive_at})
        segment_cards.append(
            {
                "carrier_name": carrier_name or "Carrier unavailable",
                "operating_carrier": _operating_carrier_name(segment),
                "flight_number": _flight_number(segment),
                "aircraft": _aircraft_name(segment),
                "aircraft_short": _aircraft_short_name(segment),
                "airline_logo_url": _segment_marketing_logo_url(segment),
                "airline_mark_fallback": mark_fallback,
                "origin_code": _airport_code(segment, "origin"),
                "destination_code": _airport_code(segment, "destination"),
                "origin_city": _airport_city(segment, "origin"),
                "destination_city": _airport_city(segment, "destination"),
                "origin_airport_name": _airport_name(segment, "origin"),
                "destination_airport_name": _airport_name(segment, "destination"),
                "depart_label": _format_datetime(depart_at),
                "arrive_label": _format_datetime(arrive_at),
                "depart_date_full": _format_full_date(depart_at),
                "arrive_date_full": _format_full_date(arrive_at),
                "depart_time": _format_time(depart_at),
                "arrive_time": _format_time(arrive_at),
                "depart_timezone_label": _timezone_label(segment, "origin", depart_at),
                "arrive_timezone_label": _timezone_label(segment, "destination", arrive_at),
                "depart_iso": _isoformat(depart_at),
                "arrive_iso": _isoformat(arrive_at),
                "departure_terminal": _terminal_label(segment, "origin"),
                "arrival_terminal": _terminal_label(segment, "destination"),
                "duration_label": _format_duration(segment.get("duration")),
            }
        )

    first_mkt = first.get("marketing_carrier") or {}
    slice_mark_fallback = str(first_mkt.get("iata_code") or "").strip().upper()[:2] or "—"
    connections = _connection_summaries(segment_cards, segment_moments)
    return {
        "label": _slice_label(idx),
        "airline_label": " + ".join(carriers) if carriers else "Airline unavailable",
        "airline_logo_url": _segment_marketing_logo_url(first),
        "airline_mark_fallback": slice_mark_fallback,
        "route": f"{_airport_code(first, 'origin')} → {_airport_code(last, 'destination')}",
        "city_route": f"{_airport_city(first, 'origin')} to {_airport_city(last, 'destination')}",
        "depart_label": _format_datetime(first_departure_at),
        "arrive_label": _format_datetime(last_arrival_at),
        "date_short_label": _format_compact_date(first_departure_at),
        "date_label": _format_date_span(
            first_departure_at,
            last_arrival_at,
        ),
        "duration_label": _format_duration(slice_data.get("duration")),
        "stops_label": _stops_label(max(0, len(segments) - 1)),
        "segment_count": len(segments),
        "segments": segment_cards,
        "connections": connections,
        "layover_summary": _layover_summary(segment_cards),
    }


def _build_flexibility_summary(offer: Mapping[str, Any]) -> dict[str, Any]:
    conditions = offer.get("conditions") or {}
    change_before = (((conditions.get("change_before_departure") or {}) if isinstance(conditions, Mapping) else {}))
    refund_before = (((conditions.get("refund_before_departure") or {}) if isinstance(conditions, Mapping) else {}))
    return {
        "changes": _condition_summary(change_before, fallback="Airline has not provided change rules."),
        "refunds": _condition_summary(refund_before, fallback="Airline has not provided refund rules."),
        "headline": "Ticket flexibility",
        "rail_summary": "Changes and refunds shown from the latest airline conditions.",
    }


def _build_ancillaries_summary(
    offer: Mapping[str, Any],
    seat_maps: list[dict[str, Any]],
    *,
    ancillaries_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    service_catalog = list(_service_catalog(offer, seat_maps).values())
    selected_services = _selected_service_cards(ancillaries_payload, service_catalog)
    bag_services = [item for item in service_catalog if item["category"] == "bags"]
    seat_services = [item for item in service_catalog if item["category"] == "seats"]
    passengers_list = list(offer.get("passengers") or [])
    passenger_ids = [str((p or {}).get("id") or "").strip() for p in passengers_list]
    seat_map_view = _seat_map_view_merged(seat_maps, service_catalog, passenger_ids)
    return {
        "headline": "Customize your trip",
        "supporting_copy": "Seats and baggage are pulled from the current airline offer.",
        "component_offer_id": str(offer.get("id") or ""),
        "component_client_key": str(offer.get("client_key") or ""),
        "component_ready": bool(offer.get("client_key")),
        "service_catalog": service_catalog,
        "selected_services": selected_services,
        "bags": bag_services,
        "seats": seat_services,
        "seat_map": seat_map_view,
        "has_services": bool(service_catalog),
        "fallback_message": "No extra seats or bags are available for this fare right now." if not service_catalog else "",
        "highlights": _ancillary_highlights(service_catalog),
    }


def _service_catalog(offer: Mapping[str, Any], seat_maps: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for service in offer.get("available_services") or []:
        item = _service_card(service, source="offer")
        if item:
            catalog[item["id"]] = item
    for seat_map in seat_maps or []:
        for cabin in seat_map.get("cabins") or []:
            for row in cabin.get("rows") or []:
                for section in row.get("sections") or []:
                    for element in section.get("elements") or []:
                        for service in element.get("available_services") or []:
                            item = _service_card(service, source="seat_map", seat=element)
                            if item:
                                catalog[item["id"]] = item
    return catalog


def _service_card(service: Mapping[str, Any], *, source: str, seat: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    service_id = str(service.get("id") or "").strip()
    if not service_id:
        return None
    amount = str((service.get("total_amount") or service.get("amount") or "0.00")).strip() or "0.00"
    currency = str(service.get("total_currency") or service.get("currency") or "USD").strip() or "USD"
    service_type = str(service.get("type") or service.get("service_type") or "").strip().lower()
    category = "seats" if "seat" in service_type or seat else "bags" if "bag" in service_type else "extras"
    passenger_id = str(service.get("passenger_id") or "").strip()
    segment_id = str(service.get("segment_id") or "").strip()
    metadata = service.get("metadata") or {}
    seat_designator = ""
    if seat:
        seat_designator = str(seat.get("designator") or seat.get("number") or "").strip()
    return {
        "id": service_id,
        "label": str(service.get("name") or service.get("description") or service.get("title") or _labelize_service_type(service_type) or "Extra").strip(),
        "category": category,
        "amount": amount,
        "currency": currency,
        "passenger_id": passenger_id,
        "segment_id": segment_id,
        "description": str(service.get("description") or metadata.get("description") or "").strip(),
        "included": _to_cents(amount) == 0,
        "recommended": category == "bags" and _to_cents(amount) > 0,
        "seat_designator": seat_designator,
        "characteristics": [str(item).strip() for item in (service.get("characteristics") or []) if str(item).strip()],
        "source": source,
    }


def _selected_service_cards(ancillaries_payload: Mapping[str, Any] | None, service_catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not ancillaries_payload:
        return []
    service_index = {item["id"]: item for item in service_catalog}
    selected: list[dict[str, Any]] = []
    for service in selected_services_from_payload(ancillaries_payload):
        service_id = str(service.get("id") or "").strip()
        if service_id and service_id in service_index:
            selected.append(service_index[service_id])
    return selected


def _ancillary_highlights(service_catalog: list[dict[str, Any]]) -> list[str]:
    highlights: list[str] = []
    if any(item["category"] == "seats" for item in service_catalog):
        highlights.append("Seat selection available")
    if any(item["category"] == "bags" and item["included"] for item in service_catalog):
        highlights.append("Bag allowance included")
    if any(item["category"] == "bags" and not item["included"] for item in service_catalog):
        highlights.append("Extra bags available")
    return highlights[:3]


def _seat_row_sort_key(row: Mapping[str, Any]) -> tuple[int, str, str]:
    """Order cabin rows front-to-back (numeric row, then suffix e.g. deck letters)."""
    raw = str(row.get("row_number") or "").strip()
    if not raw:
        return (10_000, "", raw)
    num_s = ""
    for ch in raw:
        if ch.isdigit():
            num_s += ch
        else:
            break
    suffix = raw[len(num_s) :].strip().upper()
    num = int(num_s) if num_s else 0
    return (num, suffix, raw)


def _seat_cells_to_groups(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert flat seat/gap cells into grouped rows expected by seat template."""
    groups: list[dict[str, Any]] = []
    seat_bucket: list[dict[str, Any]] = []
    for cell in cells or []:
        if str(cell.get("kind") or "").strip().lower() == "seat":
            seat_bucket.append(cell)
            continue
        if seat_bucket:
            groups.append({"kind": "seat_group", "seats": seat_bucket})
            seat_bucket = []
        groups.append({"kind": "aisle"})
    if seat_bucket:
        groups.append({"kind": "seat_group", "seats": seat_bucket})
    return groups


def _seat_header_cells_to_groups(header_cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert flat header cells into letter/aisle groups for column headers."""
    groups: list[dict[str, Any]] = []
    letter_bucket: list[str] = []
    for cell in header_cells or []:
        if str(cell.get("kind") or "").strip().lower() == "seat":
            letter_bucket.append(str(cell.get("label") or "").strip())
            continue
        if letter_bucket:
            groups.append({"kind": "letter_group", "letters": letter_bucket})
            letter_bucket = []
        groups.append({"kind": "aisle"})
    if letter_bucket:
        groups.append({"kind": "letter_group", "letters": letter_bucket})
    return groups


def _pick_seat_service_for_passenger(
    services: list[dict[str, Any]],
    *,
    passenger_id: str | None,
) -> dict[str, Any] | None:
    if not services:
        return None
    pid = (passenger_id or "").strip()
    if pid:
        for svc in services:
            if str(svc.get("passenger_id") or "").strip() == pid:
                return svc
    return services[0]


def _passenger_id_to_index(passenger_ids: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for idx, pid in enumerate(passenger_ids):
        if pid:
            out[pid] = idx
    return out


def _passenger_services_for_seat_element(
    services: list[dict[str, Any]],
    passenger_ids: list[str],
    pid_to_idx: dict[str, int],
) -> list[dict[str, Any]]:
    psvc: list[dict[str, Any]] = []
    for svc in services:
        pid = str(svc.get("passenger_id") or "").strip()
        idx = pid_to_idx.get(pid)
        if idx is not None:
            psvc.append({"passenger_index": idx, "service_id": str(svc.get("id") or "").strip()})
    psvc.sort(key=lambda x: int(x["passenger_index"]))
    if not psvc and services:
        if passenger_ids:
            for i, svc in enumerate(services):
                if i < len(passenger_ids):
                    sid = str(svc.get("id") or "").strip()
                    if sid:
                        psvc.append({"passenger_index": i, "service_id": sid})
        else:
            sid = str(services[0].get("id") or "").strip()
            if sid:
                psvc.append({"passenger_index": 0, "service_id": sid})
        psvc.sort(key=lambda x: int(x["passenger_index"]))
    seen: set[int] = set()
    deduped: list[dict[str, Any]] = []
    for item in psvc:
        i = int(item["passenger_index"])
        if i in seen:
            continue
        seen.add(i)
        deduped.append(item)
    return deduped


def _price_label_for_seat_services(catalog_rows: list[dict[str, Any]]) -> str:
    if not catalog_rows:
        return ""
    best = min(catalog_rows, key=lambda c: _to_cents(str(c.get("amount") or "0.00")))
    if best.get("included"):
        return "Included"
    return f'{best["currency"]} {best["amount"]}'


def _seat_map_view_merged(
    seat_maps: list[dict[str, Any]],
    service_catalog: list[dict[str, Any]],
    passenger_ids: list[str],
) -> list[dict[str, Any]]:
    """One seat map per segment; each seat lists per-passenger Duffel service ids."""
    catalog_by_id = {item["id"]: item for item in service_catalog}
    pid_to_idx = _passenger_id_to_index(passenger_ids)
    maps: list[dict[str, Any]] = []
    for idx, seat_map in enumerate(seat_maps or []):
        segment = seat_map.get("segment") or {}
        cabins_view = []
        for cabin in seat_map.get("cabins") or []:
            rows_view = []
            for row in cabin.get("rows") or []:
                row_number = str(row.get("number") or row.get("designator") or "").strip()
                cells = []
                for section_idx, section in enumerate(row.get("sections") or []):
                    if section_idx > 0 and cells and cells[-1].get("kind") == "seat":
                        cells.append({"kind": "gap"})
                    for element in section.get("elements") or []:
                        designator = str(element.get("designator") or element.get("number") or "").strip()
                        if not designator:
                            if str(element.get("type") or "").strip().lower() in {"aisle", "empty_space"}:
                                cells.append({"kind": "gap"})
                            continue
                        services = []
                        for service in element.get("available_services") or []:
                            service_id = str(service.get("id") or "").strip()
                            if service_id and service_id in catalog_by_id:
                                services.append(catalog_by_id[service_id])
                        psvc = _passenger_services_for_seat_element(services, passenger_ids, pid_to_idx)
                        cat_rows = [catalog_by_id[p["service_id"]] for p in psvc if p.get("service_id") in catalog_by_id]
                        price_label = _price_label_for_seat_services(cat_rows)
                        characteristics = (cat_rows[0].get("characteristics") if cat_rows else []) or []
                        first_id = str(psvc[0]["service_id"]) if psvc else ""
                        cells.append(
                            {
                                "kind": "seat",
                                "designator": designator,
                                "passenger_services": psvc,
                                "price_label": price_label,
                                "service_id": first_id,
                                "available": bool(psvc),
                                "characteristics": characteristics,
                            }
                        )
                if any(cell.get("kind") == "seat" for cell in cells):
                    rows_view.append(
                        {
                            "row_number": row_number,
                            "cells": cells,
                            "groups": _seat_cells_to_groups(cells),
                        }
                    )
            rows_view.sort(key=_seat_row_sort_key)
            if rows_view:
                header_source = max(
                    rows_view,
                    key=lambda row: (
                        len(row.get("cells") or []),
                        sum(1 for cell in row.get("cells") or [] if cell.get("kind") == "seat"),
                    ),
                )
                header_cells = [
                    (
                        {"kind": "gap"}
                        if cell.get("kind") != "seat"
                        else {"kind": "seat", "label": str(cell.get("designator") or "")[-1:]}
                    )
                    for cell in header_source.get("cells") or []
                ]
                cabins_view.append(
                    {
                        "cabin_class": str(cabin.get("cabin_class") or "").replace("_", " ").title(),
                        "deck": cabin.get("deck"),
                        "header_cells": header_cells,
                        "header_groups": _seat_header_cells_to_groups(header_cells),
                        "rows": rows_view,
                    }
                )
        if cabins_view:
            origin_code = _airport_code(segment, "origin")
            destination_code = _airport_code(segment, "destination")
            maps.append(
                {
                    "segment_title": f"{origin_code} to {destination_code}" if origin_code != "---" and destination_code != "---" else _slice_label(idx),
                    "origin_code": origin_code if origin_code != "---" else "",
                    "destination_code": destination_code if destination_code != "---" else "",
                    "segment_subtitle": _flight_number(segment) or _carrier_name(segment),
                    "cabins": cabins_view,
                }
            )
    return maps


def _seat_map_view(
    seat_maps: list[dict[str, Any]],
    service_catalog: list[dict[str, Any]],
    passenger_id: str | None = None,
) -> list[dict[str, Any]]:
    catalog_by_id = {item["id"]: item for item in service_catalog}
    maps: list[dict[str, Any]] = []
    for idx, seat_map in enumerate(seat_maps or []):
        segment = seat_map.get("segment") or {}
        cabins_view = []
        for cabin in seat_map.get("cabins") or []:
            rows_view = []
            for row in cabin.get("rows") or []:
                row_number = str(row.get("number") or row.get("designator") or "").strip()
                cells = []
                for section in row.get("sections") or []:
                    for element in section.get("elements") or []:
                        designator = str(element.get("designator") or element.get("number") or "").strip()
                        if not designator:
                            if str(element.get("type") or "").strip().lower() in {"aisle", "empty_space"}:
                                cells.append({"kind": "gap"})
                            continue
                        services = []
                        for service in element.get("available_services") or []:
                            service_id = str(service.get("id") or "").strip()
                            if service_id and service_id in catalog_by_id:
                                services.append(catalog_by_id[service_id])
                        chosen = _pick_seat_service_for_passenger(services, passenger_id=passenger_id)
                        cells.append(
                            {
                                "kind": "seat",
                                "designator": designator,
                                "price_label": (
                                    f'{chosen["currency"]} {chosen["amount"]}'
                                    if chosen and not chosen["included"]
                                    else ("Included" if chosen and chosen["included"] else "")
                                ),
                                "service_id": chosen["id"] if chosen else "",
                                "available": bool(chosen),
                                "characteristics": chosen["characteristics"] if chosen else [],
                            }
                        )
                if any(cell.get("kind") == "seat" for cell in cells):
                    rows_view.append({"row_number": row_number, "cells": cells})
            rows_view.sort(key=_seat_row_sort_key)
            if rows_view:
                header_source = max(
                    rows_view,
                    key=lambda row: (
                        len(row.get("cells") or []),
                        sum(1 for cell in row.get("cells") or [] if cell.get("kind") == "seat"),
                    ),
                )
                header_cells = [
                    (
                        {"kind": "gap"}
                        if cell.get("kind") != "seat"
                        else {"kind": "seat", "label": str(cell.get("designator") or "")[-1:]}
                    )
                    for cell in header_source.get("cells") or []
                ]
                cabins_view.append(
                    {
                        "cabin_class": str(cabin.get("cabin_class") or "").replace("_", " ").title(),
                        "deck": cabin.get("deck"),
                        "header_cells": header_cells,
                        "rows": rows_view,
                    }
                )
        if cabins_view:
            origin_code = _airport_code(segment, "origin")
            destination_code = _airport_code(segment, "destination")
            maps.append(
                {
                    "segment_title": f"{origin_code} to {destination_code}" if origin_code != "---" and destination_code != "---" else _slice_label(idx),
                    "origin_code": origin_code if origin_code != "---" else "",
                    "destination_code": destination_code if destination_code != "---" else "",
                    "segment_subtitle": _flight_number(segment) or _carrier_name(segment),
                    "cabins": cabins_view,
                }
            )
    return maps


def _summarize_order_services(order: Mapping[str, Any]) -> dict[str, Any]:
    services = order.get("services") or []
    selected_services = []
    for service in services:
        item = _service_card(service, source="order")
        if item:
            selected_services.append(item)
    return {
        "selected_services": selected_services,
        "has_services": bool(selected_services),
    }


def _hold_supported(offer: Mapping[str, Any]) -> bool:
    payment_requirements = offer.get("payment_requirements") or {}
    return bool(payment_requirements.get("requires_immediate_payment") is False)


def _identity_document_types(offer: Mapping[str, Any]) -> list[str]:
    document_types = offer.get("supported_passenger_identity_document_types") or []
    normalized = [str(item).strip().lower() for item in document_types if str(item).strip()]
    return normalized or ["passport"]


def _condition_summary(condition: Mapping[str, Any], *, fallback: str) -> dict[str, str]:
    if not condition:
        return {"status": "unknown", "label": fallback, "penalty": ""}
    allowed = condition.get("allowed")
    penalty = condition.get("penalty_amount") or condition.get("penalty_amount_currency") or condition.get("penalty_currency")
    penalty_amount = str(condition.get("penalty_amount") or "").strip()
    penalty_currency = str(condition.get("penalty_currency") or "").strip()
    penalty_label = ""
    if penalty_amount:
        penalty_label = f"{penalty_currency} {penalty_amount}".strip()
    if allowed is True:
        label = "Allowed"
        if penalty_label:
            label = f"Allowed with {penalty_label} penalty"
        return {"status": "allowed", "label": label, "penalty": penalty_label}
    if allowed is False:
        return {"status": "not_allowed", "label": "Not allowed", "penalty": penalty_label}
    return {"status": "unknown", "label": fallback, "penalty": penalty_label}


def _slice_label(idx: int) -> str:
    if idx == 0:
        return "Outbound"
    if idx == 1:
        return "Return"
    return f"Leg {idx + 1}"


def _segment_datetime(segment: Mapping[str, Any], direction: str) -> str | None:
    if direction == "departure":
        return segment.get("departing_at") or (segment.get("departure") or {}).get("at")
    return segment.get("arriving_at") or (segment.get("arrival") or {}).get("at")


def _airport_code(segment: Mapping[str, Any], key: str) -> str:
    if key == "origin":
        return (
            ((segment.get("origin") or {}).get("iata_code"))
            or ((segment.get("departure") or {}).get("iataCode"))
            or "---"
        ).strip().upper()
    return (
        ((segment.get("destination") or {}).get("iata_code"))
        or ((segment.get("arrival") or {}).get("iataCode"))
        or "---"
    ).strip().upper()


def _airport_city(segment: Mapping[str, Any], key: str) -> str:
    node = segment.get("origin") if key == "origin" else segment.get("destination")
    if not isinstance(node, Mapping):
        node = segment.get("departure") if key == "origin" else segment.get("arrival")
    city = str((node or {}).get("city_name") or (node or {}).get("city") or "").strip()
    return city or _airport_code(segment, key)


def _airport_name(segment: Mapping[str, Any], key: str) -> str:
    node = segment.get("origin") if key == "origin" else segment.get("destination")
    if not isinstance(node, Mapping):
        node = segment.get("departure") if key == "origin" else segment.get("arrival")
    name = str((node or {}).get("name") or (node or {}).get("airport_name") or "").strip()
    name = name.replace("International", "Intl") if name else ""
    return name or _airport_city(segment, key)


def _timezone_label(segment: Mapping[str, Any], key: str, value: datetime | None) -> str:
    node = segment.get("origin") if key == "origin" else segment.get("destination")
    if not isinstance(node, Mapping):
        node = segment.get("departure") if key == "origin" else segment.get("arrival")

    explicit = str(
        (node or {}).get("time_zone_abbreviation")
        or (node or {}).get("timezone_abbreviation")
        or (node or {}).get("tz_abbreviation")
        or ""
    ).strip().upper()
    if explicit:
        return explicit[:6]

    zone_name = str(
        (node or {}).get("time_zone")
        or (node or {}).get("timezone")
        or (node or {}).get("iana_time_zone")
        or ""
    ).strip()
    if not zone_name:
        zone_name = _AIRPORT_TIMEZONE_FALLBACKS.get(_airport_code(segment, key), "")
    if not (zone_name and value and ZoneInfo):
        return ""
    try:
        return value.astimezone(ZoneInfo(zone_name)).strftime("%Z")
    except Exception:
        return ""


def _terminal_label(segment: Mapping[str, Any], key: str) -> str:
    node = segment.get("origin") if key == "origin" else segment.get("destination")
    if not isinstance(node, Mapping):
        node = segment.get("departure") if key == "origin" else segment.get("arrival")
    terminal = str((node or {}).get("terminal") or "").strip()
    return f"Terminal {terminal}" if terminal else ""


def _carrier_name(segment: Mapping[str, Any]) -> str:
    marketing = segment.get("marketing_carrier") or {}
    operating = segment.get("operating_carrier") or {}
    return (
        (marketing.get("name") or "").strip()
        or (operating.get("name") or "").strip()
        or (marketing.get("iata_code") or operating.get("iata_code") or "").strip().upper()
    )


def _operating_carrier_name(segment: Mapping[str, Any]) -> str:
    marketing = segment.get("marketing_carrier") or {}
    operating = segment.get("operating_carrier") or {}
    marketing_name = str(marketing.get("name") or "").strip()
    operating_name = str(operating.get("name") or "").strip()
    if operating_name and operating_name != marketing_name:
        return operating_name
    return ""


def _flight_number(segment: Mapping[str, Any]) -> str:
    marketing = segment.get("marketing_carrier") or {}
    flight_number = str(segment.get("marketing_carrier_flight_number") or segment.get("flight_number") or "").strip()
    carrier_code = str(marketing.get("iata_code") or "").strip().upper()
    return f"{carrier_code} {flight_number}".strip()


def _aircraft_name(segment: Mapping[str, Any]) -> str:
    aircraft = segment.get("aircraft") or {}
    return str(aircraft.get("name") or aircraft.get("iata_code") or "").strip()


def _aircraft_short_name(segment: Mapping[str, Any]) -> str:
    aircraft = segment.get("aircraft") or {}
    iata_code = str(aircraft.get("iata_code") or "").strip().upper()
    name = str(aircraft.get("name") or "").strip()
    key = name.lower()
    if not name:
        return iata_code

    explicit_patterns = [
        (r"\b(crj|canadair|regional jet)\D*900\b", "CRJ-900"),
        (r"\b(crj|canadair|regional jet)\D*700\b", "CRJ-700"),
        (r"\b(crj|canadair|regional jet)\D*200\b", "CRJ-200"),
        (r"\bembraer\D*195\b|\be195\b", "E195"),
        (r"\bembraer\D*190\b|\be190\b", "E190"),
        (r"\bembraer\D*175\b|\be175\b", "E175"),
        (r"\bembraer\D*170\b|\be170\b", "E170"),
        (r"\bboeing\D*787\D*9\b|\b787-9\b", "B787-9"),
        (r"\bboeing\D*787\D*8\b|\b787-8\b", "B787-8"),
        (r"\bboeing\D*787\b|\b787\b", "B787"),
        (r"\bboeing\D*777\D*300\b|\b777-300\b", "B777-300"),
        (r"\bboeing\D*777\b|\b777\b", "B777"),
        (r"\bboeing\D*767\b|\b767\b", "B767"),
        (r"\bboeing\D*757\b|\b757\b", "B757"),
        (r"\bboeing\D*737\D*max\D*8\b|\b737 max 8\b", "B737 MAX 8"),
        (r"\bboeing\D*737\b|\b737\b", "B737"),
        (r"\bairbus\D*a350\D*900\b|\ba350-900\b", "A350-900"),
        (r"\bairbus\D*a350\b|\ba350\b", "A350"),
        (r"\bairbus\D*a330\b|\ba330\b", "A330"),
        (r"\bairbus\D*a321\b|\ba321\b", "A321"),
        (r"\bairbus\D*a320\b|\ba320\b", "A320"),
        (r"\bairbus\D*a319\b|\ba319\b", "A319"),
        (r"\bairbus\D*a220\D*300\b|\ba220-300\b", "A220-300"),
        (r"\bairbus\D*a220\b|\ba220\b", "A220"),
    ]
    for pattern, label in explicit_patterns:
        if re.search(pattern, key):
            return label

    if iata_code:
        return iata_code

    compact = re.search(r"\b([a-z])\s*[- ]?(\d{3})(?:[- ]?(\d{1,3}))?\b", key)
    if compact:
        suffix = f"-{compact.group(3)}" if compact.group(3) else ""
        return f"{compact.group(1).upper()}{compact.group(2)}{suffix}"

    return name


def _cabin_label(offer: Mapping[str, Any]) -> str:
    for slice_data in offer.get("slices") or []:
        for segment in slice_data.get("segments") or []:
            for passenger in segment.get("passengers") or []:
                label = (passenger.get("cabin_class_marketing_name") or passenger.get("cabin_class") or "").strip()
                if label:
                    return label
    return ""


def _fare_brand(offer: Mapping[str, Any]) -> str:
    for slice_data in offer.get("slices") or []:
        for segment in slice_data.get("segments") or []:
            for passenger in segment.get("passengers") or []:
                label = str(passenger.get("fare_brand_name") or passenger.get("fare_brand") or "").strip()
                if label:
                    return label
    return ""


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return f"{value.strftime('%a, %b %d')} · {value.strftime('%I:%M %p').lstrip('0')}"


def _format_full_date(value: datetime | None) -> str:
    if value is None:
        return ""
    day = value.strftime("%d").lstrip("0") or "0"
    return f"{value.strftime('%a')}, {value.strftime('%b')} {day}, {value.strftime('%Y')}"


def _format_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%I:%M %p").lstrip("0")


def _format_date_span(start: datetime | None, end: datetime | None) -> str:
    if start is None:
        return ""
    if end is None or start.date() == end.date():
        return start.strftime("%A, %b %d")
    return f"{start.strftime('%b %d')} - {end.strftime('%b %d')}"


def _format_compact_date(value: datetime | None) -> str:
    if value is None:
        return ""
    day = value.strftime("%d").lstrip("0") or "0"
    return f"{day} {value.strftime('%b %Y')}"


def _isoformat(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(timezone.utc).isoformat()


def _format_duration(duration: str | None) -> str:
    if not duration:
        return ""
    match = re.fullmatch(r"P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?", duration)
    if not match:
        return duration
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0) + (days * 24)
    minutes = int(match.group("minutes") or 0)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _format_duration_minutes(minutes: int | None) -> str:
    if minutes is None or minutes < 0:
        return ""
    hours, remainder = divmod(minutes, 60)
    if hours and remainder:
        return f"{hours}h {remainder}m"
    if hours:
        return f"{hours}h"
    return f"{remainder}m"


def _minutes_between_datetimes(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    left = start
    right = end
    if (left.tzinfo is None) != (right.tzinfo is None):
        left = left.replace(tzinfo=None)
        right = right.replace(tzinfo=None)
    try:
        minutes = int((right - left).total_seconds() // 60)
    except TypeError:
        return None
    if minutes < 0:
        return None
    return minutes


def _connection_summaries(
    segments: list[dict[str, Any]],
    moments: list[dict[str, datetime | None]],
) -> list[dict[str, str]]:
    connections: list[dict[str, str]] = []
    for idx in range(max(0, len(segments) - 1)):
        inbound = segments[idx]
        outbound = segments[idx + 1]
        inbound_moment = moments[idx] if idx < len(moments) else {}
        outbound_moment = moments[idx + 1] if idx + 1 < len(moments) else {}
        layover_minutes = _minutes_between_datetimes(
            inbound_moment.get("arrive_at"),
            outbound_moment.get("depart_at"),
        )
        connections.append(
            {
                "label": f"Stop {idx + 1}",
                "airport_code": inbound.get("destination_code") or outbound.get("origin_code") or "",
                "airport_city": inbound.get("destination_city") or outbound.get("origin_city") or "",
                "duration_label": _format_duration_minutes(layover_minutes),
                "arrive_time": inbound.get("arrive_time") or "",
                "arrive_label": inbound.get("arrive_label") or "",
                "depart_time": outbound.get("depart_time") or "",
                "depart_label": outbound.get("depart_label") or "",
                "arrival_terminal": inbound.get("arrival_terminal") or "",
                "departure_terminal": outbound.get("departure_terminal") or "",
                "incoming_flight_number": inbound.get("flight_number") or "",
                "outgoing_flight_number": outbound.get("flight_number") or "",
            }
        )
    return connections


def _layover_summary(segments: list[dict[str, Any]]) -> str:
    layover_count = max(0, len(segments) - 1)
    if layover_count <= 0:
        return "Direct journey"
    if layover_count == 1:
        return "1 connection"
    return f"{layover_count} connections"


def _stops_label(stops: int) -> str:
    if stops <= 0:
        return "Nonstop"
    if stops == 1:
        return "1 stop"
    return f"{stops} stops"


def _titleize_passenger_type(value: str) -> str:
    return value.replace("_", " ").strip().title()


def _labelize_service_type(value: str) -> str:
    return value.replace("_", " ").strip().title()


def _field_name(idx: int, key: str) -> str:
    return f"traveler_{idx}_{key}"


def _value(values: Mapping[str, Any], idx: int, key: str, fallback: str | None = None) -> str:
    raw = values.get(_field_name(idx, key), fallback or "")
    return str(raw or "").strip()


def _require(errors: dict[str, str], field_name: str, value: str, message: str) -> None:
    if not value:
        errors[field_name] = message


def _normalize_phone_number(value: str) -> str | None:
    normalized = PHONE_CLEAN_RE.sub("", (value or "").strip())
    if not normalized:
        return None
    if "+" in normalized[1:]:
        return None
    digits = re.sub(r"\D", "", normalized)
    if len(digits) < 8 or len(digits) > 15 or not normalized.startswith("+"):
        return None
    return f"+{digits}"


def _validate_birth_date(errors: dict[str, str], idx: int, value: str) -> None:
    field_name = _field_name(idx, "born_on")
    try:
        born_on = date.fromisoformat(value)
    except ValueError:
        errors[field_name] = "Use a valid date of birth."
        return
    if born_on >= date.today():
        errors[field_name] = "Date of birth must be in the past."


def _validate_future_date(errors: dict[str, str], idx: int, key: str, value: str, message: str) -> None:
    field_name = _field_name(idx, key)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        errors[field_name] = message
        return
    if parsed <= date.today():
        errors[field_name] = "This date must be in the future."


def _passenger_name(passenger: Mapping[str, Any]) -> str:
    given_name = str(passenger.get("given_name") or "").strip()
    family_name = str(passenger.get("family_name") or "").strip()
    return " ".join(part for part in [given_name, family_name] if part).strip()


def _status_label(order: Mapping[str, Any]) -> str:
    # 1. booking_status string — most direct signal
    booking_status = str(order.get("booking_status") or "").strip().lower()
    if booking_status in {"cancelled", "canceled", "voided", "void"}:
        return "Cancelled"

    # 2. cancelled_at timestamp — Duffel sets this even before booking_status propagates
    cancelled_at = str(order.get("cancelled_at") or "").strip()
    if cancelled_at and cancelled_at.lower() not in {"none", "null", ""}:
        return "Cancelled"

    # 3. confirmed order_cancellations — exists when cancellation was confirmed via API
    order_cancellations = order.get("order_cancellations") or []
    if isinstance(order_cancellations, list):
        for oc in order_cancellations:
            if isinstance(oc, dict):
                confirmed_at = str(oc.get("confirmed_at") or "").strip()
                if confirmed_at and confirmed_at.lower() not in {"none", "null", ""}:
                    return "Cancelled"

    # 4. status / payment_status fallback
    if booking_status and booking_status not in {"none", "null", "confirmed", ""}:
        return booking_status.replace("_", " ").title()

    payment_status = order.get("payment_status")
    if isinstance(payment_status, dict):
        awaiting = payment_status.get("awaiting_payment")
        if awaiting is True:
            return "Payment Pending"
        return "Confirmed"
    raw = order.get("booking_status") or order.get("status") or payment_status
    if isinstance(raw, dict):
        raw = raw.get("status") or raw.get("value") or "confirmed"
    label = str(raw or "confirmed").strip()
    if not label or label.lower() in {"none", "null", "false"}:
        return "Confirmed"
    return label.replace("_", " ").title()


def _to_cents(value: str) -> int:
    normalized = str(value or "0.00").strip().replace(",", "")
    if not normalized:
        return 0
    negative = normalized.startswith("-")
    if negative:
        normalized = normalized[1:]
    if "." in normalized:
        whole, frac = normalized.split(".", 1)
    else:
        whole, frac = normalized, "0"
    whole = re.sub(r"\D", "", whole or "0") or "0"
    frac = re.sub(r"\D", "", frac or "0")
    frac = (frac + "00")[:2]
    cents = int(whole) * 100 + int(frac)
    return -cents if negative else cents


def _from_cents(value: int, currency: str) -> str:
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"
