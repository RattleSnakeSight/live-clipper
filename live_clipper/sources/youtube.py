"""YouTube official live status (YouTube Data API v3), quota-aware.

Per channel: `@handle` -> channel id once (channels.list forHandle, 1 unit). Live discovery: search.list
(channelId, eventType=live, type=video); search sits in a small daily bucket (about 100 calls a day), so each
channel is searched at most every `max(interval, 86400 * channels / SEARCH_BUDGET)` seconds and
never more than SEARCH_BUDGET times in 24 h. A found live video is then followed with videos.list
(1 unit per 50 videos) every interval until `actualEndTime` appears.
Auth: API key (`key=`) or OAuth access token (youtube.readonly).
"""

from __future__ import annotations

import re
import time
from collections import deque

from . import RateLimited, SourceAuthError, batches

API = "https://www.googleapis.com/youtube/v3"
SEARCH_BUDGET = 100
FIX_KEY = (
    "Check the API key in Sources > YouTube and that the YouTube Data API v3 is enabled for its Google Cloud "
    "project (or sign in with Google again)"
)
_UC = re.compile(r"^(?:channel/)?(UC[\w-]{22})$")


class Client:
    def __init__(self, save, clock=time.time):
        self.save, self.clock = save, clock
        self.channel_ids: dict[str, str] = {}  # login -> UC id
        self.videos: dict[str, str] = {}  # login -> live video id being followed
        self.searched: dict[str, float] = {}  # login -> last search time
        self.search_log: deque[float] = deque()  # search calls in the last 24 h
        self.units = 0  # quota units spent by this process

    async def _get(self, http, creds: dict, path: str, cost: int, **params) -> dict:
        headers = {}
        if creds.get("access_token"):
            headers["Authorization"] = f"Bearer {creds['access_token']}"
        else:
            params["key"] = creds["api_key"]
        r = await http.get(API + path, params=params, headers=headers)
        self.units += cost
        if r.status_code < 400:
            return r.json()
        try:
            reasons = {e.get("reason") for e in r.json()["error"].get("errors", [])}
        except Exception:
            reasons = set()
        if reasons & {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}:
            raise RateLimited("YouTube API quota exceeded (resets daily at midnight Pacific time)", 3600.0)
        if r.status_code in (400, 401, 403):
            raise SourceAuthError(
                f"YouTube rejected the credentials (HTTP {r.status_code} {sorted(reasons)})", FIX_KEY
            )
        r.raise_for_status()
        return {}

    def _search_due(self, login: str, n: int, interval: float) -> bool:
        now = self.clock()
        while self.search_log and self.search_log[0] < now - 86400:
            self.search_log.popleft()
        spacing = max(interval, 86400.0 * max(1, n) / SEARCH_BUDGET)
        return len(self.search_log) < SEARCH_BUDGET and now - self.searched.get(login, 0.0) >= spacing

    async def fetch(self, http, creds: dict, logins: list[str], interval: float = 60.0) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for lg in logins:
            if lg in self.channel_ids or lg in self.videos:
                continue
            m = _UC.match(lg)
            if m:
                self.channel_ids[lg] = m.group(1)
                continue
            d = await self._get(
                http, creds, "/channels", 1, part="id", forHandle=lg if lg.startswith("@") else "@" + lg
            )
            if not d.get("items"):
                out[lg] = {
                    "state": "error",
                    "detail": f"YouTube channel '{lg}' not found",
                    "action_needed": "Check the YouTube channel URL (youtube.com/@handle)",
                }
                continue
            self.channel_ids[lg] = d["items"][0]["id"]
        searchable = [lg for lg in logins if lg in self.channel_ids and lg not in self.videos]
        for lg in searchable:
            if not self._search_due(lg, len(searchable), interval):
                continue  # not checked this round (search budget)
            self.searched[lg] = self.clock()
            self.search_log.append(self.clock())
            d = await self._get(
                http,
                creds,
                "/search",
                100,
                part="id",
                channelId=self.channel_ids[lg],
                eventType="live",
                type="video",
            )
            items = d.get("items") or []
            if items:
                self.videos[lg] = items[0]["id"]["videoId"]
            else:
                out[lg] = {"state": "offline", "detail": "no live broadcast (YouTube search.list)"}
        followed = [lg for lg in logins if lg in self.videos]
        for chunk in batches(followed, 50):
            d = await self._get(
                http,
                creds,
                "/videos",
                1,
                part="snippet,liveStreamingDetails",
                id=",".join(self.videos[lg] for lg in chunk),
            )
            by_id = {it["id"]: it for it in d.get("items") or []}
            for lg in chunk:
                it = by_id.get(self.videos[lg]) or {}
                ls = it.get("liveStreamingDetails") or {}
                if ls.get("actualStartTime") and not ls.get("actualEndTime"):
                    out[lg] = {
                        "state": "live",
                        "title": (it.get("snippet") or {}).get("title") or "",
                        "category": "",
                        "id": self.videos[lg],
                        "detail": "",
                    }
                else:
                    self.videos.pop(lg)
                    out[lg] = {"state": "offline", "detail": "the live broadcast ended (YouTube videos.list)"}
        return out
