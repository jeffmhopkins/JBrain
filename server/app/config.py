"""Environment-driven settings (loaded from the .env file via docker compose)."""
from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and the ``.env`` file."""

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

    # Local LLM (Ollama / any OpenAI-compatible server). When LLM_LOCAL_ENABLE is on, a
    # task tier whose model id is local (an Ollama 'name:tag' carrying a ':', or a
    # configured prefix) routes to the local server while cloud tiers stay on Anthropic —
    # the hybrid the CPU box wants (offload the `cheap` tier; keep the agent on the API).
    # The base URL is the OpenAI-compat /v1 path; the admin URL is the Ollama root used
    # for model list/pull/readiness. Both default to the in-compose `ollama` service.
    llm_local_enable: bool = Field(False, validation_alias=AliasChoices("LLM_LOCAL_ENABLE", "llm_local_enable"))
    llm_local_base_url: str = Field("http://ollama:11434/v1",
                                    validation_alias=AliasChoices("LLM_LOCAL_BASE_URL", "llm_local_base_url"))
    llm_local_admin_url: str = Field("http://ollama:11434",
                                     validation_alias=AliasChoices("LLM_LOCAL_ADMIN_URL", "llm_local_admin_url"))
    llm_local_model: str = Field("", validation_alias=AliasChoices("LLM_LOCAL_MODEL", "llm_local_model"))
    # Extra comma-separated id markers that count as local beyond the ':' tag rule
    # (for tagless names like 'mistral'). Matched as case-insensitive prefixes.
    llm_local_prefixes: str = Field("", validation_alias=AliasChoices("LLM_LOCAL_PREFIXES", "llm_local_prefixes"))
    # When a local-tier call fails and a cloud provider has credentials, retry on the
    # cloud default rather than erroring — so a local outage degrades, never breaks.
    llm_local_fallback: bool = Field(True, validation_alias=AliasChoices("LLM_LOCAL_FALLBACK", "llm_local_fallback"))
    # Per-request LLM timeout (seconds). CPU-only local inference can exceed the cloud
    # default — raise to ~600 when running local models on a CPU box.
    llm_timeout_seconds: float = Field(120.0, validation_alias=AliasChoices("LLM_TIMEOUT_SECONDS", "llm_timeout_seconds"))
    # Provider SDK retry budget per request. The SDK default (2) silently triples the effective
    # wait on a slow/overloaded provider, since llm_timeout_seconds is per-attempt — a chat that
    # "just hangs" for minutes. Keep it low so a wedged provider surfaces an error quickly.
    llm_max_retries: int = Field(1, validation_alias=AliasChoices("LLM_MAX_RETRIES", "llm_max_retries"))

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

    # Video → vision-summary frame sampling. Use the two together: VIDEO_FRAME_INTERVAL sets the
    # cadence — a TIME interval ("30s" → a frame every 30 seconds) or a PERCENT step ("25%" → at
    # 0/25/50/75/100% of the clip) — and VIDEO_FRAME_MAX is the HARD CAP that bounds spend (all
    # frames go in ONE vision call, so the cap is your cost ceiling). If the cadence would exceed
    # the cap, frames re-space evenly across the clip. VIDEO_FRAME_MAX=0 turns frame vision OFF
    # (transcript only — no vision call at all).
    video_frame_interval: str = Field("30s", validation_alias=AliasChoices("VIDEO_FRAME_INTERVAL", "video_frame_interval"))
    video_frame_max: int = Field(8, validation_alias=AliasChoices("VIDEO_FRAME_MAX", "video_frame_max"))

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
        """Return True if an Anthropic/LLM API key is configured."""
        return bool(self.llm_api_key)

    @property
    def has_xai(self) -> bool:
        """Return True if xAI (Grok) is available via an explicit key or provider setting."""
        # An explicit xAI key, or the legacy single-key path (LLM_PROVIDER=xai).
        return bool(self.xai_api_key) or self.llm_provider.lower() in ("xai", "grok")

    @property
    def has_local(self) -> bool:
        """Return True if local-LLM support is enabled and a base URL is set."""
        return bool(self.llm_local_enable and self.llm_local_base_url)

    @property
    def has_llm(self) -> bool:
        """Return True if any LLM provider (Anthropic, xAI, or local) is configured."""
        return self.has_anthropic or self.has_xai or self.has_local

    # Backward-compatible aliases (read-only) for the old Anthropic-specific
    # names, so any not-yet-migrated reader keeps working.
    @property
    def anthropic_api_key(self) -> str:
        """Return the LLM API key (backward-compatible alias for ``llm_api_key``)."""
        return self.llm_api_key

    @property
    def anthropic_model(self) -> str:
        """Return the LLM model name (backward-compatible alias for ``llm_model``)."""
        return self.llm_model


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    Returns:
        The ``Settings`` instance, constructed once and cached for the process lifetime.
    """
    return Settings()
