import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (or a local .env file)."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="ANVITA_", extra="ignore")

    env: Literal["development", "test", "staging", "production"] = "development"
    debug: bool = False
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    # Public base URL of the web app; used to build pay links, booking pages and invite links.
    app_base_url: str = "http://localhost:3000"
    # Comma-separated in the environment (NoDecode stops pydantic-settings from expecting JSON).
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])

    database_url: str = "postgresql+asyncpg://anvita:anvita@localhost:5432/anvita"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    jwt_secret: SecretStr = SecretStr("dev-only-change-me-dev-only-change-me")
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    webchat_token_ttl_days: int = 30

    # LLM gateway. With no key the runtime uses a deterministic offline model (dev/test only).
    anthropic_api_key: SecretStr | None = None
    llm_conversation_model: str = "claude-sonnet-5-5"
    llm_fast_model: str = "claude-haiku-5-5"
    llm_planner_model: str = "claude-opus-5-5"
    llm_refusal_fallbacks: bool = True
    llm_timeout_seconds: float = 60.0
    agent_max_tool_steps: int = 6
    agent_history_messages: int = 30
    # Users send bursts of short messages; wait this long after the last one before replying.
    agent_debounce_seconds: float = 4.0
    # If a teammate replied within this window the AI stays quiet on the thread.
    agent_human_pause_minutes: int = 30

    worker_poll_interval_seconds: float = 1.0
    worker_batch_size: int = 20
    worker_max_attempts: int = 5

    payments_provider: Literal["test", "razorpay"] = "test"
    razorpay_key_id: str | None = None
    razorpay_key_secret: SecretStr | None = None
    razorpay_webhook_secret: SecretStr | None = None
    max_ai_payment_amount: int = 50_000_00  # minor units (paise): ₹50,000

    # Deterministic stand-ins for paid providers (Apollo, Google Places, Ads, …) so every screen works in
    # development without keys. Ignored in staging/production.
    enable_sample_providers: bool = True

    whatsapp_verify_token: SecretStr | None = None
    whatsapp_app_secret: SecretStr | None = None
    whatsapp_graph_url: str = "https://graph.facebook.com/v21.0"

    rate_limit_auth_per_minute: int = 20
    rate_limit_public_per_minute: int = 60

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            if v.strip().startswith("["):
                return json.loads(v)
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.env in ("staging", "production")

    def validate_for_production(self) -> None:
        if not self.is_production:
            return
        secret = self.jwt_secret.get_secret_value()
        if len(secret) < 32 or secret.startswith("dev-only"):
            raise RuntimeError("ANVITA_JWT_SECRET must be set to a strong random value in production")
        if self.anthropic_api_key is None:
            raise RuntimeError("ANVITA_ANTHROPIC_API_KEY is required in production")
        if self.payments_provider == "test":
            raise RuntimeError("ANVITA_PAYMENTS_PROVIDER=test is not allowed in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
