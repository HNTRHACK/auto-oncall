"""Incidents live in a Redis hash (`incident:<id>`) with a 7 day TTL. Values are strings."""

from functools import lru_cache

import redis

from app.core.config import get_settings


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().redis_url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
    )


def _key(incident_id: str) -> str:
    return f"incident:{incident_id}"


def save(incident_id: str, **fields: str) -> None:
    pipe = get_redis().pipeline()  # MULTI: never leave a hash without a TTL
    pipe.hset(_key(incident_id), mapping=fields)
    pipe.expire(_key(incident_id), get_settings().incident_ttl_seconds)
    pipe.execute()


def get(incident_id: str) -> dict[str, str]:
    return get_redis().hgetall(_key(incident_id))


def claim_once(key: str, ttl_seconds: int) -> bool:
    """True the first time `key` is seen within `ttl_seconds` (atomic SET NX)."""
    return bool(get_redis().set(key, "1", nx=True, ex=ttl_seconds))


def release(key: str) -> None:
    get_redis().delete(key)
