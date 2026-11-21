"""Per-day upload quota tracking where the providers document one (counted per destination, per provider day).

YouTube: videos.insert has its own bucket, default 100 calls/day, resets at midnight Pacific time.
TikTok: about 15 direct posts per creator per day; 5 pending inbox drafts per 24 h (treated as a daily count).
Instagram: no local counter; the live number comes from content_publishing_limit (see instagram.py).
Both defaults can be overridden with the destination setting `daily_limit` (0 disables the check).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except Exception:                                   # no tz database (e.g. Windows without tzdata): fixed UTC-8
    _PT = timezone(timedelta(hours=-8))


def daily_limit(dest: dict) -> int:
    s = dest.get("settings") or {}
    if "daily_limit" in s:
        return int(s["daily_limit"] or 0)
    if dest["provider"] == "youtube":
        return 100
    if dest["provider"] == "tiktok":
        return 15 if s.get("mode") == "direct" else 5
    return 0


def _tz(dest: dict):
    return _PT if dest["provider"] == "youtube" else UTC


def day_key(dest: dict, now: float) -> str:
    return datetime.fromtimestamp(now, _tz(dest)).strftime("%Y-%m-%d")


def next_reset(dest: dict, now: float) -> float:
    tz = _tz(dest)
    t = datetime.fromtimestamp(now, tz)
    nxt = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


def used(store, dest: dict, now: float | None = None) -> int:
    r = store.q1("SELECT used FROM publish_quota WHERE destination_id=? AND day=?", dest["id"],
                 day_key(dest, now or time.time()))
    return int(r["used"]) if r else 0


def add(store, dest: dict, now: float, delta: int) -> None:
    store.x("INSERT INTO publish_quota(destination_id,day,used) VALUES(?,?,MAX(0,?)) "
            "ON CONFLICT(destination_id,day) DO UPDATE SET used=MAX(0,used+?)",
            dest["id"], day_key(dest, now), delta, delta)
