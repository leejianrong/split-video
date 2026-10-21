# MCP server (for AI agents)



`split-video mcp` runs a [Model Context Protocol](https://modelcontextprotocol.io)
server over stdio, so an agent can do everything the project commands above
do — propose splits, review and adjust them, label segments, optionally
classify the audio, and export — without a browser or a running editor. It
reads and writes the same project file, so a human can open the result in
the visual editor afterward. Tools: `list_videos`, `list_segments`,
`propose_splits`, `add_split`, `move_split`, `remove_split`, `label_segment`,
`analyze_audio`, `export`. The `compose` commands above have matching tools:
`list_timelines`, `scan_library`, `list_assets`, `prepare_asset`, `transcribe_asset`, `get_transcript`, `transcript_range`, `transcript_search`, `analyze_scenes`, `get_scenes`, `get_frame`, `get_contact_sheet`, `create_timeline`, `show_timeline`, `add_clip_to_timeline`
(a path or an asset id),
`trim_timeline_clip`, `move_timeline_clip`, `remove_timeline_clip`,
`set_timeline_transition`, `add_timeline_track`, `remove_timeline_track`,
`move_timeline_track`, `place_timeline_clip`, `set_timeline_clip`,
`add_timeline_text`, `set_timeline_text`, `render_timeline` (with `preset`, `fit` and `captions`), `list_export_presets`, `list_caption_styles`.

New to driving it? The [agent guide](../src/split_video/agent_guide/SKILL.md) is the order of calls that works (import,
report, transcribe, pick moments, plan, render), with limits, costs and error recovery. The server serves it as the
resource `guide://agent` and the prompts `highlights_workflow`, `vertical_short` and `recover_from_error`;
`split-video agent-guide` prints it. The whole workflow is tested end to end by a scripted MCP client on a long
synthetic recording ([docs/e2e-agent-test.md](e2e-agent-test.md)).

Tools only touch files under `--root` (default: the current directory):

```bash
claude mcp add split-video -- split-video mcp --root ~/recordings
```

or with Docker (the `-i` keeps stdin open for the stdio transport):

```bash
claude mcp add split-video -- docker run -i --rm -v ~/recordings:/data \
  ghcr.io/leejianrong/split-video mcp --root /data
```

You can keep the visual editor open while an agent edits the same project:
see [Revisions](revisions.md).
