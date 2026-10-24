# The media report: every signal of a recording in one call

`media_report(asset)` answers "what is in this recording?" without five tool calls: duration, silence gaps, the
loudness curve, music / speech / applause regions, scene changes, screen activity, a transcript summary and on-screen
text, as compact JSON in time buckets. Its size is bounded whatever the length of the recording (see Size). Per-bucket
values come from the same `TimeIndex` as [`what_is_happening`](time-index.md), so the two always agree. The same JSON
comes from every surface:

| Surface | Call |
| --- | --- |
| HTTP | `GET /api/v1/assets/{id}/report?bucket_seconds=&include=` (read-only) and `POST /api/v1/assets/{id}/report/prepare` (a job of kind `analysis`) |
| CLI | `split-video report <target> [--bucket-seconds S] [--include a,b] [--prepare] [--json]` (a table by default; `--json` is the document as compact JSON) |
| MCP | `media_report(asset, bucket_seconds, include, prepare)` |
| Python | `media_report.media_report(media, asset_id, bucket_seconds=, buckets=, include=, max_bytes=, prepare=)` |

The engine is `src/split_video/media_report.py`; its module docstring is the reference for the shape. Ranking moments
is not part of it: the report describes, it does not choose.

## Read-only by default, `prepare` for the cheap signals

A report computes nothing: a signal that was never made is `null` and `signals.missing` says which command makes it.
The loudness curve used to exist only after the editor served the waveform; `--prepare` (MCP `prepare=true`, HTTP
`POST .../report/prepare`) now makes the missing cheap signals headlessly:

| Signal | Prepared | Cost |
| --- | --- | --- |
| loudness | yes: one streamed decode (also stores the waveform peaks) | seconds for 3 hours |
| silence | yes: the scan at -35 dB, one ffmpeg pass | tens of seconds for 3 hours |
| audio classes | yes, when the YAMNet model is installed (else skipped, with the reason) | minutes for 3 hours |
| transcript, scenes, on-screen text | never: heavy, run `transcribe`, `scenes`, `ocr` yourself | |

On HTTP it is a job with progress (thousandths of the work) and real cancel; a cancelled job stores no partial signal
(each signal is saved whole when it finishes). On the CLI and MCP it runs inline.

## The document (`"version": 1`)

Keys are never added or removed within a version. Times are seconds, levels dBFS, every bucket column is one value per
bucket (bucket `k` covers `[k * seconds, min(duration, (k + 1) * seconds))`) or `null` as a whole.

| Key | Meaning |
| --- | --- |
| `asset` | `id`, `path`, `duration`, `size`, `has_audio`, `has_video`, `streams`, `width`, `height`, `fps` |
| `signals` | `available` (booleans for audio, loudness, silence, speech, scenes, ocr), `missing` (`[{signal, how}]`), `excluded` (left out by `include`) |
| `prepared` | what `prepare` made (`prepared`, `already`, `skipped`), else null |
| `limits`, `notices` | `max_bytes`; `coarsened`, `shrunk`, `dropped`, `truncated` say what was cut to fit |
| `buckets` | `seconds`, `count`, then the columns `audio_class`, `audio_share`, `loudness_mean_db`, `loudness_max_db`, `silence_fraction`, `words`, `words_per_minute`, `scene_changes`, `activity_mean`, `activity_max`, `screen_text_changed` (0/1), `transcript` (snippet), `screen_text` (first line, only where it changed) |
| `loudness` | `mean_db`, `p95_db`, `max_db`, and a coarse `curve` of at most 60 whole dB values, one per `curve_seconds` |
| `regions` | audio class regions of 10 s or more, same-class regions at most 2 s apart merged first (YAMNet splits speech on a 0.5 s pause): `items [[start, end, class]]` (the longest 60), `count`, `truncated`, `seconds_by_class` (classified seconds; an absorbed pause is not counted) |
| `silence_gaps` | silences of 2 s or more: `items [[start, end]]` (the longest 40), `count`, `truncated`, `threshold_db` |
| `scene_changes` | `items [[time, score]]` (the strongest 40, in time order), `count`, `truncated` |
| `transcript` | `words`, `language`, `words_per_minute`, `top_keywords [[word, count]]` (12, English stopwords left out; a frequency count, not a ranking of moments) |

## Loud sound is not silence: `other_sound`

YAMNet files pink noise or a synthesised tone under `silence_other`. The report (and [`what_is_happening`](time-index.md))
relabels such a stretch as **`other_sound`** when the stored loudness curve is above **-45 dBFS** (judged per 1 s value),
in `buckets.audio_class`, `regions` and the `at` audio summary; quiet stretches keep `silence_other`. The class list is
`music`, `singing`, `speech`, `applause_crowd`, `laughter`, `silence_other`, `other_sound`. Nothing is lost: an
`other_sound` item in `regions` has a fourth element, `"silence_other"` (the raw class), and the time index adds
`raw_class`. This is applied on read; the stored classification is unchanged, and without a loudness curve nothing is
relabelled. No key changed, so the report stays at `"version": 1`.

## Size

The default bucket is `max(10, ceil(duration / 180))` seconds: 30 s for 90 minutes, 60 s for 3
hours, about 180 buckets. `bucket_seconds` may be 5 to 600 and may not give more than 720 buckets. The document is
bounded by `max_bytes` (**40,000 bytes of compact JSON** by default, at least 4,096 in the engine), and degrades in
this order, only as far as needed, telling you in `notices`: shorter snippets (transcript/screen text 40/60, then
20/30, then 10/15 characters), no snippets, coarser buckets (doubled up to 600 s), shorter lists, no buckets, and
last of all a bare document. Measured (tests/unit/test_media_report.py): a 3 hour recording with every signal
(27,000 words, 22,500 classification frames, 540 cuts, a text change every 30 s) is **23,944 bytes** with 60 s
buckets, nothing cut but the region and cut lists. HTTP sends compact JSON; the CLI's `--json` prints the same
bytes (not indented, which would be several times larger).

## Example

`GET /api/v1/assets/{id}/report?bucket_seconds=30` on a 5 minute recording with all signals (2191 bytes as compact JSON; pretty-printed here):

```json
{
 "version": 1,
 "asset": {
  "id": "a_e37c5eab78c34f0f",
  "path": "talk.mp4",
  "duration": 300.0,
  "size": 1,
  "has_audio": true,
  "has_video": true,
  "streams": ["video", "audio"],
  "width": 1280,
  "height": 720,
  "fps": 5.0
 },
 "signals": {
  "available": {
   "audio": true,
   "loudness": true,
   "silence": true,
   "speech": true,
   "scenes": true,
   "ocr": true
  },
  "missing": [],
  "excluded": []
 },
 "prepared": null,
 "limits": {
  "max_bytes": 40000
 },
 "notices": {
  "coarsened": null,
  "shrunk": null,
  "dropped": [],
  "truncated": []
 },
 "buckets": {
  "seconds": 30.0,
  "count": 10,
  "audio_class": ["music", "music", "music", "speech", "speech", "speech", "speech", "music", "music", "music"],
  "audio_share": [1.0, 1.0, 1.0, 0.8, 1.0, 1.0, 0.67, 0.99, 1.0, 1.0],
  "loudness_mean_db": [-40.0, -40.0, -40.0, -36.4, -40.0, -40.0, -38.8, -40.0, -40.0, -40.0],
  "loudness_max_db": [-40.0, -40.0, -40.0, -22.0, -40.0, -40.0, -3.0, -40.0, -40.0, -40.0],
  "silence_fraction": [0.0, 0.0, 0.0, 0.05, 0.0, 0.0, 0.03, 0.0, 0.0, 0.0],
  "words": [0, 0, 0, 10, 0, 0, 0, 0, 0, 0],
  "words_per_minute": [0, 0, 0, 20, 0, 0, 0, 0, 0, 0],
  "scene_changes": [1, 0, 0, 0, 1, 1, 0, 0, 0, 0],
  "activity_mean": [0.13, 0.1, 0.1, 0.1, 0.1, 0.13, 0.1, 0.1, 0.1, 0.1],
  "activity_max": [0.9, 0.1, 0.1, 0.1, 0.1, 0.9, 0.1, 0.1, 0.1, 0.1],
  "screen_text_changed": [1, 0, 0, 0, 1, 0, 0, 0, 0, 0],
  "transcript": ["", "", "", "welcome everybody to the live stream ton", "", "", "", "", "", ""],
  "screen_text": ["intro slide", null, null, null, "def main():", null, null, null, null, null]
 },
 "loudness": {
  "mean_db": -39.5,
  "p95_db": -40.0,
  "max_db": -3.0,
  "curve_seconds": 5.0,
  "curve": [-40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -33, -26, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -33, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40, -40]
 },
 "regions": {
  "min_seconds": 10.0,
  "seconds_by_class": {
   "applause_crowd": 10.1,
   "music": 185.8,
   "speech": 104.2
  },
  "count": 4,
  "truncated": 0,
  "items": [
   [0.0, 96.0, "music"],
   [96.0, 200.2, "speech"],
   [200.2, 210.2, "applause_crowd"],
   [210.2, 300.0, "music"]
  ]
 },
 "silence_gaps": {
  "min_seconds": 2.0,
  "threshold_db": -35.0,
  "count": 0,
  "truncated": 0,
  "items": []
 },
 "scene_changes": {
  "count": 2,
  "truncated": 0,
  "items": [
   [20.0, 0.4],
   [150.0, 0.62]
  ]
 },
 "transcript": {
  "words": 10,
  "language": "en",
  "words_per_minute": 2.0,
  "top_keywords": [
   ["everybody", 1],
   ["live", 1],
   ["start", 1],
   ["stream", 1],
   ["tonight", 1],
   ["welcome", 1]
  ]
 }
}
```
