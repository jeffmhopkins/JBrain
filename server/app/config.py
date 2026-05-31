"""Environment-driven settings (loaded from the .env file via docker compose)."""
from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    brain_name: str = "My Brain"

    # Provider-agnostic LLM config. LLM_* are the canonical env vars; the legacy
    # ANTHROPIC_* names still work (alias) so existing .env files keep running.
    llm_provider: str = Field("anthropic", validation_alias=AliasChoices("LLM_PROVIDER", "llm_provider"))
    llm_api_key: str = Field("", validation_alias=AliasChoices("LLM_API_KEY", "ANTHROPIC_API_KEY", "llm_api_key"))
    llm_model: str = Field("claude-sonnet-4-6", validation_alias=AliasChoices("LLM_MODEL", "ANTHROPIC_MODEL", "llm_model"))

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
    def has_llm(self) -> bool:
        return bool(self.llm_api_key)

    # Backward-compatible aliases (read-only) for the old Anthropic-specific
    # names, so any not-yet-migrated reader keeps working.
    @property
    def anthropic_api_key(self) -> str:
        return self.llm_api_key

    @property
    def anthropic_model(self) -> str:
        return self.llm_model

    @property
    def has_anthropic(self) -> bool:
        return self.has_llm


@lru_cache
def get_settings() -> Settings:
    return Settings()
