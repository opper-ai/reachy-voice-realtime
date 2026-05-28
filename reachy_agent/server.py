"""Tiny FastAPI sidecar for observability.

Endpoints:
  GET /                — static UI page
  GET /api/stream      — SSE: transcripts, tool calls, state changes
  GET /api/frame       — latest JPEG bytes (from Eyes.last_jpeg)

The agent posts to publish(event) from its main loop; this server fans events
out to all connected SSE clients.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, StreamingResponse

from .vision import Eyes

STATIC_DIR = Path(__file__).parent / "static"


class UIServer:
    def __init__(self, eyes: Eyes, on_shutdown: Optional[Any] = None):
        """
        on_shutdown: a callable (e.g. asyncio.Event.set or a sync function)
                     that triggers graceful agent shutdown. Bound to
                     POST /api/shutdown from the UI button.
        """
        self._eyes = eyes
        self._on_shutdown = on_shutdown
        self._subscribers: list[asyncio.Queue] = []
        self.app = FastAPI()
        self._wire()

    def publish(self, event: dict[str, Any]) -> None:
        """Fan-out event to all SSE subscribers. Safe from any async context."""
        for q in self._subscribers:
            # Drop oldest if subscriber is slow.
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except asyncio.QueueEmpty:
                    pass

    def _wire(self) -> None:
        app = self.app

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        @app.get("/api/frame")
        async def frame() -> Response:
            jpeg = self._eyes.last_jpeg
            if not jpeg:
                return Response(status_code=204)
            return Response(
                content=jpeg,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )

        @app.post("/api/shutdown")
        async def shutdown() -> dict:
            self.publish({"type": "state", "value": "disconnecting"})
            if self._on_shutdown is not None:
                try:
                    self._on_shutdown()
                except Exception as e:
                    return {"ok": False, "error": str(e)}
            return {"ok": True}

        @app.get("/api/stream")
        async def stream() -> StreamingResponse:
            q: asyncio.Queue = asyncio.Queue(maxsize=200)
            self._subscribers.append(q)

            async def gen():
                try:
                    while True:
                        ev = await q.get()
                        yield f"data: {json.dumps(ev)}\n\n"
                finally:
                    if q in self._subscribers:
                        self._subscribers.remove(q)

            return StreamingResponse(gen(), media_type="text/event-stream")
