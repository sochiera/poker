"""Pure Planning Poker rules.

Nothing in this module touches I/O, the clock or the network: every function
takes the room it works on plus the current time. That keeps the rules easy to
test and lets the same code run on top of any storage backend (see `store.py`).

A room is a single serialisable value. Presence is part of it: every websocket
connection holds a lease that expires unless it is refreshed, so a crashed
process cannot leave a participant online forever.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from secrets import choice, compare_digest, token_urlsafe
from statistics import median
from string import ascii_uppercase, digits
from typing import Any


CARDS: tuple[int | str, ...] = (0, 1, 2, 3, 5, 8, 13, 21, "?", "☕")
ROOM_ALPHABET = ascii_uppercase + digits
ROOM_CODE_LENGTH = 6
ROOM_CODE_PATTERN = re.compile(rf"^[A-Z0-9]{{{ROOM_CODE_LENGTH}}}$")
MAX_NICKNAME_LENGTH = 32
MAX_TASK_LENGTH = 160
DEFAULT_TASK = "Untitled task"


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class Participant:
    id: str
    nickname: str
    token: str
    joined_order: int
    ever_connected: bool = False
    last_seen: float = 0.0
    vote: int | str | None = None
    # connection id -> unix timestamp at which the presence lease expires
    connections: dict[str, float] = field(default_factory=dict)

    def is_active(self, now: float) -> bool:
        return any(expires_at > now for expires_at in self.connections.values())

    def drop_expired(self, now: float) -> bool:
        stale = [cid for cid, expires_at in self.connections.items() if expires_at <= now]
        for cid in stale:
            del self.connections[cid]
        return bool(stale)


@dataclass
class HistoryEntry:
    """The public, immutable-by-default outcome of one completed round."""

    id: str
    task: str
    effort: str
    round_number: int
    created_at: float
    updated_at: float


@dataclass
class Room:
    code: str
    task: str
    host_id: str
    round_number: int = 1
    revealed: bool = False
    version: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    # Absolute expiry shared by the room and its history.  Stores use this on
    # creation; later CAS writes preserve the key's remaining lifetime.
    expires_at: float = 0.0
    participants: dict[str, Participant] = field(default_factory=dict)
    history: list[HistoryEntry] = field(default_factory=list)

    def members(self, now: float) -> list[Participant]:
        active = (p for p in self.participants.values() if p.is_active(now))
        return sorted(active, key=lambda p: p.joined_order)

    def next_join_order(self) -> int:
        return max((p.joined_order for p in self.participants.values()), default=0) + 1


# --- validation -------------------------------------------------------------


def normalise_code(value: Any) -> str:
    """Return a canonical room code or raise; never trust the raw client value."""
    if not isinstance(value, str):
        raise DomainError("room_not_found", "This room does not exist.", 404)
    code = value.strip().upper()
    if not ROOM_CODE_PATTERN.match(code):
        raise DomainError("room_not_found", "This room does not exist.", 404)
    return code


def validate_nickname(value: Any) -> str:
    if not isinstance(value, str):
        raise DomainError("invalid_nickname", "Nickname is required.")
    nickname = " ".join(value.split())
    if not nickname:
        raise DomainError("invalid_nickname", "Nickname cannot be empty.")
    if len(nickname) > MAX_NICKNAME_LENGTH:
        raise DomainError("invalid_nickname", f"Nickname may contain at most {MAX_NICKNAME_LENGTH} characters.")
    return nickname


def validate_task(value: Any) -> str:
    if value is None:
        return DEFAULT_TASK
    if not isinstance(value, str):
        raise DomainError("invalid_task", "Task must be text.")
    task = " ".join(value.split())
    if len(task) > MAX_TASK_LENGTH:
        raise DomainError("invalid_task", f"Task may contain at most {MAX_TASK_LENGTH} characters.")
    return task or DEFAULT_TASK


def validate_effort(value: Any) -> str:
    """Validate the host's selected outcome, independently of the card deck."""
    if not isinstance(value, str):
        raise DomainError("invalid_effort", "Final effort must be text.")
    effort = " ".join(value.split())
    if not effort:
        raise DomainError("invalid_effort", "Final effort cannot be empty.")
    if len(effort) > MAX_TASK_LENGTH:
        raise DomainError("invalid_effort", f"Final effort may contain at most {MAX_TASK_LENGTH} characters.")
    return effort


def new_code() -> str:
    return "".join(choice(ROOM_ALPHABET) for _ in range(ROOM_CODE_LENGTH))


# --- lifecycle --------------------------------------------------------------


def _new_participant(nickname: str, joined_order: int, now: float) -> Participant:
    return Participant(
        id=token_urlsafe(9),
        nickname=nickname,
        token=token_urlsafe(24),
        joined_order=joined_order,
        last_seen=now,
    )


def new_room(nickname: str, task: str, now: float, code: str | None = None) -> tuple[Room, Participant]:
    """Build a brand new room; `nickname` and `task` must already be validated."""
    creator = _new_participant(nickname, 1, now)
    room = Room(
        code=code or new_code(),
        task=task,
        host_id=creator.id,
        created_at=now,
        updated_at=now,
        participants={creator.id: creator},
    )
    return room, creator


def add_participant(room: Room, nickname: str, now: float, max_participants: int) -> Participant:
    if any(p.nickname.casefold() == nickname.casefold() and p.is_active(now) for p in room.participants.values()):
        raise DomainError("nickname_taken", "That nickname is already in use.", 409)
    if len(room.participants) >= max_participants:
        raise DomainError("room_full", "This room is full.", 409)
    participant = _new_participant(nickname, room.next_join_order(), now)
    room.participants[participant.id] = participant
    return participant


def find_by_token(room: Room, token: Any) -> Participant:
    if isinstance(token, str) and token:
        for participant in room.participants.values():
            if compare_digest(participant.token, token):
                return participant
    raise DomainError("unauthorized", "Your room session is invalid.", 401)


def ensure_host(room: Room, now: float) -> bool:
    """Make sure the room has a sensible host. Returns True when it changed.

    The creator keeps the role while their first page is still loading; once a
    host has actually been connected and is gone, the longest present active
    participant takes over so the room never breaks.
    """
    host = room.participants.get(room.host_id)
    if host is not None and (host.is_active(now) or not host.ever_connected):
        return False
    members = room.members(now)
    if not members:
        if host is not None:
            return False
        members = sorted(room.participants.values(), key=lambda p: p.joined_order)
        if not members:
            return False
    if members[0].id == room.host_id:
        return False
    room.host_id = members[0].id
    return True


def connect(room: Room, participant: Participant, connection_id: str, now: float, lease: float) -> None:
    if not participant.is_active(now) and any(
        p.id != participant.id and p.is_active(now) and p.nickname.casefold() == participant.nickname.casefold()
        for p in room.participants.values()
    ):
        raise DomainError("nickname_taken", "That nickname is already in use.", 409)
    participant.connections[connection_id] = now + lease
    participant.ever_connected = True
    participant.last_seen = now
    ensure_host(room, now)


def heartbeat(room: Room, participant: Participant, connection_id: str, now: float, lease: float) -> None:
    """Renew a presence lease. A late heartbeat re-establishes it rather than
    dropping a participant who is demonstrably still there."""
    participant.connections[connection_id] = now + lease
    participant.ever_connected = True
    participant.last_seen = now


def disconnect(room: Room, participant: Participant, connection_id: str, now: float) -> None:
    participant.connections.pop(connection_id, None)
    participant.last_seen = now
    if not participant.is_active(now):
        participant.vote = None
        ensure_host(room, now)


def prune(room: Room, now: float, session_ttl: float) -> bool:
    """Drop expired connections and long-gone sessions. True when something changed."""
    changed = False
    for participant in list(room.participants.values()):
        if participant.drop_expired(now):
            changed = True
            if not participant.is_active(now):
                participant.vote = None
        if not participant.is_active(now) and participant.last_seen + session_ttl <= now:
            del room.participants[participant.id]
            changed = True
    if changed:
        ensure_host(room, now)
    return changed


# --- actions ----------------------------------------------------------------


def _require_active(participant: Participant, now: float) -> None:
    if not participant.is_active(now):
        raise DomainError("unauthorized", "Participant is not connected.", 401)


def _require_host(room: Room, participant: Participant) -> None:
    if room.host_id != participant.id:
        raise DomainError("host_only", "Only the host can do that.", 403)


def vote(room: Room, participant: Participant, value: Any, now: float) -> None:
    _require_active(participant, now)
    if room.revealed:
        raise DomainError("round_revealed", "Votes cannot be changed after reveal.")
    # bool is an int and 5.0 == 5, so check the type before the deck.
    if not isinstance(value, (int, str)) or isinstance(value, bool) or value not in CARDS:
        raise DomainError("invalid_vote", "Choose one of the available cards.")
    participant.vote = value


def reveal(room: Room, participant: Participant, now: float) -> None:
    _require_active(participant, now)
    _require_host(room, participant)
    room.revealed = True


def claim_host(room: Room, participant: Participant, now: float) -> None:
    """Any connected participant can take the host role with one action."""
    _require_active(participant, now)
    room.host_id = participant.id


def new_round(room: Room, participant: Participant, task: Any, now: float) -> None:
    _require_active(participant, now)
    _require_host(room, participant)
    if task is not None and not (isinstance(task, str) and not task.strip()):
        room.task = validate_task(task)
    room.revealed = False
    room.round_number += 1
    for member in room.participants.values():
        member.vote = None


def finalize_round(room: Room, participant: Participant, effort: Any, now: float) -> HistoryEntry:
    _require_active(participant, now)
    _require_host(room, participant)
    if any(entry.round_number == room.round_number for entry in room.history):
        raise DomainError("round_already_finalized", "This round has already been finalized.", 409)
    if not room.revealed:
        raise DomainError("round_not_revealed", "Reveal the cards before saving a result.")
    entry = HistoryEntry(
        id=token_urlsafe(9), task=room.task, effort=validate_effort(effort),
        round_number=room.round_number, created_at=now, updated_at=now,
    )
    room.history.append(entry)
    return entry


def remove_history_entry(room: Room, participant: Participant, entry_id: Any, now: float) -> None:
    _require_active(participant, now)
    _require_host(room, participant)
    if not isinstance(entry_id, str) or not entry_id:
        raise DomainError("history_entry_not_found", "That history entry does not exist.", 404)
    for index, entry in enumerate(room.history):
        if entry.id == entry_id:
            del room.history[index]
            return
    raise DomainError("history_entry_not_found", "That history entry does not exist.", 404)


def rename_task(room: Room, participant: Participant, task: Any, now: float) -> None:
    """Change only the current round title; completed history stays immutable."""
    _require_active(participant, now)
    _require_host(room, participant)
    room.task = validate_task(task)


def reestimate_history(room: Room, participant: Participant, entry_id: Any, now: float) -> None:
    if not isinstance(entry_id, str) or not entry_id:
        raise DomainError("history_entry_not_found", "That history entry does not exist.", 404)
    entry = next((item for item in room.history if item.id == entry_id), None)
    if entry is None:
        raise DomainError("history_entry_not_found", "That history entry does not exist.", 404)
    new_round(room, participant, entry.task, now)


def kick(room: Room, host: Participant, target_id: Any, now: float) -> None:
    _require_active(host, now)
    _require_host(room, host)
    if not isinstance(target_id, str) or not target_id:
        raise DomainError("participant_not_found", "That person is not in the room.", 404)
    if target_id == host.id:
        raise DomainError("cannot_kick_self", "You cannot remove yourself.")
    target = room.participants.get(target_id)
    if target is None:
        raise DomainError("participant_not_found", "That person is not in the room.", 404)
    del room.participants[target_id]


# --- projections ------------------------------------------------------------


def statistics(room: Room, now: float) -> dict[str, int | float] | None:
    values = [p.vote for p in room.members(now) if type(p.vote) is int]
    if not values:
        return None
    return {
        "average": round(sum(values) / len(values), 1),
        "median": median(values),
        "highest": max(values),
        "lowest": min(values),
    }


def snapshot(room: Room, now: float) -> dict[str, Any]:
    """Public room state. Votes stay hidden until the host reveals them."""
    return {
        "code": room.code,
        "task": room.task,
        "round": room.round_number,
        "revealed": room.revealed,
        "hostId": room.host_id,
        "cards": list(CARDS),
        "version": room.version,
        "participants": [
            {
                "id": p.id,
                "nickname": p.nickname,
                "isHost": p.id == room.host_id,
                "hasVoted": p.vote is not None,
                "vote": p.vote if room.revealed else None,
            }
            for p in room.members(now)
        ],
        "statistics": statistics(room, now) if room.revealed else None,
        "history": [
            {"id": entry.id, "task": entry.task, "effort": entry.effort,
             "round": entry.round_number, "createdAt": entry.created_at, "updatedAt": entry.updated_at}
            for entry in room.history
        ],
    }


# --- serialisation ----------------------------------------------------------


def room_to_dict(room: Room) -> dict[str, Any]:
    return {
        "code": room.code,
        "task": room.task,
        "hostId": room.host_id,
        "round": room.round_number,
        "revealed": room.revealed,
        "version": room.version,
        "createdAt": room.created_at,
        "updatedAt": room.updated_at,
        "expiresAt": room.expires_at,
        "history": [
            {"id": entry.id, "task": entry.task, "effort": entry.effort,
             "round": entry.round_number, "createdAt": entry.created_at, "updatedAt": entry.updated_at}
            for entry in room.history
        ],
        "participants": [
            {
                "id": p.id,
                "nickname": p.nickname,
                "token": p.token,
                "order": p.joined_order,
                "everConnected": p.ever_connected,
                "lastSeen": p.last_seen,
                "vote": p.vote,
                "connections": p.connections,
            }
            for p in room.participants.values()
        ],
    }


def room_from_dict(data: dict[str, Any]) -> Room:
    participants = {}
    for item in data["participants"]:
        participant = Participant(
            id=item["id"],
            nickname=item["nickname"],
            token=item["token"],
            joined_order=item["order"],
            ever_connected=item["everConnected"],
            last_seen=item["lastSeen"],
            vote=item["vote"],
            connections={cid: float(exp) for cid, exp in (item["connections"] or {}).items()},
        )
        participants[participant.id] = participant
    history = [
        HistoryEntry(
            id=item["id"], task=item["task"], effort=item["effort"],
            round_number=item["round"], created_at=item["createdAt"], updated_at=item["updatedAt"],
        )
        for item in data.get("history", [])
    ]
    return Room(
        code=data["code"],
        task=data["task"],
        host_id=data["hostId"],
        round_number=data["round"],
        revealed=data["revealed"],
        version=data["version"],
        created_at=data["createdAt"],
        updated_at=data["updatedAt"],
        expires_at=float(data.get("expiresAt", 0.0)),
        participants=participants,
        history=history,
    )
