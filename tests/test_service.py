"""Service layer against a store: concurrency, pruning and event fan-out."""

import asyncio

import pytest

from planning_poker.config import ConfigError, Settings
from planning_poker.domain import DomainError
from planning_poker.service import RoomService
from planning_poker.store import MemoryStore


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build(settings: Settings | None = None) -> tuple[RoomService, MemoryStore, Clock]:
    clock = Clock()
    store = MemoryStore(clock=clock)
    settings = settings or Settings(connection_lease=45, heartbeat_interval=15, session_ttl=900, room_ttl=3600)
    return RoomService(store, settings, clock=clock), store, clock


async def connected_room(service: RoomService):
    room, host = await service.create_room("Ada", "PROJ-1 Login")
    await service.connect(room.code, host.token, "c-host")
    _, guest = await service.join_room(room.code, "Linus")
    room, _ = await service.connect(room.code, guest.token, "c-guest")
    return room, host, guest


def test_full_round_trip_over_the_store():
    async def scenario():
        service, _, _ = build()
        room, host, guest = await connected_room(service)
        await service.vote(room.code, host.id, 5)
        await service.vote(room.code, guest.id, 8)
        room = await service.reveal(room.code, host.id)
        state = service.snapshot(room)
        assert state["revealed"] is True
        assert state["statistics"] == {"average": 6.5, "median": 6.5, "highest": 8, "lowest": 5}

        room = await service.new_round(room.code, host.id, "PROJ-2 Checkout")
        state = service.snapshot(room)
        assert state["task"] == "PROJ-2 Checkout"
        assert state["round"] == 2
        assert [p["hasVoted"] for p in state["participants"]] == [False, False]

    asyncio.run(scenario())


def test_state_is_reloaded_from_the_store_not_from_memory():
    async def scenario():
        service, store, clock = build()
        room, host, _ = await connected_room(service)
        await service.vote(room.code, host.id, 3)
        # A second service shares nothing but the store, like a second container.
        other = RoomService(store, service.settings, clock=clock)
        reloaded = await other.store.load(room.code)
        assert reloaded.participants[host.id].vote == 3
        await other.reveal(room.code, host.id)
        assert (await service.store.load(room.code)).revealed is True

    asyncio.run(scenario())


def test_missing_or_malformed_room_codes():
    async def scenario():
        service, _, _ = build()
        for code in ["ZZZZZZ", "nope", ""]:
            with pytest.raises(DomainError) as error:
                await service.join_room(code, "Ada")
            assert error.value.status == 404

    asyncio.run(scenario())


def test_actions_are_announced_to_subscribers():
    async def scenario():
        service, store, _ = build()
        events = store.events()
        room, host, _ = await connected_room(service)
        received = []

        async def collect():
            async for event in events:
                received.append(event)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        await service.vote(room.code, host.id, 13)
        await service.reveal(room.code, host.id)
        await asyncio.sleep(0)
        task.cancel()

        assert [event["code"] for event in received] == [room.code, room.code]
        assert received[-1]["state"]["revealed"] is True
        assert received[1]["version"] > received[0]["version"]
        # A published snapshot never leaks session tokens.
        assert "token" not in str(received)

    asyncio.run(scenario())


def test_heartbeat_does_not_spam_events_but_keeps_presence():
    async def scenario():
        service, store, clock = build()
        room, host, _ = await connected_room(service)
        received = []

        async def collect():
            async for event in store.events():
                received.append(event)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0)
        clock.advance(20)
        await service.heartbeat(room.code, host.id, "c-host")
        await asyncio.sleep(0)
        assert received == []

        # The guest never heartbeats, so its lease lapses and that *is* news.
        clock.advance(40)
        await service.heartbeat(room.code, host.id, "c-host")
        await asyncio.sleep(0)
        task.cancel()
        assert len(received) == 1
        assert [p["nickname"] for p in received[0]["state"]["participants"]] == ["Ada"]

    asyncio.run(scenario())


def test_disconnect_transfers_host_and_expired_room_is_tolerated():
    async def scenario():
        service, store, clock = build()
        room, host, guest = await connected_room(service)
        await service.disconnect(room.code, host.id, "c-host")
        state = service.snapshot(await store.load(room.code))
        assert state["hostId"] == guest.id
        assert [p["nickname"] for p in state["participants"]] == ["Linus"]

        clock.advance(service.settings.room_ttl + 1)
        await service.disconnect(room.code, guest.id, "c-guest")  # room already expired

    asyncio.run(scenario())


def test_concurrent_votes_do_not_overwrite_each_other():
    async def scenario():
        service, _, _ = build()
        room, host, guest = await connected_room(service)
        await asyncio.gather(
            service.vote(room.code, host.id, 8),
            service.vote(room.code, guest.id, 13),
        )
        await service.reveal(room.code, host.id)
        state = service.snapshot(await service.store.load(room.code))
        assert sorted(p["vote"] for p in state["participants"]) == [8, 13]

    asyncio.run(scenario())


def test_reconnect_after_a_dropped_lease_keeps_the_identity():
    async def scenario():
        service, store, clock = build()
        room, host, _ = await connected_room(service)
        clock.advance(service.settings.connection_lease + 1)
        room, resumed = await service.connect(room.code, host.token, "c-host-2")
        assert resumed.id == host.id
        ids = [p["id"] for p in service.snapshot(room)["participants"]]
        assert ids.count(host.id) == 1

    asyncio.run(scenario())


def test_expired_session_cannot_reconnect():
    async def scenario():
        service, _, clock = build()
        room, host, _ = await connected_room(service)
        await service.disconnect(room.code, host.id, "c-host")
        clock.advance(service.settings.session_ttl + 1)
        with pytest.raises(DomainError) as error:
            await service.connect(room.code, host.token, "c-host-3")
        assert error.value.status == 401

    asyncio.run(scenario())


def test_settings_reject_inconsistent_ttls():
    with pytest.raises(ConfigError):
        Settings.from_env({"HEARTBEAT_SECONDS": "30", "CONNECTION_LEASE_SECONDS": "40"})
    with pytest.raises(ConfigError):
        Settings.from_env({"SESSION_TTL_SECONDS": "7200", "ROOM_TTL_SECONDS": "3600"})
    with pytest.raises(ConfigError):
        Settings.from_env({"ROOM_TTL_SECONDS": "not-a-number"})
    settings = Settings.from_env({"REDIS_URL": "redis://cache:6379/0", "ROOM_TTL_SECONDS": "1200"})
    assert settings.uses_redis and settings.room_ttl == 1200


def test_concurrent_finalize_replays_cas_and_persists_one_history_entry():
    class OneConflictStore(MemoryStore):
        def __init__(self, clock):
            super().__init__(clock=clock)
            self.conflicted = False

        async def save(self, room, expected_version, ttl):
            if not self.conflicted:
                self.conflicted = True
                return False
            return await super().save(room, expected_version, ttl)

    async def scenario():
        clock = Clock()
        store = OneConflictStore(clock)
        settings = Settings(connection_lease=45, heartbeat_interval=15, session_ttl=900, room_ttl=3600)
        service = RoomService(store, settings, clock=clock)
        room, host, _ = await connected_room(service)
        await service.reveal(room.code, host.id)
        store.conflicted = False
        results = await asyncio.gather(
            service.finalize_round(room.code, host.id, "5"),
            service.finalize_round(room.code, host.id, "5"),
            return_exceptions=True,
        )
        saved = await store.load(room.code)
        assert len(saved.history) == 1 and saved.history[0].effort == "5"
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert any(isinstance(result, DomainError) and result.code == "round_already_finalized" for result in results)

    asyncio.run(scenario())


def test_history_is_present_after_leave_and_rejoin_before_expiry():
    async def scenario():
        service, _, _ = build()
        room, host, _ = await connected_room(service)
        await service.reveal(room.code, host.id)
        await service.finalize_round(room.code, host.id, "8")
        await service.disconnect(room.code, host.id, "c-host")
        rejoined, _ = await service.connect(room.code, host.token, "c-host-return")
        assert service.snapshot(rejoined)["history"][0]["effort"] == "8"

    asyncio.run(scenario())
