# Bot Timetable

Personal Discord timetable bot for syncing Binus LMS and Messier schedules, sending reminders, and answering schedule questions with a local LLM.

## Phase 0 setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python -c "import discord, playwright, httpx, bs4, apscheduler, dotenv; print('ok')"
```

Copy `.env.example` to `.env` and fill in local secrets before later phases.

