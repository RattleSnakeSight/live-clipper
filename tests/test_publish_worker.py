"""Delivery state machine against the local test provider (real HTTP on 127.0.0.1)."""
import asyncio
import time

import httpx
import pytest
from publish_util import Clock, World, admin, make_clip, no_keyring, sb, videos, world  # noqa: F401

from live_clipper.publish.base import ref_marker
from live_clipper.publish.worker import Publisher


def key_of(w, did):
    return ref_marker(w.row(did)["idempotency_key"])


def test_happy_path(world, sb):
    dest = world.dest()
    clip = make_clip(world.ctx)
    did = world.pub.enqueue_clip(clip)[0]
    world.drain()
    d = world.row(did)
    assert d["status"] == "published" and d["outcome"] == "test"
    assert d["provider_post_id"] and d["provider_url"].startswith(sb.url) and d["attempts"] == 1
    assert [h["to"] for h in world.store.q1("SELECT history FROM publish_deliveries WHERE id=?", did)["history"]][:3] \
        == ["queued", "uploading", "processing"]
    vs = videos(sb, key_of(world, did))
    assert len(vs) == 1 and vs[0]["title"] == "Heist (en)"
    states = [e["state"] for e in world.events if e["delivery_id"] == did]
    assert states[-1] == "published" and all(e["test"] for e in world.events)
    assert world.notes == []                      # a test post never notifies, never counts as real
    assert dest


def test_double_enqueue_is_one_delivery(world, sb):
    world.dest()
    clip = make_clip(world.ctx)
    a = world.pub.enqueue_clip(clip)
    b = world.pub.enqueue_clip(clip)
    assert a == b and len(world.store.q("SELECT id FROM publish_deliveries")) == 1
    world.drain()
    assert world.pub.enqueue_clip(clip) == a
    world.drain()
    assert len(videos(sb)) == 1


def test_ambiguous_timeout_after_accept_reconciles_to_one_post(world, sb):
    world.dest({"timeout": 0.3})
    clip = make_clip(world.ctx)
    admin(sb, "/_admin/inject", {"op": "upload", "fail": "timeout_after_accept", "hang": 0.8})
    did = world.pub.enqueue_clip(clip)[0]
    world.drain()
    d = world.row(did)
    hist = world.store.q1("SELECT history FROM publish_deliveries WHERE id=?", did)["history"]
    assert "uncertain" in [h["to"] for h in hist] and d["status"] == "published" and d["attempts"] == 1
    assert any("reconciled" in h["detail"] for h in hist)
    assert len(videos(sb, key_of(world, did))) == 1


@pytest.mark.parametrize("provider_got_it", [True, False])
def test_restart_mid_upload_no_duplicate(world, sb, provider_got_it):
    dest = world.dest()
    did = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    world.store.update("deliveries", did, status="uploading", attempts=1, reconcile=1)    # worker died mid-upload
    if provider_got_it:
        tok = world.secrets.get("dest", dest)["access_token"]
        httpx.post(f"{sb.url}/v1/videos", params={"title": "x", "tag": key_of(world, did)}, content=b"bytes",
                   headers={"Authorization": f"Bearer {tok}"}).raise_for_status()
    pub2 = Publisher(world.store, world.ctx.settings, world.secrets)
    asyncio.run(pub2.drain())
    d = world.row(did)
    hist = world.store.q1("SELECT history FROM publish_deliveries WHERE id=?", did)["history"]
    assert d["status"] == "published"
    assert any(h["to"] == "uncertain" and "restarted" in h["detail"] for h in hist)
    assert len(videos(sb, key_of(world, did))) == 1
    assert d["attempts"] == (1 if provider_got_it else 2)


def test_uncertain_blocks_new_uploads_until_reconciled(world, sb):
    world.dest()
    d1 = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    d2 = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    world.store.update("deliveries", d1, status="uncertain", reconcile=1, next_attempt_at=10**12)
    assert all(r["id"] != d2 for r in world.pub._due())
    world.store.update("deliveries", d1, next_attempt_at=0)
    world.drain()
    assert world.row(d1)["status"] == world.row(d2)["status"] == "published"


def test_429_honors_retry_after(sb):
    clock = Clock()
    w = World(sb, clock=clock)
    w.dest()
    admin(sb, "/_admin/inject", {"op": "upload", "fail": "429", "retry_after": 30})
    did = w.pub.enqueue_clip(make_clip(w.ctx))[0]
    w.drain(horizon=0)
    d = w.row(did)
    assert d["status"] == "queued" and d["next_attempt_at"] == pytest.approx(clock.t + 30)
    clock.t += 29
    w.drain(horizon=0)
    assert w.row(did)["status"] == "queued"
    clock.t += 2
    w.drain(horizon=0)
    assert w.row(did)["status"] == "published" and len(videos(sb)) == 1


def test_401_refreshes_then_succeeds(world, sb):
    dest = world.dest()
    old = world.secrets.get("dest", dest)["access_token"]
    did = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    admin(sb, "/_admin/expire_tokens")
    world.drain()
    assert world.row(did)["status"] == "published"
    assert world.secrets.get("dest", dest)["access_token"] != old
    assert world.store.q1("SELECT status FROM publish_destinations WHERE id=?", dest)["status"] == "connected"


def test_revoked_refresh_token_needs_reconnect_holds_and_notifies(world, sb):
    dest = world.dest(provider="youtube")
    # the user revoked access: the stored refresh token no longer works at the provider
    world.secrets.update("dest", dest, {"refresh_token": "sbx_rt_revoked_by_user"})
    d1 = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    admin(sb, "/_admin/expire_tokens")
    world.drain()
    ds = world.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest)
    assert ds["status"] == "needs_reconnect" and "Reconnect" in ds["status_detail"]
    assert world.row(d1)["status"] == "paused"
    assert any("Reconnect" in t for t, _ in world.notes)
    d2 = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    assert world.row(d2)["status"] == "paused" and videos(sb) == []
    world.pub.replace_tokens(dest, admin(sb, "/_admin/issue_token"))      # user reconnects
    assert world.run(world.pub.connect(dest))["ok"]
    world.drain()
    assert world.row(d1)["status"] == world.row(d2)["status"] == "published" and len(videos(sb)) == 2


def test_no_refresh_token_needs_reconnect(sb):
    w = World(sb)
    dest = w.dest(refresh=False)
    did = w.pub.enqueue_clip(make_clip(w.ctx))[0]
    admin(sb, "/_admin/expire_tokens")
    w.drain()
    assert w.store.q1("SELECT status FROM publish_destinations WHERE id=?", dest)["status"] == "needs_reconnect"
    assert w.row(did)["status"] == "paused"


def test_unverifiable_reconcile_needs_confirmation_then_confirm_not_posted(world, sb):
    world.dest()
    did = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    world.store.update("deliveries", did, status="uncertain", reconcile=1, attempts=1)
    admin(sb, "/_admin/inject", {"op": "list", "fail": "500"})
    world.drain()
    assert world.row(did)["status"] == "needs_confirmation" and videos(sb) == []
    world.pub.confirm_not_posted(did)
    world.drain()
    assert world.row(did)["status"] == "published" and len(videos(sb)) == 1


def test_confirm_posted_closes_delivery_without_upload(world, sb):
    world.dest()
    did = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    world.store.update("deliveries", did, status="needs_confirmation", reconcile=1)
    world.pub.confirm_posted(did, "https://example.com/post/1")
    d = world.row(did)
    assert d["status"] == "published" and d["provider_url"] == "https://example.com/post/1"
    world.drain()
    assert videos(sb) == []


def test_eight_reconcile_attempts_then_needs_confirmation(world, sb):
    world.dest()
    did = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    world.store.update("deliveries", did, status="uncertain", reconcile=1, attempts=1,
                       provider_ref='{"id": "gone-but-known"}')
    admin(sb, "/_admin/inject", {"op": "status", "fail": "500", "times": 50})
    # a known provider id whose status cannot be read never becomes `failed`
    for _ in range(40):
        world.drain(horizon=0, timeout=2)
        world.store.update("deliveries", did, next_attempt_at=0) if world.row(did)["status"] != "needs_confirmation" else None
        if world.row(did)["status"] == "needs_confirmation":
            break
    assert world.row(did)["status"] == "needs_confirmation"
    assert world.row(did)["status"] != "failed"


def test_permanent_failure_stops(sb):
    w = World(sb, max_attempts=2)
    w.dest()
    did = w.pub.enqueue_clip(make_clip(w.ctx))[0]
    admin(sb, "/_admin/inject", {"op": "upload", "fail": "500", "times": 5})
    w.drain()
    d = w.row(did)
    assert d["status"] == "failed" and d["attempts"] == 2 and "HTTP 500" in d["last_error"]
    w.pub.retry(did)
    w.drain()
    assert w.row(did)["status"] == "failed"
    sb.state["inject"] = []
    w.pub.retry(did)
    w.drain()
    assert w.row(did)["status"] == "published" and len(videos(sb)) == 1


def test_permanent_400_fails_without_retry(world, sb):
    dest = world.dest()
    did = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    world.store.update("destinations", dest, settings={**world.store.q1(
        "SELECT settings FROM publish_destinations WHERE id=?", dest)["settings"]})
    # the test provider answers an empty upload with HTTP 400, which is a permanent client error
    clip = world.store.clip(world.row(did)["clip_id"], "en")
    open(clip["path"], "wb").write(b"")
    world.drain()
    d = world.row(did)
    assert d["status"] == "failed" and d["attempts"] <= 1
    assert videos(sb) == []


def test_backoff_formula(world):
    dest = {"settings": {}}
    for attempts, hi in [(1, 10), (2, 20), (3, 40), (8, 900), (20, 900)]:
        v = [world.pub._backoff(dest, attempts) for _ in range(50)]
        assert min(v) >= hi * 0.5 and max(v) <= hi


def test_manual_mode_holds_then_user_post(world, sb):
    world.dest()
    world.ctx.settings.set("publish.mode", "manual")
    did = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    assert world.row(did)["status"] == "held"
    world.drain()
    assert world.row(did)["status"] == "held" and videos(sb) == []
    world.pub.post(did)
    world.drain()
    d = world.row(did)
    assert d["status"] == "published" and d["mode"] == "manual" and len(videos(sb)) == 1


def test_nothing_posted_without_connected_enabled_acknowledged_destination(world, sb):
    clip = make_clip(world.ctx)
    assert world.pub.enqueue_clip(clip) == []                          # no destination at all
    d = world.dest(ack=False, enabled=False)
    world.store.update("destinations", d, provider="youtube")          # a real provider: gates apply
    assert world.pub.enqueue_clip(clip) == []                          # not acknowledged, not enabled
    world.store.update("destinations", d, rights_ack_at=time.time())
    assert world.pub.enqueue_clip(clip) == []                          # acknowledged but disabled
    world.store.update("destinations", d, enabled=1, status="disconnected")
    assert world.pub.enqueue_clip(clip) == []                          # disconnected
    assert videos(sb) == []


def test_channel_platform_allowlist(world, sb):
    world.dest(provider="youtube")
    world.ctx.settings.set("publish.channel_platforms", {"streamer_alpha": ["tiktok"]})
    assert world.pub.enqueue_clip(make_clip(world.ctx)) == []
    assert len(world.pub.enqueue_clip(make_clip(world.ctx, channel_id="streamer_beta"))) == 1


def test_disabled_destination_blocks_then_resumes(world, sb):
    d = world.dest(provider="youtube")
    did = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    world.store.update("destinations", d, enabled=0)
    world.pub.block_destination(d)
    world.drain()
    assert world.row(did)["status"] == "blocked" and videos(sb) == []
    world.store.update("destinations", d, enabled=1)
    world.pub.unblock_allowed()
    world.drain()
    assert world.row(did)["status"] == "published"


def test_cancel_only_before_provider_involved(world):
    world.dest()
    world.ctx.settings.set("publish.mode", "manual")
    did = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    world.pub.cancel(did)
    assert world.row(did)["status"] == "cancelled"
    d2 = world.pub.enqueue_clip(make_clip(world.ctx))[0]
    world.store.update("deliveries", d2, status="uncertain", reconcile=1)
    with pytest.raises(ValueError):
        world.pub.cancel(d2)


def test_daily_quota_defers_upload(world, sb):
    world.dest({"daily_limit": 1}, provider="youtube")
    a, b = (world.pub.enqueue_clip(make_clip(world.ctx))[0] for _ in range(2))
    world.drain(horizon=0)
    assert world.row(a)["status"] == "published"
    rb = world.row(b)
    assert rb["status"] == "queued" and rb["next_attempt_at"] > time.time() and len(videos(sb)) == 1


def test_run_loop_processes_in_background(world, sb):
    world.dest()
    clip = make_clip(world.ctx)

    async def go():
        pub = Publisher(world.store, world.ctx.settings, world.secrets)
        await pub.start()
        did = pub.enqueue_clip(clip)[0]
        for _ in range(200):
            if world.row(did)["status"] == "published":
                break
            await asyncio.sleep(0.02)
        await pub.stop()
        return did
    assert world.row(asyncio.run(go()))["status"] == "published"


def test_test_destination_is_labelled_and_never_real(tmp_path):
    from live_clipper.publish.sandbox_server import shutdown_sandbox
    from live_clipper.publish.worker import TEST_LABEL
    w = World(None, sandbox_dir=tmp_path / "sandbox")
    did = w.run(w.pub.ensure_test_destination())
    assert w.run(w.pub.ensure_test_destination()) == did
    d = w.store.q1("SELECT * FROM publish_destinations WHERE id=?", did)
    assert d["status"] == "connected" and "nothing is posted" in d["label"] and d["label"] == TEST_LABEL
    did2 = w.pub.enqueue_clip(make_clip(w.ctx))[0]
    w.drain(timeout=15)
    r = w.row(did2)
    assert r["status"] == "published" and r["outcome"] == "test"
    assert all(e["test"] for e in w.events) and w.notes == []
    shutdown_sandbox()
