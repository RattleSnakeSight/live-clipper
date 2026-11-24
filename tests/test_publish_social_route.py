"""Metricool first, never assumed: the app shows the preferred social route (Instagram and TikTok through an outside
scheduling service), skippable, and says "connected" only when a local sender has said so itself (a heartbeat file
in the app's state folder) and confirmed publications only from that sender's own receipts in the shared claims
table. Native and sandbox receipts are never counted for it, and one network never has two senders.

Real app over HTTP, real files; the sender is a synthetic stand-in that writes the heartbeat."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from publish_util import make_ctx, no_keyring  # noqa: F401
from test_publish_cadence import LEGACY_SCHEMA

from live_clipper.publish.sandbox_server import shutdown_sandbox
from live_clipper.publish.service import PublishService
from live_clipper.server import create_app

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def app():
    ctx = make_ctx()
    svc = PublishService()
    ctx.services[:] = [svc]
    with TestClient(create_app(ctx), base_url=BASE, headers={"Authorization": f"Bearer {ctx.token}"}) as c:
        yield ctx, svc, c
    shutdown_sandbox()


def heartbeat(ctx, **kw):
    body = {"name": "metricool", "verified": True, "owns": ["instagram", "tiktok"], "networks": ["instagram", "tiktok"],
            "at": time.time(), "published": {}, **kw}
    (ctx.paths.state / "social-sender.json").write_text(json.dumps(body), encoding="utf-8")


def route(c):
    return c.get("/api/social/route").json()


def dest(c, provider):
    return next(d for d in c.get("/api/destinations").json()["destinations"] if d["provider"] == provider)


def test_metricool_is_preferred_by_default_and_never_connected_from_the_preference_alone(app):
    ctx, _, c = app
    r = route(c)
    assert r["preference"] == "metricool" and r["state"] == "not_connected" and r["networks"] == []
    assert "Metricool" in r["label"] and r["needs"]                     # says what a clean install must bring


def test_a_verified_fresh_sender_makes_it_connected_and_a_stale_or_unverified_one_does_not(app):
    ctx, _, c = app
    heartbeat(ctx)
    assert route(c)["state"] == "connected" and route(c)["networks"] == ["instagram", "tiktok"]
    heartbeat(ctx, at=time.time() - 3600)
    assert route(c)["state"] == "waiting" and route(c)["networks"] == []     # seen, but not alive now
    heartbeat(ctx, at=time.time() + 3600)
    assert route(c)["state"] == "waiting"                               # a timestamp in the future never passes
    heartbeat(ctx, verified=False)
    assert route(c)["state"] == "waiting"
    heartbeat(ctx, name="something-else")                               # a sender for another service is not it
    assert route(c)["state"] == "not_connected"
    (ctx.paths.state / "social-sender.json").write_text("not json", encoding="utf-8")
    assert route(c)["state"] == "not_connected"


def test_revoking_is_the_sender_going_away_and_skipping_is_the_users_choice(app):
    ctx, _, c = app
    heartbeat(ctx)
    assert route(c)["state"] == "connected"
    (ctx.paths.state / "social-sender.json").unlink()
    assert route(c)["state"] == "not_connected"
    heartbeat(ctx)
    assert c.put("/api/social/route", json={"route": "direct"}).json()["state"] == "skipped"
    assert route(c)["networks"] == []                                   # a skipped route owns nothing
    assert c.put("/api/social/route", json={"route": "nonsense"}).status_code == 400
    assert c.put("/api/social/route", json={"route": "metricool"}).json()["state"] == "connected"


def test_only_publications_the_sender_read_back_as_published_count_as_confirmed(app):
    ctx, _, c = app
    for stmt in LEGACY_SCHEMA:
        ctx.db.execute(stmt)
    rows = [("native:1", "youtube", 0), ("native:2", "instagram", 0), ("metricool:5:instagram", "instagram", 0),
            ("metricool:5:tiktok", "tiktok", 1), ("metricool:6:tiktok", "tiktok", 0)]
    for owner, net, pending in rows:
        ctx.db.execute("INSERT INTO local_english_claims VALUES(?,?,'channel:1','m1',1,'',?,1,?)",
                       (owner, net, time.time(), pending))
    heartbeat(ctx)                                                      # no published counts reported
    r = route(c)
    assert r["confirmed"] == {} and r["unconfirmed"] == {"tiktok": 1}   # a settled claim row is not a publication
    heartbeat(ctx, published={"instagram": 2, "tiktok": 0, "youtube": 9, "x": "many", "bad": True})
    assert route(c)["confirmed"] == {"instagram": 2}                    # only its own networks, real counts


def test_a_network_the_connected_sender_owns_cannot_be_switched_on_or_sent_natively(app):
    ctx, svc, c = app
    heartbeat(ctx)
    ig = dest(c, "instagram")
    ctx.db.execute("UPDATE publish_destinations SET status='connected', rights_ack_at=? WHERE id=?",
                   (time.time(), ig["id"]))
    r = c.put(f"/api/destinations/{ig['id']}/enabled", json={"enabled": True})
    assert r.status_code == 409 and "Metricool" in r.text
    assert "posted through Metricool" in svc.pub.block_reason(svc.store.q1(
        "SELECT * FROM publish_destinations WHERE id=?", ig["id"]))
    yt = svc.store.q1("SELECT * FROM publish_destinations WHERE provider='youtube'")
    assert "Metricool" not in (svc.pub.block_reason(yt) or "")           # YouTube stays native


def test_when_the_route_is_skipped_or_the_sender_is_gone_the_direct_route_is_free_again(app):
    ctx, svc, c = app
    heartbeat(ctx)
    ig = dest(c, "instagram")
    ctx.db.execute("UPDATE publish_destinations SET status='connected', rights_ack_at=? WHERE id=?",
                   (time.time(), ig["id"]))
    c.put("/api/social/route", json={"route": "direct"})
    assert c.put(f"/api/destinations/{ig['id']}/enabled", json={"enabled": True}).status_code == 200
    c.put("/api/social/route", json={"route": "metricool"})              # the sender is connected again
    row = svc.store.q1("SELECT * FROM publish_destinations WHERE id=?", ig["id"])
    assert "Metricool" in svc.pub.block_reason(row)                      # an already-on direct route stops: one sender
    heartbeat(ctx, at=time.time() - 7200, verified=False)                # outage: stale and unverified
    assert "Metricool" in svc.pub.block_reason(row)                      # no fall-through to the direct route
    (ctx.paths.state / "social-sender.json").unlink()
    assert svc.pub.block_reason(row) is None                             # revoked: the direct route is free again


def test_setup_needs_no_direct_instagram_or_tiktok_credentials_and_none_is_a_valid_choice(app):
    ctx, _, c = app
    from live_clipper.setup_service import build_questions
    qs = {q["id"]: q for q in build_questions({"gpu": {"kind": "none", "name": ""}, "cpu": {"cores": 4}, "ram_bytes": 16 * 1024**3, "disk": {"free_bytes": 500 * 1024**3}}, "store")}
    q = qs["posting.destinations"]
    assert "none" in [o["value"] for o in q["options"]] and q["validation"].get("min_items") == 1
    assert "Metricool" in q["note"] and q["intent"] == "Post my clips"
