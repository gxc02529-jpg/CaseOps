"""Runtime settings loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CASEOPS_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "CaseOps"
    environment: str = "local"
    api_host: str = "0.0.0.0"
    api_port: int = 8080
    similarity_threshold: float = Field(default=0.58, ge=0.0, le=1.0)
    aggregation_window_hours: int = Field(default=72, ge=1, le=720)
    approval_actions: str = "refund,delete_data,change_permission"
    model_base_url: str = "https://api.deepseek.com/v1"
    model_name: str = "deepseek-chat"
    model_api_key: str = ""
    redis_url: str = "redis://localhost:6379/0"
    postgres_dsn: str = "postgresql+psycopg://caseops:caseops@localhost:5432/caseops"
    milvus_uri: str = "http://localhost:19530"

    @property
    def approval_action_set(self) -> frozenset[str]:
        return frozenset(item.strip() for item in self.approval_actions.split(",") if item.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

