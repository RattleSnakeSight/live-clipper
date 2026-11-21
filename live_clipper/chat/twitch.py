"""Twitch chat adapter.

Primary: EventSub WebSocket, `channel.chat.message` v1 (needs client_id + user access token with
`user:read:chat`; the token's owner is the "reading user"). Fallback when no token: anonymous
read-only IRC over WebSocket (`justinfan*`), which needs no credentials.

creds:   client_id, access_token, user_id (optional), broadcaster_id (optional)
options: eventsub_url, helix_url, irc_url, transport (httpx transport, tests), sleep, keepalive_grace,
         token_source (live_clipper.chat_tokens.TokenRefresher: renews an owner sign-in before expiry at each (re)connect and
         once after a Helix 401; manual tokens without a refresh_token are used as given),
         eventsub_hub (live_clipper.chat.twitch_hub.TwitchEventSubHub: share one EventSub websocket across all channels;
         without it the adapter opens its own socket, as in standalone use and tests)

Verified from memory of Twitch docs (no network doc check): EventSub welcome/keepalive/reconnect/
revocation message shapes, `session_id` + transport.method=websocket subscription body, 10s
subscribe deadline, tag names for IRC. Not exercised against EventSub live (needs a user token).
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from collections import OrderedDict

import httpx
import websockets

from ..events import ChatEvent
from ._util import AuthError, Backoff, parse_rfc3339, redact, sleeper
from .base import ChatAdapter

EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws"
HELIX_URL = "https://api.twitch.tv/helix"
IRC_URL = "wss://irc-ws.chat.twitch.tv:443"
ACT_401 = "Reconnect Twitch account (token expired)"
ACT_401_SIGNIN = "Reconnect Twitch (Sources > Twitch > Sign in): the token was refused even after renewal"
ACT_SCOPE = "Grant user:read:chat (reconnect Twitch account with that scope)"
ACT_ANON = "Connect a Twitch account (scope user:read:chat) to use EventSub instead of anonymous IRC"


def _unescape(v: str) -> str:
    return (v.replace("\\s", " ").replace("\\:", ";").replace("\\r", "\r")
             .replace("\\n", "\n").replace("\\\\", "\\"))


def parse_irc(line: str) -> tuple[dict, str, str, list[str]]:
    """':prefix' / '@tags' aware parse -> (tags, prefix, command, params)."""
    tags: dict[str, str] = {}
    if line.startswith("@"):
        raw, _, line = line.partition(" ")
        for kv in raw[1:].split(";"):
            k, _, v = kv.partition("=")
            tags[k] = _unescape(v)
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    head, sep, trailing = line.partition(" :")
    parts = head.split()
    params = parts[1:] + ([trailing] if sep else [])
    return tags, prefix, (parts[0] if parts else ""), params


def irc_emote_names(tag: str, text: str) -> tuple[str, ...]:
    """emotes tag '25:0-4,12-16/1902:6-10' -> names sliced from text (de-duplicated, in order)."""
    out: list[str] = []
    spans = []
    for grp in filter(None, tag.split("/")):
        _, _, ranges = grp.partition(":")
        for r in ranges.split(","):
            try:
                a, b = r.split("-")
                spans.append((int(a), int(b)))
            except ValueError:
                pass
    for a, b in sorted(spans):
        name = text[a:b + 1]
        if name and name not in out:
            out.append(name)
    return tuple(out)


class TwitchChat(ChatAdapter):
    platform = "twitch"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._sleep = sleeper(self.options)
        self._bo = Backoff()
        self._secrets = [self.creds.get("access_token", ""), self.creds.get("client_secret", "")]

    # ---------- shared ----------
    def _dedupe(self, mid: str) -> bool:
        if mid in self._seen:
            return True
        self._seen[mid] = None
        if len(self._seen) > 5000:
            self._seen.popitem(last=False)
        return False

    def _emit(self, ev: ChatEvent) -> None:
        self.emit(ev)
        self.health.last_message_at = ev.received_at

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.options.get("transport"), timeout=15)

    async def _fresh_creds(self, force: bool = False) -> None:
        """Pull current (possibly renewed) credentials from the shared refresher; no-op without one."""
        ts = self.options.get("token_source")
        if ts is None:
            return
        stale = self.creds.get("access_token")
        self.creds = {**self.creds, **await ts.get("twitch", force=force, stale_token=stale if force else None)}
        self._secrets = [*self._secrets, self.creds.get("access_token", ""), self.creds.get("refresh_token", "")][-12:]

    def _renewable(self) -> bool:
        ts = self.options.get("token_source")
        return bool(ts is not None and self.creds.get("refresh_token") and ts.renewable("twitch"))

    async def run(self) -> None:
        bo = self._bo
        while True:
            try:
                await self._fresh_creds()            # renew an expiring owner sign-in before (re)connecting
                if self.creds.get("access_token") and self.creds.get("client_id"):
                    hub = self.options.get("eventsub_hub")
                    if hub is not None:
                        await hub.attach(self)       # shared transport; returns only when this adapter stops
                    else:
                        await self._eventsub()
                else:
                    await self._irc()
                delay = 1.0
            except AuthError as e:
                self.health.state, self.health.detail, self.health.action_needed = "unavailable", str(e), e.action
                delay = 60.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.health.state = "connecting"
                self.health.detail = f"reconnecting: {redact(f'{type(e).__name__}: {e}', self._secrets)}"[:300]
                delay = bo.next()
            self.health.reconnects += 1
            await self._sleep(delay)

    # ---------- EventSub ----------
    def _helix_headers(self) -> dict:
        return {"Client-Id": self.creds["client_id"], "Authorization": f"Bearer {self.creds['access_token']}"}

    async def _helix(self, client: httpx.AsyncClient, method: str, path: str, **kw) -> dict:
        base = self.options.get("helix_url", HELIX_URL)
        r = await client.request(method, base + path, headers=self._helix_headers(), **kw)
        if r.status_code == 401 and self._renewable():
            await self._fresh_creds(force=True)     # TokenRevoked (an AuthError) propagates with its clear action
            r = await client.request(method, base + path, headers=self._helix_headers(), **kw)
            if r.status_code == 401:
                raise AuthError("Twitch API 401 after token renewal", ACT_401_SIGNIN)
        if r.status_code == 401:
            raise AuthError("Twitch API 401", ACT_401)
        if r.status_code == 403:
            raise AuthError("Twitch API 403 (missing scope or not permitted)", ACT_SCOPE)
        if r.status_code >= 400:
            raise RuntimeError(f"Twitch API {r.status_code}: {redact(r.text[:150], self._secrets)}")
        return r.json() if r.content else {}

    async def _resolve_ids(self, client) -> tuple[str, str]:
        uid = self.creds.get("user_id")
        if not uid:
            uid = (await self._helix(client, "GET", "/users"))["data"][0]["id"]
        bid = self.creds.get("broadcaster_id")
        if not bid:
            d = (await self._helix(client, "GET", "/users", params={"login": self.channel}))["data"]
            if not d:
                raise AuthError(f"Twitch channel '{self.channel}' not found", "Check the Twitch channel login")
            bid = d[0]["id"]
        return str(uid), str(bid)

    async def _subscribe(self, client, session_id: str, uid: str, bid: str) -> None:
        body = {"type": "channel.chat.message", "version": "1",
                "condition": {"broadcaster_user_id": bid, "user_id": uid},
                "transport": {"method": "websocket", "session_id": session_id}}
        await self._helix(client, "POST", "/eventsub/subscriptions", json=body)

    @staticmethod
    def _timeout(welcome: dict, grace: float) -> float:
        return float(welcome["payload"]["session"].get("keepalive_timeout_seconds") or 10) + grace

    async def _eventsub(self) -> None:
        url = self.options.get("eventsub_url", EVENTSUB_URL)
        grace = float(self.options.get("keepalive_grace", 5.0))
        self.health.state, self.health.detail, self.health.action_needed = "connecting", "EventSub: connecting", ""
        async with self._client() as client:
            uid, bid = await self._resolve_ids(client)
            ws = await websockets.connect(url)
            try:
                welcome = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if welcome["metadata"]["message_type"] != "session_welcome":
                    raise RuntimeError("expected session_welcome")
                await self._subscribe(client, welcome["payload"]["session"]["id"], uid, bid)
                timeout = self._timeout(welcome, grace)
                self.health.state, self.health.detail = "healthy", "EventSub websocket (channel.chat.message)"
                self._bo.reset()
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout)
                    except TimeoutError:
                        raise ConnectionError("EventSub keepalive timeout")
                    msg = json.loads(raw)
                    mt = msg["metadata"]["message_type"]
                    if mt == "session_reconnect":
                        new = await websockets.connect(msg["payload"]["session"]["reconnect_url"])
                        try:
                            w2 = json.loads(await asyncio.wait_for(new.recv(), 10))
                            timeout = self._timeout(w2, grace)
                        except BaseException:
                            await new.close()
                            raise
                        await ws.close()
                        ws = new
                        self.health.reconnects += 1
                    elif mt == "notification":
                        self._on_notification(msg)
                    elif mt == "revocation":
                        sub = msg["payload"]["subscription"]
                        st = sub.get("status", "")
                        if st in ("authorization_revoked", "user_removed"):
                            raise AuthError(f"Twitch revoked subscription ({st})", ACT_401)
                        raise ConnectionError(f"subscription revoked: {st}")
                    # session_keepalive: nothing to do, the wait_for timeout is the watchdog
            finally:
                await ws.close()

    def _on_notification(self, msg: dict) -> None:
        if msg["payload"]["subscription"]["type"] != "channel.chat.message":
            return
        mid = msg["metadata"]["message_id"]
        if self._dedupe(mid):
            return
        e = msg["payload"]["event"]
        m = e.get("message", {})
        emotes = tuple(dict.fromkeys(f["text"] for f in m.get("fragments", []) if f.get("type") == "emote"))
        self._emit(ChatEvent(
            platform="twitch", channel=self.channel, msg_id=e.get("message_id", mid),
            user_id=str(e.get("chatter_user_id", "")), user_name=e.get("chatter_user_login") or e.get("chatter_user_name", ""),
            text=m.get("text", ""), received_at=time.time(),
            sent_at=parse_rfc3339(msg["metadata"].get("message_timestamp")), emotes=emotes))

    # ---------- anonymous IRC ----------
    async def _irc(self) -> None:
        url = self.options.get("irc_url", IRC_URL)
        chan = self.channel.lower().lstrip("#")
        self.health.state = "connecting"
        self.health.detail = "anonymous read-only IRC: connecting"
        self.health.action_needed = ACT_ANON
        async with websockets.connect(url) as ws:
            for line in ("CAP REQ :twitch.tv/tags", "PASS SCHMOOPIIE", f"NICK justinfan{random.randint(10000, 99999)}", f"JOIN #{chan}"):
                await ws.send(line)
            while True:
                try:
                    data = await asyncio.wait_for(ws.recv(), float(self.options.get("irc_idle", 600)))
                except TimeoutError:
                    raise ConnectionError("IRC idle timeout")
                for line in str(data).split("\r\n"):
                    if line:
                        await self._irc_line(ws, line)

    async def _irc_line(self, ws, line: str) -> None:
        if line.startswith("PING"):
            await ws.send("PONG" + line[4:])
            return
        tags, prefix, cmd, params = parse_irc(line)
        if cmd in ("JOIN", "366"):
            self.health.state = "healthy"
            self.health.detail = "anonymous read-only IRC (no account connected)"
            self._bo.reset()
        elif cmd == "RECONNECT":
            raise ConnectionError("server requested reconnect")
        elif cmd == "NOTICE" and "Login unsuccessful" in line:
            raise RuntimeError("IRC login rejected")
        elif cmd == "PRIVMSG" and len(params) >= 2:
            mid = tags.get("id") or f"{tags.get('tmi-sent-ts')}-{prefix}"
            if self._dedupe(mid):
                return
            text = params[1]
            ts = tags.get("tmi-sent-ts")
            self._emit(ChatEvent(
                platform="twitch", channel=self.channel, msg_id=mid,
                user_id=tags.get("user-id", ""), user_name=tags.get("display-name") or prefix.split("!")[0],
                text=text, received_at=time.time(), sent_at=int(ts) / 1000 if ts and ts.isdigit() else None,
                emotes=irc_emote_names(tags.get("emotes", ""), text)))
