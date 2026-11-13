"""
Central configuration — all settings are loaded from `.env` via pydantic-settings.

Every module receives a ``Settings`` instance explicitly; nothing is imported as a
module-level singleton except through :func:`get_settings`, which keeps tests able
to build an isolated configuration.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

log = logging.getLogger(__name__)


def _split_csv(value: object) -> list[str]:
    """Accept ``"a, b ,c"`` (env style) or an already-built list."""
    if value is None:
        return []
    if isinstance(value, str):
        items = [part.strip() for part in value.replace("\n", ",").split(",")]
        return [item for item in items if item]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_bot_token: str = Field(..., min_length=10)
    telegram_chat_id: int
    telegram_timeout: int = 30

    # ── Mailboxes ─────────────────────────────────────────────────────────────
    gmail_enabled: bool = True
    gmail_email: str = ""
    gmail_app_password: str = ""
    gmail_imap_host: str = "imap.gmail.com"
    gmail_imap_port: int = 993
    gmail_imap_folder: str = "INBOX"
    gmail_display_name: str = "Gmail"

    mailru_enabled: bool = True
    mailru_email: str = ""
    mailru_app_password: str = ""
    mailru_imap_host: str = "imap.mail.ru"
    mailru_imap_port: int = 993
    mailru_imap_folder: str = "INBOX"
    mailru_display_name: str = "mail.ru"

    yandex_enabled: bool = True
    yandex_email: str = ""
    yandex_app_password: str = ""
    yandex_imap_host: str = "imap.yandex.com"
    yandex_imap_port: int = 993
    # Display name and IMAP folder are separate on purpose: the folder is a
    # protocol detail, the name is what the user sees in the digest.
    yandex_imap_folder: str = "INBOX"
    yandex_display_name: str = "Яндекс"
    # Deprecated single-field form kept so old .env files still boot.
    yandex_mailbox_name: str = ""

    imap_timeout: int = 60

    # ── LLM provider selection ────────────────────────────────────────────────
    llm_provider: Literal[
        "openrouter", "gonkarouter", "openai", "gemini", "ollama", "gigachat"
    ] = "openrouter"

    # A real UA: Cloudflare in front of GonkaRouter rejects python-urllib/requests.
    llm_user_agent: str = "EmailDigestAgent/2.0"

    # OpenRouter (primary)
    openrouter_api_key: str = ""
    openrouter_api_base: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "particle-ai/particle-1:free"
    openrouter_fallback_models: Annotated[list[str], NoDecode] = Field(default_factory=list)
    openrouter_site_url: str = "https://github.com/leshicorn/email-bot"
    openrouter_app_title: str = "Email Digest Bot"

    # GonkaRouter — fallback when OpenRouter is rate-limited or out of quota
    gonkarouter_enabled: bool = True
    gonkarouter_api_base: str = "https://api.gonkarouter.io/v1"
    gonkarouter_api_key: str = ""
    gonkarouter_model: str = "MiniMaxAI/MiniMax-M2.7"
    gonkarouter_fallback_models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "zai-org/GLM-5.3-Flash",
            "deepseek-ai/DeepSeek-V4-Flash-0731",
        ]
    )

    # Shared LLM behaviour
    llm_timeout: int = 90
    llm_attempts: int = 3
    llm_backoff_base: float = 2.0
    llm_temperature: float = 0.0
    llm_chunk_size: int = Field(default=12, ge=1, le=40)
    llm_max_tokens: int = Field(default=4000, ge=256)

    # OpenAI-compatible (secondary)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_api_base: str = "https://api.openai.com/v1"

    # Gemini (secondary)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    gemini_proxies: str = ""

    # Ollama (secondary)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"

    # GigaChat (secondary)
    gigachat_credentials: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat"

    # ── Scheduling ────────────────────────────────────────────────────────────
    digest_hour: int = Field(default=21, ge=0, le=23)
    digest_minute: int = Field(default=0, ge=0, le=59)
    timezone: str = "Europe/Moscow"

    weekly_digest_enabled: bool = True
    weekly_digest_weekday: str = "sun"
    weekly_digest_hour: int = Field(default=20, ge=0, le=23)
    weekly_digest_minute: int = Field(default=0, ge=0, le=59)

    quiet_when_empty: bool = True
    catchup_max_days: int = Field(default=7, ge=1, le=90)

    # ── Filtering ─────────────────────────────────────────────────────────────
    skip_senders_containing: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "noreply",
            "no-reply",
            "newsletter",
            "unsubscribe",
            "marketing",
            "mailer-daemon",
        ]
    )
    max_body_chars: int = Field(default=3000, ge=200)
    max_prompt_chars: int = Field(default=120_000, ge=2000)
    strip_quoted_replies: bool = True
    max_attachment_names: int = 5
    # Treat List-Unsubscribe senders as newsletters and only count them.
    newsletter_count_only: bool = True

    # ── Storage / ops ─────────────────────────────────────────────────────────
    db_path: str = "data/digest.db"
    retention_days: int = Field(default=90, ge=7)
    log_level: str = "INFO"
    log_file: str = "bot.log"
    heartbeat_path: str = "data/heartbeat"
    lock_path: str = "data/bot.lock"
    single_instance: bool = True
    outbox_flush_interval: int = Field(default=300, ge=30)

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator(
        "openrouter_fallback_models",
        "gonkarouter_fallback_models",
        "skip_senders_containing",
        mode="before",
    )
    @classmethod
    def _parse_csv(cls, value: object) -> list[str]:
        return _split_csv(value)

    @field_validator("digest_hour", "digest_minute", "weekly_digest_hour", "weekly_digest_minute",
                     "llm_chunk_size", "llm_max_tokens", "imap_timeout", "llm_timeout", mode="before")
    @classmethod
    def _parse_int(cls, value: object) -> int:
        return int(value)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - depends on tzdata
            raise ValueError(f"Unknown timezone: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _validate_provider(self) -> Settings:
        if self.llm_provider == "openrouter" and not self.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter")
        if self.llm_provider == "gonkarouter" and not self.gonkarouter_api_key:
            raise ValueError("GONKAROUTER_API_KEY is required when LLM_PROVIDER=gonkarouter")
        if self.llm_provider == "openai" and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        if self.llm_provider == "gemini" and not self.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required when LLM_PROVIDER=gemini")
        if self.llm_provider == "gigachat" and not self.gigachat_credentials:
            raise ValueError("GIGACHAT_CREDENTIALS is required when LLM_PROVIDER=gigachat")
        return self

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def model_chain(self) -> list[str]:
        """Primary model first, then the configured fallbacks (deduplicated)."""
        chain = [self.openrouter_model, *self.openrouter_fallback_models]
        seen: set[str] = set()
        return [m for m in chain if m and not (m in seen or seen.add(m))]

    @property
    def gonka_model_chain(self) -> list[str]:
        """GonkaRouter models: the primary first, then the fallbacks."""
        chain = [self.gonkarouter_model, *self.gonkarouter_fallback_models]
        seen: set[str] = set()
        return [m for m in chain if m and not (m in seen or seen.add(m))]

    @property
    def yandex_folder(self) -> str:
        return self.yandex_imap_folder or "INBOX"

    @property
    def yandex_name(self) -> str:
        return self.yandex_display_name or self.yandex_mailbox_name or "Яндекс"

    def enabled_mailboxes(self) -> list[dict[str, object]]:
        """Normalised mailbox descriptors used by :func:`fetcher.imap.build_fetchers`."""
        boxes: list[dict[str, object]] = []
        if self.gmail_enabled and self.gmail_email:
            boxes.append(
                {
                    "name": self.gmail_display_name or "Gmail",
                    "host": self.gmail_imap_host,
                    "port": self.gmail_imap_port,
                    "email": self.gmail_email,
                    "password": self.gmail_app_password,
                    "folder": self.gmail_imap_folder or "INBOX",
                }
            )
        if self.mailru_enabled and self.mailru_email:
            boxes.append(
                {
                    "name": self.mailru_display_name or "mail.ru",
                    "host": self.mailru_imap_host,
                    "port": self.mailru_imap_port,
                    "email": self.mailru_email,
                    "password": self.mailru_app_password,
                    "folder": self.mailru_imap_folder or "INBOX",
                }
            )
        if self.yandex_enabled and self.yandex_email:
            boxes.append(
                {
                    "name": self.yandex_name,
                    "host": self.yandex_imap_host,
                    "port": self.yandex_imap_port,
                    "email": self.yandex_email,
                    "password": self.yandex_app_password,
                    "folder": self.yandex_folder,
                }
            )
        return boxes


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings instance for entry points (not used by tests)."""
    return Settings()
