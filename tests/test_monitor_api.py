"""Monitor HTTP API (/api/channels*, /api/sources*), with fake capture and MockTransport platforms."""

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from live_clipper import source_status
from live_clipper.capture import Capture
from live_clipper.monitor_service import MonitorService
from live_clipper.paths import get_paths
from live_clipper.server import create_app, ensure_token
from live_clipper.services import build_context

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def platform():
    """Fake Twitch/Kick/Google endpoints; `state` toggles behaviour per test."""
    state = {"twitch_secret": "good", "requests": []}

    def h(req: httpx.Request):
        state["requests"].append(str(req.url))
        body = parse_qs(req.content.decode()) if req.content else {}
        if req.url.host == "id.twitch.tv" and req.url.path.endswith("/device"):
            return httpx.Response(
                200,
                json={
                    "device_code": "dev",
                    "user_code": "ABCD-EFGH",
                    "interval": 0,
                    "verification_uri": "https://www.twitch.tv/activate",
                    "expires_in": 60,
                },
            )
        if req.url.host in ("id.twitch.tv", "id.kick.com"):
            if body.get("grant_type") == ["authorization_code"]:
                assert body["code_verifier"] and body["code"] == ["CODE"]
                return httpx.Response(
                    200, json={"access_token": "KUSER", "refresh_token": "KR", "expires_in": 3600}
                )
            if body.get("client_secret") != [state["twitch_secret"]]:
                return httpx.Response(400, json={"message": "invalid client"})
            return httpx.Response(200, json={"access_token": "APP", "expires_in": 3600})
        if req.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"data": []})

    state["transport"] = httpx.MockTransport(h)
    return state


@pytest.fixture
def client(platform, monkeypatch):
    def start(self):
        self._task = asyncio.get_running_loop().create_future()
        self.health.state = "live"

    async def stop(self):
        self._task = None
        self.health.state = "stopped"

    monkeypatch.setattr(Capture, "start", start)
    monkeypatch.setattr(Capture, "stop", stop)

    async def probe(url, timeout=40):                     # never run the real streamlink probe (network)
        return {"state": "offline", "detail": "fake probe"}
    monkeypatch.setattr(source_status, "streamlink_status", probe)
    paths = get_paths()
    ctx = build_context(paths, port=8765, token=ensure_token(paths, regenerate=True))
    ctx.settings.set("monitor.min_free_mb", 0)
    ctx.settings.set("monitor.topic_observe_seconds", 0)
    svc = MonitorService(transport=platform["transport"])
    ctx.services.append(svc)
    seen = []
    ctx.bus.subscribe("monitor.channel_state", lambda t, p: seen.append(p))
    with TestClient(create_app(ctx), base_url=BASE, headers={"Authorization": f"Bearer {ctx.token}"}) as c:
        c.svc, c.seen, c.ctx = svc, seen, ctx
        yield c


def test_channel_crud_arm_disarm_and_events(client):
    r = client.post(
        "/api/channels",
        json={
            "url": "https://www.twitch.tv/streamer_alpha",
            "watch_mode": "ungated",
            "format_id": "gta-vi",
            "publish_platforms": ["youtube"],
            "enabled": True,
        },
    )
    assert r.status_code == 201, r.text
    ch = r.json()
    assert ch["platform"] == "twitch" and ch["login"] == "streamer_alpha" and ch["enabled"]
    assert ch["state"] == "recording" and ch["status_source"] == "stream probe (no official API connected)"
    assert ch["format_id"] == "gta-vi" and ch["publish_platforms"] == ["youtube"]
    assert client.post("/api/channels", json={"url": "twitch.tv/streamer_alpha"}).status_code == 409
    assert client.post("/api/channels", json={"url": "ftp://example.com/x"}).status_code == 422
    assert (
        client.post(
            "/api/channels", json={"url": "kick.com/streamer_beta", "watch_mode": "bogus"}
        ).status_code
        == 422
    )

    cid = ch["id"]
    upd = client.put(f"/api/channels/{cid}", json={"watch_mode": "topic_gated", "poll_seconds": 5}).json()
    assert upd["watch_mode"] == "topic_gated" and upd["poll_seconds"] == 60  # clamped 60..900
    assert upd["gate_change_pending"] is True  # takes effect on re-arm
    client.post(f"/api/channels/{cid}/disarm")
    assert client.get(f"/api/channels/{cid}").json()["state"] == "disabled"
    rearmed = client.post(f"/api/channels/{cid}/arm").json()
    assert rearmed["gate_change_pending"] is False and rearmed["watch"]["enabled"] is True

    client.svc.publish_states()
    events = client.get(f"/api/channels/{cid}/events").json()
    assert any(e["message"].startswith("monitoring twitch/streamer_alpha") for e in events)
    assert any(e["message"].startswith("state ") for e in events)
    assert (
        client.seen and client.seen[-1]["channel_id"] == cid and client.seen[-1]["login"] == "streamer_alpha"
    )
    assert len(client.get("/api/channels").json()) == 1
    assert client.delete(f"/api/channels/{cid}").status_code == 204
    assert client.get(f"/api/channels/{cid}").status_code == 404


def test_refused_channel_reports_reason(client):
    store = client.svc.store
    cid = store.add_channel("custom", "srt://h:1", "srt://h:1", {"ingest_mode": "vod_only"}, url="srt://h:1")
    r = client.post(f"/api/channels/{cid}/arm")
    assert r.status_code == 409 and "VOD-only" in r.json()["detail"]


def test_sources_separate_live_status_chat_and_capture(client, platform):
    srcs = {s["platform"]: s for s in client.get("/api/sources").json()}
    assert set(srcs) == {"twitch", "kick", "youtube"}
    for s in srcs.values():
        assert not s["connected"] and s["live_status"]["how"] == "stream probe (no official API connected)"
        assert "does not grant the right to record or redistribute" in s["capture"]["note"]
    assert srcs["kick"]["chat"]["available"] is False and "public HTTPS URL" in srcs["kick"]["chat"]["detail"]
    assert srcs["kick"]["redirect_uri"] == "http://localhost:8765/api/oauth/source/kick/callback"
    assert srcs["youtube"]["redirect_uri"] == "http://127.0.0.1:8765/api/oauth/source/youtube/callback"

    platform["twitch_secret"] = "good"
    bad = client.post("/api/sources/twitch/connect", json={"client_id": "cid", "client_secret": "wrong"})
    assert bad.status_code == 400 and "Sources > Twitch" in bad.json()["detail"]["fix"]
    ok = client.post("/api/sources/twitch/connect", json={"client_id": "cid", "client_secret": "good"}).json()
    assert ok["connected"] and ok["live_status"]["how"] == "Twitch Helix Get Streams"
    assert "good" not in str(ok) and "client_secret" in ok["fields"]
    ch = client.post("/api/channels", json={"url": "twitch.tv/streamer_alpha"}).json()
    assert ch["status_source"] == "Twitch Helix Get Streams"
    assert client.delete("/api/sources/twitch").status_code == 204
    assert client.get(f"/api/channels/{ch['id']}").json()["status_source"].startswith("stream probe")

    yt = client.post("/api/sources/youtube/connect", json={"api_key": "KEY"}).json()
    assert yt["connected"] and "5 quota units" in yt["chat"]["detail"]


def test_kick_oauth_pkce_callback_and_webhook_url(client):
    r = client.post(
        "/api/sources/kick/connect", json={"method": "oauth", "client_id": "kc", "client_secret": "ks"}
    )
    q = parse_qs(urlparse(r.json()["authorize_url"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"][0].startswith(
        "http://localhost:8765/"
    )
    assert (
        client.get("/api/oauth/source/kick/callback", params={"state": "forged", "code": "CODE"}).status_code
        == 400
    )
    ok = client.get("/api/oauth/source/kick/callback", params={"state": q["state"][0], "code": "CODE"})
    assert ok.status_code == 200 and "connected" in ok.text
    assert (
        client.get(
            "/api/oauth/source/kick/callback", params={"state": q["state"][0], "code": "CODE"}
        ).status_code
        == 400
    )  # state is single-use
    assert client.svc.secrets.chat_creds("kick")["access_token"] == "KUSER"
    assert (
        client.post("/api/sources/kick/connect", json={"webhook_public_url": "http://plain"}).status_code
        == 422
    )
    k = client.post(
        "/api/sources/kick/connect", json={"webhook_public_url": "https://hooks.example.com/webhooks/kick"}
    )
    assert k.status_code == 200 and k.json()["chat"]["available"] is True


def test_twitch_device_sign_in(client):
    r = client.post("/api/sources/twitch/connect", json={"method": "device", "client_id": "pub"}).json()
    assert r["user_code"] == "ABCD-EFGH" and r["verification_uri"] == "https://www.twitch.tv/activate"
    pend = {s["platform"]: s for s in client.get("/api/sources").json()}["twitch"]
    assert pend["pending"]["user_code"] == "ABCD-EFGH"
