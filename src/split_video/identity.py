"""Who is calling: the `Principal` every request is made on behalf of.

Local mode (the default) has exactly one principal, `LOCAL_PRINCIPAL`, and nothing is checked.
With an API token configured (see `editor/auth.py`) a request that presents it is the `token`
principal. `kind` is deliberately a closed `Literal` for now and will grow when hosted
deployments add real users (for example `"user"`); code that cares about the difference should
branch on `kind` rather than on `id`.

The principal's `id` is what jobs record as their `owner`, and what a `StoreProvider` (see
`store_provider.py`) keys the stores it hands out on. This module imports nothing else from the
engine and no web framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PrincipalKind = Literal["local", "token"]


@dataclass(frozen=True)
class Principal:
    id: str
    name: str | None = None
    kind: PrincipalKind = "local"


# The one user of a local, single-user install; also the owner of jobs nobody submitted on a request's behalf.
LOCAL_PRINCIPAL = Principal(id="local", name="Local user", kind="local")


def can_see_job(owner: str | None, principal: Principal) -> bool:
    """Whether `principal` may see and act on a job recorded with `owner`.

    A job with no owner (submitted outside any request, e.g. by in-process code) belongs to the
    local principal only."""
    if owner is None:
        return principal.kind == "local"
    return owner == principal.id
