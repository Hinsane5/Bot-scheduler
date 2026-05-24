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

### LMS — bearer JWT auth (full spec: `docs/LMS_Requirement.md`)
- **Preferred endpoint:** `POST https://func-bm7-schedule-prod.azurewebsites.net/api/Schedule/Month-v1/YYYY-M-1` (one call per month — production scraper uses this).
- **Fallback endpoint:** `Date-v1/YYYY-M-D` (one call per day — debug only).
- **Auth:** `Authorization: Bearer <JWT>` (~24 h lifetime). NOT cookies for the API itself. Cookies are only used to refresh the JWT.
- **Required custom headers:** `rOId`, `academicCareer`, `institution`, `roleName`, `roleId`. `rOId` must match the `roleOrganizationId` of whichever `roleActivity` item has `isActive: true` in the POST body.
- **POST body:** `{"roleActivity": [...]}` with user-specific role context (preserved from interactive login — don't try to switch roles).
- **Single endpoint returns ALL event types** — discriminate by `scheduleType`:
  - `"Onsite"` / `"Online"` / `"Virtual Class"` → `class`
  - `"Assignment"` (or `lamType == "ASG"`) → `assignment_deadline` (`start = customParam.dueDate`)
  - `"Event"` → `other` (`link = customParam.url` — Zoom URL exposed for Events)
  - Unknown → log warning, fall back to `other`
- **URL format:** `YYYY-M-D` WITHOUT zero padding (`2026-5-1`, not `2026-05-01`).
- Month-v1 takes the **first of the month** (`2026-5-1`), not arbitrary dates.
- Month-v1 response: array of `{dateStart, Schedule[]}` buckets; days with no events are OMITTED.
- Date-v1 returns `204 No Content` for empty days.
- **Login flow:** `lms.binus.ac.id` → Microsoft SSO → `binusmaya.binus.ac.id` → manual nav to `lms.binus.ac.id/lms/dashboard` → frontend mints JWT.
- **localStorage `persist:lms`** is CryptoJS-encrypted — we do NOT decrypt. We capture the JWT from network instead.
- **Refresh:** SSO cookies (~30–90 d) outlive JWT (24 h) → headless Playwright re-mints JWT every 20 h.

### Messier — cookie session auth (full spec: `docs/MESSIER_Requirement.md`)
- **Endpoint:** `POST https://socs1.binus.ac.id/messier/Job.svc/GetActivesJob`
- **Auth:** `Cookie: .ASPXAUTH=...` (ASP.NET Forms Auth, sliding expiry). NO bearer token. NO custom required headers (beyond `X-Requested-With`, `Content-Type`, `Referer`).
- **Backend:** classic ASP.NET WCF (not Azure Functions like LMS).
- **POST body:** `{"type": "future"}`.
- **Single endpoint returns both `teaching` and `correction_deadline` items** — discriminate by `JobType`:
  - `"Teaching"` / `"Exam Proctor"` → `teaching`
  - `"Marking"` → `correction_deadline`
  - Unknown → `other` (log warning)
- **Login flow:** `Login.aspx` → manual login → `Home.aspx` (`.ASPXAUTH` issued).
- **Refresh:** `.ASPXAUTH` is sliding-expiry; `page.goto(Home.aspx)` every ~25 min bumps the timeout forward. If response goes back to `Login.aspx`, session is dead.

### Messier response quirks the scraper MUST handle
- **ASP.NET date format:** `/Date(<ms>+<tz>)/` — parse with regex, milliseconds are UTC epoch.
- **`Id` is always `"00000000-0000-0000-0000-000000000000"`** — DO NOT use. Build composite SHA1 from `Note` + `StartDate`.
- **`Status` is inconsistent:** `"NotDone"` vs `"Not Done"` vs `"Done"` — normalize: `s.strip().replace(" ", "").lower()`.
- **`IsSubstitute` flag is unreliable** — substitute jobs have `IsSubstitute: false` but `"(Substitute)"` prefix in `Description`. Trust the prefix.
- **`LatestDate` is `253370739600000` (year 9999) for Marking** — sentinel meaning "no upper deadline." Ignore; use `EndDate`.
- **Filter `done` items** — completed jobs would generate stale reminders.
- **For `Marking` jobs:** use `EndDate` as the event `start` (it's the grading deadline). `StartDate` is when the marking window opens — not directly useful as a reminder target.

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

### Auth strategy — TWO modes (dispatch on `auth_mode`)

**LMS — `auth_mode: "bearer"`:**
- Capture JWT via Playwright `page.on("request", ...)` listener on first interactive login.
- Save JWT + Playwright `storage_state` (SSO cookies) + custom headers + POST body.
- `refresh_job_lms` runs every 20 h: headless Playwright loads SSO cookies → navigates to dashboard → captures fresh JWT silently.
- API scraping uses `httpx` + saved JWT (never Playwright per scrape — too slow).
- On 401: scraper triggers ONE `auth.refresh("lms")` attempt; retry. Still 401 → `SessionExpired` → DM user.

**Messier — `auth_mode: "cookie"`:**
- Just capture Playwright `storage_state` (includes `.ASPXAUTH`).
- No bearer token to extract, no custom headers to record.
- `refresh_job_messier` runs every ~25 min: headless Playwright with saved cookies → `page.goto(Home.aspx)` → sliding expiry bumped → save updated cookies.
- API scraping uses `httpx` + cookies from `storage_state`.
- On 302→`Login.aspx`: scraper triggers ONE `auth.refresh("messier")`; retry. Still 302 → `SessionExpired` → DM user.

**Both modes:**
- `auth_state_<portal>.json` MUST be `chmod 600` after creation.
- Files contain credentials — gitignored, never logged, never pasted into Discord/GitHub/screenshots.
- `auth.refresh(portal)` and `auth.is_session_valid(portal)` dispatch internally on `auth_mode`. Callers stay agnostic.

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
- Don't conflate LMS and Messier auth — LMS = bearer JWT, Messier = `.ASPXAUTH` cookie. They're different code paths inside `auth.py`.
- Don't loop Date-v1 per day for LMS — use Month-v1 (one call per month). Date-v1 is the debug fallback only.
- Don't zero-pad LMS URL dates — `2026-5-1`, not `2026-05-01`.
- Don't try to map every `scheduleType` to a unique `EventType` — `Onsite`/`Online`/`Virtual Class` all collapse to `class`; `Event` collapses to `other`.
- Don't run Playwright on every scrape — it's only for first login + the periodic refresh job.
- Don't try to decrypt LMS's `persist:lms` (CryptoJS). Capture the JWT from network instead.
- Don't use Messier's `Id` field for deduplication — it's always all-zeros. Build composite SHA1 from `Note` + `StartDate`.
- Don't compare Messier `Status` strings directly — normalize first (`strip().replace(" ", "").lower()`). `"NotDone"` and `"Not Done"` are the same value.
- Don't trust Messier's `IsSubstitute` flag — check for `"(Substitute)"` prefix in `Description` instead.
- Don't use Messier's `LatestDate` for Marking jobs — it's a year-9999 sentinel. Use `EndDate` (the actual deadline).
- Don't include `done`-status Messier jobs — filter them out so completed work doesn't generate stale reminders.
- Don't store credentials in code or commit messages.
- Don't add general-purpose chat — reply with scope reminder.
- Don't use Message Content Intent — `dm_messages` is sufficient.
- Don't use reactions for confirmation flows — use buttons.
- Don't use `print()` outside CLI entrypoints — use `logging`.

## Debugging tips

### Scraper / auth (LMS — bearer)
- **Returns 401** → JWT expired; scraper should have triggered `auth.refresh("lms")`. Check `refresh_log WHERE portal='lms'`. If refresh also failed → SSO cookies dead → `python -m src.auth --portal=lms` interactively.
- **Refresh returns False** → headless Playwright redirected to login. Check `auth_state_lms.json` isn't 0 bytes. Re-login.

### Scraper / auth (Messier — cookie)
- **Returns 302 / HTML page** → `.ASPXAUTH` expired; scraper should have triggered `auth.refresh("messier")`. Check `refresh_log WHERE portal='messier'`. If refresh also failed → re-login interactively.
- **Refresh returns False** → headless Playwright landed on `Login.aspx`. The `.ASPXAUTH` is permanently dead. Re-login.
- **Random 302s mid-day** → `MESSIER_REFRESH_INTERVAL_MIN=25` may be too long. Lower to 15.
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
