"""Background jobs run by the AsyncIOScheduler.

Three job types:

- ``sync_job``:    Run every enabled scraper, upsert events, log result.
- ``refresh_job``: Refresh the bearer/cookie session for one portal.
- ``backup_job``:  Daily SQLite snapshot to ``data/backup/`` with retention.

All jobs are designed to be safe-to-call when prerequisites are missing
(no auth state, no events, no DM target) — they log + return rather than
raise. The scheduler should never see an unhandled exception from one of
these jobs.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import discord

from src import auth, config, db
from src.scrapers import REGISTRY
from src.scrapers.base import SessionExpired


logger = logging.getLogger(__name__)


# --- sync_job ---------------------------------------------------------------

_SYNC_LOOKAHEAD_DAYS = 30


async def sync_job(bot: discord.Client) -> None:
    """Run all enabled scrapers, one at a time, each in its own try/except.

    After scrapers finish (success or failure), reschedule all reminder
    jobs so new/changed events get DM reminders.
    """
    for name in config.ENABLED_SCRAPERS:
        scraper = REGISTRY.get(name)
        if scraper is None:
            logger.warning("[sync] scraper %r not in REGISTRY; skipping", name)
            continue
        await _run_one_scraper(bot, name, scraper)
    await _reschedule_reminders(bot)


async def _reschedule_reminders(bot: discord.Client) -> None:
    """Rebuild the reminder schedule from current DB state.

    Pulled out so failures here don't mask sync failures and vice versa.
    """
    scheduler = getattr(bot, "scheduler", None)
    if scheduler is None:
        logger.warning("[sync] bot has no scheduler attached; skipping reschedule")
        return
    try:
        # Local import to avoid circular: jobs.py and reminders.py are peers.
        from src import reminders

        await reminders.reschedule_all(bot, scheduler)
    except Exception:
        logger.exception("[sync] reminder reschedule failed")


async def _run_one_scraper(bot: discord.Client, name: str, scraper: Any) -> None:
    today = datetime.now(tz=config.TZ).date()
    end = today + timedelta(days=_SYNC_LOOKAHEAD_DAYS)
    try:
        events = await scraper.fetch(today, end)
    except SessionExpired as exc:
        db.log_sync(name, False, error=f"session expired: {exc}")
        logger.warning("[sync] %s session expired: %s", name, exc)
        await _dm_user(
            bot,
            f"⚠️ **{name}** session expired — run "
            f"`python -m src.auth --portal={name}` and restart the bot.",
        )
        return
    except Exception as exc:  # noqa: BLE001 — sync_job must never crash the loop
        db.log_sync(name, False, error=f"{type(exc).__name__}: {exc}")
        logger.exception("[sync] %s failed unexpectedly", name)
        await _dm_user(
            bot,
            f"⚠️ **{name}** sync failed: `{type(exc).__name__}: {exc}`",
        )
        return

    try:
        inserted, updated = db.upsert_events(events)
    except Exception as exc:  # noqa: BLE001
        db.log_sync(name, False, error=f"db upsert failed: {exc}")
        logger.exception("[sync] %s db upsert failed", name)
        return

    db.log_sync(name, True, inserted, updated)
    logger.info(
        "[sync] %s: fetched %d events (%d new, %d updated)",
        name,
        len(events),
        inserted,
        updated,
    )


# --- refresh_job ------------------------------------------------------------

async def refresh_job(bot: discord.Client, portal: str) -> None:
    """Refresh saved session for ``portal`` headlessly.

    Silently returns if no auth state exists yet (user hasn't completed
    interactive login). Otherwise logs result to ``refresh_log`` and DMs
    the user only when the session is truly dead.
    """
    try:
        auth.load_creds(portal)
    except auth.AuthError:
        # No creds yet — user hasn't done interactive login for this portal.
        # Don't log or DM; this is expected.
        return

    try:
        ok = await auth.refresh(portal)
    except Exception as exc:  # noqa: BLE001
        db.log_refresh(portal, False, error=f"{type(exc).__name__}: {exc}")
        logger.exception("[refresh] %s failed unexpectedly", portal)
        await _dm_user(
            bot,
            f"⚠️ **{portal}** refresh failed: `{type(exc).__name__}: {exc}`",
        )
        return

    if ok:
        new_exp = None
        try:
            creds = auth.load_creds(portal)
            new_exp = creds.get("token_exp") or creds.get("last_refresh_at")
        except auth.AuthError:
            pass
        db.log_refresh(portal, True, new_token_exp=new_exp)
        logger.info("[refresh] %s OK (new exp/refresh: %s)", portal, new_exp)
    else:
        db.log_refresh(
            portal,
            False,
            error="refresh returned False — interactive re-auth needed",
        )
        logger.warning("[refresh] %s session truly expired", portal)
        await _dm_user(
            bot,
            f"⚠️ **{portal}** session truly expired — "
            f"run `python -m src.auth --portal={portal}` and restart the bot.",
        )


# --- backup_job -------------------------------------------------------------

BACKUP_DIR = Path(__file__).resolve().parents[1] / "data" / "backup"
BACKUP_RETENTION_DAYS = 7


async def backup_job() -> None:
    """Snapshot the SQLite DB to ``data/backup/events_YYYY-MM-DD.db``."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(tz=config.TZ).date()
    target = BACKUP_DIR / f"events_{today.isoformat()}.db"

    try:
        db.backup_to(target)
    except Exception:  # noqa: BLE001
        logger.exception("[backup] failed to write %s", target.name)
        return
    logger.info("[backup] wrote %s", target.name)

    # Prune backups older than retention window.
    cutoff = today - timedelta(days=BACKUP_RETENTION_DAYS)
    removed = 0
    for path in BACKUP_DIR.glob("events_*.db"):
        try:
            stem_date = date.fromisoformat(path.stem.removeprefix("events_"))
        except ValueError:
            continue
        if stem_date < cutoff:
            try:
                path.unlink()
                removed += 1
            except OSError:
                logger.exception("[backup] could not delete stale %s", path)
    if removed:
        logger.info("[backup] pruned %d snapshot(s) older than %dd", removed, BACKUP_RETENTION_DAYS)


# --- helpers ----------------------------------------------------------------

async def _dm_user(bot: discord.Client, message: str) -> None:
    """DM the configured user. No-op if bot isn't ready or user_id missing."""
    user_id = config.DISCORD_USER_ID
    if not user_id:
        logger.warning("[dm] DISCORD_USER_ID not set; would have sent: %s", message)
        return
    if not bot.is_ready():
        logger.warning("[dm] bot not ready; skipping: %s", message)
        return
    try:
        uid = int(user_id)
    except ValueError:
        logger.warning("[dm] DISCORD_USER_ID=%r is not an integer", user_id)
        return
    try:
        user = await bot.fetch_user(uid)
        await user.send(message)
    except Exception:  # noqa: BLE001
        logger.exception("[dm] failed to send to %s", uid)
