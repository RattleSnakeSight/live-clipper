import asyncio
import base64
import json

import httpx
import pytest

from live_clipper.chat.kick import ACT_URL, KickChat
from live_clipper.chat.registry import make_adapter

BODY = json.dumps({"message_id": "M1", "broadcaster": {"user_id": 5, "channel_slug": "Chan"},
                   "sender": {"user_id": 7, "username": "zed"}, "content": "go [emote:1:KEKW]",
                   "created_at": "2025-01-14T16:08:06Z"}).encode()


def hdrs(mid="M1", sig="", typ="chat.message.sent"):
    return {"Kick-Event-Message-Id": mid, "Kick-Event-Message-Timestamp": "2025-01-14T16:08:06Z",
            "Kick-Event-Signature": sig, "Kick-Event-Type": typ, "Kick-Event-Version": "1"}


def mk(opts=None):
    ev = []
    return KickChat("chan", ev.append, {"access_token": "t"}, {"public_url": "https://x", **(opts or {})}), ev


@pytest.fixture
def no_crypto(monkeypatch):
    """Simulate a worker without the cryptography package (the fallback path) instead of skipping."""
    import builtins
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ImportError("simulated: cryptography not installed")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)


def test_unverified_rejected_by_default_and_allowed_by_option(no_crypto):
    ad, ev = mk()
    assert asyncio.run(ad.handle_webhook(hdrs(), BODY)) == 401 and not ev
    ad, ev = mk({"allow_unverified": True})
    assert asyncio.run(ad.handle_webhook(hdrs(), BODY)) == 200
    assert asyncio.run(ad.handle_webhook(hdrs(), BODY)) == 200       # duplicate
    assert len(ev) == 1
    e = ev[0]
    assert (e.user_name, e.emotes, e.msg_id, e.sent_at) == ("zed", ("KEKW",), "M1", 1736870886.0) and ad.health.state == "healthy"


def test_signature_verification_and_dedupe():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def sign(mid, body):
        return base64.b64encode(key.sign(f"{mid}.2025-01-14T16:08:06Z.".encode() + body, padding.PKCS1v15(), hashes.SHA256())).decode()

    ad, ev = mk({"public_key_pem": pem})

    async def go():
        assert await ad.handle_webhook(hdrs(sig="AAAA"), BODY) == 401
        assert await ad.handle_webhook(hdrs(sig=sign("M1", BODY + b" ")), BODY) == 401   # tampered body
        assert await ad.handle_webhook(hdrs(sig=sign("M1", BODY)), BODY) == 200
        assert await ad.handle_webhook(hdrs(sig=sign("M1", BODY)), BODY) == 200          # redelivery
    asyncio.run(go())
    assert len(ev) == 1


def test_other_channel_ignored(no_crypto):
    ad, ev = mk({"allow_unverified": True})
    other = BODY.replace(b'"Chan"', b'"other"').replace(b'"user_id": 5', b'"user_id": 6')
    assert asyncio.run(ad.handle_webhook(hdrs("M9"), other)) == 200 and not ev


def test_no_public_url_unavailable():
    ad = KickChat("chan", lambda e: None, {"access_token": "t"}, {})
    asyncio.run(ad.run())
    assert ad.health.state == "unavailable" and ad.health.action_needed == ACT_URL


def test_subscribe_flow_and_registry():
    calls = []

    def h(req):
        calls.append((req.method, req.url.path, req.url.params.get("slug"), req.content))
        if req.url.path.endswith("/channels"):
            return httpx.Response(200, json={"data": [{"broadcaster_user_id": 5, "slug": "chan"}]})
        if req.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": [{"name": "chat.message.sent", "version": 1, "subscription_id": "s"}]})

    async def go():
        ad = make_adapter("kick", "chan", lambda e: None, {"access_token": "t"},
                          {"public_url": "https://x", "transport": httpx.MockTransport(h), "base_url": "http://k"})
        assert isinstance(ad, KickChat)
        ad.options["sleep"] = lambda s: asyncio.sleep(0.5)
        ad._sleep = ad.options["sleep"]
        ad.start()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if any(c[0] == "POST" for c in calls):
                break
        assert ad.health.state == "connecting" and "waiting for first webhook" in ad.health.detail
        await ad.stop()
    asyncio.run(go())
    post = [c for c in calls if c[0] == "POST"][0]
    assert json.loads(post[3]) == {"broadcaster_user_id": 5, "events": [{"name": "chat.message.sent", "version": 1}], "method": "webhook"}
