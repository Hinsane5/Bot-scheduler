# LMS Requirement

This document records what the project needs from Binus LMS, what has already
been discovered, and what is still missing before the LMS scraper can be built.

## Goal

Use Binus LMS as the source for student-side events:

- `class`: classes attended as a student.
- `assignment_deadline`: assignment due dates.

The scraper should convert LMS schedule items into the shared `Event` model used
by the Discord bot.

## Discovered Endpoint

LMS schedule and assignment items appear to come from the same date-based
schedule endpoint.

```text
POST https://func-bm7-schedule-prod.azurewebsites.net/api/Schedule/Date-v1/{year}-{month}-{day}
```

Example:

```text
POST https://func-bm7-schedule-prod.azurewebsites.net/api/Schedule/Date-v1/2026-5-23
POST https://func-bm7-schedule-prod.azurewebsites.net/api/Schedule/Date-v1/2026-5-10
```

The endpoint appears to return schedule entries for one specific date. The
scraper may need to call it once per day for the requested date range unless a
range endpoint is discovered.

## Request Requirements

### Method

```text
POST
```

### Required Headers Seen

```text
User-Agent: Mozilla/5.0 (...)
Accept: application/json, text/plain, */*
Accept-Language: en-US,en;q=0.9
Content-Type: application/json
Referer: https://lms.binus.ac.id/
Origin: https://lms.binus.ac.id
rOId: 3273c3c7-a4db-467b-ac3b-74c295766470
academicCareer: RS1
institution: BNS01
roleName: Student
roleId: 4bcb81bd-46a8-4a09-a923-5e812cb7007b
Authorization: Bearer <token>
```

Important: the real bearer token is a sensitive credential. Do not store it in
this file or commit it to git. It should be loaded from the authenticated LMS
browser/session state.

The captured cURL did not show cookies being sent to the schedule API. The
critical auth material appears to be the `Authorization: Bearer <token>` header
plus the role headers and JSON body.

### Request Body

The endpoint receives a `roleActivity` array.

```json
{
  "roleActivity": [
    {
      "name": "Student",
      "userCode": "2802505821",
      "roleId": "4bcb81bd-46a8-4a09-a923-5e812cb7007b",
      "roleType": "student",
      "roleOrganizationId": "3273c3c7-a4db-467b-ac3b-74c295766470",
      "academicCareerId": "d98ce516-1068-4f75-8324-63587cc631f0",
      "academicCareer": "RS1",
      "academicCareerDesc": "Undergraduate",
      "institution": "BNS01",
      "institutionDesc": "BINUS University",
      "academicProgram": "ABCSC",
      "academicProgramDesc": "Computer Science",
      "academicGroup": "SOCS",
      "academicGroupDesc": "School of Computer Science",
      "isPrimary": true,
      "isActive": true
    },
    {
      "name": "Student",
      "userCode": "2802505821",
      "roleId": "4bcb81bd-46a8-4a09-a923-5e812cb7007b",
      "roleType": "student",
      "roleOrganizationId": "8e5baaef-aee1-4413-95a8-49cdc41967d4",
      "academicCareerId": "d98ce516-1068-4f75-8324-63587cc631f0",
      "academicCareer": "RS1",
      "academicCareerDesc": "Undergraduate",
      "institution": "BNS01",
      "institutionDesc": "BINUS University",
      "isPrimary": false,
      "isActive": false
    }
  ]
}
```

## Response Shape

When the date has schedule data, the response is JSON.

```json
{
  "dateStart": "2026-05-23T09:20:00",
  "Schedule": []
}
```

Schedule entries include both normal class meetings and assignment items.

When the date has no class or assignment deadline, the endpoint returns:

```text
HTTP 204 No Content
```

The scraper should treat `204` as "no events for this date", not as a failure.

## Class Schedule Data We Have

Example class item:

```json
{
  "dateStart": "2026-05-23T07:20:00",
  "dateEnd": "2026-05-23T09:00:00",
  "title": "BO01 - LAB",
  "content": "Computer Vision",
  "location": "Alam Sutera Main Campus - A1601",
  "locationValue": "A1601",
  "scheduleType": "Onsite",
  "customParam": {
    "classId": "ff410c5e-3a4b-415a-bd35-e3e541e38747",
    "classSessionId": "e2171cc2-1e28-4fd7-a926-0aa1f31c65bb",
    "sessionNumber": "10"
  },
  "deliveryMode": "F2F",
  "deliveryModeDesc": "Face To Face",
  "academicPeriod": "2520",
  "scheduleId": "ee157c52-49da-403e-942e-b14ba125897c"
}
```

### Class Mapping To Event

| LMS field | Event field |
| --- | --- |
| `scheduleId` | Stable LMS identifier, used to build `Event.id` |
| `content` + `title` | `title` |
| `dateStart` | `start` |
| `dateEnd` | `end` |
| `location` | `location` |
| Online meeting field, if present | `link` |
| `customParam.sessionNumber`, `deliveryModeDesc` | `notes` |
| LMS | `source = "lms"` |
| Class item | `type = "class"` |

Example normalized event:

```json
{
  "source": "lms",
  "type": "class",
  "title": "Computer Vision - BO01 - LAB",
  "start": "2026-05-23T07:20:00+07:00",
  "end": "2026-05-23T09:00:00+07:00",
  "location": "Alam Sutera Main Campus - A1601",
  "link": null,
  "notes": "Session 10, Face To Face"
}
```

## Assignment Deadline Data We Have

Assignment items are returned from the same schedule endpoint with:

- `scheduleType = "Assignment"`
- `lamType = "ASG"`
- `assessmentActivity` populated
- `customParam.dueDate` and `customParam.dueDateUtc` populated

Example assignment item:

```json
{
  "dateStart": "2026-05-10T22:20:01",
  "dateEnd": "2026-05-21T23:59:00",
  "title": "LP01 - LEC",
  "content": "Computational Biology",
  "location": null,
  "locationValue": null,
  "scheduleType": "Assignment",
  "lamType": "ASG",
  "assessmentActivity": "THEORY: ASSIGNMENT",
  "customParam": {
    "classId": "f0d133b2-7991-4b97-879d-f1313a63fee7",
    "courseCode": "SCIE6062001",
    "classNumber": "19416",
    "classSessionId": "8a437370-1cdf-47dd-989c-7aea1a2741e4",
    "sessionNumber": "9",
    "classSessionContentId": "ed47c198-592d-4a46-8db2-4dd6d04076e9",
    "title": "Assignment Sesi 9",
    "assessmentId": "ed590b70-ba15-46ef-a953-32b7859beee3",
    "assessmentType": "authentic",
    "classCode": "LP01",
    "courseTitleEn": "Computational Biology",
    "ssrComponent": "LEC",
    "dueDate": "2026-05-21T23:59:00",
    "dueDateUtc": "2026-05-21T16:59:00Z"
  },
  "classDeliveryMode": "GSLC",
  "deliveryMode": "GSLC",
  "deliveryModeDesc": "Guided Self Learning Class",
  "academicPeriod": "2520",
  "scheduleId": "036dd348-dead-430a-b747-7fea9c02c5a7"
}
```

### Assignment Mapping To Event

| LMS field | Event field |
| --- | --- |
| `customParam.assessmentId` or `scheduleId` | Stable LMS identifier, used to build `Event.id` |
| `customParam.title` + `content` | `title` |
| `customParam.dueDate` or `dateEnd` | `start` |
| `null` | `end` |
| Assignment detail URL, if discovered | `link` |
| `assessmentActivity`, `sessionNumber`, `courseCode` | `notes` |
| LMS | `source = "lms"` |
| Assignment item | `type = "assignment_deadline"` |

Example normalized event:

```json
{
  "source": "lms",
  "type": "assignment_deadline",
  "title": "Assignment Sesi 9 - Computational Biology",
  "start": "2026-05-21T23:59:00+07:00",
  "end": null,
  "location": null,
  "link": null,
  "notes": "THEORY: ASSIGNMENT, Session 9, SCIE6062001"
}
```

## Detection Rules

Recommended classification:

- If `scheduleType == "Assignment"` or `lamType == "ASG"`, create an
  `assignment_deadline` event.
- Otherwise, create a `class` event for normal schedule items.

Recommended assignment deadline time:

1. Prefer `customParam.dueDate`.
2. Fallback to `dateEnd`.
3. If `dueDateUtc` is used, convert it to `Asia/Jakarta`.

Recommended class title:

```text
{content} - {title}
```

Recommended assignment title:

```text
{customParam.title} - {content}
```

## What We Still Need

### Full Copy As cURL

Captured. Sanitized shape:

```text
curl 'https://func-bm7-schedule-prod.azurewebsites.net/api/Schedule/Date-v1/2026-5-10' \
  --compressed \
  -X POST \
  -H 'Accept: application/json, text/plain, */*' \
  -H 'Content-Type: application/json' \
  -H 'Referer: https://lms.binus.ac.id/' \
  -H 'rOId: 3273c3c7-a4db-467b-ac3b-74c295766470' \
  -H 'academicCareer: RS1' \
  -H 'institution: BNS01' \
  -H 'roleName: Student' \
  -H 'roleId: 4bcb81bd-46a8-4a09-a923-5e812cb7007b' \
  -H 'Origin: https://lms.binus.ac.id' \
  -H 'Authorization: Bearer <token>' \
  --data-raw '{"roleActivity":[...]}'
```

Do not commit the real bearer token.

### Auth Token Source

The raw bearer token was not found directly in session storage or local storage.

Search results:

- `sessionStorage`: no matching `eyJ`, `token`, or `auth` values.
- `localStorage`: one matching key, `persist:lms`.

`persist:lms` contains an `auth` field whose value starts with:

```text
U2FsdGVkX1...
```

This is not the raw bearer token. It appears to be an encrypted auth blob
because `U2FsdGVkX1` is the Base64 prefix for `Salted__`, commonly seen in
CryptoJS/OpenSSL-style encrypted values.

Current conclusion:

- LMS stores encrypted auth data in `localStorage["persist:lms"].auth`.
- The frontend decrypts or resolves that auth data before sending API requests.
- The schedule API itself receives the final JWT in the request header:
  `Authorization: Bearer eyJ...`.

For implementation, the most reliable path is likely Playwright:

1. Save authenticated browser state after manual login.
2. Open LMS with that state.
3. Let the LMS frontend make the schedule request.
4. Intercept the outgoing request and reuse the final `Authorization` header,
   role headers, and request body.

Alternative path if needed:

- Find the frontend code that decrypts `persist:lms.auth`.
- Reimplement the decrypt/token extraction logic in Python.

The Playwright interception path is safer and less coupled to LMS internals.

### Token Lifetime

The pasted JWT has an expiry claim, but implementation still needs runtime
behavior confirmed:

- Does the token remain valid after browser restart?
- Does the LMS frontend refresh it automatically?
- What does the API return when the token expires?

### Date Range Behavior

Confirm whether LMS has a weekly/monthly/range endpoint. If not, the scraper
will call `/Date-v1/{year}-{month}-{day}` once per day.

### Online Class Sample

Current class samples are face-to-face. Capture one online or hybrid class item
to see where LMS stores the meeting link.

### Assignment Detail Link

The assignment sample includes IDs but no direct detail URL. Capture what
happens when opening an assignment detail page so the bot can include a clickable
assignment link in reminders.

### Empty Day Response

Known behavior:

- `HTTP 204 No Content`

The scraper should return an empty event list for that date.

### Error Responses

Capture or observe responses for:

- expired token
- missing authorization
- invalid date

This helps the scraper distinguish "no events" from "auth expired".

## Files To Save Later

The docs expect sample responses to be saved under `data/`:

```text
data/sample_response_lms_schedule.json
data/sample_response_lms_assignments.json
```

These should contain sanitized sample JSON responses without bearer tokens or
private cookies.
