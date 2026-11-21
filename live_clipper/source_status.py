"""Live status + metadata for a source, used by the topic gate (capture only while the stream TITLE is on topic).

Reference paths (used when no official API is connected for the platform; see live_clipper.sources for the official
Twitch Helix / Kick / YouTube Data API clients). Public paths only (no cookies/sessions, no private APIs, no keys):
  twitch, kick  Streamlink's public plugins (`streamlink --json`): live/offline, title, category.
  rumble        the public channel page, read only for standard schema.org JSON-LD (BroadcastEvent /
                VideoObject publication with isLiveBroadcast=true and a timezone-aware startDate <= now < endDate;
                ambiguous or unrecognizable metadata is "unavailable", never "offline"). A qualifying title here
                only requests capture; Rumble media still needs a creator-provided feed (no supported input). Rumble documents no public live-status API for
                third-party channels, Streamlink has no Rumble plugin, and rumble.com answers HTTP 403 to cloud
                requests from some networks -> "unavailable", retried with backoff.
  other         no metadata source -> "unavailable" (the gate stays closed: fail closed).
The gate opens only when the live TITLE names GTA 6 / GTA VI / Grand Theft Auto VI; the category is shown but never
starts or continues capture. Returns {"state": live|offline|unavailable|error, "title", "category", "detail",
"retry_after"}.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime

GTA6 = re.compile(r"\bgta\s*-?\s*(?:6|vi)\b|\bgrand\s+theft\s+auto\s*(?:6|vi)\b", re.I)


def is_gta6(title: str | None, category: str | None = None) -> bool:
    """Title only: a matching category alone never counts (reference rule)."""
    return bool(GTA6.search(title or ""))


def is_gta6_category(category: str | None) -> bool:
    """The platform's own category/game field names GTA 6 (evidence for a clip, never a capture gate)."""
    return bool(GTA6.search(category or ""))


def topic_state(st: dict, category_counts: bool = True) -> str:
    """live_gta (title) | live_gta_category (platform category) | live_off_topic | unverified. A live answer that
    carries neither a title nor a category is NOT evidence that the stream is off topic: it is unverified."""
    if (st or {}).get("state") != "live":
        return "unverified"
    if not str(st.get("title") or "").strip() and not str(st.get("category") or "").strip():
        return "unverified"
    if is_gta6(st.get("title")):
        return "live_gta"
    if category_counts and is_gta6_category(st.get("category")):
        return "live_gta_category"
    return "live_off_topic"


async def streamlink_status(url: str, timeout: float = 40) -> dict:
    from .capture import _streamlink_bin
    p = await asyncio.create_subprocess_exec(_streamlink_bin(), "--json", url, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except TimeoutError:
        p.kill()
        return {"state": "error", "detail": "status check timed out"}
    try:
        d = json.loads(out or b"{}")
    except ValueError:
        return {"state": "error", "detail": "unreadable status response"}
    if d.get("error"):
        off = "no playable streams" in d["error"].lower()
        return {"state": "offline" if off else "error", "detail": d["error"][:200]}
    m = d.get("metadata") or {}
    return {"state": "live" if d.get("streams") else "offline", "title": m.get("title") or "",
            "id": str(m.get("id") or ""),             # the platform's id of this broadcast (changes per stream)
            "category": m.get("category") or "", "detail": ""}


def _types(item: dict) -> set:
    t = item.get("@type")
    return {x for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)}


def _when(v) -> datetime | None:
    """Timezone-aware ISO 8601 instant, or None (missing). Raises ValueError when present but unusable (not a string,
    unparsable, date-only or without a UTC offset): a naive time cannot be placed on the timeline, so fail closed."""
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise ValueError("date is not a string")
    d = datetime.fromisoformat(v.strip())
    if d.tzinfo is None or d.utcoffset() is None:
        raise ValueError("date has no timezone")
    return d


def _broadcast_verdict(ev: dict, now: datetime) -> tuple[str, str]:
    """One BroadcastEvent -> ("live"|"ended"|"scheduled"|"not_live"|"ambiguous", reason).
    Live only when isLiveBroadcast is the JSON boolean true AND startDate <= now < endDate (endDate optional)."""
    flag = ev.get("isLiveBroadcast")
    if flag is None or flag is False:
        return "not_live", "isLiveBroadcast is not set"
    if flag is not True:
        return "ambiguous", f"isLiveBroadcast is not a boolean ({type(flag).__name__})"
    try:
        start, end = _when(ev.get("startDate")), _when(ev.get("endDate"))
    except ValueError as e:
        return "ambiguous", f"unusable broadcast date ({e})"
    if start is None:
        return "ambiguous", "live broadcast without startDate"
    if end is not None and end <= start:
        return "ambiguous", "broadcast endDate is not after startDate"
    if end is not None and end <= now:
        return "ended", "broadcast ended " + end.isoformat()
    if start > now:
        return "scheduled", "broadcast scheduled for " + start.isoformat()
    return "live", ""


def parse_public_jsonld(html: str, now: datetime | None = None) -> dict:
    """Standard schema.org JSON-LD only. A broadcast is a top-level BroadcastEvent, or the BroadcastEvent
    `publication` of a VideoObject (title from the VideoObject). It counts as live only with isLiveBroadcast == true
    (a real boolean) and a timezone-aware startDate <= now < endDate (endDate absent = still running), so an archived
    or scheduled stream is never live. Fails closed: malformed or ambiguous broadcast metadata, two live broadcasts
    with different titles, or a page with no recognizable broadcast metadata is "unavailable", never "offline";
    "offline" needs at least one well-formed broadcast that is ended, scheduled or explicitly not live."""
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    live: list[dict] = []
    ambiguous: list[str] = []
    seen = 0
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or "", re.S | re.I):
        try:
            data = json.loads(block)
        except ValueError:
            ambiguous.append("unparsable JSON-LD block")
            continue
        items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        flat: list = []
        for it in items:
            flat.extend(it["@graph"] if isinstance(it, dict) and isinstance(it.get("@graph"), list) else [it])
        for item in flat:
            if not isinstance(item, dict):
                continue
            events: list[tuple[dict, dict]] = []
            if "BroadcastEvent" in _types(item):
                events.append((item, item))
            pub = item.get("publication")
            for p in (pub if isinstance(pub, list) else [pub] if pub is not None else []):
                if isinstance(p, dict) and ("BroadcastEvent" in _types(p) or "isLiveBroadcast" in p):
                    events.append((p, item))
                elif p is not None:
                    ambiguous.append("unrecognized publication entry")
            for ev, owner in events:
                verdict, why = _broadcast_verdict(ev, now)
                seen += 1
                if verdict == "live":
                    title = owner.get("name") if isinstance(owner.get("name"), str) else ""
                    if not title and owner is not ev and isinstance(ev.get("name"), str):
                        title = ev["name"]
                    genre = owner.get("genre") if isinstance(owner.get("genre"), str) else ""
                    live.append({"title": title.strip(), "category": genre})
                elif verdict == "ambiguous":
                    ambiguous.append(why)
    titles = {x["title"] for x in live}
    if len(titles) == 1 and "" not in titles:
        return {"state": "live", "title": live[0]["title"], "category": live[0]["category"], "detail": ""}
    if len(titles) > 1:
        return {"state": "unavailable", "title": "", "category": "",
                "detail": "public page metadata lists several live broadcasts with different titles (ambiguous)"}
    if titles == {""}:
        return {"state": "unavailable", "title": "", "category": "",
                "detail": "public page metadata shows a live broadcast without a title (ambiguous)"}
    if ambiguous:
        return {"state": "unavailable", "title": "", "category": "",
                "detail": "ambiguous public page metadata: " + "; ".join(dict.fromkeys(ambiguous))[:200]}
    if seen:
        return {"state": "offline", "title": "", "category": "",
                "detail": "public page metadata shows no broadcast live now (ended, scheduled or not live)"}
    return {"state": "unavailable", "title": "", "category": "",
            "detail": "public page has no recognizable broadcast metadata; live status unknown"}


async def source_status(platform: str, name: str, source_url: str, creds: dict, client=None, hub=None) -> dict:
    """`hub` (live_clipper.sources.StatusHub): with an official API connected for this platform, the answer comes from
    that API (same dict shape plus "source": "official"); otherwise the reference paths below (stream probe)."""
    if hub is not None and not source_url and hub.connected(platform):
        return await hub.current(platform, name)
    if platform in ("twitch", "kick"):
        base = {"twitch": "https://www.twitch.tv/", "kick": "https://kick.com/"}[platform]
        return await streamlink_status(base + name.lstrip("@"))
    if platform == "rumble":
        import httpx
        url = source_url if source_url.startswith("https://rumble.com/") else f"https://rumble.com/c/{name.lstrip('@')}"
        try:
            async with (client or httpx.AsyncClient(timeout=20, follow_redirects=True)) as c:
                r = await c.get(url, headers={"User-Agent": "live-clipper (status check)"})
        except Exception as e:
            return {"state": "unavailable", "detail": f"Rumble public page request failed: {type(e).__name__}"}
        if r.status_code in (403, 429):
            ra = r.headers.get("retry-after")
            return {"state": "unavailable", "retry_after": float(ra) if ra and ra.isdigit() else None,
                    "detail": f"Rumble refused the public status request (HTTP {r.status_code}); no supported public "
                              f"live-metadata path from this network. Retrying with backoff."}
        if r.status_code != 200:
            return {"state": "unavailable", "detail": f"Rumble public page HTTP {r.status_code}"}
        return parse_public_jsonld(r.text)
    return {"state": "unavailable", "detail": "no supported live-metadata source for this platform"}
