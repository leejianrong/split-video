"""HTTP routes for edit plans (see `edit_plan.py` and docs/edit-plan.md).

`PUT /compose/projects/{name}/plan` creates or replaces the timeline project `name` from one JSON plan, atomically;
`?dry_run=true` validates and describes the result without writing. `If-Match` carries the revision the plan was
based on (the same header as every other write; the plan's own `base_revision` is the alternative, and they must
agree). A plan with any problem answers 422 with the full report (`errors`, each with `path`, `code`, `message`,
`hint`) and changes nothing; a stale revision answers 409 like any other write. `GET /schemas/edit-plan` serves the
JSON Schema of the plan format. The CLI (`apply-plan`, `plan-schema`) and MCP (`apply_plan`, `get_edit_plan_schema`)
return the same JSON.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from split_video import edit_plan
from split_video.editor.context import RequestContext, make_context_dependency
from split_video.editor.revisions import WRITE_RESPONSES, base_revision
from split_video.store_provider import StoreProvider


class PlanIssueOut(BaseModel):
    path: str  # JSON pointer into the plan
    code: str  # stable, namespaced: plan.clip.out_of_range
    message: str
    hint: str


class PlanTrackOut(BaseModel):
    id: str
    kind: str
    clips: int
    end: float


class PlanResultOut(BaseModel):
    project: str
    revision: int  # the new revision (dry run: the one it would get)
    action: str  # created | replaced
    dry_run: bool
    clips: int
    duration: float
    tracks: list[PlanTrackOut]
    warnings: list[PlanIssueOut]
    plan_hash: str  # sha256 of the canonical plan, `meta` excluded
    meta: dict[str, Any] | None = None  # the plan's own `meta`, echoed


class PlanInvalidOut(BaseModel):
    error: str = "plan_invalid"
    detail: str
    ok: bool = False
    errors: list[PlanIssueOut]
    warnings: list[PlanIssueOut]
    plan_hash: str | None


PLAN_RESPONSES: dict[int | str, dict] = {
    **WRITE_RESPONSES,
    422: {
        "model": PlanInvalidOut,
        "description": "The plan has problems; every one is in `errors`. Nothing was written.",
    },
}


def invalid_response(exc: edit_plan.PlanInvalidError) -> JSONResponse:
    count = len(exc.report.errors)
    detail = f"the plan has {count} problem{'s' if count != 1 else ''}; nothing was written"
    return JSONResponse(status_code=422, content={"error": "plan_invalid", "detail": detail, **exc.report.to_dict()})


def with_project(plan: Any, name: str) -> Any:
    """`plan` addressed to the project `name` of the URL: its `project` is filled in when absent. A different
    `project` in the body is a `PlanInvalidError`, since the URL and the document would disagree."""
    if not isinstance(plan, dict) or "project" not in plan:
        return {**plan, "project": name} if isinstance(plan, dict) else plan
    if plan["project"] != name:
        report = edit_plan.PlanReport()
        report.error(
            "/project",
            "plan.project.mismatch",
            f"the plan is for project {plan['project']!r} but the URL names {name!r}",
            "make them the same, or leave `project` out of the body",
        )
        raise edit_plan.PlanInvalidError(report)
    return plan


def make_router(provider: StoreProvider) -> APIRouter:
    router = APIRouter()
    ctx_dep = Depends(make_context_dependency(provider))

    @router.put(
        "/compose/projects/{name}/plan",
        response_model=PlanResultOut,
        response_model_exclude_none=True,
        responses=PLAN_RESPONSES,
    )
    def apply_plan(
        name: str,
        plan: Annotated[Any, Body(description="An edit plan; GET /api/v1/schemas/edit-plan is its JSON Schema.")],
        dry_run: bool = False,
        base: int | None = Depends(base_revision),
        ctx: RequestContext = ctx_dep,
    ) -> Any:
        """Create or replace the timeline project `name` from ONE JSON edit plan (sources, in/out, order, transitions,
        text, tracks), atomically: the whole project is built in memory and saved once, and a plan with any problem
        changes nothing. The body is validated against the plan schema served at `GET /api/v1/schemas/edit-plan`
        (strict: unknown fields are errors). Mode `create` (the default) fails if the project exists; mode `replace`
        needs `If-Match` (or `base_revision` in the plan), or `replace_unconditionally: true`. With `dry_run=true`
        nothing is written and the result shows what would happen. 422 carries every problem at once."""
        try:
            return edit_plan.apply_plan(
                ctx.media, ctx.store, with_project(plan, name), dry_run=dry_run, base_revision=base
            )
        except edit_plan.PlanInvalidError as exc:
            return invalid_response(exc)

    @router.get("/schemas/edit-plan")
    def edit_plan_schema() -> dict[str, Any]:
        """The JSON Schema (draft 2020-12) of an edit plan, `schema_version` 1. The same document is committed as
        docs/schemas/edit-plan.v1.schema.json; docs/edit-plan.md explains the format and the error codes."""
        return edit_plan.schema()

    return router
