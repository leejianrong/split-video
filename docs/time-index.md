# The time index: what is happening at a timestamp

`what_is_happening(asset, time, window)` answers "what is going on at 1:23:45?" in one call, from the signals the
other tools already stored. It never decodes media and never runs a model, so it is fast (see Cost) and safe to call
in a loop. The same JSON comes from every surface:

| Surface | Call |
| --- | --- |
| HTTP | `GET /api/v1/assets/{id}/at?time=&window=` and `GET /api/v1/assets/{id}/timeline?start=&end=&step=` |
| CLI | `split-video at <target> <time> [--window S] [--json]` and `split-video timeline-index <target> [--start --end --step] [--json]` (times are seconds or `1:23:45`) |
| MCP | `what_is_happening(asset, time, window)` and `timeline_index(asset, start, end, step)` |
| Python | `time_index.what_is_happening(media, asset_id, time, window)`; for many calls `TimeIndex.load(media, asset_id)` once, then `.at(time, window)`, `.window(start, end)`, `.timeline(start, end, step)` |

The engine is `src/split_video/time_index.py`; its module docstring is the reference for every rule below.

## Arguments

- `time`: the centre, in seconds, from 0 to the asset's duration (inclusive).
- `window`: total seconds, default 10, above 0 and at most 600, centred on `time` and clamped to `[0, duration]`
  (at `time` 0 the window is `[0, window / 2]`).
- Errors: unknown asset (HTTP 404, MCP tool error, CLI exit 1); negative time, time beyond the duration, bad window
  or span or step (HTTP 422, tool error, exit 1). A missing signal is never an error.

## The result (`"version": 1`)

The shape is stable within a version: keys are never added or removed, and a signal that was not computed is `null`,
not absent. It is pinned by golden files in `tests/fixtures/time_index/`. Times are seconds, levels are dBFS.

| Key | Meaning |
| --- | --- |
| `version`, `asset`, `time`, `window {start,end}`, `duration` | what was asked, after clamping |
| `audio` | `class` (the class with the most seconds in the window: `music`, `singing`, `speech`, `applause_crowd`, `laughter` or `silence_other`), `share`, `secondary`, `regions [{start,end,class,score}]` clipped to the window (at most 100, `regions_truncated`). From the YAMNet classification (`split-video analyze`). |
| `loudness` | `mean_db`, `max_db`, `min_db` over the 1 s RMS values overlapping the window, `window_seconds`, `curve [{t,db}]` (at most 60 points, thinned by keeping the loudest of each group). Only if the curve is stored; it is never computed here (`split-video report <target> --prepare` makes it without the editor, see [media-report.md](media-report.md)). |
| `silence` | `threshold_db` (the stored scan closest to -35), `inside` (is `time` in a silence), `intervals [{start,end}]` of at least 0.5 s clipped to the window (at most 50, `truncated`). From `split-video propose`. |
| `speech` | `words [{start,end,word}]` (at most 200), `text`, `word_count` (all words in the window), `words_per_minute`, `truncated`. From the transcript. |
| `visual.scene_change` | `{time, score}` of the cut nearest `time` inside the window, or null |
| `visual.scene_changes`, `scene_index`, `shot_start` | cuts inside the window; cuts at or before `time`; time of the last one (0.0 before the first) |
| `visual.activity` | `{mean, max, curve [{t,value}]}` of the 0..1 screen activity (at most 60 points) |
| `visual.description` | reserved for picture descriptions (SPL-97): always `null` for now |
| `visual.text` | the on-screen text up at `time` (else the latest shown in the window): `time`, `until`, `text`, `lines` (at most 1000 characters / 30 lines, `truncated`), `more` (other text states in the window). From `split-video ocr`. |
| `available` | one boolean per signal: `audio loudness silence speech scenes ocr description` (`description` is always false) |
| `missing` | `[{signal, how}]` for each absent signal the asset can have, with the CLI command, MCP tool and HTTP route that makes it. Audio-only assets do not list scenes or ocr; assets without sound do not list the audio signals. `description` is never listed. |

A `window` that has no data for a signal that exists (for example no words in a quiet window) is still an object
(`word_count: 0`), so `null` always means "never computed".

## Timeline (bulk)

`timeline(start, end, step)` returns `{version, asset, duration, start, end, step, count, points, available, missing}`
with one compact point per step, window `[t, t + step)`: `start`, `end`, `audio` (class), `loudness_db` (mean),
`loudness_max_db`, `word_count`, `words_per_minute`, `scene_changes`, `activity` (mean) and `text` (first line, at most
120 characters); each is null when its signal is missing. Defaults: the whole asset, step 60 s raised so there are at
most 500 points; an explicit step giving more than 500 points, or under 1 s, is an error. Pick a point, then ask `at`
for the detail.

## Example

`GET /api/v1/assets/{id}/at?time=100` on a recording with all signals (1,919 bytes as compact JSON; pretty-printed
with indent 2 it is 3.7 KB):

```json
{
  "version": 1,
  "asset": "a_e37c5eab78c34f0f",
  "time": 100.0,
  "window": {
    "start": 95.0,
    "end": 105.0
  },
  "duration": 300.0,
  "audio": {
    "class": "speech",
    "share": 0.9,
    "secondary": "music",
    "regions": [
      {
        "start": 95.0,
        "end": 96.0,
        "class": "music",
        "score": 0.9
      },
      {
        "start": 96.0,
        "end": 105.0,
        "class": "speech",
        "score": 0.9
      }
    ],
    "regions_truncated": false
  },
  "loudness": {
    "mean_db": -29.2,
    "max_db": -22.0,
    "min_db": -40.0,
    "window_seconds": 1.0,
    "curve": [
      {
        "t": 95.0,
        "db": -40.0
      },
      {
        "t": 96.0,
        "db": -40.0
      },
      {
        "t": 97.0,
        "db": -40.0
      },
      {
        "t": 98.0,
        "db": -22.0
      },
      {
        "t": 99.0,
        "db": -22.0
      },
      {
        "t": 100.0,
        "db": -22.0
      },
      {
        "t": 101.0,
        "db": -22.0
      },
      {
        "t": 102.0,
        "db": -22.0
      },
      {
        "t": 103.0,
        "db": -22.0
      },
      {
        "t": 104.0,
        "db": -40.0
      }
    ]
  },
  "silence": {
    "threshold_db": -35.0,
    "inside": false,
    "intervals": [
      {
        "start": 96.0,
        "end": 97.5
      }
    ],
    "truncated": false
  },
  "speech": {
    "words": [
      {
        "start": 98.0,
        "end": 98.4,
        "word": "welcome"
      },
      {
        "start": 98.5,
        "end": 98.9,
        "word": "everybody"
      },
      {
        "start": 99.0,
        "end": 99.4,
        "word": "to"
      },
      {
        "start": 99.5,
        "end": 99.9,
        "word": "the"
      },
      {
        "start": 100.0,
        "end": 100.4,
        "word": "live"
      },
      {
        "start": 100.5,
        "end": 100.9,
        "word": "stream"
      },
      {
        "start": 101.0,
        "end": 101.4,
        "word": "tonight"
      },
      {
        "start": 101.5,
        "end": 101.9,
        "word": "we"
      },
      {
        "start": 102.0,
        "end": 102.4,
        "word": "start"
      },
      {
        "start": 102.5,
        "end": 102.9,
        "word": "now"
      }
    ],
    "text": "welcome everybody to the live stream tonight we start now",
    "word_count": 10,
    "words_per_minute": 60.0,
    "truncated": false
  },
  "visual": {
    "scene_change": null,
    "scene_changes": 0,
    "scene_index": 1,
    "shot_start": 20.0,
    "activity": {
      "mean": 0.1,
      "max": 0.1,
      "curve": [
        {
          "t": 95.0,
          "value": 0.1
        },
        {
          "t": 96.0,
          "value": 0.1
        },
        {
          "t": 97.0,
          "value": 0.1
        },
        {
          "t": 98.0,
          "value": 0.1
        },
        {
          "t": 99.0,
          "value": 0.1
        },
        {
          "t": 100.0,
          "value": 0.1
        },
        {
          "t": 101.0,
          "value": 0.1
        },
        {
          "t": 102.0,
          "value": 0.1
        },
        {
          "t": 103.0,
          "value": 0.1
        },
        {
          "t": 104.0,
          "value": 0.1
        }
      ]
    },
    "description": null,
    "text": {
      "time": 0.0,
      "until": 140.0,
      "text": "intro slide",
      "lines": [
        "intro slide"
      ],
      "truncated": false,
      "more": 0
    }
  },
  "available": {
    "audio": true,
    "loudness": true,
    "silence": true,
    "speech": true,
    "scenes": true,
    "ocr": true,
    "description": false
  },
  "missing": []
}
```

## Cost

- Size: a default response is 1.5 to 3 KB compact; the widest possible (`window=600` on a dense recording: 200 words, 60
  curve points, 100 regions) is about 15 KB. A timeline point is about 190 bytes: 180 points (3 hours at the default step) are 35 KB, 500 points about 95 KB.
- Speed, 3 hour synthetic recording (27,000 words, 22,500 classification frames, 540 cuts): loading every stored
  document takes 0.4 to 0.7 s once (mostly the classification), then `at()` is about 0.1 ms (1.6 ms for a 600 s window) and a
  180-point `timeline()` about 40 ms; peak memory of the whole benchmark is 84 MB. The HTTP, CLI and MCP calls load the documents each time; a media report should build one
  `TimeIndex` and call it many times, or use `timeline`.
