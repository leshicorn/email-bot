"""Tests for :mod:`scheduler.jobs` — the digest pipeline with fake fetchers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fetcher.models import CalendarEvent
from scheduler.jobs import build_status_text, digest_time, run_digest_pipeline
from storage.db import iso, utcnow
from summarizer.llm import DigestResult, MailboxDigest


class FakeFetcher:
    """Minimal stand-in for :class:`fetcher.imap.IMAPFetcher`."""

    def __init__(self, name: str, emails=(), error: str | None = None) -> None:
        self.mailbox_name = name
        self.emails = list(emails)
        self.error = error
        self.calls: list[datetime] = []

    def fetch_since(self, since: datetime):
        self.calls.append(since)
        if self.error:
            raise RuntimeError(self.error)
        return list(self.emails)


def _capturing_summarizer(captured: list[list[object]]):
    def summarize_fn(emails, settings):
        captured.append(list(emails))
        return DigestResult(
            mailboxes=[MailboxDigest(name="Gmail", other=[{"subject": "Письмо"}])],
            action_items=["Проверить"],
            total_emails=len(emails),
            model="fake/model",
            provider="openrouter",
        )

    return summarize_fn


def test_pipeline_happy_path_records_run_and_enqueues(make_email, make_settings, db) -> None:
    settings = make_settings()
    captured: list[list[object]] = []
    fetchers = [
        FakeFetcher("Gmail", [make_email(1), make_email(2)]),
        FakeFetcher("mail.ru", [make_email(3, mailbox="mail.ru")]),
    ]

    outcome = run_digest_pipeline(
        settings, db, fetchers=fetchers, summarize_fn=_capturing_summarizer(captured)
    )

    assert outcome.errors == []
    assert outcome.skipped is False
    assert outcome.result.total_emails == 3
    assert outcome.digest_id is not None
    assert "Email-дайджест" in outcome.text
    assert db.pending_outbox_count() >= 1
    history = db.last_history()
    assert history is not None
    assert history["text"] == outcome.text
    run = db.last_run()
    assert run is not None
    assert run["email_count"] == 3
    assert run["status"] == "ok"
    assert len(captured) == 1
    assert len(captured[0]) == 3
    assert db.count_by_status() == {"done": 3}
    assert db.get_mailbox_state("Gmail") is not None
    assert db.get_mailbox_state("mail.ru") is not None
    assert Path(settings.heartbeat_path).exists()


def test_failing_fetcher_does_not_advance_its_watermark(make_email, make_settings, db) -> None:
    settings = make_settings()
    good = FakeFetcher("Gmail", [make_email(1)])
    bad = FakeFetcher("mail.ru", error="IMAP down")

    outcome = run_digest_pipeline(
        settings,
        db,
        fetchers=[good, bad],
        summarize_fn=_capturing_summarizer([]),
    )

    assert outcome.errors == ["mail.ru: IMAP down"]
    assert db.get_mailbox_state("Gmail") is not None
    assert db.get_mailbox_state("mail.ru") is None
    states = {row["mailbox"]: row for row in db.mailbox_states()}
    assert states["mail.ru"]["last_error"] == "IMAP down"
    assert db.last_run()["status"] == "partial"


def test_second_run_with_same_emails_is_quiet(make_email, make_settings, db) -> None:
    settings = make_settings(quiet_when_empty=True)
    emails = [make_email(1), make_email(2)]
    captured: list[list[object]] = []

    first = run_digest_pipeline(
        settings,
        db,
        fetchers=[FakeFetcher("Gmail", emails)],
        summarize_fn=_capturing_summarizer(captured),
    )
    pending_after_first = db.pending_outbox_count()
    second = run_digest_pipeline(
        settings,
        db,
        fetchers=[FakeFetcher("Gmail", emails)],
        summarize_fn=_capturing_summarizer(captured),
    )

    assert first.skipped is False
    assert second.skipped is True
    assert second.result.total_emails == 0
    assert second.digest_id is None
    assert captured[1] == []
    assert db.pending_outbox_count() == pending_after_first


def test_bulk_mail_is_counted_but_not_summarised(make_email, make_settings, db) -> None:
    settings = make_settings()
    bulk = make_email(1, is_bulk=True, bulk_reason="list-unsubscribe")
    normal = make_email(2)
    captured: list[list[object]] = []

    outcome = run_digest_pipeline(
        settings,
        db,
        fetchers=[FakeFetcher("Gmail", [bulk, normal])],
        summarize_fn=_capturing_summarizer(captured),
    )

    assert [email.message_id for email in captured[0]] == [normal.message_id]
    assert outcome.result.newsletter_total == 1
    assert outcome.result.spam_total == 0
    assert "рассылок: <b>1</b>" in outcome.text
    assert db.last_run()["newsletter_count"] == 1
    assert db.count_by_status() == {"done": 2}


def test_thread_collapsing_marks_every_member_done(make_email, make_settings, db) -> None:
    settings = make_settings()
    older = make_email(
        1, thread_key="thread-42", date=datetime(2024, 11, 18, 9, 0, tzinfo=UTC)
    )
    newer = make_email(
        2, thread_key="thread-42", date=datetime(2024, 11, 18, 10, 0, tzinfo=UTC)
    )
    captured: list[list[object]] = []

    outcome = run_digest_pipeline(
        settings,
        db,
        fetchers=[FakeFetcher("Gmail", [older, newer])],
        summarize_fn=_capturing_summarizer(captured),
    )

    assert len(captured[0]) == 1
    survivor = captured[0][0]
    assert survivor.message_id == newer.message_id
    assert survivor.thread_count == 2
    assert outcome.result.total_emails == 2
    assert db.count_by_status() == {"done": 2}
    assert "pending" not in db.count_by_status()


def test_paused_pipeline_skips_before_fetching(make_email, make_settings, db) -> None:
    settings = make_settings()
    db.set_setting("paused_until", iso(utcnow() + timedelta(days=1)))
    fetcher = FakeFetcher("Gmail", [make_email(1)])
    captured: list[list[object]] = []

    outcome = run_digest_pipeline(
        settings, db, fetchers=[fetcher], summarize_fn=_capturing_summarizer(captured)
    )

    assert outcome.skipped is True
    assert outcome.skip_reason == "пауза"
    assert fetcher.calls == []
    assert captured == []
    assert db.pending_outbox_count() == 0
    assert db.last_run() is None


def test_digest_time_uses_db_override_and_falls_back(make_settings, db) -> None:
    settings = make_settings(digest_hour=21, digest_minute=0)

    assert digest_time(settings) == (21, 0)
    assert digest_time(settings, db) == (21, 0)

    db.set_setting("digest_time", "08:05")
    assert digest_time(settings, db) == (8, 5)

    db.set_setting("digest_time", "not-a-time")
    assert digest_time(settings, db) == (21, 0)


def test_build_status_text_contains_next_run_label(make_settings, db) -> None:
    settings = make_settings(digest_hour=7, digest_minute=45)

    text = build_status_text(settings, db, None)
    assert "Следующий запуск" in text
    assert "неизвестно" in text
    assert "07:45" in text

    class FakeJob:
        next_run_time = datetime(2025, 1, 2, 21, 0, tzinfo=UTC)

    class FakeScheduler:
        def get_job(self, job_id: str):
            assert job_id == "daily_digest"
            return FakeJob()

    scheduled = build_status_text(settings, db, FakeScheduler())
    assert "02.01.2025 21:00" in scheduled


def _quota_summarizer(emails, settings):
    """Stand-in for a run where the whole free-model quota is already spent."""
    return DigestResult(
        mailboxes=[MailboxDigest(name="Gmail", other=[{"subject": "Тема", "summary": "⚠️ AI"}] )],
        total_emails=len(emails),
        failed_message_ids=[email.message_id for email in emails],
        fallback_used=True,
        quota_exhausted=True,
    )


def test_quota_outage_keeps_mail_pending_and_rewinds_window(
    make_email, make_settings, db
) -> None:
    """A spent quota must not consume messages or move the fetch window forward."""
    settings = make_settings()
    fetcher = FakeFetcher("Gmail", [make_email(1), make_email(2)])

    outcome = run_digest_pipeline(
        settings, db, fetchers=[fetcher], summarize_fn=_quota_summarizer
    )

    # Nothing was summarised, so nothing may be marked done…
    assert db.count_by_status() == {"pending": 2}
    # …and the watermark has to go back to the window we actually read,
    # otherwise these messages fall out of the next fetch and are lost.
    # Stored timestamps keep second precision, hence the truncation.
    assert db.get_mailbox_state("Gmail") == fetcher.calls[0].replace(microsecond=0)
    # A scheduled run does not send a subject-only stub: it postpones itself
    # until the quota resets, so nothing may land in the outbox now.
    assert outcome.skipped is False
    assert outcome.deferred is True
    assert outcome.digest_id is None
    assert db.pending_outbox_count() == 0


def test_quota_retry_summarises_the_requeued_mail(make_email, make_settings, db) -> None:
    """After the quota resets, the pending messages are picked up again."""
    settings = make_settings()
    first_window = []
    fetcher = FakeFetcher("Gmail", [make_email(1), make_email(2)])

    run_digest_pipeline(settings, db, fetchers=[fetcher], summarize_fn=_quota_summarizer)
    first_window.append(fetcher.calls[-1])

    captured: list[list[object]] = []
    outcome = run_digest_pipeline(
        settings, db, fetchers=[fetcher], summarize_fn=_capturing_summarizer(captured)
    )

    # The retry re-read the same window instead of moving past the queued mail.
    assert fetcher.calls[-1] <= first_window[0]
    assert db.count_by_status() == {"done": 2}
    assert len(captured) == 1
    assert {email.message_id for email in captured[0]} == {
        "<msg-1@example.com>",
        "<msg-2@example.com>",
    }
    assert outcome.result.quota_exhausted is False


def test_enqueue_false_does_not_touch_the_outbox(make_email, make_settings, db) -> None:
    """``--once`` prints the digest; the running bot must not send it again."""
    settings = make_settings()
    outcome = run_digest_pipeline(
        settings,
        db,
        fetchers=[FakeFetcher("Gmail", [make_email(1)])],
        summarize_fn=_capturing_summarizer([]),
        enqueue=False,
    )

    assert outcome.text
    assert outcome.digest_id is None
    assert db.pending_outbox_count() == 0


def test_manual_digest_sends_a_plain_listing_when_quota_is_spent(
    make_email, make_settings, db
) -> None:
    """`/digest` was asked for *now*, so it answers now — without AI text."""
    settings = make_settings()
    outcome = run_digest_pipeline(
        settings,
        db,
        kind="manual",
        force=True,
        fetchers=[FakeFetcher("Gmail", [make_email(1)])],
        summarize_fn=_quota_summarizer,
    )

    assert outcome.deferred is False
    assert outcome.digest_id is not None
    assert db.pending_outbox_count() >= 1
    assert "лимит бесплатных моделей" in outcome.text
    # Still queued for the retry after the reset.
    assert db.count_by_status() == {"pending": 1}


async def test_scheduled_run_postpones_itself_when_quota_is_spent(
    make_settings, db, monkeypatch
) -> None:
    """A cron run must reschedule instead of sending a stub at 21:00."""
    import asyncio

    from scheduler import jobs as jobs_module

    settings = make_settings()

    class FakeBot:
        def __init__(self) -> None:
            self.digest_lock = asyncio.Lock()
            self.sent: list[tuple[str, bool]] = []
            self.delivered: list[object] = []

        async def send_text(self, text, reply_markup=None, *, silent=False):
            self.sent.append((text, silent))

        async def deliver_pending(self, digest_id=None, **kwargs):
            self.delivered.append(digest_id)
            return 1

    reset = datetime.now(UTC) + timedelta(minutes=5)
    outcome = jobs_module.PipelineOutcome(
        result=DigestResult(total_emails=2, quota_exhausted=True, quota_reset_at=reset),
        text="stub",
        deferred=True,
    )
    monkeypatch.setattr(jobs_module, "run_digest_pipeline", lambda *a, **k: outcome)

    bot = FakeBot()
    scheduler = jobs_module.create_scheduler(settings, bot, db)
    scheduler.start()
    try:
        job = scheduler.get_job("daily_digest")
        assert job is not None
        await job.func()

        assert bot.delivered == []  # nothing was sent from the outbox
        assert len(bot.sent) == 1
        assert bot.sent[0][1] is True  # silent: the user is asleep
        assert "Лимит бесплатных моделей" in bot.sent[0][0]

        retry = scheduler.get_job("quota_retry")
        assert retry is not None
        assert retry.next_run_time == reset + timedelta(minutes=2)
    finally:
        scheduler.shutdown(wait=False)


def test_calendar_section_does_not_depend_on_the_model(make_email, make_settings, db) -> None:
    """Invites are deterministic data: they must render even if the model says nothing."""
    settings = make_settings()
    event = CalendarEvent(
        summary="Защита диплома",
        start=datetime(2024, 11, 18, 10, 0, tzinfo=UTC),
        end=datetime(2024, 11, 18, 11, 30, tzinfo=UTC),
        location="каб. 204",
    )
    outcome = run_digest_pipeline(
        settings,
        db,
        fetchers=[FakeFetcher("Gmail", [make_email(1, calendar_events=[event])])],
        summarize_fn=_capturing_summarizer([]),
    )

    assert outcome.result.calendar_events == [event]
    assert "📅 <b>События" in outcome.text
    assert "Защита диплома" in outcome.text
    assert "каб. 204" in outcome.text
