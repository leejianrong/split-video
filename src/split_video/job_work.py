"""The work functions the job queue runs: export, audio analysis, timeline render, proxies, scans, transcription, scene analysis and on-screen text.

Each `*_work` builds a `Callable[[JobContext], Any]` for `JobQueue.submit`. They are thin: the
actual operations are `project.run_export`, `project.run_analysis` and
`timeline_service.run_render`, which the CLI and MCP server also call inline, so there is one
implementation. All that is added here is wiring the operation's progress callbacks to the job's
`JobContext` and turning its outcome into a JSON-safe result.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from split_video import captions, library, media_report, ocr, project, proxy, scenes, timeline_service, transcribe
from split_video.editor import classify
from split_video.editor.cache import ClassificationCache
from split_video.jobs import PROXY, JobContext, JobQueue
from split_video.media_store import MediaStore
from split_video.store import ProjectStore
from split_video.timeline import Project

# A render reports a 0..1 fraction; as a job it counts thousandths of the output.
RENDER_STEPS = 1000
# A transcription reports seconds of audio done; as a job it counts thousandths of the audio.
TRANSCRIBE_STEPS = 1000
# A scene analysis reports seconds of video done; as a job it counts thousandths of the video.
SCENE_STEPS = 1000


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


def analysis_summary(cache: ClassificationCache) -> dict[str, Any]:
    """The result of an analysis job: `{regions, duration}` (classification regions found, seconds of audio)."""
    return {"regions": len(cache.get_regions()), "duration": round(cache.total_duration, 3)}


def analysis_work(
    media: MediaStore, source: str, cache: ClassificationCache, thresholds: dict[str, float]
) -> Callable[[JobContext], dict[str, Any]]:
    """Classify `source`'s audio into `cache`. Progress counts model frames. The result is `{regions, duration}`
    (`analysis_summary`); read the regions themselves with `GET /classification`."""

    def work(ctx: JobContext) -> dict[str, Any]:
        # An upfront estimate from the duration alone, so the progress bar isn't stuck at 0/0 during
        # the (possibly slow) PCM decode, which happens before the real per-frame count is known.
        estimate = max(1, int(media.probe(source).duration * classify.SAMPLE_RATE) // classify.HOP_SAMPLES)
        ctx.progress(0, total=estimate)
        project.run_analysis(media, source, cache, thresholds, ctx.progress, cancel=ctx)
        return analysis_summary(cache)

    return work


# A silence scan reports seconds of audio done; as a job it counts thousandths of the audio.
SPLIT_STEPS = 1000


def split_work(
    media: MediaStore,
    store: ProjectStore,
    source: str,
    *,
    silence_threshold: float = -35.0,
    min_silence_duration: float = 2.0,
    min_song_length: float = 30.0,
    padding: float = 0.15,
    label_prefix: str | None = None,
    base_revision: int | None = None,
) -> Callable[[JobContext], dict[str, Any]]:
    """Split the video at its silences and save the segments (`project.split_by_silence`). Progress counts
    thousandths of the audio; the result is `{video, segments, revision, labels}`. A cancelled job saves nothing."""

    def work(ctx: JobContext) -> dict[str, Any]:
        duration = max(media.probe(source).duration, 1e-9)

        def on_progress(seconds: float) -> None:
            ctx.progress(min(round(seconds / duration * SPLIT_STEPS), SPLIT_STEPS - 1), SPLIT_STEPS)

        ctx.progress(0, SPLIT_STEPS, "Listening for silence")
        return project.split_by_silence(
            store,
            media,
            source,
            silence_threshold=silence_threshold,
            min_silence_duration=min_silence_duration,
            min_song_length=min_song_length,
            padding=padding,
            label_prefix=label_prefix,
            base_revision=base_revision,
            cancel=ctx,
            on_progress=on_progress,
        )

    return work


def report_prepare_work(media: MediaStore, asset_id: str) -> Callable[[JobContext], dict[str, Any]]:
    """Make the asset's missing cheap signals (`media_report.prepare_signals`: loudness, silence scan, audio
    classification). Progress is `media_report.PREPARE_STEPS` thousandths of the work; the result is `{asset,
    prepared, already, skipped}`. A cancelled job stores no half-made signal."""

    def work(ctx: JobContext) -> dict[str, Any]:
        ctx.progress(0, media_report.PREPARE_STEPS)
        return media_report.prepare_signals(media, asset_id, on_progress=ctx.progress, cancel=ctx)

    return work


def render_work(
    timeline: Project,
    media: MediaStore,
    out_id: str,
    filename: str,
    width: int | None = None,
    height: int | None = None,
    preset: str | None = None,
    fit: str | None = None,
    name: str | None = None,
    caption_request: captions.CaptionRequest | None = None,
) -> Callable[[JobContext], dict[str, Any]]:
    """Render the timeline to `out_id`. Progress is `RENDER_STEPS` thousandths; the result is
    `timeline_service.render_result` (name, output, duration and the preset, fit, size, fps and captions report)."""

    def work(ctx: JobContext) -> dict[str, Any]:
        def on_progress(fraction: float) -> None:
            ctx.progress(round(fraction * RENDER_STEPS), RENDER_STEPS)

        used = timeline_service.run_render(
            timeline, media, out_id, on_progress, cancel=ctx, width=width, height=height, preset=preset, fit=fit,
            caption_request=caption_request,
        )  # fmt: skip
        return timeline_service.render_result(name or filename, filename, timeline, used)

    return work


def proxy_work(media: MediaStore, asset_id: str, *, force: bool = False) -> Callable[[JobContext], dict[str, Any]]:
    """Make the asset's proxy and thumbnail strip (`proxy.prepare`). Progress is `proxy.STEPS` steps; the
    result is `prepare`'s `{asset, proxy, thumbnails, made}`."""

    def work(ctx: JobContext) -> dict[str, Any]:
        def on_progress(done: float, total: int) -> None:
            ctx.progress(done, total)

        ctx.progress(0, proxy.STEPS)
        return proxy.prepare(media, asset_id, force=force, on_progress=on_progress, cancel=ctx)

    return work


def transcribe_work(
    media: MediaStore,
    asset_id: str,
    *,
    model: str = transcribe.DEFAULT_MODEL,
    language: str | None = None,
    force: bool = False,
) -> Callable[[JobContext], dict[str, Any]]:
    """Transcribe the asset (`transcribe.transcribe`). Progress counts thousandths of the audio; the result is the
    transcript's info `{asset, model, language, duration, segments, words, made}` (the transcript itself is
    `GET /assets/{id}/transcript`)."""

    def work(ctx: JobContext) -> dict[str, Any]:
        def on_progress(done: float, total: float) -> None:
            ctx.progress(round(done / max(total, 1e-9) * TRANSCRIBE_STEPS), TRANSCRIBE_STEPS)

        ctx.progress(0, TRANSCRIBE_STEPS)
        return transcribe.transcribe(
            media, asset_id, model=model, language=language, force=force, on_progress=on_progress, cancel=ctx
        )

    return work


def scene_work(
    media: MediaStore,
    asset_id: str,
    *,
    threshold: float = scenes.DEFAULT_THRESHOLD,
    min_gap: float = scenes.DEFAULT_MIN_GAP,
    screen_threshold: float = scenes.DEFAULT_SCREEN_THRESHOLD,
    force: bool = False,
) -> Callable[[JobContext], dict[str, Any]]:
    """Find the asset's scene changes and activity curve (`scenes.analyze`). Progress counts thousandths of the
    video; the result is the stored document's info `{asset, source, duration, threshold, min_gap,
    screen_threshold, scenes, activity_buckets, made}` (the signals themselves are `GET /assets/{id}/scenes`)."""

    def work(ctx: JobContext) -> dict[str, Any]:
        def on_progress(done: float, total: float) -> None:
            ctx.progress(round(done / max(total, 1e-9) * SCENE_STEPS), SCENE_STEPS)

        ctx.progress(0, SCENE_STEPS)
        return scenes.analyze(
            media,
            asset_id,
            threshold=threshold,
            min_gap=min_gap,
            screen_threshold=screen_threshold,
            force=force,
            on_progress=on_progress,
            cancel=ctx,
        )

    return work


def ocr_work(
    media: MediaStore,
    asset_id: str,
    *,
    interval: float = ocr.DEFAULT_INTERVAL,
    max_frames: int = ocr.DEFAULT_MAX_FRAMES,
    force: bool = False,
) -> Callable[[JobContext], dict[str, Any]]:
    """Read the asset's on-screen text (`ocr.recognize`). Progress counts frames (the total is known once the frames
    are chosen); the result is the stored document's counts `{asset, engine, duration, interval, max_frames,
    scenes_used, frames, scene_frames, ocr_frames, skipped_unchanged, capped, frames_before_cap, failed_frames,
    note, made}` (the text itself is `GET /assets/{id}/ocr`)."""

    def work(ctx: JobContext) -> dict[str, Any]:
        def on_progress(done: int, total: int) -> None:
            ctx.progress(done, total)

        return ocr.recognize(
            media, asset_id, interval=interval, max_frames=max_frames, force=force, on_progress=on_progress, cancel=ctx
        )

    return work


def library_scan_work(
    media: MediaStore, folder: str, *, jobs: JobQueue | None = None, owner: str | None = None
) -> Callable[[JobContext], dict[str, Any]]:
    """Scan `folder` into the asset table. Progress counts files, the message is the file just handled;
    the result is `ScanReport.to_dict()`. With `jobs`, a `proxy` job is queued for each newly added video
    asset once the scan is done, and the result gains `proxy_jobs`: `[{asset, job_id}]`."""

    def work(ctx: JobContext) -> dict[str, Any]:
        def on_progress(done: int, total: int, path: str) -> None:
            ctx.progress(done, total, message=path)

        report = library.scan(media, folder, on_progress=on_progress, cancel=ctx)
        result = report.to_dict()
        if jobs is not None:
            result["proxy_jobs"] = [
                {"asset": a.id, "job_id": jobs.submit(PROXY, proxy_work(media, a.id), label=a.path, owner=owner)}
                for a in report.added
                if a.has_video
            ]
        return result

    return work
