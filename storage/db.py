"""
SQLite storage.

Responsibilities
----------------
``processed_emails``  two-phase dedup: rows are inserted as ``pending`` *before*
                      summarisation and flipped to ``done`` (or retried) after.
``mailbox_state``     per-mailbox watermark of the last successful fetch. One
                      broken mailbox must not move the window for the others.
``digest_runs``       one row per pipeline run: counters, tokens, cost, status.
``digest_history``    rendered digest text + JSON payload, powering ``/last``,
                      ``/search`` and the "important only" button.
``outbox``            rendered messages awaiting delivery, so a Telegram outage
                      does not lose a digest that was already paid for.
``bot_settings``      runtime overrides (pause, schedule) editable from Telegram.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_emails (
    message_id   TEXT PRIMARY KEY,
    mailbox      TEXT NOT NULL,
    subject      TEXT,
    status       TEXT NOT NULL DEFAULT 'done',
    attempts     INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS mailbox_state (
    mailbox         TEXT PRIMARY KEY,
    last_ok_at      TEXT,
    last_attempt_at TEXT,
    last_error      TEXT
);

CREATE TABLE IF NOT EXISTS digest_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at            TEXT NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'daily',
    email_count       INTEGER NOT NULL DEFAULT 0,
    important_count   INTEGER NOT NULL DEFAULT 0,
    work_count        INTEGER NOT NULL DEFAULT 0,
    other_count       INTEGER NOT NULL DEFAULT 0,
    newsletter_count  INTEGER NOT NULL DEFAULT 0,
    spam_count        INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    provider          TEXT,
    model             TEXT,
    status            TEXT NOT NULL DEFAULT 'ok',
    duration_ms       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS digest_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'daily',
    text         TEXT NOT NULL,
    payload_json TEXT,
    total_emails INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_id  INTEGER,
    seq        INTEGER NOT NULL DEFAULT 0,
    text       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at    TEXT
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sender_stats (
    sender_email TEXT PRIMARY KEY,
    mailbox      TEXT,
    seen_count   INTEGER NOT NULL DEFAULT 0,
    last_seen    TEXT
);
"""

# Indexes are created *after* :data:`_MIGRATIONS`, because an old database may
# lack the columns they reference until the ALTER TABLE statements have run.
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_processed_status ON processed_emails(status);
CREATE INDEX IF NOT EXISTS idx_processed_at ON processed_emails(processed_at);
CREATE INDEX IF NOT EXISTS idx_history_created ON digest_history(created_at);
CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status);
"""

# Columns added after the first release; applied to pre-existing databases.
_MIGRATIONS: dict[str, dict[str, str]] = {
    "processed_emails": {
        "status": "TEXT NOT NULL DEFAULT 'done'",
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "first_seen": "TEXT NOT NULL DEFAULT ''",
        "processed_at": "TEXT",
    },
    "digest_runs": {
        "kind": "TEXT NOT NULL DEFAULT 'daily'",
        "important_count": "INTEGER NOT NULL DEFAULT 0",
        "work_count": "INTEGER NOT NULL DEFAULT 0",
        "other_count": "INTEGER NOT NULL DEFAULT 0",
        "newsletter_count": "INTEGER NOT NULL DEFAULT 0",
        "spam_count": "INTEGER NOT NULL DEFAULT 0",
        "prompt_tokens": "INTEGER NOT NULL DEFAULT 0",
        "completion_tokens": "INTEGER NOT NULL DEFAULT 0",
        "provider": "TEXT",
        "model": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'ok'",
        "duration_ms": "INTEGER NOT NULL DEFAULT 0",
    },
}


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_dt(value: str | None) -> datetime | None:
    """Parse either our ISO strings or SQLite's ``YYYY-MM-DD HH:MM:SS``."""
    if not value:
        return None
    try:
        text = value.replace(" ", "T", 1) if "T" not in value else value
        dt = datetime.fromisoformat(text)
    except ValueError:
        logger.warning("Unparseable timestamp: %r", value)
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


class DigestDB:
    """Thin synchronous SQLite wrapper with an explicit, migration-friendly schema."""

    def __init__(self, db_path: str = "data/digest.db") -> None:
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    # ── setup ────────────────────────────────────────────────────────────────
    def _migrate(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

        for table, columns in _MIGRATIONS.items():
            existing = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    logger.info("Migrating %s: adding column %s", table, name)
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

        # Only now can indexes over migrated columns be created safely.
        self._conn.executescript(_INDEXES)

        # Older rows predate the two-phase flow; treat them as completed.
        self._conn.execute(
            "UPDATE processed_emails SET processed_at = first_seen "
            "WHERE processed_at IS NULL AND status = 'done' AND first_seen != ''"
        )
        self._conn.commit()
        logger.debug("DB ready at %s", self.db_path)

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):  # pragma: no cover - defensive
            self._conn.close()

    def __enter__(self) -> DigestDB:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── settings ─────────────────────────────────────────────────────────────
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO bot_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, iso(utcnow())),
        )
        self._conn.commit()

    def delete_setting(self, key: str) -> None:
        self._conn.execute("DELETE FROM bot_settings WHERE key = ?", (key,))
        self._conn.commit()

    def is_paused(self) -> bool:
        until = parse_dt(self.get_setting("paused_until"))
        return bool(until and until > utcnow())

    # ── mailbox watermarks ───────────────────────────────────────────────────
    def get_mailbox_state(self, mailbox: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT last_ok_at FROM mailbox_state WHERE mailbox = ?", (mailbox,)
        ).fetchone()
        return parse_dt(row["last_ok_at"]) if row else None

    def mark_mailbox_success(self, mailbox: str, when: datetime) -> None:
        self._conn.execute(
            "INSERT INTO mailbox_state (mailbox, last_ok_at, last_attempt_at, last_error) "
            "VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(mailbox) DO UPDATE SET last_ok_at = excluded.last_ok_at, "
            "last_attempt_at = excluded.last_attempt_at, last_error = NULL",
            (mailbox, iso(when), iso(utcnow())),
        )
        self._conn.commit()

    def mark_mailbox_error(self, mailbox: str, error: str) -> None:
        self._conn.execute(
            "INSERT INTO mailbox_state (mailbox, last_ok_at, last_attempt_at, last_error) "
            "VALUES (?, NULL, ?, ?) "
            "ON CONFLICT(mailbox) DO UPDATE SET last_attempt_at = excluded.last_attempt_at, "
            "last_error = excluded.last_error",
            (mailbox, iso(utcnow()), error[:500]),
        )
        self._conn.commit()

    def mailbox_states(self) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT mailbox, last_ok_at, last_attempt_at, last_error FROM mailbox_state ORDER BY mailbox"
        ).fetchall()
        return [
            {
                "mailbox": row["mailbox"],
                "last_ok_at": parse_dt(row["last_ok_at"]),
                "last_attempt_at": parse_dt(row["last_attempt_at"]),
                "last_error": row["last_error"],
            }
            for row in rows
        ]

    # ── two-phase dedup ──────────────────────────────────────────────────────
    def register_emails(self, emails: Sequence[object]) -> list[object]:
        """Insert unseen message IDs as ``pending``; return only the new ones.

        This replaces the old "load the whole table and diff in Python" approach:
        the unique index does the work, and the row is written before the LLM is
        paid, so a crash can never bill the same message twice.
        """
        now = iso(utcnow())
        fresh: list[object] = []
        for email in emails:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO processed_emails "
                "(message_id, mailbox, subject, status, attempts, first_seen) "
                "VALUES (?, ?, ?, 'pending', 0, ?)",
                (email.message_id, email.mailbox, email.subject, now),  # type: ignore[attr-defined]
            )
            if cur.rowcount == 1:
                fresh.append(email)
        self._conn.commit()
        return fresh

    def mark_done(self, message_ids: Iterable[str]) -> int:
        ids = list(message_ids)
        if not ids:
            return 0
        self._conn.executemany(
            "UPDATE processed_emails SET status = 'done', processed_at = ? WHERE message_id = ?",
            [(iso(utcnow()), mid) for mid in ids],
        )
        self._conn.commit()
        return len(ids)

    def mark_retry(self, message_ids: Iterable[str], max_attempts: int = 3) -> int:
        """Bump ``attempts``; after *max_attempts* give up and mark ``failed``."""
        ids = list(message_ids)
        if not ids:
            return 0
        gave_up = 0
        for mid in ids:
            row = self._conn.execute(
                "SELECT attempts FROM processed_emails WHERE message_id = ?", (mid,)
            ).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            status = "failed" if attempts >= max_attempts else "pending"
            if status == "failed":
                gave_up += 1
            self._conn.execute(
                "UPDATE processed_emails SET attempts = ?, status = ? WHERE message_id = ?",
                (attempts, status, mid),
            )
        self._conn.commit()
        return gave_up

    def get_pending_ids(self) -> set[str]:
        """Message IDs left queued for a retry."""
        rows = self._conn.execute(
            "SELECT message_id FROM processed_emails WHERE status = 'pending'"
        ).fetchall()
        return {row["message_id"] for row in rows}

    def rewind_mailbox(self, mailbox: str, when: datetime) -> None:
        """Move a mailbox watermark back so the next run re-fetches the window.

        Used when a run could not summarise anything (LLM quota / outage): the
        messages stay ``pending``, and without rewinding they would fall out of
        the fetch window before the retry happens.
        """
        self._conn.execute(
            "INSERT INTO mailbox_state (mailbox, last_ok_at, last_attempt_at, last_error) "
            "VALUES (?, ?, ?, NULL) "
            "ON CONFLICT(mailbox) DO UPDATE SET last_ok_at = excluded.last_ok_at",
            (mailbox, iso(when), iso(utcnow())),
        )
        self._conn.commit()
        logger.info("Rewound watermark for %s to %s", mailbox, iso(when))

    def mark_pending(self, message_ids: Iterable[str]) -> int:
        """Return messages to the queue without counting an attempt.

        Used when the failure is account-wide (LLM quota, provider outage):
        the message is not at fault and retrying it tomorrow must not exhaust
        its three attempts.
        """
        ids = list(message_ids)
        if not ids:
            return 0
        self._conn.executemany(
            "UPDATE processed_emails SET status = 'pending' WHERE message_id = ?",
            [(mid,) for mid in ids],
        )
        self._conn.commit()
        return len(ids)

    def count_processed(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM processed_emails").fetchone()
        return int(row["n"]) if row else 0

    def count_by_status(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM processed_emails GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    # ── runs ─────────────────────────────────────────────────────────────────
    def record_run(
        self,
        *,
        kind: str = "daily",
        email_count: int = 0,
        important_count: int = 0,
        work_count: int = 0,
        other_count: int = 0,
        newsletter_count: int = 0,
        spam_count: int = 0,
        cost_usd: float = 0.0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        provider: str = "",
        model: str = "",
        status: str = "ok",
        duration_ms: int = 0,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO digest_runs (run_at, kind, email_count, important_count, work_count, "
            "other_count, newsletter_count, spam_count, cost_usd, prompt_tokens, completion_tokens, "
            "total_tokens, provider, model, status, duration_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                iso(utcnow()), kind, email_count, important_count, work_count, other_count,
                newsletter_count, spam_count, cost_usd, prompt_tokens, completion_tokens,
                total_tokens, provider, model, status, duration_ms,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def last_run_time(self) -> datetime | None:
        row = self._conn.execute("SELECT run_at FROM digest_runs ORDER BY id DESC LIMIT 1").fetchone()
        return parse_dt(row["run_at"]) if row else None

    def last_run(self) -> dict[str, object] | None:
        row = self._conn.execute("SELECT * FROM digest_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def runs_since(self, since: datetime) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT * FROM digest_runs WHERE run_at >= ? ORDER BY run_at", (iso(since),)
        ).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, object]:
        week_ago = iso(utcnow() - timedelta(days=7))
        row = self._conn.execute(
            "SELECT COUNT(*) AS runs, COALESCE(SUM(email_count), 0) AS emails, "
            "COALESCE(SUM(total_tokens), 0) AS tokens, COALESCE(SUM(cost_usd), 0) AS cost "
            "FROM digest_runs WHERE run_at >= ?",
            (week_ago,),
        ).fetchone()
        return {
            "processed_total": self.count_processed(),
            "status_counts": self.count_by_status(),
            "last_run": self.last_run(),
            "runs_7d": int(row["runs"]) if row else 0,
            "emails_7d": int(row["emails"]) if row else 0,
            "tokens_7d": int(row["tokens"]) if row else 0,
            "cost_7d": float(row["cost"]) if row else 0.0,
            "pending_outbox": self.pending_outbox_count(),
            "paused": self.is_paused(),
        }

    # ── digest history ───────────────────────────────────────────────────────
    def add_history(
        self,
        *,
        kind: str,
        text: str,
        payload_json: str | None = None,
        total_emails: int = 0,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO digest_history (created_at, kind, text, payload_json, total_emails) "
            "VALUES (?, ?, ?, ?, ?)",
            (iso(utcnow()), kind, text, payload_json, total_emails),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def get_history(self, digest_id: int) -> dict[str, object] | None:
        row = self._conn.execute("SELECT * FROM digest_history WHERE id = ?", (digest_id,)).fetchone()
        return dict(row) if row else None

    def last_history(self, kind: str | None = None) -> dict[str, object] | None:
        if kind:
            row = self._conn.execute(
                "SELECT * FROM digest_history WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT * FROM digest_history ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def search_history(self, query: str, limit: int = 10) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT * FROM digest_history WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # ── outbox ───────────────────────────────────────────────────────────────
    def enqueue(self, digest_id: int | None, chunks: Sequence[str]) -> list[int]:
        now = iso(utcnow())
        ids: list[int] = []
        for seq, text in enumerate(chunks):
            cur = self._conn.execute(
                "INSERT INTO outbox (digest_id, seq, text, status, attempts, created_at) "
                "VALUES (?, ?, ?, 'pending', 0, ?)",
                (digest_id, seq, text, now),
            )
            ids.append(int(cur.lastrowid or 0))
        self._conn.commit()
        return ids

    def pending_outbox(self, limit: int = 20) -> list[dict[str, object]]:
        rows = self._conn.execute(
            "SELECT * FROM outbox WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def pending_outbox_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM outbox WHERE status = 'pending'"
        ).fetchone()
        return int(row["n"]) if row else 0

    def mark_outbox_sent(self, outbox_id: int) -> None:
        self._conn.execute(
            "UPDATE outbox SET status = 'sent', sent_at = ? WHERE id = ?",
            (iso(utcnow()), outbox_id),
        )
        self._conn.commit()

    def mark_outbox_failed(self, outbox_id: int, error: str, max_attempts: int = 5) -> None:
        row = self._conn.execute(
            "SELECT attempts FROM outbox WHERE id = ?", (outbox_id,)
        ).fetchone()
        attempts = (row["attempts"] if row else 0) + 1
        status = "failed" if attempts >= max_attempts else "pending"
        self._conn.execute(
            "UPDATE outbox SET attempts = ?, status = ?, last_error = ? WHERE id = ?",
            (attempts, status, error[:500], outbox_id),
        )
        self._conn.commit()

    # ── housekeeping ─────────────────────────────────────────────────────────
    def prune_old(self, days: int = 90) -> int:
        deleted = 0
        for sql, params in (
            ("DELETE FROM processed_emails WHERE first_seen < ?", (iso(utcnow() - timedelta(days=days)),)),
            ("DELETE FROM digest_history WHERE created_at < ?", (iso(utcnow() - timedelta(days=days)),)),
            (
                "DELETE FROM outbox WHERE status != 'pending' AND created_at < ?",
                (iso(utcnow() - timedelta(days=days)),),
            ),
        ):
            cur = self._conn.execute(sql, params)
            deleted += cur.rowcount or 0
        self._conn.commit()
        if deleted:
            logger.info("Pruned %d old rows (> %d days)", deleted, days)
        return deleted

    def dump_json(self) -> str:  # pragma: no cover - debugging helper
        data = {
            table: [dict(r) for r in self._conn.execute(f"SELECT * FROM {table} LIMIT 50")]
            for table in ("processed_emails", "digest_runs", "outbox", "mailbox_state")
        }
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
