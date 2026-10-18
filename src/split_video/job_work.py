"""The work functions the job queue runs: export, audio analysis and timeline render.

Each `*_work` builds a `Callable[[JobContext], Any]` for `JobQueue.submit`. They are thin: the
actual operations are `project.run_export`, `project.run_analysis` and
`timeline_service.run_render`, which the CLI and MCP server also call inline, so there is one
implementation. All that is added here is wiring the operation's progress callbacks to the job's
`JobContext` and turning its outcome into a JSON-safe result.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from split_video import project, timeline_service
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache
from split_video.jobs import JobContext
from split_video.media_store import MediaStore
from split_video.timeline import Project

# A render reports a 0..1 fraction; as a job it counts thousandths of the output.
RENDER_STEPS = 1000


def export_work(
    media: MediaStore,
    plan: project.ExportPlan,
    *,
    precise: bool,
    output_format: str | None,
    write_manifest_file: bool,
    parameters: dict[str, Any],
) -> Callable[[JobContext], dict[str, Any]]:
    """Write the planned files. Progress counts files, the message is the file being written, and
    the partial result `{"files": [...]}` lists what is on disk so far; the final result adds
    `output_dir` and `manifest_path`."""

    def work(ctx: JobContext) -> dict[str, Any]:
        files: list[str] = []

        def on_file_start(filename: str) -> None:
            ctx.progress(len(files), message=filename)

        def on_file_done(media_id: str) -> None:
            files.append(media.display_path(media_id))
            ctx.progress(len(files))
            ctx.set_result({"files": list(files)})

        _, manifest_id = project.run_export(
            media,
            plan,
            precise=precise,
            output_format=output_format,
            write_manifest_file=write_manifest_file,
            parameters=parameters,
            on_file_start=on_file_start,
            on_file_done=on_file_done,
            cancel=ctx,
        )
        return {
            "output_dir": media.display_path(plan.output_dir),
            "manifest_path": media.display_path(manifest_id) if manifest_id is not None else None,
            "files": files,
        }

    return work


def analysis_work(
    media: MediaStore, source: str, cache: ClassificationCache, thresholds: dict[str, float]
) -> Callable[[JobContext], None]:
    """Classify `source`'s audio into `cache`. Progress counts model frames."""

    def work(ctx: JobContext) -> None:
        # An upfront estimate from the duration alone, so the progress bar isn't stuck at 0/0 during
        # the (possibly slow) PCM decode, which happens before the real per-frame count is known.
        estimate = max(1, int(media.probe(source).duration * classify.SAMPLE_RATE) // classify.HOP_SAMPLES)
        ctx.progress(0, total=estimate)
        project.run_analysis(media, source, cache, thresholds, ctx.progress, cancel=ctx)

    return work


def render_work(
    timeline: Project, media: MediaStore, out_id: str, filename: str
) -> Callable[[JobContext], dict[str, Any]]:
    """Render the timeline to `out_id`. Progress is `RENDER_STEPS` thousandths; the result is `{"output": filename}`."""

    def work(ctx: JobContext) -> dict[str, Any]:
        def on_progress(fraction: float) -> None:
            ctx.progress(round(fraction * RENDER_STEPS), RENDER_STEPS)

        timeline_service.run_render(timeline, media, out_id, on_progress, cancel=ctx)
        return {"output": filename}

    return work
