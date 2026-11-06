// Random access into a faststart H.264 mp4 over HTTP Range: parse moov with mp4box, fetch one GOP, decode with WebCodecs.
import { createFile, DataStream } from './node_modules/mp4box/dist/mp4box.all.mjs';

// The files carry no colour tags, so ffmpeg (the server) reads them as BT.601; the browser would guess BT.709.
const COLOR = { primaries: 'smpte170m', transfer: 'smpte170m', matrix: 'smpte170m', fullRange: false };

export class Scrub {
  static async open(url, head = 4 << 20) {
    const s = new Scrub(url);
    const file = createFile();
    let off = 0;
    const ready = new Promise((res, rej) => {
      file.onReady = res;
      file.onError = rej;
    });
    let done = false;
    ready.then(() => (done = true));
    while (!done) {
      const buf = await (await fetch(url, { headers: { Range: `bytes=${off}-${off + head - 1}` } })).arrayBuffer();
      buf.fileStart = off;
      off = file.appendBuffer(buf);
      await Promise.race([ready, new Promise((r) => setTimeout(r, 0))]);
      if (!buf.byteLength) throw new Error('no moov');
    }
    const info = await ready;
    const v = info.videoTracks[0];
    const trak = file.getTrackById(v.id);
    const entry = trak.mdia.minf.stbl.stsd.entries[0];
    const box = entry.avcC;
    const ds = new DataStream(undefined, 0, DataStream.BIG_ENDIAN);
    box.write(ds);
    s.config = { codec: v.codec, codedWidth: v.video.width, codedHeight: v.video.height, description: new Uint8Array(ds.buffer, 8), colorSpace: COLOR };
    s.samples = trak.samples.map((x) => ({ off: x.offset, size: x.size, cts: x.cts, dur: x.duration, sync: x.is_sync }));
    s.scale = v.timescale;
    s.width = v.video.width;
    s.height = v.video.height;
    s.order = s.samples.map((_, i) => i).sort((a, b) => s.samples[a].cts - s.samples[b].cts); // presentation order
    return s;
  }

  constructor(url) {
    this.url = url;
  }

  // Decode the frame shown at `t` seconds: the last keyframe at or before it up to it (one range request).
  async frameAt(t) {
    const { samples, scale, order } = this;
    const cts = Math.round(t * scale);
    let lo = 0;
    let hi = order.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (samples[order[mid]].cts <= cts) lo = mid;
      else hi = mid - 1;
    }
    const target = order[lo];
    let first = target;
    while (first > 0 && !samples[first].sync) first--;
    // Decode order is storage order: samples first..last cover the target and anything it references.
    let last = target;
    for (let i = target + 1; i < samples.length && samples[i].cts < samples[target].cts; i++) last = i; // B-frame reorder
    const start = samples[first].off;
    const end = samples[last].off + samples[last].size;
    const t0 = performance.now();
    const buf = await (await fetch(this.url, { headers: { Range: `bytes=${start}-${end - 1}` } })).arrayBuffer();
    const t1 = performance.now();
    let out = null;
    const dec = new VideoDecoder({
      output: (f) => {
        if (f.timestamp === Math.round((samples[target].cts / scale) * 1e6)) out = f;
        else f.close();
      },
      error: (e) => {
        throw e;
      },
    });
    dec.configure(this.config);
    for (let i = first; i <= last; i++) {
      const x = samples[i];
      dec.decode(new EncodedVideoChunk({ type: x.sync ? 'key' : 'delta', timestamp: Math.round((x.cts / scale) * 1e6), data: new Uint8Array(buf, x.off - start, x.size) }));
    }
    await dec.flush();
    dec.close();
    return { frame: out, fetchMs: t1 - t0, decodeMs: performance.now() - t1, bytes: buf.byteLength, frames: last - first + 1 };
  }
}
