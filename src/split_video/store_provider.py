"""The stores to use for a given principal.

Routes ask a `StoreProvider` for the `ProjectStore` and `MediaStore` of the principal making the
request instead of holding one global pair. Local mode uses `SharedStoreProvider`: the same two
stores for everyone (there is only one user). A hosted deployment supplies a provider that
returns per-user (or per-tenant) stores.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from split_video.identity import Principal
from split_video.media_store import MediaStore
from split_video.store import ProjectStore


@runtime_checkable
class StoreProvider(Protocol):
    def project_store(self, principal: Principal) -> ProjectStore: ...

    def media_store(self, principal: Principal) -> MediaStore: ...


class SharedStoreProvider:
    """The same stores for every principal."""

    def __init__(self, store: ProjectStore, media: MediaStore) -> None:
        self._store = store
        self._media = media

    def project_store(self, principal: Principal) -> ProjectStore:
        return self._store

    def media_store(self, principal: Principal) -> MediaStore:
        return self._media
