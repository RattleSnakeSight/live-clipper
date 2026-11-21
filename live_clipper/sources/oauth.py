"""OAuth 2 authorization code + PKCE (S256) with a loopback redirect to this app (Kick, Google)."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

from . import SourceAuthError

PROVIDERS = {
    "kick": {
        "authorize": "https://id.kick.com/oauth/authorize",
        "token": "https://id.kick.com/oauth/token",
        "scope": "user:read events:subscribe",
        "host": "localhost",
    },  # Kick recommends localhost
    "youtube": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/youtube.readonly",
        "host": "127.0.0.1",  # loopback IP
        "extra": {"access_type": "offline", "prompt": "consent"},
    },
}
PENDING_TTL = 600.0


def redirect_uri(platform: str, port: int) -> str:
    return f"http://{PROVIDERS[platform]['host']}:{port}/api/oauth/source/{platform}/callback"


class PendingAuth:
    """In-memory, single-use `state` values (10 minutes)."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def start(self, platform: str, port: int, client_id: str, client_secret: str = "") -> str:
        p = PROVIDERS[platform]
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        uri = redirect_uri(platform, port)
        self.items = {k: v for k, v in self.items.items() if v["at"] > time.time() - PENDING_TTL}
        self.items[state] = {
            "platform": platform,
            "verifier": verifier,
            "redirect_uri": uri,
            "client_id": client_id,
            "client_secret": client_secret,
            "at": time.time(),
        }
        q = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": uri,
            "scope": p["scope"],
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            **p.get("extra", {}),
        }
        return p["authorize"] + "?" + urlencode(q)

    def take(self, platform: str, state: str) -> dict | None:
        item = self.items.pop(state, None)
        if not item or item["platform"] != platform or item["at"] < time.time() - PENDING_TTL:
            return None
        return item

    def pending(self, platform: str) -> bool:
        return any(
            v["platform"] == platform and v["at"] > time.time() - PENDING_TTL for v in self.items.values()
        )


async def exchange(http, item: dict, code: str) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": item["client_id"],
        "redirect_uri": item["redirect_uri"],
        "code_verifier": item["verifier"],
    }
    if item["client_secret"]:
        data["client_secret"] = item["client_secret"]
    r = await http.post(PROVIDERS[item["platform"]]["token"], data=data)
    if r.status_code >= 400:
        raise SourceAuthError(
            f"sign-in failed at the token exchange (HTTP {r.status_code})",
            "Check the client ID/secret and that the redirect URL shown in Sources is registered "
            "exactly in your app, then sign in again",
        )
    d = r.json()
    out = {
        "client_id": item["client_id"],
        "client_secret": item["client_secret"],
        "access_token": d["access_token"],
        "refresh_token": d.get("refresh_token", ""),
        "expires_at": str(time.time() + float(d["expires_in"])) if d.get("expires_in") else "",
        "scope": " ".join(d["scope"]) if isinstance(d.get("scope"), list) else str(d.get("scope") or ""),
    }
    return out
