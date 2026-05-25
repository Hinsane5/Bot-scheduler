"""Discord bot — slash command surface for the bot timetable."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Iterable

import discord
from discord import app_commands

from src import config, db
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
    now: datetime | None = None,
) -> discord.Embed:
    if now is None:
        now = datetime.now(tz=config.TZ)
    events = db.list_events(start=now, type=event_type, limit=count)

    icon = TYPE_ICONS.get(event_type or "", "")
    filter_label = f" · {icon} {event_type}" if event_type else ""
    title_base = f"⏭️ Upcoming{filter_label}"

    if not events:
        return discord.Embed(
            title=title_base,
            description="_Nothing upcoming in your schedule._",
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
            description="Show the next N events, optionally filtered by type",
        )
        @app_commands.describe(
            count="How many events to show (1–25, default 5)",
            type="Filter by event type",
        )
        @app_commands.choices(type=TYPE_CHOICES)
        async def upcoming_cmd(
            interaction: discord.Interaction,
            count: app_commands.Range[int, 1, 25] = 5,
            type: app_commands.Choice[str] | None = None,
        ) -> None:
            type_value = type.value if type else None
            await interaction.response.send_message(
                embed=build_upcoming_embed(count, type_value)
            )

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
