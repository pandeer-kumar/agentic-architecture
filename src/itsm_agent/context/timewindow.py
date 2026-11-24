"""Resolve relative time ('last 24 hours', 'yesterday') ONCE into an absolute half-open UTC window."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UNITS = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800}


class WindowError(ValueError):
    pass


SUPPORTED = ("'last N minutes|hours|days|weeks', 'past N hours', 'today', 'yesterday', 'this week', 'last week', "
             "or an explicit 'YYYY-MM-DD..YYYY-MM-DD' / 'ISO-start/ISO-end'")


def resolve(expr: str, tz: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(second=0, microsecond=0)
    local_now = now.astimezone(zone)
    e = " ".join(expr.lower().strip().split())
    m = re.fullmatch(r"(?:the )?(?:last|past|previous) (\d+) (minute|hour|day|week)s?", e)
    if m:
        return now - timedelta(seconds=int(m.group(1)) * UNITS[m.group(2)]), now
    m = re.fullmatch(r"(?:the )?(?:last|past) (minute|hour|day|week)", e)
    if m:
        return now - timedelta(seconds=UNITS[m.group(1)]), now
    midnight = local_now.replace(hour=0, minute=0)
    if e == "today":
        return midnight.astimezone(timezone.utc), now
    if e == "yesterday":
        return (midnight - timedelta(days=1)).astimezone(timezone.utc), midnight.astimezone(timezone.utc)
    monday = midnight - timedelta(days=local_now.weekday())
    if e == "this week":
        return monday.astimezone(timezone.utc), now
    if e == "last week":
        return (monday - timedelta(days=7)).astimezone(timezone.utc), monday.astimezone(timezone.utc)
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*\.\.\s*(\d{4}-\d{2}-\d{2})", e)
    if m:
        s = datetime.fromisoformat(m.group(1)).replace(tzinfo=zone)
        t = datetime.fromisoformat(m.group(2)).replace(tzinfo=zone) + timedelta(days=1)
        return s.astimezone(timezone.utc), min(t.astimezone(timezone.utc), now)
    if "/" in expr:
        a, b = expr.split("/", 1)
        try:
            s, t = datetime.fromisoformat(a.strip()), datetime.fromisoformat(b.strip())
        except ValueError:
            raise WindowError(f"Could not parse {expr!r}. Supported: {SUPPORTED}")
        s = s if s.tzinfo else s.replace(tzinfo=zone)
        t = t if t.tzinfo else t.replace(tzinfo=zone)
        if t <= s:
            raise WindowError("window end must be after start")
        return s.astimezone(timezone.utc), min(t.astimezone(timezone.utc), now)
    raise WindowError(f"Could not parse {expr!r}. Supported: {SUPPORTED}")
