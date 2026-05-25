"""Reminder scheduling — schedules and fires per-event DM reminders.

Design contract:

- ``schedule_for(event, bot, scheduler)``: adds one APScheduler ``DateTrigger``
  job per ``event.remind_before`` lead minute. Job ID is
  ``f"reminder:{event.id}:{lead_min}"`` so we can find and wipe them in bulk.
  Past times are skipped silently.
- ``reschedule_all(bot, scheduler)``: removes every job with the reminder
  prefix and rebuilds from current DB state. Called on boot and after every
  ``sync_job``. APScheduler jobs do NOT survive process restart — this is
  the resilience mechanism.
- ``send_reminder(event_id, lead_min, bot)``: APScheduler callback. Re-loads
  the event from DB (so it picks up edits), checks ``reminders_sent`` for
  idempotency, sends the DM, then marks sent. Safe to fire concurrently
  with ``reschedule_all``.

Idempotency: ``reminders_sent`` (event_id, lead_min) row is the source of
truth. A reminder will never DM twice for the same lead, even if the bot
restarts mid-fire and the new ``reschedule_all`` re-adds the same job.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import discord
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from src import config, db
from src.parser import Event


logger = logging.getLogger(__name__)


# How far ahead to look for events when rescheduling. Anything further
# out won't have a reminder job scheduled now — the next reschedule_all
# (after sync_job, every 3 h) will pick them up as they enter the window.
REMINDER_LOOKAHEAD_DAYS = 60

# All reminder jobs share this prefix so we can find + wipe them.
JOB_ID_PREFIX = "reminder:"

# Past-event slack — events that started >5 min ago don't get reminders
# even if a job fires (e.g. due to clock skew or scheduler lag).
PAST_EVENT_SLACK = timedelta(minutes=5)

_TYPE_ICONS: dict[str, str] = {
    "class": "📚",
    "teaching": "👨‍🏫",
    "assignment_deadline": "📝",
    "correction_deadline": "✍️",
    "meeting": "💼",
    "other": "📌",
}


def _job_id(event_id: str, lead_min: int) -> str:
    return f"{JOB_ID_PREFIX}{event_id}:{lead_min}"


def schedule_for(event: Event, bot: Any, scheduler: AsyncIOScheduler) -> int:
    """Schedule one reminder job per lead minute. Returns count scheduled.

    Skips: negative leads, past fire times.
    Uses ``replace_existing=True`` so calling this twice for the same
    event safely overwrites instead of duplicating.
    """
    now = datetime.now(tz=config.TZ)
    scheduled = 0
    for lead_min in event.remind_before:
        if lead_min < 0:
            continue
        fire_time = event.start - timedelta(minutes=lead_min)
        if fire_time <= now:
            # Past — would either fire immediately or miss entirely.
            continue
        scheduler.add_job(
            send_reminder,
            trigger=DateTrigger(run_date=fire_time, timezone=config.TIMEZONE),
            args=[event.id, lead_min, bot],
            id=_job_id(event.id, lead_min),
            name=f"reminder for {event.title[:40]} (-{lead_min}m)",
            replace_existing=True,
            misfire_grace_time=60,
        )
        scheduled += 1
    return scheduled


async def reschedule_all(bot: Any, scheduler: AsyncIOScheduler) -> dict[str, int]:
    """Wipe every reminder job and rebuild from current DB state.

    Call after every sync and on boot. Cheap (in-memory job store).
    Returns ``{"removed": n, "scheduled": m, "events": k}`` for logging.
    """
    # 1. Wipe existing reminder jobs.
    removed = 0
    for job in list(scheduler.get_jobs()):
        if not job.id.startswith(JOB_ID_PREFIX):
            continue
        try:
            scheduler.remove_job(job.id)
            removed += 1
        except JobLookupError:
            # Job just fired or was removed concurrently; that's fine.
            pass

    # 2. Pull upcoming events from DB.
    now = datetime.now(tz=config.TZ)
    horizon = now + timedelta(days=REMINDER_LOOKAHEAD_DAYS)
    events = db.list_events(start=now, end=horizon)

    # 3. Schedule reminders for each.
    scheduled = 0
    for event in events:
        scheduled += schedule_for(event, bot, scheduler)

    logger.info(
        "[reminders] reschedule_all: removed=%d, scheduled=%d (from %d upcoming events)",
        removed,
        scheduled,
        len(events),
    )
    return {"removed": removed, "scheduled": scheduled, "events": len(events)}


async def send_reminder(event_id: str, lead_min: int, bot: Any) -> None:
    """APScheduler callback — DM the user about a single event.

    Safe to call when:
    - event was deleted (loads None, logs, returns)
    - event was already reminded (idempotency check, returns)
    - bot is not yet ready (logs, returns — APScheduler won't retry)
    - DISCORD_USER_ID unset (logs, returns)
    """
    # 1. Reload event (it may have changed since the job was scheduled).
    try:
        event = db.get_event(event_id)
    except Exception:
        logger.exception("[reminders] db.get_event(%s) failed", event_id)
        return
    if event is None:
        logger.info("[reminders] event %s no longer exists; skipping", event_id)
        return

    # 2. Past-event guard (clock skew, scheduler lag).
    now = datetime.now(tz=config.TZ)
    if event.start + PAST_EVENT_SLACK < now:
        logger.info(
            "[reminders] event %s start %s is in the past; skipping",
            event_id,
            event.start.isoformat(),
        )
        return

    # 3. Idempotency — already sent for this (event_id, lead_min)?
    try:
        if db.was_reminded(event_id, lead_min):
            logger.info(
                "[reminders] already sent for %s/%dm; skipping",
                event_id,
                lead_min,
            )
            return
    except Exception:
        logger.exception("[reminders] was_reminded check failed; will attempt send")

    # 4. Bot must be ready to fetch user + send.
    if not bot.is_ready():
        logger.warning(
            "[reminders] bot not ready; skipping send for %s/%dm "
            "(will be re-scheduled by next reschedule_all if still in window)",
            event_id,
            lead_min,
        )
        return

    user_id = config.DISCORD_USER_ID
    if not user_id:
        logger.warning("[reminders] DISCORD_USER_ID not set; cannot DM")
        return
    try:
        uid = int(user_id)
    except ValueError:
        logger.warning("[reminders] DISCORD_USER_ID=%r is not an integer", user_id)
        return

    # 5. Build + send.
    embed = _build_reminder_embed(event, lead_min, now)
    try:
        user = await bot.fetch_user(uid)
        await user.send(embed=embed)
    except Exception:
        logger.exception("[reminders] DM failed for %s/%dm", event_id, lead_min)
        return

    # 6. Mark sent (idempotency for next restart).
    try:
        db.mark_reminded(event_id, lead_min)
    except Exception:
        logger.exception("[reminders] mark_reminded failed for %s/%dm", event_id, lead_min)

    logger.info("[reminders] sent %s/%dm: %s", event_id, lead_min, event.title)


def _humanize_lead(lead_min: int) -> str:
    if lead_min <= 0:
        return "now"
    if lead_min < 60:
        return f"in {lead_min} min"
    if lead_min == 60:
        return "in 1 hour"
    if lead_min < 1440:
        hours, mins = divmod(lead_min, 60)
        return f"in {hours}h{mins:02d}m" if mins else f"in {hours} hours"
    if lead_min == 1440:
        return "in 24 hours"
    days, rem_min = divmod(lead_min, 1440)
    hours = rem_min // 60
    if days == 1 and hours == 0:
        return "in 1 day"
    if hours == 0:
        return f"in {days} days"
    return f"in {days}d {hours}h"


def _build_reminder_embed(event: Event, lead_min: int, now: datetime) -> discord.Embed:
    icon = _TYPE_ICONS.get(event.type, "•")
    when_label = _humanize_lead(lead_min)
    title = discord.utils.escape_markdown(event.title)

    embed = discord.Embed(
        title=f"⏰ {when_label} · {icon} {title}",
        color=discord.Color.gold(),
        timestamp=event.start,
    )

    when_value = f"{event.start:%a %d %b} · {event.start:%H:%M}"
    if event.end is not None:
        when_value += f" → {event.end:%H:%M}"
    embed.add_field(name="When", value=when_value, inline=False)

    if event.location:
        embed.add_field(
            name="Where",
            value=discord.utils.escape_markdown(event.location),
            inline=True,
        )
    if event.link:
        embed.add_field(name="Link", value=event.link, inline=True)
    if event.notes:
        embed.add_field(
            name="Notes",
            value=discord.utils.escape_markdown(event.notes),
            inline=False,
        )

    embed.set_footer(text=f"{event.type} · {event.source} · id {event.id}")
    return embed
