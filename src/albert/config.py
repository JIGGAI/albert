from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ALBERT_", env_file=".env", extra="ignore", case_sensitive=False
    )

    database_url: str = "sqlite:///./albert.sqlite3"
    api_key_pepper: SecretStr = SecretStr("development-only-change-me")
    public_base_url: str = "http://localhost:8080"
    log_level: str = "INFO"

    embedding_provider: Literal["hashing", "sentence-transformers", "openai-compatible"] = "hashing"
    embedding_dimensions: int = Field(default=384, ge=32, le=2000)
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_request_dimensions: bool = False

    llm_provider: Literal["none", "openai-compatible"] = "none"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: SecretStr | None = None
    classification_model: str | None = None

    worker_poll_seconds: float = Field(default=2.0, ge=0.1, le=60)
    lock_default_ttl_seconds: int = Field(default=900, ge=10, le=86400)
    max_search_limit: int = Field(default=100, ge=1, le=500)
    min_vector_similarity: float = Field(default=0.2, ge=-1, le=1)

    @field_validator("api_key_pepper")
    @classmethod
    def validate_pepper(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) < 32 and raw != "development-only-change-me":
            raise ValueError("ALBERT_API_KEY_PEPPER must contain at least 32 characters")
        return value

    @field_validator("openai_api_key", "classification_model", mode="before")
    @classmethod
    def empty_optional_strings(cls, value: object) -> object | None:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_provider_configuration(self) -> Settings:
        if self.embedding_provider == "openai-compatible" and self.openai_api_key is None:
            raise ValueError("ALBERT_OPENAI_API_KEY is required for openai-compatible embeddings")
        if self.llm_provider == "openai-compatible":
            if self.openai_api_key is None:
                raise ValueError(
                    "ALBERT_OPENAI_API_KEY is required for openai-compatible classification"
                )
            if self.classification_model is None:
                raise ValueError(
                    "ALBERT_CLASSIFICATION_MODEL is required for openai-compatible classification"
                )
        return self

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")


@lru_cache
def get_settings() -> Settings:
    return Settings()
