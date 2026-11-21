import asyncio
import time
from datetime import UTC, datetime

import httpx

from live_clipper.chat.youtube import YouTubeChat


def iso(t):
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def item(i, t):
    return {"id": i, "snippet": {"type": "textMessageEvent", "publishedAt": iso(t), "displayMessage": f"msg {i}"},
            "authorDetails": {"channelId": "UCa", "displayName": "Ann"}}


async def wait_for(cond, t=5):
    end = time.time() + t
    while not cond():
        assert time.time() < end, "timeout"
        await asyncio.sleep(0.01)


def run(handler, check, creds=None, opts=None):
    async def go():
        events, sleeps = [], []

        async def sl(s):
            sleeps.append(s)
            await asyncio.sleep(0.01)
        ad = YouTubeChat("VIDEOID", events.append, creds or {"api_key": "AIza-secret-key"},
                         {"transport": httpx.MockTransport(handler), "base_url": "http://yt", "sleep": sl, **(opts or {})})
        ad.start()
        try:
            await check(ad, events, sleeps)
        finally:
            await ad.stop()
    asyncio.run(go())


def test_waiting_then_polling_with_pagination_and_quota():
    now = time.time()
    st = {"videos": 0, "pages": []}

    def h(req):
        p = req.url.params
        if req.url.path.endswith("/videos"):
            st["videos"] += 1
            d = {} if st["videos"] == 1 else {"activeLiveChatId": "CHAT"}
            return httpx.Response(200, json={"items": [{"liveStreamingDetails": d}]})
        st["pages"].append(p.get("pageToken"))
        assert p["key"] == "AIza-secret-key" and p["liveChatId"] == "CHAT"
        n = len(st["pages"])
        if n == 1:
            return httpx.Response(200, json={"items": [item("old", now - 600), item("a", now), item("b", now)],
                                             "nextPageToken": "T1", "pollingIntervalMillis": 2500})
        if n == 2:
            return httpx.Response(200, json={"items": [item("b", now), item("c", now)], "nextPageToken": "T2", "pollingIntervalMillis": 2500})
        return httpx.Response(403, json={"error": {"message": "q", "errors": [{"reason": "quotaExceeded"}]}})

    async def check(ad, events, sleeps):
        await wait_for(lambda: ad.health.state == "degraded")
        assert [e.msg_id for e in events] == ["a", "b", "c"]      # history dropped, dup dropped
        assert st["pages"] == [None, "T1", "T2"]
        assert 30 in sleeps and 2.5 in sleeps                      # waiting retry + polling interval honoured
        assert "quota" in ad.health.action_needed.lower()
        assert events[0].user_id == "UCa" and events[0].sent_at and abs(events[0].sent_at - now) < 1
    run(h, check)


def test_connecting_state_while_not_live():
    def h(req):
        return httpx.Response(200, json={"items": [{"liveStreamingDetails": {}}]})

    async def check(ad, events, sleeps):
        await wait_for(lambda: ad.health.detail == "waiting for live chat")
        assert ad.health.state == "connecting"
    run(h, check)


def test_oauth_header_and_401():
    def h(req):
        assert req.headers["authorization"] == "Bearer oauth-secret-token"
        return httpx.Response(401, json={"error": {"errors": [{"reason": "authError"}]}})

    async def check(ad, events, sleeps):
        await wait_for(lambda: ad.health.state == "unavailable")
        assert "Reconnect" in ad.health.action_needed and "oauth-secret" not in ad.health.detail
    run(h, check, creds={"access_token": "oauth-secret-token"})
