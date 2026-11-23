import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from psycopg.rows import dict_row

from app.bot.discord_bot import create_incident_thread, get_discord_bot, post_thread_update
from app.core.config import get_settings
from app.core.database import (
    close_db_pool,
    create_incident_metric,
    get_pool,
    init_db_pool,
)
from app.core.redis_client import (
    ack_incident,
    acquire_dedup_lock,
    close_redis,
    compute_error_hash,
    enqueue_incident,
    init_redis,
    read_next_incident,
)
from app.graph.workflow import run_incident_graph
from app.schemas.incident import IncidentQueuedResponse, IncidentWebhookPayload
from app.services.rag_indexer import sync_repository_embeddings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def redis_stream_worker(stop_event: asyncio.Event) -> None:
    """Redis Streams에서 장애 작업을 1건씩 꺼내 LangGraph 파이프라인을 실행합니다."""
    logger.info("Redis Streams incident worker started.")
    while not stop_event.is_set():
        try:
            item = await read_next_incident(block_ms=3000)
            if item is None:
                continue

            msg_id, payload = item
            incident_id = payload["incident_id"]
            logger.info("Processing incident %s (stream msg_id=%s)", incident_id, msg_id)

            try:
                await run_incident_graph(payload)
            except Exception as exec_err:
                logger.exception("LangGraph execution failed for %s: %s", incident_id, exec_err)
                await post_thread_update(
                    payload.get("discord_thread_id"),
                    f"❌ 파이프라인 실행 중 예기치 않은 오류가 발생했습니다: `{exec_err}`",
                )
            finally:
                await ack_incident(msg_id)
        except asyncio.CancelledError:
            break
        except Exception as loop_err:
            logger.error("Stream worker loop error: %s", loop_err)
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db_pool()
    await init_redis()

    stop_event = asyncio.Event()
    bot_task: asyncio.Task[Any] | None = None

    if settings.DISCORD_BOT_TOKEN:
        bot = get_discord_bot()
        bot_task = asyncio.create_task(bot.start(settings.DISCORD_BOT_TOKEN))

    worker_task = asyncio.create_task(redis_stream_worker(stop_event))
    logger.info("AI Incident Responder Gateway is ready.")

    yield

    stop_event.set()
    worker_task.cancel()
    if bot_task:
        bot = get_discord_bot()
        await bot.close()
        bot_task.cancel()

    await close_redis()
    await close_db_pool()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="AI Incident Responder & Regression Verifier",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/incidents", response_model=IncidentQueuedResponse)
async def receive_incident(payload: IncidentWebhookPayload) -> IncidentQueuedResponse:
    """n8n으로부터 장애 이벤트를 수신해 중복을 필터링하고 비동기 큐에 적재합니다."""
    error_hash = compute_error_hash(payload.exception_class, payload.stack_trace)
    incident_id = uuid.uuid4().hex[:16]

    # 1. 동일 스택 트레이스 중복 폭주 차단 (TTL 내 1회만 파이프라인 실행)
    is_new = await acquire_dedup_lock(error_hash, incident_id)
    if not is_new:
        return IncidentQueuedResponse(
            status="duplicate_ignored",
            incident_id=incident_id,
            error_hash=error_hash,
            detail="동일한 스택 트레이스가 최근 처리되어 중복 요청을 무시했습니다.",
        )

    # 2. 정량 지표 초기화 및 Discord 추적 스레드 생성
    await create_incident_metric(incident_id, error_hash)
    endpoint = f"{payload.http_method} {payload.request_uri}"
    thread_id = await create_incident_thread(
        incident_id=incident_id,
        service_name=payload.service_name,
        environment=payload.environment,
        exception_class=payload.exception_class,
        endpoint=endpoint,
        error_hash=error_hash,
    )

    # 3. Redis Streams 큐 적재
    job_data = payload.model_dump(by_alias=False)
    job_data["incident_id"] = incident_id
    job_data["error_hash"] = error_hash
    job_data["discord_thread_id"] = thread_id

    await enqueue_incident(job_data)

    return IncidentQueuedResponse(
        status="queued",
        incident_id=incident_id,
        error_hash=error_hash,
        detail="장애 분석 및 회귀 검증 파이프라인 큐에 등록되었습니다.",
    )


@app.post("/api/v1/rag/sync")
async def trigger_rag_sync() -> dict[str, Any]:
    """타겟 저장소의 변경된 Java 파일만 증분 임베딩하여 pgvector에 반영합니다."""
    try:
        stats = await sync_repository_embeddings()
        return {"status": "synced", **stats}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/metrics/summary")
async def get_metrics_summary() -> dict[str, Any]:
    """이력서·포트폴리오 기재를 위한 전체 파이프라인 정량 지표를 집계합니다."""
    pool = get_pool()
    sql = """
        SELECT
            COUNT(*) AS total_incidents,
            COUNT(*) FILTER (WHERE status IN ('AWAITING_APPROVAL', 'PR_CREATED')) AS auto_verified_count,
            ROUND(AVG(retry_count), 2) AS avg_retries,
            ROUND(
                AVG(
                    CASE WHEN total_tests_count > 0
                    THEN (1.0 - (executed_tests_count::numeric / total_tests_count)) * 100
                    ELSE 0 END
                ), 1
            ) AS avg_test_reduction_pct,
            ROUND(AVG(test_duration_ms)) AS avg_test_duration_ms,
            COALESCE(SUM(groq_tokens), 0) AS total_groq_tokens,
            COALESCE(SUM(gemini_tokens), 0) AS total_gemini_tokens
        FROM incident_metrics;
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
            return dict(row) if row else {}
