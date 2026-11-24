"""PublishService: subscribes to edit.clip_ready, runs the delivery worker, serves the destination/OAuth/delivery API."""
from __future__ import annotations

import html
import logging
import re
import secrets as _rnd
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from ..logging_setup import register_secret
from . import quota
from .requirements import requirements_for
from .store import Store
from .vault import CRED_FIELDS, OAUTH_TTL, Secrets
from .worker import ACTIVE, HOLD, REAL_PLATFORMS, TEST, TEST_LABEL, Publisher

log = logging.getLogger(__name__)
PROVIDERS = REAL_PLATFORMS
_SECRETISH = re.compile(r"(?i)secret|token|password|passwd|api[_-]?key|authorization|credential|client_key|client_id")
SETTING_KEYS = {"privacy", "mode", "privacy_level", "audited", "login", "api_version", "target_account_id",
                "video_base_url", "redirect_uri", "chunk_size", "daily_limit", "category_id", "tags", "share_to_feed",
                "poll_interval", "backoff_base", "concurrency", "timeout", "disable_comment", "disable_duet",
                "disable_stitch", "audio_language"}


_ENUMS = {
    "privacy": ("public", "unlisted", "private"),
    "privacy_level": ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"),
    "mode": ("inbox", "direct"),
    "login": ("instagram", "facebook"),
}
_BOOLS = {"audited", "share_to_feed", "disable_comment", "disable_duet", "disable_stitch"}


def check_settings(body: dict) -> dict[str, str]:
    """Field -> plain message for every setting value the adapters could not use. Empty string clears a setting."""
    bad: dict[str, str] = {}
    for k, v in body.items():
        if v == "" or v is None:
            continue
        if k in _ENUMS and v not in _ENUMS[k]:
            bad[k] = f"Choose one of: {', '.join(_ENUMS[k])}."
        elif k in _BOOLS and not isinstance(v, bool):
            bad[k] = "Must be on or off."
        elif k in ("video_base_url", "redirect_uri"):
            u = urlsplit(str(v))
            if k == "video_base_url" and u.scheme != "https":
                bad[k] = "Must start with https:// so Instagram can fetch the video."
            elif u.scheme not in ("http", "https") or not u.hostname or " " in str(v):
                bad[k] = "Enter a full web address such as https://example.com/clips."
        elif k in ("daily_limit", "chunk_size", "poll_interval", "timeout", "concurrency"):
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                bad[k] = "Enter a whole number, 0 or more."
        elif k in ("category_id", "target_account_id") and not re.fullmatch(r"\d{1,20}", str(v)):
            bad[k] = "Enter digits only."
        elif k == "tags" and not (isinstance(v, list) and all(isinstance(t, str) for t in v)):
            bad[k] = "Enter a list of tags."
    return bad


class Ack(BaseModel):
    acknowledged: bool


class Enabled(BaseModel):
    enabled: bool


class ModeBody(BaseModel):
    mode: str


class TestModeBody(BaseModel):
    enabled: bool


class AppCreds(BaseModel):
    client_id: str | None = None
    client_secret: str | None = None
    client_key: str | None = None


class NewDest(BaseModel):
    provider: str
    label: str = ""


class IgToken(BaseModel):
    access_token: str
    expires_in: int | None = None
    login: str | None = None
    destination_id: int | None = None


class UrlBody(BaseModel):
    url: str = ""


class Confirm(BaseModel):
    confirm: bool = False


class PostBody(BaseModel):
    clip_id: int | str
    destination_id: int
    language: str = "en"


class Platforms(BaseModel):
    platforms: list[str] | None = None


def _page(title: str, msg: str, code: int = 200) -> HTMLResponse:
    body = (f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
            f"<body style='font-family:sans-serif;max-width:40em;margin:3em auto'><h1>{html.escape(title)}</h1>"
            f"<p>{html.escape(msg)}</p><p>You can close this tab and return to Live Clipper.</p></body>")
    return HTMLResponse(body, status_code=code)


class PublishService:
    name = "publish"

    def __init__(self) -> None:
        self.ctx = None
        self.pub: Publisher | None = None
        self.store: Store | None = None
        self.secrets: Secrets | None = None
        self.router = APIRouter(prefix="/api")
        self._routes()

    # ---- lifecycle ---------------------------------------------------------
    async def start(self, ctx) -> None:
        self.ctx = ctx
        from . import sandbox_server
        sandbox_server.APP_PORT = ctx.port
        self.store = Store(ctx.db)
        self.secrets = Secrets(ctx.secrets)
        self.pub = Publisher(self.store, ctx.settings, self.secrets, sandbox_dir=ctx.paths.root / "sandbox",
                             notify=ctx.notify, emit=lambda p: ctx.bus.publish("publish.delivery", p))
        for prov in PROVIDERS:
            if not self.store.q1("SELECT id FROM publish_destinations WHERE provider=?", prov):
                self.pub.add_destination(prov, requirements_for(prov)["label"])
        if ctx.settings.get("publish.test_mode"):
            await self.pub.ensure_test_destination()
        self._unsub = ctx.bus.subscribe("edit.clip_ready", self._on_clip_ready)
        await self.pub.start()

    async def stop(self) -> None:
        if getattr(self, "_unsub", None):
            self._unsub()
        if self.pub:
            await self.pub.stop()
        from .sandbox_server import shutdown_sandbox
        shutdown_sandbox()

    def status(self) -> dict:
        if not self.store:
            return {"started": False}
        counts = {r["status"]: r["n"] for r in self.store.q("SELECT status, COUNT(*) AS n FROM publish_deliveries "
                                                            "GROUP BY status")}
        elig = [d["provider"] for d in self.store.q("SELECT * FROM publish_destinations")
                if self.pub.eligible(d) and d["status"] == "connected"]
        return {"started": True, "mode": self.pub.mode, "eligible_destinations": elig, "deliveries": counts}

    def _on_clip_ready(self, topic: str, payload: dict) -> None:
        if not isinstance(payload, dict) or "clip_id" not in payload or not payload.get("path"):
            log.warning("ignoring malformed edit.clip_ready payload")
            return
        try:
            self.pub.enqueue_clip(payload)
        except Exception:
            log.exception("could not enqueue clip %s", payload.get("clip_id"))

    # ---- views -----------------------------------------------------------
    def _setup(self, d: dict) -> dict:
        if d["provider"] == TEST:
            return {"state": "active" if d["enabled"] else "ready_to_enable", "steps": []}
        tok = bool((self.secrets.get("dest", d["id"]) or {}).get("access_token"))
        app = self.secrets.get("app", d["provider"]) or {}
        has_app = bool(app.get("client_id") or app.get("client_key"))
        steps = [{"id": "app_credentials", "done": has_app or tok},
                 {"id": "connect", "done": d["status"] == "connected"},
                 {"id": "rights_acknowledgement", "done": bool(d["rights_ack_at"])},
                 {"id": "enable", "done": bool(d["enabled"])}]
        if d["status"] == "needs_reconnect":
            state = "needs_reconnect"
        elif not (has_app or tok) and d["status"] != "connected":
            state = "needs_credentials"
        elif d["status"] != "connected":
            state = "needs_connect"
        elif not d["rights_ack_at"]:
            state = "needs_acknowledgement"
        elif not d["enabled"]:
            state = "ready_to_enable"
        else:
            state = "active"
        return {"state": state, "steps": steps}

    def dest_view(self, d: dict) -> dict:
        is_test = d["provider"] == TEST
        s = {k: v for k, v in (d["settings"] or {}).items() if k in SETTING_KEYS}
        lim = quota.daily_limit(d)
        req = requirements_for(d["provider"], self.ctx.port)
        if not is_test:
            req["redirect_uri"] = self._redirect_uri(d)       # the address this installation really sends
        # the account the provider reported at the last check (public identifiers, never a token)
        acct_id = str(((d["settings"] or {}).get("account_info") or {}).get("account_id") or "") \
            if d["status"] == "connected" else ""
        acct_url = {"youtube": f"https://www.youtube.com/channel/{acct_id}" if acct_id else "",
                    "instagram": f"https://www.instagram.com/{d['account']}/"
                    if re.fullmatch(r"[\w.]{1,30}", d["account"] or "") and acct_id else ""}.get(d["provider"], "")
        return {"id": d["id"], "provider": d["provider"], "label": TEST_LABEL if is_test else d["label"],
                "test": is_test, "account": d["account"], "account_id": acct_id, "account_url": acct_url,
                "status": d["status"],
                "status_detail": self.secrets.redact(d["status_detail"]), "enabled": bool(d["enabled"]),
                "rights_acknowledged": bool(d["rights_ack_at"]), "rights_ack_at": d["rights_ack_at"],
                "credentials_set": self.secrets.fields("dest", d["id"]),
                "app_credentials_set": [k for k in self.secrets.fields("app", d["provider"]) if k in CRED_FIELDS],
                "settings": s, "setup": self._setup(d),
                "requirements": req,
                "quota": {"used_today": quota.used(self.store, d), "limit": lim} if lim else None}

    def delivery_view(self, d: dict, dest: dict | None = None) -> dict:
        dest = dest or self.store.q1("SELECT * FROM publish_destinations WHERE id=?", d["destination_id"]) or {}
        is_test = dest.get("provider") == TEST
        return {"id": d["id"], "clip_id": d["clip_id"], "destination_id": d["destination_id"],
                "destination": dest.get("provider", ""), "destination_label": TEST_LABEL if is_test else dest.get("label", ""),
                "test": is_test, "real": not is_test, "language": d["language"], "status": d["status"], "mode": d["mode"],
                "attempts": d["attempts"], "outcome": d["outcome"], "url": d["provider_url"],
                "post_id": d["provider_post_id"], "last_error": self.secrets.redact(d["last_error"]),
                "next_attempt_at": d["next_attempt_at"], "created_at": d["created_at"], "updated_at": d["updated_at"],
                "history": d["history"][-10:]}

    def _dest(self, dest_id: int) -> dict:
        d = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest_id)
        if not d:
            raise HTTPException(404, "destination not found")
        return d

    def _guard(self):
        if not self.pub:
            raise HTTPException(503, "publishing service is not running")

    def _act(self, fn, *a):
        self._guard()
        try:
            fn(*a)
        except KeyError as e:
            raise HTTPException(404, str(e).strip("'\"")) from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except ValueError as e:
            raise HTTPException(409, str(e)) from e

    # ---- OAuth -----------------------------------------------------------
    def _redirect_uri(self, dest: dict) -> str:
        return (dest["settings"] or {}).get("redirect_uri") or \
            f"http://127.0.0.1:{self.ctx.port}/api/oauth/{dest['provider']}/callback"

    def begin_oauth(self, dest: dict) -> str:
        app = dict(self.secrets.get("app", dest["provider"]) or {})
        if dest["provider"] == TEST:
            app.setdefault("client_id", "test")
        if not (app.get("client_id") or app.get("client_key")):
            raise HTTPException(400, "enter this app's client ID (and secret) first, or use the token form")
        adapter = self.pub.adapter(dest)
        self.secrets.purge_oauth()
        state, verifier = _rnd.token_urlsafe(32), _rnd.token_urlsafe(48)
        redirect = self._redirect_uri(dest)
        self.secrets.update("oauth", state, {"destination_id": dest["id"], "provider": dest["provider"],
                                             "verifier": verifier, "redirect_uri": redirect, "t": time.time()})
        return adapter.authorize_url(app, redirect, state, adapter.challenge(verifier))

    async def finish_oauth(self, provider: str, params: dict) -> HTMLResponse:
        state = params.get("state") or ""
        entry = self.secrets.get("oauth", state) if state else None
        if state:
            self.secrets.delete("oauth", state)                       # single use, whatever happens next
        if not entry or entry.get("provider") != provider or time.time() - float(entry.get("t", 0)) > OAUTH_TTL:
            return _page("Sign-in link invalid", "This sign-in link is unknown or expired. Start again from Destinations.",
                         400)
        dest = self.store.q1("SELECT * FROM publish_destinations WHERE id=?", entry["destination_id"])
        if not dest:
            return _page("Destination missing", "The destination was removed.", 404)
        if params.get("error"):
            code = re.sub(r"[^a-z_]", "", str(params["error"]).lower())[:40] or "denied"
            self.store.update("destinations", dest["id"], status_detail=f"authorization was not granted ({code})")
            return _page("Not connected", f"The provider reported: {code}. Nothing was changed.", 400)
        if not params.get("code"):
            return _page("Not connected", "The provider returned no authorization code.", 400)
        register_secret(params["code"])      # providers sometimes echo the code in error text
        adapter = self.pub.adapter(dest)
        app = dict(self.secrets.get("app", provider) or {})
        if provider == TEST:
            app.setdefault("client_id", "test")
        try:
            tokens = await adapter.exchange_code(app, params["code"], entry["redirect_uri"], entry["verifier"])
        except Exception as e:
            msg = self.secrets.redact(f"token exchange failed: {type(e).__name__}: {e}")[:300]
            self.store.update("destinations", dest["id"], status="error", status_detail=msg)
            log.warning("%s", msg)
            return _page("Not connected", "The provider refused the sign-in code. Try again.", 502)
        self.pub.begin_credential_change(dest["id"], "new sign-in")
        self.pub.replace_tokens(dest["id"], tokens)
        res = await self.pub.connect(dest["id"])
        if not res.get("ok"):
            return _page("Signed in, but not verified", self.secrets.redact(res.get("detail", ""))[:300], 502)
        return _page("Connected", f"Connected as {res.get('account') or 'your account'}." + (
            " This is a different account than before, so posting to this destination was switched off. Check the "
            "account in Live Clipper, then switch posting on again." if res.get("moved") else ""))

    # ---- routes ----------------------------------------------------------
    def _routes(self) -> None:
        r = self.router

        @r.get("/destinations")
        def list_destinations():
            self._guard()
            return {"destinations": [self.dest_view(d) for d in
                                     self.store.q("SELECT * FROM publish_destinations ORDER BY id")]}

        @r.post("/destinations")
        def add_destination(body: NewDest):
            self._guard()
            if body.provider not in PROVIDERS:
                raise HTTPException(400, f"provider must be one of {list(PROVIDERS)}")
            did = self.pub.add_destination(body.provider, body.label or requirements_for(body.provider)["label"])
            return self.dest_view(self._dest(did))

        @r.get("/destinations/{dest_id}")
        def get_destination(dest_id: int):
            self._guard()
            return self.dest_view(self._dest(dest_id))

        @r.put("/destinations/{dest_id}/credentials")
        def put_credentials(dest_id: int, body: AppCreds):
            """The user's own developer-app client id/secret. Written to the vault, never returned."""
            self._guard()
            d = self._dest(dest_id)
            if d["provider"] == TEST:
                raise HTTPException(400, "the test destination has no credentials")
            patch = {k: v.strip() for k, v in body.model_dump().items() if v is not None}
            self.secrets.update("app", d["provider"], patch)
            return {"app_credentials_set": [k for k in self.secrets.fields("app", d["provider"]) if k in CRED_FIELDS]}

        @r.put("/destinations/{dest_id}/settings")
        def put_settings(dest_id: int, body: dict):
            self._guard()
            d = self._dest(dest_id)
            bad = [k for k in body if k not in SETTING_KEYS or _SECRETISH.search(k)]
            if bad:
                raise HTTPException(400, f"unsupported or secret-looking setting(s): {bad}; credentials go to /credentials")
            if errs := check_settings(body):
                raise HTTPException(400, {"message": "; ".join(errs.values()), "errors": errs})
            st = {**(d["settings"] or {}), **{k: v for k, v in body.items() if v not in ("", None)}}
            for k, v in body.items():
                if v in ("", None):
                    st.pop(k, None)
            self.store.update("destinations", dest_id, settings=st)
            return self.dest_view(self._dest(dest_id))

        @r.post("/destinations/{dest_id}/connect")
        async def connect(dest_id: int):
            self._guard()
            d = self._dest(dest_id)
            if d["provider"] == TEST:
                await self.pub.ensure_test_destination()
                self.pub.save_tokens(dest_id, await self.pub.adapter(d).issue_test_token())
                self.pub.begin_credential_change(dest_id, "test sign-in")
                return {"action": "verified", **await self.pub.connect(dest_id)}
            tok = (self.secrets.get("dest", dest_id) or {}).get("access_token")
            if tok and d["status"] not in ("needs_reconnect", "error"):
                return {"action": "verified", **await self.pub.connect(dest_id)}
            return {"action": "authorize", "authorize_url": self.begin_oauth(d)}

        @r.post("/destinations/{dest_id}/disconnect")
        def disconnect(dest_id: int):
            self._act(self.pub.disconnect, dest_id)
            return self.dest_view(self._dest(dest_id))

        @r.put("/destinations/{dest_id}/enabled")
        def set_enabled(dest_id: int, body: Enabled):
            self._guard()
            d = self._dest(dest_id)
            if body.enabled:
                if d["provider"] != TEST:
                    if d["status"] != "connected":
                        raise HTTPException(409, "connect the destination before enabling it")
                    if not d["rights_ack_at"]:
                        raise HTTPException(409, "acknowledge that you have the streamer's permission first")
                # switched on (again): the card goes back to the account's own note
                note = ((d["settings"] or {}).get("account_info") or {}).get("notes", "")
                self.store.update("destinations", dest_id, enabled=1,
                                  **({"status_detail": note} if d["status"] == "connected" else {}))
                self.pub.unblock_allowed()
            else:
                self.store.update("destinations", dest_id, enabled=0)
                self.pub.block_destination(dest_id)
            return self.dest_view(self._dest(dest_id))

        @r.put("/destinations/{dest_id}/acknowledge")
        def acknowledge(dest_id: int, body: Ack):
            self._guard()
            d = self._dest(dest_id)
            if body.acknowledged:
                self.store.update("destinations", dest_id, rights_ack_at=time.time())
            else:
                self.store.update("destinations", dest_id, rights_ack_at=None, enabled=0)
                self.pub.block_destination(dest_id)
            return self.dest_view(self._dest(d["id"]))

        @r.post("/destinations/instagram/token")
        async def instagram_token(body: IgToken):
            """Paste form for a long-lived Instagram access token (dashboard "Generate token"). Vault only."""
            self._guard()
            tok = body.access_token.strip()
            if len(tok) < 16:
                raise HTTPException(400, "that does not look like an access token")
            d = (self._dest(body.destination_id) if body.destination_id else
                 self.store.q1("SELECT * FROM publish_destinations WHERE provider='instagram' ORDER BY id LIMIT 1"))
            if not d or d["provider"] != "instagram":
                raise HTTPException(404, "instagram destination not found")
            if body.login in ("instagram", "facebook"):
                self.store.update("destinations", d["id"], settings={**d["settings"], "login": body.login})
            self.pub.begin_credential_change(d["id"], "token pasted")
            self.pub.replace_tokens(d["id"], {"access_token": tok, "expires_in": body.expires_in or 60 * 86400})
            res = await self.pub.connect(d["id"])
            if not res.get("ok"):
                raise HTTPException(400, self.secrets.redact(res.get("detail", "verification failed")))
            return self.dest_view(self._dest(d["id"]))

        @r.get("/oauth/{provider}/start")
        def oauth_start(provider: str, request: Request, destination_id: int | None = None, format: str = ""):
            self._guard()
            if provider not in PROVIDERS and provider != TEST:
                raise HTTPException(404, "unknown provider")
            d = (self._dest(destination_id) if destination_id else
                 self.store.q1("SELECT * FROM publish_destinations WHERE provider=? ORDER BY id LIMIT 1", provider))
            if not d or d["provider"] != provider:
                raise HTTPException(404, "destination not found")
            url = self.begin_oauth(d)
            if format == "json" or "application/json" in request.headers.get("accept", ""):
                return {"authorize_url": url}
            return RedirectResponse(url, status_code=302)

        @r.get("/oauth/{provider}/callback")
        async def oauth_callback(provider: str, request: Request):
            self._guard()
            return await self.finish_oauth(provider, dict(request.query_params))

        @r.get("/publish/mode")
        def get_mode():
            self._guard()
            return {"mode": self.pub.mode, "modes": ["auto", "manual"],
                    "detail": "auto posts finished clips to every connected, enabled destination; manual holds them "
                              "until you click Post"}

        @r.put("/publish/mode")
        def put_mode(body: ModeBody):
            self._guard()
            if body.mode not in ("auto", "manual"):
                raise HTTPException(400, "mode must be auto or manual")
            self.ctx.settings.set("publish.mode", body.mode)
            return {"mode": body.mode}

        @r.get("/publish/test-mode")
        def get_test_mode():
            self._guard()
            return {"enabled": bool(self.ctx.settings.get("publish.test_mode")), "label": TEST_LABEL}

        @r.put("/publish/test-mode")
        async def put_test_mode(body: TestModeBody):
            self._guard()
            self.ctx.settings.set("publish.test_mode", body.enabled)
            if body.enabled:
                did = await self.pub.ensure_test_destination()
                self.store.update("destinations", did, enabled=1)
                self.pub.unblock_allowed()
            else:
                t = self.store.q1("SELECT id FROM publish_destinations WHERE provider=?", TEST)
                if t:
                    self.store.update("destinations", t["id"], enabled=0)
                    self.pub.block_destination(t["id"])
            return {"enabled": body.enabled, "label": TEST_LABEL}

        @r.get("/publish/channel-platforms")
        def get_channel_platforms():
            return {"channels": self.ctx.settings.get("publish.channel_platforms") or {}}

        @r.put("/publish/channel-platforms/{channel_id}")
        def put_channel_platforms(channel_id: str, body: Platforms):
            m = dict(self.ctx.settings.get("publish.channel_platforms") or {})
            if body.platforms is None:
                m.pop(channel_id, None)
            else:
                bad = [p for p in body.platforms if p not in PROVIDERS]
                if bad:
                    raise HTTPException(400, f"unknown platform(s): {bad}")
                m[channel_id] = body.platforms
            self.ctx.settings.set("publish.channel_platforms", m)
            return {"channel_id": channel_id, "platforms": body.platforms}

        @r.get("/publish/deliveries")
        def list_deliveries(status: str | None = None, destination_id: int | None = None, clip_id: str | None = None,
                            include_test: bool = True, limit: int = 100, offset: int = 0):
            self._guard()
            where, args = [], []
            if status:
                where.append(f"d.status IN ({','.join('?' * len(status.split(',')))})")
                args += status.split(",")
            if destination_id is not None:
                where.append("d.destination_id=?")
                args.append(destination_id)
            if clip_id is not None:
                where.append("CAST(d.clip_id AS TEXT)=?")
                args.append(clip_id)
            if not include_test:
                where.append("t.provider<>'test'")
            rows = self.store.q("SELECT d.* FROM publish_deliveries d JOIN publish_destinations t ON "
                                "t.id=d.destination_id" + (" WHERE " + " AND ".join(where) if where else "") +
                                " ORDER BY d.id DESC LIMIT ? OFFSET ?", *args, max(1, min(limit, 500)), max(0, offset))
            return {"deliveries": [self.delivery_view(x) for x in rows]}

        @r.get("/publish/deliveries/{did}")
        def get_delivery(did: int):
            self._guard()
            d = self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did)
            if not d:
                raise HTTPException(404, "delivery not found")
            return self.delivery_view(d)

        def action(name, fn):
            def handler(did: int):
                self._act(fn, did)
                return self.delivery_view(self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did))
            handler.__name__ = name
            r.post(f"/publish/deliveries/{{did}}/{name.replace('_', '-')}")(handler)

        action("post", lambda did: self.pub.post(did))
        action("retry", lambda did: self.pub.retry(did))
        action("cancel", lambda did: self.pub.cancel(did))

        @r.post("/publish/deliveries/{did}/confirm-posted")
        def confirm_posted(did: int, body: UrlBody | None = None):
            url = (body.url if body else "").strip()
            if url and (urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).hostname or " " in url):
                raise HTTPException(400, {"message": "Enter the full address of the post, starting with https://.",
                                          "errors": {"url": "Enter the full address of the post, starting with https://."}})
            self._act(self.pub.confirm_posted, did, url)
            return self.delivery_view(self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did))

        @r.post("/publish/deliveries/{did}/confirm-not-posted")
        def confirm_not_posted(did: int, body: Confirm):
            if not body.confirm:
                raise HTTPException(400, 'send {"confirm": true} to re-upload after checking the account')
            self._act(self.pub.confirm_not_posted, did)
            return self.delivery_view(self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did))

        @r.post("/publish/deliveries/{did}/recheck")
        async def recheck(did: int):
            self._guard()
            try:
                await self.pub.recheck(did)
            except ValueError as e:
                raise HTTPException(409, str(e)) from e
            return self.delivery_view(self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did))

        @r.post("/publish/post")
        def post_clip(body: PostBody):
            """Post a finished clip to a destination that has no delivery for it yet."""
            self._guard()
            try:
                did = self.pub.post_clip(body.clip_id, body.destination_id, body.language)
            except KeyError as e:
                raise HTTPException(404, "clip or destination not found") from e
            except PermissionError as e:
                raise HTTPException(403, str(e)) from e
            except Exception as e:
                raise HTTPException(409, str(e)) from e
            return self.delivery_view(self.store.q1("SELECT * FROM publish_deliveries WHERE id=?", did))


__all__ = ["ACTIVE", "HOLD", "PublishService"]
