"""Notifications: keep a recent list for the browser (GET /api/notifications) and optionally post to a webhook.

The webhook address lives in the vault (`notifications`), never in settings or logs.
"""

import logging
import time
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter

from .db import register_migrations
from .logging_setup import redact
from .services import AppContext

log = logging.getLogger(__name__)
KEEP = 200
TIMEOUT_S = 10
WEBHOOK_SECRET = "notifications"

register_migrations("notifications", ["""
CREATE TABLE notifications_item(
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL);
"""])


def detect_format(url: str, configured: str = "auto") -> str:
    if configured != "auto":
        return configured
    host = (urlsplit(url).hostname or "").lower()
    if host in ("discord.com", "discordapp.com") or host.endswith((".discord.com", ".discordapp.com")):
        return "discord"
    if host == "ntfy.sh" or host.startswith("ntfy."):
        return "ntfy"
    return "generic"


def build_request(fmt: str, title: str, body: str, ts: float) -> dict:
    """httpx keyword arguments for the POST."""
    if fmt == "discord":
        return {"json": {"content": f"**{title}**\n{body}"[:2000]}}
    if fmt == "ntfy":  # ntfy takes the message as the body and the title as a header
        return {"content": body.encode(), "headers": {"Title": title.encode("ascii", "replace").decode()}}
    return {"json": {"title": title, "body": body, "ts": ts, "source": "live-clipper"}}


class NotificationService:
    name = "notifications"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport
        self.ctx: AppContext | None = None
        self._unsub: list = []
        self.last_webhook: dict = {}
        self.router = APIRouter(prefix="/api/notifications")
        self.router.get("")(self.recent)

    async def start(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._unsub = [ctx.bus.subscribe("app.notify", self.handle),
                       ctx.bus.subscribe("publish.delivery", self.handle)]

    async def stop(self) -> None:
        for u in self._unsub:
            u()

    def status(self) -> dict:
        return {"webhook": self.last_webhook or None}

    async def handle(self, topic: str, payload: dict) -> None:
        if topic == "publish.delivery":
            if str(payload.get("state", "")).lower() != "published":
                return
            title = "Clip posted"
            body = f"Clip {payload.get('clip_id')} is on {payload.get('destination')}"
            if payload.get("url"):
                body += f": {payload['url']}"
            kind = "published"
        else:
            title, body, kind = str(payload.get("title", "")), str(payload.get("body", "")), "notify"
        title, body = redact(title), redact(body)
        now = time.time()
        self.ctx.db.execute("INSERT INTO notifications_item(ts, kind, title, body) VALUES(?,?,?,?)",
                            (now, kind, title, body))
        self.ctx.db.execute("DELETE FROM notifications_item WHERE id <= "
                            "(SELECT MAX(id) FROM notifications_item) - ?", (KEEP,))
        await self._webhook(title, body, now)

    async def _webhook(self, title: str, body: str, ts: float) -> None:
        s = self.ctx.settings
        if s.get("notify.method", "browser") not in ("webhook", "both"):
            return
        secret = self.ctx.secrets.get(WEBHOOK_SECRET)
        if not secret or not secret.get("url"):
            return
        url = secret["url"]
        fmt = detect_format(url, s.get("notify.webhook_format", "auto"))
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=TIMEOUT_S) as client:
                r = await client.post(url, **build_request(fmt, title, body, ts))
            ok = r.status_code < 300
            err = None if ok else f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            ok, err = False, type(e).__name__  # exception text can contain the URL
        self.last_webhook = {"ok": ok, "at": ts, "error": err}
        if not ok:
            log.warning("webhook notification failed: %s", err)
            self.ctx.db.event("warning", "notifications", f"webhook failed: {err}")

    def recent(self, since: int = 0, limit: int = 50) -> dict:
        rows = self.ctx.db.execute(
            "SELECT id, ts, kind, title, body FROM notifications_item WHERE id > ? ORDER BY id LIMIT ?",
            (since, max(1, min(limit, KEEP)))).fetchall()
        items = [{"id": r[0], "ts": r[1], "kind": r[2], "title": r[3], "body": r[4]} for r in rows]
        return {"items": items, "latest": items[-1]["id"] if items else since}
