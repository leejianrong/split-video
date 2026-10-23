# The visual editor (`edit`)

How to use the browser editor for splitting a recording. For starting it, see [docker.md](docker.md).

The editor is being rebuilt as one workspace; its design contract is [ux-spec.md](ux-spec.md).

`split-video edit ~/recordings` starts a local server and opens a browser
tab with a file browser to pick a video from. Point it at a file directly
(`split-video edit liveshow.mp4`) to skip the picker and open straight into
the editor for it.

In the editor:

- **Opening a video for the first time** shows a one-time setup step:
  optionally detect splits by silence (the same thresholds `split` uses —
  see [Key options](cli.md#key-options-split)), and/or kick off audio
  analysis (see **Analyze audio** below) in the background while you start
  editing. Skip both to start from one segment covering the whole
  recording and split it entirely by hand. This never comes back once
  you've gotten past it — see the save note below.
- The video plays in a real player, with a timeline underneath showing the
  currently-proposed splits. A waveform of the audio renders behind the
  timeline once it's finished decoding, so you can eyeball where the actual
  quiet/loud passages are, not just where a threshold happened to trigger.
- Click or drag anywhere on the timeline to move the playhead — it never
  adds a split by itself. Position the playhead where you want a cut, then
  click **Split** (or press `S`) to add one there. Drag a split point's tab
  to move it, hover it for a delete "×", or click it to select it — a
  **Delete split** button appears, or just press Delete/Backspace. Arrow
  keys nudge a selected split by 0.1s (1s with Shift) for precision beyond
  what dragging gives you.
- Scroll (or a trackpad's vertical two-finger swipe) to zoom the timeline
  in/out, centered on your cursor. A horizontal swipe, Shift+scroll, or the
  scrollbar pans instead; "Fit" resets to the whole recording.
- The segment table below the timeline doubles as the export preview: give
  a segment a label and color, uncheck one to leave it out of the export,
  or type a custom export filename — a live count shows how many files
  you're actually about to write.
- **Analyze audio** runs the recording through YAMNet (a general-purpose
  audio classifier) and colors the timeline by what it hears — music,
  singing, speech, applause/crowd, laughter — so you can visually vet a
  proposed split against more than just where things went quiet. It's a
  background job with a progress bar (classifying a multi-hour recording
  takes real time); the model only needs fetching once (see
  [Run without Docker](cli.md#run-without-docker-uv)) and the result is cached per file. Every
  label gets its own lane by default; the icon button next to Fit collapses
  them back to a single row.
- **Export** shows a preview of exactly which files will be written (honoring
  the include/exclude checkboxes and custom names from the segment table),
  then writes them plus `manifest.json` right there, with a progress bar
  while it runs.
- Keyboard shortcuts: `Space`/`K` play/pause, `J`/`L` jump back/forward 5s,
  `S` split at the playhead, Delete/Backspace remove the selected split,
  and `?` lists them all in the editor.
- Your splits, labels, and export choices are saved to a sidecar
  `<video>.split-video-project.json` file next to the video as you go —
  closing the editor and reopening the same recording resumes exactly
  where you left off, no setup step shown again. Silence detection, the
  waveform, and audio classification are separately cached to disk in a
  `.split-video-cache/` folder next to the video; that one's regenerable
  scratch space, not something to check in.

Useful flags: `--host`, `--port` (default `8765`), `--no-browser`, plus the
same `--silence-threshold`/`--min-silence-duration`/`--min-song-length`/
`--silence-padding` as the setup step's starting values — see
`split-video edit --help`.
