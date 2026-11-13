"""Shared fixtures for the hermetic test suite.

Nothing here touches the network: every email is built in memory and every
database lives under ``tmp_path`` (or ``:memory:``).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from config import Settings
from fetcher.models import Email
from storage.db import DigestDB


@pytest.fixture
def make_email():
    """Build :class:`fetcher.models.Email` objects with sane defaults."""

    def factory(index: int = 1, **overrides: object) -> Email:
        values: dict[str, object] = {
            "message_id": f"<msg-{index}@example.com>",
            "mailbox": "Gmail",
            "subject": f"Тема {index}",
            "sender": "Отправитель",
            "sender_email": "sender@example.com",
            "date": datetime(2024, 11, 18, 10, 0, tzinfo=UTC),
            "body_text": "Текст письма",
        }
        values.update(overrides)
        return Email(**values)  # type: ignore[arg-type]

    return factory


@pytest.fixture
def make_settings(tmp_path: Path):
    """Build isolated :class:`config.Settings` with mailboxes disabled."""

    def factory(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "_env_file": None,
            "telegram_bot_token": "1234567890:TEST",
            "telegram_chat_id": 1,
            "openrouter_api_key": "sk-test",
            "log_file": "",
            "db_path": str(tmp_path / "digest.db"),
            "heartbeat_path": str(tmp_path / "heartbeat"),
            "lock_path": str(tmp_path / "bot.lock"),
            "gmail_enabled": False,
            "mailru_enabled": False,
            "yandex_enabled": False,
        }
        values.update(overrides)
        return Settings(**values)  # type: ignore[arg-type]

    return factory


@pytest.fixture
def db(tmp_path: Path):
    """A throw-away SQLite database per test."""
    database = DigestDB(str(tmp_path / "test.db"))
    try:
        yield database
    finally:
        database.close()
