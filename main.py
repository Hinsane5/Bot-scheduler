"""Application entry point — boots the Discord bot + scheduler.

Schedules four jobs on an AsyncIOScheduler:

- ``sync_job``        every ``SYNC_INTERVAL_MIN`` minutes (default 180 = 3h).
                      First run fires ~30 s after boot so you don't wait.
- ``refresh_job_lms``      every ``LMS_REFRESH_INTERVAL_HOURS`` hours (default 20).
                           Only registered if "lms" in ENABLED_SCRAPERS.
- ``refresh_job_messier``  every ``MESSIER_REFRESH_INTERVAL_MIN`` minutes (default 25).
                           Only registered if "messier" in ENABLED_SCRAPERS.
- ``backup_job``      daily at ``BACKUP_HOUR_LOCAL`` local time.

The scheduler runs in the same asyncio loop as the bot. On shutdown
(Ctrl+C or bot disconnect), the scheduler is stopped gracefully.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src import config, jobs, reminders
from src.bot import create_bot


log = logging.getLogger("main")


def configure_logging() -> None:
    level_name = (config.LOG_LEVEL or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # discord.py is chatty at INFO — pin to WARNING unless DEBUG explicit.
    if level > logging.DEBUG:
        logging.getLogger("discord").setLevel(logging.WARNING)
    # APScheduler is also noisy at INFO; keep its job-start messages but
    # silence the scheduler internal chatter unless DEBUG.
    if level > logging.DEBUG:
        logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)


def register_jobs(scheduler: AsyncIOScheduler, bot) -> None:
    """Register sync, per-portal refresh, and backup jobs."""
    now = datetime.now(tz=config.TZ)

    # Sync — first run ~30 s after boot, then every SYNC_INTERVAL_MIN.
    scheduler.add_job(
        jobs.sync_job,
        trigger="interval",
        minutes=config.SYNC_INTERVAL_MIN,
        args=[bot],
        id="sync_job",
        name="sync all enabled scrapers",
        next_run_time=now + timedelta(seconds=30),
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    log.info(
        "Registered sync_job: every %d min (first run in 30 s)",
        config.SYNC_INTERVAL_MIN,
    )

    # Per-portal refresh — only for enabled portals.
    if "lms" in config.ENABLED_SCRAPERS:
        scheduler.add_job(
            jobs.refresh_job,
            trigger="interval",
            hours=config.LMS_REFRESH_INTERVAL_HOURS,
            args=[bot, "lms"],
            id="refresh_job_lms",
            name="refresh LMS JWT via SSO cookies",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
        )
        log.info(
            "Registered refresh_job_lms: every %d h",
            config.LMS_REFRESH_INTERVAL_HOURS,
        )

    if "messier" in config.ENABLED_SCRAPERS:
        scheduler.add_job(
            jobs.refresh_job,
            trigger="interval",
            minutes=config.MESSIER_REFRESH_INTERVAL_MIN,
            args=[bot, "messier"],
            id="refresh_job_messier",
            name="refresh Messier .ASPXAUTH (sliding session)",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=120,
        )
        log.info(
            "Registered refresh_job_messier: every %d min",
            config.MESSIER_REFRESH_INTERVAL_MIN,
        )

    # Daily backup at BACKUP_HOUR_LOCAL:00 local time.
    scheduler.add_job(
        jobs.backup_job,
        trigger="cron",
        hour=config.BACKUP_HOUR_LOCAL,
        minute=0,
        id="backup_job",
        name="daily SQLite backup",
        coalesce=True,
        max_instances=1,
    )
    log.info(
        "Registered backup_job: daily at %02d:00 %s",
        config.BACKUP_HOUR_LOCAL,
        config.TIMEZONE,
    )

    # One-shot at boot+10s: schedule reminders for all existing DB events
    # so we don't have to wait for the first sync_job (+30s) to do it.
    # Waits for bot.is_ready() so user fetch works inside any reminder
    # that fires immediately after.
    scheduler.add_job(
        _initial_reminder_setup,
        trigger="date",
        run_date=now + timedelta(seconds=10),
        args=[bot, scheduler],
        id="initial_reminder_setup",
        name="schedule reminders from existing DB events",
    )
    log.info("Registered initial_reminder_setup: one-shot at boot+10s")

    # Daily morning summary at 00:00 local time.
    # misfire_grace_time=3600 → if bot starts at 00:30, still send today's
    # summary; if it starts at 02:00, the run was missed by too much, skip.
    scheduler.add_job(
        reminders.daily_summary,
        trigger="cron",
        hour=0,
        minute=0,
        args=[bot],
        id="daily_summary_job",
        name="DM morning summary of today's events",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    log.info("Registered daily_summary_job: daily at 00:00 %s", config.TIMEZONE)


async def _initial_reminder_setup(bot, scheduler) -> None:
    """Wait briefly for bot to be ready, then schedule reminders from DB."""
    from src import reminders

    for _ in range(30):
        if bot.is_ready():
            break
        await asyncio.sleep(1)
    await reminders.reschedule_all(bot, scheduler)


async def amain() -> None:
    bot = create_bot()
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    bot.scheduler = scheduler  # attached for future /status use

    register_jobs(scheduler, bot)
    scheduler.start()
    log.info("Scheduler started — %d job(s) registered.", len(scheduler.get_jobs()))

    try:
        await bot.start(config.DISCORD_TOKEN)
    finally:
        scheduler.shutdown(wait=False)
        log.info("Scheduler stopped.")


def main() -> None:
    configure_logging()

    if not config.DISCORD_TOKEN:
        log.error("DISCORD_TOKEN is not set in .env — cannot start the bot.")
        raise SystemExit(1)

    if not config.DISCORD_GUILD_ID:
        log.warning(
            "DISCORD_GUILD_ID is not set; slash commands will sync globally "
            "and can take ~1 h to appear."
        )

    log.info("Starting Discord bot + scheduler…")
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down.")


if __name__ == "__main__":
    main()
