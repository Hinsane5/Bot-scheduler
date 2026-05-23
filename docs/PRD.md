# PRD — Personal Activities Bot

## Overview
A Discord-based personal assistant that auto-syncs my entire academic life from Binus portals (classes, teaching duties, assignment deadlines, correction deadlines), lets me add personal events manually, and **answers natural-language questions about my schedule using a local AI model** (no paid APIs). It DMs me before each event.

## Problem
I'm a Binus student AND an asisten (teaching assistant), so my schedule lives in too many places:
- **Binus LMS** — my own classes, my assignment deadlines.
- **Binus Messier (SOCS portal)** — my teaching/asisten schedule, correction deadlines.
- **Zoom invites** in email.
- **Personal commitments** I track nowhere consistent.

None push notifications. None export to a calendar. Even after I have them all in one place, asking "what do I have on Saturday?" requires me to look it up myself.

## Goal
A single Discord bot, running 24/7 on a free Oracle ARM VM, that:
1. Auto-syncs every 15 minutes from **both** Binus LMS and Messier.
2. Accepts manually-added personal events via slash commands.
3. DMs me at a configurable lead time before each event.
4. **Answers schedule questions in natural language** ("anything Saturday?", "next class?", "summarize my week") using a local Qwen LLM — no internet API calls, no paid keys.

## Non-goals (v1)
- Multi-user support.
- WhatsApp / Telegram delivery.
- Mobile / web UI.
- Two-way sync (bot writing back to portals).
- Replacing my main calendar app.
- General-purpose chatbot.

## Users
- **Me** — Binus student (NIM 2802505821) + SOCS asisten. Comfortable running Python locally.

## Event taxonomy

| Type                  | Description                                         | Source         | Auto / Manual |
|-----------------------|-----------------------------------------------------|----------------|---------------|
| `class`               | Classes I attend as a student                       | Binus LMS      | Auto          |
| `teaching`            | Sessions I teach as asisten                         | Messier        | Auto          |
| `assignment_deadline` | Assignments I owe as a student                      | Binus LMS      | Auto          |
| `correction_deadline` | Submissions I must grade as asisten                 | Messier        | Auto          |
| `meeting`             | Personal/work meetings                              | —              | Manual        |
| `other`               | Anything else                                       | —              | Manual        |

## AI / Chatbot capability

A **local LLM** (Qwen 2.5 3B Instruct via Ollama, ~2 GB RAM) gives natural-language understanding without paid APIs or internet dependency.

### Core principle: LLM extracts intent, code executes
The LLM **never** returns event data directly. It only translates user messages into structured JSON intents (validated against a JSON Schema via Ollama's structured-output mode). Python runs the actual DB query or builds the event.

```
User: "is there anything on saturday?"
  ↓
LLM extracts: {"action": "query", "date_from": "2026-05-30", "date_to": "2026-05-30"}
  ↓
Python: db.list_events(...)
  ↓
Python formats reply with real DB data
```

### Chat capabilities in v1
1. **Query** (read-only) — "anything saturday?", "next class?", "deadlines this week?".
2. **Create with confirmation** — "add meeting with John tomorrow at 9am". Bot replies with a card + **Save / Cancel buttons** (5-min timeout). Never saves without explicit click.
3. **Summarize** — "summarize my week". LLM is grounded in DB results.

### Out of chat scope (intentional)
- General conversation / jokes / off-topic.
- Editing or deleting existing events (must use `/edit` / `/delete` for safety).
- Multi-turn conversation memory.

## User stories
1. As a student, classes from Binus LMS appear in the bot automatically.
2. As an asisten, my teaching schedule from Messier appears automatically.
3. As a student, assignment deadlines from LMS show up with 24h + 1h reminders.
4. As an asisten, correction deadlines from Messier show up with 24h + 1h reminders.
5. I get a DM 10 minutes before a class or teaching session, with link/room.
6. I use `/add` for explicit personal events and `/today`, `/week`, `/upcoming` for explicit listing.
7. I DM the bot "anything saturday?" and get a real answer from my actual schedule.
8. I DM the bot "add meeting tomorrow 9am with John" — it parses, shows a card, I click **Save** to confirm.
9. I ask "summarize my week" and get a useful narrative grounded in real events.
10. `/edit` and `/delete` work on manually-added events; auto-synced events refresh themselves.
11. **I log in to LMS once (per ~quarter), and the bot auto-refreshes my session token in the background. I never have to manually re-auth on a daily basis.**
12. When a portal session truly expires (SSO cookies aged out), I get a DM telling me which portal needs re-auth.
13. When the LLM is overloaded, slash commands still answer instantly.
14. I never receive a duplicate reminder.

## Functional requirements

### Auto-sync (multi-source)
- Every 15 minutes, run all enabled scrapers (LMS, Messier).
- Each scraper independent — failure in one does not block another.
- Upsert by stable fingerprint = `sha1(source + title + start)[:12]`.
- Edits on portals propagate on next sync.
- Events that vanish from a portal stay 24h, then are pruned.

### Auto-refresh JWT (LMS, Messier)
- Bearer JWT lifetime is ~24 hours, but Microsoft SSO cookies last 30–90 days.
- A **background refresh job** runs every ~20 hours: headless Playwright loads saved SSO cookies → navigates to portal → silently receives a fresh JWT → bot captures and stores it.
- User intervention required **only when SSO cookies themselves expire** (~quarterly).

### Manual events (slash commands)
```
/add type:<meeting|other>
     title:<string>
     start:<YYYY-MM-DD HH:MM>
     [end:<YYYY-MM-DD HH:MM>]
     [link:<url>] [location:<string>]
     [remind_before:<minutes, comma-separated>]
     [notes:<string>]
```
- Only `meeting` / `other` accepted manually; auto-synced types come from portals.

### Chatbot (natural language)
- **Triggers:** `/ask <question>` slash command, OR any free-text DM to the bot.
- **Routing:** slash commands stay deterministic; only free-text/`/ask` goes through LLM.
- **Latency budget:** ≤8s for query/create, ≤15s for summarize. Bot replies with `🤔 thinking…` (typing indicator) if LLM exceeds 2s.
- **Fallback:** if LLM is offline or unsure → reply with slash-command hints.
- **Language:** LLM responds in the same language as the user's question.
- **Confirmation:** create operations show a card with **Save / Cancel buttons**, 5-min timeout. Never auto-saves.

### Listing
- `/today`, `/week`, `/upcoming count:int [type:str]` — across all sources.

### Editing
- `/edit id:<short_id>`, `/delete id:<short_id>` — manual events only.

### Reminders
- Default lead times by type:
  - `class`, `teaching`, `meeting`, `other`: `[10]`
  - `assignment_deadline`, `correction_deadline`: `[1440, 60]`
- Overridable per event.
- Discord embed with type icons.
- Idempotent: a `(event_id, lead_min)` pair never fires twice.

### Health
- `/status` — per-scraper sync status, session ages (SSO cookies + current JWT TTL), **LLM status** (Ollama reachable, model loaded, avg response time).

## Non-functional requirements
- **Cost:** $0/month. No paid APIs.
- **Reliability:** Reminders fire within 30s of target. Sync failure must NOT block reminders for known events. LLM failure must NOT block slash commands or reminders.
- **Security:** Credentials in `.env`. Session/credential files gitignored. **No data leaves the VM** — local LLM means no prompt is sent to any external API.
- **Privacy:** Personal data stays local.
- **Runtime:** Single Python process + Ollama daemon. Single SQLite file. ~3 GB RAM total.
- **Always-on:** Runs 24/7 on Oracle Cloud free-tier ARM VM. Local dev acceptable for testing.
- **Observability:** Python `logging` to stdout (picked up by systemd journald). Per-action timing in `sync_log`, `llm_log`. Daily SQLite backup to `data/backup/`.

## Success criteria
v1 ships when, for **one full week**:
- All classes (LMS) appear automatically.
- All teaching sessions (Messier) appear automatically.
- All assignment + correction deadlines have 24h + 1h reminders.
- Correct reminders for every event.
- ≥3 manual events successfully added and reminded on time.
- Chatbot answered ≥5 query questions, ≥1 create-with-confirm, ≥1 summarize correctly.
- **JWT auto-refresh ran transparently for the full week — no manual re-auth needed.**
- No bot restarts.
- Zero duplicate or false reminders.
- Bot runs continuously on Oracle ARM.

## Confirmed during Phase 1 recon (May 2026)

### LMS — schedule endpoint
- **URL:** `POST https://func-bm7-schedule-prod.azurewebsites.net/api/Schedule/Date-v1/YYYY-M-D` (one call per date)
- **Auth:** `Authorization: Bearer <JWT>` (24h lifetime, issued by `BinusServices`)
- **Custom headers required:** `rOId`, `academicCareer`, `institution`, `roleName`, `roleId`
- **POST body:** `{"roleActivity": [...]}` containing user role context
- **Frontend:** `lms.binus.ac.id` (React + Redux, `redux-persist` with CryptoJS-encrypted localStorage at `persist:lms`)
- **Login flow:** `lms.binus.ac.id` → Microsoft SSO → lands at `binusmaya.binus.ac.id` → user (or our bot) navigates to `lms.binus.ac.id/lms/dashboard` → frontend obtains JWT

### Still TBD (continuing Phase 1)
- LMS assignment-deadline endpoint (probably a different `func-bm7-*-prod.azurewebsites.net` subdomain).
- Messier teaching + correction endpoints.
- Whether Messier shares the LMS JWT or issues its own.
- Whether `outlook.office.com/calendar` auto-receives Binus schedule (could simplify entire project via Microsoft Graph API).

## Out of scope for v1 — candidates for v2
- WhatsApp / Telegram delivery channels.
- Microsoft Graph / Outlook calendar integration (deferred unless confirmed beneficial).
- Google Calendar two-way sync.
- iCal export for iPhone.
- Web dashboard.
- Snooze / dismiss inline buttons on reminders.
- Grade/score notifications.
- Multi-turn chat memory.
- LLM-driven event editing or deletion.
- Upgrade to Qwen 2.5 7B if 3B accuracy proves insufficient (drop-in via `LLM_MODEL` env var).
