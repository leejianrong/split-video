// node run.mjs MEDIA_DIR OUT_DIR [playbackSeconds]: serves the page and the media, drives headless Chromium, prints results.
import { createServer } from 'node:http';
import { createReadStream, statSync, writeFileSync, readFileSync } from 'node:fs';
import { extname, join, normalize } from 'node:path';
import { chromium } from 'playwright';

const [media, out, secs = '20'] = process.argv.slice(2);
const here = new URL('.', import.meta.url).pathname;
const types = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.json': 'application/json', '.mp4': 'video/mp4', '.png': 'image/png' };

const server = createServer((req, res) => {
  const url = decodeURIComponent(req.url.split('?')[0]);
  const root = url.startsWith('/media/') ? media : url.startsWith('/out/') ? out : here;
  const rel = normalize(url.replace(/^\/(media|out)\//, '/')).replace(/^(\.\.[/\\])+/, '');
  const file = join(root, rel === '/' ? 'index.html' : rel);
  let st;
  try { st = statSync(file); } catch { res.writeHead(404).end(); return; }
  const m = /bytes=(\d+)-(\d*)/.exec(req.headers.range ?? '');
  const head = { 'Content-Type': types[extname(file)] ?? 'application/octet-stream', 'Accept-Ranges': 'bytes' };
  if (m) {
    const a = +m[1], b = Math.min(m[2] ? +m[2] : st.size - 1, st.size - 1);
    res.writeHead(206, { ...head, 'Content-Range': `bytes ${a}-${b}/${st.size}`, 'Content-Length': b - a + 1 });
    createReadStream(file, { start: a, end: b }).pipe(res);
  } else {
    res.writeHead(200, { ...head, 'Content-Length': st.size });
    createReadStream(file).pipe(res);
  }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${server.address().port}`;

const pct = (a, p) => [...a].sort((x, y) => x - y)[Math.min(a.length - 1, Math.floor((a.length * p) / 100))];
const sum = (a) => ({ n: a.length, p50: pct(a, 50), p90: pct(a, 90), max: Math.max(...a) });
const r = (o) => JSON.parse(JSON.stringify(o, (k, v) => (typeof v === 'number' ? Math.round(v * 100) / 100 : v)));

const browser = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
try {
  const page = await browser.newPage();
  page.on('pageerror', (e) => console.log('PAGEERROR', e.message));
  page.on('console', (m) => m.type() === 'error' && console.log('CONSOLE', m.text()));
  await page.goto(base + '/index.html');
  const info = await page.evaluate(() => ({
    version: navigator.userAgent, h264: typeof VideoDecoder !== 'undefined' ? null : 'no VideoDecoder',
    renderer: (() => { const g = document.createElement('canvas').getContext('webgl2'); const e = g.getExtension('WEBGL_debug_renderer_info'); return g.getParameter(e.UNMASKED_RENDERER_WEBGL); })(),
  }));
  const cdp = await page.context().newCDPSession(page);
  const heap = async () => (await cdp.send('Performance.getMetrics')).metrics.find((m) => m.name === 'JSHeapUsedSize').value / 1e6;
  await cdp.send('Performance.enable');
  const h0 = await heap();
  const setup = await page.evaluate(() => window.spike.setup());
  const h1 = await heap();
  const result = { info, setup, heapMB: { before: h0, afterOpen: h1 } };

  const rnd = (() => { let s = 7; return () => ((s = (s * 16807) % 2147483647) / 2147483647); })();
  const times = Array.from({ length: 40 }, () => 1 + rnd() * 5390);
  const sc = await page.evaluate((t) => window.spike.scrubTimes(t), times);
  result.scrubWebCodecs = { total: sum(sc.map((x) => x.ms)), fetch: sum(sc.map((x) => x.fetchMs)), decode: sum(sc.map((x) => x.decodeMs)), kbPerFrame: sum(sc.map((x) => x.kb)) };
  result.heapMB.afterScrub = await heap();
  const vs = await page.evaluate((t) => window.spike.videoSeeks(t), times.slice(0, 20));
  result.scrubVideoElement = sum(vs);

  const fid = {};
  for (const [name, t] of [['plain', 2005], ['crossfade', 2699.5]]) {
    const dd = await page.evaluate(([t, n]) => window.spike.diff(t, `/out/golden-${n}.png`), [t, name]);
    writeFileSync(join(out, `client-${name}.png`), Buffer.from(dd.png.split(',')[1], 'base64'));
    delete dd.png;
    fid[name] = dd;
  }
  result.fidelity = fid;

  const pb = await page.evaluate(([s]) => window.spike.playback(s, 2000), [+secs]);
  result.playback = {
    seconds: pb.seconds, composites: pb.composites, mainAdvanced: pb.mainAdvanced, dropped: pb.dropped, decoded: pb.decoded,
    raf: sum(pb.rafMs), rafOver20ms: pb.rafMs.filter((x) => x > 20).length, rafOver34ms: pb.rafMs.filter((x) => x > 34).length,
    draw: sum(pb.drawMs), driftFirst100Ms: pct(pb.driftMs.slice(0, 100), 50), driftLast100Ms: pct(pb.driftMs.slice(-100), 50), driftRangeLast30sMs: sum(pb.driftMs.slice(-1800)),
    mediaTimeMinusCurrentMs: sum(pb.mediaTimeMinusCurrentMs.map(Math.abs)),
  };
  result.heapMB.afterPlayback = await heap();
  console.log(JSON.stringify(r(result), null, 1));
} finally {
  await browser.close();
  server.close();
}
