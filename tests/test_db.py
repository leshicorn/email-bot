"""Tests for :mod:`storage.db` — dedup, watermarks, outbox, history, migration."""
from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from storage.db import DigestDB, iso, utcnow


def test_register_emails_returns_only_unseen_ids(db, make_email) -> None:
    first_batch = [make_email(1), make_email(2), make_email(1)]

    fresh = db.register_emails(first_batch)
    assert [email.message_id for email in fresh] == [
        "<msg-1@example.com>",
        "<msg-2@example.com>",
    ]

    second_batch = db.register_emails([make_email(2), make_email(3)])
    assert [email.message_id for email in second_batch] == ["<msg-3@example.com>"]

    assert db.count_processed() == 3
    assert db.count_by_status() == {"pending": 3}


def test_mark_done_flips_status_and_is_idempotent(db, make_email) -> None:
    ids = [email.message_id for email in db.register_emails([make_email(1), make_email(2)])]

    assert db.mark_done(ids) == 2
    assert db.mark_done(ids) == 2
    assert db.mark_done([]) == 0
    assert db.count_by_status() == {"done": 2}


def test_mark_retry_increments_attempts_and_eventually_fails(db, make_email) -> None:
    message_id = db.register_emails([make_email(1)])[0].message_id

    assert db.mark_retry([message_id], max_attempts=3) == 0
    assert db.mark_retry([message_id], max_attempts=3) == 0
    assert db.mark_retry([message_id], max_attempts=3) == 1
    assert db.mark_retry([], max_attempts=3) == 0

    row = db._conn.execute(
        "SELECT attempts, status FROM processed_emails WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    assert (row["attempts"], row["status"]) == (3, "failed")
    assert db.count_by_status() == {"failed": 1}


def test_mark_pending_returns_to_queue_without_counting_an_attempt(db, make_email) -> None:
    message_id = db.register_emails([make_email(1)])[0].message_id
    db.mark_retry([message_id], max_attempts=3)
    db.mark_retry([message_id], max_attempts=3)
    db.mark_done([message_id])

    assert db.mark_pending([message_id]) == 1
    assert db.mark_pending([]) == 0

    row = db._conn.execute(
        "SELECT status, attempts FROM processed_emails WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    assert (row["status"], row["attempts"]) == ("pending", 2)
    assert db.get_pending_ids() == {message_id}


def test_get_pending_ids_tracks_what_is_still_queued(db, make_email) -> None:
    emails = db.register_emails([make_email(1), make_email(2)])
    first_id, second_id = (email.message_id for email in emails)

    assert db.get_pending_ids() == {first_id, second_id}

    db.mark_done([first_id])
    assert db.get_pending_ids() == {second_id}

    db.mark_done([second_id])
    assert db.get_pending_ids() == set()


def test_rewind_mailbox_moves_the_watermark_back(db) -> None:
    later = utcnow().replace(microsecond=0)
    earlier = later - timedelta(days=3)
    db.mark_mailbox_success("Gmail", later)

    db.rewind_mailbox("Gmail", earlier)

    assert db.get_mailbox_state("Gmail") == earlier
    assert db.get_mailbox_state("mail.ru") is None

    db.rewind_mailbox("mail.ru", earlier)
    assert db.get_mailbox_state("mail.ru") == earlier
    last_success = utcnow().replace(microsecond=0) - timedelta(hours=2)
    db.mark_mailbox_success("Gmail", last_success)

    db.mark_mailbox_error("Gmail", "IMAP down")

    assert db.get_mailbox_state("Gmail") == last_success
    states = {state["mailbox"]: state for state in db.mailbox_states()}
    assert set(states) == {"Gmail", "mail.ru"}
    assert states["Gmail"]["last_ok_at"] == last_success
    assert states["Gmail"]["last_error"] == "IMAP down"
    # A failed fetch must not wipe the watermark of an earlier success.
    assert states["mail.ru"]["last_ok_at"] == earlier


def test_mailbox_error_for_a_new_mailbox_has_no_watermark(db) -> None:
    db.mark_mailbox_error("mail.ru", "authentication failed")

    assert db.get_mailbox_state("mail.ru") is None
    assert db.mailbox_states()[0]["last_error"] == "authentication failed"


def test_outbox_enqueue_fail_and_sent_lifecycle(db) -> None:
    ids = db.enqueue(7, ["one", "two", "three"])

    assert len(ids) == 3
    assert db.pending_outbox_count() == 3
    pending = db.pending_outbox()
    assert [row["seq"] for row in pending] == [0, 1, 2]
    assert all(row["digest_id"] == 7 for row in pending)
    assert len(db.pending_outbox(limit=2)) == 2

    db.mark_outbox_sent(ids[0])
    assert db.pending_outbox_count() == 2

    db.mark_outbox_failed(ids[1], "telegram down", max_attempts=2)
    assert db.pending_outbox_count() == 2
    db.mark_outbox_failed(ids[1], "telegram down", max_attempts=2)
    assert db.pending_outbox_count() == 1

    row = db._conn.execute(
        "SELECT attempts, status, last_error FROM outbox WHERE id = ?", (ids[1],)
    ).fetchone()
    assert (row["attempts"], row["status"], row["last_error"]) == (
        2,
        "failed",
        "telegram down",
    )


def test_history_add_get_last_and_search(db) -> None:
    daily_id = db.add_history(
        kind="daily", text="Письмо: доставка завтра", payload_json='{"a": 1}', total_emails=2
    )
    weekly_id = db.add_history(
        kind="weekly", text="Неделя: доставка и счета", total_emails=5
    )

    assert db.get_history(daily_id)["text"] == "Письмо: доставка завтра"
    assert db.get_history(9999) is None
    assert db.last_history()["id"] == weekly_id
    assert db.last_history("daily")["id"] == daily_id
    assert db.last_history("monthly") is None

    rows = db.search_history("доставка")
    assert [row["id"] for row in rows] == [weekly_id, daily_id]
    assert [row["id"] for row in db.search_history("доставка", limit=1)] == [weekly_id]
    assert db.search_history("нет такого") == []


def test_settings_roundtrip_and_delete(db) -> None:
    assert db.get_setting("digest_time") is None
    assert db.get_setting("digest_time", "21:00") == "21:00"

    db.set_setting("digest_time", "08:30")
    assert db.get_setting("digest_time") == "08:30"

    db.set_setting("digest_time", "09:15")
    assert db.get_setting("digest_time") == "09:15"

    db.delete_setting("digest_time")
    assert db.get_setting("digest_time") is None
    assert db.is_paused() is False


@pytest.mark.parametrize(("delta_hours", "expected"), [(1, True), (-1, False)])
def test_is_paused_compares_the_timestamp(db, delta_hours: int, expected: bool) -> None:
    db.set_setting("paused_until", iso(utcnow() + timedelta(hours=delta_hours)))
    assert db.is_paused() is expected


def test_prune_old_removes_only_old_finished_rows(db, make_email) -> None:
    db.register_emails([make_email(1)])
    old_history_id = db.add_history(kind="daily", text="old history")
    db.add_history(kind="daily", text="new history")
    sent_id = db.enqueue(None, ["old sent"])[0]
    db.mark_outbox_sent(sent_id)
    pending_id = db.enqueue(None, ["old pending"])[0]

    old_stamp = iso(utcnow() - timedelta(days=200))
    db._conn.execute("UPDATE processed_emails SET first_seen = ?", (old_stamp,))
    db._conn.execute(
        "UPDATE digest_history SET created_at = ? WHERE id = ?",
        (old_stamp, old_history_id),
    )
    db._conn.execute("UPDATE outbox SET created_at = ?", (old_stamp,))
    db._conn.commit()

    deleted = db.prune_old(days=90)

    assert deleted == 3
    assert db.count_processed() == 0
    assert db.last_history()["text"] == "new history"
    assert db.pending_outbox_count() == 1
    assert db.pending_outbox()[0]["id"] == pending_id


def test_migrates_a_database_created_by_the_old_schema(tmp_path, make_email) -> None:
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE processed_emails (
            message_id TEXT PRIMARY KEY,
            mailbox    TEXT NOT NULL,
            subject    TEXT
        );
        INSERT INTO processed_emails (message_id, mailbox, subject)
        VALUES ('old-1', 'Gmail', 'Old subject');

        CREATE TABLE digest_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at       TEXT NOT NULL,
            email_count  INTEGER NOT NULL DEFAULT 0,
            cost_usd     REAL NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO digest_runs (run_at, email_count, total_tokens)
        VALUES ('2024-01-01T00:00:00+00:00', 4, 321);
        """
    )
    connection.commit()
    connection.close()

    database = DigestDB(str(path))
    try:
        processed_columns = {
            row[1]
            for row in database._conn.execute("PRAGMA table_info(processed_emails)")
        }
        assert {"status", "attempts", "first_seen", "processed_at"} <= processed_columns

        run_columns = {
            row[1] for row in database._conn.execute("PRAGMA table_info(digest_runs)")
        }
        assert {"prompt_tokens", "completion_tokens"} <= run_columns

        assert database.count_processed() == 1
        assert database.count_by_status() == {"done": 1}
        old_run = database.last_run()
        assert old_run["email_count"] == 4
        assert old_run["total_tokens"] == 321
        assert old_run["status"] == "ok"

        # Tables added after the first release are usable on the migrated file.
        new_history_id = database.add_history(kind="daily", text="migrated")
        assert database.get_history(new_history_id)["text"] == "migrated"
        assert database.pending_outbox_count() == 0
        assert database.get_setting("missing") is None

        # A new row can be inserted and the migrated columns are writable.
        email = make_email(2)
        assert database.register_emails([email]) == [email]
        assert database.mark_done([email.message_id]) == 1

        database.record_run(email_count=5, prompt_tokens=3, completion_tokens=2, total_tokens=5)
        assert database.last_run()["email_count"] == 5
    finally:
        database.close()
