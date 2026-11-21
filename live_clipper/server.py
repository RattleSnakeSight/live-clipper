"""HTTP app: loopback-only, local token auth, DNS-rebinding and cross-origin protection."""

import asyncio
import hmac
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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
        oauth_callback = scope.get("method") == "GET" and path.startswith("/api/oauth/") and path.endswith("/callback")
        if path.startswith("/api/") and path != "/api/health" and not oauth_callback:
            header = conn.headers.get("authorization", "")
            cookie = conn.cookies.get(COOKIE, "")
            if not (_same(header, f"Bearer {self.ctx.token}") or (cookie and _same(cookie, self.ctx.token))):
                return 401, "missing or invalid token"
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            refused = self.refuse(HTTPConnection(scope), scope)
            if refused and scope["type"] == "websocket":
                return await WebSocketClose(code=1008, reason=refused[1])(scope, receive, send)
            if refused:
                return await JSONResponse({"detail": refused[1]}, refused[0])(scope, receive, send)
        await self.app(scope, receive, send)


class StateValue(BaseModel):
    value: Any = None


def create_app(ctx: AppContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.bus.loop = asyncio.get_running_loop()
        started = []
        for svc in ctx.services:
            try:
                await svc.start(ctx)
                started.append(svc)
            except Exception:
                log.exception("service %s failed to start", svc.name)
        yield
        for svc in reversed(started):
            try:
                await svc.stop()
            except Exception:
                log.exception("service %s failed to stop", svc.name)

    app = FastAPI(title="Live Clipper", version=ctx.version, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.add_middleware(Guard, ctx=ctx)

    def uptime() -> float:
        return round(time.time() - ctx.started_at, 1)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": ctx.version, "uptime": uptime(), "pid": os.getpid()}

    @app.get("/api/status")
    def status():
        services = {}
        for svc in ctx.services:
            try:
                services[svc.name] = svc.status()
            except Exception as e:
                services[svc.name] = {"error": str(e)}
        return {"version": ctx.version, "uptime": uptime(), "pid": os.getpid(), "services": services}

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
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(COOKIE, ctx.token, max_age=30 * 86400, httponly=True, samesite="strict")
            return response
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
