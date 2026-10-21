# Bounded MCP tool results

An MCP tool result is read by a model, so it must not grow with the length of the recording. Every tool that can
return per-item data pages it, and says so in the result. The cap: **one MCP result stays under 64 KB of text**
(the text content, which is indented JSON; the structured copy is about 60% of that).

| Tool | Page | Whole-recording summary on every page | Measured, 2.5 h asset |
| --- | --- | --- | --- |
| `analyze_audio` | `regions`, at most `limit` (default 200, max 500) | `seconds_by_bucket`, `regions_by_bucket`, `total_seconds`, `total_regions` | 1457 regions: first page 21.8 KB, 8 pages |
| `get_transcript` | `segments`, at most `limit` (default 100, max 500) and about 28 KB of segment JSON | `total_segments`, `total_words`, `first_start`, `last_end` | 680 segments, 9153 words: first page 54.7 KB, 26 pages (was 768 KB in one result) |

Both add `count`, `truncated` (true when this is not the whole result), `next_cursor` and `notices` (a sentence on what
was cut and how to continue). While `next_cursor` is not null, call again with the same arguments plus `cursor`;
every item appears exactly once and the concatenated pages equal the unbounded result. `get_transcript` also takes
`start` and `end` to narrow the range first, and always returns at least one segment. A cursor is opaque and tied to the
stored data: after a re-analysis or re-transcription it is refused with `request.invalid` (start again without it).

Already bounded the same way: `media_report` and `timeline_index` (a byte budget with `notices`),
`transcript_range` and `transcript_search` (limit and cursor).

The CLI and the HTTP API keep their full output; the new fields exist
only on the MCP tools. Limits are in `project.REGION_PAGE_*` and `transcript_search.TRANSCRIPT_PAGE_*`.
