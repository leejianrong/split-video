import { Compositor, planAt } from './compositor.js';
import { Scrub } from './scrub.js';

const canvas = document.getElementById('c');
let plan, comp, scrubs;
const MAIN = '/media/main90.mp4';
const OVER = '/media/over90.mp4';

async function setup() {
  plan = await (await fetch('/out/plan.json')).json();
  await document.fonts.load('40px "DejaVu Sans"');
  comp = new Compositor(canvas);
  const t0 = performance.now();
  scrubs = { 'main90.mp4': await Scrub.open(MAIN), 'over90.mp4': await Scrub.open(OVER) };
  return { openMs: performance.now() - t0, samples: Object.fromEntries(Object.entries(scrubs).map(([k, s]) => [k, s.samples.length])) };
}

// Composite the plan at timeline time t from WebCodecs frames (one range request + decode per clip).
async function compose(t) {
  const scene = planAt(plan, t);
  const want = [...scene.main, ...scene.layers].filter((x) => x.clip.kind === 'media');
  const got = await Promise.all(want.map((x) => scrubs[x.clip.src].frameAt(x.src)));
  const frames = new Map();
  want.forEach((x, i) => frames.set(x.clip, { source: got[i].frame, width: scrubs[x.clip.src].width, height: scrubs[x.clip.src].height }));
  comp.draw(scene, frames);
  comp.gl.readPixels(0, 0, 1, 1, comp.gl.RGBA, comp.gl.UNSIGNED_BYTE, new Uint8Array(4)); // forces the GPU work to finish
  got.forEach((g) => g.frame.close());
  return got;
}

async function scrubTimes(times) {
  const rows = [];
  for (const t of times) {
    const t0 = performance.now();
    const got = await compose(t);
    rows.push({ t, ms: performance.now() - t0, fetchMs: Math.max(...got.map((g) => g.fetchMs)), decodeMs: Math.max(...got.map((g) => g.decodeMs)), kb: got.reduce((a, g) => a + g.bytes, 0) / 1024 });
  }
  return rows;
}

// Baseline: what a plain <video> seek costs in the same file (to 'seeked', then the first presented frame).
async function videoSeeks(times) {
  const v = document.createElement('video');
  v.muted = true;
  v.preload = 'auto';
  v.src = MAIN;
  await new Promise((r) => (v.onloadedmetadata = r));
  const rows = [];
  for (const t of times) {
    const t0 = performance.now();
    const shown = new Promise((r) => v.requestVideoFrameCallback(r));
    v.currentTime = t;
    await Promise.race([shown, new Promise((r) => setTimeout(r, 8000))]);
    rows.push(performance.now() - t0);
  }
  v.removeAttribute('src');
  v.load();
  return rows;
}

async function diff(t, goldenUrl) {
  await compose(t);
  const gl = comp.gl;
  const mine = new Uint8Array(comp.w * comp.h * 4);
  gl.readPixels(0, 0, comp.w, comp.h, gl.RGBA, gl.UNSIGNED_BYTE, mine); // bottom-up
  const bmp = await createImageBitmap(await (await fetch(goldenUrl)).blob());
  const g = new OffscreenCanvas(bmp.width, bmp.height).getContext('2d');
  g.drawImage(bmp, 0, 0);
  const gold = g.getImageData(0, 0, bmp.width, bmp.height).data;
  let sum = 0;
  let bad = 0;
  const regions = { text: [0, 0, 0], overlay: [0, 0, 0], rest: [0, 0, 0] };
  for (let y = 0; y < comp.h; y++) {
    for (let x = 0; x < comp.w; x++) {
      const a = ((comp.h - 1 - y) * comp.w + x) * 4;
      const b = (y * comp.w + x) * 4;
      let d = 0;
      for (let k = 0; k < 3; k++) d += Math.abs(mine[a + k] - gold[b + k]);
      d /= 3;
      sum += d;
      if (d > 24) bad++;
      const r = y > 100 && y < 170 && x > 80 && x < 400 ? 'text' : x > 270 && y < 130 && y > 30 ? 'overlay' : 'rest';
      regions[r][0] += d;
      regions[r][1] += d > 24 ? 1 : 0;
      regions[r][2]++;
    }
  }
  return {
    t,
    mean: sum / (comp.w * comp.h),
    share: bad / (comp.w * comp.h),
    regions: Object.fromEntries(Object.entries(regions).map(([k, v]) => [k, { mean: v[0] / v[2], share: v[1] / v[2] }])),
    png: canvas.toDataURL('image/png'),
  };
}

// Real-time playback of main (+audio) and overlay as <video> elements, drawn to the canvas on every new frame.
async function playback(seconds, startT) {
  const mk = (src, at) => {
    const v = document.createElement('video');
    v.src = src;
    v.preload = 'auto';
    v.playsInline = true;
    v.currentTime = at;
    return v;
  };
  const main = mk(MAIN, startT);
  const over = mk(OVER, startT - plan.layers[0].start);
  await Promise.all([main, over].map((v) => new Promise((r) => (v.onseeked = r))));
  const dirty = { main: false, over: false };
  const mediaTimes = [];
  const watch = (v, k) => {
    const cb = (_now, md) => {
      dirty[k] = true;
      if (k === 'main') mediaTimes.push(md.mediaTime - v.currentTime);
      v.requestVideoFrameCallback(cb);
    };
    v.requestVideoFrameCallback(cb);
  };
  watch(main, 'main');
  watch(over, 'over');
  const frames = [];
  const drift = [];
  const drawMs = [];
  let composites = 0;
  let last = performance.now();
  await Promise.all([main.play(), over.play()]);
  const t0 = performance.now();
  await new Promise((done) => {
    const tick = () => {
      const now = performance.now();
      frames.push(now - last);
      last = now;
      if (dirty.main || dirty.over) {
        const t = main.currentTime;
        const d0 = performance.now();
        const scene = planAt(plan, t);
        const fr = new Map();
        for (const x of [...scene.main, ...scene.layers]) {
          if (x.clip.kind !== 'media') continue;
          const v = x.clip.src === 'over90.mp4' ? over : main;
          fr.set(x.clip, { source: v, width: v.videoWidth, height: v.videoHeight });
        }
        comp.draw(scene, fr);
        comp.gl.readPixels(0, 0, 1, 1, comp.gl.RGBA, comp.gl.UNSIGNED_BYTE, new Uint8Array(4));
        drawMs.push(performance.now() - d0);
        composites++;
        dirty.main = dirty.over = false;
      }
      drift.push((over.currentTime + plan.layers[0].start - main.currentTime) * 1000);
      if (now - t0 < seconds * 1000) requestAnimationFrame(tick);
      else done();
    };
    requestAnimationFrame(tick);
  });
  const q = (v) => v.getVideoPlaybackQuality();
  const res = {
    seconds: (performance.now() - t0) / 1000,
    composites,
    mainAdvanced: main.currentTime - startT,
    dropped: { main: q(main).droppedVideoFrames, over: q(over).droppedVideoFrames },
    decoded: { main: q(main).totalVideoFrames, over: q(over).totalVideoFrames },
    rafMs: frames,
    drawMs,
    driftMs: drift,
    mediaTimeMinusCurrentMs: mediaTimes.map((x) => x * 1000),
  };
  main.pause();
  over.pause();
  main.removeAttribute('src');
  over.removeAttribute('src');
  main.load();
  over.load();
  return res;
}

window.spike = { setup, scrubTimes, videoSeeks, diff, playback, heap: () => performance.memory.usedJSHeapSize };
