/**
 * AudioWorkletProcessor that downsamples the mic's native sample rate
 * (usually 44.1/48kHz) to the 16kHz mono 16-bit PCM Deepgram expects, and
 * posts each ~100ms chunk back to the main thread as a transferable
 * ArrayBuffer. Runs on the audio rendering thread — must stay allocation-light
 * per call to avoid glitching the graph.
 */
class PCMDownsampleProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this._targetRate = (options && options.processorOptions && options.processorOptions.targetSampleRate) || 16000;
    this._ratio = sampleRate / this._targetRate; // `sampleRate` is a global in the worklet scope
    this._buffer = [];
    this._bufferedSamples = 0;
    // ~100ms of audio at the target rate per posted chunk — small enough for
    // low latency, large enough not to spam postMessage.
    this._chunkSamples = Math.round(this._targetRate * 0.1);
  }

  /**
   * Naive linear-interpolation downsampler. Good enough for speech-to-text
   * (Deepgram itself re-processes internally); we're optimizing for low CPU
   * cost on the audio thread over pristine fidelity.
   */
  _downsample(input) {
    if (this._ratio === 1) return input;
    const outLength = Math.round(input.length / this._ratio);
    const out = new Float32Array(outLength);
    for (let i = 0; i < outLength; i++) {
      const srcIndex = i * this._ratio;
      const lo = Math.floor(srcIndex);
      const hi = Math.min(lo + 1, input.length - 1);
      const frac = srcIndex - lo;
      out[i] = input[lo] + (input[hi] - input[lo]) * frac;
    }
    return out;
  }

  _floatToPCM16(float32) {
    const pcm16 = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      const s = Math.max(-1, Math.min(1, float32[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return pcm16;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) return true;

    const mono = input[0]; // mic capture is requested as mono; take channel 0
    const downsampled = this._downsample(mono);

    for (let i = 0; i < downsampled.length; i++) {
      this._buffer.push(downsampled[i]);
    }
    this._bufferedSamples += downsampled.length;

    while (this._bufferedSamples >= this._chunkSamples) {
      const chunk = this._buffer.splice(0, this._chunkSamples);
      this._bufferedSamples -= this._chunkSamples;
      const pcm16 = this._floatToPCM16(Float32Array.from(chunk));
      this.port.postMessage(pcm16.buffer, [pcm16.buffer]);
    }

    return true; // keep the processor alive
  }
}

registerProcessor("pcm-downsample-processor", PCMDownsampleProcessor);
