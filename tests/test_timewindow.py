from datetime import datetime, timezone

import pytest

from itsm_agent.context.timewindow import WindowError, resolve

NOW = datetime(2026, 11, 23, 9, 0, 37, tzinfo=timezone.utc)


def test_last_24_hours_is_half_open_and_minute_floored():
    s, e = resolve("last 24 hours", "Asia/Kolkata", NOW)
    assert e == datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    assert (e - s).total_seconds() == 86400


def test_yesterday_uses_user_time_zone():
    s, e = resolve("yesterday", "Asia/Kolkata", NOW)          # 14:30 IST on the 23rd
    assert s == datetime(2026, 11, 21, 18, 30, tzinfo=timezone.utc)
    assert e == datetime(2026, 11, 22, 18, 30, tzinfo=timezone.utc)


def test_dst_transition_day_is_23_hours():
    now = datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc)
    s, e = resolve("yesterday", "America/New_York", now)     # 8 March 2026 is the US spring-forward day
    assert (e - s).total_seconds() == 23 * 3600


def test_explicit_range_and_rejects_garbage():
    s, e = resolve("2026-11-01..2026-11-02", "UTC", NOW)
    assert (e - s).days == 2
    with pytest.raises(WindowError):
        resolve("since the dawn of time", "UTC", NOW)
