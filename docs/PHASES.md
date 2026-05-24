# Build Phases

Each phase has: **Goal**, **Tasks**, **Deliverable**, **Acceptance**, **Gotchas**. Do not start a phase until the previous one's acceptance criteria pass.

Total estimated effort: ~35–45 hours over 3 weeks of evenings.

---

## Phase 0 — Project setup (30 min)

**Goal:** Empty-but-runnable Python project with all deps installed.

**Tasks:**
1. `cd /Users/howardgoh/Documents/Project/Bot-Timetable`
2. `python3 -m venv .venv && source .venv/bin/activate`
3. `requirements.txt`:
   ```
   discord.py>=2.3
   playwright>=1.40
   httpx>=0.26
   beautifulsoup4>=4.12
   apscheduler>=3.10
   python-dotenv>=1.0
   tzdata>=2024.1
   ```
4. `pip install -r requirements.txt && playwright install chromium`
5. Directory skeleton (see ARCHITECTURE.md), including `src/scrapers/` and `data/backup/`.
6. `.env.example` + `.env` (placeholders).
7. `.gitignore` per ARCHITECTURE.md.
8. `git init && git add . && git commit -m "Phase 0: skeleton"`.

**Acceptance:** `python -c "import discord, playwright, httpx, bs4, apscheduler, dotenv; print('ok')"` prints `ok`. `.env` exists, gitignored.

---

## Phase 1 — Reconnaissance (DONE for core scope)

**Status from May 2026 recon:**

✅ **LMS endpoint — CONFIRMED** (see [LMS_Requirement.md](LMS_Requirement.md)):
- `POST https://func-bm7-schedule-prod.azurewebsites.net/api/Schedule/Date-v1/YYYY-M-D`
- Auth: `Authorization: Bearer <JWT>` (~24 h lifetime, BinusServices issuer)
- Custom headers: `rOId`, `academicCareer`, `institution`, `roleName`, `roleId`
- POST body: `{"roleActivity": [...]}` with user role context
- **Single endpoint** returns BOTH `class` and `assignment_deadline` items (discriminated by `scheduleType == "Assignment"` or `lamType == "ASG"`)
- 204 No Content = no events that day
- Login flow: Microsoft SSO → binusmaya → manual nav to LMS dashboard
- Sample saved to `data/sample_response_lms_schedule.json`

✅ **Messier endpoint — CONFIRMED** (see [MESSIER_Requirement.md](MESSIER_Requirement.md)):
- `POST https://socs1.binus.ac.id/messier/Job.svc/GetActivesJob`
- Auth: cookie session (`.ASPXAUTH`), NOT bearer token
- Body: `{"type": "future"}`
- **Single endpoint** returns BOTH `teaching` and `correction_deadline` items (discriminated by `JobType`: `Teaching` / `Exam Proctor` / `Marking`)
- Quirks: ASP.NET `/Date(ms+tz)/` format, all-zero `Id`, inconsistent `Status` strings, year-9999 sentinel for Marking `LatestDate`
- Login flow: classic ASP.NET form login (`Login.aspx` → `Home.aspx`)

✅ **Dual auth strategy — DESIGNED:**
- LMS = bearer JWT, refreshable via Microsoft SSO cookies every 20 h
- Messier = `.ASPXAUTH` cookie, refreshable via sliding-expiry ping every ~25 min

⏳ **Nice-to-have, not blocking:**
- **`.ASPXAUTH` absolute lifetime** — measure to confirm `MESSIER_REFRESH_INTERVAL_MIN=25` is safe.
- **Online-class meeting link** — LMS captured sample is F2F-only; capture one online class to see where the link field lives.
- **Assignment detail URL** — so reminder DMs can deep-link.
- **(Optional) Outlook calendar check** — could simplify project via Microsoft Graph (deferred).

**Acceptance:** `docs/LMS_Requirement.md` + `docs/MESSIER_Requirement.md` both exist with confirmed endpoints, field mappings, and quirks documented.

**Gotchas:** capture as cURL via DevTools (right-click XHR → Copy → Copy as cURL) — preserves all headers.

---

## Phase 2 — Dual-auth: token capture + cookie session + refresh (5–7 hours)

**Goal:** A single `auth.py` that handles BOTH auth styles:
- **LMS** — capture bearer JWT (+ SSO cookies + custom headers + POST body), refresh via SSO cookies every 20 h.
- **Messier** — capture `.ASPXAUTH` cookie (via `storage_state`), refresh via `Home.aspx` sliding bump every ~25 min.

The interface is portal-agnostic; internal dispatch on `auth_mode` (`"bearer"` vs `"cookie"`).

**Tasks:**

### Portal config (in `src/auth.py`)
```python
PORTALS = {
    "lms": {
        "mode": "bearer",
        "entry_url": "https://lms.binus.ac.id/lms/dashboard",
        # After login, navigate here to FORCE the schedule API to fire.
        # Templated with current year-month at call time.
        "schedule_url_template": "https://lms.binus.ac.id/lms/schedule/{year}-{month}",
        "api_url_pattern": re.compile(r"func-bm7-.*\.azurewebsites\.net/api/"),
        "header_keys": ["Authorization", "rOId", "academicCareer", "institution", "roleName", "roleId"],
        "login_url_marker": "login.microsoftonline.com",  # refresh failed → bounced to MS login
    },
    "messier": {
        "mode": "cookie",
        # Home.aspx, NOT Login.aspx — Login.aspx triggers a broken ASP.NET
        # cookie-detection redirect that 404s at root /Login.aspx.
        # Home.aspx unauthenticated triggers the app's own (working) login redirect.
        "entry_url": "https://socs1.binus.ac.id/messier/Home.aspx",
        "refresh_url": "https://socs1.binus.ac.id/messier/Home.aspx",
        "login_url_marker": "Login.aspx",
        # Pre-set the ASP.NET cookie-support test cookie so the server
        # skips its broken detection redirect entirely.
        "preset_cookies": [
            {"name": "AspxAutoDetectCookieSupport", "value": "1",
             "domain": "socs1.binus.ac.id", "path": "/"},
        ],
    },
}
```

### `interactive_login(portal)` (headed)
1. Headed Playwright with a **fresh context** (no saved `storage_state` — stale cookies cause weird redirects, especially on Messier).
2. Apply `preset_cookies` if configured (Messier needs the ASPX cookie pre-set).
3. For `bearer` mode: install `page.on("request", ...)` listener filtering to `api_url_pattern`, capturing `header_keys` + `request.post_data`. Keep the LATEST match.
4. Open `entry_url`. Print "Press Enter when you've finished logging in" and `await asyncio.to_thread(input)`. Don't rely on URL polling — user knows best when login completes.
5. For `bearer` mode: navigate to the **schedule page** (`schedule_url_template` rendered with current year-month) — this is what fires the bearer API call. The dashboard alone often doesn't.
6. Wait up to 60s for the listener to capture a matching request.
7. Save `auth_state_<portal>.json`:
   - **bearer:** `{auth_mode, storage_state, bearer_token, token_exp, headers, post_body, captured_at, last_refresh_at}`
   - **cookie:** `{auth_mode, storage_state, captured_at, last_refresh_at}`
8. `chmod 600` on the file.

### `refresh(portal) -> bool` (headless)
- **bearer:** headless Playwright with saved `storage_state` + preset_cookies → listener installed → `page.goto(entry_url)` → if URL contains `login_url_marker` (e.g. `login.microsoftonline.com`), SSO cookies are dead → return False. Otherwise `page.goto(schedule_url)` to force the API call → capture fresh bearer + headers → update JSON → return True.
- **cookie:** headless Playwright with saved `storage_state` + preset_cookies → `page.goto(refresh_url)` → if final URL contains `login_url_marker` (`Login.aspx`) → False; else → save new `storage_state` → True.

### `load_creds(portal) -> dict`
Reads + returns the JSON file.

### `is_session_valid(portal) -> bool`
- **bearer:** JWT `exp` is at least 30 min in the future.
- **cookie:** `last_refresh_at` is within `MESSIER_REFRESH_INTERVAL_MIN`.

### CLI
```
python -m src.auth --portal=lms                # interactive login
python -m src.auth --portal=messier            # interactive login
python -m src.auth --portal=lms --refresh      # test headless refresh
python -m src.auth --portal=messier --refresh
python -m src.auth --portal=lms --check        # validity probe
python -m src.auth --portal=messier --check
```

**Deliverable:**
- `python -m src.auth --portal=lms` → log in → `auth_state_lms.json` written with `auth_mode: "bearer"` and all fields populated.
- `python -m src.auth --portal=messier` → log in → `auth_state_messier.json` written with `auth_mode: "cookie"`.
- `--refresh` succeeds for both without manual intervention.

**Acceptance:**
- Both files exist, `chmod 600`, gitignored.
- Both `--check` return `valid` after their respective `--refresh`.
- LMS refresh advances `token_exp` ~24 h forward.
- Messier refresh advances `last_refresh_at` to "now."

**Gotchas:**
- LMS listener may fire on multiple XHRs — keep the LATEST captured. Filter to URLs containing `/api/` to skip static assets.
- Microsoft SSO can show a "stay signed in?" prompt — answer Yes once so SSO cookies persist.
- Messier `Home.aspx` may render slowly; use `page.wait_for_load_state("networkidle")` before re-saving state.
- If Messier login form is wrapped by Next.js (per the `__Host-next-auth.csrf-token` cookie observed), expect the login URL to redirect — just rely on `wait_for_url` reaching `Home.aspx`.

---

## Phase 3 — Scraper interface + LMS schedule scraper (4–6 hours)

**Goal:** A clean `Scraper` ABC and a working `LMSScraper` that returns real schedule events using `httpx` + the saved bearer token.

**Tasks:**
1. `src/parser.py` — `Event` dataclass + `EventType`, `EventSource` literals.
2. `src/scrapers/base.py` — `Scraper` ABC: `async fetch(start: date, end: date) -> list[Event]`.
3. `src/scrapers/lms.py` — `LMSScraper`:
   - Class constants: `MONTH_API`, `DATE_API`, `DASHBOARD_URL` (see ARCHITECTURE.md).
   - In `fetch(start, end)`:
     - Load creds via `auth.load_creds("lms")`. If `not is_session_valid("lms")` → `await auth.refresh("lms")`.
     - Compute distinct months in `[start, end]` (typically 1–2 months).
     - For each month, `httpx.AsyncClient` POST to `MONTH_API/{YYYY-M-1}` with bearer + custom headers + saved `post_body`.
     - On 401: try one `auth.refresh("lms")`; retry. Still 401 → raise `SessionExpired`.
   - Parse: response is `[{dateStart, Schedule: [...]}, ...]`. Flatten all `Schedule` arrays.
   - Map each item by `scheduleType`:
     - `"Assignment"` (or `lamType == "ASG"`) → `assignment_deadline` (`start = customParam.dueDate`, `end = null`).
     - `"Onsite"`, `"Online"`, `"Virtual Class"` → `class` (`start = dateStart`, `end = dateEnd`, `location = location` if set).
     - `"Event"` → `other` (`start = dateStart`, `end = dateEnd`, `link = customParam.url`).
     - Unknown → log warning + `type = "other"`.
   - Filter to the requested date range. Sort by start time.
4. `src/scrapers/__init__.py` — `REGISTRY = {"lms": LMSScraper()}`.
5. CLI: `python -m src.scrapers.lms` — fetches today + next 30 days, pretty-prints.

**Deliverable:** CLI prints a real list of your classes for the next week.

**Acceptance:**
- Output matches what LMS shows in browser.
- IDs are deterministic across runs.
- All `start` values are tz-aware `Asia/Jakarta`.

**Gotchas:**
- Always `datetime.now(ZoneInfo("Asia/Jakarta"))`. Never naïve `datetime.now()`.
- ID: `sha1(f"{source}|{title}|{start.isoformat()}".encode()).hexdigest()[:12]`. Use LMS `scheduleId` in `notes` for traceability.
- URL format is `YYYY-M-D` without zero padding (`2026-5-10`, not `2026-05-10`). Use `f"{d.year}-{d.month}-{d.day}"`, not `d.strftime("%Y-%m-%d")`.
- Month-v1 takes the **first day of the month** in the URL (`2026-5-1`), not an arbitrary date. Always normalize: `month_start = date(d.year, d.month, 1)`.
- Days with no events are omitted from the Month-v1 response — don't expect placeholder empty days.

---

## Phase 4 — Persistence (1–2 hours)

**Goal:** Events survive process restart.

**Tasks:**
1. `src/db.py` with full schema (events, reminders_sent, sync_log, refresh_log, llm_log).
2. Implement all helpers including `backup_to(path)` via SQLite online-backup API.
3. Wire LMS scraper CLI to call `db.upsert_events()`. Print `synced N events (X new, Y updated)`.

**Acceptance:** Run scraper twice; first inserts, second is no-op. Portal edits propagate. No duplicates.

**Gotchas:** Parameterized queries only. `INSERT ... ON CONFLICT(id) DO UPDATE SET ...`. Store `remind_before` as `json.dumps(...)`.

---

## Phase 5 — Discord bot skeleton (3–4 hours)

**Goal:** Bot online; `/today`, `/week`, `/upcoming` work.

**Tasks:**
1. Discord Developer Portal → create app + Bot → copy token. **Minimum intents: `dm_messages`** (no Message Content Intent needed for DM-only bot).
2. Get your numeric Discord user ID + test guild ID → `.env`.
3. Invite bot with OAuth: scopes `bot` + `applications.commands`; permissions: Send Messages, Embed Links, Use Slash Commands.
4. `src/bot.py`:
   - `Bot` with `intents = discord.Intents.default(); intents.dm_messages = True`.
   - Sync slash commands to `DISCORD_GUILD_ID` on `on_ready`.
   - `/today`, `/week`, `/upcoming count:int [type:str]` — read DB, format embed with type icons (📚 class, 👨‍🏫 teaching, 📝 assignment, ✍️ correction, 💼 meeting, 📌 other).
5. `main.py`: configures `logging`, boots bot.

**Acceptance:** Bot online. `/today` returns embed matching `src.scrapers.lms` output.

**Gotchas:** Guild-scoped sync for instant updates during dev. Embed caps: 6000 chars total / 25 fields.

---

## Phase 6 — Sync loop + refresh job + backup job (2–3 hours)

**Goal:** Bot refreshes from every scraper every 15 min, auto-refreshes JWT every 20 hours, backs up DB daily.

**Tasks:**
1. `AsyncIOScheduler` on bot's event loop.
2. `sync_job` every `SYNC_INTERVAL_MIN`:
   - For each `name in ENABLED_SCRAPERS`, own try/except.
   - `await REGISTRY[name].fetch(today, today+30d)` → upsert → log.
   - On `SessionExpired` → DM "re-auth `python -m src.auth --portal=<name>`".
3. Two separate refresh jobs (different cadences):
   - `refresh_job_lms` every `LMS_REFRESH_INTERVAL_HOURS` (20): `await auth.refresh("lms")`. If False → DM user "Run `python -m src.auth --portal=lms`."
   - `refresh_job_messier` every `MESSIER_REFRESH_INTERVAL_MIN` (25): `await auth.refresh("messier")`. If False → DM user.
   - Both log to `refresh_log`.
4. `backup_job` daily at `BACKUP_HOUR_LOCAL`:
   - `db.backup_to(f"data/backup/events_{today}.db")`.
   - Delete backups older than 7 days.
5. `/status` shows: per-scraper last sync, per-portal token TTL + last refresh, total upcoming events.

**Acceptance:**
- `/status` shows recent successful syncs + refreshes.
- Break `auth_state_lms.json` (invalid token, valid cookies) → next scrape triggers refresh → recovers transparently.
- Daily backup creates a new file; 8-day-old backups are deleted.

**Gotchas:**
- Use `AsyncIOScheduler`, not `BackgroundScheduler`.
- One scraper's failure must not affect another.

---

## Phase 7 — Reminders (2–3 hours)

**Goal:** DM at the right time before each event.

**Tasks:**
1. `src/reminders.py`:
   - `schedule_for(event, scheduler)` — for each `lead_min`, `DateTrigger` at `event.start - lead`, job name `f"{event.id}:{lead_min}"`, `replace_existing=True`.
   - `reschedule_all(scheduler)` — wipe event-jobs, list upcoming events, re-add.
   - `send_reminder(event_id, lead_min)` — load, idempotency check, embed, DM, mark sent.
2. Call `reschedule_all` on boot + after each sync.
3. Embed: type icon + time-until + location + link.
4. Test: insert an event 2 min in the future via SQL → DM should arrive.

**Acceptance:** Reminders fire on time. Restart bot mid-day; already-scheduled reminders still fire, no dupes. Deleted events don't fire.

**Gotchas:** APScheduler jobs don't survive restart — `reschedule_all` on boot is the resilience. `reminders_sent` blocks dupes.

---

## Phase 8 — Manual events (`/add`, `/edit`, `/delete`) (2–3 hours)

**Goal:** Personal events not in any portal.

**Tasks:**
1. `/add` with `type` restricted to `meeting`/`other`, `start` as `YYYY-MM-DD HH:MM`.
2. `/delete id:<short_id>` — Discord button confirmation (Yes/No, 30s timeout). On Yes: `db.delete_event` + cancel scheduled jobs.
3. `/edit id:<short_id>` — modal with current values; on submit, upsert + reschedule.
4. Sync only touches `source IN ('lms', 'messier')`; never modifies `source='manual'`.

**Acceptance:** Manual add → reminder fires. Edit reschedules. Sync doesn't touch manual events.

---

## Phase 9 — Deploy to Oracle Cloud ARM VM (3–5 hours)

**Goal:** Bot runs 24/7 on Oracle, not your laptop.

**Tasks:**
1. Sign up at oracle.com/cloud/free.
2. Provision **Ampere A1 ARM VM**, Ubuntu 22.04, 4 OCPU / 24 GB / 200 GB. Retry off-peak if capacity error.
3. SSH; install Python 3.11, git, build tools.
4. Push repo to private GitHub; clone on VM.
5. venv + deps + `playwright install --with-deps chromium`.
6. `scp` `.env` + `auth_state_lms.json` to VM. `chmod 600 auth_state_*.json`.
7. Verify `python -m src.auth --portal=lms --check` on VM.
8. `systemd` unit `/etc/systemd/system/bot-timetable.service` (see ARCHITECTURE.md).
9. `sudo systemctl daemon-reload && sudo systemctl enable --now bot-timetable`.
10. `journalctl -u bot-timetable -f` to tail.

**Acceptance:** Reboot the VM. Bot comes back. Runs 48h+ with auto-refreshes working. Reminders fire on time.

**Gotchas:** Oracle ARM capacity constrained — retry. Playwright on ARM needs `--with-deps`.

---

## Phase 10 — Add Messier scraper (3–5 hours)

**Goal:** `teaching` + `correction_deadline` events flow in alongside LMS via the cookie-auth path.

**Prereq:** Phase 2 already extended `auth.py` to support cookie mode. `python -m src.auth --portal=messier` works and `auth_state_messier.json` exists.

**Tasks:**

### `src/scrapers/messier.py`
1. Implement `MessierScraper(Scraper)` with class constants:
   - `JOBS_API = "https://socs1.binus.ac.id/messier/Job.svc/GetActivesJob"`
   - `HOME_URL = "https://socs1.binus.ac.id/messier/Home.aspx"`
2. `async def fetch(self, start: date, end: date) -> list[Event]`:
   - `creds = auth.load_creds("messier")`
   - `cookies = {c["name"]: c["value"] for c in creds["storage_state"]["cookies"] if "binus.ac.id" in c["domain"]}`
   - `httpx.AsyncClient(cookies=cookies)` → POST to `JOBS_API` with body `{"type": "future"}` and headers `{"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json; charset=utf-8", "Referer": HOME_URL, "Origin": "https://socs1.binus.ac.id"}`.
   - If response has `Location` header pointing to `Login.aspx` (302) OR body content suggests login page → trigger `await auth.refresh("messier")`; retry once. Still failing → raise `SessionExpired`.
   - Parse `response.json()["d"]` → `list[Event]`.

### Parser logic (per [MESSIER_Requirement.md](MESSIER_Requirement.md))
3. Implement `parse_aspnet_date(s)` helper.
4. For each `job`:
   - Skip if normalized `status == "done"`.
   - `start = parse_aspnet_date(job["StartDate"])` (Teaching/Proctor) OR `parse_aspnet_date(job["EndDate"])` (Marking — deadline).
   - Skip if `start < today` or `start > end_date`.
   - Map `JobType`:
     - `"Teaching"` / `"Exam Proctor"` → `type="teaching"`
     - `"Marking"` → `type="correction_deadline"`
     - Unknown → log warning, default `type="other"`
   - Parse `Description` for course name, class code, room, session number.
   - Build title per the format suggestions.
   - `id = sha1(f"messier|{job['Note']}|{start.isoformat()}".encode()).hexdigest()[:12]`.
   - `Event(source="messier", ...)`.

### Register + deploy
5. `src/scrapers/__init__.py`: `REGISTRY = {"lms": LMSScraper(), "messier": MessierScraper()}`.
6. Confirm `.env ENABLED_SCRAPERS=lms,messier` on local + VM.
7. Add `refresh_job_messier` (every `MESSIER_REFRESH_INTERVAL_MIN`) to `main.py`'s scheduler — separate from `refresh_job_lms` because frequencies differ.
8. CLI test: `python -m src.scrapers.messier` prints today's teaching + corrections.
9. Restart bot. `/status` should show two scrapers + two refresh jobs.
10. `scp auth_state_messier.json` to VM (`chmod 600`), restart `bot-timetable` service.

**Deliverable:** `/today` shows merged LMS + Messier events in chronological order. `/upcoming type:teaching` filters correctly.

**Acceptance:**
- Messier scraper runs every 15 min on its own schedule.
- Breaking `auth_state_lms.json` does NOT stop Messier syncs (and vice versa).
- All four auto-types (`class`, `teaching`, `assignment_deadline`, `correction_deadline`) receive correct reminders.
- Done items (Status=Done) are excluded.
- Unknown `JobType` logged as warning + saved as `type=other` (doesn't crash).
- Substitute jobs are correctly detected from the `(Substitute)` prefix in `Description`.

**Gotchas:**
- ASP.NET date format: `re.compile(r"^/Date\((-?\d+)[+-]\d+\)/$")` — the `-?` matters for negative dates (year-9999 sentinel computes as positive but be defensive).
- `Status` normalization: `s.strip().replace(" ", "").lower()` before comparing.
- The `Id` field is ALL ZEROS — never use it; use the composite SHA1 from `Note` + `StartDate`.
- Marking's `LatestDate` is `253370739600000` (year 9999) — IGNORE this; use `EndDate`.
- If `MESSIER_REFRESH_INTERVAL_MIN=25` proves too long (random 302s during scrape), drop to 15 min.

---

## Phase 11 — Local LLM setup + read-only chat (4–6 hours)

**Goal:** Install Ollama + Qwen on the VM, build `src/llm.py` with **structured outputs (JSON Schema enforcement)**, ship query-only chat with keep-warm.

**Tasks:**

### Install Ollama + Qwen on Oracle VM
1. `curl -fsSL https://ollama.com/install.sh | sh` (creates `ollama.service`).
2. `ollama pull qwen2.5:3b-instruct` (~2 GB).
3. Set `OLLAMA_KEEP_ALIVE=24h` env var in `/etc/systemd/system/ollama.service.d/override.conf` so model stays loaded.
4. Test: `ollama run qwen2.5:3b-instruct "hello"`.

### Build `src/llm.py`
5. Define `INTENT_SCHEMA` JSON Schema (see ARCHITECTURE.md).
6. `extract_intent(text)` — Ollama call with `"format": INTENT_SCHEMA` (not `"json"`) so the model **cannot** return wrong-shape JSON.
7. `summarize_events(question, events)` — second Ollama call, free-form text.
8. `ping()` — `/api/tags` probe.
9. `keep_warm()` — issue a 1-token generation to keep model resident.
10. System prompt injects `today`, `weekday`, **and `current_time`** dynamically.
11. Wrap all calls in `asyncio.wait_for(..., timeout=LLM_TIMEOUT_SEC)`. Raise `LLMError` on timeout.
12. CLI test: `python -m src.llm "anything saturday?"` prints intent JSON.

### Wire chat into bot
13. `on_message` handler in `src/bot.py`:
    - Ignore non-DM, non-user, slash-prefix.
    - Show typing indicator. If LLM >2s, post `🤔 thinking…`.
    - `intent = await llm.extract_intent(text)`.
    - Dispatch on `action`:
      - `query` → `db.list_events` → embed reply.
      - `create` → defer to Phase 12.
      - `summarize` → defer to Phase 13.
      - `unknown` → fallback hint.
14. `/ask <question>` slash wraps the same handler.
15. `keepwarm_job` registered in `main.py` every `OLLAMA_KEEP_ALIVE_MIN` (4).
16. Log every chat call to `llm_log`.
17. `/status` adds "LLM: ✅ qwen2.5:3b (avg 2.3s)".
18. Test extensively in Indonesian AND English.

**Acceptance:**
- DM "anything saturday?" → real DB listing.
- DM "ada apa hari sabtu?" → same in Indonesian.
- Stopping `ollama` service → chat replies "AI offline, use slash commands." Slash commands + reminders unaffected.
- After 30 min of inactivity, chat is still fast (<3s) due to keep-warm.

**Gotchas:**
- Cold first call after model unload: 5–8s. Keep-warm prevents this.
- Qwen sometimes adds markdown fences around JSON — JSON-Schema mode prevents this entirely. Use it.
- For Indonesian: test "sabtu", "minggu", "besok", "lusa", "minggu depan".

---

## Phase 12 — Chat: create event with button confirmation (3–4 hours)

**Goal:** "add meeting tomorrow 9am with John" creates a manual event after explicit **Save** button click.

**Tasks:**
1. On `intent["action"] == "create"`:
   - Build proposed `Event(source="manual", ...)`.
   - Reply with embed showing parsed event + `discord.ui.View` containing **Save** + **Cancel** buttons. Timeout 5 min.
2. Button callbacks:
   - **Save** → `db.upsert_events([event])` + `reminders.schedule_for(event)` + edit message to show "Saved ✓ (id: `...`)". Disable buttons.
   - **Cancel** → edit message to "Cancelled." Disable buttons.
   - **Timeout** → disable buttons, append "Expired."
3. Never save without explicit click.
4. Only `meeting` or `other` types accepted (LLM schema enforces this).
5. Test cases (both languages):
   - "add meeting with John tomorrow at 9"
   - "remind me to call mom friday 7pm"
   - "ada meeting besok jam 10 sama dosen"
   - Edge: missing time → bot replies "couldn't parse a time, try with HH:MM".

**Acceptance:**
- ≥5 distinct natural phrasings work end-to-end.
- Bot never saves without explicit Save click.
- Stale buttons (>5 min) don't save anything when clicked.
- Created event's reminder fires correctly.

**Gotchas:**
- Use `discord.ui.View` with `timeout` parameter, NOT manual `wait_for(reaction)`.
- LLM `start` field is ISO without TZ → localize to `Asia/Jakarta` before saving.

---

## Phase 13 — Chat: summarize / analyze (2–3 hours)

**Goal:** "summarize my week" / "how busy is tomorrow?" generates a grounded summary.

**Tasks:**
1. On `intent["action"] == "summarize"`:
   - `events = db.list_events(date_from, date_to)`.
   - If empty → skip LLM, reply "Nothing scheduled for that range."
   - If >50 events → truncate or pre-aggregate.
   - Build lightweight dicts (type, title, start, end, location).
   - `text = await llm.summarize_events(user_question, events)`.
   - Reply with `text` + footer "Based on N events in your schedule."
2. The summarize system prompt explicitly says "do NOT invent events."

**Acceptance:**
- Summaries mention only events that exist in the DB.
- Empty range → no LLM call, fast deterministic reply.
- Language matches user's question.

---

## Phase 14 (optional) — Quality of life

Once Phases 0–13 are solid:
- Snooze button on reminders (Discord button → reschedule for now + 10 min).
- `/dismiss id` to skip a recurring instance.
- Weekly proactive summary every Sunday evening DM.
- `.ics` export endpoint for iPhone calendar.
- Grade/score notifications (new Messier hook).
- Conflict detection (`/today` flags overlapping events).
- Upgrade to Qwen 2.5 7B (`ollama pull qwen2.5:7b-instruct` + change `LLM_MODEL`).
- Microsoft Graph fallback (if Outlook calendar proves to have richer data).

---

## Definition of done — overall v1

v1 is **done** when, for one full week:
- All classes (LMS) + teaching (Messier) appear automatically.
- All assignment + correction deadlines have 24h + 1h reminders.
- All reminders fire within 30s of target.
- ≥3 manual events added (via `/add` AND via chat-create) reminded on time.
- Chat answers ≥5 query questions and ≥1 summarize correctly, no fabricated events.
- **JWT auto-refreshed transparently — no manual re-auth in the test week.**
- Bot runs continuously on Oracle ARM for 7+ days.
- Zero duplicate or false reminders.
- LLM offline does not break slash commands or reminders.
