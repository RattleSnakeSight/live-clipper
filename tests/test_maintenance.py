import os
import shutil
import threading
import types
from pathlib import Path

import pytest
from ops_common import client, make_ctx, no_keyring  # noqa: F401

import live_clipper.publish.store  # noqa: F401  (publishing's tables, read for what counts as posted)
import live_clipper.store  # noqa: F401  (the clip table, read for what the user rejected)
from live_clipper.maintenance import GB, MB, PLAN_TTL_S, Action, Maintenance, MaintenanceService, PlanError
from live_clipper.store import Store as ClipStore

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

    def clip(self, cid, size_mb, posted_days_ago=None, state="published", original_mb=0, dest="youtube"):
        d = self.root / "clips" / f"clip{cid}"
        d.mkdir(parents=True)
        (d / "final_en.mp4").write_bytes(b"x" * ((size_mb - original_mb) * MB))
        if original_mb:
            (d / "source.ts").write_bytes(b"y" * (original_mb * MB))
        self.m.record_clip(cid, str(d / "final_en.mp4"))
        if posted_days_ago is not None:
            self.delivery(cid, state, posted_days_ago, dest)
        return d

    def delivery(self, cid, state, days_ago=0, dest="youtube"):
        self.ctx.db.execute("INSERT OR REPLACE INTO maintenance_delivery VALUES(?,?,?,?,?)",
                            (f"d{cid}{state}{dest}", str(cid), dest, state, self.now - days_ago * DAY))


def kinds(acts):
    return [(a.action, a.clip_id) for a in acts]


def confirm(m):
    """Clean up now the way the button does it: a preview, then the confirmation that names it."""
    return m.cleanup(dry_run=False, manual=True, plan_id=m.cleanup(dry_run=True, manual=True)["plan_id"])


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
        assert dry["dry_run"] is True and dry["plan_id"] and d.exists()
        bare = c.post("/api/storage/cleanup", json={"dry_run": False})     # an older tab: no preview named
        assert bare.status_code == 409 and bare.json()["detail"]["code"] == "plan_required" and d.exists()
        done = c.post("/api/storage/cleanup", json={"dry_run": False, "plan_id": dry["plan_id"]}).json()
        assert done["done"] == 1 and not d.exists()


def _old(p, payload=b"t"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    os.utime(p, (NOW - 7200, NOW - 7200))
    return p


def test_temp_sweep_only_touches_app_subfolders(tmp_path):
    e = Env(tmp_path)
    unrelated = [_old(e.root / "hook_x.txt"), _old(e.root / "a.part.mp4"),
                 _old(e.root / "Documents" / "hook_notes.txt"), _old(e.root / "other" / "audio_pre.wav")]
    mine = [_old(e.root / "clips" / "run1" / "b.part.mp4"), _old(e.root / "tmp" / "hook_1.txt")]
    result = e.m.cleanup(dry_run=False)
    assert result["done"] == 2
    assert all(p.exists() for p in unrelated) and not any(p.exists() for p in mine)


def test_delete_temp_refuses_paths_outside_sweep_dirs(tmp_path):
    e = Env(tmp_path)
    victim = _old(e.root / "hook_x.txt")
    outside = _old(tmp_path / "elsewhere" / "a.part.mp4")
    not_temp = _old(e.root / "clips" / "precious.docx")
    e.m.execute([Action("delete_temp", str(p), None, 1, "x") for p in (victim, outside, not_temp)])
    assert victim.exists() and outside.exists() and not_temp.exists()


def test_temp_sweep_skips_links_and_junctions(tmp_path):
    import subprocess
    e = Env(tmp_path)
    target = tmp_path / "outside"
    survivor = _old(target / "hook_keep.txt")
    (e.root / "clips").mkdir()
    link = e.root / "clips" / "linked"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    e.m.cleanup(dry_run=False)
    assert survivor.exists()
    e.m.execute([Action("delete_temp", str(link / "hook_keep.txt"), None, 1, "x")])
    assert survivor.exists()


def test_storage_root_changes_at_restart(tmp_path):
    e = Env(tmp_path)
    old_clip = e.clip(1, 1, posted_days_ago=30)
    chosen = tmp_path / "new-media"
    chosen.mkdir()
    e.ctx.settings.set("storage.dir", str(chosen))
    report = e.m.report()
    assert report["dir"] == str(e.root)
    assert report["configured_dir"] == str(chosen)
    restarted = Maintenance(e.ctx)
    assert restarted.report()["dir"] == str(chosen)
    assert not restarted.plan()
    assert old_clip.exists()


# ---- what counts as posted: the test destination posts nothing, so it never makes media removable

def test_a_test_destination_post_never_makes_a_clip_removable(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 0.01, "storage.retention_days": 1})
    d = e.clip(1, 15, posted_days_ago=30, dest="test")             # over the cap and past the retention time
    e.m.record_delivery({"delivery_id": "s", "clip_id": 1, "destination": "sandbox", "state": "published",
                         "test": True})                            # marked as a test by publishing
    assert e.m.posted_at("1") is None
    assert e.m.plan() == [] and e.m.plan(manual=True) == []
    assert e.m.cycle()["done"] == 0 and confirm(e.m)["done"] == 0 and d.exists()
    assert e.m.execute([Action("delete_clip", str(d), "1", 15 * MB, "disk cap reached")])["done"] == 0
    assert d.exists() and e.m.report()["protected_bytes"] >= 15 * MB
    e.delivery(1, "published", 30)                                 # a real destination posted it as well
    assert e.m.posted_at("1") == NOW - 30 * DAY
    assert ("delete_clip", "1") in kinds(e.m.plan())


def test_publishings_own_table_decides_and_ignores_the_test_destination(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 0.01, "storage.retention_days": 1})
    d = e.clip(1, 15, posted_days_ago=30, dest="test")
    db = e.ctx.db

    def destination(provider):
        return db.execute("INSERT INTO publish_destinations(provider, label, created_at) VALUES(?,?,0)",
                          (provider, provider)).lastrowid

    def deliver(dest_id, status, n):
        db.execute("INSERT INTO publish_deliveries(clip_id, destination_id, language, idempotency_key, "
                   "status, created_at, updated_at) VALUES(1,?,'en',?,?,?,?)",
                   (dest_id, f"k{n}", status, NOW - 30 * DAY, NOW - (30 - n) * DAY))

    deliver(destination("test"), "published", 1)
    assert e.m.posted_at("1") is None and e.m.plan() == [] and e.m.cycle()["done"] == 0 and d.exists()
    youtube = destination("youtube")
    deliver(youtube, "published", 2)
    assert e.m.posted_at("1") == NOW - 28 * DAY                    # the real post's time, not the test's
    deliver(destination("instagram"), "uploading", 3)  # the same clip is in flight at another destination
    assert e.m.posted_at("1") is None and e.m.plan() == [] and d.exists()


# ---- Clean up now runs exactly what the user confirmed in the preview

def test_a_confirmation_runs_only_the_previewed_candidates(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 50})
    a = e.clip(1, 2, posted_days_ago=1)
    pre = e.m.cleanup(dry_run=True, manual=True)
    assert [x["clip_id"] for x in pre["actions"]] == ["1"] and pre["plan_id"]
    assert pre["expires_at"] == NOW + PLAN_TTL_S and a.exists()
    b = e.clip(2, 2, posted_days_ago=1)                            # became removable after the preview
    out = e.m.cleanup(dry_run=False, manual=True, plan_id=pre["plan_id"])
    assert out["done"] == 1 and not a.exists() and b.exists()
    assert [(r["clip_id"], r["outcome"]) for r in out["results"]] == [("1", "done")]
    assert out["freed_bytes"] == 2 * MB and out["summary"]["posted"] == {"items": 1, "bytes": 2 * MB}
    assert out["not_confirmed"] == {"items": 1, "bytes": 2 * MB} and out["kept"] == 0


def test_a_folder_or_file_replaced_after_the_preview_is_left_alone(tmp_path):
    e = Env(tmp_path)
    d = e.clip(1, 2, posted_days_ago=1)
    temp = _old(e.root / "tmp" / "hook_1.txt", b"old")
    pre = e.m.cleanup(dry_run=True, manual=True)
    assert sorted(x["action"] for x in pre["actions"]) == ["delete_clip", "delete_temp"]
    shutil.rmtree(d)                                               # the same path now holds other files
    d.mkdir()
    (d / "final_en.mp4").write_bytes(b"n" * (3 * MB))
    temp.unlink()
    _old(temp, b"a new file")                                      # same name, same age, another file
    out = e.m.cleanup(dry_run=False, manual=True, plan_id=pre["plan_id"])
    assert out["done"] == 0 and out["freed_bytes"] == 0
    assert (d / "final_en.mp4").stat().st_size == 3 * MB and temp.read_bytes() == b"a new file"
    assert {(r["outcome"], r["why"]) for r in out["results"]} == {("kept", "it changed since the preview")}


def test_a_confirmed_plan_never_turns_into_a_bigger_action(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 0.01, "storage.retention_days": 365})
    d = e.clip(1, 12, posted_days_ago=1, original_mb=4)
    pre = e.m.cleanup(dry_run=True)  # The timer removes the original first.
    assert [(x["action"], x["clip_id"]) for x in pre["actions"]] == [("delete_original", "1")]
    e.ctx.settings.set("storage.clean_first", "posted_clips")      # now a plan would delete the whole clip
    out = e.m.cleanup(dry_run=False, plan_id=pre["plan_id"])
    assert out["done"] == 0 and (d / "source.ts").exists() and (d / "final_en.mp4").exists()
    assert out["results"][0]["why"] == "it is no longer eligible" and out["not_confirmed"]["items"] == 1


def test_a_confirmed_plan_that_frees_an_original_and_then_its_clip_runs_both(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 0.01, "storage.retention_days": 365})
    d = e.clip(1, 15, posted_days_ago=1, original_mb=2)
    pre = e.m.cleanup(dry_run=True)
    assert [x["action"] for x in pre["actions"]] == ["delete_original", "delete_clip"]
    out = e.m.cleanup(dry_run=False, plan_id=pre["plan_id"])
    assert out["done"] == 2 and not d.exists() and out["freed_bytes"] == 15 * MB


def test_a_confirmed_item_that_gained_a_protection_is_kept_and_says_why(tmp_path):
    e = Env(tmp_path)
    a = e.clip(1, 2, posted_days_ago=1)
    b = e.clip(2, 2, posted_days_ago=1)
    pre = e.m.cleanup(dry_run=True, manual=True)
    assert sorted(x["clip_id"] for x in pre["actions"]) == ["1", "2"]
    e.delivery(1, "queued")                                        # a new destination waits for clip 1
    e.ctx.services.append(types.SimpleNamespace(name="publish", pub=types.SimpleNamespace(
        holds_provider_bytes=lambda cid: [{"id": 7}] if cid == 2 else [])))   # an upload may resume clip 2
    out = e.m.cleanup(dry_run=False, manual=True, plan_id=pre["plan_id"])
    assert out["done"] == 0 and a.exists() and b.exists() and out["kept"] == 2


def test_the_execution_recheck_keeps_a_clip_held_after_the_fresh_plan(tmp_path, monkeypatch):
    """A hold that appears between the fresh plan and the removal itself still keeps the files."""
    e = Env(tmp_path)
    d = e.clip(1, 2, posted_days_ago=1)
    pre = e.m.cleanup(dry_run=True, manual=True)
    real_plan = Maintenance.plan

    def plan_then_hold(self, *a, **kw):
        acts = real_plan(self, *a, **kw)
        monkeypatch.setattr(Maintenance, "publisher_holds", lambda self, cid: True)
        return acts
    monkeypatch.setattr(Maintenance, "plan", plan_then_hold)
    out = e.m.cleanup(dry_run=False, manual=True, plan_id=pre["plan_id"])
    assert out["done"] == 0 and d.exists()
    assert out["results"][0]["why"] == "an upload of this clip may still need its files"


def test_a_preview_is_confirmed_once_within_its_time_and_never_after_a_restart(tmp_path):
    e = Env(tmp_path)
    a = e.clip(1, 2, posted_days_ago=1)
    pid = e.m.cleanup(dry_run=True, manual=True)["plan_id"]
    assert e.m.cleanup(dry_run=False, manual=True, plan_id=pid)["done"] == 1 and not a.exists()
    b = e.clip(2, 2, posted_days_ago=1)
    with pytest.raises(PlanError) as err:                          # pressed again: nothing more is deleted
        e.m.cleanup(dry_run=False, manual=True, plan_id=pid)
    assert err.value.code == "plan_used" and b.exists()
    old = e.m.cleanup(dry_run=True, manual=True)["plan_id"]
    e.now += PLAN_TTL_S + 1
    with pytest.raises(PlanError) as err:
        e.m.cleanup(dry_run=False, manual=True, plan_id=old)
    assert err.value.code == "plan_expired" and b.exists()
    e.now = NOW
    before_restart = e.m.cleanup(dry_run=True, manual=True)["plan_id"]
    restarted = Maintenance(e.ctx, clock=lambda: e.now, free_fn=lambda _p: e.free)
    for m, pid in ((restarted, before_restart), (e.m, "not-a-preview"), (e.m, "")):
        with pytest.raises(PlanError) as err:
            m.cleanup(dry_run=False, manual=True, plan_id=pid)
        assert err.value.code == "plan_unknown" and b.exists()


def test_two_confirmations_at_once_run_the_plan_once(tmp_path):
    e = Env(tmp_path)
    for i in (1, 2, 3):
        e.clip(i, 1, posted_days_ago=1)
    pid = e.m.cleanup(dry_run=True, manual=True)["plan_id"]
    gate, outcomes = threading.Barrier(2), []

    def confirm():
        gate.wait()
        try:
            outcomes.append(e.m.cleanup(dry_run=False, manual=True, plan_id=pid)["done"])
        except PlanError as err:
            outcomes.append(err.code)
    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes, key=str) == [3, "plan_used"]


def test_http_confirm_runs_the_preview_once_and_refuses_what_it_does_not_know(tmp_path):
    ctx = make_ctx()
    root = tmp_path / "s"
    ctx.settings.set("storage.dir", str(root))
    ctx.settings.set("storage.cap_gb", 50)
    d = root / "clips" / "clip9"
    d.mkdir(parents=True)
    (d / "final_en.mp4").write_bytes(b"z" * MB)
    with client(ctx, MaintenanceService(free_fn=lambda _p: 100 * GB)) as c:
        ctx.bus.publish("edit.clip_ready", {"clip_id": 9, "path": str(d / "final_en.mp4")})
        ctx.bus.publish("publish.delivery", {"delivery_id": "a", "clip_id": 9, "destination": "youtube",
                                             "state": "published"})
        bad = c.post("/api/storage/cleanup", json={"dry_run": False, "plan_id": "not-a-preview"})
        assert bad.status_code == 409 and bad.json()["detail"]["code"] == "plan_unknown" and d.exists()
        assert "Preview again" in bad.json()["detail"]["message"]
        pre = c.post("/api/storage/cleanup", json={"dry_run": True}).json()
        assert [a["path"] for a in pre["actions"]] == ["clips/clip9"] and pre["plan_id"] and d.exists()
        done = c.post("/api/storage/cleanup", json={"dry_run": False, "plan_id": pre["plan_id"]}).json()
        assert done["done"] == 1 and done["freed_bytes"] == MB and not d.exists()
        assert [(r["path"], r["outcome"]) for r in done["results"]] == [("clips/clip9", "done")]
        again = c.post("/api/storage/cleanup", json={"dry_run": False, "plan_id": pre["plan_id"]})
        assert again.status_code == 409 and again.json()["detail"]["code"] == "plan_used"
        assert c.get("/api/storage").json()["breakdown"]["clips"] == 0


# ---- a manual deletion always names its preview; only the timer runs without one

def test_a_manual_run_without_a_preview_is_refused_and_the_timer_still_runs(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 50, "storage.retention_days": 7})
    old = e.clip(1, 2, posted_days_ago=8)                          # past the retention time
    fresh = e.clip(2, 2, posted_days_ago=1)                        # posted: a confirmed press would take it
    temp = _old(e.root / "tmp" / "hook_1.txt")
    with pytest.raises(PlanError) as err:
        e.m.cleanup(dry_run=False, manual=True)
    assert err.value.code == "plan_required" and "Preview required" in str(err.value)
    assert old.exists() and fresh.exists() and temp.exists()      # nothing was scanned into a deletion
    out = e.m.cycle()                                              # the timer: its own rules, no preview
    assert out["done"] == 2 and not temp.exists() and not old.exists() and fresh.exists()
    assert [p.name for p in (e.root / "trash").iterdir()] == [f"1-{int(NOW)}"]


def test_http_refuses_a_deleting_request_without_a_preview_and_changes_nothing(tmp_path):
    ctx = make_ctx()
    root = tmp_path / "s"
    ctx.settings.set("storage.dir", str(root))
    d = root / "clips" / "clip9"
    d.mkdir(parents=True)
    (d / "final_en.mp4").write_bytes(b"z" * MB)
    with client(ctx, MaintenanceService(free_fn=lambda _p: 100 * GB)) as c:
        ctx.bus.publish("edit.clip_ready", {"clip_id": 9, "path": str(d / "final_en.mp4")})
        ctx.bus.publish("publish.delivery", {"delivery_id": "a", "clip_id": 9, "destination": "youtube",
                                             "state": "published"})
        before = c.get("/api/storage").json()
        for body in ({"dry_run": False}, {"dry_run": False, "plan_id": None}):
            r = c.post("/api/storage/cleanup", json=body)
            assert r.status_code == 409 and r.json()["detail"]["code"] == "plan_required"
            assert r.json()["detail"]["message"].startswith("Preview required")
        assert (d / "final_en.mp4").stat().st_size == MB
        assert c.get("/api/storage").json()["breakdown"] == before["breakdown"]


# ---- a folder another kept clip uses is never cleaned, by the button or by the automatic passes

def _share(e, folder, cid, how):
    """Track another unposted clip at the same folder or at one of the files inside it."""
    if how == "same folder":
        e.ctx.db.execute("INSERT OR IGNORE INTO maintenance_clip(clip_id, unit_path, created_at) "
                         "VALUES(?,?,?)", (str(cid), str(folder), e.now))
    else:
        f = folder / f"final_{cid}.mp4"
        f.write_bytes(b"s" * MB)
        e.m.record_clip(cid, str(f))


@pytest.mark.parametrize("how", ["same folder", "file inside"])
def test_a_folder_another_kept_clip_shares_is_never_cleaned_by_any_pass(tmp_path, how):
    e = Env(tmp_path, **{"storage.cap_gb": 0.01, "storage.retention_days": 1})
    d = e.clip(1, 15, posted_days_ago=30, original_mb=4)           # posted, past retention, over the cap
    _share(e, d, 7, how)
    alone = e.clip(3, 2, posted_days_ago=30)                       # an unshared posted clip is still cleaned
    files = sorted(p.name for p in d.iterdir())
    for acts in (e.m.plan(), e.m.plan(manual=True)):
        assert not [a for a in acts if a.clip_id in ("1", "7")] and ("delete_clip", "3") in kinds(acts)
    forced = [Action("delete_original", str(d / "source.ts"), "1", 4 * MB, "disk cap reached"),
              Action("trash_clip", str(d), "1", 0, "posted more than 1 days ago"),
              Action("delete_clip", str(d), "1", 15 * MB, "disk cap reached"),
              Action("delete_clip", str(d), "1", 15 * MB, "you asked to clean up: posted")]
    res = e.m.execute(forced)                                      # even if a plan listed it, nothing goes
    assert res["done"] == 0
    assert {r["why"] for r in res["results"]} == {"the folder is shared with another clip"}
    pre = e.m.cleanup(dry_run=True, manual=True)
    assert {x["clip_id"] for x in pre["actions"]} == {"3"}
    out = e.m.cleanup(dry_run=False, manual=True, plan_id=pre["plan_id"])
    assert out["done"] == 1 and not alone.exists()
    assert e.m.cycle()["done"] == 0  # The timer also keeps it while over the cap.
    assert e.m.release_clip("1", "rejected")["removed"] is False   # and so does the Reject path
    assert sorted(p.name for p in d.iterdir()) == files and (d / "source.ts").exists()
    assert e.m.report()["protected_bytes"] >= 15 * MB


@pytest.mark.parametrize("how", ["same folder", "file inside"])
def test_a_clip_that_starts_sharing_after_the_preview_keeps_the_folder(tmp_path, how):
    e = Env(tmp_path)
    d = e.clip(1, 2, posted_days_ago=1, original_mb=1)
    other = e.clip(2, 2, posted_days_ago=1)
    pre = e.m.cleanup(dry_run=True, manual=True)
    assert sorted(x["clip_id"] for x in pre["actions"]) == ["1", "2"]
    _share(e, d, 7, how)                                           # between the preview and the confirmation
    out = e.m.cleanup(dry_run=False, manual=True, plan_id=pre["plan_id"])
    assert out["done"] == 1 and not other.exists()
    assert (d / "source.ts").exists() and (d / "final_en.mp4").exists()
    assert [(r["clip_id"], r["outcome"]) for r in out["results"] if r["clip_id"] == "1"] == [("1", "kept")]


def test_the_execution_recheck_keeps_a_folder_shared_after_the_fresh_plan(tmp_path, monkeypatch):
    """A clip that starts using the folder between the fresh plan and the removal itself still keeps it."""
    e = Env(tmp_path)
    d = e.clip(1, 2, posted_days_ago=1)
    pre = e.m.cleanup(dry_run=True, manual=True)
    real_plan = Maintenance.plan

    def plan_then_share(self, *a, **kw):
        acts = real_plan(self, *a, **kw)
        _share(e, d, 7, "same folder")
        return acts
    monkeypatch.setattr(Maintenance, "plan", plan_then_share)
    out = e.m.cleanup(dry_run=False, manual=True, plan_id=pre["plan_id"])
    assert out["done"] == 0 and (d / "final_en.mp4").exists()
    assert out["results"][0]["why"] == "the folder is shared with another clip"


def test_a_settled_delivery_to_an_unknown_destination_alone_is_not_a_post(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 0.01, "storage.retention_days": 1})
    d = e.clip(1, 15, posted_days_ago=30, dest="")                 # the bus named no provider
    assert e.m.posted_at("1") is None and e.m.plan() == [] and e.m.plan(manual=True) == []
    assert e.m.cycle()["done"] == 0 and d.exists()
    db = e.ctx.db

    def deliver(dest_id, key, days_ago):
        db.execute("INSERT INTO publish_deliveries(clip_id, destination_id, language, idempotency_key, "
                   "status, created_at, updated_at) VALUES(1,?,'en',?,'published',?,?)",
                   (dest_id, key, NOW - days_ago * DAY, NOW - days_ago * DAY))

    deliver(999, "k-gone", 30)                                     # its destination row no longer exists
    assert e.m.posted_at("1") is None and e.m.plan() == [] and e.m.cycle()["done"] == 0 and d.exists()
    youtube = db.execute("INSERT INTO publish_destinations(provider, label, created_at) "
                         "VALUES('youtube','youtube',0)").lastrowid
    deliver(youtube, "k-yt", 29)                                   # a real post as well: now it counts
    assert e.m.posted_at("1") == NOW - 29 * DAY and ("delete_clip", "1") in kinds(e.m.plan())


# ---- a clip being captured, cut or rendered keeps its files, by every pass, until that work finishes

class Task:
    """A clip task as the engines keep it (asyncio.Task's done())."""

    def __init__(self):
        self.finished = False

    def done(self):
        return self.finished


class FakeEdit:
    """The edit engine as maintenance asks it: clip_tasks, busy(clip_id) (EditEngine's rule) and clip_dir."""

    def __init__(self, clips_dir):
        self.clip_tasks, self.clips_dir = {}, clips_dir

    def busy(self, clip_id):
        t = self.clip_tasks.get(clip_id)
        return t is not None and not t.done()

    def clip_dir(self, clip_id):
        return self.clips_dir / str(clip_id)


def engines(e):
    """The edit and monitor services with their engines, registered as in the running app."""
    edit, monitor = FakeEdit(e.root / "clips"), types.SimpleNamespace(clip_tasks={})
    e.ctx.services.extend([types.SimpleNamespace(name="edit", engine=edit),
                           types.SimpleNamespace(name="monitor", engine=monitor)])
    return edit, monitor


def rejected_clip(e, mb=2):
    """A clip the user rejected, with its own folder, tracked as the app tracks it."""
    s = ClipStore(e.ctx.db)
    cid = s.create_clip(s.add_channel("kick", "streamer_alpha"), "rejected-1", "chat_spike",
                        {"review": {"gameplay": {"state": "rejected"}}})
    d = e.root / "clips" / str(cid)
    d.mkdir(parents=True)
    (d / "final_en.mp4").write_bytes(b"r" * (mb * MB))
    e.m.record_clip(cid, str(d / "final_en.mp4"))
    return cid, d


def test_a_clip_being_rendered_is_never_listed_and_is_eligible_again_when_it_finishes(tmp_path):
    e = Env(tmp_path, **{"storage.cap_gb": 0.01, "storage.retention_days": 1})
    edit, monitor = engines(e)
    posted = e.clip(11, 15, posted_days_ago=30, original_mb=4)    # posted, past retention, over the cap
    rid, rejected = rejected_clip(e)
    temp = _old(posted / "hook_en_1_2.txt")                        # the running render's own hook file
    edit.clip_tasks[11] = Task()                                   # a re-render of the posted clip
    monitor.clip_tasks[rid] = Task()  # Capture is still running.
    assert e.m.plan() == [] and e.m.plan(manual=True) == []
    pre = e.m.cleanup(dry_run=True, manual=True)
    assert pre["actions"] == [] and e.m.report()["protected_bytes"] >= 17 * MB
    assert e.m.cycle()["done"] == 0                                # the timer keeps them too
    held = e.m.reject_clip(rid)
    assert held == {"removed": False, "freed_bytes": 0, "kept_because": "it is being rendered now"}
    assert (posted / "source.ts").exists() and (posted / "final_en.mp4").exists() and temp.exists()
    assert (rejected / "final_en.mp4").exists()
    edit.clip_tasks[11].finished = True                            # finished, not yet dropped: busy() says no
    del monitor.clip_tasks[rid]
    pre = e.m.cleanup(dry_run=True, manual=True)
    assert sorted((x["action"], x["clip_id"] or "") for x in pre["actions"]) == sorted([
        ("delete_clip", "11"), ("delete_clip", str(rid)), ("delete_original", "11"), ("delete_temp", "")])
    out = e.m.cleanup(dry_run=False, manual=True, plan_id=pre["plan_id"])
    assert out["done"] == 4 and not posted.exists() and not rejected.exists()


def test_a_render_that_starts_after_the_preview_keeps_the_confirmed_clips(tmp_path):
    e = Env(tmp_path)
    edit, monitor = engines(e)
    d = e.clip(11, 2, posted_days_ago=1, original_mb=1)
    rid, rejected = rejected_clip(e)
    temp = _old(d / "audio_pre.wav")
    pre = e.m.cleanup(dry_run=True, manual=True)
    assert sorted((x["action"], x["clip_id"] or "") for x in pre["actions"]) == sorted([
        ("delete_clip", "11"), ("delete_clip", str(rid)), ("delete_temp", "")])
    edit.clip_tasks[11] = Task()                                   # both start before the confirmation
    monitor.clip_tasks[rid] = Task()
    out = e.m.cleanup(dry_run=False, manual=True, plan_id=pre["plan_id"])
    assert out["done"] == 0 and {r["why"] for r in out["results"]} == {"it is no longer eligible"}
    assert (d / "source.ts").exists() and temp.exists() and (rejected / "final_en.mp4").exists()
    del edit.clip_tasks[11], monitor.clip_tasks[rid]  # Both jobs have finished.
    assert confirm(e.m)["done"] == 3 and not d.exists() and not rejected.exists()


def test_the_execution_recheck_keeps_files_a_render_starts_using_after_the_fresh_plan(tmp_path, monkeypatch):
    e = Env(tmp_path)
    edit, _ = engines(e)
    d = e.clip(11, 2, posted_days_ago=1, original_mb=1)
    temp = _old(d / "final_en.1.2.part.mp4")
    pre = e.m.cleanup(dry_run=True, manual=True)
    real_plan = Maintenance.plan

    def plan_then_render(self, *a, **kw):
        acts = real_plan(self, *a, **kw)
        edit.clip_tasks[11] = Task()
        return acts
    monkeypatch.setattr(Maintenance, "plan", plan_then_render)
    out = e.m.cleanup(dry_run=False, manual=True, plan_id=pre["plan_id"])
    assert out["done"] == 0 and {r["why"] for r in out["results"]} == {"it is being rendered now"}
    forced = [Action("delete_temp", str(temp), None, 1, "temporary render file"),
              Action("delete_original", str(d / "source.ts"), "11", MB, "disk cap reached"),
              Action("trash_clip", str(d), "11", 0, "posted more than 1 days ago"),
              Action("delete_clip", str(d), "11", 2 * MB, "disk cap reached")]
    res = e.m.execute(forced)  # Automatic actions are rechecked, too.
    assert res["done"] == 0 and {r["why"] for r in res["results"]} == {"it is being rendered now"}
    assert temp.exists() and (d / "source.ts").exists() and (d / "final_en.mp4").exists()


def test_an_engine_that_cannot_be_asked_keeps_every_clip(tmp_path):
    e = Env(tmp_path)
    d = e.clip(11, 2, posted_days_ago=1)
    temp = _old(e.root / "tmp" / "hook_1.txt")
    engine = types.SimpleNamespace(clip_tasks={11: Task()})
    e.ctx.services.append(types.SimpleNamespace(name="edit", engine=engine))
    assert e.m.plan(manual=True) == [] and confirm(e.m)["done"] == 0 and d.exists() and temp.exists()
