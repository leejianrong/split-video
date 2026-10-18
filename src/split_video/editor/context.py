"""What a route needs about the caller: the principal and the stores for them."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from split_video.editor.auth import principal_dependency
from split_video.identity import Principal
from split_video.media_store import MediaStore
from split_video.store import ProjectStore
from split_video.store_provider import StoreProvider


@dataclass(frozen=True)
class RequestContext:
    principal: Principal
    store: ProjectStore
    media: MediaStore


def make_context_dependency(provider: StoreProvider) -> Callable[..., RequestContext]:
    def request_context(principal: Principal = principal_dependency) -> RequestContext:
        return RequestContext(principal, provider.project_store(principal), provider.media_store(principal))

    return request_context
