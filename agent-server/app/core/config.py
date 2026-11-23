from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database & Redis
    DATABASE_URL: str = "postgresql://incident_user:incident_pass@postgres:5432/incident_db"
    REDIS_URL: str = "redis://redis:6379/0"
    WORKSPACE_DIR: str = "/workspace"

    # Free LLM API Keys & Models
    GROQ_API_KEY: str = ""
    GROQ_FAST_MODEL: str = "llama-3.1-8b-instant"

    GEMINI_API_KEY: str = ""
    GEMINI_PATCH_MODEL: str = "gemini-3.8-flash"

    # Discord
    DISCORD_BOT_TOKEN: str = ""
    DISCORD_ALERT_CHANNEL_ID: int = 0

    # Target GitHub Repository
    GITHUB_TOKEN: str = ""
    TARGET_REPO_OWNER: str = ""
    TARGET_REPO_NAME: str = ""
    TARGET_BASE_BRANCH: str = "main"

    # Pipeline & Rate-Limit Guardrails
    DEDUP_TTL_SECONDS: int = 600
    MAX_PATCH_RETRIES: int = 3
    EMBEDDING_MODEL_NAME: str = "sentence-transformers/all-MiniLM-L6-v2"

    # Redis Streams Settings
    REDIS_STREAM_NAME: str = "incident:stream"
    REDIS_CONSUMER_GROUP: str = "incident-workers"
    REDIS_CONSUMER_NAME: str = "worker-1"

    @property
    def target_repo_path(self) -> Path:
        return Path(self.WORKSPACE_DIR) / self.TARGET_REPO_NAME

    @property
    def target_repo_clone_url(self) -> str:
        if self.GITHUB_TOKEN:
            return f"https://x-access-token:{self.GITHUB_TOKEN}@github.com/{self.TARGET_REPO_OWNER}/{self.TARGET_REPO_NAME}.git"
        return f"https://github.com/{self.TARGET_REPO_OWNER}/{self.TARGET_REPO_NAME}.git"


@lru_cache
def get_settings() -> Settings:
    return Settings()
