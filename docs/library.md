# Library: catalogue a folder, proxies, thumbnails


```bash
docker run --rm -v "$PWD":/data ghcr.io/leejianrong/split-video library scan /data
docker run --rm -v "$PWD":/data ghcr.io/leejianrong/split-video library list /data --json
```

`library scan` walks the folder recursively and records every media file as an
asset (duration, size, video and audio, width, height, fps) in
`.split-video-assets.json` next to the media. A second scan probes only new or
changed files, and a 3-hour recording is identified from about 3 MiB of
samples plus one ffprobe, not read in full. Files that are not readable media
are listed as skipped with a reason. `library list` never probes. The editor
queues the same scan as a `library_scan` job (`POST /api/v1/library/scan`), and
MCP has `scan_library`.

For long recordings, make a small stand-in to scrub and analyse visually:

```bash
docker run --rm -v "$PWD":/data ghcr.io/leejianrong/split-video library scan /data --proxies
docker run --rm -v "$PWD":/data ghcr.io/leejianrong/split-video library proxies /data [--asset a_0123456789abcdef]
```

Each video asset gets a proxy (H.264, at most 640 px wide and 15 fps, a keyframe
every 2 s of video whatever the source frame rate, and the index up front, so a
browser seeks in it at once) and a thumbnail strip (one JPEG sprite with a frame
every 10 s, at most 240 tiles; each tile is within 1 s of its time, which
`thumbnails/info` states as `tolerance`), both in `.split-video-cache/` next to the video. The original is never touched;
export and render always read it, and so does audio analysis. Scans make no
proxies unless you ask (`--proxies`, `"proxies": true` on `POST
/api/v1/library/scan`, `proxies` on the MCP `scan_library`): it is minutes of
ffmpeg per hour of video. The editor queues one with `POST
/api/v1/assets/{id}/proxy` (a `proxy` job with progress and cancel), serves it
at `GET /api/v1/assets/{id}/proxy` (Range requests), and the strip at
`GET /api/v1/assets/{id}/thumbnails` with its layout at `.../thumbnails/info`.
MCP has `prepare_asset`, and `list_assets` says which assets have a proxy.

Two things differ by surface. `library scan --proxies` and the MCP `scan_library`
run inline: they make the proxies before returning and report the finished
per-asset results under `proxies`. The API's scan is a job that queues one
`proxy` job per new video and then finishes without waiting for them: its result
has `proxy_jobs` (`[{asset, job_id}]`), which you follow through `/jobs/{id}` or
`/events`.

Audio-only assets and the picture tools follow one rule. An audio-only asset has
no proxy, no scene signals and no on-screen text, and nothing can make them. For
`proxy`, `scenes` and `ocr` the CLI and MCP return a normal result with
`skipped: "audio-only asset"`, while the API treats it as an impossible request:
`POST` answers 422 and the `GET`s answer 404 saying the asset is audio-only (not
"POST to make it").
