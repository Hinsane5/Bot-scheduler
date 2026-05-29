"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv() -> None:
        return None

load_dotenv()

TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")
TZ = ZoneInfo(TIMEZONE)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID", "")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")

LMS_BASE_URL = os.getenv("LMS_BASE_URL", "https://lms.binus.ac.id")
MESSIER_BASE_URL = os.getenv("MESSIER_BASE_URL", "https://socs1.binus.ac.id/messier")

SYNC_INTERVAL_MIN = int(os.getenv("SYNC_INTERVAL_MIN", "180"))
LMS_REFRESH_INTERVAL_HOURS = int(os.getenv("LMS_REFRESH_INTERVAL_HOURS", "20"))
MESSIER_REFRESH_INTERVAL_MIN = int(os.getenv("MESSIER_REFRESH_INTERVAL_MIN", "25"))
BACKUP_HOUR_LOCAL = int(os.getenv("BACKUP_HOUR_LOCAL", "3"))
ENABLED_SCRAPERS = [
    item.strip()
    for item in os.getenv("ENABLED_SCRAPERS", "lms").split(",")
    if item.strip()
]

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:3b-instruct")
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "15"))
OLLAMA_KEEP_ALIVE_MIN = int(os.getenv("OLLAMA_KEEP_ALIVE_MIN", "4"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

DEFAULT_REMINDERS_BY_TYPE = {
    # Academic events get a four-step ramp before they start / are due.
    "class": [120, 60, 30, 10],
    "teaching": [120, 60, 30, 10],
    "assignment_deadline": [120, 60, 30, 10],
    "correction_deadline": [120, 60, 30, 10],
    # Manual / personal events get the same four-step ramp as academic
    # events — user can still override per-event in the /add or /edit form.
    "meeting": [120, 60, 30, 10],
    "other": [120, 60, 30, 10],
}
