"""HTTP routes for a recording's own timeline: `GET /project/timeline` and `POST /project/clips`.

The editor opens a video (`POST /open`, project id = its path) and its project is the video's own timeline
(ADR 0003). The compose routes address timelines by a plain name, which cannot name a video in a folder, so these two
take the video's `path` like `/state` does. `GET` shows the whole timeline in the compose shape (`ProjectOut`: tracks,
clips, revision), reading nothing into existence: a video with nothing saved is its recording as one clip, revision 0.
`POST` adds a library asset (`asset`, an id from `GET /assets`) as a clip on the main video track, at `index` (default:
the end), and saves; send `If-Match` with the revision it was based on (409 `revision_conflict` otherwise).
"""

from __future__ import annotations

from dataclasses import replace

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from split_video import project
from split_video.editor.compose import ProjectOut, project_out
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
from split_video.editor.revisions import WRITE_RESPONSES, base_revision
from split_video.errors import ProjectError, RevisionConflictError
from split_video.ffmpeg import ProbeError
from split_video.media_store import MediaError, MediaOutsideRootError
from split_video.store_provider import StoreProvider
from split_video.timeline import AssetNotFoundError, SourceNotFoundError, TimelineError

PATH_DESCRIPTION = "The video's path relative to the browse root (the project id the editor opens)."


class AddAssetClipRequest(BaseModel):
    asset: str  # an asset id from GET /assets
    index: int | None = None  # position on the main track, 0-based; default: the end


def make_router(provider: StoreProvider) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    def source_of(ctx: RequestContext, path: str) -> str:
        try:
            source = ctx.media.resolve(path)
        except MediaOutsideRootError:
            raise HTTPException(status_code=400, detail="path escapes the browse root") from None
        if not ctx.media.is_file(source):
            raise HTTPException(status_code=404, detail="file not found")
        return source

    @router.get("/project/timeline", response_model=ProjectOut)
    def get_video_timeline(path: str = Query(..., description=PATH_DESCRIPTION), ctx: RequestContext = ctx_dep):
        """The video's own timeline: its tracks and clips, and the `revision` writes are based on."""
        source = source_of(ctx, path)
        try:
            timeline = project.video_timeline(ctx.store, ctx.media, source)
            revision = project.current_revision(ctx.store, source)
            return project_out(ctx.media, path, replace(timeline, revision=revision))
        except (TimelineError, MediaError, ProbeError, ProjectError) as exc:
            raise api_error(422, exc) from None

    @router.post("/project/clips", response_model=ProjectOut, responses=WRITE_RESPONSES)
    def add_asset_clip(
        request: AddAssetClipRequest,
        path: str = Query(..., description=PATH_DESCRIPTION),
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ):
        """Add the asset as a clip on the video's main video track. 404 for an unknown asset, 422 for an audio-only
        one (it needs an audio track) or an index out of range; 409 for a stale `If-Match`."""
        source = source_of(ctx, path)
        try:
            saved = project.add_asset_clip(
                ctx.store, ctx.media, source, request.asset, index=request.index, base_revision=base
            )
            return project_out(ctx.media, path, saved)
        except RevisionConflictError:
            raise  # the app's handler answers it: 409 with the current revision
        except (AssetNotFoundError, SourceNotFoundError) as exc:
            raise api_error(404, exc) from None
        except (TimelineError, MediaError, ProbeError, ProjectError) as exc:
            raise api_error(422, exc) from None

    return router
