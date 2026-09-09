"""Local websocket fan-out.

The hub knows only about the sockets connected to *this* process. Room updates
arrive from the store's pub/sub channel, so an action performed on any instance
reaches every participant exactly the same way.

Each socket carries the version of the last state it was given: a socket that
was handed a fresh snapshot on connect will not be sent the same version again,
while everyone else still receives it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol


logger = logging.getLogger(__name__)


class Socket(Protocol):
    async def send_json(self, data: Any) -> None: ...


class Hub:
    def __init__(self) -> None:
        self._rooms: dict[str, dict[Socket, int]] = {}
        self._lock = asyncio.Lock()

    async def register(self, code: str, socket: Socket, version: int) -> None:
        async with self._lock:
            self._rooms.setdefault(code, {})[socket] = version

    async def unregister(self, code: str, socket: Socket) -> None:
        async with self._lock:
            sockets = self._rooms.get(code)
            if sockets is None:
                return
            sockets.pop(socket, None)
            if not sockets:
                self._rooms.pop(code, None)

    def connection_count(self, code: str) -> int:
        return len(self._rooms.get(code, ()))

    async def dispatch(self, event: dict[str, Any]) -> None:
        """Deliver one store event to the local sockets that have not seen it."""
        code = event.get("code")
        state = event.get("state")
        version = event.get("version")
        if not isinstance(code, str) or not isinstance(state, dict) or not isinstance(version, int):
            logger.warning("Ignoring malformed room event")
            return
        async with self._lock:
            sockets = self._rooms.get(code)
            if not sockets:
                return
            targets = [socket for socket, seen in sockets.items() if seen < version]
            for socket in targets:
                sockets[socket] = version
        message = {"type": "state", "state": state}
        for socket in targets:
            try:
                await socket.send_json(message)
            except Exception:  # noqa: BLE001 - the socket is gone, drop it
                await self.unregister(code, socket)

    async def run(self, store) -> None:
        """Consume the store's event stream until cancelled."""
        try:
            async for event in store.events():
                await self.dispatch(event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never take the app down with it
            logger.exception("Room event listener stopped unexpectedly")
