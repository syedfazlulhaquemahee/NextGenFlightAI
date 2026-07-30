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
import { parseTravelIntent, assessConfidence, TravelParseFailedError } from "./travel-intent-bridge.js?v=20260728-smarter";

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

// Pre-speech grace period: if nothing is said in the first several seconds,
// close the voice UI completely back to the default search bar (no error, no prompt).
const NO_SPEECH_TIMEOUT_MS = 7000;
// Client-side endpointing safety net (Deepgram's own endpointing/UtteranceEnd
// is the primary mechanism — this only covers the case where the socket is
// degraded and those events never arrive).
const SILENCE_FALLBACK_MS = 1400;
const SILENCE_AMPLITUDE_THRESHOLD = 0.02;
// Absolute ceiling from the moment listening begins. Whatever happens —
// continuous noise, a wedged socket, a state that never advances — the mic
// stops here. This is the hard guarantee that it can never stay open forever.
const MAX_SESSION_MS = 15000;
// How many times we'll *automatically* re-listen after a too-vague result
// before we stop and wait for a deliberate tap. Keeps the clarify loop
// conversational without ever looping forever.
const MAX_AUTO_CLARIFY = 1;
// A usable travel request needs at least this many words; anything shorter is
// treated as "didn't catch a real trip" rather than sent to the parser.
const MIN_TRANSCRIPT_WORDS = 2;

// One-time "voice is in testing" disclaimer, shown as a real modal on the first
// mic tap only; once acknowledged we remember it and go straight to listening.
const DISCLAIMER_ACK_KEY = "skair_voice_disclaimer_ack_v1";

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
    this._watchdogId = null;
    this._lowAmplitudeSince = null;
    this._speechEverDetected = false;
    this._soundEverDetected = false;
    this._listenStartedAt = 0;
    this._autoClarifyCount = 0;
    this._autoRetryTimer = null;
    this._previouslyFocused = null;
    this._disclaimerOpen = false;
    this._disclaimerPrevFocus = null;

    this._buildInlineDom();
    if (!this.ring) return; // search bar markup wasn't as expected — stay dormant
    this._buildDisclaimerModal();
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
          <button type="button" class="voice-inline-icon-btn voice-retry-btn" aria-label="Retry voice search" title="Retry voice search">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M20 12a8 8 0 1 1-2.34-5.66"/>
              <path d="M20 12v4"/>
              <path d="M20 12h-4"/>
            </svg>
          </button>
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

  // ── Disclaimer modal (a real pop-up, appended to <body>) ─────────────
  // Shown once, on the first mic tap. "Got it, start" is the user gesture that
  // then opens the mic; after the first acknowledgement we skip straight to
  // listening. Kept entirely separate from the in-bar voice stage.

  _buildDisclaimerModal() {
    const modal = document.createElement("div");
    modal.className = "voice-modal";
    modal.hidden = true;
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.setAttribute("aria-labelledby", "voiceModalTitle");
    modal.innerHTML = `
      <div class="voice-modal-backdrop" data-voice-modal-dismiss></div>
      <div class="voice-modal-card" role="document">
        <span class="voice-modal-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
            <path d="M19 10v1a7 7 0 0 1-14 0v-1"/>
            <path d="M12 18v4"/><path d="M9 22h6"/>
          </svg>
        </span>
        <h2 class="voice-modal-title" id="voiceModalTitle">Voice search is in testing</h2>
        <p class="voice-modal-text">Speak your trip out loud — say something like “Dhaka to Bangkok next Friday.” It's still experimental and may not be 100% accurate yet.</p>
        <div class="voice-modal-actions">
          <button type="button" class="voice-modal-btn voice-modal-btn--ghost" data-voice-modal-dismiss>Not now</button>
          <button type="button" class="voice-modal-btn voice-modal-btn--primary voice-modal-start">Got it, start</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);

    this.disclaimerModal = {
      root: modal,
      startBtn: modal.querySelector(".voice-modal-start"),
    };
    modal.querySelectorAll("[data-voice-modal-dismiss]").forEach((el) =>
      el.addEventListener("click", () => this._hideDisclaimerModal())
    );
    this.disclaimerModal.startBtn.addEventListener("click", () => this._ackAndStart());
  }

  _disclaimerAcknowledged() {
    try {
      return localStorage.getItem(DISCLAIMER_ACK_KEY) === "1";
    } catch (e) {
      return false; // private mode / storage blocked — just show it each time
    }
  }

  _setDisclaimerAcknowledged() {
    try {
      localStorage.setItem(DISCLAIMER_ACK_KEY, "1");
    } catch (e) {}
  }

  _beginFlow() {
    const enabled = (window.SKAIR_VOICE_CONFIG || {}).enabled !== false;
    if (enabled && !this._disclaimerAcknowledged()) {
      this._showDisclaimerModal();
    } else {
      this.startListening();
    }
  }

  _showDisclaimerModal() {
    if (!this.disclaimerModal) return this.startListening();
    this._disclaimerOpen = true;
    this._disclaimerPrevFocus = document.activeElement;
    this.disclaimerModal.root.hidden = false;
    // Force a reflow so the fade-in transition runs from the hidden state.
    void this.disclaimerModal.root.offsetWidth;
    this.disclaimerModal.root.classList.add("is-open");
    document.body.classList.add("voice-modal-open");
    requestAnimationFrame(() => this.disclaimerModal.startBtn.focus());
  }

  _hideDisclaimerModal() {
    if (!this.disclaimerModal || !this._disclaimerOpen) return;
    this._disclaimerOpen = false;
    this.disclaimerModal.root.classList.remove("is-open");
    document.body.classList.remove("voice-modal-open");
    const finish = () => {
      if (!this._disclaimerOpen) this.disclaimerModal.root.hidden = true;
    };
    if (this.reducedMotion) finish();
    else setTimeout(finish, 200);
    const prev = this._disclaimerPrevFocus;
    if (prev && document.contains(prev) && prev.offsetParent !== null) prev.focus();
    else this.micButton.focus();
  }

  _ackAndStart() {
    this._setDisclaimerAcknowledged();
    this._hideDisclaimerModal();
    this.startListening();
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
      if (e.key === "Escape" && this._disclaimerOpen) {
        e.preventDefault();
        this._hideDisclaimerModal();
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
    if (this._disclaimerOpen) {
      this._hideDisclaimerModal();
    } else if (this.machine.isActive()) {
      this.cancel();
    } else {
      this._beginFlow();
    }
  }

  async startListening(opts = {}) {
    if (this._busy) return; // guards rapid repeated clicks / double-invocation
    this._busy = true;
    // A "fresh" start (mic tap, keyboard shortcut, manual Try-again) resets the
    // auto-clarify budget; an internal conversational re-listen keeps it, so the
    // back-and-forth stays bounded.
    if (opts.fresh !== false) this._autoClarifyCount = 0;
    this._clearAutoRetry();
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
    this._soundEverDetected = false;
    this._soundTicks = 0;
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
    this._startSensors();
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
    this._clearAutoRetry();
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

  /**
   * Start the two independent loops that run while the mic is open:
   *
   *  1. A requestAnimationFrame loop — PURELY VISUAL. It feeds the orb live
   *     amplitude for a smooth reaction and does nothing else. rAF is the right
   *     tool for smooth rendering, but the wrong tool for timing (it pauses in
   *     background tabs and can be starved), which is exactly why the previous
   *     "it got stuck" bug happened.
   *
   *  2. A setInterval watchdog — ALL TIMING. Running on a fixed interval it can
   *     never be starved or paused, and it checks elapsed time directly rather
   *     than depending on a per-state code path, so there is no state in which
   *     the mic can stay open. This is the guarantee the rAF version lacked.
   */
  _startSensors() {
    this._stopSensors();
    // Anchor the listen clock to the moment we actually begin capturing, so a
    // slow permission prompt can't eat into the 3-second no-sound window.
    this._listenStartedAt = performance.now();
    const visual = () => {
      if (!this._audio) {
        this._ampLoopId = null;
        return;
      }
      const amplitude = this._audio.getAmplitude();
      this.orb.setAmplitude(this.machine.is("TRANSCRIBING") ? amplitude : amplitude * 0.4);
      this._ampLoopId = requestAnimationFrame(visual);
    };
    this._ampLoopId = requestAnimationFrame(visual);
    this._watchdogId = setInterval(() => this._watchdogTick(), 200);
  }

  _stopSensors() {
    if (this._ampLoopId) {
      cancelAnimationFrame(this._ampLoopId);
      this._ampLoopId = null;
    }
    if (this._watchdogId) {
      clearInterval(this._watchdogId);
      this._watchdogId = null;
    }
  }

  /** The single source of truth for "should we stop listening now?" */
  _watchdogTick() {
    if (!this._audio) {
      this._stopSensors();
      return;
    }
    const st = this.machine.state;
    if (st !== "LISTENING" && st !== "TRANSCRIBING") return; // busy processing; nothing to police
    const now = performance.now();
    const amplitude = this._audio.getAmplitude();

    // Any real sound (mic amplitude, not Deepgram events — those lag socket
    // setup) cancels the auto-close. Require it to persist for a couple of
    // checks (~400ms) so a single mic startup transient doesn't count as sound.
    if (amplitude >= SILENCE_AMPLITUDE_THRESHOLD) {
      this._soundTicks = (this._soundTicks || 0) + 1;
      if (this._soundTicks >= 2) this._soundEverDetected = true;
    } else {
      this._soundTicks = 0;
    }

    // (1) Absolute ceiling — the ultimate anti-stuck stop. No matter the state,
    // speech, silence, or socket health, we never stay open past this.
    if (now - this._listenStartedAt > MAX_SESSION_MS) {
      this._endUtterance();
      return;
    }

    // (2) No sound at all in the first 3 seconds → close completely back to the
    // default search bar. cancel() tears everything down and returns to IDLE,
    // so there's no lingering error or prompt — exactly as if dismissed.
    if (!this._soundEverDetected) {
      if (now - this._listenStartedAt > NO_SPEECH_TIMEOUT_MS) this.cancel();
      return;
    }

    // (3) Sound happened → end the utterance on sustained quiet (client-side
    // endpoint; Deepgram's own endpointing is the primary path).
    if (amplitude < SILENCE_AMPLITUDE_THRESHOLD) {
      if (this._lowAmplitudeSince == null) this._lowAmplitudeSince = now;
      else if (now - this._lowAmplitudeSince > SILENCE_FALLBACK_MS) this._endUtterance();
    } else {
      this._lowAmplitudeSince = null;
    }
  }

  /** Enter processing. _beginProcessing() is idempotent (guards on state), so it's safe to call from any trigger. */
  _endUtterance() {
    this._beginProcessing();
  }

  _teardownAudioAndSocket() {
    this._stopSensors();
    this._lowAmplitudeSince = null;
    this._socket?.close();
    this._socket = null;
    this._audio?.stop();
    this._audio = null;
  }

  _clearAutoRetry() {
    if (this._autoRetryTimer) {
      clearTimeout(this._autoRetryTimer);
      this._autoRetryTimer = null;
    }
  }

  // ── Processing → parsing → search ───────────────────────────────────

  async _beginProcessing() {
    // Re-entry guard: utterance-end, the silence fallback, and the max-utterance
    // cap can all race to trigger this. Only the first one (while still
    // listening) should run; the rest become no-ops.
    if (!this.machine.is("LISTENING", "TRANSCRIBING")) return;

    this._stopSensors();
    this._socket?.finalizeAndClose();
    this._audio?.stop();
    this._audio = null;

    // Include the not-yet-finalized interim words — otherwise a fast endpoint
    // can drop the tail of what was just said.
    const ctx = this.machine.context;
    const transcript = `${ctx.finalTranscript} ${ctx.interimTranscript}`.trim();
    this.waveform.collapse();

    // Advance the machine out of TRANSCRIBING into PROCESSING. Without this the
    // state stayed TRANSCRIBING (SILENCE_END was never sent), so the CSS kept
    // the waveform box at its 84px listening width — an empty flat box sitting
    // to the LEFT of the transcript while processing. Moving to PROCESSING
    // collapses that box to 0 and shows the processing status instead.
    if (this.machine.is("LISTENING")) this.machine.send("SPEECH_DETECTED"); // → TRANSCRIBING
    this.machine.send("SILENCE_END"); // TRANSCRIBING → PROCESSING

    const wordCount = transcript ? transcript.split(/\s+/).filter(Boolean).length : 0;
    if (wordCount < MIN_TRANSCRIPT_WORDS) {
      // Too little to be a real trip — re-ask instead of firing an empty search.
      this.machine.send("EMPTY_TRANSCRIPT", {
        error: { kind: "no_speech", message: "Didn't catch a trip — try “Dhaka to Bangkok next Friday”.", retryable: true },
      });
      return;
    }

    this.machine.send("HAS_TRANSCRIPT"); // PROCESSING → PARSING

    try {
      const { parseToken, preview } = await parseTravelIntent(transcript, this._abortController?.signal);
      const { confident, question } = assessConfidence(preview);
      if (!confident) {
        this._handleVague(question, preview, parseToken);
        return;
      }
      this.machine.send("PARSE_OK", { parsePreview: preview, parseToken });
      this._commitSearch(transcript, parseToken);
    } catch (err) {
      if (err && err.name === "AbortError") return;
      const message =
        err instanceof TravelParseFailedError
          ? "Couldn't quite understand that trip — say it again?"
          : "Something went wrong — tap to try again.";
      this.machine.send("PARSE_FAILED", { error: { kind: "parse_failed", message, retryable: true } });
    }
  }

  /**
   * Vague/incomplete request: never submit a half-formed search. Show the
   * clarifying question right in the voice UI. The first time, we re-listen
   * automatically after a beat so it feels like a conversation ("Which city
   * are you flying from?" → they answer). After the auto-clarify budget is
   * spent we stop and wait for a deliberate tap, so it can't loop or hang.
   */
  _handleVague(question, preview, parseToken) {
    const hasPartial = !!(preview.origin || preview.destination || preview.flex_month);
    this.machine.send("PARSE_LOW_CONFIDENCE", {
      error: { kind: "low_confidence", message: question, retryable: true },
      parsePreview: preview,
      parseToken,
    });
    // Offer "Use anyway" only when there's at least a partial trip to run with.
    this.dom.useAnywayBtn.hidden = !hasPartial;

    if (this._autoClarifyCount < MAX_AUTO_CLARIFY) {
      this._autoClarifyCount += 1;
      this._clearAutoRetry();
      this._autoRetryTimer = setTimeout(() => {
        this._autoRetryTimer = null;
        // Only auto re-listen if the user hasn't already moved on.
        if (this.machine.state === "ERROR") this._retry({ fresh: false });
      }, 1700);
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
    this._clearAutoRetry();
    const { finalTranscript, interimTranscript, parseToken } = this.machine.context;
    const transcript = `${finalTranscript} ${interimTranscript}`.trim();
    this._commitSearch(transcript, parseToken);
  }

  _retry(opts = {}) {
    // startListening() already does a hardReset() + START from scratch, so it
    // can restart cleanly from any prior state (including ERROR). A manual tap
    // is a fresh start (resets the auto-clarify budget); an internal re-ask
    // passes { fresh: false } to keep the conversation bounded.
    this._clearAutoRetry();
    this.startListening(opts);
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
    if (el.textContent === text) return; // no-op: avoids a pointless fade flicker
    if (this.reducedMotion || !el.textContent) {
      el.textContent = text;
      return;
    }
    // Crossfade instead of a hard text swap. Guard the timer so back-to-back
    // calls (fast rotation) can't stack and stutter.
    if (this._statusFadeTimer) clearTimeout(this._statusFadeTimer);
    el.classList.add("is-fading");
    this._statusFadeTimer = setTimeout(() => {
      el.textContent = text;
      el.classList.remove("is-fading");
      this._statusFadeTimer = null;
    }, 150);
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
    if (this._statusFadeTimer) {
      clearTimeout(this._statusFadeTimer);
      this._statusFadeTimer = null;
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
