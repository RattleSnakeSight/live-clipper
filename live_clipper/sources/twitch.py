"""Twitch official live status: Helix `GET /helix/streams` (up to 100 `user_login` per call).

Auth (the user's own Twitch app from dev.twitch.tv/console):
  client_id + client_secret  app access token via client credentials (POST id.twitch.tv/oauth2/token);
  client_id only             device code sign-in (public client, no secret, no redirect URL); the
                             user token also carries `user:read:chat` so chat can use EventSub.
Live status needs no scope.
Rate limit: a per-client token bucket (Ratelimit-* headers, HTTP 429); one call per interval per 100 channels.
"""

from __future__ import annotations

import time

from . import RateLimited, SourceAuthError, batches

HELIX = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
SCOPES = "user:read:chat"
FIX_APP = (
    "Check the Client ID and Client Secret of your Twitch app (dev.twitch.tv/console) in Sources > Twitch"
)
FIX_USER = "Sign in with Twitch again (Sources > Twitch > Sign in)"


async def app_token(http, client_id: str, client_secret: str) -> dict:
    r = await http.post(
        TOKEN_URL,
        data={"client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"},
    )
    if r.status_code in (400, 401, 403):
        raise SourceAuthError(f"Twitch refused the client credentials (HTTP {r.status_code})", FIX_APP)
    r.raise_for_status()
    d = r.json()
    return {
        "app_access_token": d["access_token"],
        "app_expires_at": time.time() + float(d.get("expires_in") or 3600),
    }


async def device_start(http, client_id: str) -> dict:
    """{device_code, user_code, verification_uri, expires_in, interval}."""
    r = await http.post(DEVICE_URL, data={"client_id": client_id, "scopes": SCOPES})
    if r.status_code in (400, 401, 403):
        raise SourceAuthError(
            f"Twitch refused the device sign-in request (HTTP {r.status_code})",
            "Check the Client ID and that the Twitch app's client type is Public",
        )
    r.raise_for_status()
    return r.json()


async def device_poll(http, client_id: str, device_code: str) -> dict | None:
    """None while the user has not approved yet; the token fields once approved; SourceAuthError when
    denied/expired."""
    r = await http.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "scopes": SCOPES,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    if r.status_code == 400 and r.json().get("message") in ("authorization_pending", "slow_down"):
        return None
    if r.status_code >= 400:
        raise SourceAuthError(
            f"Twitch sign-in failed: {r.json().get('message', r.status_code)}",
            "Start the Twitch sign-in again (Sources > Twitch > Sign in)",
        )
    d = r.json()
    sc = d.get("scope") or []
    return {
        "access_token": d["access_token"],
        "refresh_token": d.get("refresh_token", ""),
        "expires_at": str(time.time() + float(d.get("expires_in") or 0)),
        "scope": " ".join(sc) if isinstance(sc, list) else str(sc),
    }


class Client:
    batch = 100

    def __init__(self, save):
        self.save = save  # persists token fields into the vault entry

    async def _bearer(self, http, creds: dict, force: bool = False) -> tuple[str, bool]:
        if creds.get("client_secret"):  # app token preferred: no user session to expire
            if (
                force
                or not creds.get("app_access_token")
                or float(creds.get("app_expires_at") or 0) < time.time() + 60
            ):
                tok = await app_token(http, creds["client_id"], creds["client_secret"])
                self.save(tok)
                creds.update(tok)
            return creds["app_access_token"], True
        return creds["access_token"], False

    async def fetch(self, http, creds: dict, logins: list[str]) -> dict[str, dict]:
        creds = dict(creds)
        out: dict[str, dict] = {}
        for chunk in batches(logins, self.batch):
            params = [("user_login", lg.lstrip("@").lower()) for lg in chunk] + [("first", "100")]
            tok, app = await self._bearer(http, creds)
            r = await http.get(
                HELIX + "/streams",
                params=params,
                headers={"Client-Id": creds["client_id"], "Authorization": f"Bearer {tok}"},
            )
            if r.status_code == 401 and app:  # expired app token: one renewal
                tok, _ = await self._bearer(http, creds, force=True)
                r = await http.get(
                    HELIX + "/streams",
                    params=params,
                    headers={"Client-Id": creds["client_id"], "Authorization": f"Bearer {tok}"},
                )
            if r.status_code == 401:
                raise SourceAuthError(
                    "Twitch rejected the access token (HTTP 401)", FIX_APP if app else FIX_USER
                )
            if r.status_code == 429:
                reset = r.headers.get("ratelimit-reset", "")
                wait = max(1.0, float(reset) - time.time()) if reset.isdigit() else 60.0
                raise RateLimited("Twitch rate limit reached (HTTP 429)", wait)
            r.raise_for_status()
            live = {s["user_login"].lower(): s for s in r.json().get("data", []) if s.get("type") == "live"}
            for lg in chunk:
                s = live.get(lg.lstrip("@").lower())
                out[lg] = (
                    {
                        "state": "live",
                        "title": s.get("title") or "",
                        "category": s.get("game_name") or "",
                        "id": str(s.get("id") or ""),
                        "detail": "",
                    }
                    if s
                    else {"state": "offline", "detail": "not live (Twitch Get Streams)"}
                )
        return out
