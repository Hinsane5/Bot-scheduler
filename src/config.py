"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

TIMEZONE = os.getenv("TIMEZONE", "Asia/Jakarta")
TZ = ZoneInfo(TIMEZONE)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DISCORD_USER_ID = os.getenv("DISCORD_USER_ID", "")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "")

LMS_BASE_URL = os.getenv("LMS_BASE_URL", "https://lms.binus.ac.id")
MESSIER_BASE_URL = os.getenv("MESSIER_BASE_URL", "https://socs1.binus.ac.id/messier")

SYNC_INTERVAL_MIN = int(os.getenv("SYNC_INTERVAL_MIN", "15"))
REFRESH_INTERVAL_HOURS = int(os.getenv("REFRESH_INTERVAL_HOURS", "20"))
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
    "class": [10],
    "teaching": [10],
    "meeting": [10],
    "other": [10],
    "assignment_deadline": [1440, 60],
    "correction_deadline": [1440, 60],
}
