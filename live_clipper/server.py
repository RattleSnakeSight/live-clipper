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

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
    hosts = {f"127.0.0.1:{ctx.port}", f"localhost:{ctx.port}"}

    def authed(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        cookie = request.cookies.get(COOKIE, "")
        return _same(header, f"Bearer {ctx.token}") or (bool(cookie) and _same(cookie, ctx.token))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in hosts and not allow_remote():
            return JSONResponse({"detail": "invalid host"}, 400)  # DNS-rebinding protection
        origin = request.headers.get("origin")
        if request.method not in SAFE_METHODS and origin is not None and origin != f"http://{host}":
            return JSONResponse({"detail": "cross-origin request refused"}, 403)
        path = request.url.path
        if path.startswith("/api/") and path != "/api/health" and not authed(request):
            return JSONResponse({"detail": "missing or invalid token"}, 401)
        return await call_next(request)

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
