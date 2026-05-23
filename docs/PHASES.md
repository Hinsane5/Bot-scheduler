# Build Phases

Each phase has: **Goal**, **Tasks**, **Deliverable**, **Acceptance**, **Gotchas**. Do not start a phase until the previous one's acceptance criteria pass.

Total estimated effort: ~35–45 hours over 3 weeks of evenings (extra time vs original scope because of Messier + local LLM + chat features).

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
5. Create directory skeleton (see ARCHITECTURE.md), including `src/scrapers/`.
6. `.env.example` + `.env` (placeholders).
7. `.gitignore` per ARCHITECTURE.md.
8. `git init && git add . && git commit -m "Phase 0: project skeleton"`.

**Deliverable:** `python -c "import discord, playwright, httpx, bs4, apscheduler, dotenv; print('ok')"` prints `ok`.

**Acceptance:** Command above succeeds. `.env` exists, gitignored.

**Gotchas:** `playwright install` is ~150 MB. Set `TIMEZONE=Asia/Jakarta` explicitly.

---

## Phase 1 — Reconnaissance: BOTH portals (2–4 hours, no code)

**Goal:** Know exactly which URL to hit and what shape it returns, for **both** LMS and Messier.

**Tasks for LMS (`https://lms.binus.ac.id`):**
1. Chrome → log in → DevTools (F12) → Network → filter `Fetch/XHR`.
2. Navigate to schedule/timetable AND assignment list. Capture each:
   - URL, method, headers, cookies, sample response body.
3. Save samples to `data/sample_response_lms_schedule.json` + `data/sample_response_lms_assignments.json`.

**Tasks for Messier (`https://socs1.binus.ac.id/messier`):**
1. Same drill for teaching schedule + corrections.
2. Save to `data/sample_response_messier_teaching.json` + `data/sample_response_messier_corrections.json`.
3. **Critical:** does logging into LMS auto-log into Messier (shared SSO)?

**Write `RECON.md`** with: data source URLs per portal, login flow, field mappings → `Event`, shared-session Y/N.

**Acceptance:** All sample files captured; `RECON.md` answers every question in PRD's Open Questions.

**Gotchas:** Interact with pages fully to trigger lazy XHRs. If Messier shows nothing now (semester break), test past dates.

---

## Phase 2 — Authenticated sessions per portal (2–3 hours)

**Goal:** Per-portal interactive login that saves a reusable session file.

**Tasks:**
1. `src/auth.py` with `PORTALS = {"lms": ..., "messier": ...}`, `interactive_login(portal)`, `load_state(portal)`, `is_session_valid(portal)`.
2. CLI: `python -m src.auth --portal=lms` / `--portal=messier` / `--check`.

**Deliverable:** Both `auth_state_lms.json` + `auth_state_messier.json` exist; `--check` returns `valid` for both.

**Acceptance:** State persists across process restarts.

**Gotchas:** MFA prompts complete inside headed browser before pressing Enter.

---

## Phase 3 — Scraper interface + LMS scraper (4–6 hours)

**Goal:** Clean `Scraper` ABC + working `LMSScraper` returning real events.

**Tasks:**
1. `src/parser.py` with `Event` dataclass + `EventType` / `EventSource` literals.
2. `src/scrapers/base.py` with `Scraper` ABC.
3. `src/scrapers/lms.py` with `LMSScraper.fetch(start, end)` returning `class` + `assignment_deadline` events.
4. `src/scrapers/__init__.py` with `REGISTRY = {"lms": LMSScraper()}`.
5. CLI: `python -m src.scrapers.lms` pretty-prints today.

**Deliverable:** CLI prints LMS events matching what the portal shows.

**Acceptance:** ≥7 days into the future returned; re-running is deterministic.

**Gotchas:** Always tz-aware (`Asia/Jakarta`). Deterministic IDs from SHA1.

---

## Phase 4 — Persistence (1–2 hours)

**Goal:** Events survive process restart.

**Tasks:**
1. `src/db.py` with the schema from ARCHITECTURE.md (includes `llm_log` table — empty for now).
2. Implement all DB helpers including `log_sync(scraper, ...)` and `prune_stale(scraper, ...)`.
3. Wire LMS scraper CLI to call `db.upsert_events()` + print summary.

**Deliverable:** Run scraper twice; first inserts, second is no-op.

**Acceptance:** No duplicates; portal edits propagate after re-sync.

**Gotchas:** Parameterized queries only. `INSERT ... ON CONFLICT` for upsert.

---

## Phase 5 — Discord bot skeleton (3–4 hours)

**Goal:** Bot online; `/today`, `/week`, `/upcoming` work.

**Tasks:**
1. Discord Developer Portal → create bot → token in `.env`. Enable "Message Content Intent."
2. Get your Discord user ID + test guild ID into `.env`.
3. Invite bot to private test server.
4. `src/bot.py` with guild-scoped slash command sync + `/today`, `/week`, `/upcoming count:int [type:str]` using type icons.
5. `main.py` boots the bot.

**Deliverable:** `/today` returns clean embed from DB.

**Acceptance:** Bot online; output matches scraper CLI.

**Gotchas:** Always guild-scoped sync during dev. Embeds capped at 6000 chars / 25 fields.

---

## Phase 6 — Auto-sync loop, multi-scraper-ready (1–2 hours)

**Goal:** Bot refreshes from every enabled scraper every 15 min.

**Tasks:**
1. `AsyncIOScheduler` on the bot's event loop.
2. `sync_job` iterates `ENABLED_SCRAPERS` (initially `["lms"]`), each in own try/except.
3. On `SessionExpired` → DM re-auth instruction. On other errors → DM + log.
4. `/status` shows per-scraper last sync.

**Acceptance:** `/status` shows recent successful LMS syncs. Breaking `auth_state_lms.json` triggers the DM; restoring it recovers.

**Gotchas:** `AsyncIOScheduler`, not `BackgroundScheduler`. One scraper's failure must not affect others.

---

## Phase 7 — Reminders (2–3 hours)

**Goal:** Get a DM at the right time before each event.

**Tasks:**
1. `src/reminders.py` with `schedule_for`, `reschedule_all`, `send_reminder`.
2. Call `reschedule_all` on bot boot + after each sync.
3. Embed with type icon, time-until, location, link.
4. Test by inserting an event 2 min in the future.

**Acceptance:** Restart bot — already-scheduled reminders still fire, no duplicates. Deleted events don't fire.

**Gotchas:** APScheduler jobs don't survive restart — `reschedule_all` on boot is the resilience. `reminders_sent` prevents dupes.

---

## Phase 8 — Manual events: `/add`, `/edit`, `/delete` (2–3 hours)

**Goal:** Personal events (meetings, ad-hoc) not in any portal.

**Tasks:**
1. `/add` with `type` restricted to `meeting`/`other`, validated datetime string.
2. `/delete id` with confirmation reaction; cancels jobs.
3. `/edit id` via Discord modal.
4. Sync only touches `source IN ('lms', 'messier')`; never modifies `source='manual'`.

**Acceptance:** Manual add → reminder fires. `/edit` reschedules. Sync doesn't touch manual events.

**Gotchas:** No native datetime picker — `YYYY-MM-DD HH:MM` with clear validation.

---

## Phase 9 — Deploy to Oracle Cloud ARM VM (3–5 hours)

**Goal:** Bot runs 24/7 on free Oracle VM, not your laptop.

**Tasks:**
1. Sign up at oracle.com/cloud/free.
2. Provision **Ampere A1 ARM VM**, Ubuntu 22.04, 4 OCPU / 24 GB / 200 GB. (Retry at off-peak hours if capacity error.)
3. SSH in. Install Python 3.11, git, build tools.
4. Push repo to private GitHub. Clone on VM.
5. venv + `pip install -r requirements.txt` + `playwright install --with-deps chromium`.
6. `scp` `.env` + `auth_state_*.json` to VM. Verify `python -m src.auth --portal=lms --check` returns `valid`.
7. Create `systemd` unit `/etc/systemd/system/bot-timetable.service`:
   ```ini
   [Unit]
   Description=Bot Timetable
   After=network.target

   [Service]
   Type=simple
   User=ubuntu
   WorkingDirectory=/home/ubuntu/Bot-Timetable
   ExecStart=/home/ubuntu/Bot-Timetable/.venv/bin/python main.py
   Restart=always
   RestartSec=10

   [Install]
   WantedBy=multi-user.target
   ```
8. `sudo systemctl daemon-reload && sudo systemctl enable --now bot-timetable`.
9. `journalctl -u bot-timetable -f` to tail.

**Deliverable:** Reboot the VM. Bot comes back automatically. `/status` works within 30s.

**Acceptance:** Bot runs continuously for 48h+ on Oracle. Reminders fire on time. Re-auth flow (locally re-run `auth.py`, `scp` to VM, restart service) works.

**Gotchas:**
- Oracle ARM capacity is constrained — be persistent.
- Playwright on ARM: `--with-deps` is required.
- Bot is outbound-only; don't expose ports.

---

## Phase 10 — Add Messier scraper (3–5 hours)

**Goal:** Teaching + correction-deadline events flow in automatically alongside LMS.

**Tasks:**
1. `src/scrapers/messier.py` with `MessierScraper.fetch(start, end)` returning `teaching` + `correction_deadline` events.
2. Add to `REGISTRY`. Update `.env` `ENABLED_SCRAPERS=lms,messier` (local + VM).
3. CLI test on local: `python -m src.scrapers.messier`.
4. Restart bot. `/status` shows two scrapers.
5. `scp auth_state_messier.json` + new code to VM. Restart service.

**Deliverable:** `/today` shows merged LMS + Messier events in chronological order.

**Acceptance:** Both scrapers run independently every 15 min. Breaking one doesn't break the other. All 4 auto-types have correct reminders.

**Gotchas:** Even if same SSO, Messier session lifetime may differ. Document it.

---

## Phase 11 — Local LLM setup + read-only chat (4–6 hours)

**Goal:** Install Ollama + Qwen on the VM, build `src/llm.py`, ship the query-only chat interface.

**Tasks:**

### Install Ollama + Qwen on Oracle VM
1. SSH to VM. Install Ollama:
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```
   The installer creates `ollama.service` (systemd) automatically. Verify:
   ```bash
   systemctl status ollama
   curl http://localhost:11434/api/tags    # should return JSON
   ```
2. Pull the model:
   ```bash
   ollama pull qwen2.5:3b-instruct
   ```
   ~2 GB download. Test:
   ```bash
   ollama run qwen2.5:3b-instruct "hello"
   ```
3. Verify RAM: `free -h` — should still have >20 GB free with model loaded.

### Build `src/llm.py`
4. Implement `ping()`, `extract_intent(text)`, `summarize_events(question, events)` per ARCHITECTURE.md.
5. Use Ollama's `format: "json"` for `extract_intent`. Pass `{today, weekday}` into the system prompt dynamically.
6. Wrap calls in `asyncio.wait_for(..., timeout=LLM_TIMEOUT_SEC)`. Raise `LLMError` on timeout/parse failure.
7. CLI test: `python -m src.llm "anything saturday?"` prints the extracted intent JSON.

### Wire chat into bot
8. In `src/bot.py`, add `on_message` handler:
   - Ignore non-DM, non-self-user, or messages starting with `/`.
   - Show `🤔 thinking...` (typing indicator) if LLM takes >2s.
   - Call `llm.extract_intent(message.content)`.
   - If `action == "query"`: `db.list_events(...)`, format embed reply.
   - If `action == "summarize"`: defer to Phase 13.
   - If `action == "create"`: defer to Phase 12.
   - If `action == "unknown"`: hint at slash commands.
9. Add `/ask <question>` slash command that wraps the same handler (useful in non-DM contexts).
10. Add `/status` field: "LLM: ✅ qwen2.5:3b (avg 2.3s)" using `llm_log` table.
11. Log every chat call to `llm_log` (user msg, intent JSON, latency, error).
12. Local test extensively in Indonesian AND English: "anything saturday?", "ada apa hari sabtu?", "next class?", "kelas besok jam berapa?".

**Deliverable:** DMing the bot "anything saturday?" returns a real listing from the DB. Same for "ada apa hari sabtu?".

**Acceptance:**
- LLM returns valid JSON for ≥90% of common query phrasings.
- Slash commands still work instantly even when LLM is mid-call (test by spamming `/today` during a chat call).
- Stopping `ollama` service → chat replies "AI offline, use slash commands." Slash commands + reminders unaffected.

**Gotchas:**
- Cold first call is slow (~5–8s) — model loads into RAM. Subsequent calls ~2–4s.
- Qwen sometimes adds markdown fences around JSON — `format: "json"` mode should prevent this; if not, strip ```` ``` ```` before parsing.
- Date arithmetic in the prompt: pass `today` AND `weekday` so the LLM doesn't have to compute "this saturday."
- For Indonesian inputs, test month names ("juni", "agustus") and weekday names ("sabtu", "minggu").

---

## Phase 12 — Chat: create event with confirmation (3–4 hours)

**Goal:** "add meeting tomorrow 9am with John" creates a manual event after explicit ✅ confirmation.

**Tasks:**
1. Extend chat handler in `src/bot.py`:
   - On `intent["action"] == "create"`: build a proposed `Event(source="manual", ...)`.
   - Reply with embed showing parsed event + "React ✅ to save, ❌ to cancel."
   - Use `bot.wait_for("reaction_add", timeout=60.0, check=...)`.
   - On ✅: `db.upsert_events([event])` + `reminders.schedule_for(event)` + reply "Saved ✓ (id: `...`)".
   - On ❌ or timeout: reply "Cancelled."
2. **Never** save without confirmation. **Never** auto-create event types other than `meeting` or `other`.
3. Test cases (both languages):
   - "add meeting with John tomorrow at 9"
   - "remind me to call mom friday 7pm"
   - "ada meeting besok jam 10 sama dosen"
   - Edge: missing time → bot replies "I couldn't parse a time — try again with HH:MM."

**Deliverable:** End-to-end create flow works for at least 5 distinct natural phrasings.

**Acceptance:**
- Bot never saves an event without explicit ✅ reaction.
- Parsed event card shows exactly what will be saved (no surprises).
- Reminder for the created event fires correctly.
- Wrong parse → user clicks ❌ → nothing saved → user can rephrase.

**Gotchas:**
- Reaction handling is async; ensure the embed message ID is correctly captured.
- Time zone: LLM returns naive datetime — convert to `Asia/Jakarta` before saving.
- If user reacts ✅ on a stale card (>60s), ignore.

---

## Phase 13 — Chat: summarize / analyze (2–3 hours)

**Goal:** "summarize my week" / "how busy is tomorrow?" generates a natural-language summary grounded in real DB data.

**Tasks:**
1. Extend chat handler: on `intent["action"] == "summarize"`:
   - `events = db.list_events(date_from, date_to)`.
   - Convert to lightweight dicts (type, title, start, end, location) to keep prompt small.
   - `text = await llm.summarize_events(user_question, events)`.
   - Reply with `text` + footer "Based on N events in your schedule."
2. If `len(events) == 0`: skip the LLM call, reply "Nothing scheduled for that range." (Save compute.)
3. If `len(events) > 50`: truncate or pre-aggregate (avoid blowing context).
4. Test:
   - "summarize my week"
   - "ringkasin minggu ini dong"
   - "how busy is tomorrow?"
   - "do I have free time on saturday afternoon?"

**Deliverable:** Summaries are accurate (no fabricated events), helpful (mention patterns), and brief (2–4 sentences).

**Acceptance:**
- Generated text mentions only events actually present in the DB result.
- Empty range → no LLM call, fast deterministic reply.
- Indonesian question → Indonesian reply; English question → English reply.

**Gotchas:**
- The summarize prompt explicitly says "do NOT invent events." Verify in testing.
- For long event lists, sort by start time so the LLM gets temporal context.

---

## Phase 14 (optional) — Quality of life

Pick once Phases 0–13 are solid:
- Snooze button on reminders (Discord button → reschedule for now + 10 min).
- `/dismiss id` to skip a recurring class instance.
- Weekly proactive summary every Sunday evening DM.
- `.ics` export endpoint for iPhone calendar.
- Grade/score notifications (new Messier hook).
- Conflict detection (`/today` flags overlapping events).
- Per-channel routing (corrections → `#grading`, classes → DM).
- Upgrade to Qwen 2.5 7B if 3B accuracy proves insufficient (just `ollama pull qwen2.5:7b-instruct` + change `LLM_MODEL`).

---

## Definition of done — overall v1

v1 is **done** when, for one full week:
- All classes (LMS) + teaching (Messier) appear automatically in `/today` / `/week`.
- All assignment + correction deadlines have 24h + 1h reminders.
- All event reminders fire within 30s of target.
- ≥3 manual events added successfully (via `/add` AND via chat-create) and reminded on time.
- Chat answers ≥5 query questions and ≥1 summarize correctly, with zero fabricated events.
- Bot has run continuously on Oracle ARM for 7+ days without manual restart.
- Zero duplicate or false reminders.
- LLM offline state does not break slash commands or reminders.
