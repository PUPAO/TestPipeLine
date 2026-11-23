import hashlib
import json
import re
from typing import Any, Optional
import redis.asyncio as redis
from redis.exceptions import ResponseError

from app.core.config import get_settings

_redis_client: Optional[redis.Redis] = None


async def init_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        return
    settings = get_settings()
    _redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    await ensure_consumer_group()


async def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


def get_redis() -> redis.Redis:
    if _redis_client is None:
        raise RuntimeError("Redis client is not initialized. Call init_redis() first.")
    return _redis_client


def compute_error_hash(exception_class: str, stack_trace: str) -> str:
    """동적 객체 해시(@7a8b9c) 및 숫자를 정규화한 뒤 핵심 프레임 기준으로 에러 해시를 생성합니다."""
    normalized_lines: list[str] = [exception_class.strip()]
    for line in stack_trace.splitlines()[:15]:
        cleaned = re.sub(r"@[0-9a-fA-F]+", "@HEX", line.strip())
        cleaned = re.sub(r"\b\d+\b", "N", cleaned)
        if cleaned:
            normalized_lines.append(cleaned)
    raw = "\n".join(normalized_lines)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


async def acquire_dedup_lock(error_hash: str, incident_id: str) -> bool:
    """동일 error_hash가 TTL 내에 처리된 적이 없으면 True(신규), 이미 있으면 False(중복)를 반환합니다."""
    r = get_redis()
    settings = get_settings()
    key = f"incident:dedup:{error_hash}"
    acquired = await r.set(key, incident_id, nx=True, ex=settings.DEDUP_TTL_SECONDS)
    return bool(acquired)


async def ensure_consumer_group() -> None:
    r = get_redis()
    settings = get_settings()
    try:
        await r.xgroup_create(
            name=settings.REDIS_STREAM_NAME,
            groupname=settings.REDIS_CONSUMER_GROUP,
            id="0",
            mkstream=True,
        )
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def enqueue_incident(incident_data: dict[str, Any]) -> str:
    r = get_redis()
    settings = get_settings()
    msg_id = await r.xadd(
        settings.REDIS_STREAM_NAME,
        {"payload": json.dumps(incident_data, ensure_ascii=False)},
    )
    return str(msg_id)


async def read_next_incident(block_ms: int = 5000) -> Optional[tuple[str, dict[str, Any]]]:
    """무료 LLM Rate Limit(429) 방지를 위해 한 번에 1건씩 순차적으로 소비합니다."""
    r = get_redis()
    settings = get_settings()
    response = await r.xreadgroup(
        groupname=settings.REDIS_CONSUMER_GROUP,
        consumername=settings.REDIS_CONSUMER_NAME,
        streams={settings.REDIS_STREAM_NAME: ">"},
        count=1,
        block=block_ms,
    )
    if not response:
        return None

    _, messages = response[0]
    msg_id, fields = messages[0]
    payload = json.loads(fields["payload"])
    return str(msg_id), payload


async def ack_incident(msg_id: str) -> None:
    r = get_redis()
    settings = get_settings()
    await r.xack(settings.REDIS_STREAM_NAME, settings.REDIS_CONSUMER_GROUP, msg_id)


async def save_pending_approval(incident_id: str, approval_data: dict[str, Any], ttl_seconds: int = 86400) -> None:
    r = get_redis()
    key = f"incident:approval:{incident_id}"
    await r.set(key, json.dumps(approval_data, ensure_ascii=False), ex=ttl_seconds)


async def get_pending_approval(incident_id: str) -> Optional[dict[str, Any]]:
    r = get_redis()
    key = f"incident:approval:{incident_id}"
    raw = await r.get(key)
    return json.loads(raw) if raw else None


async def delete_pending_approval(incident_id: str) -> None:
    r = get_redis()
    await r.delete(f"incident:approval:{incident_id}")
