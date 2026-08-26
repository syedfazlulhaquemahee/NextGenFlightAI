from __future__ import annotations

import base64
import json
import os
import re
import smtplib
import ssl
import subprocess
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from typing import Any, Mapping, Sequence

import requests

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_HERE = os.path.dirname(os.path.abspath(__file__))
_TSX_BIN = os.path.join(_HERE, "node_modules", ".bin", "tsx")
_RENDER_SCRIPT = os.path.join(_HERE, "emails", "render.tsx")


def _render_react_email(template: str, data: dict[str, Any]) -> str | None:
    """Render a React Email template via the Node.js CLI. Returns HTML or None."""
    if not os.path.exists(_TSX_BIN) or not os.path.exists(_RENDER_SCRIPT):
        return None
    try:
        result = subprocess.run(
            [_TSX_BIN, _RENDER_SCRIPT, template, json.dumps(data, ensure_ascii=False)],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=_HERE,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        if result.stderr:
            print(f"[email_service] React render warning ({template}):", result.stderr[:300])
        return None
    except Exception as exc:
        print(f"[email_service] React render error ({template}):", exc)
        return None


@dataclass(frozen=True)
class SMTPSettings:
    enabled: bool
    host: str
    port: int
    username: str
    password: str
    use_tls: bool
    use_ssl: bool
    timeout_seconds: float
    from_email: str
    from_name: str
    reply_to: str
    dev_code_fallback: bool


def _normalize_email(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().lower())


def _public_base_url() -> str:
    for env_key in ("NGF_PUBLIC_BASE_URL", "NGF_APP_BASE_URL", "NGF_SITE_URL"):
        value = str(os.getenv(env_key, "")).strip()
        if value:
            return value.rstrip("/")
    return ""


def _build_tls_context() -> ssl.SSLContext:
    ca_bundle = str(os.getenv("NGF_SMTP_CA_BUNDLE", "")).strip()
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _parse_bool(value: str | None, default: bool) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def load_smtp_settings() -> SMTPSettings:
    host = str(os.getenv("NGF_SMTP_HOST", "")).strip()
    from_email = _normalize_email(os.getenv("NGF_EMAIL_FROM", ""))
    username = str(os.getenv("NGF_SMTP_USERNAME", "")).strip()
    password = str(os.getenv("NGF_SMTP_PASSWORD", "")).strip()

    use_ssl = _parse_bool(os.getenv("NGF_SMTP_USE_SSL"), False)
    use_tls = _parse_bool(os.getenv("NGF_SMTP_USE_TLS"), not use_ssl)
    default_port = 465 if use_ssl else 587

    enabled_raw = str(os.getenv("NGF_EMAIL_ENABLED", "auto")).strip().lower()
    auto_enabled = bool(host and from_email)
    if enabled_raw in {"1", "true", "yes", "on"}:
        enabled = True
    elif enabled_raw in {"0", "false", "no", "off"}:
        enabled = False
    else:
        enabled = auto_enabled

    return SMTPSettings(
        enabled=enabled,
        host=host,
        port=int(str(os.getenv("NGF_SMTP_PORT", default_port)).strip() or default_port),
        username=username,
        password=password,
        use_tls=use_tls,
        use_ssl=use_ssl,
        timeout_seconds=float(str(os.getenv("NGF_SMTP_TIMEOUT_SECONDS", "10")).strip() or "10"),
        from_email=from_email,
        from_name=str(os.getenv("NGF_EMAIL_FROM_NAME", "Skairova")).strip() or "Skairova",
        reply_to=_normalize_email(os.getenv("NGF_EMAIL_REPLY_TO", "")),
        dev_code_fallback=_parse_bool(os.getenv("NGF_EMAIL_DEV_CODE_FALLBACK"), False),
    )


def smtp_is_configured(settings: SMTPSettings | None = None) -> bool:
    conf = settings or load_smtp_settings()
    return bool(conf.enabled and conf.host and conf.from_email)


def allow_dev_code_fallback(settings: SMTPSettings | None = None) -> bool:
    conf = settings or load_smtp_settings()
    return bool(conf.dev_code_fallback)


def _normalize_recipients(to_emails: str | Sequence[str]) -> list[str]:
    if isinstance(to_emails, str):
        candidates = [to_emails]
    else:
        candidates = list(to_emails or [])
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        email = _normalize_email(raw)
        if not email or not _EMAIL_RE.match(email):
            continue
        if email in seen:
            continue
        seen.add(email)
        out.append(email)
    return out


def resend_is_configured() -> bool:
    return bool(str(os.getenv("RESEND_API_KEY", "")).strip())


def _send_via_resend(
    *,
    recipients: list[str],
    subject: str,
    text_body: str,
    html_body: str,
    attachments: list[dict[str, Any]] | None,
    from_email: str,
    from_name: str,
    reply_to: str,
) -> tuple[bool, str]:
    """Resend's HTTP API (port 443) instead of raw SMTP sockets (ports 25/
    465/587) — Render's free tier blocks outbound traffic to SMTP ports
    entirely (confirmed live: every send failed with smtp_error:OSError),
    so this is the transport that actually works there, not just a nicer
    API. Same From/Reply-To identity as the SMTP path (NGF_EMAIL_FROM*)."""
    api_key = str(os.getenv("RESEND_API_KEY", "")).strip()
    payload: dict[str, Any] = {
        "from": formataddr((from_name, from_email)) if from_name else from_email,
        "to": recipients,
        "subject": subject,
        "text": text_body,
    }
    if html_body.strip():
        payload["html"] = html_body
    if reply_to:
        payload["reply_to"] = reply_to
    if attachments:
        payload["attachments"] = [
            {
                "filename": att.get("filename", "attachment"),
                "content": base64.b64encode(att["data"]).decode("ascii"),
            }
            for att in attachments
        ]

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
    except requests.RequestException as exc:
        return False, f"resend_error:{type(exc).__name__}"

    if resp.status_code in (200, 201):
        return True, "sent"
    try:
        detail = str(resp.json().get("message") or "").strip() or resp.text[:200]
    except Exception:
        detail = resp.text[:200]
    return False, f"resend_error:{resp.status_code}:{detail}"


def send_email(
    *,
    to_emails: str | Sequence[str],
    subject: str,
    text_body: str,
    html_body: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    """
    attachments: list of dicts with keys:
        data     bytes  - raw file bytes
        filename str    - e.g. "itinerary-ABC123.pdf"
        maintype str    - e.g. "application"
        subtype  str    - e.g. "pdf"
    """
    recipients = _normalize_recipients(to_emails)
    if not recipients:
        return False, "invalid_recipient"

    if resend_is_configured():
        from_email = _normalize_email(os.getenv("NGF_EMAIL_FROM", ""))
        if not from_email:
            return False, "email_not_configured"
        return _send_via_resend(
            recipients=recipients,
            subject=str(subject or "Skairova"),
            text_body=text_body,
            html_body=html_body,
            attachments=attachments,
            from_email=from_email,
            from_name=str(os.getenv("NGF_EMAIL_FROM_NAME", "Skairova")).strip() or "Skairova",
            reply_to=_normalize_email(os.getenv("NGF_EMAIL_REPLY_TO", "")),
        )

    conf = load_smtp_settings()
    if not smtp_is_configured(conf):
        return False, "email_not_configured"

    message = EmailMessage()
    message["Subject"] = str(subject or "Skairova")
    message["From"] = formataddr((conf.from_name, conf.from_email)) if conf.from_name else conf.from_email
    message["To"] = ", ".join(recipients)
    if conf.reply_to:
        message["Reply-To"] = conf.reply_to
    message.set_content(text_body)
    if html_body.strip():
        message.add_alternative(html_body, subtype="html")
    for att in (attachments or []):
        try:
            message.add_attachment(
                att["data"],
                maintype=att.get("maintype", "application"),
                subtype=att.get("subtype", "octet-stream"),
                filename=att.get("filename", "attachment"),
            )
        except Exception as exc:
            print(f"[email_service] attachment error ({att.get('filename')}): {exc}")

    try:
        tls_context = _build_tls_context()
        if conf.use_ssl:
            with smtplib.SMTP_SSL(conf.host, conf.port, timeout=conf.timeout_seconds, context=tls_context) as server:
                if conf.username:
                    server.login(conf.username, conf.password)
                server.send_message(message)
        else:
            with smtplib.SMTP(conf.host, conf.port, timeout=conf.timeout_seconds) as server:
                if conf.use_tls:
                    server.starttls(context=tls_context)
                if conf.username:
                    server.login(conf.username, conf.password)
                server.send_message(message)
        return True, "sent"
    except smtplib.SMTPAuthenticationError as exc:
        smtp_code = int(getattr(exc, "smtp_code", 0) or 0)
        return False, f"smtp_error:SMTPAuthenticationError:{smtp_code}"
    except Exception as exc:
        return False, f"smtp_error:{type(exc).__name__}"


# ─────────────────────────────────────────────────────────────────────────────
# Shared email building blocks
# ─────────────────────────────────────────────────────────────────────────────

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO = "'SFMono-Regular',Menlo,Monaco,Consolas,'Courier New',monospace"

_C_OUTER  = "#f0f2f5"
_C_CARD   = "#ffffff"
_C_BORDER = "#e2e8f0"
_C_RULE   = "#f1f5f9"
_C_HEAD   = "#0f172a"
_C_BODY   = "#334155"
_C_MUTED  = "#64748b"
_C_FAINT  = "#94a3b8"
_C_LINK   = "#4f6fff"


def _logo_html() -> str:
    return (
        f"<span style=\"font-family:{_FONT};font-size:18px;font-weight:800;"
        f"letter-spacing:-0.04em;color:{_C_HEAD};\">SKAI</span>"
        f"<span style=\"font-family:{_FONT};font-size:18px;font-weight:800;"
        f"letter-spacing:-0.04em;color:{_C_LINK};\">ROVA</span>"
    )


def _cta_button(label: str, url: str, bg: str = "#4f6fff") -> str:
    safe_url = escape(url)
    return (
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\""
        f" style=\"margin-top:8px;\">"
        f"<tr><td style=\"border-radius:7px;background:{bg};\">"
        f"<!--[if mso]><v:roundrect xmlns:v=\"urn:schemas-microsoft-com:vml\" "
        f"xmlns:w=\"urn:schemas-microsoft-com:office:word\" href=\"{safe_url}\" "
        f"style=\"height:46px;v-text-anchor:middle;width:180px;\" arcsize=\"15%\" "
        f"strokecolor=\"{bg}\" fillcolor=\"{bg}\"><w:anchorlock/>"
        f"<center style=\"color:#ffffff;font-family:{_FONT};font-size:14px;"
        f"font-weight:600;\">{escape(label)}</center></v:roundrect><![endif]-->"
        f"<!--[if !mso]><!-->"
        f"<a href=\"{safe_url}\" target=\"_blank\" rel=\"noopener noreferrer\""
        f" style=\"display:inline-block;padding:13px 26px;font-family:{_FONT};"
        f"font-size:14px;font-weight:600;color:#ffffff;text-decoration:none;"
        f"border-radius:7px;letter-spacing:0.01em;line-height:1;\">"
        f"{escape(label)}</a>"
        f"<!--<![endif]-->"
        f"</td></tr></table>"
    )


def _divider(margin_v: str = "24px") -> str:
    return (
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\""
        f" width=\"100%\" style=\"margin:{margin_v} 0;\">"
        f"<tr><td style=\"height:1px;background:{_C_BORDER};font-size:0;"
        f"line-height:0;\">&nbsp;</td></tr></table>"
    )


def _section_label(text: str) -> str:
    return (
        f"<div style=\"font-family:{_FONT};font-size:11px;font-weight:600;"
        f"text-transform:uppercase;letter-spacing:0.08em;color:{_C_MUTED};"
        f"margin-bottom:14px;\">{escape(text)}</div>"
    )


def _detail_row(label: str, value: str, mono: bool = False, last: bool = False) -> str:
    border = "" if last else f"border-bottom:1px solid {_C_RULE};"
    val_family = _MONO if mono else _FONT
    val_size   = "16px" if mono else "14px"
    val_weight = "700"  if mono else "600"
    val_spacing = "letter-spacing:0.07em;" if mono else ""
    return (
        f"<tr><td style=\"{border}padding:12px 0;\">"
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\""
        f" border=\"0\" width=\"100%\"><tr>"
        f"<td style=\"font-family:{_FONT};font-size:13px;font-weight:500;"
        f"color:{_C_MUTED};width:44%;padding-right:16px;vertical-align:top;\">"
        f"{escape(label)}</td>"
        f"<td style=\"font-family:{val_family};font-size:{val_size};"
        f"font-weight:{val_weight};color:{_C_HEAD};text-align:right;"
        f"vertical-align:top;{val_spacing}\">"
        f"{escape(value)}</td>"
        f"</tr></table></td></tr>"
    )


def _highlight_box(*, label: str, value: str, note: str = "",
                   bg: str, border: str, label_color: str) -> str:
    note_html = (
        f"<div style=\"font-family:{_FONT};font-size:13px;color:{_C_MUTED};"
        f"line-height:1.55;margin-top:6px;\">{escape(note)}</div>"
        if note else ""
    )
    return (
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\""
        f" border=\"0\" width=\"100%\" style=\"margin-bottom:28px;\">"
        f"<tr><td style=\"background:{bg};border:1px solid {border};"
        f"border-radius:8px;padding:20px 22px;\">"
        f"<div style=\"font-family:{_FONT};font-size:11px;font-weight:600;"
        f"text-transform:uppercase;letter-spacing:0.08em;color:{label_color};"
        f"margin-bottom:8px;\">{escape(label)}</div>"
        f"<div style=\"font-family:{_MONO};font-size:26px;font-weight:700;"
        f"color:{_C_HEAD};letter-spacing:0.08em;line-height:1;\">"
        f"{escape(value)}</div>"
        f"{note_html}"
        f"</td></tr></table>"
    )


def _wrap_email(
    *,
    preheader: str,
    accent_color: str,
    accent_tint: str = "",
    title: str,
    subtitle: str,
    body_html: str,
    footer_extra: str = "",
    support_email: str = "",
) -> str:
    header_bg = accent_tint or _C_OUTER
    brand = str(os.getenv("NGF_EMAIL_BRAND_NAME", "Skairova")).strip() or "Skairova"
    sup = support_email or (
        _normalize_email(os.getenv("NGF_EMAIL_REPLY_TO", ""))
        or _normalize_email(os.getenv("NGF_EMAIL_FROM", ""))
    )
    contact_html = (
        f"<a href=\"mailto:{escape(sup)}\" style=\"color:{_C_LINK};"
        f"text-decoration:none;\">{escape(sup)}</a>"
        if sup else ""
    )

    return f"""<!doctype html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <meta http-equiv="X-UA-Compatible" content="IE=edge"/>
  <!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->
  <title>{escape(brand)}</title>
  <style>
    @media only screen and (max-width:600px){{
      .em-card{{border-radius:0!important;border-left:none!important;border-right:none!important;}}
      .em-pad{{padding-left:24px!important;padding-right:24px!important;}}
    }}
  </style>
</head>
<body style="margin:0;padding:0;background:{_C_OUTER};
             -webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">

<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;
            font-size:1px;color:{_C_OUTER};opacity:0;">
  {escape(preheader)}&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;
</div>

<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="background:{_C_OUTER};min-width:320px;">
  <tr>
    <td align="center" style="padding:44px 16px 60px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0"
             width="100%" style="max-width:560px;">

        <!-- Card -->
        <tr>
          <td class="em-card"
              style="background:{_C_CARD};border:1px solid {_C_BORDER};
                     border-radius:10px;overflow:hidden;
                     box-shadow:0 1px 6px rgba(15,23,42,0.07);">
            <table role="presentation" cellpadding="0" cellspacing="0"
                   border="0" width="100%">

              <!-- Accent line -->
              <tr>
                <td style="height:4px;background:{accent_color};
                           font-size:0;line-height:0;">&nbsp;</td>
              </tr>

              <!-- Header: logo + heading together in a tinted block -->
              <tr>
                <td class="em-pad" style="background:{header_bg};
                           border-bottom:1px solid {_C_BORDER};
                           padding:28px 36px 32px;">
                  <div style="margin-bottom:28px;">{_logo_html()}</div>
                  <h1 style="margin:0 0 10px;font-family:{_FONT};
                              font-size:28px;font-weight:700;color:{_C_HEAD};
                              line-height:1.25;letter-spacing:-0.025em;">
                    {escape(title)}
                  </h1>
                  <p style="margin:0;font-family:{_FONT};font-size:15px;
                             color:{_C_BODY};line-height:1.65;">
                    {subtitle}
                  </p>
                </td>
              </tr>

              <!-- Body -->
              <tr>
                <td class="em-pad" style="padding:32px 36px 36px;">
                  {body_html}
                </td>
              </tr>

              <!-- Footer -->
              <tr>
                <td class="em-pad"
                    style="background:{_C_RULE};border-top:1px solid {_C_BORDER};
                           padding:20px 36px 24px;">
                  {"<p style='margin:0 0 8px;font-family:" + _FONT + ";font-size:13px;color:" + _C_MUTED + ";line-height:1.6;'>Questions? Contact us at " + contact_html + ".</p>" if contact_html else ""}
                  {"<p style='margin:0 0 8px;font-family:" + _FONT + ";font-size:13px;color:" + _C_MUTED + ";line-height:1.6;'>" + footer_extra + "</p>" if footer_extra else ""}
                  <p style="margin:0;font-family:{_FONT};font-size:12px;
                             color:{_C_FAINT};line-height:1.6;">
                    &copy; {escape(brand)}
                    &nbsp;&middot;&nbsp;
                    This is an automated message &mdash; please do not reply directly.
                  </p>
                </td>
              </tr>

            </table>
          </td>
        </tr>
        <!-- /Card -->

      </table>
    </td>
  </tr>
</table>

</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# 1 · Welcome email
# ─────────────────────────────────────────────────────────────────────────────

def send_welcome_email(
    *,
    to_email: str,
    first_name: str = "",
) -> tuple[bool, str]:
    safe_name = str(first_name or "").strip()
    greeting  = safe_name or "there"
    brand     = str(os.getenv("NGF_EMAIL_BRAND_NAME", "Skairova")).strip() or "Skairova"
    base_url  = _public_base_url()
    portal_url = f"{base_url}/portal" if base_url else ""
    search_url = f"{base_url}/" if base_url else ""

    subject = f"Welcome to {brand} — your account is ready"

    text_body = (
        f"Hi {greeting},\n\n"
        f"Welcome to {brand}. Your account is ready — let's find your next flight.\n\n"
        "What you can do:\n"
        "  - Search in plain English: \"cheapest week to Lisbon in June\" works\n"
        "  - See the real price upfront: live fares, no hidden fees\n"
        "  - Every trip saved automatically to your account\n"
    )
    if search_url:
        text_body += f"\nSearch flights: {search_url}\n"
    elif portal_url:
        text_body += f"\nOpen your account: {portal_url}\n"
    text_body += f"\nHappy travels — the {brand} team.\n"

    # The "aha moment" for a flight-search product is searching, not visiting
    # the account page — so that's the primary CTA target whenever we have it.
    cta_url = search_url or portal_url
    cta = _cta_button("Search flights", cta_url or "#", "#4f6fff") if cta_url else ""

    _welcome_features = [
        ("01", "Search in plain English",
         "Tell us where and when — \"cheapest week to Lisbon in June\" works just as well as picking dates from a calendar."),
        ("02", "See the real price, upfront",
         "Live fares straight from the airlines. The price you're quoted is the price you pay — no hidden fees."),
        ("03", "Every trip, saved automatically",
         "Itineraries and receipts land in your account the moment you book, ready whenever you need them."),
    ]
    features_html = "".join(
        f"<tr><td style=\"padding-bottom:{'0' if i == len(_welcome_features) - 1 else '20px'};\">"
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" width=\"100%\">"
        f"<tr>"
        f"<td style=\"width:3px;background:#4f46e5;border-radius:2px;\"></td>"
        f"<td style=\"width:16px;\"></td>"
        f"<td style=\"vertical-align:top;\">"
        f"<div style=\"font-family:{_MONO};font-size:11px;font-weight:700;color:#4f46e5;"
        f"letter-spacing:0.06em;margin-bottom:4px;\">{num}</div>"
        f"<div style=\"font-family:{_FONT};font-size:14px;font-weight:600;color:{_C_HEAD};"
        f"margin-bottom:4px;line-height:1.4;\">{escape(title)}</div>"
        f"<div style=\"font-family:{_FONT};font-size:13px;color:{_C_MUTED};line-height:1.6;\">"
        f"{escape(desc)}</div>"
        f"</td></tr></table>"
        f"</td></tr>"
        for i, (num, title, desc) in enumerate(_welcome_features)
    )

    body_html = (
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\""
        f" border=\"0\" width=\"100%\" style=\"margin-bottom:32px;\">{features_html}</table>"
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" width=\"100%\""
        f" style=\"margin-bottom:24px;\"><tr><td style=\"height:1px;background:{_C_BORDER};"
        f"font-size:0;line-height:0;\">&nbsp;</td></tr></table>"
        f"{cta}"
        f"<p style=\"margin:20px 0 0;font-family:{_FONT};font-size:13px;"
        f"color:{_C_MUTED};\">Happy travels &mdash; the {escape(brand)} team.</p>"
    )

    html_body = _render_react_email("welcome", {
        "firstName": safe_name,
        "portalUrl": portal_url,
        "searchUrl": search_url,
        "supportEmail": _normalize_email(os.getenv("NGF_EMAIL_REPLY_TO", "")) or _normalize_email(os.getenv("NGF_EMAIL_FROM", "")),
        "brand": brand,
    }) or _wrap_email(
        preheader=f"Welcome to {brand} — your account is ready. Let's find your next flight.",
        accent_color="#4f46e5",
        accent_tint="#f5f4ff",
        title=f"Welcome, {greeting}.",
        subtitle="Your account is ready. Let's find your next flight.",
        body_html=body_html,
    )

    return send_email(to_emails=to_email, subject=subject, text_body=text_body, html_body=html_body)


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Booking confirmation (itinerary)
# ─────────────────────────────────────────────────────────────────────────────

def send_itinerary_email(
    *,
    to_email: str,
    order_summary: Mapping[str, Any],
    manage_url: str = "",
    pdf_bytes: bytes | None = None,
) -> tuple[bool, str]:
    booking_reference = str(order_summary.get("booking_reference") or "").strip()
    order_id          = str(order_summary.get("order_id") or "").strip()
    airline_name      = str(order_summary.get("airline_name") or "Your airline").strip() or "Your airline"
    currency          = str(order_summary.get("currency") or "USD").strip() or "USD"
    total_amount      = str(order_summary.get("total_amount") or "").strip() or "0.00"
    slices            = order_summary.get("slices") or []
    passenger_names: list[str] = [
        str(n).strip() for n in (order_summary.get("passenger_names") or []) if str(n).strip()
    ]

    ref_display = booking_reference or order_id or "—"
    subject     = f"Booking confirmed — {booking_reference or order_id or 'your booking'} · {str(os.getenv('NGF_EMAIL_BRAND_NAME','Skairova')).strip() or 'Skairova'}"

    route_lines = []
    for sl in slices:
        if not isinstance(sl, Mapping):
            continue
        label = str(sl.get("label") or "Flight").strip()
        route = str(sl.get("route") or "").strip() or "Route unavailable"
        dep   = str(sl.get("depart_label") or "").strip()
        route_lines.append(f"  {label}: {route}" + (f" ({dep})" if dep else ""))
    if not route_lines:
        route_lines = ["  Details available in your booking portal"]

    pax_lines = [f"  - {n}" for n in passenger_names] or ["  - Details unavailable"]
    text_body = (
        f"Your booking is confirmed.\n\n"
        f"Reference: {ref_display}\n"
        f"Airline:   {airline_name}\n"
        f"Total:     {currency} {total_amount}\n\n"
        f"Passengers:\n" + "\n".join(pax_lines) + "\n\n"
        f"Itinerary:\n" + "\n".join(route_lines)
    )
    if manage_url:
        text_body += f"\n\nManage your booking: {manage_url}\n"

    # Booking reference block
    ref_block = _highlight_box(
        label="Booking reference",
        value=ref_display,
        bg="#f0fdf4",
        border="#bbf7d0",
        label_color="#16a34a",
    )

    # Details table
    detail_rows  = _detail_row("Airline", airline_name)
    detail_rows += _detail_row("Total paid", f"{currency} {total_amount}")
    if passenger_names:
        detail_rows += _detail_row(
            "Passengers",
            ", ".join(passenger_names),
            last=True,
        )

    details_block = (
        f"{_section_label('Booking details')}"
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\""
        f" border=\"0\" width=\"100%\" style=\"margin-bottom:28px;"
        f"border-top:1px solid {_C_BORDER};\">{detail_rows}</table>"
    )

    # Itinerary rows
    slice_rows = ""
    for sl in slices:
        if not isinstance(sl, Mapping):
            continue
        label = str(sl.get("label") or "Flight").strip()
        route = str(sl.get("route") or "Route unavailable").strip()
        dep   = str(sl.get("depart_label") or "").strip()
        arr   = str(sl.get("arrive_label") or "").strip()
        times = ""
        if dep or arr:
            parts = []
            if dep: parts.append(f"Departs {escape(dep)}")
            if arr: parts.append(f"arrives {escape(arr)}")
            times = (
                f"<div style=\"font-family:{_FONT};font-size:13px;"
                f"color:{_C_MUTED};margin-top:3px;\">"
                + " &middot; ".join(parts) + "</div>"
            )
        slice_rows += (
            f"<tr><td style=\"padding:14px 0;border-bottom:1px solid {_C_RULE};\">"
            f"<div style=\"font-family:{_FONT};font-size:11px;font-weight:600;"
            f"text-transform:uppercase;letter-spacing:0.08em;color:{_C_MUTED};"
            f"margin-bottom:5px;\">{escape(label)}</div>"
            f"<div style=\"font-family:{_FONT};font-size:15px;font-weight:600;"
            f"color:{_C_HEAD};\">{escape(route)}</div>"
            f"{times}</td></tr>"
        )
    if not slice_rows:
        slice_rows = (
            f"<tr><td style=\"padding:14px 0;font-family:{_FONT};"
            f"font-size:14px;color:{_C_MUTED};\">"
            f"Itinerary details are available in your booking portal.</td></tr>"
        )

    itinerary_block = (
        f"{_section_label('Itinerary')}"
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\""
        f" border=\"0\" width=\"100%\" style=\"margin-bottom:32px;"
        f"border-top:1px solid {_C_BORDER};\">{slice_rows}</table>"
    )

    manage_cta = _cta_button("Manage booking", manage_url, "#059669") if manage_url else ""

    body_html = ref_block + details_block + itinerary_block + manage_cta

    react_slices = []
    for sl in slices:
        if not isinstance(sl, Mapping):
            continue
        raw_segs = sl.get("segments") or []
        react_segments = []
        for seg in raw_segs:
            if not isinstance(seg, Mapping):
                continue
            react_segments.append({
                "carrierName":       str(seg.get("carrier_name") or "").strip() or None,
                "flightNumber":      str(seg.get("flight_number") or "").strip() or None,
                "aircraft":          str(seg.get("aircraft") or "").strip() or None,
                "originCode":        str(seg.get("origin_code") or "").strip() or None,
                "destinationCode":   str(seg.get("destination_code") or "").strip() or None,
                "originCity":        str(seg.get("origin_city") or "").strip() or None,
                "destinationCity":   str(seg.get("destination_city") or "").strip() or None,
                "departTime":        str(seg.get("depart_time") or "").strip() or None,
                "arriveTime":        str(seg.get("arrive_time") or "").strip() or None,
                "departDateFull":    str(seg.get("depart_date_full") or "").strip() or None,
                "arriveDateFull":    str(seg.get("arrive_date_full") or "").strip() or None,
                "departureTerminal": str(seg.get("departure_terminal") or "").strip() or None,
                "arrivalTerminal":   str(seg.get("arrival_terminal") or "").strip() or None,
                "durationLabel":     str(seg.get("duration_label") or "").strip() or None,
            })
        react_slices.append({
            "label":         str(sl.get("label") or "Flight").strip(),
            "route":         str(sl.get("route") or "").strip() or "Route unavailable",
            "departLabel":   str(sl.get("depart_label") or "").strip() or None,
            "arriveLabel":   str(sl.get("arrive_label") or "").strip() or None,
            "durationLabel": str(sl.get("duration_label") or "").strip() or None,
            "stopsLabel":    str(sl.get("stops_label") or "").strip() or None,
            "segments":      react_segments,
        })

    html_body = _render_react_email("booking_confirmation", {
        "bookingReference": ref_display,
        "airlineName": airline_name,
        "currency": currency,
        "totalAmount": total_amount,
        "passengerNames": passenger_names,
        "slices": react_slices,
        "manageUrl": manage_url or "",
        "supportEmail": _normalize_email(os.getenv("NGF_EMAIL_REPLY_TO", "")) or _normalize_email(os.getenv("NGF_EMAIL_FROM", "")),
    }) or _wrap_email(
        preheader=f"Your booking {ref_display} is confirmed. Have a great flight.",
        accent_color="#059669",
        accent_tint="#f0fdf8",
        title="Your booking is confirmed.",
        subtitle=f"Your flight with {escape(airline_name)} is booked. Keep this email for your records.",
        body_html=body_html,
    )

    attachments: list[dict[str, Any]] = []
    if pdf_bytes:
        ref_slug = (booking_reference or order_id or "itinerary").upper()
        attachments.append({
            "data": pdf_bytes,
            "maintype": "application",
            "subtype": "pdf",
            "filename": f"itinerary-{ref_slug}.pdf",
        })

    return send_email(
        to_emails=to_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        attachments=attachments or None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2b · Hotel booking confirmation
# ─────────────────────────────────────────────────────────────────────────────

def send_hotel_confirmation_email(
    *,
    to_email: str,
    booking_summary: Mapping[str, Any],
    manage_url: str = "",
) -> tuple[bool, str]:
    booking_reference = str(booking_summary.get("booking_reference") or "").strip()
    hotel_name = str(booking_summary.get("hotel_name") or "Your hotel").strip() or "Your hotel"
    hotel_address = str(booking_summary.get("hotel_address") or "").strip()
    room_name = str(booking_summary.get("room_name") or "Room").strip()
    board_name = str(booking_summary.get("board_name") or "").strip()
    checkin = str(booking_summary.get("checkin") or "").strip()
    checkout = str(booking_summary.get("checkout") or "").strip()
    nights = booking_summary.get("nights")
    guest_name = str(booking_summary.get("guest_name") or "").strip()
    currency = str(booking_summary.get("currency") or "USD").strip() or "USD"
    total_amount = booking_summary.get("total_amount")
    total_display = f"{float(total_amount):.2f}" if total_amount not in (None, "") else "0.00"

    ref_display = booking_reference or "—"
    brand = str(os.getenv("NGF_EMAIL_BRAND_NAME", "Skairova")).strip() or "Skairova"
    subject = f"Hotel booking confirmed — {ref_display} · {brand}"

    text_body = (
        f"Your hotel booking is confirmed.\n\n"
        f"Reference: {ref_display}\n"
        f"Hotel:     {hotel_name}\n"
        f"Check-in:  {checkin}\n"
        f"Check-out: {checkout}\n"
        f"Room:      {room_name}" + (f" ({board_name})" if board_name else "") + "\n"
        f"Total:     {currency} {total_display}\n"
    )
    if guest_name:
        text_body += f"Guest:     {guest_name}\n"
    if manage_url:
        text_body += f"\nManage your booking: {manage_url}\n"

    ref_block = _highlight_box(
        label="Booking reference",
        value=ref_display,
        bg="#f0fdf4",
        border="#bbf7d0",
        label_color="#16a34a",
    )

    detail_rows = _detail_row("Hotel", hotel_name)
    if hotel_address:
        detail_rows += _detail_row("Address", hotel_address)
    detail_rows += _detail_row("Check-in", checkin)
    detail_rows += _detail_row("Check-out", checkout)
    detail_rows += _detail_row("Room", room_name + (f" · {board_name}" if board_name else ""))
    if guest_name:
        detail_rows += _detail_row("Guest", guest_name)
    detail_rows += _detail_row("Total paid", f"{currency} {total_display}", last=True)

    details_block = (
        f"{_section_label('Stay details')}"
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\""
        f" border=\"0\" width=\"100%\" style=\"margin-bottom:28px;"
        f"border-top:1px solid {_C_RULE};\">{detail_rows}</table>"
    )

    manage_cta = _cta_button("Manage booking", manage_url, "#059669") if manage_url else ""
    body_html = ref_block + details_block + manage_cta

    html_body = _render_react_email("hotel_confirmation", {
        "bookingReference": ref_display,
        "hotelName": hotel_name,
        "hotelAddress": hotel_address,
        "roomName": room_name,
        "boardName": board_name,
        "checkin": checkin,
        "checkout": checkout,
        "nights": nights,
        "guestName": guest_name,
        "currency": currency,
        "totalAmount": total_display,
        "manageUrl": manage_url or "",
        "supportEmail": _normalize_email(os.getenv("NGF_EMAIL_REPLY_TO", "")) or _normalize_email(os.getenv("NGF_EMAIL_FROM", "")),
    }) or _wrap_email(
        preheader=f"Your stay at {hotel_name} ({ref_display}) is confirmed.",
        accent_color="#059669",
        accent_tint="#f0fdf8",
        title="Your hotel is booked.",
        subtitle=f"Your stay at {escape(hotel_name)} is confirmed. Keep this email for your records.",
        body_html=body_html,
    )

    return send_email(to_emails=to_email, subject=subject, text_body=text_body, html_body=html_body)


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Booking cancellation
# ─────────────────────────────────────────────────────────────────────────────

def send_cancellation_email(
    *,
    to_email: str,
    order_summary: Mapping[str, Any],
    refund_amount: str = "",
    refund_currency: str = "USD",
    search_url: str = "",
) -> tuple[bool, str]:
    booking_reference = str(order_summary.get("booking_reference") or "").strip()
    order_id          = str(order_summary.get("order_id") or "").strip()
    airline_name      = str(order_summary.get("airline_name") or "").strip()
    currency          = str(order_summary.get("currency") or refund_currency or "USD").strip()
    brand             = str(os.getenv("NGF_EMAIL_BRAND_NAME", "Skairova")).strip() or "Skairova"

    ref_display = booking_reference or order_id or "—"
    subject     = f"Booking cancelled — {booking_reference or order_id or 'your booking'} · {brand}"

    has_refund = False
    try:
        has_refund = float(refund_amount) > 0 if refund_amount else False
    except (ValueError, TypeError):
        has_refund = False

    refund_ccy = refund_currency or currency

    text_body = f"Your booking has been cancelled.\n\nReference: {ref_display}\n"
    if airline_name:
        text_body += f"Airline: {airline_name}\n"
    if has_refund:
        text_body += (
            f"Refund: {refund_ccy} {refund_amount} will be returned to your original "
            f"payment method within 5-10 business days.\n"
        )
    else:
        text_body += "This booking was non-refundable. No refund will be issued.\n"
    if search_url:
        text_body += f"\nSearch new flights: {search_url}\n"

    # Reference block
    ref_block = _highlight_box(
        label="Cancelled booking reference",
        value=ref_display,
        bg="#fff1f2",
        border="#fecdd3",
        label_color="#e11d48",
    )

    # Details table
    detail_rows = ""
    if airline_name:
        detail_rows += _detail_row("Airline", airline_name)
    detail_rows += _detail_row("Status", "Cancelled", last=not has_refund)

    details_block = (
        f"{_section_label('Cancellation details')}"
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\""
        f" border=\"0\" width=\"100%\" style=\"margin-bottom:28px;"
        f"border-top:1px solid {_C_BORDER};\">{detail_rows}</table>"
    )

    # Refund block
    if has_refund:
        refund_block = (
            f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\""
            f" border=\"0\" width=\"100%\" style=\"margin-bottom:32px;\">"
            f"<tr><td style=\"background:#f0fdf4;border:1px solid #bbf7d0;"
            f"border-radius:8px;padding:20px 22px;\">"
            f"<div style=\"font-family:{_FONT};font-size:11px;font-weight:600;"
            f"text-transform:uppercase;letter-spacing:0.08em;color:#16a34a;"
            f"margin-bottom:8px;\">Refund</div>"
            f"<div style=\"font-family:{_MONO};font-size:26px;font-weight:700;"
            f"color:{_C_HEAD};line-height:1;margin-bottom:8px;\">"
            f"{escape(refund_ccy)} {escape(refund_amount)}</div>"
            f"<div style=\"font-family:{_FONT};font-size:13px;color:{_C_MUTED};"
            f"line-height:1.55;\">Will be returned to your original payment method "
            f"within 5&ndash;10 business days.</div>"
            f"</td></tr></table>"
        )
    else:
        refund_block = (
            f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\""
            f" border=\"0\" width=\"100%\" style=\"margin-bottom:32px;\">"
            f"<tr><td style=\"background:#fff1f2;border:1px solid #fecdd3;"
            f"border-radius:8px;padding:16px 20px;\">"
            f"<p style=\"margin:0;font-family:{_FONT};font-size:13px;"
            f"font-weight:500;color:#9f1239;line-height:1.55;\">"
            f"This booking was non-refundable. No refund will be issued.</p>"
            f"</td></tr></table>"
        )

    base_url        = _public_base_url()
    resolved_search = search_url or (f"{base_url}/" if base_url else "")
    cta             = _cta_button("Search new flights", resolved_search, _C_LINK) if resolved_search else ""

    hero_sub = (
        f"A refund of {escape(refund_ccy)} {escape(refund_amount)} will be returned "
        f"to your original payment method."
        if has_refund else
        "Your cancellation has been processed."
    )

    body_html = ref_block + details_block + refund_block + cta

    html_body = _render_react_email("booking_cancellation", {
        "bookingReference": ref_display,
        "airlineName": airline_name or None,
        "hasRefund": has_refund,
        "refundAmount": refund_amount if has_refund else "",
        "refundCurrency": refund_ccy,
        "searchUrl": resolved_search or "",
        "supportEmail": _normalize_email(os.getenv("NGF_EMAIL_REPLY_TO", "")) or _normalize_email(os.getenv("NGF_EMAIL_FROM", "")),
        "brand": brand,
    }) or _wrap_email(
        preheader=(
            f"Booking {ref_display} cancelled."
            + (f" Refund of {refund_ccy} {refund_amount} is on its way." if has_refund else "")
        ),
        accent_color="#dc2626",
        accent_tint="#fff8f8",
        title="Your booking has been cancelled.",
        subtitle=hero_sub,
        body_html=body_html,
    )

    return send_email(to_emails=to_email, subject=subject, text_body=text_body, html_body=html_body)


# ─────────────────────────────────────────────────────────────────────────────
# 4 · Password reset code
# ─────────────────────────────────────────────────────────────────────────────

def send_password_reset_code_email(
    *,
    to_email: str,
    verification_code: str,
    ttl_minutes: int = 10,
    first_name: str = "",
) -> tuple[bool, str]:
    safe_name = str(first_name or "").strip()
    greeting  = safe_name or "there"
    code      = str(verification_code or "").strip()
    expiry    = max(1, int(ttl_minutes))

    subject = "Your Skairova verification code"

    text_body = (
        f"Hi {greeting},\n\n"
        f"Your Skairova verification code is: {code}\n\n"
        f"This code expires in {expiry} minutes.\n\n"
        "If you did not request this, you can safely ignore this email. "
        "Your account password will not change.\n"
    )

    spaced_code = "  ".join(list(code)) if len(code) <= 8 else code

    body_html = (
        f"<p style=\"margin:0 0 28px;font-family:{_FONT};font-size:15px;"
        f"color:{_C_BODY};line-height:1.65;\">"
        f"Hi <strong style=\"color:{_C_HEAD};\">{escape(greeting)}</strong>, "
        f"enter the code below to complete your password reset.</p>"

        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\""
        f" border=\"0\" width=\"100%\" style=\"margin-bottom:28px;\">"
        f"<tr><td align=\"center\""
        f" style=\"background:{_C_RULE};border:1px solid {_C_BORDER};"
        f"border-radius:8px;padding:34px 20px;\">"
        f"<div style=\"font-family:{_MONO};font-size:40px;font-weight:700;"
        f"color:{_C_HEAD};letter-spacing:0.22em;line-height:1;\">"
        f"{escape(spaced_code)}</div>"
        f"<div style=\"font-family:{_FONT};font-size:13px;color:{_C_MUTED};"
        f"margin-top:14px;\">Expires in "
        f"<strong style=\"color:{_C_BODY};\">{expiry} minutes</strong></div>"
        f"</td></tr></table>"

        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\""
        f" border=\"0\" width=\"100%\">"
        f"<tr><td style=\"border-left:3px solid {_C_BORDER};"
        f"padding:10px 16px;\">"
        f"<p style=\"margin:0;font-family:{_FONT};font-size:13px;"
        f"color:{_C_MUTED};line-height:1.6;\">"
        f"If you did not request a password reset, you can ignore this email. "
        f"Your account and password will remain unchanged.</p>"
        f"</td></tr></table>"
    )

    html_body = _render_react_email("password_reset", {
        "firstName": safe_name or None,
        "code": code,
        "expiryMinutes": expiry,
        "supportEmail": _normalize_email(os.getenv("NGF_EMAIL_REPLY_TO", "")) or _normalize_email(os.getenv("NGF_EMAIL_FROM", "")),
    }) or _wrap_email(
        preheader=f"Your verification code is {code}. Expires in {expiry} minutes.",
        accent_color="#2563eb",
        accent_tint="#f0f6ff",
        title="Verify your identity.",
        subtitle="Use the code below to complete your password reset on Skairova.",
        body_html=body_html,
    )

    return send_email(to_emails=to_email, subject=subject, text_body=text_body, html_body=html_body)
