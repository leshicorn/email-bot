"""
Telegram bot: commands, inline actions and delivery.

Delivery always goes through the outbox, so neither a Telegram outage nor a
message longer than 4096 characters can silently swallow a digest that was
already generated (and already paid for in tokens).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.formatter import (
    format_digest,
    format_error,
    format_help,
    format_weekly,
    split_message,
)
from config import Settings
from storage.db import DigestDB, iso, utcnow
from summarizer.llm import DigestResult

logger = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"^(\d+)\s*([mhdw])$", re.IGNORECASE)
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> timedelta | None:
    """``3d`` → 3 days; also accepts ``12h``, ``30m``, ``1w``."""
    match = _DURATION_RE.match((text or "").strip())
    if not match:
        return None
    amount, unit = match.groups()
    return timedelta(seconds=int(amount) * _UNIT_SECONDS[unit.lower()])


def _esc(text: object) -> str:
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def digest_keyboard(digest_id: int | None) -> InlineKeyboardMarkup:
    rows = []
    if digest_id:
        rows.append(
            [
                InlineKeyboardButton("📌 Только важное", callback_data=f"imp:{digest_id}"),
                InlineKeyboardButton("🔁 Ещё раз", callback_data="again"),
            ]
        )
    else:
        rows.append([InlineKeyboardButton("🔁 Ещё раз", callback_data="again")])
    rows.append([InlineKeyboardButton("📊 Статус", callback_data="status")])
    return InlineKeyboardMarkup(rows)


class DigestBot:
    """Command surface + outbox delivery."""

    def __init__(
        self,
        settings: Settings,
        db: DigestDB,
        run_digest_callback: Callable[..., object],
    ) -> None:
        self.settings = settings
        self.db = db
        self._run_digest = run_digest_callback
        self._app: Application | None = None
        self._scheduler = None
        # Shared with the scheduler so cron and /digest can never overlap.
        self.digest_lock = asyncio.Lock()

    # ── wiring ───────────────────────────────────────────────────────────────
    def set_scheduler(self, scheduler) -> None:
        self._scheduler = scheduler

    def build_app(self, post_init=None, post_shutdown=None) -> Application:
        builder = (
            Application.builder()
            .token(self.settings.telegram_bot_token)
            .read_timeout(self.settings.telegram_timeout)
            .write_timeout(self.settings.telegram_timeout)
            .connect_timeout(self.settings.telegram_timeout)
        )
        if post_init:
            builder = builder.post_init(post_init)
        if post_shutdown:
            builder = builder.post_shutdown(post_shutdown)

        app = builder.build()
        for name, handler in (
            ("start", self._cmd_start),
            ("help", self._cmd_help),
            ("status", self._cmd_status),
            ("digest", self._cmd_digest),
            ("last", self._cmd_last),
            ("important", self._cmd_important),
            ("stats", self._cmd_stats),
            ("search", self._cmd_search),
            ("pause", self._cmd_pause),
            ("resume", self._cmd_resume),
            ("time", self._cmd_time),
            ("retry", self._cmd_retry),
        ):
            app.add_handler(CommandHandler(name, handler))
        app.add_handler(MessageHandler(filters.Regex(r"(?i)^(Дайджест|Статус)$"), self._cmd_button))
        app.add_handler(CallbackQueryHandler(self._on_callback))
        self._app = app
        return app

    def _reply_keyboard(self) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            [["Дайджест", "Статус"]],
            resize_keyboard=True,
            input_field_placeholder="Выбери действие",
        )

    # ── guards / low-level send ──────────────────────────────────────────────
    def _is_owner(self, update: Update) -> bool:
        chat = update.effective_chat
        return bool(chat and chat.id == self.settings.telegram_chat_id)

    async def _reject(self, update: Update) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                "👋 Привет! Я — бот для персонального дайджеста почты.\n\n"
                "Работаю только для своего владельца. Хочешь такого же — разверни свою копию!"
            )

    async def _reply(self, update: Update, text: str, **kwargs) -> None:
        message = update.effective_message
        if message is not None:
            await message.reply_html(text, **kwargs)
        else:
            await self.send_text(text)

    async def send_text(
        self,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        *,
        silent: bool = False,
    ) -> None:
        """Send *text*, splitting it into Telegram-sized chunks."""
        if self._app is None:
            raise RuntimeError("Bot is not built yet — call build_app() first")
        chunks = split_message(text)
        for index, chunk in enumerate(chunks):
            markup = reply_markup if index == len(chunks) - 1 else None
            await self._app.bot.send_message(
                chat_id=self.settings.telegram_chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
                disable_notification=silent,
            )

    async def deliver_pending(
        self,
        digest_id: int | None = None,
        *,
        with_keyboard: bool = True,
        silent: bool = False,
    ) -> int:
        """Deliver queued messages in order, returning how many were sent.

        The keyboard is attached to the final chunk only, which keeps a
        multi-part digest from sprouting buttons on every message.
        """
        pending = self.db.pending_outbox(limit=50)
        if not pending:
            return 0
        keyboard = digest_keyboard(digest_id) if with_keyboard else None
        sent = 0
        for index, row in enumerate(pending):
            is_last = index == len(pending) - 1
            try:
                await self.send_text(
                    str(row["text"]), keyboard if is_last else None, silent=silent
                )
            except Exception as exc:  # noqa: BLE001 - retried later by the scheduler
                logger.error("Outbox %s delivery failed: %s", row["id"], exc)
                self.db.mark_outbox_failed(int(row["id"]), str(exc))
                break
            self.db.mark_outbox_sent(int(row["id"]))
            sent += 1
        return sent

    # ── digest execution ─────────────────────────────────────────────────────
    async def _run_pipeline_and_send(self, update: Update, *, kind: str = "manual") -> None:
        if self.digest_lock.locked():
            await self._reply(update, "⏳ Дайджест уже собирается, подожди…")
            return
        async with self.digest_lock:
            await self._reply(update, "⏳ Собираю дайджест…")
            try:
                outcome = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self._run_digest(kind=kind, force=True)
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("/digest failed: %s", exc, exc_info=True)
                await self._reply(update, format_error(exc))
                return

            if outcome.skipped:  # type: ignore[attr-defined]
                reason = outcome.skip_reason or "нечего присылать"  # type: ignore[attr-defined]
                await self._reply(update, f"😴 Дайджест пропущен: {reason}")
                return
            if outcome.errors:  # type: ignore[attr-defined]
                await self._reply(
                    update, "⚠️ Часть ящиков недоступна:\n" + "\n".join(outcome.errors[:3])  # type: ignore[attr-defined]
                )
            await self.deliver_pending(outcome.digest_id)  # type: ignore[attr-defined]

    def _schedule_label(self) -> str:
        from scheduler.jobs import digest_time

        hour, minute = digest_time(self.settings, self.db)
        return f"{hour:02d}:{minute:02d}"

    # ── commands ─────────────────────────────────────────────────────────────
    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        await self._reply(
            update,
            "👋 <b>Email Digest Bot на связи</b>\n\n"
            f"Присылаю сводку по почте каждый день в <b>{self._schedule_label()}</b> "
            f"({self.settings.timezone}).\n\n"
            "/digest — собрать сейчас\n"
            "/status — состояние и расписание\n"
            "/help — все команды",
            reply_markup=self._reply_keyboard(),
        )

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        await self._reply(update, format_help())

    async def _cmd_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        text = (update.effective_message.text or "").strip().lower() if update.effective_message else ""
        if text == "дайджест":
            return await self._cmd_digest(update, ctx)
        return await self._cmd_status(update, ctx)

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        from scheduler.jobs import build_status_text

        await self._reply(update, build_status_text(self.settings, self.db, self._scheduler))

    async def _cmd_digest(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        await self._run_pipeline_and_send(update, kind="manual")

    async def _cmd_last(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        row = self.db.last_history()
        if not row:
            await self._reply(update, "История пуста — дайджестов ещё не было.")
            return
        await self.send_text(str(row["text"]))

    async def _cmd_important(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        await self._send_important(None)

    async def _send_important(self, digest_id: int | None) -> None:
        row = self.db.get_history(digest_id) if digest_id else self.db.last_history()
        if not row or not row.get("payload_json"):
            await self.send_text("Пока нет данных для фильтра по важному.")
            return
        result = DigestResult.from_dict(json.loads(str(row["payload_json"])))
        text = format_digest(result, datetime.now(tz=self.settings.tz), important_only=True)
        await self.send_text(text)

    async def _cmd_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        await self._reply(update, format_weekly(self.db.stats()))

    async def _cmd_search(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        query = " ".join(ctx.args or []).strip()
        if not query:
            await self._reply(update, "Напиши так: /search доставка")
            return
        rows = self.db.search_history(query, limit=5)
        if not rows:
            await self._reply(update, f"По запросу «{_esc(query)}» ничего не нашлось.")
            return
        lines = [f"🔍 <b>Нашлось: {len(rows)}</b>", ""]
        for row in rows:
            stamp = str(row["created_at"]).replace("T", " ")[:16]
            plain = re.sub(r"<[^>]+>", "", str(row["text"]))
            position = max(0, plain.lower().find(query.lower()))
            snippet = plain[max(0, position - 60) : position + 140].replace("\n", " ")
            lines.append(f"<b>{stamp}</b>")
            lines.append(f"  …{_esc(snippet)}…")
            lines.append("")
        lines.append("<i>Полный текст: /last</i>")
        await self._reply(update, "\n".join(lines))

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        delta = parse_duration(" ".join(ctx.args or []) or "1d")
        if delta is None:
            await self._reply(update, "Формат: /pause 3d (или 12h, 30m, 1w)")
            return
        until = utcnow() + delta
        self.db.set_setting("paused_until", iso(until) or "")
        pretty = until.astimezone(self.settings.tz).strftime("%d.%m.%Y %H:%M")
        await self._reply(update, f"⏸ Пауза до {pretty} ({self.settings.timezone}).")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        self.db.delete_setting("paused_until")
        await self._reply(update, "▶️ Пауза снята.")

    async def _cmd_time(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        match = _TIME_RE.match(" ".join(ctx.args or []).strip())
        if not match:
            await self._reply(update, "Формат: /time 08:30")
            return
        hour, minute = int(match.group(1)), int(match.group(2))
        self.db.set_setting("digest_time", f"{hour:02d}:{minute:02d}")
        message = f"⏰ Время дайджеста: {hour:02d}:{minute:02d} ({self.settings.timezone})."
        if self._scheduler is not None:
            from scheduler.jobs import reschedule_digest

            try:
                reschedule_digest(self._scheduler, self.settings, self.db)
                message += " Расписание обновлено."
            except Exception as exc:  # noqa: BLE001
                logger.error("Reschedule failed: %s", exc)
                message += " Новое время применится после перезапуска."
        await self._reply(update, message)

    async def _cmd_retry(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._reject(update)
        pending = self.db.pending_outbox_count()
        if not pending:
            await self._reply(update, "Очередь пуста — всё доставлено.")
            return
        sent = await self.deliver_pending(None, with_keyboard=False)
        left = self.db.pending_outbox_count()
        tail = f" Осталось: {left}." if left else ""
        await self._reply(update, f"📤 Отправлено {sent} из {pending}.{tail}")

    # ── callbacks ────────────────────────────────────────────────────────────
    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self._is_owner(update):
            await query.answer("Только для владельца", show_alert=False)
            return
        await query.answer()
        data = query.data or ""

        if data.startswith("imp:"):
            raw = data.split(":", 1)[1]
            await self._send_important(int(raw) if raw.isdigit() else None)
            return
        if data == "again":
            await self._run_pipeline_and_send(update, kind="manual")
            return
        if data == "status":
            from scheduler.jobs import build_status_text

            await self.send_text(build_status_text(self.settings, self.db, self._scheduler))

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def post_startup(self, application: Application) -> None:
        await application.bot.send_message(
            chat_id=self.settings.telegram_chat_id,
            text="🚀 <b>Email Digest Bot запущен</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=self._reply_keyboard(),
        )
        with contextlib.suppress(Exception):
            await application.bot.set_my_commands(
                [
                    ("digest", "Собрать дайджест сейчас"),
                    ("last", "Повторить последний дайджест"),
                    ("important", "Только важное"),
                    ("status", "Статус и расписание"),
                    ("stats", "Расход за неделю"),
                    ("help", "Справка"),
                ]
            )
