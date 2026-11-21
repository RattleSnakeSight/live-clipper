"""One shared, authenticated Twitch EventSub WebSocket for every Twitch channel in this process.

Why: Twitch limits EventSub WebSocket transports per user token (documented: 3 connections per client_id + user,
300 enabled subscriptions per connection, a total cost cap). One socket per channel failed in production with
HTTP 429 "number of websocket transports limit exceeded" at nine channels. The hub keeps one connection and one
`channel.chat.message` subscription per broadcaster, and routes each notification only to the adapters attached to
that broadcaster.

Behaviour:
  * attach(adapter) registers a route (blocks until the adapter is stopped); detach deletes the broadcaster's
    subscription only when no other route still needs it, and closes the socket when no route is left.
  * session_reconnect: move to the new URL, subscriptions carry over (no resubscribe). Lost connection or keepalive
    timeout: reconnect with backoff, new session, every route subscribed again.
  * Errors are per route: an unknown login, 403, 400 or a 429 refusal marks only that channel (health + action) and
    is retried later with backoff; other channels stay healthy. A 429 is reported verbatim as a provider refusal
    (plus the documented limit it most likely hit) and honours Ratelimit-Reset. authorization_revoked or a token that
    cannot be renewed affects every route (same token).
  * Token renewal: credentials come from the shared TokenRefresher (renewed before expiry at each connect, once after
    a Helix 401). Subscriptions are created with the token owner's user_id, so their cost is 0 (Twitch's rule for
    subscriptions the authorizing user is part of); the reported total_cost / max_total_cost is shown in health.
options: eventsub_url, helix_url, transport (httpx, tests), sleep, keepalive_grace, retry_cap (seconds)
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import websockets

from ._util import AuthError, Backoff, redact, sleeper

EVENTSUB_URL = "wss://eventsub.wss.twitch.tv/ws"
HELIX_URL = "https://api.twitch.tv/helix"
ACT_401 = "Reconnect Twitch account (token expired)"
ACT_SCOPE = "Grant user:read:chat (reconnect Twitch account with that scope)"
ACT_LIMIT = ("Twitch refused another EventSub subscription/transport for this token. Stop other running copies of "
             "the app that use the same Twitch sign-in (each holds connections), or watch fewer channels")


@dataclass
class Route:
    adapter: object
    login: str
    bid: str | None = None
    retry_at: float = 0.0
    fails: int = 0


class TwitchEventSubHub:
    def __init__(self, creds_fn: Callable[[], dict], token_source=None, options: dict | None = None):
        self.creds_fn, self.token_source = creds_fn, token_source
        self.options = options or {}
        self._sleep = sleeper(self.options)
        self.routes: dict[int, Route] = {}
        self.subs: dict[str, str] = {}            # broadcaster id -> subscription id (current session)
        self.session_id: str | None = None
        self.creds: dict = {}
        self.uid: str | None = None
        self.quota: dict = {}
        self.connections = 0                      # websocket connects (status/tests)
        self.state, self.detail = "idle", ""
        self._ws = None
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None
        self._loop = None

    # ---------------------------------------------------------------- public
    async def attach(self, adapter) -> None:
        """Route `adapter.channel` through the shared connection until the calling task is cancelled."""
        self._bind()
        r = Route(adapter, adapter.channel.lower().lstrip("#"), bid=adapter.options.get("broadcaster_id"))
        self.routes[id(adapter)] = r
        self._set(r, "connecting", "EventSub (shared connection): subscribing")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="lc-twitch-eventsub-hub")
        self._wake.set()
        try:
            if self.session_id:                   # connected already: subscribe this channel now
                try:
                    async with self._client() as c:
                        await self._subscribe_route(c, r)
                except AuthError as e:            # token problem: shown here; the hub loop retries for everyone
                    self._set(r, "unavailable", str(e), e.action)
                except Exception as e:
                    self._retry_later(r, "connecting", f"subscribe failed: {type(e).__name__}; retrying")
            await asyncio.Event().wait()
        finally:
            self.routes.pop(id(adapter), None)
            await asyncio.shield(self._release(r))

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def status(self) -> dict:
        return {"state": self.state, "detail": self.detail, "channels": len(self.routes), "subscriptions": len(self.subs),
                "connections": self.connections, "quota": dict(self.quota)}

    # ---------------------------------------------------------------- internals
    def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:                # asyncio primitives belong to one loop
            self._loop, self._wake, self._lock, self._task = loop, asyncio.Event(), asyncio.Lock(), None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.options.get("transport"), timeout=15)

    def _secrets(self) -> list:
        return [self.creds.get("access_token", ""), self.creds.get("refresh_token", ""), self.creds.get("client_secret", "")]

    def _set(self, r: Route, state: str, detail: str, action: str = "") -> None:
        h = r.adapter.health
        h.state, h.detail, h.action_needed = state, redact(detail, self._secrets())[:300], action

    def _healthy_detail(self) -> str:
        q = self.quota
        cost = f", cost {q['total_cost']}/{q['max_total_cost']}" if "max_total_cost" in q else ""
        return f"EventSub websocket (shared connection, {len(self.subs)} channel subscriptions{cost})"

    def _mark_bid_healthy(self, bid: str) -> None:
        for r in self.routes.values():
            if r.bid == bid:
                r.fails, r.retry_at = 0, 0.0
                self._set(r, "healthy", self._healthy_detail())

    def _retry_later(self, r: Route, state: str, detail: str, action: str = "", at: float | None = None) -> None:
        r.fails += 1
        cap = float(self.options.get("retry_cap", 900))
        r.retry_at = at if at is not None else time.time() + min(cap, 30.0 * 2 ** (r.fails - 1))
        self._set(r, state, detail, action)

    async def _fresh(self, force: bool = False) -> None:
        stale = self.creds.get("access_token")
        if self.token_source is not None:
            self.creds = dict(await self.token_source.get("twitch", force=force, stale_token=stale if force else None))
        else:
            self.creds = dict(self.creds_fn() or {})
        if not (self.creds.get("access_token") and self.creds.get("client_id")):
            raise AuthError("no Twitch user token for EventSub", ACT_401)

    def _renewable(self) -> bool:
        return bool(self.token_source is not None and self.creds.get("refresh_token") and
                    self.token_source.renewable("twitch"))

    async def _helix(self, client, method: str, path: str, **kw) -> httpx.Response:
        """Helix request; one token renewal + retry on 401. Persistent 401 raises AuthError (affects all routes)."""
        url = self.options.get("helix_url", HELIX_URL) + path

        def hdr():
            return {"Client-Id": self.creds["client_id"], "Authorization": f"Bearer {self.creds['access_token']}"}
        r = await client.request(method, url, headers=hdr(), **kw)
        if r.status_code == 401 and self._renewable():
            await self._fresh(force=True)
            r = await client.request(method, url, headers=hdr(), **kw)
        if r.status_code == 401:
            raise AuthError("Twitch API 401", ACT_401)
        return r

    @staticmethod
    def _msg(r: httpx.Response) -> str:
        try:
            d = r.json()
            return str(d.get("message") or d.get("error") or "")[:160]
        except ValueError:
            return r.text[:160]

    async def _user_id(self, client) -> str:
        if self.creds.get("user_id"):
            return str(self.creds["user_id"])
        r = await self._helix(client, "GET", "/users")
        if r.status_code >= 400:
            raise RuntimeError(f"Twitch API {r.status_code}: {redact(self._msg(r), self._secrets())}")
        return str(r.json()["data"][0]["id"])

    async def _resolve(self, client, r: Route) -> bool:
        if r.bid:
            return True
        resp = await self._helix(client, "GET", "/users", params={"login": r.login})
        if resp.status_code >= 400:
            self._retry_later(r, "connecting", f"Twitch user lookup HTTP {resp.status_code}; retrying")
            return False
        d = resp.json().get("data") or []
        if not d:
            self._retry_later(r, "unavailable", f"Twitch channel '{r.login}' not found", "Check the Twitch channel login",
                              at=time.time() + float(self.options.get("retry_cap", 900)))
            return False
        r.bid = str(d[0]["id"])
        return True

    async def _subscribe_route(self, client, r: Route) -> None:
        """Subscribe r's broadcaster on the current session. Per-route errors stay on that route."""
        async with self._lock:
            sid = self.session_id
            if not sid or id(r.adapter) not in self.routes or time.time() < r.retry_at:
                return
            try:
                if not await self._resolve(client, r):
                    return
                if r.bid in self.subs:
                    self._mark_bid_healthy(r.bid)
                    return
                body = {"type": "channel.chat.message", "version": "1",
                        "condition": {"broadcaster_user_id": r.bid, "user_id": self.uid},
                        "transport": {"method": "websocket", "session_id": sid}}
                resp = await self._helix(client, "POST", "/eventsub/subscriptions", json=body)
            except AuthError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._retry_later(r, "connecting", f"subscribe failed: {type(e).__name__}: {e}; retrying")
                return
            if sid != self.session_id:
                return                              # session changed meanwhile; the new session resubscribes
            msg = redact(self._msg(resp), self._secrets())
            if resp.status_code in (200, 202):
                d = resp.json()
                data = d.get("data") or [{}]
                self.subs[r.bid] = str(data[0].get("id") or "")
                self.quota = {k: d[k] for k in ("total", "total_cost", "max_total_cost") if k in d}
                self._mark_bid_healthy(r.bid)
                for x in self.routes.values():      # refresh the shared detail (count/cost) on healthy routes
                    if x.adapter.health.state == "healthy":
                        self._set(x, "healthy", self._healthy_detail())
            elif resp.status_code == 409:           # already subscribed on this session
                self.subs.setdefault(r.bid, "")
                self._mark_bid_healthy(r.bid)
            elif resp.status_code == 429:
                reset = resp.headers.get("ratelimit-reset")
                at = float(reset) if reset and reset.isdigit() and float(reset) > time.time() else None
                self._retry_later(r, "unavailable", f"Twitch refused the subscription (HTTP 429: {msg or 'too many'})",
                                  ACT_LIMIT, at=at)
            elif resp.status_code == 403:
                self._retry_later(r, "unavailable", f"Twitch API 403 (missing scope or not permitted): {msg}",
                                  ACT_SCOPE, at=time.time() + float(self.options.get("retry_cap", 900)))
            else:
                self._retry_later(r, "unavailable" if resp.status_code < 500 else "connecting",
                                  f"Twitch subscription HTTP {resp.status_code}: {msg}")

    async def _subscribe_due(self, client) -> None:
        for r in list(self.routes.values()):
            if r.bid not in self.subs and time.time() >= r.retry_at:
                await self._subscribe_route(client, r)

    async def _release(self, r: Route) -> None:
        bid = r.bid
        if bid and not any(x.bid == bid for x in self.routes.values()):
            async with self._lock:
                sub = self.subs.pop(bid, None)
                if sub and self.session_id:
                    try:
                        async with self._client() as c:
                            await self._helix(c, "DELETE", "/eventsub/subscriptions", params={"id": sub})
                    except Exception:
                        pass                        # the subscription dies with the session anyway
        if not self.routes and self._ws is not None:
            try:
                await self._ws.close()              # last channel gone: drop the transport
            except Exception:
                pass

    def _all(self, state: str, detail: str, action: str = "") -> None:
        self.state, self.detail = state, detail
        for r in self.routes.values():
            self._set(r, state, detail, action)

    async def _run(self) -> None:
        bo = Backoff()
        while True:
            if not self.routes:
                self.state = "idle"
                self._wake.clear()
                await self._wake.wait()
                continue
            try:
                await self._session(bo)
                delay = 0.5
            except AuthError as e:
                self._all("unavailable", str(e), e.action)
                delay = 60.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self.routes:
                    self._all("connecting", f"reconnecting: {type(e).__name__}: {e}")
                    for r in self.routes.values():
                        r.adapter.health.reconnects += 1
                delay = bo.next()
            finally:
                self.session_id, self._ws = None, None
                self.subs.clear()
            if self.routes:
                await self._sleep(delay)

    async def _session(self, bo: Backoff) -> None:
        url = self.options.get("eventsub_url", EVENTSUB_URL)
        grace = float(self.options.get("keepalive_grace", 5.0))
        await self._fresh()
        async with self._client() as client:
            self.uid = await self._user_id(client)
            for r in list(self.routes.values()):    # look ids up before connecting (10 s subscribe deadline)
                if not r.bid and time.time() >= r.retry_at:
                    await self._resolve(client, r)
            if not self.routes:
                return
            ws = await websockets.connect(url)
            self._ws = ws
            self.connections += 1
            try:
                w = json.loads(await asyncio.wait_for(ws.recv(), 10))
                if w["metadata"]["message_type"] != "session_welcome":
                    raise RuntimeError("expected session_welcome")
                timeout = float(w["payload"]["session"].get("keepalive_timeout_seconds") or 10) + grace
                self.subs.clear()
                self.session_id = w["payload"]["session"]["id"]
                await self._subscribe_due(client)
                self.state, self.detail = "healthy", self._healthy_detail()
                if self.subs:
                    bo.reset()
                last = time.monotonic()
                while True:
                    due = [r.retry_at for r in self.routes.values() if r.bid not in self.subs]
                    wait = timeout - (time.monotonic() - last)
                    if due:
                        wait = min(wait, max(0.05, min(due) - time.time()))
                    try:
                        raw = await asyncio.wait_for(ws.recv(), max(0.05, wait))
                    except TimeoutError:
                        if time.monotonic() - last >= timeout:
                            raise ConnectionError("EventSub keepalive timeout")
                        await self._subscribe_due(client)
                        continue
                    last = time.monotonic()
                    msg = json.loads(raw)
                    mt = msg["metadata"]["message_type"]
                    if mt == "notification":
                        self._route(msg)
                    elif mt == "session_reconnect":
                        new = await websockets.connect(msg["payload"]["session"]["reconnect_url"])
                        try:
                            w2 = json.loads(await asyncio.wait_for(new.recv(), 10))
                            timeout = float(w2["payload"]["session"].get("keepalive_timeout_seconds") or 10) + grace
                        except BaseException:
                            await new.close()
                            raise
                        await ws.close()
                        ws = self._ws = new
                        self.session_id = w2["payload"]["session"].get("id") or self.session_id
                        self.connections += 1
                        for r in self.routes.values():
                            r.adapter.health.reconnects += 1
                    elif mt == "revocation":
                        self._revoked(msg)
                    if not self.routes:
                        return
            finally:
                await ws.close()

    def _route(self, msg: dict) -> None:
        sub = msg["payload"].get("subscription") or {}
        if sub.get("type") != "channel.chat.message":
            return
        ev = msg["payload"].get("event") or {}
        bid = str(ev.get("broadcaster_user_id") or (sub.get("condition") or {}).get("broadcaster_user_id") or "")
        for r in list(self.routes.values()):
            if r.bid and r.bid == bid:
                try:
                    r.adapter._on_notification(msg)
                except Exception as e:              # one channel's handler bug never stops the others
                    self._set(r, "healthy", f"dropped a malformed chat event: {type(e).__name__}")

    def _revoked(self, msg: dict) -> None:
        sub = msg["payload"]["subscription"]
        st = sub.get("status", "")
        if st == "authorization_revoked":
            raise AuthError(f"Twitch revoked subscription ({st})", ACT_401)
        bid = str((sub.get("condition") or {}).get("broadcaster_user_id") or "")
        if not bid:
            bid = next((b for b, s in self.subs.items() if s and s == sub.get("id")), "")
        self.subs.pop(bid, None)
        for r in self.routes.values():
            if r.bid == bid:
                self._retry_later(r, "unavailable", f"Twitch revoked this channel's subscription ({st})",
                                  "Check that the Twitch channel still exists" if st == "user_removed" else "")
