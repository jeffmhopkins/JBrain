"""Environment-driven settings (loaded from the .env file via docker compose)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    brain_name: str = "My Brain"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # The pasteable access key (the "cert"). If set, it is authoritative and
    # seeded/rotated into the DB on boot. If empty, the server generates one on
    # first run and reveals it once (logs + /data/access-key.txt).
    jbrain_access_key: str = ""

    # Allowed CORS origins (comma-separated) for a separately-hosted PWA, e.g.
    # GitHub Pages. "*" is safe here because auth is a bearer token, not cookies.
    jbrain_cors_origins: str = "*"

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
