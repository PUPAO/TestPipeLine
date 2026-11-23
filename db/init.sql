CREATE EXTENSION IF NOT EXISTS vector;

-- 1. 증분 임베딩 기반 코드 RAG 저장소
CREATE TABLE IF NOT EXISTS code_chunks (
    id SERIAL PRIMARY KEY,
    file_path VARCHAR(512) NOT NULL,
    class_name VARCHAR(255) NOT NULL,
    domain_tag VARCHAR(64) NOT NULL,
    content TEXT NOT NULL,
    content_hash VARCHAR(64) NOT NULL,
    embedding vector(384),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT uq_code_chunks_file_class UNIQUE (file_path, class_name)
);

-- 메타데이터 선필터링 + 코사인 유사도 HNSW 인덱스
CREATE INDEX IF NOT EXISTS idx_code_chunks_domain ON code_chunks (domain_tag);
CREATE INDEX IF NOT EXISTS idx_code_chunks_embedding
    ON code_chunks USING hnsw (embedding vector_cosine_ops);

-- 2. 장애 대응 파이프라인 정량 지표 기록 테이블 (이력서 수치 산출용)
CREATE TABLE IF NOT EXISTS incident_metrics (
    incident_id VARCHAR(64) PRIMARY KEY,
    error_hash VARCHAR(64) NOT NULL,
    error_category VARCHAR(64),
    target_domain VARCHAR(64),
    status VARCHAR(32) NOT NULL,          -- QUEUED, ANALYZING, TESTING, AWAITING_APPROVAL, PR_CREATED, REJECTED, ESCALATED
    retry_count INT DEFAULT 0,
    total_tests_count INT DEFAULT 0,
    executed_tests_count INT DEFAULT 0,
    test_duration_ms INT DEFAULT 0,
    groq_tokens INT DEFAULT 0,
    gemini_tokens INT DEFAULT 0,
    pr_url VARCHAR(512),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    resolved_at TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_incident_metrics_status ON incident_metrics (status);
CREATE INDEX IF NOT EXISTS idx_incident_metrics_created_at ON incident_metrics (created_at DESC);
