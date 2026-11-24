"""What the owner asked for: pressing Reject or Clean up removes the clip files that are eligible and frees the
disk, while a clip that is waiting, in flight, shared, approved or not decided yet keeps its files, and the rows
that prove what was sent (deliveries, claims, clip rows) stay.

Real clip store, real publisher with the local sandbox as provider, real files on the real disk; every clip,
channel and byte is synthetic."""
import os
import shutil
import types
from pathlib import Path

from publish_util import World, make_clip, no_keyring, sb  # noqa: F401

from live_clipper.maintenance import MB, Maintenance, _size
from live_clipper.store import Store as ClipStore


class Rig:
    def __init__(self, sb, tmp_path):
        self.w = World(sb)
        self.db = self.w.ctx.db
        self.root = tmp_path / "store"
        self.root.mkdir()
        self.w.ctx.settings.set("storage.dir", str(self.root))
        self.w.ctx.settings.set("storage.cap_gb", 50)
        self.m = Maintenance(self.w.ctx)
        self.clips = ClipStore(self.db)
        self.channel = self.clips.add_channel("kick", "streamer_alpha", "https://kick.com/streamer_alpha")
        self.w.dest(provider="youtube")
        self.w.ctx.services.append(types.SimpleNamespace(name="publish", pub=self.w.pub))
        self.n = 0

    def clip(self, mb=3, review=None, files=("final_en.mp4", "preview.mp4", "source.ts")) -> int:
        """A finished clip with its folder, tracked the way the app tracks a clip that reached posting."""
        self.n += 1
        cid = self.clips.create_clip(self.channel, f"k{self.n}", "chat_spike", {}, action_time=1000.0 + 900 * self.n,
                                     window_start=980.0 + 900 * self.n, window_end=1010.0 + 900 * self.n,
                                     status="ready")
        d = self.root / "clips" / str(cid)
        d.mkdir(parents=True)
        for name in files:
            (d / name).write_bytes(b"\x00\x00\x00\x18ftypmp42" + os.urandom(mb * MB // len(files)))
        if review:
            self.clips.merge_json("clips", cid, "trigger", {"review": {"gameplay": {"state": review}}})
        self.m.record_clip(cid, str(d / "final_en.mp4"))
        return cid

    def deliver(self, cid: int) -> int:
        c = make_clip(self.w.ctx, channel_id=str(self.channel))
        c.update(clip_id=cid, path=str(self.root / "clips" / str(cid) / "final_en.mp4"))
        return self.w.pub.enqueue_clip(c)[0]

    def folder(self, cid: int) -> Path:
        return self.root / "clips" / str(cid)

    def audit(self, cid: int) -> dict:
        return {"clip": bool(self.clips.clip(cid)),
                "deliveries": self.db.execute("SELECT COUNT(*) FROM publish_deliveries WHERE clip_id=?",
                                              (cid,)).fetchone()[0],
                "tracked": [tuple(r) for r in self.db.execute(
                    "SELECT state FROM maintenance_clip WHERE clip_id=?", (str(cid),))]}


def test_reject_removes_the_files_frees_the_disk_and_keeps_the_audit_rows(sb, tmp_path):
    r = Rig(sb, tmp_path)
    cid = r.clip(mb=6)
    other = r.clip(mb=6)                                       # a neighbour that must not be touched
    before = _size(r.root)
    free = shutil.disk_usage(r.root).free
    out = r.m.reject_clip(cid)
    assert out["removed"] is True and out["freed_bytes"] >= 6 * MB - 1024
    assert not r.folder(cid).exists() and r.folder(other).exists()
    assert before - _size(r.root) == out["freed_bytes"]        # measured on the disk, not computed
    assert shutil.disk_usage(r.root).free >= free              # nothing was taken, space was given back
    assert r.audit(cid) == {"clip": True, "deliveries": 0, "tracked": [("deleted",)]}
    assert r.clips.clip(cid)["trigger"]["media_removed"]["why"] == "rejected"


def test_reject_cancels_what_was_only_waiting_and_then_removes_the_files(sb, tmp_path):
    r = Rig(sb, tmp_path)
    cid = r.clip()
    did = r.deliver(cid)                                       # queued, the provider was never contacted
    out = r.m.reject_clip(cid)
    assert out["removed"] is True and not r.folder(cid).exists()
    row = r.w.row(did)
    assert row["status"] == "cancelled" and row["attempts"] == 0   # the delivery row stays as the audit
    r.w.drain(horizon=0)
    assert r.w.row(did)["status"] == "cancelled"               # and nothing is sent afterwards


def test_reject_keeps_the_files_while_an_upload_may_still_need_them(sb, tmp_path):
    r = Rig(sb, tmp_path)
    cid = r.clip()
    did = r.deliver(cid)
    r.w.store.update("deliveries", did, status="uncertain", reconcile=1)
    out = r.m.reject_clip(cid)
    assert out["removed"] is False and "upload" in out["kept_because"] and r.folder(cid).exists()
    assert r.w.row(did)["status"] == "uncertain"               # not cancelled, not pretended finished


def test_reject_removes_a_clip_that_was_already_posted_and_never_touches_the_post(sb, tmp_path):
    r = Rig(sb, tmp_path)
    cid = r.clip()
    did = r.deliver(cid)
    r.w.drain(horizon=0)
    assert r.w.row(did)["status"] == "published"
    assert r.m.reject_clip(cid)["removed"] is True and not r.folder(cid).exists()
    assert r.w.row(did)["status"] == "published"


def test_reject_never_touches_a_folder_another_clip_shares_or_one_outside_the_store(sb, tmp_path):
    r = Rig(sb, tmp_path)
    a, b = r.clip(), r.clip()
    r.db.execute("UPDATE maintenance_clip SET unit_path=? WHERE clip_id=?", (str(r.folder(a)), str(b)))
    out = r.m.reject_clip(a)
    assert out["removed"] is False and "shared" in out["kept_because"] and r.folder(a).exists()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.mp4").write_bytes(b"x" * 1024)
    c = r.clip()
    r.db.execute("UPDATE maintenance_clip SET unit_path=? WHERE clip_id=?", (str(outside), str(c)))
    shutil.rmtree(r.folder(c))
    assert r.m.reject_clip(c)["removed"] is False and (outside / "x.mp4").exists()


def test_cleanup_pressed_by_the_user_removes_posted_and_rejected_clips_at_once_and_keeps_the_rest(sb, tmp_path):
    r = Rig(sb, tmp_path)
    posted = r.clip(mb=4)
    r.deliver(posted)
    r.w.drain(horizon=0)
    rejected = r.clip(mb=4, review="rejected")                 # never queued: tracked only by its folder
    approved = r.clip(mb=4, review="approved")                 # approved, not posted yet
    waiting = r.clip(mb=4)
    waiting_delivery = r.deliver(waiting)
    r.w.store.update("deliveries", waiting_delivery, next_attempt_at=time_far())   # stays queued
    undecided = r.clip(mb=4)                                   # no decision, no delivery
    before = _size(r.root)
    preview = r.m.cleanup(dry_run=True, manual=True)
    assert {a["clip_id"] for a in preview["actions"] if a["action"] == "delete_clip"} == {str(posted), str(rejected)}
    assert _size(r.root) == before                             # a preview deletes nothing
    done = r.m.cleanup(dry_run=False, manual=True)
    assert done["done"] >= 2 and not r.folder(posted).exists() and not r.folder(rejected).exists()
    for kept in (approved, waiting, undecided):
        assert r.folder(kept).exists() and len(list(r.folder(kept).iterdir())) == 3
    assert before - _size(r.root) == done["freed_bytes"] >= 8 * MB - 4096
    assert r.audit(posted)["deliveries"] == 1 and r.audit(posted)["clip"] is True


def test_the_background_pass_keeps_its_retention_rules_and_only_the_press_is_immediate(sb, tmp_path):
    r = Rig(sb, tmp_path)
    posted = r.clip()
    r.deliver(posted)
    r.w.drain(horizon=0)
    rejected = r.clip(review="rejected")
    r.m.cycle()                                                # what runs every minute by itself
    assert r.folder(posted).exists() and r.folder(rejected).exists()


def test_clip_folders_the_app_never_tracked_are_found_by_the_press(sb, tmp_path):
    """Clips that were held or rejected by selection never reached posting, so nothing tracked their folder."""
    r = Rig(sb, tmp_path)
    cid = r.clip(review="rejected")
    r.db.execute("DELETE FROM maintenance_clip")
    assert r.m.cleanup(dry_run=False, manual=True)["done"] == 1 and not r.folder(cid).exists()


def time_far() -> float:
    import time
    return time.time() + 86400


def test_the_real_buttons_through_the_app_reject_then_clean_up(sb, tmp_path):
    """Reject and Clean up now over HTTP, in a running app with the edit and storage services: bytes and references
    before and after, on real files in a temporary store."""
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    from live_clipper.edit_service import EditService
    from live_clipper.maintenance import MaintenanceService
    from live_clipper.server import create_app
    r = Rig(sb, tmp_path)
    a, b = r.clip(mb=5), r.clip(mb=5)
    ctx = r.w.ctx
    class Publishing:                                      # the publish service, minus its routes
        name, router = "publish", APIRouter()
        pub = r.w.pub

        async def start(self, ctx): ...
        async def stop(self): ...
    ctx.services[:] = [EditService(), MaintenanceService(), Publishing()]
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8765",
                    headers={"Authorization": f"Bearer {ctx.token}"}) as c:
        before = c.get("/api/storage").json()["breakdown"]["clips"]
        res = c.post(f"/api/clips/{a}/review", json={"approved": False}).json()
        assert res["media"]["removed"] is True and res["media"]["freed_bytes"] >= 5 * MB - 1024
        assert not r.folder(a).exists() and r.folder(b).exists()
        assert before - c.get("/api/storage").json()["breakdown"]["clips"] == res["media"]["freed_bytes"]
        approved = c.post(f"/api/clips/{b}/review", json={"approved": True}).json()
        assert approved.get("media") is None and r.folder(b).exists()          # approving removes nothing
        r.clips.merge_json("clips", b, "trigger", {"review": {"gameplay": {"state": "rejected"}}})
        preview = c.post("/api/storage/cleanup", json={"dry_run": True}).json()
        assert [x["clip_id"] for x in preview["actions"]] == [str(b)] and r.folder(b).exists()
        done = c.post("/api/storage/cleanup", json={"dry_run": False}).json()
        assert done["done"] == 1 and done["freed_bytes"] >= 5 * MB - 1024 and not r.folder(b).exists()


# ---- the owner's setup: a separate sender (Metricool bridge) shares the claims table and may still need a clip
def with_outside_sender(r):
    from test_publish_cadence import LEGACY_SCHEMA
    for stmt in LEGACY_SCHEMA:
        r.db.execute(stmt)
    from live_clipper.publish.cadence import Cadence
    r.w.pub.cadence = Cadence(r.db)                            # the app as released: it writes its claim row


def qualify(r, cid):
    r.clips.merge_json("clips", cid, "trigger", {"selection": {"en": {"decision": "qualified", "reasons": []}}})


def outside_claim(r, cid, network, pending, owner=None):
    r.db.execute("INSERT INTO local_english_claims VALUES(?,?,?,?,?,?,?,1,?)",
                 (owner or f"metricool:{cid}:{network}", network, f"channel:{r.channel}", f"m{cid}", cid, "ab" * 32,
                  1000.0, pending))


def test_a_clip_the_outside_sender_may_still_post_is_not_cleaned_because_youtube_succeeded(sb, tmp_path):
    r = Rig(sb, tmp_path)
    with_outside_sender(r)
    cid = r.clip()
    qualify(r, cid)
    r.deliver(cid)
    r.w.drain(horizon=0)                                       # native YouTube settled
    assert r.m.cleanup(dry_run=False, manual=True)["done"] == 0 and r.folder(cid).exists()
    outside_claim(r, cid, "instagram", pending=0)              # Instagram settled, TikTok still open
    assert r.m.cleanup(dry_run=False, manual=True)["done"] == 0 and r.folder(cid).exists()
    outside_claim(r, cid, "tiktok", pending=0)                 # both settled: nothing outside needs it any more
    assert r.m.cleanup(dry_run=False, manual=True)["done"] == 1 and not r.folder(cid).exists()


def test_a_clip_the_outside_sender_has_claimed_and_not_confirmed_is_never_removed(sb, tmp_path):
    r = Rig(sb, tmp_path)
    with_outside_sender(r)
    cid = r.clip()
    r.deliver(cid)
    r.w.drain(horizon=0)
    outside_claim(r, cid, "tiktok", pending=1)                 # submitted, receipt not confirmed yet
    assert r.m.cleanup(dry_run=False, manual=True)["done"] == 0
    out = r.m.reject_clip(cid)
    assert out["removed"] is False and "outside sender" in out["kept_because"] and r.folder(cid).exists()
    assert r.m.cycle()["done"] == 0                            # nor does the background pass take it


def test_reject_is_visible_to_the_outside_sender_before_it_can_submit(sb, tmp_path):
    """The sender selects clips whose selection decision is qualified and whose file exists: Reject changes both."""
    r = Rig(sb, tmp_path)
    with_outside_sender(r)
    cid = r.clip()
    qualify(r, cid)
    query = ("SELECT json_extract(trigger,'$.selection.en.decision') FROM monitor_clips WHERE id=?")
    assert r.db.execute(query, (cid,)).fetchone()[0] == "qualified"
    out = r.m.reject_clip(cid)
    assert out["removed"] is True and r.db.execute(query, (cid,)).fetchone()[0] == "rejected"
    assert not r.folder(cid).exists()


def test_a_test_destination_success_never_makes_a_clip_removable(sb, tmp_path):
    r = Rig(sb, tmp_path)
    test_dest = r.w.dest()                                     # provider "test"
    cid = r.clip()
    c = make_clip(r.w.ctx, channel_id=str(r.channel))
    c.update(clip_id=cid, path=str(r.folder(cid) / "final_en.mp4"))
    did = r.w.pub._insert(c, r.w.store.q1("SELECT * FROM publish_destinations WHERE id=?", test_dest), "en", "auto", 1)
    r.w.pub.store.save_clip(c)
    r.w.drain(horizon=0)
    assert r.w.row(did)["status"] == "published"
    assert r.m.cleanup(dry_run=False, manual=True)["done"] == 0 and r.folder(cid).exists()


def test_a_delete_that_fails_part_way_reports_only_what_is_really_gone(sb, tmp_path, monkeypatch):
    r = Rig(sb, tmp_path)
    cid = r.clip(mb=6)
    d = r.folder(cid)
    real_rm = Maintenance._rm

    def partial(self, p):                                      # one file is in use (the sender is reading it)
        for f in sorted(p.iterdir()):
            if f.name != "final_en.mp4":
                f.unlink()
    monkeypatch.setattr(Maintenance, "_rm", partial)
    before = _size(d)
    out = r.m.reject_clip(cid)
    left = _size(d)
    assert out["removed"] is False and out["freed_bytes"] == before - left > 0 and "in use" in out["kept_because"]
    assert [row[0] for row in r.db.execute("SELECT state FROM maintenance_clip WHERE clip_id=?", (str(cid),))] == ["kept"]
    monkeypatch.setattr(Maintenance, "_rm", real_rm)
    assert r.m.reject_clip(cid)["removed"] is True and not d.exists()      # and a retry finishes the job
