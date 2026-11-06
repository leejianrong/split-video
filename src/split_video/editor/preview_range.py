"""HTTP routes for preview chunks, short low-resolution mp4s of a stretch of the timeline (see `preview_range.py`).

`POST /compose/projects/{name}/preview-range` with `{start, end, height, preset, fit, source}` answers
`{job_id, cached, key, revision, result}`: a chunk already cached for this revision and request is answered at once
(`cached` true, `result` the chunk, the job already `done`), otherwise a `preview_range` job is queued (poll or cancel
it with `/jobs/{job_id}`; progress is thousandths; its result is the same chunk description). The chunk is fetched with
`GET /compose/projects/{name}/preview-range/{key}` as `video/mp4` with Range support, so a `<video>` plays and seeks in
it. A client that may abort the POST before the job id comes back sends an `Idempotency-Key` header: the same key (per
principal) answers with the same job, and `DELETE /preview-range-requests/{key}` cancels that job by key, or, if the
POST has not arrived yet, cancels it on arrival (204 either way; the 256 most recent keys are remembered). After an
edit the key is gone (404): ask for the range again. `POST /project/preview-range?path=` and `GET /project/preview-
range/{key}?path=` are the same for the video's own timeline. Errors: 404 unknown timeline or missing source, 400 for
a bad height or source or a range over 30 s (`preview.range_too_long`), 422 for a range outside the timeline or a
timeline the export would refuse too.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from starlette.responses import Response

from split_video import frames, preview_range, timeline_service
from split_video import project as project_service
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
from split_video.editor.media_response import media_response
from split_video.editor.video_timeline import PATH_DESCRIPTION
from split_video.errors import ProjectError
from split_video.ffmpeg import ProbeError
from split_video.job_work import RENDER_STEPS, preview_range_work
from split_video.jobs import PREVIEW_RANGE, JobQueue
from split_video.media_store import MediaError, MediaOutsideRootError
from split_video.store_provider import StoreProvider
from split_video.timeline import (
    AssetNotFoundError,
    Project,
    ProjectNotFoundError,
    SourceNotFoundError,
    TimelineError,
)


class PreviewRangeRequest(BaseModel):
    start: float  # timeline seconds
    end: float  # timeline seconds, at most 30 s after start; cut to the timeline's end
    height: int | None = None  # pixels, default 480, at most 720, never above the export's
    preset: str | None = None  # export preset: sets the aspect ratio and frame rate
    fit: str | None = None  # letterbox (default) or crop
    source: str = "auto"  # auto (proxies where there are any) or original


class PreviewRangeStartOut(BaseModel):
    job_id: str
    cached: bool  # the chunk was already made: the job is done and `result` says what it is
    key: str  # names the chunk: GET .../preview-range/{key}
    revision: int  # of the timeline the chunk is rendered from
    result: dict[str, Any] | None = None  # the chunk when cached, else on the job when it is done


CHUNK_RESPONSES = {
    200: {"description": "The mp4 (H.264 and AAC).", "content": {"video/mp4": {"schema": {"type": "string", "format": "binary"}}}},
    206: {"description": "The requested byte range."},
}  # fmt: skip

MAX_KEYS = 256
KEY_HEADER = Header(None, description="A client-chosen key: a retry with it is the same job, and DELETE /preview-range-requests/{key} cancels it")  # fmt: skip


def make_router(provider: StoreProvider, jobs: JobQueue) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))
    lock = threading.Lock()
    # Request key -> the answer given for it; None once cancelled before its POST arrived. Bounded, oldest first out.
    requests: OrderedDict[tuple[str, str], PreviewRangeStartOut | None] = OrderedDict()

    def remember(slot: tuple[str, str], out: PreviewRangeStartOut | None) -> None:
        requests[slot] = out
        requests.move_to_end(slot)
        while len(requests) > MAX_KEYS:
            requests.popitem(last=False)

    def start(
        project: Project,
        name: str,
        anchor: str,
        request: PreviewRangeRequest,
        ctx: RequestContext,
        key: str | None,
    ) -> PreviewRangeStartOut:
        try:
            plan = preview_range.plan_range(
                project, ctx.media, anchor, request.start, request.end, name=name, height=request.height,
                preset=request.preset, fit=request.fit, source=request.source,
            )  # fmt: skip
        except frames.FrameArgumentError as exc:
            raise api_error(400, exc) from None
        except frames.FrameError as exc:
            raise api_error(422, exc) from None
        except (ProjectNotFoundError, SourceNotFoundError, AssetNotFoundError, MediaError) as exc:
            raise api_error(404, exc) from None
        except TimelineError as exc:
            raise api_error(422, exc) from None
        label = f"{name} {plan.start:g}-{plan.end:g} s"
        hit = preview_range.lookup(ctx.media, plan)
        slot = (ctx.principal.id, key or "")
        with lock:
            seen = requests.get(slot) if key else None
            if seen is not None:
                return seen
            if hit is not None:
                job_id = jobs.record_done(
                    PREVIEW_RANGE, result=hit, label=label, total=RENDER_STEPS, owner=ctx.principal.id
                )
                out = PreviewRangeStartOut(job_id=job_id, cached=True, key=plan.key, revision=plan.revision, result=hit)
            else:
                job_id = jobs.submit(
                    PREVIEW_RANGE,
                    preview_range_work(ctx.media, plan),
                    label=label,
                    total=RENDER_STEPS,
                    owner=ctx.principal.id,
                )
                out = PreviewRangeStartOut(job_id=job_id, cached=False, key=plan.key, revision=plan.revision)
            if key:
                if slot in requests:  # cancelled before this POST arrived
                    jobs.cancel(job_id)
                remember(slot, out)
            return out

    def chunk(anchor: str, key: str, project: Project, request: Request, ctx: RequestContext) -> Response:
        media_id = preview_range.find_chunk(ctx.media, anchor, key, project.revision)
        if media_id is None:
            raise HTTPException(
                status_code=404,
                detail="no such preview chunk: the timeline changed or it was evicted; start the range again",
            )
        response = media_response(ctx.media, media_id, request)
        response.headers["Cache-Control"] = "private, no-cache"
        return response

    def named(ctx: RequestContext, name: str) -> Project:
        try:
            return timeline_service.load_existing(ctx.store, name, ctx.media)
        except TimelineError as exc:
            raise api_error(404 if isinstance(exc, ProjectNotFoundError) else 422, exc) from None

    def video_of(ctx: RequestContext, path: str) -> tuple[str, Project]:
        try:
            video = ctx.media.resolve(path)
        except MediaOutsideRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root") from None
        if not ctx.media.is_file(video):
            raise HTTPException(status_code=404, detail="file not found")
        try:
            return video, project_service.video_timeline_with_revision(ctx.store, ctx.media, video)
        except (TimelineError, MediaError, ProbeError, ProjectError) as exc:
            raise api_error(422, exc) from None

    @router.post("/compose/projects/{name}/preview-range", response_model=PreviewRangeStartOut)
    def start_named(
        name: str,
        request: PreviewRangeRequest,
        ctx: RequestContext = ctx_dep,
        idempotency_key: str | None = KEY_HEADER,
    ) -> PreviewRangeStartOut:
        """Queue a low-resolution mp4 of the timeline from `start` to `end` seconds (at most 30 s), drawn by the
        export's own filter graph with all audio mixed (no captions), cached by project revision: asking again for
        the same range before an edit returns the chunk at once (`cached`). Fetch the finished chunk with
        `GET /compose/projects/{name}/preview-range/{key}`."""
        return start(named(ctx, name), name, preview_range.anchor_of_project(name), request, ctx, idempotency_key)

    @router.get("/compose/projects/{name}/preview-range/{key}", responses=CHUNK_RESPONSES, response_class=Response)
    def get_named_chunk(name: str, key: str, request: Request, ctx: RequestContext = ctx_dep) -> Response:
        """The chunk `key` as `video/mp4` with Range support, while the timeline is still at the revision it was
        rendered from; 404 after an edit or once evicted (only the 8 most recent chunks are kept)."""
        return chunk(preview_range.anchor_of_project(name), key, named(ctx, name), request, ctx)

    @router.post("/project/preview-range", response_model=PreviewRangeStartOut)
    def start_video(
        request: PreviewRangeRequest,
        path: str = Query(..., description=PATH_DESCRIPTION),
        ctx: RequestContext = ctx_dep,
        idempotency_key: str | None = KEY_HEADER,
    ) -> PreviewRangeStartOut:
        """The same chunk for the video's own timeline (the project the editor has open), addressed by the video's
        `path`: the answer is that of `POST /compose/projects/{name}/preview-range`."""
        video, project = video_of(ctx, path)
        return start(project, path, video, request, ctx, idempotency_key)

    @router.get("/project/preview-range/{key}", responses=CHUNK_RESPONSES, response_class=Response)
    def get_video_chunk(
        key: str, request: Request, path: str = Query(..., description=PATH_DESCRIPTION), ctx: RequestContext = ctx_dep
    ) -> Response:
        """The chunk `key` of the video's own timeline, as `GET /compose/projects/{name}/preview-range/{key}`."""
        video, project = video_of(ctx, path)
        return chunk(video, key, project, request, ctx)

    @router.delete("/preview-range-requests/{key}", status_code=204, response_class=Response)
    def cancel_request(key: str, ctx: RequestContext = ctx_dep) -> Response:
        """Cancel the render started by the POST that carried `Idempotency-Key: {key}`, whether or not the client
        ever read its job id. A key whose POST has not arrived yet cancels that POST's job when it does. 204 always."""
        slot = (ctx.principal.id, key)
        with lock:
            seen = requests.get(slot)
            if seen is not None:
                jobs.cancel(seen.job_id)
            else:
                remember(slot, None)
        return Response(status_code=204)

    return router
