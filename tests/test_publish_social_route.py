"""Metricool first, never assumed: the app shows the preferred social route (Instagram and TikTok through an outside
scheduling service), skippable, and says "connected" only when a local sender has said so itself (a heartbeat file
in the app's state folder) and confirmed publications only from that sender's own receipts in the shared claims
table. Native and sandbox receipts are never counted for it, and one network never has two senders.

Real app over HTTP, real files; the sender is a synthetic stand-in that writes the heartbeat."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from publish_util import World, make_clip, make_ctx, no_keyring, sb  # noqa: F401
from test_publish_cadence import LEGACY_SCHEMA

from live_clipper.publish.sandbox_server import shutdown_sandbox
from live_clipper.publish.service import PublishService
from live_clipper.publish.social import Social
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
    heartbeat(ctx, name="something-else")                               # a sender for another service is not it
    assert route(c)["state"] == "not_connected"
    (ctx.paths.state / "social-sender.json").write_text("not json", encoding="utf-8")
    assert route(c)["state"] == "not_connected"
    heartbeat(ctx, owns=[], networks=[])
    assert route(c)["state"] == "waiting" and route(c)["owned"] == []   # seen, but names no network
    heartbeat(ctx, verified=False)
    r = route(c)
    assert r["state"] == "held" and r["owned"] == ["instagram", "tiktok"]   # configured, not verified: held, not connected
    heartbeat(ctx)
    assert route(c)["state"] == "connected" and route(c)["networks"] == ["instagram", "tiktok"]
    heartbeat(ctx, at=time.time() - 3600)
    r = route(c)
    assert r["state"] == "held" and r["networks"] == [] and r["owned"] == ["instagram", "tiktok"]   # not alive now
    heartbeat(ctx, at=time.time() + 3600)
    assert route(c)["state"] == "held"                                  # a timestamp in the future never passes


def test_only_the_users_skip_hands_the_networks_back_never_the_heartbeat_going_away(app):
    ctx, _, c = app
    heartbeat(ctx)
    assert route(c)["state"] == "connected"
    (ctx.paths.state / "social-sender.json").unlink()
    r = route(c)
    assert r["state"] == "held" and r["owned"] == ["instagram", "tiktok"]   # the sender went away: still its networks
    assert c.put("/api/social/route", json={"route": "direct"}).json()["state"] == "skipped"
    assert route(c)["owned"] == []                                      # a skipped route owns nothing
    assert c.put("/api/social/route", json={"route": "nonsense"}).status_code == 400
    assert c.put("/api/social/route", json={"route": "metricool"}).json()["state"] == "not_connected"   # no claim back
    heartbeat(ctx)
    assert route(c)["state"] == "connected"


def ig_ready(ctx, svc, c):
    ig = dest(c, "instagram")
    ctx.db.execute("UPDATE publish_destinations SET status='connected', enabled=1, rights_ack_at=? WHERE id=?",
                   (time.time(), ig["id"]))
    return svc.store.q1("SELECT * FROM publish_destinations WHERE id=?", ig["id"])


BROKEN = {
    "truncated json": lambda p: p.write_text('{"name": "metricool", "verified": tr', encoding="utf-8"),
    "unreadable": lambda p: (p.unlink(), p.mkdir()),
    "missing": lambda p: p.unlink(),
    "owns is a number": lambda p: p.write_text(json.dumps({"name": "metricool", "verified": True, "owns": 42,
                                                           "at": time.time()}), encoding="utf-8"),
    "owns is a string": lambda p: p.write_text(json.dumps({"name": "metricool", "verified": True, "owns": "instagram",
                                                           "at": time.time()}), encoding="utf-8"),
    "not an object": lambda p: p.write_text("[1, 2]", encoding="utf-8"),
    "unverified and stale": lambda p: p.write_text(json.dumps({"name": "metricool", "verified": False,
                                                               "owns": [], "at": time.time() - 7200}), encoding="utf-8"),
}


@pytest.mark.parametrize("how", BROKEN)
def test_a_broken_heartbeat_never_hands_the_networks_back_and_never_raises(app, how):
    ctx, svc, c = app
    ig = ig_ready(ctx, svc, c)
    heartbeat(ctx)
    assert "Metricool" in svc.pub.block_reason(ig)
    BROKEN[how](ctx.paths.state / "social-sender.json")
    assert "Metricool" in svc.pub.block_reason(ig)                      # still the sender's network
    r = c.get("/api/social/route")
    assert r.status_code == 200 and r.json()["state"] == "held" and r.json()["owned"] == ["instagram", "tiktok"]
    assert c.put(f"/api/destinations/{ig['id']}/enabled", json={"enabled": True}).status_code == 409


@pytest.mark.parametrize("body", [{"published": [1]}, {"published": "many"}, {"at": "soon"}, {"at": 10**1000},
                                  {"owns": {"x": 1}}, {"networks": 7, "owns": None}, {"verified": "yes"}])
def test_wrong_types_in_a_heartbeat_never_break_the_route_or_the_publisher(app, body):
    ctx, svc, c = app
    heartbeat(ctx, **body)
    assert c.get("/api/social/route").status_code == 200
    yt = svc.store.q1("SELECT * FROM publish_destinations WHERE provider='youtube'")
    svc.pub.block_reason(yt)                                            # no exception


def test_a_configured_but_unverified_sender_keeps_its_networks_when_its_file_breaks(app):
    ctx, svc, c = app
    heartbeat(ctx, verified=False, at=time.time() - 7200)              # the first report seen after the update
    assert svc.social.owned() == {"instagram", "tiktok"}
    (ctx.paths.state / "social-sender.json").write_text("{", encoding="utf-8")
    assert svc.social.owned() == {"instagram", "tiktok"}


def test_a_failed_write_of_the_record_never_raises_and_a_failed_read_holds_both_networks(app, monkeypatch):
    import sqlite3
    ctx, svc, c = app

    def boom(*a):
        raise sqlite3.OperationalError("database is locked")
    heartbeat(ctx)
    monkeypatch.setattr(ctx.app_state, "set", boom)
    assert svc.social.owned() == {"instagram", "tiktok"}               # held from the file this time, no exception
    (ctx.paths.state / "social-sender.json").write_text("{", encoding="utf-8")
    assert svc.social.owned() == {"instagram", "tiktok"}               # not recorded yet, still held in memory
    monkeypatch.undo()
    assert svc.social.owned() == {"instagram", "tiktok"}               # the write is retried once it works
    assert ctx.app_state.get("social.owned") == ["instagram", "tiktok"]
    monkeypatch.setattr(ctx.app_state, "get", boom)
    (ctx.paths.state / "social-sender.json").unlink()
    assert svc.social.owned() == {"instagram", "tiktok"}               # the record cannot be read: fail closed


def test_a_clean_install_claims_nothing(app):
    ctx, svc, c = app
    ig = ig_ready(ctx, svc, c)
    assert svc.social.owned() == set() and svc.pub.block_reason(ig) is None
    assert route(c)["owned"] == []


def test_ownership_survives_a_restart_without_the_heartbeat(app):
    ctx, svc, c = app
    heartbeat(ctx)
    assert svc.social.owned() == {"instagram", "tiktok"}
    (ctx.paths.state / "social-sender.json").unlink()
    assert Social(make_ctx()).owned() == {"instagram", "tiktok"}        # a new process, the same data folder


@pytest.mark.parametrize("failure", ["ownership", "preference"])
def test_failed_skip_transaction_keeps_preference_and_ownership(app, monkeypatch, failure):
    import sqlite3

    ctx, svc, c = app
    heartbeat(ctx)
    assert svc.social.owned() == {"instagram", "tiktok"}
    (ctx.paths.state / "social-sender.json").unlink()
    target = ctx.app_state if failure == "ownership" else ctx.settings

    def fail(*args):
        raise sqlite3.OperationalError("synthetic write failure")

    with monkeypatch.context() as patch:
        patch.setattr(target, "set", fail)
        with pytest.raises(sqlite3.OperationalError):
            c.put("/api/social/route", json={"route": "direct"})
    assert ctx.settings.get("social.route", "metricool") == "metricool"
    assert ctx.app_state.get("social.owned") == ["instagram", "tiktok"]
    assert svc.social.owned() == {"instagram", "tiktok"}
    assert route(c)["state"] == "held"


def test_sender_registration_and_explicit_skip_are_serialized(app, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    ctx, svc, _ = app
    heartbeat(ctx)
    entered, proceed, skipping = threading.Event(), threading.Event(), threading.Event()
    real_set = ctx.app_state.set

    def save(key, value):
        if key == "social.owned" and value:
            entered.set()
            assert proceed.wait(5)
        return real_set(key, value)

    def skip():
        skipping.set()
        svc.social.set_route("direct")

    monkeypatch.setattr(ctx.app_state, "set", save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        registration = pool.submit(svc.social.owned)
        try:
            assert entered.wait(5)
            release = pool.submit(skip)
            assert skipping.wait(5)
        finally:
            proceed.set()
        assert registration.result(timeout=5) == {"instagram", "tiktok"}
        release.result(timeout=5)
    assert ctx.app_state.get("social.owned") == []
    (ctx.paths.state / "social-sender.json").unlink()
    svc.social.set_route("metricool")
    assert svc.social.owned() == set()  # neither a late registration nor the in-memory hold survived the skip


def test_ownership_uses_the_publishers_lock_and_existing_claim_transaction(app):
    ctx, svc, _ = app
    heartbeat(ctx)
    assert svc.social._lock is svc.store._lock
    with svc.store._lock, svc.store.atomic():
        assert svc.social.owned() == {"instagram", "tiktok"}


def test_claim_and_route_change_take_the_lock_before_the_database(sb, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    w = World(sb)
    w.dest(provider="youtube")
    social = Social(w.ctx, lock=w.store._lock)
    w.pub.social_owned = social.owned
    w.ctx.settings.set("publish.mode", "manual")
    first = w.pub.enqueue_clip(make_clip(w.ctx))[0]
    held = w.pub.enqueue_clip(make_clip(w.ctx))[0]
    w.pub.post(first)
    entered, requesting, request_db = threading.Event(), threading.Event(), threading.Event()
    roles = {}
    original_tx = w.ctx.db.tx

    @contextmanager
    def transaction():
        role = roles.get(threading.get_ident())
        if role == "request":
            request_db.set()
        with original_tx() as conn:
            if role == "claim":
                entered.set()
                assert requesting.wait(5)
                # With the wrong order the request already owns the Python lock and waits for this DB.
                # With the correct order it cannot reach BEGIN until the claim releases both locks.
                request_db.wait(0.2)
            yield conn

    def claim():
        roles[threading.get_ident()] = "claim"
        w.ctx.db.execute("PRAGMA busy_timeout=300")
        return w.pub._claim_upload(first)

    def request():
        roles[threading.get_ident()] = "request"
        w.ctx.db.execute("PRAGMA busy_timeout=300")
        requesting.set()
        social.set_route("direct")
        w.pub.retry(held)

    monkeypatch.setattr(w.ctx.db, "tx", transaction)
    with ThreadPoolExecutor(max_workers=2) as pool:
        upload = pool.submit(claim)
        assert entered.wait(5)
        change = pool.submit(request)
        assert upload.result(timeout=5)["status"] == "uploading"
        change.result(timeout=5)
    assert w.row(held)["status"] == "queued"


def test_an_existing_verified_sender_is_registered_on_first_use_after_the_update(app):
    ctx, svc, c = app
    heartbeat(ctx, at=time.time() - 3600, owns=None)                    # older senders wrote only `networks`
    assert ctx.app_state.get("social.owned") is None
    assert svc.social.owned() == {"instagram", "tiktok"}
    assert ctx.app_state.get("social.owned") == ["instagram", "tiktok"]


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


def test_when_the_route_is_skipped_the_direct_route_is_free_and_a_gone_sender_keeps_it_off(app):
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
    assert "Metricool" in svc.pub.block_reason(row)                      # gone is not a handoff: still off
    c.put("/api/social/route", json={"route": "direct"})
    assert svc.pub.block_reason(row) is None                             # the user's skip frees it


def test_youtube_keeps_posting_and_a_waiting_instagram_delivery_stays_blocked_when_the_heartbeat_breaks(sb):
    w = World(sb)
    w.pub.factories["instagram"] = w.pub.factories["test"]              # both rows talk to the local provider
    w.pub.social_owned = Social(w.ctx).owned
    yt, ig = w.dest(provider="youtube"), w.dest(provider="instagram")
    first = make_clip(w.ctx)
    w.store.save_clip(first)
    early = w.pub.enqueue(first["clip_id"], ig, mode="auto")            # queued before the sender registered
    heartbeat(w.ctx)
    assert w.pub.social_owned() == {"instagram", "tiktok"}               # registered
    (w.ctx.paths.state / "social-sender.json").write_text(json.dumps({"name": "metricool", "owns": 42}),
                                                         encoding="utf-8")
    ids = w.pub.enqueue_clip(make_clip(w.ctx))
    w.drain()
    rows = {w.row(i)["destination_id"]: w.row(i)["status"] for i in ids}
    assert rows == {yt: "published"}                                    # no new Instagram delivery at all
    assert w.row(early)["status"] == "blocked"                          # the waiting one never reached the provider


def test_setup_needs_no_direct_instagram_or_tiktok_credentials_and_none_is_a_valid_choice(app):
    ctx, _, c = app
    from live_clipper.setup_service import build_questions
    qs = {q["id"]: q for q in build_questions({"gpu": {"kind": "none", "name": ""}, "cpu": {"cores": 4}, "ram_bytes": 16 * 1024**3, "disk": {"free_bytes": 500 * 1024**3}}, "store")}
    q = qs["posting.destinations"]
    assert "none" in [o["value"] for o in q["options"]] and q["validation"].get("min_items") == 1
    assert "Metricool" in q["note"] and q["intent"] == "Post my clips"
