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
    # xAI (Grok) — OpenAI-compatible. Set XAI_API_KEY to make grok-* models selectable
    # alongside Claude; the provider is inferred from the chosen model id.
    xai_api_key: str = Field("", validation_alias=AliasChoices("XAI_API_KEY", "xai_api_key"))
    xai_base_url: str = Field("https://api.x.ai/v1", validation_alias=AliasChoices("XAI_BASE_URL", "xai_base_url"))

    # The pasteable access key (the "cert"). If set, it is authoritative and
    # seeded/rotated into the DB on boot. If empty, the server generates one on
    # first run and reveals it once (logs + /data/access-key.txt).
    jbrain_access_key: str = ""

    # Allowed CORS origins (comma-separated) for a separately-hosted PWA, e.g.
    # GitHub Pages. "*" is safe here because auth is a bearer token, not cookies.
    jbrain_cors_origins: str = "*"

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    db_path: str = "/data/brain.db"

    # Street-address geocoding (JBrain's first outside source). Base URL of a Nominatim
    # (OpenStreetMap) server — the public instance by default. Reverse/forward lookups
    # send coordinates/queries here (cached, so a spot is sent at most once). Point it at
    # a self-hosted Nominatim to keep coordinates on your own infra; set blank to disable.
    geocoder_url: str = Field("https://nominatim.openstreetmap.org",
                              validation_alias=AliasChoices("GEOCODER_URL", "geocoder_url"))

    # Local speech-to-text (faster-whisper). Audio attachments are transcribed on a
    # background thread with no external API key — same ethos as the local embeddings.
    # Model size trades accuracy for RAM/speed: tiny | base | small | medium | large-v3.
    # "base" is a good CPU default; drop to "tiny" on a tight (2 GB) box. compute_type
    # "int8" keeps the CPU footprint small.
    audio_model: str = Field("base", validation_alias=AliasChoices("AUDIO_MODEL", "audio_model"))
    audio_compute_type: str = Field("int8", validation_alias=AliasChoices("AUDIO_COMPUTE_TYPE", "audio_compute_type"))

    # Web Push (VAPID). Keys auto-generate on first boot into the DB `meta` table
    # if these are blank, so existing installs gain push with zero config. Set
    # them here only to pin a keypair across machines/restores. `vapid_subject` is
    # the required contact URL sent to push services.
    vapid_subject: str = Field("mailto:admin@localhost", validation_alias=AliasChoices("VAPID_SUBJECT", "vapid_subject"))
    vapid_private_key: str = Field("", validation_alias=AliasChoices("VAPID_PRIVATE_KEY", "vapid_private_key"))
    vapid_public_key: str = Field("", validation_alias=AliasChoices("VAPID_PUBLIC_KEY", "vapid_public_key"))

    # Loaded from the JBRAIN_DOMAIN env var; used for cookie/CORS context.
    jbrain_domain: str = "localhost"

    @property
    def has_anthropic(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def has_xai(self) -> bool:
        # An explicit xAI key, or the legacy single-key path (LLM_PROVIDER=xai).
        return bool(self.xai_api_key) or self.llm_provider.lower() in ("xai", "grok")

    @property
    def has_llm(self) -> bool:
        return self.has_anthropic or self.has_xai

    # Backward-compatible aliases (read-only) for the old Anthropic-specific
    # names, so any not-yet-migrated reader keeps working.
    @property
    def anthropic_api_key(self) -> str:
        return self.llm_api_key

    @property
    def anthropic_model(self) -> str:
        return self.llm_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
