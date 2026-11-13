"""
Entry point.

Modes::

    python main.py            # bot + scheduler (normal operation)
    python main.py --once     # one digest run, printed to stdout, nothing sent
    python main.py --dry-run  # same, but against a throwaway in-memory database
    python main.py --weekly   # print the weekly summary
    python main.py --check    # validate config, IMAP logins and the LLM key
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot.formatter import format_error, format_weekly
from bot.telegram_bot import DigestBot
from config import Settings, get_settings
from fetcher.imap import build_fetchers
from scheduler.jobs import (
    PipelineOutcome,
    create_scheduler,
    run_digest_pipeline,
    touch_heartbeat,
)
from storage.db import DigestDB

logger = logging.getLogger("main")


def setup_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if settings.log_file:
        try:
            path = Path(settings.log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:  # pragma: no cover - read-only fs
            logger.warning("Cannot open log file %s: %s", settings.log_file, exc)

    # httpx logs every Telegram API call including the bot token in the URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def acquire_single_instance_lock(path: str):
    """Return an open file handle holding the lock, or ``None`` if taken."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = target.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(__import__("os").getpid()))
    handle.flush()
    return handle


def _print_outcome(outcome: PipelineOutcome) -> None:
    if outcome.skipped:
        print(f"[skipped] {outcome.skip_reason}")
        return
    print(outcome.text)
    if outcome.errors:
        print("\n[errors]", *outcome.errors, sep="\n  ")


def run_once(settings: Settings, *, dry_run: bool = False) -> int:
    db = DigestDB(":memory:" if dry_run else settings.db_path)
    try:
        outcome = run_digest_pipeline(
            settings, db, kind="dry-run" if dry_run else "once", force=True, enqueue=False
        )
        _print_outcome(outcome)
        return 0 if not outcome.errors else 1
    except Exception as exc:  # noqa: BLE001
        logger.error("Digest failed: %s", exc, exc_info=True)
        print(format_error(exc))
        return 1
    finally:
        db.close()


def run_weekly(settings: Settings) -> int:
    db = DigestDB(settings.db_path)
    try:
        print(format_weekly(db.stats()))
        return 0
    finally:
        db.close()


def run_check(settings: Settings) -> int:
    ok = True
    print(f"Provider: {settings.llm_provider}")
    chain = settings.model_chain if settings.llm_provider == "openrouter" else [settings.openai_model]
    print("Model chain:", " → ".join(chain))

    if settings.llm_provider == "openrouter":
        from summarizer.llm import fetch_openrouter_pricing, quota_status

        quota = quota_status(settings)
        if quota:
            used, limit = quota.get("used"), quota.get("limit")
            remaining = quota.get("remaining")
            print(f"Квота бесплатных моделей: {used}/{limit} за сутки, осталось {remaining}")
            if not remaining:
                print("  ⚠️ дневной лимит исчерпан — бесплатные модели ответят 429 до сброса в 00:00 UTC")
                print("  подсказка: 10 кредитов на openrouter.ai поднимают лимит до 1000 запросов/сутки")

        pricing = fetch_openrouter_pricing(settings.openrouter_api_base, settings.openrouter_api_key)
        if pricing:
            missing = [m for m in chain if m not in pricing]
            print(f"OpenRouter: ключ принят, моделей в каталоге: {len(pricing)}")
            if missing:
                ok = False
                print("  ⚠️ нет в каталоге:", ", ".join(missing))
            for model in chain:
                entry = pricing.get(model) or {}
                free = ":free" in model or (
                    str(entry.get("prompt")) in {"0", "0.0"} and str(entry.get("completion")) in {"0", "0.0"}
                )
                print(f"  · {model} — {'бесплатная' if free else 'платная'}")
        else:
            ok = False
            print("OpenRouter: не удалось получить каталог (проверь ключ и сеть)")

    for fetcher in build_fetchers(settings):
        try:
            print("IMAP:", fetcher.test_connection())
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"IMAP: ❌ {fetcher.mailbox_name}: {exc}")

    print("OK" if ok else "ЕСТЬ ПРОБЛЕМЫ")
    return 0 if ok else 1


def run_bot(settings: Settings) -> int:
    lock = None
    if settings.single_instance:
        lock = acquire_single_instance_lock(settings.lock_path)
        if lock is None:
            logger.error(
                "Another instance already holds %s — refusing to start a second poller",
                settings.lock_path,
            )
            return 1

    db = DigestDB(settings.db_path)

    def run_digest(*, kind: str = "daily", force: bool = False) -> PipelineOutcome:
        return run_digest_pipeline(settings, db, kind=kind, force=force)

    bot = DigestBot(settings, db, run_digest)
    scheduler = create_scheduler(settings, bot, db)
    bot.set_scheduler(scheduler)

    async def post_init(application) -> None:
        scheduler.start()
        touch_heartbeat(settings, "startup")
        logger.info(
            "Digest scheduled at %02d:%02d %s — starting polling",
            settings.digest_hour, settings.digest_minute, settings.timezone,
        )
        try:
            await bot.post_startup(application)
        except Exception as exc:  # noqa: BLE001 - greeting is not critical
            logger.warning("Startup notification failed: %s", exc)
        await bot.deliver_pending(None, with_keyboard=False)

    async def post_shutdown(application) -> None:
        logger.info("Shutting down scheduler…")
        try:
            scheduler.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Scheduler shutdown skipped: %s", exc)
        db.close()
        if lock is not None:
            lock.close()

    app = bot.build_app(post_init=post_init, post_shutdown=post_shutdown)
    app.run_polling(drop_pending_updates=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Email Digest Agent")
    parser.add_argument("--once", action="store_true", help="run one digest and print it")
    parser.add_argument("--dry-run", action="store_true", help="like --once, with a throwaway DB")
    parser.add_argument("--weekly", action="store_true", help="print the weekly summary")
    parser.add_argument("--check", action="store_true", help="validate config, IMAP and LLM")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings)

    if args.check:
        return run_check(settings)
    if args.weekly:
        return run_weekly(settings)
    if args.dry_run:
        return run_once(settings, dry_run=True)
    if args.once:
        return run_once(settings)

    logger.info("Starting Email Digest Agent …")
    return run_bot(settings)


if __name__ == "__main__":
    sys.exit(main())
