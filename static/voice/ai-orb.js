/**
 * Skairova Voice AI — the orb.
 *
 * This is a faithful port of the React Bits "Orb" component
 * (https://reactbits.dev/backgrounds/orb), which is a WebGL shader effect
 * normally rendered via the OGL library. Since Skairova's frontend is plain
 * static JS (no bundler, no npm modules in the browser), the exact same GLSL
 * fragment shader is compiled here with raw WebGL — no dependencies — so we
 * get pixel-identical output to reactbits.dev with hoverIntensity = 2.
 *
 * The public API (setState / setAmplitude / shake / start / stop / destroy)
 * is unchanged so voice-overlay.js keeps working. Two Skairova-specific
 * touches sit on top of the stock shader:
 *   • `hover` is driven by live mic amplitude, so the orb visibly reacts to
 *     your voice (in addition to real pointer hover, exactly like React Bits).
 *   • `hue` shifts per state (error → red, success → teal); the idle /
 *     listening / speaking states keep the stock hue = 0 look.
 */

const VERT = `
precision highp float;
attribute vec2 position;
attribute vec2 uv;
varying vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = vec4(position, 0.0, 1.0);
}
`;

// Fragment shader: verbatim from React Bits' Orb component.
const FRAG = `
precision highp float;

uniform float iTime;
uniform vec3 iResolution;
uniform float hue;
uniform float hover;
uniform float rot;
uniform float hoverIntensity;
varying vec2 vUv;

vec3 rgb2yiq(vec3 c) {
  float y = dot(c, vec3(0.299, 0.587, 0.114));
  float i = dot(c, vec3(0.595716, -0.274453, -0.321263));
  float q = dot(c, vec3(0.211456, -0.522591, 0.311135));
  return vec3(y, i, q);
}

vec3 yiq2rgb(vec3 c) {
  float r = c.x + 0.9563 * c.y + 0.6210 * c.z;
  float g = c.x - 0.2721 * c.y - 0.6474 * c.z;
  float b = c.x - 1.1070 * c.y + 1.7046 * c.z;
  return vec3(r, g, b);
}

vec3 adjustHue(vec3 color, float hueDeg) {
  float hueRad = hueDeg * 3.14159265 / 180.0;
  vec3 yiq = rgb2yiq(color);
  float cosA = cos(hueRad);
  float sinA = sin(hueRad);
  float i = yiq.y * cosA - yiq.z * sinA;
  float q = yiq.y * sinA + yiq.z * cosA;
  yiq.y = i;
  yiq.z = q;
  return yiq2rgb(yiq);
}

vec3 hash33(vec3 p3) {
  p3 = fract(p3 * vec3(0.1031, 0.11369, 0.13787));
  p3 += dot(p3, p3.yxz + 19.19);
  return -1.0 + 2.0 * fract(vec3(
    p3.x + p3.y,
    p3.x + p3.z,
    p3.y + p3.z
  ) * p3.zyx);
}

float snoise3(vec3 p) {
  const float K1 = 0.333333333;
  const float K2 = 0.166666667;
  vec3 i = floor(p + (p.x + p.y + p.z) * K1);
  vec3 d0 = p - (i - (i.x + i.y + i.z) * K2);
  vec3 e = step(vec3(0.0), d0 - d0.yzx);
  vec3 i1 = e * (1.0 - e.zxy);
  vec3 i2 = 1.0 - e.zxy * (1.0 - e);
  vec3 d1 = d0 - (i1 - K2);
  vec3 d2 = d0 - (i2 - K1);
  vec3 d3 = d0 - 0.5;
  vec4 h = max(0.6 - vec4(
    dot(d0, d0),
    dot(d1, d1),
    dot(d2, d2),
    dot(d3, d3)
  ), 0.0);
  vec4 n = h * h * h * h * vec4(
    dot(d0, hash33(i)),
    dot(d1, hash33(i + i1)),
    dot(d2, hash33(i + i2)),
    dot(d3, hash33(i + 1.0))
  );
  return dot(vec4(31.316), n);
}

vec4 extractAlpha(vec3 colorIn) {
  float a = max(max(colorIn.r, colorIn.g), colorIn.b);
  return vec4(colorIn.rgb / (a + 1e-5), a);
}

const vec3 baseColor1 = vec3(0.611765, 0.262745, 0.996078);
const vec3 baseColor2 = vec3(0.298039, 0.760784, 0.913725);
const vec3 baseColor3 = vec3(0.062745, 0.078431, 0.600000);
const float innerRadius = 0.6;
const float noiseScale = 0.65;

float light1(float intensity, float attenuation, float dist) {
  return intensity / (1.0 + dist * attenuation);
}
float light2(float intensity, float attenuation, float dist) {
  return intensity / (1.0 + dist * dist * attenuation);
}

vec4 draw(vec2 uv) {
  vec3 color1 = adjustHue(baseColor1, hue);
  vec3 color2 = adjustHue(baseColor2, hue);
  vec3 color3 = adjustHue(baseColor3, hue);

  float ang = atan(uv.y, uv.x);
  float len = length(uv);
  float invLen = len > 0.0 ? 1.0 / len : 0.0;

  float n0 = snoise3(vec3(uv * noiseScale, iTime * 0.5)) * 0.5 + 0.5;
  float r0 = mix(mix(innerRadius, 1.0, 0.4), mix(innerRadius, 1.0, 0.6), n0);
  float d0 = distance(uv, (r0 * invLen) * uv);
  float v0 = light1(1.0, 10.0, d0);
  v0 *= smoothstep(r0 * 1.05, r0, len);
  float cl = cos(ang + iTime * 2.0) * 0.5 + 0.5;

  float a = iTime * -1.0;
  vec2 pos = vec2(cos(a), sin(a)) * r0;
  float d = distance(uv, pos);
  float v1 = light2(1.5, 5.0, d);
  v1 *= light1(1.0, 50.0, d0);

  float v2 = smoothstep(1.0, mix(innerRadius, 1.0, n0 * 0.5), len);
  float v3 = smoothstep(innerRadius, mix(innerRadius, 1.0, 0.5), len);

  vec3 col = mix(color1, color2, cl);
  col = mix(color3, col, v0);
  col = (col + v1) * v2 * v3;
  col = clamp(col, 0.0, 1.0);

  return extractAlpha(col);
}

vec4 mainImage(vec2 fragCoord) {
  vec2 center = iResolution.xy * 0.5;
  float size = min(iResolution.x, iResolution.y);
  vec2 uv = (fragCoord - center) / size * 2.0;

  float angle = rot;
  float s = sin(angle);
  float c = cos(angle);
  uv = vec2(c * uv.x - s * uv.y, s * uv.x + c * uv.y);

  uv.x += hover * hoverIntensity * 0.1 * sin(uv.y * 10.0 + iTime);
  uv.y += hover * hoverIntensity * 0.1 * sin(uv.x * 10.0 + iTime);

  return draw(uv);
}

void main() {
  vec2 fragCoord = vUv * iResolution.xy;
  vec4 col = mainImage(fragCoord);
  gl_FragColor = vec4(col.rgb * col.a, col.a);
}
`;

// Per-state hue offset (degrees). Idle/listening/speaking keep the stock look.
const STATE_HUE = {
  idle: 0,
  listening: 0,
  speaking: 0,
  thinking: -35,
  success: 150,
  error: 210,
};

function compileShader(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader);
    gl.deleteShader(shader);
    throw new Error("Orb shader compile failed: " + log);
  }
  return shader;
}

export class AIOrb {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {{hoverIntensity?: number, rotateOnHover?: boolean}} [opts]
   */
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.hoverIntensity = opts.hoverIntensity != null ? opts.hoverIntensity : 2; // per the requested reactbits config
    this.rotateOnHover = opts.rotateOnHover !== false;
    this._reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    this._time = 0;
    this._rot = 0;
    this._hover = 0;
    this._pointerHover = 0;
    this._amp = 0;
    this._hue = 0;
    this._targetHue = 0;
    this._shakeBoost = 0;
    this._rafId = null;
    this._last = 0;
    this._ok = false;

    this._initGL();
    if (this._ok) {
      this._resize();
      this._resizeObserver = new ResizeObserver(() => this._resize());
      this._resizeObserver.observe(canvas);
      this._bindPointer();
    } else {
      this._initCanvas2DFallback();
    }
    this._loop = this._loop.bind(this);
  }

  _initGL() {
    const gl =
      this.canvas.getContext("webgl", { alpha: true, premultipliedAlpha: true, antialias: true }) ||
      this.canvas.getContext("experimental-webgl", { alpha: true, premultipliedAlpha: true });
    if (!gl) return;
    this.gl = gl;

    let program;
    try {
      const vs = compileShader(gl, gl.VERTEX_SHADER, VERT);
      const fs = compileShader(gl, gl.FRAGMENT_SHADER, FRAG);
      program = gl.createProgram();
      gl.attachShader(program, vs);
      gl.attachShader(program, fs);
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
        throw new Error("Orb program link failed: " + gl.getProgramInfoLog(program));
      }
    } catch (err) {
      console.warn("[voice] WebGL orb unavailable, using fallback:", err);
      return;
    }
    this.program = program;
    gl.useProgram(program);

    // Fullscreen triangle (OGL Triangle geometry equivalent): the uv 0..2 span
    // maps 0..1 across the visible area so vUv * iResolution == fragCoord.
    const positions = new Float32Array([-1, -1, 3, -1, -1, 3]);
    const uvs = new Float32Array([0, 0, 2, 0, 0, 2]);

    this._posBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this._posBuf);
    gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW);
    const posLoc = gl.getAttribLocation(program, "position");
    gl.enableVertexAttribArray(posLoc);
    gl.vertexAttribPointer(posLoc, 2, gl.FLOAT, false, 0, 0);

    this._uvBuf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, this._uvBuf);
    gl.bufferData(gl.ARRAY_BUFFER, uvs, gl.STATIC_DRAW);
    const uvLoc = gl.getAttribLocation(program, "uv");
    gl.enableVertexAttribArray(uvLoc);
    gl.vertexAttribPointer(uvLoc, 2, gl.FLOAT, false, 0, 0);

    this._u = {
      iTime: gl.getUniformLocation(program, "iTime"),
      iResolution: gl.getUniformLocation(program, "iResolution"),
      hue: gl.getUniformLocation(program, "hue"),
      hover: gl.getUniformLocation(program, "hover"),
      rot: gl.getUniformLocation(program, "rot"),
      hoverIntensity: gl.getUniformLocation(program, "hoverIntensity"),
    };

    gl.clearColor(0, 0, 0, 0);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    this._ok = true;
  }

  _bindPointer() {
    // Real pointer hover, exactly like the reactbits component.
    this._onPointerMove = (e) => {
      const rect = this.canvas.getBoundingClientRect();
      const x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      const y = ((e.clientY - rect.top) / rect.height) * 2 - 1;
      this._pointerHover = Math.sqrt(x * x + y * y) < 0.8 ? 1 : 0;
    };
    this._onPointerLeave = () => {
      this._pointerHover = 0;
    };
    this.canvas.addEventListener("pointermove", this._onPointerMove);
    this.canvas.addEventListener("pointerleave", this._onPointerLeave);
  }

  _resize() {
    if (!this._ok) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = this.canvas.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
    this.gl.viewport(0, 0, w, h);
    this._resW = w;
    this._resH = h;
  }

  /** @param {"idle"|"listening"|"speaking"|"thinking"|"success"|"error"} state */
  setState(state) {
    if (STATE_HUE[state] == null) return;
    this.state = state;
    this._targetHue = STATE_HUE[state];
  }

  /** Live mic amplitude (0..1) → drives the orb's hover distortion so it reacts to your voice. */
  setAmplitude(amplitude) {
    this._amp = Math.max(0, Math.min(1, amplitude));
  }

  /** Error jolt: a brief hover spike (the shader's hover distortion reads as a shudder). */
  shake() {
    if (this._reducedMotion) return;
    this._shakeBoost = 1;
  }

  start() {
    if (this._rafId) return;
    this._last = performance.now();
    this._rafId = requestAnimationFrame(this._loop);
  }

  stop() {
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
    if (this._ok && this.gl) {
      this.gl.clear(this.gl.COLOR_BUFFER_BIT);
    } else if (this._ctx2d) {
      this._ctx2d.clearRect(0, 0, this.canvas.width, this.canvas.height);
    }
  }

  destroy() {
    this.stop();
    if (this._resizeObserver) this._resizeObserver.disconnect();
    if (this._onPointerMove) this.canvas.removeEventListener("pointermove", this._onPointerMove);
    if (this._onPointerLeave) this.canvas.removeEventListener("pointerleave", this._onPointerLeave);
    if (this._ok && this.gl) {
      const ext = this.gl.getExtension("WEBGL_lose_context");
      if (ext) ext.loseContext();
    }
  }

  _loop(now) {
    const dt = Math.min(0.05, (now - this._last) / 1000);
    this._last = now;

    if (!this._reducedMotion) this._time = now * 0.001;

    // Combine real pointer hover with voice amplitude and the transient shake.
    this._shakeBoost = Math.max(0, this._shakeBoost - dt * 2.5);
    const target = Math.min(1, Math.max(this._pointerHover, this._amp * 1.4, this._shakeBoost));
    this._hover += (target - this._hover) * 0.1;
    if (this.rotateOnHover && target > 0.3 && !this._reducedMotion) {
      this._rot += dt * 0.3 * this._hover;
    }
    this._hue += (this._targetHue - this._hue) * 0.06;

    this._draw();
    this._rafId = requestAnimationFrame(this._loop);
  }

  _draw() {
    if (this._ok) {
      const gl = this.gl;
      gl.useProgram(this.program);
      gl.uniform1f(this._u.iTime, this._time);
      gl.uniform3f(this._u.iResolution, this._resW, this._resH, this._resW / Math.max(1, this._resH));
      gl.uniform1f(this._u.hue, this._hue);
      gl.uniform1f(this._u.hover, this._hover);
      gl.uniform1f(this._u.rot, this._rot);
      gl.uniform1f(this._u.hoverIntensity, this.hoverIntensity);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    } else {
      this._drawFallback();
    }
  }

  // ── Canvas-2D fallback (no WebGL): a simple gradient blob so the UI still
  //    reads, without pretending to be the shader orb. ──
  _initCanvas2DFallback() {
    this._ctx2d = this.canvas.getContext("2d");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const rect = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.round(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * dpr));
    this._ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._cssW = rect.width;
    this._cssH = rect.height;
  }

  _drawFallback() {
    const ctx = this._ctx2d;
    if (!ctx) return;
    const w = this._cssW;
    const h = this._cssH;
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2;
    const cy = h / 2;
    const r = Math.min(w, h) * 0.42 * (1 + this._hover * 0.08);
    const g = ctx.createRadialGradient(cx - r * 0.3, cy - r * 0.3, r * 0.1, cx, cy, r);
    g.addColorStop(0, "rgba(156,120,255,0.95)");
    g.addColorStop(0.5, "rgba(96,180,235,0.9)");
    g.addColorStop(1, "rgba(30,40,150,0.85)");
    ctx.fillStyle = g;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.fill();
  }
}
