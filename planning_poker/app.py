from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import domain
from .config import Settings
from .domain import DomainError
from .hub import Hub
from .service import RoomService
from .store import RoomStore, build_store


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
AUTH_TIMEOUT_SECONDS = 10

logger = logging.getLogger("planning_poker")


def create_app(settings: Settings | None = None, store: RoomStore | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    store = store or build_store(settings)
    service = RoomService(store, settings)
    hub = Hub()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        listener = asyncio.create_task(hub.run(store), name="room-events")
        logger.info(
            "Planning Poker ready (store=%s, room_ttl=%ss, session_ttl=%ss)",
            store.name,
            settings.room_ttl,
            settings.session_ttl,
        )
        try:
            yield
        finally:
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass
            await store.close()

    app = FastAPI(title="Planning Poker", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.service = service
    app.state.hub = hub
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def error_response(error: DomainError) -> JSONResponse:
        return JSONResponse({"error": error.code, "message": error.message}, status_code=error.status)

    def session_response(room, participant) -> JSONResponse:
        return JSONResponse(
            {"code": room.code, "participantId": participant.id, "token": participant.token},
            status_code=201,
        )

    async def read_json(request: Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - malformed body
            raise DomainError("invalid_json", "Send a valid JSON request.") from None
        if not isinstance(payload, dict):
            raise DomainError("invalid_json", "Send a valid JSON object.")
        return payload

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        healthy = await store.ping()
        body = {"status": "ok" if healthy else "degraded", "store": store.name}
        return JSONResponse(body, status_code=200 if healthy else 503)

    @app.get("/")
    async def home() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/room/{code}")
    async def room_page(code: str) -> FileResponse:
        return FileResponse(STATIC_DIR / "room.html")

    @app.post("/api/rooms")
    async def create_room(request: Request) -> JSONResponse:
        try:
            payload = await read_json(request)
            room, participant = await service.create_room(payload.get("nickname"), payload.get("task"))
            logger.info("Room %s created", room.code)
            return session_response(room, participant)
        except DomainError as error:
            return error_response(error)

    @app.post("/api/rooms/{code}/join")
    async def join_room(code: str, request: Request) -> JSONResponse:
        try:
            payload = await read_json(request)
            room, participant = await service.join_room(code, payload.get("nickname"))
            return session_response(room, participant)
        except DomainError as error:
            return error_response(error)

    async def emit_error(socket: WebSocket, error: DomainError) -> None:
        try:
            await socket.send_json({"type": "error", "code": error.code, "message": error.message})
        except Exception:  # noqa: BLE001 - client already gone
            pass

    async def authenticate(socket: WebSocket, code: str, connection_id: str):
        """The session token travels in the first frame, never in the URL, so it
        cannot end up in proxy or access logs."""
        try:
            message = await asyncio.wait_for(socket.receive_json(), AUTH_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, ValueError):
            raise DomainError("unauthorized", "Send your session token first.", 401) from None
        if not isinstance(message, dict) or message.get("type") != "auth":
            raise DomainError("unauthorized", "Send your session token first.", 401)
        return await service.connect(code, message.get("token"), connection_id)

    async def heartbeat_loop(socket: WebSocket, code: str, participant_id: str, connection_id: str) -> None:
        """Keep this connection's presence lease alive. If it cannot be renewed
        (expired room or session) the socket is closed so the client reconnects."""
        while True:
            await asyncio.sleep(settings.heartbeat_interval)
            try:
                await service.heartbeat(code, participant_id, connection_id)
            except DomainError as error:
                logger.info("Heartbeat stopped for room %s: %s", code, error.code)
                await emit_error(socket, error)
                try:
                    await socket.close(code=1008)
                except RuntimeError:
                    pass
                return

    @app.websocket("/ws/{code}")
    async def websocket_room(socket: WebSocket, code: str) -> None:
        await socket.accept()
        connection_id = token_urlsafe(8)
        room = participant = heartbeat = None
        try:
            room, participant = await authenticate(socket, code, connection_id)
            # Register before the first send so no update can slip through the gap;
            # the version guard keeps the client from rendering an older state.
            await hub.register(room.code, socket, room.version)
            await socket.send_json({"type": "welcome", "participantId": participant.id})
            await socket.send_json({"type": "state", "state": service.snapshot(room)})
            heartbeat = asyncio.create_task(heartbeat_loop(socket, room.code, participant.id, connection_id))
            while True:
                message = await socket.receive_json()
                if not isinstance(message, dict):
                    await emit_error(socket, DomainError("invalid_message", "Message must be an object."))
                    continue
                action = message.get("type")
                try:
                    if action == "vote":
                        await service.vote(room.code, participant.id, message.get("value"))
                    elif action == "reveal":
                        await service.reveal(room.code, participant.id)
                    elif action == "new_round":
                        await service.new_round(room.code, participant.id, message.get("task"), message.get("historyEntryId"))
                    elif action == "finalize_round":
                        await service.finalize_round(room.code, participant.id, message.get("effort"))
                    elif action == "remove_history_entry":
                        await service.remove_history_entry(room.code, participant.id, message.get("historyEntryId"))
                    elif action == "rename_task":
                        await service.rename_task(room.code, participant.id, message.get("task"))
                    elif action == "claim_host":
                        await service.claim_host(room.code, participant.id)
                    elif action == "kick":
                        await service.kick(room.code, participant.id, message.get("participantId"))
                    elif action == "ping":
                        await socket.send_json({"type": "pong"})
                    else:
                        raise DomainError("unknown_action", "Unknown action.")
                except DomainError as error:
                    await emit_error(socket, error)
        except DomainError as error:
            await emit_error(socket, error)
            try:
                await socket.close(code=1008)
            except RuntimeError:
                pass
        except (WebSocketDisconnect, ValueError, RuntimeError):
            pass
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            if room is not None and participant is not None:
                await hub.unregister(room.code, socket)
                await service.disconnect(room.code, participant.id, connection_id)

    return app


app = create_app()
