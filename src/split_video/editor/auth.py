"""Request authentication for the HTTP API: who is calling, and the optional bearer token.

By default (no `SPLIT_VIDEO_TOKEN`) the server is in local mode: nothing is checked and every
request is `LOCAL_PRINCIPAL`. With a token configured every request under `/api/` and `/media/`
must carry `Authorization: Bearer <token>`, or it gets a 401. The check is an ASGI middleware over
those two prefixes (`AuthMiddleware`), so a route added later cannot forget it. The static
frontend, `/openapi.json` and the docs UI stay public.

Known limitation: browsers cannot attach an Authorization header to `<video src>` or
`EventSource`, and the bundled UI sends no token, so with a token set the bundled UI cannot use
the API. Token mode is for agents, API clients and hosted deployments.

An `Authenticator` decides who a request is (`None` = not authenticated). `create_app` takes
one, so tests and hosted deployments can supply their own; `TokenAuth` maps tokens to principals
(the env-based one has a single shared token, but several named tokens fit the same class).
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from fastapi import Depends, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from split_video.identity import LOCAL_PRINCIPAL, Principal

TOKEN_ENV = "SPLIT_VIDEO_TOKEN"
PROTECTED_PREFIXES = ("/api/", "/media/")
UNAUTHORIZED_ERROR = "unauthorized"

SECURITY_SCHEME = "bearerAuth"
SECURITY_DESCRIPTION = (
    "`Authorization: Bearer <token>`. Required only when the server is started with SPLIT_VIDEO_TOKEN set; "
    "without it the API is open (local single-user mode) and a stray header is ignored."
)


class UnauthorizedOut(BaseModel):
    detail: str
    error: str = UNAUTHORIZED_ERROR


class Unauthorized(Exception):
    """The request is not authenticated; the app answers 401."""


def unauthorized_response() -> JSONResponse:
    body = UnauthorizedOut(detail="Missing or invalid bearer token. Send `Authorization: Bearer <token>`.")
    return JSONResponse(status_code=401, content=body.model_dump(), headers={"WWW-Authenticate": "Bearer"})


UNAUTHORIZED_RESPONSES: dict[int | str, dict] = {
    401: {
        "model": UnauthorizedOut,
        "description": "No valid bearer token (only when the server is started with SPLIT_VIDEO_TOKEN).",
    }
}


@runtime_checkable
class Authenticator(Protocol):
    required: bool  # whether requests must authenticate (False: local mode)

    def authenticate(self, request: Request) -> Principal | None:
        """The principal making `request`, or None if it is not authenticated."""
        ...


class LocalAuth:
    """Local mode: no check; everyone is the local user."""

    required = False

    def authenticate(self, request: Request) -> Principal | None:
        return LOCAL_PRINCIPAL


def bearer_token(request: Request) -> str | None:
    """The token in `Authorization: Bearer <token>`, or None if absent, another scheme, or empty."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, rest = header.strip().partition(" ")
    token = rest.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


class TokenAuth:
    """Bearer-token check: `tokens` maps each accepted token to the principal it stands for."""

    required = True

    def __init__(self, tokens: Mapping[str, Principal]) -> None:
        if not tokens or any(not t for t in tokens):
            raise ValueError("TokenAuth needs at least one non-empty token")
        self._tokens = [(t.encode("utf-8"), p) for t, p in tokens.items()]

    @classmethod
    def single(cls, token: str) -> TokenAuth:
        """One shared token; whoever presents it is the `token` principal."""
        return cls({token: Principal(id="token", name="API token", kind="token")})

    def authenticate(self, request: Request) -> Principal | None:
        presented = bearer_token(request)
        if presented is None:
            return None
        candidate = presented.encode("utf-8")
        found: Principal | None = None
        for token, principal in self._tokens:  # no early exit: the time doesn't say which one matched
            if hmac.compare_digest(candidate, token) and found is None:
                found = principal
        return found


def authenticator_from_env(env: Mapping[str, str] | None = None) -> Authenticator:
    """`TokenAuth` if `SPLIT_VIDEO_TOKEN` is set to something (whitespace is stripped), else `LocalAuth`."""
    value = (os.environ if env is None else env).get(TOKEN_ENV, "").strip()
    return TokenAuth.single(value) if value else LocalAuth()


def is_protected(path: str) -> bool:
    return path.startswith(PROTECTED_PREFIXES) or path in ("/api", "/media")


class AuthMiddleware:
    """Authenticates every request under `/api/` and `/media/` before routing; 401 if it fails.

    On success the principal is on `request.state.principal`."""

    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        self.app = app
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_protected(scope["path"]):
            principal = self.authenticator.authenticate(Request(scope))
            if principal is None:
                await unauthorized_response()(scope, receive, send)
                return
            scope.setdefault("state", {})["principal"] = principal
        await self.app(scope, receive, send)


def current_principal(request: Request) -> Principal:
    """Dependency: the principal making this request (fails closed with 401 if none was resolved)."""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        principal = request.app.state.authenticator.authenticate(request)
        if principal is None:
            raise Unauthorized
        request.state.principal = principal
    return principal


# Use as `principal: Principal = principal_dependency` in route signatures.
principal_dependency = Depends(current_principal)
