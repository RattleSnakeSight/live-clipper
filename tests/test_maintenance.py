import os
from pathlib import Path

from ops_common import client, make_ctx, no_keyring  # noqa: F401

from live_clipper.maintenance import GB, MB, Maintenance, MaintenanceService

DAY = 86400
NOW = 1_800_000_000.0


class Env:
    def __init__(self, tmp_path, **settings):
        self.ctx = make_ctx()
        self.root = tmp_path / "store"
        self.root.mkdir()
        self.free = 100 * GB
        self.now = NOW
        base = {"storage.dir": str(self.root), "storage.cap_gb": 1}
        for k, v in {**base, **settings}.items():
            self.ctx.settings.set(k, v)
        self.m = Maintenance(self.ctx, clock=lambda: self.now, free_fn=lambda _p: self.free)

    def clip(self, cid, size_mb, posted_days_ago=None, state="published", original_mb=0):
        d = self.root / "clips" / f"clip{cid}"
        d.mkdir(parents=True)
        (d / "final_en.mp4").write_bytes(b"x" * ((size_mb - original_mb) * MB))
        if original_mb:
            (d / "source.ts").write_bytes(b"y" * (original_mb * MB))
        self.m.record_clip(cid, str(d / "final_en.mp4"))
        if posted_days_ago is not None:
            self.delivery(cid, state, posted_days_ago)
        return d

    def delivery(self, cid, state, days_ago=0):
        self.ctx.db.execute("INSERT OR REPLACE INTO maintenance_delivery VALUES(?,?,?,?,?)",
                            (f"d{cid}{state}", str(cid), "youtube", state, self.now - days_ago * DAY))


def kinds(acts):
    return [(a.action, a.clip_id) for a in acts]


def test_under_cap_nothing_to_do(tmp_path):
    e = Env(tmp_path)
    e.clip(1, 100, posted_days_ago=1)
    assert e.m.plan() == []


def test_unposted_clips_are_never_planned_or_deleted_even_far_over_cap(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 1})
    unposted = e.clip(1, 700)  # no delivery at all
    queued = e.clip(2, 700, posted_days_ago=100, state="queued")
    held = e.clip(3, 100, posted_days_ago=100, state="held")
    uploading = e.clip(4, 100, posted_days_ago=100, state="uploading")
    partial = e.clip(5, 100, posted_days_ago=100)
    e.delivery(5, "queued")  # one destination published, another still waiting
    failed_only = e.clip(6, 100, posted_days_ago=100, state="failed")
    assert e.m.plan() == []
    result = e.m.cleanup(dry_run=False)
    assert result["done"] == 0
    for d in (unposted, queued, held, uploading, partial, failed_only):
        assert d.exists()
    assert e.m.report()["over_cap"] and e.m.report()["protected_bytes"] > 0


def test_cap_enforcement_order(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 1, "storage.retention_days": 365})
    tmp_old = e.root / "clips" / "x.part.mp4"
    tmp_old.parent.mkdir(parents=True, exist_ok=True)
    tmp_old.write_bytes(b"t" * (50 * MB))
    os.utime(tmp_old, (NOW - 7200, NOW - 7200))
    tmp_fresh = e.root / "fresh.part.mp4"
    tmp_fresh.write_bytes(b"t" * MB)
    os.utime(tmp_fresh, (NOW - 10, NOW - 10))
    old = e.clip(1, 300, posted_days_ago=10, original_mb=200)
    mid = e.clip(2, 300, posted_days_ago=5, original_mb=200)
    new = e.clip(3, 300, posted_days_ago=1, original_mb=200)
    protected = e.clip(4, 300)
    # total about 1250 MB, cap 1024 MB: need roughly 226 MB
    acts = e.m.plan()
    assert kinds(acts)[0][0] == "delete_temp" and acts[0].path == str(tmp_old)
    assert tmp_fresh.as_posix() not in [Path(a.path).as_posix() for a in acts]
    # originals first, oldest first, and just enough: the 50 MB temp file plus one 200 MB original cover it
    assert kinds(acts)[1:] == [("delete_original", "1")]
    e.m.execute(acts)
    assert not (old / "source.ts").exists() and (old / "final_en.mp4").exists()
    assert (mid / "source.ts").exists() and (new / "source.ts").exists()
    assert protected.exists()
    assert e.m.plan() == []


def test_whole_clips_deleted_oldest_first_when_originals_not_enough(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 1, "storage.retention_days": 365})
    clips = [e.clip(i, 400, posted_days_ago=10 - i) for i in (1, 2, 3)]  # 1200 MB, clip 1 is oldest
    acts = e.m.plan()
    assert kinds(acts) == [("delete_clip", "1")]
    e.m.execute(acts)
    assert not clips[0].exists() and clips[1].exists() and clips[2].exists()


def test_clean_first_posted_clips_skips_originals(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 1, "storage.retention_days": 365,
                                "storage.clean_first": "posted_clips"})
    e.clip(1, 600, posted_days_ago=10, original_mb=500)
    e.clip(2, 600, posted_days_ago=1, original_mb=500)
    assert kinds(e.m.plan()) == [("delete_clip", "1")]


def test_clean_when_full_rule_off_never_deletes_for_cap(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 1, "storage.cleanup_rules": ["expire_posted"],
                                "storage.retention_days": 365})
    e.clip(1, 1500, posted_days_ago=1)
    assert e.m.plan() == []


def test_retention_moves_posted_to_trash_then_purges_after_keep_time(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 50, "storage.retention_days": 7})
    old = e.clip(1, 10, posted_days_ago=8)
    fresh = e.clip(2, 10, posted_days_ago=2)
    acts = e.m.plan()
    assert kinds(acts) == [("trash_clip", "1")]
    assert e.m.execute(acts)["done"] == 1
    assert not old.exists() and fresh.exists()
    trash = list((e.root / "trash").iterdir())
    assert len(trash) == 1 and trash[0].name == f"1-{int(NOW)}" and (trash[0] / "final_en.mp4").exists()
    assert e.m.plan() == []  # restorable until the keep time passes
    e.now += 169 * 3600
    acts = e.m.plan()
    assert ("purge_trash", "1") in kinds(acts)
    e.m.execute(acts)
    assert not trash[0].exists()


def test_trash_purged_early_under_cap_pressure_before_clips(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 1, "storage.retention_days": 7})
    e.clip(1, 600, posted_days_ago=8)
    e.m.execute(e.m.plan())  # clip 1 into trash, still counted
    e.clip(2, 600, posted_days_ago=1)
    acts = e.m.plan()
    assert kinds(acts) == [("purge_trash", "1")]


def test_critical_free_space_purges_trash_early(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 50, "storage.retention_days": 7})
    e.clip(1, 10, posted_days_ago=8)
    e.m.execute(e.m.plan())
    e.free = 1000 * MB  # below critical 1536 MB
    assert kinds(e.m.plan()) == [("purge_trash", "1")]


def test_protection_rechecked_at_execution(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 1, "storage.retention_days": 365})
    d = e.clip(1, 1500, posted_days_ago=10)
    acts = e.m.plan()
    assert acts
    e.delivery(1, "queued")  # a new destination is queued between planning and executing
    assert e.m.execute(acts)["done"] == 0 and d.exists()


def test_purge_refuses_paths_outside_trash(tmp_path):
    e = Env(tmp_path)
    from live_clipper.maintenance import Action
    victim = e.root / "precious.txt"
    victim.write_text("keep")
    e.m.execute([Action("purge_trash", str(victim), "1", 4, "x")])
    assert victim.exists()


def test_backpressure_admission_order(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 1})
    assert e.m.admit_clip(0) == (True, None)
    assert e.m.admit_clip(6)[1] == "too many clips in progress"
    e.clip(1, 1500)  # over cap (unposted, so it stays)
    assert "cap" in e.m.admit_clip(6)[1]  # cap is checked before the active-clip limit
    e.m.captures_paused = True
    assert "paused" in e.m.admit_clip(0)[1]
    e.free = 2000 * MB  # below lo (4096 MB): reported first
    assert e.m.admit_clip(0)[1] == "low free disk space"


def test_health_states_and_producer_pause(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 50})
    assert e.m.health()["state"] == "healthy"
    e.free = 3000 * MB
    assert e.m.health()["state"] == "backpressure"
    e.free = 1000 * MB
    assert e.m.health()["state"] == "degraded"
    assert e.m.enforce_producers() is True and e.m.captures_paused
    e.free = 3000 * MB
    assert e.m.enforce_producers() is None  # still below the resume mark
    e.free = 5000 * MB
    assert e.m.enforce_producers() is False and e.m.health()["state"] == "healthy"


def test_http_endpoints_and_bus_bookkeeping(tmp_path):
    ctx = make_ctx()
    root = tmp_path / "s"
    ctx.settings.set("storage.dir", str(root))
    ctx.settings.set("storage.cap_gb", 1)
    ctx.settings.set("storage.retention_days", 365)
    d = root / "clips" / "clip9"
    d.mkdir(parents=True)
    (d / "final_en.mp4").write_bytes(b"z" * (1500 * MB))
    c = client(ctx, MaintenanceService(free_fn=lambda _p: 100 * GB))
    with c:
        ctx.bus.publish("edit.clip_ready", {"clip_id": 9, "path": str(d / "final_en.mp4")})
        r = c.get("/api/storage").json()
        assert r["over_cap"] and r["breakdown"]["clips"] >= 1500 * MB and r["protected_bytes"] > 0
        assert r["next_cleanup"] == []
        ctx.bus.publish("publish.delivery", {"delivery_id": "a", "clip_id": 9, "destination": "youtube",
                                             "state": "published"})
        r = c.get("/api/storage").json()
        assert [a["action"] for a in r["next_cleanup"]] == ["delete_clip"]
        assert r["next_cleanup"][0]["path"] == "clips/clip9"
        dry = c.post("/api/storage/cleanup", json={}).json()
        assert dry["dry_run"] is True and d.exists()
        done = c.post("/api/storage/cleanup", json={"dry_run": False}).json()
        assert done["done"] == 1 and not d.exists()
