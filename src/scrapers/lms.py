"""LMS scraper."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from src import auth, config, db
from src.parser import Event, EventType
from src.scrapers.base import Scraper, SessionExpired


logger = logging.getLogger(__name__)


class LMSScraper(Scraper):
    name = "lms"

    DASHBOARD_URL = "https://lms.binus.ac.id/lms/dashboard"
    MONTH_API = "https://func-bm7-schedule-prod.azurewebsites.net/api/Schedule/Month-v1"
    DATE_API = "https://func-bm7-schedule-prod.azurewebsites.net/api/Schedule/Date-v1"

    async def fetch(self, start: date, end: date) -> list[Event]:
        """Fetch LMS events within the inclusive date window."""
        if end < start:
            raise ValueError("end must be on or after start")

        creds = await self._valid_creds()
        events = await self._fetch_with_creds(start, end, creds)
        events = _dedupe_events(events)
        events.sort(key=lambda event: event.start)
        return events

    async def _valid_creds(self) -> dict[str, Any]:
        if not auth.is_session_valid(self.name):
            if not await auth.refresh(self.name):
                raise SessionExpired("LMS session expired; run `python -m src.auth --portal=lms`.")
        return auth.load_creds(self.name)

    async def _fetch_with_creds(self, start: date, end: date, creds: dict[str, Any]) -> list[Event]:
        headers = self._headers(creds)
        post_body = creds.get("post_body")
        if not isinstance(post_body, dict):
            raise ValueError("LMS auth state is missing post_body; re-run `python -m src.auth --portal=lms`.")

        async with httpx.AsyncClient(timeout=30) as client:
            responses = await self._fetch_months(client, start, end, headers, post_body)
            if any(response.status_code == 401 for response in responses):
                if not await auth.refresh(self.name):
                    raise SessionExpired("LMS session expired; run `python -m src.auth --portal=lms`.")
                creds = auth.load_creds(self.name)
                headers = self._headers(creds)
                post_body = creds.get("post_body")
                if not isinstance(post_body, dict):
                    raise ValueError("LMS auth state is missing post_body after refresh.")
                responses = await self._fetch_months(client, start, end, headers, post_body)
                if any(response.status_code == 401 for response in responses):
                    raise SessionExpired("LMS session expired after refresh.")

        items: list[dict[str, Any]] = []
        for response in responses:
            if response.status_code == 204:
                continue
            response.raise_for_status()
            items.extend(self._flatten_schedule(response.json()))

        return [
            event
            for item in items
            if (event := self._parse_item(item)) is not None
            and start <= event.start.date() <= end
        ]

    async def _fetch_months(
        self,
        client: httpx.AsyncClient,
        start: date,
        end: date,
        headers: dict[str, str],
        post_body: dict[str, Any],
    ) -> list[httpx.Response]:
        responses: list[httpx.Response] = []
        for month_start in _month_starts(start, end):
            url = f"{self.MONTH_API}/{month_start.year}-{month_start.month}-1"
            responses.append(await client.post(url, headers=headers, json=post_body))
        return responses

    def _headers(self, creds: dict[str, Any]) -> dict[str, str]:
        saved_headers = creds.get("headers")
        token = creds.get("bearer_token")
        if not isinstance(saved_headers, dict) or not isinstance(token, str) or not token:
            raise ValueError("LMS auth state is incomplete; re-run `python -m src.auth --portal=lms`.")

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://lms.binus.ac.id",
            "Referer": "https://lms.binus.ac.id/",
            "Authorization": f"Bearer {token}",
        }
        for key in ("rOId", "academicCareer", "institution", "roleName", "roleId"):
            value = saved_headers.get(key)
            if value is not None:
                headers[key] = str(value)
        return headers

    def _flatten_schedule(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            schedule = payload.get("Schedule")
            return schedule if isinstance(schedule, list) else []

        if not isinstance(payload, list):
            logger.warning("Unexpected LMS response shape: %s", type(payload).__name__)
            return []

        items: list[dict[str, Any]] = []
        for bucket in payload:
            if not isinstance(bucket, dict):
                continue
            schedule = bucket.get("Schedule")
            if isinstance(schedule, list):
                items.extend(item for item in schedule if isinstance(item, dict))
        return items

    def _parse_item(self, item: dict[str, Any]) -> Event | None:
        event_type = self._event_type(item)
        custom = item.get("customParam") if isinstance(item.get("customParam"), dict) else {}

        if event_type == "assignment_deadline":
            start_raw = custom.get("dueDate") or item.get("dateEnd")
            end_dt = None
            title = _join_title(custom.get("title"), item.get("content"))
        else:
            start_raw = item.get("dateStart")
            end_dt = _parse_lms_datetime(item.get("dateEnd"))
            title = _join_title(item.get("content"), item.get("title"))

        start_dt = _parse_lms_datetime(start_raw)
        if start_dt is None:
            logger.warning("Skipping LMS item without parseable start: %s", item)
            return None

        notes = self._notes(item, custom)
        event_id = _event_id("lms", title, start_dt)
        return Event(
            id=event_id,
            source="lms",
            type=event_type,
            title=title,
            start=start_dt,
            end=end_dt,
            location=_string_or_none(item.get("location")),
            link=self._link(item, custom),
            notes=notes,
            remind_before=list(config.DEFAULT_REMINDERS_BY_TYPE.get(event_type, [])),
        )

    def _event_type(self, item: dict[str, Any]) -> EventType:
        schedule_type = _string_or_none(item.get("scheduleType"))
        lam_type = _string_or_none(item.get("lamType"))
        if schedule_type == "Assignment" or lam_type == "ASG":
            return "assignment_deadline"
        if schedule_type in {"Onsite", "Online", "Virtual Class"}:
            return "class"
        if schedule_type in {"Event", "Onsite Exam"}:
            return "other"
        logger.warning("Unknown LMS scheduleType %r; treating as other", schedule_type)
        return "other"

    def _notes(self, item: dict[str, Any], custom: dict[str, Any]) -> str | None:
        parts = [
            _prefixed("Schedule ID", item.get("scheduleId")),
            _prefixed("Session", custom.get("sessionNumber")),
            _string_or_none(item.get("deliveryModeDesc")),
            _string_or_none(item.get("assessmentActivity")),
            _string_or_none(custom.get("courseCode")),
        ]
        return "; ".join(part for part in parts if part) or None

    def _link(self, item: dict[str, Any], custom: dict[str, Any]) -> str | None:
        for source in (custom, item):
            for key in ("url", "link", "meetingUrl", "meetingURL", "zoomUrl", "zoomURL"):
                value = _string_or_none(source.get(key))
                if value:
                    return value
        return None


def _month_starts(start: date, end: date) -> list[date]:
    current = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    months: list[date] = []
    while current <= last:
        months.append(current)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return months


def _parse_lms_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=config.TZ)
    return parsed.astimezone(config.TZ)


def _join_title(primary: Any, secondary: Any) -> str:
    parts = [_string_or_none(primary), _string_or_none(secondary)]
    clean = [part for part in parts if part]
    return " - ".join(clean) if clean else "Untitled LMS Event"


def _event_id(source: str, title: str, start: datetime) -> str:
    raw = f"{source}|{title}|{start.isoformat()}".encode("utf-8")
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
        events = await LMSScraper().fetch(today, end)
        inserted, updated = db.upsert_events(events)
        db.log_sync("lms", True, inserted, updated)
    except Exception as exc:
        db.log_sync("lms", False, error=str(exc))
        raise

    print(f"synced {len(events)} events ({inserted} new, {updated} updated)")
    if not events:
        print(f"No LMS events from {today} through {end}.")
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
    parser = argparse.ArgumentParser(description="Fetch and print LMS events.")
    parser.add_argument("--days", type=int, default=30, help="Number of days after today to fetch.")
    args = parser.parse_args()
    if args.days < 0:
        parser.error("--days must be zero or greater")
    raise SystemExit(asyncio.run(_run_cli(args.days)))


if __name__ == "__main__":
    main()
