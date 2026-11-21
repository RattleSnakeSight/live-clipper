"""Official live-status clients and the batching hub (httpx.MockTransport only; no platform is contacted)."""

import asyncio
import json
from urllib.parse import parse_qs

import httpx
import pytest

from live_clipper.secrets import Vault
from live_clipper.sources import (
    RateLimited,
    SourceAuthError,
    SourceSecrets,
    StatusHub,
    describe,
    kick,
    twitch,
    youtube,
)


def run(coro):
    return asyncio.run(coro)


async def fetch(client, handler, creds, logins):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        return await client.fetch(http, creds, logins)


# ------------------------------------------------------------------ Twitch
def test_twitch_batches_100_logins_per_call_with_app_token():
    calls, saved = [], []

    def h(req):
        if req.url.host == "id.twitch.tv":
            assert parse_qs(req.content.decode())["grant_type"] == ["client_credentials"]
            return httpx.Response(200, json={"access_token": "APP", "expires_in": 3600})
        assert req.headers["client-id"] == "cid" and req.headers["authorization"] == "Bearer APP"
        logins = req.url.params.get_list("user_login")
        calls.append(logins)
        live = [
            {
                "user_login": lg,
                "type": "live",
                "title": "GTA 6 day",
                "game_name": "Grand Theft Auto VI",
                "id": f"b-{lg}",
            }
            for lg in logins
            if lg.endswith("7")
        ]
        return httpx.Response(200, json={"data": live})

    logins = [f"streamer_{i}" for i in range(150)]
    out = run(fetch(twitch.Client(saved.append), h, {"client_id": "cid", "client_secret": "sec"}, logins))
    assert [len(c) for c in calls] == [100, 50]
    assert out["streamer_7"] == {
        "state": "live",
        "title": "GTA 6 day",
        "category": "Grand Theft Auto VI",
        "id": "b-streamer_7",
        "detail": "",
    }
    assert out["streamer_1"]["state"] == "offline"
    assert saved and saved[0]["app_access_token"] == "APP"


def test_twitch_renews_expired_app_token_once_then_reports_exact_fix():
    tokens = iter(["OLD", "NEW"])

    def h(req):
        if req.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        return (
            httpx.Response(200, json={"data": []})
            if req.headers["authorization"] == "Bearer NEW"
            else httpx.Response(401)
        )

    out = run(
        fetch(twitch.Client(lambda p: None), h, {"client_id": "c", "client_secret": "s"}, ["streamer_alpha"])
    )
    assert out["streamer_alpha"]["state"] == "offline"

    def refused(req):
        return httpx.Response(400, json={"message": "invalid client secret"})

    with pytest.raises(SourceAuthError) as ei:
        run(fetch(twitch.Client(lambda p: None), refused, {"client_id": "c", "client_secret": "bad"}, ["x"]))
    assert "Client Secret" in ei.value.fix and "Sources > Twitch" in ei.value.fix


def test_twitch_rate_limit_honours_reset_header():
    def h(req):
        return httpx.Response(429, headers={"ratelimit-reset": "9999999999"})

    with pytest.raises(RateLimited) as ei:
        run(
            fetch(
                twitch.Client(lambda p: None),
                h,
                {"client_id": "c", "access_token": "user"},
                ["streamer_beta"],
            )
        )
    assert ei.value.retry_after > 60


def test_twitch_device_flow_pending_then_token():
    answers = iter(
        [
            httpx.Response(400, json={"message": "authorization_pending"}),
            httpx.Response(
                200,
                json={
                    "access_token": "U",
                    "refresh_token": "R",
                    "expires_in": 14400,
                    "scope": ["user:read:chat"],
                },
            ),
        ]
    )

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(answers))) as http:
            return [
                await twitch.device_poll(http, "cid", "dev"),
                await twitch.device_poll(http, "cid", "dev"),
            ]

    first, second = run(go())
    assert first is None and second["access_token"] == "U" and second["scope"] == "user:read:chat"


# ------------------------------------------------------------------ Kick
def test_kick_resolves_slugs_once_and_batches_50_ids():
    calls = {"channels": 0, "livestreams": []}

    def h(req):
        if req.url.host == "id.kick.com":
            return httpx.Response(200, json={"access_token": "APP", "expires_in": 3600})
        if req.url.path.endswith("/channels"):
            calls["channels"] += 1
            slug = req.url.params["slug"]
            if slug == "missing_one":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(
                200, json={"data": [{"broadcaster_user_id": int(slug.split("_")[1]), "slug": slug}]}
            )
        ids = req.url.params.get_list("broadcaster_user_id")
        calls["livestreams"].append(len(ids))
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "broadcaster_user_id": 3,
                        "stream_title": "GTA VI heists",
                        "category": {"name": "Grand Theft Auto VI"},
                        "started_at": "2026-11-20T10:00:00Z",
                    }
                ]
            },
        )

    client = kick.Client(lambda p: None)
    logins = [f"s_{i}" for i in range(60)] + ["missing_one"]
    creds = {"client_id": "c", "client_secret": "s"}
    out = run(fetch(client, h, creds, logins))
    assert calls["channels"] == 61 and calls["livestreams"] == [50, 10]
    assert out["s_3"]["state"] == "live" and out["s_3"]["title"] == "GTA VI heists"
    assert out["s_4"]["state"] == "offline"
    assert out["missing_one"]["state"] == "error" and out["missing_one"]["action_needed"]
    run(fetch(client, h, creds, logins[:60]))
    assert calls["channels"] == 61  # ids are cached


# ------------------------------------------------------------------ YouTube
def test_youtube_quota_aware_search_then_videos_follow():
    now = [1_000_000.0]
    calls = []

    def h(req):
        calls.append(req.url.path.rsplit("/", 1)[1])
        assert req.url.params["key"] == "KEY"
        if req.url.path.endswith("/channels"):
            return httpx.Response(200, json={"items": [{"id": "UC" + "a" * 22}]})
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json={"items": [{"id": {"videoId": "vid00000001"}}]})
        ended = now[0] > 1_000_500
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "vid00000001",
                        "snippet": {"title": "GTA 6 stream"},
                        "liveStreamingDetails": {
                            "actualStartTime": "x",
                            **({"actualEndTime": "y"} if ended else {}),
                        },
                    }
                ]
            },
        )

    c = youtube.Client(lambda p: None, clock=lambda: now[0])
    out = run(fetch(c, h, {"api_key": "KEY"}, ["@streamer_gamma"]))
    assert out["@streamer_gamma"]["state"] == "live" and out["@streamer_gamma"]["title"] == "GTA 6 stream"
    assert calls == ["channels", "search", "videos"]
    now[0] += 600
    out = run(fetch(c, h, {"api_key": "KEY"}, ["@streamer_gamma"]))
    assert out["@streamer_gamma"]["state"] == "offline" and calls[-1] == "videos"
    calls.clear()
    now[0] += 60  # search spacing for one channel: 864 s (100 a day)
    assert run(fetch(c, h, {"api_key": "KEY"}, ["@streamer_gamma"])) == {} and calls == []
    assert c.units == 1 + 100 + 1 + 1


def test_youtube_invalid_key_and_quota():
    def bad(req):
        return httpx.Response(400, json={"error": {"errors": [{"reason": "keyInvalid"}]}})

    with pytest.raises(SourceAuthError) as ei:
        run(fetch(youtube.Client(lambda p: None), bad, {"api_key": "nope"}, ["@streamer_alpha"]))
    assert "API key" in ei.value.fix

    def quota(req):
        return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})

    with pytest.raises(RateLimited):
        run(fetch(youtube.Client(lambda p: None), quota, {"api_key": "k"}, ["@streamer_alpha"]))


# ------------------------------------------------------------------ hub
def _secrets(tmp_path, **platforms):
    sec = SourceSecrets(Vault(tmp_path / "secrets"))
    for p, creds in platforms.items():
        sec.update("source", p, creds)
    return sec


def test_hub_polls_all_watched_logins_in_one_batch_and_wakes_waiters(tmp_path):
    seen = []

    def h(req):
        if req.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "APP", "expires_in": 3600})
        seen.append(sorted(req.url.params.get_list("user_login")))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"user_login": "streamer_alpha", "type": "live", "title": "t", "game_name": "", "id": "1"}
                ]
            },
        )

    sec = _secrets(tmp_path, twitch={"client_id": "c", "client_secret": "s"})
    hub = StatusHub(sec, transport=httpx.MockTransport(h), intervals={"twitch": 3600})

    async def go():
        a, b = await asyncio.gather(hub.next("twitch", "streamer_alpha"), hub.next("twitch", "streamer_beta"))
        cur = await hub.current("twitch", "streamer_alpha")  # fresh: no new request
        await hub.close()
        return a, b, cur

    a, b, cur = run(go())
    assert a["state"] == "live" and a["source"] == "official" and b["state"] == "offline"
    assert cur["checked_at"] == a["checked_at"]
    assert len(seen) <= 2 and seen[-1] == ["streamer_alpha", "streamer_beta"]
    assert hub.connected("twitch") and not hub.connected("kick")
    assert sec.chat_creds("twitch")["app_access_token"] == "APP"  # token persisted in the vault entry


def test_hub_rejected_credentials_become_needs_action(tmp_path):
    sec = _secrets(tmp_path, kick={"client_id": "c", "client_secret": "wrong"})
    hub = StatusHub(sec, transport=httpx.MockTransport(lambda r: httpx.Response(401)))

    async def go():
        st = await hub.next("kick", "streamer_beta")
        await hub.close()
        return st

    st = run(go())
    assert st["state"] == "error" and "Sources > Kick" in st["action_needed"]
    assert hub.problem["kick"]["fix"] == st["action_needed"]
    d = describe("kick", sec.chat_creds("kick"), type("S", (), {"kick_webhook_url": ""})(), hub)
    assert (
        d["live_status"]["fix"]
        and d["chat"]["available"] is False
        and "public HTTPS URL" in d["chat"]["detail"]
    )
    assert "does not grant the right to record" in d["capture"]["note"]
    assert "wrong" not in json.dumps(d)  # field names only, never values


def test_describe_labels_stream_probe_without_official_api():
    d = describe("twitch", {}, None)
    assert d["live_status"] == {
        "available": False,
        "how": "stream probe (no official API connected)",
        "detail": "",
        "fix": "",
        "auth": "",
    }
    assert d["chat"]["how"].startswith("anonymous read-only IRC")
