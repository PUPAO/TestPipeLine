from typing import Any, Optional, TypedDict


class AgentState(TypedDict):
    # 기본 장애 컨텍스트
    incident_id: str
    error_hash: str
    service_name: str
    environment: str
    exception_class: str
    message: str
    stack_trace: str
    http_method: str
    request_uri: str
    discord_thread_id: Optional[str]

    # 1단계: Groq 경량 분류 결과
    error_category: str
    target_domain: str

    # 2단계: pgvector RAG 컨텍스트
    retrieved_files: list[dict[str, Any]]

    # 3단계: Gemini Flash 생성 패치
    patch_diff: str
    patch_explanation: str

    # 4단계: 영향도 분석 및 선별 회귀 테스트 루프
    branch_name: str
    affected_tests: list[str]
    total_tests_count: int
    executed_tests_count: int
    test_duration_ms: int
    test_passed: bool
    test_error_log: Optional[str]
    retry_count: int

    # 정량 지표 누적 (이력서 기재용)
    groq_tokens: int
    gemini_tokens: int
