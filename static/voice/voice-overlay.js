/**
 * Skairova Voice AI — overlay controller.
 *
 * The orchestrator. Owns the state machine transitions, builds/animates the
 * overlay DOM, and is the only module that touches the existing AI search
 * bar (#aiText / #aiParseToken / #aiForm) — everything else (audio, socket,
 * orb, waveform, parsing) is a clean, DOM-free primitive this file wires
 * together.
 *
 * Integration contract with the existing search bar: on a successful parse,
 * this module sets the transcript into the *real* #aiText textarea, fires a
 * native `input` event (so the page's own autoResize/char-count/warm-parse
 * listeners run exactly as if the user had typed), and then calls
 * `aiForm.requestSubmit()` — the same POST-and-navigate flow text search
 * already uses. Voice never invents its own search path.
 */

import { VoiceStateMachine } from "./voice-state-machine.js";
import { AudioCapture, MicrophonePermissionError } from "./audio-capture.js";
import { DeepgramSocket, VoiceRateLimitedError } from "./deepgram-socket.js";
import { WaveformCanvas } from "./waveform-canvas.js";
import { AIOrb } from "./ai-orb.js?v=20260728-reactbits-orb";
import { parseTravelIntent, assessConfidence, TravelParseFailedError } from "./travel-intent-bridge.js";

const LISTENING_MESSAGES = [
  "Listening…",
  "Go ahead…",
  "Where would you like to travel?",
  "Tell me your destination…",
];
const PROCESSING_MESSAGES = [
  "Understanding your trip…",
  "Finding airports…",
  "Analyzing travel request…",
  "Preparing search…",
];

// Pre-speech grace period: if nobody says anything at all, stop gracefully
// instead of leaving the mic open forever.
const NO_SPEECH_TIMEOUT_MS = 8000;
// Client-side endpointing safety net (Deepgram's own endpointing/UtteranceEnd
// is the primary mechanism — this only covers the case where the socket is
// degraded and those events never arrive).
const SILENCE_FALLBACK_MS = 1600;
const SILENCE_AMPLITUDE_THRESHOLD = 0.02;

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function isMac() {
  return /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent || "");
}

export class VoiceOverlayController {
  constructor({ micButtonId = "voiceMicBtn", textareaId = "aiText", formId = "aiForm", tokenId = "aiParseToken" } = {}) {
    this.micButton = document.getElementById(micButtonId);
    this.textarea = document.getElementById(textareaId);
    this.form = document.getElementById(formId);
    this.tokenInput = document.getElementById(tokenId);
    if (!this.micButton || !this.textarea || !this.form) return; // page doesn't have the AI search bar

    this.machine = new VoiceStateMachine();
    this.reducedMotion = prefersReducedMotion();

    this._audio = null;
    this._socket = null;
    this._abortController = null;
    this._busy = false;
    this._sessionGen = 0;
    this._rotationTimer = null;
    this._ampLoopId = null;
    this._lowAmplitudeSince = null;
    this._speechEverDetected = false;
    this._listenStartedAt = 0;
    this._previouslyFocused = null;

    this._buildInlineDom();
    if (!this.ring) return; // search bar markup wasn't as expected — stay dormant
    this._wireMicButton();
    this._wireKeyboardShortcuts();
    this._wireStateTransitions();
    window.addEventListener("pagehide", () => this._hardTeardown());
  }

  // ── DOM construction (inline, inside the AI search bar) ──────────────

  _buildInlineDom() {
    // Mount the voice UI *inside* the existing search bar's gradient ring, so
    // the bar transforms in place — no full-screen modal, no separate view.
    this.ring = this.micButton.closest(".ai-input-ring");
    this.wrap = this.ring ? this.ring.querySelector(".ai-input-wrap") : null;
    if (!this.ring || !this.wrap) {
      this.ring = null; // page markup unexpectedly different — disable gracefully
      return;
    }

    const stage = document.createElement("div");
    stage.className = "voice-inline";
    stage.setAttribute("aria-hidden", "true");
    stage.dataset.voiceState = "IDLE";
    if (this.reducedMotion) stage.classList.add("voice-reduced-motion");

    stage.innerHTML = `
      <span class="voice-inline-orb-wrap" aria-hidden="true">
        <canvas class="voice-inline-orb"></canvas>
      </span>
      <div class="voice-inline-center">
        <div class="voice-inline-live">
          <canvas class="voice-inline-waveform" aria-hidden="true"></canvas>
          <p class="voice-inline-transcript" aria-live="polite"></p>
        </div>
        <p class="voice-inline-status" aria-live="polite"></p>
        <div class="voice-inline-error" hidden>
          <span class="voice-inline-error-msg"></span>
          <button type="button" class="voice-inline-btn voice-retry-btn">Try again</button>
          <button type="button" class="voice-inline-btn voice-inline-btn--ghost voice-use-anyway-btn" hidden>Use anyway</button>
        </div>
      </div>
      <button type="button" class="voice-inline-close" aria-label="Cancel voice search" title="Cancel (Esc)">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18"/><path d="M6 6l12 12"/></svg>
      </button>
    `;
    this.ring.appendChild(stage);

    this.dom = {
      stage,
      orbCanvas: stage.querySelector(".voice-inline-orb"),
      waveformCanvas: stage.querySelector(".voice-inline-waveform"),
      statusText: stage.querySelector(".voice-inline-status"),
      transcript: stage.querySelector(".voice-inline-transcript"),
      errorBox: stage.querySelector(".voice-inline-error"),
      errorMessage: stage.querySelector(".voice-inline-error-msg"),
      retryBtns: stage.querySelectorAll(".voice-retry-btn"),
      useAnywayBtn: stage.querySelector(".voice-use-anyway-btn"),
      closeBtn: stage.querySelector(".voice-inline-close"),
    };

    this.orb = new AIOrb(this.dom.orbCanvas);
    this.waveform = new WaveformCanvas(this.dom.waveformCanvas);

    this.dom.closeBtn.addEventListener("click", () => this.cancel());
    this.dom.retryBtns.forEach((btn) => btn.addEventListener("click", () => this._retry()));
    this.dom.useAnywayBtn.addEventListener("click", () => this._useAnyway());
  }

  _wireMicButton() {
    this.micButton.setAttribute("aria-pressed", "false");
    this.micButton.addEventListener("click", (e) => {
      e.preventDefault();
      this._haptic(10);
      this.toggle();
    });
  }

  _wireKeyboardShortcuts() {
    document.addEventListener("keydown", (e) => {
      const isShortcut = e.shiftKey && e.code === "Space" && (isMac() ? e.metaKey : e.ctrlKey);
      if (isShortcut) {
        e.preventDefault();
        this.toggle();
        return;
      }
      if (e.key === "Escape" && this.machine.isActive()) {
        e.preventDefault();
        this.cancel();
      }
    });
  }

  // ── State machine → UI ──────────────────────────────────────────────

  _wireStateTransitions() {
    this.machine.onTransition(({ to, context }) => {
      this.micButton.classList.toggle("voice-active", this.machine.isActive());
      this.micButton.setAttribute("aria-pressed", String(this.machine.isActive()));
      // CSS keys visibility of orb / waveform / status / error off this attr.
      if (to !== "CANCELLED" && to !== "SUCCESS") this.dom.stage.dataset.voiceState = to;

      switch (to) {
        case "PERMISSION":
          this._openInline();
          this._showError(false);
          this.orb.setState("idle");
          this.orb.start();
          this._setStatus("Getting ready…");
          break;
        case "LISTENING":
          this._showError(false);
          this.orb.setState("listening");
          this._beginRotatingStatus(LISTENING_MESSAGES, 2400);
          this._setTranscript("");
          break;
        case "TRANSCRIBING":
          this._clearRotatingStatus();
          this.orb.setState("speaking");
          this._setStatus("Listening…");
          this._setTranscript((context.finalTranscript + " " + context.interimTranscript).trim());
          break;
        case "PROCESSING":
          this.orb.setState("thinking");
          this._beginRotatingStatus(PROCESSING_MESSAGES, 850);
          break;
        case "PARSING":
          this.orb.setState("thinking");
          break;
        case "SEARCHING":
          this._clearRotatingStatus();
          this.orb.setState("success");
          this._setStatus("Found it — searching…");
          this._haptic(15);
          break;
        case "SUCCESS":
          break;
        case "ERROR":
          this._clearRotatingStatus();
          this.orb.setState("error");
          this.orb.shake();
          this._haptic([10, 40, 10]);
          this._renderError(context.error);
          break;
        case "CANCELLED":
          this._clearRotatingStatus();
          this._closeInline();
          this.machine.send("RESET");
          break;
        case "IDLE":
          break;
      }
    });
  }

  // ── Public control surface ──────────────────────────────────────────

  toggle() {
    if (this.machine.isActive()) {
      this.cancel();
    } else {
      this.startListening();
    }
  }

  async startListening() {
    if (this._busy) return; // guards rapid repeated clicks / double-invocation
    this._busy = true;
    this.machine.hardReset();
    this.machine.send("START");

    // Server told us at page-load whether Deepgram is configured at all. If
    // not, fail fast with a clean message instead of prompting for mic
    // permission first — /voice/session-token would 503 anyway, but this
    // skips the pointless permission dialog on a backend that can't listen.
    const config = window.SKAIR_VOICE_CONFIG || {};
    if (config.enabled === false) {
      this.machine.send("DENIED", {
        error: { kind: "unavailable", message: "Voice search isn't set up yet — try typing your trip instead.", retryable: false },
      });
      this._busy = false;
      return;
    }

    // Generation guard: if cancel() runs while we're mid-`await` below (e.g.
    // Escape pressed while the permission prompt is still open), bumping
    // _sessionGen makes every stale continuation below a no-op instead of
    // resurrecting a mic stream nobody asked for anymore.
    const mySession = ++this._sessionGen;
    this._abortController = new AbortController();
    this._speechEverDetected = false;
    this._listenStartedAt = performance.now();

    const audio = new AudioCapture();
    const socket = new DeepgramSocket();
    this._audio = audio;
    this._socket = socket;

    try {
      // Mic permission is requested immediately — no spinner, no confirmation
      // step — so the user can start talking the instant the prompt clears.
      await audio.start((frame) => this._onAudioFrame(frame));
    } catch (err) {
      this._busy = false;
      if (mySession !== this._sessionGen) return; // cancelled before permission settled
      this._audio = null;
      this._handlePermissionError(err);
      return;
    }

    if (mySession !== this._sessionGen) {
      audio.stop(); // cancelled while the permission prompt was open — release the mic we just got
      this._busy = false;
      return;
    }

    this.machine.send("GRANTED");
    this._analyser = audio.getAnalyser();
    this.waveform.start(this._analyser);
    this._startAmpLoop();
    this._busy = false;

    this._wireSocketEvents(socket);
    try {
      await socket.connect(this._abortController.signal);
    } catch (err) {
      if (mySession !== this._sessionGen) return; // cancelled mid-connect
      if (err && err.name === "AbortError") return;
      this._teardownAudioAndSocket();
      const kind = err instanceof VoiceRateLimitedError ? "rate_limited" : "unavailable";
      this.machine.send("SOCKET_ERROR", {
        error: {
          kind,
          message:
            kind === "rate_limited"
              ? "Give it a moment — you've started voice search a lot in the last minute."
              : "Voice search is having trouble connecting right now.",
          retryable: true,
        },
      });
      return;
    }

    if (mySession !== this._sessionGen) {
      // cancelled in the instant between connect() resolving and this check
      this._teardownAudioAndSocket();
    }
  }

  cancel() {
    this._sessionGen++;
    this._abortController?.abort();
    this._teardownAudioAndSocket();
    this.waveform.stop();
    this._clearRotatingStatus();
    if (this.machine.is("IDLE", "CANCELLED")) {
      this._closeInline();
      return;
    }
    this.machine.send("CANCEL");
  }

  // ── Audio + socket plumbing ─────────────────────────────────────────

  _onAudioFrame(frame) {
    this._socket?.sendAudioFrame(frame);
  }

  _wireSocketEvents(socket) {
    socket.addEventListener("speech-started", () => {
      this._speechEverDetected = true;
      this._lowAmplitudeSince = null;
      if (this.machine.state === "LISTENING") this.machine.send("SPEECH_DETECTED");
    });

    socket.addEventListener("transcript", (e) => {
      const { transcript, isFinal } = e.detail;
      this._speechEverDetected = true;
      this._lowAmplitudeSince = null;
      if (this.machine.state === "LISTENING") this.machine.send("SPEECH_DETECTED");

      if (isFinal) {
        const merged = `${this.machine.context.finalTranscript} ${transcript}`.trim();
        this.machine.send("INTERIM", { finalTranscript: merged, interimTranscript: "" });
      } else {
        this.machine.send("INTERIM", { interimTranscript: transcript });
      }
      this._setTranscript((this.machine.context.finalTranscript + " " + this.machine.context.interimTranscript).trim());
    });

    socket.addEventListener("utterance-end", () => {
      if (this.machine.state === "TRANSCRIBING" && this.machine.context.finalTranscript.trim()) {
        this._beginProcessing();
      }
    });

    socket.addEventListener("error", () => {
      if (!this.machine.isActive()) return;
      this._teardownAudioAndSocket();
      this.machine.send("SOCKET_ERROR", {
        error: { kind: "network", message: "Lost the voice connection.", retryable: true },
      });
    });

    socket.addEventListener("disconnected", () => {
      if (this.machine.is("LISTENING", "TRANSCRIBING")) {
        this._teardownAudioAndSocket();
        this.machine.send("SOCKET_ERROR", {
          error: { kind: "network", message: "Lost the voice connection.", retryable: true },
        });
      }
    });
  }

  /** requestAnimationFrame loop: feeds the orb real amplitude and runs the client-side silence fallback. */
  _startAmpLoop() {
    const step = () => {
      if (!this._audio) return;
      const amplitude = this._audio.getAmplitude();
      this.orb.setAmplitude(this.machine.is("TRANSCRIBING") ? amplitude : amplitude * 0.4);

      const now = performance.now();
      if (this.machine.state === "LISTENING" && !this._speechEverDetected) {
        if (now - this._listenStartedAt > NO_SPEECH_TIMEOUT_MS) {
          this.machine.send("SILENCE_TIMEOUT", {
            error: { kind: "no_speech", message: "We didn't catch that.", retryable: true },
          });
          this._teardownAudioAndSocket();
          this._ampLoopId = null;
          return;
        }
      } else if (this.machine.state === "TRANSCRIBING") {
        if (amplitude < SILENCE_AMPLITUDE_THRESHOLD) {
          if (this._lowAmplitudeSince == null) this._lowAmplitudeSince = now;
          else if (now - this._lowAmplitudeSince > SILENCE_FALLBACK_MS) {
            this._beginProcessing();
            this._ampLoopId = null;
            return;
          }
        } else {
          this._lowAmplitudeSince = null;
        }
      }
      this._ampLoopId = requestAnimationFrame(step);
    };
    this._ampLoopId = requestAnimationFrame(step);
  }

  _stopAmpLoop() {
    if (this._ampLoopId) {
      cancelAnimationFrame(this._ampLoopId);
      this._ampLoopId = null;
    }
  }

  _teardownAudioAndSocket() {
    this._stopAmpLoop();
    this._socket?.close();
    this._socket = null;
    this._audio?.stop();
    this._audio = null;
  }

  // ── Processing → parsing → search ───────────────────────────────────

  async _beginProcessing() {
    this._stopAmpLoop();
    this._socket?.finalizeAndClose();
    this._audio?.stop();
    this._audio = null;

    const transcript = this.machine.context.finalTranscript.trim();
    this.waveform.collapse();

    if (!transcript) {
      this.machine.send("EMPTY_TRANSCRIPT", {
        error: { kind: "no_speech", message: "We didn't catch that.", retryable: true },
      });
      return;
    }

    this.machine.send("HAS_TRANSCRIPT");

    try {
      const { parseToken, preview } = await parseTravelIntent(transcript, this._abortController?.signal);
      const { confident, question } = assessConfidence(preview);
      if (!confident) {
        this.machine.send("PARSE_LOW_CONFIDENCE", {
          error: { kind: "low_confidence", message: question, retryable: true },
          parsePreview: preview,
          parseToken,
        });
        this.dom.useAnywayBtn.hidden = !(preview.origin || preview.destination || preview.flex_month);
        return;
      }
      this.machine.send("PARSE_OK", { parsePreview: preview, parseToken });
      this._commitSearch(transcript, parseToken);
    } catch (err) {
      if (err && err.name === "AbortError") return;
      const message =
        err instanceof TravelParseFailedError
          ? "Couldn't quite understand that trip."
          : "Something went wrong understanding that.";
      this.machine.send("PARSE_FAILED", { error: { kind: "parse_failed", message, retryable: true } });
    }
  }

  _commitSearch(transcript, parseToken) {
    this.textarea.value = transcript;
    this.textarea.dispatchEvent(new Event("input", { bubbles: true }));
    if (this.tokenInput && parseToken) this.tokenInput.value = parseToken;
    this.machine.send("NAVIGATE");

    const settleDelay = this.reducedMotion ? 120 : 480;
    setTimeout(() => {
      this._closeInline();
      this.form.requestSubmit();
      this.machine.send("RESET");
    }, settleDelay);
  }

  _useAnyway() {
    const { finalTranscript } = this.machine.context;
    const { parseToken } = this.machine.context;
    this._commitSearch(finalTranscript.trim(), parseToken);
  }

  _retry() {
    // startListening() already does a hardReset() + START from scratch, so
    // it can restart cleanly from any prior state (including ERROR) without
    // needing an explicit RETRY transition first.
    this.startListening();
  }

  // ── Error / permission rendering ────────────────────────────────────

  _handlePermissionError(err) {
    const kind = err instanceof MicrophonePermissionError ? err.kind : "unknown";
    const messages = {
      // Guide the user to the address-bar site control, since a "denied" with
      // no prompt means the browser has this site's mic set to Block.
      denied: "Mic blocked. Allow it via the site icon in the address bar, then retry.",
      not_found: "No microphone was found on this device.",
      insecure_context: "Voice search needs a secure (https or localhost) connection.",
      unsupported: "This browser doesn't support voice search.",
      unknown: (err && err.message) || "Could not access the microphone.",
    };
    this.machine.send("DENIED", {
      error: { kind, message: messages[kind] || messages.unknown, retryable: kind !== "unsupported" && kind !== "insecure_context" },
    });
  }

  _renderError(error) {
    if (!error) return;
    this._showError(true);
    this.dom.errorMessage.textContent = error.message;
    this.dom.retryBtns.forEach((b) => (b.hidden = !error.retryable));
    this._setStatus("");
    this._setTranscript("");
    const focusTarget = this.dom.stage.querySelector(".voice-retry-btn:not([hidden])") || this.dom.closeBtn;
    focusTarget.focus();
  }

  _showError(show) {
    this.dom.errorBox.hidden = !show;
    this.dom.stage.classList.toggle("has-error", show);
  }

  // ── Small UI helpers ─────────────────────────────────────────────────

  _setStatus(text) {
    const el = this.dom.statusText;
    if (this.reducedMotion || !el.textContent) {
      el.textContent = text;
      return;
    }
    // Subtle crossfade between rotating status lines instead of a hard text swap.
    el.classList.add("is-fading");
    setTimeout(() => {
      el.textContent = text;
      el.classList.remove("is-fading");
    }, 140);
  }

  _setTranscript(text) {
    this.dom.transcript.textContent = text;
  }

  _beginRotatingStatus(messages, intervalMs) {
    this._clearRotatingStatus();
    let i = 0;
    this._setStatus(messages[0]);
    this._rotationTimer = setInterval(() => {
      i = (i + 1) % messages.length;
      this._setStatus(messages[i]);
    }, intervalMs);
  }

  _clearRotatingStatus() {
    if (this._rotationTimer) {
      clearInterval(this._rotationTimer);
      this._rotationTimer = null;
    }
  }

  _haptic(pattern) {
    if (navigator.vibrate) {
      try {
        navigator.vibrate(pattern);
      } catch (e) {}
    }
  }

  // ── Inline open / close ─────────────────────────────────────────────

  _openInline() {
    this._previouslyFocused = document.activeElement;
    this.dom.stage.setAttribute("aria-hidden", "false");
    // Toggling this class on the ring visually swaps the normal input contents
    // for the voice stage (CSS handles the crossfade + any height growth).
    this.ring.classList.add("voice-active-inline");
    document.body.classList.add("voice-inline-active");
  }

  _closeInline() {
    this.ring.classList.remove("voice-active-inline");
    document.body.classList.remove("voice-inline-active");
    this.orb.stop();
    this.waveform.stop();
    const finish = () => {
      this.dom.stage.setAttribute("aria-hidden", "true");
      this._showError(false);
      this._setTranscript("");
      this._setStatus("");
      this.dom.stage.dataset.voiceState = "IDLE";
    };
    if (this.reducedMotion) finish();
    else setTimeout(finish, 260);
    const canRestoreFocus =
      this._previouslyFocused && document.contains(this._previouslyFocused) && this._previouslyFocused.offsetParent !== null;
    (canRestoreFocus ? this._previouslyFocused : this.micButton).focus();
  }

  _hardTeardown() {
    this._teardownAudioAndSocket();
    this.orb.destroy();
    this.waveform.destroy();
    this._clearRotatingStatus();
  }
}

/**
 * The controller is created unconditionally, even on unsupported browsers —
 * AudioCapture.isSupported() is re-checked inside startListening(), so an
 * unsupported browser still gets a clear, retryable error message on tap
 * instead of a mic button that silently does nothing.
 */
export function initVoiceSearch(options) {
  return new VoiceOverlayController(options);
}
