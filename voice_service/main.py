"""
Skairova Voice AI — Deepgram streaming proxy.

Why this exists as a separate service (not a Flask route):
Flask (served here via gunicorn --worker-class gthread) does not give us a
cheap, long-lived, fully async duplex socket per connection. Voice needs one
open WebSocket per active listener, forwarding audio up and transcripts down
in near-real-time with no head-of-line blocking. FastAPI + uvicorn (asyncio)
is the right tool for that; Flask remains the app of record for everything
else (auth, search, booking) and simply *mints tokens* this service trusts.

Flow:
  Browser --(wss, short-lived JWT)--> this proxy --(wss, DEEPGRAM_API_KEY)--> Deepgram Nova-3

The Deepgram API key never reaches the browser. The browser only ever holds
a signed, single-purpose, short-TTL token minted by Flask's
`/voice/session-token` route (see app.py), signed with the same
VOICE_PROXY_SECRET this service verifies against.

Run:
    pip install -r voice_service/requirements.txt
    uvicorn voice_service.main:app --host 0.0.0.0 --port 8781

Environment (shared with the main Flask app's .env):
    DEEPGRAM_API_KEY         Deepgram account key (server-side only)
    VOICE_PROXY_SECRET       HS256 secret shared with Flask for JWT verification
    VOICE_PROXY_PORT         Port to bind (default 8781)
    VOICE_PROXY_ALLOWED_ORIGINS   Comma-separated allowlist for browser Origin header
    DEEPGRAM_LANGUAGE_MODE    "multi" (default) for Nova-3 code-switching, or a
                               single BCP-47 code to pin one language.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
from contextlib import suppress
from typing import Any

import jwt
import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosed

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a soft dependency here
    pass

try:
    import certifi
except ImportError:  # pragma: no cover - certifi is a soft dependency here
    certifi = None

logging.basicConfig(level=os.getenv("VOICE_LOG_LEVEL", "INFO"))
logger = logging.getLogger("skairova.voice_proxy")

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "").strip()
VOICE_PROXY_SECRET = os.getenv("VOICE_PROXY_SECRET", "").strip()
DEEPGRAM_LANGUAGE_MODE = os.getenv("DEEPGRAM_LANGUAGE_MODE", "multi").strip() or "multi"
DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"

# Silence duration (ms) Deepgram must observe before it emits a finalized
# UtteranceEnd — this is the server-side half of "stop ~1s after silence".
ENDPOINTING_MS = int(os.getenv("VOICE_ENDPOINTING_MS", "1000"))
UTTERANCE_END_MS = int(os.getenv("VOICE_UTTERANCE_END_MS", "1200"))

# Audio contract with the browser client (see static/voice/audio-capture.js).
AUDIO_ENCODING = "linear16"
AUDIO_SAMPLE_RATE = 16000
AUDIO_CHANNELS = 1

MAX_UPSTREAM_RETRIES = 3
RETRY_BASE_DELAY_S = 0.5

_DEEPGRAM_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where()) if certifi else ssl.create_default_context()

_allowed_origins_raw = os.getenv("VOICE_PROXY_ALLOWED_ORIGINS", "").strip()
ALLOWED_ORIGINS = [o.strip() for o in _allowed_origins_raw.split(",") if o.strip()] or ["*"]

app = FastAPI(title="Skairova Voice Proxy", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "deepgram_configured": bool(DEEPGRAM_API_KEY),
        "language_mode": DEEPGRAM_LANGUAGE_MODE,
    }


def _verify_session_token(token: str) -> dict[str, Any] | None:
    """Verify the short-lived JWT minted by Flask's /voice/session-token.

    Returns the decoded claims on success, None on any failure. We never
    raise here — an invalid token is just a closed connection, not a 500.
    """
    if not token or not VOICE_PROXY_SECRET:
        return None
    try:
        claims = jwt.decode(
            token,
            VOICE_PROXY_SECRET,
            algorithms=["HS256"],
            audience="voice-proxy",
            options={"require": ["exp", "iat", "aud", "sid"]},
        )
        return claims
    except jwt.PyJWTError as exc:
        logger.info("Rejected voice session token: %s", exc)
        return None


def _deepgram_query() -> str:
    params = {
        "model": "nova-3",
        "language": DEEPGRAM_LANGUAGE_MODE,
        "punctuate": "true",
        "smart_format": "true",
        "interim_results": "true",
        "endpointing": str(ENDPOINTING_MS),
        "utterance_end_ms": str(UTTERANCE_END_MS),
        "vad_events": "true",
        "encoding": AUDIO_ENCODING,
        "sample_rate": str(AUDIO_SAMPLE_RATE),
        "channels": str(AUDIO_CHANNELS),
        # Nova-3 filler-word / noise handling — keep transcripts clean for the parser.
        "filler_words": "false",
    }
    return "&".join(f"{k}={v}" for k, v in params.items())


async def _open_deepgram_socket():
    """Open (with bounded retry + backoff) the upstream Deepgram connection."""
    if not DEEPGRAM_API_KEY:
        raise RuntimeError("DEEPGRAM_API_KEY is not configured on the voice proxy")

    url = f"{DEEPGRAM_URL}?{_deepgram_query()}"
    last_err: Exception | None = None
    for attempt in range(MAX_UPSTREAM_RETRIES):
        try:
            # `extra_headers` is the pinned websockets==13.1 legacy client's
            # kwarg for this; the newer websockets.asyncio.client (default in
            # 14+) renamed it to `additional_headers` — update this if you
            # ever bump the pin.
            return await websockets.connect(
                url,
                extra_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
                ssl=_DEEPGRAM_SSL_CONTEXT,
                ping_interval=5,
                ping_timeout=10,
                max_size=2**20,
            )
        except Exception as exc:  # noqa: BLE001 - we want to retry any transient failure
            last_err = exc
            delay = RETRY_BASE_DELAY_S * (2**attempt)
            logger.warning("Deepgram connect attempt %s failed: %s (retry in %.1fs)", attempt + 1, exc, delay)
            await asyncio.sleep(delay)
    raise RuntimeError(f"Could not reach Deepgram after {MAX_UPSTREAM_RETRIES} attempts") from last_err


async def _pump_client_to_deepgram(client: WebSocket, upstream, stop: asyncio.Event) -> None:
    """Forward binary audio frames (and the close signal) from browser to Deepgram."""
    try:
        while not stop.is_set():
            message = await client.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if (data := message.get("bytes")) is not None:
                await upstream.send(data)
                continue
            if (text := message.get("text")) is not None:
                # Client sends small JSON control messages, e.g. {"type":"close_stream"}
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if parsed.get("type") == "close_stream":
                    # Deepgram's documented "finalize the stream" signal.
                    with suppress(ConnectionClosed):
                        await upstream.send(json.dumps({"type": "CloseStream"}))
                    break
    except WebSocketDisconnect:
        pass
    finally:
        stop.set()


async def _pump_deepgram_to_client(client: WebSocket, upstream, stop: asyncio.Event) -> None:
    """Forward Deepgram transcript/VAD events straight through to the browser."""
    try:
        async for raw in upstream:
            if stop.is_set():
                break
            if client.application_state != WebSocketState.CONNECTED:
                break
            await client.send_text(raw if isinstance(raw, str) else raw.decode("utf-8", "ignore"))
    except ConnectionClosed:
        pass
    finally:
        stop.set()


@app.websocket("/ws/voice")
async def voice_stream(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token", "")
    claims = _verify_session_token(token)
    if not claims:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="invalid_or_expired_token")
        return

    await websocket.accept()
    session_id = claims.get("sid", "unknown")
    started_at = time.monotonic()
    logger.info("Voice session %s starting", session_id)

    try:
        upstream = await _open_deepgram_socket()
    except Exception as exc:  # noqa: BLE001
        logger.error("Voice session %s: upstream failure: %s", session_id, exc)
        with suppress(Exception):
            await websocket.send_text(json.dumps({"type": "proxy_error", "message": "deepgram_unavailable"}))
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    await websocket.send_text(json.dumps({"type": "ready", "session_id": session_id}))

    stop = asyncio.Event()
    try:
        await asyncio.gather(
            _pump_client_to_deepgram(websocket, upstream, stop),
            _pump_deepgram_to_client(websocket, upstream, stop),
        )
    finally:
        with suppress(Exception):
            await upstream.close()
        with suppress(Exception):
            if websocket.application_state == WebSocketState.CONNECTED:
                await websocket.close()
        logger.info("Voice session %s ended after %.1fs", session_id, time.monotonic() - started_at)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "voice_service.main:app",
        host="0.0.0.0",
        port=int(os.getenv("VOICE_PROXY_PORT", "8781")),
        reload=bool(os.getenv("VOICE_PROXY_RELOAD")),
    )
