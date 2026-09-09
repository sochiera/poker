"""Room storage backends.

A room is stored as one JSON document keyed by its code, and every write is a
compare-and-set on the room's `version`. That is enough to keep several app
processes (or containers) consistent without any locking protocol: a losing
writer simply reloads and replays its change.

State changes are announced on a single pub/sub channel so a participant
connected to instance A sees what someone on instance B just did.

Two backends are available:

* `RedisStore`   — shared, survives an app restart, expires by TTL. Production.
* `MemoryStore`  — no dependencies, single process. Local dev and tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from .domain import Room, room_from_dict, room_to_dict


logger = logging.getLogger(__name__)

Event = dict[str, Any]

#: Compare-and-set: only overwrite the room when its stored version is the one
#: we based our change on. Returns 1 on success, 0 on conflict, -1 if gone.
_CAS_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return -1 end
if tonumber(cjson.decode(raw)['version']) ~= tonumber(ARGV[1]) then return 0 end
local remaining = redis.call('PTTL', KEYS[1])
if remaining <= 0 then return -1 end
redis.call('SET', KEYS[1], ARGV[2], 'PX', remaining)
return 1
"""


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


class RoomStore:
    """Interface implemented by both backends."""

    name = "store"

    async def create(self, room: Room, ttl: int) -> bool:
        """Store a new room; False when the code is already taken."""
        raise NotImplementedError

    async def load(self, code: str) -> Room | None:
        raise NotImplementedError

    async def save(self, room: Room, expected_version: int, ttl: int) -> bool:
        """Compare-and-set; False when another writer got there first."""
        raise NotImplementedError

    async def publish(self, event: Event) -> None:
        raise NotImplementedError

    def events(self) -> AsyncIterator[Event]:
        raise NotImplementedError

    async def ping(self) -> bool:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class MemoryStore(RoomStore):
    name = "memory"

    def __init__(self, clock=time.time) -> None:
        self._rooms: dict[str, tuple[str, float]] = {}
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._lock = asyncio.Lock()
        self._clock = clock

    def _get(self, code: str) -> dict[str, Any] | None:
        entry = self._rooms.get(code)
        if entry is None:
            return None
        raw, expires_at = entry
        if expires_at <= self._clock():
            self._rooms.pop(code, None)
            return None
        return json.loads(raw)

    async def create(self, room: Room, ttl: int) -> bool:
        async with self._lock:
            if self._get(room.code) is not None:
                return False
            expires_at = room.expires_at or self._clock() + ttl
            room.expires_at = expires_at
            self._rooms[room.code] = (_dumps(room_to_dict(room)), expires_at)
            return True

    async def load(self, code: str) -> Room | None:
        async with self._lock:
            data = self._get(code)
        return None if data is None else room_from_dict(data)

    async def save(self, room: Room, expected_version: int, ttl: int) -> bool:
        async with self._lock:
            current = self._get(room.code)
            if current is None or current["version"] != expected_version:
                return False
            # Never turn an activity write into a lease renewal.
            _, expires_at = self._rooms[room.code]
            if expires_at <= self._clock():
                self._rooms.pop(room.code, None)
                return False
            room.expires_at = expires_at
            self._rooms[room.code] = (_dumps(room_to_dict(room)), expires_at)
            return True

    async def publish(self, event: Event) -> None:
        for queue in list(self._subscribers):
            queue.put_nowait(json.loads(_dumps(event)))

    async def events(self) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    async def ping(self) -> bool:
        return True


class RedisStore(RoomStore):
    name = "redis"

    def __init__(self, url: str, prefix: str = "pp") -> None:
        from redis.asyncio import Redis  # imported lazily: optional for local dev

        self._url = url
        self._prefix = prefix
        self._redis = Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        # Pub/Sub connections block while waiting for the next event. They must
        # not inherit the command client's read timeout or an idle room causes
        # the subscription to reconnect every few seconds and miss messages.
        self._events_redis = Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=None,
            socket_connect_timeout=5,
        )
        self._cas = self._redis.register_script(_CAS_SCRIPT)

    def _key(self, code: str) -> str:
        return f"{self._prefix}:room:{code}"

    @property
    def _channel(self) -> str:
        return f"{self._prefix}:events"

    async def create(self, room: Room, ttl: int) -> bool:
        if not room.expires_at:
            room.expires_at = time.time() + ttl
        remaining = max(1, int((room.expires_at - time.time()) * 1000))
        stored = await self._redis.set(self._key(room.code), _dumps(room_to_dict(room)), nx=True, px=remaining)
        return bool(stored)

    async def load(self, code: str) -> Room | None:
        raw = await self._redis.get(self._key(code))
        return None if raw is None else room_from_dict(json.loads(raw))

    async def save(self, room: Room, expected_version: int, ttl: int) -> bool:
        result = await self._cas(
            keys=[self._key(room.code)],
            args=[expected_version, _dumps(room_to_dict(room))],
        )
        return int(result) == 1

    async def publish(self, event: Event) -> None:
        await self._redis.publish(self._channel, _dumps(event))

    async def events(self) -> AsyncIterator[Event]:
        while True:
            pubsub = self._events_redis.pubsub(ignore_subscribe_messages=True)
            try:
                await pubsub.subscribe(self._channel)
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    try:
                        yield json.loads(message["data"])
                    except (TypeError, ValueError):
                        logger.warning("Discarding malformed event from %s", self._channel)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - keep the fan-out alive
                logger.warning("Redis subscription lost (%s); retrying in 2s", type(error).__name__)
                await asyncio.sleep(2)
            finally:
                try:
                    await pubsub.aclose()
                except Exception:  # noqa: BLE001 - best effort cleanup
                    pass

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception as error:  # noqa: BLE001 - reported as an unhealthy service
            logger.warning("Redis ping failed: %s", type(error).__name__)
            return False

    async def close(self) -> None:
        await self._redis.aclose()
        await self._events_redis.aclose()


def build_store(settings) -> RoomStore:
    """Pick a backend from configuration."""
    if settings.uses_redis:
        return RedisStore(settings.redis_url, settings.key_prefix)
    logger.warning(
        "REDIS_URL is not set: using the in-memory store. State lives in this "
        "process only and is lost on restart - do not use it on a server."
    )
    return MemoryStore()
