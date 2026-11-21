"""Monitor service: channels, arming, official live-status sources, capture + chat (routes under /api)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .config import Settings
from .engine import Engine, LiveRefused, public_state
from .sources import (
    OFFICIAL,
    PLATFORMS,
    STREAM_PROBE,
    SourceAuthError,
    SourceSecrets,
    StatusHub,
    describe,
    has_live_status_creds,
    oauth,
)
from .sources import kick as kick_src
from .sources import twitch as twitch_src
from .store import Store

log = logging.getLogger(__name__)
WATCH_MODES = ("ungated", "topic_gated")
_LOGIN = {
    "twitch": re.compile(r"^[A-Za-z0-9_]{1,25}$"),
    "kick": re.compile(r"^[A-Za-z0-9_-]{1,40}$"),
    "youtube": re.compile(r"^(@[A-Za-z0-9._-]{1,100}|channel/UC[\w-]{22})$"),
}
FEED_SCHEMES = ("rtmp", "rtmps", "srt", "http", "https", "rtsp")


def parse_channel_url(url: str) -> tuple[str, str, str]:
    """(platform, login, source_url). Twitch/Kick/YouTube channel pages, or a creator-provided feed URL."""
    url = url.strip()
    if len(url) > 500:
        raise ValueError("URL is too long")
    if url.startswith("streamlink:"):
        return "custom", url, url
    u = urlparse(url if "://" in url else "https://" + url)
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    parts = [p for p in u.path.split("/") if p]
    plat = {"twitch.tv": "twitch", "kick.com": "kick", "youtube.com": "youtube"}.get(host)
    if plat:
        login = ""
        if plat == "youtube":
            if parts and parts[0].startswith("@"):
                login = parts[0]
            elif len(parts) >= 2 and parts[0] == "channel":
                login = f"channel/{parts[1]}"
        elif parts:
            login = parts[0]
        if not _LOGIN[plat].match(login):
            hint = (
                "youtube.com/@handle or youtube.com/channel/UC..." if plat == "youtube" else f"{host}/<name>"
            )
            raise ValueError(f"not a {plat} channel URL (expected {hint})")
        return plat, login, ""
    if u.scheme in FEED_SCHEMES and u.hostname:
        return "custom", url, url
    raise ValueError(
        "unsupported URL: use a Twitch, Kick or YouTube channel URL, or a feed URL "
        "(rtmp://, srt://, rtsp://, http(s)://...m3u8)"
    )


class ChannelIn(BaseModel):
    url: str
    watch_mode: str = "ungated"
    poll_seconds: int = 120
    format_id: str | None = None
    publish_platforms: list[str] = Field(default_factory=list)
    enabled: bool = True
    display_name: str = ""


class ChannelPatch(BaseModel):
    watch_mode: str | None = None
    poll_seconds: int | None = None
    format_id: str | None = None
    publish_platforms: list[str] | None = None
    enabled: bool | None = None
    display_name: str | None = None


class ConnectIn(BaseModel):
    method: str = "credentials"  # credentials | device (twitch) | oauth (kick, youtube)
    client_id: str = ""
    client_secret: str = ""
    api_key: str = ""
    webhook_public_url: str | None = None  # kick chat (optional)


def _gate(watch_mode: str, poll: int) -> dict:
    if watch_mode not in WATCH_MODES:
        raise HTTPException(422, f"watch_mode must be one of {', '.join(WATCH_MODES)}")
    return {"enabled": watch_mode == "topic_gated", "poll_seconds": max(60, min(900, int(poll or 120)))}


class MonitorService:
    name = "monitor"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport
        self.ctx = None
        self.engine: Engine | None = None
        self.store: Store | None = None
        self.secrets: SourceSecrets | None = None
        self.hub: StatusHub | None = None
        self.error = ""
        self.pending = oauth.PendingAuth()
        self.devices: dict[str, dict] = {}
        self.last_state: dict[int, str] = {}
        self._tasks: list[asyncio.Task] = []
        self.router = self._routes()

    # ---- lifecycle -------------------------------------------------------
    async def start(self, ctx) -> None:
        self.ctx = ctx
        self.store = Store(ctx.db)
        self.secrets = SourceSecrets(ctx.secrets)
        self.settings = Settings.from_kv(ctx.settings, ctx.paths.media)
        try:
            self.hub = StatusHub(self.secrets, transport=self.transport)
            self.engine = Engine(self.settings, self.store, self.secrets, hub=self.hub)
            self.hub.tokens = self.engine.tokens
            await self.engine.start()
        except Exception as e:  # e.g. the edit subsystem's buffer is not installed
            self.error = f"{type(e).__name__}: {e}"
            log.exception("monitor engine failed to start")
            return
        self._tasks.append(asyncio.create_task(self._publish_states(), name="monitor-states"))
        if self.settings.kick_webhook_url and self.settings.kick_webhook_port:
            self._tasks.append(asyncio.create_task(self._kick_listener(), name="lc-kick-webhook"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.engine:
            await self.engine.stop()

    def status(self) -> dict:
        if not self.engine:
            return {"state": "error", "detail": self.error or "not started"}
        states = [self.last_state.get(cid) for cid in self.engine.monitors]
        return {
            "state": "running",
            "channels": len(self.store.channels()),
            "armed": len(self.engine.monitors),
            "recording": states.count("recording"),
            "sources": self.hub.status() if self.hub else {},
        }

    async def _kick_listener(self) -> None:
        import uvicorn

        from .kick_webhook import create_webhook_app

        cfg = uvicorn.Config(
            create_webhook_app(self.engine),
            host="127.0.0.1",
            port=self.settings.kick_webhook_port,
            log_config=None,
            access_log=False,
        )
        await uvicorn.Server(cfg).serve()

    async def _publish_states(self) -> None:
        while True:
            try:
                self.publish_states()
            except Exception:
                log.exception("state publish failed")
            await asyncio.sleep(1.0)

    def publish_states(self) -> None:
        for ch in self.store.channels():
            v = self.channel_view(ch)
            if self.last_state.get(ch["id"]) != v["state"]:
                old = self.last_state.get(ch["id"])
                self.last_state[ch["id"]] = v["state"]
                self.store.log(
                    f"state {old or 'new'} -> {v['state']}" + (f": {v['detail']}" if v["detail"] else ""),
                    channel_id=ch["id"],
                )
                self.ctx.bus.publish(
                    "monitor.channel_state",
                    {
                        "channel_id": ch["id"],
                        "platform": ch["platform"],
                        "login": ch["name"],
                        "state": v["state"],
                        "detail": v["detail"],
                    },
                )

    # ---- views -----------------------------------------------------------
    def _engine(self) -> Engine:
        if not self.engine:
            raise HTTPException(503, f"monitor engine is not running: {self.error or 'not started'}")
        return self.engine

    def channel_view(self, ch: dict) -> dict:
        m = self.engine.monitors.get(ch["id"]) if self.engine else None
        st = m.status() if m else {}
        pub = public_state(st, bool(ch["armed"]) and m is not None)
        gate = (ch["settings"] or {}).get("topic_gate") or {}
        if st.get("status_source"):
            source = st["status_source"]
        elif ch["source_url"]:
            source = "creator-provided feed"
        else:
            source = (
                OFFICIAL[ch["platform"]] if self.hub and self.hub.connected(ch["platform"]) else STREAM_PROBE
            )
        fix_link = ""
        if pub["state"] == "needs_action":
            official_problem = (st.get("official") or {}).get("action_needed")
            fix_link = f"#/sources/{ch['platform']}" if official_problem else f"#/channels/{ch['id']}"
        out = {
            "id": ch["id"],
            "url": ch["url"],
            "platform": ch["platform"],
            "login": ch["name"],
            "display_name": ch["display_name"] or ch["name"],
            "watch_mode": "topic_gated" if gate.get("enabled") else "ungated",
            "poll_seconds": int(gate.get("poll_seconds") or 120),
            "format_id": ch["format_id"],
            "publish_platforms": ch["publish_platforms"],
            "enabled": bool(ch["armed"]),
            **pub,
            "fix_link": fix_link,
            "status_source": source,
            "gate_change_pending": bool(m) and m.gated != bool(gate.get("enabled")),
            **{
                k: st[k]
                for k in (
                    "capture",
                    "chat",
                    "watch",
                    "official",
                    "buffer_seconds",
                    "buffer_target",
                    "epochs",
                    "recent_chat",
                )
                if k in st
            },
        }
        return out

    def _channel(self, cid: int) -> dict:
        ch = self.store.channel(cid) if self.store else None
        if not ch:
            raise HTTPException(404, "channel not found")
        return ch

    async def _rearm(self, platform: str) -> None:
        """Official source connected/removed: armed channels of that platform pick the new status path."""
        e = self.engine
        if not e:
            return
        for cid, m in list(e.monitors.items()):
            if m.ch["platform"] == platform and not m.ch["source_url"]:
                await e.disarm(cid)
                e.arm(cid)

    # ---- routes ----------------------------------------------------------
    def _routes(self) -> APIRouter:
        r = APIRouter(prefix="/api", tags=["monitor"])

        @r.get("/channels")
        async def list_channels():
            self._engine()
            return [self.channel_view(c) for c in self.store.channels()]

        @r.post("/channels", status_code=201)
        async def create_channel(body: ChannelIn):
            e = self._engine()
            try:
                platform, login, source_url = parse_channel_url(body.url)
            except ValueError as ex:
                raise HTTPException(422, str(ex)) from None
            if self.store.q1("SELECT 1 FROM channels WHERE platform=? AND name=?", platform, login):
                raise HTTPException(409, f"{platform}/{login} is already added")
            cid = self.store.add_channel(
                platform,
                login,
                source_url,
                {"topic_gate": _gate(body.watch_mode, body.poll_seconds)},
                url=body.url.strip(),
                display_name=body.display_name.strip()[:100],
                format_id=body.format_id,
                publish_platforms=body.publish_platforms,
            )
            if body.enabled:
                e.arm(cid)
            return self.channel_view(self.store.channel(cid))

        @r.get("/channels/{cid}")
        async def get_channel(cid: int):
            self._engine()
            return self.channel_view(self._channel(cid))

        @r.put("/channels/{cid}")
        async def put_channel(cid: int, body: ChannelPatch):
            e = self._engine()
            ch = self._channel(cid)
            fields = {}
            if body.watch_mode is not None or body.poll_seconds is not None:
                cur = (ch["settings"] or {}).get("topic_gate") or {}
                mode = body.watch_mode or ("topic_gated" if cur.get("enabled") else "ungated")
                gate = _gate(mode, body.poll_seconds or cur.get("poll_seconds") or 120)
                self.store.merge_json("channels", cid, "settings", {"topic_gate": gate})  # applies on re-arm
            for k in ("format_id", "publish_platforms", "display_name"):
                if getattr(body, k) is not None:
                    fields[k] = getattr(body, k)
            self.store.update("channels", cid, **fields)
            if body.enabled is True and not ch["armed"]:
                try:
                    e.arm(cid)
                except LiveRefused as ex:
                    raise HTTPException(409, str(ex)) from None
            elif body.enabled is False and ch["armed"]:
                await e.disarm(cid)
            return self.channel_view(self.store.channel(cid))

        @r.delete("/channels/{cid}", status_code=204)
        async def delete_channel(cid: int):
            self._channel(cid)
            await self._engine().disarm(cid)
            self.store.x("DELETE FROM channels WHERE id=?", cid)
            self.last_state.pop(cid, None)

        @r.post("/channels/{cid}/arm")
        async def arm(cid: int):
            self._channel(cid)
            try:
                self._engine().arm(cid)
            except LiveRefused as ex:
                raise HTTPException(409, str(ex)) from None
            return self.channel_view(self.store.channel(cid))

        @r.post("/channels/{cid}/disarm")
        async def disarm(cid: int):
            self._channel(cid)
            await self._engine().disarm(cid)
            return self.channel_view(self.store.channel(cid))

        @r.get("/channels/{cid}/events")
        async def events(cid: int, limit: int = 200):
            self._channel(cid)
            return self.store.q(
                "SELECT id, ts, level, clip_id, message FROM log WHERE channel_id=? ORDER BY id DESC LIMIT ?",
                cid,
                max(1, min(1000, limit)),
            )

        @r.get("/sources")
        async def sources():
            self._engine()
            out = []
            for p in PLATFORMS:
                pend = self.devices.get(p) or (
                    {"kind": "oauth", "redirect_uri": oauth.redirect_uri(p, self.ctx.port)}
                    if p in oauth.PROVIDERS and self.pending.pending(p)
                    else None
                )
                d = describe(p, self.secrets.chat_creds(p), self.settings, self.hub, pend)
                if p in oauth.PROVIDERS:
                    d["redirect_uri"] = oauth.redirect_uri(p, self.ctx.port)
                out.append(d)
            return out

        @r.post("/sources/{platform}/connect")
        async def connect(platform: str, body: ConnectIn):
            self._engine()
            if platform not in PLATFORMS:
                raise HTTPException(404, "unknown platform")
            try:
                return await self.connect(platform, body)
            except SourceAuthError as ex:
                raise HTTPException(400, {"detail": str(ex), "fix": ex.fix}) from None

        @r.get("/oauth/source/{platform}/callback", response_class=HTMLResponse)
        async def callback(platform: str, state: str = "", code: str = "", error: str = ""):
            item = self.pending.take(platform, state)
            if not item:
                raise HTTPException(400, "unknown or expired sign-in; start it again from Sources")
            if error or not code:
                return HTMLResponse(
                    f"<p>Sign-in was not completed ({error or 'no code'}). Start it again from Sources.</p>",
                    400,
                )
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=20) as http:
                    tok = await oauth.exchange(http, item, code)
            except SourceAuthError as ex:
                return HTMLResponse(f"<p>{ex}. {ex.fix}.</p>", 400)
            self.secrets.update("source", platform, tok)
            await self._rearm(platform)
            return HTMLResponse(f"<p>{platform.capitalize()} connected. You can close this tab.</p>")

        @r.delete("/sources/{platform}", status_code=204)
        async def disconnect(platform: str):
            if platform not in PLATFORMS:
                raise HTTPException(404, "unknown platform")
            self.secrets.delete(platform)
            self.devices.pop(platform, None)
            await self._rearm(platform)

        return r

    async def connect(self, platform: str, body: ConnectIn) -> dict:
        port = self.ctx.port
        if body.webhook_public_url is not None and platform == "kick":
            url = body.webhook_public_url.strip()
            if url and not url.startswith("https://"):
                raise HTTPException(422, "the Kick webhook URL must be a public https:// URL")
            self.ctx.settings.set("monitor.kick_webhook_url", url)
            self.settings.kick_webhook_url = url
        async with httpx.AsyncClient(transport=self.transport, timeout=20) as http:
            if body.method == "credentials":
                if platform == "youtube":
                    if not body.api_key:
                        raise HTTPException(422, "api_key is required")
                    from .sources.youtube import Client

                    await Client(lambda p: None)._get(
                        http, {"api_key": body.api_key}, "/videos", 1, part="id", id="none"
                    )  # 1 quota unit: proves the key works
                    self.secrets.update("source", platform, {"api_key": body.api_key})
                else:
                    if not (body.client_id and body.client_secret):
                        if platform == "kick" and body.webhook_public_url is not None and not body.client_id:
                            return describe(
                                platform, self.secrets.chat_creds(platform), self.settings, self.hub
                            )
                        raise HTTPException(422, "client_id and client_secret are required")
                    mod = twitch_src if platform == "twitch" else kick_src
                    tok = await mod.app_token(http, body.client_id, body.client_secret)
                    self.secrets.update(
                        "source",
                        platform,
                        {"client_id": body.client_id, "client_secret": body.client_secret, **tok},
                    )
            elif body.method == "device" and platform == "twitch":
                if not body.client_id:
                    raise HTTPException(422, "client_id is required")
                d = await twitch_src.device_start(http, body.client_id)
                info = {
                    "kind": "device",
                    "user_code": d["user_code"],
                    "verification_uri": d["verification_uri"],
                    "expires_at": time.time() + float(d.get("expires_in") or 1800),
                    "state": "waiting",
                }
                self.devices[platform] = info
                self._tasks.append(asyncio.create_task(self._device_wait(body.client_id, d, info)))
                return info
            elif body.method == "oauth" and platform in oauth.PROVIDERS:
                if not body.client_id:
                    raise HTTPException(422, "client_id is required")
                url = self.pending.start(platform, port, body.client_id, body.client_secret)
                return {
                    "kind": "oauth",
                    "authorize_url": url,
                    "redirect_uri": oauth.redirect_uri(platform, port),
                }
            else:
                raise HTTPException(422, f"method {body.method!r} is not available for {platform}")
        await self._rearm(platform)
        return describe(platform, self.secrets.chat_creds(platform), self.settings, self.hub)

    async def _device_wait(self, client_id: str, d: dict, info: dict) -> None:
        interval = float(d.get("interval") or 5)
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=20) as http:
                while time.time() < info["expires_at"]:
                    await asyncio.sleep(interval)
                    tok = await twitch_src.device_poll(http, client_id, d["device_code"])
                    if tok:
                        self.secrets.update("source", "twitch", {"client_id": client_id, **tok})
                        info["state"] = "connected"
                        self.devices.pop("twitch", None)
                        await self._rearm("twitch")
                        return
            info["state"], info["detail"] = "expired", "The code expired. Start the Twitch sign-in again."
        except SourceAuthError as e:
            info["state"], info["detail"] = "failed", f"{e}. {e.fix}."


__all__ = ["MonitorService", "parse_channel_url", "has_live_status_creds"]
