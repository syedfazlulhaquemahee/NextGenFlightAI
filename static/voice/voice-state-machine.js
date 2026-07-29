/**
 * Skairova Voice AI — finite state machine.
 *
 * A single explicit source of truth for "what is voice search doing right
 * now", so the overlay, orb, and waveform never have to reconstruct that
 * from a pile of booleans (isListening && !isProcessing && hasTranscript...).
 * Every other voice module reacts to `state` + `context`, it never owns them.
 *
 * States: IDLE, PERMISSION, LISTENING, TRANSCRIBING, PROCESSING, PARSING,
 *         SEARCHING, SUCCESS, ERROR, CANCELLED
 */

/** @typedef {"IDLE"|"PERMISSION"|"LISTENING"|"TRANSCRIBING"|"PROCESSING"|"PARSING"|"SEARCHING"|"SUCCESS"|"ERROR"|"CANCELLED"} VoiceState */

/**
 * @typedef {Object} VoiceContext
 * @property {string} interimTranscript
 * @property {string} finalTranscript
 * @property {string} detectedLanguage
 * @property {number} retryCount
 * @property {{kind: string, message: string, retryable: boolean, suggestion?: string}|null} error
 * @property {Object|null} parsePreview
 * @property {string} parseToken
 */

/** @returns {VoiceContext} */
function createInitialContext() {
  return {
    interimTranscript: "",
    finalTranscript: "",
    detectedLanguage: "",
    retryCount: 0,
    error: null,
    parsePreview: null,
    parseToken: "",
  };
}

// Transition table: state -> event -> next state (or a function returning one).
// Anything not listed is simply ignored — invalid transitions are no-ops,
// not thrown errors, so a late/duplicate event from a slow network can never
// crash the UI.
const TRANSITIONS = {
  IDLE: {
    START: "PERMISSION",
  },
  PERMISSION: {
    GRANTED: "LISTENING",
    DENIED: "ERROR",
    CANCEL: "CANCELLED",
  },
  LISTENING: {
    SPEECH_DETECTED: "TRANSCRIBING",
    SILENCE_TIMEOUT: "ERROR", // opened the mic, nobody said anything
    SOCKET_ERROR: "ERROR",
    CANCEL: "CANCELLED",
  },
  TRANSCRIBING: {
    INTERIM: "TRANSCRIBING",
    SILENCE_END: "PROCESSING",
    SOCKET_ERROR: "ERROR",
    CANCEL: "CANCELLED",
  },
  PROCESSING: {
    HAS_TRANSCRIPT: "PARSING",
    EMPTY_TRANSCRIPT: "ERROR",
    CANCEL: "CANCELLED",
  },
  PARSING: {
    PARSE_OK: "SEARCHING",
    PARSE_LOW_CONFIDENCE: "ERROR",
    PARSE_FAILED: "ERROR",
    CANCEL: "CANCELLED",
  },
  SEARCHING: {
    NAVIGATE: "SUCCESS",
    CANCEL: "CANCELLED",
  },
  SUCCESS: {
    RESET: "IDLE",
  },
  ERROR: {
    RETRY: "PERMISSION",
    RESET: "IDLE",
    CANCEL: "CANCELLED",
  },
  CANCELLED: {
    RESET: "IDLE",
  },
};

export class VoiceStateMachine extends EventTarget {
  constructor() {
    super();
    /** @type {VoiceState} */
    this.state = "IDLE";
    this.context = createInitialContext();
  }

  /**
   * @param {string} event
   * @param {Partial<VoiceContext>} [contextPatch]
   */
  send(event, contextPatch) {
    const table = TRANSITIONS[this.state];
    const next = table && table[event];
    if (!next) return false; // no-op: not a valid transition from here

    const prevState = this.state;
    if (contextPatch) Object.assign(this.context, contextPatch);
    this.state = next;

    this.dispatchEvent(
      new CustomEvent("transition", {
        detail: { from: prevState, to: next, event, context: this.context },
      })
    );
    return true;
  }

  /** Hard reset back to IDLE with a clean context, regardless of current state. */
  hardReset() {
    const prevState = this.state;
    this.state = "IDLE";
    this.context = createInitialContext();
    this.dispatchEvent(
      new CustomEvent("transition", {
        detail: { from: prevState, to: "IDLE", event: "HARD_RESET", context: this.context },
      })
    );
  }

  is(...states) {
    return states.includes(this.state);
  }

  /** True while a mic/socket session is meaningfully "active" (worth cleaning up on unmount). */
  isActive() {
    return this.is("PERMISSION", "LISTENING", "TRANSCRIBING", "PROCESSING", "PARSING", "SEARCHING");
  }

  /** @param {(detail: {from: VoiceState, to: VoiceState, event: string, context: VoiceContext}) => void} fn */
  onTransition(fn) {
    const handler = (e) => fn(e.detail);
    this.addEventListener("transition", handler);
    return () => this.removeEventListener("transition", handler);
  }
}
