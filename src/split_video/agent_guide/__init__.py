"""The agent guide: one SKILL.md (this package's data file, so it ships in the wheel) served three ways.

The MCP server exposes it as the resource `guide://agent` and as prompts (`highlights_workflow`, `vertical_short`,
`recover_from_error`), the CLI prints it with `split-video agent-guide`. tests/unit/test_agent_guide.py parses the guide
and fails when a tool it names is not a registered MCP tool or an example uses an argument the tool does not have.
"""

from __future__ import annotations

from importlib import resources

GUIDE_URI = "guide://agent"


def guide_text() -> str:
    """The whole SKILL.md (front matter included, which is what a skill loader wants)."""
    return resources.files(__package__).joinpath("SKILL.md").read_text(encoding="utf-8")


def highlights_workflow(asset: str = "", goal: str = "a highlights reel", shorts: int = 0) -> str:
    target = f"asset {asset}" if asset else "the recording (find its asset id with `list_assets()`)"
    extra = (
        f" Then make {shorts} vertical shorts from the best candidates: one timeline each, `set_clip_crop()` with aspect "
        "9:16, and render them with `queue_renders()` at preset vertical-1080."
        if shorts
        else ""
    )
    return (
        f"Goal: {goal}, from {target}. Follow the split-video guide (resource {GUIDE_URI}).\n"
        "1. `scan_library()` if the file is new, then `media_report()` with prepare true.\n"
        "2. `transcribe_asset()` if the goal depends on speech (slow: about 17 min for 2.5 h) and `analyze_audio()` for "
        "music or applause.\n"
        "3. `suggest_highlights()` with count about twice what you need; confirm the top ones with "
        "`transcript_range()` and `get_contact_sheet()`. Never read a whole transcript.\n"
        "4. One `apply_plan()` with dry_run true, read `warnings` and `timeline`, then again with dry_run false.\n"
        f"5. Render with `queue_renders()` and wait with `render_queue_status()` (not tightly).{extra}\n"
        "On a failed call read `code` and `fix` of the error; on `revision.conflict` reload and retry."
    )


def vertical_short(asset: str, start: float, end: float, project: str = "short1", captions: str = "shorts") -> str:
    return (
        f"Make a vertical short from asset {asset}, seconds {start} to {end}. Follow the split-video guide (resource "
        f"{GUIDE_URI}).\n"
        f"1. `apply_plan()` with a one-clip plan: project {project}, mode create, in {start}, out {end}; dry_run first.\n"
        f"2. `set_clip_crop()` on {project}, clip 1, aspect 9:16 (move `center_x` to follow the subject).\n"
        f"3. `transcribe_asset()` if there is no transcript yet and you want captions.\n"
        f"4. `queue_renders()` with project {project}, preset vertical-1080, captions {captions}, a unique output; "
        "then `render_queue_status()`."
    )


def recover_from_error(error: str = "") -> str:
    return (
        "A split-video tool failed. Read the JSON error: branch on `code`, do what `fix` says, do not retry the same "
        "call unchanged.\n"
        "- revision.conflict: reload with `show_timeline()`, re-apply your change, retry with the current_revision as "
        "base_revision.\n"
        "- plan.invalid or ops.invalid: nothing was written; fix every entry of `errors` and resend (use dry_run).\n"
        "- transcript.missing or analysis.missing: run the step `fix` names (`transcribe_asset()`, `analyze_scenes()`), "
        "then ask again.\n"
        "- extra.transcription_missing: the optional extra is not installed; tell the user, continue without speech.\n"
        "- render.output_exists: pick another output name. job.unknown: ids die with a restart; list with "
        "`render_queue_status()`. ffmpeg.failed: do not retry blindly.\n"
        f"The full table is in the guide (resource {GUIDE_URI}) and docs/errors.md.\n"
        + (f"\nThe error: {error}" if error else "")
    )
