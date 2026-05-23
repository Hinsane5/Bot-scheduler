# PRD — Personal Activities Bot

## Overview
A Discord-based personal assistant that auto-syncs my entire academic life from Binus portals (classes, teaching duties, assignment deadlines, correction deadlines), lets me add personal events manually, and **answers natural-language questions about my schedule using a local AI model** (no paid APIs). It DMs me before each event.

## Problem
I'm a Binus student AND an asisten (teaching assistant), so my schedule lives in too many places:
- **Binus LMS** — my own classes, my assignment deadlines.
- **Binus Messier (SOCS portal)** — my teaching/asisten schedule, correction deadlines.
- **Zoom invites** in email.
- **Personal commitments** I track nowhere consistent.

None push notifications. None export to a calendar. And even after I have them all in one place, asking "what do I have on Saturday?" requires me to look it up myself — friction I want to remove.

## Goal
A single Discord bot, running 24/7 on a free Oracle ARM VM, that:
1. Auto-syncs every 15 minutes from **both** Binus LMS and Messier.
2. Accepts manually-added personal events via slash commands.
3. DMs me at a configurable lead time before each event.
4. **Answers schedule questions in natural language** ("anything Saturday?", "next class?", "summarize my week") using a local Qwen LLM — no internet API calls, no paid keys.

## Non-goals (v1)
- Multi-user support — single-user tool, my Discord account only.
- WhatsApp / Telegram delivery — Discord only.
- Mobile or web UI.
- Two-way sync (bot writing back to school portals).
- Replacing my main calendar app.
- General-purpose chatbot (off-topic chat, jokes, etc.) — the AI is scoped to schedule tasks only.

## Users
- **Me** — sole user. Binus student + SOCS asisten. Comfortable running Python locally.

## Event taxonomy

| Type                  | Description                                         | Source         | Auto / Manual           |
|-----------------------|-----------------------------------------------------|----------------|-------------------------|
| `class`               | Classes I attend as a student                       | Binus LMS      | Auto                    |
| `teaching`            | Sessions I teach as asisten                         | Messier (SOCS) | Auto                    |
| `assignment_deadline` | Assignments I owe as a student                      | Binus LMS      | Auto                    |
| `correction_deadline` | Submissions I must grade as asisten                 | Messier (SOCS) | Auto                    |
| `meeting`             | Personal/work meetings                              | —              | Manual via `/add` or chat |
| `other`               | Anything else                                       | —              | Manual via `/add` or chat |

> **Note on sources:** the above mapping is my best guess. Phase 1 recon confirms which portal hosts which data type.

## AI / Chatbot capability

A **local LLM** (Qwen 2.5 3B Instruct via Ollama, ~2 GB RAM) gives the bot natural-language understanding without paid APIs or internet dependency.

### Core principle: LLM extracts intent, code executes
The LLM **never** returns event data directly. It only translates user messages into structured JSON intents. Python then runs the actual DB query or builds the event. This makes it impossible for the LLM to hallucinate an event into existence.

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
2. **Create with confirmation** — "add meeting with John tomorrow at 9am". Bot replies with a card showing the parsed event; user reacts ✅ to save or ❌ to cancel. Never saves without confirmation.
3. **Summarize** — "summarize my week", "how busy is tomorrow?". LLM is called twice: once to extract date range, once to write a 2–4 sentence summary grounded in the DB results (no inventing events).

### Out of chat scope (intentional)
- General conversation / jokes / off-topic — bot replies "I only handle schedule questions."
- Editing or deleting existing events — must use `/edit` or `/delete` slash commands for safety.
- Multi-turn conversation memory — each message is independent (simpler, more predictable).

## User stories
1. As a student, I want classes from Binus LMS to appear in the bot automatically.
2. As an asisten, I want my teaching schedule from Messier to appear automatically.
3. As a student, I want assignment deadlines from LMS to show up with 24h + 1h reminders.
4. As an asisten, I want correction deadlines from Messier to show up with 24h + 1h reminders.
5. As a user, I want a DM 10 minutes before a class or teaching session starts, with title, room/Zoom link.
6. As a user, I want `/add` for explicit personal events and `/today`, `/week`, `/upcoming` for explicit listing.
7. As a user, I want to DM the bot "anything saturday?" and get a real answer from my actual schedule.
8. As a user, I want to DM the bot "add meeting tomorrow 9am with John" and have it parse + confirm before saving.
9. As a user, I want to ask "summarize my week" and get a useful narrative based on real events.
10. As a user, I want `/edit` and `/delete` to work on manually-added events; auto-synced events refresh themselves.
11. As a user, when a portal session expires I want a DM saying which portal needs re-auth.
12. As a user, when the local LLM is overloaded or slow, I want the bot to still answer slash commands instantly (LLM should not block deterministic commands).
13. As a user, I should never receive a duplicate reminder for the same event.

## Functional requirements

### Auto-sync (multi-source)
- Every 15 minutes, run all enabled scrapers (LMS, Messier).
- Each scraper independent — failure in one does not block the other.
- Upsert by stable fingerprint = `sha1(source + title + start)[:12]`.
- Edits on portals propagate on next sync.
- Events that vanish from a portal stay 24h, then are pruned.

### Manual events (slash commands)
```
/add type:<meeting|other>
     title:<string>
     start:<YYYY-MM-DD HH:MM>
     [end:<YYYY-MM-DD HH:MM>]
     [link:<url>]
     [location:<string>]
     [remind_before:<minutes, comma-separated>]
     [notes:<string>]
```
- Auto-synced types (`class`, `teaching`, deadlines) cannot be added manually — only via portal sync.

### Chatbot (natural language)
- **Triggers:** `/ask <question>` slash command, OR any free-text DM to the bot (no slash prefix).
- **Routing:**
  - Slash commands (`/today`, `/add`, etc.) → deterministic Python, never touch LLM.
  - Free-text / `/ask` → LLM intent extractor → action handler.
- **Latency budget:** ≤8 seconds end-to-end for query/create, ≤15 seconds for summarize. Bot replies with `🤔 thinking…` immediately if LLM call exceeds 2s.
- **Fallback:** if LLM is offline or returns `unknown`, bot replies with a polite suggestion to use slash commands.
- **Language:** LLM responds in the same language as the user's question (Indonesian or English).

### Listing
- `/today`, `/week`, `/upcoming count:int [type:str]` — across all sources.

### Editing
- `/edit id:<short_id>`, `/delete id:<short_id>` — manual events only.

### Reminders
- Default lead times by type:
  - `class`, `teaching`, `meeting`, `other`: `[10]`
  - `assignment_deadline`, `correction_deadline`: `[1440, 60]`
- Overridable per event.
- Discord embed format with type icons (📚 class, 👨‍🏫 teaching, 📝 assignment, ✍️ correction, 💼 meeting, 📌 other).
- Idempotent: a `(event_id, lead_min)` pair never fires twice.

### Health
- `/status` — per-scraper sync status, session ages, total upcoming events, **LLM status** (Ollama reachable Y/N, model loaded, average response time).

## Non-functional requirements
- **Cost:** $0/month. No paid APIs.
- **Reliability:** Reminders fire within 30s of target. Sync failure must NOT block reminders for known events. LLM failure must NOT block slash commands or reminders.
- **Security:** Credentials in `.env`. Session files gitignored. No data leaves the VM — local LLM means no prompt is sent to any external API.
- **Privacy:** Personal data stays local. No third-party data sharing.
- **Runtime:** Single Python process + Ollama daemon. Single SQLite file. ~3 GB RAM total (bot + LLM model loaded).
- **Always-on:** Runs 24/7 on Oracle Cloud free-tier ARM VM. Local dev acceptable for testing.

## Success criteria
v1 ships when, for **one full week**:
- All my classes (LMS) appear automatically.
- All my teaching sessions (Messier) appear automatically.
- All assignment + correction deadlines appear with 24h + 1h reminders.
- I receive correct reminders for every event.
- I have added ≥3 manual events successfully and received their reminders.
- I have successfully used the chatbot for: at least 5 query questions, 1 create-with-confirm flow, 1 summarize request — all returning correct, grounded answers.
- No bot restarts needed.
- Zero duplicate or false reminders.
- Bot runs continuously on Oracle ARM VM.

## Open questions (resolved during build)
- **Phase 1:** Does LMS expose JSON XHRs? Does Messier? Shared SSO? Session lifetimes?
- **Phase 11:** Does Qwen 2.5 3B reliably return valid JSON intents on Indonesian inputs at >90% accuracy? If not, fall back to Qwen 2.5 7B (~5GB RAM, still fits Oracle ARM).
- **Phase 11:** Does Ollama on ARM CPU give acceptable latency (<5s typical)? If not, switch to llama.cpp server with more aggressive quantization.

## Out of scope for v1 — candidates for v2
- WhatsApp / Telegram as alternative delivery channels.
- Google Calendar two-way sync.
- iCal export for iPhone import.
- Web dashboard.
- Snooze / dismiss inline buttons on reminders.
- Grade/score notifications (Messier publishes a grade → DM).
- Multi-turn chat memory (currently each message is independent).
- LLM-driven event editing or deletion (too risky — kept as slash commands).
