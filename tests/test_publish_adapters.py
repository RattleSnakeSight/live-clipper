"""Provider adapters against httpx.MockTransport: request shapes, state mapping, labelling, PKCE."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from live_clipper.publish.base import PublishJob, retry_after
from live_clipper.publish.instagram import InstagramAdapter
from live_clipper.publish.tiktok import MB, TikTokAdapter, chunk_plan
from live_clipper.publish.youtube import YouTubeAdapter

CREDS = {"access_token": "AT-very-secret-123", "refresh_token": "RT-very-secret-456", "client_id": "cid",
         "client_secret": "csecret-789"}


class Mock:
    """Route table (method, url-substring) -> handler(request) -> Response. Records every request."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, req):
        self.calls.append(req)
        for (m, frag), fn in self.routes.items():
            if req.method == m and frag in str(req.url):
                return fn(req)
        return httpx.Response(404, json={"error": {"message": f"no route {req.method} {req.url}"}})

    @property
    def transport(self):
        return httpx.MockTransport(self)

    def find(self, m, frag):
        return [c for c in self.calls if c.method == m and frag in str(c.url)]


def job(tmp_path, size=300 * 1024, **kw) -> PublishJob:
    p = Path(tmp_path) / "v.mp4"
    p.write_bytes(b"\x01" * size)
    return PublishJob(1, "ab" * 32, p, "Huge heist escape", description="Huge heist escape", language="en", ref={},
                      settings=kw)


def run(c):
    return asyncio.run(c)


# ---------------------------------------------------------------- YouTube
def yt_video(vid="VID1", up="uploaded", priv="private", tags=(), desc=""):
    return {"id": vid, "status": {"uploadStatus": up, "privacyStatus": priv},
            "snippet": {"tags": list(tags), "description": desc}}


def test_youtube_resumable_upload_shapes(tmp_path):
    got = {"ranges": []}

    def init(req):
        got["meta"] = json.loads(req.content)
        return httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=S1"})

    def put(req):
        got["ranges"].append(req.headers["content-range"])
        end = int(req.headers["content-range"].split("-")[1].split("/")[0])
        if end + 1 < 300 * 1024:
            return httpx.Response(308, headers={"Range": f"bytes=0-{end}"})
        return httpx.Response(200, json=yt_video(up="uploaded"))

    m = Mock({("POST", "upload/youtube/v3/videos"): init, ("PUT", "upload_id=S1"): put})
    j = job(tmp_path)
    ad = YouTubeAdapter({"chunk_size": 256 * 1024, "privacy": "unlisted"}, transport=m.transport)
    res = run(ad.upload(j, CREDS))
    init_req = m.find("POST", "upload/youtube/v3/videos")[0]
    assert "paidProductPlacementDetails" in str(init_req.url)
    assert init_req.headers["authorization"] == "Bearer AT-very-secret-123"
    sn, st = got["meta"]["snippet"], got["meta"]["status"]
    assert sn["title"].endswith("#Shorts") and j.marker in sn["tags"] and f"ref {j.marker}" in sn["description"]
    assert st["privacyStatus"] == "unlisted" and st["selfDeclaredMadeForKids"] is False
    assert got["meta"]["paidProductPlacementDetails"] == {"hasPaidProductPlacement": False}
    assert got["ranges"] == ["bytes 0-262143/307200", "bytes 262144-307199/307200"]
    assert res.state == "processing" and j.ref["id"] == "VID1" and j.ref["sent_all"] is True


def test_youtube_privacy_always_explicit_and_result_labels():
    ad = YouTubeAdapter({}, transport=None)
    assert ad.metadata(PublishJob(1, "k" * 64, Path("x"), "t"))["status"]["privacyStatus"] == "public"
    assert ad._result(yt_video(up="processed", priv="public")).state == "published"
    r = ad._result(yt_video(up="processed", priv="unlisted"))
    assert r.state == "private" and "unlisted" in r.detail
    assert ad._result(yt_video(up="rejected")).rejected is True


def test_youtube_quota_403_is_rate_limited(tmp_path):
    body = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    m = Mock({("POST", "upload/youtube/v3/videos"): lambda r: httpx.Response(403, json=body)})
    res = run(YouTubeAdapter({}, transport=m.transport).upload(job(tmp_path), CREDS))
    assert res.state == "rate_limited" and res.retry_after == 3600.0


def test_youtube_oauth_desktop_pkce_and_refresh():
    ad = YouTubeAdapter({})
    verifier = "v" * 64
    u = urlparse(ad.authorize_url({"client_id": "cid"}, "http://127.0.0.1:8765/api/oauth/youtube/callback", "st",
                                  ad.challenge(verifier)))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.netloc == "accounts.google.com" and q["code_challenge_method"] == "S256"
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert q["code_challenge"] == expect
    assert q["scope"] == ("https://www.googleapis.com/auth/youtube.upload "
                          "https://www.googleapis.com/auth/youtube.readonly") and q["access_type"] == "offline"
    seen = []

    def tok(req):
        seen.append(parse_qs(req.content.decode()))
        return httpx.Response(200, json={"access_token": "new-at", "expires_in": 3599})

    ad2 = YouTubeAdapter({}, transport=Mock({("POST", "oauth2.googleapis.com/token"): tok}).transport)
    out = run(ad2.refresh({"refresh_token": "rt", "client_id": "cid"}))             # public client: no secret sent
    assert out["access_token"] == "new-at" and "client_secret" not in seen[0]
    run(ad2.exchange_code({"client_id": "cid"}, "code1", "http://127.0.0.1:1/cb", verifier))
    assert seen[1]["code_verifier"] == [verifier] and "client_secret" not in seen[1]


def test_youtube_exchange_failure_raises_without_echoing_code():
    m = Mock({("POST", "oauth2.googleapis.com/token"): lambda r: httpx.Response(400, json={"error": "invalid_grant"})})
    with pytest.raises(ValueError) as e:
        run(YouTubeAdapter({}, transport=m.transport).exchange_code({"client_id": "c"}, "SECRETCODE", "u", "v"))
    assert "invalid_grant" in str(e.value) and "SECRETCODE" not in str(e.value)


def test_youtube_reconcile_session_then_playlist(tmp_path):
    j = job(tmp_path)
    vid = yt_video("OLD", up="processed", priv="public", tags=[j.marker])

    def chans(r):
        return httpx.Response(200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
    m = Mock({("GET", "/youtube/v3/channels"): chans,
              ("GET", "/youtube/v3/playlistItems"): lambda r: httpx.Response(
                  200, json={"items": [{"contentDetails": {"videoId": "OLD"}}]}),
              ("GET", "/youtube/v3/videos"): lambda r: httpx.Response(200, json={"items": [vid]})})
    res = run(YouTubeAdapter({}, transport=m.transport).reconcile(j, CREDS))
    assert res.state == "published" and "reconciled" in res.detail and j.ref["id"] == "OLD"
    m2 = Mock({("GET", "/youtube/v3/channels"): chans,
               ("GET", "/youtube/v3/playlistItems"): lambda r: httpx.Response(200, json={"items": []})})
    j2 = job(tmp_path)
    assert run(YouTubeAdapter({}, transport=m2.transport).reconcile(j2, CREDS)) is None
    j2.ref["sent_all"] = True       # every byte was sent: never "not found -> upload again"
    assert run(YouTubeAdapter({}, transport=m2.transport).reconcile(j2, CREDS)).keep_reconciling


# ---------------------------------------------------------------- TikTok
def test_chunk_plan():
    assert chunk_plan(3 * MB) == (3 * MB, 1)
    assert chunk_plan(25 * MB, 10 * MB) == (10 * MB, 2)
    assert chunk_plan(500 * MB, 1000 * MB)[0] == 64 * MB


def test_tiktok_pkce_is_hex_s256_and_scopes():
    ad = TikTokAdapter({})
    verifier = "x" * 64
    u = urlparse(ad.authorize_url({"client_key": "ck"}, "http://127.0.0.1:8765/api/oauth/tiktok/callback", "st",
                                  ad.challenge(verifier)))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert q["code_challenge"] == hashlib.sha256(verifier.encode()).hexdigest()
    assert q["code_challenge_method"] == "S256" and q["scope"] == "user.info.basic,video.upload,video.publish"
    assert q["client_key"] == "ck" and q["redirect_uri"].startswith("http://127.0.0.1:8765/")


def test_tiktok_exchange_sends_verifier_and_reports_errors():
    seen = []

    def tok(req):
        seen.append(parse_qs(req.content.decode()))
        return httpx.Response(200, json={"access_token": "a", "refresh_token": "r", "open_id": "o"})
    ad = TikTokAdapter({}, transport=Mock({("POST", "/v2/oauth/token/"): tok}).transport)
    run(ad.exchange_code({"client_key": "ck", "client_secret": "cs"}, "c", "http://127.0.0.1:1/cb", "ver"))
    assert seen[0]["code_verifier"] == ["ver"] and seen[0]["client_key"] == ["ck"]
    bad = TikTokAdapter({}, transport=Mock({("POST", "/v2/oauth/token/"): lambda r: httpx.Response(
        400, json={"error": "invalid_grant", "error_description": "bad code"})}).transport)
    with pytest.raises(ValueError, match="invalid_grant"):
        run(bad.exchange_code({"client_key": "ck"}, "c", "u", "v"))


def tt_routes(status, init_url="init/", capture=None):
    def init(req):
        if capture is not None:
            capture["body"] = json.loads(req.content)
        return httpx.Response(200, json={"data": {"publish_id": "P1", "upload_url": "https://upload.example.com/u1"},
                                         "error": {"code": "ok"}})
    info = {"data": {"privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"], "creator_username": "example_clips"},
            "error": {"code": "ok"}}
    return {("POST", "/v2/post/publish/creator_info/query/"): lambda r: httpx.Response(200, json=info),
            ("POST", f"/v2/post/publish/{init_url}"): init,
            ("PUT", "upload.example.com"): lambda r: httpx.Response(201),
            ("POST", "/v2/post/publish/status/fetch/"): lambda r: httpx.Response(200, json={
                "data": {"status": status, "publicaly_available_post_id": [123]}, "error": {"code": "ok"}})}


def test_tiktok_inbox_upload_is_draft(tmp_path):
    m = Mock(tt_routes("SEND_TO_USER_INBOX", "inbox/video/init/"))
    j = job(tmp_path)
    ad = TikTokAdapter({}, transport=m.transport)
    assert run(ad.upload(j, CREDS)).state == "processing" and j.ref["publish_id"] == "P1"
    assert run(ad.poll(j, "P1", CREDS)).state == "draft"
    assert not m.find("POST", "creator_info")


def test_tiktok_direct_unaudited_is_self_only_and_private(tmp_path):
    cap = {}
    m = Mock(tt_routes("PUBLISH_COMPLETE", "video/init/", cap))
    j = job(tmp_path)
    ad = TikTokAdapter({"mode": "direct"}, transport=m.transport)
    run(ad.upload(j, CREDS))
    assert cap["body"]["post_info"]["privacy_level"] == "SELF_ONLY"
    assert cap["body"]["source_info"]["source"] == "FILE_UPLOAD" and j.ref["final_chunk_attempted"] and j.ref["uploaded"]
    res = run(ad.poll(j, "P1", CREDS))
    assert res.state == "private" and res.post_id == "123"


def test_tiktok_errors_map_to_states():
    ad = TikTokAdapter({})

    def r(code, status=200):
        return httpx.Response(status, json={"error": {"code": code, "message": "m"}})
    assert ad._err(r("access_token_invalid", 401), "x").state == "auth_expired"
    assert ad._err(r("rate_limit_exceeded", 429), "x").state == "rate_limited"
    assert ad._err(r("spam_risk_too_many_posts", 403), "x").retry_after == 3600.0
    p = ad._err(r("unaudited_client_can_only_post_to_private_accounts", 403), "x")
    assert p.state == "failed" and p.permanent


def test_tiktok_reconcile_rules(tmp_path):
    ad = TikTokAdapter({}, transport=Mock(tt_routes("PROCESSING_UPLOAD")).transport)
    j = job(tmp_path)
    assert run(ad.reconcile(j, CREDS)) is None                       # nothing started: safe
    j.ref.update(init_started=True)
    assert run(ad.reconcile(j, CREDS)).state == "uncertain"          # init may have succeeded, no id
    j.ref.update(publish_id="P1", final_chunk_attempted=True, upload_v=2)
    assert run(ad.reconcile(j, CREDS)).state == "processing"         # keep polling, never re-init


# ---------------------------------------------------------------- Instagram
def ig_routes(cap):
    def media(req):
        cap["media"] = parse_qs(req.content.decode())
        return httpx.Response(200, json={"id": "C1"})

    def rupload(req):
        cap["rupload"] = req
        return httpx.Response(200, json={"success": True})
    limit = {"data": [{"quota_usage": 1, "config": {"quota_total": 50}}]}
    return {("GET", "content_publishing_limit"): lambda r: httpx.Response(200, json=limit),
            ("POST", "/media_publish"): lambda r: httpx.Response(200, json={"id": "M1"}),
            ("POST", "/17841/media"): media,
            ("POST", "rupload.facebook.com"): rupload,
            ("GET", "/C1"): lambda r: httpx.Response(200, json={"status_code": "FINISHED"}),
            ("GET", "/M1"): lambda r: httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/abc/"})}


def test_instagram_facebook_login_resumable_flow(tmp_path):
    cap = {}
    m = Mock(ig_routes(cap))
    j = job(tmp_path, login="facebook", account_info={"account_id": "17841"})
    ad = InstagramAdapter(j.settings, transport=m.transport)
    res = run(ad.upload(j, CREDS))
    assert res.state == "processing" and cap["media"]["upload_type"] == ["resumable"]
    assert cap["media"]["media_type"] == ["REELS"] and cap["media"]["caption"][0].endswith(j.marker)
    assert cap["rupload"].headers["authorization"] == "OAuth AT-very-secret-123" and cap["rupload"].headers["offset"] == "0"
    pub = run(ad.poll(j, "C1", CREDS))
    assert pub.state == "published" and pub.url.endswith("/abc/") and j.ref["publish_started"] and j.ref["media_id"] == "M1"


def test_instagram_login_needs_public_video_url(tmp_path):
    cap = {}
    m = Mock(ig_routes(cap))
    j = job(tmp_path, account_info={"account_id": "17841"})
    res = run(InstagramAdapter(j.settings, transport=m.transport).upload(j, CREDS))
    assert res.state == "failed" and res.permanent and "video_base_url" in res.detail
    j2 = job(tmp_path, account_info={"account_id": "17841"}, video_base_url="https://media.example.com/clips/")
    res = run(InstagramAdapter(j2.settings, transport=m.transport).upload(j2, CREDS))
    assert res.state == "processing" and cap["media"]["video_url"] == ["https://media.example.com/clips/v.mp4"]
    assert "upload_type" not in cap["media"] and "rupload" not in cap


def test_instagram_publishing_limit_reached(tmp_path):
    cap = {}
    routes = ig_routes(cap)
    full = {"data": [{"quota_usage": 50, "config": {"quota_total": 50}}]}
    routes[("GET", "content_publishing_limit")] = lambda r: httpx.Response(200, json=full)
    j = job(tmp_path, login="facebook", account_info={"account_id": "17841"})
    res = run(InstagramAdapter(j.settings, transport=Mock(routes).transport).upload(j, CREDS))
    assert res.state == "rate_limited" and "media" not in cap


def test_instagram_errors():
    ad = InstagramAdapter({})

    def r(code, status=400, sub=None):
        return httpx.Response(status, json={"error": {"code": code, "error_subcode": sub, "message": "m"}})
    assert ad._err(r(190, 400), "x").state == "auth_expired"
    assert ad._err(r(4), "x").state == "rate_limited"
    assert ad._err(r(100, 400, 2207042), "x").retry_after == 3600.0
    assert ad._err(r(2), "x").permanent is False


def test_retry_after_header_forms():
    assert retry_after(httpx.Response(429, headers={"retry-after": "30"})) == 30.0
    assert retry_after(httpx.Response(429)) == 60.0
    assert retry_after(httpx.Response(429, headers={"x-ratelimit-reset": "12"})) == 12.0
    assert retry_after(httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"})) > 1e6


def test_facebook_failed_long_lived_exchange_does_not_echo_short_token():
    short = "SHORT-LIVED-FB-TOKEN-123456"

    def exch(req):
        if req.url.params.get("grant_type") == "fb_exchange_token":
            return httpx.Response(400, json={"error": {"type": "OAuthException", "message": "Invalid token"}})
        return httpx.Response(200, json={"access_token": short})

    m = Mock({("GET", "/oauth/access_token"): exch})
    ad = InstagramAdapter({"login": "facebook"}, transport=m.transport)
    with pytest.raises(ValueError) as e:
        run(ad.exchange_code({"client_id": "c", "client_secret": "s"}, "code", "u", "v"))
    assert "OAuthException" in str(e.value) and short not in str(e.value) and "fb_exchange_token" not in str(e.value)
