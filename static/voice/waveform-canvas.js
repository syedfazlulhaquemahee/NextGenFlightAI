/**
 * Skairova Voice AI — realtime waveform.
 *
 * Draws the microphone's *actual* amplitude, not a fake looping animation.
 * Reads directly from the AnalyserNode each frame via requestAnimationFrame,
 * so it's cheap (no DOM, no SVG) and always in sync with what's really being
 * heard.
 */

const BAR_COUNT = 40;
const GRADIENT_STOPS = [
  { offset: 0, color: "rgba(79, 131, 255, 0.95)" }, // blue
  { offset: 0.5, color: "rgba(123, 97, 255, 0.95)" }, // indigo
  { offset: 1, color: "rgba(168, 97, 255, 0.95)" }, // purple
];

export class WaveformCanvas {
  /** @param {HTMLCanvasElement} canvas */
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this._analyser = null;
    this._rafId = null;
    this._bars = new Array(BAR_COUNT).fill(0.04);
    this._collapseFactor = 1; // 1 = full amplitude, animates to 0 on collapse()
    this._reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this._resize();
    this._resizeObserver = new ResizeObserver(() => this._resize());
    this._resizeObserver.observe(canvas);
  }

  _resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.round(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * dpr));
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._cssWidth = rect.width;
    this._cssHeight = rect.height;
  }

  /** @param {AnalyserNode} analyser */
  start(analyser) {
    this._analyser = analyser;
    this._collapseFactor = 1;
    this._freqData = new Uint8Array(analyser.frequencyBinCount);
    if (this._rafId) return;
    const loop = () => {
      this._draw();
      this._rafId = requestAnimationFrame(loop);
    };
    this._rafId = requestAnimationFrame(loop);
  }

  /** Animates the waveform down to a flat line, then stops the render loop. Used entering PROCESSING. */
  collapse(onDone) {
    const start = performance.now();
    const duration = this._reducedMotion ? 120 : 420;
    const startFactor = this._collapseFactor;
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      this._collapseFactor = startFactor * (1 - eased);
      if (t < 1) {
        requestAnimationFrame(step);
      } else {
        this.stop();
        if (onDone) onDone();
      }
    };
    requestAnimationFrame(step);
  }

  stop() {
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
    this._analyser = null;
    this._clear();
  }

  destroy() {
    this.stop();
    this._resizeObserver.disconnect();
  }

  _clear() {
    this.ctx.clearRect(0, 0, this._cssWidth, this._cssHeight);
  }

  _draw() {
    this._clear();
    if (!this._analyser) return;

    this._analyser.getByteFrequencyData(this._freqData);
    const bins = this._freqData.length;
    const samplesPerBar = Math.max(1, Math.floor(bins / BAR_COUNT));

    for (let i = 0; i < BAR_COUNT; i++) {
      let sum = 0;
      const start = i * samplesPerBar;
      for (let j = 0; j < samplesPerBar; j++) sum += this._freqData[start + j] || 0;
      const avg = sum / samplesPerBar / 255; // 0..1
      const target = 0.04 + avg * 0.96 * this._collapseFactor;
      // Smooth toward the target so bars don't jitter frame to frame.
      this._bars[i] += (target - this._bars[i]) * (this._reducedMotion ? 0.25 : 0.45);
    }

    const w = this._cssWidth;
    const h = this._cssHeight;
    const gap = w / BAR_COUNT;
    const barWidth = Math.max(2, gap * 0.42);

    const gradient = this.ctx.createLinearGradient(0, 0, w, 0);
    GRADIENT_STOPS.forEach((stop) => gradient.addColorStop(stop.offset, stop.color));
    this.ctx.fillStyle = gradient;
    this.ctx.beginPath();

    for (let i = 0; i < BAR_COUNT; i++) {
      const barHeight = Math.max(2, this._bars[i] * h);
      const x = i * gap + (gap - barWidth) / 2;
      const y = (h - barHeight) / 2;
      const radius = Math.min(barWidth / 2, 3);
      this._roundedRect(x, y, barWidth, barHeight, radius);
    }
    this.ctx.fill();
  }

  _roundedRect(x, y, width, height, radius) {
    const ctx = this.ctx;
    ctx.moveTo(x, y + radius);
    ctx.arcTo(x, y, x + radius, y, radius);
    ctx.lineTo(x + width - radius, y);
    ctx.arcTo(x + width, y, x + width, y + radius, radius);
    ctx.lineTo(x + width, y + height - radius);
    ctx.arcTo(x + width, y + height, x + width - radius, y + height, radius);
    ctx.lineTo(x + radius, y + height);
    ctx.arcTo(x, y + height, x, y + height - radius, radius);
    ctx.closePath();
  }
}
