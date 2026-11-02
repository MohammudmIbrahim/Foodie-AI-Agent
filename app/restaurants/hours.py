"""Minimal, conservative parser for common OSM `opening_hours` values.

Supports `24/7`, day ranges/lists, multiple time spans, spans past midnight and `off`.
Anything else (holidays, months, week numbers, comments, sunrise) returns None = unknown.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_TIME = r"(\d{1,2}):(\d{2})"
_SPAN = re.compile(rf"^{_TIME}\s*-\s*{_TIME}\+?$")
_UNSUPPORTED = re.compile(r"sunrise|sunset|dawn|dusk|\[|\"|week|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\|\|")

# weekday index -> list of (start_minute, end_minute) where end may exceed 1440
Schedule = dict[int, list[tuple[int, int]]]


def _days(spec: str) -> list[int] | None:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if a not in DAYS or b not in DAYS:
                return None
            i, j = DAYS.index(a), DAYS.index(b)
            out.extend(DAYS.index(DAYS[k % 7]) for k in range(i, i + ((j - i) % 7) + 1))
        elif part in DAYS:
            out.append(DAYS.index(part))
        else:
            return None
    return out


def parse_opening_hours(value: str | None) -> Schedule | None:
    if not value:
        return None
    value = value.strip()
    if value == "24/7":
        return {d: [(0, 2880)] for d in range(7)}
    if _UNSUPPORTED.search(value):
        return None
    schedule: Schedule = {}
    for rule in [r.strip() for r in value.split(";") if r.strip()]:
        if re.match(r"^(PH|SH)\b", rule):
            continue  # public/school holiday rules are ignored; arrival-day checks only
        if re.search(r"\b(PH|SH)\b", rule):
            return None
        m = re.match(r"^([A-Za-z,\- ]+?)\s+(.+)$", rule)
        if m and re.match(r"^[A-Z][a-z]", m.group(1)):
            days = _days(m.group(1).replace(" ", ""))
            times = m.group(2).strip()
        elif re.match(r"^\d", rule):
            days, times = list(range(7)), rule
        else:
            return None
        if days is None:
            return None
        if times == "off" or times == "closed":
            for d in days:
                schedule[d] = []
            continue
        spans: list[tuple[int, int]] = []
        for t in times.split(","):
            sm = _SPAN.match(t.strip())
            if not sm:
                return None
            h1, m1, h2, m2 = (int(x) for x in sm.groups())
            if h1 > 24 or h2 > 48 or m1 > 59 or m2 > 59:
                return None
            start, end = h1 * 60 + m1, h2 * 60 + m2
            if end <= start:
                end += 1440
            spans.append((start, end))
        for d in days:
            schedule[d] = spans  # later rules override earlier ones, as in OSM
    return schedule or None


def open_at(value: str | None, when: datetime) -> bool | None:
    """True/False if the hours string is understood, None (unknown) otherwise."""
    schedule = parse_opening_hours(value)
    if schedule is None:
        return None
    minute = when.hour * 60 + when.minute
    today = when.weekday()
    for start, end in schedule.get(today, []):
        if start <= minute < end:
            return True
    yesterday = (when - timedelta(days=1)).weekday()
    for start, end in schedule.get(yesterday, []):
        if end > 1440 and minute + 1440 < end and minute + 1440 >= start:
            return True
    if today not in schedule and len(schedule) < 7:
        # Day not listed: OSM semantics say closed, but partial data is common -> unknown.
        return None
    return False
