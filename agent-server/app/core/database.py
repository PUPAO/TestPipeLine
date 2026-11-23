from datetime import datetime, timezone
from typing import Any, Optional
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pgvector.psycopg import register_vector_async

from app.core.config import get_settings

_pool: Optional[AsyncConnectionPool] = None


async def init_db_pool() -> None:
    global _pool
    if _pool is not None:
        return
    settings = get_settings()
    _pool = AsyncConnectionPool(
        conninfo=settings.DATABASE_URL,
        min_size=1,
        max_size=5,
        open=False,
        configure=register_vector_async,
    )
    await _pool.open()


async def close_db_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialized. Call init_db_pool() first.")
    return _pool


async def upsert_code_chunk(
    file_path: str,
    class_name: str,
    domain_tag: str,
    content: str,
    content_hash: str,
    embedding: list[float],
) -> None:
    pool = get_pool()
    query = """
        INSERT INTO code_chunks (file_path, class_name, domain_tag, content, content_hash, embedding, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (file_path, class_name)
        DO UPDATE SET
            domain_tag = EXCLUDED.domain_tag,
            content = EXCLUDED.content,
            content_hash = EXCLUDED.content_hash,
            embedding = EXCLUDED.embedding,
            updated_at = NOW()
        WHERE code_chunks.content_hash IS DISTINCT FROM EXCLUDED.content_hash;
    """
    async with pool.connection() as conn:
        await conn.execute(query, (file_path, class_name, domain_tag, content, content_hash, embedding))


async def get_existing_chunk_hashes() -> dict[str, str]:
    """증분 임베딩을 위해 기존 DB에 저장된 (file_path -> content_hash) 맵을 조회합니다."""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT file_path, content_hash FROM code_chunks;")
            rows = await cur.fetchall()
            return {row["file_path"]: row["content_hash"] for row in rows}


async def search_similar_code(
    query_embedding: list[float],
    domain_tag: Optional[str] = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """메타데이터(domain_tag)로 1차 범위를 좁힌 뒤 코사인 유사도 상위 코드를 검색합니다."""
    pool = get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if domain_tag and domain_tag != "general":
                sql = """
                    SELECT file_path, class_name, domain_tag, content,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM code_chunks
                    WHERE domain_tag = %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s;
                """
                await cur.execute(sql, (query_embedding, domain_tag, query_embedding, limit))
                rows = await cur.fetchall()
                if rows:
                    return list(rows)

            # 해당 도메인 결과가 없거나 general인 경우 전체에서 검색
            fallback_sql = """
                SELECT file_path, class_name, domain_tag, content,
                       1 - (embedding <=> %s::vector) AS similarity
                FROM code_chunks
                ORDER BY embedding <=> %s::vector
                LIMIT %s;
            """
            await cur.execute(fallback_sql, (query_embedding, query_embedding, limit))
            return list(await cur.fetchall())


async def create_incident_metric(incident_id: str, error_hash: str) -> None:
    pool = get_pool()
    sql = """
        INSERT INTO incident_metrics (incident_id, error_hash, status)
        VALUES (%s, %s, 'QUEUED')
        ON CONFLICT (incident_id) DO NOTHING;
    """
    async with pool.connection() as conn:
        await conn.execute(sql, (incident_id, error_hash))


async def update_incident_metric(
    incident_id: str,
    *,
    status: Optional[str] = None,
    error_category: Optional[str] = None,
    target_domain: Optional[str] = None,
    retry_count: Optional[int] = None,
    total_tests_count: Optional[int] = None,
    executed_tests_count: Optional[int] = None,
    test_duration_ms: Optional[int] = None,
    groq_tokens: Optional[int] = None,
    gemini_tokens: Optional[int] = None,
    pr_url: Optional[str] = None,
    resolved: bool = False,
) -> None:
    pool = get_pool()
    updates: list[str] = []
    params: list[Any] = []

    field_map = {
        "status": status,
        "error_category": error_category,
        "target_domain": target_domain,
        "retry_count": retry_count,
        "total_tests_count": total_tests_count,
        "executed_tests_count": executed_tests_count,
        "test_duration_ms": test_duration_ms,
        "groq_tokens": groq_tokens,
        "gemini_tokens": gemini_tokens,
        "pr_url": pr_url,
    }
    for col, val in field_map.items():
        if val is not None:
            updates.append(f"{col} = %s")
            params.append(val)

    if resolved:
        updates.append("resolved_at = %s")
        params.append(datetime.now(timezone.utc))

    if not updates:
        return

    params.append(incident_id)
    sql = f"UPDATE incident_metrics SET {', '.join(updates)} WHERE incident_id = %s;"
    async with pool.connection() as conn:
        await conn.execute(sql, params)
