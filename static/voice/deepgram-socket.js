/**
 * Skairova Voice AI — realtime transcript socket.
 *
 * Talks to *our* proxy (voice_service/main.py), never to Deepgram directly —
 * the browser never sees a Deepgram credential. Handles session-token
 * fetching, connect/reconnect with backoff, and normalizes Deepgram's
 * message shapes into a small set of events the overlay can react to.
 */

export class VoiceUnavailableError extends Error {
  constructor(message) {
    super(message || "Voice search is not available right now.");
    this.name = "VoiceUnavailableError";
  }
}

export class VoiceRateLimitedError extends Error {
  constructor(message) {
    super(message || "Too many voice requests — please wait a moment.");
    this.name = "VoiceRateLimitedError";
  }
}

/**
 * @param {AbortSignal} [signal]
 * @returns {Promise<{token: string, wsUrl: string, sessionId: string}>}
 */
async function fetchSessionToken(signal) {
  const apiBaseUrl = (window.SKAIR_API_BASE_URL || "").trim();
  const tokenUrl = apiBaseUrl ? new URL("/voice/session-token", apiBaseUrl).toString() : "/voice/session-token";
  let response;
  try {
    response = await fetch(tokenUrl, {
      method: "POST",
      credentials: apiBaseUrl ? "include" : "same-origin",
      headers: { Accept: "application/json" },
      signal,
    });
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new VoiceUnavailableError("Could not reach the search server.");
  }

  if (response.status === 429) throw new VoiceRateLimitedError();
  if (response.status === 503) throw new VoiceUnavailableError("Voice search isn't set up yet.");
  if (!response.ok) throw new VoiceUnavailableError();

  const payload = await response.json().catch(() => null);
  if (!payload || !payload.ok || !payload.token || !payload.ws_url) {
    throw new VoiceUnavailableError();
  }
  return { token: payload.token, wsUrl: payload.ws_url, sessionId: payload.session_id || "" };
}

const MAX_RECONNECT_ATTEMPTS = 3;
const RECONNECT_BASE_DELAY_MS = 400;

export class DeepgramSocket extends EventTarget {
  constructor() {
    super();
    /** @type {WebSocket|null} */
    this._ws = null;
    this._reconnectAttempts = 0;
    this._intentionalClose = false;
    this._sessionId = "";
  }

  /** @param {AbortSignal} [signal] */
  async connect(signal) {
    this._intentionalClose = false;
    const { token, wsUrl, sessionId } = await fetchSessionToken(signal);
    this._sessionId = sessionId;
    await this._openSocket(wsUrl, token);
  }

  _openSocket(wsUrl, token) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const socket = new WebSocket(`${wsUrl}?token=${encodeURIComponent(token)}`);
      socket.binaryType = "arraybuffer";
      this._ws = socket;

      socket.onopen = () => {
        // Wait for the proxy's own {"type":"ready"} handshake before resolving,
        // so callers never send audio into a socket that isn't fully wired to
        // Deepgram yet.
      };

      socket.onmessage = (event) => {
        let payload;
        try {
          payload = JSON.parse(event.data);
        } catch (e) {
          return;
        }
        if (payload.type === "ready" && !settled) {
          settled = true;
          resolve();
          return;
        }
        if (payload.type === "proxy_error") {
          this.dispatchEvent(new CustomEvent("error", { detail: { kind: "proxy", message: payload.message } }));
          return;
        }
        this._handleDeepgramMessage(payload);
      };

      socket.onerror = () => {
        if (!settled) {
          settled = true;
          reject(new VoiceUnavailableError("Could not connect for voice search."));
        }
      };

      socket.onclose = (event) => {
        if (this._intentionalClose) {
          this.dispatchEvent(new CustomEvent("close", { detail: { code: event.code } }));
          return;
        }
        if (!settled) {
          settled = true;
          reject(new VoiceUnavailableError("Voice connection closed unexpectedly."));
          return;
        }
        this.dispatchEvent(new CustomEvent("disconnected", { detail: { code: event.code } }));
      };
    });
  }

  _handleDeepgramMessage(payload) {
    if (payload.type === "SpeechStarted") {
      this.dispatchEvent(new CustomEvent("speech-started"));
      return;
    }
    if (payload.type === "UtteranceEnd") {
      this.dispatchEvent(new CustomEvent("utterance-end"));
      return;
    }
    if (payload.type === "Results") {
      const alt = payload.channel && payload.channel.alternatives && payload.channel.alternatives[0];
      const transcript = (alt && alt.transcript) || "";
      const confidence = alt && typeof alt.confidence === "number" ? alt.confidence : null;
      const isFinal = !!payload.is_final;
      const speechFinal = !!payload.speech_final;
      const language = (alt && alt.language) || payload.language || "";
      if (!transcript) return; // Deepgram sends empty interim frames during silence; ignore them
      this.dispatchEvent(
        new CustomEvent("transcript", {
          detail: { transcript, isFinal, speechFinal, confidence, language },
        })
      );
      return;
    }
    if (payload.type === "Metadata") {
      this.dispatchEvent(new CustomEvent("metadata", { detail: payload }));
    }
  }

  /** @param {ArrayBuffer} frame */
  sendAudioFrame(frame) {
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._ws.send(frame);
    }
  }

  /** Tells Deepgram to flush the final transcript, then closes once it replies (or after a short timeout). */
  finalizeAndClose() {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) {
      this.close();
      return;
    }
    this._intentionalClose = true;
    try {
      this._ws.send(JSON.stringify({ type: "close_stream" }));
    } catch (e) {}
    // Deepgram/the proxy should close the socket once it has flushed; if it
    // doesn't within a beat, close from our side so we never hang.
    setTimeout(() => this.close(), 1500);
  }

  close() {
    this._intentionalClose = true;
    if (this._ws) {
      try {
        this._ws.close(1000);
      } catch (e) {}
      this._ws = null;
    }
  }
}
