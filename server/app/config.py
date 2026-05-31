"""Environment-driven settings (loaded from the .env file via docker compose)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    brain_name: str = "My Brain"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    admin_username: str = "admin"
    admin_password: str = "change-me-please"
    session_secret: str = "dev-insecure-secret-change-me"

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    db_path: str = "/data/brain.db"

    # Loaded from the JBRAIN_DOMAIN env var; used for cookie/CORS context.
    jbrain_domain: str = "localhost"

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
