# split-video

A CLI (and, since the visual editor, a small local web app) that splits a long recording of back-to-back songs into individual files by detecting silence gaps with ffmpeg.

## Agent rules

- **Never run more than 2 subagents in parallel on this project.** If a task seems to want more, split it into sequential batches of at most 2.
- **When agents need to work in parallel on this repo, give each one an isolated git worktree via `treehouse`** (`treehouse get --lease` to acquire one, `treehouse return <path>` to release it when the agent is done) instead of ad hoc `git worktree` commands, so parallel agents can't collide by editing the same files in the main working tree.
