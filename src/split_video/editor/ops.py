"""HTTP routes for edit-operation batches (see `edit_ops.py` and docs/edit-ops.md).

`POST /compose/projects/{name}/ops` applies a list of timeline edits to the existing project `name`, atomically: all of
them or none, saved once. `?dry_run=true` runs the same engine and answers with the resulting project and any errors
without writing anything (not the project, not the asset table). `If-Match` carries the revision the batch was written
against (the plan's header; `base_revision` in the body is the alternative and they must agree); a stale one answers 409
like every write. A batch with any problem answers 422 with the full report (`errors` with `path`, `code`, `message`,
`hint`, plus `applied` and per-op `results`) and changes nothing. `GET /schemas/edit-ops` serves the JSON Schema. The CLI
(`apply-ops`, `ops-schema`) and MCP (`apply_ops`, `get_edit_ops_schema`) return the same JSON.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from split_video import edit_ops
from split_video.editor.compose import _http as http_error
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.plan import PlanIssueOut, PlanTrackOut
from split_video.editor.revisions import WRITE_RESPONSES, base_revision
from split_video.errors import RevisionConflictError
from split_video.store_provider import StoreProvider
from split_video.timeline import TimelineError


class OpResultOut(BaseModel):
    index: int  # position in the batch: `/ops/<index>`
    ok: bool
    summary: str
    ids: dict[str, str]  # the clip / track the op touched (for a creating op: the id it made)
    code: str | None = None  # failed ops: the error code


class OpsResultOut(BaseModel):
    project: str
    revision: int  # the new revision (dry run: the one it would get, a prediction)
    action: str  # edited
    dry_run: bool
    applied: int
    ops: int
    clips: int
    duration: float
    tracks: list[PlanTrackOut]
    results: list[OpResultOut]
    warnings: list[PlanIssueOut]
    ops_hash: str  # sha256 of the canonical document, `meta` excluded
    timeline: dict[str, Any] | None = None  # dry run: the whole project, as `GET /compose/projects/{name}`
    meta: dict[str, Any] | None = None  # the document's own `meta`, echoed


class OpsInvalidOut(BaseModel):
    error: str = "ops_invalid"
    detail: str
    ok: bool = False
    errors: list[PlanIssueOut]
    warnings: list[PlanIssueOut]
    ops_hash: str | None
    dry_run: bool
    applied: int  # ops that applied in memory before the batch stopped (nothing was written)
    results: list[OpResultOut]
    timeline: dict[str, Any] | None = None  # dry run: the project after the ops that applied


OPS_RESPONSES: dict[int | str, dict] = {
    **WRITE_RESPONSES,
    422: {
        "model": OpsInvalidOut,
        "description": "The batch has problems; they are in `errors`. Nothing was written.",
    },
}


def with_project(doc: Any, name: str) -> Any:
    """`doc` addressed to the project `name` of the URL: `project` is filled in when absent, and a different one is an
    `OpsInvalidError` (the URL and the document would disagree)."""
    if not isinstance(doc, dict):
        return doc
    if "project" not in doc:
        return {**doc, "project": name}
    if doc["project"] != name:
        report = edit_ops.OpsReport()
        report.error(
            "/project",
            "ops.project.mismatch",
            f"the document is for project {doc['project']!r} but the URL names {name!r}",
            "make them the same, or leave `project` out of the body",
        )
        raise edit_ops.OpsInvalidError(report)
    return doc


def invalid_response(exc: edit_ops.OpsInvalidError) -> JSONResponse:
    return JSONResponse(status_code=422, content=edit_ops.invalid_body(exc.report))


def make_router(provider: StoreProvider) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    @router.post(
        "/compose/projects/{name}/ops",
        response_model=OpsResultOut,
        response_model_exclude_none=True,
        responses=OPS_RESPONSES,
    )
    def apply_ops(
        name: str,
        ops: Annotated[Any, Body(description="An ops document; GET /api/v1/schemas/edit-ops is its JSON Schema.")],
        dry_run: bool = False,
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> Any:
        """Apply a batch of timeline edits (add_clip, trim_clip, move_clip, remove_clip, set_transition,
        set_clip_properties, add_text_clip, set_text_properties, add_track, remove_track, move_track, place_clip) to the
        existing project `name`, atomically: every op or none, saved once. Clips and tracks are addressed by id; a
        creating op may be given an `id` that later ops in the batch use. The body is validated against
        `GET /api/v1/schemas/edit-ops` (strict). With `dry_run=true` nothing is written and the result carries the
        resulting project as `timeline`. The first op the project refuses stops the batch; `continue_on_error: true`
        in the body (dry run only) collects every independent problem. 422 carries the report, 409 a stale
        revision (`If-Match`)."""
        try:
            return edit_ops.apply_ops(
                ctx.media, ctx.store, with_project(ops, name), dry_run=dry_run, base_revision=base
            )
        except edit_ops.OpsInvalidError as exc:
            return invalid_response(exc)
        except RevisionConflictError:
            raise  # the app's handler answers it: 409 with the current revision
        except TimelineError as exc:  # no such project
            raise http_error(exc) from None

    @router.get("/schemas/edit-ops")
    def edit_ops_schema() -> dict[str, Any]:
        """The JSON Schema (draft 2020-12) of an ops document, `schema_version` 1. The same document is committed as
        docs/schemas/edit-ops.v1.schema.json; docs/edit-ops.md explains the format and the error codes."""
        return edit_ops.schema()

    return router
