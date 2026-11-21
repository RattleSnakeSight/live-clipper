"""Hard rolling freshness cutoff, for every creator, language and publisher.

A stream moment that is 24 hours old (>=) is never posted.

The moment's time is its ORIGINAL live time in UTC, taken only from a trusted source:
  * a live capture's own action time (recorded when the moment was captured from the live stream);
  * a VOD import whose official broadcast start was given: that start + the moment's offset;
  * official-archive evidence attached to the clip: the archive's start + the moment's offset.
All clips of one creator moment (re-cuts, simulcast twins, re-imports) share the EARLIEST trusted time, so
editing,
cloning, importing again or retrying never makes a moment younger. A clip without a trusted time (a VOD import
without a broadcast start, a missing or inconsistent timestamp) is HELD: it is never posted, and it is never
cleaned up on that basis either.

Posting gates (reason()): enqueue and the automatic rules, the native claim right before an upload (so a
queued,
deferred, retried or restart-recovered delivery cannot go out late), the candidate list and the advisory plan.
Sending through an external publisher additionally needs a safety margin before the cutoff, because the
platform
publishes later than the host hands over. Removing expired media is the retention service's job, not this
module's.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from . import moments

HARD_MAX_H = 24.0


def max_age_s(settings) -> float:
    """The cutoff in seconds. The setting can only make it stricter than 24 h, never looser."""
    try:
        h = float(getattr(settings, "freshness_hours", HARD_MAX_H))
    except (TypeError, ValueError):
        h = HARD_MAX_H
    return min(HARD_MAX_H, max(1.0, h)) * 3600


def margin_s(settings) -> float:
    return max(0.0, float(getattr(settings, "freshness_margin_minutes", 15))) * 60


def _own_time(store, c: dict | None) -> tuple[float | None, str]:
    """(trusted original UTC time of this clip's moment, basis) or (None, why it is not trusted)."""
    if not c:
        return None, "the clip no longer exists"
    trig = c.get("trigger") or {}
    at = c.get("action_time")
    arch = (trig.get("topic") or {}).get("archive") or {}
    if arch.get("vod_start") is not None and arch.get("offset_s") is not None:
        return float(arch["vod_start"]) + float(arch["offset_s"]), "official archive start + moment offset"
    if trig.get("input") == "vod_import":
        row = store.q1("SELECT vod_start, timeline_base FROM vod_imports WHERE id=?", trig.get("import_id")) \
            if trig.get("import_id") is not None else None
        if not row or row["vod_start"] is None or at is None or \
                abs(float(row["timeline_base"] or row["vod_start"]) - float(row["vod_start"])) > 1.0:
            return None, "VOD import without a verified broadcast start time"
        return float(at), "VOD broadcast start + moment offset"
    if at is None:
        return None, "no recorded moment time"
    try:
        at = float(at)
    except (TypeError, ValueError):
        return None, "the recorded moment time is not a time"
    # not a UTC epoch (e.g. a file-relative or zero timestamp)
    if not 1_000_000_000 <= at <= 4_000_000_000:
        return None, "the recorded moment time is not a UTC time"
    return at, "live capture time"


def moment_time(store, clip: dict) -> dict:
    """{"t": UTC seconds or None, "trusted": bool, "basis": str}: the earliest trusted time over the whole
    moment."""
    best, basis, why = None, "", ""
    ids = [clip["id"]]
    # (a moment id is assigned at capture; this never assigns one)
    if (clip.get("trigger") or {}).get("moment_id"):
        try:
            ids = moments.moment_clips(store, clip["id"])
        except Exception:
            ids = [clip["id"]]
    for cid in sorted(set(ids) | {clip["id"]}):
        c = clip if cid == clip["id"] else moments.clip_meta(store, cid)
        if cid != clip["id"] and c is not None and "trigger" not in c:
            c = store.clip(cid) or c
        t, b = _own_time(store, c)
        if t is None:
            why = why or b if cid != clip["id"] else b
        elif best is None or t < best:
            best, basis = t, b
    if best is None:
        return {"t": None, "trusted": False, "basis": why or "no trusted moment time"}
    return {"t": best, "trusted": True, "basis": basis}


def expires_at(store, settings, clip: dict) -> float | None:
    m = moment_time(store, clip)
    return m["t"] + max_age_s(settings) if m["trusted"] else None


def _utc(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def reason(store, settings, clip: dict | None, at: float | None = None, external: bool = False,
           who: str = "an external publisher") -> str | None:
    """Why this clip may NOT be handed over so that it is published at `at` (None = fresh enough). external:
    the
    hand-over goes to a publisher that posts later, so the safety margin applies."""
    if not clip:
        return None
    at = time.time() if at is None else float(at)
    m = moment_time(store, clip)
    if not m["trusted"]:
        return f"freshness: held, the moment's original time is not trusted ({m['basis']})"
    end = m["t"] + max_age_s(settings)
    lim = end - (margin_s(settings) if external else 0.0)
    if at >= lim:
        h = max_age_s(settings) / 3600
        if at >= end:
            return (f"freshness: the moment ({_utc(m['t'])}, {m['basis']}) is {h:g} h old at {_utc(at)}: "
                    f"it is never posted")
        return (f"freshness: the moment ({_utc(m['t'])}) reaches {h:g} h at {_utc(end)}; {who} needs "
                f"{margin_s(settings) / 60:g} min before that, so it is not handed over for {_utc(at)}")
    return None


def native_reason(store, settings, clip: dict | None, at: float | None = None) -> str | None:
    """Why a native CONTROLLABLE WRITE (a first upload, a resumed upload, a replay after a token refresh,
    Instagram's media_publish) may not happen now. A platform makes an accepted upload public only after
    processing (YouTube, TikTok: without any further app write) or after a later publish step, so the same
    safety
    margin as for an external publisher applies. Read-only lookups are never gated: an accepted or uncertain
    upload
    keeps being reconciled and its holds stay."""
    return reason(store, settings, clip, at, external=True, who="a native upload")


def expired(store, settings, clip: dict, now: float | None = None) -> bool:
    """Past the 24 h cutoff."""
    m = moment_time(store, clip)
    return bool(m["trusted"] and (time.time() if now is None else now) >= m["t"] + max_age_s(settings))
