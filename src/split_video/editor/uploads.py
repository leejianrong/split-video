"""HTTP route for importing a file from the browser: `POST /assets/upload?name=&folder=`.

A browser cannot give the server a path, so a file dropped on the editor (or chosen with its file picker) is sent as
the raw request body and written into the workspace through the `MediaStore` (`media_import.py` holds the rules):
a plain media file name, a free name in `folder` (`gig (2).mp4` if `gig.mp4` exists), probed and registered as an
asset. The answer is the asset (`GET /assets/{id}`), `201`. A name or type that is not accepted is 400 or 415 and an
unreadable file 422, all with the catalogue's `code` and `fix`, and nothing is left on disk. The body is streamed in
chunks, never held whole; the request is the progress (a browser reports upload progress itself). Files that are
already in the workspace need no upload: `POST /assets/register` adds them by path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from split_video import assets, media_import
from split_video.editor.compose import AssetOut, asset_out
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.errors import api_error
from split_video.media_store import MediaError, MediaOutsideRootError
from split_video.store_provider import StoreProvider


def make_router(provider: StoreProvider) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    @router.post("/assets/upload", response_model=AssetOut, status_code=201)
    async def upload_asset(
        request: Request,
        name: str = Query(..., description="The file's name, for example `gig.mp4`; folders in it are dropped."),
        folder: str = Query("", description="The workspace folder to put it in; empty is the workspace root."),
        ctx: RequestContext = ctx_dep,
    ) -> AssetOut:
        """Receive the raw body as a media file named `name` in `folder` and register it as an asset. Send the body
        as is (`Content-Type: application/octet-stream`), not as a multipart form."""
        try:
            incoming = await run_in_threadpool(media_import.IncomingFile, ctx.media, folder, name)
        except media_import.ImportError_ as exc:
            raise api_error(415 if exc.error_code == "upload.unsupported_type" else 400, exc) from None
        except MediaOutsideRootError:
            raise api_error(400, ValueError("folder escapes the browse root")) from None
        except MediaError as exc:
            raise api_error(404, exc) from None

        def describe(asset: assets.Asset) -> AssetOut:
            with assets.open_book(ctx.media) as book:
                return asset_out(book, book.require(asset.id))

        await run_in_threadpool(incoming.__enter__)
        try:
            async for chunk in request.stream():
                if chunk:
                    await run_in_threadpool(incoming.write, chunk)
            asset = await run_in_threadpool(incoming.finish)
            result = await run_in_threadpool(describe, asset)
        except media_import.ImportError_ as exc:
            raise api_error(422 if exc.error_code == "media.unreadable" else 400, exc) from None
        finally:
            await run_in_threadpool(incoming.__exit__, None, None, None)
        return result

    return router
