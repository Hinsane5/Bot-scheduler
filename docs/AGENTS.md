# AGENTS.md — Bot Timetable

Instructions for any AI coding agent (Claude, Cursor, Codex, etc.) working on this project.

## What this is
A personal Discord bot that:
1. Auto-syncs the user's Binus academic schedule (LMS + Messier).
2. DMs reminders before each event.
3. Answers natural-language schedule questions via a **local LLM** (Qwen 2.5 3B via Ollama — no paid APIs, runs on the same VM).

Single-user. Runs 24/7 on free Oracle Cloud ARM VM. $0/month. Python + discord.py + Playwright + SQLite + Ollama.

## Read these first, in order
1. **`PRD.md`** — problem, scope, event taxonomy, chatbot capability, success criteria.
2. **`ARCHITECTURE.md`** — components, `Event` dataclass, SQLite schema, data flows, LLM interface, file layout.
3. **`PHASES.md`** — incremental build plan. Source of truth for "what to do next."
4. **`RECON.md`** — created in Phase 1. Real portal endpoints + login flow.

If any of these conflict with this file, **the doc wins** — update this file to match.

## Where we are
Check `PHASES.md` and repo state:
- No `src/` → Phase 0 not started.
- `src/` but no `RECON.md` → Phase 1 next.
- `RECON.md` but no `auth_state_lms.json` → Phase 2.
- `auth_state_*` but no `src/scrapers/lms.py` → Phase 3.
- ...
- Bot on Oracle but no `src/llm.py` → Phase 11.
- `src/llm.py` exists, chat replies but no create-with-confirm → Phase 12.

Do not skip phases. Each has acceptance criteria — verify before moving on.

## Tech stack (locked)
- Python 3.11+
- `discord.py` 2.x
- `playwright`, `httpx`, `beautifulsoup4`
- `apscheduler` 3.x (`AsyncIOScheduler`)
- `sqlite3` (stdlib)
- `python-dotenv`
- **Ollama** (daemon, separate process)
- **Qwen 2.5 3B Instruct** (Q4_K_M GGUF) via Ollama

Do not introduce: Redis, Postgres, Docker, web frameworks, ORMs, **any paid API**, OpenAI/Anthropic/Google SDKs, LangChain, vector DBs.

## Project layout
```
src/
├── auth.py              # per-portal interactive login + session probe
├── parser.py            # Event dataclass + EventType/EventSource
├── db.py                # SQLite CRUD, upsert, idempotency
├── reminders.py         # APScheduler glue for reminder jobs
├── bot.py               # discord.py Bot + slash commands + chat handler
├── config.py            # .env loader, constants
├── llm.py               # Ollama wrapper (Phase 11)
└── scrapers/
    ├── base.py          # Scraper ABC
    ├── lms.py           # Phase 3
    └── messier.py       # Phase 10
main.py                  # boots bot + scheduler + sync_job
```

## Hard rules

### Timezone
All `datetime` values tz-aware in `Asia/Jakarta`. **Never** `datetime.now()` — always `datetime.now(ZoneInfo("Asia/Jakarta"))`. Persist as ISO 8601 with offset. APScheduler uses same TZ.

### Event IDs
Deterministic SHA1, never random:
```python
event_id = hashlib.sha1(f"{source}|{title}|{start.isoformat()}".encode()).hexdigest()[:12]
```
This is what makes upsert idempotent.

### Scraper interface
Subclass `src/scrapers/base.Scraper`. Implement `async fetch(start, end) -> list[Event]`. Register in `src/scrapers/__init__.py:REGISTRY`. Do not bypass.

### Sync isolation
In `sync_job`, each scraper in its own try/except. One failure must never block another. DM on `SessionExpired`; log + DM on other exceptions.

### Reminders are derived, not stored
On boot and after every sync, call `reminders.reschedule_all(scheduler)`. APScheduler jobs do NOT persist across restart — `reminders_sent` table prevents dupes.

### Manual vs auto events
Manual `/add` and chat-create accept only `meeting` or `other`. Auto-synced types (`class`, `teaching`, `assignment_deadline`, `correction_deadline`) come from portals. `/edit` and `/delete` operate only on `source="manual"` rows.

### SQL
Parameterized queries only (`?`). Never string-concat user input. `INSERT ... ON CONFLICT(id) DO UPDATE SET ...` for upsert.

### Secrets
- Credentials in `.env` (gitignored). Never log them.
- Session files (`auth_state_*.json`) gitignored.
- Verify `git status` clean of `.env`, `auth_state_*.json`, `data/` before any `git add`.
- Never commit `data/events.db`, `data/sample_response_*.json`, `data/last_failed_payload_*.json`.

### LLM rules (Phase 11+)
- **The LLM never returns event data.** It only extracts structured intents (JSON) OR generates summary text from events Python passes in.
- **All DB reads/writes go through `db.py`.** The LLM proposes; code executes. This makes it impossible for the LLM to invent events.
- **Create operations require explicit ✅ confirmation** via Discord reaction. Never auto-save from chat.
- **Summarize prompts must include the actual events** (passed as JSON list). The LLM is grounded — it summarizes provided data, never recalls or invents.
- **JSON-mode output for intent extraction.** Use Ollama's `format: "json"`. Validate against schema; on failure, reply with fallback.
- **The LLM is an enhancement, never a dependency.** Bot must work fully (slash commands, reminders, auto-sync) if Ollama is offline. Wrap every LLM call in try/except + timeout.
- **No multi-turn context.** Each message is independent. Don't add conversation memory without updating PRD.
- **Scope is schedule-only.** If user asks off-topic, return polite fallback. Don't waste compute on general chat.
- **Log every LLM call** to `llm_log` table (user msg, intent, latency, error). Lets us spot regressions when Qwen updates.

### Slash commands stay deterministic
Slash commands (`/today`, `/add`, `/edit`, `/delete`, `/status`) must never call the LLM. They use typed parameters and direct DB queries. The LLM only handles `/ask` and free-text DMs.

## Commands you'll use
```bash
# --- Local dev ---
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Interactive login
python -m src.auth --portal=lms
python -m src.auth --portal=messier
python -m src.auth --portal=lms --check

# Scraper CLI tests
python -m src.scrapers.lms
python -m src.scrapers.messier

# LLM CLI tests
python -m src.llm "anything saturday?"
python -m src.llm "ada meeting besok ga?"

# Run the bot
python main.py

# Ollama local
ollama serve                            # daemon (auto on VM via systemd)
ollama pull qwen2.5:3b-instruct
ollama run qwen2.5:3b-instruct "hello"  # quick model test
ollama list                             # see installed models

# --- DB inspection ---
sqlite3 data/events.db "SELECT id, type, title, start FROM events ORDER BY start LIMIT 20;"
sqlite3 data/events.db "SELECT scraper, ran_at, success, error FROM sync_log ORDER BY ran_at DESC LIMIT 10;"
sqlite3 data/events.db "SELECT * FROM reminders_sent ORDER BY sent_at DESC LIMIT 10;"
sqlite3 data/events.db "SELECT user_message, intent_json, latency_ms, error FROM llm_log ORDER BY ran_at DESC LIMIT 10;"

# --- Oracle VM ops ---
ssh ubuntu@<vm-ip>
sudo systemctl status bot-timetable
sudo systemctl status ollama
sudo systemctl restart bot-timetable
journalctl -u bot-timetable -f
journalctl -u ollama -f
free -h                                 # check RAM usage (bot + model)
```

## What NOT to do
- Don't copy code from existing Binus scraper repos (radityaharya/binusmaya_py, etc.) — user wants from scratch. Reference for architecture only, never paste.
- Don't add features outside the current phase. Ship the phase's deliverable first.
- Don't refactor working code for "cleanliness" unless asked.
- Don't add Docker, CI, type-checking config, linters, pre-commit hooks, or test frameworks unless asked.
- Don't write speculative tests. Manual phase-acceptance is fine for v1.
- Don't add try/except around things that can't fail.
- Don't write comments that restate code. Only comment hidden invariants.
- Don't change `Event` dataclass or SQLite schema without updating `ARCHITECTURE.md`.
- Don't bypass `reschedule_all` by scheduling reminders directly inside `sync_job`.
- Don't call paid LLM APIs. Don't add OpenAI/Anthropic/Google SDK dependencies. Local Ollama only.
- Don't let the LLM directly produce events or DB writes. It returns intents; Python executes.
- Don't add multi-turn chat memory without a PRD update.
- Don't add general-purpose chat (jokes, off-topic). Reply with polite scope reminder.

## Debugging tips

### Scraper
- **Returns nothing** → `python -m src.auth --portal=<name> --check`. If invalid, re-login. If valid, the endpoint URL or response shape probably changed — capture fresh sample → update parser.

### Reminders
- **Didn't fire** → check `sqlite3 data/events.db "SELECT * FROM reminders_sent WHERE event_id='...'"`. Present → fired (Discord DM failed; check logs). Empty → scheduler didn't trigger (check `scheduler.get_jobs()`).
- **Duplicate fired** → `reminders_sent` not being written, or `event.id` non-deterministic. Verify SHA1 inputs.

### Bot
- **`/today` empty but events in portal** → check `sync_log` for last error per scraper; check `data/last_failed_payload_*.json`.
- **Bot offline** → `journalctl -u bot-timetable -f` (VM) or terminal output (local).

### LLM
- **Chat replies "AI offline"** → check `systemctl status ollama` on VM. Restart with `sudo systemctl restart ollama`.
- **LLM returns garbage** → check `llm_log` for raw output. Common: missing `format: "json"`, prompt too long, model not fully loaded (first call after restart is slow).
- **Wrong date parsed** → Qwen often confuses relative dates if the prompt doesn't include `today` and `weekday`. Verify the system prompt template.
- **Slow (>10s)** → model probably swapped out of RAM. `ollama ps` to see loaded models. Keep one model warm via periodic pings, or accept first-call latency.
- **Indonesian inputs fail** → Qwen 2.5 3B handles Indonesian well but test specific phrasings. If accuracy is poor, log examples to `llm_log` and refine the prompt OR upgrade to Qwen 2.5 7B (same code, just `LLM_MODEL` env var).

### RAM on Oracle VM
- **OOM** → `free -h`. Expected: bot ~150 MB + ollama daemon ~200 MB + Qwen 3B loaded ~2.2 GB = ~2.5 GB. If higher, check for memory leaks (`ollama ps`, unexpected models).

## When updating these docs
- Scope or success criteria → update `PRD.md`.
- Data model, components, or flows → update `ARCHITECTURE.md`.
- Build order or phase definitions → update `PHASES.md`.
- Conventions, commands, or guardrails → update this file.

Keep all four in sync. Code disagreeing with a doc → fix the doc OR the code, not just one.
