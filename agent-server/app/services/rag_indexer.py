import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.database import (
    get_existing_chunk_hashes,
    search_similar_code,
    upsert_code_chunk,
)
from app.services.git_runner import ensure_repo_cloned
from app.services.llm_router import embed_text, embed_texts_batch

logger = logging.getLogger(__name__)

IGNORED_DIRS = {"global", "common", "config", "util", "infra", "exception", "dto", "entity", "repository", "service", "controller", "domain"}


def _infer_domain_tag(rel_path: str) -> str:
    """파일 경로(예: src/main/java/com/remine/review/service/ReviewService.java)에서 도메인 태그를 추출합니다."""
    parts = Path(rel_path).parts
    if "java" in parts:
        idx = parts.index("java")
        package_parts = [p.lower() for p in parts[idx + 1 : -1]]
        # com.company.<domain>... 패턴 우선 탐색
        candidates = [p for p in package_parts[2:] if p not in IGNORED_DIRS]
        if candidates:
            return candidates[0]
    return "general"


async def sync_repository_embeddings() -> dict[str, Any]:
    """타겟 저장소에서 변경된 Java 소스만 선별해 증분 임베딩을 수행합니다."""
    repo_path = await ensure_repo_cloned()
    src_root = repo_path / "src" / "main" / "java"
    if not src_root.exists():
        return {"scanned": 0, "updated": 0, "skipped": 0}

    existing_hashes = await get_existing_chunk_hashes()
    to_embed: list[dict[str, str]] = []
    scanned = 0
    skipped = 0

    for java_file in src_root.rglob("*.java"):
        scanned += 1
        rel_path = java_file.relative_to(repo_path).as_posix()
        content = java_file.read_text(encoding="utf-8", errors="ignore")
        if not content.strip():
            skipped += 1
            continue

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if existing_hashes.get(rel_path) == content_hash:
            skipped += 1
            continue

        to_embed.append(
            {
                "file_path": rel_path,
                "class_name": java_file.stem,
                "domain_tag": _infer_domain_tag(rel_path),
                "content": content[:12000],
                "content_hash": content_hash,
            }
        )

    if to_embed:
        texts = [f"File: {item['file_path']}\n{item['content']}" for item in to_embed]
        vectors = await asyncio.to_thread(embed_texts_batch, texts)
        for item, vec in zip(to_embed, vectors):
            await upsert_code_chunk(
                file_path=item["file_path"],
                class_name=item["class_name"],
                domain_tag=item["domain_tag"],
                content=item["content"],
                content_hash=item["content_hash"],
                embedding=vec,
            )

    logger.info("RAG incremental sync complete: scanned=%d, updated=%d, skipped=%d", scanned, len(to_embed), skipped)
    return {"scanned": scanned, "updated": len(to_embed), "skipped": skipped}


async def retrieve_relevant_code(
    exception_class: str,
    message: str,
    stack_trace: str,
    domain_tag: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """스택 트레이스에 직접 등장한 파일 + 도메인 필터링 pgvector 유사도 검색 결과를 결합합니다."""
    repo_path = await ensure_repo_cloned()
    results: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    # 1. 스택 트레이스에 명시된 로컬 프로젝트 파일 직접 로드 (가장 정확한 컨텍스트)
    src_root = repo_path / "src" / "main" / "java"
    if src_root.exists():
        for java_file in src_root.rglob("*.java"):
            if java_file.name in stack_trace and len(results) < 3:
                rel_path = java_file.relative_to(repo_path).as_posix()
                seen_paths.add(rel_path)
                results.append(
                    {
                        "file_path": rel_path,
                        "class_name": java_file.stem,
                        "domain_tag": _infer_domain_tag(rel_path),
                        "content": java_file.read_text(encoding="utf-8", errors="ignore")[:12000],
                    }
                )

    # 2. pgvector 메타데이터(domain_tag) 선필터링 벡터 검색 보완
    query_text = f"{exception_class}: {message}\n" + "\n".join(stack_trace.splitlines()[:12])
    query_vec = await asyncio.to_thread(embed_text, query_text)
    similar_rows = await search_similar_code(query_vec, domain_tag=domain_tag, limit=limit)

    for row in similar_rows:
        if row["file_path"] not in seen_paths and len(results) < limit:
            seen_paths.add(row["file_path"])
            results.append(row)

    return results
