"""Run the editor app under uvicorn so that open event streams can't hold up shutdown.

uvicorn's graceful shutdown waits for every in-flight response to finish before it runs the app's
lifespan shutdown. An SSE stream never finishes by itself, so `Server.shutdown` is wrapped to end
all event streams first (the app's `EventBus`, `app.state.events`); `timeout_graceful_shutdown` is
the backstop for anything else still open.
"""

from __future__ import annotations

import socket
from typing import Any

import uvicorn

GRACEFUL_SHUTDOWN_SECONDS = 5


class EditorServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, app: Any) -> None:
        super().__init__(config)
        self._editor_app = app

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        bus = getattr(getattr(self._editor_app, "state", None), "events", None)
        if bus is not None:
            bus.close()
        await super().shutdown(sockets=sockets)


def make_server(app: Any, host: str, port: int, *, log_level: str = "warning") -> EditorServer:
    config = uvicorn.Config(
        app, host=host, port=port, log_level=log_level, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS
    )
    return EditorServer(config, app)


def serve(app: Any, host: str, port: int) -> None:
    make_server(app, host, port).run()
