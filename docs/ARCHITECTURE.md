# Architecture & Tech Stack

## Tech stack

| Concern              | Choice                                  | Why                                                                |
|----------------------|-----------------------------------------|--------------------------------------------------------------------|
| Language             | Python 3.11+                            | Best ecosystem for Discord + scraping + LLM glue                  |
| Bot framework        | `discord.py` 2.x                        | Mature, supports slash commands, embeds, modals, reactions        |
| Browser automation   | `playwright`                            | Handles Microsoft SSO redirects + JS-rendered pages               |
| HTTP client          | `httpx`                                 | Async, used for JSON endpoints + Ollama API                       |
| HTML parsing         | `beautifulsoup4`                        | Fallback if no JSON endpoint                                      |
| Scheduler            | `apscheduler` 3.x (`AsyncIOScheduler`) | In-process; interval + date-triggered jobs                        |
| Storage              | SQLite (stdlib `sqlite3`)               | Zero setup, single file                                           |
| Config               | `python-dotenv`                         | `.env` for credentials + LLM config                               |
| **Local LLM runtime**| **Ollama**                              | Simplest path: REST API, GGUF support, JSON-mode, ARM-friendly    |
| **Local LLM model**  | **Qwen 2.5 3B Instruct (Q4_K_M GGUF)** | ~2 GB RAM, strong Indonesian + structured output, ~10–20 tok/s on Oracle ARM |
| Hosting              | Oracle Cloud free-tier ARM VM           | 4 OCPU / 24 GB RAM, $0/month forever, always-on                   |

## System diagram

```
                              ┌──────────────┐
                              │ Discord User │ (me)
                              └──────┬───────┘
       slash commands                │                free-text DMs / /ask
       (deterministic)               │                (LLM-routed)
                                     ▼
┌────────────────────────────────────────────────────────────────────┐
│ main.py  (single Python process on Oracle ARM VM)                  │
│                                                                    │
│  ┌──────────────────┐  ┌────────────────┐  ┌──────────────────┐    │
│  │ discord.py bot   │  │ APScheduler    │  │ sync_job (15m)   │    │
│  │ /today /add ...  │  │ - reminder jobs│  │ iterates SCRAPERS│    │
│  │ /ask + free-text │  │ - sync job     │  │  ┌────────────┐  │    │
│  └────────┬─────────┘  └────────┬───────┘  │  │ LMSScraper │  │    │
│           │  free-text          │          │  │ MessierScr.│  │    │
│           ▼                     │          │  └─────┬──────┘  │    │
│  ┌──────────────────┐           │          │        │         │    │
│  │ src/llm.py       │           │          │        ▼         │    │
│  │ intent extract   │           │          │  upsert events   │    │
│  │ + summarize      │           │          └─────────┬────────┘    │
│  └────────┬─────────┘           │                    │             │
│           │ httpx POST          ▼                    ▼             │
│           ▼                  ┌──────────────────────────────┐      │
│  ┌──────────────────┐        │ db.py (SQLite: events.db)    │      │
│  │ Ollama daemon    │        └──────────────────────────────┘      │
│  │ qwen2.5:3b       │                                              │
│  │ localhost:11434  │                                              │
│  └──────────────────┘                                              │
└────────────────────────────────────────────────────────────────────┘
                                     │ httpx / Playwright
              ┌──────────────────────┴──────────────────────┐
              ▼                                             ▼
      ┌──────────────────┐                        ┌─────────────────────┐
      │ Binus LMS portal │                        │ Binus Messier (SOCS)│
      └──────────────────┘                        └─────────────────────┘
```

## Components

### `src/config.py`
Loads `.env`. Exports:
- `BINUS_USER`, `BINUS_PASS`
- `DISCORD_TOKEN`, `DISCORD_USER_ID`, `DISCORD_GUILD_ID`
- `LMS_BASE_URL`, `LMS_SCHEDULE_ENDPOINT`
- `MESSIER_BASE_URL`, `MESSIER_SCHEDULE_ENDPOINT`
- `SYNC_INTERVAL_MIN` (default 15), `TIMEZONE` (default `Asia/Jakarta`)
- `ENABLED_SCRAPERS: list[str]` (default `["lms", "messier"]`)
- **`OLLAMA_URL`** (default `http://localhost:11434`)
- **`LLM_MODEL`** (default `qwen2.5:3b-instruct`)
- **`LLM_TIMEOUT_SEC`** (default `15`)
- `DEFAULT_REMINDERS_BY_TYPE: dict[str, list[int]]`

### `src/auth.py`
Per-portal session management (unchanged from earlier docs).
CLI: `python -m src.auth --portal=lms` / `--portal=messier` / `--check`.

### `src/scrapers/`
Pluggable scrapers behind a common `Scraper` ABC (unchanged):
- `src/scrapers/base.py` — `Scraper` ABC.
- `src/scrapers/lms.py` — `LMSScraper`, returns `class` + `assignment_deadline` events.
- `src/scrapers/messier.py` — `MessierScraper`, returns `teaching` + `correction_deadline` events.
- `src/scrapers/__init__.py` — `REGISTRY = {"lms": LMSScraper(), "messier": MessierScraper()}`.

### `src/parser.py`
`Event` dataclass + `EventType`, `EventSource` literals.

### `src/db.py`
SQLite CRUD + idempotency helpers (unchanged):
`init`, `upsert_events`, `list_events`, `get_event`, `delete_event`, `mark_reminded`, `was_reminded`, `log_sync`, `last_sync`, `prune_stale`.

### `src/reminders.py`
APScheduler glue:
`schedule_for`, `reschedule_all`, `send_reminder`.

### `src/llm.py` *(new)*
Thin wrapper around Ollama's REST API for two operations.

```python
# src/llm.py
import httpx, json
from src.config import OLLAMA_URL, LLM_MODEL, LLM_TIMEOUT_SEC

INTENT_SYSTEM_PROMPT = """You are an intent extractor for a personal schedule bot.
Given a user message, return JSON matching EXACTLY ONE of these schemas:

{"action": "query", "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD", "type": null | "class" | "teaching" | "assignment_deadline" | "correction_deadline" | "meeting" | "other"}

{"action": "create", "title": "string", "start": "YYYY-MM-DDTHH:MM", "end": "YYYY-MM-DDTHH:MM" | null, "type": "meeting" | "other", "location": "string" | null, "link": "string" | null}

{"action": "summarize", "date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD"}

{"action": "unknown"}

Today is {today} ({weekday}). Time zone is Asia/Jakarta.
Resolve relative dates ("saturday", "tomorrow", "next week") to absolute dates.
Respond with ONLY valid JSON. No prose, no markdown, no explanation."""

SUMMARIZE_SYSTEM_PROMPT = """You are a personal schedule summarizer.
Given a JSON list of events and a user question, write a brief (2-4 sentences) helpful summary in the same language as the user's question.
Mention patterns: busy days, deadline clusters, free time.
Do NOT invent events not in the list. If the list is empty, say so plainly."""

async def extract_intent(user_message: str) -> dict:
    """Returns one of the four schemas above as a dict. Raises LLMError on timeout/parse failure."""
    ...

async def summarize_events(user_question: str, events: list[dict]) -> str:
    """Returns natural-language summary grounded in the provided events list."""
    ...

async def ping() -> bool:
    """True if Ollama is reachable and the model is loaded."""
    ...
```

Uses Ollama's `format: "json"` mode for `extract_intent` to guarantee parseable output.

### `src/bot.py`
discord.py `Bot` with:
- **Slash commands** (deterministic): `/today`, `/week`, `/upcoming`, `/add`, `/edit`, `/delete`, `/status`, `/ask`.
- **Free-text DM handler** (`on_message`): if message is a DM, not a command, and from `DISCORD_USER_ID` → route to LLM handler.
- **LLM handler** (`handle_chat`):
  1. Send `🤔 thinking...` if LLM call takes >2s (use `asyncio.wait_for` with a typing indicator).
  2. `intent = await llm.extract_intent(text)`.
  3. Dispatch on `intent["action"]`:
     - `query` → `db.list_events(...)` → format embed reply.
     - `create` → build proposed `Event` → reply with embed + ✅/❌ reactions → on ✅, `db.upsert_events` + `reminders.schedule_for`.
     - `summarize` → `db.list_events(...)` → `llm.summarize_events(question, events)` → reply.
     - `unknown` → polite fallback with slash-command hints.
  4. If `llm.LLMError` raised → reply "AI is offline, try `/today` instead."

### `main.py`
Boots bot + scheduler + `sync_job`. Calls `llm.ping()` on startup and logs if Ollama is unreachable (warning, not fatal — slash commands still work without LLM).

## Data model

### `Event` dataclass
```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

EventType = Literal[
    "class", "teaching", "meeting", "other",
    "assignment_deadline", "correction_deadline",
]
EventSource = Literal["lms", "messier", "manual"]

@dataclass
class Event:
    id: str                      # sha1(source + title + start_iso)[:12]
    source: EventSource
    type: EventType
    title: str
    start: datetime              # tz-aware (Asia/Jakarta)
    end: datetime | None = None
    location: str | None = None
    link: str | None = None
    notes: str | None = None
    remind_before: list[int] = field(default_factory=list)
    created_at: datetime = None
    updated_at: datetime = None
```

### SQLite schema
```sql
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT,
    location TEXT,
    link TEXT,
    notes TEXT,
    remind_before TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

CREATE TABLE IF NOT EXISTS reminders_sent (
    event_id TEXT NOT NULL,
    lead_min INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (event_id, lead_min)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scraper TEXT NOT NULL,
    ran_at TEXT NOT NULL,
    success INTEGER NOT NULL,
    inserted INTEGER,
    updated INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS llm_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT NOT NULL,
    user_message TEXT NOT NULL,
    intent_json TEXT,
    latency_ms INTEGER,
    error TEXT
);
```

## Data flows

### Auto-sync (every 15 min, all enabled scrapers)
Each scraper runs in its own try/except. Failure in one does not stop others. (Detail unchanged from earlier docs.)

### Reminder firing
APScheduler → `send_reminder(event_id, lead_min)` → idempotency check → DM embed → `mark_reminded`. (Unchanged.)

### Chat: query
```
1. User DMs: "anything saturday?"
2. bot.on_message detects free-text DM from authorized user.
3. await llm.extract_intent(text)
   → {"action": "query", "date_from": "2026-05-30", "date_to": "2026-05-30", "type": null}
4. events = db.list_events(date_from, date_to, type=None)
5. Bot replies with embed listing events (or "Nothing scheduled.")
6. db.log_llm(...) for observability.
```

### Chat: create with confirmation
```
1. User DMs: "add meeting with John tomorrow 9am at Zoom"
2. await llm.extract_intent(text)
   → {"action": "create", "title": "Meeting with John", "start": "2026-05-24T09:00", "type": "meeting", "location": "Zoom", ...}
3. Build proposed Event (NOT saved). Reply with embed:
   "📌 Proposed event:
    💼 Meeting with John
    📅 2026-05-24 09:00
    📍 Zoom
    React ✅ to save, ❌ to cancel."
4. Bot waits for reaction (with a 60s timeout).
5. On ✅: db.upsert_events([event]); reminders.schedule_for(event); reply "Saved ✓".
6. On ❌ or timeout: reply "Cancelled."
```

### Chat: summarize
```
1. User DMs: "summarize my week"
2. await llm.extract_intent(text)
   → {"action": "summarize", "date_from": "2026-05-23", "date_to": "2026-05-30"}
3. events = db.list_events(date_from, date_to)
4. event_summaries = [{"type": e.type, "title": e.title, "start": e.start.isoformat(), "location": e.location} for e in events]
5. text = await llm.summarize_events(user_message, event_summaries)
6. Reply with text + "Based on N events in your DB."
```

## Error handling & resilience

| Failure                       | Behavior                                                                                  |
|-------------------------------|-------------------------------------------------------------------------------------------|
| LMS / Messier session expired | Per-scraper failure; DM with `--portal=<name>` re-auth instruction                        |
| One scraper crashes hard      | Other scraper still runs; reminders for known events continue                             |
| Portal HTML/JSON shape change | `ParseError`; raw saved to `data/last_failed_payload_*.json`                              |
| Discord disconnect            | `discord.py` auto-reconnects; APScheduler keeps firing                                    |
| Process restart               | `reschedule_all` on boot; `reminders_sent` prevents dupes                                 |
| **Ollama daemon down**        | `llm.LLMError` raised; chat replies "AI offline, use slash commands"; slash commands + reminders unaffected |
| **LLM returns invalid JSON**  | Caught; logged to `llm_log`; chat reply suggests slash commands                           |
| **LLM returns `unknown`**     | Polite hint listing example questions                                                     |
| **LLM timeout (>15s)**        | Same as Ollama down — fall back gracefully                                                |
| Timezone drift                | All timestamps ISO 8601 with offset; APScheduler uses `TIMEZONE`                          |

**Critical principle:** the LLM is an enhancement, never a dependency. Bot must remain useful (slash commands, reminders) if Ollama is offline.

## Configuration & secrets

### `.env` (gitignored)
```
BINUS_USER=...
BINUS_PASS=...
DISCORD_TOKEN=...
DISCORD_USER_ID=...
DISCORD_GUILD_ID=...

LMS_BASE_URL=https://lms.binus.ac.id
LMS_SCHEDULE_ENDPOINT=
MESSIER_BASE_URL=https://socs1.binus.ac.id/messier
MESSIER_SCHEDULE_ENDPOINT=

SYNC_INTERVAL_MIN=15
TIMEZONE=Asia/Jakarta
ENABLED_SCRAPERS=lms,messier

OLLAMA_URL=http://localhost:11434
LLM_MODEL=qwen2.5:3b-instruct
LLM_TIMEOUT_SEC=15
```

### `.gitignore` (must include)
```
.env
auth_state_*.json
data/
__pycache__/
.venv/
*.pyc
.DS_Store
```

## File / directory structure

```
Bot-Timetable/
├── .env                          # gitignored
├── .env.example
├── .gitignore
├── auth_state_lms.json           # gitignored
├── auth_state_messier.json       # gitignored
├── data/
│   ├── events.db                 # gitignored
│   ├── sample_response_lms_*.json
│   ├── sample_response_messier_*.json
│   └── last_failed_payload_*.json
├── src/
│   ├── __init__.py
│   ├── auth.py
│   ├── parser.py
│   ├── db.py
│   ├── reminders.py
│   ├── bot.py
│   ├── config.py
│   ├── llm.py                    # Phase 11
│   └── scrapers/
│       ├── __init__.py
│       ├── base.py
│       ├── lms.py                # Phase 3
│       └── messier.py            # Phase 10
├── main.py
├── requirements.txt
├── PRD.md
├── ARCHITECTURE.md
├── PHASES.md
├── AGENTS.md
├── RECON.md                      # Phase 1
└── README.md
```

## Hosting

### Production: Oracle Cloud free-tier ARM VM
- **Ampere A1**, Ubuntu 22.04, 4 OCPU / 24 GB RAM / 200 GB disk. Free forever (not a trial).
- RAM budget: bot ~150 MB + Ollama daemon idle ~200 MB + Qwen 2.5 3B loaded ~2.2 GB → ~2.5 GB total. Massive headroom on 24 GB.
- `systemd` units for `bot-timetable.service` AND `ollama.service` (Ollama installer creates the latter automatically).

### Local dev: laptop
- Run `ollama serve` in one terminal, `python main.py` in another.
- Same code. Used for development; production runs on Oracle.

### What we do NOT use
- **Heroku / Render / Vercel free tiers** — cold starts break reminders + can't host the LLM.
- **GitHub Actions cron** — bot needs persistent connection.
- **Paid LLM APIs (OpenAI/Anthropic/Google)** — costs money, requires internet, sends data off-machine.
- **Docker / Kubernetes** — overkill.

## Out-of-scope architecture (v2+)
- Multi-channel delivery (Telegram, WhatsApp) — abstract `Notifier` interface.
- Google Calendar two-way sync.
- LLM-driven event editing/deletion (kept as slash commands for safety).
- Multi-turn chat memory.
- Larger LLM (Qwen 7B) if 3B accuracy proves insufficient — drop-in via `LLM_MODEL` env var.
