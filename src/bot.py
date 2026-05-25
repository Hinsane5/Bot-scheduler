"""Discord bot — slash command surface for the bot timetable."""

from __future__ import annotations

import logging
import hashlib
from datetime import date, datetime, time, timedelta
from typing import Iterable

import discord
from discord import app_commands

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
    """Render an event as one Markdown line for an embed."""
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


# --- bot --------------------------------------------------------------------

class TimetableBot(discord.Client):
    """Slash-command-only Discord bot."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.dm_messages = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
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

        @self.tree.command(name="add", description="Add a manual meeting or other event")
        @app_commands.describe(
            type="Manual event type",
            title="Event title",
            start="Start time in YYYY-MM-DD HH:MM",
            end="Optional end time in YYYY-MM-DD HH:MM",
            location="Optional location",
            link="Optional URL",
            remind_before="Optional comma-separated reminder minutes, e.g. 10 or 1440,60",
            notes="Optional notes",
        )
        @app_commands.choices(type=MANUAL_TYPE_CHOICES)
        async def add_cmd(
            interaction: discord.Interaction,
            type: app_commands.Choice[str],
            title: str,
            start: str,
            end: str | None = None,
            location: str | None = None,
            link: str | None = None,
            remind_before: str | None = None,
            notes: str | None = None,
        ) -> None:
            try:
                event = build_manual_event(
                    type_value=type.value,
                    title=title,
                    start_text=start,
                    end_text=end,
                    location=location,
                    link=link,
                    notes=notes,
                    remind_before=remind_before,
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
            message = f"⚠️ Error running command: `{error.__class__.__name__}`"
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

    async def setup_hook(self) -> None:
        """Sync slash commands once at startup."""
        if config.DISCORD_GUILD_ID:
            try:
                guild_id = int(config.DISCORD_GUILD_ID)
            except ValueError:
                logger.warning(
                    "DISCORD_GUILD_ID=%r is not an integer; falling back to global sync",
                    config.DISCORD_GUILD_ID,
                )
                synced = await self.tree.sync()
                logger.info("Synced %d slash command(s) globally", len(synced))
                return
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(
                "Synced %d slash command(s) to guild %s (instant)",
                len(synced),
                guild_id,
            )
        else:
            synced = await self.tree.sync()
            logger.info(
                "Synced %d slash command(s) globally (~1h propagation)",
                len(synced),
            )

    async def on_ready(self) -> None:
        user = self.user
        if user is not None:
            logger.info("Logged in as %s (id=%s)", user, user.id)
        else:
            logger.info("Logged in.")


def create_bot() -> TimetableBot:
    return TimetableBot()
