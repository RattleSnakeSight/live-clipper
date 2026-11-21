"""Kick official live status: `GET /public/v1/livestreams` (up to 50 `broadcaster_user_id` per call).

The v1 endpoint is the documented one with multi-broadcaster lookup (marked deprecated; the v2 cursor
endpoint's parameters were not verifiable when this was written). Slugs resolve once to broadcaster ids
via `GET /public/v1/channels?slug=`.
Auth (the user's own Kick app, kick.com/settings/developer): client_id + client_secret -> app access token
via client credentials (public data), or a user token from the PKCE sign-in. No rate-limit page is
documented, so polling stays at one batched call per interval.
"""

from __future__ import annotations

import time

from . import RateLimited, SourceAuthError, batches

API = "https://api.kick.com/public/v1"
TOKEN_URL = "https://id.kick.com/oauth/token"
FIX_APP = (
    "Check the Client ID and Client Secret of your Kick app (kick.com/settings/developer) in Sources > Kick"
)
FIX_USER = "Sign in with Kick again (Sources > Kick > Sign in)"


async def app_token(http, client_id: str, client_secret: str) -> dict:
    r = await http.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
    )
    if r.status_code in (400, 401, 403):
        raise SourceAuthError(f"Kick refused the client credentials (HTTP {r.status_code})", FIX_APP)
    r.raise_for_status()
    d = r.json()
    return {
        "app_access_token": d["access_token"],
        "app_expires_at": time.time() + float(d.get("expires_in") or 3600),
    }


class Client:
    batch = 50

    def __init__(self, save):
        self.save = save
        self.ids: dict[str, int] = {}  # slug -> broadcaster_user_id

    async def _bearer(self, http, creds: dict, force: bool = False) -> tuple[str, bool]:
        if creds.get("client_id") and creds.get("client_secret") and not creds.get("access_token"):
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

    async def _get(self, http, creds: dict, path: str, params) -> dict:
        tok, app = await self._bearer(http, creds)
        r = await http.get(API + path, params=params, headers={"Authorization": f"Bearer {tok}"})
        if r.status_code == 401 and app:
            tok, _ = await self._bearer(http, creds, force=True)
            r = await http.get(API + path, params=params, headers={"Authorization": f"Bearer {tok}"})
        if r.status_code in (401, 403):
            raise SourceAuthError(
                f"Kick rejected the access token (HTTP {r.status_code})", FIX_APP if app else FIX_USER
            )
        if r.status_code == 429:
            ra = r.headers.get("retry-after", "")
            raise RateLimited("Kick rate limit reached (HTTP 429)", float(ra) if ra.isdigit() else 60.0)
        r.raise_for_status()
        return r.json()

    async def fetch(self, http, creds: dict, logins: list[str]) -> dict[str, dict]:
        creds = dict(creds)
        out: dict[str, dict] = {}
        for lg in logins:
            slug = lg.lstrip("@").lower()
            if slug not in self.ids:
                d = (await self._get(http, creds, "/channels", {"slug": slug})).get("data") or []
                if not d:
                    out[lg] = {
                        "state": "error",
                        "detail": f"Kick channel '{slug}' not found",
                        "action_needed": "Check the Kick channel URL",
                    }
                    continue
                self.ids[slug] = int(d[0]["broadcaster_user_id"])
        known = [lg for lg in logins if lg not in out]
        for chunk in batches(known, self.batch):
            ids = [self.ids[lg.lstrip("@").lower()] for lg in chunk]
            params = [("broadcaster_user_id", str(i)) for i in ids] + [("limit", "100")]
            data = (await self._get(http, creds, "/livestreams", params)).get("data") or []
            live = {int(s.get("broadcaster_user_id") or 0): s for s in data}
            for lg, bid in zip(chunk, ids, strict=True):
                s = live.get(bid)
                out[lg] = (
                    {
                        "state": "live",
                        "title": s.get("stream_title") or "",
                        "category": (s.get("category") or {}).get("name") or "",
                        "id": str(s.get("started_at") or ""),
                        "detail": "",
                    }
                    if s
                    else {"state": "offline", "detail": "not live (Kick livestreams)"}
                )
        return out
