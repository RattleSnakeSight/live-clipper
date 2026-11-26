"""Channel retirement preserves clip attribution and never grants an old clip a new channel's permissions."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from live_clipper.config import Settings
from live_clipper.db import KV, REGISTRY, Database
from live_clipper.edit_service import EditService
from live_clipper.engine import Engine
from live_clipper.monitor_service import MonitorService
from live_clipper.publish.store import Store as PublishStore
from live_clipper.publish.worker import Publisher
from live_clipper.store import Store


def services(tmp_path, db):
    store = Store(db)
    config = Settings(data_dir=tmp_path / "scratch")
    engine = Engine(config, store)
    monitor = MonitorService()
    monitor.engine, monitor.settings, monitor.store = engine, config, store
    edit = EditService()
    edit.engine = engine.edit
    app = FastAPI()
    app.include_router(monitor.router)
    app.include_router(edit.router)
    return TestClient(app), engine


def test_retired_channel_survives_delete_readd_and_restart(tmp_path):
    path = tmp_path / "fixture.sqlite"
    db = Database(path)
    db.migrate()
    api, engine = services(tmp_path, db)
    options = {"url": "twitch.tv/creator", "enabled": False, "display_name": "Original Creator",
               "format_id": "gta-vi", "publish_platforms": ["youtube"]}
    with api:
        cid = api.post("/api/channels", json=options).json()["id"]
        clip_id = engine.store.create_clip(cid, "fixture", "manual", {})
        before = engine.selection.publish_fields(engine.store.clip(clip_id), "en")
        assert before["destinations"] == ["youtube"] and "Original Creator" in before["description"]
        assert api.delete(f"/api/channels/{cid}").status_code == 204
        assert api.get("/api/channels").json() == []
        assert api.get(f"/api/channels/{cid}").status_code == 404
        assert api.put(f"/api/channels/{cid}", json={"display_name": "Changed"}).status_code == 404
        assert api.post(f"/api/channels/{cid}/arm").status_code == 404
        for login in ("replacement", "creator"):
            result = api.post("/api/channels", json={**options, "url": f"twitch.tv/{login}",
                              "display_name": "Current", "publish_platforms": ["tiktok"]})
            assert result.status_code == 201 and result.json()["id"] > cid
        assert api.post("/api/channels", json=options).status_code == 409
    reopened = Database(path)
    reopened.migrate()
    api, engine = services(tmp_path, reopened)
    with api:
        assert len(api.get("/api/channels").json()) == 2
        assert api.get(f"/api/clips/{clip_id}").json()["channel_name"] == "Original Creator"
        assert api.get("/api/clips").json()[0]["channel_name"] == "Original Creator"
        assert engine.selection.publish_fields(engine.store.clip(clip_id), "en") == before


def test_v2_upgrade_reserves_orphans_and_quarantines_proven_id_reuse(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    db = Database(path)
    with monkeypatch.context() as old:
        old.setitem(REGISTRY, "monitor", REGISTRY["monitor"][:2])
        db.migrate()
        store = Store(db)
        cid = store.add_channel("twitch", "new_owner", publish_platforms=["youtube"])
        store.update("channels", cid, created_at=200)
        older = store.create_clip(cid, "legacy-older", "manual", {}, created_at=100)
        newer = store.create_clip(cid, "legacy-newer", "manual", {}, created_at=300)
        orphan = store.create_clip(40, "missing", "manual", {})
        KV(db, "settings_kv").set("publish.channel_platforms", {"70": ["youtube"]})
        cache = PublishStore(db)
        for clip_id, channel_id in ((older, cid), (newer, cid), (orphan, 40), (1000, 60)):
            cache.save_clip({"clip_id": clip_id, "channel_id": channel_id, "language": "en",
                             "title": "Stored title", "destinations": ["youtube"]})

    db = Database(path)
    db.migrate()
    db.migrate()
    api, engine = services(tmp_path, db)
    cache = PublishStore(db)
    publisher = Publisher(cache, KV(db, "settings_kv"), None)
    with api:
        assert [r["id"] for r in api.get("/api/channels").json()] == [cid]
        next_id = api.post("/api/channels", json={"url":"kick.com/another", "enabled":False}).json()["id"]
        assert next_id > 70
        assert engine.store.clip(older)["channel_id"] != cid
        for clip_id in (older, orphan):
            clip = engine.store.clip(clip_id)
            assert api.get(f"/api/clips/{clip_id}").json()["channel_name"] == "Unknown historical channel"
            assert engine.selection.publish_fields(clip, "en")["destinations"] == []
            assert api.post(f"/api/clips/{clip_id}/publish", json={}).status_code == 409
        for clip_id in (older, orphan, 1000):
            payload = cache.clip(clip_id)
            assert payload["title"] == "Stored title" and payload["destinations"] == []
            assert publisher.platform_reason(payload, {"provider": "youtube"})
        assert cache.clip(older)["channel_id"] == engine.store.clip(older)["channel_id"]
        assert engine.store.clip(newer)["channel_id"] == cid
        assert api.get(f"/api/clips/{newer}").json()["channel_name"] == "new_owner"
        assert cache.clip(newer)["destinations"] == ["youtube"]


def test_retiring_identity_cannot_be_rearmed_during_stop(tmp_path, monkeypatch):
    db = Database(tmp_path / "fixture.sqlite")
    db.migrate()
    api, engine = services(tmp_path, db)
    stopping, release = threading.Event(), threading.Event()
    original = engine.disarm

    async def held_stop(cid):
        import asyncio

        stopping.set()
        await asyncio.to_thread(release.wait, 5)
        await original(cid)

    monkeypatch.setattr(engine, "disarm", held_stop)
    with api, ThreadPoolExecutor(max_workers=1) as pool:
        options = {"url": "twitch.tv/creator", "enabled": False}
        cid = api.post("/api/channels", json=options).json()["id"]
        deleted = pool.submit(api.delete, f"/api/channels/{cid}")
        try:
            assert stopping.wait(2)
            assert api.post(f"/api/channels/{cid}/arm").status_code == 404
            with pytest.raises(KeyError):
                engine.arm(cid)
            replacement = api.post("/api/channels", json=options)
            assert replacement.status_code == 201 and replacement.json()["id"] != cid
        finally:
            release.set()
        assert deleted.result(timeout=4).status_code == 204


@pytest.mark.parametrize("from_version", [2, 3])
def test_upgrade_restores_only_monitored_finals_with_missing_channels(tmp_path, monkeypatch, from_version):
    path = tmp_path / "old-publisher.sqlite"
    db = Database(path)
    with monkeypatch.context() as old:
        old.setitem(REGISTRY, "monitor", REGISTRY["monitor"][:from_version])
        db.migrate()
        store = Store(db)
        channel = store.add_channel("twitch", "known", publish_platforms=["youtube", "tiktok"])
        store.update("channels", channel, created_at=0.5)  # precedes the retired clip
        live = store.create_clip(channel, "kept", "manual", {}, files={"final": {"en": "version-two.mp4"}})
        missing = store.create_clip(49, "orphan", "manual", {})
        ledger = 98
        db.execute("""INSERT INTO monitor_clip_ledger
            (clip_id, channel_id, creator_key, moment_id, moment_creator, dedupe_key,
             clip_created_at, confirmed_at, retired_at)
            VALUES (?,?,'original','m98','original','retired',1,2,3)""", (ledger, channel))
        cache = PublishStore(db)

        def save(clip_id, language, filename, **fields):
            payload = {"clip_id": clip_id, "language": language, "path": filename, "title": "Saved", **fields}
            cache.save_clip(payload)
            return payload

        save(live, "en", f"C:\\old\\clips\\{live}\\version-two.mp4", destinations=["instagram", "youtube"])
        save(ledger, "en", f"/old/clips/{ledger}/final_en.mp4")
        save(missing, "en", f"/old/clips/{missing}/final_en.mp4")
        collided = save(live, "es", "/manual/standalone.mp4")
        standalone = save(999, "en", "/manual/independent.mp4", destinations=["instagram"])
        named = save(str(live), "en", f"/old/clips/{live}/version-two.mp4")

    db = Database(path)
    db.migrate()
    db.migrate()
    cache = PublishStore(db)
    pub = Publisher(cache, KV(db, "settings_kv"), None)
    recovered = cache.clip(live)
    assert recovered["channel_id"] == channel and recovered["destinations"] == ["youtube"]
    retired = cache.clip(ledger)
    assert retired["channel_id"] == channel and retired["destinations"] == ["youtube", "tiktok"]
    assert cache.clip(missing)["channel_id"] == 49 and cache.clip(missing)["destinations"] == []
    assert pub.platform_reason(recovered, {"provider": "instagram"})
    assert pub.platform_reason(cache.clip(missing), {"provider": "youtube"})
    assert cache.clip(live, "es") == collided
    assert cache.clip(999) == standalone
    assert cache.clip(str(live)) == named
    stored = db.execute("SELECT channel_id FROM publish_clips WHERE clip_id=? AND language='en'", (live,))
    assert stored.fetchone()[0] == channel
