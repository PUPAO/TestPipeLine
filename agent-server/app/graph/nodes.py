from app.bot.discord_bot import post_approval_card, post_thread_update
from app.core.database import update_incident_metric
from app.core.redis_client import save_pending_approval
from app.graph.state import AgentState
from app.services.git_runner import apply_patch_and_run_tests, ensure_repo_cloned
from app.services.impact_analyzer import analyze_test_impact
from app.services.llm_router import (
    classify_incident_with_groq,
    generate_patch_with_gemini,
)
from app.services.rag_indexer import retrieve_relevant_code


async def classify_node(state: AgentState) -> dict:
    await update_incident_metric(state["incident_id"], status="ANALYZING")
    category, domain, tokens = await classify_incident_with_groq(
        exception_class=state["exception_class"],
        message=state["message"],
        stack_trace=state["stack_trace"],
        request_uri=state["request_uri"],
    )

    total_groq = state.get("groq_tokens", 0) + tokens
    await update_incident_metric(
        state["incident_id"],
        error_category=category,
        target_domain=domain,
        groq_tokens=total_groq,
    )
    await post_thread_update(
        state.get("discord_thread_id"),
        f"🔍 **[1/4] 경량 분류 완료 (Groq)** — 유형: `{category}` | 도메인: `{domain}`",
    )
    return {
        "error_category": category,
        "target_domain": domain,
        "groq_tokens": total_groq,
    }


async def retrieve_node(state: AgentState) -> dict:
    files = await retrieve_relevant_code(
        exception_class=state["exception_class"],
        message=state["message"],
        stack_trace=state["stack_trace"],
        domain_tag=state["target_domain"],
        limit=5,
    )
    file_names = ", ".join(f"`{f['class_name']}`" for f in files) or "없음"
    await post_thread_update(
        state.get("discord_thread_id"),
        f"📚 **[2/4] 코드 컨텍스트 검색 완료 (pgvector)** — 참조 클래스: {file_names}",
    )
    return {"retrieved_files": files}


async def generate_patch_node(state: AgentState) -> dict:
    attempt_num = state.get("retry_count", 0) + 1
    await post_thread_update(
        state.get("discord_thread_id"),
        f"🧠 **[3/4] 패치 생성 중 (Gemini Flash, 시도 {attempt_num}회차)**...",
    )

    explanation, diff_text, tokens = await generate_patch_with_gemini(
        exception_class=state["exception_class"],
        message=state["message"],
        stack_trace=state["stack_trace"],
        retrieved_files=state.get("retrieved_files", []),
        previous_diff=state.get("patch_diff", ""),
        test_error_log=state.get("test_error_log"),
    )

    total_gemini = state.get("gemini_tokens", 0) + tokens
    await update_incident_metric(state["incident_id"], gemini_tokens=total_gemini)

    return {
        "patch_explanation": explanation,
        "patch_diff": diff_text,
        "gemini_tokens": total_gemini,
    }


async def analyze_and_test_node(state: AgentState) -> dict:
    await update_incident_metric(state["incident_id"], status="TESTING")
    repo_path = await ensure_repo_cloned()

    affected_tests, total_tests = analyze_test_impact(repo_path, state["patch_diff"])
    executed_count = len(affected_tests)

    await post_thread_update(
        state.get("discord_thread_id"),
        f"🧪 **[4/4] 영향도 분석 및 선별 회귀 테스트 실행** — 전체 {total_tests}개 중 **{executed_count}개** 선별 검증 중...",
    )

    passed, branch_name, error_log, duration_ms = await apply_patch_and_run_tests(
        incident_id=state["incident_id"],
        patch_diff=state["patch_diff"],
        affected_tests=affected_tests,
    )

    next_retry = state.get("retry_count", 0) if passed else state.get("retry_count", 0) + 1
    await update_incident_metric(
        state["incident_id"],
        retry_count=next_retry,
        total_tests_count=total_tests,
        executed_tests_count=executed_count,
        test_duration_ms=duration_ms,
    )

    if not passed:
        await post_thread_update(
            state.get("discord_thread_id"),
            f"⚠️ **회귀 테스트 실패 (누적 실패 {next_retry}회)** — 실패 로그를 기반으로 패치를 재수정합니다.\n```\n{error_log[:800]}\n```",
        )

    return {
        "branch_name": branch_name,
        "affected_tests": affected_tests,
        "total_tests_count": total_tests,
        "executed_tests_count": executed_count,
        "test_duration_ms": duration_ms,
        "test_passed": passed,
        "test_error_log": error_log if not passed else None,
        "retry_count": next_retry,
    }


async def request_approval_node(state: AgentState) -> dict:
    await update_incident_metric(state["incident_id"], status="AWAITING_APPROVAL")

    approval_payload = {
        "incident_id": state["incident_id"],
        "error_hash": state["error_hash"],
        "error_category": state["error_category"],
        "target_domain": state["target_domain"],
        "branch_name": state["branch_name"],
        "patch_diff": state["patch_diff"],
        "patch_explanation": state["patch_explanation"],
        "affected_tests": state["affected_tests"],
        "executed_tests_count": state["executed_tests_count"],
        "total_tests_count": state["total_tests_count"],
        "discord_thread_id": state.get("discord_thread_id"),
    }
    await save_pending_approval(state["incident_id"], approval_payload)
    await post_approval_card(state.get("discord_thread_id"), approval_payload)
    return {}


async def escalate_node(state: AgentState) -> dict:
    await update_incident_metric(state["incident_id"], status="ESCALATED", resolved=True)
    await post_thread_update(
        state.get("discord_thread_id"),
        f"🚨 **자동 복구 한도 초과 ({state['retry_count']}회 실패)** — 수동 확인이 필요합니다.\n"
        f"마지막 실패 로그:\n```\n{(state.get('test_error_log') or '')[:1000]}\n```",
    )
    return {}
