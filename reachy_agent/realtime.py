"""Opper realtime WS client wrapper.

Mints a session ticket, opens the WebSocket, exposes async `send` for outbound
events and an `events()` async iterator for inbound events.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx
import websockets

OPPER_BASE_URL = os.environ.get("OPPER_BASE_URL", "https://api.opper.ai")


async def mint_ticket(
    config: dict[str, Any], ttl_seconds: int = 60, *, api_key: str | None = None
) -> dict[str, Any]:
    key = api_key or os.environ.get("OPPER_API_KEY")
    if not key:
        raise RuntimeError("no Opper API key supplied")

    body = {"config": config, "ttl_seconds": ttl_seconds}

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{OPPER_BASE_URL}/v3/realtime-sessions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"mint failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _ws_url_from_ticket(ticket: dict[str, Any]) -> str:
    ws_url = ticket.get("ws_url")
    if not ws_url:
        base = OPPER_BASE_URL.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{base.rstrip('/')}/v3/realtime"
    return f"{ws_url}?ticket={ticket['client_secret']}"


class Realtime:
    """Thin async wrapper over the Opper realtime WS."""

    def __init__(self, ws: websockets.ClientConnection):
        self._ws = ws

    @classmethod
    async def connect(cls, ticket: dict[str, Any]) -> "Realtime":
        ws = await websockets.connect(
            _ws_url_from_ticket(ticket), max_size=16 * 1024 * 1024
        )
        rt = cls(ws)
        await rt.send({"type": "session.start", "config": {}})
        return rt

    async def send(self, event: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(event))

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        async for raw in self._ws:
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue

    async def close(self) -> None:
        await self._ws.close()
