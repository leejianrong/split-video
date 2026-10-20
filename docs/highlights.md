# Highlight suggestions: a deterministic first pass

`suggest_highlights(asset)` slides a window over a whole recording, scores each window from the signals the other
tools already stored, and returns ranked, non-overlapping candidates with the reasons each one was chosen. It never
decodes media and never runs a model (it reads the same stored documents as [`what_is_happening`](time-index.md) and
the [media report](media-report.md)), so it is repeatable and cheap: the agent's own judgement starts from a short
list instead of three hours. It proposes; it does not decide what is good. The same JSON comes from every surface:

| Surface | Call |
| --- | --- |
| HTTP | `GET /api/v1/assets/{id}/highlights?preset=&count=&min_seconds=&max_seconds=&target_seconds=&start=&end=&keywords=&weights=&min_gap_seconds=&snap=` |
| CLI | `split-video highlights <target> [--preset P] [--count N] [--min-seconds S] [--max-seconds S] [--target-seconds S] [--start T] [--end T] [--keywords a,b] [--weight signal=value ...] [--min-gap-seconds S] [--no-snap] [--json]` (a table by default; `--json` is the document as compact JSON) |
| MCP | `suggest_highlights(asset, preset, count, min_seconds, max_seconds, target_seconds, start, end, keywords, weights, min_gap_seconds, snap)` |
| Python | `highlights.suggest_highlights(media, asset_id, preset=, count=, ..., weights=, keywords=)`; with a loaded `TimeIndex`, `highlights.suggest_from_index(index, ...)` |

The engine is `src/split_video/highlights.py`; its module docstring is the reference for the method. `start` and `end`
of each candidate are plain seconds on the asset's timeline, so they can go straight into an edit plan.

## Method

1. Each stored signal becomes one value per second, with running sums (the window maximum comes from a sparse table), so
   a window is a few lookups. Nothing loops over the recording in Python per window.
2. Windows of `target_seconds` (default 30, clamped to `[min_seconds, max_seconds]`) every 5 s.
3. Each window gets a feature in 0..1 per signal, **against the recording's own distribution**:
   `(value - median) / (high - median)` clipped to 0..1, `high` being the 98th percentile (at least `median +` a minimum
   spread, so a flat recording does not turn noise into full marks). Quiet and loud recordings both work, and steady
   background (constant chatter, a constant tone) scores 0: only what stands out scores. Class shares and keyword
   coverage are fractions of the window and use 1.0 as the high end.
4. Score = `sum(weight * feature) / sum(weight)` over the signals that exist, minus the silence penalty, clipped to
   0..1. A signal that was never computed is left out and the other weights renormalise: `signals.used` says which
   were used and `signals.missing` how to make the rest.
5. Greedy non-maximum suppression: the best window, then the next best one at least `min_gap_seconds` (default 10) from
   every pick, until `count`. Windows scoring below 0.05 are not candidates (`notes` says when fewer come back).
6. With `snap` (default) a peak longer than the window is taken whole (it grows 5 s at a time while the next 5 s alone
   score at least half of the window), the start moves to the nearest boundary at or before it and the end to the
   nearest one at or after it, within 10 s: a silence gap (a stored silence of 0.5 s or more; else a quiet
   `silence_other` stretch) first, else a sentence boundary (a gap above 0.4 s between transcribed words), else a scene
   change. The duration stays within `min_seconds` and `max_seconds`, and a candidate never comes within
   `min_gap_seconds` of another. Each candidate is scored again on its final edges and ranked by that score;
   `snapped` says what moved (`from` is the scored window).

## Signals and presets

| Signal | Feature (0..1) | Comes from |
| --- | --- | --- |
| `speech` | words per second | transcript |
| `loudness` | half the window's peak dB, half its mean dB | stored loudness curve |
| `applause` | share of the window that is `applause_crowd` or `laughter` | audio classes |
| `music` | share that is `music` or `singing` | audio classes |
| `noise` | share that is `other_sound` (loud sound YAMNet filed as silence; see [time-index.md](time-index.md)) | audio classes + loudness |
| `scene` | scene cuts and text-only screen changes per window | `scenes` |
| `activity` | 0.7 mean + 0.3 max of the screen activity | `scenes` |
| `ocr` | times the on-screen text changed | `ocr` |
| `keywords` | `keywords` on screen (coverage of the window) or spoken (up to half) | `ocr`, transcript |
| `silence` | **penalty**: how much more of the window is silent than in the median window | silence scan |

Weights per preset (the `silence` column is the penalty strength, not renormalised):

| preset | speech | loudness | applause | music | noise | scene | activity | ocr | keywords | silence | default keywords |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `default` | 1.0 | 1.0 | 1.0 | 0.5 | 0.3 | 0.7 | 0.5 | 0.5 | 1.5 | 0.4 | none |
| `gig` | 0.2 | 1.5 | 2.0 | 1.5 | 0.3 | 0.2 | 0.1 | 0.0 | 1.0 | 0.5 | none |
| `stream` | 1.5 | 0.5 | 0.3 | 0.1 | 0.2 | 1.0 | 0.8 | 1.0 | 2.0 | 0.4 | FAILED, error, traceback, exception |
| `lecture` | 1.5 | 0.2 | 0.3 | 0.0 | 0.0 | 1.5 | 0.3 | 1.2 | 1.5 | 0.3 | none |

`gig` is music, applause and loudness (speech low); `stream` is speech density, screen changes, on-screen text and
keywords with a little applause; `lecture` is speech, slide changes and on-screen text with loudness low.

**Overriding.** `weights` replaces single entries of the preset: `speech:2,applause:0.5` (HTTP), `--weight speech=2
--weight applause=0.5` (CLI), `{"speech": 2}` (MCP; HTTP also takes a JSON object). 0 switches a signal off. An unknown
signal or a negative or non-numeric weight is an error that lists the valid signals. `keywords` (comma separated, or a
list) replaces the preset's keywords, `[]` / an empty value means none; matching is case-insensitive.
The effective weights and keywords are echoed in `params`.

## The result (`"version": 1`)

Keys are never removed or renamed within a version. Times are seconds, rounded to 3 decimals.

| Key | Meaning |
| --- | --- |
| `asset`, `preset` | what was asked |
| `params` | `count`, `min_seconds`, `max_seconds`, `target_seconds`, `start`, `end`, `min_gap_seconds`, `snap`, `hop_seconds`, `snap_tolerance_seconds`, `keywords`, `weights` (every signal, effective) |
| `signals` | `available` (as in time-index: audio, loudness, silence, speech, scenes, ocr), `missing` (`[{signal, how}]`), `used` (the weight signals that scored) |
| `candidates[]` | `rank`, `start`, `end`, `duration`, `score` (0..1), `reasons [{signal, weight, value, note}]` (weight is the share of the total, value the 0..1 feature, note says what was found), `score_parts {signal: contribution}` (they add up to `score` before rounding; `silence` is negative), `snapped {start, end, from}` (`silence`, `sentence`, `scene` or null), and when there is something to say `title_hint`, `transcript_excerpt`, `ocr_excerpt` (at most 240 characters) |
| `notes` | plain sentences: where default keywords came from, signals with a weight but no data, fewer candidates than asked |

Errors: unknown asset (HTTP 404, MCP tool error, CLI exit 1); bad parameters, an unknown preset or weight signal, a
negative weight, a bad span (422, tool error, exit 1); nothing stored that can score a window (HTTP 409, tool error,
exit 1), with a message naming what to run: `split-video report <target> --prepare`, `transcribe`, `scenes`, `ocr`.

## Example

A 1 hour recording with an applause peak at 1000-1030 and a speech burst with a failing test on screen at 2000-2040
(`GET /api/v1/assets/{id}/highlights?preset=stream&count=2`, shortened):

```json
{
  "version": 1,
  "preset": "stream",
  "signals": {"used": ["speech", "loudness", "applause", "music", "noise", "scene", "activity", "ocr", "keywords"], "missing": []},
  "candidates": [
    {
      "rank": 1, "start": 2000.0, "end": 2040.0, "duration": 40.0, "score": 0.885,
      "reasons": [
        {"signal": "keywords", "weight": 0.27, "value": 1.0, "note": "on-screen text with 'FAILED': FAILED tests/unit/test_render.py::test_overlay_order"},
        {"signal": "speech", "weight": 0.203, "value": 1.0, "note": "speech 4.0 words/s"},
        {"signal": "scene", "weight": 0.135, "value": 1.0, "note": "6 scene/screen changes at 2003, 2009, 2015, 2021, ..."}
      ],
      "score_parts": {"speech": 0.203, "loudness": 0.068, "scene": 0.135, "activity": 0.108, "ocr": 0.101, "keywords": 0.27},
      "snapped": {"start": "silence", "end": "silence", "from": {"start": 2000.0, "end": 2030.0}},
      "title_hint": "FAILED tests/unit/test_render.py::test_overlay_order"
    },
    {
      "rank": 2, "start": 1000.0, "end": 1030.0, "duration": 30.0, "score": 0.108,
      "reasons": [
        {"signal": "loudness", "weight": 0.068, "value": 1.0, "note": "loudness peak -8 dB, mean -9 dB"},
        {"signal": "applause", "weight": 0.041, "value": 0.989, "note": "applause 30s at 1000.32-1030"}
      ]
    }
  ],
  "notes": ["keywords from the stream preset: FAILED, error, traceback, exception", "2 candidates: no more non-overlapping windows scoring 0.05 or more"]
}
```

With `preset=gig` the same recording ranks the applause peak first (0.60) and the burst second (0.35).

## Cost

Reads stored documents only. On a 3 hour synthetic recording (10,800 s, 27,000 words, 22,500 classification frames, 180
screen changes), scoring takes about 0.03 s once the documents are loaded (0.15 to 0.6 s, mostly the classification);
peak memory of the whole run is about 80 MB. The HTTP, CLI and MCP calls load the documents on each call. Unit tests
assert under 1 s for the scoring and 2 s including the load.

## Limits

- It cannot see anything that was not stored: a gig with no audio classification is judged by loudness alone, a silent
  screen recording by its screen changes. `signals.missing` says what would sharpen it.
- Keywords match substrings of on-screen text and single spoken words (not phrases across words).
- Windows are scored on a 1 s grid; boundaries are exact times (silence and sentence edges, scene changes).
