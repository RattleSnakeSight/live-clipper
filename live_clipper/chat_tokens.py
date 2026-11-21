"""Read-only chat token renewal (owner sign-in tokens only).

Tokens saved by a source sign-in (live_clipper.sources: Twitch device flow as a public client, Kick and YouTube PKCE
user OAuth) carry a rotating `refresh_token` and `expires_at`. Chat adapters ask this object for current credentials:
  * before expiry (within `skew` seconds) the token is renewed at (re)connect / resubscribe time;
  * after a provider 401 the adapter asks again with force=True and the token it was using.

Rules:
  * Manual/legacy credentials (no refresh_token) are returned untouched and never renewed.
  * One asyncio.Lock per platform and a re-read of the encrypted store inside it: when several channels hit expiry
    or a 401 together, exactly one refresh request is made and the others get the rotated token. A forced refresh
    whose stale token has already been replaced returns the replacement without a request (refresh tokens are
    single-use, so a second parallel rotation would otherwise burn the new one and look like a revocation).
  * Rotated tokens are written back only through SecretStore (Fernet-encrypted at rest), and only if the stored
    refresh token is still the one used (an owner re-sign-in in the meantime wins).
  * A refused refresh (HTTP 400/401: revoked, expired or invalid grant) raises TokenRevoked with a clear action and is
    remembered for that refresh token, so retries do not hammer the provider until the owner signs in again.
    Network errors and 5xx are transient: a still-valid token keeps being used; an expired one raises RuntimeError
    and the adapter retries with its normal backoff.
Endpoints (documented OAuth refresh grants): Twitch POST https://id.twitch.tv/oauth2/token (grant_type=refresh_token,
client_id; client_secret only when one is stored, public clients send none); Kick POST https://id.kick.com/oauth/token
(grant_type=refresh_token, client_id, client_secret); Google POST https://oauth2.googleapis.com/token (same grant). Tests inject an httpx.MockTransport; no request is made unless a
stored refresh token needs it.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable

import httpx

from .chat._util import AuthError

ENDPOINTS = {"twitch": "https://id.twitch.tv/oauth2/token", "kick": "https://id.kick.com/oauth/token",
             "youtube": "https://oauth2.googleapis.com/token"}
ACT_REVOKED = {"twitch": "Reconnect Twitch (Sources > Twitch > Sign in): the saved sign-in was revoked or expired",
               "kick": "Reconnect Kick (Sources > Kick > Sign in): the saved sign-in was revoked or expired",
               "youtube": "Reconnect YouTube (Sources > YouTube > Sign in): the saved sign-in was revoked or expired"}


class TokenRevoked(AuthError):
    """The provider refused the refresh token; only a new owner sign-in fixes it."""


def _fp(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()[:16]


class TokenRefresher:
    def __init__(self, secrets, transport=None, skew: float = 300.0, clock: Callable[[], float] = time.time,
                 endpoints: dict | None = None):
        self.secrets, self.transport, self.skew, self.clock = secrets, transport, skew, clock
        self.endpoints = {**ENDPOINTS, **(endpoints or {})}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._revoked: dict[str, tuple] = {}          # platform -> (fingerprint of refused refresh token, message)
        self.refreshes: dict[str, int] = {}           # successful rotations (status/tests)
        self.last_error: dict[str, str] = {}

    def _lock(self, platform: str) -> asyncio.Lock:
        loop = asyncio.get_running_loop()             # one lock per platform per event loop (the server has one)
        key = (platform, id(loop))
        if key not in self._locks:
            self._locks = {k: v for k, v in self._locks.items() if k[1] == id(loop)}
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _creds(self, platform: str) -> dict:
        return dict(self.secrets.chat_creds(platform) or {}) if self.secrets else {}

    def renewable(self, platform: str) -> bool:
        return platform in self.endpoints and bool(self._creds(platform).get("refresh_token"))

    async def get(self, platform: str, force: bool = False, stale_token: str | None = None) -> dict:
        """Current credentials for `platform`, renewed if they expire within `skew` seconds or if force=True."""
        async with self._lock(platform):
            creds = self._creds(platform)
            rt = creds.get("refresh_token")
            if not rt or platform not in self.endpoints:
                return creds                                       # manual/legacy credentials: never touched
            if force and stale_token is not None and creds.get("access_token") != stale_token:
                return creds                                       # another channel already rotated it
            try:
                exp = float(creds.get("expires_at") or 0)
            except ValueError:
                exp = 0.0
            now = self.clock()
            if not force and (exp <= 0 or exp - now > self.skew):
                return creds                                       # still fresh (or unknown expiry: renew on 401)
            memo = self._revoked.get(platform)
            if memo and memo[0] == _fp(rt):               # same refused token: no new request until a re-sign-in
                raise TokenRevoked(memo[1] + " (not retried until the owner signs in again)", ACT_REVOKED[platform])
            try:
                fresh = await self._refresh(platform, creds)
            except TokenRevoked as e:
                if self._creds(platform).get("refresh_token") != rt:   # owner signed in again meanwhile
                    return self._creds(platform)
                self._revoked[platform] = (_fp(rt), str(e))
                raise
            except Exception as e:
                self.last_error[platform] = f"refresh failed: {type(e).__name__}"
                if not force and exp > now:
                    return creds                                   # transient; the current token is still valid
                raise RuntimeError(f"{platform} token refresh failed ({type(e).__name__}); will retry") from None
            # write back only if nobody replaced the refresh token while the request was in flight
            if self._creds(platform).get("refresh_token") != rt:
                return self._creds(platform)
            stored = self.secrets.update("chat", platform, fresh)
            self.refreshes[platform] = self.refreshes.get(platform, 0) + 1
            self.last_error.pop(platform, None)
            return dict(stored)

    async def _refresh(self, platform: str, creds: dict) -> dict:
        data = {"grant_type": "refresh_token", "refresh_token": creds["refresh_token"],
                "client_id": creds.get("client_id", "")}
        if creds.get("client_secret"):                 # Kick needs it; Twitch public (device) clients have none
            data["client_secret"] = creds["client_secret"]
        if not data["client_id"]:
            raise TokenRevoked(f"{platform} client_id is not set, cannot renew the sign-in", ACT_REVOKED[platform])
        async with httpx.AsyncClient(transport=self.transport, timeout=15) as c:
            r = await c.post(self.endpoints[platform], data=data)
        if r.status_code in (400, 401, 403):
            try:
                why = str((r.json() or {}).get("message") or (r.json() or {}).get("error") or "")[:80]
            except ValueError:
                why = ""
            self.last_error[platform] = f"refresh refused (HTTP {r.status_code})"
            raise TokenRevoked(f"{platform.capitalize()} refused the token refresh (HTTP {r.status_code}"
                               f"{': ' + why if why else ''}); the sign-in was revoked or expired",
                               ACT_REVOKED[platform])
        if r.status_code != 200:
            raise ConnectionError(f"HTTP {r.status_code}")
        t = r.json()
        if not t.get("access_token"):
            raise ConnectionError("no access_token in refresh response")
        out = {"access_token": t["access_token"],
               "expires_at": str(self.clock() + float(t.get("expires_in") or 0)) if t.get("expires_in") else None}
        if t.get("refresh_token"):
            out["refresh_token"] = t["refresh_token"]   # rotating refresh tokens: keep the newest only
        if t.get("scope"):
            sc = t["scope"]
            out["scope"] = " ".join(sc) if isinstance(sc, list) else str(sc)
        return out
