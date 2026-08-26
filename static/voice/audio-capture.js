/**
 * Skairova Voice AI — microphone capture.
 *
 * Owns everything between "user clicked the mic" and "we have a stream of
 * 16kHz PCM16 frames plus a live AnalyserNode for the waveform". Nothing in
 * here knows about Deepgram, the state machine, or the DOM — it's a plain
 * capture primitive so it's easy to reason about and to clean up.
 */

export class MicrophonePermissionError extends Error {
  /** @param {"denied"|"not_found"|"insecure_context"|"unsupported"|"unknown"} kind */
  constructor(kind, message) {
    super(message || kind);
    this.name = "MicrophonePermissionError";
    this.kind = kind;
  }
}

async function classifyGetUserMediaError(err) {
  const name = (err && err.name) || "";
  if (name === "NotAllowedError" || name === "PermissionDeniedError") {
    // `NotAllowedError` is also used for a restrictive Permissions-Policy or
    // an OS-level privacy block. Do not tell someone to enable the site
    // toggle when the browser already reports it as allowed.
    const policy = document.permissionsPolicy || document.featurePolicy;
    if (policy && typeof policy.allowsFeature === "function" && !policy.allowsFeature("microphone")) {
      return new MicrophonePermissionError("unknown", "Voice is blocked by this page's microphone policy. Reload after the site update, then retry.");
    }
    try {
      const permission = await navigator.permissions?.query?.({ name: "microphone" });
      if (permission?.state === "granted") {
        return new MicrophonePermissionError("unknown", "Microphone permission is allowed, but this browser or device could not start it. Check your device privacy settings or close another app using the mic.");
      }
    } catch (_ignored) {
      // Not every browser exposes microphone state through Permissions API.
    }
    return new MicrophonePermissionError("denied", "Microphone access was denied.");
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return new MicrophonePermissionError("not_found", "No microphone was found on this device.");
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return new MicrophonePermissionError("unknown", "The microphone is already in use by another app.");
  }
  return new MicrophonePermissionError("unknown", (err && err.message) || "Could not access the microphone.");
}

const WORKLET_URL = new URL("./pcm-worklet-processor.js", import.meta.url);
const TARGET_SAMPLE_RATE = 16000;

export class AudioCapture {
  constructor() {
    /** @type {MediaStream|null} */
    this._stream = null;
    /** @type {AudioContext|null} */
    this._audioContext = null;
    /** @type {AudioWorkletNode|ScriptProcessorNode|null} */
    this._processorNode = null;
    /** @type {AnalyserNode|null} */
    this._analyser = null;
    this._onFrame = null;
    this._usingWorklet = false;
  }

  static isSupported() {
    return !!(
      window.isSecureContext &&
      navigator.mediaDevices &&
      navigator.mediaDevices.getUserMedia &&
      (window.AudioContext || window.webkitAudioContext)
    );
  }

  /** @returns {AnalyserNode|null} live amplitude/frequency data for the waveform renderer. */
  getAnalyser() {
    return this._analyser;
  }

  /**
   * Requests the microphone and starts streaming 16kHz PCM16 frames.
   * @param {(frame: ArrayBuffer) => void} onFrame called on the main thread with each ~100ms PCM16 chunk.
   * @throws {MicrophonePermissionError}
   */
  async start(onFrame) {
    if (!AudioCapture.isSupported()) {
      throw new MicrophonePermissionError(
        window.isSecureContext ? "unsupported" : "insecure_context",
        window.isSecureContext
          ? "This browser doesn't support microphone capture."
          : "Voice search requires a secure (https) connection."
      );
    }
    this._onFrame = onFrame;

    try {
      this._stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
        video: false,
      });
    } catch (err) {
      throw await classifyGetUserMediaError(err);
    }

    const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
    this._audioContext = new AudioContextCtor();
    if (this._audioContext.state === "suspended") {
      await this._audioContext.resume().catch(() => {});
    }

    const source = this._audioContext.createMediaStreamSource(this._stream);

    this._analyser = this._audioContext.createAnalyser();
    this._analyser.fftSize = 1024;
    this._analyser.smoothingTimeConstant = 0.75;
    source.connect(this._analyser);

    await this._attachProcessor(source);

    return this._stream;
  }

  async _attachProcessor(source) {
    if (this._audioContext.audioWorklet) {
      try {
        await this._audioContext.audioWorklet.addModule(WORKLET_URL);
        const node = new AudioWorkletNode(this._audioContext, "pcm-downsample-processor", {
          processorOptions: { targetSampleRate: TARGET_SAMPLE_RATE },
        });
        node.port.onmessage = (event) => {
          if (this._onFrame) this._onFrame(event.data);
        };
        source.connect(node);
        // AudioWorkletNode must be connected to a destination-reachable graph
        // in some browsers to keep processing; route to a silent gain node
        // instead of speakers to avoid feedback.
        const silence = this._audioContext.createGain();
        silence.gain.value = 0;
        node.connect(silence);
        silence.connect(this._audioContext.destination);
        this._processorNode = node;
        this._usingWorklet = true;
        return;
      } catch (err) {
        // Fall through to ScriptProcessor fallback below.
        console.warn("[voice] AudioWorklet unavailable, falling back to ScriptProcessor:", err);
      }
    }
    this._attachScriptProcessorFallback(source);
  }

  /** Legacy fallback for browsers without AudioWorklet support. */
  _attachScriptProcessorFallback(source) {
    const bufferSize = 4096;
    const node = this._audioContext.createScriptProcessor(bufferSize, 1, 1);
    const ratio = this._audioContext.sampleRate / TARGET_SAMPLE_RATE;

    node.onaudioprocess = (event) => {
      const input = event.inputBuffer.getChannelData(0);
      const outLength = Math.round(input.length / ratio);
      const pcm16 = new Int16Array(outLength);
      for (let i = 0; i < outLength; i++) {
        const srcIndex = Math.floor(i * ratio);
        const s = Math.max(-1, Math.min(1, input[srcIndex] || 0));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      if (this._onFrame) this._onFrame(pcm16.buffer);
    };

    source.connect(node);
    const silence = this._audioContext.createGain();
    silence.gain.value = 0;
    node.connect(silence);
    silence.connect(this._audioContext.destination);
    this._processorNode = node;
    this._usingWorklet = false;
  }

  /** Instantaneous RMS amplitude in [0, 1] — used for the client-side silence fallback and orb reactivity. */
  getAmplitude() {
    if (!this._analyser) return 0;
    const data = new Uint8Array(this._analyser.fftSize);
    this._analyser.getByteTimeDomainData(data);
    let sumSquares = 0;
    for (let i = 0; i < data.length; i++) {
      const normalized = (data[i] - 128) / 128;
      sumSquares += normalized * normalized;
    }
    return Math.sqrt(sumSquares / data.length);
  }

  /** Releases the microphone and tears down the audio graph. Safe to call multiple times. */
  stop() {
    if (this._processorNode) {
      try {
        this._processorNode.disconnect();
        if (this._processorNode.port) this._processorNode.port.onmessage = null;
      } catch (e) {}
      this._processorNode = null;
    }
    if (this._analyser) {
      try {
        this._analyser.disconnect();
      } catch (e) {}
      this._analyser = null;
    }
    if (this._stream) {
      this._stream.getTracks().forEach((track) => track.stop());
      this._stream = null;
    }
    if (this._audioContext) {
      const ctx = this._audioContext;
      this._audioContext = null;
      if (ctx.state !== "closed") ctx.close().catch(() => {});
    }
    this._onFrame = null;
  }
}
