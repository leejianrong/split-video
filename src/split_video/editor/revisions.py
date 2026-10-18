"""How the HTTP API carries project revisions (see `store.py` for what they are).

One mechanism for every write route: the optional `If-Match` request header holds the revision the
client based its change on, as a bare or quoted integer (`If-Match: "3"`). If it is not the
project's current revision the route answers 409 with a `RevisionConflictOut` body and writes
nothing. Without the header the change is applied to the latest version (atomically, so it can't
lose another writer's change; it just isn't checked against what the client last saw). Every
project state response carries `revision`; a client keeps the latest one and sends it back.

Other 409s (export or render output already exists, project already exists) have no `error`
field in their body; a revision conflict always has `"error": "revision_conflict"`.
"""

from __future__ import annotations

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from split_video.errors import RevisionConflictError

REVISION_ERROR = "revision_conflict"

IF_MATCH_DESCRIPTION = (
    'The project revision this change is based on, as an integer, bare or quoted (`If-Match: "3"`). '
    "If the project has moved on, the request fails with 409 `revision_conflict` and nothing is written. "
    "Omit it to apply the change to the latest version."
)


class RevisionConflictOut(BaseModel):
    detail: str  # what happened and how to retry
    error: str = REVISION_ERROR
    current_revision: int  # the project's revision now: reload, re-apply, retry with If-Match set to this
    base_revision: int | None  # the revision the request said it was based on


# Declared on every write route so the schema documents the 409.
WRITE_RESPONSES: dict[int | str, dict] = {
    409: {
        "model": RevisionConflictOut,
        "description": (
            "`If-Match` didn't match the project's current revision (`error` is `revision_conflict`). "
            "Other 409s on some routes (output file or project already exists) have only `detail`."
        ),
    }
}


def base_revision(if_match: str | None = Header(default=None, alias="If-Match", description=IF_MATCH_DESCRIPTION)):
    """Dependency: the revision from `If-Match`, or None when absent (or `*`)."""
    if if_match is None:
        return None
    text = if_match.strip()
    if text == "*":
        return None
    text = text.removeprefix("W/").strip('"')
    if not text.isascii() or not text.isdigit():
        raise HTTPException(status_code=400, detail=f"If-Match must be a revision number, got {if_match!r}")
    return int(text)


def conflict_response(exc: RevisionConflictError) -> JSONResponse:
    body = RevisionConflictOut(
        detail=f"{exc} (Send the revision as the If-Match header.)",
        current_revision=exc.current,
        base_revision=exc.base,
    )
    return JSONResponse(status_code=409, content=body.model_dump())
