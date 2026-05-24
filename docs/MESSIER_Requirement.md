# Messier Requirement

This document records what the project needs from Binus Messier (SOCS asisten portal), what has been discovered, and what is still missing before the Messier scraper can be built.

## Goal

Use Binus Messier as the source for asisten-side events:
- `teaching`: lectures/lab sessions I deliver as asisten (and `Exam Proctor` duties).
- `correction_deadline`: marking/grading deadlines.

The scraper converts Messier "jobs" into the shared `Event` model.

## Discovered Endpoint

```text
POST https://socs1.binus.ac.id/messier/Job.svc/GetActivesJob
```

**A single endpoint returns BOTH teaching and correction (marking) jobs** in one feed — discriminated by the `JobType` field on each item. Much simpler than LMS, which mixes types within a single schedule response.

## Request Requirements

### Method
```text
POST
```

### Required Headers
```text
X-Requested-With: XMLHttpRequest
Content-Type: application/json; charset=utf-8
Referer: https://socs1.binus.ac.id/messier/Home.aspx
Origin: https://socs1.binus.ac.id
Cookie: .ASPXAUTH=<COOKIE>; (others)
```

### Request Body
```json
{"type": "future"}
```
- `"future"` returns upcoming jobs.
- Other values (`"all"`, `"past"`?) — TBD.

## Authentication

**Cookie-based, NOT bearer token.** Backend is classic ASP.NET WCF (`Job.svc`).

- Auth cookie: `.ASPXAUTH=...` (encrypted server-side ticket).
- Session lifetime: **sliding expiry** — each request bumps the timeout forward. Absolute max unknown (TBD).
- Login flow: `https://socs1.binus.ac.id/messier/Login.aspx` → manual login → redirects to `Home.aspx` (start of authenticated session).
- After auth, `.ASPXAUTH` is auto-included by the browser on all `/messier/` requests.

Other cookies seen but not critical for auth: `AspxAutoDetectCookieSupport`, `__Host-next-auth.csrf-token`, `__Secure-next-auth.callback-url`, `TS0166f5d1` (anti-bot), various Google Analytics cookies.

Important: the real `.ASPXAUTH` value is a credential. Do not store in this file or commit it to git. Captured browser state lives only in `auth_state_messier.json` (gitignored).

## Response Shape

WCF JSON wrapper:
```json
{"d": [<ClientJob>, ...]}
```

Each `ClientJob`:
```json
{
  "__type": "ClientJob:#Messier.Model",
  "Category": "Lecture",
  "Description": "COSC6092001-Code Reengineering BH01  A1301 11",
  "EndDate": "/Date(1780653600000+0700)/",
  "FileNote": null,
  "Id": "00000000-0000-0000-0000-000000000000",
  "IsSubstitute": false,
  "JobType": "Teaching",
  "LatestDate": "/Date(1780592400000+0700)/",
  "Note": "1ab02f00-3f01-f111-a1d2-9440c921bcaf",
  "StartDate": "/Date(1780647600000+0700)/",
  "Status": "Not Done",
  "SubstituteSchedule": false,
  "User": "HW25-2"
}
```

## Quirks the scraper MUST handle

### 1. ASP.NET classic date format
`/Date(<ms>+<tzoffset>)/` — milliseconds since UNIX epoch (UTC) + TZ offset (informational).

```python
import re
from datetime import datetime
from zoneinfo import ZoneInfo

ASPNET_DATE = re.compile(r"^/Date\((-?\d+)[+-]\d+\)/$")

def parse_aspnet_date(s: str) -> datetime:
    m = ASPNET_DATE.match(s)
    ms = int(m.group(1))
    return datetime.fromtimestamp(ms / 1000, tz=ZoneInfo("Asia/Jakarta"))
```

### 2. `Id` is always all-zeros — DO NOT use as identifier
`"00000000-0000-0000-0000-000000000000"` for every item. Build a deterministic fingerprint instead:
```python
event_id = sha1(f"messier|{note}|{start.isoformat()}".encode()).hexdigest()[:12]
```
The `Note` field is a stable internal schedule GUID — usable as the unique component.

### 3. `Status` is inconsistent
Values seen: `"NotDone"`, `"Not Done"` (with space), `"Done"`. Always normalize before comparing:
```python
status = job["Status"].strip().replace(" ", "").lower()  # "notdone" | "done"
```

### 4. `IsSubstitute` flag is unreliable
Substitute jobs have `IsSubstitute: false` AND `"(Substitute)"` prefix in `Description`. Trust the **prefix**, not the flag.

### 5. `LatestDate` sentinel for Marking jobs
`"/Date(253370739600000+0700)/"` → year 9999. Means "no upper deadline" — **ignore this value** for Marking. Use `EndDate` as the real deadline.

### 6. Filter `Done` jobs
Items with normalized `status == "done"` are completed — exclude from auto-sync to avoid stale reminders.

## JobType → EventType mapping

| `JobType`      | → `Event.type`         | Time source                              | Notes                                  |
|----------------|------------------------|------------------------------------------|----------------------------------------|
| `Teaching`     | `teaching`             | `StartDate` (start), `EndDate` (end)     | Real class session                     |
| `Exam Proctor` | `teaching`             | `StartDate` (start), `EndDate` (end)     | Same shape; add "Exam Proctor" to notes|
| `Marking`      | `correction_deadline`  | `EndDate` (deadline); ignore `LatestDate`| Grading task                           |

If an unknown `JobType` is encountered: log a warning and fall back to `other`.

## Field mapping (Messier → Event)

| Messier field                              | Event field          |
|--------------------------------------------|----------------------|
| (composite SHA1)                           | `id`                 |
| literal `"messier"`                        | `source`             |
| `JobType` → mapped per table above         | `type`               |
| parsed from `Description`                  | `title`              |
| `StartDate` (Teaching/Proctor) / `EndDate` (Marking) | `start`     |
| `EndDate` (Teaching/Proctor) / `null` (Marking) | `end`          |
| extracted room from `Description` (e.g. `A1301`) | `location`     |
| `null` (Messier exposes no meeting URL)    | `link`               |
| `JobType` + session info + `(Substitute)` flag + `Note` | `notes`  |

### Description parsing

Format observed:
```
{courseCode}-{courseName} {classCode}  {room} {sessionNumber}
```
Examples:
- `"COSC6092001-Code Reengineering BH01  A1301 11"` → courseName=`Code Reengineering`, classCode=`BH01`, room=`A1301`, session=`11`
- `"COMP6048001-Data Structures L401  A1301 33"` → session=`33`
- `"(Substitute) COSC6093001-Software Architecture BH01  A1301 6"` → `IsSubstitute` (from prefix), session=`6`
- `"COSC6100001-Cloud Infrastructure Project BA01 -LA01"` (Marking) → no session number; `BA01` is class code

Suggested title formats:
- Teaching: `"{courseName} {classCode} — session {sessionNumber}"`
- Exam Proctor: `"{courseName} {classCode} (Exam Proctor)"`
- Marking: `"Mark {classCode} — {courseName}"`

## Example normalized events

**Teaching:**
```json
{
  "source": "messier",
  "type": "teaching",
  "title": "Code Reengineering BH01 — session 11",
  "start": "2026-06-05T07:00:00+07:00",
  "end": "2026-06-05T08:40:00+07:00",
  "location": "A1301",
  "link": null,
  "notes": "Teaching · session 11"
}
```

**Exam Proctor:**
```json
{
  "source": "messier",
  "type": "teaching",
  "title": "Code Reengineering BH01 (Exam Proctor)",
  "start": "2026-06-04T14:20:00+07:00",
  "end": "2026-06-04T16:00:00+07:00",
  "location": "A1301",
  "link": null,
  "notes": "Exam Proctor"
}
```

**Marking:**
```json
{
  "source": "messier",
  "type": "correction_deadline",
  "title": "Mark BA01 — Cloud Infrastructure Project",
  "start": "2026-06-18T17:00:00+07:00",
  "end": null,
  "location": null,
  "link": null,
  "notes": "Marking · COSC6100001-Cloud Infrastructure BA01"
}
```

## Login + refresh flow

- **Initial login:** `python -m src.auth --portal=messier` → headed Playwright opens `Login.aspx` → user logs in → lands on `Home.aspx` → script saves `storage_state` (all cookies) to `auth_state_messier.json`.
- **Refresh:** every ~25 minutes (TBD per `.ASPXAUTH` measurement), headless Playwright loads cookies → `page.goto("https://socs1.binus.ac.id/messier/Home.aspx")` → sliding session resets.
- **On 302-redirect-to-Login.aspx during scrape** → session dead → DM user with `python -m src.auth --portal=messier` instruction.

## What we still need

- **`.ASPXAUTH` absolute lifetime** — needs measurement (leave idle for various durations, observe when it dies). Drives `MESSIER_REFRESH_INTERVAL_MIN`.
- **Other body values for `GetActivesJob`** — `"all"`, `"past"`? Useful for backfilling history.
- **Confirm:** does Messier share LMS login (Microsoft SSO), or is it fully separate? Different cookies suggest separate sessions; verify on next interactive login.
- **More `JobType` values** in the wild (Mentoring, Workshop, …) — log unknowns as `other`.
- **Online-class meeting link** — Messier doesn't expose one in `ClientJob`. Confirm whether asisten classes are F2F-only, or whether the link lives on a different page.

## Files To Save Later

```text
data/sample_response_messier_jobs.json    # full GetActivesJob response, sanitized
```

Sanitized = strip the cookie header, keep response body as-is (no credentials in body).
