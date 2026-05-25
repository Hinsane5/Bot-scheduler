"""Messier scraper."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from src import auth, config, db
from src.parser import Event, EventType
from src.scrapers.base import Scraper, SessionExpired


logger = logging.getLogger(__name__)

ASPNET_DATE = re.compile(r"^/Date\((-?\d+)[+-]\d+\)/$")
ROOM_RE = re.compile(r"\b[A-Z]\d{4}(?:-\d{2})?\b")
CLASS_CODE_RE = re.compile(r"\b[A-Z]{1,3}\d{2,3}\b")


class MessierScraper(Scraper):
    name = "messier"

    JOBS_API = "https://socs1.binus.ac.id/messier/Job.svc/GetActivesJob"
    HOME_URL = "https://socs1.binus.ac.id/messier/Home.aspx"
    LOGIN_URL = "https://socs1.binus.ac.id/messier/Login.aspx"

    async def fetch(self, start: date, end: date) -> list[Event]:
        """Fetch Messier events within the inclusive date window."""
        if end < start:
            raise ValueError("end must be on or after start")

        creds = await self._valid_creds()
        jobs = await self._fetch_jobs_with_creds(creds)
        events = [
            event
            for job in jobs
            if (event := self.parse_job(job)) is not None
            and start <= event.start.date() <= end
        ]
        events = _dedupe_events(events)
        events.sort(key=lambda event: event.start)
        return events

    async def _valid_creds(self) -> dict[str, Any]:
        if not auth.is_session_valid(self.name):
            if not await auth.refresh(self.name):
                raise SessionExpired("Messier session expired; run `python -m src.auth --portal=messier`.")
        return auth.load_creds(self.name)

    async def _fetch_jobs_with_creds(self, creds: dict[str, Any]) -> list[dict[str, Any]]:
        cookies = self._cookies(creds)
        async with httpx.AsyncClient(cookies=cookies, timeout=30, follow_redirects=False) as client:
            response = await self._post_jobs(client)
            if self._is_auth_failure(response):
                if not await auth.refresh(self.name):
                    raise SessionExpired("Messier session expired; run `python -m src.auth --portal=messier`.")
                creds = auth.load_creds(self.name)
                client.cookies.clear()
                client.cookies.update(self._cookies(creds))
                response = await self._post_jobs(client)
                if self._is_auth_failure(response):
                    raise SessionExpired("Messier session expired after refresh.")

        response.raise_for_status()
        payload = response.json()
        jobs = payload.get("d") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            logger.warning("Unexpected Messier response shape: %s", type(payload).__name__)
            return []
        return [job for job in jobs if isinstance(job, dict)]

    async def _post_jobs(self, client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            self.JOBS_API,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json; charset=utf-8",
                "Origin": "https://socs1.binus.ac.id",
                "Referer": self.HOME_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
            json={"type": "future"},
        )

    def _cookies(self, creds: dict[str, Any]) -> dict[str, str]:
        storage_state = creds.get("storage_state")
        cookies = storage_state.get("cookies") if isinstance(storage_state, dict) else None
        if not isinstance(cookies, list):
            raise ValueError("Messier auth state is missing storage_state cookies.")

        cookie_map: dict[str, str] = {}
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            domain = str(cookie.get("domain") or "")
            name = cookie.get("name")
            value = cookie.get("value")
            if "binus.ac.id" not in domain or not isinstance(name, str) or not isinstance(value, str):
                continue
            cookie_map[name] = value
        if ".ASPXAUTH" not in cookie_map:
            raise SessionExpired("Messier .ASPXAUTH cookie missing; run `python -m src.auth --portal=messier`.")
        return cookie_map

    def _is_auth_failure(self, response: httpx.Response) -> bool:
        location = response.headers.get("location", "")
        if response.status_code in {301, 302, 303, 307, 308} and "Login.aspx" in location:
            return True
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type.lower():
            text = response.text[:2000]
            return "Login.aspx" in text or "<html" in text.lower()
        return False

    def parse_job(self, job: dict[str, Any]) -> Event | None:
        if _normalized_status(job.get("Status")) == "done":
            return None

        job_type = _string_or_none(job.get("JobType")) or "Unknown"
        event_type = self._event_type(job_type)
        description = _string_or_none(job.get("Description")) or ""
        parsed_description = parse_description(description)

        if event_type == "correction_deadline":
            start_dt = parse_aspnet_date(job.get("EndDate"))
            end_dt = None
        else:
            start_dt = parse_aspnet_date(job.get("StartDate"))
            end_dt = parse_aspnet_date(job.get("EndDate"))

        if start_dt is None:
            logger.warning("Skipping Messier job without parseable date: %s", job)
            return None

        title = self._title(job_type, event_type, parsed_description)
        note = _string_or_none(job.get("Note")) or description or title
        event_id = _event_id(note, start_dt)
        notes = self._notes(job_type, job, parsed_description)
        return Event(
            id=event_id,
            source="messier",
            type=event_type,
            title=title,
            start=start_dt,
            end=end_dt,
            location=parsed_description.get("room"),
            link=None,
            notes=notes,
            remind_before=list(config.DEFAULT_REMINDERS_BY_TYPE.get(event_type, [])),
        )

    def _event_type(self, job_type: str) -> EventType:
        if job_type in {"Teaching", "Exam Proctor"}:
            return "teaching"
        if job_type == "Marking":
            return "correction_deadline"
        logger.warning("Unknown Messier JobType %r; treating as other", job_type)
        return "other"

    def _title(self, job_type: str, event_type: EventType, parsed: dict[str, str | None]) -> str:
        course = parsed.get("course_name") or "Untitled Messier Job"
        class_code = parsed.get("class_code")
        session = parsed.get("session")

        if event_type == "correction_deadline":
            return f"Mark {class_code} - {course}" if class_code else f"Mark - {course}"
        if job_type == "Exam Proctor":
            return f"{course} {class_code} (Exam Proctor)" if class_code else f"{course} (Exam Proctor)"
        if session:
            return f"{course} {class_code} - session {session}" if class_code else f"{course} - session {session}"
        return f"{course} {class_code}" if class_code else course

    def _notes(self, job_type: str, job: dict[str, Any], parsed: dict[str, str | None]) -> str | None:
        parts = [
            job_type,
            "Substitute" if parsed.get("is_substitute") else None,
            _prefixed("Session", parsed.get("session")),
            _string_or_none(job.get("Category")),
            _string_or_none(job.get("Note")),
        ]
        return "; ".join(part for part in parts if part) or None


def parse_aspnet_date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    match = ASPNET_DATE.match(value)
    if not match:
        return None
    millis = int(match.group(1))
    return datetime.fromtimestamp(millis / 1000, tz=config.TZ)


def parse_description(description: str) -> dict[str, str | None]:
    text = description.strip()
    is_substitute = text.startswith("(Substitute)")
    if is_substitute:
        text = text.removeprefix("(Substitute)").strip()

    room = None
    room_match = ROOM_RE.search(text)
    if room_match:
        room = room_match.group(0)
        text_without_room = (text[: room_match.start()] + " " + text[room_match.end() :]).strip()
    else:
        text_without_room = text

    session = None
    session_match = re.search(r"\b(\d{1,3})\s*$", text_without_room)
    if session_match:
        session = session_match.group(1)
        text_without_room = text_without_room[: session_match.start()].strip()

    course_code = None
    course_name = text_without_room
    if "-" in text_without_room:
        maybe_code, remainder = text_without_room.split("-", 1)
        maybe_code = maybe_code.strip()
        if maybe_code:
            course_code = maybe_code
        course_name = remainder.strip()

    class_code = None
    class_match = CLASS_CODE_RE.search(course_name)
    if class_match:
        match = class_match
        class_code = match.group(0)
        course_name = (course_name[: match.start()] + " " + course_name[match.end() :]).strip()

    course_name = re.sub(r"\s+-[A-Z]{1,3}\d{2,3}\s*$", "", course_name)
    course_name = re.sub(r"\s+", " ", course_name).strip(" -") or None
    return {
        "is_substitute": "true" if is_substitute else None,
        "course_code": course_code,
        "course_name": course_name,
        "class_code": class_code,
        "room": room,
        "session": session,
    }


def _normalized_status(value: Any) -> str:
    return str(value or "").strip().replace(" ", "").lower()


def _event_id(note: str, start: datetime) -> str:
    raw = f"messier|{note}|{start.isoformat()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _dedupe_events(events: list[Event]) -> list[Event]:
    unique: dict[str, Event] = {}
    for event in events:
        unique.setdefault(event.id, event)
    return list(unique.values())


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _prefixed(label: str, value: Any) -> str | None:
    text = _string_or_none(value)
    return f"{label} {text}" if text else None


async def _run_cli(days: int) -> int:
    today = datetime.now(tz=config.TZ).date()
    end = today + timedelta(days=days)
    try:
        events = await MessierScraper().fetch(today, end)
        inserted, updated = db.upsert_events(events)
        db.log_sync("messier", True, inserted, updated)
    except Exception as exc:
        db.log_sync("messier", False, error=str(exc))
        raise

    print(f"synced {len(events)} events ({inserted} new, {updated} updated)")
    if not events:
        print(f"No Messier events from {today} through {end}.")
        return 0

    for event in events:
        end_text = f" -> {event.end.strftime('%H:%M')}" if event.end else ""
        location = f" @ {event.location}" if event.location else ""
        print(
            f"{event.start:%Y-%m-%d %H:%M}{end_text} "
            f"[{event.type}] {event.title}{location} ({event.id})"
        )
    return 0

def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and print Messier events.")
    parser.add_argument("--days", type=int, default=30, help="Number of days after today to fetch.")
    args = parser.parse_args()
    if args.days < 0:
        parser.error("--days must be zero or greater")
    raise SystemExit(asyncio.run(_run_cli(args.days)))


if __name__ == "__main__":
    main()
