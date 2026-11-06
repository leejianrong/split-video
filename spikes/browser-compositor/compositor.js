// WebGL2 compositor: the plan's layers at time t, drawn from any texImage2D source (VideoFrame, <video>).
const VS = `#version 300 es
uniform vec4 rect; uniform vec2 size; out vec2 uv;
void main(){ vec2 c=vec2(float(gl_VertexID&1), float((gl_VertexID>>1)&1)); uv=c;
  vec2 p=(rect.xy+c*rect.zw)/size; gl_Position=vec4(p.x*2.0-1.0,1.0-p.y*2.0,0,1);}`;
const FS = `#version 300 es
precision mediump float; uniform sampler2D tex; uniform float alpha; in vec2 uv; out vec4 o;
void main(){ vec4 c=texture(tex,uv); o=c*alpha; }`;

export function planAt(plan, t) {
  const at = (c) => t >= c.start && t < c.end;
  const main = [];
  plan.main.forEach((c, i) => {
    if (!at(c) || (c.transition === 'crossfade' && t < c.start + c.transition_duration)) return; // fading in: drawn by the previous clip
    main.push({ clip: c, src: c.in_point + (t - c.start), weight: 1 });
    const next = plan.main[i + 1];
    if (next && next.transition === 'crossfade' && t >= next.start) {
      main.push({ clip: next, src: next.in_point + (t - next.start), weight: (t - next.start) / next.transition_duration });
    }
  });
  const layers = plan.layers.filter(at).map((c) => ({ clip: c, src: c.in_point + (t - c.start) }));
  return { main, layers };
}

export class Compositor {
  constructor(canvas, font = 'DejaVu Sans') {
    this.w = canvas.width;
    this.h = canvas.height;
    this.font = font;
    const gl = (this.gl = canvas.getContext('webgl2', { preserveDrawingBuffer: true, premultipliedAlpha: true }));
    const sh = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = (this.prog = gl.createProgram());
    gl.attachShader(p, sh(gl.VERTEX_SHADER, VS));
    gl.attachShader(p, sh(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(p);
    gl.useProgram(p);
    this.u = Object.fromEntries(['rect', 'size', 'alpha'].map((n) => [n, gl.getUniformLocation(p, n)]));
    gl.uniform2f(this.u.size, this.w, this.h);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    gl.pixelStorei(gl.UNPACK_PREMULTIPLY_ALPHA_WEBGL, true);
    this.tex = gl.createTexture();
    this.textCanvas = new OffscreenCanvas(this.w, this.h);
  }

  quad(source, rect, alpha) {
    const { gl } = this;
    gl.bindTexture(gl.TEXTURE_2D, this.tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
    gl.uniform4f(this.u.rect, ...rect);
    gl.uniform1f(this.u.alpha, alpha);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }

  // drawtext's rules from render.build_plan: size round(h*font_size), centred on (x*w, y*h) by its line box.
  text(clip) {
    const px = Math.max(8, Math.round(this.h * clip.font_size));
    const g = this.textCanvas.getContext('2d');
    g.clearRect(0, 0, this.w, this.h);
    g.font = `${px}px "${this.font}"`;
    g.fillStyle = clip.color;
    const m = g.measureText(clip.text);
    const lineH = m.fontBoundingBoxAscent + m.fontBoundingBoxDescent;
    g.fillText(clip.text, this.w * clip.x - m.width / 2, this.h * clip.y - lineH / 2 + m.fontBoundingBoxAscent);
    this.quad(this.textCanvas, [0, 0, this.w, this.h], clip.opacity);
  }

  // frames: Map clip -> {source, width, height} for every clip planAt returned; null source means "not ready".
  draw(scene, frames) {
    const { gl, w, h } = this;
    gl.clearColor(0, 0, 0, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    for (const m of scene.main) {
      const f = frames.get(m.clip);
      if (f) this.quad(f.source, [0, 0, w, h], m.weight); // letterbox/crop not implemented: sources are 16:9
    }
    for (const l of scene.layers) {
      const c = l.clip;
      if (c.kind === 'text') {
        this.text(c);
        continue;
      }
      const f = frames.get(c);
      if (!f) continue;
      const ow = Math.max(2, Math.floor((w * c.scale) / 2) * 2);
      const oh = Math.round((ow * f.height) / f.width);
      this.quad(f.source, [w * c.x - ow / 2, h * c.y - oh / 2, ow, oh], c.opacity);
    }
  }
}
