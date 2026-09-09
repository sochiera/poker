"""Redis backend contract.

Skipped unless a Redis is reachable. Point REDIS_TEST_URL (or REDIS_URL) at one,
for example: docker run --rm -p 6379:6379 redis:7-alpine
"""

import asyncio
import json
import os
from secrets import token_hex

import pytest

from planning_poker import domain
from planning_poker.config import Settings
from planning_poker.service import RoomService
from planning_poker.store import RedisStore


URL = os.environ.get("REDIS_TEST_URL") or os.environ.get("REDIS_URL") or "redis://127.0.0.1:6379/0"


def redis_available() -> bool:
    try:
        return asyncio.run(_ping())
    except Exception:  # noqa: BLE001 - no server, no tests
        return False


async def _ping() -> bool:
    store = RedisStore(URL, prefix=f"pptest:{token_hex(4)}")
    try:
        return await store.ping()
    finally:
        await store.close()


requires_redis = pytest.mark.skipif(not redis_available(), reason=f"no Redis at {URL}")


def store(prefix: str) -> RedisStore:
    return RedisStore(URL, prefix=prefix)


class FakeRedis:
    """Small controlled-clock Redis stub for the CAS/TTL contract."""

    def __init__(self, clock):
        self.clock = clock
        self.values: dict[str, tuple[str, int]] = {}

    def _value(self, key: str) -> str | None:
        entry = self.values.get(key)
        if entry is None:
            return None
        value, expires_at_ms = entry
        if expires_at_ms <= self.clock.now_ms:
            del self.values[key]
            return None
        return value

    async def set(self, key, value, *, nx=False, px=None):
        if nx and self._value(key) is not None:
            return False
        assert px is not None
        self.values[key] = (value, self.clock.now_ms + px)
        return True

    async def get(self, key):
        return self._value(key)

    async def pttl(self, key):
        if self._value(key) is None:
            return -2
        return self.values[key][1] - self.clock.now_ms

    def register_script(self, source):
        assert "PTTL" in source

        async def cas(*, keys, args):
            key = keys[0]
            raw = self._value(key)
            if raw is None:
                return -1
            if json.loads(raw)["version"] != int(args[0]):
                return 0
            remaining = await self.pttl(key)
            if remaining <= 0:
                return -1
            self.values[key] = (args[1], self.clock.now_ms + remaining)
            return 1

        return cas


class RedisClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    @property
    def now_ms(self) -> int:
        return round(self.now * 1000)


def fake_redis_store(clock: RedisClock) -> RedisStore:
    """Build RedisStore's real create/load/save methods over a fake Redis."""
    backend = RedisStore.__new__(RedisStore)
    backend._prefix = "fake"
    backend._redis = FakeRedis(clock)
    backend._cas = backend._redis.register_script(__import__("planning_poker.store", fromlist=["_CAS_SCRIPT"])._CAS_SCRIPT)
    return backend


@requires_redis
def test_rooms_are_shared_between_instances_and_survive_a_restart():
    async def scenario():
        prefix = f"pptest:{token_hex(4)}"
        settings = Settings(redis_url=URL, key_prefix=prefix, room_ttl=120, session_ttl=60)
        first, second = store(prefix), store(prefix)
        try:
            service_a = RoomService(first, settings)
            service_b = RoomService(second, settings)

            room, host = await service_a.create_room("Ada", "PROJ-9")
            await service_a.connect(room.code, host.token, "c-host")
            _, guest = await service_b.join_room(room.code, "Linus")
            await service_b.connect(room.code, guest.token, "c-guest")

            # A vote on instance B is visible to instance A.
            await service_b.vote(room.code, guest.id, 13)
            reloaded = await service_a.store.load(room.code)
            assert reloaded.participants[guest.id].vote == 13
            assert len(reloaded.members(reloaded.updated_at)) == 2

            # A "restarted" process reads the same room from Redis.
            fresh = store(prefix)
            try:
                restarted = await RoomService(fresh, settings).store.load(room.code)
                assert restarted.task == "PROJ-9"
                assert restarted.version == reloaded.version
            finally:
                await fresh.close()

            ttl = await first._redis.pttl(f"{prefix}:room:{room.code}")
            assert 0 < ttl <= settings.room_ttl * 1000
        finally:
            await first.close()
            await second.close()

    asyncio.run(scenario())


@requires_redis
def test_compare_and_set_rejects_a_stale_write():
    async def scenario():
        prefix = f"pptest:{token_hex(4)}"
        settings = Settings(redis_url=URL, key_prefix=prefix, room_ttl=120, session_ttl=60)
        backend = store(prefix)
        try:
            service = RoomService(backend, settings)
            room, host = await service.create_room("Ada", "PROJ-9")
            assert await backend.create(room, settings.room_ttl) is False  # code already taken

            stale = await backend.load(room.code)
            fresh = await backend.load(room.code)
            based_on = fresh.version
            fresh.task = "moved on"
            fresh.version = based_on + 1
            assert await backend.save(fresh, based_on, settings.room_ttl) is True

            stale.task = "based on old state"
            assert await backend.save(stale, stale.version, settings.room_ttl) is False
            assert (await backend.load(room.code)).task == "moved on"

            # ... and the service simply replays the change on top of the winner.
            await service.connect(room.code, host.token, "c-host")
            await service.new_round(room.code, host.id, "PROJ-10")
            assert (await backend.load(room.code)).task == "PROJ-10"
        finally:
            await backend.close()

    asyncio.run(scenario())


@requires_redis
def test_events_reach_a_subscriber_on_another_instance():
    async def scenario():
        prefix = f"pptest:{token_hex(4)}"
        settings = Settings(redis_url=URL, key_prefix=prefix, room_ttl=120, session_ttl=60)
        publisher, subscriber = store(prefix), store(prefix)
        try:
            received: list[dict] = []

            async def collect():
                async for event in subscriber.events():
                    received.append(event)

            task = asyncio.create_task(collect())
            await asyncio.sleep(0.3)  # let the subscription settle

            service = RoomService(publisher, settings)
            room, host = await service.create_room("Ada", "PROJ-9")
            await service.connect(room.code, host.token, "c-host")
            await service.vote(room.code, host.id, 8)
            await service.reveal(room.code, host.id)

            for _ in range(50):
                if len(received) >= 3:
                    break
                await asyncio.sleep(0.05)
            task.cancel()

            assert [event["code"] for event in received[:3]] == [room.code] * 3
            assert received[-1]["state"]["statistics"]["average"] == 8
            assert "token" not in str(received)
        finally:
            await publisher.close()
            await subscriber.close()

    asyncio.run(scenario())


@requires_redis
def test_expired_room_is_gone():
    async def scenario():
        prefix = f"pptest:{token_hex(4)}"
        backend = store(prefix)
        try:
            room, _ = domain.new_room("Ada", "PROJ-9", 1000.0)
            assert await backend.create(room, ttl=1) is True
            assert await backend.load(room.code) is not None
            await asyncio.sleep(1.2)
            assert await backend.load(room.code) is None
        finally:
            await backend.close()

    asyncio.run(scenario())


def test_redis_save_preserves_remaining_absolute_room_ttl(monkeypatch):
    """A Redis CAS save retains the key's remaining TTL, rather than renewing it."""
    async def scenario():
        clock = RedisClock()
        monkeypatch.setattr("planning_poker.store.time.time", lambda: clock.now)
        backend = fake_redis_store(clock)
        room, _ = domain.new_room("Ada", "PROJ-9", clock.now)
        assert await backend.create(room, ttl=30)
        clock.now += 7
        saved = await backend.load(room.code)
        assert saved is not None
        saved.task = "PROJ-10"
        saved.version += 1
        assert await backend.save(saved, expected_version=0, ttl=30)
        remaining = await backend._redis.pttl(backend._key(room.code))
        assert remaining == 23_000
        assert remaining < 30_000

    asyncio.run(scenario())


def test_expired_room_and_history_are_not_resurrected_by_cas(monkeypatch):
    async def scenario():
        clock = RedisClock()
        monkeypatch.setattr("planning_poker.store.time.time", lambda: clock.now)
        backend = fake_redis_store(clock)
        room, host = domain.new_room("Ada", "PROJ-9", clock.now)
        room.expires_at = clock.now + 1
        domain.connect(room, host, "c", clock.now, 45)
        domain.reveal(room, host, clock.now)
        domain.finalize_round(room, host, "8", clock.now)
        assert room.history
        assert await backend.create(room, 60)
        stale = await backend.load(room.code)
        clock.now += 2
        stale.version += 1
        assert await backend.save(stale, 0, 60) is False
        assert await backend.load(room.code) is None
        assert backend._key(room.code) not in backend._redis.values

    asyncio.run(scenario())
