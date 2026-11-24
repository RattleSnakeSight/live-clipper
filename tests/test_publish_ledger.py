"""The claims table of a 0.3.0 installation that was patched by hand, across the update: one file per network by
its SHA-256, the file read before the claim and never under the database write lock, claims the old version
left open, an outside publisher writing to the same table from its own connection, retries, and what stays off.

Real worker, real clip store, the local sandbox as provider; every row and file is synthetic."""
import hashlib
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from publish_util import admin, make_clip, make_ctx, no_keyring, sb, videos  # noqa: F401
from test_publish_cadence import GAP, LEGACY_SCHEMA, Studio

from live_clipper import moments
from live_clipper.publish.cadence import Cadence
from live_clipper.publish.sandbox_server import shutdown_sandbox
from live_clipper.publish.service import PublishService
from live_clipper.server import create_app

HEAD = b"\x00\x00\x00\x18ftypmp42"


def patched(sb) -> Studio:
    """A studio whose database carries the old table and trigger, as that installation's does."""
    s = Studio(sb)
    for stmt in LEGACY_SCHEMA:
        s.w.ctx.db.execute(stmt)
    return s


def sha(clip: dict) -> str:
    return hashlib.sha256(Path(clip["path"]).read_bytes()).hexdigest()


def claim(s: Studio, owner: str):
    return s.w.ctx.db.execute("SELECT * FROM local_english_claims WHERE owner=?", (owner,)).fetchone()


def said(d: dict) -> str:
    return d["history"][-1]["detail"]


def test_the_file_is_read_once_before_the_claim_and_never_under_the_write_lock(sb, monkeypatch):
    s = patched(sb)
    s.w.pub.factories["tiktok"] = s.w.pub.factories["test"]
    s.w.dest(provider="youtube")
    s.w.dest(provider="tiktok")
    alpha = s.streamer("streamer_alpha")
    other = sqlite3.connect(s.w.ctx.db.path, isolation_level=None, timeout=0)   # another program's connection
    reads, real = [], hashlib.file_digest

    def watched(f, *a, **k):
        try:                                           # could another program write while the file is read?
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            reads.append("free")
        except sqlite3.OperationalError:
            reads.append("write-locked")
        return real(f, *a, **k)
    monkeypatch.setattr(hashlib, "file_digest", watched)
    clip = s.clip(alpha)
    sent = s.send(clip)
    other.close()
    assert [d["status"] for d in sent] == ["published", "published"], [said(d) for d in sent]
    assert reads == ["free"]                           # one read for both accounts, with the database free
    assert {claim(s, f"native:{d['id']}")["digest"] for d in sent} == {sha(clip)}


def test_a_file_is_refused_on_a_network_that_has_it_whatever_clip_moment_or_streamer_carries_it(sb):
    s = patched(sb)
    s.w.dest(provider="youtube")
    alpha, beta = s.streamer("streamer_alpha"), s.streamer("streamer_beta")
    db = s.w.ctx.db
    first = s.send(s.clip(alpha))[0]
    assert first["status"] == "published"
    # older posts keep their row but stop holding the streamer's interval, as on the patched installation
    db.execute("UPDATE local_english_claims SET occupies=0 WHERE owner=?", (f"native:{first['id']}",))
    twin = s.clip(alpha, at=s.clock.t + 9000)                              # another clip, another moment
    twin["path"] = s.w.store.clip(first["clip_id"])["path"]
    s.clock.t += GAP + 120
    refused = s.send(twin)[0]
    assert refused["status"] == "disallowed" and "same file" in said(refused) and len(videos(sb)) == 1
    # an outside publisher's row for a file (a quarantined send: no interval held), under another streamer's name
    theirs = s.clip(beta)
    db.execute("INSERT INTO local_english_claims VALUES('outside:3','youtube','channel:900','m900',900,?,?,0,0)",
               (sha(theirs), s.clock.t - 9 * GAP))
    refused = s.send(theirs)[0]
    assert refused["status"] == "disallowed" and "same file" in said(refused) and len(videos(sb)) == 1
    # the same row on another network does not stop this one, and a file nobody claimed goes out
    db.execute("UPDATE local_english_claims SET network='tiktok' WHERE owner='outside:3'")
    assert s.send(s.clip(beta))[0]["status"] == "published" and len(videos(sb)) == 2


def test_an_outside_row_for_the_moment_is_a_duplicate_even_when_it_holds_no_interval(sb):
    s = patched(sb)
    s.w.dest(provider="youtube")
    alpha = s.streamer("streamer_alpha")
    clip = s.clip(alpha)
    s.w.ctx.db.execute("INSERT INTO local_english_claims VALUES('outside:4','youtube',?,?,?,'',?,0,0)",
                       (f"channel:{alpha}", moments.moment_id(s.clips, clip["clip_id"]), clip["clip_id"],
                        s.clock.t - 9 * GAP))
    refused = s.send(clip)[0]
    assert refused["status"] == "disallowed" and "one post per moment" in said(refused) and not videos(sb)
    # a row for this very clip counts whatever name the streamer had when it was written (a later simulcast link)
    again = s.clip(s.streamer("streamer_beta"))
    s.w.ctx.db.execute("INSERT INTO local_english_claims VALUES('outside:5','youtube','group:earlier','m-old',?,'',?,0,0)",
                       (again["clip_id"], s.clock.t - 9 * GAP))
    refused = s.send(again)[0]
    assert refused["status"] == "disallowed" and "one post per moment" in said(refused) and not videos(sb)


def test_the_same_moment_still_reaches_a_second_account_of_one_network(sb):
    s = patched(sb)
    s.w.dest(provider="youtube")
    two = s.w.dest(provider="youtube")
    st = s.w.store.q1("SELECT settings FROM publish_destinations WHERE id=?", two)["settings"]
    st["account_info"]["account_id"] = "UC_second_synthetic"               # another channel than the first row's
    s.w.store.update("destinations", two, settings=st)
    sent = s.send(s.clip(s.streamer("streamer_alpha")))
    assert [d["status"] for d in sent] == ["published", "published"], [said(d) for d in sent]
    assert len(videos(sb)) == 2


def test_a_claim_the_old_version_left_open_holds_the_streamer_until_its_delivery_is_retried(sb):
    s = patched(sb)
    s.w.dest(provider="youtube")
    alpha, beta = s.streamer("streamer_alpha"), s.streamer("streamer_beta")
    db = s.w.ctx.db
    lost = s.clip(alpha)
    stuck = s.w.pub.enqueue_clip(lost)[0]
    # what the patched 0.3.0 worker left when the platform refused an upload: a failed delivery, its claim open
    db.execute("INSERT INTO local_english_claims VALUES(?,'youtube',?,?,?,?,?,1,1)",
               (f"native:{stuck}", f"channel:{alpha}", moments.moment_id(s.clips, lost["clip_id"]), lost["clip_id"],
                sha(lost), s.clock.t))
    s.w.store.update("deliveries", stuck, status="failed", attempts=1, reconcile=1, claimed_at=s.clock.t)
    s.clock.t += 3 * GAP
    waiting = s.send(s.clip(alpha))[0]                                     # another moment of that streamer
    assert waiting["status"] == "queued" and waiting["attempts"] == 0 and not videos(sb)
    assert f"delivery {stuck}" in said(waiting) and "Retry" in said(waiting)
    assert s.send(s.clip(beta))[0]["status"] == "published"                # another streamer is not held
    s.w.pub.cadence = Cadence(db)                                          # a restart changes nothing
    s.clock.t += 61
    s.w.drain(horizon=0)
    assert s.w.row(waiting["id"])["status"] == "queued" and len(videos(sb)) == 1
    s.w.pub.retry(stuck)                                                   # the owner settles it: it is sent now
    s.w.drain(horizon=0)
    assert s.w.row(stuck)["status"] == "published" and len(videos(sb)) == 2
    row = claim(s, f"native:{stuck}")
    assert (row["pending"], row["occupies"], row["digest"]) == (0, 1, sha(lost))
    s.clock.t += 61                                                        # the two hours now count from that post
    s.w.drain(horizon=0)
    assert s.w.row(waiting["id"])["status"] == "queued" and "two hours" in said(s.w.row(waiting["id"]))
    s.clock.t += GAP
    s.w.drain(horizon=0)
    assert s.w.row(waiting["id"])["status"] == "published" and len(videos(sb)) == 3


def test_a_file_that_changed_after_it_was_read_is_read_again_before_the_claim(sb):
    s = patched(sb)
    s.w.dest(provider="youtube")
    clip = s.clip(s.streamer("streamer_alpha"))
    did = s.w.pub.enqueue_clip(clip)[0]
    real, once = s.w.pub._claim_upload, []

    def a_new_render_lands_first(did):
        if not once:
            once.append(1)
            Path(clip["path"]).write_bytes(HEAD + os.urandom(3000))
        return real(did)
    s.w.pub._claim_upload = a_new_render_lands_first
    s.w.drain(horizon=0)
    row = s.w.row(did)
    assert row["status"] == "queued" and row["attempts"] == 0 and "read again" in said(row) and not videos(sb)
    assert claim(s, f"native:{did}") is None                               # nothing was claimed with the older hash
    s.clock.t += 3
    s.w.drain(horizon=0)
    row = s.w.row(did)
    assert row["status"] == "published" and row["attempts"] == 1 and len(videos(sb)) == 1
    assert claim(s, f"native:{did}")["digest"] == sha(clip)                # the file that was sent, not the older one


def test_an_upload_the_platform_put_off_keeps_its_claim_and_is_sent_once(sb):
    s = patched(sb)
    s.w.dest(provider="youtube")
    alpha = s.streamer("streamer_alpha")
    clip = s.clip(alpha)
    admin(sb, "/_admin/inject", {"op": "upload", "fail": "429", "retry_after": 30})
    did = s.w.pub.enqueue_clip(clip)[0]
    s.w.drain(horizon=0)
    assert s.w.row(did)["status"] == "queued" and not videos(sb)
    row = claim(s, f"native:{did}")
    assert (row["pending"], row["occupies"], row["digest"]) == (1, 1, sha(clip))
    other = s.send(s.clip(alpha))[0]                                       # held: that upload is not settled
    assert other["status"] == "queued" and "not confirmed" in said(other)
    s.clock.t += 31
    s.w.drain(horizon=0)                                                   # its own claim is not a duplicate of itself
    assert s.w.row(did)["status"] == "published" and len(videos(sb)) == 1
    row = claim(s, f"native:{did}")
    assert (row["pending"], row["occupies"], row["digest"]) == (0, 1, sha(clip))
    assert s.w.ctx.db.execute("SELECT COUNT(*) FROM local_english_claims WHERE clip_id=?",
                              (clip["clip_id"],)).fetchone()[0] == 1


def test_two_clips_with_one_file_due_together_send_it_once(sb):
    s = patched(sb)
    s.w.dest(provider="youtube", extra={"concurrency": 2})
    a, b = s.clip(s.streamer("streamer_alpha")), s.clip(s.streamer("streamer_beta"))
    shutil.copyfile(a["path"], b["path"])                                  # two streamers, two files, the same bytes
    ids = s.w.pub.enqueue_clip(a) + s.w.pub.enqueue_clip(b)
    s.w.drain(horizon=0)                       # both are due in one round: the first claim decides, in the database
    assert sorted(s.w.row(i)["status"] for i in ids) == ["disallowed", "published"] and len(videos(sb)) == 1
    assert s.w.ctx.db.execute("SELECT COUNT(*) FROM local_english_claims").fetchone()[0] == 1


def test_an_outside_publisher_mid_transaction_and_the_native_claim_exclude_each_other(sb):
    s = patched(sb)
    s.w.dest(provider="youtube")
    alpha = s.streamer("streamer_alpha")
    did = s.w.pub.enqueue_clip(s.clip(alpha))[0]
    outside = sqlite3.connect(s.w.ctx.db.path, isolation_level=None, timeout=15)
    outside.execute("BEGIN IMMEDIATE")                                     # the other program is reserving right now
    outside.execute("INSERT INTO local_english_claims VALUES('outside:9','tiktok',?,'m-theirs',9009,'',?,1,1)",
                    (f"channel:{alpha}", s.clock.t))
    real, at_claim, claimed = s.w.pub._claim_upload, threading.Event(), threading.Event()

    def watched(did):
        at_claim.set()
        try:
            return real(did)
        finally:
            claimed.set()
    s.w.pub._claim_upload = watched
    worker = threading.Thread(target=lambda: s.w.drain(horizon=0), daemon=True)
    worker.start()
    assert at_claim.wait(20)                                               # the worker reached its claim ...
    assert not claimed.wait(0.5)                                           # ... and waits there for the database
    row = s.w.row(did)
    assert row["status"] == "queued" and row["attempts"] == 0 and not videos(sb)
    outside.execute("COMMIT")
    assert claimed.wait(20)
    worker.join(20)
    outside.close()
    row = s.w.row(did)                                                     # and then finds the other program's claim
    assert not worker.is_alive() and row["status"] == "queued" and row["attempts"] == 0 and not videos(sb)
    assert "not confirmed" in said(row)
    assert claim(s, f"native:{did}") is None


def test_a_confirmed_post_is_recorded_at_the_time_it_was_confirmed_not_when_its_upload_started(sb):
    s = patched(sb)
    db, t = s.w.ctx.db, s.clock.t
    db.execute("INSERT INTO local_english_claims VALUES('native:77','youtube','channel:1','m1',1,'',?,1,1)", (t,))
    seen = {"id": 77, "status": "published", "claimed_at": t, "created_at": t - 5, "public_at": None,
            "history": [{"t": t, "from": "queued", "to": "uploading"}, {"t": t + 90, "from": "processing",
                                                                         "to": "published"}]}
    Cadence(db).settle(seen)
    row = claim(s, "native:77")
    assert (row["pending"], row["occupies"]) == (0, 1) and row["post_time"] == pytest.approx(t + 90, abs=0.01)
    db.execute("UPDATE local_english_claims SET pending=1, post_time=? WHERE owner='native:77'", (t,))
    Cadence(db).settle({**seen, "public_at": t + 40})                      # the platform's own public time wins
    assert claim(s, "native:77")["post_time"] == pytest.approx(t + 40, abs=0.01)


def test_a_delivery_and_its_claim_change_together_or_not_at_all(sb):
    s = patched(sb)
    s.w.dest(provider="youtube")
    did = s.w.pub.enqueue_clip(s.clip(s.streamer("streamer_alpha")))[0]

    def power_cut(d):
        raise RuntimeError("stopped between the two writes")
    s.w.pub.cadence.settle = power_cut
    with pytest.raises(RuntimeError):
        s.w.pub._set(did, "failed", "the platform refused it")
    assert s.w.row(did)["status"] == "queued"                              # no failed delivery with an open claim
    assert not s.w.ctx.db.conn().in_transaction


def test_a_claim_is_released_as_soon_as_the_provider_shows_no_earlier_post(sb):
    """A delivery that stops being a possible post without ending (the provider has nothing of it) frees the
    streamer for the outside publisher too: the table says what the delivery says."""
    s = patched(sb)
    s.w.dest(provider="youtube")
    did = s.w.pub.enqueue_clip(s.clip(s.streamer("streamer_alpha")))[0]
    s.w.ctx.db.execute("INSERT INTO local_english_claims VALUES(?,'youtube','channel:1','m1',1,'',?,1,1)",
                       (f"native:{did}", s.clock.t))
    s.w.store.update("deliveries", did, reconcile=1)
    s.w.pub._set(did, "queued", "reconcile: provider has no earlier post; safe to upload", reconcile=0)
    row = claim(s, f"native:{did}")
    assert (row["pending"], row["occupies"]) == (0, 0)
    # an upload that may have landed keeps its claim through a switched-off destination, and loses it when cancelled
    s.w.ctx.db.execute("UPDATE local_english_claims SET pending=1, occupies=1 WHERE owner=?", (f"native:{did}",))
    s.w.store.update("deliveries", did, reconcile=1)
    s.w.pub._set(did, "blocked", "destination is disabled", blocked_from="uncertain")
    assert tuple(claim(s, f"native:{did}"))[-2:] == (1, 1)
    s.w.pub._set(did, "cancelled", "cancelled by the user")
    assert tuple(claim(s, f"native:{did}"))[-2:] == (0, 0)


def test_the_upload_fingerprint_keeps_its_form_and_a_missing_file_has_none(sb, monkeypatch):
    """Stored upload sessions are compared with this string across an update: size and the first 16 hex digits
    of the SHA-256, however often it is asked."""
    s = Studio(sb)
    clip = make_clip(s.w.ctx)
    reads, real = [], hashlib.file_digest
    monkeypatch.setattr(hashlib, "file_digest", lambda f, *a, **k: (reads.append(1), real(f, *a, **k))[1])
    want = f"v1:{os.path.getsize(clip['path'])}:{sha(clip)[:16]}"
    assert [s.w.pub.media_fp(clip) for _ in range(3)] == [want] * 3 and len(reads) == 1
    assert s.w.pub.media_digest(clip, read=False) == sha(clip)             # known already: nothing is read
    os.remove(clip["path"])
    assert s.w.pub.media_fp(clip) == "" and s.w.pub.media_digest(clip, read=False) == "" and len(reads) == 1
    other = make_clip(s.w.ctx)
    assert s.w.pub.media_digest(other, read=False) == "" and len(reads) == 1   # not read yet: asked, never read


def test_an_update_keeps_instagram_and_tiktok_off_and_every_setting_where_it_was(tmp_path):
    """The database a 0.3.0 installation hands to the new version: YouTube on, Instagram and TikTok never
    connected and off (an outside publisher owns them), automatic posting chosen. Starting the new version's
    publishing service changes none of it, and a finished clip is queued for YouTube only."""
    ctx = make_ctx()
    db = ctx.db
    now = time.time()
    for prov, status, enabled, ack in (("youtube", "connected", 1, now), ("tiktok", "disconnected", 0, None),
                                       ("instagram", "disconnected", 0, None)):
        db.execute("INSERT INTO publish_destinations(provider,label,settings,status,enabled,rights_ack_at,created_at) "
                   "VALUES(?,?,?,?,?,?,?)", (prov, prov, '{"account_info": {"account_id": "UC_example", '
                                             '"verified_gen": 0}}' if prov == "youtube" else "{}", status, enabled,
                                             ack, now))
    for stmt in LEGACY_SCHEMA:
        db.execute(stmt)
    db.execute("INSERT INTO local_english_claims VALUES('outside:1','instagram','channel:1','m1',1,'',?,1,0)", (now,))
    ctx.settings.set("publish.mode", "auto")
    ctx.settings.set("publish.channel_platforms", {"1": ["youtube", "instagram", "tiktok"]})

    def snapshot():
        return ([tuple(r) for r in db.execute("SELECT provider, status, enabled, rights_ack_at, settings FROM "
                                              "publish_destinations WHERE provider<>'test' ORDER BY id")],
                [tuple(r) for r in db.execute("SELECT key, value_json FROM settings_kv ORDER BY key")],
                [tuple(r) for r in db.execute("SELECT * FROM local_english_claims")],
                [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'local_%' ORDER BY name")])
    before = snapshot()
    svc = PublishService()
    ctx.services[:] = [svc]
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8765") as c:
        c.headers.update({"Authorization": f"Bearer {ctx.token}"})
        listed = {d["provider"]: d for d in c.get("/api/destinations").json()["destinations"]}
    shutdown_sandbox()
    assert snapshot() == before
    ids = svc.pub.enqueue_clip(make_clip(ctx, channel_id="1"))    # the worker is stopped: queued, never sent
    assert (listed["instagram"]["enabled"], listed["tiktok"]["enabled"], listed["youtube"]["enabled"]) == (
        False, False, True)
    queued = [svc.store.q1("SELECT provider FROM publish_destinations WHERE id=(SELECT destination_id FROM "
                           "publish_deliveries WHERE id=?)", i)["provider"] for i in ids]
    assert sorted(set(queued) - {"test"}) == ["youtube"]
