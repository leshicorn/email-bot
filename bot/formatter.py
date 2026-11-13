"""
Rendering: :class:`DigestResult` → Telegram-safe HTML.

Kept free of I/O so every branch is unit-testable. Two things matter here that
used to be wrong:

* Telegram rejects messages longer than 4096 characters, and a digest for 30
  emails is already ~5700 — :func:`split_message` breaks it on line boundaries.
* Russian needs three plural forms (1 письмо / 2 письма / 5 писем).
"""
from __future__ import annotations

from datetime import datetime

from summarizer.llm import DigestResult

TELEGRAM_LIMIT = 4096
#: Leave room for the "(продолжение)" prefix on follow-up chunks.
CHUNK_LIMIT = TELEGRAM_LIMIT - 80

_RU_MONTHS = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def plural(number: int, one: str, few: str, many: str) -> str:
    """Russian plural form for *number* (``1 письмо``, ``2 письма``, ``5 писем``)."""
    n = abs(int(number))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _esc(text: object) -> str:
    """Escape HTML specials for Telegram's HTML parse mode."""
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _ru_date(dt: datetime) -> str:
    return f"{dt.day} {_RU_MONTHS[dt.month]} {dt.year}"


def _plural_letters(n: int) -> str:
    return plural(n, "письмо", "письма", "писем")


def _render_items(items: list[dict], lines: list[str], *, skip_ai_marker: bool = True) -> None:
    for item in items:
        sender = _esc(item.get("sender", ""))
        subject = _esc(item.get("subject", "") or "(без темы)")
        summary = _esc(item.get("summary", ""))
        lines.append(f"  • <b>{subject}</b>")
        if sender:
            lines.append(f"    <i>от {sender}</i>")
        if summary and not (skip_ai_marker and summary.startswith("⚠️ AI недоступен")):
            lines.append(f"    {summary}")


def format_digest(
    result: DigestResult,
    generated_at: datetime | None = None,
    *,
    important_only: bool = False,
) -> str:
    """Render the digest as Telegram HTML."""
    now = generated_at or datetime.now()
    lines: list[str] = []

    title = "Email-дайджест" if not important_only else "Важное из почты"
    lines.append(f"📧 <b>{title} · {_esc(_ru_date(now))}</b>")
    lines.append("")

    counts = result.counts()

    if important_only:
        has_any = (
            any(mb.important for mb in result.mailboxes)
            or result.action_items
            or result.calendar_events
        )
        if not has_any:
            lines.append("📌 Важного за период нет — можно выдохнуть.")
            return "\n".join(lines)
    elif result.total_emails == 0:
        lines.append("✅ Новых писем нет. Хорошего вечера!")
        return "\n".join(lines)

    for mailbox in result.mailboxes:
        if important_only:
            if not mailbox.important:
                continue
        elif mailbox.is_empty():
            continue

        lines.append(f"📬 <b>{_esc(mailbox.name)}</b>")

        if mailbox.important:
            lines.append(f"📌 <b>Важное ({len(mailbox.important)})</b>")
            _render_items(mailbox.important, lines, skip_ai_marker=False)
            lines.append("")

        if not important_only:
            if mailbox.work:
                lines.append(f"💼 <b>Рабочее ({len(mailbox.work)})</b>")
                _render_items(mailbox.work, lines)
                lines.append("")

            if mailbox.other:
                lines.append(f"📂 <b>Прочее ({len(mailbox.other)})</b>")
                _render_items(mailbox.other, lines)
                lines.append("")

    if not important_only:
        # Local counters are authoritative: bulk mail is filtered before the
        # model ever sees it, so any newsletter_count from the model is a guess.
        newsletter = result.newsletter_total or sum(mb.newsletter_count for mb in result.mailboxes)
        spam = result.spam_total or sum(mb.spam_count for mb in result.mailboxes)
        counters = []
        if newsletter:
            counters.append(f"рассылок: <b>{newsletter}</b>")
        if spam:
            counters.append(f"спама: <b>{spam}</b>")
        if counters:
            lines.append("🗑 Пропущено — " + ", ".join(counters))
            lines.append("")

    if result.calendar_events:
        lines.append("📅 <b>События:</b>")
        for event in result.calendar_events[:7]:
            lines.append(f"  • {_esc(event.human())}")
        lines.append("")

    if result.action_items:
        lines.append("⚡ <b>Требуют внимания:</b>")
        for item in result.action_items[:12]:
            lines.append(f"  • {_esc(item)}")
        lines.append("")

    # ── footer ───────────────────────────────────────────────────────────────
    if important_only:
        footer = (
            f"📊 Из {result.total_emails} {_plural_letters(result.total_emails)} · "
            f"важных: {counts['important']}"
        )
    else:
        parts = [f"📊 Обработано {result.total_emails} {_plural_letters(result.total_emails)}"]
        if result.model:
            parts.append(f"· {_esc(result.model)}")
        if result.total_tokens:
            token_word = plural(result.total_tokens, "токен", "токена", "токенов")
            parts.append(
                f"· 🪙 {result.total_tokens} {token_word}"
                f" (вход {result.prompt_tokens} / выход {result.completion_tokens})"
            )
        if result.cost_usd > 0:
            parts.append(f"· ${result.cost_usd:.4f}")
        elif result.total_tokens:
            parts.append("· бесплатно 🎉")
        if result.quota_exhausted:
            parts.append("· ⚠️ лимит бесплатных моделей")
        elif result.fallback_used:
            parts.append("· ⚠️ часть писем без AI")
        footer = " ".join(parts)

    lines.append(f"<i>{footer}</i>")
    return "\n".join(lines)


def split_message(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split *text* into Telegram-sized chunks without breaking HTML tags.

    Our HTML never spans lines, so cutting on line boundaries is safe; a single
    over-long line is hard-split as a last resort.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in text.split("\n"):
        while len(line) > limit:
            # Pathological line (a wall of text with no breaks).
            available = limit - current_len - 1
            if available > 200:
                current.append(line[:available])
                chunks.append("\n".join(current))
                current, current_len = [], 0
                line = line[available:]
            else:
                if current:
                    chunks.append("\n".join(current))
                current, current_len = [], 0
            line = line.lstrip("\n")
        projected = current_len + len(line) + (1 if current else 0)
        if projected > limit and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line) + (1 if len(current) > 1 else 0)

    if current:
        chunks.append("\n".join(current))

    chunks = [chunk.strip("\n") for chunk in chunks if chunk.strip()]
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [
            chunk if index == 0 else f"<i>(продолжение {index + 1}/{total})</i>\n{chunk}"
            for index, chunk in enumerate(chunks)
        ]
    return chunks or [text[:limit]]


def format_error(exc: BaseException) -> str:
    return f"❌ <b>Ошибка дайджеста</b>\n<code>{_esc(str(exc)[:900])}</code>"


def format_status(
    *,
    stats: dict,
    mailboxes: list[dict],
    next_run: str,
    digest_time: str,
    paused_until: str | None = None,
) -> str:
    lines = ["🤖 <b>Email Digest Bot</b>", ""]
    lines.append(f"⏰ Время отправки: <b>{_esc(digest_time)}</b>")
    lines.append(f"🕐 Следующий запуск: <b>{_esc(next_run)}</b>")

    if paused_until:
        lines.append(f"⏸ Пауза до: <b>{_esc(paused_until)}</b>")

    last_run = stats.get("last_run") or {}
    if last_run:
        run_at = str(last_run.get("run_at") or "")
        pretty = run_at.replace("T", " ")[:16]
        count = int(last_run.get("email_count") or 0)
        lines.append(
            f"📬 Последний запуск: <b>{_esc(pretty)}</b> · {count} {_plural_letters(count)}"
        )
    else:
        lines.append("📬 Последний запуск: <b>ещё не было</b>")

    lines.append(f"🗂 В базе: <b>{stats.get('processed_total', 0)}</b> писем")
    lines.append(
        f"📈 За 7 дней: <b>{stats.get('runs_7d', 0)}</b> запусков · "
        f"{stats.get('emails_7d', 0)} писем · {stats.get('tokens_7d', 0)} токенов"
    )
    if stats.get("pending_outbox"):
        lines.append(f"📤 В очереди на отправку: <b>{stats['pending_outbox']}</b>")

    if mailboxes:
        lines.append("")
        lines.append("📮 <b>Ящики:</b>")
        for box in mailboxes:
            stamp = box.get("last_ok_str") or ""
            if stamp:
                lines.append(f"  • {_esc(box['mailbox'])} — <b>ok</b> ({_esc(stamp)})")
            else:
                error = (box.get("last_error") or "нет данных")[:60]
                lines.append(f"  • {_esc(box['mailbox'])} — ⚠️ {_esc(error)}")
    return "\n".join(lines)


def format_weekly(stats: dict, period_days: int = 7) -> str:
    lines = ["📈 <b>Неделя в почте</b>", ""]
    lines.append(f"📬 Обработано писем: <b>{stats.get('emails_7d', 0)}</b>")
    lines.append(f"🔁 Запусков: <b>{stats.get('runs_7d', 0)}</b>")
    lines.append(f"🪙 Токенов: <b>{stats.get('tokens_7d', 0)}</b>")
    cost = float(stats.get("cost_7d") or 0)
    lines.append(
        f"💸 Расход: <b>{'бесплатно' if cost == 0 else f'${cost:.4f}'}</b>"
    )
    if stats.get("processed_total"):
        lines.append(f"🗂 Всего в базе: <b>{stats['processed_total']}</b> писем")
    lines.append("")
    lines.append(f"<i>Период: последние {period_days} {plural(period_days, 'день', 'дня', 'дней')}</i>")
    return "\n".join(lines)


def format_help() -> str:
    return (
        "📖 <b>Команды</b>\n\n"
        "/digest — собрать дайджест прямо сейчас\n"
        "/last — повторить последний дайджест\n"
        "/important — только важное\n"
        "/status — состояние, расписание, ящики\n"
        "/stats — расход токенов за неделю\n"
        "/search СЛОВО — поиск по истории дайджестов\n"
        "/pause 3d — пауза (3d, 12h, 30m)\n"
        "/resume — снять паузу\n"
        "/time 08:30 — изменить время дайджеста\n"
        "/retry — отправить застрявшие сообщения\n"
        "/help — эта справка"
    )
