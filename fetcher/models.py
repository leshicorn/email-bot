"""
Email data models shared by the fetcher, the summarizer and the formatter.

The objects here are deliberately free of I/O so they can be built in tests.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime

_RE_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|wg|отв|перес)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)
_RE_SPACE = re.compile(r"\s+")

#: Reply markers understood by :func:`normalise_subject`.
_REPLY_MARKERS = (
    "-----original message-----",
    "________________________________",
    "forwarded message",
    "---------- пересылаемое сообщение",
    "---------- исходное сообщение",
)


@dataclass(slots=True)
class CalendarEvent:
    """A VEVENT extracted from a text/calendar part."""

    summary: str = ""
    start: datetime | None = None
    end: datetime | None = None
    location: str = ""
    organizer: str = ""

    def sort_key(self) -> datetime:
        return self.start or datetime.max

    def human(self) -> str:
        """One-line Russian description, e.g. ``18 ноя 10:30–11:00 · каб. 204 · Защита``."""
        parts: list[str] = []
        if self.start:
            day = f"{self.start.day} {_RU_MONTHS_SHORT[self.start.month]}"
            if self.end and self.end.date() == self.start.date():
                parts.append(f"{day} {self.start:%H:%M}–{self.end:%H:%M}")
            else:
                parts.append(f"{day} {self.start:%H:%M}")
        if self.location:
            parts.append(self.location)
        if self.summary:
            parts.append(self.summary)
        return " · ".join(parts)


_RU_MONTHS_SHORT = {
    1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "мая", 6: "июн",
    7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
}


@dataclass(slots=True)
class Email:
    message_id: str
    mailbox: str
    subject: str
    sender: str
    sender_email: str
    date: datetime
    body_text: str
    is_read: bool = False
    is_flagged: bool = False
    is_bulk: bool = False
    bulk_reason: str = ""
    attachments: list[str] = field(default_factory=list)
    calendar_events: list[CalendarEvent] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    in_reply_to: str = ""
    thread_key: str = ""
    thread_count: int = 1
    thread_subjects: list[str] = field(default_factory=list)
    size_bytes: int = 0

    @property
    def has_calendar(self) -> bool:
        return bool(self.calendar_events)

    def flags_line(self) -> str:
        """Compact marker string used in the LLM prompt."""
        flags: list[str] = []
        if self.is_flagged:
            flags.append("★ помечено звездой")
        if not self.is_read:
            flags.append("не прочитано")
        if self.is_bulk:
            flags.append(f"рассылка{': ' + self.bulk_reason if self.bulk_reason else ''}")
        return ", ".join(flags)


def normalise_subject(subject: str) -> str:
    """Strip ``Re:``/``Fwd:`` prefixes and collapse whitespace."""
    cleaned = _RE_PREFIX.sub("", subject or "")
    return _RE_SPACE.sub(" ", cleaned).strip().lower()


def make_thread_key(
    subject: str,
    references: list[str] | None = None,
    in_reply_to: str = "",
    message_id: str = "",
) -> str:
    """Best-effort thread identifier.

    Uses the root of the ``References`` chain when present, otherwise the
    normalised subject. Falls back to ``message_id`` so unrelated mails never
    collapse into one thread.
    """
    if references:
        return references[0].strip()
    if in_reply_to:
        return in_reply_to.strip()
    normalised = normalise_subject(subject)
    return f"subj:{normalised}" if normalised else f"mid:{message_id}"


def collapse_threads(emails: list[Email], max_subjects: int = 4) -> list[Email]:
    """Merge replies into their thread's newest email.

    The newest message keeps its body; older ones contribute only their counts
    and subjects. This keeps the prompt small and gives the LLM one bullet per
    conversation instead of one per reply.
    """
    groups: dict[str, list[Email]] = {}
    order: list[str] = []
    for email in emails:
        key = email.thread_key or email.message_id
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(email)

    collapsed: list[Email] = []
    for key in order:
        members = groups[key]
        if len(members) == 1:
            collapsed.append(members[0])
            continue
        members_sorted = sorted(members, key=lambda e: e.date)
        newest = members_sorted[-1]
        subjects: list[str] = []
        for member in members_sorted:
            if member.subject not in subjects:
                subjects.append(member.subject)
        collapsed.append(
            replace(
                newest,
                thread_count=len(members),
                thread_subjects=subjects[:max_subjects],
                is_flagged=any(m.is_flagged for m in members),
                calendar_events=[ev for m in members for ev in m.calendar_events],
                attachments=[a for m in members for a in m.attachments][:10],
            )
        )
    collapsed.sort(key=lambda e: e.date)
    return collapsed
