"""Official live-status sources (Twitch Helix, Kick public API, YouTube Data API) and their credentials.

Three separate permissions, never mixed up in data or API:
  live status  official API with the user's own app credentials (this package); without them the
               reference stream probe is used and labelled STREAM_PROBE.
  chat         live_clipper.chat adapters (Twitch anonymous IRC or EventSub with user:read:chat;
               Kick webhooks need a public HTTPS URL; YouTube liveChatMessages costs quota).
  capture      Streamlink + FFmpeg (third-party tools). Live-status access does not grant recording or
               redistribution rights.

Credentials live in the vault entry `source.<platform>` (the chat adapters read the same entry).
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import guide

log = logging.getLogger(__name__)
PLATFORMS = ("twitch", "kick", "youtube")
STREAM_PROBE = "stream probe (no official API connected)"
OFFICIAL = {
    "twitch": "Twitch Helix Get Streams",
    "kick": "Kick public API livestreams",
    "youtube": "YouTube Data API (search.list / videos.list)",
}
RIGHTS_NOTE = (
    "Monitoring permission (live status or chat access) does not grant the right to record or redistribute "
    "a stream. Recording uses Streamlink and FFmpeg, third-party tools; check the platform's terms and the "
    "streamer's rights before clipping."
)


class SourceAuthError(Exception):
    """The platform rejected the user's credentials; `fix` is the exact user action."""

    def __init__(self, detail: str, fix: str):
        super().__init__(detail)
        self.fix = fix


class RateLimited(Exception):
    def __init__(self, detail: str, retry_after: float):
        super().__init__(detail)
        self.retry_after = retry_after


def batches(items: list, n: int) -> list[list]:
    return [items[i : i + n] for i in range(0, len(items), n)]


class SourceSecrets:
    """Vault adapter with the reference SecretStore surface used by chat adapters and TokenRefresher."""

    def __init__(self, vault):
        self.vault = vault

    def chat_creds(self, platform: str) -> dict:
        return self.vault.get(f"source.{platform}") or {}

    creds = chat_creds

    def update(self, ns: str, platform: str, patch: dict) -> dict:
        """Merge; a None or empty value removes that field (reference semantics)."""
        cur = self.chat_creds(platform)
        for k, v in patch.items():
            if v is None or v == "":
                cur.pop(k, None)
            else:
                cur[k] = v
        self.vault.set(f"source.{platform}", cur)
        return cur

    def delete(self, platform: str) -> None:
        self.vault.delete(f"source.{platform}")


def has_live_status_creds(platform: str, c: dict) -> bool:
    if platform == "twitch":
        return bool(c.get("client_id") and (c.get("client_secret") or c.get("access_token")))
    if platform == "kick":
        return bool((c.get("client_id") and c.get("client_secret")) or c.get("access_token"))
    if platform == "youtube":
        return bool(c.get("api_key") or c.get("access_token"))
    return False


def _clients(save) -> dict:
    from . import kick, twitch, youtube

    return {
        "twitch": twitch.Client(save("twitch")),
        "kick": kick.Client(save("kick")),
        "youtube": youtube.Client(save("youtube")),
    }


class StatusHub:
    """One poll loop per platform: every watched login is checked in batches (Twitch 100, Kick 50 per
    call) at the
    platform's interval, results are cached and handed to every channel waiting for a newer answer."""

    INTERVALS = {"twitch": 60.0, "kick": 60.0, "youtube": 60.0}

    def __init__(
        self,
        secrets: SourceSecrets,
        transport: httpx.AsyncBaseTransport | None = None,
        tokens=None,
        intervals: dict | None = None,
    ):
        self.secrets, self.transport, self.tokens = secrets, transport, tokens
        self.intervals = {**self.INTERVALS, **(intervals or {})}
        self.clients = _clients(lambda p: lambda patch: self.secrets.update("source", p, patch))
        self.watched: dict[str, set[str]] = {p: set() for p in PLATFORMS}
        self.cache: dict[tuple[str, str], dict] = {}
        self.problem: dict[str, dict] = {}  # platform -> {"detail", "fix"} while credentials are rejected
        self.due: dict[str, float] = {}
        self.calls: dict[str, int] = {p: 0 for p in PLATFORMS}
        self._cond: asyncio.Condition | None = None
        self._wake: asyncio.Event | None = None
        self._loop = None
        self._task: asyncio.Task | None = None

    # ---- public ----------------------------------------------------------
    def connected(self, platform: str) -> bool:
        return platform in PLATFORMS and has_live_status_creds(platform, self.secrets.chat_creds(platform))

    def interval(self, platform: str) -> float:
        return self.intervals.get(platform, 60.0)

    def watch(self, platform: str, login: str) -> None:
        self._bind()
        if login not in self.watched[platform]:
            self.watched[platform].add(login)
            self.due[platform] = 0.0
            self._wake.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="monitor-status-hub")

    def unwatch(self, platform: str, login: str) -> None:
        self.watched.get(platform, set()).discard(login)

    async def next(self, platform: str, login: str, after: float = 0.0) -> dict:
        """The first answer for `login` checked after `after`."""
        self.watch(platform, login)
        async with self._cond:
            await self._cond.wait_for(
                lambda: self.cache.get((platform, login), {}).get("checked_at", 0) > after
            )
            return dict(self.cache[(platform, login)])

    async def current(self, platform: str, login: str) -> dict:
        """Latest answer if younger than one interval, else the next one."""
        st = self.cache.get((platform, login))
        if st and time.time() - st["checked_at"] < self.interval(platform):
            self.watch(platform, login)
            return dict(st)
        return await self.next(platform, login, st["checked_at"] if st else 0.0)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.wait([self._task], timeout=10)
            self._task = None

    def status(self) -> dict:
        return {
            p: {
                "connected": self.connected(p),
                "watched": len(self.watched[p]),
                "calls": self.calls[p],
                "problem": self.problem.get(p),
            }
            for p in PLATFORMS
        }

    # ---- loop ------------------------------------------------------------
    def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop, self._cond, self._wake, self._task = loop, asyncio.Condition(), asyncio.Event(), None

    async def _run(self) -> None:
        while True:
            now = time.time()
            for p in PLATFORMS:
                if self.watched[p] and now >= self.due.get(p, 0.0):
                    await self.poll(p)
            pending = [self.due[p] for p in PLATFORMS if self.watched[p]]
            self._wake.clear()
            wait = max(0.05, min(pending) - time.time()) if pending else None
            try:
                await asyncio.wait_for(self._wake.wait(), wait)
            except TimeoutError:
                pass

    async def poll(self, platform: str) -> None:
        logins = sorted(self.watched[platform])
        self.due[platform] = time.time() + self.interval(platform)
        creds = self.secrets.chat_creds(platform)
        results: dict[str, dict] = {}
        if not has_live_status_creds(platform, creds):
            results = {
                lg: {"state": "error", "detail": f"no official {platform} API connected"} for lg in logins
            }
        else:
            try:
                if self.tokens is not None and creds.get("refresh_token"):
                    creds = {**creds, **await self.tokens.get(platform)}
                async with httpx.AsyncClient(transport=self.transport, timeout=20) as http:
                    results = await self.clients[platform].fetch(http, creds, logins)
                self.calls[platform] += 1
                self.problem.pop(platform, None)
            except SourceAuthError as e:
                self.problem[platform] = {"detail": str(e), "fix": e.fix}
                results = {lg: {"state": "error", "detail": str(e), "action_needed": e.fix} for lg in logins}
            except RateLimited as e:
                self.due[platform] = time.time() + max(self.interval(platform), e.retry_after)
                results = {
                    lg: {"state": "unavailable", "detail": str(e), "retry_after": e.retry_after}
                    for lg in logins
                }
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network / 5xx: unknown, retried next interval
                results = {
                    lg: {"state": "error", "detail": f"{OFFICIAL[platform]} failed: {type(e).__name__}"}
                    for lg in logins
                }
        now = time.time()
        async with self._cond:
            for lg, st in results.items():
                base = {
                    "state": "error",
                    "title": "",
                    "category": "",
                    "id": "",
                    "detail": "",
                    "action_needed": "",
                }
                self.cache[(platform, lg)] = {**base, **st, "source": "official", "checked_at": now}
            self._cond.notify_all()


def describe(
    platform: str, creds: dict, settings, hub: StatusHub | None = None, pending: dict | None = None
) -> dict:
    """GET /api/sources entry: what is connected and what each permission enables."""
    connected = has_live_status_creds(platform, creds)
    g = guide.for_platform(platform)
    route, how = guide.how_connected(platform, creds) if connected else ("", "")
    problem = (hub.problem.get(platform) if hub else None) or None
    live = {
        "available": connected,
        "how": OFFICIAL[platform] if connected else STREAM_PROBE,
        "detail": problem["detail"] if problem else "",
        "fix": problem["fix"] if problem else "",
    }
    if platform == "twitch":
        user = bool(creds.get("access_token"))
        chat_scope = "user:read:chat" in str(creds.get("scope") or "")
        chat = {
            "available": True,
            "how": "EventSub channel.chat.message (your Twitch sign-in)"
            if user and chat_scope
            else "anonymous read-only IRC (no account needed)",
            "detail": ""
            if user and chat_scope
            else "Sign in with Twitch (device code, scope user:read:chat) to read chat through EventSub.",
        }
        live["auth"] = (
            "app access token (client credentials)"
            if creds.get("client_secret")
            else ("user access token (device code sign-in)" if user else "")
        )
    elif platform == "kick":
        url = getattr(settings, "kick_webhook_url", "")
        chat = {
            "available": bool(url and connected),
            "how": "chat.message.sent webhooks",
            "detail": f"webhooks delivered to {url}"
            if url and connected
            else "Chat unavailable: Kick delivers chat only by webhook to a public HTTPS URL (and needs Kick "
            "credentials). Set a public URL that forwards to the local webhook listener, or leave chat off.",
        }
    else:
        chat = {
            "available": connected,
            "how": "liveChatMessages.list polling",
            "detail": "Each chat poll costs 5 quota units of the default 10,000 units a day, so long streams "
            "can exhaust the quota."
            if connected
            else "Needs a YouTube API key or Google sign-in.",
        }
    out = {
        "platform": platform,
        "connected": connected,
        "live_status": live,
        "chat": chat,
        "capture": {"tool": "Streamlink + FFmpeg (third-party)", "note": RIGHTS_NOTE},
        "fields": sorted(k for k, v in creds.items() if v not in (None, "")),
        # what the Sources page tells the customer: what this is for and each way to connect (guide.py)
        "about": g["about"],
        "routes": g["routes"],
        "route": route,
        "how": how,
    }
    if "chat_setup" in g:  # the local address a tunnel forwards Kick's chat webhooks to
        port = getattr(settings, "kick_webhook_port", 8788)
        out["chat_setup"] = {
            **g["chat_setup"],
            "listener": f"http://127.0.0.1:{port}/webhooks/kick" if port else "",
        }
    if pending:
        out["pending"] = pending
    return out
