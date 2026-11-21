"""YouTube live chat adapter (YouTube Data API v3, liveChatMessages.list polling).

`channel` is a video id (11 chars) or a channel id (UC..., 24 chars). A channel id needs
search.list (eventType=live), which costs 100 quota units per lookup; a video id costs 1 unit via
videos.list. Prefer configuring the video id / pinned live URL when known.

creds:   api_key  OR  access_token (OAuth, scope youtube.readonly)
options: base_url, transport (httpx, tests), sleep, backfill (bool; keep messages sent before start)

streamList (server-streaming liveChatMessages.streamList) is NOT used: it is served over gRPC
(googleapis youtube.api.v3 V3DataLiveChatMessageService) and needs grpcio + protos, a heavy
dependency. Polling honours pollingIntervalMillis and nextPageToken. Quota: each list call is
~5 units; at the API's suggested ~5-10s interval that is far above the 10k/day default, so a
long stream needs a raised quota. Quota exhaustion is reported as `degraded` with action_needed.
Assumptions (not verified live): response field names follow the public reference
(items[].id, snippet.publishedAt/displayMessage, authorDetails.channelId/displayName).
"""
from __future__ import annotations

import asyncio
import time

import httpx

from ..events import ChatEvent
from ._util import AuthError, parse_rfc3339, redact, sleeper
from .base import ChatAdapter

BASE = "https://www.googleapis.com/youtube/v3"
ACT_QUOTA = "YouTube API quota exceeded: request a quota increase or wait for the daily reset (midnight Pacific)"
ACT_AUTH = "Reconnect YouTube account / check API key (credentials rejected)"


class _Gone(Exception):
    """Chat not available (ended / not live yet): go back to resolving."""


class _Quota(Exception):
    pass


class YouTubeChat(ChatAdapter):
    platform = "youtube"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sleep = sleeper(self.options)
        self._secrets = [self.creds.get("api_key", ""), self.creds.get("access_token", "")]
        self._seen: set[str] = set()

    async def _get(self, client, path: str, **params) -> dict:
        headers = {}
        if self.creds.get("access_token"):
            headers["Authorization"] = f"Bearer {self.creds['access_token']}"
        elif self.creds.get("api_key"):
            params["key"] = self.creds["api_key"]
        else:
            raise AuthError("no YouTube credentials", "Add a YouTube API key or connect a Google account")
        r = await client.get(self.options.get("base_url", BASE) + path, params=params, headers=headers)
        if r.status_code < 400:
            return r.json()
        try:
            err = r.json()["error"]
            reasons = {e.get("reason") for e in err.get("errors", [])}
        except Exception:
            err, reasons = {}, set()
        if r.status_code == 401:
            raise AuthError("YouTube API 401", ACT_AUTH)
        if reasons & {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}:
            raise _Quota(sorted(reasons)[0])
        if reasons & {"liveChatEnded", "liveChatNotFound", "liveChatDisabled"} or r.status_code == 404:
            raise _Gone(next(iter(reasons), "not found"))
        if r.status_code == 403:
            raise AuthError(f"YouTube API 403 {sorted(reasons)}", ACT_AUTH)
        raise RuntimeError(f"YouTube API {r.status_code}: {redact(str(err.get('message', ''))[:150], self._secrets)}")

    async def _resolve_chat_id(self, client) -> str | None:
        ch = self.channel
        vid = ch
        if ch.startswith("UC") and len(ch) == 24:
            d = await self._get(client, "/search", part="id", channelId=ch, eventType="live", type="video")
            if not d.get("items"):
                return None
            vid = d["items"][0]["id"]["videoId"]
        d = await self._get(client, "/videos", part="liveStreamingDetails", id=vid)
        items = d.get("items") or []
        return (items[0].get("liveStreamingDetails") or {}).get("activeLiveChatId") if items else None

    async def run(self) -> None:
        async with httpx.AsyncClient(transport=self.options.get("transport"), timeout=20) as client:
            while True:
                try:
                    chat_id = await self._resolve_chat_id(client)
                    if not chat_id:
                        self.health.state, self.health.detail, self.health.action_needed = "connecting", "waiting for live chat", ""
                        await self._sleep(30)
                        continue
                    await self._poll(client, chat_id)
                except _Gone:
                    self.health.state, self.health.detail, self.health.action_needed = "connecting", "waiting for live chat", ""
                    await self._sleep(30)
                except _Quota:
                    self.health.state, self.health.detail, self.health.action_needed = "degraded", "quota exceeded", ACT_QUOTA
                    await self._sleep(300)
                except AuthError as e:
                    self.health.state, self.health.detail, self.health.action_needed = "unavailable", str(e), e.action
                    await self._sleep(60)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.health.state = "connecting"
                    self.health.detail = f"retrying: {redact(f'{type(e).__name__}: {e}', self._secrets)}"[:300]
                    self.health.reconnects += 1
                    await self._sleep(10)

    async def _poll(self, client, chat_id: str) -> None:
        token, first, started = None, True, time.time()
        while True:
            params = {"liveChatId": chat_id, "part": "snippet,authorDetails", "maxResults": 200}
            if token:
                params["pageToken"] = token
            d = await self._get(client, "/liveChat/messages", **params)
            token = d.get("nextPageToken")
            interval = max(float(d.get("pollingIntervalMillis", 5000)) / 1000, 1.0)
            self.health.state, self.health.action_needed = "healthy", ""
            self.health.detail = f"polling liveChatMessages every {interval:.1f}s"
            for it in d.get("items", []):
                sn = it.get("snippet", {})
                mid = it["id"]
                sent = parse_rfc3339(sn.get("publishedAt"))
                if mid in self._seen or sn.get("type", "textMessageEvent") != "textMessageEvent":
                    continue
                self._seen.add(mid)
                if first and sent is not None and sent < started - 5 and not self.options.get("backfill"):
                    continue                      # history from before we joined
                a = it.get("authorDetails", {})
                now = time.time()
                self.emit(ChatEvent(platform="youtube", channel=self.channel, msg_id=mid,
                                    user_id=a.get("channelId", ""), user_name=a.get("displayName", ""),
                                    text=sn.get("displayMessage") or sn.get("textMessageDetails", {}).get("messageText", ""),
                                    received_at=now, sent_at=sent))
                self.health.last_message_at = now
            first = False
            if len(self._seen) > 10000:
                self._seen.clear()
            await self._sleep(interval)
