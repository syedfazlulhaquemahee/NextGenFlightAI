from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from typing import Any

ROLE_ORDER = {
    "agent_user": 10,
    "agency_admin": 20,
    "skairova_admin": 30,
    "super_admin": 40,
}

ROLE_PERMISSIONS = {
    "agent_user": {
        "agent.dashboard.view",
        "agent.bookings.own",
    },
    "agency_admin": {
        "agent.dashboard.view",
        "agent.users.view",
        "agent.users.disable",       # disable users in own agency only
        "agent.users.request",       # request adding a user (needs super admin approval)
        "agent.bookings.view",
        "agent.bookings.own",
        "agent.finance.view",        # own agency earnings only (platform profit hidden)
        "agent.logs.view",
    },
    "skairova_admin": {
        "agent.dashboard.view",
        "agent.users.view",
        "agent.users.create",
        "agent.users.disable",
        "agent.agencies.view",
        "agent.bookings.view",
        "agent.bookings.own",
        "agent.finance.view",
        "agent.logs.view",
        "agent.requests.review",     # approve/reject user add requests
    },
    "super_admin": {
        "agent.dashboard.view",
        "agent.users.view",
        "agent.users.create",
        "agent.users.disable",
        "agent.users.manage",        # password reset, full user control
        "agent.platform.manage",
        "agent.agencies.view",
        "agent.bookings.view",
        "agent.bookings.own",
        "agent.finance.view",
        "agent.finance.disburse",
        "agent.markup.manage",
        "agent.logs.view",
        "agent.requests.review",
    },
}


def normalize_role(value: Any, default: str = "agent_user") -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in ROLE_ORDER else default


def resolve_effective_role(global_role: Any, membership_role: Any) -> str:
    left = normalize_role(global_role)
    right = normalize_role(membership_role, default=left)
    return left if ROLE_ORDER[left] >= ROLE_ORDER[right] else right


def role_permissions(global_role: Any, membership_role: Any) -> set[str]:
    effective = resolve_effective_role(global_role, membership_role)
    permissions = set(ROLE_PERMISSIONS.get(effective, set()))
    if effective in {"skairova_admin", "super_admin"}:
        permissions.update(ROLE_PERMISSIONS.get("agency_admin", set()))
    return permissions


def has_permission(global_role: Any, membership_role: Any, permission: str) -> bool:
    return permission in role_permissions(global_role, membership_role)


def generate_password_salt() -> str:
    return os.urandom(16).hex()


def hash_password(password: str, salt_hex: str) -> str:
    salt = bytes.fromhex(str(salt_hex or "").strip())
    return hashlib.pbkdf2_hmac(
        "sha256",
        str(password or "").encode("utf-8"),
        salt,
        240_000,
    ).hex()


def verify_password(password: str, salt_hex: str, expected_hash: str) -> bool:
    if not salt_hex or not expected_hash:
        return False
    actual = hash_password(password, salt_hex)
    return hmac.compare_digest(actual, str(expected_hash or "").strip())


def generate_totp_secret(length: int = 32) -> str:
    raw = secrets.token_bytes(max(10, length))
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _totp_counter(for_time: int | None = None, interval: int = 30) -> int:
    now_value = int(time.time() if for_time is None else for_time)
    return now_value // max(1, int(interval))


def generate_totp_code(secret: str, for_time: int | None = None, interval: int = 30, digits: int = 6) -> str:
    normalized = str(secret or "").strip().upper()
    if not normalized:
        return ""
    padding = "=" * ((8 - (len(normalized) % 8)) % 8)
    key = base64.b32decode(normalized + padding, casefold=True)
    counter = _totp_counter(for_time=for_time, interval=interval)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


def verify_totp_code(secret: str, code: Any, window: int = 1, interval: int = 30, digits: int = 6) -> bool:
    candidate = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(candidate) != digits:
        return False
    now_value = int(time.time())
    for offset in range(-max(0, int(window)), max(0, int(window)) + 1):
        if hmac.compare_digest(
            generate_totp_code(secret, for_time=now_value + (offset * interval), interval=interval, digits=digits),
            candidate,
        ):
            return True
    return False


def generate_backup_codes(count: int = 8) -> list[str]:
    codes: list[str] = []
    for _ in range(max(1, int(count))):
        raw = secrets.token_bytes(6).hex().upper()  # 48 bits of entropy
        codes.append(f"{raw[:4]}-{raw[4:8]}-{raw[8:]}")
    return codes


def hash_backup_code(code: Any) -> str:
    normalized = str(code or "").strip().upper().replace(" ", "").replace("-", "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def device_label(user_agent: Any) -> str:
    raw = str(user_agent or "").strip()
    if not raw:
        return "Unknown device"
    lowered = raw.lower()
    if "iphone" in lowered:
        return "iPhone browser"
    if "ipad" in lowered:
        return "iPad browser"
    if "android" in lowered:
        return "Android browser"
    if "mac os x" in lowered or "macintosh" in lowered:
        return "Mac browser"
    if "windows" in lowered:
        return "Windows browser"
    if "linux" in lowered:
        return "Linux browser"
    return "Web browser"
