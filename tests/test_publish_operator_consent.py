"""Operator posting decisions survive reconnects, render completion and worker handoffs."""

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from publish_util import make_clip, sb, videos, world  # noqa: F401
from test_edit_service import wait_idle
from test_pipeline import clip as ready_clip

from live_clipper import formats, postprocess
from live_clipper.edit_service import EditService
from live_clipper.maintenance import Maintenance
from live_clipper.publish.base import PublishResult, ref_marker
from live_clipper.server import create_app


def test_reconnect_in_manual_mode_waits_for_explicit_post_per_delivery(world, sb):
    destination = world.dest()
    world.ctx.settings.set("publish.mode", "manual")
    world.pub.mark_needs_reconnect(world.store.q1("SELECT * FROM publish_destinations WHERE id=?", destination), "offline")
    first, second = [world.pub.enqueue_clip(make_clip(world.ctx))[0] for _ in range(2)]
    assert world.run(world.pub.connect(destination))["ok"]
    world.pub.recover()
    world.drain()
    assert videos(sb) == []
    assert world.row(first)["status"] == world.row(second)["status"] == "held"
    world.pub.post(first)
    world.drain()
    assert world.row(first)["status"] == "published"
    assert world.row(second)["status"] == "held"
    assert len(videos(sb)) == 1


def test_switch_to_manual_after_enqueue_requires_post(world, sb):
    world.dest()
    did, = world.pub.enqueue_clip(make_clip(world.ctx))
    world.ctx.settings.set("publish.mode", "manual")
    world.drain()
    assert videos(sb) == []
    assert world.row(did)["status"] == "held"
    assert world.row(did)["attempts"] == 0


def test_manual_mode_at_adapter_dispatch_holds_an_unsent_delivery(world, sb):
    dest = world.dest()
    did, = world.pub.enqueue_clip(make_clip(world.ctx))
    adapter = world.pub.adapter(world.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest))
    original_upload = adapter.upload

    async def switch_before_first_write(job, creds):
        world.ctx.settings.set("publish.mode", "manual")
        return await original_upload(job, creds)

    adapter.upload = switch_before_first_write
    world.run(world.pub.process(did))
    assert videos(sb) == []
    assert world.row(did)["status"] == "held"
    adapter.upload = original_upload
    world.pub.post(did)
    world.drain()
    assert world.row(did)["status"] == "published" and len(videos(sb)) == 1


@pytest.mark.parametrize("receipt", [True, False])
def test_reconcile_in_manual_mode_adopts_existing_receipt_or_holds(world, sb, receipt):
    dest = world.dest()
    did, = world.pub.enqueue_clip(make_clip(world.ctx))
    world.store.update("deliveries", did, status="uncertain", reconcile=1)
    world.ctx.settings.set("publish.mode", "manual")
    adapter = world.pub.adapter(world.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest))

    async def reconcile(job, creds):
        return PublishResult("published", post_id="fixture-post") if receipt else None

    adapter.reconcile = reconcile
    world.run(world.pub.process(did))
    row = world.row(did)
    assert videos(sb) == []
    assert (row["status"], row["provider_post_id"]) == (("published", "fixture-post") if receipt else ("held", ""))


@pytest.mark.parametrize("busy", [False, True])
def test_reject_never_hands_off_and_cancels_waiting_post_during_render(world, busy, monkeypatch):
    edit = EditService()
    world.ctx.services[:] = [edit]
    world.dest()
    events = []
    world.ctx.bus.subscribe("edit.*", lambda topic, p: events.append((topic, p)))
    with TestClient(create_app(world.ctx), base_url="http://127.0.0.1:8765",
                    headers={"Authorization": f"Bearer {world.ctx.token}"}) as client:
        _reject_during_render(client, edit, world, events, busy, monkeypatch)


def _reject_during_render(client, edit, world, events, busy, monkeypatch):
    engine = edit.engine
    channel = engine.store.add_channel("custom", "streamer_alpha", "")
    cid = ready_clip(engine.store, channel)
    folder = engine.clip_dir(cid)
    folder.mkdir(parents=True)
    (folder / "final_en.mp4").write_bytes(b"synthetic final")
    formats.create_rule(engine.store.db, {"id": "test-force", "name": "Test rule", "actions": {"decision": "force_qualify"}})
    engine.bus.subscribe("edit.clip_ready", lambda _, p: world.pub.enqueue_clip(p))
    m = Maintenance(world.ctx)
    publishing = SimpleNamespace(name="publish", pub=world.pub)
    # Attach storage after the fixture app started; keep its background sweep off.
    storage = SimpleNamespace(name="maintenance", m=m)
    edit.ctx.services.extend([storage, publishing])
    entered, release = threading.Event(), threading.Event()

    async def pending(*args):
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)

    monkeypatch.setattr(postprocess, "rerender_preview", pending)
    try:
        assert engine.publisher.on_language_ready(cid, "en")
        did = world.store.q1("SELECT id FROM publish_deliveries WHERE clip_id=?", cid)["id"]
        if busy:
            assert client.post(f"/api/clips/{cid}/render", json={"overrides": {"encode": {"render_crf": 20}}}).is_success
            assert entered.wait(3)
        events.clear()
        result = client.post(f"/api/clips/{cid}/review", json={"approved": False}).json()
        assert result["published"] == []
        assert result["selection"]["en"]["decision"] == "rejected"
        assert result["media"]["removed"] is (not busy)
        assert world.row(did)["status"] == "cancelled"
        release.set()
        wait_idle(client, cid)
        assert not engine.publisher.on_language_ready(cid, "en")
        assert not any(topic == "edit.clip_ready" for topic, _ in events)
    finally:
        release.set()
        edit.ctx.services[:] = [edit]


def test_upgrade_keeps_only_provable_post_approval(tmp_path):
    from live_clipper.db import REGISTRY, Database

    db = Database(tmp_path / "previous.db")
    db.conn().executescript(REGISTRY["publish"][0])
    db.execute("CREATE TABLE schema_migrations(namespace, version, applied_at, PRIMARY KEY(namespace, version))")
    db.execute("INSERT INTO schema_migrations VALUES ('publish',1,0)")
    released = {"from": "held", "to": "queued", "detail": "released by the user (manual post)"}
    waiting = {"from": "", "to": "paused", "detail": "enqueued (manual)"}
    moved = {"from": "queued", "to": "held", "detail": "waiting for a different account"}
    for i, history in enumerate(([released], [waiting], [released, moved]), start=1):
        db.execute("INSERT INTO publish_deliveries(clip_id,destination_id,language,idempotency_key,status,mode,"
                   "created_at,updated_at,history) VALUES(?,1,'en',?,'queued','manual',0,0,?)",
                   (i, str(i), json.dumps(history)))
    db.migrate()
    db.migrate()
    assert [r[0] for r in db.execute("SELECT post_approved FROM publish_deliveries ORDER BY id")] == [1, 0, 0]


def test_cancel_and_claim_have_one_winner_and_cannot_erase_a_receipt(world, sb):
    from concurrent.futures import ThreadPoolExecutor

    world.dest()
    did, = world.pub.enqueue_clip(make_clip(world.ctx))
    # Race normal scheduler and user requests, each with its own SQLite connection.
    start = threading.Barrier(2)

    def cancel():
        start.wait(3)
        try:
            world.pub.cancel(did)
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=1) as pool:
        cancelled = pool.submit(cancel)
        start.wait(3)
        world.run(world.pub.process(did))
        won = cancelled.result(timeout=3)
    world.drain()
    row = world.row(did)
    if won:
        assert row["status"] == "cancelled" and not videos(sb) and not row["provider_post_id"]
    else:
        assert row["status"] == "published" and len(videos(sb)) == 1 and row["provider_post_id"]
        with pytest.raises(ValueError):
            world.pub.cancel(did)
        assert world.row(did)["provider_post_id"] == row["provider_post_id"]


@pytest.mark.parametrize("action", ["cancel", "reject", "resume", "adopt", "legacy"])
def test_manual_mode_at_instagram_final_write_preserves_container_and_receipt(world, action):
    from live_clipper import gameplay
    from live_clipper.publish.instagram import InstagramAdapter
    from live_clipper.store import Store as ClipStore

    writes = []
    marker = None

    def provider(req):
        if req.method != "GET":
            writes.append(req.url.path)
            return httpx.Response(200, json={"id": "fixture-media"})
        if req.url.path.endswith("/content_publishing_limit"):
            # The operator changes mode while the adapter's last read is outstanding.
            world.pub.set_mode("manual")
            return httpx.Response(200, json={"data": []})
        if req.url.path.endswith("/media"):
            items = [{"id": "fixture-media", "caption": marker}] if action in ("adopt", "legacy") else []
            return httpx.Response(200, json={"data": items})
        return httpx.Response(200, json={"status_code": "PUBLISHED" if action == "legacy" else "FINISHED"})

    transport = httpx.MockTransport(provider)
    world.pub.factories["instagram"] = lambda settings, _: InstagramAdapter(settings, transport=transport)
    dest = world.pub.add_destination("instagram", "Test", {"account_info": {"account_id": "fixture-account"}})
    world.store.update("destinations", dest, status="connected", enabled=1, rights_ack_at=1)
    clips = ClipStore(world.ctx.db)
    channel = clips.add_channel("custom", "streamer_alpha", "")
    cid = ready_clip(clips, channel)
    clip = {**make_clip(world.ctx), "clip_id": cid, "channel_id": channel}
    folder = world.ctx.paths.media / "clips" / str(cid)
    folder.mkdir(parents=True)
    path = Path(clip["path"]).replace(folder / "final_en.mp4")
    clip["path"] = str(path)
    did, = world.pub.enqueue_clip(clip)
    ref = {"container": "fixture-container", "id": "fixture-container", "publish_started": action in ("legacy", "adopt")}
    world.store.update("deliveries", did, status="processing", reconcile=1, provider_ref=json.dumps(ref))
    marker = ref_marker(world.row(did)["idempotency_key"])

    if action == "legacy":
        world.store.update("deliveries", did, status="held", mode="manual")
        world.pub.recover()
        assert world.row(did)["status"] == "needs_confirmation"
        assert world.run(world.pub.recheck(did)) == "published"
        assert world.row(did)["provider_post_id"] == "fixture-media" and not writes
        return
    world.run(world.pub.process(did))
    row = world.row(did)
    assert not writes and json.loads(row["provider_ref"])["container"] == "fixture-container"
    world.pub.recover()
    if action == "adopt":
        assert row["status"] == "uncertain" and row["reconcile"]
        world.run(world.pub.process(did))
        assert world.row(did)["provider_post_id"] == "fixture-media"
        assert world.row(did)["status"] == "published" and not writes
    else:
        assert row["status"] == "held"
        if action == "resume":
            world.pub.post(did)
            assert world.row(did)["reconcile"]
            world.run(world.pub.process(did))  # resolve the old container before contacting the publish endpoint
            assert world.row(did)["status"] == "processing" and not writes
            world.run(world.pub.process(did))
            assert world.row(did)["status"] == "published" and world.row(did)["provider_post_id"] == "fixture-media"
            assert len(writes) == 1 and writes[0].endswith("/media_publish")
            return
        if action == "cancel":
            world.pub.cancel(did)
        else:
            gameplay.review(clips, cid, False)
            world.ctx.services.append(SimpleNamespace(name="publish", pub=world.pub))
            assert Maintenance(world.ctx).reject_clip(cid)["removed"] and not path.exists()
        assert world.row(did)["status"] == "cancelled"
        assert not world.pub.holds_provider_bytes(cid)
        assert json.loads(world.row(did)["provider_ref"])["container"] == "fixture-container" and not writes

@pytest.mark.parametrize("mid_call", [False, True])
def test_reconnected_account_waits_for_separate_post_after_prior_attempt_is_resolved(world, sb, mid_call):
    dest = world.dest(provider="youtube")
    did, = world.pub.enqueue_clip(make_clip(world.ctx))
    d = world.store.q1("SELECT * FROM publish_destinations WHERE id=?", dest)
    adapter = world.pub.adapter(d)
    original_info = world.run(adapter.account(world.pub.creds(d)))

    async def changed_account(creds):
        return {**original_info, "account": "Second synthetic account", "account_id": "fixture-second"}

    adapter.account = changed_account
    if mid_call:
        async def switch_before_write(job, creds):
            assert (await world.pub.connect(dest))["moved"]
            why = job.may_write()
            assert why
            return PublishResult("failed", permanent=True, expired=True, detail=why)
        adapter.upload = switch_before_write
        world.run(world.pub.process(did))
    else:
        world.store.update("deliveries", did, status="uncertain", reconcile=1)
        assert world.run(world.pub.connect(dest))["moved"]
    assert world.row(did)["status"] == "needs_confirmation"
    world.store.update("destinations", dest, enabled=1)
    world.pub.confirm_not_posted(did)
    world.pub.recover()
    assert world.row(did)["status"] == "held"
    world.drain()
    assert not videos(sb)
    world.pub.post(did)
    world.drain()
    assert len(videos(sb)) == 1 and world.row(did)["status"] == "published"


@pytest.mark.parametrize("legacy_mismatch", [False, True])
def test_tiktok_manual_resume_polls_only_the_new_upload_after_reconciling_the_partial(world, legacy_mismatch):
    from live_clipper.publish.tiktok import MB, TikTokAdapter

    uploads, chunks, lookups = [], [], []

    def provider(req):
        if req.url.path.endswith("/creator_info/query/"):
            data = {"privacy_level_options": ["SELF_ONLY"], "creator_username": "example_clips"}
        elif req.url.path.endswith("/video/init/"):
            pid = f"attempt-{len(uploads) + 1}"
            uploads.append(pid)
            data = {"publish_id": pid, "upload_url": f"https://upload.example.com/{pid}"}
        elif req.method == "PUT":
            chunks.append(req.url.path)
            if len(chunks) == 1:
                world.pub.set_mode("manual")
            return httpx.Response(201)
        elif req.url.path.endswith("/status/fetch/"):
            pid = json.loads(req.content)["publish_id"]
            lookups.append(pid)
            data = ({"status": "PROCESSING_UPLOAD", "uploaded_bytes": 5 * MB} if pid == "attempt-1"
                    else {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["fixture-post"]})
        else:
            raise AssertionError(f"unexpected mock request: {req.method} {req.url.path}")
        return httpx.Response(200, json={"data": data, "error": {"code": "ok"}})

    transport = httpx.MockTransport(provider)
    world.pub.factories["tiktok"] = lambda settings, _: TikTokAdapter(settings, transport=transport)
    dest = world.pub.add_destination("tiktok", "Test", {"mode": "direct", "chunk_size": 5 * MB})
    world.store.update("destinations", dest, status="connected", enabled=1, rights_ack_at=1)
    did, = world.pub.enqueue_clip(make_clip(world.ctx, size=10 * MB))
    world.run(world.pub.process(did))
    assert world.row(did)["status"] == "held" and chunks == ["/attempt-1"]
    world.pub.post(did)
    world.run(world.pub.process(did))
    assert world.row(did)["status"] == "processing" and lookups == ["attempt-1"]
    ref = json.loads(world.row(did)["provider_ref"])
    assert ref["id"] == ref["publish_id"] == "attempt-2"
    if legacy_mismatch:  # A processing row persisted by an earlier version before this poll.
        world.store.update("deliveries", did, provider_ref=json.dumps({**ref, "id": "attempt-1"}))
    world.run(world.pub.process(did))
    assert lookups == ["attempt-1", "attempt-2"]
    row = world.row(did)
    assert row["status"] == "private" and row["provider_post_id"] == "fixture-post"
    assert json.loads(row["provider_ref"])["id"] == "attempt-2"
    with pytest.raises(ValueError):
        world.pub.retry(did)
    world.pub.recover()
    world.drain()
    assert uploads == ["attempt-1", "attempt-2"] and chunks == ["/attempt-1", "/attempt-2", "/attempt-2"]
    assert lookups == ["attempt-1", "attempt-2"]
