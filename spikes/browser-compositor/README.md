# Browser compositor spike (SPL-73)

Throwaway prototype for [the decision](../../docs/decisions/browser-compositor.md). Not part of the app, the frontend build or CI. Never commit the media.

```sh
M=/some/scratch/dir   # 90-minute, 480x270, 15 fps proxies (H.264 + AAC), faststart, GOP 30
ffmpeg -nostdin -f lavfi -i testsrc2=s=480x270:r=15:d=5400 -f lavfi -i sine=f=440:r=44100:d=5400 \
  -c:v libx264 -preset ultrafast -g 30 -pix_fmt yuv420p -b:v 300k -c:a aac -b:a 64k -movflags +faststart -shortest $M/main90.mp4
ffmpeg -nostdin -f lavfi -i testsrc=s=320x180:r=15:d=4400 -vf hue=h=120 \
  -c:v libx264 -preset ultrafast -g 30 -pix_fmt yuv420p -b:v 200k -movflags +faststart $M/over90.mp4
uv run python spikes/browser-compositor/golden.py $M $M      # plan.json, golden-*.png, server-timing.json
cd spikes/browser-compositor && npm install && npx playwright install chromium
timeout 300 node run.mjs $M $M 60                            # JSON of every number in the decision
```

- `compositor.js`: WebGL2 draw of the plan (main track with cross-fade, picture-in-picture clip, text clip).
- `scrub.js`: mp4box.js reads the index, one HTTP Range per GOP, WebCodecs decodes to the target frame.
- `spike.js`, `index.html`: the page's measurements (scrub, `<video>` seek baseline, pixel diff, playback).
- `run.mjs`: static server with Range support plus headless Chromium; `golden.py`: the server's frames.
