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

## Phase 1 — Reconnaissance (mostly DONE, finishing up)

**Status from May 2026 recon:**

✅ **LMS schedule endpoint — CONFIRMED:**
- `POST https://func-bm7-schedule-prod.azurewebsites.net/api/Schedule/Date-v1/YYYY-M-D`
- Auth: `Authorization: Bearer <JWT>` (24h lifetime, BinusServices issuer)
- Custom headers: `rOId`, `academicCareer`, `institution`, `roleName`, `roleId`
- POST body: `{"roleActivity": [...]}` with user-specific role context
- Sample saved to `data/sample_response_lms_schedule.json`

✅ **Auth flow — CONFIRMED:**
- Login → Microsoft SSO → `binusmaya.binus.ac.id` → manually nav to `lms.binus.ac.id/lms/dashboard` → frontend issues JWT to `func-bm7-*`.
- localStorage `persist:lms` is CryptoJS-encrypted (we bypass by capturing token from network instead of decrypting).

✅ **Refresh strategy — DESIGNED:**
- SSO cookies (Microsoft + Binusmaya) live 30–90 days.
- Headless Playwright `refresh_job` loads cookies, navigates, captures new JWT.

❌ **Still TBD:**
- **LMS assignment endpoint** — probably a different `func-bm7-*-prod.azurewebsites.net` subdomain. Find by navigating to assignment/to-do page in DevTools.
- **Messier teaching endpoint** — capture XHR on `socs1.binus.ac.id/messier/`.
- **Messier correction endpoint** — same drill on a correction-deadline page.
- **Messier auth:** does it share the LMS JWT, or issue its own?
- **(Optional) Outlook calendar** — does `outlook.office.com/calendar` auto-receive Binus schedule? If yes, Microsoft Graph is a cleaner option.

**Tasks:**
1. Navigate LMS → assignments/to-do page. DevTools → Network → capture an assignment XHR. Save to `data/sample_response_lms_assignments.json`. Note URL, headers, body.
2. Log in to Messier. Capture teaching schedule + correction-deadline XHRs. Save samples. Note whether the bearer token matches the LMS one.
3. (Optional, 2 min) Check Outlook calendar.
4. Update `RECON.md` with all findings.

**Acceptance:** `RECON.md` answers all open questions. All sample payloads captured.

**Gotchas:** Capture as cURL via DevTools (right-click XHR → Copy → Copy as cURL) — preserves all headers.

---

## Phase 2 — Auth with token capture + refresh (4–6 hours)

**Goal:** `auth.py` that captures the bearer JWT, SSO cookies, custom headers, and POST body on first login. Includes a headless refresh path that silently re-mints the JWT using saved SSO cookies.

**Tasks:**

### `interactive_login(portal)`
1. Headed Playwright (`headless=False`).
2. `page.on("request", ...)` listener that filters requests to the portal's API base (e.g. `func-bm7-*.azurewebsites.net`). On match, capture:
   - `Authorization` header
   - All custom headers (`rOId`, `roleId`, `academicCareer`, `institution`, `roleName`)
   - `request.post_data` (the `roleActivity` JSON)
3. `page.goto(LMS_DASHBOARD_URL)`. SSO redirects user to login.
4. Wait for the user to log in (`input("Press Enter once dashboard is fully loaded...")` OR `page.wait_for_url(LMS_DASHBOARD_URL)`).
5. If page lands on `binusmaya.binus.ac.id` after login, `page.goto(LMS_DASHBOARD_URL)` to force the LMS to load.
6. Wait until the listener has captured a request to the API (with timeout).
7. Decode JWT to extract `exp` for `token_exp`.
8. Write `auth_state_<portal>.json` with:
   ```json
   {
     "storage_state": page.context.storage_state(),
     "bearer_token": "...",
     "token_exp": "...",
     "headers": {...},
     "post_body": {...},
     "captured_at": "...",
     "last_refresh_at": "..."
   }
   ```
9. `chmod 600` on the file.

### `refresh_token(portal) -> bool`
1. Headless Playwright (`headless=True`) with `storage_state` from saved file.
2. Same listener pattern as above.
3. `page.goto(LMS_DASHBOARD_URL)`. If frontend tries to redirect to login → SSO cookies dead → return False.
4. On captured XHR → extract fresh bearer + headers → update the JSON file → return True.

### `is_token_valid(portal, safety_margin_min=30) -> bool`
- Parses `token_exp`, returns True if expiry is at least `safety_margin_min` minutes in the future.

### `load_creds(portal) -> dict`
- Reads + returns the JSON file.

### CLI
```python
# python -m src.auth --portal=lms             # interactive
# python -m src.auth --portal=lms --refresh   # test headless refresh
# python -m src.auth --portal=lms --check     # validity probe
```

**Deliverable:** Run `python -m src.auth --portal=lms` → log in → `auth_state_lms.json` written. Then `python -m src.auth --portal=lms --refresh` → no UI, no prompts → new token captured silently.

**Acceptance:**
- After interactive login, file contains all required fields.
- `--refresh` succeeds without manual intervention, with `token_exp` advanced ~24h.
- `--check` returns `valid` immediately after login or refresh.
- File is `chmod 600`, gitignored.

**Gotchas:**
- The token capture listener can fire on multiple XHRs — keep the LATEST captured headers (frontend may issue several calls during dashboard load).
- Microsoft SSO can show a "stay signed in?" prompt — answer Yes once during interactive login so SSO cookies persist.
- Some `func-bm7-*` calls may not include the custom headers (e.g. asset/static endpoints). Filter for ones whose URL contains `/api/`.

---

## Phase 3 — Scraper interface + LMS schedule scraper (4–6 hours)

**Goal:** A clean `Scraper` ABC and a working `LMSScraper` that returns real schedule events using `httpx` + the saved bearer token.

**Tasks:**
1. `src/parser.py` — `Event` dataclass + `EventType`, `EventSource` literals.
2. `src/scrapers/base.py` — `Scraper` ABC: `async fetch(start: date, end: date) -> list[Event]`.
3. `src/scrapers/lms.py` — `LMSScraper`:
   - In `fetch`, load creds via `auth.load_creds("lms")`.
   - If `not is_token_valid("lms")` → `await auth.refresh_token("lms")`.
   - For each date in range:
     - `httpx.AsyncClient` POST to `LMS_API_BASE/api/Schedule/Date-v1/{date}` with bearer + custom headers + POST body.
     - If 401: try one refresh; retry; if still 401 → raise `SessionExpired`.
   - Parse JSON → `list[Event]` (only `class` for now; `assignment_deadline` added once recon completes).
   - Sort by start time.
4. `src/scrapers/__init__.py` — `REGISTRY = {"lms": LMSScraper()}`.
5. CLI: `python -m src.scrapers.lms` — fetches today + next 7 days, pretty-prints.

**Deliverable:** CLI prints a real list of your classes for the next week.

**Acceptance:**
- Output matches what LMS shows in browser.
- IDs are deterministic across runs.
- All `start` values are tz-aware `Asia/Jakarta`.

**Gotchas:**
- Always `datetime.now(ZoneInfo("Asia/Jakarta"))`. Never naïve `datetime.now()`.
- ID: `sha1(f"{source}|{title}|{start.isoformat()}".encode()).hexdigest()[:12]`.
- The endpoint expects URL format `YYYY-M-D` (no zero padding observed in your curl — `2026-5-10`, not `2026-05-10`). Verify with sample.

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
3. `refresh_job` every `REFRESH_INTERVAL_HOURS` (20):
   - For each portal, `await auth.refresh_token(portal)`. Log to `refresh_log`. If False → DM user.
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

**Goal:** Teaching + correction-deadline events flow in alongside LMS.

**Tasks:**
1. If Messier shares LMS JWT → `MessierScraper` reuses `auth.load_creds("lms")`.
2. If Messier issues its own JWT → run separate `interactive_login("messier")` + `refresh_token("messier")` paths.
3. `src/scrapers/messier.py` with `MessierScraper.fetch`.
4. Add to `REGISTRY`. Update `.env` `ENABLED_SCRAPERS=lms,messier` on local + VM.
5. CLI test, restart bot, deploy to VM.

**Acceptance:** `/today` shows merged LMS + Messier events. Each scraper independent. All 4 auto-types get correct reminders.

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
