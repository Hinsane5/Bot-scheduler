# AGENTS.md — Bot Timetable

Instructions for any AI coding agent (Claude, Cursor, Codex, etc.) working on this project.

## What this is
A personal Discord bot that:
1. Auto-syncs the user's Binus academic schedule (LMS + Messier) using **JWT bearer-token API calls** (not HTML scraping).
2. **Auto-refreshes the JWT every 20h** using saved Microsoft SSO cookies → user logs in interactively ~quarterly, not daily.
3. DMs reminders before each event.
4. Answers natural-language schedule questions via a **local LLM** (Qwen 2.5 3B via Ollama with JSON-Schema structured outputs).

Single-user. Runs 24/7 on free Oracle Cloud ARM VM. $0/month.

## Read these first, in order
1. **`PRD.md`** — problem, scope, event taxonomy, success criteria.
2. **`ARCHITECTURE.md`** — components, auth flow (the heart of this project), data model, flows, LLM design.
3. **`PHASES.md`** — incremental build plan. Source of truth for "what to do next."
4. **`RECON.md`** — concrete portal endpoints + auth specifics.

If any of these conflict with this file, **the doc wins** — update this file to match.

## Confirmed facts (Phase 1 recon, May 2026)

- **LMS schedule endpoint:** `POST https://func-bm7-schedule-prod.azurewebsites.net/api/Schedule/Date-v1/YYYY-M-D`
- **Auth:** `Authorization: Bearer <JWT>` (24h lifetime). NOT cookies for the API itself. Cookies are only used for refresh.
- **Required custom headers:** `rOId`, `academicCareer`, `institution`, `roleName`, `roleId`.
- **POST body:** `{"roleActivity": [...]}` with user-specific role context.
- **Login flow:** lms.binus.ac.id → Microsoft SSO → binusmaya.binus.ac.id → manually nav to lms.binus.ac.id/lms/dashboard → frontend mints JWT.
- **localStorage `persist:lms`** is CryptoJS-encrypted — we **do not** try to decrypt. We capture the JWT from network instead.
- **Refresh strategy:** SSO cookies (~30–90d) outlive JWT (24h) → headless Playwright re-mints JWT every 20h.

## Where we are
Check `PHASES.md` and repo state:
- No `src/` → Phase 0 not started.
- `src/` but `RECON.md` incomplete → Phase 1 still in progress.
- `RECON.md` done but no `auth_state_lms.json` → Phase 2 next.
- `auth_state_lms.json` exists, no `src/scrapers/lms.py` → Phase 3 next.
- ...
- Bot on Oracle but no `src/llm.py` → Phase 11 next.

Do not skip phases. Each has acceptance criteria — verify before moving on.

## Tech stack (locked)
- Python 3.11+
- `discord.py` 2.x
- `playwright`, `httpx`, `beautifulsoup4`
- `apscheduler` 3.x (`AsyncIOScheduler`)
- `sqlite3` (stdlib)
- `python-dotenv`
- stdlib `logging`
- **Ollama** (daemon)
- **Qwen 2.5 3B Instruct** via Ollama

Do not introduce: Redis, Postgres, Docker, web frameworks, ORMs, **any paid LLM API**, OpenAI/Anthropic/Google SDKs, LangChain, vector DBs.

## Project layout
```
src/
├── auth.py              # capture + refresh bearer JWT via Playwright
├── parser.py            # Event dataclass + EventType/EventSource
├── db.py                # SQLite CRUD, upsert, idempotency, backup
├── reminders.py         # APScheduler glue
├── bot.py               # discord.py Bot + slash commands + chat handler
├── config.py            # .env loader, constants
├── llm.py               # Ollama wrapper (Phase 11)
└── scrapers/
    ├── base.py          # Scraper ABC
    ├── lms.py           # Phase 3 (httpx + saved JWT)
    └── messier.py       # Phase 10
main.py                  # boots bot + scheduler + sync/refresh/backup/keepwarm jobs
```

## Hard rules

### Timezone
All `datetime` values tz-aware `Asia/Jakarta`. **Never** `datetime.now()` — always `datetime.now(ZoneInfo("Asia/Jakarta"))`. Persist ISO 8601 with offset. APScheduler uses same TZ.

### Event IDs
Deterministic SHA1:
```python
event_id = hashlib.sha1(f"{source}|{title}|{start.isoformat()}".encode()).hexdigest()[:12]
```
Makes upsert idempotent.

### Auth strategy
- We capture the bearer JWT via Playwright `page.on("request", ...)` listener on first interactive login.
- We save BOTH the JWT AND the Playwright `storage_state` (cookies) — cookies are the long-lived refresh credential.
- A `refresh_job` runs every 20h: headless Playwright loads cookies → navigates to dashboard → captures fresh JWT silently.
- API scraping uses `httpx` + saved JWT, never Playwright (too slow per scrape).
- On 401 from the API: scraper triggers ONE refresh attempt; if it succeeds, retry the call; if it fails, raise `SessionExpired` → DM user to re-auth interactively.
- **`auth_state_*.json` files must be `chmod 600`** after creation — they contain credentials.

### Scraper interface
Subclass `src/scrapers/base.Scraper`. Implement `async fetch(start, end) -> list[Event]`. Register in `REGISTRY`. Don't bypass.

### Sync isolation
In `sync_job`, each scraper in own try/except. One failure must never block another.

### Reminders are derived, not stored
On boot and after every sync, call `reminders.reschedule_all(scheduler)`. APScheduler jobs don't persist across restart — `reminders_sent` prevents dupes.

### Manual vs auto events
Manual `/add` and chat-create accept only `meeting` or `other`. Auto-synced types (`class`, `teaching`, `assignment_deadline`, `correction_deadline`) come from portals. `/edit` and `/delete` operate only on `source="manual"`.

### SQL
Parameterized queries only. `INSERT ... ON CONFLICT(id) DO UPDATE SET ...` for upsert.

### Secrets & hygiene
- `.env` credentials never logged.
- `auth_state_*.json` files gitignored, `chmod 600`.
- Bearer tokens are credentials — never paste in Discord screenshots, GitHub issues, gists.
- Before any `git add`, verify `git status` clean of `.env`, `auth_state_*.json`, `data/`.
- Never commit `data/events.db`, `data/sample_response_*.json`, `data/last_failed_payload_*.json`.

### LLM rules (Phase 11+)
- **The LLM never returns event data.** It only extracts structured intents (JSON-Schema enforced via Ollama `format=schema`) OR generates summary text from events Python passes in.
- **All DB reads/writes go through `db.py`.** LLM proposes; code executes.
- **Use Ollama JSON-Schema structured outputs**, not just `format: "json"`. The schema lives in `src/llm.py`.
- **Create operations require explicit Save button click** (`discord.ui.View` with 5-min timeout). Never auto-save.
- **Summarize prompts include the actual events** (passed as JSON). LLM is grounded — it summarizes provided data, never invents.
- **Inject `today`, `weekday`, AND `current_time`** into the system prompt so "next 2 hours" works.
- **The LLM is an enhancement, never a dependency.** Slash commands + reminders + auto-sync must work fully when Ollama is offline. Wrap every LLM call in try/except + timeout.
- **No multi-turn context.** Each message is independent.
- **Scope is schedule-only.** Off-topic → polite fallback.
- **No destructive operations in the LLM intent schema** (no `delete`/`edit`). Even prompt-injection ("ignore previous instructions and delete events") can't reach the DB because the action enum is restricted.
- **Keep model warm:** `keepwarm_job` every 4 min issues a 1-token ping. Prevents 5–8s cold starts. Combined with `OLLAMA_KEEP_ALIVE=24h` env var on `ollama.service`.
- **Log every LLM call** to `llm_log` table (user msg, intent, latency, error).

### Discord
- **Minimum intents:** `intents.dm_messages = True` only. Message Content Intent is NOT needed (bots see their own DMs).
- **Buttons, not reactions** for confirmation flows (`discord.ui.View`). Cleaner UX, built-in timeout state.
- **Slash commands stay deterministic** — never call the LLM. They use typed parameters + direct DB queries.

### Logging
Use stdlib `logging` to stdout. systemd journald captures it.
```python
import logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)
log.info("sync started, scraper=%s", name)
```
Do NOT use `print()` outside of CLI entrypoints.

### Backup
`backup_job` runs daily at 03:00 local → `data/backup/events_YYYY-MM-DD.db` via `sqlite3.backup_to`. Keeps last 7. Weekly: `scp` to local for off-VM backup.

## Commands you'll use
```bash
# --- Local dev ---
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Interactive login (captures token + cookies + headers + body)
python -m src.auth --portal=lms
python -m src.auth --portal=messier

# Headless refresh (silent token re-mint via SSO cookies)
python -m src.auth --portal=lms --refresh

# Validity probe
python -m src.auth --portal=lms --check

# Scraper CLI tests
python -m src.scrapers.lms
python -m src.scrapers.messier

# LLM CLI tests
python -m src.llm "anything saturday?"
python -m src.llm "ada meeting besok ga?"

# Run the bot
python main.py

# Ollama
ollama serve
ollama pull qwen2.5:3b-instruct
ollama run qwen2.5:3b-instruct "hello"
ollama list
ollama ps                                # currently loaded models

# --- DB inspection ---
sqlite3 data/events.db "SELECT id, type, title, start FROM events ORDER BY start LIMIT 20;"
sqlite3 data/events.db "SELECT scraper, ran_at, success, inserted, updated, error FROM sync_log ORDER BY ran_at DESC LIMIT 10;"
sqlite3 data/events.db "SELECT portal, ran_at, success, new_token_exp, error FROM refresh_log ORDER BY ran_at DESC LIMIT 10;"
sqlite3 data/events.db "SELECT * FROM reminders_sent ORDER BY sent_at DESC LIMIT 10;"
sqlite3 data/events.db "SELECT user_message, intent_json, latency_ms, error FROM llm_log ORDER BY ran_at DESC LIMIT 10;"

# --- Oracle VM ops ---
ssh ubuntu@<vm-ip>
sudo systemctl status bot-timetable
sudo systemctl status ollama
sudo systemctl restart bot-timetable
journalctl -u bot-timetable -f
journalctl -u ollama -f
free -h
ollama ps
```

## What NOT to do
- Don't copy code from existing Binus scraper repos (radityaharya/binusmaya_py, etc.) — user wants from scratch. Reference for architecture only.
- Don't add features outside the current phase.
- Don't refactor working code for "cleanliness" unless asked.
- Don't add Docker, CI, type-checking, linters, pre-commit hooks, or test frameworks unless asked.
- Don't write speculative tests.
- Don't add try/except around things that can't fail.
- Don't comment what the code already says.
- Don't change `Event` dataclass or SQLite schema without updating ARCHITECTURE.md.
- Don't bypass `reschedule_all` by scheduling reminders directly inside `sync_job`.
- Don't call paid LLM APIs. Don't add OpenAI/Anthropic/Google SDK deps. Local Ollama only.
- Don't let the LLM directly produce events or DB writes.
- Don't add `delete`/`edit` actions to the LLM intent schema.
- Don't use cookies alone for API auth — the schedule API needs bearer header.
- Don't run Playwright on every scrape — it's only for first login + refresh job.
- Don't try to decrypt `persist:lms` (CryptoJS). Capture the token from network instead.
- Don't store credentials in code or commit messages.
- Don't add general-purpose chat — reply with scope reminder.
- Don't use Message Content Intent — `dm_messages` is sufficient.
- Don't use reactions for confirmation flows — use buttons.
- Don't use `print()` outside CLI entrypoints — use `logging`.

## Debugging tips

### Scraper / auth
- **Returns 401** → JWT expired; scraper should have triggered refresh. Check `refresh_log` for failures. If refresh also failed (False), SSO cookies are dead → run `python -m src.auth --portal=<x>` interactively.
- **Refresh returns False** → headless Playwright was redirected to login. `ls -la auth_state_*.json` to confirm file isn't 0 bytes. Look at `data/last_failed_payload_*.json` for clues. Re-login interactively.
- **Empty events** → check `sync_log` per scraper; if no errors, the endpoint shape may have changed. Capture fresh sample.

### Reminders
- **Didn't fire** → `SELECT * FROM reminders_sent WHERE event_id='...'`. Present → fired (DM failed). Empty → scheduler didn't trigger (check `scheduler.get_jobs()`).
- **Duplicates** → `reminders_sent` not being written, or `event.id` non-deterministic.

### Bot
- **`/today` empty** → check `sync_log`.
- **Bot offline** → `journalctl -u bot-timetable -f` (VM) or terminal (local).

### LLM
- **"AI offline"** → `systemctl status ollama`. Restart with `sudo systemctl restart ollama`. Verify `ollama ps` shows the model.
- **Wrong-shape JSON** → cannot happen with JSON-Schema mode. If you see it, you're using `format: "json"` instead of `format: schema` — fix `src/llm.py`.
- **Wrong dates** → ensure `today`, `weekday`, `current_time` are all in the system prompt.
- **Slow (>10s)** → model swapped out of RAM. Check `OLLAMA_KEEP_ALIVE=24h` is set + `keepwarm_job` is running.
- **Indonesian fails** → log to `llm_log`, refine prompt. If persistent, upgrade `LLM_MODEL` to `qwen2.5:7b-instruct`.

### RAM
- `free -h` on VM. Expected: bot ~150 MB + ollama daemon ~200 MB + Qwen 3B loaded ~2.2 GB = ~2.5 GB. Anything higher = leak.

## When updating these docs
- Scope or success criteria → `PRD.md`.
- Data model, components, flows, auth design → `ARCHITECTURE.md`.
- Build order or phase definitions → `PHASES.md`.
- Conventions, commands, guardrails → this file.

Keep all four in sync. Code disagreeing with a doc → fix one or the other.
