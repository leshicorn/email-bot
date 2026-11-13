"""Tests for the pure helpers in :mod:`fetcher.imap` (no sockets)."""
from __future__ import annotations

import base64
import email
import email.policy
from datetime import UTC, datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from fetcher.imap import (
    _decode_header_value,
    _extract_attachments,
    _extract_calendar,
    _html_to_text,
    detect_bulk,
    extract_body,
    parse_ics,
    strip_quoted_reply,
)

LONG_ANSWER = "Подробный ответ по всем вопросам. " * 10

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20241118T103000Z
DTEND:20241118T113000Z
SUMMARY:Защита проекта
LOCATION:каб. 204
ORGANIZER;CN=Alice:mailto:alice@example.com
END:VEVENT
END:VCALENDAR
"""


def _build_multipart() -> email.message.Message:
    message = MIMEMultipart("mixed")
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText("Текстовая версия письма.", "plain", "utf-8"))
    alternative.attach(MIMEText("<p>HTML версия</p>", "html", "utf-8"))
    message.attach(alternative)

    attachment = MIMEApplication(b"%PDF-1.4 fake", _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename="счёт.pdf")
    message.attach(attachment)

    message.attach(MIMEText(ICS, "calendar", "utf-8"))
    return email.message_from_bytes(message.as_bytes(), policy=email.policy.compat32)


@pytest.mark.parametrize(
    "suffix",
    [
        "\n\nOn Mon, Nov 11, 2024 at 10:00 AM Alice <a@b.com> wrote:\n> quoted",
        "\n> строка цитаты\n> ещё одна строка",
        "\n-- \nВася\nДиректор",
        "\n________________________________\nFrom: Alice\nSent: Monday",
    ],
)
def test_strip_quoted_reply_cuts_history(suffix: str) -> None:
    cleaned = strip_quoted_reply(LONG_ANSWER + suffix)
    assert cleaned == LONG_ANSWER.strip()


def test_strip_quoted_reply_removes_mobile_trailer() -> None:
    cleaned = strip_quoted_reply(LONG_ANSWER + "\n\nSent from my iPhone")
    assert cleaned == LONG_ANSWER.strip()
    assert "iPhone" not in cleaned


def test_strip_quoted_reply_keeps_a_short_answer_intact() -> None:
    assert strip_quoted_reply("Ок, сделаю.") == "Ок, сделаю."
    assert strip_quoted_reply("") == ""


def test_parse_ics_extracts_event_fields() -> None:
    events = parse_ics(ICS)

    assert len(events) == 1
    event = events[0]
    assert event.summary == "Защита проекта"
    assert event.location == "каб. 204"
    assert event.organizer == "alice@example.com"
    assert event.start == datetime(2024, 11, 18, 10, 30, tzinfo=UTC)
    assert event.end == datetime(2024, 11, 18, 11, 30, tzinfo=UTC)
    assert event.human() == "18 ноя 10:30–11:30 · каб. 204 · Защита проекта"


def test_parse_ics_unfolds_continuation_lines() -> None:
    ics = (
        "BEGIN:VEVENT\n"
        "DTSTART:20241118T103000Z\n"
        "SUMMARY:Встреча с командой\n"
        "  по проекту\n"
        "END:VEVENT"
    )
    events = parse_ics(ics)
    assert events[0].summary == "Встреча с командой по проекту"


def test_extract_body_prefers_plain_part() -> None:
    message = _build_multipart()
    body = extract_body(message, max_chars=3000)
    assert body == "Текстовая версия письма."
    assert "HTML версия" not in body


def test_extract_attachments_and_calendar_from_multipart() -> None:
    message = _build_multipart()

    assert _extract_attachments(message) == ["счёт.pdf"]

    events = _extract_calendar(message)
    assert len(events) == 1
    assert events[0].summary == "Защита проекта"


def test_extract_body_converts_html_when_no_plain_part() -> None:
    html = (
        "<html><body><p>Привет</p><script>alert(1)</script>"
        "<p>мир</p></body></html>"
    )
    message = email.message_from_bytes(
        MIMEText(html, "html", "utf-8").as_bytes(), policy=email.policy.compat32
    )

    body = extract_body(message, max_chars=1000)

    assert "Привет" in body
    assert "мир" in body
    assert "alert" not in body
    assert "<" not in body


@pytest.mark.parametrize(
    ("header", "reason"),
    [
        ("List-Unsubscribe: <mailto:unsubscribe@example.com>", "list-unsubscribe"),
        ("Precedence: bulk", "precedence:bulk"),
        ("X-Spam-Flag: YES", "spam"),
        ("List-Id: <list.example.com>", "list-id"),
    ],
)
def test_detect_bulk_uses_headers(header: str, reason: str) -> None:
    message = email.message_from_string(f"Subject: Hello\n{header}\n\nbody")
    assert detect_bulk(message, "someone@example.com", []) == (True, reason)


def test_detect_bulk_falls_back_to_sender_patterns() -> None:
    message = email.message_from_string("Subject: Promo\n\nbody")
    assert detect_bulk(message, "newsletter@example.com", ["noreply", "newsletter"]) == (
        True,
        "sender:newsletter",
    )
    assert detect_bulk(message, "friend@example.com", ["newsletter"]) == (False, "")


def test_detect_bulk_leaves_normal_mail_alone() -> None:
    message = email.message_from_string(
        "From: friend@example.com\nSubject: Hello\n\nbody"
    )
    assert detect_bulk(message, "friend@example.com", ["newsletter"]) == (False, "")


def test_decode_header_value_handles_rfc2047_cyrillic() -> None:
    encoded = base64.b64encode("Привет, мир".encode()).decode()
    raw = f"=?UTF-8?B?{encoded}?="

    assert _decode_header_value(raw) == "Привет, мир"
    assert _decode_header_value(raw.encode()) == "Привет, мир"
    assert _decode_header_value(None) == ""
    assert _decode_header_value("Plain subject") == "Plain subject"


def test_html_to_text_strips_markup_and_scripts() -> None:
    html = (
        "<html><head><style>.x{color:red}</style></head><body>"
        "<p>Привет</p><script>bad()</script><p>мир</p></body></html>"
    )
    text = _html_to_text(html)

    assert "Привет" in text
    assert "мир" in text
    assert "bad" not in text
    assert "<" not in text
