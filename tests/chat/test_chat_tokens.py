"""Read-only chat token renewal: Twitch public device client + Kick user OAuth. MockTransports only (no provider is
contacted). Covers stored rotation, revoked refresh, no parallel rotation, manual legacy creds, Kick app fallback."""
from __future__ import annotations

import asyncio
import time
from urllib.parse import parse_qs

import httpx
import pytest
from monitor_env import env

from live_clipper.chat_tokens import TokenRefresher, TokenRevoked

TW_TOKEN = "https://id.twitch.test/oauth2/token"
KICK_TOKEN = "https://id.kick.test/oauth/token"


class TokenEndpoint:
    """Fake OAuth refresh endpoint with single-use (rotating) refresh tokens, like Twitch public clients and Kick."""

    def __init__(self, valid=("RT-1",), status=None, delay=0.0):
        self.valid, self.status, self.delay, self.calls, self.n = set(valid), status, delay, [], 0

    async def __call__(self, req: httpx.Request):
        body = {k: v[0] for k, v in parse_qs(req.content.decode()).items()}
        self.calls.append((req.url.host, body))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status:
            return httpx.Response(self.status)
        if body.get("grant_type") != "refresh_token" or body.get("refresh_token") not in self.valid:
            return httpx.Response(400, json={"status": 400, "message": "Invalid refresh token"})
        self.valid.discard(body["refresh_token"])            # single use
        self.n += 1
        new_rt = f"RT-{self.n + 1}"
        self.valid.add(new_rt)
        return httpx.Response(200, json={"access_token": f"AT-{self.n + 1}", "refresh_token": new_rt,
                                         "expires_in": 14400, "scope": ["user:read:chat"], "token_type": "bearer"})


def refresher(sec, ep, **kw):
    return TokenRefresher(sec, transport=httpx.MockTransport(ep), endpoints={"twitch": TW_TOKEN, "kick": KICK_TOKEN},
                          **kw)


def signin(sec, platform, expires_in=60, **extra):
    sec.update("source", platform, {"client_id": "pub-client", "access_token": "AT-1", "refresh_token": "RT-1",
                               "expires_at": str(time.time() + expires_in), "scope": "user:read:chat", **extra})


# ------------------------------------------------------------------ successful stored rotation
def test_twitch_public_client_rotation_is_stored_encrypted(tmp_path):
    s, _, sec = env(tmp_path)
    signin(sec, "twitch", expires_in=60)                     # inside the 5-minute renewal window
    ep = TokenEndpoint()
    tr = refresher(sec, ep)
    creds = asyncio.run(tr.get("twitch"))
    assert creds["access_token"] == "AT-2" and creds["refresh_token"] == "RT-2"
    host, body = ep.calls[0]
    assert host == "id.twitch.test" and body == {"grant_type": "refresh_token", "refresh_token": "RT-1",
                                                  "client_id": "pub-client"}          # public client: no secret
    stored = sec.chat_creds("twitch")
    assert stored["access_token"] == "AT-2" and stored["refresh_token"] == "RT-2" and stored["client_id"] == "pub-client"
    assert float(stored["expires_at"]) > time.time() + 14000
    raw = sec.vault.file.read_bytes()
    assert b"AT-2" not in raw and b"RT-2" not in raw                                    # encrypted at rest
    # fresh token: no further request
    asyncio.run(tr.get("twitch"))
    assert len(ep.calls) == 1 and tr.refreshes == {"twitch": 1}


def test_twitch_adapter_renews_on_401_and_retries(tmp_path):
    from live_clipper.chat.twitch import TwitchChat
    _, _, sec = env(tmp_path)
    signin(sec, "twitch", expires_in=10_000)                 # not expiring: only the 401 triggers renewal
    ep = TokenEndpoint()
    helix = []

    async def h(req: httpx.Request):
        if req.url.host == "id.twitch.test":
            return await ep(req)
        helix.append(req.headers["authorization"])
        if req.headers["authorization"] != "Bearer AT-2":
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [{"id": "42"}]})
    t = httpx.MockTransport(h)
    tr = TokenRefresher(sec, transport=t, endpoints={"twitch": TW_TOKEN})
    ad = TwitchChat("somechan", lambda e: None, sec.chat_creds("twitch"), {"token_source": tr, "transport": t})

    async def go():
        async with httpx.AsyncClient(transport=t) as c:
            return await ad._resolve_ids(c)
    assert asyncio.run(go()) == ("42", "42")
    assert helix[0] == "Bearer AT-1" and helix[1:] == ["Bearer AT-2", "Bearer AT-2"] and len(ep.calls) == 1
    assert sec.chat_creds("twitch")["refresh_token"] == "RT-2"


def test_kick_user_token_renewed_before_expiry_with_client_secret(tmp_path):
    from live_clipper.chat.kick import KickChat
    _, _, sec = env(tmp_path)
    signin(sec, "kick", expires_in=30, client_secret="kick-secret")
    ep = TokenEndpoint()
    seen = []

    async def h(req: httpx.Request):
        if req.url.host == "id.kick.test":
            return await ep(req)
        seen.append((req.method, req.headers["authorization"]))
        if req.url.path.endswith("/channels"):
            return httpx.Response(200, json={"data": [{"broadcaster_user_id": 77}]})
        if req.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": [{"subscription_id": "S1"}]})
    t = httpx.MockTransport(h)
    tr = TokenRefresher(sec, transport=t, endpoints={"kick": KICK_TOKEN})
    ad = KickChat("chan", lambda e: None, sec.chat_creds("kick"),
                  {"token_source": tr, "transport": t, "public_url": "https://x/webhooks/kick",
                   "base_url": "https://api.kick.test/public/v1", "id_base_url": "https://id.kick.test"})
    assert asyncio.run(ad.subscribe()) == ["S1"]
    assert ep.calls[0][1]["client_secret"] == "kick-secret" and ep.calls[0][1]["grant_type"] == "refresh_token"
    assert {a for _, a in seen} == {"Bearer AT-2"} and ad.mode == "user"
    assert sec.chat_creds("kick")["access_token"] == "AT-2"


# ------------------------------------------------------------------ revoked refresh
def test_revoked_twitch_refresh_fails_clearly_and_does_not_hammer(tmp_path):
    from live_clipper.chat.twitch import TwitchChat
    _, _, sec = env(tmp_path)
    signin(sec, "twitch", expires_in=-5)                     # already expired
    ep = TokenEndpoint(valid=())                              # provider revoked RT-1
    tr = refresher(sec, ep)
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError
    ad = TwitchChat("somechan", lambda e: None, sec.chat_creds("twitch"), {"token_source": tr, "sleep": fake_sleep,
                                                                           "transport": httpx.MockTransport(ep)})
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ad.run())
    assert ad.health.state == "unavailable" and "revoked or expired" in ad.health.detail
    assert ad.health.action_needed.startswith("Reconnect Twitch (Sources")
    assert sleeps == [60.0, 60.0, 60.0] and len(ep.calls) == 1        # remembered: no repeated refresh requests
    assert sec.chat_creds("twitch")["refresh_token"] == "RT-1"        # nothing overwritten
    # owner signs in again -> the new refresh token is tried
    sec.update("source", "twitch", {"access_token": "AT-9", "refresh_token": "RT-9", "expires_at": str(time.time() - 1)})
    ep.valid.add("RT-9")
    assert asyncio.run(tr.get("twitch"))["refresh_token"] == "RT-2"


def test_revoked_kick_signin_falls_back_to_app_token_or_fails_clearly(tmp_path):
    from live_clipper.chat.kick import KickChat
    _, _, sec = env(tmp_path)
    ep = TokenEndpoint(valid=())

    async def h(req: httpx.Request):
        if req.url.host == "id.kick.test":
            if parse_qs(req.content.decode()).get("grant_type") == ["client_credentials"]:
                return httpx.Response(200, json={"access_token": "APP", "expires_in": 3600})
            return await ep(req)
        if req.headers["authorization"] != "Bearer APP":
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [{"broadcaster_user_id": 77}]})
    t = httpx.MockTransport(h)
    opts = {"transport": t, "public_url": "https://x/webhooks/kick", "base_url": "https://api.kick.test/public/v1",
            "id_base_url": "https://id.kick.test"}

    async def resolve(ad):
        async with httpx.AsyncClient(transport=t) as c:
            return await ad.resolve_channel_id(c)
    # with app client credentials: app access token takes over (explicit broadcaster id path preserved)
    signin(sec, "kick", expires_in=-1, client_secret="kick-secret")
    ad = KickChat("chan", lambda e: None, sec.chat_creds("kick"),
                  {**opts, "token_source": TokenRefresher(sec, transport=t, endpoints={"kick": KICK_TOKEN})})
    assert asyncio.run(resolve(ad)) == 77 and ad.mode == "app" and "revoked" in ad.user_token_note
    # without client credentials: clear revocation error
    sec.delete("kick")
    sec.update("source", "kick", {"client_id": "c", "access_token": "AT-1", "refresh_token": "RT-1",
                             "expires_at": str(time.time() - 1)})
    ad2 = KickChat("chan", lambda e: None, sec.chat_creds("kick"),
                   {**opts, "token_source": TokenRefresher(sec, transport=t, endpoints={"kick": KICK_TOKEN})})
    with pytest.raises(TokenRevoked) as ei:
        asyncio.run(resolve(ad2))
    assert ei.value.action.startswith("Reconnect Kick")


# ------------------------------------------------------------------ no parallel rotation
def test_parallel_channels_share_one_rotation(tmp_path):
    _, _, sec = env(tmp_path)
    signin(sec, "twitch", expires_in=10_000)
    ep = TokenEndpoint(delay=0.05)
    tr = refresher(sec, ep)

    async def go():
        # five channels got a 401 with the same stale token at once
        return await asyncio.gather(*[tr.get("twitch", force=True, stale_token="AT-1") for _ in range(5)])
    out = asyncio.run(go())
    assert len(ep.calls) == 1 and {c["access_token"] for c in out} == {"AT-2"}
    assert sec.chat_creds("twitch")["refresh_token"] == "RT-2"
    # and pre-expiry renewals from several channels also collapse into one request
    sec.update("source", "twitch", {"expires_at": str(time.time() + 10)})

    async def go2():
        return await asyncio.gather(*[tr.get("twitch") for _ in range(4)])
    assert {c["access_token"] for c in asyncio.run(go2())} == {"AT-3"} and len(ep.calls) == 2


def test_parallel_kick_adapters_rotate_once(tmp_path):
    from live_clipper.chat.kick import KickChat
    _, _, sec = env(tmp_path)
    signin(sec, "kick", expires_in=5, client_secret="kick-secret")
    ep = TokenEndpoint(delay=0.05)

    async def h(req):
        if req.url.host == "id.kick.test":
            return await ep(req)
        return httpx.Response(200, json={"data": [{"broadcaster_user_id": 1}]})
    t = httpx.MockTransport(h)
    tr = TokenRefresher(sec, transport=t, endpoints={"kick": KICK_TOKEN})
    ads = [KickChat(f"c{i}", lambda e: None, sec.chat_creds("kick"),
                    {"token_source": tr, "transport": t, "base_url": "https://api.kick.test/public/v1"}) for i in range(4)]

    async def go():
        async with httpx.AsyncClient(transport=t) as c:
            await asyncio.gather(*[a.resolve_channel_id(c) for a in ads])
    asyncio.run(go())
    assert len(ep.calls) == 1 and all(a.creds["access_token"] == "AT-2" for a in ads)


def test_owner_resignin_during_refresh_wins(tmp_path):
    _, _, sec = env(tmp_path)
    signin(sec, "kick", expires_in=1, client_secret="x")

    async def h(req):
        sec.update("source", "kick", {"access_token": "OWNER-NEW", "refresh_token": "OWNER-RT"})   # callback lands
        return httpx.Response(200, json={"access_token": "AT-2", "refresh_token": "RT-2", "expires_in": 60})
    tr = TokenRefresher(sec, transport=httpx.MockTransport(h), endpoints={"kick": KICK_TOKEN})
    assert asyncio.run(tr.get("kick"))["access_token"] == "OWNER-NEW"
    assert sec.chat_creds("kick")["refresh_token"] == "OWNER-RT"


# ------------------------------------------------------------------ manual legacy creds, transient errors
def test_manual_legacy_creds_are_never_renewed(tmp_path):
    _, _, sec = env(tmp_path)
    sec.update("source", "twitch", {"client_id": "c", "access_token": "MANUAL", "expires_at": str(time.time() - 100)})
    ep = TokenEndpoint()
    tr = refresher(sec, ep)
    assert asyncio.run(tr.get("twitch", force=True, stale_token="MANUAL"))["access_token"] == "MANUAL"
    assert ep.calls == [] and not tr.renewable("twitch")


def test_transient_failure_keeps_valid_token_and_retries_expired(tmp_path):
    _, _, sec = env(tmp_path)
    signin(sec, "twitch", expires_in=120)
    ep = TokenEndpoint(status=503)
    tr = refresher(sec, ep)
    assert asyncio.run(tr.get("twitch"))["access_token"] == "AT-1"           # still valid for 2 min
    sec.update("source", "twitch", {"expires_at": str(time.time() - 1)})
    with pytest.raises(RuntimeError, match="will retry"):
        asyncio.run(tr.get("twitch"))
    ep.status = None
    assert asyncio.run(tr.get("twitch"))["access_token"] == "AT-2"           # not treated as a revocation


def test_engine_shares_one_refresher_across_channels(tmp_path):
    from live_clipper.engine import Engine
    s, store, sec = env(tmp_path)
    eng = Engine(s, store, sec)
    assert isinstance(eng.tokens, TokenRefresher) and eng.tokens.secrets is sec
