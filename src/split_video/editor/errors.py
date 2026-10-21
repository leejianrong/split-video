"""HTTP rendering of the error catalogue (see `error_catalogue.py` and docs/errors.md).

`api_error(status, exc)` is the `HTTPException` a route raises for an engine failure: the status is the route's own
(unchanged), the body is the `detail` string the API always had plus the catalogue's `code`, `message`, `fix` and
`details`, the same payload the CLI and the MCP server give.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from split_video.error_catalogue import ErrorInfo, argument_error, describe

ERROR_RESPONSE = "The request failed; `code` is a stable code of the error catalogue (docs/errors.md) and `fix` says what to do next."


class ApiErrorOut(BaseModel):
    """The body of an error response (409 and 422 bodies of some routes add their own fields to these)."""

    detail: str  # the message, as every error response has always had it
    code: str  # stable, namespaced: clip.overlap (docs/errors.md)
    message: str
    fix: str  # a concrete next action
    details: dict[str, Any] | None = None  # structured data when there is some (a range, the valid choices)


class ApiError(HTTPException):
    def __init__(self, status_code: int, info: ErrorInfo) -> None:
        super().__init__(status_code=status_code, detail=info.message)
        self.info = info


def api_error(status_code: int, exc: BaseException) -> ApiError:
    return ApiError(status_code, describe(exc))


def body_of(info: ErrorInfo) -> dict[str, Any]:
    return {"detail": info.message, **info.to_dict()}


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=body_of(exc.info), headers=exc.headers)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's own 422 for a query, path or body that does not match the route's schema: its `detail` list stays
    as it was; `code` (`request.invalid`), `message`, `fix` and `details` are added."""
    problems = []
    for item in exc.errors():
        where, *rest = item["loc"]
        problems.append(
            {
                "argument": ".".join(str(part) for part in rest) or str(where),
                "in": str(where),
                "problem": item["msg"],
                "type": item["type"],
            }
        )
    info = argument_error(f"{request.method} {request.url.path}", problems)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors()), **info.to_dict()})
