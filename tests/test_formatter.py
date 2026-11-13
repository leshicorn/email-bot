"""Tests for :mod:`bot.formatter` — rendering and Telegram chunking."""
from __future__ import annotations

from datetime import datetime

import pytest

from bot.formatter import (
    TELEGRAM_LIMIT,
    format_digest,
    format_error,
    format_help,
    format_status,
    format_weekly,
    plural,
    split_message,
)
from fetcher.models import CalendarEvent
from summarizer.llm import DigestResult, MailboxDigest


def test_plural_letter_forms() -> None:
    cases = {
        0: "писем",
        1: "письмо",
        2: "письма",
        4: "письма",
        5: "писем",
        11: "писем",
        12: "писем",
        21: "письмо",
        22: "письма",
        25: "писем",
    }
    for number, expected in cases.items():
        assert plural(number, "письмо", "письма", "писем") == expected


def test_plural_token_forms() -> None:
    cases = {1: "токен", 2: "токена", 5: "токенов", 11: "токенов", 21: "токен", 22: "токена"}
    for number, expected in cases.items():
        assert plural(number, "токен", "токена", "токенов") == expected


def test_format_digest_empty() -> None:
    text = format_digest(DigestResult(total_emails=0), datetime(2024, 11, 13, 21, 0))
    assert "Email-дайджест" in text
    assert "13 ноября 2024" in text
    assert "Новых писем нет" in text


def test_format_digest_normal_sections_and_footer() -> None:
    result = DigestResult(
        mailboxes=[
            MailboxDigest(
                name="Gmail",
                important=[{"sender": "Алиса", "subject": "Счёт", "summary": "Оплатить до пятницы"}],
                work=[{"sender": "Боб", "subject": "Спринт", "summary": "План готов"}],
                other=[{"sender": "Магазин", "subject": "Заказ", "summary": "Отправлен"}],
            )
        ],
        action_items=["Ответить Алисе"],
        total_emails=3,
        model="test/model",
        prompt_tokens=120,
        completion_tokens=80,
        total_tokens=200,
    )
    text = format_digest(result, datetime(2024, 11, 13, 21, 0))

    assert "📬 <b>Gmail</b>" in text
    assert "📌 <b>Важное (1)</b>" in text
    assert "💼 <b>Рабочее (1)</b>" in text
    assert "📂 <b>Прочее (1)</b>" in text
    assert "⚡ <b>Требуют внимания:</b>" in text
    assert "  • Ответить Алисе" in text
    assert "📊 Обработано 3 письма" in text
    assert "test/model" in text
    assert "🪙 200 токенов (вход 120 / выход 80)" in text
    assert "бесплатно 🎉" in text


def test_format_digest_escapes_html_in_user_content() -> None:
    result = DigestResult(
        mailboxes=[
            MailboxDigest(
                name="Gmail <&>",
                important=[
                    {
                        "sender": "Evil <e@x>",
                        "subject": "<b>hack</b> & <script>",
                        "summary": "sum & <ok>",
                    }
                ],
            )
        ],
        action_items=["<do> &"],
        total_emails=1,
    )
    text = format_digest(result, datetime(2024, 11, 13, 21, 0))

    assert "Gmail &lt;&amp;&gt;" in text
    assert "&lt;b&gt;hack&lt;/b&gt; &amp; &lt;script&gt;" in text
    assert "Evil &lt;e@x&gt;" in text
    assert "sum &amp; &lt;ok&gt;" in text
    assert "&lt;do&gt; &amp;" in text
    assert "<do> &" not in text


def test_format_digest_important_only_filters_other_sections() -> None:
    result = DigestResult(
        mailboxes=[
            MailboxDigest(
                name="Gmail",
                important=[{"sender": "Алиса", "subject": "Срочно", "summary": "Ответить"}],
                work=[{"subject": "Работа"}],
                newsletter_count=7,
            ),
            MailboxDigest(name="mail.ru", work=[{"subject": "Только работа"}]),
        ],
        action_items=["Сделать"],
        total_emails=5,
    )
    text = format_digest(result, datetime(2024, 11, 13, 21, 0), important_only=True)

    assert "Важное из почты" in text
    assert "📬 <b>Gmail</b>" in text
    assert "mail.ru" not in text
    assert "📌 <b>Важное (1)</b>" in text
    assert "💼" not in text
    assert "📂" not in text
    assert "🗑" not in text
    assert "📊 Из 5 писем · важных: 1" in text


def test_format_digest_important_only_with_nothing_important() -> None:
    result = DigestResult(
        mailboxes=[MailboxDigest(name="Gmail", work=[{"subject": "Работа"}])],
        total_emails=1,
    )
    text = format_digest(result, datetime(2024, 11, 13, 21, 0), important_only=True)
    assert "Важного за период нет" in text


def test_format_digest_renders_calendar_events() -> None:
    result = DigestResult(
        mailboxes=[MailboxDigest(name="Gmail", other=[{"subject": "Письмо"}])],
        calendar_events=[
            CalendarEvent(
                summary="Защита проекта",
                start=datetime(2024, 11, 18, 10, 30),
                end=datetime(2024, 11, 18, 11, 0),
                location="каб. 204",
            )
        ],
        total_emails=1,
    )
    text = format_digest(result, datetime(2024, 11, 13, 21, 0))

    assert "📅 <b>События:</b>" in text
    assert "18 ноя 10:30–11:00 · каб. 204 · Защита проекта" in text


def test_format_digest_mailbox_level_bulk_counters() -> None:
    result = DigestResult(
        mailboxes=[
            MailboxDigest(
                name="Gmail",
                other=[{"subject": "Письмо"}],
                newsletter_count=3,
                spam_count=2,
            )
        ],
        total_emails=1,
    )
    text = format_digest(result, datetime(2024, 11, 13, 21, 0))

    assert text.count("🗑 Пропущено") == 1
    assert "рассылок: <b>3</b>, спама: <b>2</b>" in text


def test_format_digest_global_bulk_counter_fallback() -> None:
    result = DigestResult(
        mailboxes=[MailboxDigest(name="Gmail", other=[{"subject": "Письмо"}])],
        newsletter_total=5,
        spam_total=2,
        total_emails=1,
    )
    text = format_digest(result, datetime(2024, 11, 13, 21, 0))

    assert text.count("🗑 Пропущено") == 1
    assert "рассылок: <b>5</b>" in text
    assert "спама: <b>2</b>" in text


def test_format_digest_global_counters_win_over_mailbox_counts() -> None:
    result = DigestResult(
        mailboxes=[
            MailboxDigest(name="Gmail", other=[{"subject": "Письмо"}], newsletter_count=1)
        ],
        newsletter_total=9,
        total_emails=1,
    )
    text = format_digest(result, datetime(2024, 11, 13, 21, 0))

    assert text.count("🗑 Пропущено") == 1
    assert "рассылок: <b>9</b>" in text
    assert "рассылок: <b>1</b>" not in text


def test_format_digest_reports_cost_and_fallback_footer() -> None:
    result = DigestResult(
        mailboxes=[MailboxDigest(name="Gmail", other=[{"subject": "Письмо"}])],
        total_emails=2,
        cost_usd=0.0123,
        fallback_used=True,
    )
    text = format_digest(result, datetime(2024, 11, 13, 21, 0))

    assert "$0.0123" in text
    assert "⚠️ часть писем без AI" in text
    assert "бесплатно 🎉" not in text


def test_split_message_returns_single_chunk_for_short_text() -> None:
    assert split_message("Привет\nмир") == ["Привет\nмир"]


def test_split_message_splits_10k_digest_losslessly() -> None:
    lines = [f"Строка {index:03d}: " + "данные " * 20 for index in range(70)]
    text = "\n".join(lines)
    assert len(text) > 9000

    chunks = split_message(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_LIMIT for chunk in chunks)
    assert not chunks[0].startswith("<i>(продолжение")
    assert all(chunk.startswith("<i>(продолжение") for chunk in chunks[1:])

    restored: list[str] = []
    for index, chunk in enumerate(chunks):
        body = chunk if index == 0 else chunk.split("\n", 1)[1]
        restored.extend(body.split("\n"))
    assert restored == lines


def test_split_message_hard_splits_a_pathological_line() -> None:
    text = "x" * 10_000
    chunks = split_message(text)

    assert len(chunks) > 1
    assert all(len(chunk) <= TELEGRAM_LIMIT for chunk in chunks)
    restored = "".join(
        chunk if index == 0 else chunk.split("\n", 1)[1] for index, chunk in enumerate(chunks)
    )
    assert restored == text


def test_format_error_escapes_exception_text() -> None:
    text = format_error(ValueError("<boom> & stuff"))
    assert "Ошибка дайджеста" in text
    assert "&lt;boom&gt; &amp; stuff" in text
    assert "<boom>" not in text


def test_format_status_escapes_input_and_handles_empty_stats() -> None:
    text = format_status(
        stats={"last_run": {"run_at": "2024-11-13T21:00:00", "email_count": 2}},
        mailboxes=[
            {"mailbox": "G <x>", "last_ok_at": datetime(2024, 11, 13, 10, 0)},
            {"mailbox": "Bad <&>", "last_error": "oops <&>"},
        ],
        next_run="<soon>",
        digest_time="<21:00>",
        paused_until="<later>",
    )

    assert "Следующий запуск" in text
    assert "&lt;soon&gt;" in text
    assert "&lt;21:00&gt;" in text
    assert "&lt;later&gt;" in text
    assert "G &lt;x&gt;" in text
    assert "Bad &lt;&amp;&gt;" in text
    assert "oops &lt;&amp;&gt;" in text

    empty = format_status(stats={}, mailboxes=[], next_run="—", digest_time="21:00")
    assert "ещё не было" in empty


def test_format_weekly_values_and_empty_defaults() -> None:
    text = format_weekly(
        {
            "emails_7d": 12,
            "runs_7d": 3,
            "tokens_7d": 900,
            "cost_7d": 0.02,
            "processed_total": 40,
        }
    )
    assert "Обработано писем: <b>12</b>" in text
    assert "Запусков: <b>3</b>" in text
    assert "Токенов: <b>900</b>" in text
    assert "$0.0200" in text
    assert "Всего в базе: <b>40</b>" in text
    assert "последние 7 дней" in text

    empty = format_weekly({})
    assert "бесплатно" in empty


def test_format_help_lists_commands() -> None:
    text = format_help()
    for command in ("/digest", "/last", "/important", "/status", "/pause", "/help"):
        assert command in text


@pytest.mark.parametrize("length", [1, 11, 21])
def test_plural_is_positive_for_negative_numbers(length: int) -> None:
    assert plural(-length, "письмо", "письма", "писем") == plural(
        length, "письмо", "письма", "писем"
    )
