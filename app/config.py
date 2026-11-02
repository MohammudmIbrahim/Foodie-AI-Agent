"""Typed configuration loaded from environment / .env with safe defaults."""

from __future__ import annotations

from functools import cached_property
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_PEPPER = "dev-only-pepper-not-for-production"
JEV_SCHEMAS = ("typesafe_systemone", "jevtypesafe_decide")


class ConfigError(RuntimeError):
    """Raised at startup when configuration is unsafe or incomplete."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: Literal["development", "test", "production"] = "development"
    host: str = "127.0.0.1"
    port: int = 5050
    database_url: str = "sqlite:///./data/foodie.db"
    log_level: str = "INFO"
    id_hash_pepper: str = ""

    message_provider: Literal["mock", "bluebubbles"] = "mock"
    bb_base_url: str = "http://127.0.0.1:1234"
    bb_password: str = ""
    bb_webhook_secret: str = ""
    bb_send_method: str = "apple-script"
    bb_timeout_seconds: float = 10.0

    allowed_chat_guids: str = ""
    setup_senders: str = ""

    decision_provider: Literal["rules", "jev"] = "rules"
    jev_enabled: bool = False
    jev_api_key: str = ""
    jev_schema: str = ""
    jev_base_url: str = ""
    jev_endpoint: str = ""
    jev_model: str = ""
    jev_timeout_seconds: float = 4.0
    jev_min_confidence: float = 0.6
    jev_max_calls_per_session: int = 4
    jev_max_calls_per_day: int = 200

    places_provider: Literal["mock", "osm"] = "mock"
    enable_live_search: bool = False
    places_fixture_path: str = "tests/fixtures/osm_snapshot.json"
    geocode_fixture_path: str = "tests/fixtures/geocode_snapshot.json"
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"
    osm_user_agent: str = "FoodieAI-local-MVP/0.1"
    places_timeout_seconds: float = 45.0  # public Overpass often takes ~30 s
    places_cache_seconds: int = 900

    default_city: str = ""
    default_radius_miles: float = 5
    max_radius_miles: float = 15
    timezone: str = ""

    session_idle_minutes: float = 10
    collect_seconds: float = 15
    debounce_seconds: float = 1.5
    poll_minutes: float = 10
    group_consent_min: int = 2
    retention_minutes: float = 60
    outbox_max_attempts: int = 2
    outbox_ttl_seconds: int = 120

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if self.decision_provider == "jev" and not self.jev_enabled:
            raise ConfigError("DECISION_PROVIDER=jev requires JEV_ENABLED=true.")
        if self.jev_enabled:
            if not self.jev_api_key:
                raise ConfigError(
                    "JEV_ENABLED=true but JEV_API_KEY is empty. Refusing to start rather than "
                    "use an unintended or unconfigured paid provider."
                )
            if self.jev_schema not in JEV_SCHEMAS:
                raise ConfigError(f"JEV_SCHEMA must be one of {JEV_SCHEMAS} when Jev is enabled.")
            if not self.jev_base_url.startswith("https://") or not self.jev_endpoint:
                raise ConfigError("JEV_BASE_URL (https) and JEV_ENDPOINT are required for Jev.")
            if self.jev_schema == "typesafe_systemone" and not self.jev_model:
                raise ConfigError("JEV_MODEL is required for the typesafe_systemone schema.")
        if self.places_provider == "osm" and not self.enable_live_search:
            raise ConfigError("PLACES_PROVIDER=osm requires ENABLE_LIVE_SEARCH=true.")
        if self.app_env == "production":
            if not self.id_hash_pepper or self.id_hash_pepper == DEV_PEPPER:
                raise ConfigError("ID_HASH_PEPPER must be set to a secret value in production.")
            if self.message_provider == "bluebubbles" and not self.bb_webhook_secret:
                raise ConfigError("BB_WEBHOOK_SECRET is required for the BlueBubbles webhook.")
        if self.message_provider == "bluebubbles" and not (self.bb_password and self.bb_webhook_secret):
            raise ConfigError("MESSAGE_PROVIDER=bluebubbles requires BB_PASSWORD and BB_WEBHOOK_SECRET.")
        if not 0 < self.default_radius_miles <= self.max_radius_miles:
            raise ConfigError("DEFAULT_RADIUS_MILES must be > 0 and <= MAX_RADIUS_MILES.")
        return self

    @cached_property
    def allowed_chats(self) -> frozenset[str]:
        return frozenset(s.strip() for s in self.allowed_chat_guids.split(",") if s.strip())

    @cached_property
    def setup_sender_set(self) -> frozenset[str]:
        return frozenset(s.strip().lower() for s in self.setup_senders.split(",") if s.strip())

    @property
    def pepper(self) -> str:
        return self.id_hash_pepper or DEV_PEPPER

    @property
    def sqlite_path(self) -> str:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            raise ConfigError("Only sqlite:/// DATABASE_URL values are supported in the MVP.")
        return self.database_url[len(prefix):]

    @property
    def jev_active(self) -> bool:
        return self.jev_enabled and self.decision_provider == "jev"
