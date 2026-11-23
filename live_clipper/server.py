"""HTTP app: loopback-only, local token auth, DNS-rebinding and cross-origin protection."""

import asyncio
import hmac
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketClose

from .logging_setup import register_secret
from .paths import Paths
from .secrets import write_private
from .services import AppContext

log = logging.getLogger(__name__)
COOKIE = "live_clipper_token"
WEB_DIR = Path(__file__).parent / "web"
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def allow_remote() -> bool:
    return os.environ.get("LIVE_CLIPPER_ALLOW_REMOTE") == "1"


def ensure_token(paths: Paths, regenerate: bool = False) -> str:
    """The local access token, persisted in the state dir. The supervisor regenerates it on each start."""
    path = paths.state / "token"
    if regenerate or not path.exists():
        write_private(path, secrets.token_urlsafe(32).encode())
    token = path.read_text().strip()
    register_secret(token)
    return token


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# What still answers while the update gate is closed: health and status, the update check itself, reading app
# flags (the theme) and the API reference. Everything else under /api/ is refused with 423.
OPEN_WHILE_GATED = ("/api/health", "/api/status", "/api/update", "/api/update/check", "/api/docs",
                    "/api/openapi.json")
GATED = {
    "checking": "Live Clipper is checking for updates. It starts once the check is done.",
    "pending": "Update pending: install the new version of Live Clipper, then start it again.",
    "unknown": "Live Clipper could not check for updates. It tries again by itself, and runs once it can.",
}


def open_while_gated(path: str, method: str | None) -> bool:
    return path in OPEN_WHILE_GATED or (method == "GET" and path.startswith("/api/app-state/"))


class Guard:
    """ASGI middleware for HTTP and WebSocket: Host check, same-origin check, token check on /api/*."""

    def __init__(self, app, ctx: AppContext):
        self.app, self.ctx = app, ctx
        self.hosts = {f"127.0.0.1:{ctx.port}", f"localhost:{ctx.port}"}

    def refuse(self, conn: HTTPConnection, scope) -> tuple[int, str] | None:
        host = conn.headers.get("host", "")
        if host not in self.hosts and not allow_remote():
            return 400, "invalid host"  # DNS-rebinding protection
        origin = conn.headers.get("origin")
        changes_state = scope["type"] == "websocket" or scope["method"] not in SAFE_METHODS
        if changes_state and origin is not None and origin != f"http://{host}":
            return 403, "cross-origin request refused"
        path = conn.url.path
        # OAuth provider redirects arrive cross-site, so the SameSite=Strict cookie is absent. Those GET
        # callbacks are authenticated by their single-use, server-side `state` instead of the token.
        oauth_callback = (scope.get("method") == "GET" and path.startswith("/api/oauth/")
                          and path.endswith("/callback"))
        if path.startswith("/api/") and path != "/api/health" and not oauth_callback:
            header = conn.headers.get("authorization", "")
            cookie = conn.cookies.get(COOKIE, "")
            if not (_same(header, f"Bearer {self.ctx.token}") or (cookie and _same(cookie, self.ctx.token))):
                return 401, "missing or invalid token"
        gate = getattr(self.ctx, "update_gate", None)
        if gate is not None and gate.state != "current" and path.startswith("/api/") and not open_while_gated(
                path, scope.get("method")):
            return 423, "update"
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            refused = self.refuse(HTTPConnection(scope), scope)
            if refused and scope["type"] == "websocket":
                code = 1013 if refused[0] == 423 else 1008  # 1013: try again later
                return await WebSocketClose(code=code, reason=refused[1])(scope, receive, send)
            if refused and refused[0] == 423:
                view = self.ctx.update_gate.view()
                return await JSONResponse({"detail": GATED[view["state"]], "update": view}, 423)(
                    scope, receive, send)
            if refused:
                return await JSONResponse({"detail": refused[1]}, refused[0])(scope, receive, send)
        await self.app(scope, receive, send)


# the API reference may load this app's own files and nothing else (data: is the empty tab icon)
DOCS_CSP = ("default-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


class StateValue(BaseModel):
    value: Any = None


class CheckIn(BaseModel):
    kind: Literal["open", "after_start", "retry"] = "open"


def create_app(ctx: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.bus.loop = asyncio.get_running_loop()
        started = []

        async def start(services) -> None:
            for svc in services:
                try:
                    await svc.start(ctx)
                    started.append(svc)
                except Exception:
                    log.exception("service %s failed to start", svc.name)

        gate = ctx.update_gate
        if gate is None:
            await start(ctx.services)
        else:
            # only the heartbeat runs until the update check says this copy is current
            await start([s for s in ctx.services if getattr(s, "essential", False)])
            gate.on_current = lambda: start([s for s in ctx.services if s not in started])
            await gate.start()
        yield
        if gate is not None:
            await gate.stop()
        for svc in reversed(started):
            try:
                await svc.stop()
            except Exception:
                log.exception("service %s failed to stop", svc.name)

    # docs_url=None: the framework's docs page loads its viewer from a public CDN, which fails offline and
    # shows a third party who opened it. /api/docs below is a local page that reads /api/openapi.json instead.
    app = FastAPI(title="Live Clipper", version=ctx.version, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.add_middleware(Guard, ctx=ctx)

    @app.get("/api/docs", include_in_schema=False)
    def api_docs():
        return FileResponse(WEB_DIR / "api-docs.html", headers={"Content-Security-Policy": DOCS_CSP})

    def uptime() -> float:
        return round(time.time() - ctx.started_at, 1)

    def update_view() -> dict:
        # no gate: an in-process app (tests, tools); the worker always has one
        return ctx.update_gate.view() if ctx.update_gate else {"state": "current", "installed": ctx.version}

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": ctx.version, "uptime": uptime(), "pid": os.getpid(),
                "update": update_view()["state"]}

    @app.get("/api/update")
    def update_state():
        return update_view()

    @app.post("/api/update/check")
    async def update_check(body: CheckIn | None = None):
        """Check now: each open of the app (the CLI, a browser tab) and Check again. Overlapping calls share
        one request, and the CLI's call right after a start reuses the worker's start check."""
        if ctx.update_gate is None:
            return update_view()
        try:
            return await asyncio.wait_for(ctx.update_gate.check(body.kind if body else "open"), 15)
        except TimeoutError:
            return update_view()

    @app.get("/api/status")
    def status():
        services = {}
        gate = ctx.update_gate
        for svc in ctx.services:
            if gate is not None and not gate.services_started and not getattr(svc, "essential", False):
                services[svc.name] = {"state": "paused", "reason": "update check"}
                continue
            try:
                services[svc.name] = svc.status()
            except Exception as e:
                services[svc.name] = {"error": str(e)}
        return {"version": ctx.version, "uptime": uptime(), "pid": os.getpid(), "services": services,
                "update": update_view()}

    @app.get("/api/app-state/{key}")
    def get_app_state(key: str):
        return {"key": key, "value": ctx.app_state.get(key)}

    @app.put("/api/app-state/{key}")
    def put_app_state(key: str, body: StateValue):
        ctx.app_state.set(key, body.value)
        return {"key": key, "value": body.value}

    for svc in ctx.services:
        if svc.router is not None:
            app.include_router(svc.router)

    @app.get("/", include_in_schema=False)
    def index(t: str | None = None):
        if t and _same(t, ctx.token):
            log.info("browser opened the app (one-time link redeemed)")
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(COOKIE, ctx.token, max_age=30 * 86400, httponly=True, samesite="strict")
            return response
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
