"""HTTP route for one frame of a timeline's edited result (see `preview.py`).

`GET /compose/projects/{name}/preview-frame?time=&width=&preset=&fit=&format=&quality=&source=` answers with the JPEG or
PNG bytes, rendered synchronously by the export's own filter graph (no job). What the frame is says in headers:
`X-Preview-Revision`, `X-Preview-Time` (the frame shown), `X-Preview-Width`, `X-Preview-Height`,
`X-Preview-Source` ("proxy", "original" or "mixed") and `X-Preview-Duration`. The `ETag` is a hash of the project
revision and the request, so a client that sends it back in `If-None-Match` gets a bodiless 304 without a render
for as long as the timeline has not changed. `GET /project/preview-frame?path=` is the same for the video's own timeline (the
project the editor opens), addressed by the video's path. Errors: 404 unknown timeline or missing source, 400 for a bad format,
source, quality or width, 422 for a time outside the timeline or a timeline the export would refuse too.
"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.responses import Response

from split_video import frames, presets, preview, timeline_service
from split_video import project as project_service
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
from split_video.editor.video_timeline import PATH_DESCRIPTION
from split_video.errors import ProjectError
from split_video.ffmpeg import ProbeError
from split_video.media_store import MediaError, MediaOutsideRootError
from split_video.store_provider import StoreProvider
from split_video.timeline import (
    AssetNotFoundError,
    Project,
    ProjectNotFoundError,
    SourceNotFoundError,
    TimelineError,
)

# Revalidate every time: the ETag makes that a cheap 304 while the timeline is unchanged.
CACHE_CONTROL = "private, no-cache"

IMAGE_RESPONSES = {
    200: {
        "description": "The encoded image.",
        "content": {mime: {"schema": {"type": "string", "format": "binary"}} for mime in frames.FORMATS.values()},
    },
    304: {"description": "The timeline and request are those of the `If-None-Match` ETag."},
}


def make_router(provider: StoreProvider) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    def respond(
        project: Project,
        name: str,
        request: Request,
        ctx: RequestContext,
        time: float,
        width: int | None,
        preset: str | None,
        fit: str | None,
        format: str,
        quality: int,
        source: str,
    ) -> Response:
        try:
            presets.resolve_export(preset, None, None, fit)
        except TimelineError as exc:
            raise api_error(422, exc) from None
        etag = hashlib.sha1(
            f"{name}|{project.revision}|{time}|{width}|{preset}|{fit}|{format}|{quality}|{source}".encode()
        ).hexdigest()
        if f'"{etag}"' in request.headers.get("if-none-match", ""):
            return Response(status_code=304, headers={"ETag": f'"{etag}"', "Cache-Control": CACHE_CONTROL})
        try:
            frame = preview.preview_frame(
                project, ctx.media, time, name=name, width=width, preset=preset, fit=fit, format=format,
                quality=quality, source=source,
            )  # fmt: skip
        except frames.FrameArgumentError as exc:
            raise api_error(400, exc) from None
        except frames.FrameError as exc:
            raise api_error(422, exc) from None
        except (ProjectNotFoundError, SourceNotFoundError, AssetNotFoundError, MediaError) as exc:
            raise api_error(404, exc) from None
        except TimelineError as exc:
            raise api_error(422, exc) from None
        return Response(
            frame.data,
            media_type=frame.mime,
            headers={
                "Cache-Control": CACHE_CONTROL,
                "ETag": f'"{etag}"',
                "X-Preview-Revision": str(frame.revision),
                "X-Preview-Time": f"{frame.time:g}",
                "X-Preview-Width": str(frame.width),
                "X-Preview-Height": str(frame.height),
                "X-Preview-Source": frame.source,
                "X-Preview-Duration": f"{frame.duration:g}",
            },
        )

    @router.get("/compose/projects/{name}/preview-frame", responses=IMAGE_RESPONSES, response_class=Response)
    def get_preview_frame(
        name: str,
        request: Request,
        time: float,
        width: int | None = None,
        preset: str | None = None,
        fit: str | None = None,
        format: str = "jpeg",
        quality: int = frames.DEFAULT_QUALITY,
        source: str = "auto",
        ctx: RequestContext = ctx_dep,
    ) -> Response:
        """The edited result at `time` seconds as a JPEG (default) or PNG, `width` pixels wide (default 640, never
        above the export's; height follows the export's aspect ratio). Main track, overlays, titles, colour and
        crop are drawn by the export's own filter graph; captions and sound are not. `preset` and `fit` are the
        export's, so a vertical preset previews the vertical frame. `source=auto` reads proxies where they exist
        (a frame within 1/15 s of the time), `original` reads the files like the export."""
        try:
            project = timeline_service.load_existing(ctx.store, name, ctx.media)
        except TimelineError as exc:
            raise api_error(404 if isinstance(exc, ProjectNotFoundError) else 422, exc) from None
        return respond(project, name, request, ctx, time, width, preset, fit, format, quality, source)

    @router.get("/project/preview-frame", responses=IMAGE_RESPONSES, response_class=Response)
    def get_video_preview_frame(
        request: Request,
        time: float,
        path: str = Query(..., description=PATH_DESCRIPTION),
        width: int | None = None,
        preset: str | None = None,
        fit: str | None = None,
        format: str = "jpeg",
        quality: int = frames.DEFAULT_QUALITY,
        source: str = "auto",
        ctx: RequestContext = ctx_dep,
    ) -> Response:
        """The same picture for the video's own timeline (the project the editor has open, `GET /project/timeline`),
        addressed by the video's `path`: the answer, headers and ETag are those of
        `GET /compose/projects/{name}/preview-frame`."""
        try:
            video = ctx.media.resolve(path)
        except MediaOutsideRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root") from None
        if not ctx.media.is_file(video):
            raise HTTPException(status_code=404, detail="file not found")
        try:
            project = project_service.video_timeline_with_revision(ctx.store, ctx.media, video)
        except (TimelineError, MediaError, ProbeError, ProjectError) as exc:
            raise api_error(422, exc) from None
        return respond(project, path, request, ctx, time, width, preset, fit, format, quality, source)

    return router
