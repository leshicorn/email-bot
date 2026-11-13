"""Tests for :mod:`config` — parsing, derived values and validation."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from config import Settings


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "telegram_bot_token": "1234567890:TEST",
        "telegram_chat_id": 1,
        "openrouter_api_key": "sk-test",
        "db_path": str(tmp_path / "t.db"),
        "log_file": "",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_settings_builds_with_minimal_args(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    assert settings.telegram_bot_token == "1234567890:TEST"
    assert settings.telegram_chat_id == 1
    assert settings.llm_provider == "openrouter"
    assert settings.db_path == str(tmp_path / "t.db")
    assert settings.digest_hour == 21


def test_comma_separated_fallback_models_are_parsed(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path, openrouter_fallback_models=" fallback-a, fallback-b ,, fallback-a "
    )
    assert settings.openrouter_fallback_models == ["fallback-a", "fallback-b", "fallback-a"]


def test_comma_separated_skip_patterns_are_parsed(tmp_path: Path) -> None:
    settings = _settings(tmp_path, skip_senders_containing="noreply, promo ,\n digest")
    assert settings.skip_senders_containing == ["noreply", "promo", "digest"]


def test_model_chain_dedupes_and_keeps_primary_first(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        openrouter_model="primary/model",
        openrouter_fallback_models=["secondary/model", "primary/model", "third/model"],
    )
    assert settings.model_chain == ["primary/model", "secondary/model", "third/model"]


def test_yandex_folder_and_name_fall_back_to_legacy_value(tmp_path: Path) -> None:
    legacy = _settings(
        tmp_path,
        yandex_imap_folder="",
        yandex_display_name="",
        yandex_mailbox_name="Legacy",
    )
    assert legacy.yandex_folder == "INBOX"
    assert legacy.yandex_name == "Legacy"

    explicit = _settings(
        tmp_path,
        yandex_imap_folder="Yandex/INBOX",
        yandex_display_name="Ящик",
        yandex_mailbox_name="Legacy",
    )
    assert explicit.yandex_folder == "Yandex/INBOX"
    assert explicit.yandex_name == "Ящик"

    defaults = _settings(tmp_path, yandex_display_name="", yandex_mailbox_name="")
    assert defaults.yandex_name == "Яндекс"


def test_missing_openrouter_key_raises_validation_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ValidationError, match="OPENROUTER_API_KEY"):
        _settings(tmp_path, openrouter_api_key="", llm_provider="openrouter")
