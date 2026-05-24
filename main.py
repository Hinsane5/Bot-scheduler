"""Application entry point — boots the Discord bot."""

from __future__ import annotations

import logging
import sys

from src import config
from src.bot import create_bot


def configure_logging() -> None:
    level_name = (config.LOG_LEVEL or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # discord.py is chatty at INFO — pin it to WARNING unless user opted DEBUG.
    if level > logging.DEBUG:
        logging.getLogger("discord").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    log = logging.getLogger(__name__)

    if not config.DISCORD_TOKEN:
        log.error("DISCORD_TOKEN is not set in .env — cannot start the bot.")
        raise SystemExit(1)

    if not config.DISCORD_GUILD_ID:
        log.warning(
            "DISCORD_GUILD_ID is not set; slash commands will sync globally "
            "and can take ~1h to appear. Set DISCORD_GUILD_ID to your test "
            "server's ID for instant updates during development."
        )

    bot = create_bot()
    log.info("Starting Discord bot…")
    # log_handler=None: we already configured logging above.
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
