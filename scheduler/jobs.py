"""
The digest pipeline and the scheduler that drives it.

The pipeline is deliberately boring and explicit:

    fetch per mailbox (own watermark) → mark bulk → register as pending
    → collapse threads → summarise → done/retry → record run
    → render → history → outbox

Failure of one mailbox no longer moves the window for the others, and a failed
fetch never advances that mailbox's watermark at all.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot.formatter import (
    format_digest,
    format_error,
    format_status,
    format_weekly,
    split_message,
)
from config import Settings
from fetcher.imap import build_fetchers
from fetcher.models import Email, collapse_threads
from storage.db import DigestDB, iso, utcnow
from summarizer.llm import DigestResult
from summarizer.llm import summarize as default_summarize

logger = logging.getLogger(__name__)


@dataclass
class PipelineOutcome:
    result: DigestResult
    text: str
    digest_id: int | None = None
    skipped: bool = False
    deferred: bool = False
    skip_reason: str = ""
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def counts(self) -> dict[str, int]:
        return self.result.counts()


def touch_heartbeat(settings: Settings, note: str = "startup") -> None:
    """Create/refresh the heartbeat file (used by Docker/systemd monitoring)."""
    try:
        path = Path(settings.heartbeat_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{iso(utcnow())}	0	0	0	{note}\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk issues
        logger.warning("Cannot write heartbeat: %s", exc)


def _write_heartbeat(settings: Settings, outcome: PipelineOutcome) -> None:
    path = Path(settings.heartbeat_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            f"{iso(utcnow())}\t{outcome.result.total_emails}\t"
            f"{outcome.result.total_tokens}\t{int(outcome.skipped)}"
        )
        path.write_text(payload + "\n", encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk issues
        logger.warning("Cannot write heartbeat: %s", exc)


def run_digest_pipeline(
    settings: Settings,
    db: DigestDB,
    *,
    kind: str = "daily",
    force: bool = False,
    enqueue: bool = True,
    fetchers=None,
    summarize_fn=None,
) -> PipelineOutcome:
    """Collect, summarise, render and enqueue a digest."""
    started = time.monotonic()
    run_started = utcnow()
    errors: list[str] = []

    summarize_fn = summarize_fn or default_summarize

    if db.is_paused() and not force:
        logger.info("Digest skipped: paused")
        return PipelineOutcome(
            result=DigestResult(total_emails=0),
            text="",
            skipped=True,
            skip_reason="пауза",
        )

    fetchers = fetchers if fetchers is not None else build_fetchers(settings)
    default_since = run_started - timedelta(days=settings.catchup_max_days)

    collected: list[Email] = []
    # Remember the window each mailbox was read from: a quota failure has to
    # rewind to *that* instant, not to "now", or the messages we left pending
    # would fall outside the next fetch window and be lost for good.
    windows: dict[str, datetime] = {}
    ok_mailboxes: list[str] = []
    for fetcher in fetchers:
        since = db.get_mailbox_state(fetcher.mailbox_name) or default_since
        windows[fetcher.mailbox_name] = since
        logger.info("[%s] fetching since %s", fetcher.mailbox_name, since.isoformat())
        try:
            emails = fetcher.fetch_since(since)
        except Exception as exc:  # noqa: BLE001 - one mailbox must not kill the run
            message = f"{fetcher.mailbox_name}: {exc}"
            errors.append(message)
            db.mark_mailbox_error(fetcher.mailbox_name, str(exc))
            logger.error("Fetcher failed: %s", message)
            continue
        # Only a successful fetch moves this mailbox's watermark.
        db.mark_mailbox_success(fetcher.mailbox_name, run_started)
        ok_mailboxes.append(fetcher.mailbox_name)
        collected.extend(emails)

    collected.sort(key=lambda email: email.date)

    # Register first: bulk mail is deduplicated too, so it is never re-fetched or
    # re-counted, but it does not have to reach the LLM.
    pending_before = db.get_pending_ids()
    newly_seen = db.register_emails(collected)
    newly_seen_ids = {email.message_id for email in newly_seen}

    # A previous run may have left messages queued (LLM quota or outage). They are
    # re-fetched because the watermark was rewound, so pick them up again instead
    # of ignoring them as "not new".
    requeued = [
        email
        for email in collected
        if email.message_id in pending_before and email.message_id not in newly_seen_ids
    ]
    if requeued:
        logger.info("Picked up %d message(s) left pending by a previous run", len(requeued))
    fresh = newly_seen + requeued
    newsletter_count = sum(1 for email in fresh if email.is_bulk)
    spam_count = sum(1 for email in fresh if email.is_bulk and "spam" in email.bulk_reason)

    if settings.newsletter_count_only:
        candidates = [email for email in fresh if not email.is_bulk]
    else:
        candidates = list(fresh)

    # Replies collapse into one digest bullet, but every member still has to be
    # marked processed — otherwise collapsed siblings stay 'pending' forever.
    thread_members: dict[str, list[str]] = {}
    for email in candidates:
        thread_members.setdefault(email.thread_key or email.message_id, []).append(email.message_id)

    to_summarise = collapse_threads(candidates)
    logger.info(
        "Pipeline: %d fetched / %d new / %d to summarise (%d bulk, %d spam)",
        len(collected), len(fresh), len(to_summarise), newsletter_count, spam_count,
    )

    result = summarize_fn(to_summarise, settings)
    result.newsletter_total = newsletter_count
    result.spam_total = spam_count
    # Calendar invites are deterministic data, not a model opinion: collect them
    # here so the section survives a total LLM outage.
    events = [event for email in to_summarise for event in email.calendar_events if event.start]
    result.calendar_events = sorted(events, key=lambda event: event.start or datetime.max)[:10]
    # Report every new email, including bulk mail that only got counted.
    result.total_emails = len(fresh)

    # Two-phase: hand failed chunks back for a retry instead of losing them.
    failed_representatives = set(result.failed_message_ids)
    failed_ids: set[str] = set()
    for email in to_summarise:
        key = email.thread_key or email.message_id
        if email.message_id in failed_representatives:
            failed_ids.update(thread_members.get(key, [email.message_id]))

    done_ids = [email.message_id for email in fresh if email.message_id not in failed_ids]
    db.mark_done(done_ids)
    if failed_ids:
        if result.quota_exhausted:
            # The account is rate-limited, not the messages — keep them queued
            # and pull the watermark back so the retry can actually see them.
            db.mark_pending(sorted(failed_ids))
            for name in ok_mailboxes:
                db.rewind_mailbox(name, windows[name])
            logger.warning(
                "%d message(s) kept pending: free-model quota exhausted; "
                "watermarks rewound for the retry",
                len(failed_ids),
            )
        else:
            gave_up = db.mark_retry(sorted(failed_ids), max_attempts=settings.llm_attempts)
            if gave_up:
                logger.warning("%d message(s) permanently failed summarisation", gave_up)

    counts = result.counts()
    duration_ms = int((time.monotonic() - started) * 1000)

    now_local = datetime.now(tz=settings.tz)
    text = format_digest(result, now_local)

    nothing_to_report = (
        result.total_emails == 0
        and not result.calendar_events
        and newsletter_count == 0
        and spam_count == 0
    )
    skipped = bool(settings.quiet_when_empty and nothing_to_report and not force)

    status = "partial" if errors else "ok"
    digest_id: int | None = None
    # A manual /digest still gets the plain listing: the user asked *now*.
    # A scheduled run waits for the quota to reset instead of sending a
    # subject-only digest and then repeating itself tomorrow.
    deferred = bool(result.quota_exhausted and kind not in {"manual", "dry-run"})
    if not skipped and enqueue and not deferred:
        digest_id = db.add_history(
            kind=kind,
            text=text,
            payload_json=__import__("json").dumps(result.to_dict(), ensure_ascii=False),
            total_emails=result.total_emails,
        )
        db.enqueue(digest_id, split_message(text))

    db.record_run(
        kind=kind,
        email_count=result.total_emails,
        important_count=counts["important"],
        work_count=counts["work"],
        other_count=counts["other"],
        newsletter_count=newsletter_count,
        spam_count=spam_count,
        cost_usd=result.cost_usd,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        provider=result.provider,
        model=result.model,
        status=status,
        duration_ms=duration_ms,
    )
    db.prune_old(days=settings.retention_days)

    outcome = PipelineOutcome(
        result=result,
        text=text,
        digest_id=digest_id,
        skipped=skipped,
        deferred=deferred,
        skip_reason="нечего присылать" if skipped else "",
        errors=errors,
        duration_ms=duration_ms,
    )
    _write_heartbeat(settings, outcome)
    logger.info(
        "Digest ready in %dms: %d emails, %d tokens, skipped=%s",
        duration_ms, result.total_emails, result.total_tokens, skipped,
    )
    return outcome


# ── schedule helpers ─────────────────────────────────────────────────────────

def digest_time(settings: Settings, db: DigestDB | None = None) -> tuple[int, int]:
    """Runtime override from ``bot_settings`` wins over ``.env``."""
    if db is not None:
        override = db.get_setting("digest_time")
        if override and ":" in override:
            hour_str, minute_str = override.split(":", 1)
            try:
                return int(hour_str), int(minute_str)
            except ValueError:
                logger.warning("Bad digest_time override: %r", override)
    return settings.digest_hour, settings.digest_minute


def _next_run_str(scheduler: AsyncIOScheduler, job_id: str) -> str:
    job = scheduler.get_job(job_id)
    if not job or not job.next_run_time:
        return "неизвестно"
    return job.next_run_time.strftime("%d.%m.%Y %H:%M")


def reschedule_digest(scheduler: AsyncIOScheduler, settings: Settings, db: DigestDB) -> str:
    """Re-apply the daily trigger after a ``/time`` change."""
    hour, minute = digest_time(settings, db)
    scheduler.reschedule_job(
        "daily_digest",
        trigger=CronTrigger(hour=hour, minute=minute, timezone=settings.tz),
    )
    return f"{hour:02d}:{minute:02d}"


def create_scheduler(settings: Settings, bot, db: DigestDB) -> AsyncIOScheduler:
    """Build the scheduler: daily digest, weekly summary, outbox retry."""
    scheduler = AsyncIOScheduler(timezone=settings.tz)

    async def scheduled_digest() -> None:
        logger.info("Scheduled digest triggered")
        # Same lock as /digest: a cron run and a manual run must never overlap.
        async with bot.digest_lock:
            try:
                outcome = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: run_digest_pipeline(settings, db, kind="daily")
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Scheduled digest failed: %s", exc, exc_info=True)
                await bot.send_text(format_error(exc))
                return
            if outcome.skipped:
                logger.info("Scheduled digest skipped: %s", outcome.skip_reason)
                return
            if outcome.deferred:
                await _postpone_for_quota(outcome)
                return
            await bot.deliver_pending(outcome.digest_id)

    async def _postpone_for_quota(outcome: PipelineOutcome) -> None:
        """Re-run once the free-model quota resets instead of sending a stub."""
        reset = outcome.result.quota_reset_at
        run_at = (reset + timedelta(minutes=2)) if reset else (utcnow() + timedelta(hours=4))
        when = run_at.astimezone(settings.tz).strftime("%d.%m %H:%M")
        scheduler.add_job(
            quota_retry,
            "date",
            run_date=run_at,
            id="quota_retry",
            replace_existing=True,
            misfire_grace_time=7200,
        )
        logger.warning("Free-model quota exhausted; digest postponed until %s", when)
        await bot.send_text(
            "⏳ <b>Лимит бесплатных моделей исчерпан.</b>\n"
            f"Соберу дайджест автоматически после сброса квоты — около <b>{when}</b>.\n"
            "<i>Письма не потеряны: они ждут в очереди.</i>",
            silent=True,
        )

    async def quota_retry() -> None:
        logger.info("Quota retry: running the postponed digest")
        async with bot.digest_lock:
            try:
                outcome = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: run_digest_pipeline(settings, db, kind="daily")
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Quota retry failed: %s", exc, exc_info=True)
                return
            if outcome.skipped:
                return
            if outcome.deferred:
                logger.error("Quota still exhausted after the reset — giving up until tomorrow")
                return
            # Silent: this lands in the middle of the night for most timezones.
            await bot.deliver_pending(outcome.digest_id, silent=True)

    async def scheduled_weekly() -> None:
        if not settings.weekly_digest_enabled:
            return
        try:
            text = format_weekly(db.stats())
            digest_id = db.add_history(kind="weekly", text=text, total_emails=0)
            db.enqueue(digest_id, split_message(text))
            await bot.deliver_pending(digest_id, with_keyboard=False)
        except Exception as exc:  # noqa: BLE001
            logger.error("Weekly digest failed: %s", exc, exc_info=True)

    async def retry_outbox() -> None:
        if db.pending_outbox_count():
            sent = await bot.deliver_pending(None, with_keyboard=False)
            if sent:
                logger.info("Outbox retry delivered %d message(s)", sent)

    hour, minute = digest_time(settings, db)
    scheduler.add_job(
        scheduled_digest,
        CronTrigger(hour=hour, minute=minute, timezone=settings.tz),
        id="daily_digest",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        scheduled_weekly,
        CronTrigger(
            day_of_week=settings.weekly_digest_weekday,
            hour=settings.weekly_digest_hour,
            minute=settings.weekly_digest_minute,
            timezone=settings.tz,
        ),
        id="weekly_digest",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        retry_outbox,
        "interval",
        seconds=settings.outbox_flush_interval,
        id="outbox_retry",
        replace_existing=True,
    )

    logger.info(
        "Scheduler configured: daily %02d:%02d, weekly %s %02d:%02d (%s)",
        hour, minute, settings.weekly_digest_weekday,
        settings.weekly_digest_hour, settings.weekly_digest_minute, settings.timezone,
    )
    return scheduler


def build_status_text(settings: Settings, db: DigestDB, scheduler: AsyncIOScheduler | None) -> str:
    hour, minute = digest_time(settings, db)
    paused_until = db.get_setting("paused_until")
    paused_pretty = None
    if paused_until:
        from storage.db import parse_dt

        parsed = parse_dt(paused_until)
        if parsed and parsed > utcnow():
            paused_pretty = parsed.astimezone(settings.tz).strftime("%d.%m.%Y %H:%M")
    mailboxes = [
        {
            **state,
            # Render in the configured zone, not the server's local time.
            "last_ok_str": (
                state["last_ok_at"].astimezone(settings.tz).strftime("%d.%m %H:%M")
                if state.get("last_ok_at")
                else ""
            ),
        }
        for state in db.mailbox_states()
    ]
    return format_status(
        stats=db.stats(),
        mailboxes=mailboxes,
        next_run=_next_run_str(scheduler, "daily_digest") if scheduler else "неизвестно",
        digest_time=f"{hour:02d}:{minute:02d}",
        paused_until=paused_pretty,
    )
