import asyncio
import sys

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from live_clipper.paths import get_paths
from live_clipper.server import create_app, ensure_token
from live_clipper.services import EventBus, build_context, load_services

BASE = "http://127.0.0.1:8765"


@pytest.fixture(autouse=True)
def no_keyring(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)


class DemoService:
    name = "demo"

    def __init__(self):
        self.router = APIRouter(prefix="/api/demo")
        self.router.get("/ping")(lambda: {"pong": True})
        self.events = []

    async def start(self, ctx):
        self.events.append("start")
        ctx.bus.publish("demo.started", {"ok": True})

    async def stop(self):
        self.events.append("stop")

    def status(self):
        return {"running": "start" in self.events}


@pytest.fixture
def ctx():
    paths = get_paths()
    return build_context(paths, port=8765, token=ensure_token(paths, regenerate=True))


def auth(ctx):
    return {"Authorization": f"Bearer {ctx.token}"}


def test_token_persisted_in_state_dir():
    paths = get_paths()
    t1 = ensure_token(paths, regenerate=True)
    assert len(t1) >= 32
    assert ensure_token(paths) == t1
    assert (paths.state / "token").read_text().strip() == t1
    assert ensure_token(paths, regenerate=True) != t1


def test_health_needs_no_auth(ctx):
    r = TestClient(create_app(ctx), base_url=BASE).get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["version"] and body["pid"] > 0 and body["uptime"] >= 0


def test_api_requires_token(ctx):
    c = TestClient(create_app(ctx), base_url=BASE)
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/status", headers=auth(ctx)).status_code == 200


def test_token_link_sets_strict_httponly_cookie_and_redirects(ctx):
    c = TestClient(create_app(ctx), base_url=BASE, follow_redirects=False)
    bad = c.get("/?t=wrong")
    assert "set-cookie" not in bad.headers
    r = c.get(f"/?t={ctx.token}")
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.get("/api/status").status_code == 200  # cookie now in the client jar
    assert c.get("/").status_code == 200


def test_rejects_foreign_host_header(ctx):
    c = TestClient(create_app(ctx), base_url=BASE)
    assert c.get("/api/health", headers={"Host": "evil.example:8765"}).status_code == 400
    assert c.get("/api/health", headers={"Host": "127.0.0.1:9999"}).status_code == 400
    assert c.get("/api/health", headers={"Host": "localhost:8765"}).status_code == 200


def test_rejects_cross_origin_state_change(ctx):
    c = TestClient(create_app(ctx), base_url=BASE)
    url = "/api/app-state/wizard_done"
    evil = c.put(url, json={"value": True}, headers={**auth(ctx), "Origin": "http://evil.example"})
    assert evil.status_code == 403
    ok = c.put(url, json={"value": True}, headers={**auth(ctx), "Origin": BASE})
    assert ok.status_code == 200
    assert c.put(url, json={"value": 2}, headers=auth(ctx)).status_code == 200  # non-browser client


def test_app_state_roundtrip(ctx):
    c = TestClient(create_app(ctx), base_url=BASE, headers=auth(ctx))
    assert c.get("/api/app-state/tour").json() == {"key": "tour", "value": None}
    c.put("/api/app-state/tour", json={"value": {"step": 3}})
    assert c.get("/api/app-state/tour").json() == {"key": "tour", "value": {"step": 3}}


def test_services_mounted_started_and_reported(ctx):
    svc = DemoService()
    ctx.services.append(svc)
    seen = []
    ctx.bus.subscribe("demo.*", lambda topic, payload: seen.append((topic, payload)))
    with TestClient(create_app(ctx), base_url=BASE, headers=auth(ctx)) as c:
        assert svc.events == ["start"]
        assert c.get("/api/demo/ping").json() == {"pong": True}
        status = c.get("/api/status").json()
        assert status["services"]["demo"] == {"running": True}
    assert svc.events == ["start", "stop"]
    assert seen == [("demo.started", {"ok": True})]


def test_load_services_from_import_paths():
    services = load_services(["test_server:DemoService"])
    assert [s.name for s in services] == ["demo"]


def test_bus_wildcards_and_async_handlers():
    bus = EventBus()
    got = []

    async def ahandler(topic, payload):
        got.append(("async", topic))

    bus.subscribe("*", lambda t, p: got.append(("all", t)))
    bus.subscribe("edit.*", lambda t, p: got.append(("edit", t)))
    unsubscribe = bus.subscribe("edit.done", ahandler)
    bus.subscribe("edit.done", lambda t, p: 1 / 0)  # a broken handler must not break others

    async def main():
        bus.publish("edit.done", {"clip": 1})
        bus.publish("select.picked", None)
        await asyncio.sleep(0)
        unsubscribe()
        bus.publish("edit.done", {})
        await asyncio.sleep(0)

    asyncio.run(main())
    assert got.count(("async", "edit.done")) == 1
    assert ("edit", "edit.done") in got and ("all", "select.picked") in got
    assert ("edit", "select.picked") not in got


def test_notify_publishes_on_bus(ctx):
    got = []
    ctx.bus.subscribe("app.notify", lambda t, p: got.append(p))
    ctx.notify("Clip ready", "3 new clips")
    assert got == [{"title": "Clip ready", "body": "3 new clips"}]
