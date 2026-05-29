"""Discord bot — slash command surface for the bot timetable."""

from __future__ import annotations

import logging
import hashlib
from datetime import date, datetime, time, timedelta
from typing import Iterable

import discord
from discord import app_commands
from discord.app_commands import AppCommandContext, AppInstallationType

from src import config, db, reminders
from src.parser import Event


logger = logging.getLogger(__name__)


# --- presentation helpers ---------------------------------------------------

TYPE_ICONS: dict[str, str] = {
    "class": "📚",
    "teaching": "👨‍🏫",
    "assignment_deadline": "📝",
    "correction_deadline": "✍️",
    "meeting": "💼",
    "other": "📌",
}

TYPE_CHOICES: list[app_commands.Choice[str]] = [
    app_commands.Choice(name="📚 Class", value="class"),
    app_commands.Choice(name="👨‍🏫 Teaching", value="teaching"),
    app_commands.Choice(name="📝 Assignment deadline", value="assignment_deadline"),
    app_commands.Choice(name="✍️ Correction deadline", value="correction_deadline"),
    app_commands.Choice(name="💼 Meeting", value="meeting"),
    app_commands.Choice(name="📌 Other", value="other"),
]

MANUAL_TYPE_CHOICES: list[app_commands.Choice[str]] = [
    app_commands.Choice(name="💼 Meeting", value="meeting"),
    app_commands.Choice(name="📌 Other", value="other"),
]

EMBED_COLOR = discord.Color.blurple()

# Discord limits — be conservative.
EMBED_DESCRIPTION_MAX = 4000
EMBED_FIELD_VALUE_MAX = 1000
LIST_PAGE_SIZE = 6
LIST_MAX_EVENTS = 180


def _fmt_day(d: date) -> str:
    """Cross-platform short day format: '25 May'."""
    return f"{d.day} {d:%b}"


def _fmt_full_date(d: date) -> str:
    """Cross-platform full date: 'Mon, 25 May 2026'."""
    return f"{d:%a}, {d.day} {d:%b %Y}"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def format_event_line(event: Event) -> str:
    """Render an event as one Markdown line for an embed.

    Manual events get their ID appended so the user can copy it into
    ``/edit`` or ``/delete``. Auto-synced events don't show an ID because
    they can't be edited/deleted from Discord (only via the portal).
    """
    icon = TYPE_ICONS.get(event.type, "•")
    time_str = event.start.strftime("%H:%M")
    if event.end is not None:
        time_str += f"–{event.end.strftime('%H:%M')}"

    title = discord.utils.escape_markdown(event.title)
    parts = [f"{icon} `{time_str}` **{title}**"]
    if event.location:
        parts.append(f"· {discord.utils.escape_markdown(event.location)}")
    if event.link:
        parts.append(f"· [link]({event.link})")
    if event.source == "manual":
        parts.append(f"· `{event.id}`")
    return " ".join(parts)


def _events_on_day(events: Iterable[Event], target: date) -> list[Event]:
    return [e for e in events if e.start.date() == target]


def parse_local_datetime(value: str, field_name: str = "time") -> datetime:
    """Parse a user-supplied local datetime in ``YYYY-MM-DD HH:MM`` format."""
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError(f"{field_name} must use `YYYY-MM-DD HH:MM`.") from exc
    return parsed.replace(tzinfo=config.TZ)


def parse_reminder_minutes(value: str | None, default: list[int]) -> list[int]:
    if value is None or not value.strip():
        return list(default)
    reminders_out: list[int] = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            minutes = int(raw)
        except ValueError as exc:
            raise ValueError("remind_before must be comma-separated minutes, e.g. `10` or `1440,60`.") from exc
        if minutes < 0:
            raise ValueError("remind_before values must be zero or greater.")
        reminders_out.append(minutes)
    return reminders_out or list(default)


def make_manual_event_id(title: str, start: datetime) -> str:
    raw = f"manual|{title}|{start.isoformat()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def build_manual_event(
    *,
    type_value: str,
    title: str,
    start_text: str,
    end_text: str | None = None,
    location: str | None = None,
    link: str | None = None,
    notes: str | None = None,
    remind_before: str | None = None,
    event_id: str | None = None,
) -> Event:
    if type_value not in {"meeting", "other"}:
        raise ValueError("Manual events can only be `meeting` or `other`.")
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("title cannot be empty.")

    start = parse_local_datetime(start_text, "start")
    end = parse_local_datetime(end_text, "end") if end_text and end_text.strip() else None
    if end is not None and end <= start:
        raise ValueError("end must be after start.")

    default_reminders = config.DEFAULT_REMINDERS_BY_TYPE[type_value]
    return Event(
        id=event_id or make_manual_event_id(clean_title, start),
        source="manual",
        type=type_value,  # type: ignore[arg-type]
        title=clean_title,
        start=start,
        end=end,
        location=_clean_optional(location),
        link=_clean_optional(link),
        notes=_clean_optional(notes),
        remind_before=parse_reminder_minutes(remind_before, default_reminders),
    )


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _event_to_embed(event: Event, title: str) -> discord.Embed:
    embed = discord.Embed(title=title, color=EMBED_COLOR)
    embed.description = format_event_line(event)
    embed.add_field(name="ID", value=f"`{event.id}`", inline=True)
    embed.add_field(name="Type", value=event.type, inline=True)
    embed.add_field(name="Source", value=event.source, inline=True)
    embed.add_field(name="Reminders", value=", ".join(str(m) for m in event.remind_before) or "none", inline=False)
    if event.notes:
        embed.add_field(name="Notes", value=_truncate(discord.utils.escape_markdown(event.notes), EMBED_FIELD_VALUE_MAX), inline=False)
    return embed


async def schedule_manual_event(event: Event, interaction: discord.Interaction) -> None:
    scheduler = getattr(interaction.client, "scheduler", None)
    if scheduler is None:
        return
    reminders.schedule_for(event, interaction.client, scheduler)


async def reschedule_manual_events(interaction: discord.Interaction) -> None:
    scheduler = getattr(interaction.client, "scheduler", None)
    if scheduler is None:
        return
    await reminders.reschedule_all(interaction.client, scheduler)


class DeleteConfirmView(discord.ui.View):
    def __init__(self, event: Event) -> None:
        super().__init__(timeout=30)
        self.event = event

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        deleted = db.delete_event(self.event.id, manual_only=True)
        if deleted:
            await reschedule_manual_events(interaction)
            embed = _event_to_embed(self.event, "Deleted manual event")
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.edit_message(
                content="That manual event no longer exists.",
                embed=None,
                view=None,
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Delete cancelled.", embed=None, view=None)


class EditEventModal(discord.ui.Modal):
    def __init__(self, event: Event) -> None:
        super().__init__(title=f"Edit {event.id}")
        self.event = event

        self.title_input = discord.ui.TextInput(
            label="Title",
            default=event.title,
            max_length=200,
        )
        self.start_input = discord.ui.TextInput(
            label="Start (YYYY-MM-DD HH:MM)",
            default=event.start.strftime("%Y-%m-%d %H:%M"),
            max_length=16,
        )
        self.end_input = discord.ui.TextInput(
            label="End (YYYY-MM-DD HH:MM, optional)",
            default=event.end.strftime("%Y-%m-%d %H:%M") if event.end else "",
            required=False,
            max_length=16,
        )
        self.location_input = discord.ui.TextInput(
            label="Location/link/notes",
            default=self._packed_optional_fields(event),
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=600,
        )
        self.reminders_input = discord.ui.TextInput(
            label="Reminder minutes",
            default=",".join(str(m) for m in event.remind_before),
            required=False,
            max_length=80,
        )

        self.add_item(self.title_input)
        self.add_item(self.start_input)
        self.add_item(self.end_input)
        self.add_item(self.location_input)
        self.add_item(self.reminders_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            location, link, notes = self._unpack_optional_fields(str(self.location_input.value))
            updated = build_manual_event(
                type_value=self.event.type,
                title=str(self.title_input.value),
                start_text=str(self.start_input.value),
                end_text=str(self.end_input.value),
                location=location,
                link=link,
                notes=notes,
                remind_before=str(self.reminders_input.value),
                event_id=self.event.id,
            )
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

        _, updated_count = db.upsert_events([updated])
        await reschedule_manual_events(interaction)
        embed_title = "Updated manual event" if updated_count else "Manual event unchanged"
        await interaction.response.send_message(
            embed=_event_to_embed(updated, embed_title),
            ephemeral=True,
        )

    @staticmethod
    def _packed_optional_fields(event: Event) -> str:
        lines = []
        if event.location:
            lines.append(f"location: {event.location}")
        if event.link:
            lines.append(f"link: {event.link}")
        if event.notes:
            lines.append(f"notes: {event.notes}")
        return "\n".join(lines)

    @staticmethod
    def _unpack_optional_fields(value: str) -> tuple[str | None, str | None, str | None]:
        location: str | None = None
        link: str | None = None
        notes_lines: list[str] = []
        for line in value.splitlines():
            stripped = line.strip()
            lowered = stripped.lower()
            if lowered.startswith("location:"):
                location = stripped.split(":", 1)[1].strip() or None
            elif lowered.startswith("link:"):
                link = stripped.split(":", 1)[1].strip() or None
            elif lowered.startswith("notes:"):
                notes_lines.append(stripped.split(":", 1)[1].strip())
            elif stripped:
                notes_lines.append(stripped)
        notes = "\n".join(line for line in notes_lines if line).strip() or None
        return location, link, notes


class AddEventModal(discord.ui.Modal):
    """Form for /add — mirrors EditEventModal but for a brand-new event.

    Discord has no native calendar/date picker, so the Start/End fields are
    plain text inputs in ``YYYY-MM-DD HH:MM`` format. Start is pre-filled with
    the next round hour so the user usually only has to tweak it, and Reminder
    minutes default to the type's standard ramp (2h, 1h, 30m, 10m).
    """

    def __init__(self, type_value: str) -> None:
        super().__init__(title="Add manual event")
        self.type_value = type_value

        now = datetime.now(tz=config.TZ)
        default_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        default_reminders = config.DEFAULT_REMINDERS_BY_TYPE.get(type_value, [10])

        self.title_input = discord.ui.TextInput(
            label="Title",
            placeholder="e.g. Project sync with John",
            max_length=200,
        )
        self.start_input = discord.ui.TextInput(
            label="Start (YYYY-MM-DD HH:MM)",
            default=default_start.strftime("%Y-%m-%d %H:%M"),
            max_length=16,
        )
        self.end_input = discord.ui.TextInput(
            label="End (YYYY-MM-DD HH:MM, optional)",
            required=False,
            max_length=16,
        )
        self.details_input = discord.ui.TextInput(
            label="Location/link/notes",
            placeholder="location: Room 301\nlink: https://...\nnotes: bring laptop",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=600,
        )
        self.reminders_input = discord.ui.TextInput(
            label="Reminder minutes",
            default=",".join(str(m) for m in default_reminders),
            required=False,
            max_length=80,
        )

        self.add_item(self.title_input)
        self.add_item(self.start_input)
        self.add_item(self.end_input)
        self.add_item(self.details_input)
        self.add_item(self.reminders_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            location, link, notes = EditEventModal._unpack_optional_fields(str(self.details_input.value))
            event = build_manual_event(
                type_value=self.type_value,
                title=str(self.title_input.value),
                start_text=str(self.start_input.value),
                end_text=str(self.end_input.value),
                location=location,
                link=link,
                notes=notes,
                remind_before=str(self.reminders_input.value),
            )
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

        inserted, updated = db.upsert_events([event])
        await schedule_manual_event(event, interaction)
        if inserted:
            embed_title = "Saved manual event"
        elif updated:
            embed_title = "Updated existing manual event"
        else:
            embed_title = "Manual event already exists"
        await interaction.response.send_message(
            embed=_event_to_embed(event, embed_title),
            ephemeral=True,
        )


# --- embed builders ---------------------------------------------------------

def build_today_embed(now: datetime | None = None) -> discord.Embed:
    if now is None:
        now = datetime.now(tz=config.TZ)
    today = now.date()
    start = datetime.combine(today, time.min, tzinfo=config.TZ)
    end = datetime.combine(today, time.max, tzinfo=config.TZ)
    events = db.list_events(start=start, end=end)

    title_base = f"📅 Today · {_fmt_full_date(today)}"
    if not events:
        return discord.Embed(
            title=title_base,
            description="_Nothing scheduled for today._",
            color=EMBED_COLOR,
        )

    description = "\n".join(format_event_line(e) for e in events)
    return discord.Embed(
        title=f"{title_base} · {len(events)}",
        description=_truncate(description, EMBED_DESCRIPTION_MAX),
        color=EMBED_COLOR,
    )


def build_week_embed(now: datetime | None = None) -> discord.Embed:
    if now is None:
        now = datetime.now(tz=config.TZ)
    today = now.date()
    end_date = today + timedelta(days=6)
    start_dt = datetime.combine(today, time.min, tzinfo=config.TZ)
    end_dt = datetime.combine(end_date, time.max, tzinfo=config.TZ)
    events = db.list_events(start=start_dt, end=end_dt)

    title_base = f"🗓️ Week · {_fmt_day(today)} → {_fmt_day(end_date)}"
    if not events:
        return discord.Embed(
            title=title_base,
            description="_Nothing scheduled this week._",
            color=EMBED_COLOR,
        )

    embed = discord.Embed(
        title=f"{title_base} · {len(events)}",
        color=EMBED_COLOR,
    )
    for offset in range(7):
        day = today + timedelta(days=offset)
        day_events = _events_on_day(events, day)
        if not day_events:
            continue
        field_value = "\n".join(format_event_line(e) for e in day_events)
        embed.add_field(
            name=_fmt_full_date(day),
            value=_truncate(field_value, EMBED_FIELD_VALUE_MAX),
            inline=False,
        )
    return embed


def build_status_embed(now: datetime | None = None) -> discord.Embed:
    """Health dashboard: scrapers, sessions, upcoming counts."""
    if now is None:
        now = datetime.now(tz=config.TZ)

    embed = discord.Embed(title="🩺 Bot Status", color=EMBED_COLOR)

    # Scraper sync status
    sync_lines: list[str] = []
    for name in config.ENABLED_SCRAPERS:
        last = db.last_sync(name)
        if last is None:
            sync_lines.append(f"⏳ **{name}**: never run")
            continue
        ran_at = _parse_iso(last["ran_at"])
        ago = _humanize_past(now - ran_at) if ran_at else "?"
        if last["success"]:
            sync_lines.append(
                f"✅ **{name}**: {ago} ago "
                f"(+{last.get('inserted') or 0} new, ~{last.get('updated') or 0} upd)"
            )
        else:
            err = (last.get("error") or "").splitlines()[0][:120]
            sync_lines.append(f"❌ **{name}**: failed {ago} ago — `{err}`")
    embed.add_field(
        name="📡 Scrapers",
        value="\n".join(sync_lines) or "_None enabled._",
        inline=False,
    )

    # Session/refresh status per enabled portal
    refresh_lines: list[str] = []
    for portal in config.ENABLED_SCRAPERS:
        last = db.last_refresh(portal)
        if last is None:
            refresh_lines.append(f"⏳ **{portal}**: never refreshed")
            continue
        ran_at = _parse_iso(last["ran_at"])
        ago = _humanize_past(now - ran_at) if ran_at else "?"
        if last["success"]:
            ttl_suffix = ""
            exp_str = last.get("new_token_exp")
            if exp_str:
                exp_dt = _parse_iso(exp_str)
                if exp_dt is not None:
                    ttl = exp_dt - now
                    if ttl.total_seconds() > 0:
                        ttl_suffix = f" · TTL {_humanize_past(ttl)}"
                    else:
                        ttl_suffix = " · ⚠️ TTL expired"
            refresh_lines.append(f"✅ **{portal}**: {ago} ago{ttl_suffix}")
        else:
            err = (last.get("error") or "").splitlines()[0][:120]
            refresh_lines.append(f"❌ **{portal}**: failed {ago} ago — `{err}`")
    embed.add_field(
        name="🔑 Sessions",
        value="\n".join(refresh_lines) or "_N/A._",
        inline=False,
    )

    # Upcoming event counts
    end_24h = now + timedelta(hours=24)
    end_7d = now + timedelta(days=7)
    count_24h = len(db.list_events(start=now, end=end_24h))
    count_7d = len(db.list_events(start=now, end=end_7d))
    embed.add_field(
        name="📅 Upcoming",
        value=f"Next 24h: **{count_24h}** · Next 7d: **{count_7d}**",
        inline=False,
    )

    embed.set_footer(
        text=f"Sync every {config.SYNC_INTERVAL_MIN} min · {config.TIMEZONE}"
    )
    return embed


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=config.TZ)
    return parsed


def _humanize_past(delta: timedelta) -> str:
    total = int(abs(delta.total_seconds()))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m"
    if total < 86400:
        hours, rem = divmod(total, 3600)
        minutes = rem // 60
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    days, rem = divmod(total, 86400)
    hours = rem // 3600
    return f"{days}d{hours:02d}h" if hours else f"{days}d"


def build_upcoming_embed(
    count: int,
    event_type: str | None,
    days_window: int | None = None,
    now: datetime | None = None,
) -> discord.Embed:
    """Build the /upcoming embed.

    Filters:
    - ``event_type``: optional type filter (class, teaching, etc.)
    - ``days_window``: optional integer N — restrict to events in the next
      N days from now. ``None`` means no upper bound.
    - ``count``: hard cap on number of events returned.
    """
    if now is None:
        now = datetime.now(tz=config.TZ)

    end_dt: datetime | None = None
    if days_window is not None:
        end_dt = now + timedelta(days=days_window)

    events = db.list_events(
        start=now,
        end=end_dt,
        type=event_type,
        limit=count,
    )

    # Compose title — append each active filter as " · <label>".
    parts: list[str] = []
    if event_type:
        icon = TYPE_ICONS.get(event_type, "")
        parts.append(f"{icon} {event_type}")
    if days_window is not None:
        parts.append("next 1 day" if days_window == 1 else f"next {days_window} days")
    filter_label = " · ".join(parts)
    title_base = "⏭️ Upcoming"
    if filter_label:
        title_base = f"{title_base} · {filter_label}"

    if not events:
        empty_msg = (
            "_Nothing upcoming in this window._"
            if days_window is not None
            else "_Nothing upcoming in your schedule._"
        )
        return discord.Embed(
            title=title_base,
            description=empty_msg,
            color=EMBED_COLOR,
        )

    lines: list[str] = []
    for event in events:
        date_label = _fmt_day(event.start.date())
        lines.append(f"`{date_label}` {format_event_line(event)}")
    description = "\n".join(lines)
    return discord.Embed(
        title=f"{title_base} · {len(events)}",
        description=_truncate(description, EMBED_DESCRIPTION_MAX),
        color=EMBED_COLOR,
    )


def list_filter_label(event_type: str | None, days_window: int | None) -> str:
    parts: list[str] = []
    if event_type:
        icon = TYPE_ICONS.get(event_type, "")
        parts.append(f"{icon} {event_type}")
    if days_window is not None:
        parts.append("next 1 day" if days_window == 1 else f"next {days_window} days")
    return " · ".join(parts)


def get_list_events(
    event_type: str | None = None,
    days_window: int | None = None,
    now: datetime | None = None,
) -> list[Event]:
    if now is None:
        now = datetime.now(tz=config.TZ)
    end_dt = now + timedelta(days=days_window) if days_window is not None else None
    # Filter by end time, not start — an event that began but hasn't ended
    # should still appear in the schedule list.
    return db.list_events(
        end_after=now,
        end=end_dt,
        type=event_type,
        limit=LIST_MAX_EVENTS,
    )


def build_list_embed(
    events: list[Event],
    page: int,
    *,
    event_type: str | None = None,
    days_window: int | None = None,
    now: datetime | None = None,
) -> discord.Embed:
    """Build one page of the paginated /list UI."""
    if now is None:
        now = datetime.now(tz=config.TZ)

    total_pages = max(1, (len(events) + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    filter_label = list_filter_label(event_type, days_window)
    title = "Schedule"
    if filter_label:
        title = f"{title} · {filter_label}"

    if not events:
        embed = discord.Embed(
            title=title,
            description="_Nothing upcoming in this window._",
            color=EMBED_COLOR,
            timestamp=now,
        )
        embed.set_footer(text="Page 1/1 · 0 events")
        return embed

    start_idx = page * LIST_PAGE_SIZE
    page_events = events[start_idx : start_idx + LIST_PAGE_SIZE]
    embed = discord.Embed(
        title=f"{title} · {len(events)} events",
        color=EMBED_COLOR,
        timestamp=now,
    )
    for event in page_events:
        icon = TYPE_ICONS.get(event.type, "•")
        date_label = _fmt_full_date(event.start.date())
        time_label = event.start.strftime("%H:%M")
        if event.end is not None:
            time_label += f" - {event.end.strftime('%H:%M')}"
        detail_parts = [f"`{time_label}`"]
        if event.location:
            detail_parts.append(discord.utils.escape_markdown(event.location))
        if event.link:
            detail_parts.append(f"[link]({event.link})")
        if event.source == "manual":
            detail_parts.append(f"id `{event.id}`")
        detail_parts.append(event.source)
        embed.add_field(
            name=_truncate(f"{icon} {date_label} · {event.title}", 256),
            value=_truncate(" · ".join(detail_parts), EMBED_FIELD_VALUE_MAX),
            inline=False,
        )

    first_item = start_idx + 1
    last_item = start_idx + len(page_events)
    embed.set_footer(
        text=(
            f"Page {page + 1}/{total_pages} · Showing {first_item}-{last_item} "
            f"of {len(events)} · {config.TIMEZONE}"
        )
    )
    return embed


class ScheduleListView(discord.ui.View):
    def __init__(
        self,
        events: list[Event],
        *,
        event_type: str | None,
        days_window: int | None,
        owner_id: int,
    ) -> None:
        super().__init__(timeout=180)
        self.events = events
        self.event_type = event_type
        self.days_window = days_window
        self.owner_id = owner_id
        self.page = 0
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This schedule page belongs to another interaction.", ephemeral=True)
        return False

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.page = min(self.total_pages - 1, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.events) + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)

    def embed(self) -> discord.Embed:
        return build_list_embed(
            self.events,
            self.page,
            event_type=self.event_type,
            days_window=self.days_window,
        )

    def _sync_buttons(self) -> None:
        self.prev_page.disabled = self.page <= 0
        self.next_page.disabled = self.page >= self.total_pages - 1


async def send_schedule_list(interaction: discord.Interaction) -> None:
    """Send the paginated schedule UI with a channel fallback.

    Discord invalidates slash interaction tokens quickly. If the bot was
    delayed after being idle, even the initial ``defer`` can fail with
    ``Unknown interaction``. In that case we still send the page as a normal
    channel message so the command remains useful instead of crashing.
    """
    try:
        await interaction.response.defer()
        deferred = True
    except discord.NotFound:
        deferred = False

    events = get_list_events()
    view = ScheduleListView(
        events,
        event_type=None,
        days_window=None,
        owner_id=interaction.user.id,
    )
    embed = view.embed()

    if deferred:
        try:
            await interaction.followup.send(embed=embed, view=view)
            return
        except discord.NotFound:
            logger.warning("list-schedule followup expired; falling back to channel send")

    channel = interaction.channel
    if channel is None:
        logger.warning("list-schedule interaction expired and no channel is available")
        return
    try:
        await channel.send(embed=embed, view=view)  # type: ignore[union-attr]
    except discord.HTTPException:
        logger.exception("list-schedule channel fallback failed")


# --- bot --------------------------------------------------------------------

class TimetableBot(discord.Client):
    """Slash-command-only Discord bot."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.dm_messages = True
        super().__init__(intents=intents)
        # Tree-wide defaults so every command is usable in DMs and the bot
        # can optionally be installed as a user app.
        self.tree = app_commands.CommandTree(
            self,
            allowed_contexts=AppCommandContext(
                guild=True, dm_channel=True, private_channel=True
            ),
            allowed_installs=AppInstallationType(guild=True, user=True),
        )
        # Gate so initial on_ready (during boot) doesn't double-trigger the
        # reminder reschedule that _initial_reminder_setup already runs.
        self._first_ready = True
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(name="today", description="Show today's scheduled events")
        async def today_cmd(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(embed=build_today_embed())

        @self.tree.command(name="week", description="Show the next 7 days of events")
        async def week_cmd(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(embed=build_week_embed())

        @self.tree.command(name="status", description="Show bot health and sync status")
        async def status_cmd(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(embed=build_status_embed())

        @self.tree.command(
            name="upcoming",
            description="Show the next N events, optionally filtered by type and time window",
        )
        @app_commands.describe(
            count="How many events to show (1–50). Default: 5 alone, up to 50 when 'days' is set.",
            type="Filter by event type",
            days="Only events within the next N days (1=next 24h, 7=one week, …). Omit for no window limit.",
        )
        @app_commands.choices(type=TYPE_CHOICES)
        async def upcoming_cmd(
            interaction: discord.Interaction,
            count: app_commands.Range[int, 1, 50] | None = None,
            type: app_commands.Choice[str] | None = None,
            days: app_commands.Range[int, 1, 60] | None = None,
        ) -> None:
            type_value = type.value if type else None
            # Smart default: explicit count wins. Otherwise:
            #   - With `days` set, user is asking "what's in this window" — show up to 50.
            #   - Without `days`, default to next 5 events as before.
            effective_count = count if count is not None else (50 if days is not None else 5)
            await interaction.response.send_message(
                embed=build_upcoming_embed(effective_count, type_value, days_window=days)
            )

        @self.tree.command(name="list-schedule", description="Browse upcoming schedule pages with Prev/Next buttons")
        async def list_schedule_cmd(interaction: discord.Interaction) -> None:
            await send_schedule_list(interaction)

        @self.tree.command(name="add", description="Add a manual meeting or other event (opens a form)")
        @app_commands.describe(type="Manual event type — the rest is filled in on the form")
        @app_commands.choices(type=MANUAL_TYPE_CHOICES)
        async def add_cmd(
            interaction: discord.Interaction,
            type: app_commands.Choice[str],
        ) -> None:
            await interaction.response.send_modal(AddEventModal(type.value))

        @self.tree.command(name="delete", description="Delete a manually-added event")
        @app_commands.describe(id="Manual event ID or unique ID prefix")
        async def delete_cmd(interaction: discord.Interaction, id: str) -> None:
            event = db.find_event_by_prefix(id, manual_only=True)
            if event is None:
                await interaction.response.send_message(
                    "No unique manual event matched that ID.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                embed=_event_to_embed(event, "Delete this manual event?"),
                view=DeleteConfirmView(event),
                ephemeral=True,
            )

        @self.tree.command(name="edit", description="Edit a manually-added event")
        @app_commands.describe(id="Manual event ID or unique ID prefix")
        async def edit_cmd(interaction: discord.Interaction, id: str) -> None:
            event = db.find_event_by_prefix(id, manual_only=True)
            if event is None:
                await interaction.response.send_message(
                    "No unique manual event matched that ID.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(EditEventModal(event))

        @self.tree.error
        async def on_app_error(
            interaction: discord.Interaction,
            error: app_commands.AppCommandError,
        ) -> None:
            logger.exception("Slash command error", exc_info=error)

            # CommandNotFound = Discord routed an unknown command to us
            # (stale client cache, bot restart mid-flight, etc.). Discord
            # already shows the user a generic "interaction failed", so
            # don't try to send our own message — it'll race against the
            # original interaction's auto-ack and 40060.
            if isinstance(error, app_commands.CommandNotFound):
                return

            message = f"⚠️ Error: `{error.__class__.__name__}`"
            # Wrap everything: by the time we run, the interaction may
            # have been auto-acknowledged by Discord (3s timeout) OR by
            # the original handler partially responding before crashing.
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except discord.HTTPException:
                # Initial response path failed — try followup as a fallback.
                try:
                    await interaction.followup.send(message, ephemeral=True)
                except discord.HTTPException:
                    logger.warning(
                        "Could not surface error to user (interaction expired/acked)",
                        exc_info=True,
                    )

    async def setup_hook(self) -> None:
        """Sync slash commands once at startup.

        We always sync globally so commands appear in DMs (guild-only
        commands can't be used outside that guild). When ``DISCORD_GUILD_ID``
        is set we additionally sync to that guild for instant updates while
        the global copy propagates (~1h on Discord's side).
        """
        if config.DISCORD_GUILD_ID:
            try:
                guild_id = int(config.DISCORD_GUILD_ID)
            except ValueError:
                logger.warning(
                    "DISCORD_GUILD_ID=%r is not an integer; using global sync only",
                    config.DISCORD_GUILD_ID,
                )
                guild_id = None
        else:
            guild_id = None

        if guild_id is not None:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            guild_synced = await self.tree.sync(guild=guild)
            logger.info(
                "Synced %d slash command(s) to guild %s (instant)",
                len(guild_synced),
                guild_id,
            )

        global_synced = await self.tree.sync()
        logger.info(
            "Synced %d slash command(s) globally — DM-capable (~1h propagation)",
            len(global_synced),
        )

    async def on_ready(self) -> None:
        user = self.user
        if user is not None:
            logger.info("Logged in as %s (id=%s)", user, user.id)
        else:
            logger.info("Logged in.")
        if self._first_ready:
            self._first_ready = False
            return
        await self._reschedule_after_reconnect("on_ready")

    async def on_resumed(self) -> None:
        """Discord WebSocket resumed after a disconnect — recover any
        reminders that should have fired while we were away."""
        await self._reschedule_after_reconnect("on_resumed")

    async def _reschedule_after_reconnect(self, source: str) -> None:
        scheduler = getattr(self, "scheduler", None)
        if scheduler is None:
            return
        logger.info("Discord %s; rebuilding reminder schedule", source)
        try:
            await reminders.reschedule_all(self, scheduler)
        except Exception:
            logger.exception("[%s] reminder reschedule failed", source)


def create_bot() -> TimetableBot:
    return TimetableBot()
