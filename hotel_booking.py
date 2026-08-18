"""Hotel checkout: holder/guest form building + validation.

Mirrors duffel_booking.py's shape for the flight side (build_traveler_forms /
validate_checkout_form), but LiteAPI's book() only needs a holder (primary
contact) and one guest name per room — no passport/DOB, matching what
LiteAPI's POST /rates/book actually asks for (holder: firstName, lastName,
email, phone; guests: occupancyNumber, firstName, lastName, email).
"""

from __future__ import annotations

import re
from typing import Any, Mapping

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_phone_number(value: str) -> str:
    digits = re.sub(r"[^\d+]", "", value or "")
    if digits and not digits.startswith("+"):
        digits = "+" + digits.lstrip("+")
    return digits if len(digits) >= 8 else ""


def build_hotel_traveler_form(*, room_count: int, form: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Holder (primary contact) + one guest-name pair per room.

    `form` re-hydrates whatever the user already typed after a validation
    failure, so a rejected submission doesn't make them start over.
    """
    values = form or {}
    holder = {
        "first_name": str(values.get("holder_first_name") or "").strip(),
        "last_name": str(values.get("holder_last_name") or "").strip(),
        "email": str(values.get("holder_email") or "").strip(),
        "phone": str(values.get("holder_phone") or "").strip(),
    }
    guests = [
        {
            "index": i,
            "first_name": str(values.get(f"guest_{i}_first_name") or "").strip(),
            "last_name": str(values.get(f"guest_{i}_last_name") or "").strip(),
        }
        for i in range(max(1, room_count))
    ]
    return {"holder": holder, "guests": guests}


def validate_hotel_checkout_form(
    *, room_count: int, form: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, str]]:
    """Validate a submitted holder+guests form.

    Returns (holder_payload, guests_payload, errors). `holder_payload` is
    None when there are errors — callers should treat a non-empty `errors`
    dict as the source of truth, not the presence/absence of the payload.
    """
    errors: dict[str, str] = {}
    first_name = str(form.get("holder_first_name") or "").strip()
    last_name = str(form.get("holder_last_name") or "").strip()
    email = str(form.get("holder_email") or "").strip().lower()
    phone = _normalize_phone_number(str(form.get("holder_phone") or ""))

    if not first_name:
        errors["holder_first_name"] = "Enter a first name."
    if not last_name:
        errors["holder_last_name"] = "Enter a last name."
    if not EMAIL_RE.match(email):
        errors["holder_email"] = "Enter a valid email address."
    if not phone:
        errors["holder_phone"] = "Enter a phone number with country code, like +15551234567."

    guests_payload: list[dict[str, Any]] = []
    for i in range(max(1, room_count)):
        g_first = str(form.get(f"guest_{i}_first_name") or "").strip()
        g_last = str(form.get(f"guest_{i}_last_name") or "").strip()
        if not g_first:
            errors[f"guest_{i}_first_name"] = "Enter a first name."
        if not g_last:
            errors[f"guest_{i}_last_name"] = "Enter a last name."
        guests_payload.append(
            {
                "occupancyNumber": i + 1,
                "firstName": g_first,
                "lastName": g_last,
                "email": email,
            }
        )

    if errors:
        return None, guests_payload, errors

    holder_payload = {"firstName": first_name, "lastName": last_name, "email": email, "phone": phone}
    return holder_payload, guests_payload, errors
