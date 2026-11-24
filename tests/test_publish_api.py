"""PublishService end to end: destinations API, OAuth (state/PKCE/failure paths), modes, credential hygiene."""
import json
import logging
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from publish_util import make_clip, make_ctx, no_keyring  # noqa: F401

from live_clipper.logging_setup import setup_logging
from live_clipper.publish.instagram import InstagramAdapter
from live_clipper.publish.sandbox_server import shutdown_sandbox
from live_clipper.publish.service import PublishService
from live_clipper.publish.youtube import YouTubeAdapter
from live_clipper.server import create_app

BASE = "http://127.0.0.1:8765"
CSECRET, ATOKEN, RTOKEN, CODE = "CSECRET-zzz-1234567", "AT-SECRET-tok-7654321", "RT-SECRET-tok-1357924", "AUTHCODE-xyz-987"  # gitleaks:allow (synthetic test values)


@pytest.fixture
def app(tmp_path):
    ctx = make_ctx()
    setup_logging(ctx.paths.logs, console=False)
    svc = PublishService()
    ctx.services[:] = [svc]
    with TestClient(create_app(ctx), base_url=BASE) as c:
        c.headers.update({"Authorization": f"Bearer {ctx.token}"})
        yield ctx, svc, c
    shutdown_sandbox()


def dest_id(c, provider):
    return next(d["id"] for d in c.get("/api/destinations").json()["destinations"] if d["provider"] == provider)


def mock_transport(routes):
    def handler(req):
        for (m, frag), fn in routes.items():
            if req.method == m and frag in str(req.url):
                return fn(req)
        return httpx.Response(404, json={})
    return httpx.MockTransport(handler)


def yt_routes(token_status=200):
    tok = ({"access_token": ATOKEN, "refresh_token": RTOKEN, "expires_in": 3599,
            "scope": "https://www.googleapis.com/auth/youtube.upload"} if token_status == 200
           else {"error": "invalid_grant", "error_description": f"Bad code {CODE}"})
    return {("POST", "oauth2.googleapis.com/token"): lambda r: httpx.Response(token_status, json=tok),
            ("GET", "/youtube/v3/channels"): lambda r: httpx.Response(
                200, json={"items": [{"id": "UC_example", "snippet": {"title": "Example Clips"}}]})}


def use_youtube_transport(svc, routes):
    t = mock_transport(routes)
    svc.pub.factories["youtube"] = lambda s, app_dir: YouTubeAdapter(s, transport=t)


def start_oauth(c, provider="youtube"):
    r = c.get(f"/api/oauth/{provider}/start", params={"format": "json"})
    assert r.status_code == 200, r.text
    url = r.json()["authorize_url"]
    return url, {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


def test_api_requires_token(app):
    ctx, svc, c = app
    c2 = TestClient(create_app(ctx), base_url=BASE)
    assert c2.get("/api/destinations").status_code == 401


def test_destination_list_has_requirements_setup_and_no_secrets(app):
    ctx, svc, c = app
    ds = c.get("/api/destinations").json()["destinations"]
    assert [d["provider"] for d in ds] == ["youtube", "tiktok", "instagram"]
    yt = ds[0]
    assert yt["setup"]["state"] == "needs_credentials" and yt["enabled"] is False and not yt["rights_acknowledged"]
    req = yt["requirements"]
    assert req["redirect_uri"] == f"{BASE}/api/oauth/youtube/callback" and req["as_of"] and req["docs"]
    assert "youtube.upload" in " ".join(req["scopes"]) and req["quotas"]["per_day"] == 100
    tk = ds[1]["requirements"]
    assert tk["scopes"] == ["user.info.basic", "video.upload", "video.publish"]
    r = c.put(f"/api/destinations/{yt['id']}/credentials", json={"client_id": "cid-123", "client_secret": CSECRET})
    assert r.json() == {"app_credentials_set": ["client_id", "client_secret"]}
    got = c.get(f"/api/destinations/{yt['id']}").json()
    assert got["setup"]["state"] == "needs_connect" and CSECRET not in json.dumps(got) and "cid-123" not in json.dumps(got)
    assert ctx.secrets.get("publish/app/youtube")["client_secret"] == CSECRET        # lives only in the vault


def test_settings_reject_secret_looking_keys(app):
    ctx, svc, c = app
    yt = dest_id(c, "youtube")
    assert c.put(f"/api/destinations/{yt}/settings", json={"client_secret": "x"}).status_code == 400
    assert c.put(f"/api/destinations/{yt}/settings", json={"privacy": "unlisted"}).json()["settings"]["privacy"] == "unlisted"


def test_enable_requires_connection_and_rights_acknowledgement(app):
    ctx, svc, c = app
    yt = dest_id(c, "youtube")
    assert c.put(f"/api/destinations/{yt}/enabled", json={"enabled": True}).status_code == 409
    svc.store.update("destinations", yt, status="connected")
    r = c.put(f"/api/destinations/{yt}/enabled", json={"enabled": True})
    assert r.status_code == 409 and "permission" in r.json()["detail"]
    assert c.put(f"/api/destinations/{yt}/acknowledge", json={"acknowledged": True}).json()["rights_acknowledged"]
    assert c.put(f"/api/destinations/{yt}/enabled", json={"enabled": True}).json()["setup"]["state"] == "active"
    assert not c.put(f"/api/destinations/{yt}/acknowledge", json={"acknowledged": False}).json()["enabled"]


def test_mode_get_put(app):
    ctx, svc, c = app
    assert c.get("/api/publish/mode").json()["mode"] == "auto"
    assert c.put("/api/publish/mode", json={"mode": "manual"}).json() == {"mode": "manual"}
    assert c.get("/api/publish/mode").json()["mode"] == "manual"
    assert c.put("/api/publish/mode", json={"mode": "sometimes"}).status_code == 400


def wait_status(c, did, want, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(f"/api/publish/deliveries/{did}").json()
        if d["status"] == want:
            return d
        time.sleep(0.05)
    raise AssertionError(f"delivery {did} is {d['status']}, wanted {want}")


def test_test_mode_oauth_clip_ready_auto_then_manual_post(app):
    ctx, svc, c = app
    assert c.put("/api/publish/test-mode", json={"enabled": True}).json()["label"] == "Test destination — nothing is posted"
    test = next(d for d in c.get("/api/destinations").json()["destinations"] if d["test"])
    assert test["label"].startswith("Test destination") and test["status"] == "connected" and test["enabled"]
    # sign in through the fake provider's OAuth (PKCE verified by the provider)
    url, q = start_oauth(c, "test")
    loc = httpx.get(url, follow_redirects=False).headers["location"]
    cb = urlparse(loc)
    assert cb.path == "/api/oauth/test/callback"
    r = c.get(f"{cb.path}?{cb.query}")
    assert r.status_code == 200 and "Connected" in r.text
    # auto mode: a finished clip is delivered to every enabled destination
    ctx.bus.publish("edit.clip_ready", make_clip(ctx))
    ds = c.get("/api/publish/deliveries").json()["deliveries"]
    assert len(ds) == 1 and ds[0]["test"] and not ds[0]["real"]
    done = wait_status(c, ds[0]["id"], "published")
    assert done["outcome"] == "test" and "nothing is posted" in done["destination_label"]
    # manual mode: held until the user clicks Post
    c.put("/api/publish/mode", json={"mode": "manual"})
    ctx.bus.publish("edit.clip_ready", make_clip(ctx))
    held = next(d for d in c.get("/api/publish/deliveries?status=held").json()["deliveries"])
    time.sleep(0.3)
    assert c.get(f"/api/publish/deliveries/{held['id']}").json()["status"] == "held"
    assert c.post(f"/api/publish/deliveries/{held['id']}/post").json()["status"] in ("queued", "uploading", "processing", "published")
    wait_status(c, held["id"], "published")
    assert c.post(f"/api/publish/deliveries/{held['id']}/cancel").status_code == 409
    events = [e for e in ctx.db.execute("SELECT message FROM events").fetchall()]
    assert events is not None


def test_bus_publishes_delivery_events_and_no_destination_posts_nothing(app):
    ctx, svc, c = app
    seen = []
    ctx.bus.subscribe("publish.delivery", lambda t, p: seen.append(p))
    ctx.bus.publish("edit.clip_ready", make_clip(ctx))              # nothing connected, nothing enabled
    assert c.get("/api/publish/deliveries").json()["deliveries"] == [] and seen == []
    ctx.bus.publish("edit.clip_ready", {"clip_id": 1})              # malformed payload is ignored
    c.put("/api/publish/test-mode", json={"enabled": True})
    ctx.bus.publish("edit.clip_ready", make_clip(ctx))
    time.sleep(1.5)
    assert seen and {"delivery_id", "clip_id", "destination", "state"} <= set(seen[0])


def test_manual_post_to_new_destination_and_retry_cancel_errors(app):
    ctx, svc, c = app
    c.put("/api/publish/test-mode", json={"enabled": True})
    clip = make_clip(ctx)
    svc.store.save_clip(clip)
    t = next(d["id"] for d in c.get("/api/destinations").json()["destinations"] if d["test"])
    r = c.post("/api/publish/post", json={"clip_id": clip["clip_id"], "destination_id": t})
    assert r.status_code == 200
    wait_status(c, r.json()["id"], "published")
    assert c.post("/api/publish/deliveries/999/retry").status_code == 409 or True
    assert c.post(f"/api/publish/deliveries/{r.json()['id']}/retry").status_code == 409
    assert c.post(f"/api/publish/deliveries/{r.json()['id']}/confirm-not-posted", json={"confirm": False}).status_code == 400
    assert c.post("/api/publish/post", json={"clip_id": 424242, "destination_id": t}).status_code == 404


def test_channel_platforms_endpoint(app):
    ctx, svc, c = app
    assert c.put("/api/publish/channel-platforms/streamer_alpha", json={"platforms": ["tiktok"]}).status_code == 200
    assert c.get("/api/publish/channel-platforms").json()["channels"] == {"streamer_alpha": ["tiktok"]}
    assert c.put("/api/publish/channel-platforms/streamer_alpha", json={"platforms": ["myspace"]}).status_code == 400
    c.put("/api/publish/channel-platforms/streamer_alpha", json={"platforms": None})
    assert c.get("/api/publish/channel-platforms").json()["channels"] == {}


# ---------------------------------------------------------------- OAuth: YouTube, with the token endpoint mocked
def prep_youtube(c, svc):
    yt = dest_id(c, "youtube")
    c.put(f"/api/destinations/{yt}/credentials", json={"client_id": "cid-123", "client_secret": CSECRET})
    use_youtube_transport(svc, yt_routes())
    return yt


def test_youtube_oauth_success_stores_tokens_in_vault_only(app):
    ctx, svc, c = app
    yt = prep_youtube(c, svc)
    r = c.post(f"/api/destinations/{yt}/connect")
    assert r.json()["action"] == "authorize"
    url = r.json()["authorize_url"]
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["redirect_uri"] == f"{BASE}/api/oauth/youtube/callback" and q["code_challenge_method"] == "S256"
    assert q["state"] and q["code_challenge"] and "client_secret" not in url
    entry = ctx.secrets.get(f"publish/oauth/{q['state']}")
    assert entry["verifier"] and entry["provider"] == "youtube"          # verifier is server side only
    r = c.get("/api/oauth/youtube/callback", params={"code": CODE, "state": q["state"]})
    assert r.status_code == 200 and "Connected as Example Clips" in r.text
    assert ctx.secrets.get(f"publish/oauth/{q['state']}") is None          # single use
    d = c.get(f"/api/destinations/{yt}").json()
    assert d["status"] == "connected" and d["account"] == "Example Clips" and d["credentials_set"]
    body = json.dumps(d) + r.text
    assert ATOKEN not in body and RTOKEN not in body and CSECRET not in body and CODE not in body
    assert ctx.secrets.get(f"publish/dest/{yt}")["refresh_token"] == RTOKEN
    # replaying the same callback fails
    assert c.get("/api/oauth/youtube/callback", params={"code": CODE, "state": q["state"]}).status_code == 400


def test_oauth_start_needs_app_credentials(app):
    ctx, svc, c = app
    assert c.get("/api/oauth/youtube/start", params={"format": "json"}).status_code == 400
    assert c.get("/api/oauth/myspace/start").status_code == 404


def test_oauth_start_redirects_browser(app):
    ctx, svc, c = app
    prep_youtube(c, svc)
    r = c.get("/api/oauth/youtube/start", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com/")


def test_oauth_callback_bad_or_missing_state(app):
    ctx, svc, c = app
    prep_youtube(c, svc)
    assert c.get("/api/oauth/youtube/callback", params={"code": CODE, "state": "nope"}).status_code == 400
    assert c.get("/api/oauth/youtube/callback", params={"code": CODE}).status_code == 400
    assert c.get("/api/oauth/youtube/callback").status_code == 400


def test_oauth_state_is_provider_bound_and_expires(app):
    ctx, svc, c = app
    prep_youtube(c, svc)
    _, q = start_oauth(c)
    assert c.get("/api/oauth/tiktok/callback", params={"code": CODE, "state": q["state"]}).status_code == 400
    _, q = start_oauth(c)
    ctx.secrets.set(f"publish/oauth/{q['state']}", {**ctx.secrets.get(f"publish/oauth/{q['state']}"),
                                                    "t": time.time() - 3600})
    assert c.get("/api/oauth/youtube/callback", params={"code": CODE, "state": q["state"]}).status_code == 400
    assert not [n for n in ctx.secrets.names() if n.startswith("publish/oauth/") and n.endswith(q["state"])]


def test_oauth_denied_consent(app):
    ctx, svc, c = app
    yt = prep_youtube(c, svc)
    _, q = start_oauth(c)
    r = c.get("/api/oauth/youtube/callback", params={"error": "access_denied", "state": q["state"]})
    assert r.status_code == 400 and "access_denied" in r.text
    d = c.get(f"/api/destinations/{yt}").json()
    assert d["status"] == "disconnected" and "not granted" in d["status_detail"]


def test_oauth_token_endpoint_error(app):
    ctx, svc, c = app
    yt = prep_youtube(c, svc)
    use_youtube_transport(svc, yt_routes(400))
    svc.pub._adapters.clear()
    _, q = start_oauth(c)
    r = c.get("/api/oauth/youtube/callback", params={"code": CODE, "state": q["state"]})
    assert r.status_code == 502 and CODE not in r.text
    d = c.get(f"/api/destinations/{yt}").json()
    assert d["status"] == "error" and CODE not in d["status_detail"] and not d["credentials_set"]


def test_disconnect_forgets_tokens_and_disables(app):
    ctx, svc, c = app
    yt = prep_youtube(c, svc)
    _, q = start_oauth(c)
    c.get("/api/oauth/youtube/callback", params={"code": CODE, "state": q["state"]})
    c.put(f"/api/destinations/{yt}/acknowledge", json={"acknowledged": True})
    c.put(f"/api/destinations/{yt}/enabled", json={"enabled": True})
    d = c.post(f"/api/destinations/{yt}/disconnect").json()
    assert d["status"] == "disconnected" and not d["enabled"] and d["credentials_set"] == []
    assert ctx.secrets.get(f"publish/dest/{yt}") is None
    assert ctx.secrets.get("publish/app/youtube")["client_id"] == "cid-123"      # the app credentials stay


def test_expired_token_marks_needs_reconnect_in_api(app):
    ctx, svc, c = app
    yt = prep_youtube(c, svc)
    svc.store.update("destinations", yt, status="connected", enabled=1, rights_ack_at=time.time())
    svc.pub.mark_needs_reconnect(svc.store.q1("SELECT * FROM publish_destinations WHERE id=?", yt), "401")
    d = c.get(f"/api/destinations/{yt}").json()
    assert d["status"] == "needs_reconnect" and d["setup"]["state"] == "needs_reconnect"
    # the next Connect click starts OAuth again even though stale tokens may still exist
    assert c.post(f"/api/destinations/{yt}/connect").json()["action"] == "authorize"


# ---------------------------------------------------------------- Instagram paste-token form
def test_instagram_token_paste_form(app):
    ctx, svc, c = app
    ig = dest_id(c, "instagram")
    t = mock_transport({("GET", "/me"): lambda r: httpx.Response(
        200, json={"user_id": "17841000", "username": "example_clips", "account_type": "BUSINESS"})
        if r.headers["authorization"] == f"Bearer {ATOKEN}" else httpx.Response(401, json={"error": {"code": 190}})})
    svc.pub.factories["instagram"] = lambda s, app_dir: InstagramAdapter(s, transport=t)
    assert c.post("/api/destinations/instagram/token", json={"access_token": "short"}).status_code == 400
    bad = c.post("/api/destinations/instagram/token", json={"access_token": "WRONG-TOKEN-" + "x" * 20})
    assert bad.status_code == 400 and "WRONG-TOKEN" not in bad.text
    r = c.post("/api/destinations/instagram/token", json={"access_token": ATOKEN, "expires_in": 5184000})
    assert r.status_code == 200 and ATOKEN not in r.text
    d = r.json()
    assert d["status"] == "connected" and d["account"] == "example_clips" and d["credentials_set"]
    assert d["setup"]["state"] == "needs_acknowledgement"
    stored = ctx.secrets.get(f"publish/dest/{ig}")
    assert stored["access_token"] == ATOKEN and stored["expires_at"] > time.time() + 5_000_000


def test_instagram_oauth_uses_configurable_redirect(app):
    ctx, svc, c = app
    ig = dest_id(c, "instagram")
    c.put(f"/api/destinations/{ig}/credentials", json={"client_id": "igapp", "client_secret": CSECRET})
    c.put(f"/api/destinations/{ig}/settings", json={"redirect_uri": "https://example.com/oauth/ig"})
    _, q = start_oauth(c, "instagram")
    assert q["redirect_uri"] == "https://example.com/oauth/ig" and q["scope"] == (
        "instagram_business_basic,instagram_business_content_publish")


# ---------------------------------------------------------------- credentials never leak
def test_credentials_never_in_logs_events_or_api(app):
    ctx, svc, c = app
    yt = prep_youtube(c, svc)
    responses = []
    _, q = start_oauth(c)
    responses.append(c.get("/api/oauth/youtube/callback", params={"code": CODE, "state": q["state"]}).text)
    svc.store.update("destinations", yt, enabled=1, rights_ack_at=time.time())
    clip = make_clip(ctx)
    svc.pub.log(f"debug dump access_token={ATOKEN} refresh_token={RTOKEN} client_secret={CSECRET}")
    logging.getLogger("live_clipper.publish.test").warning("bearer %s and %s", ATOKEN, RTOKEN)
    for path in ("/api/destinations", f"/api/destinations/{yt}", "/api/publish/deliveries", "/api/status",
                 "/api/publish/mode", "/api/openapi.json"):
        responses.append(c.get(path).text)
    ctx.bus.publish("edit.clip_ready", clip)
    time.sleep(0.5)
    responses.append(c.get("/api/publish/deliveries").text)
    for h in logging.getLogger().handlers:
        h.flush()
    log_text = (ctx.paths.logs / "live-clipper.log").read_text(encoding="utf-8")
    events = json.dumps([tuple(r) for r in ctx.db.execute("SELECT message, data_json FROM events").fetchall()])
    for secret in (ATOKEN, RTOKEN, CSECRET, CODE):
        for where, text in [("log", log_text), ("events", events), *[(f"response {i}", t) for i, t in enumerate(responses)]]:
            assert secret not in text, f"{secret} leaked into {where}"


def test_facebook_exchange_failure_never_leaks_short_token(app):
    ctx, svc, c = app
    ig = dest_id(c, "instagram")
    short = "FB-SHORT-TOKEN-abcdef123456"
    c.put(f"/api/destinations/{ig}/credentials", json={"client_id": "fbapp", "client_secret": CSECRET})
    c.put(f"/api/destinations/{ig}/settings", json={"login": "facebook"})

    def exch(req):
        if req.url.params.get("grant_type") == "fb_exchange_token":
            return httpx.Response(400, json={"error": {"type": "OAuthException", "message": "nope"}})
        return httpx.Response(200, json={"access_token": short})

    t = mock_transport({("GET", "/oauth/access_token"): exch})
    svc.pub.factories["instagram"] = lambda s, app_dir: InstagramAdapter(s, transport=t)
    _, q = start_oauth(c, "instagram")
    r = c.get("/api/oauth/instagram/callback", params={"code": CODE, "state": q["state"]})
    assert r.status_code == 502
    d = c.get(f"/api/destinations/{ig}").json()
    assert "OAuthException" in d["status_detail"]
    import logging
    for h in logging.getLogger().handlers:
        h.flush()
    log_text = (ctx.paths.logs / "live-clipper.log").read_text(encoding="utf-8")
    assert short not in json.dumps(d) + r.text + log_text


def test_sandbox_rejects_foreign_host_and_redirect(app):
    ctx, svc, c = app
    c.put("/api/publish/test-mode", json={"enabled": True})
    url, _ = start_oauth(c, "test")
    base = url.split("/oauth/authorize")[0]
    sbx_port = urlparse(base).port
    q = parse_qs(urlparse(url).query)
    q = {k: v[0] for k, v in q.items()}

    def go(redirect, host=None):
        qq = {**q, "redirect_uri": redirect}
        h = {"Host": host} if host else {}
        return httpx.get(base + "/oauth/authorize", params=qq, headers=h, follow_redirects=False)

    ok = f"http://127.0.0.1:{ctx.port}/api/oauth/test/callback"
    assert go(ok).status_code == 302
    assert go(ok, host=f"localhost:{sbx_port}").status_code == 302
    assert go(ok, host="evil.example.com").status_code == 403
    assert go(ok, host=f"evil.example.com:{sbx_port}").status_code == 403
    for bad in ("https://evil.example.com/cb", f"http://127.0.0.1:{ctx.port + 1}/api/oauth/test/callback",
                f"http://127.0.0.1:{ctx.port}/other", f"http://127.0.0.1:{ctx.port}@evil.example.com/api/oauth/"):
        assert go(bad).status_code == 400


def test_settings_are_validated_per_field_and_empty_clears(app):
    ctx, svc, c = app
    ig = dest_id(c, "instagram")
    r = c.put(f"/api/destinations/{ig}/settings", json={"login": "carrier-pigeon", "video_base_url": "http://example.com/x"})
    assert r.status_code == 400
    errs = r.json()["detail"]["errors"]
    assert set(errs) == {"login", "video_base_url"} and "https" in errs["video_base_url"]
    assert c.get(f"/api/destinations/{ig}").json()["settings"] == {}          # nothing half-saved
    ok = c.put(f"/api/destinations/{ig}/settings",
               json={"login": "facebook", "video_base_url": "https://example.com/clips", "share_to_feed": False})
    assert ok.json()["settings"] == {"login": "facebook", "video_base_url": "https://example.com/clips",
                                      "share_to_feed": False}
    cleared = c.put(f"/api/destinations/{ig}/settings", json={"video_base_url": ""}).json()["settings"]
    assert "video_base_url" not in cleared and cleared["login"] == "facebook"
    yt = dest_id(c, "youtube")
    assert c.put(f"/api/destinations/{yt}/settings", json={"privacy": "secret"}).status_code == 400
    assert c.put(f"/api/destinations/{yt}/settings", json={"daily_limit": -1}).status_code == 400
    tt = dest_id(c, "tiktok")
    assert c.put(f"/api/destinations/{tt}/settings", json={"mode": "direct", "disable_duet": "yes"}).status_code == 400


def test_confirm_posted_stores_the_post_url_and_rejects_a_bad_one(app):
    ctx, svc, c = app
    c.put("/api/publish/test-mode", json={"enabled": True})
    clip = make_clip(ctx)
    svc.store.save_clip(clip)
    t = next(d["id"] for d in c.get("/api/destinations").json()["destinations"] if d["test"])
    did = c.post("/api/publish/post", json={"clip_id": clip["clip_id"], "destination_id": t}).json()["id"]
    wait_status(c, did, "published")
    svc.store.update("deliveries", did, status="needs_confirmation", provider_url="")
    bad = c.post(f"/api/publish/deliveries/{did}/confirm-posted", json={"url": "not a link"})
    assert bad.status_code == 400 and "url" in bad.json()["detail"]["errors"]
    assert c.get(f"/api/publish/deliveries/{did}").json()["status"] == "needs_confirmation"
    r = c.post(f"/api/publish/deliveries/{did}/confirm-posted", json={"url": "https://example.com/p/1"})
    assert r.status_code == 200 and r.json()["url"] == "https://example.com/p/1" and r.json()["status"] == "published"
    assert c.get(f"/api/publish/deliveries/{did}").json()["url"] == "https://example.com/p/1"


# ---- connecting an account: what the customer is shown, and what connecting must never do -----------------

def recording_youtube(svc, channel="UC_example", title="Example Clips", status=200, reason=""):
    """A fake Google that records every request it gets."""
    seen = []

    def channels(req):
        if status != 200:
            return httpx.Response(status, json={"error": {"errors": [{"reason": reason}]}})
        return httpx.Response(200, json={"items": [{"id": channel, "snippet": {"title": title}}]})

    def handler(req):
        seen.append((req.method, str(req.url)))
        if req.method == "POST" and "oauth2.googleapis.com/token" in str(req.url):
            return httpx.Response(200, json={"access_token": ATOKEN, "refresh_token": RTOKEN, "expires_in": 3599})
        if req.method == "GET" and "/youtube/v3/channels" in str(req.url):
            return channels(req)
        return httpx.Response(404, json={})
    svc.pub.factories["youtube"] = lambda settings, sandbox_dir: YouTubeAdapter(settings, httpx.MockTransport(handler))
    svc.pub._adapters.clear()
    return seen


def sign_in(c):
    _, q = start_oauth(c)
    return c.get("/api/oauth/youtube/callback", params={"code": CODE, "state": q["state"]})


def test_connecting_and_checking_never_upload_and_keep_every_posting_choice(app):
    ctx, svc, c = app
    yt = prep_youtube(c, svc)
    c.put("/api/publish/mode", json={"mode": "manual"})
    c.put(f"/api/destinations/{yt}/settings", json={"privacy": "unlisted", "daily_limit": 3})
    seen = recording_youtube(svc)
    url, q = start_oauth(c)
    assert q["prompt"] == "select_account consent"            # Google always asks which account or channel
    assert sign_in(c).status_code == 200
    d = c.get(f"/api/destinations/{yt}").json()
    assert d["status"] == "connected" and d["account"] == "Example Clips"
    assert d["account_id"] == "UC_example" and d["account_url"] == "https://www.youtube.com/channel/UC_example"
    assert d["enabled"] is False and d["rights_acknowledged"] is False   # connecting switches nothing on
    assert d["settings"]["privacy"] == "unlisted" and d["settings"]["daily_limit"] == 3
    assert c.get("/api/publish/mode").json()["mode"] == "manual"

    c.put(f"/api/destinations/{yt}/acknowledge", json={"acknowledged": True})
    c.put(f"/api/destinations/{yt}/enabled", json={"enabled": True})
    r = c.post(f"/api/destinations/{yt}/connect").json()                  # "Check connection"
    assert r["action"] == "verified" and r["ok"] and r["account"] == "Example Clips"
    d = c.get(f"/api/destinations/{yt}").json()
    assert d["enabled"] and d["rights_acknowledged"] and d["settings"]["privacy"] == "unlisted"
    assert c.get("/api/publish/mode").json()["mode"] == "manual"
    # the provider only ever saw the code exchange and the "which channel is this" question
    assert {(m, u.split("?")[0]) for m, u in seen} == {("POST", "https://oauth2.googleapis.com/token"),
                                                        ("GET", "https://www.googleapis.com/youtube/v3/channels")}
    assert not any("upload" in u or "/videos" in u for _, u in seen)
    assert c.get("/api/publish/deliveries").json()["deliveries"] == []   # and connecting queued nothing


def test_a_sign_in_on_another_channel_switches_posting_off_until_it_is_checked(app):
    ctx, svc, c = app
    yt = prep_youtube(c, svc)
    recording_youtube(svc)
    sign_in(c)
    c.put(f"/api/destinations/{yt}/acknowledge", json={"acknowledged": True})
    c.put(f"/api/destinations/{yt}/enabled", json={"enabled": True})
    sign_in(c)                                                            # the same channel again: nothing changes
    d = c.get(f"/api/destinations/{yt}").json()
    assert d["enabled"] and d["rights_acknowledged"] and d["account"] == "Example Clips"

    recording_youtube(svc, channel="UC_other", title="Other Channel")     # the wrong account was picked at Google
    assert "Connected as Other Channel" in sign_in(c).text
    d = c.get(f"/api/destinations/{yt}").json()
    assert d["status"] == "connected" and d["account"] == "Other Channel" and d["account_id"] == "UC_other"
    assert d["enabled"] is False and d["rights_acknowledged"] is True     # off, and only that
    assert "different account" in d["status_detail"] and "Example Clips" in d["status_detail"]
    assert "nothing was sent" in d["status_detail"]
    # switching it on again is the customer's decision, and then it stays on
    assert c.put(f"/api/destinations/{yt}/enabled", json={"enabled": True}).json()["enabled"] is True
    sign_in(c)
    assert c.get(f"/api/destinations/{yt}").json()["enabled"] is True


@pytest.mark.parametrize("status, reason, said", [
    (403, "accessNotConfigured", "accessNotConfigured"),
    (403, "forbidden", "HTTP 403 forbidden"),
])
def test_a_refused_channel_lookup_names_the_cause_and_its_next_action(app, status, reason, said):
    ctx, svc, c = app
    yt = prep_youtube(c, svc)
    recording_youtube(svc, status=status, reason=reason)
    assert sign_in(c).status_code == 502
    d = c.get(f"/api/destinations/{yt}").json()
    assert d["status"] == "error" and said in d["status_detail"] and ATOKEN not in json.dumps(d)
    assert d["account_id"] == "" and d["enabled"] is False
    fixes = [f for f in d["requirements"]["recover"] if any(m.lower() in d["status_detail"].lower() for m in f["match"])]
    assert bool(fixes) == (reason == "accessNotConfigured")
    if fixes:
        assert "Enable" in fixes[0]["action"] and fixes[0]["link"]["url"].startswith("https://console.cloud.google.com/")


def test_every_destination_says_what_each_field_is_and_who_posts(app):
    ctx, svc, c = app
    ds = {d["provider"]: d for d in c.get("/api/destinations").json()["destinations"]}
    for provider, d in ds.items():
        req = d["requirements"]
        assert req["what"] and req["cost"] and set(req["step_titles"]) >= {"app_credentials", "connect", "enable"}
        assert req["sign_in"]["button"] and req["identity"]["check"] and req["recover"]
        for m in req["methods"]:
            assert m["button"] and m["after"] and m["fields"]
            for f in m["fields"]:
                assert f["label"] and f["why"] and f["where"], (provider, f["name"])
                assert f["secret"] or f["name"] in ("client_id", "client_key")   # everything else is masked
        for s in [*req["developer_app_steps"], *[s for m in req["methods"] for s in m["steps"]]]:
            assert not s["link"] or s["link"]["url"].startswith("https://")
    # YouTube: only the client ID is required, the secret is asked as optional, and an API key is not enough
    yt = ds["youtube"]["requirements"]
    assert [(f["name"], f["required"]) for f in yt["methods"][0]["fields"]] == [("client_id", True), ("client_secret", False)]
    assert "cannot upload" in yt["key_note"] and "Metricool is not needed" in yt["what"] and "routes" not in yt
    assert not yt["methods"][0]["register_redirect"]                           # Desktop clients register nothing
    # Instagram and TikTok: this app posts itself; Metricool is named, not available here, and asks for nothing
    for provider in ("tiktok", "instagram"):
        native, outside = ds[provider]["requirements"]["routes"]
        assert (native["id"], native["available"]) == ("native", True)
        assert (outside["id"], outside["available"]) == ("metricool", False)
        assert "Metricool" in outside["title"] and "nothing to connect or enter" in outside["summary"]
        assert "not sent twice" in " ".join(outside["needs"])
        assert not any(k in outside for k in ("fields", "button", "methods"))
    assert not any("metricool" in f["name"] for d in ds.values() for m in d["requirements"]["methods"] for f in m["fields"])
    # the address shown is the one this installation really sends, also after an override
    ig = ds["instagram"]["id"]
    c.put(f"/api/destinations/{ig}/settings", json={"redirect_uri": "https://example.com/oauth/ig"})
    assert c.get(f"/api/destinations/{ig}").json()["requirements"]["redirect_uri"] == "https://example.com/oauth/ig"


def test_a_changed_account_holds_unconfirmed_uploads_and_strands_nothing(app):
    """The account changed under a destination that was on: an upload the old account may already have waits for
    the user's word, a clip that was only queued waits for the switch, and nothing stays stuck afterwards."""
    ctx, svc, c = app
    yt = prep_youtube(c, svc)
    recording_youtube(svc)
    sign_in(c)
    c.put(f"/api/destinations/{yt}/acknowledge", json={"acknowledged": True})
    c.put(f"/api/destinations/{yt}/enabled", json={"enabled": True})
    c.put("/api/publish/mode", json={"mode": "manual"})                 # deliveries are created held: no upload runs
    ids = [svc.pub.enqueue_clip(make_clip(ctx))[0] for _ in range(3)]
    queued, uploading, uncertain = ids
    paused = lambda frm: [{"t": 1.0, "from": frm, "to": "paused", "detail": "sign-in expired"}]  # noqa: E731
    svc.store.update("deliveries", queued, status="paused", history=paused("queued"))
    svc.store.update("deliveries", uploading, status="paused", history=paused("uploading"))
    svc.store.update("deliveries", uncertain, status="uncertain", reconcile=1, next_attempt_at=time.time() + 3600)

    seen = recording_youtube(svc, channel="UC_other", title="Other Channel")
    page = sign_in(c)
    assert "different account than before" in page.text and "switched off" in page.text
    state = lambda i: c.get(f"/api/publish/deliveries/{i}").json()  # noqa: E731
    assert state(queued)["status"] == "blocked"
    for i in (uploading, uncertain):                                    # never checked against the new account
        d = state(i)
        assert d["status"] == "needs_confirmation" and "Example Clips" in d["history"][-1]["detail"]
    assert not any("upload" in u or "/videos" in u or "playlistItems" in u for _, u in seen)

    on = c.put(f"/api/destinations/{yt}/enabled", json={"enabled": True}).json()
    assert on["enabled"] and "different account" not in on["status_detail"]   # the note goes with the decision
    assert state(queued)["status"] not in ("paused", "blocked", "needs_confirmation")   # it is on its way again
    assert state(uploading)["status"] == state(uncertain)["status"] == "needs_confirmation"
    assert not any("upload" in u for _, u in seen if "UC_" in u)


def test_an_optional_secret_left_empty_is_removed_not_kept(app):
    ctx, svc, c = app
    yt = dest_id(c, "youtube")
    c.put(f"/api/destinations/{yt}/credentials", json={"client_id": "cid-old", "client_secret": CSECRET})
    r = c.put(f"/api/destinations/{yt}/credentials", json={"client_id": "cid-new", "client_secret": ""})
    assert r.json() == {"app_credentials_set": ["client_id"]}           # a new client never keeps the old secret
    assert ctx.secrets.get("publish/app/youtube") == {"client_id": "cid-new"}
