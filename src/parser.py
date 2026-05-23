"""Event parsing primitives.

Implemented in Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


EventType = Literal[
    "class",
    "teaching",
    "meeting",
    "other",
    "assignment_deadline",
    "correction_deadline",
]
EventSource = Literal["lms", "messier", "manual"]


@dataclass
class Event:
    id: str
    source: EventSource
    type: EventType
    title: str
    start: datetime
    end: datetime | None = None
    location: str | None = None
    link: str | None = None
    notes: str | None = None
    remind_before: list[int] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

