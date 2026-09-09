"""Application service: applies domain rules on top of a store.

Every mutation follows the same shape — load, prune, apply, compare-and-set,
announce. Any instance can serve any request, and a conflicting write is simply
replayed against the fresh state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from random import uniform
from typing import Any, Callable

from . import domain
from .config import Settings
from .domain import DomainError, Participant, Room
from .store import RoomStore


logger = logging.getLogger(__name__)

MAX_CAS_ATTEMPTS = 8
MAX_CODE_ATTEMPTS = 12

Mutation = Callable[[Room, float], None]


class RoomService:
    def __init__(self, store: RoomStore, settings: Settings, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.settings = settings
        self._now = clock

    # --- helpers ----------------------------------------------------------

    def snapshot(self, room: Room) -> dict[str, Any]:
        return domain.snapshot(room, self._now())

    async def _mutate(self, code: str, apply: Mutation, *, announce: bool = True) -> Room:
        """Load → apply → compare-and-set, retrying when another writer wins."""
        code = domain.normalise_code(code)
        for attempt in range(MAX_CAS_ATTEMPTS):
            room = await self.store.load(code)
            if room is None:
                raise DomainError("room_not_found", "This room does not exist.", 404)
            now = self._now()
            pruned = domain.prune(room, now, self.settings.session_ttl)
            apply(room, now)
            expected = room.version
            room.version = expected + 1
            room.updated_at = now
            if await self.store.save(room, expected, self.settings.room_ttl):
                if announce or pruned:
                    await self.store.publish(
                        {"code": room.code, "version": room.version, "state": domain.snapshot(room, now)}
                    )
                return room
            await asyncio.sleep(uniform(0.005, 0.02) * (attempt + 1))
        raise DomainError("room_busy", "The room is busy right now, please retry.", 503)

    # --- commands ---------------------------------------------------------

    async def create_room(self, nickname: Any, task: Any = None) -> tuple[Room, Participant]:
        clean_nickname = domain.validate_nickname(nickname)
        clean_task = domain.validate_task(task)
        for _ in range(MAX_CODE_ATTEMPTS):
            now = self._now()
            room, creator = domain.new_room(clean_nickname, clean_task, now)
            room.expires_at = now + self.settings.room_ttl
            if await self.store.create(room, self.settings.room_ttl):
                return room, creator
        raise DomainError("room_unavailable", "Could not allocate a room, please retry.", 503)

    async def join_room(self, code: str, nickname: Any) -> tuple[Room, Participant]:
        clean_nickname = domain.validate_nickname(nickname)
        joined: dict[str, Participant] = {}

        def apply(room: Room, now: float) -> None:
            joined["participant"] = domain.add_participant(
                room, clean_nickname, now, self.settings.max_participants
            )

        room = await self._mutate(code, apply, announce=False)
        return room, joined["participant"]

    async def connect(self, code: str, token: Any, connection_id: str) -> tuple[Room, Participant]:
        connected: dict[str, Participant] = {}

        def apply(room: Room, now: float) -> None:
            participant = domain.find_by_token(room, token)
            domain.connect(room, participant, connection_id, now, self.settings.connection_lease)
            connected["participant"] = participant

        room = await self._mutate(code, apply)
        return room, connected["participant"]

    async def heartbeat(self, code: str, participant_id: str, connection_id: str) -> None:
        def apply(room: Room, now: float) -> None:
            participant = room.participants.get(participant_id)
            if participant is None:
                raise DomainError("unauthorized", "Your room session expired.", 401)
            domain.heartbeat(room, participant, connection_id, now, self.settings.connection_lease)

        await self._mutate(code, apply, announce=False)

    async def disconnect(self, code: str, participant_id: str, connection_id: str) -> None:
        def apply(room: Room, now: float) -> None:
            participant = room.participants.get(participant_id)
            if participant is not None:
                domain.disconnect(room, participant, connection_id, now)

        try:
            await self._mutate(code, apply)
        except DomainError as error:
            # A room that expired or was already cleaned up is not a failure.
            if error.code not in {"room_not_found", "room_busy"}:
                raise
            logger.info("Skipping disconnect bookkeeping for %s: %s", code, error.code)

    async def vote(self, code: str, participant_id: str, value: Any) -> Room:
        return await self._mutate(code, lambda room, now: domain.vote(room, self._member(room, participant_id), value, now))

    async def reveal(self, code: str, participant_id: str) -> Room:
        return await self._mutate(code, lambda room, now: domain.reveal(room, self._member(room, participant_id), now))

    async def new_round(self, code: str, participant_id: str, task: Any, history_entry_id: Any = None) -> Room:
        def apply(room: Room, now: float) -> None:
            participant = self._member(room, participant_id)
            if history_entry_id is None:
                domain.new_round(room, participant, task, now)
            else:
                domain.reestimate_history(room, participant, history_entry_id, now)
        return await self._mutate(
            code, apply
        )

    async def finalize_round(self, code: str, participant_id: str, effort: Any) -> Room:
        return await self._mutate(
            code, lambda room, now: domain.finalize_round(room, self._member(room, participant_id), effort, now)
        )

    async def remove_history_entry(self, code: str, participant_id: str, entry_id: Any) -> Room:
        return await self._mutate(
            code, lambda room, now: domain.remove_history_entry(
                room, self._member(room, participant_id), entry_id, now
            )
        )

    async def rename_task(self, code: str, participant_id: str, task: Any) -> Room:
        return await self._mutate(
            code, lambda room, now: domain.rename_task(room, self._member(room, participant_id), task, now)
        )

    async def claim_host(self, code: str, participant_id: str) -> Room:
        return await self._mutate(
            code, lambda room, now: domain.claim_host(room, self._member(room, participant_id), now)
        )

    async def kick(self, code: str, host_id: str, target_id: Any) -> Room:
        return await self._mutate(
            code, lambda room, now: domain.kick(room, self._member(room, host_id), target_id, now)
        )

    @staticmethod
    def _member(room: Room, participant_id: str) -> Participant:
        participant = room.participants.get(participant_id)
        if participant is None:
            raise DomainError("unauthorized", "Your room session expired.", 401)
        return participant
