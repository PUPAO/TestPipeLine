import asyncio
import json
import logging
import re
from typing import Any, Optional

from fastembed import TextEmbedding
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_embedder: Optional[TextEmbedding] = None
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"


def get_embedder() -> TextEmbedding:
    global _embedder
    if _embedder is None:
        settings = get_settings()
        model_name = (
            DEFAULT_FASTEMBED_MODEL
            if settings.EMBEDDING_MODEL_NAME.startswith("sentence-transformers/")
            else settings.EMBEDDING_MODEL_NAME
        )
        logger.info("Loading lightweight ONNX embedding model: %s", model_name)
        _embedder = TextEmbedding(model_name=model_name)
    return _embedder


def embed_text(text: str) -> list[float]:
    model = get_embedder()
    vector = next(model.embed([text]))
    return vector.tolist()


def embed_texts_batch(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = get_embedder()
    return [v.tolist() for v in model.embed(texts, batch_size=16)]


def _extract_token_count(response: Any) -> int:
    usage = getattr(response, "usage_metadata", None)
    if isinstance(usage, dict):
        return int(usage.get("total_tokens", 0))
    meta = getattr(response, "response_metadata", {}) or {}
    token_usage = meta.get("token_usage") or meta.get("usage_metadata") or {}
    return int(token_usage.get("total_tokens", 0) or token_usage.get("totalTokenCount", 0))


def _strip_code_fences(raw: str) -> str:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json|diff|patch)?\s*\n", "", cleaned)
    cleaned = re.sub(r"\n```\s*$", "", cleaned)
    return cleaned.strip()


async def classify_incident_with_groq(
    exception_class: str,
    message: str,
    stack_trace: str,
    request_uri: str,
) -> tuple[str, str, int]:
    """경량 모델(Groq)로 에러 카테고리와 RAG 필터링용 도메인 태그를 추출합니다."""
    settings = get_settings()
    llm = ChatGroq(
        api_key=settings.GROQ_API_KEY,
        model=settings.GROQ_FAST_MODEL,
        temperature=0.0,
        max_tokens=256,
    )

    top_trace = "\n".join(stack_trace.splitlines()[:25])
    system_prompt = (
        "You are a Spring Boot incident triage classifier. "
        "Analyze the error and respond ONLY with a valid JSON object containing two keys:\n"
        '1. "error_category": one of ["NPE", "SQL_EXCEPTION", "VALIDATION", "CONCURRENCY", "EXTERNAL_API", "AUTH", "BUSINESS_LOGIC", "UNKNOWN"]\n'
        '2. "target_domain": lowercase domain package keyword extracted from stack trace or URI (e.g., "auth", "review", "timeline", "payment", "member", "general").'
    )
    user_prompt = (
        f"URI: {request_uri}\n"
        f"Exception: {exception_class}\n"
        f"Message: {message}\n"
        f"StackTrace (Top 25 lines):\n{top_trace}"
    )

    for attempt in range(3):
        try:
            resp = await llm.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
            tokens = _extract_token_count(resp)
            parsed = json.loads(_strip_code_fences(str(resp.content)))
            category = str(parsed.get("error_category", "UNKNOWN")).upper()
            domain = str(parsed.get("target_domain", "general")).lower().strip()
            return category, domain, tokens
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            logger.warning("Groq classification fallback triggered: %s", e)
            return "UNKNOWN", "general", 0

    return "UNKNOWN", "general", 0


async def generate_patch_with_gemini(
    exception_class: str,
    message: str,
    stack_trace: str,
    retrieved_files: list[dict[str, Any]],
    previous_diff: str = "",
    test_error_log: Optional[str] = None,
) -> tuple[str, str, int]:
    """고성능 모델(Gemini Flash)로 근본 원인을 분석하고 Unified Git Diff 패치를 생성합니다."""
    settings = get_settings()
    llm = ChatGoogleGenerativeAI(
        google_api_key=settings.GEMINI_API_KEY,
        model=settings.GEMINI_PATCH_MODEL,
        temperature=0.1,
    )

    context_blocks = []
    for item in retrieved_files:
        context_blocks.append(
            f"### File: {item['file_path']} (Domain: {item['domain_tag']})\n"
            f"```java\n{item['content']}\n```"
        )
    rag_context = "\n\n".join(context_blocks) if context_blocks else "No matching repository files retrieved."

    retry_section = ""
    if test_error_log:
        retry_section = (
            "\n\n[PREVIOUS ATTEMPT FAILED]\n"
            f"Previous Diff:\n```diff\n{previous_diff}\n```\n"
            f"Compilation / JUnit Failure Log:\n```\n{test_error_log[:3000]}\n```\n"
            "Fix the root cause while resolving the compilation or regression test failure above."
        )

    system_prompt = (
        "You are a senior Java 21 / Spring Boot backend engineer. "
        "Given an incident stack trace and source files, produce a minimal, safe bug fix.\n"
        "Respond ONLY with a valid JSON object with two keys:\n"
        '1. "explanation": Korean summary of the root cause and fix (2-4 sentences).\n'
        '2. "unified_diff": Standard unified git diff (`--- a/src/...`, `+++ b/src/...`, `@@ ... @@`) '
        "that can be cleanly applied with `git apply`. Ensure exact context lines match the provided source files."
    )
    user_prompt = (
        f"Exception: {exception_class}\n"
        f"Message: {message}\n"
        f"Stack Trace:\n{stack_trace[:4000]}\n\n"
        f"Relevant Source Files:\n{rag_context}"
        f"{retry_section}"
    )

    for attempt in range(3):
        try:
            resp = await llm.ainvoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
            tokens = _extract_token_count(resp)
            parsed = json.loads(_strip_code_fences(str(resp.content)))
            explanation = str(parsed.get("explanation", "원인 분석 완료")).strip()
            diff_text = _strip_code_fences(str(parsed.get("unified_diff", "")))
            return explanation, diff_text, tokens
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                await asyncio.sleep(3 ** (attempt + 1))
                continue
            raise RuntimeError(f"Gemini patch generation failed: {e}") from e

    raise RuntimeError("Gemini patch generation exhausted retries.")
