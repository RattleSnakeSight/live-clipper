import asyncio
import json
import time

import httpx
import websockets

from live_clipper.chat._util import parse_rfc3339
from live_clipper.chat.twitch import TwitchChat, irc_emote_names, parse_irc

CREDS = {"client_id": "cid", "access_token": "tok-secret-123456", "user_id": "1", "broadcaster_id": "2"}


def welcome(sid="S1", ka=1):
    return json.dumps({"metadata": {"message_type": "session_welcome"},
                       "payload": {"session": {"id": sid, "keepalive_timeout_seconds": ka}}})


def notif(mid="m1", text="hi KEKW"):
    return json.dumps({"metadata": {"message_id": mid, "message_type": "notification",
                                    "message_timestamp": "2024-01-01T00:00:00.123456789Z"},
                       "payload": {"subscription": {"type": "channel.chat.message"},
                                   "event": {"message_id": "chat-" + mid, "chatter_user_id": "9", "chatter_user_login": "bob",
                                             "message": {"text": text, "fragments": [{"type": "text", "text": "hi "}, {"type": "emote", "text": "KEKW"}]}}}})


async def wait_for(cond, t=5):
    end = time.time() + t
    while not cond():
        assert time.time() < end, "timeout"
        await asyncio.sleep(0.02)


def helix(calls, status=202):
    def h(req: httpx.Request):
        calls.append((req.method, req.url.path, req.content))
        return httpx.Response(status, json={"data": []})
    return httpx.MockTransport(h)


async def _run(handler, creds, extra, check):
    events = []
    async with websockets.serve(handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        calls = []
        opts = {"eventsub_url": f"ws://127.0.0.1:{port}", "irc_url": f"ws://127.0.0.1:{port}", "transport": helix(calls),
                "helix_url": "http://h", "keepalive_grace": 0.2, "sleep": lambda s: asyncio.sleep(0.01), **extra}
        ad = TwitchChat("chan", events.append, creds, opts)
        ad.start()
        try:
            await check(ad, events, calls, port)
        finally:
            await ad.stop()


def test_eventsub_notification():
    async def handler(ws):
        await ws.send(welcome(ka=30))
        await asyncio.sleep(0.2)
        await ws.send(notif())
        await ws.send(notif())                      # duplicate delivery
        await ws.wait_closed()

    async def check(ad, events, calls, port):
        await wait_for(lambda: events)
        await asyncio.sleep(0.1)
        assert len(events) == 1
        e = events[0]
        assert (e.user_id, e.user_name, e.text, e.emotes, e.msg_id) == ("9", "bob", "hi KEKW", ("KEKW",), "chat-m1")
        assert abs(e.sent_at - 1704067200.123456) < 1e-3
        body = json.loads(calls[0][2])
        assert calls[0][1] == "/eventsub/subscriptions" and body["transport"]["session_id"] == "S1"
        assert body["type"] == "channel.chat.message" and ad.health.state == "healthy"
    asyncio.run(_run(handler, CREDS, {}, check))


def test_keepalive_timeout_reconnects():
    conns = []

    async def handler(ws):
        conns.append(1)
        await ws.send(welcome(ka=1))
        await ws.wait_closed()  # silence -> client must reconnect

    async def check(ad, events, calls, port):
        # wait for the reconnect AND the re-subscription it triggers (asserting at the 2nd connect raced it)
        await wait_for(lambda: len(conns) >= 2 and len(calls) >= 2 and ad.health.reconnects >= 1, 6)
        assert ad.health.reconnects >= 1 and len(calls) >= 2
    asyncio.run(_run(handler, CREDS, {}, check))


def test_session_reconnect():
    conns = []

    async def handler(ws):
        n = len(conns)
        conns.append(n)
        if n == 0:
            await ws.send(welcome("S1", 30))
            await ws.send(json.dumps({"metadata": {"message_type": "session_reconnect"},
                                      "payload": {"session": {"reconnect_url": f"ws://127.0.0.1:{PORT[0]}/new"}}}))
            await ws.wait_closed()
        else:
            await ws.send(welcome("S2", 30))
            await asyncio.sleep(0.1)
            await ws.send(notif("m2"))
            await ws.wait_closed()

    PORT = [0]

    async def check(ad, events, calls, port):
        await wait_for(lambda: events)
        assert len(calls) == 1                      # no resubscribe on session_reconnect
        assert len(conns) == 2 and ad.health.state == "healthy"

    async def go():
        async def h2(ws):
            await handler(ws)
        async with websockets.serve(h2, "127.0.0.1", 0) as srv:
            PORT[0] = srv.sockets[0].getsockname()[1]
            events, calls = [], []
            ad = TwitchChat("chan", events.append, CREDS, {"eventsub_url": f"ws://127.0.0.1:{PORT[0]}", "transport": helix(calls),
                                                          "helix_url": "http://h", "sleep": lambda s: asyncio.sleep(0.01)})
            ad.start()
            try:
                await check(ad, events, calls, PORT[0])
            finally:
                await ad.stop()
    asyncio.run(go())


def test_401_and_403_health():
    async def handler(ws):
        await ws.send(welcome())
        await ws.wait_closed()

    for status, frag in ((401, "token expired"), (403, "user:read:chat")):
        async def check(ad, events, calls, port, frag=frag):
            await wait_for(lambda: ad.health.state == "unavailable")
            assert frag in ad.health.action_needed
            assert "tok-secret" not in ad.health.detail

        async def go(status=status, check=check):
            async with websockets.serve(handler, "127.0.0.1", 0) as srv:
                port = srv.sockets[0].getsockname()[1]
                ad = TwitchChat("chan", lambda e: None, CREDS, {"eventsub_url": f"ws://127.0.0.1:{port}",
                                "transport": helix([], status), "helix_url": "http://h", "sleep": lambda s: asyncio.sleep(5)})
                ad.start()
                try:
                    await check(ad, [], [], port)
                finally:
                    await ad.stop()
        asyncio.run(go())


def test_irc_parse_helpers():
    tags, prefix, cmd, params = parse_irc("@id=abc;display-name=Bo\\sB;tmi-sent-ts=1700000000123 :bob!bob@bob.tmi.twitch.tv PRIVMSG #chan :Kappa hello : x")
    assert tags["display-name"] == "Bo B" and cmd == "PRIVMSG" and params == ["#chan", "Kappa hello : x"]
    assert irc_emote_names("25:0-4,12-16/1902:6-10", "Kappa Keepo Kappa") == ("Kappa", "Keepo")
    assert parse_rfc3339("2024-01-01T00:00:00.5Z") == 1704067200.5


def test_irc_anonymous_privmsg_and_ping():
    got = {}

    async def handler(ws):
        got["sent"] = []
        for _ in range(4):
            got["sent"].append(await ws.recv())
        await ws.send(":tmi.twitch.tv 001 justinfan1 :Welcome\r\n:x!x@x.tmi.twitch.tv JOIN #chan")
        await ws.send("PING :tmi.twitch.tv")
        got["pong"] = await ws.recv()
        await ws.send("@emotes=25:0-4;id=m-1;user-id=77;display-name=Al;tmi-sent-ts=1700000000123 :al!al@al.tmi.twitch.tv PRIVMSG #chan :Kappa go\r\n")
        await ws.wait_closed()

    async def check(ad, events, calls, port):
        await wait_for(lambda: events)
        e = events[0]
        assert (e.msg_id, e.user_id, e.user_name, e.text, e.emotes, e.sent_at) == ("m-1", "77", "Al", "Kappa go", ("Kappa",), 1700000000.123)
        assert got["pong"] == "PONG :tmi.twitch.tv"
        assert got["sent"][0] == "CAP REQ :twitch.tv/tags" and got["sent"][-1] == "JOIN #chan"
        assert any(s.startswith("NICK justinfan") for s in got["sent"])
        assert ad.health.state == "healthy" and "anonymous" in ad.health.detail and "EventSub" in ad.health.action_needed
        assert calls == []                          # no Helix traffic without a token
    asyncio.run(_run(handler, {}, {}, check))
