import asyncio
import json

import httpx
from ops_common import client, make_ctx, no_keyring  # noqa: F401

from live_clipper.diagnostics import DiagnosticsService
from live_clipper.notifications import NotificationService, build_request, detect_format

URL = "https://ntfy.example.com/topic_secret_xyz"


def setup(method="webhook", url=URL, fmt="auto", status=200):
    ctx = make_ctx()
    ctx.settings.set("notify.method", method)
    ctx.settings.set("notify.webhook_format", fmt)
    ctx.secrets.set("notifications", {"url": url})
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(status)

    return ctx, NotificationService(httpx.MockTransport(handler)), seen


def run(ctx, svc, *events):
    async def go():
        await svc.start(ctx)
        for topic, payload in events:
            await svc.handle(topic, payload)
    asyncio.run(go())


def test_format_detection():
    assert detect_format("https://discord.com/api/webhooks/1/x") == "discord"
    assert detect_format("https://ntfy.sh/abc") == "ntfy"
    assert detect_format("https://example.com/hook") == "generic"
    assert detect_format("https://discord.com/x", "generic") == "generic"
    assert build_request("discord", "T", "B", 0)["json"] == {"content": "**T**\nB"}


def test_generic_webhook_and_list():
    ctx, svc, seen = setup(url="https://example.com/hook")
    run(ctx, svc, ("app.notify", {"title": "Disk space", "body": "Low"}))
    body = json.loads(seen[0].content)
    assert body["title"] == "Disk space" and body["body"] == "Low" and body["source"] == "live-clipper"
    assert svc.recent()["items"][0]["title"] == "Disk space"


def test_ntfy_and_discord_formats():
    ctx, svc, seen = setup()
    run(ctx, svc, ("app.notify", {"title": "Hello", "body": "World"}))
    assert seen[0].content == b"World" and seen[0].headers["Title"] == "Hello"
    ctx, svc, seen = setup(url="https://discord.com/api/webhooks/1/abc")
    run(ctx, svc, ("app.notify", {"title": "Hello", "body": "World"}))
    assert json.loads(seen[0].content) == {"content": "**Hello**\nWorld"}


def test_only_published_deliveries_notify():
    ctx, svc, seen = setup(url="https://example.com/h")
    run(ctx, svc,
        ("publish.delivery", {"delivery_id": "1", "clip_id": 5, "destination": "youtube", "state": "queued"}),
        ("publish.delivery", {"delivery_id": "1", "clip_id": 5, "destination": "youtube",
                              "state": "published", "url": "https://example.com/v/1"}))
    assert len(seen) == 1
    item = svc.recent()["items"][0]
    assert item["kind"] == "published" and "https://example.com/v/1" in item["body"]


def test_no_webhook_when_disabled_or_unset():
    ctx, svc, seen = setup(method="browser")
    run(ctx, svc, ("app.notify", {"title": "a", "body": "b"}))
    assert seen == [] and len(svc.recent()["items"]) == 1


def test_secrets_redacted_and_failures_do_not_leak_url():
    ctx, svc, seen = setup(url="https://example.com/h", status=500)
    run(ctx, svc, ("app.notify", {"title": "t", "body": "login failed password=hunter2 api_key=abc123"}))
    text = svc.recent()["items"][0]["body"]
    assert "hunter2" not in text and "abc123" not in text
    assert "hunter2" not in json.loads(seen[0].content)["body"]
    assert svc.last_webhook == {"ok": False, "at": svc.last_webhook["at"], "error": "HTTP 500"}
    ev = ctx.db.execute("SELECT message FROM events WHERE source='notifications'").fetchall()
    assert ev and "example.com" not in ev[0][0]


def test_transport_error_is_swallowed():
    ctx = make_ctx()
    ctx.settings.set("notify.method", "both")
    ctx.secrets.set("notifications", {"url": URL})

    def boom(req):
        raise httpx.ConnectError(f"cannot reach {req.url}")

    svc = NotificationService(httpx.MockTransport(boom))
    run(ctx, svc, ("app.notify", {"title": "a", "body": "b"}))
    assert svc.last_webhook["ok"] is False and "topic_secret_xyz" not in str(svc.last_webhook)


def test_http_since_and_bus_subscription():
    ctx, svc, _ = setup(method="browser")
    c = client(ctx, svc)
    with c:
        ctx.notify("One", "first")
        ctx.notify("Two", "second")
        got = c.get("/api/notifications").json()
        assert [i["title"] for i in got["items"]] == ["One", "Two"]
        later = c.get(f"/api/notifications?since={got['items'][0]['id']}").json()
        assert [i["title"] for i in later["items"]] == ["Two"]
        assert c.get(f"/api/notifications?since={got['latest']}").json()["items"] == []


def test_diagnostics_redacts_log_and_counts_restarts(monkeypatch):
    ctx = make_ctx()
    (ctx.paths.logs / "live-clipper.log").write_text(
        "line one\nGET /x?access_token=supersecret123 200\n", encoding="utf-8")
    ctx.db.event("warning", "supervisor", "worker exited; restarting", {"reason": "exited"})
    ctx.db.event("info", "other", "worker restarting elsewhere")
    c = client(ctx, DiagnosticsService())
    with c:
        d = c.get("/api/diagnostics").json()
    assert d["restarts"]["count"] == 1 and d["restarts"]["recent"][0]["data"]["reason"] == "exited"
    assert "supersecret123" not in json.dumps(d) and len(d["log_tail"]) == 2
    assert d["disk"]["free_bytes"] > 0 and "found" in d["ffmpeg"] and "diagnostics" in d["services"]
