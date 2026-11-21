"""Kick chat adapter: webhook receiver for `chat.message.sent` (v1).

Kick has no public chat websocket; the official API delivers events by webhook to the URL set in
the app's developer settings (kick.com/settings/developer). The server must route POSTs to
`KickChat.handle_webhook(headers, body) -> http_status`.

creds:   access_token (owner's user token, scopes `user:read events:subscribe`) and/or client_id + client_secret
         (app access token via client_credentials). Kick documents that an app access token may subscribe to
         another creator's events by passing an explicit broadcaster_user_id, so third-party chat works in app mode:
         used when no user token is set, or when the user token is refused (403) for that broadcaster.
options: public_url (required; the public HTTPS URL that forwards to the Kick webhook listener, Sources > Kick),
         base_url, transport (httpx, tests), sleep, allow_unverified (bool), public_key_pem,
         broadcaster_user_id (skip slug lookup), resubscribe_every (seconds, default 600),
         token_source (live_clipper.chat_tokens.TokenRefresher: renews the owner's user OAuth token before expiry and once
         after a 401; a manual token without refresh_token is used as given; a revoked sign-in falls back to the app
         access token when client credentials exist, otherwise fails with a clear action)

Verified against github.com/KickEngineering/KickDevDocs and https://api.kick.com/swagger/doc.yaml
(fetched 2026-11): POST /public/v1/events/subscriptions body {broadcaster_user_id?, events:
[{name, version}], method:"webhook"}; GET same path lists subscriptions; GET
/public/v1/channels?slug= (returns data[].broadcaster_user_id, slug); GET /public/v1/public-key
(data.public_key, PEM); webhook headers; signature = base64 RSA PKCS1v15 SHA-256 over
"{message_id}.{timestamp}.{raw body}"; chat payload fields (message_id, sender.user_id/username,
broadcaster.channel_slug, content with [emote:id:NAME], emotes, created_at RFC3339).
Assumptions: the public-key endpoint works with our bearer token; list-subscriptions response items
carry `event`/`name`; the webhook URL itself is configured in the Kick app, not via API (so
public_url is only checked for presence, not registered).
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections import OrderedDict

import httpx

from ..events import ChatEvent
from ._util import AuthError, parse_rfc3339, redact, sleeper
from .base import ChatAdapter

BASE = "https://api.kick.com/public/v1"
ACT_URL = ("Kick delivers chat only by webhook to a public HTTPS URL. Set one in Sources > Kick (for example a "
          "tunnel to the local webhook listener), or leave Kick chat off")
ACT_401 = "Reconnect Kick account (token expired)"
ACT_SCOPE = "Grant events:subscribe (reconnect Kick account with that scope)"
_EMOTE = re.compile(r"\[emote:\d+:([^\]]+)\]")


class KickChat(ChatAdapter):
    platform = "kick"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._sleep = sleeper(self.options)
        self._secrets = [self.creds.get("access_token", "")]
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._pem: bytes | None = (self.options["public_key_pem"].encode() if self.options.get("public_key_pem") else None)
        self.broadcaster_id = self.options.get("broadcaster_user_id")
        self._got_webhook = False

    # ---------- API ----------
    async def _app_token(self, client) -> str:
        tok = getattr(self, "_app", None)
        if tok and tok[1] > time.time() + 60:
            return tok[0]
        r = await client.post(self.options.get("id_base_url", "https://id.kick.com") + "/oauth/token",
                              data={"grant_type": "client_credentials", "client_id": self.creds.get("client_id", ""),
                                    "client_secret": self.creds.get("client_secret", "")})
        if r.status_code != 200:
            raise AuthError(f"Kick app token request failed (HTTP {r.status_code})", ACT_401)
        d = r.json()
        self._app = (d["access_token"], time.time() + float(d.get("expires_in") or 3600))
        self._secrets = [*self._secrets, d["access_token"]]
        return d["access_token"]

    def _can_app(self) -> bool:
        return bool(self.creds.get("client_id") and self.creds.get("client_secret"))

    def _renewable(self) -> bool:
        ts = self.options.get("token_source")
        return bool(ts is not None and self.creds.get("refresh_token") and ts.renewable("kick"))

    async def _fresh_creds(self, force: bool = False) -> bool:
        """Renew the owner's user token via the shared refresher. False = sign-in revoked and the app access token
        takes over (client credentials present); TokenRevoked propagates when there is no app fallback."""
        from ..chat_tokens import TokenRevoked
        ts = self.options.get("token_source")
        if ts is None or getattr(self, "mode", "user") == "app":
            return True
        stale = self.creds.get("access_token")
        try:
            self.creds = {**self.creds, **await ts.get("kick", force=force, stale_token=stale if force else None)}
        except TokenRevoked as e:
            if not self._can_app():
                raise
            self.mode, self.user_token_note = "app", str(e)
            return False
        self._secrets = [*self._secrets, self.creds.get("access_token", ""), self.creds.get("refresh_token", "")][-12:]
        return True

    async def _bearer(self, client) -> str:
        if getattr(self, "mode", "user") != "app" and self.creds.get("access_token"):
            await self._fresh_creds()               # renew an expiring owner sign-in (no-op for manual tokens)
        if getattr(self, "mode", "user") == "app" or (not self.creds.get("access_token") and self._can_app()):
            self.mode = "app"
            return await self._app_token(client)
        self.mode = "user"
        return self.creds.get("access_token", "")

    async def _api(self, client, method: str, path: str, **kw) -> dict:
        r = await client.request(method, self.options.get("base_url", BASE) + path,
                                 headers={"Authorization": f"Bearer {await self._bearer(client)}"}, **kw)
        if r.status_code == 401 and getattr(self, "mode", "user") == "user" and self._renewable():
            await self._fresh_creds(force=True)     # one renewal, then retry (app mode if the sign-in was revoked)
            r = await client.request(method, self.options.get("base_url", BASE) + path,
                                     headers={"Authorization": f"Bearer {await self._bearer(client)}"}, **kw)
        if r.status_code in (401, 403) and getattr(self, "mode", "user") == "user" and self._can_app():
            self.mode = "app"                     # user token not permitted for this broadcaster: app access token
            r = await client.request(method, self.options.get("base_url", BASE) + path,
                                     headers={"Authorization": f"Bearer {await self._app_token(client)}"}, **kw)
        if r.status_code == 401:
            raise AuthError("Kick API 401", ACT_401)
        if r.status_code == 403:
            raise AuthError("Kick API 403", ACT_SCOPE)
        if r.status_code >= 400:
            raise RuntimeError(f"Kick API {r.status_code}: {redact(r.text[:150], self._secrets)}")
        return r.json() if r.content else {}

    async def resolve_channel_id(self, client) -> int:
        if not self.broadcaster_id:
            d = (await self._api(client, "GET", "/channels", params={"slug": self.channel})).get("data") or []
            if not d:
                raise AuthError(f"Kick channel '{self.channel}' not found", "Check the Kick channel slug")
            self.broadcaster_id = d[0]["broadcaster_user_id"]
        return self.broadcaster_id

    async def subscribe(self, client=None) -> list[str]:
        """Returns the ids of subscriptions this call created (so a one-shot test can remove exactly those)."""
        if client is None:
            async with self._client() as c:
                return await self.subscribe(c)
        bid = await self.resolve_channel_id(client)
        d = await self._api(client, "POST", "/events/subscriptions", json={
            "broadcaster_user_id": bid, "events": [{"name": "chat.message.sent", "version": 1}], "method": "webhook"})
        created = []
        for s in d.get("data") or []:
            if s.get("error"):
                raise RuntimeError(f"Kick subscription error: {redact(s['error'], self._secrets)}")
            if s.get("subscription_id"):
                created.append(str(s["subscription_id"]))
        return created

    async def unsubscribe(self, client, ids: list[str]) -> None:
        """DELETE only the given subscription ids (never other subscriptions of this app)."""
        if ids:
            await self._api(client, "DELETE", "/events/subscriptions", params=[("id", i) for i in ids])

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.options.get("transport"), timeout=15)

    async def _subscribed(self, client) -> bool:
        d = await self._api(client, "GET", "/events/subscriptions", params={"broadcaster_user_id": await self.resolve_channel_id(client)})
        return any((s.get("event") or s.get("name")) == "chat.message.sent" for s in d.get("data") or [])

    async def run(self) -> None:
        if not self.options.get("public_url"):
            self.health.state, self.health.detail, self.health.action_needed = "unavailable", "no public webhook URL configured", ACT_URL
            return
        async with self._client() as client:
            while True:
                try:
                    if not await self._subscribed(client):
                        await self.subscribe(client)
                    if not self._got_webhook:
                        self.health.state, self.health.action_needed = "connecting", ""
                        how = "app access token, explicit broadcaster id" if getattr(self, "mode", "user") == "app" else "owner user token"
                        if getattr(self, "user_token_note", ""):
                            how += "; owner sign-in needs reconnecting (refresh refused)"
                        self.health.detail = f"subscribed ({how}); waiting for first webhook delivery"
                    wait = float(self.options.get("resubscribe_every", 600))
                except AuthError as e:
                    self.health.state, self.health.detail, self.health.action_needed = "unavailable", str(e), e.action
                    wait = 60
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.health.state = "connecting"
                    self.health.detail = f"retrying: {redact(f'{type(e).__name__}: {e}', self._secrets)}"[:300]
                    self.health.reconnects += 1
                    wait = 15
                await self._sleep(wait)

    # ---------- webhook ----------
    async def _public_key(self, refresh: bool = False) -> bytes:
        if self._pem is None or refresh:
            async with self._client() as c:
                self._pem = (await self._api(c, "GET", "/public-key"))["data"]["public_key"].encode()
                self._key_fetched_at = time.time()
        return self._pem

    async def _verify(self, mid: str, ts: str, sig: str, body: bytes) -> bool:
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError:
            return bool(self.options.get("allow_unverified"))
        try:
            signature = base64.b64decode(sig)
        except Exception:
            return False
        msg = f"{mid}.{ts}.".encode() + body
        # a second try with a re-fetched key handles rotation; never for a pinned key, and at most
        # once per 10 minutes so forged webhooks cannot make us hammer the key endpoint
        can_refresh = not self.options.get("public_key_pem") and time.time() - getattr(self, "_key_fetched_at", 0) > 600
        for refresh in ((False, True) if can_refresh else (False,)):
            try:
                key = serialization.load_pem_public_key(await self._public_key(refresh))
                key.verify(signature, msg, padding.PKCS1v15(), hashes.SHA256())
                return True
            except InvalidSignature:
                continue
            except Exception:
                if self.options.get("public_key_pem"):
                    return False
                continue
        return False

    async def handle_webhook(self, headers: dict, body: bytes) -> int:
        h = {k.lower(): v for k, v in headers.items()}
        mid, ts = h.get("kick-event-message-id", ""), h.get("kick-event-message-timestamp", "")
        if not mid or not await self._verify(mid, ts, h.get("kick-event-signature", ""), body):
            return 401
        if mid in self._seen:
            return 200
        self._seen[mid] = None
        if len(self._seen) > 5000:
            self._seen.popitem(last=False)
        if h.get("kick-event-type") != "chat.message.sent":
            return 200
        try:
            e = json.loads(body)
            slug = (e.get("broadcaster") or {}).get("channel_slug", "")
            if slug.lower() != self.channel.lower() and (e.get("broadcaster") or {}).get("user_id") != self.broadcaster_id:
                return 200                          # another channel sharing this app's webhook
            s = e["sender"]
            now = time.time()
            text = e.get("content", "")
            self.emit(ChatEvent(platform="kick", channel=self.channel, msg_id=e.get("message_id", mid),
                                user_id=str(s.get("user_id", "")), user_name=s.get("username", ""), text=text,
                                received_at=now, sent_at=parse_rfc3339(e.get("created_at")),
                                emotes=tuple(dict.fromkeys(_EMOTE.findall(text)))))
        except (KeyError, ValueError, TypeError):
            return 400
        self._got_webhook = True
        self.health.state, self.health.detail, self.health.action_needed = "healthy", "receiving webhooks", ""
        self.health.last_message_at = now
        return 200
