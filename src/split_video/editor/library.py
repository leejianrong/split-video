"""HTTP route for scanning a folder into the library, run as a `library_scan` job.

The scan is slow on a large folder, so the route only validates the folder and queues the job;
poll it (or follow `/events`) through `/jobs/{job_id}`, whose `completed` and `total` count files
and whose result is the scan report. The library itself is listed by `GET /assets`.

With `proxies`, the scan job queues one `proxy` job per newly added video and then finishes: it does NOT wait for
them. Its result lists them as `proxy_jobs`, `[{asset, job_id}]` (follow each through `/jobs/{id}` or `/events`). The CLI
(`library scan --proxies`) and MCP (`scan_library` with `proxies`) are not jobs, so they make the proxies
before returning and put the finished per-asset results under `proxies`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from split_video.editor.context import RequestContext, make_context_dependency
from split_video.job_work import library_scan_work
from split_video.jobs import LIBRARY_SCAN, JobQueue
from split_video.media_store import MediaNotFoundError, MediaOutsideRootError
from split_video.store_provider import StoreProvider


class LibraryScanRequest(BaseModel):
    folder: str = ""  # relative to the browse root; empty scans the whole workspace
    # Also queue a `proxy` job (proxy + thumbnail strip) for each newly added video. Off by default: it is
    # minutes of ffmpeg per hour of video. The scan job's result then lists their ids as `proxy_jobs`, and the scan
    # job is done before they are (the CLI and MCP make the proxies inline instead and return the results).
    proxies: bool = False


class LibraryScanStartResponse(BaseModel):
    job_id: str


def make_router(provider: StoreProvider, jobs: JobQueue) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    @router.post("/library/scan", response_model=LibraryScanStartResponse)
    def scan_library(
        request: LibraryScanRequest | None = None, ctx: RequestContext = ctx_dep
    ) -> LibraryScanStartResponse:
        """Queue a scan of `folder`: every new or changed media file under it is probed once and recorded
        as an asset (see `GET /assets`). Files already recorded are not probed again. The job's result is
        `{folder, total, added, copies, unchanged, skipped}`. With `proxies` it also has `proxy_jobs` (the ids of
        the `proxy` jobs queued for the new videos); the scan job finishes without waiting for them."""
        folder = request.folder if request else ""
        proxies = bool(request and request.proxies)
        try:
            start = ctx.media.resolve(folder)
            ctx.media.list_dir(start)
        except MediaOutsideRootError:
            raise HTTPException(status_code=400, detail="folder escapes the browse root") from None
        except MediaNotFoundError:
            raise HTTPException(status_code=404, detail=f"folder not found: {folder}") from None
        job_id = jobs.submit(
            LIBRARY_SCAN,
            library_scan_work(ctx.media, start, jobs=jobs if proxies else None, owner=ctx.principal.id),
            label=start or "workspace",
            owner=ctx.principal.id,
        )
        return LibraryScanStartResponse(job_id=job_id)

    return router
