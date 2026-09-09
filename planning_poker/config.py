"""Runtime configuration. Everything that differs between a laptop and a VPS
comes from environment variables, so the same image runs in both places."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


class ConfigError(RuntimeError):
    pass


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    #: Empty means "no Redis": a single-process in-memory store for local dev.
    redis_url: str = ""
    key_prefix: str = "pp"
    #: Absolute lifetime of a room and its history, measured from creation.
    room_ttl: int = 30 * 24 * 3600
    #: How long a disconnected participant's session (identity + token) is kept.
    session_ttl: int = 900
    #: Presence lease per websocket connection; a crashed process expires by itself.
    connection_lease: int = 45
    heartbeat_interval: int = 15
    max_participants: int = 50
    log_level: str = "INFO"

    @property
    def uses_redis(self) -> bool:
        return bool(self.redis_url)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        settings = cls(
            redis_url=env.get("REDIS_URL", "").strip(),
            key_prefix=env.get("REDIS_KEY_PREFIX", "").strip() or "pp",
            room_ttl=_int(env, "ROOM_TTL_SECONDS", cls.room_ttl, minimum=60),
            session_ttl=_int(env, "SESSION_TTL_SECONDS", cls.session_ttl, minimum=30),
            connection_lease=_int(env, "CONNECTION_LEASE_SECONDS", cls.connection_lease, minimum=10),
            heartbeat_interval=_int(env, "HEARTBEAT_SECONDS", cls.heartbeat_interval, minimum=5),
            max_participants=_int(env, "MAX_PARTICIPANTS", cls.max_participants, minimum=2),
            log_level=(env.get("LOG_LEVEL", "").strip() or "INFO").upper(),
        )
        if settings.heartbeat_interval * 2 > settings.connection_lease:
            raise ConfigError(
                "CONNECTION_LEASE_SECONDS must be at least twice HEARTBEAT_SECONDS "
                "so a single missed heartbeat does not drop a participant."
            )
        if settings.session_ttl > settings.room_ttl:
            raise ConfigError("SESSION_TTL_SECONDS cannot be longer than ROOM_TTL_SECONDS.")
        return settings
