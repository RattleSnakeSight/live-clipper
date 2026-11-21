"""Shared Twitch EventSub transport: one websocket for many channels, per-broadcaster routing, independent
unsubscribe, reconnect + resubscribe, per-channel error isolation, 429 provider refusal, token renewal.
Offline only: a local fake EventSub websocket and an httpx MockTransport for Helix."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import websockets

from live_clipper.chat.twitch import TwitchChat
from live_clipper.chat.twitch_hub import ACT_LIMIT, TwitchEventSubHub

CREDS = {"client_id": "cid", "access_token": "AT-1"}


async def wait_for(cond, t=6):
    end = time.time() + t
    while not cond():
        assert time.time() < end, "timeout"
        await asyncio.sleep(0.02)


def notif(bid, mid, text="pog"):
    return json.dumps({"metadata": {"message_id": mid, "message_type": "notification",
                                    "message_timestamp": "2026-11-19T00:00:00Z"},
                       "payload": {"subscription": {"type": "channel.chat.message",
                                                    "condition": {"broadcaster_user_id": bid, "user_id": "U1"}},
                                   "event": {"broadcaster_user_id": bid, "message_id": "c-" + mid,
                                             "chatter_user_id": "9", "chatter_user_login": "viewer",
                                             "message": {"text": text, "fragments": []}}}})


class FakeTwitch:
    """Fake EventSub server + Helix. Records connections, subscriptions (per session) and deletions."""

    def __init__(self, refuse=(), ghosts=(), unauthorized_token=None):
        self.conns, self.posts, self.deletes, self.tokens = [], [], [], []
        self.refuse, self.ghosts, self.unauthorized_token = set(refuse), set(ghosts), unauthorized_token
        self.closed = 0
        self.port = 0

    async def handler(self, ws):
        sid = f"S{len(self.conns) + 1}"
        self.conns.append((sid, ws))
        await ws.send(json.dumps({"metadata": {"message_type": "session_welcome"},
                                  "payload": {"session": {"id": sid, "keepalive_timeout_seconds": 30}}}))
        try:
            await ws.wait_closed()
        finally:
            self.closed += 1

    async def send(self, raw):
        await self.conns[-1][1].send(raw)

    def helix(self, req: httpx.Request):
        tok = req.headers["authorization"].split()[-1]
        self.tokens.append(tok)
        if tok == self.unauthorized_token:
            return httpx.Response(401, json={"message": "Invalid OAuth token"})
        if req.method == "GET" and req.url.path == "/users":
            login = req.url.params.get("login")
            if login is None:
                return httpx.Response(200, json={"data": [{"id": "U1"}]})
            return httpx.Response(200, json={"data": [] if login in self.ghosts else [{"id": "id-" + login}]})
        if req.url.path == "/eventsub/subscriptions" and req.method == "POST":
            b = json.loads(req.content)
            bid, sid = b["condition"]["broadcaster_user_id"], b["transport"]["session_id"]
            assert b["condition"]["user_id"] == "U1" and b["type"] == "channel.chat.message"
            self.posts.append((sid, bid))
            if bid in self.refuse:
                return httpx.Response(429, json={"error": "Too Many Requests", "status": 429,
                                                 "message": "number of websocket transports limit exceeded"})
            return httpx.Response(202, json={"data": [{"id": f"sub-{bid}-{len(self.posts)}"}], "total": len(self.posts),
                                             "total_cost": 0, "max_total_cost": 10})
        if req.url.path == "/eventsub/subscriptions" and req.method == "DELETE":
            self.deletes.append(req.url.params.get("id"))
            return httpx.Response(204)
        return httpx.Response(404)


async def world(fake, logins, token_source=None):
    srv = await websockets.serve(fake.handler, "127.0.0.1", 0)
    fake.port = srv.sockets[0].getsockname()[1]
    hub = TwitchEventSubHub(lambda: dict(CREDS), token_source=token_source,
                            options={"eventsub_url": f"ws://127.0.0.1:{fake.port}", "helix_url": "http://helix",
                                     "transport": httpx.MockTransport(fake.helix), "keepalive_grace": 0.5,
                                     "sleep": lambda s: asyncio.sleep(0.02), "retry_cap": 0.3})
    events = {}
    ads = []
    for i, login in enumerate(logins):
        key = f"{login}#{i}"
        events[key] = []
        ad = TwitchChat(login, events[key].append, dict(CREDS), {"eventsub_hub": hub, "sleep": lambda s: asyncio.sleep(0.02)})
        ads.append(ad)
    for ad in ads:                                   # all channels start at once
        ad.start()
    return srv, hub, ads, events


async def teardown(srv, hub, ads):
    for ad in ads:
        await ad.stop()
    await hub.close()
    srv.close()
    await srv.wait_closed()


def test_simultaneous_channels_share_one_socket_and_route_by_broadcaster():
    async def go():
        fake = FakeTwitch()
        srv, hub, ads, ev = await world(fake, ["alpha", "bravo", "charlie", "alpha"])   # alpha twice (monitor + probe)
        try:
            await wait_for(lambda: all(a.health.state == "healthy" for a in ads))
            assert len(fake.conns) == 1                                       # one transport for every channel
            assert sorted(b for _, b in fake.posts) == ["id-alpha", "id-bravo", "id-charlie"]   # one sub per broadcaster
            assert {s for s, _ in fake.posts} == {"S1"}
            await fake.send(notif("id-bravo", "m1"))
            await fake.send(notif("id-alpha", "m2"))
            await fake.send(notif("id-unknown", "m3"))                        # not ours: dropped
            await wait_for(lambda: ev["bravo#1"] and ev["alpha#0"] and ev["alpha#3"])
            await asyncio.sleep(0.1)
            assert [e.msg_id for e in ev["bravo#1"]] == ["c-m1"] and ev["charlie#2"] == []
            assert [e.msg_id for e in ev["alpha#0"]] == ["c-m2"] == [e.msg_id for e in ev["alpha#3"]]
            assert all(e.channel == "alpha" for e in ev["alpha#0"])
            assert "shared connection" in ads[0].health.detail and "cost 0/10" in ads[0].health.detail
        finally:
            await teardown(srv, hub, ads)
    asyncio.run(go())


def test_independent_unsubscribe_and_last_channel_closes_socket():
    async def go():
        fake = FakeTwitch()
        srv, hub, ads, ev = await world(fake, ["alpha", "bravo", "bravo"])
        try:
            await wait_for(lambda: all(a.health.state == "healthy" for a in ads))
            bravo_sub = next(f"sub-{b}-{i + 1}" for i, (_, b) in enumerate(fake.posts) if b == "id-bravo")
            await ads[1].stop()                                               # bravo still needed by ads[2]
            assert fake.deletes == []
            await ads[2].stop()                                               # last bravo route: delete only bravo
            assert fake.deletes == [bravo_sub]
            await fake.send(notif("id-alpha", "m1"))
            await wait_for(lambda: ev["alpha#0"])
            assert ads[0].health.state == "healthy" and len(fake.conns) == 1
            await ads[0].stop()                                               # nothing left: transport closed
            await wait_for(lambda: fake.closed == 1)
            assert len(fake.deletes) == 2 and hub.status()["channels"] == 0
            # a channel armed later reopens one socket
            again = TwitchChat("charlie", lambda e: None, dict(CREDS), {"eventsub_hub": hub})
            ads.append(again)
            again.start()
            await wait_for(lambda: again.health.state == "healthy")
            assert len(fake.conns) == 2
        finally:
            await teardown(srv, hub, ads)
    asyncio.run(go())


def test_disconnect_reconnects_and_resubscribes_every_channel():
    async def go():
        fake = FakeTwitch()
        srv, hub, ads, ev = await world(fake, ["alpha", "bravo"])
        try:
            await wait_for(lambda: all(a.health.state == "healthy" for a in ads))
            await fake.conns[-1][1].close()                                   # provider drops the socket
            await wait_for(lambda: len(fake.conns) == 2 and len(fake.posts) == 4 and
                           all(a.health.state == "healthy" for a in ads))
            assert sorted(b for s, b in fake.posts if s == "S2") == ["id-alpha", "id-bravo"]
            assert all(a.health.reconnects >= 1 for a in ads)
            await fake.send(notif("id-bravo", "after"))
            await wait_for(lambda: ev["bravo#1"])
            assert ev["alpha#0"] == []
        finally:
            await teardown(srv, hub, ads)
    asyncio.run(go())


def test_session_reconnect_moves_without_resubscribing():
    async def go():
        fake = FakeTwitch()
        srv, hub, ads, ev = await world(fake, ["alpha", "bravo"])
        try:
            await wait_for(lambda: all(a.health.state == "healthy" for a in ads))
            await fake.send(json.dumps({"metadata": {"message_type": "session_reconnect"},
                                        "payload": {"session": {"reconnect_url": f"ws://127.0.0.1:{fake.port}/r"}}}))
            await wait_for(lambda: len(fake.conns) == 2)
            await asyncio.sleep(0.1)
            await fake.send(notif("id-alpha", "moved"))
            await wait_for(lambda: ev["alpha#0"])
            assert len(fake.posts) == 2                                       # subscriptions carried over
        finally:
            await teardown(srv, hub, ads)
    asyncio.run(go())


def test_one_channel_refused_or_missing_does_not_affect_others():
    async def go():
        fake = FakeTwitch(refuse={"id-bravo"}, ghosts={"ghost"})
        srv, hub, ads, ev = await world(fake, ["alpha", "bravo", "ghost", "charlie"])
        try:
            await wait_for(lambda: ads[0].health.state == ads[3].health.state == "healthy" and
                           ads[1].health.state == ads[2].health.state == "unavailable")
            b = ads[1].health
            assert "HTTP 429" in b.detail and "number of websocket transports limit exceeded" in b.detail
            assert b.action_needed == ACT_LIMIT
            assert "not found" in ads[2].health.detail
            await fake.send(notif("id-charlie", "m1"))
            await wait_for(lambda: ev["charlie#3"])
            n = sum(1 for _, x in fake.posts if x == "id-bravo")
            await wait_for(lambda: sum(1 for _, x in fake.posts if x == "id-bravo") > n)   # retried with backoff
            fake.refuse.clear()                                               # limit freed: recovers on retry
            await wait_for(lambda: ads[1].health.state == "healthy")
            assert len(fake.conns) == 1 and ads[0].health.state == "healthy"
        finally:
            await teardown(srv, hub, ads)
    asyncio.run(go())


def test_revocation_of_one_subscription_is_isolated():
    async def go():
        fake = FakeTwitch()
        srv, hub, ads, ev = await world(fake, ["alpha", "bravo"])
        try:
            await wait_for(lambda: all(a.health.state == "healthy" for a in ads))
            await fake.send(json.dumps({"metadata": {"message_type": "revocation"}, "payload": {"subscription": {
                "id": "x", "status": "user_removed", "type": "channel.chat.message",
                "condition": {"broadcaster_user_id": "id-bravo", "user_id": "U1"}}}}))
            await wait_for(lambda: ads[1].health.state == "unavailable")
            assert "user_removed" in ads[1].health.detail and ads[0].health.state == "healthy"
            await fake.send(notif("id-alpha", "m1"))
            await wait_for(lambda: ev["alpha#0"])
            assert len(fake.conns) == 1
        finally:
            await teardown(srv, hub, ads)
    asyncio.run(go())


def test_401_renews_shared_token_once_and_retries():
    class Source:
        def __init__(self):
            self.cur, self.forced = {**CREDS, "refresh_token": "RT"}, 0

        async def get(self, platform, force=False, stale_token=None):
            if force and stale_token == self.cur["access_token"]:
                self.forced += 1
                self.cur = {**self.cur, "access_token": "AT-2"}
            return dict(self.cur)

        def renewable(self, platform):
            return True

    async def go():
        fake = FakeTwitch(unauthorized_token="AT-1")
        src = Source()
        srv, hub, ads, ev = await world(fake, ["alpha", "bravo", "charlie"], token_source=src)
        try:
            await wait_for(lambda: all(a.health.state == "healthy" for a in ads))
            assert src.forced == 1 and fake.tokens[-1] == "AT-2" and len(fake.conns) == 1
        finally:
            await teardown(srv, hub, ads)
    asyncio.run(go())


def test_engine_monitors_use_the_shared_hub(tmp_path, monkeypatch):
    from monitor_env import env

    from live_clipper.chat import registry
    from live_clipper.chat.twitch_hub import TwitchEventSubHub as H
    from live_clipper.engine import Engine
    s, store, sec = env(tmp_path)
    eng = Engine(s, store, sec)
    assert isinstance(eng.twitch_hub, H) and eng.twitch_hub.token_source is eng.tokens
    seen = []

    class Dummy:
        def __init__(self, platform):
            self.platform = platform

        def start(self):
            pass

        async def stop(self):
            pass
    monkeypatch.setattr(registry, "make_adapter", lambda p, ch, emit, creds, opts: seen.append((p, opts)) or Dummy(p))

    async def go():
        ids = [store.add_channel("twitch", f"t{i}") for i in range(3)] + [store.add_channel("kick", "k0")]
        for cid in ids:
            eng.arm(cid)
            eng.monitors[cid].capture.start = lambda: None
            eng.monitors[cid]._start_media_and_chat()
        for cid in ids:
            await eng.disarm(cid)
    asyncio.run(go())
    tw = [o for p, o in seen if p == "twitch"]
    assert len(tw) >= 3 and all(o["eventsub_hub"] is eng.twitch_hub for o in tw)          # 3 monitors
    assert all("eventsub_hub" not in o for p, o in seen if p == "kick")                   # Kick untouched
