# Decision: a browser compositor for the preview (SPL-73)

Status: accepted (2026-11-06). Card SPL-73, a spike: nothing shipped. The prototype is in
[`spikes/browser-compositor/`](../../spikes/browser-compositor/README.md) (plain JS, outside the frontend build and CI; the
README regenerates every number below). Feeds the preview targets in [ux-spec.md](../ux-spec.md#preview).

## Recorded decision

| | Decision |
|---|---|
| **A full client compositor** (WebGL plus WebCodecs draws the preview, the server only exports) | **NO-GO for now.** It is fast (a scrub in 34 ms against the server's 320 ms) but the server already meets the 500 ms target, and a second renderer has to copy every filter `render.build_plan` has, with fidelity gaps already visible in three features. |
| **A scrub draft** (the browser shows the main track's own proxy frame during a scrub, and the server's exact still replaces it) | **GO, as a later card, only if** the server still misses the 500 ms target on a real hosted setup (see "Revisit when"). Cheapest path, about 3 points. |
| Export | Unchanged: the server, `render.build_plan`. The browser never exports. |

## What was built

A 90-minute, 480x270, 15 fps H.264 main proxy (81 000 frames, 1 keyframe per 2 s, AAC) and a second 73-minute overlay
file. The timeline is the golden-frame "typical" one scaled up: two main-track clips with a 1 s cross-fade at 44:59,
a picture-in-picture clip (35% wide, 70% opaque) from 16:40 and a "Golden 123" text clip from 33:20, 5399 s in all.

- `compositor.js`: WebGL2, one textured quad per layer, premultiplied alpha, the same placement rules as `build_plan`
  (overlay width `int(w*scale)` rounded to even, centre at `x`,`y`; text size `round(h*font_size)` centred by line box).
- `scrub.js`: mp4box.js parses the index (moov) once, then a scrub is one HTTP Range for the GOP up to the target frame
  and one WebCodecs `VideoDecoder` run. No `<video>` element is involved.
- Playback: two `<video>` elements (main with sound, overlay) drawn to the canvas on every new frame
  (`requestVideoFrameCallback`).
- The server frames come from `preview_frame` (`source="original"`, 480 px, PNG) for the same timeline.

## Measurements

Machine: 16 cores, WSL2, **no GPU**: WebGL ran on SwiftShader (software). Headless Chromium 156 (Playwright's build; it
has H.264 in `<video>` and in WebCodecs). Media served from loopback, so there is no network latency in any client
number. Scrub times are 40 random places in the 90 minutes (20 for the `<video>` row and the server), each a cold seek.

| Measurement | Result |
|---|---|
| Open both files (index of 81 000 + 66 168 frames, Range requests) | 0.77 s |
| Scrub to a composited frame, WebCodecs (Range + decode + 3 layers drawn) | median **34 ms**, p90 53, max 94 (fetch 7, decode 16, GOP about 69 KB) |
| Scrub, plain `<video>` seek in the same file (to the first presented frame) | median 39 ms, p90 58, max 73 |
| Scrub, server `preview_frame` in process (the current stills, minus HTTP) | median **320 ms**, p90 417, max 460 (20 times) |
| Playback, 60 s, 2 tracks and text: frames composited | 1492 (the 2 sources deliver 15 fps each, so about 25 per s: a draw for either's new frame) |
| Dropped by the decoder (`getVideoPlaybackQuality`) | 3 of 901 (main), 0 of 908 (overlay) |
| Animation frame interval | median 16.7 ms, p90 20.4, max 49; 407 of 3583 over 20 ms, 13 over 34 ms |
| Draw cost per composite (software GL, includes the GPU wait) | median 13.2 ms, p90 18.9, max 42 |
| Sync: presented frame's time against the audio clock (`mediaTime - currentTime`) | median 30 ms late, max 83 |
| Sync: overlay against main, free-running elements | 115 ms behind after 2 s, 150 ms after 60 s: **grows about 0.6 ms per second** |
| Memory, JS heap | 85 MB just after opening, 8 MB after a GC with both indexes still held; the decoded frames are closed at once |
| Fidelity, mean absolute difference per channel (of 255) at 33:25, overlay and text on | **2.65**, 2.0% of pixels off by more than 24 |
| Fidelity at 44:59.5, the half-way cross-fade | **3.31**, 2.0% |

The golden-frame tests' own tolerance for the server's preview against its export is mean 2.5 and 0.6% of pixels
([compose.md](../compose.md)), so the client's frame is just outside the limit on the plain case and further out on text.
Where the differences are (plain case, mean over the region): the picture outside overlay and text 0.7, the
picture-in-picture box 3.5 (bilinear against ffmpeg's bicubic downscale), the text 10.0 (the browser's rasteriser and
hinting against `drawtext`). The cross-fade's 3.3 is spread over the whole frame and
was not tracked down (candidates: `xfade`'s 8-bit YUV mix against RGB blending, and a one-frame offset on the 30 fps output
grid); it is not a wrong frame.

**Colour is the largest and the only surprising gap**: with no colour tags (a typical `ffmpeg` proxy) the browser
decodes as BT.709 and ffmpeg as BT.601, a mean difference of 9.8 on saturated bars (red's green channel 24 against 0). Setting the
decoder's `colorSpace` to match removed it (0.7 outside overlay and text). The compositor must therefore read the
file's tags and the engine's matrix rule, a second place for a rule that `build_plan` holds.

## What could not be measured, and why

- **A real GPU.** No GPU here, so the draw cost and animation-frame jank are software-rendering numbers and pessimistic;
  `texImage2D` of a video frame on a GPU is normally well under a millisecond. Nothing is quoted for it.
- **Network latency.** Loopback only. A hosted editor adds a round trip to every Range request (the scrub's fetch is one
  request, so it adds one RTT to 34 ms) and to the server's still.
- **Other browsers**, Safari's WebCodecs and Firefox included; **HEVC, AV1, 10-bit, VFR and B-frame sources** (the
  proxies are ours and H.264 8-bit, GOP 30, no B-frames, so those are the cases that matter and were not run).
  The index code handles B-frame order but it is untested.
- **Audio mixing.** Gain, mute, several audio tracks and the export's loudness rules would be Web Audio; only the one
  main track's own sound was heard through `<video>`.
- **Captions, colour adjustment (`eq`), crop, reframe, canvas fit (letterbox or crop), 9:16 and 1:1, the safe zone**:
  not drawn. Each is a filter in `render.py` that would need a shader or canvas copy and a golden-frame test.
- **A/V sync by ear or by a clap track**: the numbers are the clock offsets above, not an end-to-end loopback test.
- **Memory of the whole tab** (GPU memory, decoder buffers): only the JS heap was read.

## Why NO-GO on the full compositor

1. **The server already meets the target it was meant to fix.** 320 ms median and 460 ms worst, against "a still within
   500 ms" ([ux-spec.md](../ux-spec.md#preview)), on a project cut to one instant, with no cache and no proxy needed.
   A busy machine or a hosted round trip eats the 40 ms of headroom, which is the real reason to keep this option.
2. **Two renderers will drift.** Today `render.build_plan` writes the graph for both the export and the preview frame, and the
   golden tests prove the two agree. A browser compositor is a second implementation of overlay placement, scaling, text,
   cross-fade, colour matrix and every filter added later, and the browser's rasteriser cannot match `drawtext` pixel for
   pixel. The spike's own numbers show it: three features, three different gaps, one of them (colour) invisible until measured.
   Keeping them in step needs a browser golden harness on top of the existing one, run in CI with a real browser.
3. **The playback it would replace works.** The server's 8 s chunks start in under 2 s and a draft main track plays at once
   ([frontend.md](../frontend.md#playback)). The client path's real win, instant play, is also what a scrub draft gives for
   the main track alone.
4. **The pieces that are hard are not the ones prototyped.** The spike drew three layers from clean proxies. Audio, every
   filter, captions and non-H.264 sources are the other 80% of the work.

## The cheapest viable path (if the 500 ms target slips)

A **scrub draft** with no new renderer. While the playhead is dragged, show the main track's proxy frame from a plain `<video>`
(seek in 39 ms, measured; `playback.svelte.ts` already has a draft `<video>` for the main file), under the existing "draft"
label, and let the server still replace it when it arrives. Overlays and text may be missing during the drag, never wrong
once released. No WebCodecs, no WebGL, no export change, and the exact frame is still the server's.

| Step | Points |
|---|---|
| Scrub draft: main track `<video>` seek on drag, still replaces it, tests with the fake slot | 3 |
| *(Not recommended now)* WebCodecs scrub with overlay and text drawn client side, golden harness in a browser | 8 |
| *(Not recommended now)* Full compositor: playback, audio mix, all filters, captions, 9:16, browser CI, drift guard | 20 or more |

## Revisit when

- the server's still is over 500 ms at p90 on the hosted setup (round trip plus a shared render worker), or
- scrub stills must be visibly interactive at 30 fps while dragging (the server gives about 3 per second), or
- real GPU numbers and a second browser are in hand and the filter list has stopped growing.

If the full compositor is ever built, `render.build_plan` must stay the single source: emit a plan the browser reads
(placement, times, colour matrix, text) instead of the browser re-deriving it from clips, as `golden.py` does here for the
fields it needs, and fail CI when a browser frame leaves the golden tolerance.

## Risks

- **Drift between renderers** (above): the main one. The preview would stop being what the export produces.
- **Free-running media elements drift** (0.6 ms per second here, so 2 s by the end of a long take): one must be the clock and
  the others corrected, which the chunked server preview avoids by baking the mix.
- **Software-GL numbers** may hide real GPU behaviour in either direction, and low-end laptops are the stated audience.
- **Index memory and open time** for very long recordings: 0.8 s and under 8 MB for two 90-minute files here; a 3-hour
  source at 30 fps would be several times that.
- **Codec coverage**: WebCodecs decodes only what the browser does, so HEVC phone footage would need server proxies
  anyway (as it does now).
